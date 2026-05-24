# LearnAgent Runtime 设计

> 说明产品级 Runtime 语义：Thread/Run 生命周期、状态机、ExecutionEngine 与 EventStore/Timeline 的责权分界。  
> 事件与 payload 形状见 [data-flow-design.md](./data-flow-design.md)；项目模块地图见 [agent-learning-guide.md](./agent-learning-guide.md)。

**K/C/S 位置**：Kernel **M02–M04**（产品层 Run/Event 事实源）；与 LangGraph checkpoint 分界见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)。详见 [guide §2.1·§3](./agent-learning-guide.md)。

---

## 0. 实现状态

| 项 | 状态 | 验收脚本 |
|---|---|---|
| EventStore append / 分页 | ✅ | `verify_runtime_event_store.py` |
| Timeline 投影 + SSE 同源 | ✅ | `verify_runtime_timeline.py` |
| Checkpoint ↔ Run 关联 | ✅ | `verify_runtime_checkpoint_link.py` |
| ExecutionEngine cancel / approve / 并发槽 | ✅ | `verify_runtime_execution_engine.py` |
| Session MVP（REST + SSE 冒烟） | ✅ | `verify_session_mvp.py` |
| Golden Run 事件契约 | ✅ | `verify_golden_scenarios.py` |
| 失败一致性 v1（sequence / tool_end 幂等 / failure meta） | ✅ | `verify_runtime_execution_engine.py`、`verify_memory_checkpoint_consistency.py` |
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
| `archived` | 冷归档；可清理 checkpoint | `ThreadLifecycle` 对 idle 超时的 ended thread |

```text
active --> ended --> archived
              |
              +--> archive 时 CheckpointStore.purge_thread(thread_id)
```

- `conversation_id`（旧 `/v1/chat`）与 `thread_id` 等价。
- Thread 状态存在 `threads` 表；与 Run 状态 **独立**（一个 thread 可有多个 run）。

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
| 创建 Run | ExecutionEngine | `queued` + `run_created` |
| 开始执行 | ExecutionEngine | `running` + `run_started` |
| 危险工具 interrupt | Graph → Engine | `waiting_approval` + `approval_required` + checkpoint meta |
| approve | ExecutionEngine | `running` + `approval_resolved` + `run_started{resumed}` |
| reject | ExecutionEngine | `Command(resume=False)` 后 `completed` |
| PolicyGate scope deny | Graph → safety_gate | 无 tool 执行；可选 `credential_binding_audit(scope_denied)` |
| 正常结束 | ExecutionEngine | `completed` + `done` / `run_completed_meta` |
| cancel | ExecutionEngine | `cancelling` → `cancelled` + `cancel_requested` |
| 超时/异常 | ExecutionEngine | `failed` + `error` |

---

## 5. 核心流程

### 5.1 后台 Run + SSE

```text
POST /v1/threads/{id}/runs  (stream=true)
  -> ExecutionEngine.create_run (queued)
  -> asyncio.Task -> ChatRunner.run_stream
  -> RuntimeEvent -> EventStore + stream_queue
  -> GET/订阅 SSE 或 /v1/runs/{id}/ws

终态 -> finalize_memory -> memory_* 事件
     -> compact_checkpoint (若 runner 提供)
     -> 从 Engine 内存表移除 ManagedRun
```

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

| 重启前 Run 状态 | 行为 |
|-----------------|------|
| `waiting_approval` | 可 **rehydrate**：Engine 从 EventStore 恢复 ManagedRun，可再次 approve/reject（SSE 需重连） |
| `running` / `queued` | 标记 **failed**（无 durable 续跑） |
| 终态 | 不变 |

---

## 6. 组件边界

### 6.1 EventStore（M02，写模型）

- 表：`threads`、`runs`、`events`
- **唯一**持久化 Run 状态与事件行的模块
- 提供：创建 run、更新 status、append_event、分页读 events、`complete_run` / `fail_run`
- 不执行图、不调用 LLM

### 6.2 ExecutionEngine（M03）

- 进程内 `ManagedRun` 表 + `asyncio.Semaphore`（`max_concurrent_runs`）
- `run_timeout_seconds` 包裹单次 `run_stream`
- 连接 FastAPI handler 与 `ChatRunner`
- 终态后调用 `runner.finalize_memory`、`runner.compact_checkpoint`（若存在）

### 6.3 TimelineProjector（M04，读模型）

- 输入：raw `events` 列表 + 可选 run 行
- 输出：`timeline.items`（token、tool、retrieval、approval、warnings、checkpoint 块等）
- **不写** EventStore；与 [data-flow-design.md](./data-flow-design.md) 中各 `kind` 投影规则一致
- `GET /v1/runs/{id}/timeline` 的 `checkpoint` 块来自 `run_checkpoint_meta` / `run_completed_meta`，不暴露 raw LangGraph state

### 6.4 与 ChatRunner / GraphEventMapper

| 责任 | 归属 |
|------|------|
| 产生 `RuntimeEvent` 流 | GraphEventMapper |
| `_emit` → EventStore + SSE 帧 | ChatRunner |
| Run 何时算结束、是否 waiting_approval | ExecutionEngine |
| checkpoint `message_count` 快照 | Engine `_append_checkpoint_meta` + CheckpointReader |
| `credential_binding_audit`（scope / set cookie） | `nodes.safety_gate`、`tool_handlers` → MemoryManager.append_event |

---

## 7. EventStore 与 Checkpoint 失败一致性（目标态）

产品事实源（EventStore）与 working memory（LangGraph checkpoint）职责不同；同一次 Run 会交替写入，因此需定义失败语义。**完整策略表见 [agent-learning-guide.md](./agent-learning-guide.md) §2.6**；本节只列 Runtime 侧要点与当前差距。

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
| `run_failed` + `last_successful_event_id` | ✅ `run_failed_meta` / `run_consistency_checked` 已落地 |
| EventStore 写失败不静默继续 | 部分路径仍 best-effort |
| checkpoint sync 失败可观测 | ✅ `checkpoint_sync_failed` 已落地；checkpoint 资源释放由验证脚本覆盖 |

### 7.4 与 data-flow / guardrail 的衔接

| 主题 | 本文档 | 详见 |
|------|--------|------|
| Run FSM、cancel、approval | §4–§5 | — |
| `RuntimeEvent` 结构、`schema_version` | — | [data-flow-design.md](./data-flow-design.md) §2 |
| EventStore 行、`kind` 列表 | — | data-flow §5.4、`event_schema.py` |
| `credential_binding_audit` payload | §6.4 | data-flow §2.5、[guardrail-policy-design.md](./guardrail-policy-design.md) §8 |
| SSE 帧格式 | — | data-flow §5.5 |
| Tool 审计字段 | — | data-flow §2.2 |

新增事件种类时：先改 `event_schema` + Contract，再在本 Runtime 流程中确认 **哪一步 append**。

---

## 8. 未来优化方向

路线图见 [agent-learning-guide.md](./agent-learning-guide.md) §7 L8、[guide §2.8](./agent-learning-guide.md)。

### 8.1 八层栈改造分配

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **3** | L8 | EventStore/checkpoint 失败一致性 v1：`sequence`、`tool_call_id` 幂等、`last_successful_event_id` | ✅ `verify_runtime_execution_engine.py`、`verify_memory_checkpoint_consistency.py` |
| **3** | L8 Storage | `running`/`queued` durable resume 或外部队列 PoC | `verify_runtime_execution_engine.py` 扩展 |
| **3** | L8 | Run 级幂等键 `client_run_id` | API + event_store |
| **3** | L8 | orphan run 清理策略配置化 + 审计事件 | eval golden |
| **3** | L8 | Timeline 读模型缓存 / UI 默认 cursor 分页 | `verify_runtime_timeline.py` |

### 8.2 技术项（原 §8 列表）

- `running` / `queued` 重启后的 durable resume（或外部队列接管）
- Run 级幂等键（`client_run_id`）与重复创建防护
- Timeline 读模型缓存与分页游标在 UI 侧默认使用
- `cancelling` 与 LangGraph cancel 语义对齐的可观测事件
- 将 orphan run 清理策略配置化并写入 EventStore 审计事件

---

## 9. 遗留问题

| 问题 | 影响 |
|------|------|
| 仅单进程 Engine | 多实例部署需重新设计 Run 锁与事件写入 |
| `running` 重启即 failed | 长任务易被误杀 |
| EventStore ↔ checkpoint 失败语义未闭环 | 见 guide §2.6、本文 §7.3 |
| rehydrate 后无 SSE 自动恢复 | UI 需 polling timeline |
| Timeline 每次全量投影 | 超长 Run 读延迟 |
| `verify_*` 多夹具库 | 与生产同库路径需隔离 |

---

## 10. 非目标

- 不定义 LangGraph 节点内部 state 字段（见 `agent/state.py`）
- 不定义 Memory 压缩策略（见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)）

---

## Appendix: Checkpoint Consistency v2

Checkpoint consistency v2 keeps the existing split of responsibilities:

- EventStore is the product fact source for Thread, Run, Event, Timeline, approval, cancel, tool audit, and final answer.
- LangGraph checkpoint is the working-memory fact source for graph state and message history.
- The two stores are not treated as one atomic transaction.

When a completed run reaches terminal finalization, `ExecutionEngine` writes `checkpoint_consistency_checked` after reading the latest LangGraph checkpoint snapshot. The event compares `run_completed_meta.message_count` with the checkpoint snapshot and records:

- `checkpoint_read_ok`
- `checkpoint_missing`
- `checkpoint_has_interrupt`
- `checkpoint_message_count_actual`
- `checkpoint_message_count_reported`
- `checkpoint_match`
- `warnings`
- `error`
- `source_event_ids`

Checkpoint read failure, missing checkpoint, or count mismatch does not change a completed run into failed. It is an observability warning only. The existing `run_consistency_checked` event now includes the checkpoint v2 summary fields so Timeline and debug tooling can show one compact consistency view.

Local debug export:

```powershell
E:\Conda\envs\learnagent312\python.exe scripts\export_run_debug_bundle.py --event-store-path storage\learnagent-events.sqlite --checkpoint-path storage\langgraph-checkpoints.sqlite --run-id <run_id>
```

The debug bundle includes run/thread rows, raw events, Timeline projection, latest run consistency, latest checkpoint consistency, and raw checkpoint SQLite inspection. It is a local troubleshooting artifact and may contain user input or event payloads.
- 不定义 Eval 套件（见 [eval-design.md](./eval-design.md)）
- 不定义 HTTP 路径全集（见 README §5–6）
