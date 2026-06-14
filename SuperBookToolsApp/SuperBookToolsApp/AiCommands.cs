#pragma warning disable CA2235 // Mark all non-serializable fields

using System;
using System.Buffers;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.CompilerServices;
using System.Diagnostics.CodeAnalysis;
using System.Runtime.Serialization;

using IPA.Cores.Basic;
using IPA.Cores.Helper.Basic;
using static IPA.Cores.Globals.Basic;

using IPA.Cores.Codes;
using IPA.Cores.Helper.Codes;
using static IPA.Cores.Globals.Codes;

using SuperBookTools;
using SuperBookTools.App;

namespace SuperBookTools.App
{
    public static class SuperBookExternalTools
    {
        public static readonly ImageMagickUtil ImageMagick = new ImageMagickUtil(new ImageMagickOptions(
            Path.Combine(Env.AppRootDir, @"..\external_tools\external_tools\image_tools\ImageMagick-portable-Q16-HDRI-x64\magick.exe"),
            Path.Combine(Env.AppRootDir, @"..\external_tools\external_tools\image_tools\ImageMagick-portable-Q16-HDRI-x64\mogrify.exe"),
            Path.Combine(Env.AppRootDir, @"..\external_tools\external_tools\image_tools\exiftool-13.30_64\exiftool.exe"),
            Path.Combine(Env.AppRootDir, @"..\external_tools\external_tools\image_tools\QPDF\bin\qpdf.exe"),
            Path.Combine(Env.AppRootDir, @"..\external_tools\external_tools\image_tools\pdfcpu\pdfcpu.exe")
        ));

        public static readonly FfMpegUtil FfMpeg = new FfMpegUtil(new FfMpegUtilOptions(
            Path.Combine(Env.AppRootDir, @"_dummy.exe"),
            Path.Combine(Env.AppRootDir, @"_dummy.exe")));

        public static readonly PdfYomitokuLib YomiToku = new PdfYomitokuLib(
            Path.Combine(Env.AppRootDir, @"..\external_tools\external_tools\image_tools\yomitoku"),
            Path.Combine(Env.AppRootDir, @"..\external_tools\external_tools\image_tools\pandoc\pandoc.exe"),
            Path.Combine(Env.AppRootDir, @"PythonScripts\yomitoku_multi_export.py"));

        public static readonly AiUtilBasicSettings Settings = new AiUtilBasicSettings
        {
            AiTest_RealEsrgan_BaseDir = Path.Combine(Env.AppRootDir, @"..\external_tools\external_tools\image_tools\RealEsrgan\RealEsrgan_Repo"),
            AiTest_TesseractOCR_Data_Dir = Path.Combine(Env.AppRootDir, @"..\external_tools\external_tools\image_tools\TesseractOCR_Data"),
        };
        public static readonly AiTask Task = new AiTask(Settings, FfMpeg);

        public const string Post_OCR_Dir = "Post_OCR_Dir";
    }

    public static class SuperBookAppConfig
    {
        public static readonly string TempRootPath;
        public static readonly int RealEsrganTile; // 0 = AiUtilRealEsrganPerformOption の既定値 (2048) を使用

        static SuperBookAppConfig()
        {
            TempRootPath = Env.MyLocalTempDir;
            RealEsrganTile = 0;

            foreach (var fileName in new[] { "appsettings.json", "appsettings.local.json" })
            {
                var configPath = System.IO.Path.Combine(Env.AppRootDir, fileName);
                if (!File.Exists(configPath)) continue;

                try
                {
                    var json = File.ReadAllText(configPath);
                    using var doc = System.Text.Json.JsonDocument.Parse(json);

                    if (doc.RootElement.TryGetProperty("TempRootPath", out var val))
                    {
                        var str = val.GetString();
                        if (!string.IsNullOrWhiteSpace(str))
                        {
                            TempRootPath = str;
                            Con.WriteLine($"[Config] TempRootPath = \"{TempRootPath}\" ({fileName})");
                        }
                    }

                    if (doc.RootElement.TryGetProperty("RealEsrganTile", out var tileVal)
                        && tileVal.TryGetInt32(out var tileInt) && tileInt > 0)
                    {
                        RealEsrganTile = tileInt;
                        Con.WriteLine($"[Config] RealEsrganTile = {RealEsrganTile} ({fileName})");
                    }
                }
                catch (Exception ex)
                {
                    Con.WriteLine($"[Config] {fileName} 読み込み失敗 (スキップ): {ex.Message}");
                }
            }
        }
    }

    public static partial class Commands
    {
        [ConsoleCommand(
            "ConvertPdf command",
            "ConvertPdf [srcDir] [/dst:dstDir] [/ocr:yes|no]",
            "ConvertPdf command")]
        public static async Task<int> ConvertPdf(ConsoleService c, string cmdName, string str)
        {
            ConsoleParam[] args =
            {
                new ConsoleParam("[srcDir]", ConsoleService.Prompt, "Source directory path: ", ConsoleService.EvalNotEmpty, null),
                new ConsoleParam("dst", ConsoleService.Prompt, "Destination directory path: ", ConsoleService.EvalNotEmpty, null),
                // ConsoleService.Prompt は空入力を null (キャンセル) 扱いするため、
                // Enter = デフォルト Y を実現するには ReadLine を直接呼ぶ
                new ConsoleParam("ocr", (svc, p) => svc.ReadLine((string?)p ?? ""), "Perform Japanese High-Quality OCR? [Y/n/e=epub-only]: ", null, null),
                new ConsoleParam("eta", (svc, p) => svc.ReadLine((string?)p ?? ""), "Show conversion time estimate (ETA)? [Y/n]: ", null, null),
            };
            ConsoleParamValueList vl = c.ParseCommandList(cmdName, str, args);

            string srcDir = vl.DefaultParam.StrValue;
            string dstDir = vl["dst"].StrValue;

            srcDir = PP.RemoveLastSeparatorChar(await Lfs.NormalizePathAsync(srcDir, normalizeRelativePathIfSupported: true));
            dstDir = PP.RemoveLastSeparatorChar(await Lfs.NormalizePathAsync(dstDir, normalizeRelativePathIfSupported: true));

            $"- Source Dir: \"{srcDir}\""._Print();
            $"- Destination Dir: \"{dstDir}\""._Print();

            string ocrRaw = vl["ocr"].StrValue._NonNullTrim();
            bool epubOnly = ocrRaw.StartsWith("e", StringComparison.OrdinalIgnoreCase);
            bool performOcr = !epubOnly && (ocrRaw._IsEmpty() || ocrRaw.StartsWith("y", StringComparison.OrdinalIgnoreCase));

            string etaRaw = vl["eta"].StrValue._NonNullTrim();
            bool showEta = !epubOnly && (etaRaw._IsEmpty() || etaRaw.StartsWith("y", StringComparison.OrdinalIgnoreCase));

            if (!epubOnly && srcDir._IsSamei(dstDir))
            {
                throw new CoresException("srcDir must not be same to dstDir.");
            }

            await Lfs.CreateDirectoryAsync(dstDir);

            SuperPerformPdfOptions options = new SuperPerformPdfOptions {/* MaxPagesForDebug = 120, SaveDebugPng = true, SkipRealesrgan = true */ };

            string mdPagedReadingOrder = "right2left"; // デフォルト: 縦書き

            if (performOcr || epubOnly)
            {
                ""._Print();
                "***"._Print();
                if (performOcr)
                {
                    $"The \"ocr\" option is enabled. This OCR feature uses \"YomiKaku\" AI engine published by kotaro.kinoshita-san. Plesae read the https://github.com/kotaro-kinoshita/yomitoku/blob/cba0a134e0d2ad3bfdce163231b3cb91de07928e/README.md license document."._Print();
                }
                else
                {
                    "EPUB only mode: Skipping PDF processing and OCR. Regenerating EPUB from existing md_paged."._Print();
                }
                "***"._Print();
                ""._Print();

                // reading_order の確認 (Enter = R = right2left 固定 / a = auto 自動判定)
                string? roInput = c.ReadLine("Reading order for md_paged (right2left=右→左に固定/縦組み向け / auto=自動判定・誤判定の可能性あり) [R/a]: ");
                string roTrimmed = (roInput ?? "")._NonNullTrim();
                if (roTrimmed.StartsWith("a", StringComparison.OrdinalIgnoreCase))
                {
                    mdPagedReadingOrder = "auto";
                    "-> reading_order: auto (読み順を自動判定。横組み等に有効だが誤判定の可能性あり)"._Print();
                }
                else
                {
                    mdPagedReadingOrder = "right2left";
                    "-> reading_order: right2left (読み順を右→左に固定。縦組み向け)"._Print();
                }
                ""._Print();
            }

            if (!epubOnly)
            {
                var srcFiles = (await Lfs.EnumDirectoryAsync(srcDir, true)).Where(x => x.IsFile && x.Name.StartsWith("_") == false && x.Name._IsExtensionMatch(".pdf")).OrderBy(x => x.FullPath, StrCmpi)._Shuffle().ToList();

                int numTotal = srcFiles.Count();
                int numOk = 0;
                int numError = 0;
                int numSkip = 0;

                $"Total {numTotal} Files"._Error();

                int currentNumber = 0;

                List<string> errorFilesList = new();

                // ETA プリスキャン: 全PDFのページ数取得 + スキップ予測
                int[] preScanPageCounts = new int[numTotal];
                bool[] preScanWillSkip = new bool[numTotal];
                if (showEta && numTotal > 0)
                {
                    $"[ETA] Pre-scanning {numTotal} files..."._Error();
                    string optionsDigestPart = options._ObjectToJson();
                    var prescanTasks = srcFiles.Select(async (src, idx) =>
                    {
                        try
                        {
                            int pageCount = await SuperBookExternalTools.ImageMagick.GetPdfPageCountAsync(src.FullPath);
                            string relPath = PP.GetRelativeFileName(src.FullPath, srcDir);
                            string dstPath = PP.Combine(dstDir, relPath);
                            var meta = await Lfs.GetFileMetadataAsync(src.FullPath);
                            string digest = $"{meta.LastWriteTime!.Value.Ticks} {meta.Size} {optionsDigestPart}"._Digest();
                            bool willSkip = await Lfs.IsOkFileExistsAsync(dstPath, digest);
                            return (pageCount, willSkip, idx);
                        }
                        catch
                        {
                            return (0, false, idx);
                        }
                    }).ToList();
                    var prescanResults = await Task.WhenAll(prescanTasks);
                    foreach (var (pageCount, willSkip, idx) in prescanResults)
                    {
                        preScanPageCounts[idx] = pageCount;
                        preScanWillSkip[idx] = willSkip;
                    }
                    int predictedSkip = preScanWillSkip.Count(x => x);
                    int predictedProcess = numTotal - predictedSkip;
                    int pagesToProcess = prescanResults.Where(r => !r.willSkip).Sum(r => r.pageCount);
                    $"[ETA] Pre-scan done: {numTotal} files ({predictedSkip} already done, {predictedProcess} to process, {pagesToProcess} pages)"._Error();
                }

                double fallbackSecsPerPage = performOcr ? 15.0 : 5.0;
                var eta = new ConvertPdfEtaTracker(preScanPageCounts, preScanWillSkip, fallbackSecsPerPage);

                // RealESRGAN サーバーを全PDFで共有してモデルロードを1回にする
                await using var sharedRealesrgan = new AiUtilRealEsrganEngine(SuperBookExternalTools.Settings);

                foreach (var (src, idx) in srcFiles.Select((s, i) => (s, i)))
                {
                    currentNumber++;
                    string relativePath = PP.GetRelativeFileName(src.FullPath, srcDir);
                    string dstPath = PP.Combine(dstDir, relativePath);
                    int thisFilePages = preScanPageCounts[idx];

                    string startSuffix = showEta
                        ? $" [{thisFilePages} pages | {eta.GetEtaString()}]"
                        : "";
                    $"<< {currentNumber} / {numTotal} >> '{src.FullPath}' Start{startSuffix}"._Error();

                    var fileSw = Stopwatch.StartNew();
                    try
                    {
                        var (wasProcessed, actualPageCount) = await SuperPdfUtil.PerformPdfAsync(src.FullPath, dstPath, options, sharedRealesrgan: sharedRealesrgan);
                        fileSw.Stop();
                        int pagesForEta = actualPageCount > 0 ? actualPageCount : thisFilePages;

                        if (!wasProcessed)
                        {
                            numSkip++;
                            if (showEta) eta.RecordCompletion(wasSkipped: true, pagesForEta, fileSw.Elapsed);
                            $"<< {currentNumber} / {numTotal} >> '{src.FullPath}' Skip"._Error();
                        }
                        else
                        {
                            numOk++;
                            if (showEta) eta.RecordCompletion(wasSkipped: false, pagesForEta, fileSw.Elapsed);
                            string okSuffix = showEta
                                ? $" [{fileSw.Elapsed:hh\\:mm\\:ss} | {eta.GetRateString()} | {eta.GetEtaString()}]"
                                : "";
                            $"<< {currentNumber} / {numTotal} >> '{src.FullPath}' OK{okSuffix}"._Error();
                        }
                    }
                    catch (Exception ex)
                    {
                        fileSw.Stop();
                        if (showEta) eta.RecordCompletion(wasSkipped: false, thisFilePages, fileSw.Elapsed);
                        Con.WriteLine($"<< {currentNumber} / {numTotal} >> Error: {src.FullPath} -> {dstPath}");
                        ex._Error();
                        errorFilesList.Add(src.FullPath);
                        numError++;
                    }
                }

                if (errorFilesList.Count >= 1)
                {
                    $"--- Error files ---"._Error();
                    foreach (var errFile in errorFilesList)
                    {
                        $"- {errFile}"._Error();
                    }
                }

                $"\n\n<< ConvertPdf Result >>\nnumTotal = {numTotal}, numSkip = {numSkip}, numOk = {numOk}, numError = {numError}\n\n"._Error();
            }

            if (performOcr)
            {
                Con.WriteLine("Performing Japanese OCR started ...");

                await SuperBookExternalTools.YomiToku.PerformOcrDirAsync(dstDir, PP.Combine(dstDir, SuperBookExternalTools.Post_OCR_Dir), SuperBookExternalTools.Post_OCR_Dir, mdPagedReadingOrder);

                Con.WriteLine("Performing Japanese OCR completed.");
            }
            else if (epubOnly)
            {
                Con.WriteLine("EPUB-only generation started ...");

                string postOcrDir = PP.Combine(dstDir, SuperBookExternalTools.Post_OCR_Dir);
                string epubDir = PP.Combine(postOcrDir, "epub");
                string mdPagedDir = PP.Combine(postOcrDir, "md_paged");

                await SuperBookExternalTools.YomiToku.ConvertMdPagedDirToEpubAsync(
                    mdPagedDir,
                    epubDir,
                    rotateImagesLeft: mdPagedReadingOrder._IsSamei("right2left"));

                Con.WriteLine("EPUB-only generation completed.");
            }

            return 0;
        }
    }

    private sealed class ConvertPdfEtaTracker
    {
        private long _totalPagesAccumulated;
        private double _totalSecondsAccumulated;
        private int _pagesRemaining;
        private readonly double _fallbackSecsPerPage;

        public ConvertPdfEtaTracker(int[] preScanPageCounts, bool[] preScanWillSkip, double fallbackSecsPerPage)
        {
            _fallbackSecsPerPage = fallbackSecsPerPage;
            for (int i = 0; i < preScanPageCounts.Length; i++)
                if (!preScanWillSkip[i]) _pagesRemaining += preScanPageCounts[i];
        }

        private double PagesPerSecond =>
            _totalSecondsAccumulated > 0 ? _totalPagesAccumulated / _totalSecondsAccumulated : 0;

        public void RecordCompletion(bool wasSkipped, int pageCount, TimeSpan elapsed)
        {
            _pagesRemaining = Math.Max(0, _pagesRemaining - pageCount);
            if (!wasSkipped && pageCount > 0 && elapsed.TotalSeconds > 1.0)
            {
                _totalPagesAccumulated += pageCount;
                _totalSecondsAccumulated += elapsed.TotalSeconds;
            }
        }

        public string GetEtaString()
        {
            double secsPerPage = PagesPerSecond > 0 ? 1.0 / PagesPerSecond : _fallbackSecsPerPage;
            double remainingSecs = _pagesRemaining * secsPerPage;
            string label = PagesPerSecond > 0 ? "ETA" : "ETA~";
            var finishAt = DateTime.Now.AddSeconds(remainingSecs);
            return $"{label}: {TimeSpan.FromSeconds(remainingSecs):hh\\:mm\\:ss} (finish ~{finishAt:HH:mm})";
        }

        public string GetRateString() =>
            PagesPerSecond > 0 ? $"{PagesPerSecond:F1} pg/s" : "-- pg/s";
    }
}
