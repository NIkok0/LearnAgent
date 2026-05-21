# Watermark Java Backend — Tech Selection

队列、Redis Stream 与任务 JSON 字段说明（测试知识库）。

## Message Queue — Redis Stream

| Item | Value |
|------|-------|
| 默认 Stream key | `wm:jobs:stream`（环境变量 `WM_JOBS_STREAM_KEY`） |
| 消费者组 | `wm-workers`（`WM_JOBS_GROUP`） |
| 消息格式 | JSON 字符串 |

Redis Stream 的 key 默认叫 **`wm:jobs:stream`**。生产环境可通过 `WM_JOBS_STREAM_KEY` 覆盖，但文档与脚本示例均使用该默认值。

## Watermark Job JSON Fields

队列（Redis Stream）中每条水印任务的 JSON 字段：

```json
{
  "jobId": "uuid",
  "fileId": 1,
  "watermarkText": "string",
  "algorithmType": "DWT",
  "status": "QUEUED",
  "priority": 0,
  "createdAt": "2026-05-18T10:00:00Z",
  "updatedAt": "2026-05-18T10:00:00Z",
  "ownerUserId": "uuid",
  "retryCount": 0,
  "errorCode": null,
  "errorMessage": null
}
```

| Field | Type | Description |
|-------|------|-------------|
| jobId | UUID | 任务唯一 ID |
| fileId | long | 关联文件 |
| watermarkText | string | 嵌入文本 |
| algorithmType | string | DWT / LSB / HYBRID |
| status | enum | QUEUED, PROCESSING, COMPLETED, FAILED |
| priority | int | 越大越优先 |
| createdAt | ISO8601 | 创建时间 |
| updatedAt | ISO8601 | 最后更新时间 |
| ownerUserId | UUID | 提交用户 |
| retryCount | int | 重试次数 |
| errorCode | string? | 失败码 |
| errorMessage | string? | 失败说明 |

## Worker Configuration

Worker 通过 `WM_JOBS_WORKER_COUNT` 控制并发消费者数量。每个消费者使用 `XREADGROUP` 从 `wm:jobs:stream` 拉取任务。

## Status Lifecycle

`QUEUED` → `PROCESSING` → `COMPLETED` 或 `FAILED`。状态变更写回 DB 并通过 Stream ACK 确认。
