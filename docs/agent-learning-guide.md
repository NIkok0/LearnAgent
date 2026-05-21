# LearnAgent 项目地图

> 全项目模块划分、职责边界、文档索引与成熟度总表。  
> 操作安装与 API 字段见 [README.md](../README.md)；本页回答「有哪些块、边界在哪、详细设计读哪份文档」。

---

## 1. 文档怎么用


| 文档                                                     | 写什么                       | 不写什么                                                  |
| ------------------------------------------------------ | ------------------------- | ----------------------------------------------------- |
| [README.md](../README.md)                              | 安装、环境变量、REST/SSE API、本地运行 | 模块边界哲学、长篇缺口清单                                         |
| **本页（agent-learning-guide）**                           | 模块地图、依赖关系、成熟度、读哪份 doc     | 逐步实施 checklist、工具脚本实现细节                               |
| `docs/*-design.md`                                     | 各模块/横切稳定设计、遗留问题、未来方向      | 操作命令（见 [ci-design.md](./ci-design.md)）、逐步实施 checklist |
| [tech-selection-design.md](./tech-selection-design.md) | 对外框架对比、引入/替换决策            | 与 README 重复的 API 说明                                   |


---

## 2. 架构总览

### 2.1 三层控制

```text
产品层   ExecutionEngine + EventStore + Timeline     Run FSM、cancel/approve、事件审计
编排层   LangGraph StateGraph + checkpoint             节点路由、interrupt/resume、messages 合并
决策层   ChatOpenAI + ToolNode                         生成与 tool_calls（非确定性）
```

用户一条消息 → 一次 **Run**（产品层）→ `ChatRunner` 驱动图（编排层）→ LLM/工具（决策层）。

### 2.2 数据流（简图）

```text
Client / UI (static/index.html)
    |
    |  REST / SSE / WebSocket
    v
M01  server.py ........................ API 入口、请求校验
    |
    v
M03  execution_engine.py .............. Run 调度、cancel/approve、流队列
    |
    v
M07  runner.py + event_mapper.py ...... 组装图输入、astream、_emit 事件
    |                    \
    |                     +--> M05 contracts/* --> Adapter --> 统一 payload
    v                    /
M06  graph.py + nodes.py .............. LangGraph：planner -> assistant <-> tools
    |
    +--> Checkpoint SQLite (working memory messages)
    |
    +--> M08 LLM (ChatOpenAI) / M11 Tool handlers

M07 / M06 运行时写入 -----> M05 -----> M02 event_store.py (Thread/Run/Event 事实源)
M04 timeline.py 只读 M02 .......... Timeline 投影（CQRS 读模型）

M09  memory/manager.py
    +-- episodic 摘要 --> EventStore (memory_* 事件)
    +-- 压缩策略 ------> Checkpoint (CheckpointCompactor)
    +-- RAG 检索 -------> M10 rag/ (search_docs，不经 checkpoint 全量存储)
```

### 2.3 四条边界规则

1. **产品层（M02–M04、M03）**：Run 状态、Timeline、cancel/approve 以 EventStore 为准；客户端不自行重建 Run 状态机。详见 [runtime-design.md](./runtime-design.md)。
2. **编排层（M06–M07）**：LangGraph checkpoint 的 `messages` 为 **working memory 真相源**；HTTP `messages[]` 仅传当前轮 user。详见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)。
3. **契约层（M05）**：跨模块 payload 以 `RuntimeEvent`、`ToolResultModel` 为准；写入前经 Adapter 展平/脱敏。详见 [data-flow-design.md](./data-flow-design.md)。
4. **审计层（M02）**：不用 EventStore 事件 replay 生成 checkpoint 全量历史；Episodic 摘要仅作 inject，不替代对话正文。

### 2.4 Kernel / Capability / Scenario 三层

LearnAgent 的长期架构目标：**先搭通用 Agent 范式（Kernel），再插能力包（Capability），最后用场景包（Scenario）做业务定制。**  
shell / git / MCP 与 RAG / HTTP 同级，都是 Capability 层的 Tool 扩展，**不**改写 Run FSM 或 EventStore 契约。

#### 2.4.1 三层定义

```text
┌─────────────────────────────────────────────────────────────┐
│  Scenario Pack（场景包）— 换业务主要改这里                      │
│  语料 manifest · tool 白名单 · router 规则 · prompt · eval   │
└───────────────────────────┬─────────────────────────────────┘
                            │ 配置 / 加载
┌───────────────────────────▼─────────────────────────────────┐
│  Capability Pack（能力包）— 按需启用                            │
│  RAG · HTTP · shell · git · MCP · （未来）code index …        │
│  统一走 ToolRegistry → Policy → Handler → ToolResultModel    │
└───────────────────────────┬─────────────────────────────────┘
                            │ 注册 / 调用
┌───────────────────────────▼─────────────────────────────────┐
│  Kernel（内核）— 跨场景稳定                                     │
│  Run FSM · LangGraph · EventStore/Timeline · Contracts       │
│  Policy 框架 · Memory 分层模型 · Eval 门禁                      │
└─────────────────────────────────────────────────────────────┘
```

| 层 | 回答的问题 | 换场景时 | LearnAgent 模块（现状） |
|----|------------|----------|-------------------------|
| **Kernel** | 一次 Run 怎么启停、怎么审计、怎么扩展 | **不换** | M02–M07、M05、M12 框架、M09 分层模型、Eval 横切 |
| **Capability** | 有哪些工具/知识源、怎么执行 | **按需启用** | M10 RAG、M11 Tool handlers、`ToolRegistry` / `ToolSpec` |
| **Scenario** | 这个业务查什么文档、调什么 API、说什么话 | **主要定制** | 今日：`docs/source/`、水印 router/prompt/eval；目标：`scenarios/<name>/` |

**与 §2.1「三层控制」的关系**：§2.1 是**运行时控制面**（产品 / 编排 / 决策）；§2.4 是**产品化分层**（内核 / 能力 / 场景）。二者正交，不冲突。

**与 §7 八层栈的关系**：L1–L4 多为 Capability（RAG 数据链）+ Contracts；L5–L8 多为 Kernel；Scenario 横切 L1–L7 的配置与 prompt。

#### 2.4.2 通用 Run 循环（Kernel 不变）

```text
User Input
  → Run 启动（ExecutionEngine）
  → Context Assembly（working checkpoint + retrieve + memory inject，budget 截断）  ← 目标：统一 Context Manager
  → Plan / Route（tool_router / 未来 plan_updated）
  → LLM → tool_calls
  → Policy Gate（risk · 审批 · hooks）
  → Tool Execution → ToolResultModel
  → Observation → EventStore（tool_* / retrieval_* / memory_*）
  → （可选）Reflector / Replan
  → Output（SSE token + done）
```

Kernel 只保证这条链**可跑、可测、可观测**；具体是 `search_docs` 还是 `run_shell` 由 Capability + Scenario 决定。

#### 2.4.3 Capability 扩展约定

所有外部能力（含 MCP）必须经同一管道，禁止在 Node 里散落裸 HTTP / subprocess：

```text
ToolSpec(name, args_schema, category, risk_level, requires_approval, timeout_seconds)
  → PolicyRegistry（白名单 / 审批 / 未来 hooks）
  → Handler（async coroutine）
  → ToolResultModel → RuntimeEvent(tool_end)
```

| Capability 类型 | `category` 示例 | 典型 risk | 代码锚点（现状 / 目标） |
|-----------------|-----------------|-----------|-------------------------|
| RAG 检索 | `memory` | low | `rag/` + `search_docs` ✅ |
| HTTP API | `http` | medium~high | `tools/http_tools.py` ✅ |
| Shell | `shell` | high | `tools/extensions/shell/` ❌ 待建 |
| Git | `vcs` | medium~high | `tools/extensions/git/` ❌ 待建 |
| MCP | `mcp` | 按 server | `tools/extensions/mcp/` ❌ §7.5 PoC |

注册入口：`ToolRegistry.register_async()`（`tools/registry.py`）。今日水印三件套通过 `ToolRegistry.from_agent_tools()` 硬编码；**目标**改为 Scenario 声明启用哪些 Capability，由 loader 批量注册。

#### 2.4.4 Scenario 包目录约定（目标布局）

> **现状**：水印 Demo 配置分散在 `docs/source/`、`agent/tool_router.py`、`agent/prompts.py`、`eval/`。  
> **目标**：收敛到 `scenarios/<name>/`，Kernel 通过 `SCENARIO` 环境变量（或等价配置）加载。

```text
scenarios/
  watermark/                          # 当前 Demo（迁移目标）
    scenario.yaml                     # 元信息：启用哪些 capability packs
    docs/
      docs_manifest.json              # RAG 语料清单（可 symlink 到 docs/source/）
    policy.yaml                       # tool 白名单、审批规则（或指向 guardrail 配置）
    prompts/
      system.md
      tool_grounded.md
    router/
      rules.yaml                      # 或保留 tool_router 的声明式规则
    memory/
      policy.yaml                     # recall / inject / TTL 覆盖项
    eval/
      golden.json                     # 场景 golden（可引用 eval/golden/）
      rag_cases.json

  minimal/                            # 最小场景（仅验证 Kernel）
    scenario.yaml                     # capabilities: [] 或 echo only
    eval/
      smoke.json

copilot_agent/                        # Kernel + Capability 实现（不随场景复制）
  runtime/                            # Run FSM · EventStore · Timeline
  contracts/                          # RuntimeEvent · ToolResult · validate
  agent/                              # Graph · runner · nodes
  memory/                             # 分层记忆实现
  rag/                                # RAG Capability 实现
  tools/
    registry.py                       # ToolSpec 协议
    http_tools.py                       # HTTP Capability
    extensions/                       # 未来：shell · git · mcp
      mcp/
      shell/
      git/
```

**`scenario.yaml` 最小字段（约定，实现待做）**：

```yaml
name: watermark
description: Watermark platform docs + API agent
capabilities:
  - rag
  - http
# 可选：memory_policy: memory/policy.yaml
# 可选：router: router/rules.yaml
# 可选：prompts_dir: prompts/
docs_dir: docs          # 相对本 scenario 目录；或 WATERMARK_DOCS_PATH 覆盖
eval:
  golden: eval/golden.json
  rag_cases: eval/rag_cases.json
```

**加载规则（约定）**：

1. Kernel 启动时读取 `SCENARIO`（默认 `watermark`），解析 `scenarios/<name>/scenario.yaml`。
2. 按 `capabilities` 注册 Tool；RAG 使用 scenario 内 `docs/` 或 env 覆盖路径。
3. Prompt / router / memory 策略**覆盖** Kernel 默认值，不修改 `runtime/`、`contracts/` 源码。
4. Eval 脚本可通过 `--scenario watermark` 只跑该目录下 case（与 `--profile` 正交）。

#### 2.4.5 场景定制 vs Kernel 改动（决策表）

| 需求 | 应改 Scenario | 应改 Capability | 应改 Kernel |
|------|---------------|-----------------|-------------|
| 换文档语料 | ✅ manifest | — | — |
| 换 API 白名单 | ✅ policy.yaml | ⚠️ http 工具参数 | — |
| 换意图分类规则 | ✅ router | — | — |
| 换 SYSTEM_PROMPT | ✅ prompts | — | — |
| 新增 MCP server | ⚠️ policy 白名单 | ✅ mcp adapter | — |
| 新增 shell 工具 | ⚠️ policy sandbox | ✅ shell handler | ⚠️ Policy hooks |
| Run cancel 语义 | — | — | ✅ runtime |
| 新 Event kind | — | — | ✅ contracts + event_schema |

**原则**：能不进 Kernel 就不进；Capability 只做「一种工具的协议实现」；业务差异进 Scenario。

#### 2.4.6 实现路线图（与 §7 对齐）

| 阶段 | 目标 | 对应 §7 |
|------|------|---------|
| **A** | Scenario loader + `scenarios/watermark/` 迁移 | 与 §7.3 并行（Tool 插件化） |
| **B** | 统一 Context Manager 单入口 | §7.3 L5 |
| **C** | MCP Capability adapter | §7.5 L6 |
| **D** | shell / git Capability + `scenarios/coding/` 示例 | §7.5 后独立立项 |

详细任务仍写入各 `*-design.md` 的「八层栈改造分配」；本节只定**分层哲学与目录约定**。

---

## 3. 模块一览（14 + 横切）

成熟度 **高 / 中 / 低**；缺口细节见同列设计文档的 **未来优化** / **遗留问题**（`data-flow-design` 无遗留节，见该文档 §8）。


| ID  | 模块                 | 代码锚点                                                        | 职责                                   | 不负责              | 成熟度 | 设计文档 · 缺口 §                                                                                                        |
| --- | ------------------ | ----------------------------------------------------------- | ------------------------------------ | ---------------- | --- | ------------------------------------------------------------------------------------------------------------------ |
| M01 | API / Server       | `copilot_agent/server.py`                                   | HTTP/SSE/WS、请求校验、挂载 Engine/Runner    | Run FSM、图节点      | 高   | [README](../README.md) §5–6；长 Run/WS → [runtime-design](./runtime-design.md) §8·§9                                 |
| M02 | Runtime Contract   | `runtime/event_store.py`, `run_state.py`, `event_schema.py` | Thread/Run/Event 事实源、FSM、事件类型        | LLM、工具实现         | 高   | [runtime-design](./runtime-design.md) §8·§9；[data-flow-design](./data-flow-design.md) §8                           |
| M03 | Execution Engine   | `runtime/execution_engine.py`                               | Run 调度、cancel/approve、超时、流队列、终态触发压缩  | 事件 schema、图逻辑    | 中   | [runtime-design](./runtime-design.md) §8·§9                                                                        |
| M04 | Timeline 读模型       | `runtime/timeline.py`                                       | events → UI/API timeline 投影          | 写入 EventStore    | 高   | [runtime-design](./runtime-design.md) §6.3、§8·§9                                                                   |
| M05 | Contracts          | `copilot_agent/contracts/`                                  | Envelope、ToolResult、Adapter、validate | 业务编排             | 中   | [data-flow-design](./data-flow-design.md) §8                                                                       |
| M06 | Agent Graph        | `agent/graph.py`, `nodes.py`, `state.py`                    | LangGraph 图、路由、`safety_gate`         | REST、Run API     | 中   | [memory-checkpoint-design](./memory-checkpoint-design.md) §8·§9；编排 [tech-selection](./tech-selection-design.md) §4 |
| M07 | ChatRunner / 流映射   | `agent/runner.py`, `stream/event_mapper.py`                 | 图输入、astream、emit RuntimeEvent        | EventStore SQL   | 中   | 同 M06；事件形状 [data-flow-design](./data-flow-design.md)                                                               |
| M08 | LLM                | `llm/provider.py`                                           | ChatOpenAI 配置与薄封装                    | Tool、Memory 策略   | 中   | [tech-selection-design](./tech-selection-design.md) §4                                                             |
| M09 | Memory             | `memory/manager.py`, `policy.py`, `checkpoint_compactor.py` | Episodic 摘要/召回、checkpoint 压缩         | Run 生命周期、RAG 建索引 | 中   | [memory-checkpoint-design](./memory-checkpoint-design.md) §8.5·§9                                                    |
| M10 | RAG                | `rag/`、`docs/source/`                                      | ingest、结构化 API 元数据、检索、Tool-grounded 注入 | Run/Event、审批     | **中高** | [rag-design](./rag-design.md) §11.0·[tool-grounded-design](./tool-grounded-design.md) §12.1 |
| M11 | Tool               | `tools/`, `agent/tool_handlers.py`                          | 注册、白名单 HTTP、handlers                 | 危险策略判定（归 M12）    | 中   | [data-flow-design](./data-flow-design.md) §8；[tech-selection](./tech-selection-design.md) §4                       |
| M12 | Guardrail / Policy | `policy/registry.py`, `nodes` safety_gate                   | 风险分级、审批、危险 POST 拦截                   | 工具 HTTP 实现       | 低   | [guardrail-policy-design](./guardrail-policy-design.md) §10·§11                                                    |
| M13 | Observability      | `observability/langfuse_tracer.py`                          | Langfuse trace/span、日志               | EventStore 写入    | 低   | [observability-design](./observability-design.md) §9·§10                                                           |
| M14 | Session 凭据         | `conversation_store.py`                                     | 按 thread 缓存 WMSESSIONID（内存 TTL）      | 多租户认证            | 中   | **本页 M14 说明**（无 design doc）                                                                                        |


**横切**


| 名称        | 锚点                             | 边界               | 成熟度 | 设计文档 · 缺口 §                                                             |
| --------- | ------------------------------ | ---------------- | --- | ----------------------------------------------------------------------- |
| Eval / 回归 | `scripts/verify_*.py`, `eval/` | 验证行为与契约，不实现产品逻辑  | 中   | [eval-design](./eval-design.md) §7·§8；[ci-design](./ci-design.md) §2–§5 |
| UI 控制台    | `static/index.html`            | 本地 Timeline/审批调试 | —   | —                                                                       |
| 配置        | `settings.py`                  | 环境变量聚合，不含业务规则    | —   | —                                                                       |


**非 MVP（刻意后置）**：Planning（[tech-selection](./tech-selection-design.md) §4）、Multi-Agent、外部队列（Temporal/Celery）、Mem0/Zep、多租户。

**M14 说明**（无独立 `*-design.md`）：`ConversationCookieStore` 在进程内按 `conversation_id`（即 `thread_id`）保存水印平台登录 Cookie，供 `http_get`/`http_post` 带会话访问；TTL 由配置控制，**不**落盘、**不**替代平台账号体系。排障时与 M11 白名单、M12 审批分开查。

---

## 4. 模块依赖（允许方向）

```text
M01 → M03 → M07 → M06 → (LLM M08, Tool M11 via graph)
M07 → M05 → M02
M07 → M09 → M02, checkpoint
M11 → M12（策略查询）
M07 → M13（trace）
M11,M07 → M14（登录态 cookie）
M04 只读 M02
```

**禁止**：M02 调用 M07；M10 写入 Run FSM；M12 在 `tools/http_tools` 外重复实现 HTTP。

---

## 5. 横切：Eval

- **数据集**：`eval/phase4-eval-cases.json`（**20** 条 docs + api/safety 期望）、`eval/golden/runtime-golden-scenarios.json`（Run 事件契约）
- **聚合入口**：`scripts/verify_eval_suite.py --profile {core|rag|full}`
- **设计**：[eval-design.md](./eval-design.md)
- **CI**：[ci-design.md](./ci-design.md)

成熟度 **中**：core 含 contract + runtime + golden + memory-checkpoint；仍缺 Promptfoo 驱动真实 Agent E2E、LLM judge 夜跑。

---

## 6. 文档索引


| 你想…                         | 文档                                                           | 主题（写什么）                                        |
| --------------------------- | ------------------------------------------------------------ | ---------------------------------------------- |
| 跑起来、调 API                   | [README.md](../README.md)                                    | 安装、环境变量、REST/SSE API、本地运行                      |
| 模块边界、成熟度、**八层栈改造顺序** | **本页** §3–§4、**§7** | 14 模块地图、依赖、按层波次索引 |
| **Kernel / Capability / Scenario 分层** | **本页** **§2.4** | 通用范式、目录约定、与八层栈关系 |
| M14 登录 Cookie（无 design doc） | **本页** §3 **M14 说明**                                         | 进程内 WMSESSIONID、TTL、与 M11/M12 分工               |
| Run/Thread、cancel/approve   | [runtime-design.md](./runtime-design.md)                     | FSM、ExecutionEngine、Timeline、审批续跑              |
| 事件/工具 payload 契约            | [data-flow-design.md](./data-flow-design.md)                 | RuntimeEvent、ToolResult、Adapter、SSE/EventStore |
| Memory 与 checkpoint         | [memory-checkpoint-design.md](./memory-checkpoint-design.md) | Working memory 真相源、压缩、episodic                 |
| RAG、search_docs、Tool-grounded、检索评测 | [rag-design.md](./rag-design.md) ·[tool-grounded-design.md](./tool-grounded-design.md) | §0 状态表、ingest、分层评测、verify 命令 |
| Guardrail、审批、HTTP 白名单       | [guardrail-policy-design.md](./guardrail-policy-design.md)   | Policy、safety_gate、白名单、与 Run 协作                |
| 排障、Langfuse、ID 关联           | [observability-design.md](./observability-design.md)         | EventStore 产品轨 + Langfuse、trace 关联             |
| Eval 分层与 golden             | [eval-design.md](./eval-design.md)                           | profile、golden、聚合 summary                      |
| CI 失败怎么查                    | [ci-design.md](./ci-design.md)                               | `agent-ci` / `eval-ci`、本地复现                    |
| 框架选型、主线与优化方向                | [tech-selection-design.md](./tech-selection-design.md) §3–§4 | 对外框架对比、当前选择与优化方向                               |
| Demo 验收与产品场景                | [demo-requirements-design.md](./demo-requirements-design.md) | 水印任务 Agent + 文档问答验收                            |


**待补（可选）**：`api-design.md`（REST 字段若需从 README 抽离时再写）。

---

## 7. 建议改造顺序（八层栈）

总路线图按 **Ingestion → Preprocess → Schema Extraction → Pydantic Validation → Agent State → Tool Execution → Output → Storage/Audit** 八层组织；**产品化分层**见 **§2.4 Kernel / Capability / Scenario**。  
**本页只写波次、依赖与文档索引**；具体任务、验收命令、代码锚点见各 `*-design.md` 的 **「八层栈改造分配」** 小节。

### 7.0 八层栈 ↔ LearnAgent 映射

```text
数据源          LearnAgent 现状                          主文档
────────────────────────────────────────────────────────────────────────
L1 Ingestion    md manifest + upload；`IngestSource`；env 语料路径   rag-design §11.0
                ❌ 网页/DB 同步（§7.5）
L2 Preprocess   md 分块；BM25/向量；memory embedding    rag-design §5；memory §4.5
                ❌ OCR/PDF/HTML 通用流水线
L3 Schema       api_parse；memory llm_extractor          rag-design §4.4；memory §4.5
                ❌ 通用 Extract→Record 中间层
L4 Pydantic     RuntimeEvent/ToolResult；FastAPI 入参    data-flow-design §2
                ⚠️ memory/RAG 部分 loose payload
L5 Agent State  checkpoint；tool_route；episodic/LTM     memory-checkpoint；tool-grounded
                ❌ plan_updated / 显式 Planning
L6 Tool Exec    search_docs + 白名单 HTTP + 审批          tool-grounded；guardrail-policy
                ❌ MCP/DB/代码/邮件
L7 Output       SSE token + ToolMessage 证据              data-flow-design §2
                ❌ 最终回答固定 JSON schema
L8 Storage      EventStore + Timeline + eval              runtime；observability；eval
                ⚠️ trace_id/cost 未闭环；无 GDPR 删除
```

### 7.1 已落地基线（2026-05）

| 层 | 已交付 | 验收 |
|----|--------|------|
| L1–L2 | 9 篇 `docs/source/` ingest、热更新、BM25+RRF+可选向量 | `verify_phase4_ragas.py`，[rag-design §0](./rag-design.md) |
| L3 | API 契约 `api_parse`；Memory 规则/LLM 抽取 + pending | `verify_rag_api_ingest.py`，`verify_memory_production_v2.py` |
| L4 | `RuntimeEvent`/`ToolResultModel` + Adapter 链 | `verify_contract_events.py`，[data-flow §2](./data-flow-design.md) |
| L5 | checkpoint 真相源；tool_router；episodic + `memory_items` | `verify_memory_checkpoint_consistency.py` |
| L6 | Tool-grounded 路由；path 注入；诊断模板；审批 | `verify_tool_router.py`，`--profile e2e` |
| L7 | 流式 NL + `retrieval_completed` 溯源；L4-lite | `verify_citation_l4.py` |
| L8 | EventStore/Timeline；`checkpoint_compacted`；core/rag/e2e eval | `verify_eval_suite.py` |

详情：[rag-design §0](./rag-design.md)、[tool-grounded §0](./tool-grounded-design.md)、[memory-checkpoint §0](./memory-checkpoint-design.md)、[demo-requirements §0](./demo-requirements-design.md)。

---

### 7.2 第 1 波 — 数据前段（L1–L4）✅ 已完成（2026-05-21）

**目标**：把「文档 RAG 垂直切片」扩展为可复用的 **接入 → 预处理 → 抽取 → 校验** 流水线，仍不阻塞 Agent 主链路。

| 层 | 改造项 | 状态 | 设计文档 |
|----|--------|------|----------|
| L1 | `IngestSource` 抽象（`FileIngestSource`；Url/Api 占位） | ✅ | [rag-design §11.0](./rag-design.md) |
| L1 | `WATERMARK_DOCS_PATH` 接生产 `backend-java/docs` | ✅ 已支持 | [rag-design §11.0](./rag-design.md) |
| L1 | `POST /v1/rag/upload` + reload | ✅ | [rag-design §11.0](./rag-design.md) |
| L2 | `response_fields` JSON 块解析 | ✅ | [rag-design §11.0](./rag-design.md) |
| L2 | 动态 top-k / `RAG_CONTEXT_BUDGET_CHARS` | ✅ | [rag-design §11.0](./rag-design.md) |
| L2 | `docs_manifest.json` + glob 扩面 | ✅ | [rag-design §11.0](./rag-design.md) |
| L3 | 统一 `ExtractedRecord`（`contracts/extract.py`） | ✅ | [data-flow §8.1](./data-flow-design.md) |
| L4 | `memory_*` Pydantic 子模型 | ✅ | [data-flow §8.1](./data-flow-design.md) |
| L4 | `retrieval_completed` 严格校验 | ✅ 基线已有 | [data-flow §8.1](./data-flow-design.md) |
| L4 | `GET /events?validated=1` | ✅ | [data-flow §8.1](./data-flow-design.md) |

**验收**：`verify_extract_validate.py`、`verify_events_validated.py`、`verify_rag_api_ingest.py`（含 response_fields）、`verify_rag_retrieval_quality.py`（budget packing）；`--profile rag` 见 [eval-design §7.5](./eval-design.md)。

---

### 7.3 第 2 波 — 决策与执行（L5–L6）

**目标**：从「规则 Tool-grounded」进化为 **可观测规划 + 可扩展工具**，Demo 可 `--mode live`。

| 层 | 改造项 | 设计文档 |
|----|--------|----------|
| L5 | 检索 path merge 进 `tool_route`；`plan_updated` / 步骤 outcome PoC | [tool-grounded §12.1](./tool-grounded-design.md)、[tech-selection §4](./tech-selection-design.md) |
| L5 | Memory 续轮 inject 去重收尾；episodic 向量索引（可选） | [memory §8.5](./memory-checkpoint-design.md) |
| L6 | `timeout_seconds` 执行层强制；策略表 YAML 版本化 | [guardrail §10.5](./guardrail-policy-design.md) |
| L6 | 真实 LLM E2E（`verify_demo_golden_e2e.py --mode live`） | [tool-grounded §12.1](./tool-grounded-design.md)、[eval §7.5](./eval-design.md) |

**横切验收**：`--profile e2e` proxy 保持 PASS；live 模式有 key 时夜跑。见 [demo-requirements §6](./demo-requirements-design.md)。

---

### 7.4 第 3 波 — 输出与审计闭环（L7–L8）

**目标**：结构化交付 + 生产级追踪/回放。

| 层 | 改造项 | 设计文档 |
|----|--------|----------|
| L7 | 可选 `FinalAnswerModel`（NL + citations + structured fields） | [data-flow §8.1](./data-flow-design.md) |
| L7 | 输出 Guard（secret/PII 模式检测） | [guardrail §10.5](./guardrail-policy-design.md) |
| L8 | `trace_id` 写入 EventStore；generation span；token/cost 进 `run_completed_meta` | [observability §9.6](./observability-design.md) |
| L8 | 失败 run 一键导出 timeline；RAGAS 夜跑趋势 | [eval §7.5](./eval-design.md)、[observability §9.6](./observability-design.md) |
| L8 | `running`/`queued` durable resume 或外部队列 PoC | [runtime §8.1](./runtime-design.md) |

---

### 7.5 第 4 波 — 平台扩展（非 Demo 阻塞）

| 层 | 改造项 | 设计文档 |
|----|--------|----------|
| L1 | 网页/DB 同步 ingest | [rag-design §11.0](./rag-design.md) |
| L3–L4 | 多租户 `tenant_id` + 服务端 `user_id` 鉴权 | [memory §8.5](./memory-checkpoint-design.md)、[guardrail §10.5](./guardrail-policy-design.md) |
| L6 | MCP registry PoC；LiteLLM fallback | [guardrail §10.5](./guardrail-policy-design.md)、[tech-selection §4](./tech-selection-design.md) |
| L8 | Memory GDPR 删除 API；OpenTelemetry 双写 | [memory §8.5](./memory-checkpoint-design.md)、[observability §9.6](./observability-design.md) |
| — | Multi-Agent、外部队列 Temporal | [tech-selection §4](./tech-selection-design.md) |

---

### 7.6 改造原则

1. **按层验收**：每波只动 1–2 个层，避免同时改 ingest + 编排 + 观测导致回归难定位。  
2. **文档下沉**：本页 §7 只保留波次索引；新增任务必须写入对应 design doc 的「八层栈改造分配」。  
3. **Eval 门禁**：每层至少一条 `verify_*` 或 eval case 扩展，见 [eval-design](./eval-design.md)。  
4. **Demo 优先**：第 1–2 波服务「生产文档 + live E2E」；第 4 波按需立项。

