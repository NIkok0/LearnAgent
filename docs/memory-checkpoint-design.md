# LearnAgent Memory 与 Checkpoint 设计

> 说明 Working Memory 与 LangGraph Checkpoint 如何分工、输入语义如何收敛，以及压缩与一致性校验的设计边界。  
> 关联文档：[agent-learning-guide.md](./agent-learning-guide.md)、[runtime-design.md](./runtime-design.md)、[data-flow-design.md](./data-flow-design.md)、[tech-selection-design.md](./tech-selection-design.md)、[eval-design.md](./eval-design.md)

**K/C/S 位置**：Kernel **M09 Memory** + LangGraph checkpoint；Context 装配（M15）→ [context-manager-design.md](./context-manager-design.md)。

---

## 0. 实现状态总览（学习入口）

| 能力 | 状态 | 代码 / 数据锚点 |
|------|------|-----------------|
| Working memory（checkpoint `messages`） | ✅ | §2–§4 |
| Episodic 跨 Run 摘要（EventStore） | ✅ | `memory_run_summary` / `memory_thread_summary` |
| **结构化长期记忆库**（`memory_items`） | ✅ Phase 1 | `memory/item_store.py`，`item_writer.py` |
| 写入去重 + 冲突覆盖 + 版本历史 | ✅ | `MemoryItemWriter.upsert_candidate` |
| TTL 过期 + 容量淘汰（importance 保护） | ✅ | `delete_expired`，`evict_lowest_score` |
| 混合召回（keyword + 时间衰减 + importance） | ✅ | `recall_long_term_items` |
| scope：`user` / `session`（`threads.user_id`） | ✅ | `EventStore.get_user_id`，默认 `user_id=thread_id` |
| 续轮去重 `DEFAULT_KERNEL_PROMPT` / 记忆 inject | ✅ | `context/assemble.py` / `context/memory_inject.py` |
| 规则偏好抽取（短→长，无 LLM） | ✅ | `extract_memory_candidates` |
| **向量 episodic 召回 + HyDE** | ✅ Phase 2 | `memory/embedding.py`，`hyde.py`，`long_term_use_vector` |
| **LLM 记忆提取器 + pending 标签** | ✅ Phase 2 | `llm_extractor.py`；`pending_confirmation`；`confirm_memory_item` |
| **`checkpoint_compacted` 写入 EventStore** | ✅ Phase 2 | `runner.compact_checkpoint` → `checkpoint_compacted` 事件 |
| Context Manager 单入口 | ✅ | 见 [context-manager-design §0](./context-manager-design.md) |
| GDPR 删除 API / 真实鉴权 user 绑定 | ❌ | 见 §8.5 |
| Memory 回归 | ✅ | `verify_memory_checkpoint_consistency.py`、`verify_memory_production_v1.py`、`verify_memory_production_v2.py` |

套件见 [ci-design.md](./ci-design.md)。

---

## 1. 设计动机

多轮 Agent 场景里，若同时把「客户端 `messages[]` 全量历史」和「LangGraph checkpoint」当作对话历史，会出现：

| 问题 | 后果 |
|---|---|
| 双写历史 | 同一轮 user 内容在 HTTP body 与 checkpoint 各存一份，resume/审批后易错位 |
| EventStore 与 checkpoint 计数不一致 | Timeline `run_completed_meta.message_count` 与真实 checkpoint 对不上 |
| checkpoint 无限增长 | 长线程 token 成本上升，且无统一压缩策略 |
| episodic 与 working 边界模糊 | 摘要注入与原始消息混在一起，难以断言预算 |

本设计确立两条原则：

1. **Working Memory 的事实来源是 LangGraph checkpoint 中的 `messages`**
2. **EventStore 是审计与 episodic 召回的事实来源，不反向重建 working 全量历史**

客户端 `messages[]` 在兼容期内仍可携带历史，但服务端只把**当前轮 user** 当作图输入增量；历史以 checkpoint 为准。

---

## 2. 记忆分层

```text
┌─────────────────────────────────────────────────────────────┐
│  Working（短期）                                             │
│  LangGraph checkpoint.state["messages"]                      │
│  多轮对话、tool 往返、interrupt 状态                          │
└─────────────────────────────────────────────────────────────┘
         ▲ 每轮仅追加 current turn + 可选 episodic inject
         │ 超阈值 → CheckpointCompactor（deterministic 摘要）
┌─────────────────────────────────────────────────────────────┐
│  Episodic（跨 Run 摘要）                                      │
│  EventStore: memory_run_summary / memory_thread_summary      │
│  经 MemoryPolicy 召回 → SystemMessage 注入（非 checkpoint 本体）│
└─────────────────────────────────────────────────────────────┘
         ▲ Run 结束后 summarize_run / update_thread_summary
┌─────────────────────────────────────────────────────────────┐
│  Semantic（RAG）                                             │
│  RagStore.search → search_docs 工具结果                       │
└─────────────────────────────────────────────────────────────┘
         ▲ search_docs 工具
┌─────────────────────────────────────────────────────────────┐
│  Long-term（结构化）                                         │
│  SQLite memory_items（user/session scope）                   │
│  Run 结束规则抽取 → 混合召回 → [LongTermMemory] inject        │
└─────────────────────────────────────────────────────────────┘
```

| 层 | 存储 | 注入方式 |
|---|---|---|
| Working | SQLite checkpoint（`agent_checkpoint_path`） | `graph_input.messages` 经 `add_messages` 合并 |
| Episodic | EventStore 事件 payload | `memory_context_messages()` → 单条 `SystemMessage` |
| Semantic | 向量/关键词索引 | 工具调用，不进 checkpoint unless 作为 ToolMessage |
| Long-term | SQLite `memory_items` | Run 结束写入；召回后 `[LongTermMemory]` inject |

---

## 3. 数据流（单轮 Run）

```mermaid
flowchart LR
  client[Client messages array] --> trim[current_turn_messages]
  trim --> runner[ChatRunner.run_stream]
  episodic[MemoryManager episodic recall] --> inject[memory_context_messages]
  checkpoint[(LangGraph Checkpoint)] --> graph[Agent Graph add_messages]
  trim --> graph
  inject --> graph
  graph --> events[EventStore + SSE]
  events --> meta[run_completed_meta.message_count]
  graph --> compact[CheckpointCompactor after run]
  idle[ThreadLifecycle idle TTL] --> compact
```

**非 resume 路径**（`ChatRunner`）：

- `current_turn_messages(messages)`：从客户端列表中只取**最后一条 user**
- `build_context(..., messages=turn_messages)`：`working.current_turn_messages` 与兼容字段 `working.messages` 均为当前轮
- `graph_input`：`SystemMessage(DEFAULT_KERNEL_PROMPT 或 Scenario prompt)` + episodic inject + `_to_lc_messages(turn_messages)`
- LangGraph 将上述列表与 checkpoint 已有 `messages` **追加合并**（`AgentState.messages` + `add_messages`）

**resume 路径**：

- `graph_input = Command(resume=...)`，不重新拼装历史；interrupt / approval 状态由 checkpoint 承载

**Run 结束后**：

- `ExecutionEngine` 在终态时调用 `runner.compact_checkpoint(thread_id)`（若存在）
- `ThreadLifecycle` 对 idle 超时的 active thread 周期性调用同一压缩入口

---

## 4.5 结构化长期记忆（Phase 1 生产化）

八股中的「长期记忆管线（编码→存储→检索→注入）」在本项目 Phase 1 落地为 **规则抽取 + SQLite + 混合打分**，不依赖 LLM 提取器（Phase 2）。

### 存储（`memory_items` 表）

与 EventStore 同库（`agent_event_store_path`），字段含：`user_id`、`thread_id`、`scope`（`user|session`）、`memory_type`（`fact|preference|behavior|task_summary`）、`importance`、`confidence`、`version`、`supersedes_id`、`is_deprecated`、`expires_at`、`history_json`。

### 写入（Run 结束后）

`summarize_run` → `MemoryItemWriter.persist_run_memories`：

1. 规则抽取候选（goal→`task_summary`；token 输出→`fact`；「不喜欢/偏好」→`preference`）
2. `importance < memory_long_term_importance_min` 丢弃
3. 相同 `content_hash` → dedup skip
4. 相似度 ≥ dedup 阈值且 ≥ conflict 阈值 → **覆盖**旧条（deprecated + version+1 + history）
5. TTL 到期删除 + 超 `memory_long_term_max_items_per_user` 淘汰低分条（`importance ≥ protected` 不参与）

### 检索（每轮 `build_context`）

`recall_long_term_items`：`final_score = w_kw·keyword + w_time·time_decay + w_imp·importance`，过滤 `is_deprecated` / 过期 / 低于 `memory_long_term_recall_min_score`。

User scope 记忆跨 thread 可见（同 `threads.user_id`）；session scope 仅本 thread。

### 注入位置

`[EpisodicMemory]` 与 `[LongTermMemory]` 合并进单条 inject SystemMessage，位于 kernel/scenario prompt 之后、当前 user 之前（避免 Lost in the Middle）。续轮可通过 `memory_inject_dedupe_*` 跳过重复 System 消息。

验收：`python scripts/verify_memory_production_v1.py`（已纳入 `--profile core`）。

### Phase 2 增强（2026-05）

| 能力 | 实现 |
|------|------|
| **向量召回** | `memory/embedding.py`；写入时存 `embedding_json`；召回：`keyword(原 query) + vector(HyDE query)` 混合打分 |
| **HyDE** | `memory/hyde.py`；`memory_hyde_mode=rule\|llm`（无 key 时 rule fallback） |
| **LLM 提取器** | `memory/llm_extractor.py`；Run 结束 JSON 抽取；`confidence < memory_llm_confirm_threshold` → `pending_confirmation=true` |
| **pending 不注入** | `list_active(include_pending=False)`；`MemoryManager.confirm_memory_item()` 确认 |
| **checkpoint_compacted** | Run 终态压缩后写 EventStore；Timeline `kind: memory` |

验收：`python scripts/verify_memory_production_v2.py`（`--profile core`）。

---

## 4. Checkpoint 压缩

### 4.1 组件

`CheckpointCompactor`（`copilot_agent/memory/checkpoint_compactor.py`）：

- 读取 `aget_state` 的 `messages` 列表
- 超过 `checkpoint_compact_message_threshold` 时，将**较早消息**合并为一条 deterministic `SystemMessage`（前缀 `[CheckpointCompaction]`）
- 保留最近 `checkpoint_compact_keep_recent_turns` 个 user 轮及其后的 assistant/tool 消息
- 通过 `RemoveMessage` + `aupdate_state` 回写，不新建 thread

### 4.2 安全约束

| 条件 | 行为 |
|---|---|
| `checkpoint_compact_enabled=false` | 跳过 |
| `state.next` 非空（interrupt / `waiting_approval`） | `reason=has_interrupt`，不压缩 |
| 消息数 ≤ threshold | `reason=below_threshold` |

压缩摘要为**规则拼接**（角色 + 截断内容），非 LLM 生成，便于 eval 断言与回归稳定。

### 4.3 与 Episodic 的关系

- Episodic 摘要写在 EventStore，按 goal / 预算召回后**每轮注入**为独立 `SystemMessage`
- Checkpoint 压缩只处理 **checkpoint 内** 的 `BaseMessage` 列表，不删除 EventStore 中的 `memory_*` 事件
- 二者互补：checkpoint 控 token 上限；episodic 控跨 Run 可检索摘要

---

## 4.6 Context Manager 与 Memory 边界（M15）

分工表、装配流水线与配置见 **[context-manager-design.md §5](./context-manager-design.md)**。本节只强调：**checkpoint 仍是 working memory 真相源**；MemoryManager 负责存储与召回，Context Manager 负责每轮 **组装图输入**。

---

## 5. 策略配置（`MemoryPolicyConfig`）

| 字段 / Settings | 默认 | 含义 |
|---|---|---|
| `memory_checkpoint_compact_enabled` | `true` | 是否启用压缩 |
| `memory_checkpoint_compact_message_threshold` | `40` | 触发压缩的消息条数 |
| `memory_checkpoint_compact_keep_recent_turns` | `6` | 保留最近 user 轮数 |
| `memory_checkpoint_compact_summary_max_chars` | `2000` | 压缩摘要最大字符 |

Episodic 相关（`enabled`、`episodic_recall_top_k`、`thread_summary_max_*`、`conflict_jaccard_threshold` 等）见 `copilot_agent/memory/policy.py`，与 checkpoint 压缩正交。

---

## 6. 一致性契约

产品与评测关心的不变量：

| 不变量 | 说明 |
|---|---|
| 当前轮输入 | `current_turn_messages` 长度为 1，content 为最后 user |
| 计数对齐 | `CheckpointReader.snapshot().message_count` ≈ `run_completed_meta.message_count`（同 thread 最近一次 completed run） |
| 压缩可观测 | 压缩后 `message_count` 下降，且 `has_interrupt` 时永不压缩 |
| Resume 安全 | interrupt 线程压缩返回 `has_interrupt`；approve/reject 后继续图不依赖客户端全量历史 |

回归入口：`verify_memory_checkpoint_consistency.py`（套件名 `memory_checkpoint_consistency`，见 [ci-design §3](./ci-design.md)）。

---

## 7. 模块边界

| 模块 | 职责 |
|---|---|
| `agent/message_utils.py` | `current_turn_messages`、`last_user_content` |
| `agent/runner.py` | 图输入组装、`compact_checkpoint`、`finalize_memory` |
| `memory/manager.py` | `build_context`、`summarize_run`、EventStore 事件写入 |
| `memory/checkpoint_compactor.py` | checkpoint 压缩实现 |
| `memory/policy.py` | 预算、召回、压缩阈值 |
| `runtime/checkpoint_reader.py` | 只读 snapshot（`message_count`、`has_interrupt`） |
| `runtime/execution_engine.py` | Run 终态触发压缩 |
| `runtime/thread_lifecycle.py` | idle 线程周期压缩 |
| `agent/stream/event_mapper.py` | 流式事件 + `run_completed_meta` 附带 checkpoint 计数 |

---

## 8. 未来优化方向

### 8.1 输入与图状态

- **首轮 vs 续轮分支**：续轮不再向 `graph_input` 重复注入 kernel/scenario prompt 与 episodic `SystemMessage`，改为仅在 checkpoint 为空或策略版本变更时注入，避免 `add_messages` 累积重复 System 消息
- **HTTP API 契约**：文档化「`messages` 仅传当前 user」；对仍传全量历史的客户端打 deprecation 日志或校验警告
- **CheckpointReader 增强**：暴露 `last_user_turn_index`、是否含 `[CheckpointCompaction]`，供 Timeline / UI 展示

### 8.2 压缩质量

- 可选 **LLM 摘要压缩**（夜跑 / feature flag），与 deterministic 路径 A/B 对比
- ~~压缩事件写入 EventStore（`checkpoint_compacted`），Timeline 可展示「何时压缩、压缩前后条数」~~ ✅ Phase 2
- 按 token 数而非 message 条数触发阈值，更贴近模型上下文窗口

### 8.3 Episodic 与 Working 协同

- 压缩时把被折叠轮次的 hash 记入 episodic，防止「checkpoint 丢了细节但 episodic 未补」
- 失败 / cancelled Run 的摘要策略与 `include_failed_runs` 在长线程下的交互测试扩面
- 与 [data-flow-design.md](./data-flow-design.md) 中 `retrieval_completed` 联动：RAG 片段是否应进入 checkpoint 或仅保留在 ToolMessage

### 8.4 运维与评测

- 压缩失败告警（`compact_result.reason` 分布）
- 在 golden scenario 中增加「多轮 + 超阈值 + resume」组合 case
- 提供 `scripts/export_checkpoint_messages.py` 便于与失败 run 的 timeline 对照

### 8.5 八层栈改造分配（待办）

Wave1–2 已完成项见 **§0**。路线图索引：[agent-learning-guide §7](./agent-learning-guide.md)。

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **2** | L5 Agent State | 续轮 inject 去重收尾 | `verify_memory_checkpoint_consistency.py` 扩展 |
| **2** | L5 | 压缩时 hash 写入 episodic，防 checkpoint 丢细节 | 新 golden case |
| **2** | L2 | episodic run summary 可选向量索引 | `verify_memory_production_v2.py` 扩展 |
| **3** | L8 | `export_checkpoint_messages.py` + 失败 run timeline 导出联动 | eval + observability |
| **4** | L3/L4 | 服务端 `user_id` 鉴权；Memory GDPR 删除 API | 新 verify + guardrail 联动 |
| **4** | L8 | 跨 thread checkpoint 迁移工具 | 运维脚本 |

---

## 9. 遗留问题

| 问题 | 影响 | 说明 |
|---|---|---|
| 每轮仍注入 kernel/scenario prompt + episodic | checkpoint 内可能堆积多条内容相近的 `SystemMessage` | 与「checkpoint 为唯一历史」目标部分冲突；长线程依赖 Compactor 兜底 |
| 客户端仍可传全量 `messages[]` | 易被误用为「仍以 HTTP 携历史」 | 服务端已截断为 current turn，但 API 语义未在 OpenAPI 中强制 |
| 压缩摘要非语义级 | 旧细节可能丢失，回复质量下降 | 当前为 deterministic 截断；无 LLM 回填 |
| `run_completed_meta` 与 snapshot 对齐为 best-effort | 极端并发或压缩竞态下可能短暂不一致 | verify 使用顺序化夹具；生产多 worker 未全覆盖 |
| Episodic inject 与 Compaction 前缀无去重 | 模型可能同时看到 `[EpisodicMemory]` 与 `[CheckpointCompaction]` | 需 prompt / 节点层约定优先级 |
| idle 压缩依赖后台任务 | 高负载时 idle TTL 内 checkpoint 仍可能过大 | `thread_lifecycle` 周期与 TTL 需按环境调参 |
| Memory 预览 API 仍暴露 `messages` 兼容字段 | 前端若展示 `working.messages` 可能误导 | 应逐步只读 `current_turn_messages` |
| 无跨 thread 迁移工具 | checkpoint 路径变更或损坏时恢复困难 | 仅依赖 SQLite 文件路径配置 |

---

## 10. 非目标

- 不用 EventStore 事件流重放生成 LangGraph `messages`（审计只读，不作 working 重建源）
- 不在 checkpoint 内存储未脱敏的 secrets（工具审计仍走 `ToolResultModel.sanitized_*`）
- 不把 RAG 全库嵌入 checkpoint（语义检索仍经 `search_docs`）
