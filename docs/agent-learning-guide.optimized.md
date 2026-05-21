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

### 2.0 架构视角约定

本文以 **Kernel / Capability / Scenario（K/C/S）** 作为长期架构主视角：

```text
K/C/S：回答「系统长期怎么分层、哪些东西可替换、哪些东西不可变」
三层控制：回答「一次 Run 在运行时由谁负责」
八层栈：回答「改造和验收按什么顺序推进」
```

三者不是并列竞争关系，而是不同剖面：

| 视角 | 用途 | 是否作为主架构边界 |
|------|------|--------------------|
| **Kernel / Capability / Scenario** | 长期产品化分层、插件化、业务迁移 | **是** |
| 产品层 / 编排层 / 决策层 | 单次 Run 的运行时控制面 | 否，作为运行时解释 |
| L1–L8 八层栈 | 阶段性改造路线与验收波次 | 否，作为实施计划 |

原则：**K/C/S 定边界，三层控制解释运行时，八层栈安排施工顺序。**

### 2.1 三层控制

```text
产品层   ExecutionEngine + EventStore + Timeline       Run FSM、cancel/approve、事件审计
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

M15 Context Manager（目标）
    +-- checkpoint messages ----> working memory
    +-- RAG snippets -----------> M10 rag/（search_docs，不经 checkpoint 全量存储）
    +-- episodic inject --------> M09 memory/manager.py
    +-- tool schemas/policy ----> ToolRegistry + M12 PolicyGate
    +-- token budget -----------> context packing / compaction
```

### 2.3 六条边界规则

1. **产品层（M02–M04、M03）**：Run 状态、Timeline、cancel/approve 以 EventStore 为准；客户端不自行重建 Run 状态机。详见 [runtime-design.md](./runtime-design.md)。
2. **编排层（M06–M07）**：LangGraph checkpoint 的 `messages` 为 **working memory 真相源**；HTTP `messages[]` 仅传当前轮 user。详见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)。
3. **契约层（M05）**：跨模块 payload 以 `RuntimeEvent`、`ToolResultModel`、`ContextBundle`、`PolicyDecision` 为准；写入前经 Adapter 展平/脱敏。详见 [data-flow-design.md](./data-flow-design.md)。
4. **审计层（M02）**：不用 EventStore 事件 replay 生成 checkpoint 全量历史；Episodic 摘要仅作 inject，不替代对话正文。
5. **策略层（M12）**：最终权限裁决权归 Kernel PolicyGate；Capability 只能声明风险，Scenario 只能收紧策略，不能放宽 Kernel 默认策略。
6. **上下文层（M15）**：所有 LLM 输入统一经 Context Manager 装配；Runner、Node、Memory、RAG 不各自拼接上下文。

### 2.4 Kernel / Capability / Scenario 三层

LearnAgent 的长期架构目标：**先稳定通用 Agent Runtime（Kernel），再插能力包（Capability），最后用场景包（Scenario）做业务定制。**

shell / git / MCP 与 RAG / HTTP 同级，都是 Capability 层 Tool 扩展，**不**改写 Run FSM、EventStore、Contracts 或 PolicyGate。

#### 2.4.1 三层定义

```text
┌─────────────────────────────────────────────────────────────┐
│  Scenario Pack（场景包）— 声明式业务配置，不执行代码             │
│  manifest · resource binding · tool allowlist · router       │
│  prompts · business eval · budget · policy override          │
│  只能收紧 Kernel 默认策略，不能放宽安全边界                    │
└───────────────────────────┬─────────────────────────────────┘
                            │ 声明 / 覆盖
┌───────────────────────────▼─────────────────────────────────┐
│  Capability Pack（能力包）— 能力声明 + Handler 实现             │
│  RAG · HTTP · shell · git · MCP · code index …               │
│  ToolSpec · args_schema · result_schema · risk metadata      │
│  不拥有最终授权权，不直接写 Run FSM / EventStore                │
└───────────────────────────┬─────────────────────────────────┘
                            │ 注册 / 被调度
┌───────────────────────────▼─────────────────────────────────┐
│  Kernel（内核）— 跨场景稳定 Agent Runtime                      │
│  Run FSM · LangGraph · EventStore/Timeline · Contracts       │
│  Context Manager · PolicyGate · Memory · Eval · Audit        │
│  Sandbox · timeout · approval · checkpoint · rollback        │
└─────────────────────────────────────────────────────────────┘
```

| 层 | 回答的问题 | 换场景时 | LearnAgent 模块（现状 / 目标） |
|----|------------|----------|-------------------------------|
| **Kernel** | 一次 Run 怎么启停、怎么审计、怎么授权、怎么装配上下文 | **不换** | M02–M07、M05、M09、M12、M13、M15、Eval 横切 |
| **Capability** | 有哪些工具/知识源、参数和结果怎么校验、如何执行 | **按需启用** | M10 RAG、M11 Tool handlers、`ToolRegistry` / `ToolSpec` |
| **Scenario** | 这个业务查什么文档、允许什么工具、说什么话、怎么评测 | **主要定制** | 今日：`docs/source/`、水印 router/prompt/eval；目标：`scenarios/<name>/` |

**与 §2.1「三层控制」的关系**：§2.1 是**运行时控制面**（产品 / 编排 / 决策）；§2.4 是**主架构分层**（内核 / 能力 / 场景）。二者正交，不冲突。

**与 §7 八层栈的关系**：L1–L4 多为 Capability（RAG 数据链）+ Contracts；L5–L8 多为 Kernel；Scenario 横切 L1–L7 的配置与 prompt。

#### 2.4.2 通用 Run 循环（Kernel 不变）

```text
User Input
  → Run 启动（ExecutionEngine）
  → Context Manager 构造 ContextBundle
       working checkpoint + retrieval + episodic memory + tool schemas + policy hints + budget
  → Plan / Route（tool_router / 未来 plan_updated）
  → LLM → tool_calls
  → Kernel PolicyGate（risk · approval · scenario allowlist · hooks · timeout · sandbox）
  → Tool Execution（Capability Handler）
  → ToolResultModel / RuntimeEvent(tool_end)
  → Observation → EventStore（tool_* / retrieval_* / memory_*）
  → （可选）Reflector / Replan
  → Output（SSE token + done）
```

Kernel 只保证这条链**可跑、可测、可观测、可审计、可恢复**；具体是 `search_docs`、`http_get` 还是 `run_shell`，由 Capability + Scenario 决定。

#### 2.4.3 Capability 扩展约定

所有外部能力（含 MCP）必须经同一管道，禁止在 Node 里散落裸 HTTP / subprocess：

```text
ToolSpec(name, args_schema, result_schema, category, risk_level, required_scopes,
         requires_approval, timeout_seconds, side_effect_level, supports_rollback)
  → ToolRegistry.register_async()
  → Kernel PolicyGate（最终 allow / ask / deny）
  → Approval / Sandbox / Timeout / Hooks
  → Handler（async coroutine）
  → ToolResultModel
  → RuntimeEvent(tool_end)
```

| Capability 类型 | `category` 示例 | 典型 risk | 代码锚点（现状 / 目标） |
|-----------------|-----------------|-----------|-------------------------|
| RAG 检索 | `memory` | low | `rag/` + `search_docs` ✅ |
| HTTP API | `http` | medium~high | `tools/http_tools.py` ✅ |
| Shell | `shell` | high | `tools/extensions/shell/` ❌ 待建 |
| Git | `vcs` | medium~high | `tools/extensions/git/` ❌ 待建 |
| MCP | `mcp` | 按 server | `tools/extensions/mcp/` ❌ §7.5 PoC |

Capability 只负责：

```text
声明 ToolSpec；
实现 Handler；
返回 ToolResultModel；
声明风险、权限、超时、是否支持 rollback。
```

Capability 不负责：

```text
最终授权；
直接读写 EventStore；
修改 Run FSM；
绕过 Scenario allowlist；
绕过 Kernel safety/eval gate。
```

#### 2.4.4 Scenario 包目录约定（目标布局）

> **现状**：水印 Demo 配置分散在 `docs/source/`、`agent/tool_router.py`、`agent/prompts.py`、`eval/`。  
> **目标**：收敛到 `scenarios/<name>/`，Kernel 通过 `SCENARIO` 环境变量（或等价配置）加载。

Scenario Pack 必须是**声明式配置包**，不允许包含任意 Python 执行逻辑；所有字段通过 `ScenarioConfig` Pydantic schema 校验。

```text
scenarios/
  watermark/                          # 当前 Demo（迁移目标）
    scenario.yaml                     # 元信息：启用哪些 capability packs
    docs/
      docs_manifest.json              # RAG 语料清单（可 symlink 到 docs/source/）
    resources.yaml                    # 数据源、API base、credential binding 名称
    policy.yaml                       # tool allowlist、审批规则、预算；只能收紧 Kernel 默认策略
    prompts/
      system.md
      tool_grounded.md
    router/
      rules.yaml                      # 声明式路由规则；不含任意代码
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
  contracts/                          # RuntimeEvent · ToolResult · ContextBundle · validate
  agent/                              # Graph · runner · nodes
  context/                            # M15 Context Manager（目标目录）
  memory/                             # 分层记忆实现
  rag/                                # RAG Capability 实现
  tools/
    registry.py                       # ToolSpec 协议
    http_tools.py                     # HTTP Capability
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
resources: resources.yaml
policy: policy.yaml
prompts_dir: prompts
router: router/rules.yaml
memory_policy: memory/policy.yaml
docs_dir: docs          # 相对本 scenario 目录；或 WATERMARK_DOCS_PATH 覆盖
eval:
  golden: eval/golden.json
  rag_cases: eval/rag_cases.json
budgets:
  max_context_tokens: 24000
  max_tool_calls: 8
  max_run_seconds: 120
```

**Scenario 可以做**：

```text
选择 capability；
绑定资源与语料；
声明 tool allowlist / denylist；
覆盖 prompt、router、memory policy；
设置业务 eval 和预算；
收紧 Kernel 默认 policy。
```

**Scenario 不可以做**：

```text
修改 Tool handler；
绕过 Kernel PolicyGate；
禁用 Kernel Eval / safety gate；
写入 EventStore schema；
改 Run FSM；
直接执行 subprocess / HTTP；
放宽 Kernel 默认 policy。
```

**加载规则（约定）**：

1. Kernel 启动时读取 `SCENARIO`（默认 `watermark`），解析并校验 `scenarios/<name>/scenario.yaml`。
2. Loader 读取 `capabilities`，批量注册 ToolSpec；RAG 使用 scenario 内 `docs/` 或 env 覆盖路径。
3. Prompt / router / memory / eval / policy 仅覆盖 Kernel 默认配置，不修改 `runtime/`、`contracts/` 源码。
4. Scenario policy 与 Kernel policy 取交集：Scenario 只能收紧，不能放宽。
5. Eval 脚本可通过 `--scenario watermark` 只跑该目录下 case（与 `--profile` 正交）。

#### 2.4.5 场景定制 vs Capability vs Kernel 改动（决策表）

| 需求 | 应改 Scenario | 应改 Capability | 应改 Kernel |
|------|---------------|-----------------|-------------|
| 换文档语料 | ✅ manifest/resources | — | — |
| 换 API 白名单 | ✅ policy.yaml | ⚠️ http 工具参数 schema | — |
| 换意图分类规则 | ✅ router/rules.yaml | — | — |
| 换 SYSTEM_PROMPT | ✅ prompts | — | — |
| 调整上下文预算 | ✅ budgets | — | ⚠️ Context Manager 策略扩展 |
| 新增 MCP server | ⚠️ policy/resources 白名单 | ✅ mcp adapter | — |
| 新增 shell 工具 | ⚠️ policy sandbox 配置 | ✅ shell handler | ⚠️ PolicyGate / sandbox hooks |
| Run cancel 语义 | — | — | ✅ runtime |
| 新 Event kind | — | — | ✅ contracts + event_schema |
| EventStore/checkpoint 失败一致性 | — | — | ✅ runtime + contracts |
| 多租户 credential 体系 | ⚠️ resource binding | ⚠️ capability required_scopes | ✅ credential/session manager |

**原则**：Scenario 声明业务意图；Capability 实现一种能力；Kernel 负责调度、授权、上下文、审计、恢复与状态一致性。

#### 2.4.6 实现路线图（与 §7 对齐）

| 阶段 | 目标 | 对应 §7 |
|------|------|---------|
| **A** | Scenario loader + `scenarios/watermark/` 迁移 | 与 §7.3 并行（Tool 插件化） |
| **B** | 统一 Context Manager 单入口 | §7.3 L5 |
| **C** | PolicyGate kernel 化：allow/ask/deny、timeout、approval、hooks | §7.3 L6 |
| **D** | EventStore/checkpoint 失败一致性与 idempotency | §7.4 L8 |
| **E** | MCP Capability adapter | §7.5 L6 |
| **F** | shell / git Capability + `scenarios/coding/` 示例 | §7.5 后独立立项 |
| **G** | M14 Credential/Session Manager 重构 | §7.5 平台扩展 |

详细任务仍写入各 `*-design.md` 的「八层栈改造分配」；本节只定**主架构边界、目录约定与跨层决策规则**。

### 2.5 Context Manager（M15，目标模块）

Context Manager 是 Kernel 的显式模块，负责每轮 LLM 输入的统一装配。它不是 Memory，也不是 RAG，也不是 Prompt 模板；它是把这些来源按预算和优先级组合成 `ContextBundle` 的唯一入口。

#### 2.5.1 职责

```text
输入：
- 当前 user message
- LangGraph checkpoint messages（working memory）
- RAG retrieval snippets
- episodic / long-term memory inject
- Scenario prompt / router / business hints
- Tool schemas / allowed tools
- Policy hints / approval state
- token budget / run budget

输出：
- ContextBundle
- context_built RuntimeEvent
- 被截断 / 被压缩 / 被注入内容的审计摘要
```

#### 2.5.2 不负责

```text
不负责 RAG 建索引；
不负责长期记忆存储；
不负责 Tool handler 执行；
不负责 Run FSM；
不负责最终权限裁决。
```

#### 2.5.3 建议契约

```python
class ContextBundle(BaseModel):
    thread_id: str
    run_id: str
    user_message: str
    checkpoint_messages: list[dict]
    retrieved_context: list[dict]
    memory_injections: list[dict]
    scenario_prompts: list[str]
    enabled_tool_schemas: list[dict]
    policy_hints: list[dict]
    budget: dict
    truncation_report: dict
```

#### 2.5.4 装配优先级

```text
1. 当前 user message 和系统安全约束永远保留；
2. 当前 Run 必需的 tool schema 和 policy hints 保留；
3. 与当前任务强相关的 retrieval snippets 保留；
4. 最近 checkpoint messages 保留；
5. episodic memory 只按需 inject；
6. 超预算时先压缩历史，再减少弱相关 retrieval，最后减少 memory inject。
```

### 2.6 EventStore 与 Checkpoint 的失败一致性策略

EventStore 是产品事实源；LangGraph checkpoint 是 working memory 真相源。二者职责不同，但同一次 Run 中会被连续更新，因此必须定义失败语义。

#### 2.6.1 基本原则

```text
EventStore 记录「产品事实」：run_started、tool_start、tool_end、approval、run_completed、run_failed。
Checkpoint 记录「模型继续推理所需的 working messages」。
Timeline 只读 EventStore，不从 checkpoint 反推产品状态。
```

#### 2.6.2 写入顺序建议

```text
Run started：先写 EventStore，再启动 LangGraph。
Tool start：先写 EventStore，再执行 Handler。
Tool end：Handler 返回 ToolResultModel 后，先校验，再写 EventStore，再更新 checkpoint。
Assistant message：先形成可校验 message，再更新 checkpoint，再写 assistant_message 事件。
Run completed：确认 checkpoint 与关键事件写入成功后，再写 run_completed。
Run failed：任何阶段失败都写 run_failed，并携带 last_successful_event_id。
```

#### 2.6.3 必要字段

```text
event_id：全局事件 ID
run_id / thread_id：Run 与会话关联
sequence：Run 内单调递增序号
tool_call_id：工具调用幂等键
checkpoint_id：关联 checkpoint 版本
parent_event_id：可选，用于建立因果链
idempotency_key：重试去重
status：pending / committed / failed / compensated
```

#### 2.6.4 典型失败处理

| 场景 | 处理策略 |
|------|----------|
| EventStore 写失败，checkpoint 未写 | 停止本步，返回 `run_failed` 或重试；不得继续执行工具 |
| EventStore 写成功，checkpoint 写失败 | 标记 `checkpoint_sync_failed`，Run 进入 `recoverable_failed`，允许从 EventStore + last checkpoint 恢复 |
| Tool 执行成功，tool_end 写失败 | 使用 `tool_call_id` 幂等重试写入；禁止重复执行有副作用 Tool |
| checkpoint 写成功，assistant 事件写失败 | 重试事件写入；若失败，标记 timeline 不完整，不影响 checkpoint 恢复 |
| SSE 已推送，落库失败 | 前端展示为 transient；服务端以 EventStore 为准，下次刷新以后端事实为准 |
| cancel 与 tool execution 并发 | cancel 写 EventStore；若 Tool 不可中断，等待 ToolResult 后进入 cancelling → cancelled / failed |

#### 2.6.5 最小 MVP 要求

```text
1. 每个 tool_call_id 幂等；
2. 每个 RuntimeEvent 有 run 内 sequence；
3. run_failed 携带 last_successful_event_id；
4. EventStore 写失败时不能静默继续；
5. checkpoint compact / sync 失败必须可观测。
```

### 2.7 M14 未来演进：Credential / Session 体系

当前 M14 是 `ConversationCookieStore`：在进程内按 `conversation_id` / `thread_id` 缓存 WMSESSIONID，供水印平台 HTTP 工具带会话访问。MVP 阶段可接受，但它不是长期 credential 架构。

未来应演进为 Kernel 下的 **Credential / Session Manager**，并与 Scenario resource binding、Capability required scopes、PolicyGate 审批联动。

#### 2.7.1 目标对象

```python
class CredentialBinding(BaseModel):
    binding_id: str
    tenant_id: str | None = None
    user_id: str
    thread_id: str | None = None
    provider: str
    credential_type: Literal["cookie", "api_key", "oauth_token", "session"]
    scopes: list[str]
    expires_at: str | None = None
    storage: Literal["memory", "encrypted_store", "external_secret_manager"]
    audit_required: bool = True
```

#### 2.7.2 演进原则

```text
短期：继续保留进程内 Cookie Store，但显式标记为 single-process MVP。
中期：引入 CredentialBinding，Scenario 只引用 binding 名称，不直接写 secret。
长期：支持多租户、撤销、过期、审计、加密存储和外部 secret manager。
```

#### 2.7.3 边界

```text
M14 负责 credential/session 绑定与读取；
M11 Capability 声明 required_scopes；
M12 PolicyGate 判断当前 Run 是否允许使用该 binding；
Scenario 只能声明使用哪个 binding，不直接保存 secret；
EventStore 只记录 credential binding id / scope，不记录 secret 本体。
```

---

## 3. 模块一览（15 + 横切）

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
| M09 | Memory             | `memory/manager.py`, `policy.py`, `checkpoint_compactor.py` | Episodic 摘要/召回、checkpoint 压缩         | Context 装配、Run 生命周期、RAG 建索引 | 中   | [memory-checkpoint-design](./memory-checkpoint-design.md) §8.5·§9                                                    |
| M10 | RAG                | `rag/`、`docs/source/`                                      | ingest、结构化 API 元数据、检索、Tool-grounded 注入 | Run/Event、审批     | **中高** | [rag-design](./rag-design.md) §11.0·[tool-grounded-design](./tool-grounded-design.md) §12.1 |
| M11 | Tool / Capability  | `tools/`, `agent/tool_handlers.py`                          | ToolSpec、注册、handlers、ToolResultModel   | 最终授权、审批、Run FSM    | 中   | [data-flow-design](./data-flow-design.md) §8；[tech-selection](./tech-selection-design.md) §4                       |
| M12 | Kernel PolicyGate  | `policy/registry.py`, `nodes` safety_gate                   | 最终 allow/ask/deny、风险分级、审批、hooks、timeout/sandbox 策略 | 工具 Handler 实现       | 低   | [guardrail-policy-design](./guardrail-policy-design.md) §10·§11                                                    |
| M13 | Observability      | `observability/langfuse_tracer.py`                          | Langfuse trace/span、日志               | EventStore 写入    | 低   | [observability-design](./observability-design.md) §9·§10                                                           |
| M14 | Credential / Session | `conversation_store.py`（现状）；目标 `credentials/`          | 会话凭据绑定、TTL、scope、未来多租户 credential 管理 | Tool 授权裁决、平台账号体系 | 中→低 | **本页 §2.7 / M14 说明**（无 design doc）                                                                                |
| M15 | Context Manager    | 目标：`context/manager.py`；现状分散于 `runner.py`/memory/rag | 统一构造 ContextBundle、budget packing、截断/压缩审计 | RAG 建索引、Memory 存储、LLM 生成 | 低 | **本页 §2.5**；后续可拆 `context-design.md` |


**横切**


| 名称        | 锚点                             | 边界               | 成熟度 | 设计文档 · 缺口 §                                                             |
| --------- | ------------------------------ | ---------------- | --- | ----------------------------------------------------------------------- |
| Eval / 回归 | `scripts/verify_*.py`, `eval/` | 验证行为与契约，不实现产品逻辑  | 中   | [eval-design](./eval-design.md) §7·§8；[ci-design](./ci-design.md) §2–§5 |
| UI 控制台    | `static/index.html`            | 本地 Timeline/审批调试 | —   | —                                                                       |
| 配置        | `settings.py`                  | 环境变量聚合，不含业务规则    | —   | —                                                                       |


**非 MVP（刻意后置）**：Planning（[tech-selection](./tech-selection-design.md) §4）、Multi-Agent、外部队列（Temporal/Celery）、Mem0/Zep、多租户、持久化 Credential/Secret Manager。

**M14 说明**（无独立 `*-design.md`）：当前 `ConversationCookieStore` 在进程内按 `conversation_id`（即 `thread_id`）保存水印平台登录 Cookie，供 `http_get`/`http_post` 带会话访问；TTL 由配置控制，**不**落盘、**不**替代平台账号体系。该实现仅作为 single-process MVP。未来按 §2.7 演进为 `CredentialBinding`：Scenario 只引用 binding 名称，M11 声明 required scopes，M12 PolicyGate 决定当前 Run 是否允许使用，EventStore 只记录 binding id / scope，不记录 secret 本体。

---

## 4. 模块依赖（允许方向）

```text
M01 → M03 → M07 → M06 → (LLM M08, Tool M11 via graph)
M07 → M15 → (checkpoint, M09 memory, M10 RAG, ToolRegistry schemas, M12 policy hints)
M07 → M05 → M02
M07 → M13（trace）
M06/M07 → M12（Kernel PolicyGate 最终裁决）
M11 → M05（ToolResultModel）
M11 → M14（credential/session binding 读取，需经 M12 授权）
M04 只读 M02
```

**禁止**：M02 调用 M07；M10 写入 Run FSM；M11/Capability 自行绕过 M12 授权；Scenario 注入任意执行逻辑；M12 在 `tools/http_tools` 外重复实现 HTTP；EventStore 从 checkpoint 反推产品状态。

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
| 模块边界、成熟度、**八层栈改造顺序** | **本页** §3–§4、**§7** | 15 模块地图、依赖、按层波次索引 |
| **Kernel / Capability / Scenario 分层** | **本页** **§2.0、§2.4** | 主架构视角、通用范式、目录约定、与八层栈关系 |
| M14 Credential / Session（无 design doc） | **本页** §2.7、§3 **M14 说明** | 进程内 WMSESSIONID、未来 CredentialBinding、与 M11/M12 分工 |
| Run/Thread、cancel/approve   | [runtime-design.md](./runtime-design.md)                     | FSM、ExecutionEngine、Timeline、审批续跑              |
| 事件/工具 payload 契约            | [data-flow-design.md](./data-flow-design.md)                 | RuntimeEvent、ToolResult、Adapter、SSE/EventStore |
| Memory 与 checkpoint         | [memory-checkpoint-design.md](./memory-checkpoint-design.md) | Working memory 真相源、压缩、episodic                 |
| Context Manager             | **本页** §2.5；待补 `context-design.md` | ContextBundle、预算、检索/记忆/tool schema/policy hints 装配 |
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
L4 Pydantic     RuntimeEvent/ToolResult；FastAPI 入参；ScenarioConfig    data-flow-design §2
                ⚠️ memory/RAG 部分 loose payload
L5 Agent State  checkpoint；tool_route；episodic/LTM；Context Manager     memory-checkpoint；tool-grounded；本页 §2.5
                ❌ plan_updated / 显式 Planning；ContextBundle 待落地
L6 Tool Exec    search_docs + 白名单 HTTP + Kernel PolicyGate          tool-grounded；guardrail-policy
                ❌ MCP/DB/代码/邮件；shell/git sandbox
L7 Output       SSE token + ToolMessage 证据              data-flow-design §2
                ❌ 最终回答固定 JSON schema
L8 Storage      EventStore + Timeline + eval；失败一致性策略              runtime；observability；eval；本页 §2.6
                ⚠️ trace_id/cost 未闭环；无 GDPR 删除；durable resume 待做
```

### 7.1 已落地基线（2026-05）

| 层 | 已交付 | 验收 |
|----|--------|------|
| L1–L2 | 9 篇 `docs/source/` ingest、热更新、BM25+RRF+可选向量 | `verify_phase4_ragas.py`，[rag-design §0](./rag-design.md) |
| L3 | API 契约 `api_parse`；Memory 规则/LLM 抽取 + pending | `verify_rag_api_ingest.py`，`verify_memory_production_v2.py` |
| L4 | `RuntimeEvent`/`ToolResultModel` + Adapter 链 | `verify_contract_events.py`，[data-flow §2](./data-flow-design.md) |
| L5 | checkpoint 真相源；tool_router；episodic + `memory_items`；Context Manager 目标已定义 | `verify_memory_checkpoint_consistency.py` |
| L6 | Tool-grounded 路由；path 注入；诊断模板；审批；PolicyGate 目标边界已定义 | `verify_tool_router.py`，`--profile e2e` |
| L7 | 流式 NL + `retrieval_completed` 溯源；L4-lite | `verify_citation_l4.py` |
| L8 | EventStore/Timeline；`checkpoint_compacted`；core/rag/e2e eval；失败一致性策略目标已定义 | `verify_eval_suite.py` |

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
| L5 | Context Manager 单入口 PoC：ContextBundle、budget packing、截断审计 | **本页 §2.5**；后续 `context-design.md` |
| L5 | 检索 path merge 进 `tool_route`；`plan_updated` / 步骤 outcome PoC | [tool-grounded §12.1](./tool-grounded-design.md)、[tech-selection §4](./tech-selection-design.md) |
| L5 | Memory 续轮 inject 去重收尾；episodic 向量索引（可选） | [memory §8.5](./memory-checkpoint-design.md) |
| L6 | PolicyGate kernel 化：Scenario policy 只能收紧；Capability 不自授权 | [guardrail §10.5](./guardrail-policy-design.md)、**本页 §2.4.3** |
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
| L8 | EventStore/checkpoint 失败一致性：sequence、tool_call_id、last_successful_event_id | **本页 §2.6**、[runtime §8.1](./runtime-design.md) |
| L8 | `running`/`queued` durable resume 或外部队列 PoC | [runtime §8.1](./runtime-design.md) |

---

### 7.5 第 4 波 — 平台扩展（非 Demo 阻塞）

| 层 | 改造项 | 设计文档 |
|----|--------|----------|
| L1 | 网页/DB 同步 ingest | [rag-design §11.0](./rag-design.md) |
| L3–L4 | 多租户 `tenant_id` + 服务端 `user_id` 鉴权 | [memory §8.5](./memory-checkpoint-design.md)、[guardrail §10.5](./guardrail-policy-design.md) |
| L6 | MCP registry PoC；LiteLLM fallback | [guardrail §10.5](./guardrail-policy-design.md)、[tech-selection §4](./tech-selection-design.md) |
| L8 | Memory GDPR 删除 API；OpenTelemetry 双写 | [memory §8.5](./memory-checkpoint-design.md)、[observability §9.6](./observability-design.md) |
| M14 | CredentialBinding / Session Manager：多租户、scope、撤销、加密存储 | **本页 §2.7**；未来 `credential-design.md` |
| — | Multi-Agent、外部队列 Temporal | [tech-selection §4](./tech-selection-design.md) |

---

### 7.6 改造原则

1. **按层验收**：每波只动 1–2 个层，避免同时改 ingest + 编排 + 观测导致回归难定位。  
2. **文档下沉**：本页 §7 只保留波次索引；新增任务必须写入对应 design doc 的「八层栈改造分配」。  
3. **Eval 门禁**：每层至少一条 `verify_*` 或 eval case 扩展，见 [eval-design](./eval-design.md)。  
4. **Demo 优先**：第 1–2 波服务「生产文档 + live E2E」；第 4 波按需立项。
5. **Contract 先行**：ScenarioConfig、ToolSpec、ContextBundle、PolicyDecision、CredentialBinding 先定义 schema，再落实现。

