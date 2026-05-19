# LearnAgent

LearnAgent 是一个用于学习和演进产品级 Agent Runtime 的 FastAPI + LangGraph 示例项目。当前项目采用的主线框架是：

```text
ChatOpenAI + LangGraph + SQLite/EventStore + RAG + StructuredTool + ExecutionEngine
```

它已经具备 thread、run、event timeline、checkpoint、RAG、工具调用、安全闸门、后台 run 管理、approval 和基础 timeline UI。它还不是完整 SaaS 产品：多用户权限、生产级沙箱、外部任务队列、部署编排、完整 Memory Manager 仍属于后续演进。

## 1. Project Overview

LearnAgent 的目标不是做一个通用聊天机器人，而是把产品级 Agent 系统拆成清晰模块，并逐步实现每个模块的工程边界：

- LLM：推理核心，负责理解输入、调用工具、生成输出。
- Planning：规划与行动编排，当前使用 LangGraph 实现 ReAct-like 流程。
- Memory：工作记忆、语义记忆、事件记忆的组合。
- Tool：工具定义、参数校验、白名单和调用适配。
- Execution Engine：run 生命周期、后台任务、cancel、approval 和事件流。
- Guardrail：安全策略、危险动作拦截、工具边界和用户确认。
- Observability：trace、span、事件时间线和工具审计。
- Memory Manager：后续抽象，用于统一管理摘要、召回和长期记忆。

## 2. Current Capabilities

- **Thread / Run / Event Store**：使用 SQLite 持久化线程、运行记录和可回放事件时间线。
- **SSE 兼容接口**：保留 `POST /v1/chat`，继续输出 `meta`、`token`、`tool_start`、`tool_end`、`done`、`error` 等事件。
- **后台 Run 管理**：支持创建后台 run、查询状态、查询事件、取消 run。
- **Approval 工作流**：危险工具调用进入 `waiting_approval`，可通过 API approve 或 reject。
- **LangGraph + Checkpoint**：对话状态使用 LangGraph 编排，并通过 SQLite checkpoint 持久化。
- **RAG**：从本地文档构建知识检索，支持关键词检索和可选向量检索。
- **工具调用审计**：工具开始和结束事件会记录工具名、call id、参数、结果、耗时和成功状态。
- **Safety Gate**：危险 `POST /api/v1/jobs/watermark` 需要部署开关和用户确认。
- **Timeline UI**：`/ui/` 提供本地 runtime 控制台，用于创建 run、查看事件、cancel、approve 和 reject。

## 3. Agent Architecture Mapping

### LLM

**模块职责**：LLM 是推理核心，负责理解用户输入、结合上下文做决策、选择工具、生成最终回复。

**当前实现**：`copilot_agent/agent/runner.py` 使用 `langchain-openai.ChatOpenAI`，通过 OpenAI-compatible 配置接入 DeepSeek 或 OpenAI 兼容服务。

**当前采用工具**：`ChatOpenAI`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`。

**下一步细化方向**：`LLMProvider` 已作为薄适配层存在；后续需要设计模型路由、fallback、token/cost 统计、prompt 版本管理。未来可接 `LiteLLM` 作为多模型网关。

### Planning

**模块职责**：Planning 负责把目标拆成可执行步骤，并在执行过程中根据工具结果动态调整。

**当前实现**：`copilot_agent/agent/graph.py` 使用 LangGraph 编排 `assistant -> safety_gate -> tools -> assistant`，属于 ReAct-like 循环。

**当前采用工具**：`LangGraph StateGraph`、`ToolNode`、LangGraph checkpoint。

**下一步细化方向**：当前已有 observe-only `planner` 节点和 `plan_created` 事件；后续需要设计 `plan_updated`、plan step schema 和 Plan-and-Execute 流程。

### Memory

**模块职责**：Memory 解决 LLM 无状态问题，为当前任务、历史轨迹和领域知识提供上下文。

**当前实现**：

- Working Memory：LangGraph messages + SQLite checkpoint。
- Semantic Memory：`copilot_agent/rag/` 文档检索。
- Episodic Memory：`EventStore` 中的 run/event timeline 初版。

**当前采用工具**：SQLite checkpoint、SQLite EventStore、关键词 RAG、可选 Chroma vector RAG。

**下一步细化方向**：`MemoryManager` v1 已统一 checkpoint path、RAG、EventStore，并基于 timeline 生成 deterministic run/thread summary；后续需要设计 episodic search 和 working memory compression。

### Tool

**模块职责**：Tool 是 Agent 与外部世界交互的接口，负责工具注册、参数 schema、调用适配和结果规范化。

**当前实现**：`copilot_agent/tools/registry.py` 使用 `ToolRegistry` 和 `ToolSpec` 注册 `search_docs`、`http_get`、`http_post`，并向 LangGraph 输出 `StructuredTool`。

**当前采用工具**：LangChain `StructuredTool`、Pydantic schema、`httpx`、`copilot_agent/tools/whitelist.py`。

**下一步细化方向**：继续深化 `ToolRegistry`，补充统一结果协议、工具版本、工具级 timeout/retry enforcement；未来可接 MCP。

### Execution Engine

**模块职责**：Execution Engine 把 LLM 的决策转成真实执行，负责 run 生命周期、任务调度、取消、错误处理和事件输出。

**当前实现**：`copilot_agent/runtime/execution_engine.py` 使用本地 `asyncio.Task` 管理 active run、cancel、approval、stream queue；`RunManager` 保留为兼容别名。

**当前采用工具**：FastAPI、asyncio、SQLite EventStore、SSE、WebSocket event stream。

**下一步细化方向**：增加 timeout、retry、并发限制、幂等键、失败降级、run 恢复；未来可替换为 Temporal、Celery 或其他外部任务系统。

### Guardrail

**模块职责**：Guardrail 负责输入、工具、输出和权限边界，防止越权调用、危险动作和敏感信息泄露。

**当前实现**：`safety_gate` 拦截危险 `http_post`，HTTP path whitelist 防止任意 URL 访问，cookie 和 set-cookie 会脱敏。

**当前采用工具**：Pydantic、HTTP whitelist、自研 safety gate、`COPILOT_ALLOW_JOB_POST`、run approval。

**下一步细化方向**：`PolicyRegistry` 已承接当前危险工具审批判断；后续需要设计输入校验、输出校验、工具参数策略、secret/PII 检测、approval policy 和权限分级。

### Observability

**模块职责**：Observability 负责记录 Agent 每一步发生了什么，支撑调试、审计、评估和线上排障。

**当前实现**：Langfuse trace/span 记录 LLM 和 tool 链路，EventStore 记录 runtime timeline，`tool_start/tool_end` 记录工具审计。

**当前采用工具**：Langfuse、Python logging、SQLite EventStore。

**下一步细化方向**：统一 `thread_id/run_id` correlation，增加 token/cost/latency metrics，后续接 OpenTelemetry、Prometheus、Sentry。

### Memory Manager

**模块职责**：Memory Manager 是 Memory 的治理层，负责何时写入、何时召回、何时摘要、何时遗忘。

**当前实现**：`copilot_agent/memory/manager.py` 已提供 v1 memory facade，统一持有 RAG、EventStore 和 checkpoint path，并生成 `memory_run_summary` / `memory_thread_summary` 派生事件。

**当前采用工具**：SQLite checkpoint、EventStore 和 RAG。

**下一步细化方向**：继续深化 `MemoryManager`，实现 episodic search、semantic retrieval 编排和 working memory compression。未来可评估 LangMem、Zep、Mem0。

## 4. Runtime Architecture

```text
Client / Browser
    |
    | REST / SSE / WebSocket event stream
    v
FastAPI server
    |
    +--> ExecutionEngine
    |       |
    |       +--> asyncio.Task per active run
    |       +--> cancel / approve / reject
    |       +--> stream queue for /v1/chat compatibility
    |
    +--> EventStore (SQLite)
    |       |
    |       +--> threads
    |       +--> runs
    |       +--> events
    |
    +--> ChatRunner
            |
            +--> LangGraph assistant -> safety_gate -> tools -> assistant
            +--> RAG search_docs
            +--> http_get / http_post whitelist tools
            +--> LangGraph SQLite checkpoint
```

核心思路是：`ExecutionEngine` 管理运行生命周期，`ChatRunner` 负责 Agent 执行，`EventStore` 负责把 runtime 过程中发生的事情落成可查询、可回放的 timeline。

关键代码模块：

- `copilot_agent/server.py`：FastAPI 入口，提供 chat、thread、run、event、cancel、approval、WebSocket API。
- `copilot_agent/runtime/event_store.py`：SQLite event store，管理 `threads`、`runs`、`events`。
- `copilot_agent/runtime/execution_engine.py`：本地执行引擎，负责 active run、取消、approval 暂停/恢复和 SSE 兼容流。
- `copilot_agent/runtime/run_manager.py`：兼容旧导入名的薄包装，后续新代码应优先使用 `ExecutionEngine`。
- `copilot_agent/runtime/timeline.py`：CQRS / Projection Read Model，将 raw events 投影为 UI/API 使用的 run timeline。
- `copilot_agent/agent/runner.py`：Agent 执行器，连接 LLM、LangGraph、RAG、工具、安全闸门和事件输出。
- `copilot_agent/agent/graph.py`：LangGraph 状态图，编排 `assistant -> safety_gate -> tools -> assistant`。
- `copilot_agent/rag/`：文档加载、切分、关键词检索、向量索引和混合检索。
- `copilot_agent/tools/`：受控 HTTP 工具，只允许访问白名单 API。

## 5. Data Model

默认事件数据库：

```text
storage/learnagent-events.sqlite
```

可通过环境变量覆盖：

```env
AGENT_EVENT_STORE_PATH=storage/learnagent-events.sqlite
```

### Thread

thread 表示一条长期会话。`/v1/chat` 中的旧 `conversation_id` 现在等价于 `thread_id`，以兼容旧客户端。

最小字段：

- `id`
- `title`
- `status`
- `created_at`
- `updated_at`

### Run

run 表示一次 Agent 执行。一个 thread 可以有多个 run。

当前状态：

- `queued`
- `running`
- `waiting_approval`
- `cancelling`
- `cancelled`
- `completed`
- `failed`

### Event

event 是 run 的可回放时间线。常见事件：

- `run_created`
- `run_started`
- `token`
- `tool_start`
- `tool_end`
- `approval_required`
- `approval_resolved`
- `cancel_requested`
- `cancelled`
- `done`
- `error`

对外 API 会把数据库里的 `payload_json` 解析成 `payload` 对象返回。

## 6. Public API

### Chat SSE

```http
POST /v1/chat
```

请求：

```json
{
  "conversation_id": "optional-old-id",
  "thread_id": "optional-thread-id",
  "messages": [
    {"role": "user", "content": "Java API 是否存活？"}
  ],
  "confirm_dangerous": false
}
```

响应为 SSE。`meta` 事件会返回：

```json
{
  "conversation_id": "...",
  "thread_id": "...",
  "run_id": "..."
}
```

### Thread / Run

```http
POST /v1/threads
GET  /v1/threads/{thread_id}
GET  /v1/threads/{thread_id}/runs
GET  /v1/threads/{thread_id}/events
GET  /v1/threads/{thread_id}/events?run_id={run_id}
```

创建后台 run：

```http
POST /v1/threads/{thread_id}/runs
```

```json
{
  "messages": [
    {"role": "user", "content": "检查部署状态"}
  ],
  "confirm_dangerous": false
}
```

查询 run 和事件：

```http
GET /v1/runs/{run_id}
GET /v1/runs/{run_id}/events
GET /v1/runs/{run_id}/timeline
GET /v1/runs/{run_id}/ws
```

### Cancel / Approval

```http
POST /v1/runs/{run_id}/cancel
POST /v1/runs/{run_id}/approve
POST /v1/runs/{run_id}/reject
```

cancel 是 cooperative cancellation：runtime 会标记取消、取消后台 task，并写入 `cancel_requested` 和 `cancelled` 事件。已经完成、失败或取消的 run 再 cancel，不会重复写终态事件。

approval 当前只用于 safety gate 拦截到的危险工具调用。approve 后会用同一组 messages 带确认标记重新执行；reject 会写入拒绝说明并完成 run。当前版本不做 LangGraph node-level resume。

## 7. Module Roadmap

模块级技术选型对比见 [docs/agent-runtime-tech-selection.md](docs/agent-runtime-tech-selection.md)。该文档区分“可直接采用”“可集成但需要适配”和“需要 LearnAgent 设计”的能力，避免把开源框架已有能力误判为项目缺口。

| 模块 | 当前工具 | 下一步抽象 | 产品级方向 |
|---|---|---|---|
| LLM | `ChatOpenAI` | `LLMProvider` | LiteLLM、多模型路由、fallback、成本统计 |
| Planning | LangGraph ReAct-like | `planner` 节点 | Plan-and-Execute、plan event、动态修正 |
| Memory | checkpoint + RAG + EventStore summary | `MemoryManager` | 召回、episodic search、长期记忆治理 |
| Tool | `StructuredTool` | `ToolRegistry` | MCP、权限等级、工具版本、统一结果协议 |
| Execution | `ExecutionEngine + asyncio` | runtime policy | timeout、retry、限流、幂等、外部任务队列 |
| Guardrail | safety gate + whitelist | `PolicyRegistry` | PII/secret 检测、输入输出校验、approval policy |
| Observability | Langfuse + EventStore | Trace correlation | OpenTelemetry、Prometheus、Sentry、cost dashboard |

自动优化完成状态：

| 项目 | 状态 | 验证方式 |
|---|---|---|
| `ToolRegistry` | 已独立工具注册和工具元数据，`ChatRunner` 只传入工具实现函数 | `verify_architecture_adapters.py` |
| `MemoryManager` | 已作为 v1 facade 统一 RAG、EventStore、checkpoint path、run summary 和 thread summary | `verify_memory_v1.py`、`verify_architecture_adapters.py` |
| `LLMProvider` | 已封装 OpenAI-compatible `ChatOpenAI` 初始化和 provider metadata | `verify_architecture_adapters.py` |
| `PolicyRegistry` | 已承接危险工具审批判断，保持 safety gate 行为兼容 | `verify_architecture_adapters.py`、`verify_phase3_safety_gate.py` |
| Planning observe node | 已增加 observe-only `planner` 和 `plan_created` event，不改变 ReAct 执行语义 | `verify_architecture_adapters.py` |
| `ExecutionEngine` | 已从 `RunManager` 抽出本地后台执行引擎，`RunManager` 保留为兼容别名 | `verify_runtime_run_manager.py` |
| `TimelineProjector` | 已采用 CQRS / Projection Read Model：EventStore 作为事实源，`/v1/runs/{run_id}/timeline` 返回面向 UI 的聚合 timeline | `verify_runtime_timeline.py` |
| Tool Audit v1 | 已增加 `ToolResult` audit envelope、工具 payload sanitizer、`tool_start/tool_end` 审计契约验证 | `verify_tool_audit_v1.py` |

剩余更深层能力不在自动优化清单中，因为它们会改变产品语义或 runtime contract，需要单独设计后再实现。

已完成模块的不足和后续优化方向记录在 [docs/agent-runtime-tech-selection.md](docs/agent-runtime-tech-selection.md) 的 `Completed Module Gaps And Optimization Directions` 章节。当前重点仍是先跑通 MVP：已有模块只补影响可用性、可复盘性和安全审计的最小增强，复杂外部框架接入放到后续 PoC。

### MVP 高优先级问题

当前目标是快速跑通 MVP，然后再迭代增强。下一步最关键模块不是继续扩展外部框架，而是把现有 `ExecutionEngine + EventStore + MemoryManager v1 + Timeline UI` 的闭环打磨稳定。

| 优先级 | 模块 / 问题 | 为什么影响 MVP | 下一步动作 |
|---|---|---|---|
| 高 | Runtime / Timeline 闭环 | MVP 必须能稳定创建 run、查看 timeline、cancel、approve/reject，并复盘事件 | 完成一次端到端手工验收脚本：创建 thread/run、观察 SSE/UI timeline、验证 memory summary 事件 |
| 高 | Memory v1 迭代 | 当前已有 run/thread summary，MVP 需要确认它不会污染当前回答，同时能提供跨 run 上下文 | 验证新 run 能读取 thread summary；增加 summary preview 和独立 memory 回归用例 |
| 高 | Tool audit / result consistency | MVP 的可信度依赖工具调用是否可追踪、是否脱敏、是否能定位失败 | 固化 `tool_start/tool_end` 字段检查，补充失败工具调用的审计验证 |
| 中 | Approval/cancel 语义 | 当前可用但仍是重新执行式 approval，MVP 阶段只需稳定和可解释 | 文档中明确当前语义；暂不做 LangGraph node-level resume |
| 中 | Observability correlation | MVP 排障需要按 `thread_id/run_id` 查事件，但完整 trace/cost dashboard 可后置 | 保持 EventStore 为事实源，后续再接 OpenTelemetry/metrics |
| 低 | 外部 memory / 外部队列 / 多用户权限 | 会增加复杂度，不是跑通单用户本地 MVP 的必要条件 | 暂缓，只保留调研入口 |

## 8. Local Development

建议使用 Python 3.12。Dockerfile 和 CI 都以 Python 3.12 作为当前项目基线。

Conda 环境：

```powershell
cd E:\code\LearnAgent
conda create -n learnagent312 python=3.12 -y
conda activate learnagent312
python -m pip install -U pip
python -m pip install -r requirements.txt
```

如果本机已经安装了 Python 3.12，也可以使用 venv：

```powershell
cd E:\code\LearnAgent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
```

配置 `.env`：

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=deepseek-chat
OPENAI_BASE_URL=https://api.deepseek.com/v1

WATERMARK_API_BASE_URL=http://127.0.0.1:8080

AGENT_CHECKPOINT_PATH=storage/langgraph-checkpoints.sqlite
AGENT_EVENT_STORE_PATH=storage/learnagent-events.sqlite

RAG_USE_VECTOR=false
RAG_REBUILD_INDEX=false
RAG_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
HF_HOME=F:\model

COPILOT_ALLOW_JOB_POST=false
LANGFUSE_ENABLED=false
```

启动服务：

```powershell
cd E:\code\LearnAgent
uvicorn copilot_agent.server:app --host 0.0.0.0 --port 8090
```

访问：

- Health: `http://127.0.0.1:8090/health`
- Runtime UI: `http://127.0.0.1:8090/ui/`

## 9. Verification

MVP runtime acceptance：

```powershell
python scripts\verify_mvp_runtime_acceptance.py --event-store-path storage\verify-mvp-runtime-events.sqlite --checkpoint-path storage\verify-mvp-runtime-checkpoints.sqlite
```

该脚本包含 deterministic runtime 闭环和一次真实 `hello agent` `/v1/chat` + LLM agent loop；因此需要 `.env` 中配置可用的 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。

Runtime event store：

```powershell
python scripts\verify_runtime_event_store.py --event-store-path storage\verify-runtime-events.sqlite
```

Memory v1：

```powershell
python scripts\verify_memory_v1.py --event-store-path storage\verify-memory-v1-events.sqlite --checkpoint-path storage\verify-memory-v1-checkpoints.sqlite
```

后台 run、cancel、approval：

```powershell
python scripts\verify_runtime_run_manager.py --event-store-path storage\verify-run-manager-events.sqlite
```

Runtime timeline projection:
```powershell
python scripts\verify_runtime_timeline.py --event-store-path storage\verify-runtime-timeline-events.sqlite
```

Tool audit v1:
```powershell
python scripts\verify_tool_audit_v1.py --event-store-path storage\verify-tool-audit-events.sqlite
```

Python 编译检查：

```powershell
python -m compileall copilot_agent scripts
```

LangGraph checkpoint 和 safety gate 回归：

```powershell
python scripts\verify_phase3_checkpoint.py
python scripts\verify_phase3_safety_gate.py
```

RAG / eval 验证：

```powershell
python scripts\build_index.py
python scripts\verify_phase4_dataset.py
python scripts\verify_phase4_ragas.py --mode proxy --disable-vector
```

## Runtime State Diagram

```mermaid
stateDiagram-v2
    direction TB

    state Thread {
        [*] --> active
        active --> ended: browser close
        active --> archived: idle 10m
        ended --> archived: auto TTL cleanup
    }

    state Run {
        [*] --> queued
        queued --> running
        running --> waiting_approval
        waiting_approval --> running
        waiting_approval --> completed: reject
        running --> cancelling
        cancelling --> cancelled
        running --> completed
        running --> failed
    }

    state GraphNode {
        [*] --> planner
        planner --> assistant
        assistant --> safety_gate: tool_calls
        assistant --> [*]: no tool_calls
        safety_gate --> tools: allowed tool_calls
        safety_gate --> [*]: blocked / no tool_calls
        tools --> assistant
    }

    active --> Run: user sends message
    running --> GraphNode: execute one run
```
