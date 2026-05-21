# Runbook — Watermark Platform Operations

运维 Runbook（测试知识库）。

## 1. Daily Checks

1. `GET /actuator/health` — API UP
2. Redis `PING` 与 Stream `XLEN wm:jobs:stream`
3. Worker 日志无连续 OOM / 连接拒绝
4. 对象存储 HEAD 探针

## 2. QUEUED / PROCESSING 任务排查

### 2.1 任务卡在 QUEUED

1. 确认 Worker 进程运行且 `WM_JOBS_WORKER_COUNT > 0`
2. 检查 `WM_JOBS_STREAM_KEY` 是否为 `wm:jobs:stream`
3. `XINFO GROUPS wm:jobs:stream` — 消费者组 `wm-workers` 是否存在
4. 查看 pending 消息是否堆积；必要时 `XCLAIM` 或重启 Worker
5. 对照 DEPLOY-SERVER.md「QUEUED 长时间不消费」

### 2.2 任务卡在 PROCESSING

1. 在 DB 或 `GET /api/v1/jobs/{id}` 确认 `jobId` 与 `updatedAt`
2. Worker 日志搜索 `jobId` — 是否卡在下载或算法阶段
3. 检查对象存储延迟与文件大小
4. 超过 SLA（见 OPERATIONS-SLO-SLA.md）则标记 incident

## 3. Failed Jobs

| errorCode | 常见原因 | 动作 |
|-----------|----------|------|
| FILE_NOT_FOUND | fileId 无效或已删除 | 核对文件列表 |
| STORAGE_TIMEOUT | 对象存储不可达 | 检查网络与凭证 |
| ALGORITHM_ERROR | 不支持格式或参数 | README_ALGORITHM.md |
| VALIDATION_ERROR | watermarkText 为空等 | 修正请求重试 |

## 4. Redis Stream Maintenance

- 禁止在生产随意 `DEL wm:jobs:stream`
- 使用 `XTRIM` 时需确认无未 ACK 的 pending 任务
- 备份：定期导出 Stream 长度与 pending 指标到监控系统

## 5. Rollback

API 回滚：切换至上一版本 jar，重启后跑 `verify-config.ps1`。Worker 与 API 版本需匹配 `job JSON` schema。
