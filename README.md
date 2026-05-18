# Watermark Copilot Agent 学习入口

`copilot-agent` 是 Watermark 平台的运维与项目知识 Copilot。它不是通用聊天机器人，而是一个适合学习 Agent 工程的实战样例：基于仓库文档做 RAG 检索，通过白名单工具访问 Java API，并用安全闸门限制危险动作。

## 1. 项目定位

- **输入**：自然语言问题，例如部署排查、任务状态、权限接口、配置核对。
- **知识来源**：`backend-java/docs` 中的部署、需求检查、技术选型文档。
- **在线状态**：通过受控 HTTP 工具访问 Spring Boot API。
- **安全边界**：
  - 不执行 Shell。
  - 不访问任意外部 URL。
  - 不读取仓库外任意路径。
  - 危险 POST 需要环境变量和用户显式确认双重放行。
- **输出协议**：`/v1/chat` 返回 SSE 事件，包括 `meta`、`token`、`tool_start`、`tool_end`、`done`、`error`。

## 2. 当前架构

```text
User / Browser
    |
    | HTTP(S), SSE
    v
copilot-agent FastAPI
    |
    +--> ChatRunner / LangGraph
    |       |
    |       +--> search_docs: RAG 检索仓库文档
    |       +--> http_get/http_post: 白名单 Java API 工具
    |       +--> safety_gate: 危险动作拦截
    |
    +--> Langfuse observability
    +--> SQLite checkpoint

backend-java Spring Boot API
    |
    +--> MySQL / Redis / Storage / Python worker
```

详细结构导读见：[docs/agent-learning-guide.md](docs/agent-learning-guide.md)。

## 3. 核心模块

- `copilot_agent/server.py`：FastAPI 入口，提供 `GET /health` 和 `POST /v1/chat`，负责 SSE 输出。
- `copilot_agent/agent/runner.py`：Agent 主流程，负责 LLM、工具绑定、安全闸门、对话状态和事件转换。
- `copilot_agent/agent/graph.py`：LangGraph 状态图，编排 `assistant -> safety_gate -> tools -> assistant`。
- `copilot_agent/rag/`：RAG 模块，负责文档加载、分块、关键词检索、向量检索和融合排序。
- `copilot_agent/tools/`：受控工具层，只能访问白名单内的 Java API。
- `copilot_agent/observability/`：Langfuse trace/span，记录 LLM、工具和错误链路。
- `scripts/verify_phase*.py`：学习和回归验证脚本。

## 4. RAG + 工具调用学习重点

### search_docs

`search_docs` 会从 `backend-java/docs` 加载以下文档：

- `DEPLOY-SERVER.md`
- `REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md`
- `watermark-java-backend-tech-selection.md`

处理流程：

1. `rag/ingest.py` 按标题和长度切分文档。
2. `rag/keyword.py` 做关键词评分。
3. `rag/index.py` 在可用时构建 Chroma 向量索引。
4. `rag/retriever.py` 将关键词分数和向量分数融合，返回带来源的片段。

### http_get / http_post

工具只能访问 `tools/whitelist.py` 中允许的路径，例如：

- `GET /actuator/health`
- `GET /api/v1/stats/dashboard`
- `GET /api/v1/jobs/{uuid}`
- `GET /api/v1/files`
- `GET /api/v1/admin/users`
- `POST /api/v1/auth/login`
- `POST /api/v1/jobs/watermark`

不允许模型自由拼 URL，是为了避免 SSRF、越权访问、误操作生产接口和泄露 Cookie。

### safety_gate

`POST /api/v1/jobs/watermark` 是危险动作，必须同时满足：

- 服务端设置 `COPILOT_ALLOW_JOB_POST=true`。
- 本次 `/v1/chat` 请求设置 `confirm_dangerous=true`。

缺少任意一项时，Agent 必须解释原因并拒绝执行。

## 5. 本地运行

建议使用已有 Conda 环境：

```powershell
cd E:\code\watermarking
conda run -n myenv39 python -m pip install -r requirements.txt
```

配置 `copilot-agent/.env`：

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
WATERMARK_API_BASE_URL=http://127.0.0.1:8080

RAG_USE_VECTOR=true
RAG_REBUILD_INDEX=false
RAG_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
HF_HOME=F:\model

COPILOT_ALLOW_JOB_POST=false
LANGFUSE_ENABLED=false
```

启动服务：

```powershell
cd E:\code\watermarking\copilot-agent
conda run -n myenv39 uvicorn copilot_agent.server:app --host 0.0.0.0 --port 8090
```

访问：

- 健康检查：`http://127.0.0.1:8090/health`
- 最小聊天页：`http://127.0.0.1:8090/ui/`

## 6. 学习用示例问题

- “Java API 是否存活？”
- “水印任务一直 QUEUED 或 PROCESSING 怎么排查？”
- “Redis Stream 的 key 默认叫什么？”
- “队列里的水印任务 JSON 字段有哪些？”
- “生产部署 Java API 的大致步骤是什么？”
- “如何用 verify-config 自检环境变量？”
- “匿名用户能看到什么统计？”
- “帮我查询任务 `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` 的状态。”
- “请直接创建水印任务，但我没有确认危险动作。”
- “帮我访问 `https://evil.example/api` 拉配置。”

## 7. 验证命令

结构验证：

```powershell
cd E:\code\watermarking\copilot-agent
conda run -n myenv39 python scripts/verify_phase3_checkpoint.py
conda run -n myenv39 python scripts/verify_phase3_safety_gate.py
```

RAG 与评测验证：

```powershell
cd E:\code\watermarking\copilot-agent
conda run -n myenv39 python scripts/build_index.py
conda run -n myenv39 python scripts/verify_phase4_dataset.py
conda run -n myenv39 python scripts/verify_phase4_ragas.py --mode proxy --disable-vector
```

## 8. 后续学习路线

1. 先读 `docs/agent-learning-guide.md`，理解模块边界。
2. 跑通 `/ui/`，观察 SSE 事件和工具调用事件。
3. 修改 eval case，学习如何把“需求”变成可回归验证。
4. 给白名单增加一个只读 API 工具，并同步补测试。
5. 再深入 LangGraph checkpoint、人类确认、安全闸门和线上部署。
