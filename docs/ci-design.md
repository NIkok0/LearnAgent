# LearnAgent CI 设计

> 说明仓库内两条 GitHub Actions 工作流、本地复现命令与失败排查入口。  
> Eval 分层设计见 [eval-design.md](./eval-design.md)；项目模块地图见 [agent-learning-guide.md](./agent-learning-guide.md)。

---

## 1. 工作流一览

| 工作流文件 | 名称 | 触发 | 作用 |
|---|---|---|---|
| [`.github/workflows/agent-ci.yml`](../.github/workflows/agent-ci.yml) | LearnAgent CI | PR / push `main`/`master` | Phase3 图/checkpoint + Phase4 数据集与 RAG proxy（轻依赖） |
| [`.github/workflows/eval-ci.yml`](../.github/workflows/eval-ci.yml) | LearnAgent Eval CI | PR / push；每日 schedule；`workflow_dispatch` | `verify_eval_suite` 聚合门禁（core / full） |

两条流水线 **并行、互不替代**：`agent-ci` 偏 LangGraph 最小回归；`eval-ci` 偏 Runtime/Memory/Contract/Golden 全量 deterministic 套件。

---

## 2. LearnAgent CI（agent-ci.yml）

### 2.1 步骤

| 步骤 | 脚本 | 目的 |
|---|---|---|
| Verify Phase 3 checkpoint | `scripts/verify_phase3_checkpoint.py` | 同 `thread_id` 多轮后 checkpoint 增长、Tool 路径执行、checkpoint 文件存在 |
| Verify Phase 3 safety gate | `scripts/verify_phase3_safety_gate.py` | `confirm_dangerous=false` 时危险 `http_post` 被闸门拦截且不执行工具 |
| Verify Phase 4 dataset | `scripts/verify_phase4_dataset.py` | `eval/phase4-eval-cases.json` 结构与安全样例完备 |
| Verify Phase 4 RAG proxy | `scripts/verify_phase4_ragas.py --mode proxy --disable-vector` | 离线检索指标门禁（不依赖向量库与 LLM） |

依赖：仅安装 `pydantic-settings`、`langgraph`、`langgraph-checkpoint-sqlite`、`langchain-core`（见 workflow）。

### 2.2 Phase 3 判定口径（摘要）

**checkpoint**（`phase3_step4=PASS`）需同时满足：

- `messages_after_turn2 > messages_after_turn1 >= 3`
- checkpoint 文件存在
- 第一轮出现 `ToolMessage`

**safety_gate**（`phase3_safety_gate=PASS`）需同时满足：

- 拦截文案含 `gated`
- 危险 `http_post` 调用次数为 0
- 输出中无 `ToolMessage`

产物（可选本地指定）：`artifacts/phase3/phase3-checkpoint-summary.json`、`artifacts/phase3/phase3-safety-gate-summary.json`。

### 2.3 Phase 4 判定口径（摘要）

**dataset**：case id、category、`expected_tools` / `forbidden_tools` 等结构校验无错误即 PASS。

**ragas proxy**（默认 gate）：`docs_cases >= 3`、`retrieval_hit_rate >= 0.9`、`required_source_full_match_rate >= 0.6`。

人工验收问句与期望工具路径见数据集 [`eval/phase4-eval-cases.json`](../eval/phase4-eval-cases.json)（不再维护独立 Markdown 用例表）。

---

## 3. LearnAgent Eval CI（eval-ci.yml）

### 3.1 Job 分层

| Job | 条件 | 命令 |
|---|---|---|
| `eval_core` | PR / push（非 schedule） | `python scripts/verify_eval_suite.py --profile core` |
| `eval_full_nightly` | cron 或 `workflow_dispatch` 且 `run_full=true` | `python scripts/verify_eval_suite.py --profile full`（schedule 可加 `--enable-ragas`） |

依赖：`requirements.txt` 全量安装。

### 3.2 core profile 套件（当前）

Contract：`contract_events`、`tool_audit_v1`、`eval_cases_contract`  
Runtime / Memory：`golden_scenarios`、`runtime_event_store`、`runtime_timeline`、`runtime_checkpoint_link`、`runtime_run_manager`、`session_mvp`、`memory_checkpoint_consistency`

聚合产物：`artifacts/eval/eval-suite-summary.json`。

Job Summary 字段：`overall_pass`、`suites_failed`、`contract_schema_ok`、`failed_scenarios`、`runtime_contract_breaks` 等。

### 3.3 full profile

`core` + `phase4_ragas`（`auto` / 可选 RAGAS）。文档缺失时若脚本带 `--allow-missing-docs` 可 `SKIP`；Nightly 建议配置 `WATERMARK_DOCS_PATH` 或仓库内 `docs/source` 以获得真实 `rag_metrics`。

---

## 4. 本地复现

在仓库根目录 `E:\code\LearnAgent`（或你的 clone 路径）：

```powershell
# 等价 agent-ci
python scripts/verify_phase3_checkpoint.py
python scripts/verify_phase3_safety_gate.py
python scripts/verify_phase4_dataset.py
python scripts/verify_phase4_ragas.py --mode proxy --disable-vector

# 等价 eval-ci PR 门禁
python scripts/verify_eval_suite.py --profile core

# 夜跑增强
python scripts/verify_eval_suite.py --profile full --enable-ragas

# RAG / Demo 专项（本地，未接入 CI PR 门禁）
python scripts/verify_eval_suite.py --profile rag
python scripts/verify_eval_suite.py --profile e2e
```

单套件调试：直接运行对应 `scripts/verify_*.py`；失败时查看各脚本写的 `artifacts/**/**-summary.json`。

---

## 5. 失败排查（简表）

| 现象 | 优先检查 |
|---|---|
| Phase3 checkpoint FAIL | `langgraph-checkpoint-sqlite`、`storage/` 可写、`copilot_agent/agent/graph.py` |
| Phase3 safety_gate FAIL | `copilot_agent/agent/nodes.py` 中 `safety_gate`、`confirm_dangerous` 传参 |
| Phase4 dataset FAIL | `eval/phase4-eval-cases.json` 字段与 category 约束 |
| Phase4 ragas FAIL | 文档路径、`ingest` 源文件；proxy 模式下检索阈值 |
| eval core FAIL | `eval-suite-summary.json` 中 `failed_suites`；打开对应子套件 summary |
| eval 超时 | `session_mvp` 等长套件；本地可先单独跑失败项 |

---

## 6. 非目标

- 不在本文档维护 Phase4 Step3 overall / baseline 刷新流水线（脚本 `verify_phase4_overall.py` 存在但未接入当前 workflow）
- 不替代 [eval-design.md](./eval-design.md) 中的分层架构与 golden 数据模型说明
