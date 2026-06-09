# DN_SuperBook_PDF_Converter 高速化改修ガイド（Claude Code向け）

## 前提情報

### リポジトリ構成
```
C:\src\DN_SuperBook_PDF_Converter\
├── SuperBookToolsApp\          # メインアプリ（C# / .NET 6.0）
│   └── SuperBookToolsApp\
│       └── AiCommands.cs       # ConvertPdfコマンド定義
├── SuperBookTools\
│   └── Basic\
│       └── SuperPdfUtil.cs     # PDF処理メインロジック
└── internal_libs\IPA-DN-Cores\
    └── Cores.NET\Cores.Basic\AppLib\Misc\
        └── AiUtil.cs           # Real-ESRGAN / yomitoku呼び出し
```

### 現在の処理フロー（1ファイルあたり）
1. ImageMagick: PDF → BMP展開（300dpi、2480x3508px）
2. Real-ESRGAN: BMP → アップスケール（Tile=1024、BatchChunkCount=32、x2倍）
3. pdfcpu / QPDF: PDF再合成
4. exiftool: メタデータ付与
5. yomitoku: OCRテキスト生成
6. 全ファイル完了後にOCR結果をpdf_ocredフォルダへ出力

### 判明しているボトルネック
- **ImageMagick PDF→BMP展開**: シングルプロセス、CPUバウンド。514ページで10〜20分
- **Real-ESRGAN**: 1チャンク（32枚）あたり約4〜26分（Tileサイズ依存）。pythonプロセスをチャンクごとに起動・終了するオーバーヘッドあり
- **yomitoku OCR**: 1ページあたり約5秒（GPU使用）

### 現在の設定値（AiUtil.cs）
```csharp
public class AiUtilRealEsrganPerformOption
{
    public string Model = "RealESRGAN_x4plus";
    public int Tile = 1024;
    public int Pad = 16;
    public double OutScale = 1.0;
    public bool Skip = false;
    public bool FaceMode = false;
    public bool Fp32 = false;
    public int BatchChunkCount = 32;
}
```

---

## 改修項目

### 改修1: Real-ESRGANのpythonプロセス常駐化（最優先・効果大）

**現状の問題:**
チャンクごとにpythonプロセスを起動・終了している。起動時にGPUモデルロード（数秒〜十数秒）が毎回発生している。

**改修方針:**
pythonプロセスを起動したまま標準入出力でジョブを渡すサーバーモード化。

**実装案:**

`AiUtil.cs` の Real-ESRGAN呼び出し部分を以下のように変更：

```csharp
// 現在: チャンクごとにcmd.exeを起動してpythonを実行
// 改修後: pythonプロセスを1回起動してstdinで入力フォルダを渡す

// Python側にサーバーモードスクリプトを追加
// inference_realesrgan_server.py を作成し、
// stdinからinput_dir/output_dirを受け取ってループ処理する
```

**Python側スクリプト（inference_realesrgan_server.py）の骨格:**
```python
import sys
import os
# 既存のモデルロード処理（1回だけ実行）
model = load_model(...)

for line in sys.stdin:
    input_dir, output_dir = line.strip().split('|')
    process_images(model, input_dir, output_dir)
    print("DONE", flush=True)  # C#側への完了通知
```

**C#側の変更箇所:** `AiUtil.cs` の `AiUtilRealEsrganEngine.PerformAsync()`

---

### 改修2: ImageMagickのページ並列展開（効果大）

**現状の問題:**
`magick.exe` がPDF全ページを1プロセスで逐次展開している。514ページで47分かかるケースがある。

**改修方針:**
Ghostscriptを直接呼び出してページ範囲を並列展開する。

**実装案（SuperPdfUtil.cs）:**
```csharp
// 現在のImageMagick呼び出し1回を
// GhostscriptによるN並列に変更

int parallelism = 4; // CPUコア数に応じて調整
int pagesPerJob = totalPages / parallelism;

var tasks = Enumerable.Range(0, parallelism).Select(i =>
    Task.Run(() => RunGhostscript(
        srcPdf,
        startPage: i * pagesPerJob + 1,
        endPage: (i + 1) * pagesPerJob,
        outputDir: tmpDir
    ))
).ToArray();

await Task.WhenAll(tasks);
```

**Ghostscriptコマンド例:**
```
gswin64c.exe -dNOPAUSE -dBATCH -sDEVICE=bmp16m -r300
  -dFirstPage=1 -dLastPage=128
  -sOutputFile=page_%05d.bmp input.pdf
```

**注意:** GhostscriptはImageMagickポータブル版に同梱されている場合がある。パスを確認すること。

---

### 改修3: yomitokuのバッチサイズ調整（効果中）

**現状の問題:**
yomitokuが1ページずつ処理している可能性がある。

**改修方針:**
yomitokuのAPIがバッチ処理をサポートしている場合、複数ページを同時にGPUに送る。

**確認箇所:** `AiUtil.cs` のyomitoku呼び出し部分でバッチサイズパラメータを探す。

---

### 改修4: Windowsセキュリティブロックの自動解除（小改修・安定性向上）

**現状の問題:**
他のPCからコピーしたPDFにZone.Identifierが付与されており、magick.exeがハングする。

**改修箇所:** `SuperPdfUtil.cs` のPerformPdfMainAsync()の冒頭に追加

```csharp
// Zone.Identifierの自動解除
private static void UnblockFile(string filePath)
{
    string zoneFile = filePath + ":Zone.Identifier";
    if (File.Exists(zoneFile))
    {
        File.Delete(zoneFile);
    }
}
```

---

### 改修5: OCR処理の並列化（効果中・要検討）

**現状の問題:**
全ファイルのPDF変換完了後に逐次OCR処理している。

**改修方針:**
各ファイルのPDF変換完了直後にOCRを非同期で開始し、次のファイルの変換と並列実行する。

**注意事項:**
- Real-ESRGANとyomitokuが同時にGPUを使うとVRAM競合（16GB）が発生する
- Real-ESRGAN完了→yomitokuOCR開始のタイミング制御が必要
- セマフォによる排他制御が必要

---

## Claude Codeへの指示テンプレート

以下をClaude Codeのセッション冒頭に貼り付けて使用する：

```
# DN_SuperBook_PDF_Converter 改修セッション

## 環境
- OS: Windows 11
- Runtime: .NET 6.0
- GPU: NVIDIA RTX 5070 Ti (16GB VRAM)
- CPU: AMD Ryzen 7 5700X (8コア/16スレッド)

## リポジトリパス
C:\src\DN_SuperBook_PDF_Converter\

## 今回の改修目標
[改修1〜5から選択して記載]

## 制約
- AiUtil.cs は internal_libs 配下にあり、変更は可能だが
  git submodule の可能性があるため変更前に確認すること
- pythonスクリプトの変更は
  external_tools\...\RealEsrgan\RealEsrgan_Repo\Real-ESRGAN\ 配下
- ビルドはVisual Studio 2022でReleaseビルド
- テストは d:\tmp の小さいPDF（数十ページ）で先に確認すること
```

---

## 優先順位まとめ

| 優先度 | 改修項目 | 期待効果 | 難易度 |
|--------|----------|----------|--------|
| 1 | Real-ESRGANプロセス常駐化 | チャンクごとのモデルロード削減 | 中 |
| 2 | ImageMagick並列展開 | BMP展開時間を1/4に短縮 | 中 |
| 3 | Windowsセキュリティブロック自動解除 | ハング防止 | 低 |
| 4 | yomitokuバッチサイズ調整 | OCR速度向上 | 低〜中 |
| 5 | OCR処理並列化 | 全体処理時間短縮 | 高 |
