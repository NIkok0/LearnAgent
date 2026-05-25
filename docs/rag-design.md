# LearnAgent RAG 设计

> 说明水印平台文档的加载、分块、混合检索，以及 `search_docs` 与 EventStore / Eval 的衔接；不重复 Run FSM 与 Tool 审计通用契约。  
> 关联文档：[agent-learning-guide.md](./agent-learning-guide.md)、[demo-requirements-design.md](./demo-requirements-design.md) §3、[tool-design.md](./tool-design.md)、[data-flow-design.md](./data-flow-design.md) §2.4、[eval-design.md](./eval-design.md)、[ci-design.md](./ci-design.md)  
> **文档定位**：本项目 M10 的**实现设计**（代码锚点 + 边界 + 评测）。通用 RAG 全链路概念（BM25、rerank、GraphRAG 等）在 §1.2、§2.1、§5.5、§9.4 以「现状 vs 目标」对照呈现，便于与面试/知识体系对齐。

**K/C/S 位置**：Capability **M10 RAG**（ingest + 检索）；编排层 Tool-grounded → [tool-design.md](./tool-design.md)。详见 [guide §2.4](./agent-learning-guide.md)。

---

## 0. 实现状态总览（学习入口）

读代码前可先对照下表，了解 **M10 RAG** 当前做到哪一步。

| 能力 | 状态 | 代码 / 数据锚点 |
|------|------|-----------------|
| 文档目录解析（env / Scenario `docs_dir` / manifest） | ✅ 已实现 | `rag/ingest.py` → `repo_docs_dir()`；`scenarios/<name>/docs/docs_manifest.json` |
| `IngestSource` 抽象（File / Url / Api 占位） | ✅ 已实现 | `rag/ingest_source.py` |
| 9 份 Demo 语料 + **manifest glob**（无 Kernel 硬编码文件名） | ✅ 已实现 | `scenarios/watermark/docs/docs_manifest.json`；Kernel 无 `DOC_FILENAMES` |
| Markdown 按标题分块 + 超长滑动窗口 | ✅ 已实现 | `load_chunks()` |
| Chunk 元数据 `section_title` / `heading_path` / `doc_type` / `chunk_index` / `updated_at` | ✅ 已实现 | `rag/schema.py`，`ingest.py` |
| API 契约结构化 ingest（endpoint / 字段表 / Error Model） | ✅ 已实现 | `rag/api_parse.py`，`API-CONTRACT.md` |
| `response_fields` JSON 块解析 | ✅ 已实现 | `api_parse._parse_response_json_blocks` |
| `POST /v1/rag/upload` + manifest 注册 | ✅ 已实现 | `server.py`，`docs_manifest.register_uploaded_file` |
| 动态 context budget / top-k | ✅ 已实现 | `schema.select_chunks_for_budget`，`RAG_CONTEXT_BUDGET_CHARS` |
| 关键词检索（ASCII + CJK token + 长句 2-gram） | ✅ 已实现 | `rag/keyword.py`，`rag/tokenize.py` |
| BM25 稀疏检索 | ✅ 已实现 | `rag/bm25.py` |
| RRF 多路融合（keyword + BM25 + vector） | ✅ 已实现，默认开启 | `rag/fusion.py`，`RAG_USE_RRF=true` |
| 查询改写（口语 → 平台术语） | ✅ 已实现 | `rag/query_rewrite.py`，`RAG_QUERY_REWRITE_ENABLED` |
| Query 路由（sparse / dense / hybrid） | ✅ 已实现 | `rag/query_router.py`，动态 BM25+向量 RRF 权重 |
| `doc_type` / query hint / authority 参与打分 | ✅ 已实现 | `rag/fusion.py` → `DOC_TYPE_BOOST` + `apply_authority_boost` |
| 检索结果 dedup（同 section） | ✅ 已实现 | `dedup_chunks()`，`RAG_DEDUP_RESULTS` |
| Cross-encoder rerank（融合候选→top_k） | ⚠️ 可选，默认关 | `rag/rerank.py`，`RAG_RERANK_ENABLED`；dedup 后 rerank |
| 向量检索 + Chroma 持久化（可选） | ✅ 已实现，**默认关闭** | `rag/index.py`，`RAG_USE_VECTOR=false` |
| Turn 前预检索（preretrieval） | ✅ 已实现，默认开 | `context/preretrieval.py`，`CONTEXT_PRERETRIEVAL_ENABLED` |
| Private RAG 上下文护栏 | ✅ 已实现 | `rag/context_guard.py` → `[PrivateRAGContext]` 头 + budget 截断 |
| `search_docs` 与 preretrieval 去重 | ✅ 已实现 | `context/preretrieval_dedupe.py` |
| `search_docs` tool + `retrieval_completed` 落库 | ✅ 已实现 | `agent/tool_handlers.py`，`contracts/adapters/tool_rag.py` |
| `retrieval_completed.call_id` 与 `search_docs` tool 关联 | ✅ 已实现 | `agent/tool_call_context.py`，`event_mapper.py` |
| 检索 → API path / 字段 hints 注入 tool 结果 | ✅ 已实现 | `rag/api_paths.py`，`suggested_api_paths` / `api_field_hints` |
| Timeline `kind: retrieval` 投影 | ✅ 已实现 | `runtime/timeline.py` |
| 离线 proxy 检索评测（20 docs case + gold chunk） | ✅ 已实现 | `eval/phase4-eval-cases.json`，`verify_phase4_ragas.py` |
| PR / Nightly 检索 profile 分离 | ✅ 已实现 | PR：`--disable-vector`；Nightly：`phase4_ragas_nightly` + `bge-small-zh-v1.5` |
| 测试知识库（虚构 Demo 内容） | ✅ 已实现 | `scenarios/watermark/docs/`（Scenario 语料；非 Kernel 源码） |
| 热更新（watch + `POST /v1/rag/reload`） | ✅ 已实现 | `rag/reload.py`，两阶段 reload |
| 向量增量 upsert（按文件 manifest） | ✅ 已实现 | `rag/manifest.py`，`sync_vector_index` |
| 分层评测（Recall@k / faithfulness / E2E） | ⚠️ 部分 | L1 proxy + **gold_chunk Recall@k/MRR** ✅；L4-lite ✅；L5 proxy ✅；L3 RAGAS 可选 |
| Tool-grounded 编排（先 RAG 再 API） | ✅ 已实现 | [tool-design.md](./tool-design.md) |
| 上传新 md / 多租户 collection | ⚠️ 部分 | upload ✅ + **doc_security** 字段 ✅；MVP **单 collection + metadata 过滤** ✅ |
| `doc_security` manifest（tenant / acl / classification / authority） | ✅ 已实现 | `docs_manifest.json` → `DocChunk`；`rag/security.py` |
| Policy-aware 检索（tenant / ACL / classification） | ✅ 已实现 | `rag/policy_filter.py`，`policy_aware_search` |
| Policy 预过滤 + 向量路径共存 | ✅ 已实现 | `RagStore._vector_chunk_allowlist`；Chroma metadata `tenant_id` / `authority` |
| Scenario `default_tenant_id` → runner / preretrieval | ✅ 已实现 | `scenario/schema.py`，`request_context.py`，`runner.py` |
| Credential + `rag_allowed_scopes` → `allowed_scopes` | ✅ 已实现 | `merge_retrieval_scopes()`；`watermark.yaml` `group:ops/security` |
| Scenario `rag_embedding_model`（中文向量默认） | ✅ 已实现 | `bootstrap._apply_rag_runtime_settings`；`BAAI/bge-small-zh-v1.5` |
| Authority 冲突裁决（boost + heading dedup） | ✅ 已实现 | `fusion.apply_authority_boost`，`dedup_chunks` 保留最高 authority |
| RAGAS 作为 PR 硬门禁 | ❌ 未实现 | Nightly E2E RAGAS ✅；PR 仍 proxy-only |
| L7 结构化 citations（`SearchDocsToolData.citations`） | ✅ 已实现 | `tool_data.CitationItem`，`rag/citations.py`，`tool_handlers` |
| L2 上下文质量指标（overlap / truncation） | ✅ 已实现 | `eval/context_quality.py`，`phase4_ragas` proxy_metrics |
| RAG E2E 生成评测（retrieve → LLM → RAGAS） | ✅ Nightly | `eval/rag_e2e.py`，`verify_rag_e2e_ragas.py` |
| RAG metrics 历史趋势 / 回归检测 | ✅ Nightly | `rag_metrics/history/`，`eval/rag_metrics_trend.py` |

**成熟度**：**高** — ingest/manifest/response JSON/budget/ExtractedRecord 已闭环；**真实 LLM E2E** 仍待 [guide §2.8](./agent-learning-guide.md) / [tool-design §5](./tool-design.md)。

---

## 1. 设计动机

司法材料确权 Demo 需要 Agent **依据平台文档**回答部署、队列、排障与算法问题，而不是凭空生成。纯 LLM 会在 Redis key、接口路径、状态枚举上产生「看似合理」的错误。

| 问题 | 若无 RAG |
|------|----------|
| 部署 / Runbook 细节 | 编造配置项或遗漏前置条件 |
| API 路径与字段 | 与真实 Java API 契约不一致 |
| 排障 vs 实时状态 | 用文档猜任务状态，或不该调 API 时乱调 |
| 可复盘 | 用户无法看到「回答依据哪几份文档」 |

本设计覆盖 **M10 RAG**（`copilot_agent/rag/`），与相邻模块分层：

- **RAG 层**：文档 ingest、检索、混合打分、prompt 摘录格式化
- **Tool 层**：`search_docs` handler、HTTP 工具 → 通用契约见 [data-flow-design.md](./data-flow-design.md)
- **编排层**：LLM 是否调用 `search_docs`、是否与 `http_get` 组合 → 规则路由 + Tool-grounded 编排，见 [tool-design.md](./tool-design.md)
- **Eval 层**：离线 proxy / 可选 RAGAS → 见 [eval-design.md](./eval-design.md)

**边界（M10 禁止）**：RAG **不**写入 Run FSM、**不**替代 LangGraph checkpoint 作为对话真相源；检索结果仅通过 tool 返回值与 `retrieval_completed` 事件进入产品轨。

### 1.2 方案选型：RAG vs 直接喂文档 vs 微调

| 方案 | 适用 | LearnAgent 为何不选为唯一手段 |
|------|------|------------------------------|
| **直接塞文档进 Prompt** | 单份报告、一次性分析 | 9+ 平台文档 + 多轮 Run；窗口与 token 不可扩展；无检索选择 |
| **Fine-tuning** | 固定输出风格、分类判别 | 平台规则/API/Redis key **频繁变更**；难追溯来源；更新成本远高于 reload 知识库 |
| **RAG（当前）** | 可更新私有文档、需引用溯源 | 水印 Demo 核心路径：`search_docs` 找证据 + LLM 组织答案 + Timeline 审计 |

**RAG 在本项目的「证据驱动」含义**：检索层负责 **找到** 相关 chunk；生成层（M08 LLM + M06 编排）负责 **整合与表达**；M10 **不**保证最终回答一定 grounded——That 依赖 Prompt、模型行为与后续 citation 评测（§9.4）。

**与 Tool Agent 的组合**：静态文档走 RAG；**实时状态**（任务 ID、健康检查）走 `http_get`（§6.2），对应「结构化事实 + 文档解释」的多源模式，而非单一向量库包办。

---

## 2. 端到端数据流

### 2.1 通用 RAG 两阶段 vs LearnAgent 映射

生产 RAG 通常拆成 **离线建库** 与 **在线问答**；LearnAgent 将各环节映射到具体模块（空白 = 当前未独立实现）：

```text
【离线建库】
  采集 / 解析 / 清洗          → 仅 Markdown 白名单（§4.6）
  分块 chunking               → ingest.load_chunks（§4.3）
  向量化 + 索引               → sync_vector_index + rag_manifest（§7）
  元数据治理                  → doc_type ✅；**doc_security / authority** ✅（§4.6–§4.7）

【在线问答】
  Query 理解 / 改写           → query_rewrite（§5.1）
  召回（BM25 + 向量 + …）     → keyword + BM25 + 可选 vector（§5）
  融合 / Rerank               → RRF + doc_type/authority boost（§5.3）；rerank ⚠️ 可选（默认关）
  上下文构造                  → build_guarded_context + format_chunks_for_prompt（§5.8）
  LLM 生成 + 引用             → ToolHandlers → M08 LLM（§6）
  评测 / 监控 / 反馈          → proxy + 可选 RAGAS（§9）；线上指标 → M13
```

**检索与生成解耦（职责）**：

| 阶段 | 负责模块 | 输出 |
|------|----------|------|
| 检索 | M10 `RagStore.search_detailed` / `policy_aware_search` | top-k `DocChunk` + route 元数据 |
| 上下文打包 | M10 `build_guarded_context` → `format_chunks_for_prompt` | `excerpts_markdown`（含 `[PrivateRAGContext]` 头） |
| 生成 | M08 LLM + M06 图 | 最终 assistant 消息；引用文件名靠 Prompt |

### 2.2 进程内实现数据流

```
[启动] server.py
    |-- apply_scenario_environment()  → configure_rag_rules + rag_embedding_model
    |-- RagStoreManager(trigger=startup)
    |     |-- build_rag_store()  → load_chunks + 可选向量
    |-- attach_memory(MemoryManager)  … 后续 reload 同步 swap
    |-- [可选] asyncio 轮询 docs_source_fingerprint → reload(watch)
    v
[用户 Run] LangGraph assistant
    |-- [可选] preretrieval (context/preretrieval.py)
    |     |-- policy_aware_search_docs + build_guarded_context
    |     └─ SystemMessage [PreRetrievedDocs] 注入 turn 上下文
    |-- LLM 决定 tool_calls (含 search_docs)
    v
[工具] ToolHandlers.search_docs(query)
    |-- build_retrieval_request() → MemoryManager.policy_aware_search_docs
    |     └─ RagStore.policy_aware_search (top_k = dynamic_search_top_k)
    |-- apply_preretrieval_dedupe (与 turn 内预检索去重)
    |-- build_guarded_context → excerpts_markdown (+ [PrivateRAGContext] 头)
    |-- citations[] + suggested_api_paths / api_field_hints
    |-- RagSearchAdapter -> ToolResultModel (给 LLM)
    |-- append_event(retrieval_completed)  (有 thread_id + run_id 时)
    |-- Langfuse tool span (start_tool_span / end_tool_span)
    v
[读模型] TimelineProjector -> kind: retrieval
[评测] verify_phase4_ragas.py --mode proxy (CI 默认关向量)
```

**原则**：检索逻辑集中在 `RagStore`；进程内单例 store，**不**按 Run 重建索引；chunk 全文不进 checkpoint，仅 tool 摘录与事件 payload 进入可审计轨迹。

---

## 3. 核心组件与边界

| 组件 | 路径 | 职责 | 不负责 |
|------|------|------|--------|
| `repo_docs_dir` / `load_chunks` | `rag/ingest.py` | 解析文档目录、标题栈分块、API 结构化元数据 | LLM 路由、引用格式强制 |
| `parse_api_section` | `rag/api_parse.py` | 从 API 契约 Markdown 解析 endpoint / 字段表 / Error Model | HTTP 调用 |
| `DocChunk` / `format_chunks_for_prompt` | `rag/schema.py` | 块元数据、API 字段、摘录拼接（含 `\| GET /path` 头） | 向量模型训练 |
| `extract_api_paths` | `rag/api_paths.py` | 从 chunk 结构化字段 + regex 提取白名单内 API path | Tool 路由规则 |
| `keyword_search` / `keyword_scores` | `rag/keyword.py` | 稀疏 token 计分 | 语义同义词扩展 |
| `BM25Index` | `rag/bm25.py` | Okapi BM25 稀疏路 | 在线 query 改写 |
| `rewrite_query` / doc_type hints | `rag/query_rewrite.py` | Scenario overlay 规则扩展 + doc_type hint | LLM 改写 |
| `route_query` | `rag/query_router.py` | sparse/dense/hybrid 通道权重 | LLM 意图分类 |
| `rrf_fuse` / `dedup_chunks` / `apply_authority_boost` | `rag/fusion.py` | RRF 融合、doc_type/authority boost、去重 | — |
| `rerank_chunks` | `rag/rerank.py` | Cross-encoder 精排（可选，dedup 之后） | — |
| `build_guarded_context` | `rag/context_guard.py` | Private RAG 头 + budget 截断 + 审计 payload | 检索打分 |
| `RagPolicyFilter` / `build_retrieval_request` | `rag/policy_filter.py`, `rag/request_context.py` | tenant/ACL/classification 过滤；构造 `RetrievalRequest` | 稀疏/向量算法 |
| `preretrieve_docs` | `context/preretrieval.py` | Turn 前自动检索并注入 `[PreRetrievedDocs]` | Tool 注册 |
| `apply_preretrieval_dedupe` | `context/preretrieval_dedupe.py` | `search_docs` 与预检索结果去重 | 检索算法 |
| `citations_from_chunks` | `rag/citations.py` | chunk → `CitationItem` | — |
| `build_vector_index` / `sync_vector_index` | `rag/index.py`, `rag/manifest.py` | Chroma 持久化、全量/增量 upsert | 在线 query 改写 |
| `RagStoreManager` | `rag/reload.py` | 热更新、指纹检测、`MemoryManager` swap | 上传新文件、扩白名单 |
| `RagStore` / `build_rag_store` | `rag/retriever.py` | 混合融合检索入口；`policy_aware_search` | Tool 注册 |
| `RagSearchAdapter` | `contracts/adapters/tool_rag.py` | `ToolResultModel` + `retrieval_completed` payload | HTTP 调用 |
| `ToolHandlers.search_docs` | `agent/tool_handlers.py` | policy 检索、context guard、落库、可观测 span | 检索算法本身 |
| `MemoryManager.policy_aware_search_docs` | `memory/manager.py` | 对 runner/tool 暴露带策略的检索 | 文档 ingest |

对外入口：`copilot_agent.rag` 导出 `build_rag_store`、`load_chunks`、`format_chunks_for_prompt`；旧 `rag/markdown_rag.py` 兼容 facade 已删除。

---

## 4. 文档 Ingest

### 4.1 文档目录解析

`repo_docs_dir()` 按优先级：

1. 环境变量 `COPILOT_DOCS_PATH`，或 Scenario `resources.docs_path_env` 指向的环境变量（须为已存在目录）
2. Scenario `docs_dir` / `resources.docs_fallback`（如 `scenarios/watermark/docs`）
3. 父级 monorepo 的 `backend-java/docs/`

未找到目录时 `load_chunks()` 返回空列表，日志 warning，**RAG 处于禁用态**（`search_docs` 仍可调但无命中）。

### 4.2 语料与 manifest（Scenario 驱动）

**不再**在 Kernel `ingest.py` 硬编码 `DOC_FILENAMES`。文件发现由 Scenario 目录内 **`docs_manifest.json`** 驱动：

- 默认：`include_glob: "*.md"` + 可选 `load_order` / `doc_types` 映射
- watermark Demo：`scenarios/watermark/docs/docs_manifest.json`（9 篇 md，与 [demo-requirements-design.md](./demo-requirements-design.md) §3.1 对齐）

| 文件（watermark Demo） | `doc_type` | 知识类型 |
|------|------------|----------|
| `REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md` | `requirements` | 需求检查、已知偏差 |
| `API-CONTRACT.md` | `api_contract` | REST 契约 |
| `DEPLOY-SERVER.md` | `deploy` | 部署、环境变量、verify-config |
| `SECURITY-BASELINE.md` | `security` | 安全与白名单 |
| `RUNBOOK.md` | `runbook` | 运维排障 |
| `OPERATIONS-SLO-SLA.md` | `operations` | SLO/SLA、告警 |
| `watermark-java-backend-tech-selection.md` | `tech_selection` | Redis Stream、队列 JSON |
| `README.md` | `overview` | 平台总览 |
| `README_ALGORITHM.md` | `algorithm` | 水印算法 |

**换业务**：在新 Scenario 的 `docs/` 下放置 `docs_manifest.json` + markdown，**不改** `rag/ingest.py`。

**测试知识库**：`scenarios/watermark/docs/` 为 LearnAgent 虚构 Demo 文档。接入生产语料时设置 `COPILOT_DOCS_PATH` 或 Scenario `docs_dir`。

### 4.3 分块策略

| 参数 | 默认 | 行为 |
|------|------|------|
| `max_chunk_chars` | 1400 | 超长 section 滑动窗口切分 |
| `overlap` | 200 | 相邻块重叠，减少边界截断 |
| 切分锚点 | `#` 标题行 | 维护标题栈，按 section 聚合后再按长度切 |

**标题栈**：遇 `#` / `##` / … 更新层级栈；每个 section 记录：

- `section_title` — 该 section 首个标题文本
- `heading_path` — 栈拼接，如 `Deploy Server > Environment Variables`
- `doc_type` — 由 `docs_manifest.doc_type_for(source)` 赋值（manifest `doc_types` 映射）

同一 section 切出的多个子块 **共享** 上述元数据。

### 4.4 `DocChunk` 字段（当前）

| 字段 | 类型 | 说明 |
|------|------|------|
| `source` | str | 文件名 |
| `start_line` | int | section 起始行号 |
| `text` | str | chunk 正文 |
| `section_title` | str | section 标题 |
| `heading_path` | str | 标题路径 |
| `doc_type` | str | 文档类型标签，默认 `doc` |
| `chunk_index` | int | 文件内递增序号（ingest 赋值） |
| `updated_at` | str | 源文件 mtime ISO8601 |
| `api_endpoint` | `ApiEndpoint?` | `method` + `path`（如 `GET /api/v1/jobs/{id}`） |
| `request_fields` | `list[ApiField]` | 请求参数字段表 |
| `response_fields` | `list[ApiField]` | 响应 JSON 块解析（`api_parse._parse_response_json_blocks`） |
| `error_codes` | `list[ApiErrorCode]` | Error Model 表（HTTP / code / meaning） |
| `tenant_id` | str | 租户 ID，默认 `default`（来自 manifest `doc_security`） |
| `doc_id` | str | 文档 ID，默认等于 `source` |
| `acl` | `list[str]` | ACL scope 列表（如 `group:ops`） |
| `classification` | str | 密级，默认 `internal` |
| `pii_level` | str | PII 等级，默认 `none` |
| `authority` | int | 权威度 0–100，参与 boost 与同 heading dedup |
| `chunk_id` | property | `{source}:{start_line}:{content_hash[:16]}`，向量 upsert 主键 |

**解析入口**：`doc_type == api_contract` 时，`ingest._load_file_chunks` 调用 `parse_api_section()`（`rag/api_parse.py`）。

**验收**：`python scripts/verify_rag_domain.py --case api_ingest`（login 字段、health endpoint、UNAUTHORIZED error code）。

### 4.5 摘录格式（给 LLM）

`format_chunks_for_prompt` 每个块头部示例：

```text
--- API-CONTRACT.md | Jobs > GET /api/v1/jobs/{id} [api_contract] | GET /api/v1/jobs/{id} ---
（正文）
```

API endpoint 存在时，块头追加 `| {method} {path}`，便于 LLM 与 Tool-grounded 对齐。

### 4.6 Ingest 能力矩阵（现状 vs 目标）

| 能力 | 现状 | 目标 / 缺口 |
|------|------|-------------|
| 输入格式 | 仅 Markdown（`.md`） | PDF / HTML / OpenAPI YAML（远期） |
| 文件发现 | ✅ Scenario `docs_manifest.json` + glob | 多租户 **独立 collection**（远期） |
| 结构化字段 | ✅ endpoint / request / response / error_codes / **authority** | 版本号 superseded 标记 |
| 增量 ingest | ✅ 热 reload + upload API（含 **tenant / acl / classification** form） | — |
| 多租户 | ✅ 单 collection `wm_docs` + **Chroma metadata + policy pre-filter** | 按 tenant 独立 collection（远期） |

**当前约束**：新增 md 优先改 Scenario **`docs_manifest.json`** 或 `POST /v1/rag/upload`；Kernel 无固定文件名列表。

### 4.7 知识冲突与优先级

Demo 文档可能出现 **同一事实多处描述**（如 Runbook vs API 契约、旧版 README vs 新 DEPLOY）。Wave B 起引入 **authority** 参与打分与同 heading dedup：

| 场景 | 当前行为 | 规划方向 |
|------|----------|----------|
| 多 chunk 内容矛盾 | 同 `(source, heading_path)` 保留 **最高 authority** chunk | 跨 source 冲突检测（远期） |
| 文档 vs 实时 API | Prompt 要求 API 优先；无代码强制 | Tool-grounded 节点：API 结果覆盖文档陈述 |
| 旧版段落未删除 | 仍可被检索命中 | `updated_at` + 版本过滤；ingest 时标记 superseded |
| `doc_type` / authority 优先级 | ✅ `DOC_TYPE_BOOST` + `apply_authority_boost` | 可配置 per-scenario 权重 |

**实现锚点**：`DocChunk.authority`（manifest `doc_security` 或 doc_type 默认）；`fusion.apply_authority_boost`；`dedup_chunks` 按 authority 裁决。

**产品原则**：可引用 > 可检索 > 可生成；冲突时 Timeline 展示 `RetrievalSourceItem.authority`，便于审计依据来源。

---

## 5. 检索：稀疏 + 可选向量 + RRF

### 5.1 查询改写与分词

**改写**（`rag/query_rewrite.py`，`RAG_QUERY_REWRITE_ENABLED=true`）：

- 规则表 **不硬编码在 Kernel**，由 Scenario overlay 注入：`apply_scenario_environment()` → `configure_rag_rules(scenario.rag_rules)`。
- watermark Demo 规则在 `config/watermark-rag.yaml`（如「队列」→ `Redis Stream WM_JOBS`、「卡住」→ `QUEUED PROCESSING`）。
- 改写后的 `search_query` 用于 keyword/BM25/向量；**原始 query** 仍用于 `query_doc_type_hints()`。

**分词**（`rag/tokenize.py`）：

- ASCII：`[A-Za-z0-9_./:-]{2,}`
- 中文：连续 CJK 序列（≥2 字）；长度 >4 的 CJK 序列额外生成 **2-gram**（提升「环境变量」等短语召回）

空 query 直接返回前 `top_k` chunk；keyword 路无 token 时得分为空，但 BM25/向量/改写仍可能命中。融合全空时，`search_detailed` 会 **fallback 到 `keyword_search`** 末级兜底。

### 5.2 关键词检索

- 基于 `token_set(query)` 对 chunk 正文计分。
- 长 ASCII token（>4 字符）权重 3，否则 1；零分时尝试子串包含（含中文原文）。
- `keyword_scores` max 归一化，键 `(source, start_line)`。

### 5.3 BM25 + RRF 融合

**BM25**（`rag/bm25.py`）：Okapi BM25，`k1=1.5`，`b=0.75`；热 reload 时随 `replace_chunks` 重建。

**融合**（`RagStore.search`，默认 RRF）：

```text
search_query = rewrite_query(query)           # 可关
kw  = keyword_scores(chunks, search_query)
bm25 = BM25Index.scores(search_query)         # RAG_USE_BM25
vec = _vector_scores(search_query)            # 可选

RRF（RAG_USE_RRF=true，k=RAG_RRF_K=60）:
  score(key) += weight_i / (k + rank_i)       # 各路 rank 来自 kw/bm25/vec

线性加权（RAG_USE_RRF=false）:
  score = kw_w*kw + bm25_w*bm25 + vec_w*vec

doc_type boost（RAG_DOC_TYPE_BOOST_ENABLED）:
  score *= DOC_TYPE_BOOST[doc_type] * query_hint[doc_type]

authority boost（RAG_AUTHORITY_BOOST_ENABLED）:
  score *= (1.0 + (authority - 50) * 0.002)

pool_k = max(top_k * MULTIPLIER, RAG_RERANK_CANDIDATES)  # rerank 开时
→ dedup → [可选 rerank] → top_k
```

**Cross-encoder rerank**（`rag/rerank.py`，`RAG_RERANK_ENABLED=false` 默认）：

| 项 | 默认 |
|----|------|
| 模型 | `BAAI/bge-reranker-base`（`RAG_RERANK_MODEL`） |
| 候选池 | 融合后 top `RAG_RERANK_CANDIDATES=50` |
| 输出 | rerank 后取 `top_k` |
| 依赖 | `sentence-transformers`（`requirements-vector.txt`） |

流程：RRF 召回 ≤50 → dedup → CrossEncoder(query, chunk.text) 重排 → top_k。CI 保持 rerank 关闭；本地 `--enable-rerank` 或 env 开启。

验收：`python scripts/verify_rag_rerank.py`。

**doc_type 静态权重**（`fusion.DOC_TYPE_BOOST`）：`api_contract` 1.12 > `requirements` 1.10 > `deploy`/`tech_selection` 1.08 > … > `overview` 1.0。Query hint 规则见 `query_rewrite.query_doc_type_hints`。

### 5.4 Query 路由（BM25 + 向量混合）

`rag/query_router.py` 在 **RRF 融合前** 按 query 特征选择通道权重（`RAG_QUERY_ROUTE_ENABLED=true`）：

| 模式 | 触发条件 | keyword | BM25 | vector | 典型 query |
|------|----------|---------|------|--------|------------|
| `sparse` | 多精确信号（API path、`WM_JOBS_*`、错误码） | 0.35 | **1.25** | 0.25 | `POST /api/v1/auth/login 需要哪些字段？` |
| `dense` | 开放式中文、少精确术语 | 0.25 | 0.55 | **1.20** | `需求检查表里有哪些已知偏差？` |
| `hybrid` | 同时含精确术语 + 开放式问法 | 0.50 | 1.00 | **0.85** | `水印任务一直 QUEUED 怎么排查？` |

向量未启用时：`vector_weight=0`，权重在 keyword/BM25 间重归一化；`dense` 降级为 hybrid 行为。

`RagStore.search_detailed()` 返回 `RagSearchResult(route=...)`；`search_docs` 将 `retrieval_mode` / `retrieval_route` 写入 `retrieval_completed` 事件。

验收：`python scripts/verify_rag_domain.py --case retrieval_quality`（含 sparse/dense/hybrid 路由权重）。

### 5.5 向量检索（可选）

启用条件：`settings.rag_use_vector == True` 且 LlamaIndex + Chroma + HuggingFace embedding 依赖可 import。

| 项 | 实现 |
|----|------|
| Embedding | `HuggingFaceEmbedding(model_name=settings.rag_embedding_model)` |
| 默认模型 | Settings 默认 `BAAI/bge-small-en-v1.5`；**active Scenario** 可覆盖（watermark → `BAAI/bge-small-zh-v1.5`，见 `config/watermark.yaml` + `bootstrap._apply_rag_runtime_settings`） |
| 存储 | `storage/chroma`（或 `RAG_CHROMA_PATH`）集合 `wm_docs` |
| 增量 sync | `sync_vector_index()` + `rag_manifest.json`：仅 changed/removed 文件 upsert/delete（§7） |
| 全量重建 | `RAG_REBUILD_INDEX=true`；embedding 模型变更时也会自动清空重建 |
| Top-K | retriever `similarity_top_k` ≥ `max(rag_vector_top_k, 12)` |

向量节点 metadata：`source`、`start_line`、`section_title`、`heading_path`、`doc_type`、`chunk_id`、`chunk_index`、`tenant_id`、`classification`、`authority`（及 API 相关 `http_method`/`http_path`），与稀疏键 `(source, start_line)` 对齐便于 RRF。

### 5.6 Tool 侧参数

| 调用点 | `top_k` | 摘录上限 |
|--------|---------|----------|
| `RagStore.search` / `search_detailed`（默认） | 6 | — |
| `ToolHandlers.search_docs` | `dynamic_search_top_k(budget_chars=RAG_CONTEXT_BUDGET_CHARS, ceiling=8)` | `build_guarded_context(..., max_chars=14000)` |
| `preretrieve_docs` | `dynamic_search_top_k(budget_chars=preretrieve_budget, ceiling=6)` | `CONTEXT_PRERETRIEVAL_BUDGET_CHARS`（默认 3500） |

生产路径 **`search_docs` 不走** `MemoryManager.search_docs`，而是：

1. `build_retrieval_request()`（tenant / scopes / classification）
2. `policy_aware_search_docs()` → `RagStore.policy_aware_search`
3. `build_guarded_context()` 生成 `excerpts_markdown`（非直接 `format_chunks_for_prompt`）

Handler 返回 LLM 的 `data` 字段：`excerpts_markdown`、`sources`（去重文件名）、**`citations[]`**、`suggested_api_paths`、`api_field_hints`、`context_guard` 审计块、`preretrieval_dedupe` 元数据。

### 5.7 检索架构：现状 vs 目标

| 环节 | 通用实践 | LearnAgent 现状 | 仍缺 |
|------|----------|-----------------|------|
| Query 理解 | 意图分类、实体抽取 | 规则改写 + **query 路由** ✅ | LLM 改写/分类 |
| 稀疏召回 | BM25 / Elasticsearch | keyword + BM25 + CJK | 专业中文分词器 |
| 稠密召回 | Embedding + ANN | 可选 Chroma；Scenario 可配中文 embedding | PR 默认 `--disable-vector` |
| 多路融合 | RRF / 学习排序 | **路由加权 RRF** + doc_type/authority boost ✅ | 学习排序 |
| 精排 Rerank | Cross-encoder | ✅ 可选（默认关）；Nightly profile 可开 | PR 默认仍关 |
| 元数据过滤 | doc_type / 时间 / 租户 | ✅ policy pre-filter + vector metadata（Wave B） | 时间/superseded 过滤 |
| Fallback | 改写后重试 | keyword_search 末级兜底 | 向量默认兜底策略 |

### 5.8 上下文打包（Context Packing）

在线摘录经 **`build_guarded_context`**（`rag/context_guard.py`）包装，而非裸 `format_chunks_for_prompt`：

| 项 | 现状 | 缺口 |
|----|------|------|
| 安全头 | ✅ `[PrivateRAGContext]` 声明（不可信数据、须 cite 文件名） | — |
| 排序 | 融合 score 降序；`RAG_RERANK_ENABLED=true` 时 dedup 后再 Cross-encoder 重排 | — |
| 去重 | ✅ `(source, heading_path)` dedup（检索 `_finalize` + 打包前 `dedup_chunks`） | 文本相似度 dedup |
| 长度 | `select_chunks_for_budget` 按 `RAG_CONTEXT_BUDGET_CHARS`（默认 14000）动态装包 | 无「证据摘要」压缩层 |
| 结构 | 块头含 source / heading / doc_type / API endpoint | 章节级 citation 强制校验 |
| 引用 | ✅ 结构化 `citations[]` + Prompt 要求 cite 文件名 | E2E LLM judge |

**与 checkpoint 边界**：`[PreRetrievedDocs]`（preretrieval）与 tool 摘录进入 messages/tool 结果，**不**把 chunk 全文写入 LangGraph checkpoint；`retrieval_completed` 保留结构化 sources 供 Timeline 审计。

---

## 6. 与 Runtime / 契约 / 可观测的衔接

### 6.1 `retrieval_completed` 事件

当 `RunnableConfig.configurable` 含 `conversation_id`/`thread_id` 与 `run_id` 时，`search_docs` 成功后写入 EventStore：

```text
payload (build_retrieval_completed_payload)
├── query
├── call_id?              ← 与 search_docs tool_start 对齐（GraphEventMapper 注入）
├── sources[]:
│     source_file
│     section_title?
│     heading_path?
│     doc_type?
│     start_line
│     chunk_index           ← 来自 DocChunk.chunk_index
│     http_method?          ← 来自 DocChunk.api_endpoint
│     http_path?
│     request_field_names[]
│     error_codes[]
├── source_count, excerpt_chars, success
├── retrieval_mode?, retrieval_route?
```

`search_docs` 返回 LLM 的 `data` additionally 含：

```text
citations[]             ← citations_from_chunks(hits)
suggested_api_paths[]   ← enrich_retrieval_payload → extract_api_paths
api_field_hints[]       ← 各 hit 的 endpoint / request_fields / error_codes 摘要
context_guard{}         ← budget / truncated / source_files 审计
preretrieval_dedupe{}   ← 与 turn 内预检索去重元数据
```

Timeline 投影为 `kind: "retrieval"`；`call_id` 与 `kind: tool` 可关联（见 `verify_runtime_timeline.py`）。

### 6.2 Turn 前预检索（Preretrieval）

`context/preretrieval.py` 在 planner 路由推荐 `search_docs` 时，**LLM 调用 tool 之前**自动检索：

- 开关：`CONTEXT_PRERETRIEVAL_ENABLED=true`（默认开）
- 预算：`min(CONTEXT_PRERETRIEVAL_BUDGET_CHARS, total_budget/2)`，默认 cap 3500 字符
- 路径：与 `search_docs` 相同 — `build_retrieval_request` + `policy_aware_search_docs` + `build_guarded_context`
- 输出：注入 `SystemMessage`，前缀 `[PreRetrievedDocs]`，提示 LLM 优先使用已有摘录、仅在需要时再 `search_docs`

同 turn 内再次 `search_docs` 时，`context/preretrieval_dedupe.py` 可跳过与预检索完全重复的 chunk（`CONTEXT_PRERETRIEVAL_DEDUPE_ENABLED`）。

### 6.3 System Prompt 与 Tool-grounded 行为

`agent/prompts.py` 要求：部署/队列/已知问题用 `search_docs` 并**引用文件名**；实时状态用 `http_get`；文档与 API 均无依据时明确说明。

**Tool-grounded 编排**（planner 规则路由、检索 path 注入、排障模板、双层闸门）见 **[tool-design.md](./tool-design.md)**；M10 负责证据检索与结构化 metadata，M06 负责工具顺序与 enforcement。

### 6.4 MemoryManager 中的 RAG

`MemoryManager` 持有同一 `RagStore` 引用；热 reload 时 `reload_rag_store()` swap 实例。

| 方法 | 用途 |
|------|------|
| `policy_aware_search_docs` | **生产主路径**（tool + preretrieval） |
| `search_docs` / `search_docs_detailed` | 无策略过滤的便捷封装（评测脚本等） |

`build_context` 在 meta 中暴露 `rag_enabled`、`rag_chunks` 计数，**不**把全文 chunk 注入 checkpoint。长期记忆策略见 [memory-checkpoint-design.md](./memory-checkpoint-design.md)。

---

## 7. 热更新与增量向量索引

改 Scenario `docs_dir`（或 `COPILOT_DOCS_PATH`）下 Markdown 后，无需重启 Agent 进程即可更新检索。

### 7.1 两阶段 reload（关键词即时 + 向量增量/异步）

```text
docs_source_fingerprint()          # 全局 mtime+size，触发 reload
        │
        ├─ POST /v1/rag/reload / watch
        v
[阶段 1 — 同步、毫秒~秒级]
  load_chunks() → RagStore.replace_chunks()
  search_docs 关键词路径立即反映变更

[阶段 2 — 默认异步，RAG_USE_VECTOR=true]
  sync_vector_index()
    ├─ compute_delta(rag_manifest.json)  # 仅 changed/removed 文件
    ├─ Chroma delete(chunk_id…) / upsert(变更文件 chunk)
    └─ RagStore.update_vector_index()   # 完成后混合检索恢复
```

**chunk_id**：`{source}:{start_line}:{content_hash(text)[:16]}`，稳定可 upsert。  
**manifest**：`storage/chroma/rag_manifest.json` 记录每文件 `file_fp` + `chunk_ids`。

未变更文件：**不** re-chunk、**不** re-embed。1000 文件中改 1 个 → 仅该文件 chunk 进入 upsert。

### 7.2 HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/rag/status` | 含 `vector_index_status`（`ready`/`rebuilding`/`stale`/`failed`/`disabled`）、`last_vector_sync`、`vector_rebuilding` |
| POST | `/v1/rag/reload` | 关键词即时 refresh；向量默认异步增量 sync |

### 7.3 配置

| 变量 / Settings | 默认 | 作用 |
|-----------------|------|------|
| `RAG_HOT_RELOAD_ENABLED` | `true` | 后台 fingerprint 轮询 |
| `RAG_HOT_RELOAD_POLL_SECONDS` | `2.0` | 轮询间隔 |
| `RAG_VECTOR_ASYNC_RELOAD` | `true` | 热更新时向量异步；`false` 则阻塞直到 upsert 完成 |
| `RAG_REBUILD_INDEX` | `false` | `true` 时清空 collection 并全量 upsert |

### 7.4 验证

```bash
python scripts/verify_rag_hot_reload.py
python scripts/verify_rag_domain.py --case retrieval_quality
python scripts/verify_rag_rerank.py
```

**范围**：manifest 跟踪 Scenario 目录内 glob 匹配文件；上传 API 可注册新文件。

---

## 8. 配置项

| 变量 / Settings | 默认 | 作用 |
|-----------------|------|------|
| `COPILOT_DOCS_PATH` | — | 覆盖文档根目录 |
| `RAG_USE_VECTOR` / `rag_use_vector` | `false` | 是否构建 Chroma 向量索引 |
| `RAG_REBUILD_INDEX` / `rag_rebuild_index` | `false` | 强制重建向量集合 |
| `RAG_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | HuggingFace 嵌入模型；active Scenario 可覆盖（如 watermark → `bge-small-zh-v1.5`） |
| `RAG_CHROMA_PATH` | `storage/chroma` | Chroma 持久化目录 |
| `RAG_KEYWORD_WEIGHT` | `0.5` | RRF/线性融合中 keyword 路权重 |
| `RAG_VECTOR_WEIGHT` | `0.5` | RRF/线性融合中 vector 路权重 |
| `RAG_BM25_WEIGHT` | `1.0` | RRF/线性融合中 BM25 路权重 |
| `RAG_VECTOR_TOP_K` | `12` | 向量召回候选数 |
| `RAG_QUERY_ROUTE_ENABLED` | `true` | sparse/dense/hybrid 动态 BM25+向量权重 |
| `RAG_USE_BM25` | `true` | Okapi BM25 稀疏路 |
| `RAG_USE_RRF` | `true` | RRF 融合；`false` 时回退线性加权 |
| `RAG_RRF_K` | `60` | RRF 常数 k |
| `RAG_DOC_TYPE_BOOST_ENABLED` | `true` | doc_type 静态 + query hint 加权 |
| `RAG_DEDUP_RESULTS` | `true` | 同 `(source, heading_path)` 去重 |
| `RAG_FUSION_CANDIDATE_MULTIPLIER` | `4` | 融合后候选倍数（rerank 关时使用） |
| `RAG_RERANK_ENABLED` | `false` | Cross-encoder 精排 |
| `RAG_RERANK_MODEL` | `BAAI/bge-reranker-base` | 精排模型 |
| `RAG_RERANK_CANDIDATES` | `50` | 进入 rerank 的融合候选数 |
| `RAG_RERANK_MAX_CHARS` | `512` | rerank 输入截断 |
| `RAG_QUERY_REWRITE_ENABLED` | `true` | 规则 query 扩展（Scenario overlay 注入规则表） |
| `RAG_AUTHORITY_BOOST_ENABLED` | `true` | manifest `authority` 参与融合打分 |
| `RAG_CONTEXT_BUDGET_CHARS` | `14000` | `search_docs` 摘录字符预算 |
| `PRIVATE_RAG_REQUIRE_CITATIONS` | `true` | context guard 要求 cite 文件名 |
| `PRIVATE_RAG_OUTPUT_GUARD_ENABLED` | `true` | 输出侧敏感信息检测（`detect_sensitive_output`） |
| `PRIVATE_RAG_OUTPUT_GUARD_BLOCK` | `true` | 检测到敏感输出时阻断 |
| `CONTEXT_PRERETRIEVAL_ENABLED` | `true` | Turn 前自动检索 |
| `CONTEXT_PRERETRIEVAL_BUDGET_CHARS` | `3500` | 预检索摘录预算 |
| `CONTEXT_PRERETRIEVAL_DEDUPE_ENABLED` | `true` | `search_docs` 与预检索去重 |
| `AGENT_TOOL_ROUTE_ENABLED` | `true` | Planner 注入 tool 路由 SystemMessage |
| `AGENT_TOOL_ROUTE_ENFORCE` | `true` | safety_gate 拦截偏离路由的 tool |
| `RAG_HOT_RELOAD_ENABLED` | `true` | 文档变更自动 reload |
| `RAG_HOT_RELOAD_POLL_SECONDS` | `2.0` | 自动 reload 轮询间隔 |
| `RAG_VECTOR_ASYNC_RELOAD` | `true` | 热更新向量异步增量 sync |
| `HF_HOME` | — | 嵌入模型缓存（`apply_hf_home`） |

CI 行为见 [ci-design.md](./ci-design.md)；RAG 代理指标见 `phase4_ragas`（`--mode proxy --disable-vector --allow-missing-docs`）。

---

## 9. 评测与 CI

### 9.1 数据集

`eval/phase4-eval-cases.json` 中 `category: "docs"` 的 case 当前 **20** 条（P4-001～P4-005、P4-012、P4-015～P4-028），覆盖 9 份文档，字段：

- `question`
- `required_sources`（期望出现在检索结果 `source` 集合中）
- `expected_tools: ["search_docs"]`（供 Agent 编排评测，非 `phase4_ragas` 本脚本使用）

同文件另有 `api` / `safety` case，由其他 verify 脚本消费。

### 9.2 Proxy 指标（`verify_phase4_ragas.py`）

对每条 docs case 调用 `build_rag_store().search(question, top_k=6)`：

| 指标 | 含义 |
|------|------|
| `required_source_coverage` | `required_sources` 命中比例 |
| `required_source_full_match` | 是否全部 required 命中 |
| `retrieval_hit_rate` | 是否至少返回 1 个 chunk |

**CI 通过阈值**（proxy）：`docs_cases >= 3` 且 `retrieval_hit_rate >= 0.9` 且 `required_source_full_match_rate >= 0.6`。

**当前本地基线**（`scenarios/watermark/docs` + `--disable-vector`）：20 case 通常可达 `full_match_rate = 1.0`；结果写入 `artifacts/phase4/phase4-ragas-summary.json`。

文档缺失时：`--allow-missing-docs` → `phase4_ragas=SKIP`；`rag_hot_reload` 在无 Scenario docs 时也可 SKIP。

### 9.3 RAGAS 轨道

`--mode auto|ragas` 在具备 `OPENAI_API_KEY` 且 ragas 可 import 时尝试 `faithfulness` / `answer_relevancy`；否则仅 proxy。  
聚合 profile 见 [ci-design §5](./ci-design.md)；分层语义见 [eval-design §4](./eval-design.md)。

**proxy 测的是检索是否命中预期文档**，不测 LLM 最终回答质量或 Agent 是否选对 tool。

### 9.4 分层评测模型（现状 vs 目标）

| 层级 | 指标 | 现状 | 目标 |
|------|------|------|------|
| L1 检索 | Recall@k、MRR、required_source 命中 | ✅ proxy（`required_source_*` + **`gold_chunk_recall_at_k` / MRR**） | chunk_id 级更细标注 |
| L2 上下文 | 摘录是否含答案句、重复率 | ✅ overlap / truncation proxy | chunk 答案句标注 |
| L3 生成 | faithfulness、answer relevancy | ✅ Nightly E2E RAGAS（`verify_rag_e2e_ragas`） | PR 硬门禁仍 ❌ |
| L4 引用 | citation accuracy、文件名对齐 | ✅ L4-lite + **structured citations** | E2E LLM judge + 章节级对齐 |
| L5 Agent E2E | tool 选择、先 RAG 后 API | ✅ proxy（28 case + Demo 6 golden） | 真实 LLM 轨迹 |

**原则**：L1 + L4-lite + L5 proxy 已纳入 `--profile rag|e2e`；L3 RAGAS 仍非 PR 硬门禁。

L5 工具轨迹、Demo golden 脚本分工见 [tool-design §3.9](./tool-design.md) 与 [eval-design §4.4](./eval-design.md)。聚合命令见 [README §6](../README.md)。

### 9.5 数据集规范（扩展方向）

当前 `phase4-eval-cases.json` docs case 字段（v1.3.0）：

```json
{
  "question": "...",
  "required_sources": ["DEPLOY-SERVER.md"],
  "expected_tools": ["search_docs"],
  "question_type": "factual",
  "gold_chunks": [{"source": "DEPLOY-SERVER.md", "start_line": 38}],
  "must_not_sources": []
}
```

| 字段 | 用途 | 状态 |
|------|------|------|
| `gold_chunks`（`source` + `start_line`） | L1 Recall@k / MRR | ✅ Wave A |
| `question_type` | `factual` / `procedural` / `troubleshooting` / `api_lookup` | ✅ Wave A |
| `must_not_sources` | 负样本：不应召回的文档 | ✅ Wave A（可选） |
| `requires_api_after_rag` | E2E：排障类应先 `search_docs` 再 `http_get` |

问题类型分布目标：事实查表 ~40%、流程 ~30%、排障 ~20%、API 契约 ~10%，与 Demo 文档类型对齐。

### 9.6 优化诊断工作流

检索或回答质量回归时，建议按序排查（对应 §5.5）：

```text
1. 跑 proxy：verify_phase4_ragas.py --disable-vector
   └─ full_match 下降 → 看 summary 中 miss 的 required_sources

2. 单 query 复现：build_rag_store().search(question, top_k=8)
   └─ 看返回 chunk 的 source / heading_path / score

3. 分因：
   ├─ 无 ASCII token → 中文 fallback / 启用向量 / 查询改写
   ├─ 文档在库但未进 top-k → 分块边界、BM25/rerank、doc_type boost
   ├─ 检索对但回答错 → L3 RAGAS / prompt / 模型
   └─ 未调 search_docs → L5 E2E / Tool-grounded 节点

4. 改 ingest 或 reload 后：verify_rag_hot_reload + verify_rag_domain.py --case retrieval_quality
5. 向量变更后：对比 --disable-vector vs --enable-vector 同一 case 集
```

---

## 10. 文档关系

- **上游**：[data-flow-design.md](./data-flow-design.md)（`ToolResultModel`）、[tool-design.md](./tool-design.md)（编排）
- **下游**：Demo 验收、[eval-design §4.4](./eval-design.md)（RAG/L5 分层）
- **全量索引**：[agent-learning-guide §6](./agent-learning-guide.md)

---

## 11. 未来优化方向

路线图波次见 [agent-learning-guide.md](./agent-learning-guide.md) §7。本节保留技术细节；**按层任务清单**见下方 **§11.0**。

### 11.0 八层栈改造分配（待办）

Wave1 已完成项见 **§0**。路线图索引：[agent-learning-guide §7](./agent-learning-guide.md)。

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **A** | L2 | ~~向量 PR/Nightly profile 分离；gold chunk 指标；CJK bigram~~ | ✅ `phase4_ragas_nightly` + `rag_metrics/` |
| **B** | L1–L2 | ~~doc_security ingest；tenant 贯通；policy+vector 共存；authority dedup~~ | ✅ `verify_rag_domain.py --case doc_security_ingest` + `verify_policy_aware_rag_v1` |
| **C** | L2–L3–L7–L8 | ~~structured citations；L2 context metrics；RAG E2E RAGAS；metrics history~~ | ✅ `verify_rag_e2e_ragas` + `rag_metrics/history/` |
| **2** | L2 | RAGAS 夜跑 faithfulness 趋势告警 | `--profile full` + `rag_e2e_ragas` |
| **4** | L1 | 网页 crawl / DB 同步 ingest | 立项后单独立项 |
| **4** | L2 | PDF/HTML/OCR preprocess 插件 | 非 MVP |

### 11.1 Ingest 与知识库运维

- ~~Kernel 硬编码文件名 → **glob + manifest**~~ ✅ Scenario `docs_manifest.json`
- ~~API 文档专用解析（method / path / 字段表 / **response JSON**）~~ ✅ `api_parse.py`
- ~~上传新 md 自动纳入索引~~ ✅ `POST /v1/rag/upload`；多租户 collection 隔离（远期）。

### 11.2 检索质量

§11.2 前八项 **已实现**（2026-05）：Scenario overlay 改写、`tokenize`（CJK + 2-gram）、BM25、RRF、`doc_type`/`authority` boost、`dedup_chunks`、动态 top-k、context guard。验收：`python scripts/verify_rag_domain.py --case retrieval_quality` + `verify_phase4_ragas.py`。

**仍待做**：

1. ~~**动态 top-k** — 按 context 预算截断（§5.8）。~~ ✅ Wave1
2. **LLM 查询改写** — 替代/补充规则表。
3. ~~**向量 Nightly profile + 中文 embedding**~~ ✅ Wave A（PR 仍 `--disable-vector`；Nightly `BAAI/bge-small-zh-v1.5` + rerank）

### 11.3 Tool-grounded 编排

编排细节（路由、path 注入、排障模板、L5 评测）见 **[tool-design.md](./tool-design.md)**；本文档只覆盖检索与 ingest 侧。

### 11.4 评测与可观测

与 §9.4 分层模型对齐；L5/L4 脚本分工见 [eval-design §4.4](./eval-design.md)。全局缺口见 [guide §2.8](./agent-learning-guide.md)。

---

## 12. 遗留问题

| 问题 | 影响 | 说明 |
|------|------|------|
| Rerank 默认关闭 | 多路召回 top-k 可能含噪声 | 已实现 Cross-encoder，但 `RAG_RERANK_ENABLED=false` |
| 向量默认关闭 | 语义召回弱 | `rag_use_vector=false` |
| 规则改写覆盖有限 | 新口语/新术语需补 Scenario overlay | `config/<scenario>-rag.yaml` |
| 测试库非生产文档 | 接入真实平台需替换内容 | `scenarios/watermark/docs` 为虚构 Demo |

跨模块缺口（真实 LLM E2E、RAGAS PR 门禁等）见 [guide §2.8](./agent-learning-guide.md)。

---

## 13. 非目标（本文档不覆盖）

以下能力在通用 RAG 面试中常见，但 **不属于 M10 MVP** 或已归属其他模块：

| 非目标 | 归属 / 说明 |
|--------|-------------|
| HTTP 白名单与审批 | M11/M12 → [guardrail-policy-design.md](./guardrail-policy-design.md)（Scenario `HttpPathPolicy`） |
| Run 状态机、cancel、SSE | M03 → [runtime-design.md](./runtime-design.md) |
| Episodic / checkpoint 压缩 | M09 → [memory-checkpoint-design.md](./memory-checkpoint-design.md) |
| 通用 Trace、token 账单 | M13 → [observability-design.md](./observability-design.md) |
| GraphRAG / 知识图谱 | 远期；当前无图索引 |
| Text-to-SQL / 结构化 DB 检索 | 平台状态走 `http_get`，非向量库 |
| 多模态（图片/PDF OCR RAG） | 当前仅 Markdown ingest |
| 外部向量 SaaS 替换 Chroma | 当前阶段不纳入 MVP |
| Fine-tuning 替代 RAG | 见 §1.2；文档变更场景不采用 |

---

## 14. 本地验证与学习路径

### 14.1 一键评测

```bash
# 默认读 active Scenario docs；watermark Demo 请显式设置 SCENARIO=watermark
python scripts/verify_phase4_ragas.py --mode proxy --disable-vector

# 查看逐 case 命中
type artifacts\phase4\phase4-ragas-summary.json

python scripts/verify_rag_hot_reload.py
python scripts/verify_rag_domain.py --case api_ingest
python scripts/verify_rag_domain.py --case api_path_extraction
python scripts/verify_policy_aware_rag_v1.py

# 聚合 profile 见 README §6 / ci-design §7
```

### 14.2 建议阅读顺序

1. **§0 状态表** → 已实现 vs 缺口一览  
2. **§2.1 + §2.2 + §5.5** → 通用 RAG 链路与本项目进程内数据流  
3. **`scenarios/watermark/docs/` + `config/watermark-rag.yaml`** → Demo 语料、manifest、改写规则  
4. **`rag/ingest.py` + `rag/docs_manifest.py` + `rag/api_parse.py`** → manifest、分块、API 结构化元数据  
5. **`rag/reload.py` + §7** → 热更新与增量向量  
6. **`rag/retriever.py` + `rag/fusion.py` + §5.3～§5.6** → 混合检索、RRF、policy 过滤  
7. **`rag/context_guard.py` + `context/preretrieval.py` + §6.2** → 上下文护栏与预检索  
8. **`agent/tool_handlers.py` + §6.1** → tool、事件、citations  
9. **[tool-design.md](./tool-design.md)** → 编排层 Tool-grounded  
10. **`eval/phase4-eval-cases.json` + §9.4～§9.6** → proxy 与分层评测

### 14.3 可选：启用向量

```bash
set RAG_USE_VECTOR=true
python scripts/verify_phase4_ragas.py --mode proxy --enable-vector
```

需安装 `requirements-vector.txt` 依赖并允许首次 embedding 模型下载。
