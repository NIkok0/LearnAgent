# LearnAgent v2 Runtime Architecture

> **本文负责**：把 LearnAgent v2 收敛为代码助手式 Agent Runtime 产品内核，明确 Runtime / Context / Capability / Governance 四层边界。  
> **本文不负责**：新增 Skill 类型、扩展 MCP marketplace、多租户、分布式 worker 或重模型评测策略。  
> **相关文档**：[runtime-design.md](./runtime-design.md)、[context-manager-design.md](./context-manager-design.md)、[capability-design.md](./capability-design.md)、[tool-design.md](./tool-design.md)、[guardrail-policy-design.md](./guardrail-policy-design.md)、[rag-design.md](./rag-design.md)

---

## 1. v2 目标

LearnAgent v2 的目标不是继续堆功能，而是把现有原型收敛成一个能稳定解释自身行为的 Agent Runtime：

```text
User Goal
  -> Create Run
  -> Build Context
  -> Planner
  -> Assistant
  -> Tool Proposal
  -> Safety Gate
  -> Tool Execution / HITL
  -> Observation
  -> Final Answer
  -> Timeline / Audit
```

v2 优先保证这条主链路可追踪、可恢复、可审批、可调试。RAG、Memory、Skills、MCP 都是增强能力，不应该反过来污染 Runtime 主状态机。

---

## 2. 四层边界

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| Runtime Core | Thread、Run、EventStore、Checkpoint、ExecutionEngine、Timeline | RAG 策略、Skill 触发、业务 prompt |
| Context Core | system prompt、user goal、memory、retrieval、skill、tool trace 的装配与预算 | 产生业务事实、执行工具、授权工具 |
| Capability Core | ToolRegistry、CapabilityPack、MCP adapter、ToolResult 标准化 | 决定本轮是否调用工具 |
| Governance Core | PolicyGate、HITL、approval、side effect ledger、idempotency、credential scope | 提供工具实现、替 planner 做业务判断 |

原则：

- Planner / Skill 只能建议，不授权。
- MCP / HTTP / RAG tool 必须进入 ToolRegistry 后统一治理。
- EventStore 是产品事实源；LangGraph checkpoint 是图执行恢复状态。
- Timeline 面向解释和排障，不替代 EventStore。

---

## 3. Context Core Provider 化

v2 引入 `ContextBlock` 作为 provider 化的中间产物：

```text
ContextBlock {
  source,
  priority,
  content,
  token_estimate,
  policy_tags,
  trace_id,
  metadata
}
```

第一阶段保持现有 `ContextManager.assemble()` 行为不变，只在 `ContextBundle.context_blocks` 中记录 provider trace。后续再逐步把内部逻辑迁移成独立 provider：

| Provider | 产物 | 说明 |
| --- | --- | --- |
| SystemPromptProvider | `source=system_prompt` | Scenario system prompt |
| MemoryProvider | `source=memory:*` | episodic / long-term memory 注入摘要 |
| RetrievalProvider | `source=retrieval` | RetrievalGate 决策与真实检索结果 |
| SkillProvider | `source=skill` | skill selection / injected / missing capability |
| ToolTraceProvider | `source=tool_trace` | 后续用于压缩工具轨迹 |
| BudgetPacker | `source=budget_packer` | 上下文预算、截断、checkpoint packing |

这个设计要回答三个问题：

- 为什么这段上下文进来了？
- 为什么这轮没有搜 RAG？
- 为什么某个 skill 被 preview 但没有注入？

---

## 4. Planner 与 RAG 边界

Planner v2 的职责收窄为：

- 输出 `route_kind`。
- 输出 `recommended_tools`。
- 输出 `plan_steps`。

Planner 不执行工具，不越过 `safety_gate`，不直接改变 Scenario policy。LLM planner 优先，规则 planner 是 deterministic fallback。

RAG v2 是证据工具，不是每轮默认上下文：

- 文档、接口、部署、错误码、排障问题：`retrieve`。
- 相似 query 且权限上下文一致：`reuse_cache`。
- 闲聊、确认、格式化：`skip_rag`。
- 当前、今天、最新、线上是否生效：`route_to_tool_api`。
- memory 已有高置信 answer seed：跳过或降低检索优先级。

RetrievalGate 的 decision 必须进入 context trace 和 Timeline，避免 RAG 行为变成黑盒。

---

## 5. Tool Execution 产品化

Tool Execution 是 LearnAgent 最核心的工程亮点，v2 继续强化：

- 每个 tool 都必须有 schema、risk、scope、timeout。
- 每次 tool call 都必须有 `call_id`。
- 高风险工具必须进入 HITL。
- 写操作必须有 idempotency key。
- tool result 标准化后再回给模型。
- side effect 发生后必须落 ledger。
- assistant 最终失败时，不默认回滚副作用，只提供补偿建议和审计说明。

工具治理的成功标准不是“模型能调用工具”，而是“用户能知道为什么允许、为什么审批、为什么拒绝、执行后发生了什么”。

---

## 6. Timeline Debugger

v2 Timeline 固定三层视图：

| 视图 | 用户 |
| --- | --- |
| Runtime | 默认视图，展示 run 生命周期、planner、RAG decision、tool、approval、final answer |
| Assistant State | 调试视图，展示 assistant_state / reasoning / delta |
| Raw Events | 兜底视图，展示完整事件 payload |

失败定位至少要能区分：

- LLM 输出失败。
- tool schema 不合法。
- PolicyGate 拒绝。
- RAG 没召回或被 policy filter。
- checkpoint / EventStore 状态不一致。
- 外部 API 失败。

---

## 7. 第一阶段验收

第一阶段只做架构收敛，不改变运行协议：

- `ContextBundle` 增加 `context_blocks`。
- `ContextManager.assemble()` 继续生成原有 graph messages。
- `context_blocks` 能解释 system、memory、retrieval、skill、tool schema、policy、budget 来源。
- 现有 REST / SSE / Run 状态机不变。
- 现有 core-fast 验证继续通过。

验证入口：

```bash
python scripts/verify_context_manager.py
python scripts/verify_skills_v1.py
python scripts/verify_eval_suite.py --profile core-fast
python -m compileall copilot_agent scripts
```

---

## 8. 暂缓事项

v2 第一阶段不做：

- 多 Agent 协作。
- 多租户隔离。
- 分布式 worker。
- 外部 Skill 安装。
- MCP marketplace。
- 大规模重写 ExecutionEngine。
- 新增更多 verifier。

先把主链路讲清楚，再扩能力。
