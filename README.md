# LearnAgent

LearnAgent 是一个本地单用户 Agent Runtime 项目，用来验证“可信 Agent + Tool Execution + 私有 RAG + Memory Governance”的工程闭环。

当前目标不是完整 SaaS，而是把 Agent 应用开发中最关键的运行时能力跑通：多轮会话、后台 Run、SSE 输出、工具调用、人工审批、取消中断、Checkpoint、Timeline 回放、RAG 文档治理、Memory 写入/召回/删除审计，以及 deterministic verification suite。

## Tech Stack

- Python / FastAPI
- LangGraph / LangChain
- Pydantic
- SQLite EventStore / SQLite Checkpoint
- SSE / WebSocket
- RAG / BM25 / optional vector retrieval
- Tool Calling / MCP
- OpenAI-compatible LLM provider

## Core Capabilities

- **Agent Runtime**
  - Thread / Run / Event 三层运行时模型
  - 后台执行、取消、审批续跑、失败记录
  - LangGraph checkpoint 支撑多轮工作记忆

- **Tool Governance**
  - `search_docs`、`http_get`、`http_post`、MCP tool 统一注册
  - Tool schema、risk level、timeout、retry、idempotency、audit
  - 高风险写操作进入 PolicyGate / approval
  - side effect ledger 记录真实或被阻断的写副作用

- **RAG**
  - 本地文档加载、manifest、chunk、BM25/RRF 检索
  - 文档上传、列表、删除、reload
  - 删除后不可检索，并写入 audit / deletion proof

- **Memory**
  - Working Memory：LangGraph checkpoint messages
  - Episodic Memory：EventStore run/thread summary
  - Long-term Memory：结构化 memory item store
  - 短期记忆结构化摘要、长期记忆写入门控、召回解释、删除审计

- **Observability**
  - SQLite EventStore 作为产品事实源
  - Timeline projection 支持 Run 回放
  - `llm_generation`、tool audit、policy decision、retrieval、context、memory、checkpoint consistency 等事件
  - 可选 Langfuse / LangSmith 作为外部模型观测 provider

## Project Layout

```text
copilot_agent/
  server.py        FastAPI app and HTTP/SSE APIs
  settings.py      environment config
  agent/           LangGraph runner, nodes, stream mapper
  runtime/         EventStore, ExecutionEngine, Timeline, Run FSM
  context/         context assembly and checkpoint packing
  memory/          short-term summary, long-term memory, recall, governance
  rag/             document ingest, retrieval, lifecycle
  tools/           ToolRegistry, HTTP tools, audit, sanitize
  policy/          PolicyGate / safety rules
  contracts/       event and tool payload contracts
  observability/   provider abstraction

scripts/           verification and export scripts
docs/              design notes
static/            local chat + timeline UI
storage/           local SQLite files
```

## Quick Start

Create environment and install dependencies:

```powershell
conda create -n learnagent312 python=3.12 -y
conda activate learnagent312
pip install -r requirements.txt
```

Create `.env`:

```env
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash

SCENARIO=minimal
COPILOT_CAPABILITIES=rag,http

AGENT_EVENT_STORE_PATH=storage/learnagent-events.sqlite
AGENT_CHECKPOINT_PATH=storage/langgraph-checkpoints.sqlite
```

Start the server:

```powershell
uvicorn copilot_agent.server:app --reload --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000/ui/
```

## Common APIs

```http
POST /v1/threads
POST /v1/threads/{thread_id}/runs
GET  /v1/runs/{run_id}
GET  /v1/runs/{run_id}/events
GET  /v1/runs/{run_id}/timeline
POST /v1/runs/{run_id}/cancel
POST /v1/runs/{run_id}/approve
POST /v1/runs/{run_id}/reject
```

RAG:

```http
GET    /v1/rag/status
POST   /v1/rag/reload
POST   /v1/rag/upload
GET    /v1/rag/documents
DELETE /v1/rag/documents/{doc_id}
GET    /v1/rag/documents/{doc_id}/deletion-proof
```

Memory:

```http
GET    /v1/threads/{thread_id}/memory?goal=...
GET    /v1/threads/{thread_id}/memory/items
POST   /v1/threads/{thread_id}/memory/items/{item_id}/confirm
POST   /v1/threads/{thread_id}/memory/items/{item_id}/reject
DELETE /v1/threads/{thread_id}/memory/items/{item_id}
GET    /v1/threads/{thread_id}/memory/items/{item_id}/deletion-proof
```

Debug / audit exports:

```powershell
python scripts/export_run_debug_bundle.py --run-id <run_id>
python scripts/export_rag_deletion_proof.py --doc-id <doc_id>
python scripts/export_memory_deletion_proof.py --thread-id <thread_id> --item-id <item_id>
```

## Verification

Fast deterministic gate:

```powershell
python scripts/verify_eval_suite.py --profile core-fast --summary-json artifacts/eval/eval-core-fast-summary.json
```

RAG profile:

```powershell
python scripts/verify_eval_suite.py --profile rag --summary-json artifacts/eval/eval-rag-summary.json
```

Compile check:

```powershell
python -m compileall copilot_agent scripts
```

Useful focused checks:

```powershell
python scripts/verify_memory_domain.py --case all
python scripts/verify_tool_governance_domain.py --case all
python scripts/verify_rag_domain.py --case all
python scripts/verify_live_llm_e2e_acceptance.py --require-live --message "hello agent"
```

`core-fast` is deterministic and does not require external LLM calls. Live LLM acceptance is optional and requires `OPENAI_API_KEY`.

## Notes

- EventStore is the product fact source.
- LangGraph checkpoint is the working-memory fact source.
- RAG and Memory deletion are product-layer governance semantics: newly retrieved / recalled content is removed or redacted, but historical EventStore run records are not rewritten.
- This project is currently local single-user MVP. It does not include production auth, multi-tenant isolation, external job queues, or hosted deployment hardening.

## Docs

- [Runtime design](docs/runtime-design.md)
- [Memory checkpoint design](docs/memory-checkpoint-design.md)
- [RAG design](docs/rag-design.md)
- [Tool design](docs/tool-design.md)
- [Observability design](docs/observability-design.md)
- [CI design](docs/ci-design.md)
