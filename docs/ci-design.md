# LearnAgent CI 设计

> GitHub Actions 工作流、本地复现命令与失败排查。套件清单以 `scripts/verify_manifest.py` 和 `scripts/verify_eval_suite.py` 为准。
> Eval 分层见 [eval-design.md](./eval-design.md)；模块地图见 [agent-learning-guide.md](./agent-learning-guide.md)。

**本文负责**：CI workflow、profile 到 job 的映射、本地复现命令、脚本治理规则。
**本文不负责**：评测指标语义、模块业务设计、单个 verifier 断言细节。
**权威来源/相关文档**：套件枚举以 `scripts/verify_manifest.py` 为准；聚合语义以 `scripts/verify_eval_suite.py` 为准。

---

## 0. 实现状态

| 项 | 状态 | 说明 |
|---|---|---|
| 单一 CI 工作流 | ✅ | `.github/workflows/eval-ci.yml` |
| PR 门禁 | ✅ | 只跑 `--profile core-fast` |
| RAG proxy | ✅ | 独立 `rag` profile，本地或手动运行 |
| Nightly | ✅ | `full` + vector/rerank/RAGAS/E2E 趋势 |
| Live / API | ✅ | manual only，不影响 PR |

---

## 1. Profile 边界

| Profile | 职责 | 是否进入 PR | 外部依赖 |
|---|---|---:|---|
| `core-fast` | 产品交付硬门禁：契约、脚本治理、policy/credential、retrieval gate、runtime event/timeline 快检 | ✅ | 无真实 LLM、无 HF、无 vector/rerank、无 live API |
| `core` | 本地/手动扩展 deterministic suite：session、checkpoint、memory、完整 domain | ❌ | 不要求真实 LLM，但允许 LangGraph/FastAPI 等集成依赖 |
| `rag` | deterministic RAG proxy 与文档安全验证 | ❌ | 强制 `RAG_USE_VECTOR=false`、`RAG_RERANK_ENABLED=false` |
| `full` | Nightly 长链路：core + rag + 重模型趋势 + e2e | schedule / workflow_dispatch | 可用 vector/rerank/RAGAS，失败必须有 artifact |
| `manual/live` | 真实 OpenAI/API key、部署、线上接口 | 手动 | 真实外部服务 |

核心原则：PR CI 是交付门禁，不是能力展示。HF、RAGAS、live LLM、vector/rerank、长 E2E 不作为 PR 必过条件。

---

## 2. Workflow 映射

| Job | 触发 | 命令 | Summary |
|---|---|---|---|
| `eval_core` | `pull_request` / `push` | `python scripts/verify_eval_suite.py --profile core-fast --summary-json artifacts/eval/eval-core-fast-summary.json` | `overall_pass`、`failed_suites`、`failed_cases`、`runtime_contract_breaks`、`duration_ms` |
| `eval_full_nightly` | `schedule` / `workflow_dispatch` | `python scripts/verify_eval_suite.py --profile full --suite-timeout-seconds 180` | nightly status、stage、reason、metrics path、`rag_regression.reason` |

Nightly 的 HuggingFace preload 只是 cache warmup，`continue-on-error: true`。正确性由 `phase4_ragas_nightly` 的 preflight、stage timeout 和 `nightly-latest.json` 决定。

---

## 3. core-fast 准入

`core-fast` 只放稳定、确定、无外部网络、无真实 LLM、无重模型下载的 suite。目标是 30-35 秒内完成。

允许进入：

| 类型 | 示例 |
|---|---|
| 纯契约 | `contract_events`、`eval_cases_contract`、`events_validated` |
| 脚本治理 | `scripts_manifest`、`eval_suite_timeout_v1` |
| 轻量 runtime | `runtime_domain --case event_store`、`runtime_domain --case timeline` |
| policy / credential 快检 | `policy_credentials`、`policy_docs_contract` |
| context/RAG 纯规则 | `context_providers_v1`、`retrieval_gate_v1` |

不允许进入：

| 类型 | 示例 |
|---|---|
| 重模型或向量 | Chroma build、sentence-transformers、CrossEncoder、RAGAS |
| live 依赖 | OpenAI/API key、线上 HTTP、真实 MCP server |
| 长链路集成 | `session_mvp`、完整 `runtime_domain --case all`、完整 `memory_domain` |
| 重包入口 | 因 import 副作用加载 `server.py`、真实 RAG store、LLM provider 的纯函数 verifier |

---

## 4. 失败诊断

Domain verifier 必须在 summary 顶层写出：

```json
{
  "failed_cases": ["timeline"],
  "case_status": {"timeline": "FAIL"},
  "case_reasons": {"timeline": "AssertionError: ..."}
}
```

`verify_eval_suite.py` 会把 domain case 提升为第一屏诊断，例如：

```text
failed_suites=runtime_timeline_gate
failed_cases=runtime_domain:timeline
runtime_contract_breaks=runtime_domain:timeline
```

RAG nightly 必须写 `artifacts/eval/rag_metrics/nightly-latest.json`。聚合语义：

| Reason | 含义 |
|---|---|
| `nightly_metrics_missing` | artifact 真不存在，视为聚合/脚本 bug |
| `nightly_metrics_skipped:<reason>` | model/vector preflight skip |
| `nightly_metrics_timeout:<stage>` | 阶段或 suite timeout |
| `metric_missing` | artifact 存在但趋势字段缺失 |

---

## 5. 本地复现

```powershell
# PR 等价门禁
python scripts/verify_eval_suite.py --profile core-fast --summary-json artifacts/eval/eval-core-fast-summary.json

# 扩展 deterministic 检查
python scripts/verify_eval_suite.py --profile core

# RAG proxy，不启用 vector/rerank/live LLM
python scripts/verify_eval_suite.py --profile rag --summary-json artifacts/eval/eval-rag-summary.json

# Nightly 本地复现，可能因为 HF cache / vector backend 失败，但必须给出具体 reason
python scripts/verify_eval_suite.py --profile full --suite-timeout-seconds 180

# 手工 RAGAS 增强路径
python scripts/verify_eval_suite.py --profile full --enable-ragas --suite-timeout-seconds 180
```

单 case 调试优先使用 domain verifier：

```powershell
python scripts/verify_runtime_domain.py --case timeline
python scripts/verify_rag_domain.py --case retrieval_quality
python scripts/verify_memory_domain.py --case all
python scripts/verify_tool_governance_domain.py --case all
```

---

## 6. 新增 Suite 规则

每个新增 suite 必须声明：

| 字段 | 要求 |
|---|---|
| profile | `core-fast` / `core` / `rag` / `full` / `manual` |
| 预期耗时 | 是否影响 35s PR 预算 |
| 外部依赖 | 是否需要网络、真实 API key、HF cache、vector backend |
| status contract | summary JSON 或 stdout 必须有明确 status key |
| artifact | 失败时能定位到 case/stage/reason |

默认规则：能进 domain verifier 就不要新增物理脚本；能放 `core` 就不要放 `core-fast`；需要 live key、重模型、长 E2E 的只放 `full` 或 `manual`。

