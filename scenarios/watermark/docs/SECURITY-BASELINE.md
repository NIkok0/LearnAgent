# Security Baseline

水印平台安全基线（测试知识库）。

## Transport and Session

- 生产环境必须启用 HTTPS；禁止在公网明文传输 `WMSESSIONID`。
- Cookie 属性：`HttpOnly`、`Secure`（生产）、`SameSite=Lax`。
- 会话 TTL 默认 8 小时，可通过 `WM_SESSION_TTL_HOURS` 调整。

## Authentication and Authorization

- 所有 `/api/v1/files`、`/api/v1/jobs/*` 需有效登录会话。
- `/api/v1/admin/*` 需管理员角色；普通用户返回 403。
- Agent 不得要求用户粘贴 Cookie；登录通过 `POST /api/v1/auth/login` 由服务端存储会话。

## API Whitelist (Agent)

LearnAgent 仅允许白名单 HTTP 工具访问以下路径（详见 API-CONTRACT.md）：

- `GET /actuator/health`
- `POST /api/v1/auth/login`
- `GET /api/v1/stats/dashboard`
- `GET /api/v1/files`, `GET /api/v1/files/{id}`
- `GET /api/v1/jobs/{id}`
- `GET /api/v1/admin/stats`, `/users`, `/groups`
- `POST /api/v1/jobs/watermark`（需审批 + `COPILOT_ALLOW_JOB_POST=true`）

禁止访问外部 URL 或未登记路径。

## Secrets Management

- 对象存储密钥、Redis 密码不得写入 Git；使用环境变量或密钥管理服务。
- 日志中脱敏：`password`、`authorization`、`WMSESSIONID`。

## Dangerous Operations

`POST /api/v1/jobs/watermark` 标记为高风险：必须用户显式批准且环境开关开启。
