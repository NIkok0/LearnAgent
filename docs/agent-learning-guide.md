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

**文档维护规则**（防止内容重复回流）：

1. **套件 / Profile 数字** — 只改 `scripts/verify_eval_suite.py` + [ci-design.md](./ci-design.md)
2. **conda 命令块** — 只保留 [README §6](../README.md) + ci-design §7 两处
3. **八层栈新任务** — 只写入对应 `*-design.md` 的「八层栈改造分配（待办）」；本页 **§7** 只更新 L1–L8 状态行
4. **跨模块缺口** — 只写入 **§2.8**；各 design doc 只写模块独有 gap

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


| 视角                                 | 用途               | 是否作为主架构边界 |
| ---------------------------------- | ---------------- | --------- |
| **Kernel / Capability / Scenario** | 长期产品化分层、插件化、业务迁移 | **是**     |
| 产品层 / 编排层 / 决策层                    | 单次 Run 的运行时控制面   | 否，作为运行时解释 |
| L1–L8 八层栈                          | 阶段性改造路线与验收波次     | 否，作为实施计划  |


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

M15 Context Manager（M15 单入口，见 [context-manager-design.md](./context-manager-design.md)）
    +-- checkpoint messages ----> working memory
    +-- RAG snippets -----------> M10 rag/（search_docs，不经 checkpoint 全量存储）
    +-- episodic inject --------> M09 memory/manager.py
    +-- tool schemas/policy ----> ToolRegistry + M12 PolicyGate（含 required_scopes）
    +-- token budget -----------> context packing / compaction
    +-- credential audit -------> EventStore `credential_binding_audit`（M12/M14，见 §2.7）
```

### 2.3 六条边界规则

1. **产品层（M02–M04、M03）**：Run 状态、Timeline、cancel/approve 以 EventStore 为准；客户端不自行重建 Run 状态机。详见 [runtime-design.md](./runtime-design.md)。
2. **编排层（M06–M07）**：LangGraph checkpoint 的 `messages` 为 **working memory 真相源**；HTTP `messages[]` 仅传当前轮 user。详见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)。
3. **契约层（M05）**：跨模块 payload 以 `RuntimeEvent`、`ToolResultModel`、`ContextBundle`、`PolicyDecision` 为准；写入前经 Adapter 展平/脱敏。详见 [data-flow-design.md](./data-flow-design.md)。
4. **审计层（M02）**：不用 EventStore 事件 replay 生成 checkpoint 全量历史；Episodic 摘要仅作 inject，不替代对话正文。
5. **策略层（M12）**：最终权限裁决权归 Kernel PolicyGate；Capability 只能声明风险，Scenario 只能收紧策略，不能放宽 Kernel 默认策略。
6. **上下文层（M15）**：所有 LLM 输入统一经 Context Manager 装配；Runner、Node、Memory、RAG 不各自拼接上下文。

### 2.4 Kernel / Capability / Scenario 三层

LearnAgent 的长期架构目标：**先稳定 Kernel，再插 Capability，最后用 Scenario 承载业务差异——与 Kernel 解耦，而不是把业务逻辑写进 Kernel。**

> **Scenario ≠ Pack**：`scenarios/<name>/` 只是配置存放方式之一；目标是**配置与 Kernel 源码分离**，不是做成可分发「场景包」。详见 §2.4.4。

shell / git / MCP 与 RAG / HTTP 同级，都是 Capability 层 Tool 扩展，**不**改写 Run FSM、EventStore、Contracts 或 PolicyGate。

#### 2.4.1 三层定义

```text
┌─────────────────────────────────────────────────────────────┐
│  Scenario（业务配置层）— 声明式 overlay，不执行代码            │
│  prompt · policy allowlist · router rules · docs 绑定       │
│  HTTP path 白名单 · credential binding 引用 · budget/eval   │
│  只能收紧 Kernel 默认策略，不能放宽安全边界                    │
└───────────────────────────┬─────────────────────────────────┘
                            │ 只读加载 / 覆盖
┌───────────────────────────▼─────────────────────────────────┐
│  Capability（能力层）— ToolSpec 声明 + Handler 实现           │
│  RAG · HTTP · shell · git · MCP · code index …               │
└───────────────────────────┬─────────────────────────────────┘
                            │ 注册 / 被调度
┌───────────────────────────▼─────────────────────────────────┐
│  Kernel（内核）— 跨场景稳定 Agent Runtime                      │
│  Run FSM · LangGraph · EventStore/Timeline · Contracts       │
│  Context Manager · PolicyGate · Memory · Eval · Audit        │
└─────────────────────────────────────────────────────────────┘
```


| 层              | 回答的问题                         | 换场景时        | LearnAgent 模块（现状 / 目标）                                                 |
| -------------- | ----------------------------- | ----------- | ---------------------------------------------------------------------- |
| **Kernel**     | 一次 Run 怎么启停、怎么审计、怎么授权、怎么装配上下文 | **不换**      | M02–M07、M05、M09、M12、M13、M15、Eval 横切                                    |
| **Capability** | 有哪些工具/知识源、参数和结果怎么校验、如何执行      | **按需启用**    | M10 RAG、M11 Tool handlers、`ToolRegistry` / `ToolSpec`                  |
| **Scenario**   | 这个业务查什么文档、允许什么工具、说什么话、怎么评测    | **换业务时改配置** | 配置：`config/<name>.yaml`；加载器：`copilot_agent/scenario/`（Kernel 侧只读） |


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
  → Kernel PolicyGate（risk · approval · scenario allowlist · required_scopes · hooks · timeout · sandbox）
  → Tool Execution（Capability Handler）
  → ToolResultModel / RuntimeEvent(tool_end)
  → Observation → EventStore（tool_* / retrieval_* / memory_* / credential_binding_audit）
  → （可选）Reflector / Replan
  → Output（SSE token + done）
```

Kernel 只保证这条链**可跑、可测、可观测、可审计、可恢复**；具体是 `search_docs`、`http_get` 还是 `run_shell`，由 Capability + Scenario 决定。

#### 2.4.3 Capability 扩展约定

所有外部能力（含 MCP）经 **ToolSpec → ToolRegistry → PolicyGate → Handler → ToolResultModel** 同一管道注册；禁止在 Graph 节点内散落裸 HTTP / subprocess。

**详细设计**：[tool-design.md §2](./tool-design.md)（Capability packs、`COPILOT_CAPABILITIES`、category 与职责边界）。

#### 2.4.4 Scenario 配置存放约定（解耦，非打包）

> **现状（2→3→1 扁平 + K/C/S 解耦，2026-05）**：主业务配置在 `config/watermark.yaml`；router / mcp / prompt 在 `config/`；RAG 语料在 `scenarios/watermark/docs/`（`docs_manifest.json`）；MCP server 在 `scenarios/watermark/mcp/`；HTTP 路径白名单在 Scenario `resources.http_*_paths`（经 `HttpPathPolicy` 注入 Kernel，**不在** `http_tools.py` 硬编码）。  
> **加载**：默认 `SCENARIO=minimal` 用于 Kernel smoke；Demo 显式设置 `SCENARIO=watermark`。`scenario/loader.py` 只读取 `config/<name>.yaml`，不再支持旧目录树 `scenarios/<name>/scenario.yaml`。

**三层路径，别混为一谈**：


| 路径                        | 归属            | 含义                                       |
| ------------------------- | ------------- | ---------------------------------------- |
| `copilot_agent/scenario/` | **Kernel 模块** | loader、schema、`RouterEngine`（读 YAML 的引擎） |
| `config/`                 | **业务 overlay** | 扁平主配置 + prompt / router / mcp 指针      |
| `scenarios/<name>/docs/`  | **语料数据**      | RAG markdown + `docs_manifest.json`（glob / load_order） |
| `scenarios/<name>/mcp/`   | **业务 MCP 脚本** | stdio server 实现（如 `watermark_ops.py`），由 `config/*-mcp.yaml` 指向 |


Scenario 内容必须是**声明式**（YAML / Markdown / JSON），不允许塞任意 Python；字段经 `ScenarioConfig` 等 schema 校验。

**Capability 开关在部署层**，不在 Scenario YAML：

```bash
COPILOT_CAPABILITIES=rag,http,mcp   # 默认；设 none 可测零 capability 部署
```

**当前 Demo 布局**：

```text
config/
  watermark.yaml              # 主配置：policy、budgets、eval、resources（含 HTTP 白名单、credential）
  minimal.yaml                # Kernel smoke：policy 收紧、零 tool
  watermark-prompt.md
  watermark-router.yaml
  watermark-mcp.yaml          # → scenarios/watermark/mcp/watermark_ops.py
  watermark-memory.yaml
  watermark-rag.yaml          # query rewrite / doc_type boost
  watermark-diagnosis.yaml    # troubleshooting 模板

scenarios/watermark/
  docs/                       # RAG 语料（9 篇 md + docs_manifest.json）
  mcp/watermark_ops.py        # 业务 MCP server（不在 copilot_agent/ 树内）

copilot_agent/scenario/       # Kernel loader + RouterEngine + HttpPathPolicy
  loader.py
  http_paths.py
  router/
```

**`config/watermark.yaml` 结构（扁平）**：

```yaml
name: watermark
policy: { tool_allowlist: [...], ... }      # 内联 policy
prompt_file: config/watermark-prompt.md
router: config/watermark-router.yaml        # 相对 repo 根
mcp: config/watermark-mcp.yaml              # 仅当 COPILOT_CAPABILITIES 含 mcp
memory_policy: config/watermark-memory.yaml
docs_dir: scenarios/watermark/docs
eval:
  golden: eval/golden/runtime-golden-scenarios.json
  rag_cases: eval/phase4-eval-cases.json
resources:
  api_base_url_env: WATERMARK_API_BASE_URL   # Scenario 声明 env 名；Kernel resolve_api_base_url()
  docs_path_env: COPILOT_DOCS_PATH
  credential_binding: wmsession
  credential_cookie_name: WMSESSIONID
  credential_provider: watermark_java_api
  credential_scopes: [http:read, http:write]
  http_get_actuator_paths: [/actuator/health]
  http_get_patterns: [^/api/v1/stats/dashboard/?$, ...]
  http_post_paths: [/api/v1/auth/login, /api/v1/jobs/watermark]
  rag_rules: config/watermark-rag.yaml
  diagnosis: config/watermark-diagnosis.yaml
budgets: { max_context_chars: 14000, ... }
```

**Scenario 可以做**：

```text
选择 policy / prompt / router / docs 绑定；
声明 tool allowlist / denylist；
设置业务 eval 和 budget；
收紧 Kernel 默认 policy。
```

**Scenario 不可以做**：

```text
修改 Tool handler；
安装或开关 Capability（由 COPILOT_CAPABILITIES 控制）；
绕过 Kernel PolicyGate；
禁用 Kernel Eval / safety gate；
写入 EventStore schema；
改 Run FSM；
直接执行 subprocess / HTTP；
放宽 Kernel 默认 policy。
```

**加载规则**：

1. 启动时读 `SCENARIO`（默认 `minimal`），`loader` 优先解析 `config/<name>.yaml`。
2. `COPILOT_CAPABILITIES`（默认 `rag,http,mcp`）决定**部署启用哪些 Capability**；Scenario 的 `policy.tool_allowlist` 只做收紧。
3. Prompt / router / policy / eval 仅 overlay Kernel 默认项；**不修改** `runtime/`、`contracts/` 源码。
4. Scenario policy 与 Kernel policy **取交集**（Scenario 只能收紧）。
5. 换业务 = 换 `config/<name>.yaml` 或 `SCENARIO` 名，**不是**换 Kernel 源码。

#### 2.4.5 场景定制 vs Capability vs Kernel 改动（决策表）


| 需求                          | 应改 Scenario             | 应改 Capability                 | 应改 Kernel                     |
| --------------------------- | ----------------------- | ----------------------------- | ----------------------------- |
| 换文档语料                       | ✅ `docs_dir` + `docs_manifest.json` | —                             | —                             |
| 换 API 白名单                   | ✅ `resources.http_*_paths` → `HttpPathPolicy` | —                | —                             |
| 换意图分类规则                     | ✅ `config/*-router.yaml` | —                             | —                             |
| 换 Scenario prompt           | ✅ `config/*-prompt.md`  | 内置 `DEFAULT_KERNEL_PROMPT` 仅作 fallback | —                             |
| RAG rewrite / diagnosis      | ✅ `config/*-rag.yaml` / `*-diagnosis.yaml` | —                | —                             |
| 调整上下文预算                     | ✅ `budgets` + Context Manager packing | —                             | ⚠️ 高级 packing 策略可扩展       |
| 新增 MCP server               | ✅ policy allowlist + `config/*-mcp.yaml` 指针 | ✅ mcp adapter + Scenario `mcp/` 脚本 | —                             |
| 新增 shell 工具                 | ⚠️ policy sandbox 配置    | ✅ shell handler（待建）       | ⚠️ PolicyGate / sandbox hooks |
| Run cancel 语义               | —                       | —                             | ✅ runtime                     |
| 新 Event kind                | —                       | —                             | ✅ contracts + event_schema    |
| EventStore/checkpoint 失败一致性 | —                       | —                             | ⚠️ 目标已定义（§2.6）；见 **§7 L8**、§2.8 |
| Credential binding + scope   | ✅ `resources.credential_*` | ✅ `ToolSpec.required_scopes` | ✅ `CredentialManager` + PolicyGate 裁决 + `credential_binding_audit` |
| 多租户 / 加密 credential       | ⚠️ `tenant_id` 字段预留   | —                             | ⚠️ MVP 进程内 memory；长期见 **§2.8** |


**原则**：Scenario 声明业务意图；Capability 实现一种能力；Kernel 负责调度、授权、上下文、审计、恢复与状态一致性。

#### 2.4.6 实现路线图（与 §7 对齐）


| 阶段     | 目标                                                            | 对应 §7                                                      |
| ------ | ------------------------------------------------------------- | ---------------------------------------------------------- |
| **A**  | Scenario 与 Kernel 解耦 + loader                                 | ✅ `scenario/loader` + `config/watermark.yaml` 作 Demo 配置     |
| **B**  | 统一 Context Manager 单入口                                        | ✅ `context/manager.py` + `ContextBundle` + `verify_context_manager.py` |
| **C**  | PolicyGate kernel 化：allow/ask/deny、scenario allowlist、**required_scopes** | ✅ `PolicyRegistry` + `nodes.safety_gate` |
| **C′** | K/C/S 代码分层：`kernel/bootstrap` + `tools/capability/`*          | ✅ 2026-05                                                  |
| **C″** | Scenario 声明式 tool router：`router/rules.yaml` + `RouterEngine` | ✅ 2026-05                                                  |
| **D**  | EventStore/checkpoint 失败一致性与 idempotency                      | §7 L8、[runtime-design](./runtime-design.md) |
| **E**  | MCP Capability adapter + Scenario 外置 server                     | ✅ `tools/extensions/mcp/` + `scenarios/watermark/mcp/` + `config/watermark-mcp.yaml` |
| **F**  | shell / git Capability + `scenarios/coding/` 示例               | §7 L6、§2.8 |
| **G**  | M14 Credential/Session + M12 scope 裁决                         | ✅ MVP；长期加密/多租户见 **§2.8** |


详细任务仍写入各 `*-design.md` 的「八层栈改造分配」；本节只定**主架构边界、目录约定与跨层决策规则**。

#### 2.4.7 代码分层映射（K/C/S → 目录）


| 层              | 职责                                    | 代码锚点                                                                         |
| -------------- | ------------------------------------- | ---------------------------------------------------------------------------- |
| **Scenario**   | policy、prompt、budget、HTTP 白名单、credential 引用 | `config/<name>.yaml` · `scenario/loader.py` · `scenario/http_paths.py` |
| **Capability** | ToolSpec（含 `required_scopes`）+ Handler 注册 | `tools/capability/{rag,http,mcp}.py` · `settings.enabled_capabilities()` |
| **Kernel**     | 启动装配、PolicyGate、Context、Graph Run、Credential 注入 | `kernel/bootstrap.py` · `policy/` · `credentials/` · `context/manager.py` · `agent/runner.py` |


```text
server lifespan
  → load_scenario()                    # Scenario
  → McpRuntime.start()                 # Capability 运行时（MCP）
  → build_kernel_components()          # Kernel bootstrap
       → load_capability_packs()       # 按 settings.enabled_capabilities() 注册 ToolSpec（含 required_scopes）
       → HttpPathPolicy.from_resources() + bind_http_path_policy()
       → PolicyRegistry(credential_manager=...)  # Scenario policy ∩ scope gate ∩ Kernel gate
  → ChatRunner                         # LangGraph Run 循环（仅编排，不注册工具）
```

**验收（K/C/S 解耦）**：`verify_scenario_loader.py`、`verify_context_manager.py`、`verify_mcp_capability.py`、`verify_policy_credentials.py`、`verify_policy_docs_contract.py`（`--profile core`，见 [eval-design §3](./eval-design.md)）。

新增 Capability 时：**只加** `tools/capability/<name>.py` 并在 `loader.CAPABILITY_PACKS` 注册；**不改** `runner.py` / `runtime/` / `contracts/`。

#### 2.4.8 Kernel 解耦残留（Known coupling，换业务时可清理）

以下不影响 watermark Demo，是换业务时仍需关注的兼容/配置点：

| 位置 | 残留 | 换业务建议 |
| ---- | ---- | ---------- |
| `settings.scenario` | 默认已改为 `minimal` | Demo/业务部署显式设 `SCENARIO=<name>` |
| API base URL | 已从 Kernel watermark setting 移到 Scenario resources | 使用 `api_base_url` / `api_base_url_env` / `default_api_base_url` |
| `server.py` FastAPI title | 已改为 `LearnAgent` | 完成 |
| docs path | Kernel 只写 Scenario 声明的 `docs_path_env`，默认 `COPILOT_DOCS_PATH` | watermark 如需专用 env，在 Scenario resources 声明 |
| `scenario/loader.py` | 不再回退到 `scenarios/watermark/docs` | 新 Scenario 必须显式 `docs_dir` 或 `resources.docs_fallback` |
| `tools/http_tools.py` | HTTP client 已中性化为 `ScenarioHttpClient` | 保持业务路径只来自 Scenario resources |

Tool 描述与 `registry.from_agent_tools()` 文案已中性化；MCP / 路径名中的 `watermark` 属于 Scenario 配置，合理保留。

### 2.6 EventStore 与 Checkpoint 的失败一致性策略

EventStore 是产品事实源；LangGraph checkpoint 是 working memory 真相源。二者职责不同，但同一次 Run 中会被连续更新，因此必须定义失败语义。

#### 2.6.1 基本原则

```text
EventStore 记录「产品事实」：run_started、tool_start、tool_end、approval、run_completed、run_failed、`credential_binding_audit`（仅 binding id / scope，无 secret）。
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


| 场景                             | 处理策略                                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------------------- |
| EventStore 写失败，checkpoint 未写   | 停止本步，返回 `run_failed` 或重试；不得继续执行工具                                                           |
| EventStore 写成功，checkpoint 写失败  | 标记 `checkpoint_sync_failed`，Run 进入 `recoverable_failed`，允许从 EventStore + last checkpoint 恢复 |
| Tool 执行成功，tool_end 写失败         | 使用 `tool_call_id` 幂等重试写入；禁止重复执行有副作用 Tool                                                    |
| checkpoint 写成功，assistant 事件写失败 | 重试事件写入；若失败，标记 timeline 不完整，不影响 checkpoint 恢复                                                |
| SSE 已推送，落库失败                   | 前端展示为 transient；服务端以 EventStore 为准，下次刷新以后端事实为准                                              |
| cancel 与 tool execution 并发     | cancel 写 EventStore；若 Tool 不可中断，等待 ToolResult 后进入 cancelling → cancelled / failed           |


#### 2.6.5 最小 MVP 要求

```text
1. 每个 tool_call_id 幂等；
2. 每个 RuntimeEvent 有 run 内 sequence；
3. run_failed 携带 last_successful_event_id；
4. EventStore 写失败时不能静默继续；
5. checkpoint compact / sync 失败必须可观测。
```

### 2.7 M14 Credential / Session 体系

#### 2.7.0 现状（2026-05）

- **`CredentialBinding` + `CredentialManager`**（`credentials/`）：Scenario `resources.credential_*` 声明 binding id、provider、scopes；进程内 memory 存储（single-process MVP）。
- **`ToolSpec.required_scopes`**（M11）：Capability 声明工具所需 scope（如 `http_get` → `http:read`，`http_post` → `http:write`）。
- **PolicyGate 统一裁决**（M12）：`PolicyRegistry.evaluate_required_scopes()` 在 `safety_gate` 前检查 binding 是否授予所需 scope；拒绝时 reason=`credential_scope_denied`。
- **EventStore 审计**：`credential_binding_audit` 事件（`scope_allowed` / `scope_denied` / `credential_set` / `credential_read_denied`）；payload 经 `CredentialBindingAuditPayload` 严格校验，**不含 secret**。
- 旧 `ConversationCookieStore` 已删除；Cookie/session 统一通过 `CredentialManager` 管理。

**验收**：`verify_policy_credentials.py`。

长期仍待：加密存储、外部 secret manager、多租户撤销与过期（**§2.8**）。

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
短期：✅ 进程内 Cookie Store + CredentialBinding schema（single-process MVP）。
中期：✅ Scenario 只引用 binding 名称；M12 PolicyGate scope 裁决 + EventStore credential_binding_audit。
长期：多租户、撤销、过期、加密存储和外部 secret manager。
```

#### 2.7.3 边界

```text
M14 负责 credential/session 绑定与读取（含 audit_ref，不含 secret）；
M11 Capability 声明 required_scopes；
M12 PolicyGate 判断当前 Run 是否允许使用该 binding（统一裁决，Handler 不得自授权）；
Scenario 只能声明使用哪个 binding / scopes，不直接保存 secret；
EventStore 通过 credential_binding_audit 只记录 binding id / scope / action，不记录 secret 本体。
```

---

### 2.8 全局 Known Gaps（单一清单）

跨模块缺口只在本节维护；各 `*-design.md` 的「遗留问题 / 未来优化」只写**本模块独有**项，全局项请链到本节。

| 缺口 | 状态 | 主文档 |
|---|---|---|
| 真实 LLM E2E（`verify_demo_golden_e2e.py --mode live`） | ❌ | [tool-design §5](./tool-design.md) |
| RAGAS 作为 PR 硬门禁 | ❌ | [eval-design §0](./eval-design.md)、[rag-design §11.2](./rag-design.md) |
| `trace_id` / generation span / token-cost 闭环 | ❌ | [observability §0](./observability-design.md) |
| EventStore/checkpoint **失败一致性**（sequence、`last_successful_event_id`） | ⚠️ | **本页 §2.6**、[runtime §7.3](./runtime-design.md) |
| `running`/`queued` **durable resume** | ❌ | [runtime §8.1](./runtime-design.md) |
| 策略表 YAML 版本化 + `policy_version` 事件 | ❌ | [guardrail §10.5](./guardrail-policy-design.md) |
| 输出 Guard（secret/PII 模式检测） | ❌ | [guardrail §10.2](./guardrail-policy-design.md) |
| Promptfoo 场景编排 | ❌ | [eval-design §7](./eval-design.md) |
| Memory GDPR 删除 / 服务端 `user_id` 鉴权 | ❌ | [memory §8.5](./memory-checkpoint-design.md) |

---

## 3. 模块一览（15 + 横切）

成熟度 **高 / 中 / 低**；缺口细节见同列设计文档的 **未来优化** / **遗留问题**（`data-flow-design` 无遗留节，见该文档 §8）。


| ID  | 模块                   | 代码锚点                                                        | 职责                                                 | 不负责                         | 成熟度    | 设计文档 · 缺口 §                                                                                                        |
| --- | -------------------- | ----------------------------------------------------------- | -------------------------------------------------- | --------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------ |
| M01 | API / Server         | `copilot_agent/server.py`                                   | HTTP/SSE/WS、请求校验、挂载 Engine/Runner                  | Run FSM、图节点                 | 高      | [README](../README.md) §5–6；长 Run/WS → [runtime-design](./runtime-design.md) §8·§9                                 |
| M02 | Runtime Contract     | `runtime/event_store.py`, `run_state.py`, `event_schema.py` | Thread/Run/Event 事实源、FSM、事件类型                      | LLM、工具实现                    | 高      | [runtime-design](./runtime-design.md) §8·§9；[data-flow-design](./data-flow-design.md) §8                           |
| M03 | Execution Engine     | `runtime/execution_engine.py`                               | Run 调度、cancel/approve、超时、流队列、终态触发压缩                | 事件 schema、图逻辑               | 中      | [runtime-design](./runtime-design.md) §8·§9                                                                        |
| M04 | Timeline 读模型         | `runtime/timeline.py`                                       | events → UI/API timeline 投影                        | 写入 EventStore               | 高      | [runtime-design](./runtime-design.md) §6.3、§8·§9                                                                   |
| M05 | Contracts            | `copilot_agent/contracts/`                                  | Envelope、ToolResult、Adapter、validate               | 业务编排                        | 中      | [data-flow-design](./data-flow-design.md) §8                                                                       |
| M06 | Agent Graph          | `agent/graph.py`, `nodes.py`, `state.py`                    | LangGraph 图、路由、`safety_gate`                       | REST、Run API                | 中      | [memory-checkpoint-design](./memory-checkpoint-design.md) §8·§9；编排 [tech-selection](./tech-selection-design.md) §4 |
| M07 | ChatRunner / 流映射     | `agent/runner.py`, `stream/event_mapper.py`                 | 图输入、astream、emit RuntimeEvent                      | EventStore SQL              | 中      | 同 M06；事件形状 [data-flow-design](./data-flow-design.md)                                                               |
| M08 | LLM                  | `llm/provider.py`                                           | ChatOpenAI 配置与薄封装                                  | Tool、Memory 策略              | 中      | [tech-selection-design](./tech-selection-design.md) §4                                                             |
| M09 | Memory               | `memory/manager.py`, `policy.py`, `checkpoint_compactor.py` | Episodic 摘要/召回、checkpoint 压缩                       | Context 装配、Run 生命周期、RAG 建索引 | 中      | [memory-checkpoint-design](./memory-checkpoint-design.md) §8.5·§9                                                  |
| M10 | RAG                  | `rag/`、`scenarios/watermark/docs/`（业务语料）              | ingest、结构化 API 元数据、检索、Tool-grounded 注入             | Run/Event、审批                | **中高** | [rag-design](./rag-design.md) §0 |
| M11 | Tool / Capability    | `tools/capability/*`, `agent/tool_handlers.py`                | ToolSpec（含 `required_scopes`）、注册、handlers、Tool-grounded 编排 | 最终授权、审批、Run FSM、EventStore 直写 | 中      | [tool-design](./tool-design.md)；契约 [data-flow](./data-flow-design.md) §0 |
| M12 | Kernel PolicyGate    | `policy/registry.py`, `nodes.safety_gate`                   | allow/ask/deny、scenario allowlist、**required_scopes 裁决**、dangerous path、MCP allowlist | 工具 Handler 实现、secret 存储      | 中      | [guardrail-policy-design](./guardrail-policy-design.md) §10·§11；`verify_policy_credentials.py`                    |
| M13 | Observability        | `observability/langfuse_tracer.py`                          | Langfuse trace/span、日志                             | EventStore 写入               | 低      | [observability-design](./observability-design.md) §9·§10                                                           |
| M14 | Credential / Session | `credentials/`（`CredentialManager`） | binding 元数据、scope gate、**`credential_binding_audit` 写入** | PolicyGate 裁决、平台账号体系、secret 持久化 | 中      | **本页 §2.7**                                                                                 |
| M15 | Context Manager      | `context/manager.py`, `contracts/context.py`                | `ContextBundle` 装配、memory/router/RAG/preretrieval/packing | RAG 建索引、Memory 存储、LLM 生成、权限裁决    | **中**   | [context-manager-design.md](./context-manager-design.md) |


**横切**


| 名称        | 锚点                             | 边界               | 成熟度 | 设计文档 · 缺口 §                                                             |
| --------- | ------------------------------ | ---------------- | --- | ----------------------------------------------------------------------- |
| Eval / 回归 | `scripts/verify_*.py`, `eval/` | 验证行为与契约，不实现产品逻辑  | 中   | [eval-design](./eval-design.md) §7·§8；[ci-design](./ci-design.md) §2–§5 |
| UI 控制台    | `static/index.html`            | 本地 Timeline/审批调试 | —   | —                                                                       |
| 配置        | `settings.py`                  | 环境变量聚合，不含业务规则    | —   | —                                                                       |


**非 MVP（刻意后置）**：Planning（[tech-selection](./tech-selection-design.md) §4）、Multi-Agent、外部队列（Temporal/Celery）、Mem0/Zep、多租户、持久化 Credential/Secret Manager。

**M14 说明**（无独立 `*-design.md`）：Scenario `resources.credential_*` → `CredentialManager` + `CredentialBinding`；M11 声明 `ToolSpec.required_scopes`；M12 `PolicyRegistry` 统一裁决；EventStore 写入 `credential_binding_audit`（binding id / scope / action，无 secret）。进程内 memory 为 single-process MVP；长期见 **§2.8**。

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

- **数据集**：`eval/phase4-eval-cases.json`（**20** 条 docs + api/safety 期望）、`eval/golden/runtime-golden-scenarios.json`（Run 事件契约）、`eval/golden/demo-golden-scenarios.json`（Demo 1–6 proxy）
- **聚合入口**：`scripts/verify_eval_suite.py`；Profile 与套件数 SSOT → [ci-design §2–§6](./ci-design.md)；分层职责 → [eval-design](./eval-design.md)；本地命令 → [README §6](../README.md)
- **CI**：单一 `eval-ci.yml`（PR = `core` + `rag`；Nightly = `full`）

全局 Eval 缺口见 **§2.8**。

---

## 6. 文档索引


| 你想…                                    | 文档                                                                                     | 主题（写什么）                                            |
| -------------------------------------- | -------------------------------------------------------------------------------------- | -------------------------------------------------- |
| 跑起来、调 API                              | [README.md](../README.md)                                                              | 安装、环境变量、REST/SSE API、本地运行                          |
| 模块边界、成熟度、**八层栈实现状态**                   | **本页** §3–§4、**§7**                                                                    | 15 模块地图、依赖、L1–L8 一览                                  |
| **Kernel / Capability / Scenario 分层**  | **本页** **§2.0、§2.4**                                                                   | 主架构视角、通用范式、目录约定、与八层栈关系                             |
| M14 Credential / Session（无 design doc） | **本页** §2.7、§3 **M14 说明**                                                              | CredentialBinding、PolicyGate scope 裁决、`credential_binding_audit`、与 M11/M12 分工  |
| Run/Thread、cancel/approve              | [runtime-design.md](./runtime-design.md)                                               | FSM、ExecutionEngine、Timeline、审批续跑                  |
| 事件/工具 payload 契约                       | [data-flow-design.md](./data-flow-design.md)                                           | RuntimeEvent、ToolResult、Adapter、SSE/EventStore     |
| Memory 与 checkpoint                    | [memory-checkpoint-design.md](./memory-checkpoint-design.md)                           | Working memory 真相源、压缩、episodic                     |
| Context Manager                        | [context-manager-design.md](./context-manager-design.md)                                   | ContextBundle、`assemble()`、budget packing、`context_built` 审计 |
| Guardrail、审批、Scenario HTTP 白名单、scope 裁决 | [guardrail-policy-design.md](./guardrail-policy-design.md)                             | PolicyGate、`required_scopes`、`HttpPathPolicy`、与 Run 协作        |
| RAG 知识库与检索                         | [rag-design.md](./rag-design.md) | §0 状态表、ingest、检索评测 |
| Tool / Capability / Tool-grounded 编排 | [tool-design.md](./tool-design.md) | M11 注册执行 + M06 路由与轨迹评测 |
| 排障、Langfuse、ID 关联                      | [observability-design.md](./observability-design.md)                                   | EventStore 产品轨 + Langfuse、trace 关联                 |
| 全局 Known Gaps（跨模块缺口）              | **本页 §2.8**                                                                          | 真实 LLM E2E、RAGAS、trace、durable resume 等单一清单              |
| Eval 分层与 golden                        | [eval-design.md](./eval-design.md)                                                     | profile 语义、数据集、聚合协议（套件 SSOT → ci-design） |
| CI 失败怎么查                               | [ci-design.md](./ci-design.md)                                                         | `eval-ci.yml`（PR：core + rag；Nightly：full）、本地 conda 复现 |
| 框架选型、主线与优化方向                           | [tech-selection-design.md](./tech-selection-design.md) §3–§4                           | 对外框架对比、当前选择与优化方向                                   |
| Demo 验收与产品场景                           | [demo-requirements-design.md](./demo-requirements-design.md)                           | 水印任务 Agent + 文档问答验收                                |


**待补（可选）**：`api-design.md`（REST 字段若需从 README 抽离时再写）。

---

## 7. 八层栈实现状态

按 **Ingestion → Preprocess → Schema → Pydantic → Agent State → Tool Exec → Output → Storage/Audit** 组织；与 K/C/S 关系见 **§2.4**。  
**本页只保留各层现状一览**；待办任务写入各 `*-design.md` 的「八层栈改造分配（待办）」；跨模块缺口见 **§2.8**。

| 层 | 职责（简） | 状态 | 已实现（2026-05） | 主文档 |
|---|---|:---:|---|---|
| **L1** Ingestion | 语料接入、manifest、upload/reload | ✅ | `IngestSource`；Scenario `docs_manifest.json`；`POST /v1/rag/upload`；`COPILOT_DOCS_PATH` / Scenario docs env | [rag-design §0](./rag-design.md) |
| **L2** Preprocess | 分块、检索索引、context 预算 | ✅ | Markdown 分块；BM25 + RRF + 可选向量；动态 top-k；`response_fields` 解析 | [rag-design §0](./rag-design.md) |
| **L3** Schema | 结构化抽取、API/记忆 schema | ✅ | `api_parse`；Memory 规则/LLM 抽取 + pending；统一 `ExtractedRecord` | [rag-design §0](./rag-design.md)、[data-flow §0](./data-flow-design.md) |
| **L4** Pydantic | 边界契约、校验落库 | ✅ | `RuntimeEvent` / `ToolResultModel`；payload 子模型；`GET /events?validated=1` | [data-flow §0](./data-flow-design.md) |
| **L5** Agent State | checkpoint、路由、上下文装配 | ⚠️ | LangGraph checkpoint；规则 `tool_router`；**Context Manager** + `ContextBundle`；episodic/LTM inject | [context-manager §0](./context-manager-design.md)、[memory-checkpoint §0](./memory-checkpoint-design.md)、[tool-design §0](./tool-design.md) |
| **L6** Tool Exec | 受控工具、Policy、MCP | ⚠️ | `search_docs` + Scenario HTTP 白名单；PolicyGate + **required_scopes**；MCP adapter + Scenario `mcp/` | [tool-design §0](./tool-design.md)、[guardrail §0](./guardrail-policy-design.md) |
| **L7** Output | 流式输出、引用、结构化交付 | ⚠️ | SSE token + ToolMessage；`retrieval_completed` 溯源；L4-lite citation | [data-flow §0](./data-flow-design.md)、[tool-design §0](./tool-design.md) |
| **L8** Storage/Audit | EventStore、Timeline、eval、一致性 | ⚠️ | EventStore + Timeline；`checkpoint_compacted`；`credential_binding_audit`；eval suite（core + rag PR CI） | [runtime §0](./runtime-design.md)、[observability §0](./observability-design.md)、[eval-design §0](./eval-design.md) |

**图例**：✅ Demo 基线已闭环；⚠️ 主路径可用，仍有 §2.8 / design doc 待办。

**L1–L4 未做（远期）**：网页/DB 同步 ingest、PDF/OCR/HTML 通用预处理流水线。  
**L5–L8 主要待办**：`plan_updated` / live LLM E2E、path merge 进 `tool_route`、`timeout_seconds` 强制、策略表 YAML、`FinalAnswerModel`、失败一致性、`trace_id`/cost、durable resume — 详见 **§2.8** 与各 design doc。
