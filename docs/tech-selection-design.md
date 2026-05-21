# LearnAgent 技术选型设计

> 调研状态：阶段性调研，日期为 2026-05-19。本文用于指导 LearnAgent 下一阶段架构演进，不等价于完整 benchmark、源码级审计或长期维护成本评估。

## 1. 选型结论摘要

开源 Agent 生态已经覆盖了 LLM 接入、工具调用、图编排、checkpoint、人类审批、memory、guardrail、trace 和任务队列的大量底层能力。LearnAgent 当前缺少的不是这些底层能力本身，而是把它们组合成一致产品语义的 runtime contract。

结论按三类划分：

| 分类 | 当前判断 | 代表方案 | LearnAgent 处理方式 |
|---|---|---|---|
| 可直接采用 | 能直接服务当前架构，改造成本低 | `ChatOpenAI`、LangGraph StateGraph/checkpoint、LangChain `StructuredTool`、FastAPI SSE、SQLite EventStore | 保持为主线实现 |
| 可集成但需要适配 | 有成熟能力，但数据模型、事件协议或运行语义不同 | LiteLLM、LangGraph interrupt、OpenAI Agents guardrails/tracing、Zep/Mem0、Langfuse/LangSmith、Temporal/Celery | 先保留 adapter 边界，后续逐项 PoC |
| 需要 LearnAgent 设计 | 不是通用框架能自动决定的产品语义 | runtime event contract、tool governance schema、memory orchestration policy、approval/cancel semantics、trace correlation、tool result protocol | 作为项目内核心 contract 设计 |

当前推荐主线仍是：

```text
FastAPI + LangGraph + LangChain + SQLite EventStore + RAG + ExecutionEngine
```

原因是它最贴近当前单用户本地 runtime 目标，能最小化迁移成本，并允许后续逐步接入 LiteLLM、LangGraph interrupt、外部 memory、外部队列和更完整的 observability。

## 2. 评估维度

后续每个模块按以下维度评估：

| 维度 | 说明 |
|---|---|
| 成熟度 | 是否有稳定文档、社区使用、生产案例或清晰版本演进 |
| 架构兼容性 | 是否适配当前 FastAPI + LangGraph + SQLite + SSE 架构 |
| thread/run/checkpoint | 是否支持会话、运行记录、状态持久化或恢复 |
| approval / interrupt / cancel | 是否支持人类审批、中断、恢复、取消 |
| tool metadata / audit | 是否支持工具元数据、风险等级、调用审计 |
| memory / RAG / long-term recall | 是否支持工作记忆、语义检索、长期记忆 |
| tracing / observability | 是否支持 trace、span、metrics、事件导出 |
| 单用户本地部署 | 是否适合本地轻量运行，不强依赖外部托管服务 |
| 引入成本和替换成本 | 依赖复杂度、迁移成本、与当前代码耦合度 |

## 3. 模块级对比矩阵

### Agent Framework / Orchestration

| 方案 | 成熟度 | 优势 | 限制 | 与 LearnAgent 匹配度 | 结论 |
|---|---:|---|---|---|---|
| LangGraph | 高 | 图编排、streaming、checkpoint、durable execution、human-in-the-loop 能力完整 | 需要项目自己定义 REST/SSE/event store contract | 高 | 当前主线 |
| OpenAI Agents SDK | 中高 | Agent、tool、handoff、guardrail、tracing 一体化 | 更偏 OpenAI SDK 生态；当前项目已深度使用 LangGraph | 中 | 可作为 guardrail/tracing 参考 |
| CrewAI | 中高 | Crews/Flows、memory、planning 概念完整，上手快 | 更偏任务自动化框架，迁移现有 LangGraph runtime 成本较高 | 中 | 暂不替换主线 |
| AutoGen | 中高 | 多 Agent、human input、tools、memory/RAG 能力强 | 更偏多 Agent 协作范式，不直接匹配当前 thread/run/event runtime | 中 | 作为多 Agent 阶段备选 |
| Semantic Kernel | 中 | 插件、planner、memory、企业 .NET/Python 生态 | 当前 Python Agent 能力仍在演进；迁移成本较高 | 中低 | 暂不作为主线 |

### Runtime / Task Execution

| 方案 | 支持能力 | 优势 | 限制 | LearnAgent 仍需设计 | 结论 |
|---|---|---|---|---|---|
| 当前 `ExecutionEngine` | 本地 `asyncio.Task`、queued/running/waiting/cancelled/completed/failed、SSE queue、`run_timeout_seconds`、全局限流、`waiting_approval` rehydrate | 与 EventStore、REST、SSE 完全匹配，是唯一 runtime task 管理入口 | 仅单进程；缺 retry、幂等；`running` 态重启仍 failed | run 状态机、事件语义、幂等、全量 durable resume | 短期继续深化 |
| LangGraph durable execution | checkpoint、resume、持久化执行 | 与 LangGraph 原生一致 | 不直接提供 LearnAgent REST run API 和 timeline schema | 事件落库、SSE 兼容、cancel/approval 对外协议 | 逐步接入 |
| LangGraph Platform | threads/runs、托管 runtime | 产品化能力强 | 引入托管平台和部署模型，偏离当前本地目标 | 本地模式下仍需 runtime contract | 后续评估 |
| Temporal | durable workflow、重试、长任务、恢复 | 任务编排成熟 | 引入 worker/server，复杂度高；不是 Agent 事件模型 | Agent event、tool audit、LLM stream 映射 | 生产化后备选 |
| Celery | 分布式任务、retry、队列、revoke | Python 生态成熟 | 不提供 graph checkpoint、approval resume、LLM event stream | runtime 状态机和事件协议 | 后续外部队列备选 |

### LLM Provider / Router

| 方案 | 支持能力 | 优势 | 限制 | LearnAgent 仍需设计 | 结论 |
|---|---|---|---|---|---|
| `ChatOpenAI` | OpenAI-compatible chat、streaming、tool binding | 当前已用，兼容 DeepSeek/OpenAI-compatible | 路由、fallback、成本统计较弱 | provider metadata、事件统计 | 当前主线 |
| LiteLLM | 多模型路由、fallback、cost、proxy、虚拟 key | 覆盖模型网关常见能力 | 引入 proxy 或额外 SDK 层；需适配 LangChain/LangGraph | cost 写入 EventStore、模型策略 | 后续优先 PoC |
| OpenAI SDK | 官方模型调用、streaming、responses/agents 能力 | 官方支持强 | 非 OpenAI-compatible 多厂商路由需另做 | 与现有 LangChain tool binding 的边界 | 可作为低层 provider |
| 当前 `LLMProvider` | 封装 settings 和模型元数据 | 简单、低耦合 | 目前只是 thin adapter | fallback、cost、prompt version | 持续演进 |

### Tool System

| 方案 | 支持能力 | 优势 | 限制 | LearnAgent 仍需设计 | 结论 |
|---|---|---|---|---|---|
| LangChain `StructuredTool` | schema、tool calling adapter | 当前已用，LangGraph `ToolNode` 兼容 | 不负责风险等级、审计协议、审批策略 | tool governance schema | 当前执行层主线 |
| LangGraph `ToolNode` | 图节点执行工具调用 | 与 graph 循环匹配 | 不定义产品级工具目录 | tool metadata、tool result protocol | 当前主线 |
| MCP | 标准化外部工具/资源协议 | 适合未来接 IDE、文件、终端、浏览器等外部能力 | 当前项目工具较少，引入过早会增加复杂度 | registry 映射、权限、审计 | 未来扩展方向 |
| CrewAI Tools | CrewAI 生态工具体系 | 与 CrewAI flows/agents 配合好 | 不匹配当前 LangGraph ToolNode 主线 | 跨框架工具适配 | 暂不采用 |
| 当前 `ToolRegistry` | name、schema、category、risk、approval、timeout、audit metadata | 贴合 EventStore 和 PolicyRegistry | 还没有统一结果协议、timeout/retry enforcement | `ToolResultProtocol`、执行策略 | 继续深化 |

### Planning

| 方案 | 支持能力 | 优势 | 限制 | LearnAgent 仍需设计 | 结论 |
|---|---|---|---|---|---|
| LangGraph StateGraph | 显式状态机、条件边、循环 | 与当前 agent graph 完全匹配 | 不自动定义 plan 生命周期 | `plan_created/plan_updated` schema | 当前主线 |
| CrewAI Planning | 面向任务/crew 的 planning | 概念清晰 | 与当前 LangGraph runtime 不直接兼容 | plan event 与 run timeline 映射 | 参考 |
| Semantic Kernel Planners | 插件组合规划 | 适合 SK plugin 生态 | 当前官方也强调函数调用对 planner 的替代场景 | 迁移成本高 | 参考 |
| 当前 observe-only planner | 写 `plan_created`，不改变执行语义 | 低风险、可观测 | 还不是真正 Plan-and-Execute | plan state、plan update、step outcome | 短期继续 |

### Memory

| 方案 | 支持能力 | 优势 | 限制 | LearnAgent 仍需设计 | 结论 |
|---|---|---|---|---|---|
| LangGraph memory/checkpoint | thread state、checkpoint、long-term store | 与 graph 原生集成 | 不直接统一 RAG/EventStore/timeline summary | memory orchestration policy | 当前基础能力 |
| Zep | Agent memory、session memory、长期记忆 | 产品化 memory 方向成熟 | 额外服务依赖；数据模型需适配 | 与 EventStore/checkpoint 的一致性 | 后续 PoC |
| Mem0 | 用户记忆、偏好记忆、跨会话 recall | API 简洁，生态活跃 | 仍需治理写入和召回策略 | memory 写入/遗忘/压缩规则 | 后续 PoC |
| CrewAI Memory | unified memory、scope、recall | 框架内 memory 体验完整 | 引入 CrewAI 运行模型 | 与 LangGraph runtime 融合成本 | 参考 |
| Semantic Kernel Memory | memory connector/provider 思路 | 企业生态强 | Python Agent memory 仍需验证 | 接入成本不确定 | 参考 |
| 当前 `MemoryManager` | RAG、EventStore、checkpoint path、deterministic run/thread summary | 贴合项目数据源，不新增外部依赖或 schema migration | 还没有 episodic search、compression | recall、污染控制 | 继续设计 |

### Guardrail / Policy

| 方案 | 支持能力 | 优势 | 限制 | LearnAgent 仍需设计 | 结论 |
|---|---|---|---|---|---|
| OpenAI Agents guardrails | input/output guardrail、agent 集成 | 与 Agents SDK 配合好 | 当前主线不是 Agents SDK | REST approval 与 tool policy 映射 | 参考 |
| NeMo Guardrails | 对话安全、rails、actions | guardrail DSL 较完整 | 引入配置体系和运行层 | 工具风险、HTTP 白名单、审批语义 | 后续评估 |
| Guardrails AI | 输出结构、校验、rail spec | 适合输出校验 | 不覆盖完整 runtime policy | tool/input/output 分层策略 | 输出校验备选 |
| OPA | 通用策略引擎 | 策略表达强，适合权限/合规 | 对 Agent tool semantics 没有内建理解 | 数据模型和策略输入 | 生产权限阶段评估 |
| Presidio | PII 检测和脱敏 | PII 能力明确 | 只覆盖敏感信息检测 | secret/cookie/tool args 规则 | 可集成 |
| 当前 `PolicyRegistry` | 危险工具判断、approval decision | 贴合当前工具和 EventStore | 还没有 PII、输入/输出校验、策略版本 | policy schema、decision audit | 继续设计 |

### Observability

| 方案 | 支持能力 | 优势 | 限制 | LearnAgent 仍需设计 | 结论 |
|---|---|---|---|---|---|
| Langfuse | LLM trace、span、tool trace | 当前已有接入基础 | 不是 runtime event store | trace/event correlation | 保留 |
| LangSmith | LangChain/LangGraph observability | 与 LangChain 生态匹配 | 引入外部服务和账号体系 | 与本地 SQLite timeline 对齐 | 可选 |
| OpenTelemetry | 标准 tracing/metrics/logs | 生产标准 | 需要大量 instrumentation | span 命名、attribute schema | 生产阶段引入 |
| OpenAI Agents tracing | Agents SDK 内置 trace | 覆盖 LLM、tool、guardrail、自定义事件 | 主线不是 Agents SDK | 数据同步到 EventStore | 参考 |
| 当前 EventStore timeline | thread/run/event 可回放事实源 | 完全服务本地 runtime | 不是指标系统；没有聚合 dashboard | trace correlation、metrics export | 当前主线事实源 |
| `TimelineProjector` | CQRS / Projection Read Model，将 raw events 投影为 run timeline | 保持 EventStore 为事实源，适合 UI/API 查询 | 当前为即时投影，未做缓存表或复杂查询优化 | timeline schema、warning 规则、后续 read model cache | MVP 高优先级 |

### Sandbox

| 方案 | 支持能力 | 优势 | 限制 | LearnAgent 仍需设计 | 结论 |
|---|---|---|---|---|---|
| Docker | 容器隔离、资源限制 | 通用、易部署 | 安全边界依赖配置；Windows 本地体验需验证 | tool permission、mount、audit | 文件/终端工具优先候选 |
| E2B | 代码执行 sandbox | Agent 场景友好 | 外部服务依赖 | 本地模式和审计集成 | 可选 |
| gVisor | 强化容器隔离 | 安全性更强 | 部署复杂度高 | 与本地 Windows 开发不直接匹配 | 生产阶段评估 |
| Firecracker | microVM 隔离 | 强隔离 | 运维复杂度高 | 调度和文件访问协议 | 远期评估 |
| 当前实现 | 无完整文件/终端沙箱 | 简单 | 不能安全开放任意文件/终端工具 | sandbox policy、执行审计、资源限制 | 明确缺口 |

### UI / Control Channel

| 方案 | 支持能力 | 优势 | 限制 | LearnAgent 仍需设计 | 结论 |
|---|---|---|---|---|---|
| FastAPI SSE | 单向事件流 | 当前 `/v1/chat` 兼容，简单稳定 | 不支持客户端实时控制消息 | 与 run timeline 对齐 | 当前保留 |
| FastAPI WebSocket | 双工控制 | 适合 timeline、approval、cancel、token 同步 | 需要定义协议和重连语义 | websocket event protocol | 下一阶段 |
| LangGraph frontend HITL patterns | approval UI 思路 | 与 LangGraph interrupt 匹配 | 当前 UI 是自建 runtime 控制台 | API 映射 | 参考 |
| 当前 timeline UI | 本地 run 创建、投影 timeline 查看、cancel、approve、reject | 贴合 EventStore 和 `TimelineProjector` | 还不是完整产品 UI | timeline 交互和 WebSocket | 持续演进 |

## 4. LearnAgent 当前选择与优化方向

> 对照代码与各 `*-design.md`（截至 2026-05）。「当前选择」为**已接入主线**；「优化方向」为下一阶段演进，不再写「保持/下一步」式判断。

| 模块 | 当前选择（已落地） | 优化方向 |
|---|---|---|
| Agent orchestration | LangGraph `StateGraph`（`assistant → safety_gate → tools`）；危险工具 `interrupt()` + `Command(resume)` 续跑 | 显式 `plan_updated` / 步骤 outcome；Plan-and-Execute PoC；Multi-Agent 非 MVP |
| 数据流契约 | `contracts/`：`RuntimeEvent` envelope、`ToolResultModel`、Adapter（SSE/EventStore/HTTP/RAG）、`validate` | 更多 `kind` 的 Pydantic 子模型；可选 `GET /events?validated=1`；见 [data-flow-design.md](./data-flow-design.md) §8 |
| LLM | `ChatOpenAI`（OpenAI-compatible）+ 薄 `LLMProvider`；`MAX_LLM_INFLIGHT` 限流 | LiteLLM PoC（路由/fallback）；token/cost 写入 run 事件或 metrics |
| Tool | `StructuredTool` + `ToolNode` + `ToolRegistry`；`tool_start/tool_end` 审计 v1、脱敏 | `timeout_seconds` 执行层强制；失败路径统一 envelope；MCP registry PoC |
| Runtime | FastAPI + `ExecutionEngine` + SQLite EventStore；`run_state` FSM、`TimelineProjector`；超时/并发槽、`waiting_approval` rehydrate；`schema_version: 1`、事件分页、`run_*_meta` | `running`/`queued` 重启 durable resume；Run 幂等键；见 [runtime-design.md](./runtime-design.md) §8–9 |
| Memory | LangGraph checkpoint + RAG + EventStore `memory_*` 摘要；v1.1 policy（keyword episodic、budget、conflict）；`current_turn_messages`；`CheckpointCompactor` | 向量 episodic；续轮去重 System/episodic 注入；可选 LLM 压缩摘要；见 [memory-checkpoint-design.md](./memory-checkpoint-design.md) §8 |
| RAG | 9 篇 Demo 语料 + BM25/RRF/可选向量；Scenario `docs_manifest.json`；结构化 API ingest；Tool-grounded + proxy eval | RAGAS 夜跑；生产语料替换；见 [rag-design.md](./rag-design.md) §10 |
| Policy | `PolicyRegistry` + Scenario HTTP 白名单 + **`required_scopes`** + `safety_gate` + credential audit | 策略表版本化；输入/输出 Guard；见 [guardrail-policy-design.md](./guardrail-policy-design.md) |
| Observability | EventStore 为 timeline 事实源；Langfuse trace/span（LLM/tool） | `trace_id` 与 `run_id`/`tool_call_id` 写入事件；token/cost/latency 聚合；OpenTelemetry 生产阶段 |
| UI / 控制通道 | REST + SSE（`/v1/chat` 兼容）；`/ui` timeline；`GET /timeline`；`WS /v1/runs/{id}/ws` 回放 | WS 重连与增量事件协议；长 Run 默认 cursor 拉取；完整产品 UI 后置 |
| Eval（横切） | `verify_eval_suite`；profile 见 [ci-design](./ci-design.md) | 见 [guide §2.8](./agent-learning-guide.md) |

## 5. 需自建的产品语义与项目实现

这些能力开源框架不会替 LearnAgent 定稿。下表：**为何要自建**、**最小设计目标**；**实现快照**见 [agent-learning-guide §3](./agent-learning-guide.md) 与各 `*-design.md` **§0**；**全局缺口**见 [guide §2.8](./agent-learning-guide.md)。

| 需设计能力 | 为什么不能直接交给框架 | 最小设计目标 | 实现快照 |
|---|---|---|---|
| Runtime 事件契约 | LangGraph/Temporal 有状态，但 REST/SSE/timeline 的事件类型与 payload 是本项目 API | 固定 Run/Event 状态机；payload schema 与版本；可回放、可分页查询 | [runtime §0](./runtime-design.md)、[data-flow §0](./data-flow-design.md) |
| Approval / cancel 语义 | 框架有 interrupt/revoke，但 approve/reject/cancel 与 Run 行、timeline 的映射需自建 | `approval_*` / `cancel_*` 与 Run 状态一致落库并可复盘 | [guardrail §0](./guardrail-policy-design.md)、[runtime §0](./runtime-design.md) |
| Tool 治理 schema | 工具框架不管业务风险等级、审批规则、审计元数据 | `ToolSpec` 含 risk、**required_scopes**、approval、timeout、audit | [guardrail §0](./guardrail-policy-design.md) |
| Tool 结果协议 | 各工具返回结构不一，LangChain 不强制审计格式 | 统一 `ToolResultModel`、脱敏、`tool_call_id` 进 timeline | [data-flow §0](./data-flow-design.md) |
| Memory 编排策略 | 框架不管何时写摘要、何时召回、如何压缩 working memory | episodic 召回；checkpoint 为 working 真相源 | [memory-checkpoint §0](./memory-checkpoint-design.md)、[context-manager §0](./context-manager-design.md) |
| RAG / 文档问答 | 框架不管业务文档集、引用溯源与检索评测 | 混合检索；required_source proxy 指标 | [rag §0](./rag-design.md)、[tool-design §0](./tool-design.md) |
| Trace 关联 | Langfuse 与 EventStore 是两套 ID | `thread_id`/`run_id`/`tool_call_id`/`trace_id` 可互查 | [observability §0](./observability-design.md) |
| Sandbox 策略 | 容器只提供隔离，权限/审计语义仍要自建 | shell/git 类工具 permission + 审计 | 未实现；HTTP 白名单见 [guardrail §0](./guardrail-policy-design.md) |

| 模块边界与改造优先级见 [agent-learning-guide.md](./agent-learning-guide.md) §3、**§7（八层栈）**。分项设计见 [runtime-design.md](./runtime-design.md)、[rag-design.md](./rag-design.md)、[guardrail-policy-design.md](./guardrail-policy-design.md)、[observability-design.md](./observability-design.md)、[memory-checkpoint-design.md](./memory-checkpoint-design.md)、[context-manager-design.md](./context-manager-design.md)、[data-flow-design.md](./data-flow-design.md)、[eval-design.md](./eval-design.md)、[ci-design.md](./ci-design.md)。

## 6. 参考资料

- [LangGraph interrupts / human-in-the-loop](https://docs.langchain.com/oss/python/langgraph/human-in-the-loop)
- [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph)
- [OpenAI Agents SDK tracing](https://openai.github.io/openai-agents-python/tracing/)
- [OpenAI Agents SDK guardrails](https://openai.github.io/openai-agents-python/ref/guardrail/)
- [CrewAI Flows](https://docs.crewai.com/en/concepts/flows)
- [CrewAI Memory](https://docs.crewai.com/en/concepts/memory)
- [Semantic Kernel Agent Framework](https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/)
- [Semantic Kernel Agent Memory](https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/agent-memory)
- [AutoGen AgentChat](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/index.html)
- [AutoGen Memory and RAG](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/memory.html)
- [LiteLLM docs](https://docs.litellm.ai/)
- [Temporal docs](https://docs.temporal.io/)
- [Celery tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
