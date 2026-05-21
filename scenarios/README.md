# scenarios/ — 业务数据（非 manifest）

**Scenario overlay 只从 `config/<name>.yaml` 加载。** 本目录仅保留与业务绑定的**数据**，不再放 `scenario.yaml` / policy / router 等 manifest。

| 路径 | 用途 |
|------|------|
| `minimal/docs/` | Kernel smoke 语料 |
| `watermark/docs/` | watermark Demo RAG 语料（Markdown） |
| `watermark/mcp/` | watermark Demo MCP server 脚本 |

加载入口：`SCENARIO=watermark` → `config/watermark.yaml`（见 `copilot_agent/scenario/loader.py`）。

旧目录树 `scenarios/<name>/scenario.yaml` 不再支持；新增场景必须添加 `config/<name>.yaml`。
