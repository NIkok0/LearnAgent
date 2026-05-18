# LearnAgent

LearnAgent 是一个用于学习和演进产品级 Agent Runtime 的 FastAPI + LangGraph 示例项目。它从一个简单的 SSE 聊天接口，逐步升级为具备 thread、run、event timeline、checkpoint、RAG、工具调用、安全闸门、后台任务管理和 approval 工作流的本地单用户 Agent 系统。

当前项目仍然不是完整 SaaS 产品：没有多用户权限系统、没有 WebSocket、没有生产级文件/终端沙箱，也没有云端部署编排。它的目标是把 Agent Runtime 的核心结构先做清楚，方便继续往产品级能力演进。

## 1. 当前能力

- **Thread / Run / Event Store**：使用 SQLite 持久化线程、运行记录和可回放事件时间线。
- **SSE 兼容接口**：保留 `POST /v1/chat`，继续输出 `meta`、`token`、`tool_start`、`tool_end`、`done`、`error` 等事件。
- **后台 Run 管理**：支持创建后台 run、查询状态、查询事件、取消 run。
- **Approval 工作流**：危险工具调用进入 `waiting_approval`，可通过 API approve 或 reject。
- **LangGraph + Checkpoint**：对话状态使用 LangGraph 编排，并通过 SQLite checkpoint 持久化。
- **RAG**：从本地文档构建知识检索，支持关键词检索和可选向量检索。
- **工具调用审计**：工具开始和结束事件会记录工具名、call id、参数、结果、耗时和成功状态。
- **Safety Gate**：危险 `POST /api/v1/jobs/watermark` 需要部署开关和用户确认。

## 2. Runtime 架构

```text
Client / Browser
    |
    | REST / SSE
    v
FastAPI server
    |
    +--> RunManager
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

核心思路是：`RunManager` 管理运行生命周期，`ChatRunner` 负责 Agent 执行，`EventStore` 负责把 runtime 过程中发生的事情落成可查询、可回放的 timeline。

## 3. 关键模块

- `copilot_agent/server.py`：FastAPI 入口，提供 chat、thread、run、event、cancel、approval API。
- `copilot_agent/runtime/event_store.py`：SQLite event store，管理 `threads`、`runs`、`events`。
- `copilot_agent/runtime/run_manager.py`：本地后台任务管理器，负责 active run、取消、approval 暂停/恢复和 SSE 兼容流。
- `copilot_agent/agent/runner.py`：Agent 执行器，连接 LLM、LangGraph、RAG、工具、安全闸门和 SSE 事件。
- `copilot_agent/agent/graph.py`：LangGraph 状态图，编排 `assistant -> safety_gate -> tools -> assistant`。
- `copilot_agent/rag/`：文档加载、切分、关键词检索、向量索引和混合检索。
- `copilot_agent/tools/`：受控 HTTP 工具，只允许访问白名单 API。
- `scripts/verify_*.py`：无需或少依赖外部服务的结构验证脚本。

## 4. 数据模型

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

## 5. API

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
```

### Cancel / Approval

```http
POST /v1/runs/{run_id}/cancel
POST /v1/runs/{run_id}/approve
POST /v1/runs/{run_id}/reject
```

cancel 是 cooperative cancellation：runtime 会标记取消、取消后台 task，并写入 `cancel_requested` 和 `cancelled` 事件。已经完成、失败或取消的 run 再 cancel，不会重复写终态事件。

approval 当前只用于 safety gate 拦截到的危险工具调用。approve 后会用同一组 messages 带确认标记重新执行；reject 会写入拒绝说明并完成 run。当前版本不做 LangGraph node-level resume。

## 6. RAG 和工具边界

`search_docs` 会读取 `docs/source/` 下的项目文档，并返回带来源的片段。向量检索可通过 `RAG_USE_VECTOR` 开关启用或关闭。

HTTP 工具只允许访问 `copilot_agent/tools/whitelist.py` 中定义的路径。模型不能自由拼接任意 URL，避免 SSRF、越权访问和泄露 cookie。

危险动作：

```http
POST /api/v1/jobs/watermark
```

必须同时满足：

- 服务端设置 `COPILOT_ALLOW_JOB_POST=true`
- run 获得用户确认，或 `/v1/chat` 请求设置 `confirm_dangerous=true`

## 7. 本地运行

安装依赖：

```powershell
cd E:\code\LearnAgent
python -m pip install -r requirements.txt
```

配置 `.env`：

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=

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
- Minimal UI: `http://127.0.0.1:8090/ui/`

## 8. 验证命令

Runtime event store：

```powershell
python scripts\verify_runtime_event_store.py --event-store-path storage\verify-runtime-events.sqlite
```

后台 run、cancel、approval：

```powershell
python scripts\verify_runtime_run_manager.py --event-store-path storage\verify-run-manager-events.sqlite
```

Python 编译检查：

```powershell
python -m compileall copilot_agent scripts
```

LangGraph checkpoint 和 safety gate 回归需要安装 LangChain / LangGraph 相关依赖：

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

## 9. 当前限制和下一步

当前已经完成 Runtime Core 的第一版，但仍有几个明显缺口：

- WebSocket 双工通道尚未实现。
- 前端 timeline 仍是后续工作，当前只保证后端 timeline 数据完整。
- 多任务并行目前是本地单进程 `asyncio.Task`，没有外部队列和分布式 worker。
- 文件/终端沙箱还没有纳入工具系统。
- 权限模型仍是单用户本地模式，没有用户、组织、项目和 RBAC。
- approval 采用重新执行确认后的 run 逻辑，不做 LangGraph 中途 resume。
- 生产部署、监控、迁移和备份策略还没有产品化。

建议下一阶段优先做前端 timeline 和 WebSocket 控制通道，让 run 状态、事件回放、取消和 approval 变成可操作的产品界面。
