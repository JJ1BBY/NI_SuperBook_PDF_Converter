#!/usr/bin/env python3
"""
Real-ESRGAN Server Mode
モデルを1回だけロードし、stdinからジョブ(input_dir|output_dir)を受け取って処理する。

プロトコル:
  stdin:  "input_dir|output_dir"  → 処理実行
          "EXIT"                  → 終了
  stdout: "SERVER_LOADING"        → モデルロード開始
          "SERVER_READY"          → 準備完了 (C#側はここまで待機)
          "DONE"                  → ジョブ完了
          "ERROR: <msg>"          → エラー発生

Usage (C#側から起動):
  venv\Scripts\python.exe Real-ESRGAN/inference_realesrgan_server.py
      -n RealESRGAN_x4plus --tile 512 --tile_pad 16 --outscale 2.00
"""

import argparse
import os
import sys
import glob
import queue
import threading

import cv2


def parse_args():
    parser = argparse.ArgumentParser(description='Real-ESRGAN Server Mode')
    parser.add_argument('-n', '--model_name', type=str, default='RealESRGAN_x4plus',
                        help='Model name')
    parser.add_argument('--tile', type=int, default=512, help='Tile size (0=no tiling)')
    parser.add_argument('--tile_pad', type=int, default=16, help='Tile padding')
    parser.add_argument('--pre_pad', type=int, default=0, help='Pre padding')
    parser.add_argument('--outscale', type=float, default=2.0, help='Output scale')
    parser.add_argument('--face_enhance', action='store_true', help='Enable face enhancement')
    parser.add_argument('--fp32', action='store_true', help='Use FP32 precision')
    return parser.parse_args()


def load_model(args):
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer

    model_configs = {
        'RealESRGAN_x4plus':            (RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4), 4),
        'RealESRNet_x4plus':            (RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4), 4),
        'RealESRGAN_x4plus_anime_6B':   (RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=6,  num_grow_ch=32, scale=4), 4),
        'RealESRGAN_x2plus':            (RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=2), 2),
    }

    try:
        from realesrgan.archs.srvgg_arch import SRVGGNetCompact
        model_configs['realesr-animevideov3']   = (SRVGGNetCompact(num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=16, upscale=4, act_type='prelu'), 4)
        model_configs['realesr-general-x4v3']   = (SRVGGNetCompact(num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=32, upscale=4, act_type='prelu'), 4)
    except ImportError:
        pass

    if args.model_name not in model_configs:
        raise ValueError(f"Unknown model: {args.model_name}. Available: {list(model_configs.keys())}")

    model, netscale = model_configs[args.model_name]

    model_path = os.path.join('weights', args.model_name + '.pth')
    if not os.path.isfile(model_path):
        from basicsr.utils.download_util import load_file_from_url
        ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
        urls = {
            'RealESRGAN_x4plus':          'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth',
            'RealESRGAN_x2plus':          'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth',
            'RealESRGAN_x4plus_anime_6B': 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth',
            'realesr-animevideov3':       'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth',
            'realesr-general-x4v3':       'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth',
        }
        url = urls.get(args.model_name, '')
        if url:
            model_path = load_file_from_url(url=url, model_dir=os.path.join(ROOT_DIR, 'weights'), progress=True)

    upsampler = RealESRGANer(
        scale=netscale,
        model_path=model_path,
        model=model,
        tile=args.tile,
        tile_pad=args.tile_pad,
        pre_pad=args.pre_pad,
        half=not args.fp32,
    )

    face_enhancer = None
    if args.face_enhance:
        from gfpgan import GFPGANer
        face_enhancer = GFPGANer(
            model_path='https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.3.pth',
            upscale=args.outscale,
            arch='clean',
            channel_multiplier=2,
            bg_upsampler=upsampler,
        )

    import torch
    import numpy as np

    # cudnn benchmark: 固定サイズ入力に対し最速カーネルを自動選択
    if torch.cuda.is_available():
        torch.backends.cudnn.benchmark = True
        torch.backends.cuda.matmul.allow_tf32 = True

    # torch.inductor のコンパイルキャッシュを永続化 (2回目以降の起動で再コンパイル不要)
    _cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.torch_compile_cache')
    os.makedirs(_cache_dir, exist_ok=True)
    os.environ.setdefault('TORCHINDUCTOR_CACHE_DIR', _cache_dir)

    # torch.compile: conv+活性化+残差加算を1カーネルに融合し VRAM 往復を削減
    # SERVER_READY 送信前にウォームアップ推論を実行しコンパイルを完了させる
    # triton-windows が未導入の環境では自動的に eager モードにフォールバック
    try:
        upsampler.model = torch.compile(upsampler.model, mode="max-autotune")
        print("  [torch.compile] enabled (max-autotune)", file=sys.stderr, flush=True)
        tile_sz = args.tile if args.tile > 0 else 512
        warmup_img = np.zeros((tile_sz, tile_sz, 3), dtype=np.uint8)
        print(f"  [torch.compile] warmup ({tile_sz}x{tile_sz})...", file=sys.stderr, flush=True)
        upsampler.enhance(warmup_img, outscale=args.outscale)
        print("  [torch.compile] warmup done", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"  [torch.compile] unavailable, using eager mode: {e}", file=sys.stderr, flush=True)

    device_name = "CUDA:" + torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU (CUDA unavailable!)"
    print(f"  [Device] {device_name}, VRAM={torch.cuda.get_device_properties(0).total_memory // 1024**2}MB" if torch.cuda.is_available() else f"  [Device] {device_name}", file=sys.stderr, flush=True)

    return upsampler, face_enhancer


def _vram_str():
    """現在のVRAM使用量を文字列で返す (CUDA利用可能時のみ)"""
    try:
        import torch
        if torch.cuda.is_available():
            used  = torch.cuda.memory_reserved(0)  // 1024**2
            total = torch.cuda.get_device_properties(0).total_memory // 1024**2
            peak  = torch.cuda.max_memory_allocated(0) // 1024**2
            pct   = used / total * 100
            warn  = " *** OOM RISK ***" if pct >= 90 else (" [HIGH]" if pct >= 75 else "")
            return f"VRAM {used}/{total}MB (peak_alloc={peak}MB, {pct:.0f}%){warn}"
    except Exception:
        pass
    return ""


def process_dir(upsampler, face_enhancer, args, input_dir, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    paths = sorted(glob.glob(os.path.join(input_dir, '*')))
    if not paths:
        return

    import torch

    _SENTINEL = object()
    # 先読みキュー: GPU推論中に次画像を読み込む (depth=2)
    read_q = queue.Queue(maxsize=2)
    # 後書きキュー: GPU推論中に前画像を書き込む (depth=2)
    write_q = queue.Queue(maxsize=2)

    def reader():
        for path in paths:
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None:
                print(f"  [skip] cannot read: {path}", file=sys.stderr, flush=True)
                continue
            if len(img.shape) == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            read_q.put((path, img))
        read_q.put(_SENTINEL)

    def writer():
        while True:
            item = write_q.get()
            if item is _SENTINEL:
                break
            save_path, output, src_name, dst_name = item
            cv2.imwrite(save_path, output)
            print(f"  done: {src_name} -> {dst_name}", file=sys.stderr, flush=True)

    reader_thread = threading.Thread(target=reader, daemon=True)
    writer_thread = threading.Thread(target=writer, daemon=True)
    reader_thread.start()
    writer_thread.start()

    while True:
        item = read_q.get()
        if item is _SENTINEL:
            break
        path, img = item
        imgname, ext = os.path.splitext(os.path.basename(path))

        h, w = img.shape[:2]
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats(0)
        print(f"  [VRAM before] {_vram_str()}  img={w}x{h}", file=sys.stderr, flush=True)

        if face_enhancer is not None:
            _, _, output = face_enhancer.enhance(img, has_aligned=False, only_center_face=False, paste_back=True)
        else:
            output, _ = upsampler.enhance(img, outscale=args.outscale)

        print(f"  [VRAM after ] {_vram_str()}", file=sys.stderr, flush=True)

        save_path = os.path.join(output_dir, imgname + ext)
        write_q.put((save_path, output, os.path.basename(path), imgname + ext))

    write_q.put(_SENTINEL)
    writer_thread.join()
    reader_thread.join()


def main():
    args = parse_args()

    print("SERVER_LOADING", flush=True)
    print(f"Loading model: {args.model_name} tile={args.tile} pad={args.tile_pad} scale={args.outscale}", file=sys.stderr, flush=True)

    try:
        upsampler, face_enhancer = load_model(args)
    except Exception as e:
        print(f"ERROR: Model load failed: {e}", flush=True)
        sys.exit(1)

    print("SERVER_READY", flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == "EXIT":
            break
        try:
            input_dir, output_dir = line.split("|", 1)
            input_dir = input_dir.strip()
            output_dir = output_dir.strip()
            print(f"  job: {input_dir} -> {output_dir}", file=sys.stderr, flush=True)
            process_dir(upsampler, face_enhancer, args, input_dir, output_dir)
            print("DONE", flush=True)
        except Exception as e:
            print(f"ERROR: {e}", flush=True)


if __name__ == '__main__':
    main()
