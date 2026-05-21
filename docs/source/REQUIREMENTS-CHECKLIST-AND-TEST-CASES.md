# Requirements Checklist and Test Cases

司法材料确权水印平台需求检查表（测试知识库版本）。

## Known Deviations and Risk Points

以下为本期 Demo / 测试环境的已知偏差与风险点（requirements checklist）：

| ID | 类别 | 偏差或风险 | 影响 | 缓解措施 |
|----|------|------------|------|----------|
| R-001 | 队列 | Worker 默认单实例，高并发下 QUEUED 堆积 | 任务延迟 | 水平扩展 `WM_JOBS_WORKER_COUNT` |
| R-002 | 存储 | 对象存储断连时 PROCESSING 任务可能超时 | 任务 FAILED | 配置重试与 RUNBOOK §3 |
| R-003 | API | 匿名用户无法查询任务详情 | 功能受限 | 先 `POST /api/v1/auth/login` |
| R-004 | 算法 | TIFF 超大页仅支持 DWT 模式 | 部分文件失败 | 见 README_ALGORITHM.md |
| R-005 | 安全 | 生产未强制 HTTPS 时 Cookie 明文传输 | 会话泄露 | SECURITY-BASELINE.md §2 |
| R-006 | 部署 | verify-config 未纳入 CI 门禁 | 配置漂移 | DEPLOY-SERVER.md §2 |

## Test Cases (Smoke)

- TC-01：健康检查 `GET /actuator/health` 返回 UP
- TC-02：登录后 `GET /api/v1/files` 返回文件列表
- TC-03：`search_docs` 能检索 DEPLOY-SERVER 中 verify-config 说明
- TC-04：未审批时 `POST /api/v1/jobs/watermark` 被 Agent 拦截

## Acceptance Notes

- 回答「已知偏差或风险点」时必须引用本检查表条目，不得编造未登记风险。
