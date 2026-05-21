# Watermark Algorithms

水印算法说明（测试知识库）。

## Supported Algorithms

| algorithmType | Description | Typical Use |
|---------------|-------------|-------------|
| DWT | 离散小波变换，鲁棒性好 | PDF、PNG、JPEG |
| LSB | 最低有效位，容量大 | PNG、BMP |
| HYBRID | DWT + 纠错编码 | 高价值司法材料 |

默认：`DWT`（见 API `POST /api/v1/jobs/watermark`）。

## Supported File Types

| Format | DWT | LSB | Notes |
|--------|-----|-----|-------|
| PDF | yes | no | 逐页处理 |
| PNG | yes | yes | 推荐 |
| JPEG | yes | limited | 有损，强度降低 |
| TIFF | yes | no | 超大页见 R-004 偏差 |
| DOCX | planned | — | 当前版本不支持 |

## Processing Pipeline

1. Worker 从 Redis Stream 读取 `jobId`、`fileId`、`watermarkText`
2. 从对象存储下载源文件
3. 按 `algorithmType` 嵌入水印，上传结果
4. 更新 status：`PROCESSING` → `COMPLETED` 或 `FAILED`

## Performance Notes

- 10MB PDF + DWT：通常 1–3 分钟（见 OPERATIONS-SLO-SLA.md）
- PROCESSING 过长时查 Worker CPU 与对象存储 RTT

## Failure Modes

- `ALGORITHM_ERROR`：格式不支持或 TIFF 页数超限
- 用户可见说明应引用本文档与 REQUIREMENTS R-004

## Selection Guide

- 需要抗压缩、抗截图 → **DWT**
- 需要最大容量、无损 PNG → **LSB**
- 高价值归档 → **HYBRID**
