# API Contract — Watermark Java Backend

REST API 契约（v1）。Agent 仅可调用白名单内路径。

## Base URL

- 开发：`http://localhost:8080`
- 生产：由 `WM_API_BASE_URL` 配置

## Authentication

### POST /api/v1/auth/login

登录并下发 `WMSESSIONID` Cookie。

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| username | string | yes | 用户名 |
| password | string | yes | 密码 |

Response 200：

```json
{ "success": true, "userId": "uuid" }
```

## Health

### GET /actuator/health

存活探针。Response：`{"status":"UP"}`。

## Stats

### GET /api/v1/stats/dashboard

匿名可访问的仪表盘统计（公开指标）。

## Files

### GET /api/v1/files

列出当前用户可见文件（需登录）。

### GET /api/v1/files/{id}

单个文件元数据与处理状态。

## Jobs

### GET /api/v1/jobs/{id}

查询水印任务状态。`status` 枚举：`QUEUED` | `PROCESSING` | `COMPLETED` | `FAILED`。

### POST /api/v1/jobs/watermark

创建水印任务（高风险，需审批与环境开关 `COPILOT_ALLOW_JOB_POST=true`）。

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| fileId | long | yes | 文件 ID |
| watermarkText | string | yes | 水印文本 |
| algorithmType | string | no | 默认 `DWT` |

## Admin (requires admin role)

### GET /api/v1/admin/stats

### GET /api/v1/admin/users

### GET /api/v1/admin/groups

## Error Model

| HTTP | code | meaning |
|------|------|---------|
| 401 | UNAUTHORIZED | 未登录或会话过期 |
| 403 | FORBIDDEN | 无权限 |
| 404 | NOT_FOUND | 资源不存在 |
| 422 | VALIDATION_ERROR | 参数非法 |
| 500 | INTERNAL_ERROR | 服务端异常 |
