# LearnAgent Tool / Capability 设计

> **M11 Capability**（ToolSpec 注册与 Handler 执行）与 **M06 Tool-grounded 编排**（规则路由 + safety_gate 协作）的统一设计入口。  
> 不重复 RAG 检索链、HTTP 白名单细节、`ToolResultModel` 契约或 Policy 裁决逻辑。  
> 关联文档：[agent-learning-guide.md](./agent-learning-guide.md)、[demo-requirements-design.md](./demo-requirements-design.md) §2.4 / §3.5 / §4、[rag-design.md](./rag-design.md) §6、[guardrail-policy-design.md](./guardrail-policy-design.md)、[context-manager-design.md](./context-manager-design.md)、[data-flow-design.md](./data-flow-design.md) §2.4、[eval-design.md](./eval-design.md)、[runtime-design.md](./runtime-design.md)

**K/C/S 位置**：**M11** Capability（ToolSpec + Handler + packs）；**M06** Tool-grounded 编排（RouterEngine + route enforce）。Policy 裁决见 [guardrail-policy-design.md](./guardrail-policy-design.md)；RAG 检索链见 [rag-design.md](./rag-design.md)。架构索引见 [guide §2.4](./agent-learning-guide.md)。

---

## 0. 实现状态总览（学习入口）

### Capability（M11）

| 能力 | 状态 | 代码 / 配置锚点 |
|------|------|-----------------|
| ToolSpec + `ToolRegistry.register_async()` | ✅ 已实现 | `tools/registry.py` |
| Capability packs（rag / http / mcp） | ✅ 已实现 | `tools/capability/loader.py` · `CAPABILITY_PACKS` |
| 部署开关 `COPILOT_CAPABILITIES` | ✅ 已实现 | `settings.enabled_capabilities()` |
| Handler 统一入口 | ✅ 已实现 | `agent/tool_handlers.py` |
| MCP 动态工具注册 | ✅ 已实现 | `tools/extensions/mcp/` · `McpCapability` |
| `search_docs` / `http_get` / `http_post` | ✅ 已实现 | `tools/capability/{rag,http}.py` |
| shell / git Capability pack | ❌ 待建 | 见 **§5** · [guide §2.8](./agent-learning-guide.md) |
| `ToolSpec.timeout_seconds` 强制 | ⚠️ 部分 | 声明已有；ExecutionEngine 强制见 [guardrail §5](./guardrail-policy-design.md) |

### Tool-grounded 编排（M06）

| 能力 | 状态 | 代码 / 数据锚点 |
|------|------|-----------------|
| 问题类型五分类（knowledge / live_status / troubleshooting / dangerous_execute / safety_reject） | ✅ 已实现 | `scenario/router/` + `RouterEngine` |
| Planner 注入 Tool routing SystemMessage | ✅ 已实现 | `ContextManager.plan_route()` → `nodes.planner` |
| `plan_created` 事件含 `tool_route` | ✅ 已实现 | `strategy: react_with_safety_gate` |
| `AgentState.tool_route` 跨节点传递 | ✅ 已实现 | `agent/state.py`；assemble 写入 `graph_config.tool_route` |
| safety_gate 路由强制（`tool_allowed`） | ✅ 已实现 | `AGENT_TOOL_ROUTE_ENFORCE=true` |
| Policy + **required_scopes** + 审批与路由双层闸门 | ✅ 已实现 | `policy/registry.py` + `safety_gate`；见 [guardrail-policy-design.md](./guardrail-policy-design.md) |
| `suggested_paths` 规则推断（health / jobs / files / admin） | ✅ 已实现 | `tool_router.py` 正则 + UUID 抽取 |
| System Prompt 与路由文案一致 | ✅ 已实现 | `agent/prompts.py` |
| L5 工具轨迹 proxy 评测（28 case） | ✅ 已实现 | `verify_phase4_tool_trajectory.py`，`eval/tool_trajectory.py` |
| 路由单元测试（28 case 分类） | ✅ 已实现 | `verify_tool_router.py` |
| **检索结果 → API path 结构化注入** | ✅ 已实现 | `rag/api_paths.py` + ingest `api_endpoint`；`suggested_api_paths` / `api_field_hints` |
| **排障结构化诊断模板**（QUEUED / PROCESSING / FAILED） | ✅ 已实现 | `agent/diagnosis.py`；`AGENT_DIAGNOSIS_TEMPLATE_ENABLED` |
| **`retrieval_completed.call_id` 与 tool 关联** | ✅ 已实现 | `agent/tool_call_context.py`，`event_mapper.py`，`tool_handlers.py` |
| **Demo 1–6 golden E2E（proxy）** | ✅ 已实现 | `verify_demo_golden_e2e.py` |
| **L4 citation（L4-lite）** | ✅ 已实现 | `verify_citation_l4.py` |

待办见 **§5**；全局缺口见 [guide §2.8](./agent-learning-guide.md)。

---

## 1. K/C/S 与模块边界

```text
M11 Capability (tools/capability/, agent/tool_handlers.py) ← 本文 §2
  ToolSpec 声明 + Handler 实现 + pack 注册
  不负责 .................... 问题分类、工具调用顺序、Policy 最终裁决

M06 编排 (agent/ + context/) ← 本文 §3
  RouterEngine / tool_router .... 意图分类 + recommended_tools + suggested_paths
  ContextManager.plan_route ..... planner 复用 assemble 内 route
  planner / assistant / safety_gate ... 路由注入 + LLM tool_calls + route enforce

M10 RAG (rag/) — 见 rag-design
  search_docs 检索链、ingest、BM25/向量
  不负责 .................... 是否调用 http_get、工具顺序

M12 Guardrail (policy/, safety_gate) — 见 guardrail-policy
  Scenario allowlist、required_scopes、interrupt 审批
  不负责 .................... 问题分类、RAG 与 API 的组合顺序

M15 Context Manager — 见 context-manager-design
  assemble() 单入口；router + preretrieval + packing
  不负责 .................... ToolSpec 注册、Handler 实现
```

**原则**：Capability **声明并执行**工具；Tool-grounded **决策**工具顺序与禁止项（planner + safety_gate）；Policy **裁决**是否允许执行；RAG **提供**文档证据。

---

## 2. Capability 层（M11）

### 2.1 统一管道

所有外部能力（含 MCP）经同一管道注册；禁止在 Graph 节点内散落裸 HTTP / subprocess。

```text
ToolSpec(name, args_schema, category, risk_level, required_scopes, requires_approval, timeout_seconds)
  → ToolRegistry.register_async()
  → PolicyRegistry（Scenario allowlist ∩ required_scopes ∩ 审批）
  → Handler（async coroutine，agent/tool_handlers.py）
  → ToolResultModel → RuntimeEvent(tool_end)
```

契约细节：[data-flow-design.md](./data-flow-design.md) §2.4；Policy 协作：[guardrail-policy-design.md](./guardrail-policy-design.md)。

### 2.2 部署开关：`COPILOT_CAPABILITIES`

| 环境变量 | 默认 | 含义 |
|----------|------|------|
| `COPILOT_CAPABILITIES` | `rag,http,mcp` | 逗号分隔，决定**部署启用哪些 Capability pack** |
| （空 / `none` / `off`） | — | 零 capability 部署（Kernel smoke） |

- **部署层**开关：由 `settings.enabled_capabilities()` 解析，在 `kernel/bootstrap.py` → `load_capability_packs()` 注册。
- **Scenario 只做收紧**：`policy.tool_allowlist` 与已注册工具取交集，不能放宽 Kernel 默认策略。
- MCP pack 需 Scenario 提供 `config/*-mcp.yaml` 且 bootstrap 已启动 `McpRuntime`。

### 2.3 `CAPABILITY_PACKS`

实现：`copilot_agent/tools/capability/loader.py`

| pack 名 | 类 | 注册工具（典型） |
|---------|-----|------------------|
| `rag` | `RagCapability` | `search_docs` |
| `http` | `HttpCapability` | `http_get`, `http_post` |
| `mcp` | `McpCapability` | Scenario MCP server 暴露的工具（动态） |

未知 pack 名会 `log.warning` 并跳过，不阻断启动。

### 2.4 `category` 与职责

| Capability 类型 | `category` | 典型 risk | 代码锚点 |
|-----------------|------------|-----------|----------|
| RAG 检索 | `memory` | low | `tools/capability/rag.py` · `search_docs` |
| HTTP API | `http` | medium~high | `tools/capability/http.py` · `http_get` / `http_post` |
| MCP | `mcp` | 依 server | `tools/capability/mcp.py` · `tools/extensions/mcp/` |
| Shell | `shell` | high | 待建 · `tools/extensions/shell/` |
| Git | `vcs` | medium~high | 待建 · `tools/extensions/git/` |

`category` 写入 `tool_start` / `tool_end` 审计 payload，供 Timeline 与评测使用。

### 2.5 Do / Don't

**Do**

- 新增能力：实现 `CapabilityPack.register()`，在 `CAPABILITY_PACKS` 注册，**不改** `runner.py` / `runtime/` / `contracts/`。
- 在 `ToolSpec` 上声明 `required_scopes`、`risk_level`、`requires_approval`、`timeout_seconds`。
- Handler 只通过 `ToolHandlers` 返回 `ToolResultModel`（或等价 dict），由 Runtime 写 EventStore。
- HTTP 路径校验委托 Scenario `HttpPathPolicy`（`tools/whitelist.py`），不在 Handler 内硬编码业务 path。

**Don't**

- 在 `nodes.py` / Graph 节点内直接 `httpx` / `subprocess`。
- Capability Handler 绕过 `PolicyRegistry` 或自行写 Run FSM。
- 在 Scenario YAML 中「放宽」仅部署层启用的工具集。
- 把 Tool-grounded 路由规则写进 Capability pack（路由属 M06，见 §3）。

### 2.6 新增 Capability pack 步骤

1. 新建 `copilot_agent/tools/capability/<name>.py`，实现 `CapabilityPack`（`register(registry, ctx)`）。
2. 在 `loader.CAPABILITY_PACKS` 增加 `"<name>": <Name>Capability()`。
3. 在 `agent/tool_handlers.py` 增加对应 async handler（若需共享 deps，经 `CapabilityContext` 注入）。
4. 更新 Scenario `policy.tool_allowlist`（如需）与文档 **§0 / §2.4** 表。
5. 验收：`verify_policy_docs_contract.py`（ToolSpec 与 policy 文档一致）；pack 专项脚本（参考 `verify_mcp_capability.py`）。

启动链摘要（与 [guide §2.4.7](./agent-learning-guide.md) 一致）：

```text
server lifespan
  → load_scenario()
  → McpRuntime.start()                 # mcp pack 依赖
  → build_kernel_components()
       → load_capability_packs(registry, capabilities=settings.enabled_capabilities(), ctx=...)
       → HttpPathPolicy + PolicyRegistry
  → ChatRunner
```

---

## 3. Tool-grounded 编排（M06）

### 3.1 设计动机

司法材料确权 Demo 的用户问题天然混合三类信息需求：

| 类型 | 示例 | 错误做法 |
|------|------|----------|
| **静态知识** | Redis Stream 默认 key、部署步骤 | 调用业务 API 猜配置 |
| **实时状态** | 任务 UUID 当前状态、平台 health | 只查文档、编造 JSON |
| **排障 + 状态** | 任务一直 QUEUED 怎么办 | 只给文档不查任务；或只查 API 不给 Runbook |
| **高风险执行** | 创建水印任务 POST | 未审批直接 enqueue |

**Tool-grounded RAG** 在本项目中的定义：

> 不是「RAG 回答一切」，也不是「Agent 自由调 API」，而是 **按问题类型选择证据源（文档 / API / 二者组合）**，并在 Timeline 中可复盘「先查了哪些文档、再调了哪些工具」。

#### 与相邻方案的边界

| 方案 | 适用 | LearnAgent 为何不单独采用 |
|------|------|---------------------------|
| **纯 RAG 问答** | FAQ、部署说明 | 无法回答「这个 job id 现在什么状态」 |
| **纯 Tool Agent** | 健康检查、CRUD | 易编造 Redis key、Worker 配置、错误码含义 |
| **Prompt 约束 ReAct** | 小模型、少工具 | 换模型 / 加工具后轨迹不稳定；难评测 |
| **Tool-grounded RAG（当前）** | 文档 + 实时 API + 审批 | 规则路由 + Policy 双层 + eval proxy |

---

### 3.2 端到端数据流

#### 3.2.1 图拓扑

```text
[用户消息] Run 创建
    │
    v
planner (nodes.planner)
    │-- ContextManager.plan_route(goal)  # Scenario RouterEngine
    │-- append plan_created { tool_route, available_tools }
    │-- assemble() 已将 route 写入 graph_config（planner 复用，避免二次 route）
    v
assistant
    │-- LLM + bound tools → AIMessage(tool_calls?)
    v
safety_gate
    │-- PolicyRegistry.evaluate_tool_calls (Scenario allowlist + required_scopes + 审批)
    │-- credential_binding_audit（scope 允许/拒绝，无 secret）
    │-- [可选] tool_allowed(route, tool_name) 路由强制
    │-- interrupt → waiting_approval
    v
tools (ToolNode)
    │-- search_docs → RagStore + retrieval_completed
    │-- http_get / http_post → Scenario HTTP 白名单（HttpPathPolicy）
    v
assistant (循环，直至无 tool_calls 或 MAX_ROUNDS)
```

图定义：`agent/graph.py` — `planner → assistant ⇄ safety_gate → tools`。

#### 3.2.2 单次 Turn 内的 Tool-grounded 时序

```text
User: "为什么任务一直 QUEUED？jobId=xxxxxxxx-..."
  │
  ├─ planner: kind=troubleshooting
  │     recommended: search_docs → http_get
  │     suggested_paths: /api/v1/jobs/{uuid}, /actuator/health, ...
  │
  ├─ assistant: tool_calls=[search_docs]
  ├─ tools: retrieval_completed (Runbook, tech-selection)
  ├─ assistant: tool_calls=[http_get /api/v1/jobs/{uuid}]
  ├─ tools: tool_start/end (live status JSON)
  └─ assistant: 最终回答（文档原因 + 实时状态 + 排查步骤）
```

**与纯 knowledge 对比**：`kind=knowledge` 时 `recommended_tools=("search_docs",)` 且 **禁止** `http_get` / `http_post`（`tool_allowed` 硬约束）。

#### 3.2.3 与 EventStore / Timeline 的衔接

| 事件 | Tool-grounded 语义 | Timeline |
|------|-------------------|----------|
| `plan_created` | 含 `tool_route.kind`、`recommended_tools` | `kind: plan_created` |
| `retrieval_completed` | 排障 / 知识类的文档依据 | `kind: retrieval` |
| `tool_start` / `tool_end` | API 调用审计 | `kind: tool` |
| `approval_required` | 高风险 POST 暂停 | `kind: approval` |
| `credential_binding_audit` | PolicyGate scope / 登录存 cookie | Timeline 可选展示（审计轨） |

`retrieval_completed` 与 `tool_start` 通过 `call_id` 关联同一轮检索—执行链（§3.7.3）。

---

### 3.3 问题分类（ToolRouteKind）

`RouterEngine.route()` / `route_tools()` 输出不可变 `ToolRoute`（规则来自 Scenario `config/*-router.yaml`）：

```text
ToolRoute
├── kind: ToolRouteKind
├── recommended_tools: tuple[str, ...]    # 期望调用顺序
├── forbidden_tools: tuple[str, ...]      # 本 turn 禁止
├── suggested_paths: tuple[str, ...]      # 给 LLM 的 http 路径提示（非强制）
└── rationale: str                        # 写入 plan / 调试
```

#### 3.3.1 五类意图与 Demo 映射

| kind | 含义 | recommended_tools（典型） | forbidden | Demo / case |
|------|------|---------------------------|-----------|-------------|
| `knowledge` | 静态文档、API 契约、白名单策略 | `search_docs` | `http_get`, `http_post` | Demo 无直接对应；P4-001–005 |
| `live_status` | 健康、文件、任务 UUID、dashboard | `http_get` 或 `http_post→http_get`（需登录） | — | Demo 1、2；P4-006–008 |
| `troubleshooting` | QUEUED/PROCESSING/FAILED、排查 | `search_docs` → `http_get` | `http_post` | Demo 3；P4-001 |
| `dangerous_execute` | 已确认创建水印任务 | `search_docs` → `http_post` | — | Demo 4–5；P4-009–010 |
| `safety_reject` | 外部 URL、未确认危险 POST | （无） | 全部或 `http_post` | Demo 6；P4-011+ |

#### 3.3.2 分类优先级（规则栈）

`route_tools()` 按以下顺序短路（**先匹配先返回**）：

```text
1. safety_reject .......... evil URL / 未 gates 的危险 POST
2. dangerous_execute ...... confirm_dangerous + allow_job_post + 创建任务意图
3. knowledge .............. API 契约问答（endpoint + 字段/返回/默认）
4. troubleshooting ....... QUEUED|PROCESSING|FAILED / 排查 / 卡住 / 怎么办（**优先于 live_status**）
5. live_status ........... UUID / health / files / stats / admin / 登录链
6. knowledge (default) ... 其余静态问题 → 仅 search_docs
```

**设计取舍**：带 UUID 的排障问句（如 Demo 3）必须走 `troubleshooting`，避免仅 `http_get` 跳过 RAG。

---

### 3.4 ToolRouter 规则设计

实现：`copilot_agent/scenario/router/`（`RouterEngine` + YAML rules）。

#### 3.4.1 关键模式（摘录）

| 信号 | 路由倾向 |
|------|----------|
| `https?://evil`、`evil.example` | `safety_reject` |
| `创建水印任务`、`fileId=`、`POST /api/v1/jobs/watermark`（非契约问法） | `dangerous_execute` 或拦截 |
| `需要哪些字段`、`返回什么`、`契约` + `/api/` | `knowledge`（禁止 HTTP） |
| UUID 或 `是否存活`、`/actuator/health` | `live_status` → `/actuator/health` |
| `QUEUED`、`PROCESSING`、`排查`、`卡住` | `troubleshooting` |
| UUID 在排障句中 | `suggested_paths` 前置 `/api/v1/jobs/{uuid}` |

#### 3.4.2 `suggested_paths` 语义

- **不是** HTTP 白名单的超集；实际可调用路径仍由 **Scenario `HttpPathPolicy`**（`tools/whitelist.py` 委托）校验。
- **是** 给 LLM 的软提示，减少路径幻觉；L5/Demo proxy 优先 `suggested_api_paths[0]`，其次 `tool_route.suggested_paths[0]`。
- **结构化来源**：ingest 解析的 `DocChunk.api_endpoint`（见 [rag-design.md](./rag-design.md) §4.4）→ `extract_api_paths()` 优先于 chunk 文本 regex。

#### 3.4.3 `build_route_system_message`

Planner 注入的 SystemMessage 模板（英文，便于模型遵循）：

```text
Tool routing plan for this user turn (follow before choosing tools):
- Intent: troubleshooting
- Recommended tool order: search_docs -> http_get
- Do not call: http_post
- Suggested API paths (http_get whitelist): /api/v1/jobs/{uuid}, ...
- Rationale: Runbook/deploy docs first, then check live task or platform status.
```

与 Scenario prompt 或内置 `DEFAULT_KERNEL_PROMPT` **叠加**；后者只作为缺省 kernel fallback。

#### 3.4.4 `tool_allowed(route, tool_name)`

| 条件 | 行为 |
|------|------|
| `tool_name in forbidden_tools` | 拒绝 |
| `kind == safety_reject` | 拒绝一切 tool |
| `recommended_tools` 为空 | 拒绝一切 tool |
| `kind == knowledge` 且 tool 为 http_* | 拒绝 |
| 其他 | 允许（仍须过 Policy + 白名单） |

`safety_gate` 在 `AGENT_TOOL_ROUTE_ENFORCE=true` 时对 **整批** `tool_calls` 检查；若有 blocked 工具，返回说明 AIMessage，不进入 ToolNode。

---

### 3.5 Planner 与 AgentState

#### 3.5.1 配置项

| 环境变量 / settings | 默认 | 含义 |
|---------------------|------|------|
| `AGENT_TOOL_ROUTE_ENABLED` | `true` | planner 是否分类并注入 SystemMessage |
| `AGENT_TOOL_ROUTE_ENFORCE` | `true` | safety_gate 是否强制 `tool_allowed` |
| `AGENT_RETRIEVAL_PATH_INJECT` | `true` | `search_docs` 返回 `suggested_api_paths` |
| `AGENT_DIAGNOSIS_TEMPLATE_ENABLED` | `true` | troubleshooting 注入排障 outline SystemMessage |
| `COPILOT_ALLOW_JOB_POST` | `false` | 是否允许危险 POST 路径（部署级） |
| 请求级 `confirm_dangerous` | `false` | 用户是否确认创建任务（Demo 5） |

#### 3.5.2 `plan_created` payload

```text
plan_created
├── goal: str
├── strategy: "react_with_safety_gate"   # Context Manager assemble 路径
├── tool_route: ToolRoute.as_dict()      # 与 assemble 内 route 一致
└── available_tools: ToolSpec.public_dict[]
```

#### 3.5.3 Checkpoint 边界

- `tool_route` 存入 `AgentState`，随 LangGraph checkpoint 续跑。
- routing SystemMessage 在 planner 每轮追加；续轮时由 `MemoryManager` / compactor 策略决定是否保留（见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)）。
- **不**把 RAG 全文 chunk 写入 checkpoint；仅 tool message 摘要 + `retrieval_completed` 事件。

---

### 3.6 与 Guardrail 的双层闸门

Tool-grounded 与 Policy **串联而非替代**：

```text
LLM tool_calls
    │
    v
[层 1] PolicyRegistry
    │-- Scenario tool_allowlist / MCP allowlist
    │-- ToolSpec.required_scopes vs CredentialManager
    │-- COPILOT_ALLOW_JOB_POST / confirm_dangerous
    │-- ToolSpec.requires_approval → interrupt
    v
[层 2] tool_allowed (route enforce)
    │-- knowledge 类误调 http_get → 拦截
    │-- troubleshooting 误调 http_post → 拦截
    v
ToolNode → HTTP 白名单 [层 3]
```

| 场景 | 路由层 | Policy 层 |
|------|--------|-----------|
| Demo 4 未 approve 创建任务 | `dangerous_execute` 或拦截 | `requires_approval` → interrupt |
| Demo 6 外部 URL | `safety_reject` | `allowed=false` |
| 部署类问题误调 health | `knowledge` + enforce | GET 白名单内仍会被 route 拦 |

详情：[guardrail-policy-design.md](./guardrail-policy-design.md) §2、§4。

---

### 3.7 RAG 与 Tool 的组合模式

#### 3.7.1 四种产品模式

| 模式 | kind | 文档角色 | API 角色 |
|------|------|----------|----------|
| **Doc-only** | `knowledge` | 唯一依据 | 禁止 |
| **API-only** | `live_status` | 可选不调用 | 唯一事实源 |
| **Doc then API** | `troubleshooting` | Runbook / 部署解释 | 验证任务/平台状态 |
| **Doc then Approval POST** | `dangerous_execute` | 说明参数与风险 | 审批后 enqueue |

#### 3.7.2 检索驱动工具选择（已实现）

```text
ingest: parse_api_section → DocChunk.api_endpoint / request_fields
    ↓
search_docs → RagStore.search → hits
    ↓
extract_api_paths(hits)  # 优先 chunk.api_endpoint，regex fallback
    ↓
ToolResult.data:
  suggested_api_paths[]
  api_field_hints[]      # endpoint + 字段名 + error_codes 摘要
    ↓
assistant 下一轮 http_get 优先使用 suggested_api_paths
```

| 步骤 | 状态 |
|------|------|
| ingest 结构化 `api_endpoint` | ✅ `rag/api_parse.py` |
| `extract_api_paths` 优先结构化字段 | ✅ `rag/api_paths.py` |
| `search_docs` enrich | ✅ `tool_handlers.py` |
| merge 进 `tool_route.suggested_paths`（planner 二次更新） | ✅ assemble 内 `tool_route` 供 planner 复用；仍靠 LLM 读 ToolMessage 选 path |
| eval 断言 path 来自检索 chunk | ⚠️ Demo 3 golden 已验轨迹；未断言 path 来源文件 |

#### 3.7.3 `retrieval_completed.call_id`（已实现）

```text
GraphEventMapper.on_tool_start → set_tool_call_context(call_id)
    ↓
ToolHandlers.search_docs → get_current_call_id() → retrieval_completed.call_id
    ↓
Timeline: retrieval.call_id == search_docs tool_start.call_id
```

实现：`agent/tool_call_context.py`；验收：`verify_runtime_timeline.py`（`retrieval_call_id_linked`）。

---

### 3.8 排障输出（已实现）

[demo-requirements-design.md](./demo-requirements-design.md) §2.4 要求 Agent 能解释 QUEUED / PROCESSING / FAILED。

**实现**：`agent/diagnosis.py` + `nodes.assistant` 注入 SystemMessage（`AGENT_DIAGNOSIS_TEMPLATE_ENABLED=true`）。

触发条件：`tool_route.kind == troubleshooting` 且 messages 中已有 `search_docs` + `http_get` 的 ToolMessage。

输出结构（固定 markdown 章节）：

```markdown
## 文档依据
## 当前任务状态
## 可能原因
## 建议排查步骤
```

内容来源：静态模板表（Worker / Redis / 算法）+ `http_get` job JSON 的 `status` / `errorCode` + 检索 `sources`。

验收：`python scripts/verify_diagnosis_template.py`。

---

### 3.9 评测设计

Tool-grounded 评测分 **路由分类** 与 **工具轨迹** 两层，均 deterministic、无真实 LLM。

#### 3.9.1 路由分类（L5-pre）

```bash
python scripts/verify_tool_router.py
```

- 输入：`eval/phase4-eval-cases.json` 全部 28 case
- 断言：`route_tools(question)` 的 `kind`、`recommended_tools`、`forbidden_tools` 与 case 期望一致

#### 3.9.2 工具轨迹 proxy（L5）

```bash
python scripts/verify_phase4_tool_trajectory.py
```

| 指标 | 含义 |
|------|------|
| `required_tools_ok` | `expected_tools` 均被执行（支持 `http_post:/path`） |
| `forbidden_tools_ok` | 未调用 forbidden |
| `route_order_ok` | 顺序与 `recommended_tools` 一致 |
| `rag_before_api_ok` | troubleshooting：`search_docs` 先于 `http_get` |
| `blocked_ok` | `expect_blocked` 时零 tool |
| `tool_trajectory_pass_rate` | 28 case 通过率 |

**局限**：mock LLM **严格按 route 调用**；不测真实模型是否遵循 SystemMessage。真实 LLM E2E 见 §3.9.4。

#### 3.9.3 与 RAG L1 的关系

| 层级 | 脚本 | 测什么 |
|------|------|--------|
| L1 检索 | `verify_phase4_ragas.py` | `required_sources` 是否命中 |
| L4-lite | `verify_citation_l4.py` | 回答是否 cite 文件名 |
| L5 轨迹 | `verify_phase4_tool_trajectory.py` | 工具种类、顺序、拦截 |
| Demo golden | `verify_demo_golden_e2e.py` | Demo 1–6 组合断言 |
| L3 | RAGAS | faithfulness — **非 PR 门禁** |

#### 3.9.4 Demo 脚本验收矩阵

| Demo | 期望轨迹 | proxy 覆盖 | 真实 LLM |
|------|----------|:----------:|:----------:|
| Demo 1 health | `http_get /actuator/health` | ✅ `demo_01` | ❌ |
| Demo 2 job 状态 | `http_get /api/v1/jobs/{id}` | ✅ `demo_02` | ❌ |
| Demo 3 QUEUED 排查 | `search_docs` → `http_get` | ✅ `demo_03` | ❌ |
| Demo 4 拦截 POST | 无 `http_post` | ✅ `demo_04` | ❌ |
| Demo 5 approve POST | `http_post` watermark | ✅ `demo_05` | ❌ |
| Demo 6 非法 URL | 零 tool | ✅ `demo_06` | ❌ |

```bash
python scripts/verify_demo_golden_e2e.py
```

聚合 profile 见 [README §6](../README.md)。**仍缺**：`--mode live` — 见 [guide §2.8](./agent-learning-guide.md)。

---

### 3.10 配置与开关

| 变量 | 默认 | 说明 |
|------|------|------|
| `AGENT_TOOL_ROUTE_ENABLED` | `true` | 关闭则退化为纯 ReAct + Policy |
| `AGENT_TOOL_ROUTE_ENFORCE` | `true` | 关闭则仅提示路由，不硬拦 tool_calls |
| `AGENT_RETRIEVAL_PATH_INJECT` | `true` | `search_docs` 返回 path/字段 hints |
| `AGENT_DIAGNOSIS_TEMPLATE_ENABLED` | `true` | troubleshooting 排障 outline |
| `COPILOT_ALLOW_JOB_POST` | `false` | 与 Demo 4/5 环境一致 |
| `MAX_ROUNDS` | `12` | `agent/prompts.py`；防止 tool 循环 |

本地调试 excerpt：

```bash
# 只看路由分类
python scripts/verify_tool_router.py

# 完整 L5 proxy
python scripts/verify_phase4_tool_trajectory.py

# Demo golden（6 case）
python scripts/verify_demo_golden_e2e.py
```

---

### 3.11 方案选型：规则路由 vs LLM Planner

| 方案 | 优点 | 缺点 | LearnAgent 选择 |
|------|------|------|-----------------|
| **纯 Prompt ReAct** | 实现简单 | 轨迹不稳定、难评测 | 已弃用为主路径 |
| **规则 ToolRouter（当前）** | 可测、可解释、零额外 token | 口语覆盖有限 | ✅ P0–P3 默认 |
| **LLM 意图分类** | 泛化好 | 波动、需 judge | §5 目标 |
| **Plan-and-Execute** | 多步任务清晰 | 复杂度高 | 远期（tech-selection §4） |

当前 **planner 节点名** 保留，但实现是 **deterministic router** 而非 LLM 规划；`plan_created.strategy=react_with_safety_gate` 反映「路由 + ReAct」而非完整 Plan-and-Execute。

---

## 4. 与相邻模块

| 模块 | 本文覆盖 | SSOT 文档 |
|------|----------|-----------|
| RAG ingest / 检索 / BM25 | `search_docs` Handler 与 path 注入 | [rag-design.md](./rag-design.md) |
| Policy / 审批 / scope | ToolSpec 声明 + 双层闸门 | [guardrail-policy-design.md](./guardrail-policy-design.md) |
| Context 装配 / preretrieval | planner 复用 `assemble()` 内 route | [context-manager-design.md](./context-manager-design.md) |
| `ToolResultModel` / SSE | Handler 输出契约 | [data-flow-design.md](./data-flow-design.md) |

---

## 5. 八层栈改造分配（待办）

与 [agent-learning-guide §7 L5–L6](./agent-learning-guide.md) 对齐；全局缺口见 [guide §2.8](./agent-learning-guide.md)。

| 层 | 任务 | 验收 |
|-----|------|------|
| **L5** | planner 硬 merge：`suggested_api_paths` → 更新 `tool_route.suggested_paths` | e2e 扩展 |
| **L5** | `plan_updated` 事件 + 步骤 `outcome`（Plan-and-Execute PoC） | golden 扩展 |
| **L6** | 真实 LLM E2E `--mode live` | `verify_demo_golden_e2e.py --mode live` |
| **L5** | LLM 意图分类 fallback（规则优先，LLM 兜底） | 新 eval case |
| **L6** | shell / git Capability pack + `scenarios/coding/` 示例 | 新 verify + guide §2.8 |
| **L6** | `ToolSpec.timeout_seconds` → ExecutionEngine 强制 | runtime + tool 单测 |
| **L5** | 多 Agent / 子目标分解 | [tech-selection §4](./tech-selection-design.md) |

---

## 6. 代码索引

| 模块 | 路径 |
|------|------|
| Capability loader | `copilot_agent/tools/capability/loader.py` |
| Capability packs | `copilot_agent/tools/capability/{rag,http,mcp}.py` |
| ToolSpec / Registry | `copilot_agent/tools/registry.py` |
| Handlers | `copilot_agent/agent/tool_handlers.py` |
| Kernel bootstrap | `copilot_agent/kernel/bootstrap.py` |
| MCP 扩展 | `copilot_agent/tools/extensions/mcp/` |
| 路由核心 | `copilot_agent/scenario/router/`（`engine.py`、`schema.py`） |
| Context 单入口 | [context-manager-design.md](./context-manager-design.md) · `copilot_agent/context/manager.py` |
| 排障模板 | `copilot_agent/agent/diagnosis.py` |
| call_id 上下文 | `copilot_agent/agent/tool_call_context.py` |
| API path 提取 | `copilot_agent/rag/api_paths.py` |
| API ingest 解析 | `copilot_agent/rag/api_parse.py` |
| Planner / safety_gate | `copilot_agent/agent/nodes.py` |
| 图拓扑 | `copilot_agent/agent/graph.py` |
| State | `copilot_agent/agent/state.py` |
| System Prompt | `copilot_agent/agent/prompts.py` |
| retrieval payload | `copilot_agent/contracts/events/retrieval.py` |
| L4 citation | `copilot_agent/eval/citation.py` |
| 轨迹评测 | `copilot_agent/eval/tool_trajectory.py` |
| 数据集 | `eval/phase4-eval-cases.json`，`eval/golden/demo-golden-scenarios.json` |
| verify 脚本 | `scripts/verify_tool_router.py`，`scripts/verify_phase4_tool_trajectory.py`，`scripts/verify_demo_golden_e2e.py`，`scripts/verify_mcp_capability.py` |

---

## 7. 文档关系

| 方向 | 文档 |
|------|------|
| **上游** | [agent-learning-guide.md](./agent-learning-guide.md) §2.4（K/C/S）、§6 索引 |
| **下游** | [demo-requirements-design.md](./demo-requirements-design.md)（Demo 轨迹）、[eval-design.md](./eval-design.md)（L5 分层） |
| **并列 SSOT** | [rag-design.md](./rag-design.md)（M10）、[guardrail-policy-design.md](./guardrail-policy-design.md)（M12）、[context-manager-design.md](./context-manager-design.md)（M15）、[data-flow-design.md](./data-flow-design.md)（契约） |

维护规则（与 [guide §1](./agent-learning-guide.md) 一致）：

- Capability 新增/改 ToolSpec → 改 **§2** + 代码
- Tool-grounded 路由/评测 → 改 **§3**
- guide §2.4.3 只保留 K/C/S 边界与链接，不写实现细节

可选阅读：Tool use / Function calling → LangChain bound tools + ToolNode；Grounding → RAG 摘录 + API JSON；HITL → Policy interrupt；Agent 评测 → 轨迹断言优于单一 BLEU。
