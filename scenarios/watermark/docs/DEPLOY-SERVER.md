# Deploy Server — Java API and Workers

水印平台 Java API 与 Worker 部署指南（测试知识库）。

## QUEUED and PROCESSING Troubleshooting (Quick Guide)

用户问「水印任务一直 QUEUED 或 PROCESSING 怎么排查」时，按下列顺序检查：

1. **QUEUED**：Worker 是否运行、`WM_JOBS_WORKER_COUNT`、Redis Stream key `WM_JOBS_STREAM_KEY`（默认 `wm:jobs:stream`）、消费者组 `WM_JOBS_GROUP`。
2. **PROCESSING**：对象存储下载、算法耗时、Worker 线程是否卡住；对照 RUNBOOK §2。
3. 任务 JSON 字段与 Stream 配置见 `watermark-java-backend-tech-selection.md`。

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WM_API_PORT` | 8080 | API 监听端口 |
| `WM_JOBS_STREAM_KEY` | `wm:jobs:stream` | Redis Stream 任务队列 key |
| `WM_JOBS_GROUP` | `wm-workers` | 消费者组名 |
| `WM_JOBS_WORKER_COUNT` | 1 | Worker 进程数 |
| `WM_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接 |
| `COPILOT_ALLOW_JOB_POST` | false | 是否允许 Agent 创建任务 |

## verify-config Self-Check

部署前用脚本自检环境变量与依赖连通性：

```bash
# Linux / macOS
./scripts/verify-config.sh

# Windows PowerShell
.\scripts\verify-config.ps1
```

`verify-config` 会检查：Java 版本、Redis ping、`WM_JOBS_*` 变量、对象存储可达性。失败时退出码非 0。

## Production Deployment Steps

1. 安装 JDK 17+ 与 Redis 6+
2. 复制 `application-prod.yml`，设置 `WM_REDIS_URL` 与对象存储凭证
3. 运行 `verify-config.sh` 或 `verify-config.ps1` 通过自检
4. 启动 API：`java -jar watermark-api.jar --spring.profiles.active=prod`
5. 启动 Worker：`java -jar watermark-worker.jar`，确认订阅 `WM_JOBS_STREAM_KEY`
6. 验证 `GET /actuator/health` 为 UP，并投递一条测试任务

## Task Status Troubleshooting

### QUEUED 长时间不消费

可能原因：

- Worker 未启动或未加入消费者组 `WM_JOBS_GROUP`
- Redis Stream key 配置错误（检查 `WM_JOBS_STREAM_KEY`）
- Redis 不可达或 Stream 被 trim 清空
- `WM_JOBS_WORKER_COUNT=0`

排查：Redis CLI `XINFO GROUPS wm:jobs:stream`，查看 pending 与 lag。

### PROCESSING 长时间不结束

可能原因：

- 大文件下载慢或对象存储超时
- 算法推理耗时（见 README_ALGORITHM.md）
- Worker 进程卡住（线程 dump）
- 下游回调阻塞

排查：查 Worker 日志中的 `jobId`，对照 RUNBOOK.md §2。

### FAILED

见 RUNBOOK.md「失败任务」章节与 REQUIREMENTS 检查表 R-002、R-004。
