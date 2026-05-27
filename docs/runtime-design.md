# LearnAgent Runtime 设计

> 说明产品级 Runtime 语义：Thread/Run 生命周期、状态机、ExecutionEngine 与 EventStore/Timeline 的责权分界。  
> 事件与 payload 形状见 [data-flow-design.md](./data-flow-design.md)；项目模块地图见 [agent-learning-guide.md](./agent-learning-guide.md)。

**K/C/S 位置**：Kernel **M02–M04**（产品层 Run/Event 事实源）；与 LangGraph checkpoint 分界见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)。详见 [guide §2.1·§3](./agent-learning-guide.md)。

---

## 0. 实现状态

| 项 | 状态 | 验收脚本 |
|---|---|---|
| EventStore append / 分页（`after_id` + `limit`） | ✅ | `verify_runtime_domain.py --case event_store` |
| Timeline 投影 + SSE / WebSocket 同源 | ✅ | `verify_runtime_domain.py --case timeline`、`verify_session_mvp.py` |
| Checkpoint ↔ Run 关联 | ✅ | `verify_runtime_checkpoint_link.py` |
| ExecutionEngine cancel / approve / 并发槽 / 超时 | ✅ | `verify_runtime_domain.py --case execution_engine` |
| Run 创建幂等（`idempotency_key` + payload hash） | ✅ | `verify_runtime_domain.py --case execution_engine` |
| 进程重启 orphan 恢复（`queued`/`running`→failed，`cancelling`→cancelled，`waiting_approval` rehydrate） | ✅ | `verify_runtime_domain.py --case durability` |
| Checkpoint consistency v2（`checkpoint_consistency_checked` + `run_consistency_checked` 摘要） | ✅ | `verify_checkpoint_consistency_v2.py` |
| Side-effects 读模型（`GET /v1/runs/{id}/side-effects`） | ✅ | `verify_tool_governance_domain.py --case read_model` |
| Thread 生命周期清理（idle end → archive → checkpoint purge） | ✅ | `verify_thread_lifecycle_cleaner.py`、`verify_thread_archive_api.py` |
| Session MVP（REST + SSE 冒烟） | ✅ | `verify_session_mvp.py` |
| Golden Run 事件契约 | ✅ | `verify_golden_scenarios.py` |
| 失败一致性 v1（sequence / tool_end 幂等 / failure meta） | ✅ | `verify_runtime_domain.py --case execution_engine`、`verify_memory_checkpoint_consistency.py` |
| `running`/`queued` durable resume | ❌ | — |

套件见 [ci-design.md](./ci-design.md)。

---

## 1. 设计动机

LangGraph 解决「图怎么跑、checkpoint 怎么存」；LearnAgent 额外需要 **产品级 Runtime 契约**：

| 能力 | LangGraph 自带 | LearnAgent 必须自建 |
|------|----------------|-------------------|
| 一次用户请求 = 一次 Run | 否（只有 thread） | 是（`runs` 表 + FSM） |
| REST/SSE 可查询 timeline | 否 | EventStore + Timeline 投影 |
| cancel / approval API | interrupt 能力有，对外协议无 | ExecutionEngine + EventStore |
| 多 tab 并发、超时、限流 | 否 | ExecutionEngine 策略 |

本设计定义 **M02 Runtime 契约、M03 Execution Engine、M04 Timeline** 的行为边界，不重复 `RuntimeEvent` 字段表。

---

## 2. 三层责权（与 LangGraph 分界）

```text
产品层（本文档范围）
  EventStore .......... Thread/Run/Event 事实源；Run FSM 持久化
  ExecutionEngine ..... 调度 asyncio Task、cancel/approve、超时、并发槽
  TimelineProjector ... 只读投影，供 GET /timeline 与 UI

编排层（见 agent/graph、runner）
  LangGraph ........... planner -> assistant <-> tools；interrupt + Command(resume)
  Checkpoint .......... working memory（messages）；与 Run 通过 meta 事件关联

决策层
  LLM / Tool .......... 非确定性；结果经 M05 Contract 写入 EventStore
```

**原则**：客户端以 **Run id + EventStore** 判断进度；不以 LangGraph 内部 state 代替 Run 状态。

---

## 3. Thread 生命周期

| 状态 | 含义 | 典型触发 |
|------|------|----------|
| `active` | 会话进行中 | 创建 thread、新消息 |
| `ended` | 用户结束会话，仍可查历史 | `POST .../end` |
| `archived` | 冷归档；可清理 checkpoint | `ThreadLifecycleCleaner` 对 idle 超时的 `ended` thread 归档 |

```text
active --> ended --> archived
              |
              +--> archive 时 CheckpointStore.purge_thread(thread_id)
```

- `conversation_id`（旧 `/v1/chat`）与 `thread_id` 等价。
- Thread 状态存在 `threads` 表；与 Run 状态 **独立**（一个 thread 可有多个 run，但 **同时仅允许一个非终态 Run**）。
- 后台清理：`ThreadLifecycleCleaner` 按 `thread_active_idle_ttl_seconds` / `thread_ended_archive_ttl_seconds` 批量 end / archive；archive 时 purge checkpoint 并写 `thread_checkpoint_purged`。

---

## 4. Run 状态机

### 4.1 状态与终态

| 状态 | 终态？ | 含义 |
|------|:------:|------|
| `queued` | 否 | 已创建，等待执行槽 |
| `running` | 否 | 图在执行或流式输出中 |
| `waiting_approval` | 否 | LangGraph `interrupt()` 后暂停，等 approve/reject |
| `cancelling` | 否 | 已请求取消，协作收尾中 |
| `cancelled` | 是 | 用户取消或任务被 cancel |
| `completed` | 是 | 正常结束 |
| `failed` | 是 | 超时、异常、或进程重启后非 approval 等待态 |

合法转移见 `runtime/run_state.py` 中 `ALLOWED_RUN_TRANSITIONS`（代码为权威来源）。

### 4.2 转移示意（文本）

```text
queued --> running --> completed
              |   \--> failed
              |
              +--> waiting_approval --> running (resume approve)
              |                    \-> completed (reject 路径)
              |
              +--> cancelling --> cancelled

queued/running/waiting_approval --> cancelling --> cancelled
```

### 4.3 谁改状态

| 操作 | 模块 | Run 行 + 事件 |
|------|------|----------------|
| 创建 Run | ExecutionEngine | `queued` + `run_created`（payload 含 messages；可选 `idempotency_key` 复用已有 run） |
| 开始执行 | ExecutionEngine | `running` + `run_started` |
| 危险工具 interrupt | Graph → Engine | `waiting_approval` + `approval_required` + `run_checkpoint_meta` |
| approve | ExecutionEngine | `running` + `approval_resolved` + `run_started{resumed}` |
| reject | ExecutionEngine | `Command(resume=False)` 后 `completed`；可写 `policy_decision_recorded` / blocked `tool_side_effect_recorded` |
| PolicyGate scope deny | Graph → safety_gate | 无 tool 执行；可选 `credential_binding_audit(scope_denied)`、`policy_decision_recorded` |
| 正常结束 | ExecutionEngine | `completed` + `done` / `run_completed_meta` + consistency 事件 |
| cancel | ExecutionEngine | `cancelling` → `cancelled` + `cancel_requested` |
| 超时/异常 | ExecutionEngine | `failed` + `error` / `run_failed_meta` |

---

## 5. 核心流程

### 5.1 后台 Run + 流式输出

**两条创建路径**（均经 `ExecutionEngine.create_run`）：

| 入口 | `stream` | 客户端如何收事件 |
|------|:--------:|------------------|
| `POST /v1/threads/{id}/runs` | `false` | 轮询 `GET /v1/runs/{id}`、`/events`、`/timeline`，或订阅 `WS /v1/runs/{id}/ws` |
| `POST /v1/chat`（兼容） | `true` | 同 Run 的 **SSE**（`text/event-stream`），`manager.stream(run_id)` 消费 `stream_queue` |

```text
create_run (queued) + run_created
  -> asyncio.Task -> ChatRunner.run_stream
  -> RuntimeEvent -> EventStore (+ stream_queue 若 stream=true)
  -> SSE (/v1/chat) 或 WS (/v1/runs/{id}/ws) 或事后 GET /events

终态 -> finalize_memory -> memory_* 事件
     -> compact_checkpoint (若 runner 提供)
     -> run_completed_meta / run_failed_meta
     -> checkpoint_consistency_checked + run_consistency_checked (completed 路径)
     -> 从 Engine 内存表移除 ManagedRun
```

可选 **`idempotency_key`**：同 thread 下相同 key 且 payload hash 一致时复用已有 run（不重复启动 task）；hash 冲突返回 409。

### 5.2 Cancel（协作式）

- API：`POST /v1/runs/{id}/cancel`
- 已终态 Run：幂等返回，不重复写终态事件
- 进行中：置 `cancelling`，写 `cancel_requested`，`cancel_requested=True` 唤醒 approval 等待，并 `task.cancel()`
- LangGraph 侧通过 `GraphInterrupted` / 检查 `cancel_requested` 收敛到 `cancelled`

### 5.3 Approval（产品层 + 编排层）

```text
assistant 产出危险 tool_calls
  -> safety_gate -> interrupt(payload)
  -> GraphEventMapper 抛 GraphInterrupted
  -> Engine: waiting_approval, approval_required 事件, run_checkpoint_meta

用户 POST approve
  -> approval_resolved(approved=true)
  -> Command(resume=True), confirm_dangerous 生效, 从 checkpoint 续跑

用户 POST reject
  -> approval_resolved(approved=false)
  -> Command(resume=False), 拒绝语义, complete_run
```

**与「整图重跑」区别**：续跑依赖 checkpoint + `Command(resume)`，不重新提交全量历史 messages。

### 5.4 进程重启

Engine 启动时 `_cleanup_orphan_runs()` 扫描 EventStore 中非终态 Run：

| 重启前 Run 状态 | 行为 |
|-----------------|------|
| `waiting_approval` | **rehydrate**：恢复 `ManagedRun`（`rehydrated=true`），可从 EventStore 恢复 messages / interrupt payload，可再次 approve/reject（需重连 SSE/WS） |
| `running` / `queued` | 标记 **failed**，写 `error` + `run_failed_meta`（`reason=process_restarted`） |
| `cancelling` | 收敛为 **cancelled**，写 `cancelled` 事件 |
| 终态 | 不变 |

`runs.recovered_at` / `recovery_reason` 记录恢复轨迹；**无** `running`/`queued` durable resume。

---

## 6. 组件边界

### 6.1 EventStore（M02，写模型）

- 表：`threads`、`runs`、`events`（`runs` 含 `idempotency_key`、`recovered_at`、`recovery_reason`；`events` 含 run-local `sequence`）
- **唯一**持久化 Run 状态与事件行的模块
- 提供：创建 run（含幂等复用）、更新 status、`append_event`、分页读 events（`after_id` + `limit` + `has_more`）、`complete_run` / `fail_run`、`mark_run_recovered`、`subscribe`（供 WebSocket 推送）
- 约束：非 `active` thread 不可创建 run；同 thread **同时仅一个非终态 run**（否则 `ActiveRunExistsError`）
- 不执行图、不调用 LLM

### 6.2 ExecutionEngine（M03）

- 进程内 `ManagedRun` 表 + `asyncio.Semaphore`（`max_concurrent_runs`，默认 4）+ `run_timeout_seconds`（默认 120s）
- 启动时 `_cleanup_orphan_runs()` 恢复非终态 Run（见 §5.4）
- 连接 FastAPI handler 与 `ChatRunner`；`stream=true` 时向 `stream_queue` 写 SSE 帧
- 终态后调用 `runner.finalize_memory`、`runner.compact_checkpoint`（若存在），并写 consistency 事件

### 6.3 TimelineProjector（M04，读模型）

- 输入：raw `events` 列表 + 可选 run 行
- 输出：`timeline.items`，常见 `kind` 包括：
  - `assistant_output`（token 缓冲合并）
  - `tool` / `retrieval` / `side_effect` / `policy_decision`
  - `approval` / lifecycle（run_created、run_started、done、error、cancel_*）
  - `final_answer`（来自 `done.final_answer` 契约块）
  - `checkpoint` / `memory` / `warning`
- 汇总字段含 `side_effects`、`policy_decisions`、`tools` 等计数
- **不写** EventStore；与 [data-flow-design.md](./data-flow-design.md) 中各 `kind` 投影规则一致
- `GET /v1/runs/{id}/timeline` 当前返回 **全量** events + 投影（非增量 cursor）；checkpoint 块来自 meta 事件，不暴露 raw LangGraph state

### 6.4 Side-effects 读模型

- 模块：`runtime/side_effects.py`（`build_side_effect_read_model`）
- API：`GET /v1/runs/{id}/side-effects`
- 从 `tool_side_effect_recorded` 事件投影写工具副作用（`confirmed` / `reused` / `none` / `unknown` / `blocked`），供审计与 UI 展示；Timeline 中同步投影为 `kind: side_effect`

### 6.5 与 ChatRunner / GraphEventMapper

| 责任 | 归属 |
|------|------|
| 产生 `RuntimeEvent` 流 | GraphEventMapper |
| `_emit` → EventStore + SSE 帧 | ChatRunner |
| Run 何时算结束、是否 waiting_approval | ExecutionEngine |
| checkpoint `message_count` 快照 | Engine `_append_checkpoint_meta` + `CheckpointReader` |
| `credential_binding_audit`（scope / set cookie） | `nodes.safety_gate`、`tool_handlers` → MemoryManager.append_event |
| `policy_decision_recorded` / blocked `tool_side_effect_recorded` | `runtime/policy_audit.py`、reject 路径、`tool_handlers` |
| `tool_side_effect_recorded`（写工具确认） | `tool_handlers` + `tools/audit.py` → EventStore |

---

## 7. EventStore 与 Checkpoint 一致性

产品事实源（EventStore）与 working memory（LangGraph checkpoint）职责不同；同一次 Run 会交替写入。**完整策略表见 [agent-learning-guide.md](./agent-learning-guide.md) §2.6**；本节列 Runtime 侧已落地行为与剩余差距。

### 7.1 基本原则

```text
EventStore 记录产品事实：run_*、tool_*、approval_*、credential_binding_audit（无 secret）、memory_*。
Checkpoint 记录模型续推所需的 messages。
Timeline 只读 EventStore，不从 checkpoint 反推 Run 状态。
```

### 7.2 建议写入顺序

| 步骤 | 顺序 |
|------|------|
| Run 启动 | EventStore `run_started` → 启动 LangGraph |
| Tool | EventStore `tool_start` → Handler → `tool_end` → 更新 checkpoint |
| PolicyGate deny | `credential_binding_audit`（若有 scope 检查）→ 拦截 AIMessage，**不**执行 tool |
| Run 完成 | 确认关键事件已落库 → `run_completed_meta` / `done` |

### 7.3 最小 MVP 要求 vs 现状

| 要求 | 现状 |
|------|------|
| `tool_call_id` 幂等 | ✅ mapper 生成 call_id；EventStore 对重复 `tool_end.call_id` 幂等返回已有事件 |
| Run 内 `sequence` 单调递增 | ✅ EventStore append 时分配 run-local sequence；读取按 `sequence,id` 排序 |
| Run 完成一致性摘要 | ✅ `run_consistency_checked` + `checkpoint_consistency_checked`（v2，见 Appendix） |
| EventStore 写失败不静默继续 | ⚠️ 部分路径仍 best-effort |
| checkpoint sync 失败可观测 | ✅ `checkpoint_sync_failed`；资源释放见验证脚本 |

### 7.4 与 data-flow / guardrail 的衔接

| 主题 | 本文档 | 详见 |
|------|--------|------|
| Run FSM、cancel、approval | §4–§5 | — |
| `RuntimeEvent` 结构、`schema_version` | — | [data-flow-design.md](./data-flow-design.md) §2 |
| EventStore 行、`kind` 列表 | — | data-flow §5.4、`event_schema.py` |
| `credential_binding_audit` payload | §6.5 | data-flow §2.5、[guardrail-policy-design.md](./guardrail-policy-design.md) §8 |
| SSE 帧格式 | — | data-flow §5.5 |
| Tool 审计字段 | — | data-flow §2.2 |

新增事件种类时：先改 `event_schema` + Contract，再在本 Runtime 流程中确认 **哪一步 append**。

---

## 8. 未来优化方向

路线图见 [agent-learning-guide.md](./agent-learning-guide.md) §7 L8、[guide §2.8](./agent-learning-guide.md)。

### 8.1 八层栈改造分配

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **3** | L8 | EventStore/checkpoint 失败一致性 v1：`sequence`、`tool_call_id` 幂等、`last_successful_event_id` | ✅ `verify_runtime_domain.py --case execution_engine`、`verify_memory_checkpoint_consistency.py` |
| **3** | L8 Storage | `running`/`queued` durable resume 或外部队列 PoC | ❌ 待做 |
| **3** | L8 | Run 级幂等键（`idempotency_key` + payload hash） | ✅ `CreateRunRequest` / `ChatRequest` |
| **3** | L8 | orphan run 启动恢复 + `recovered_at` 审计 | ✅ `_cleanup_orphan_runs` + `verify_runtime_domain.py --case durability` |
| **3** | L8 | Timeline 读模型缓存 / UI 默认 cursor 分页 | ❌ 事件 API 已分页；timeline 仍全量 |

### 8.2 仍待做

- `running` / `queued` 重启后的 **durable resume**（或外部队列接管）
- Timeline **增量**读模型 / 投影缓存（events 分页已支持 `after_id`）
- `cancelling` 与 LangGraph cancel 语义对齐的专用可观测事件
- orphan 恢复策略 **配置化**（当前为 Engine 启动固定逻辑）
- EventStore 写失败路径全面 fail-fast

---

## 9. 遗留问题

| 问题 | 影响 |
|------|------|
| 仅单进程 Engine | 多实例部署需重新设计 Run 锁与事件写入 |
| `running`/`queued` 重启即 failed | 长任务易被误杀；无 durable resume |
| EventStore 写失败部分 best-effort | 极端情况下事实源与 checkpoint 可能短暂不一致 |
| rehydrate 后无 SSE/WS 自动恢复 | UI 需 polling timeline 或重连 WS |
| Timeline 每次全量投影 | 超长 Run 读延迟；events 分页与 timeline 未分离 |
| `verify_*` 多夹具库 | 与生产同库路径需隔离 |

---

## 10. 非目标

- 不定义 LangGraph 节点内部 state 字段（见 `agent/state.py`）
- 不定义 Memory 压缩策略（见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)）
- 不定义 Eval 套件（见 [eval-design.md](./eval-design.md)）
- 不定义 HTTP 路径全集（见 README §5–6）

---

## Appendix: Checkpoint Consistency v2

Checkpoint consistency v2 保持 EventStore 与 LangGraph checkpoint 的职责分离：

- **EventStore**：Thread / Run / Event / Timeline / approval / cancel / tool audit / final answer 等产品事实。
- **LangGraph checkpoint**：图 state 与 message history（working memory）。
- 两者 **不做** 同一原子事务。

Run 进入终态并完成 finalize 时，`ExecutionEngine` 读取最新 checkpoint 快照并写入 `checkpoint_consistency_checked`，对比 `run_completed_meta.message_count` 与 checkpoint，记录：

- `checkpoint_read_ok` / `checkpoint_missing` / `checkpoint_has_interrupt`
- `checkpoint_message_count_actual` / `checkpoint_message_count_reported` / `checkpoint_match`
- `warnings` / `error` / `source_event_ids`

读取失败、checkpoint 缺失或 message 计数不一致 **不会** 把已 `completed` 的 Run 改回 `failed`，仅作为可观测 warning。`run_consistency_checked` 会嵌入上述摘要字段，供 Timeline 与 debug 工具一次展示。

本地调试导出：

```powershell
python scripts/export_run_debug_bundle.py --event-store-path storage\learnagent-events.sqlite --checkpoint-path storage\langgraph-checkpoints.sqlite --run-id <run_id>
```

debug bundle 含 run/thread 行、raw events、Timeline 投影、最新 consistency 事件与 checkpoint SQLite 检查；可能含用户输入，仅用于本地排障。
