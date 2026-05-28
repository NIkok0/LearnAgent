# LearnAgent Context Manager 设计

> 说明 Kernel **M15** 如何统一装配每轮 LLM 输入：Memory 召回、Tool 路由、RAG preretrieval、budget packing 与 `context_built` 审计。  
> 关联文档：[agent-learning-guide.md](./agent-learning-guide.md)、[memory-checkpoint-design.md](./memory-checkpoint-design.md)、[rag-design.md](./rag-design.md)、[tool-design.md](./tool-design.md)、[data-flow-design.md](./data-flow-design.md)、[eval-design.md](./eval-design.md)

**K/C/S 位置**：Kernel **M15**；读 Scenario prompt/router、Memory 召回、RAG 检索结果，产出 `ContextBundle` 与 `graph_messages`；**不**建索引、**不**存 Memory、**不**做 Policy 最终裁决。架构索引见 [guide §2.4](./agent-learning-guide.md)。

**本文负责**：每轮 LLM 输入装配、budget packing、preretrieval/memory/tool route 注入、`context_built` 审计。  
**本文不负责**：Memory 持久化、RAG 建库、Tool 执行、PolicyGate 最终裁决、Run FSM。  
**权威来源**：模块边界与全局缺口见 [agent-learning-guide.md](./agent-learning-guide.md)；各输入来源内部策略见对应专项文档。

---

## 0. 实现状态

| 能力 | 状态 | 验收脚本 |
|------|------|----------|
| `assemble()` 单入口（memory + router + preretrieval + packing） | ✅ | `verify_context_manager.py` |
| `ContextBundle` schema（`contracts/context.py`） | ✅ | `verify_context_manager.py` |
| `plan_route()` / `route_system_message()` | ✅ | `verify_tool_router.py`（路由）；assemble 集成见 context verify |
| RAG preretrieval + `retrieval_completed`（`retrieval_mode=preretrieval`） | ✅ | `verify_context_manager.py` |
| `search_docs` 与 preretrieval 去重（`preretrieval_dedupe`） | ✅ | `verify_context_manager.py` |
| checkpoint 预算压缩（`checkpoint_pack`）+ `pack_graph_messages` | ✅ | `verify_context_manager.py` |
| `build_assistant_injections()` 排障 outline | ✅ | `verify_diagnosis_template.py` |
| `context_built` EventStore 审计 | ✅ | `verify_context_manager.py` |
| 高级 packing 策略（语义级压缩） | ❌ | — |

套件见 [ci-design.md](./ci-design.md)（`context_manager` 在 `core` 的 `CONTRACT_SUITES`）。

---

## 1. 设计动机

在引入 M15 之前，Runner、Graph 节点、Memory、RAG 各自拼接 SystemMessage 与检索摘录，导致：

| 问题 | 后果 |
|------|------|
| 多入口拼 prompt | 预算不可控；同一轮 retrieval 可能重复注入 |
| 无统一 truncation 报告 | Timeline 无法回答「为何截断」 |
| router 与 RAG 顺序分散 | Tool-grounded 语义（先路由、再 preretrieval）难断言 |

**原则**：每轮 LLM 可见输入只经 **`ContextManager.assemble()`** 产出；`ChatRunner` 将 `bundle.graph_messages` 交给 LangGraph。

---

## 2. 职责边界

### 2.1 负责

```text
输入：
- 当前 user message（goal）
- LangGraph checkpoint messages（working memory）
- Memory 召回（episodic / long-term inject）
- Scenario system prompt
- RouterEngine → tool_route SystemMessage
- RAG preretrieval snippets（按路由与预算）
- Scenario budgets（max_context_chars 等）
- ToolRegistry public schemas（写入 bundle，供 plan/audit）

输出：
- ContextBundle（结构化装配摘要）
- graph_messages（实际进图的 BaseMessage 列表）
- context_built RuntimeEvent（可选）
- preretrieval 时 retrieval_completed（retrieval_mode=preretrieval）
```

### 2.2 不负责

```text
不负责 RAG 建索引与 ingest；
不负责 Memory 持久化与 episodic 写入；
不负责 Tool handler 执行；
不负责 Run FSM / cancel / approve；
不负责 PolicyGate 最终 allow/deny（只携带 policy_hints / tool_route）。
```

与 Memory 分工详见 **§5**；与 RAG 检索链详见 [rag-design.md](./rag-design.md) §5–§6。

---

## 3. 核心类型

### 3.1 `ContextBundle`

定义于 `copilot_agent/contracts/context.py`：

```python
class ContextBundle(BaseModel):
    thread_id: str
    run_id: str | None = None
    user_message: str = ""
    checkpoint_messages: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_context: list[dict[str, Any]] = Field(default_factory=list)
    memory_injections: list[dict[str, Any]] = Field(default_factory=list)
    scenario_prompts: list[str] = Field(default_factory=list)
    enabled_tool_schemas: list[dict[str, Any]] = Field(default_factory=list)
    policy_hints: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    truncation_report: dict[str, Any] = Field(default_factory=dict)
    graph_messages: list[Any] = Field(default_factory=list, exclude=True)
```

`graph_messages` 为运行时产物，不参与 JSON 序列化；评测与审计读 `truncation_report` + `context_built` payload。

### 3.2 `ContextBuiltPayload`

EventStore `kind=context_built` 的结构化载荷见 [data-flow-design.md](./data-flow-design.md) §2.5；由 `context/events.build_context_built_payload()` 构造。

---

## 4. 装配流水线

`ContextManager.assemble()` 顺序（`context/manager.py`）：

```text
1. memory.build_context()           → episodic / LTM 召回字典
2. plan_route() + route_system_message()
3. preretrieve_docs()               → 可选 [PreRetrievedDocs] + retrieval_completed
4. build_graph_turn_messages()      → system + memory inject + checkpoint + user turn
5. pack_checkpoint_for_budget()     → 超预算时压缩 checkpoint 历史
6. pack_graph_messages()            → 对本轮 graph_messages 字符截断
7. build_preretrieval_cache()       → tool-time search_docs 去重用
8. 构造 ContextBundle + _emit_context_built()
```

调用前必须 `bind_graph()`，否则 `assemble()` 抛错。

### 4.1 装配优先级（超预算时）

```text
1. 当前 user message 与系统安全约束永远保留；
2. 当前 Run 必需的 tool schema 与 policy hints 保留；
3. 与当前任务强相关的 retrieval snippets 保留；
4. 最近 checkpoint messages 保留；
5. episodic memory 只按需 inject；
6. 超预算时先压缩 checkpoint 历史，再减少弱相关 retrieval，最后减少 memory inject。
```

实现：`checkpoint_pack` → `pack_graph_messages`；步骤名写入 `truncation_report.truncation_steps`。

### 4.2 Tool-time RAG enrich

`context/retrieval.enrich_retrieval_payload()` 在 `search_docs` 执行后 enrich 结果；若 `truncation_report.preretrieval_cache` 存在且 `context_preretrieval_dedupe_enabled`，则跳过与 preretrieval 重复的 chunk。

---

## 5. 与 Memory / RAG / Tool-grounded 的边界

| 职责 | Context Manager | MemoryManager | RAG / Tool-grounded |
|------|-----------------|---------------|---------------------|
| Working messages | 读 checkpoint；`checkpoint_pack` + packing | 不直接改 checkpoint 列表 | — |
| Episodic / LTM inject | `assemble()` 内注入 SystemMessage | `build_context` / `memory_context_messages` | — |
| RAG preretrieval | 轮首 `[PreRetrievedDocs]`；写 `retrieval_completed` | append EventStore | `RagStore.search` |
| Tool route | `RouterEngine` → `tool_route` 进 bundle | — | [tool-design.md](./tool-design.md) |
| 排障模板 | `build_assistant_injections()` | — | `agent/diagnosis.py` |
| 审计 | `context_built` | `memory_*` / `checkpoint_compacted` | `retrieval_completed`（tool 路径） |

**原则**：checkpoint 仍是 working memory 真相源（见 [memory-checkpoint-design.md](./memory-checkpoint-design.md) §1）；Context Manager 只在每轮 **组装图输入** 时读/压/注。

---

## 6. 配置开关

| Settings | 默认 | 含义 |
|----------|------|------|
| `context_preretrieval_enabled` | `true` | 轮首 RAG 预检索 |
| `context_preretrieval_budget_chars` | `3500` | preretrieval 摘录预算 |
| `context_packing_enabled` | `true` | `pack_graph_messages` 截断 |
| `context_checkpoint_pack_enabled` | `true` | checkpoint 预算压缩 |
| `context_preretrieval_dedupe_enabled` | `true` | tool-time 与 preretrieval 去重 |
| `context_emit_built_event` | `true` | 写 `context_built` 事件 |
| `rag_context_budget_chars` | `14000` | 总 context 字符预算 fallback |
| Scenario `budgets.max_context_chars` | 场景配置 | 优先于 settings fallback |

Router / 排障相关：`agent_tool_route_enabled`、`agent_diagnosis_template_enabled`（见 [tool-design.md](./tool-design.md) §3.10）。

---

## 7. 运行时集成

```text
ChatRunner
  → ContextManager.bind_graph(graph)
  → assemble(thread_id, run_id, turn_messages, goal, ...)
  → graph.ainvoke / astream({ messages: bundle.graph_messages, tool_route: ... })
```

`plan_created` 载荷由 `plan_created_payload()` 生成，含 `tool_route` 与 `available_tools`。

---

## 8. 八层栈改造分配（待办）

Wave2 基线（单入口、preretrieval、packing、`context_built`）见 **§0**。路线图索引：[agent-learning-guide §7](./agent-learning-guide.md)。

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **2** | L5 | 续轮 inject 与 Context 装配去重收尾（与 memory §8.5 联动） | `verify_memory_checkpoint_consistency.py` 扩展 |
| **2** | L5 | ~~planner 硬 merge 检索 path → `tool_route.suggested_paths`~~ | ✅ [tool-design §3.7](./tool-design.md) |
| **3** | L5 | 语义级 context 压缩（LLM 摘要替代 deterministic 截断） | 新 eval case |
| **4** | L5 | 多 Agent 子目标各自的 context partition | tech-selection §4 |

---

## 9. 代码索引

| 模块 | 路径 |
|------|------|
| ContextManager | `copilot_agent/context/manager.py` |
| ContextBundle | `copilot_agent/contracts/context.py` |
| Graph 回合装配 | `copilot_agent/context/assemble.py` |
| Checkpoint 预算压缩 | `copilot_agent/context/checkpoint_pack.py` |
| 字符 packing | `copilot_agent/context/packing.py` |
| Preretrieval | `copilot_agent/context/preretrieval.py` |
| Preretrieval 去重 | `copilot_agent/context/preretrieval_dedupe.py` |
| Tool-time enrich | `copilot_agent/context/retrieval.py` |
| `context_built` payload | `copilot_agent/context/events.py` |
| Memory inject 辅助 | `copilot_agent/context/memory_inject.py` |
| 回归脚本 | `scripts/verify_context_manager.py` |

---

## 10. 文档关系

- **上游**：[memory-checkpoint-design.md](./memory-checkpoint-design.md)（checkpoint 真相源）、[rag-design.md](./rag-design.md)（检索）、[tool-design.md](./tool-design.md)（路由）
- **下游**：`ChatRunner`、`nodes.planner` / `nodes.assistant`
- **契约**：[data-flow-design.md](./data-flow-design.md) §2.5（`context_built`）
- **全量索引**：[agent-learning-guide §6](./agent-learning-guide.md)

---

## 11. 遗留问题

| 问题 | 影响 |
|------|------|
| packing 为 deterministic 截断 | 长线程可能丢失中间细节；依赖 CheckpointCompactor 兜底 |
| `ContextBundle.checkpoint_messages` 字段预留 | 当前 assemble 路径主要用 `graph_messages`；字段未每轮填充 |
| 无独立 context 预览 API | 调试依赖 EventStore `context_built` 或 memory preview |

全局缺口见 [agent-learning-guide §2.8](./agent-learning-guide.md)。
