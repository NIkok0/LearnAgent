# LearnAgent Observability 设计

> 说明排障与审计时的「双轨可观测」：EventStore 产品事实源 + Langfuse LLM/工具 trace；以及 ID 关联、脱敏与后续 metrics 方向。  
> 关联文档：[agent-learning-guide.md](./agent-learning-guide.md)、[runtime-design.md](./runtime-design.md)、[data-flow-design.md](./data-flow-design.md)、[guardrail-policy-design.md](./guardrail-policy-design.md)、[tech-selection-design.md](./tech-selection-design.md) §5

**K/C/S 位置**：Kernel **M13**（模型轨 Langfuse，辅）；产品轨以 EventStore/Timeline 为主（M02–M04）。详见 [guide §3 M13](./agent-learning-guide.md)。

---

## 0. 实现状态

| 项 | 状态 | 验收脚本 |
|---|---|---|
| EventStore 产品轨（timeline / SSE） | ✅ | `verify_runtime_event_store.py`、`verify_runtime_timeline.py` |
| `retrieval_completed` ↔ tool `call_id` 关联 | ✅ | `verify_runtime_timeline.py` |
| Langfuse trace / tool span | ⚠️ 可选 | 无 CI 硬门禁 |
| `trace_id` 写入 `RuntimeEvent.correlation` | ❌ | — |
| generation span + token/cost 聚合 | ❌ | — |

全局缺口见 [agent-learning-guide §2.8](./agent-learning-guide.md)。

---

## 1. 设计动机

Agent 线上问题通常是：「用户说回答错了」——需要同时回答 **业务上发生了什么**（Run、工具、审批）和 **模型侧发生了什么**（哪轮 LLM、调了哪些 tool）。

| 诉求 | 仅日志 | 仅 Langfuse | 仅 EventStore |
|------|--------|-------------|---------------|
| 按 thread/run 复盘产品行为 | 难关联 | session 可对齐 thread，但无 Run FSM | 强 |
| 看 token 流、tool 参数审计 | 无结构 | 部分 | 强（timeline） |
| 对比多次调用的 LLM 质量 | 无 | 强 | 弱 |
| 本地单用户、无 SaaS | 够用 | 需配置密钥 | 够用 |

本设计采用 **双轨**，不互相替代：

1. **产品轨（主）**：SQLite EventStore + Timeline 投影 → 与 API/SSE/UI 一致，见 [runtime-design.md](./runtime-design.md)。
2. **模型轨（辅）**：Langfuse trace/generation/tool span → 可选，配置密钥后启用。

M13 **不负责** 业务状态机定义；**负责** 把两轨 ID 对齐、敏感字段脱敏、失败时降级不拖垮 Run。

---

## 2. 双轨架构

```text
一次 Run (run_stream)
    |
    +-- [产品轨] GraphEventMapper -> RuntimeEvent -> EventStore
    |       correlation: thread_id, run_id, tool_call_id
    |       kinds: token, tool_*, approval_*, done, error, memory_*, ...
    |       读模型: TimelineProjector -> GET /timeline, /ui
    |
    +-- [模型轨] ChatRunner.start_chat_trace (Langfuse)
            session_id = conversation_id (= thread_id)
            configurable.trace 传入图与 tool_handlers
            tool: start_tool_span / end_tool_span (search_docs, http_*)
            end_chat_trace + flush_langfuse
```

```text
排障推荐顺序:
  1. 用 run_id 拉 EventStore / timeline（产品真相）
  2. 用 thread_id 在 Langfuse 搜 session（若已配置）
  3. 用应用日志（http_get/post path、脱敏 cookie）补网络层细节
```

---

## 3. 核心组件与边界

| 组件 | 路径 | 职责 | 不负责 |
|------|------|------|--------|
| EventStore | `runtime/event_store.py` | thread/run/event 持久化、分页 | LLM trace、聚合指标 |
| TimelineProjector | `runtime/timeline.py` | 聚合 timeline、**一致性 warnings** | 写入事件 |
| `RuntimeEvent` / `CorrelationIds` | `contracts/base.py` | 预留 `trace_id` 字段 | 自动填充 Langfuse id |
| Langfuse tracer | `observability/langfuse_tracer.py` | trace、tool span、脱敏、flush | Run 状态、SSE |
| Python `logging` | 各模块 `log.info` 等 | 开发/运维日志 | 结构化关联 ID（未统一） |
| `sanitize_observability_payload` | `langfuse_tracer.py` | Langfuse 入参/出参脱敏 | EventStore payload（走 `tools/sanitize`） |

---

## 4. 产品轨：EventStore 与 Timeline

### 4.1 事实源原则

- 凡 **产品语义**（Run 状态、审批、取消、工具审计、**PolicyGate credential 绑定审计**（`credential_binding_audit`）、memory 摘要）以 EventStore 为准。
- Langfuse 缺失或失败 **不影响** Run 完成；`langfuse_tracer` 内 `_safe_call` 吞异常并打日志。

### 4.2 关联 ID（当前）

| ID | 来源 | 落库 |
|----|------|------|
| `thread_id` | API / `conversation_id` | events.thread_id、runs.thread_id |
| `run_id` | `create_run` | events.run_id、runs.id |
| `tool_call_id` | LangGraph tool call `id` | `tool_start`/`tool_end` payload 的 `call_id` |
| `trace_id` | `CorrelationIds` 已定义 | **通常未写入**（见 §6） |

### 4.3 Timeline 作为轻量「一致性观测」

`TimelineProjector` 在投影时附加 `warnings`，例如：

| code | 含义 |
|------|------|
| `completed_without_done` | Run 已完成但无 `done` 事件 |
| `failed_without_error_event` | failed 但无 `error` |
| `cancel_requested_not_cancelled` | 已请求取消但未终态 |
| `tool_missing_call_id` | tool 事件缺少 call_id |

用于 UI/API 发现 **事件流与 Run 行不一致**，不是 metrics 系统。

工具审计字段形状见 [data-flow-design.md](./data-flow-design.md)（`tool_start`/`tool_end`、`sanitized_*`）。

---

## 5. 模型轨：Langfuse

### 5.1 启用条件

`settings.langfuse_configured` 为真需同时满足：

- `langfuse_enabled=true`
- `langfuse_public_key`、`langfuse_secret_key` 非空
- 可选 `langfuse_host`（默认 `https://cloud.langfuse.com`）

未配置时所有 `start_*` 返回 `None`，Run 行为不变。

### 5.2 生命周期（与 Run 对齐）

| 阶段 | 调用 | 说明 |
|------|------|------|
| `run_stream` 开始 | `start_chat_trace` | `name=wm_chat_turn`，`session_id=conversation_id` |
| 传入图 | `configurable["trace"]` | `tool_handlers` 内取 trace 打 tool span |
| 工具执行 | `start_tool_span` / `end_tool_span` | `name=tool:{search_docs\|http_get\|http_post}`，args/result 经 `sanitize_observability_payload` |
| 正常/异常结束 | `end_chat_trace` | 写入 assistant_preview 或 error |
| `finally` | `flush_langfuse` | 进程退出时 `server` lifespan 也会 flush |

### 5.3 脱敏（模型轨）

`sanitize_observability_payload` 与 Guardrail 共用敏感键集合（cookie、password、authorization 等）；字符串超长截断（默认 1500 字符）。  
与 [guardrail-policy-design.md](./guardrail-policy-design.md) §8 的 EventStore 脱敏路径**并行**，不自动同步规则列表。

### 5.4 已导出未接线

`start_generation_span` / `end_generation_span` 已在 `observability/__init__.py` 导出，**当前图路径未调用**（无 per-LLM-round generation  span）。后续可在 `assistant` 节点或 `GraphEventMapper` 的 `on_chat_model_end` 接入。

---

## 6. ID 关联（目标 vs 现状）

### 6.1 目标关联图

```text
thread_id (= conversation_id)
    ├── run_id ────────────── EventStore runs + events
    ├── Langfuse session_id ─ 同 thread_id
    └── trace_id (Langfuse trace id) ─ 应写入 EventStore 事件 meta，便于互跳

run_id
    └── tool_call_id ──────── tool_* 事件、timeline.tools[]

Langfuse trace
    ├── generation (规划)
    └── span: tool:*
```

### 6.2 现状

| 关联 | 状态 |
|------|------|
| `thread_id` ↔ Langfuse `session_id` | **已实现**（`start_chat_trace`） |
| `run_id` ↔ Langfuse trace | **弱**：trace metadata 未标准写入 `run_id` |
| Langfuse `trace.id` ↔ EventStore `trace_id` | **未实现**：`RuntimeEvent.correlation.trace_id` 多为空 |
| `tool_call_id` ↔ Langfuse tool span | **部分**：span 按工具名，未存 LangGraph call id |

因此排障时常 **人工** 用时间戳 + thread_id 对齐两轨，无法从 timeline 一键打开 Langfuse。

---

## 7. 配置项

| 变量 / Settings | 默认 | 作用 |
|----------------|------|------|
| `LANGFUSE_ENABLED` / `langfuse_enabled` | `true` | 总开关（无 key 仍不连） |
| `LANGFUSE_PUBLIC_KEY` | `""` | 公钥 |
| `LANGFUSE_SECRET_KEY` | `""` | 私钥 |
| `LANGFUSE_HOST` | Langfuse Cloud | 自建可改 host |
| `AGENT_EVENT_STORE_PATH` | `storage/learnagent-events.sqlite` | 产品轨存储 |

---

## 8. 文档关系

- **上游**：[runtime-design.md](./runtime-design.md)（EventStore/Timeline）、[data-flow-design.md](./data-flow-design.md)（`RuntimeEvent` 字段）
- **下游**：Langfuse 配置、结构化日志规范（待落地）
- **全量索引**：[agent-learning-guide §6](./agent-learning-guide.md)

---

## 9. 未来优化方向

### 9.1 关联 ID 打通（高优先级）

- `start_chat_trace` 返回的 trace id 写入 `configurable`，`GraphEventMapper` 构造 `RuntimeEvent` 时填 `correlation.trace_id`。
- 可选：首条 `run_started` 或 `meta` 事件带 `langfuse_trace_url` 片段，供 UI 跳转。
- Langfuse trace `metadata` 固定带 `run_id`、`model`。

### 9.2 LLM 可观测补全

- 在 `on_chat_model_stream` / `on_chat_model_end` 调用 `start_generation_span` / `end_generation_span`。
- 记录 `finish_reason`、`tool_names`、token 用量（若 API 返回 usage）。

### 9.3 Metrics 与标准 trace

- 进程内计数：run 完成率、tool 失败率、approval 次数、run 耗时直方图（Prometheus `/metrics` 或写入 `run_completed_meta`）。
- 生产阶段评估 OpenTelemetry：EventStore 写 span、与 Langfuse 二选一或双写。

### 9.4 日志规范

- 结构化日志（JSON）：固定字段 `thread_id`、`run_id`、`tool_call_id`。
- 与 EventStore 事件 id 对齐，避免仅 `log.info("http_get path=...")` 孤立存在。

### 9.5 评测与告警

- Timeline `warnings` 非空率进 eval 抽样或夜跑统计。
- Langfuse 配置缺失时启动日志明确 `Langfuse configured=false`（`server` 已有 info）。

### 9.6 八层栈改造分配

路线图见 [agent-learning-guide.md](./agent-learning-guide.md) §7 第 3–4 波（L8 Storage/Audit）。

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **3** | L8 | `trace_id` 写入 `RuntimeEvent.correlation`；UI 可选 Langfuse 链接 | `verify_runtime_timeline.py` |
| **3** | L8 | `start_generation_span` / token usage → `run_completed_meta` | Langfuse + EventStore 对照 |
| **3** | L8 | 结构化 JSON 日志（`thread_id`/`run_id`/`tool_call_id`） | 日志规范 doc |
| **3** | L8 | 失败 run 导出 timeline JSON（与 eval §7.4 联动） | `scripts/export_run_timeline.py` |
| **3** | L8 | Timeline `warnings` 非空率进夜跑统计 | eval profile |
| **4** | L8 | Prometheus `/metrics` 或 OTel 双写 | observability PoC |

---

## 10. 遗留问题

| 问题 | 影响 | 说明 |
|------|------|------|
| 双轨无统一 trace_id | 排障要在 EventStore 与 Langfuse 间手工对齐 | §6.2 |
| 无 token/cost 聚合 | 无法按 Run 看账单 | usage 未进 EventStore |
| generation span 未用 | Langfuse 中 LLM 层次不完整 | 函数已写未调用 |
| 日志无统一关联字段 | grep 困难 | 分散在各模块 |
| Langfuse 强依赖外网/SaaS | 离线环境仅产品轨 | 符合本地 MVP 目标 |
| 无 OpenTelemetry | 难接 Grafana/Tempo | 规划中 |
| Timeline warnings 未进 CI 阈值 | 一致性问题可能漏检 | 仅投影时计算 |

---

## 11. 非目标

- 不替代 EventStore 作为产品审计事实源
- 不在本文档定义 Dashboard 具体 UI（`/ui` 见 README）
- 不把 Langfuse 作为 PR 硬门禁（eval 以 deterministic 为主，见 [eval-design.md](./eval-design.md)）
- 不涵盖 RAG 检索质量指标（见 eval-design / `phase4_ragas`）
- 不定义 Sentry/错误上报（未接入）
