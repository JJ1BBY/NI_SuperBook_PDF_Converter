#!/usr/bin/env python3
"""
yomitoku_multi_export.py

1 回の yomitoku 推論 (DocumentAnalyzer) の結果から、複数フォーマット
(md / html / json / searchable PDF、combined / paged) を一括エクスポートする
ドライバスクリプト。

yomitoku 標準 CLI (yomitoku.cli.main) は 1 回の実行で 1 フォーマットしか出力できず、
フォーマットごとに重い GPU 推論 (text detection + layout 解析 + 文字認識) を
繰り返すことになるため、本スクリプトで「1 推論 → N 出力」を実現する。

各エクスポートのロジックは yomitoku.cli.main の process_single_file /
merge_all_pages / save_merged_file と同一結果になるように実装してある。
出力ファイル名も CLI と互換:
    paged    : {dirname}_{filename}_p{N}.{ext}
    combined : {dirname}_{filename}.{ext}

使用方法:
    venv\\Scripts\\python.exe yomitoku_multi_export.py <job.json>

job.json の例:
{
    "src_path": "C:\\tmp\\ocr\\xxx.pdf",
    "device": "cuda",
    "dpi": 300,
    "lite": false,
    "ignore_meta": true,
    "reading_order": "",
    "tasks": [
        {
            "name": "md_paged",
            "format": "md",
            "outdir": "C:\\tmp\\ocr\\ocrdst_md_paged",
            "combine": false,
            "ignore_line_break": true,
            "encoding": "utf-8",
            "figure": true,
            "figure_letter": true,
            "figure_width": 200
        }
    ]
}

reading_order が空文字の場合は "auto" として扱う。
注意: 分析パラメータ (device / dpi / lite / ignore_meta / reading_order) は
ジョブ全体で 1 セットであり、これらが異なる出力は別ジョブに分ける必要がある。
"""

import json
import os
import re
import sys
import time


def _merge_close_figures(figures, gap_threshold):
    """近接する figure bbox を繰り返しマージして1つにまとめる。

    x / y 両方向のギャップが gap_threshold px 以下の場合に隣接とみなし、
    それらを union bbox で統合する。チェーン (A-B, B-C で A-C が遠くても統合)
    になるよう変化がなくなるまで繰り返す。
    """
    if len(figures) <= 1:
        return figures

    changed = True
    pool = list(figures)

    while changed:
        changed = False
        merged = []
        used = [False] * len(pool)

        for i in range(len(pool)):
            if used[i]:
                continue
            ax1, ay1, ax2, ay2 = pool[i].box
            combined_paragraphs = list(pool[i].paragraphs)
            base_order = pool[i].order

            for j in range(i + 1, len(pool)):
                if used[j]:
                    continue
                bx1, by1, bx2, by2 = pool[j].box
                x_gap = max(0, max(ax1, bx1) - min(ax2, bx2))
                y_gap = max(0, max(ay1, by1) - min(ay2, by2))
                if x_gap <= gap_threshold and y_gap <= gap_threshold:
                    ax1, ay1 = min(ax1, bx1), min(ay1, by1)
                    ax2, ay2 = max(ax2, bx2), max(ay2, by2)
                    combined_paragraphs.extend(pool[j].paragraphs)
                    if base_order is None or (
                        pool[j].order is not None and pool[j].order < base_order
                    ):
                        base_order = pool[j].order
                    used[j] = True
                    changed = True

            merged.append(
                pool[i].model_copy(
                    update={
                        "box": [ax1, ay1, ax2, ay2],
                        "paragraphs": combined_paragraphs,
                        "order": base_order,
                    }
                )
            )

        pool = merged

    return pool

SUPPORTED_FORMATS = ["md", "html", "json", "pdf"]


def _sanitize_path_component(component):
    # yomitoku.cli.main._sanitize_path_component と同一 (ファイル名互換のため)
    if not component:
        return component
    return re.sub(r"^\.+", lambda m: "_" * len(m.group(0)), component)


def main():
    if len(sys.argv) != 2:
        print("Usage: python yomitoku_multi_export.py <job.json>", file=sys.stderr)
        return 1

    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)

    # torch / yomitoku の import はモデル類のロードを伴い重いため、job 読み込み成功後に行う
    import torch
    from pathlib import Path
    from PIL import Image
    from yomitoku.data.functions import load_image, load_pdf
    from yomitoku.document_analyzer import DocumentAnalyzer
    from yomitoku.utils.searchable_pdf import create_searchable_pdf
    from yomitoku.export import save_html, save_json, save_markdown
    from yomitoku.export import convert_json, convert_html, convert_markdown

    src_path = Path(job["src_path"])
    if not src_path.exists():
        raise FileNotFoundError(f"File not found: {src_path}")

    tasks = job["tasks"]
    if not tasks:
        raise ValueError("No export tasks specified")

    for task in tasks:
        if task["format"] not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported format: {task['format']}")
        os.makedirs(task["outdir"], exist_ok=True)

    device = job.get("device", "cuda")

    configs = {
        "ocr": {
            "text_detector": {"path_cfg": None},
            "text_recognizer": {"path_cfg": None},
        },
        "layout_analyzer": {
            "layout_parser": {"path_cfg": None},
            "table_structure_recognizer": {"path_cfg": None},
        },
    }

    if job.get("lite"):
        configs["ocr"]["text_recognizer"]["model_name"] = "parseq-tiny"
        if device == "cpu" or not torch.cuda.is_available():
            configs["ocr"]["text_detector"]["infer_onnx"] = True

    analyzer = DocumentAnalyzer(
        configs=configs,
        visualize=False,
        device=device,
        ignore_meta=bool(job.get("ignore_meta", False)),
        reading_order=job.get("reading_order") or "auto",
    )

    dpi = int(job.get("dpi", 200))
    merge_gap = int(job.get("merge_figure_gap", 0))

    if src_path.suffix[1:].lower() == "pdf":
        imgs = load_pdf(src_path, dpi=dpi)
    else:
        imgs = load_image(src_path)

    dirname = _sanitize_path_component(src_path.parent.name)
    filename = src_path.stem

    total_pages = len(imgs)
    task_names = ", ".join(t["name"] for t in tasks)
    print(
        f"[multi_export] {total_pages} pages, {len(tasks)} export tasks: {task_names}",
        flush=True,
    )

    merged = {task["name"]: [] for task in tasks}

    total_start = time.time()

    for page_index, img in enumerate(imgs):
        page_no = page_index + 1
        start = time.time()

        result, _, _ = analyzer(img)

        if merge_gap > 0 and len(result.figures) > 1:
            original_count = len(result.figures)
            merged_figures = _merge_close_figures(result.figures, merge_gap)
            if len(merged_figures) < original_count:
                print(
                    f"  [figure_merge] page {page_no}: {original_count} → {len(merged_figures)} figures (gap={merge_gap}px)",
                    flush=True,
                )
                result = result.model_copy(update={"figures": merged_figures})
            else:
                # [DEBUG] 複数figureがあるがギャップ超過でマージされなかったページ
                boxes = [f.box for f in result.figures]
                print(
                    f"  [figure_merge_skip] page {page_no}: {original_count} figures not merged (gap>{merge_gap}px) boxes={boxes}",
                    flush=True,
                )

        for task in tasks:
            fmt = task["format"]
            combine = bool(task["combine"])
            out_path = os.path.join(
                task["outdir"], f"{dirname}_{filename}_p{page_no}.{fmt}"
            )

            if fmt == "md":
                if combine:
                    md, _ = convert_markdown(
                        result,
                        out_path,
                        ignore_line_break=task["ignore_line_break"],
                        img=img,
                        export_figure=task["figure"],
                        export_figure_letter=task["figure_letter"],
                        figure_width=task["figure_width"],
                        figure_dir="figures",
                    )
                    merged[task["name"]].append(md)
                else:
                    result.to_markdown(
                        out_path,
                        ignore_line_break=task["ignore_line_break"],
                        img=img,
                        export_figure=task["figure"],
                        export_figure_letter=task["figure_letter"],
                        figure_width=task["figure_width"],
                        figure_dir="figures",
                        encoding=task["encoding"],
                    )

            elif fmt == "html":
                if combine:
                    html, _ = convert_html(
                        result,
                        out_path,
                        ignore_line_break=task["ignore_line_break"],
                        img=img,
                        export_figure=task["figure"],
                        export_figure_letter=task["figure_letter"],
                        figure_width=task["figure_width"],
                        figure_dir="figures",
                    )
                    merged[task["name"]].append(html)
                else:
                    result.to_html(
                        out_path,
                        ignore_line_break=task["ignore_line_break"],
                        img=img,
                        export_figure=task["figure"],
                        export_figure_letter=task["figure_letter"],
                        figure_width=task["figure_width"],
                        figure_dir="figures",
                        encoding=task["encoding"],
                    )

            elif fmt == "json":
                # convert_json / export_json は ignore_line_break 指定時に
                # paragraph.contents / cell.contents を破壊的に書き換えるため、
                # 他タスクと共有している result を守る目的で deep copy を渡す
                result_copy = result.model_copy(deep=True)
                if combine:
                    converted = convert_json(
                        result_copy,
                        out_path,
                        task["ignore_line_break"],
                        img,
                        task["figure"],
                        "figures",
                    )
                    merged[task["name"]].append(converted.model_dump())
                else:
                    result_copy.to_json(
                        out_path,
                        ignore_line_break=task["ignore_line_break"],
                        encoding=task["encoding"],
                        img=img,
                        export_figure=task["figure"],
                        figure_dir="figures",
                    )

            elif fmt == "pdf":
                if combine:
                    merged[task["name"]].append(result)
                else:
                    create_searchable_pdf(
                        [Image.fromarray(img[:, :, ::-1])],
                        [result],
                        output_path=out_path,
                        font_path=None,
                    )

        elapsed = time.time() - start
        print(
            f"[multi_export] page {page_no}/{total_pages} done in {elapsed:.2f} sec",
            flush=True,
        )

    for task in tasks:
        if not task["combine"]:
            continue

        fmt = task["format"]
        out_path = os.path.join(task["outdir"], f"{dirname}_{filename}.{fmt}")
        data = merged[task["name"]]

        if fmt == "md":
            save_markdown("\n".join(data), out_path, task["encoding"])
        elif fmt == "html":
            save_html("\n".join(data), out_path, task["encoding"])
        elif fmt == "json":
            save_json(data, out_path, task["encoding"])
        elif fmt == "pdf":
            pil_images = [Image.fromarray(img[:, :, ::-1]) for img in imgs]
            create_searchable_pdf(
                pil_images,
                data,
                output_path=out_path,
                font_path=None,
            )

        print(f"[multi_export] merged output: {out_path}", flush=True)

    total_elapsed = time.time() - total_start
    print(
        f"[multi_export] all {len(tasks)} tasks done in {total_elapsed:.2f} sec",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
