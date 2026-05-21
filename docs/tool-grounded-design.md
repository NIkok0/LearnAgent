# LearnAgent Tool-grounded RAG 设计

> 说明 Agent 如何按问题类型组合 **RAG 检索** 与 **受控 HTTP 工具**，并在 planner / safety_gate 层落地「先查文档、再查实时状态、危险操作须审批」的产品语义；不重复 RAG 检索链路与 HTTP 白名单细节。  
> 关联文档：[agent-learning-guide.md](./agent-learning-guide.md)、[demo-requirements-design.md](./demo-requirements-design.md) §2.4 / §3.5 / §4、[rag-design.md](./rag-design.md) §6、[guardrail-policy-design.md](./guardrail-policy-design.md)、[data-flow-design.md](./data-flow-design.md) §2.4、[eval-design.md](./eval-design.md)、[runtime-design.md](./runtime-design.md)

**文档定位**：本项目 **M06 编排层** 中 Tool-grounded 行为的**实现设计**（意图分类 → 工具计划 → 执行约束 → 评测）。M10 负责「找到文档」；M11 负责「调用 API」；本文档负责「何时用哪条链路、以什么顺序」。

---

## 0. 实现状态总览（学习入口）

| 能力 | 状态 | 代码 / 数据锚点 |
|------|------|-----------------|
| 问题类型五分类（knowledge / live_status / troubleshooting / dangerous_execute / safety_reject） | ✅ 已实现 | `agent/tool_router.py` → `route_tools()` |
| Planner 注入 Tool routing SystemMessage | ✅ 已实现 | `agent/nodes.py` → `planner()` |
| `plan_created` 事件含 `tool_route` | ✅ 已实现 | `strategy: tool_grounded_react` |
| `AgentState.tool_route` 跨节点传递 | ✅ 已实现 | `agent/state.py` |
| safety_gate 路由强制（`tool_allowed`） | ✅ 已实现 | `AGENT_TOOL_ROUTE_ENFORCE=true` |
| Policy + 审批与路由双层闸门 | ✅ 已实现 | `policy/` + `safety_gate`；见 [guardrail-policy-design.md](./guardrail-policy-design.md) |
| `suggested_paths` 规则推断（health / jobs / files / admin） | ✅ 已实现 | `tool_router.py` 正则 + UUID 抽取 |
| System Prompt 与路由文案一致 | ✅ 已实现 | `agent/prompts.py` |
| L5 工具轨迹 proxy 评测（28 case） | ✅ 已实现 | `verify_phase4_tool_trajectory.py`，`eval/tool_trajectory.py` |
| 路由单元测试（28 case 分类） | ✅ 已实现 | `verify_tool_router.py` |
| **检索结果 → API path 结构化注入** | ✅ 已实现 | `rag/api_paths.py` + ingest `api_endpoint`；`suggested_api_paths` / `api_field_hints` |
| **排障结构化诊断模板**（QUEUED / PROCESSING / FAILED） | ✅ 已实现 | `agent/diagnosis.py`；`AGENT_DIAGNOSIS_TEMPLATE_ENABLED` |
| **`retrieval_completed.call_id` 与 tool 关联** | ✅ 已实现 | `agent/tool_call_context.py`，`event_mapper.py`，`tool_handlers.py` |
| **Demo 1–6 golden E2E（proxy）** | ✅ 已实现 | `eval/golden/demo-golden-scenarios.json`，`verify_demo_golden_e2e.py`，`--profile e2e` |
| **L4 citation（L4-lite）** | ✅ 已实现 | `eval/citation.py`，`verify_citation_l4.py` |
| **真实 LLM E2E**（`--mode live`） | ❌ 未实现 | proxy mock LLM 已 PASS；无 key 时 SKIP |
| LLM 意图分类 / Plan-and-Execute | ❌ 未实现 | 当前全规则路由 |

**成熟度**：**中高** — 规则路由 + 检索注入 + 排障模板 + L4/L5 proxy + Demo golden 已闭环；**真实 LLM 轨迹**仍是主要缺口。

---

## 1. 设计动机

司法材料确权 Demo 的用户问题天然混合三类信息需求：

| 类型 | 示例 | 错误做法 |
|------|------|----------|
| **静态知识** | Redis Stream 默认 key、部署步骤 | 调用业务 API 猜配置 |
| **实时状态** | 任务 UUID 当前状态、平台 health | 只查文档、编造 JSON |
| **排障 + 状态** | 任务一直 QUEUED 怎么办 | 只给文档不查任务；或只查 API 不给 Runbook |
| **高风险执行** | 创建水印任务 POST | 未审批直接 enqueue |

**Tool-grounded RAG** 在本项目中的定义：

> 不是「RAG 回答一切」，也不是「Agent 自由调 API」，而是 **按问题类型选择证据源（文档 / API / 二者组合）**，并在 Timeline 中可复盘「先查了哪些文档、再调了哪些工具」。

### 1.1 与相邻方案的边界

| 方案 | 适用 | LearnAgent 为何不单独采用 |
|------|------|---------------------------|
| **纯 RAG 问答** | FAQ、部署说明 | 无法回答「这个 job id 现在什么状态」 |
| **纯 Tool Agent** | 健康检查、CRUD | 易编造 Redis key、Worker 配置、错误码含义 |
| **Prompt 约束 ReAct** | 小模型、少工具 | 换模型 / 加工具后轨迹不稳定；难评测 |
| **Tool-grounded RAG（当前）** | 文档 + 实时 API + 审批 | 规则路由 + Policy 双层 + eval proxy |

### 1.2 模块责权（与 RAG / Guardrail 分界）

```text
M10 RAG (rag/)
  search_docs ............... 返回 DocChunk 摘录 + retrieval_completed
  不负责 .................... 是否调用 http_get、工具顺序

M06 编排 (agent/) ← 本文档范围
  tool_router ............... 意图分类 + recommended_tools + suggested_paths
  planner ................... 写 plan_created、注入 routing SystemMessage
  assistant ................. LLM 产出 tool_calls（应遵循 routing plan）
  safety_gate ............... Policy 审批 + route enforce

M12 Guardrail (policy/, safety_gate)
  不负责 .................... 问题分类、RAG 与 API 的组合顺序
  负责 ...................... 危险 POST、白名单外路径、interrupt 审批

M11 Tool (tools/)
  http_get / http_post ...... 白名单硬边界（最后一道）
```

**原则**：Tool-grounded **决策**在 planner；**执行许可**在 safety_gate + HTTP 白名单；**证据内容**在 RAG / API 响应。

---

## 2. 端到端数据流

### 2.1 图拓扑

```text
[用户消息] Run 创建
    │
    v
planner (nodes.planner)
    │-- route_tools(last_user_content)
    │-- append plan_created { tool_route, available_tools }
    │-- messages += SystemMessage(build_route_system_message)
    v
assistant
    │-- LLM + bound tools → AIMessage(tool_calls?)
    v
safety_gate
    │-- PolicyRegistry.evaluate_tool_calls (审批 / 拦截)
    │-- [可选] tool_allowed(route, tool_name) 路由强制
    │-- interrupt → waiting_approval
    v
tools (ToolNode)
    │-- search_docs → RagStore + retrieval_completed
    │-- http_get / http_post → 白名单 HTTP
    v
assistant (循环，直至无 tool_calls 或 MAX_ROUNDS)
```

图定义：`agent/graph.py` — `planner → assistant ⇄ safety_gate → tools`。

### 2.2 单次 Turn 内的 Tool-grounded 时序

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

### 2.3 与 EventStore / Timeline 的衔接

| 事件 | Tool-grounded 语义 | Timeline |
|------|-------------------|----------|
| `plan_created` | 含 `tool_route.kind`、`recommended_tools` | `kind: plan_created` |
| `retrieval_completed` | 排障 / 知识类的文档依据 | `kind: retrieval` |
| `tool_start` / `tool_end` | API 调用审计 | `kind: tool` |
| `approval_required` | 高风险 POST 暂停 | `kind: approval` |

`retrieval_completed` 与 `tool_start`  ideally 通过 `call_id` 关联同一轮检索—执行链；**当前 handler 未传 call_id**（§8.3）。

---

## 3. 问题分类（ToolRouteKind）

`route_tools()` 输出不可变 `ToolRoute`：

```text
ToolRoute
├── kind: ToolRouteKind
├── recommended_tools: tuple[str, ...]    # 期望调用顺序
├── forbidden_tools: tuple[str, ...]      # 本 turn 禁止
├── suggested_paths: tuple[str, ...]      # 给 LLM 的 http 路径提示（非强制）
└── rationale: str                        # 写入 plan / 调试
```

### 3.1 五类意图与 Demo 映射

| kind | 含义 | recommended_tools（典型） | forbidden | Demo / case |
|------|------|---------------------------|-----------|-------------|
| `knowledge` | 静态文档、API 契约、白名单策略 | `search_docs` | `http_get`, `http_post` | Demo 无直接对应；P4-001–005 |
| `live_status` | 健康、文件、任务 UUID、dashboard | `http_get` 或 `http_post→http_get`（需登录） | — | Demo 1、2；P4-006–008 |
| `troubleshooting` | QUEUED/PROCESSING/FAILED、排查 | `search_docs` → `http_get` | `http_post` | Demo 3；P4-001 |
| `dangerous_execute` | 已确认创建水印任务 | `search_docs` → `http_post` | — | Demo 4–5；P4-009–010 |
| `safety_reject` | 外部 URL、未确认危险 POST | （无） | 全部或 `http_post` | Demo 6；P4-011+ |

### 3.2 分类优先级（规则栈）

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

## 4. ToolRouter 规则设计

实现：`copilot_agent/agent/tool_router.py`。

### 4.1 关键模式（摘录）

| 信号 | 路由倾向 |
|------|----------|
| `https?://evil`、`evil.example` | `safety_reject` |
| `创建水印任务`、`fileId=`、`POST /api/v1/jobs/watermark`（非契约问法） | `dangerous_execute` 或拦截 |
| `需要哪些字段`、`返回什么`、`契约` + `/api/` | `knowledge`（禁止 HTTP） |
| UUID 或 `是否存活`、`/actuator/health` | `live_status` → `/actuator/health` |
| `QUEUED`、`PROCESSING`、`排查`、`卡住` | `troubleshooting` |
| UUID 在排障句中 | `suggested_paths` 前置 `/api/v1/jobs/{uuid}` |

### 4.2 `suggested_paths` 语义

- **不是** HTTP 白名单的超集；实际可调用路径仍由 `tools/whitelist.py` 校验。
- **是** 给 LLM 的软提示，减少路径幻觉；L5/Demo proxy 优先 `suggested_api_paths[0]`，其次 `tool_route.suggested_paths[0]`。
- **结构化来源**：ingest 解析的 `DocChunk.api_endpoint`（见 [rag-design.md](./rag-design.md) §4.4）→ `extract_api_paths()` 优先于 chunk 文本 regex。

### 4.3 `build_route_system_message`

Planner 注入的 SystemMessage 模板（英文，便于模型遵循）：

```text
Tool routing plan for this user turn (follow before choosing tools):
- Intent: troubleshooting
- Recommended tool order: search_docs -> http_get
- Do not call: http_post
- Suggested API paths (http_get whitelist): /api/v1/jobs/{uuid}, ...
- Rationale: Runbook/deploy docs first, then check live task or platform status.
```

与 `agent/prompts.py` 的 `SYSTEM_PROMPT` **叠加**；后者强调 cite 文件名、禁止编造 API。

### 4.4 `tool_allowed(route, tool_name)`

| 条件 | 行为 |
|------|------|
| `tool_name in forbidden_tools` | 拒绝 |
| `kind == safety_reject` | 拒绝一切 tool |
| `recommended_tools` 为空 | 拒绝一切 tool |
| `kind == knowledge` 且 tool 为 http_* | 拒绝 |
| 其他 | 允许（仍须过 Policy + 白名单） |

`safety_gate` 在 `AGENT_TOOL_ROUTE_ENFORCE=true` 时对 **整批** `tool_calls` 检查；若有 blocked 工具，返回说明 AIMessage，不进入 ToolNode。

---

## 5. Planner 与 AgentState

### 5.1 配置项

| 环境变量 / settings | 默认 | 含义 |
|---------------------|------|------|
| `AGENT_TOOL_ROUTE_ENABLED` | `true` | planner 是否分类并注入 SystemMessage |
| `AGENT_TOOL_ROUTE_ENFORCE` | `true` | safety_gate 是否强制 `tool_allowed` |
| `AGENT_RETRIEVAL_PATH_INJECT` | `true` | `search_docs` 返回 `suggested_api_paths` |
| `AGENT_DIAGNOSIS_TEMPLATE_ENABLED` | `true` | troubleshooting 注入排障 outline SystemMessage |
| `COPILOT_ALLOW_JOB_POST` | `false` | 是否允许危险 POST 路径（部署级） |
| 请求级 `confirm_dangerous` | `false` | 用户是否确认创建任务（Demo 5） |

### 5.2 `plan_created` payload

```text
plan_created
├── goal: str                    # 本轮用户问题摘要
├── strategy: "tool_grounded_react" | "react_with_safety_gate"
├── tool_route: ToolRoute.as_dict()
└── available_tools: ToolSpec.public_dict[]
```

`strategy=react_with_safety_gate`：路由关闭时仍写 plan，但不注入 routing SystemMessage。

### 5.3 Checkpoint 边界

- `tool_route` 存入 `AgentState`，随 LangGraph checkpoint 续跑。
- routing SystemMessage 在 planner 每轮追加；续轮时由 `MemoryManager` / compactor 策略决定是否保留（见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)）。
- **不**把 RAG 全文 chunk 写入 checkpoint；仅 tool message 摘要 + `retrieval_completed` 事件。

---

## 6. 与 Guardrail 的双层闸门

Tool-grounded 与 Policy **串联而非替代**：

```text
LLM tool_calls
    │
    v
[层 1] PolicyRegistry
    │-- COPILOT_ALLOW_JOB_POST
    │-- confirm_dangerous
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

## 7. RAG 与 Tool 的组合模式

### 7.1 四种产品模式

| 模式 | kind | 文档角色 | API 角色 |
|------|------|----------|----------|
| **Doc-only** | `knowledge` | 唯一依据 | 禁止 |
| **API-only** | `live_status` | 可选不调用 | 唯一事实源 |
| **Doc then API** | `troubleshooting` | Runbook / 部署解释 | 验证任务/平台状态 |
| **Doc then Approval POST** | `dangerous_execute` | 说明参数与风险 | 审批后 enqueue |

### 7.2 检索驱动工具选择（已实现）

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
| merge 进 `tool_route.suggested_paths`（planner 二次更新） | ❌ 仍靠 LLM 读 ToolMessage |
| eval 断言 path 来自检索 chunk | ⚠️ Demo 3 golden 已验轨迹；未断言 path 来源文件 |

### 7.3 `retrieval_completed.call_id`（已实现）

```text
GraphEventMapper.on_tool_start → set_tool_call_context(call_id)
    ↓
ToolHandlers.search_docs → get_current_call_id() → retrieval_completed.call_id
    ↓
Timeline: retrieval.call_id == search_docs tool_start.call_id
```

实现：`agent/tool_call_context.py`；验收：`verify_runtime_timeline.py`（`retrieval_call_id_linked`）。

---

## 8. 排障输出（已实现）

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

## 9. 评测设计

Tool-grounded 评测分 **路由分类** 与 **工具轨迹** 两层，均 deterministic、无真实 LLM。

### 9.1 路由分类（L5-pre）

```bash
python scripts/verify_tool_router.py
```

- 输入：`eval/phase4-eval-cases.json` 全部 28 case
- 断言：`route_tools(question)` 的 `kind`、`recommended_tools`、`forbidden_tools` 与 case 期望一致

### 9.2 工具轨迹 proxy（L5）

```bash
python scripts/verify_phase4_tool_trajectory.py
python scripts/verify_eval_suite.py --profile rag
```

| 指标 | 含义 |
|------|------|
| `required_tools_ok` | `expected_tools` 均被执行（支持 `http_post:/path`） |
| `forbidden_tools_ok` | 未调用 forbidden |
| `route_order_ok` | 顺序与 `recommended_tools` 一致 |
| `rag_before_api_ok` | troubleshooting：`search_docs` 先于 `http_get` |
| `blocked_ok` | `expect_blocked` 时零 tool |
| `tool_trajectory_pass_rate` | 28 case 通过率 |

**局限**：mock LLM **严格按 route 调用**；不测真实模型是否遵循 SystemMessage。真实 LLM E2E 见 §9.4。

### 9.3 与 RAG L1 的关系

| 层级 | 脚本 | 测什么 |
|------|------|--------|
| L1 检索 | `verify_phase4_ragas.py` | `required_sources` 是否命中 |
| L4-lite | `verify_citation_l4.py` | 回答是否 cite 文件名 |
| L5 轨迹 | `verify_phase4_tool_trajectory.py` | 工具种类、顺序、拦截 |
| Demo golden | `verify_demo_golden_e2e.py` | Demo 1–6 组合断言 |
| L3 | RAGAS | faithfulness — **非 PR 门禁** |

### 9.4 Demo 脚本验收矩阵

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
python scripts/verify_eval_suite.py --profile e2e
```

**仍缺**：`--mode live` 真实 LLM + 可选 mock Watermark API 服务。

---

## 10. 配置与开关

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
python scripts/verify_eval_suite.py --profile e2e
```

---

## 11. 方案选型：规则路由 vs LLM Planner

| 方案 | 优点 | 缺点 | LearnAgent 选择 |
|------|------|------|-----------------|
| **纯 Prompt ReAct** | 实现简单 | 轨迹不稳定、难评测 | 已弃用为主路径 |
| **规则 ToolRouter（当前）** | 可测、可解释、零额外 token | 口语覆盖有限 | ✅ P0–P3 默认 |
| **LLM 意图分类** | 泛化好 | 波动、需 judge | §12 目标 |
| **Plan-and-Execute** | 多步任务清晰 | 复杂度高 | 远期（tech-selection §4） |

当前 **planner 节点名** 保留，但实现是 **deterministic router** 而非 LLM 规划；`plan_created.strategy=tool_grounded_react` 反映「路由 + ReAct」而非完整 Plan-and-Execute。

---

## 12. 待办与路线图

与 [agent-learning-guide.md](./agent-learning-guide.md) **§7 八层栈** 第 2 波（L5–L6）对齐。

### 12.1 八层栈改造分配

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **基线** | L5/L6 | 规则 `tool_router`；path 注入；diagnosis；Demo golden proxy | `--profile e2e` |
| **2** | L5 | planner 二次 merge：`suggested_api_paths` → 硬更新 `tool_route.suggested_paths` | `verify_tool_router.py` + e2e |
| **2** | L5 | `plan_updated` 事件 + 步骤 `outcome`（Plan-and-Execute PoC） | golden 扩展 |
| **2** | L6 | 真实 LLM E2E `--mode live` | `verify_demo_golden_e2e.py --mode live` |
| **2** | L5 | LLM 意图分类 fallback（规则优先，LLM 兜底） | 新 eval case |
| **4** | L5 | 多 Agent / 子目标分解 | tech-selection §4 |

### 12.2 优先级表（实现状态）

与 `demo-requirements-design.md` §6 对齐：

| 优先级 | 项 | 状态 |
|--------|-----|------|
| ~~P0~~ | Demo 1–6 golden proxy | ✅ `verify_demo_golden_e2e.py` |
| **P0** | 真实 LLM E2E（`--mode live`） | ❌ |
| ~~P1~~ | 排障结构化诊断模板 | ✅ `diagnosis.py` |
| ~~P2~~ | `retrieval_completed.call_id` | ✅ |
| ~~P2~~ | ingest API + 检索 path 注入 | ✅ [rag-design.md](./rag-design.md) §4.4 |
| ~~P3~~ | L4-lite citation | ✅ `verify_citation_l4.py` |
| **P2** | planner 二次 merge 检索 path 进 `tool_route` | ❌ |
| **P3** | LLM 意图分类 fallback | ❌ |
| **P4** | `plan_updated`、多步子目标 | ❌ |

---

## 13. 代码索引

| 模块 | 路径 |
|------|------|
| 路由核心 | `copilot_agent/agent/tool_router.py` |
| 排障模板 | `copilot_agent/agent/diagnosis.py` |
| call_id 上下文 | `copilot_agent/agent/tool_call_context.py` |
| API path 提取 | `copilot_agent/rag/api_paths.py` |
| API ingest 解析 | `copilot_agent/rag/api_parse.py` |
| Planner / safety_gate | `copilot_agent/agent/nodes.py` |
| 图拓扑 | `copilot_agent/agent/graph.py` |
| State | `copilot_agent/agent/state.py` |
| System Prompt | `copilot_agent/agent/prompts.py` |
| search_docs handler | `copilot_agent/agent/tool_handlers.py` |
| retrieval payload | `copilot_agent/contracts/events/retrieval.py` |
| L4 citation | `copilot_agent/eval/citation.py` |
| 轨迹评测 | `copilot_agent/eval/tool_trajectory.py` |
| 数据集 | `eval/phase4-eval-cases.json`，`eval/golden/demo-golden-scenarios.json` |
| verify 脚本 | `scripts/verify_tool_router.py`，`scripts/verify_phase4_tool_trajectory.py`，`scripts/verify_demo_golden_e2e.py` |

---

## 14. 面试 / 知识体系对照（可选阅读）

| 概念 | 本项目落地 |
|------|------------|
| Tool use / Function calling | LangChain bound tools + ToolNode |
| Grounding | RAG 摘录 + API JSON 双源 |
| HITL | Policy interrupt + `waiting_approval` |
| Agent 评测 | 轨迹断言优于单一 BLEU |
| RAG vs Agent 边界 | M10 检索 vs M06 编排（本文档） |

更完整的 Agent 模块地图见 [agent-learning-guide.md](./agent-learning-guide.md) §2–§3。
