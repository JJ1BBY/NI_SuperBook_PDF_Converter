# RealESRGAN 実測パフォーマンスメモ

## 速度 (2026/06/10 計測)

| 構成 | n | 平均 (秒/32枚) | σ |
|------|---|----------------|---|
| 旧: x4plus / Tile=512 | 75 | 219.3 s | 21.2 s |
| 新: x2plus / Tile=2048 | 21 | 55.8 s | 0.5 s |

**高速化倍率: 約7.9倍** (219.3 / 55.8)  
**秒/枚換算**: 旧 13.7 s/枚 → 新 1.74 s/枚

> ※ 旧構成の「約4倍速」という記述は誤り。正確には7.9倍。

## VRAM 使用量 (x2plus / Tile=2048)

| 指標 | 値 |
|------|-----|
| peak_alloc (PyTorch) | 約 9.2 GB |
| タスクマネージャー表示 | 12.3 GB / 16 GB (75%) |

> Tile=0 (no tiling) は共有メモリ使用でOOM。  
> Tile=1024 は未使用VRAMが多すぎ非効率。  
> Tile=2048 が最適 (VRAM 75%で安定)。

## 現在の設定 (AiUtil.cs)

```csharp
public class AiUtilRealEsrganPerformOption
{
    public string Model = "RealESRGAN_x2plus";
    public int Tile = 2048;
    ...
}
```

SuperPdfUtil.cs の呼び出し側:
```csharp
Model = "RealESRGAN_x2plus", // 2x出力には2xモデルを使用 (x4plusで2x出力は内部で4x→2xとなり非効率)
```
