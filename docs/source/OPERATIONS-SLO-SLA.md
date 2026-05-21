# Operations SLO and SLA

水印平台运维 SLO/SLA（测试知识库）。

## Service Level Objectives

| Service | SLO | Measurement |
|---------|-----|-------------|
| API availability | 99.5% / 30d | `GET /actuator/health` success rate |
| Job enqueue latency | p95 < 2s | POST 到 QUEUED 写入 |
| QUEUED → PROCESSING | p95 < 60s | 正常负载、Worker 健康 |
| PROCESSING → COMPLETED | p95 < 5min | 10MB 以下 PDF，DWT 算法 |
| Search / RAG (Agent) | p95 < 3s | `search_docs` 端到端 |

## Alerting Thresholds

- API health 连续 3 次失败 → P1
- **Redis Stream pending > 100 持续 10min → P2**（告警阈值；见 Runbook 排查 pending）
- FAILED 率 > 5% / 1h → P2
- Worker 全部离线 → P1

### Redis Stream pending P2 告警

当 `wm:jobs:stream` 的消费者组 pending 消息 **超过 100 条且持续 10 分钟**，触发 **P2** 级别告警。On-call 按 RUNBOOK.md §2 处理 QUEUED 堆积。

## SLA Commitments (Demo Tier)

- 计划内维护窗口：每周日 02:00–04:00 UTC
- P1 响应：30 分钟内确认；P2：4 小时内

## Escalation

1. On-call 工程师 — Runbook 初步排查
2. 平台负责人 — 涉及数据丢失或 SEC 事件
3. 对照 SECURITY-BASELINE.md 处理凭证泄露

## Reporting

每周导出：任务量、各 status 占比、平均 QUEUED/PROCESSING 时长、top errorCode。
