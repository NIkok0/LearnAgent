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
| 规则偏好抽取（无 LLM） | ✅ | `memory/rule_extract.py` → `extract_memory_candidates` |
| 续轮去重 kernel prompt / episodic / long-term inject | ✅ 部分 | `context/assemble.py` → `inject_dedupe_*`；checkpoint 已有 prior turn 时跳过重复 SystemMessage |
| Context Manager 单入口（memory + router + preretrieval + packing） | ✅ | `context/manager.py` → `assemble()`；`context_built` 事件 |
| Checkpoint 字符预算压缩（assemble 时） | ✅ | `context/checkpoint_pack.py` → `pack_checkpoint_for_budget` |
| **向量 long-term 召回 + HyDE** | ✅ Phase 2，默认关 | `memory/embedding.py`，`hyde.py`；`memory_long_term_use_vector=false` |
| **LLM 记忆提取器 + pending 标签** | ✅ Phase 2 | `llm_extractor.py`；`pending_confirmation`；`MemoryManager.confirm_memory_item()` |
| **`checkpoint_compacted` 写入 EventStore** | ✅ | `runner.compact_checkpoint` → `memory_emit_checkpoint_compacted` |
| Checkpoint consistency v2（completed run 对账） | ✅ | `runtime/execution_engine.py`；`verify_checkpoint_consistency_v2.py` |
| GDPR 删除 API / 真实鉴权 user 绑定 | ❌ | 见 §10.5 |
| Memory 回归 | ✅ | `verify_memory_checkpoint_consistency.py`、`verify_memory_production_v1.py`、`verify_memory_production_v2.py`、`verify_checkpoint_consistency_v2.py` |

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
         ▲ 每轮 ContextManager 追加 current turn + inject；超预算 → pack_checkpoint_for_budget；超条数 → CheckpointCompactor
┌─────────────────────────────────────────────────────────────┐
│  Episodic（跨 Run 摘要）                                      │
│  EventStore: memory_run_summary / memory_thread_summary      │
│  经 MemoryPolicy 召回 → SystemMessage 注入（非 checkpoint 本体）│
└─────────────────────────────────────────────────────────────┘
         ▲ Run 结束后 summarize_run / update_thread_summary
┌─────────────────────────────────────────────────────────────┐
│  Semantic（RAG）                                             │
│  preretrieval + search_docs → ToolMessage / SystemMessage    │
└─────────────────────────────────────────────────────────────┘
         ▲ ContextManager / tools
┌─────────────────────────────────────────────────────────────┐
│  Long-term（结构化）                                         │
│  SQLite memory_items（user/session scope）                   │
│  Run 结束规则/LLM 抽取 → 混合召回 → [LongTermMemory] inject   │
└─────────────────────────────────────────────────────────────┘
```

| 层 | 存储 | 注入方式 |
|---|---|---|
| Working | SQLite checkpoint（`agent_checkpoint_path`） | `graph_input.messages` 经 `add_messages` 合并 |
| Episodic | EventStore 事件 payload | `memory_context_messages()` → 单条 `SystemMessage` |
| Semantic（RAG） | RagStore + preretrieval / `search_docs` | Turn 前 `[PreRetrievedDocs]` 或 tool 摘录；不进 checkpoint 全文 unless ToolMessage |
| Long-term | SQLite `memory_items` | Run 结束写入；召回后 `[LongTermMemory]` inject |

**Episodic 召回（每轮）**：`get_eligible_run_summaries` 取最近 N 个 run summary → `recall_episodic_runs` 按 goal keyword overlap 打分 → `goals_conflict`（Jaccard < `conflict_jaccard_threshold` 视为冲突并丢弃）→ 与 thread summary 合并为 `[EpisodicMemory]` inject。

---

## 3. 数据流（单轮 Run）

```mermaid
flowchart LR
  client[Client messages array] --> trim[current_turn_messages]
  trim --> cm[ContextManager.assemble]
  episodic[MemoryManager.build_context] --> cm
  prerag[preretrieve_docs optional] --> cm
  router[ToolRoute SystemMessage] --> cm
  checkpoint[(LangGraph Checkpoint)] --> pack[pack_checkpoint_for_budget]
  cm --> pack
  pack --> graph[Agent Graph add_messages]
  cm --> graph
  graph --> events[EventStore + SSE/WS]
  events --> meta[run_completed_meta.message_count]
  graph --> finalize[finalize_memory after run]
  finalize --> episodicWrite[memory_run_summary / memory_items]
  finalize --> compact[compact_checkpoint after run]
  idle[ThreadLifecycle idle] --> compact
```

**非 resume 路径**（`ChatRunner.run_stream` → `ContextManager.assemble`）：

1. `current_turn_messages(messages)`：客户端列表只保留**最后一条 user**
2. `MemoryManager.build_context(...)`：召回 episodic + long-term，生成 `inject_preview`
3. `preretrieve_docs`（可选）：路由推荐 `search_docs` 时预检索 RAG，注入 `[PreRetrievedDocs]`
4. `build_graph_turn_messages`：Scenario/kernel prompt（续轮可 dedupe）+ episodic/long-term inject + router/preretrieval SystemMessage + 当前 user turn
5. `pack_checkpoint_for_budget`：若 **已有 checkpoint** + 本轮 graph 输入超 Scenario 字符预算，回写压缩/裁剪 checkpoint（见 §4.4）
6. `pack_graph_messages`：对**本轮**待追加的 graph 输入做字符 packing
7. LangGraph `add_messages` 将 packed 列表与 checkpoint 已有 `messages` **追加合并**

**Run 终态顺序**（`ExecutionEngine._finalize_memory`，仅 completed run 写 consistency）：

1. `checkpoint_consistency_checked` + `run_consistency_checked`（对账 message 计数）
2. `finalize_memory` → `summarize_run` / `update_thread_summary` / long-term 写入
3. `compact_checkpoint`（条数阈值压缩，可选写 `checkpoint_compacted`）

**resume 路径**：

- `graph_input = Command(resume=...)`，不重新走 `assemble`；interrupt / approval 状态由 checkpoint 承载

**idle 清理**：`ThreadLifecycleCleaner` 对 idle thread 周期性调用 `compact_checkpoint`（同 Run 终态入口）

---

## 4. Checkpoint 压缩

### 4.1 组件

`CheckpointCompactor`（`copilot_agent/memory/checkpoint_compactor.py`）：

- 读取 `aget_state` 的 `messages` 列表
- 超过 `checkpoint_compact_message_threshold` 时，将**较早消息**合并为一条 deterministic `SystemMessage`（前缀 `[CheckpointCompaction]`）
- 保留最近 `checkpoint_compact_keep_recent_turns` 个 user 轮及其后的 assistant/tool 消息
- 通过 `RemoveMessage` + `aupdate_state` 回写，不新建 thread
- Run 终态 / idle 清理由 `ChatRunner.compact_checkpoint` 调用；成功时可选写 `checkpoint_compacted` 事件（`memory_emit_checkpoint_compacted`）

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

### 4.4 Checkpoint 字符预算压缩（assemble 时）

与 §4.1 **条数阈值压缩**不同，`pack_checkpoint_for_budget`（`context/checkpoint_pack.py`）在 **每轮 `ContextManager.assemble`** 前执行，按 Scenario `max_context_chars` 控制 working memory 体量：

| 步骤 | 条件 | 行为 |
|------|------|------|
| 1 | `context_checkpoint_pack_enabled=false` | 跳过 |
| 2 | `checkpoint_chars + new_turn_chars ≤ budget` | 跳过 |
| 3 | 消息数 > `checkpoint_compact_message_threshold` | 先尝试 `CheckpointCompactor` |
| 4 | 仍超预算 | 对较早 prefix 做 deterministic 摘要（同 `[CheckpointCompaction]` 前缀） |
| 5 | 仍超预算 | 丢弃最旧非 user 消息 / 截断旧 assistant-tool 内容 |

结果写入 `ContextBundle.truncation_report.checkpoint_pack`，并可在 `context_built` 事件中观测。与 Run 结束后 `compact_checkpoint` **可叠加**（assemble 时控字符，idle/终态时控条数）。

---

## 5. 结构化长期记忆（Phase 1 + Phase 2）

Phase 1 为 **规则抽取 + SQLite + 混合打分**；Phase 2 在此基础上增加 **HyDE / 向量 / LLM 提取 + pending 确认**（均可通过 settings 关闭）。

### 5.1 存储（`memory_items` 表）

与 EventStore 同库（`agent_event_store_path`），字段含：`user_id`、`thread_id`、`scope`（`user|session`）、`memory_type`（`fact|preference|behavior|task_summary`）、`importance`、`confidence`、`version`、`supersedes_id`、`is_deprecated`、`expires_at`、`pending_confirmation`、`embedding_json`、`history_json`。

### 5.2 写入（Run 结束后）

`summarize_run` → `MemoryItemWriter.persist_run_memories`：

1. **规则抽取**（`memory/rule_extract.py`）：goal→`task_summary`；token 输出→`fact`；偏好短语→`preference`
2. **可选 LLM 抽取**（`memory/llm_extractor.py`，`memory_llm_extract_enabled`）：JSON 候选；`confidence < memory_llm_confirm_threshold` → `pending_confirmation=true`
3. `importance < memory_long_term_importance_min` 丢弃
4. 相同 `content_hash` → dedup skip
5. 相似度 ≥ dedup 阈值且 ≥ conflict 阈值 → **覆盖**旧条（deprecated + version+1 + history）
6. TTL 到期删除 + 超 `memory_long_term_max_items_per_user` 淘汰低分条（`importance ≥ protected` 不参与）

### 5.3 检索（每轮 `build_context` / `get_memory_preview`）

`recall_long_term_items`：默认 `final_score = w_kw·keyword + w_time·decay + w_imp·importance`；启用向量时叠加 HyDE query + embedding 相似度（`memory_long_term_use_vector`，默认 **关**）。过滤 `is_deprecated` / 过期 / pending / 低于 `memory_long_term_recall_min_score`。

User scope 记忆跨 thread 可见（同 `threads.user_id`，默认 `user_id=thread_id`）；session scope 仅本 thread。

### 5.4 注入位置

`build_episodic_inject_bundle` 将 `[EpisodicMemory]` 与 `[LongTermMemory]` 合并为单条 inject SystemMessage，经 `memory_context_messages()` 进入 `ContextManager.assemble`。续轮可通过 `memory_inject_dedupe_*` 跳过 checkpoint 中已存在的同前缀 SystemMessage。

验收：`python scripts/verify_memory_production_v1.py`（`--profile core`）。

### 5.5 Phase 2 增强（可选开关）

| 能力 | 实现 | 默认 |
|------|------|------|
| **向量召回** | `memory/embedding.py`；写入时存 embedding；召回混合 keyword + vector | `memory_long_term_use_vector=false` |
| **HyDE** | `memory/hyde.py`；`memory_hyde_mode=rule\|llm` | `memory_hyde_enabled=true` |
| **LLM 提取器** | `memory/llm_extractor.py` | `memory_llm_extract_enabled=true` |
| **pending 不注入** | `list_active(include_pending=False)`；`confirm_memory_item()` | — |

验收：`python scripts/verify_memory_production_v2.py`（`--profile core`）。

---

## 6. Context Manager 与 Memory 边界（M15）

分工表、装配流水线与配置见 **[context-manager-design.md §5](./context-manager-design.md)**。本节只强调：**checkpoint 仍是 working memory 真相源**；MemoryManager 负责存储与召回，Context Manager 负责每轮 **组装图输入**。

---

## 7. 策略配置（`MemoryPolicyConfig` + Settings）

### Checkpoint 条数压缩

| 字段 / Settings | 默认 | 含义 |
|---|---|---|
| `memory_checkpoint_compact_enabled` | `true` | 是否启用 `CheckpointCompactor` |
| `memory_checkpoint_compact_message_threshold` | `40` | 触发压缩的消息条数 |
| `memory_checkpoint_compact_keep_recent_turns` | `6` | 保留最近 user 轮数 |
| `memory_checkpoint_compact_summary_max_chars` | `2000` | 压缩摘要最大字符 |
| `memory_emit_checkpoint_compacted` | `true` | 压缩成功后写 `checkpoint_compacted` 事件 |

### Checkpoint 字符预算（Context Manager）

| Settings | 默认 | 含义 |
|---|---|---|
| `context_checkpoint_pack_enabled` | `true` | assemble 前 `pack_checkpoint_for_budget` |
| Scenario `budgets.max_context_chars` | 见 scenario | 总上下文字符预算 |

### Episodic / Long-term

Episodic（`enabled`、`episodic_recall_top_k`、`thread_summary_max_*`、`conflict_jaccard_threshold` 等）与 long-term（`memory_long_term_*`、`memory_hyde_*`、`memory_llm_*`、`memory_inject_dedupe_*`）见 `copilot_agent/memory/policy.py` 与 `settings.py`，与 checkpoint 压缩正交。

### Context 装配

| Settings | 默认 | 含义 |
|---|---|---|
| `context_packing_enabled` | `true` | 对本轮 graph 输入做字符 packing |
| `context_emit_built_event` | `true` | 写 `context_built` 审计事件 |
| `context_preretrieval_enabled` | `true` | Turn 前 RAG 预检索（见 [rag-design.md §6.2](./rag-design.md)） |

---

## 8. 一致性契约

产品与评测关心的不变量：

| 不变量 | 说明 |
|---|---|
| 当前轮输入 | `current_turn_messages` 长度为 1，content 为最后 user |
| 计数对齐 | `CheckpointReader.snapshot().message_count` ≈ `run_completed_meta.message_count`（completed run）；不一致时写 `checkpoint_consistency_checked` warning，**不改** Run 终态 |
| 压缩可观测 | 条数压缩 → `checkpoint_compacted`；assemble 压缩 → `context_built.checkpoint_compacted` |
| Resume 安全 | `state.next` 非空时不压缩；approve/reject 续跑不依赖客户端全量历史 |
| inject 去重 | 续轮可跳过重复 kernel prompt / `[EpisodicMemory]` / `[LongTermMemory]`（`inject_dedupe_*`） |

回归入口：

- `verify_memory_checkpoint_consistency.py`（`memory_checkpoint_consistency`）
- `verify_checkpoint_consistency_v2.py`（dual-store 对账）
- `verify_memory_production_v1.py` / `v2.py`（long-term 管线）

见 [ci-design §3](./ci-design.md)、[runtime-design.md Appendix](./runtime-design.md)。

---

## 9. 模块边界

| 模块 | 职责 |
|---|---|
| `agent/message_utils.py` | `current_turn_messages`、`last_user_content` |
| `agent/runner.py` | 委托 `ContextManager.assemble`；`compact_checkpoint`、`finalize_memory` |
| `context/manager.py` | M15 单入口：memory + router + preretrieval + checkpoint pack + packing |
| `context/assemble.py` | `build_graph_turn_messages`、inject dedupe |
| `context/memory_inject.py` | episodic/long-term → `SystemMessage` |
| `context/checkpoint_pack.py` | assemble 前字符预算压缩 |
| `context/packing.py` | 本轮 graph 输入字符 packing |
| `memory/manager.py` | `build_context`、`summarize_run`、long-term 写入 |
| `memory/checkpoint_compactor.py` | 条数阈值 checkpoint 压缩 |
| `memory/item_store.py` / `item_writer.py` | 结构化 long-term 存储与召回 |
| `memory/policy.py` | episodic/long-term/checkpoint 策略 |
| `runtime/checkpoint_reader.py` | 只读 snapshot（`message_count`、`has_interrupt`） |
| `runtime/execution_engine.py` | Run 终态 `finalize_memory` + `compact_checkpoint`；v2 consistency |
| `runtime/thread_lifecycle.py` | idle 线程周期 `compact_checkpoint` |
| `agent/stream/event_mapper.py` | `run_completed_meta.message_count` |

---

## 10. 未来优化方向

### 10.1 输入与图状态

- ~~**续轮 inject 去重**~~ ✅ 部分：`inject_dedupe_system_prompt` / `inject_dedupe_memory_messages`（`context/assemble.py`）；router/preretrieval SystemMessage 仍每轮注入
- **HTTP API 契约**：文档化「`messages` 仅传当前 user」；对仍传全量历史的客户端打 deprecation 日志或校验警告
- **CheckpointReader 增强**：暴露 `last_user_turn_index`、是否含 `[CheckpointCompaction]`，供 Timeline / UI 展示

### 10.2 压缩质量

- 可选 **LLM 摘要压缩**（夜跑 / feature flag），与 deterministic 路径 A/B 对比
- ~~压缩事件写入 EventStore（`checkpoint_compacted`），Timeline 可展示「何时压缩、压缩前后条数」~~ ✅ Phase 2
- 按 token 数而非 message 条数触发阈值，更贴近模型上下文窗口

### 10.3 Episodic 与 Working 协同

- 压缩时把被折叠轮次的 hash 记入 episodic，防止「checkpoint 丢了细节但 episodic 未补」
- 失败 / cancelled Run 的摘要策略与 `include_failed_runs` 在长线程下的交互测试扩面
- 与 [data-flow-design.md](./data-flow-design.md) 中 `retrieval_completed` 联动：RAG 片段是否应进入 checkpoint 或仅保留在 ToolMessage

### 10.4 运维与评测

- 压缩失败告警（`compact_result.reason` 分布）
- 在 golden scenario 中增加「多轮 + 超阈值 + resume」组合 case
- 提供 `scripts/export_checkpoint_messages.py` 便于与失败 run 的 timeline 对照

### 10.5 八层栈改造分配（待办）

Wave1–2 已完成项见 **§0**。路线图索引：[agent-learning-guide §7](./agent-learning-guide.md)。

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **2** | L5 Agent State | 续轮 router/preretrieval inject 去重 | `verify_memory_checkpoint_consistency.py` 扩展 |
| **2** | L5 | 压缩时 hash 写入 episodic，防 checkpoint 丢细节 | 新 golden case |
| **2** | L2 | episodic run summary 可选向量索引 | `verify_memory_production_v2.py` 扩展 |
| **3** | L8 | `export_checkpoint_messages.py` + 失败 run timeline 导出联动 | eval + observability |
| **4** | L3/L4 | 服务端 `user_id` 鉴权；Memory GDPR 删除 API | 新 verify + guardrail 联动 |
| **4** | L8 | 跨 thread checkpoint 迁移工具 | 运维脚本 |

---

## 11. 遗留问题

| 问题 | 影响 | 说明 |
|---|---|---|
| 续轮仍注入 router / preretrieval SystemMessage | checkpoint 内 System 消息仍可能增多 | kernel/episodic/long-term 已 dedupe；tool route 与 RAG 预检索尚未 dedupe |
| 客户端仍可传全量 `messages[]` | 易被误用为「仍以 HTTP 携历史」 | 服务端已截断为 current turn，但 API 语义未在 OpenAPI 中强制 |
| 压缩摘要非语义级 | 旧细节可能丢失，回复质量下降 | 当前为 deterministic 截断；无 LLM 回填 |
| `run_completed_meta` 与 snapshot 对齐为 best-effort | 极端并发或压缩竞态下可能短暂不一致 | verify 使用顺序化夹具；生产多 worker 未全覆盖 |
| Episodic inject 与 Compaction 前缀无去重 | 模型可能同时看到 `[EpisodicMemory]` 与 `[CheckpointCompaction]` | 需 prompt / 节点层约定优先级 |
| idle 压缩依赖后台任务 | 高负载时 idle TTL 内 checkpoint 仍可能过大 | `thread_lifecycle` 周期与 TTL 需按环境调参 |
| Memory 预览 API 仍暴露 `messages` 兼容字段 | 前端若展示 `working.messages` 可能误导 | 应逐步只读 `current_turn_messages` |
| 无跨 thread 迁移工具 | checkpoint 路径变更或损坏时恢复困难 | 仅依赖 SQLite 文件路径配置 |

---

## 12. 非目标

- 不用 EventStore 事件流重放生成 LangGraph `messages`（审计只读，不作 working 重建源）
- 不在 checkpoint 内存储未脱敏的 secrets（工具审计仍走 `ToolResultModel.sanitized_*`）
- 不把 RAG 全库嵌入 checkpoint（语义检索经 preretrieval / `search_docs`；摘录以 SystemMessage / ToolMessage 形式进入图）

---

## Appendix: Consistency v2 / Debug Bundle

Memory/checkpoint consistency v2 明确双存储边界（详见 [runtime-design.md Appendix](./runtime-design.md)）：

- **EventStore**：产品事实与可回放 Timeline。
- **LangGraph checkpoint**：working memory（messages + interrupt state）。
- 两者 **不做** 原子事务；LearnAgent 仅用派生事件对账，**不**从 EventStore 重建 checkpoint。

Run 完成 finalize 时，`ExecutionEngine` 在 `run_completed_meta` 之后写入 `checkpoint_consistency_checked`，对比 message 计数并记录 read/missing/interrupt/match/warnings。不一致 **不会** 将 `completed` 改回 `failed`。

验证：

```powershell
python scripts/verify_checkpoint_consistency_v2.py --event-store-path storage\verify-checkpoint-consistency-events.sqlite --checkpoint-path storage\verify-checkpoint-consistency-checkpoints.sqlite
```

调试导出（含 Timeline、consistency 事件、checkpoint SQLite 检查）：

```powershell
python scripts/export_run_debug_bundle.py --event-store-path storage\learnagent-events.sqlite --checkpoint-path storage\langgraph-checkpoints.sqlite --run-id <run_id>
```

---

## Appendix: Memory Conversion / Eviction v1

Short-term memory (`memory_run_summary.memory_candidates_seed`) is treated as a candidate stream, not durable memory by itself. `MemoryItemWriter.conversion_skip_reason` is the quality gate before a seed becomes an active `memory_items` row.

Conversion defaults:
- completed runs may write reusable `task_summary` / `fact` candidates.
- failed, cancelled, rejected, or policy-blocked runs do not write ordinary facts.
- explicit `preference` / `behavior` candidates may still write when confidence is high enough.
- reusable `policy_decision` facts may write as governance memory.
- low-confidence, low-importance, non-reusable, empty, or sensitive candidates are skipped with an explainable `MemoryWriteResult.reason`.

Eviction defaults:
- `MemoryItemStore.evict_lowest_score` keeps its public signature but now uses `memory_eviction_score`.
- the score combines importance, confidence, access count, last access recency, update recency, memory type, pending status, and near-expiry TTL.
- `importance >= memory_long_term_protected_importance` remains protected.
- eviction is soft: items are deprecated and history records `reason=capacity_limit_v2` plus `eviction_score`.

Verification:
```powershell
python scripts/verify_memory_conversion_eviction_v1.py
```
