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
| `minimal.yaml` | Kernel smoke scenario |

**Capability 开关**不在 Scenario 里：由 `COPILOT_CAPABILITIES=rag,http,mcp` 控制（部署层）。

**Memory 分层**：部署开关与 store 路径在 `settings` / env；业务调参在 `config/*-memory.yaml`（overlay Kernel 默认，只能覆盖字段不能改代码）。

语料和业务脚本在 `scenarios/<name>/`，但 manifest 只放在 `config/`。

旧目录树 `scenarios/<name>/scenario.yaml` 不再支持；新增场景必须添加 `config/<name>.yaml`。
