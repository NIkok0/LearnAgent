# LearnAgent Capability / Skills / MCP 设计

> **本文负责**：Capability 层定位、Skill Pack 声明式能力注入、MCP 协议接入、三者边界与验证入口。  
> **本文不负责**：Tool policy 具体裁决、RAG 检索算法、Run FSM、外部 skill 安装、MCP marketplace。  
> **相关文档**：[agent-learning-guide.md](./agent-learning-guide.md)、[tool-design.md](./tool-design.md)、[guardrail-policy-design.md](./guardrail-policy-design.md)、[context-manager-design.md](./context-manager-design.md)、[rag-design.md](./rag-design.md)

---

## 1. 定位

LearnAgent 的 Capability 层不是一个单独功能，而是一组把“Agent 可以使用什么能力”收口治理的机制：

- **Capability Pack**：把可执行能力注册进 `ToolRegistry`，例如 RAG、HTTP、MCP。它负责 ToolSpec、handler、参数 schema、risk level、required scopes、timeout 等工具入口声明。
- **Skill Pack**：Scenario 启用的声明式能力包，用于把任务说明、触发条件、推荐工具、RAG 文档提示、安全约束注入上下文。Skill v1 不执行代码，也不替代 ToolRegistry。
- **MCP**：外部工具、资源、Prompt 的协议接入层。LearnAgent 通过 MCP runtime 发现或加载外部 server 暴露的 tools，再转换为受 ToolRegistry / PolicyGate 管理的工具。

一句话边界：**Capability 声明和注册工具，Skill 提示如何使用能力，MCP 引入外部能力，PolicyGate 决定最终能不能执行。**

---

## 2. 三者边界

| 模块 | 负责 | 不负责 | 主要代码 |
| --- | --- | --- | --- |
| Capability Pack | 注册工具、绑定 handler、声明 schema / risk / scopes / timeout | 决定本轮是否该调用工具、审批裁决 | `copilot_agent/tools/capability/`、`copilot_agent/tools/registry.py` |
| Skill Pack | 任务触发、上下文 instructions、推荐工具、所需 capability | 执行工具、绕过权限、动态安装代码 | `copilot_agent/skills/`、`skills/*/skill.yaml` |
| MCP Runtime | 加载 MCP server 配置、管理连接、调用 MCP tool/resource/prompt | 替代 ToolRegistry 或 PolicyGate | `copilot_agent/tools/extensions/mcp/` |
| ContextManager | 选择 skill、注入 skill system message、记录 context 元数据 | 产生业务事实、执行工具 | `copilot_agent/context/manager.py` |
| PolicyGate | allow / ask / deny、审批、scope 与 side effect 约束 | 提供工具实现、选择 skill | `copilot_agent/policy/`、`guardrail-policy-design.md` |

核心原则：

- Skill 只能增强上下文，不能扩大 Scenario policy、credential scope、approval 或 side effect ledger。
- MCP tool 进入系统后必须变成 ToolRegistry 条目，不能绕过统一工具审计。
- Capability Pack 提供“可用能力”，Planner / Safety Gate / PolicyGate 决定“本轮怎么用、能不能用”。
- EventStore 是产品事实源，Capability / Skill / MCP 只写入治理元数据，不把 secret、cookie、raw 用户输入写进事件 payload。

---

## 3. Skills v1

### 3.1 Skill 是声明式能力包

Skill v1 使用本地仓库内的 `skills/<name>/skill.yaml`，由 `SkillRegistry` 加载。典型字段：

| 字段 | 含义 |
| --- | --- |
| `name` | skill 名称 |
| `description` | 对 API preview 和模型上下文可见的简短描述 |
| `triggers.keywords` | 中英文关键词触发条件 |
| `triggers.routes` | route 类型弱触发条件 |
| `instructions` | 注入模型的能力说明和约束 |
| `tool_allowlist` | 推荐使用的工具列表，不等于最终授权 |
| `required_capabilities` | 注入该 skill 所需的 capability |
| `docs_dir` | skill 相关文档目录提示 |
| `risk_level` | skill 自身风险提示，取值 `low` / `medium` / `high` |

Skill 不包含 Python 入口，不执行任意代码，不做外部安装、签名校验或 marketplace。它更接近“Scenario 能力说明书”，而不是插件系统。

### 3.2 选择与注入规则

`select_skills()` 使用确定性打分：

- keyword 命中是强信号。
- route 命中是弱信号。
- 分数达到阈值后才进入候选，避免只因为 `knowledge` 等泛 route 就泛化误命中。
- 结果包含 `selection_score`、`trigger_reasons`、`missing_capabilities`、`injected`。

缺 capability 时的行为：

- API preview 仍可以展示该 skill，并提示缺失 capability。
- Context 注入时不注入该 skill 的 instructions。
- `skill_selected` 事件记录治理字段，但不记录用户原文或 raw payload。

这样可以同时满足两个目标：前端/调试视图能解释“为什么没注入”，模型上下文不会收到不可执行能力的误导性指令。

### 3.3 API 与事件

当前 Skills v1 的主要入口：

- `GET /v1/skills`：查看当前可加载的 skill。
- `GET /v1/threads/{thread_id}/skills?goal=...`：按 goal 预览 skill 选择结果。
- `skill_selected` event：记录选中 skill、分数、是否注入、缺失 capability 等治理元数据。

`skill_selected` 只用于观测与审计，不改变 SSE / REST / Run 状态机协议。

---

## 4. MCP 接入

### 4.1 配置与 transport

MCP server 通过 Scenario 或配置文件声明，示例位于：

- `config/mcp_demo-mcp.yaml`
- `config/watermark-mcp.yaml`
- `config/mcp_http_example.yaml`

当前支持的 transport：

| transport | 用途 |
| --- | --- |
| `mock` | 本地 deterministic 验证与 demo |
| `stdio` | 启动本地 MCP server 进程 |
| `sse` | 连接远端 SSE MCP server |
| `streamable-http` | 连接支持 streamable HTTP 的 MCP server |

`McpServerDefinition` 可以声明 `tools`、`resources`、`prompts`，也可以开启 `discover_tools`、`discover_resources`、`discover_prompts` 从 server 侧发现能力。

### 4.2 工具注册流程

MCP 的接入链路：

```text
Scenario MCP config
  -> McpRuntime.start()
  -> McpToolHandlers.bind_tool(server, tool)
  -> McpCapability.register()
  -> register_mcp_tools()
  -> ToolRegistry.register_async()
  -> PolicyGate / ExecutionEngine 统一执行治理
```

MCP tool 会被转换为 ToolRegistry 里的普通工具条目：

- 工具名通过 `mcp_registry_tool_name(server, tool)` 生成，避免不同 server 的 tool 名冲突。
- args schema 由 MCP `input_schema` 转换为本地 tool schema。
- `risk_level`、`requires_approval`、`required_scopes`、`timeout_seconds` 进入 ToolSpec。
- handler 最终调用 `McpToolHandlers.invoke()`，结果再标准化为 LearnAgent 的 tool result。

因此 MCP 是“外部能力接入协议”，不是独立执行通道。模型看到的是受控工具，运行时执行的是统一治理后的 ToolSpec。

### 4.3 Resource 与 Prompt

MCP resources / prompts 可以作为外部 server 暴露的补充能力，但在 LearnAgent v1 中要遵守两个边界：

- resource 不能直接作为无权限数据源塞进 prompt，必须经过 Scenario / Context / Policy 约束。
- prompt template 只能作为上下文或工具说明的辅助材料，不能覆盖系统级安全策略。

---

## 5. 安全与治理

Capability / Skills / MCP 的统一安全原则：

| 原则 | 说明 |
| --- | --- |
| Scenario 优先 | Scenario allowlist、policy、credential scope 是最终边界 |
| Skill 只提示 | Skill instructions 不能授权工具，也不能绕过审批 |
| MCP 受控注册 | MCP tool 必须进入 ToolRegistry，执行前经过 PolicyGate |
| 高风险显式治理 | `requires_approval`、`risk_level`、`required_scopes`、side effect ledger 必须保留 |
| 缺能力不注入 | `required_capabilities` 不满足时，不把 skill instructions 注入模型 |
| 事件最小化 | event payload 只记录治理元数据，不写 secret、cookie、raw response |

这套边界的目标是避免三类问题：

1. Skill 写了“可以调用某工具”，但 Scenario 未授权。
2. MCP server 暴露了高风险工具，但绕过本地审批与审计。
3. Context 注入了缺 capability 的执行建议，诱导模型生成不可执行或越权 tool call。

---

## 6. 与相邻模块边界

| 相邻模块 | Capability 文档说明 | 详细设计归属 |
| --- | --- | --- |
| Tool Governance | 只说明 MCP / Capability 如何注册 ToolSpec | [tool-design.md](./tool-design.md) |
| Guardrail / HITL | 只说明最终裁决不能被 Skill/MCP 绕过 | [guardrail-policy-design.md](./guardrail-policy-design.md) |
| Context Manager | 只说明 skill selection 与 system message 注入 | [context-manager-design.md](./context-manager-design.md) |
| RAG | 只说明 RAG 可以作为 capability / skill 推荐工具 | [rag-design.md](./rag-design.md) |
| Runtime | 只说明事件与审计元数据，不描述 Run FSM | [runtime-design.md](./runtime-design.md) |

---

## 7. 验证入口

常用验证：

```bash
python scripts/verify_skills_v1.py
python scripts/verify_mcp_capability.py
python scripts/verify_context_manager.py
python scripts/verify_eval_suite.py --profile core-fast
python -m compileall copilot_agent scripts
```

验收重点：

- 中文 trigger 能正确命中 skill。
- route-only 低分不会泛化注入。
- 缺 capability 的 skill 可以 preview，但不会进入 system prompt。
- MCP mock / stdio / policy 注册路径可验证。
- MCP tool 进入 ToolRegistry 后仍受 risk、scope、approval 和 timeout 约束。

---

## 8. 非目标

Capability / Skills / MCP v1 暂不做：

- 外部 skill 安装、版本仓库、签名校验或 marketplace。
- Skill Python 插件执行。
- MCP server 的开放式公网自动发现。
- 让 MCP resource 绕过 RAG / Policy 直接进入 prompt。
- 让 Skill 改写 Run 状态机、SSE 协议或 REST 协议。

后续如果要扩展外部 skill 或远端 MCP marketplace，需要先补齐签名校验、来源信任、租户隔离、沙箱执行、供应链审计和回滚策略。
