# Scenario 配置（扁平）

业务 overlay 入口：`config/<name>.yaml`（由 `SCENARIO` 环境变量选择，默认 `minimal`）。

| 文件 | 内容 |
|------|------|
| `watermark.yaml` | policy（内联）、budgets、路径指针 |
| `watermark-prompt.md` | system prompt |
| `watermark-router.yaml` | 工具路由规则 |
| `watermark-mcp.yaml` | MCP server 声明 |
| `watermark-memory.yaml` | 记忆策略 overlay（recall top_k 等业务调参） |
| `watermark-rag.yaml` | RAG query rewrite / doc_type boost |
| `watermark-diagnosis.yaml` | troubleshooting 诊断模板 |
| `mcp_demo.yaml` | MCP demo scenario |
| `mcp_demo-mcp.yaml` | MCP demo mock server |
| `mcp_http_example.yaml` | MCP streamable-http example (resources, prompts, reconnect) |
| `minimal.yaml` | Kernel smoke scenario |

**Capability 开关**不在 Scenario 里：由 `COPILOT_CAPABILITIES=rag,http,mcp` 控制（部署层）。

**Memory 分层**：部署开关与 store 路径在 `settings` / env；业务调参在 `config/*-memory.yaml`（overlay Kernel 默认，只能覆盖字段不能改代码）。

语料和业务脚本在 `scenarios/<name>/`，但 manifest 只放在 `config/`。

旧目录树 `scenarios/<name>/scenario.yaml` 不再支持；新增场景必须添加 `config/<name>.yaml`。

---

## MCP 配置

MCP server 在独立的 YAML 文件中声明，通过场景 `config/<name>.yaml` 中的 `mcp:` 字段引用。

### 支持的 transport

| Transport | 说明 | 需要 |
|-----------|------|------|
| `mock` | 进程内模拟，用于测试 | 无 |
| `stdio` | 子进程通信 | `mcp>=1.6.0` |
| `sse` | Server-Sent Events | `mcp>=1.6.0` |
| `streamable-http` | 双向 HTTP (MCP 2025 新规范) | `mcp>=1.6.0` 或 `httpx` |

### 自动发现

- `discover_tools: true` — 启动时调用 `list_tools` 发现工具
- `discover_resources: true` — 启动时调用 `list_resources` 发现资源
- `discover_prompts: true` — 启动时调用 `list_prompts` 发现提示模板

### 自动重连

```yaml
reconnect:
  enabled: true
  max_retries: 10
  backoff_base_s: 1.0      # 首次重试延迟
  backoff_max_s: 60.0       # 最大延迟
  health_check_interval_s: 15.0  # 健康检查间隔
```

指数退避：1s → 2s → 4s → 8s → 16s → 32s → 60s (max)，带 10% jitter。

### 动态管理 API

```
GET    /v1/mcp/servers                 # 列出所有 MCP 服务器及状态
POST   /v1/mcp/servers                 # 运行时添加 MCP 服务器
DELETE /v1/mcp/servers/{name}          # 断开并移除
POST   /v1/mcp/servers/{name}/reconnect # 手动重连
```
