# Agent 开发学习导读：Watermark Copilot

这份文档用当前项目做学习材料，目标是把 Agent 开发拆成几个能落地理解的层次：入口、编排、RAG、工具、安全、观测、评测。

## 1. 先看整体分层

```text
浏览器 / 调用方
  |
  v
server.py
  - HTTP 路由
  - SSE 协议
  - 生命周期初始化
  |
  v
agent/runner.py
  - 组装系统提示词
  - 调用 LLM
  - 绑定工具
  - 输出 token/tool 事件
  |
  v
agent/graph.py
  - assistant 节点
  - safety_gate 节点
  - tools 节点
  - SQLite checkpoint
  |
  +--> rag/
  |     - 文档加载
  |     - 关键词检索
  |     - 向量检索
  |     - 融合排序
  |
  +--> tools/
  |     - 白名单路径校验
  |     - Java API GET/POST
  |     - Cookie 存储与脱敏
  |
  +--> observability/
        - Langfuse trace
        - tool span
        - 敏感信息脱敏
```

学习时可以把它理解成一句话：**LLM 负责判断下一步，RAG 负责给知识，工具负责查实时状态，安全闸门负责防止误操作。**

## 2. 入口层：`server.py`

入口层只做协议相关的事情：

- `GET /health`：服务健康检查。
- `POST /v1/chat`：接收对话请求，返回 SSE 流。
- 启动时构建 RAG store。
- 创建 `ChatRunner`。
- 关闭时 flush Langfuse。
- 挂载 `static/` 到 `/ui/`，提供最小聊天页面。

它不应该直接写业务推理，也不应该直接访问 Java API。这样可以让 HTTP 协议层和 Agent 逻辑分开。

## 3. 编排层：`runner.py` + `graph.py`

`runner.py` 是 Agent 主控：

- 把用户消息转换成 LangChain message。
- 注入系统提示词。
- 绑定 `search_docs`、`http_get`、`http_post` 三个工具。
- 把 LangGraph 事件转换成 SSE 事件。
- 管理会话级 Cookie，不把 Cookie 暴露给模型输出。

`graph.py` 定义状态流：

```text
assistant -> safety_gate -> tools -> assistant
assistant -> END
safety_gate -> END
```

含义：

- `assistant` 让模型决定回答还是调用工具。
- 如果模型要调用工具，先进入 `safety_gate`。
- 如果安全闸门允许，再进入 `tools`。
- 工具结果回到 `assistant`，模型继续总结。
- 没有工具调用时直接结束。

这里体现了 Agent 编排的核心：模型不是一次性回答，而是在“思考、调用工具、观察结果、再回答”的循环中完成任务。

## 4. RAG 层：`rag/`

RAG 解决“模型不知道项目细节”的问题。当前实现读取 `backend-java/docs` 中的固定文档：

- `DEPLOY-SERVER.md`
- `REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md`
- `watermark-java-backend-tech-selection.md`

数据流：

```text
Markdown 文档
  -> 按标题和长度切块
  -> keyword_scores
  -> 可选 Chroma vector retrieval
  -> 分数融合
  -> 返回 excerpts_markdown + sources
```

学习重点：

- 为什么必须返回 sources：让回答能追溯到项目文档。
- 为什么保留关键词检索：离线、稳定、CI 友好。
- 为什么向量检索可选：本地模型、缓存、网络和环境都会影响稳定性。

## 5. 工具层：`tools/`

工具层解决“实时状态查询”的问题，例如服务是否存活、任务状态、用户列表等。

当前工具：

- `http_get(path, cookie_header)`
- `http_post(path, json_body, cookie_header, idempotency_key)`
- `search_docs(query)`

`tools/whitelist.py` 是边界核心。它只允许固定路径，拒绝：

- 不以 `/` 开头的路径。
- 包含 `..` 的路径。
- `//example.com` 这类可疑路径。
- 带 URL scheme 的完整外部 URL。
- 未列入白名单的 Java API。

这就是 Agent 工程里非常重要的原则：**工具能力越强，边界越要清楚。**

## 6. 安全闸门

当前高风险动作是创建水印任务：

```text
POST /api/v1/jobs/watermark
```

它必须同时满足：

- `COPILOT_ALLOW_JOB_POST=true`
- `/v1/chat` 请求体中 `confirm_dangerous=true`

否则 `safety_gate` 会直接返回拒绝说明，不让请求进入工具节点。

这种设计适合学习 human-in-the-loop：

- 模型可以提出操作建议。
- 系统可以要求用户确认。
- 执行权限由服务端配置控制。
- 危险动作默认关闭。

## 7. 观测层：`observability/`

观测层用于回答“Agent 为什么这么做”：

- 一次对话是一条 trace。
- 每次工具调用是一段 span。
- 输入、输出、错误会被记录。
- Cookie、password、set-cookie 等敏感字段会脱敏。

学习时不需要一开始就开启 Langfuse。先本地跑通，再接观测平台会更容易理解。

## 8. 评测层：`eval/` + `scripts/`

Agent 项目不能只靠手工试。当前已有两类验证：

- Phase3：验证 LangGraph checkpoint 和 safety gate。
- Phase4：验证评测数据集、RAG 命中、安全规则和趋势指标。

推荐学习方式：

1. 先读 `eval/phase4-eval-cases.json`。
2. 选一个 case，看它期望调用什么工具。
3. 跑 `verify_phase4_dataset.py` 检查数据集结构。
4. 跑 `verify_phase4_ragas.py --mode proxy --disable-vector` 检查文档检索命中。
5. 新增工具时，同步新增 case。

## 9. 第一阶段不要做什么

为了保持学习路径稳定，第一阶段先不做这些事：

- 不让 Agent 执行 Shell。
- 不让 Agent 访问任意外部 URL。
- 不让 Agent 直连数据库。
- 不把 Copilot 部署到生产域名。
- 不把 Copilot 强行嵌入 Spring Boot 主站页面。

先把 RAG、工具、安全和评测跑通，再考虑 UI 集成和线上部署。

## 10. 建议阅读顺序

1. `README.md`
2. `server.py`
3. `agent/runner.py`
4. `agent/graph.py`
5. `tools/whitelist.py`
6. `tools/http_tools.py`
7. `rag/ingest.py`
8. `rag/retriever.py`
9. `eval/phase4-eval-cases.json`
10. `scripts/verify_phase4_dataset.py`
