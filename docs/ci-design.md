# LearnAgent CI 设计

> GitHub Actions 工作流、本地复现命令与失败排查。**套件清单以** `scripts/verify_eval_suite.py` **为准**。  
> Eval 分层见 [eval-design.md](./eval-design.md)；模块地图见 [agent-learning-guide.md](./agent-learning-guide.md)。

---

## 0. 实现状态

| 项 | 状态 | 说明 |
|---|---|---|
| 单一 CI 工作流 | ✅ | `.github/workflows/eval-ci.yml`（已移除 `agent-ci.yml`） |
| PR 门禁 | ✅ | `--profile core` + `--profile rag` |
| Nightly | ✅ | `--profile full` + `requirements-vector.txt` + `bge-small-zh-v1.5` |
| 环境 | ✅ | PR：`requirements.txt`；Nightly 追加 `requirements-vector.txt` |

---

## 1. 工作流一览

| Job | 触发 | 命令 | 产物 |
|---|---|---|---|
| `eval_core` | PR / push（非 schedule） | `core` → `rag` | `eval-suite-summary.json` + `eval-rag-summary.json` |
| `eval_full_nightly` | cron / `workflow_dispatch` | `full` + vector/rerank env | `eval-suite-summary.json` + `rag_metrics/nightly-latest.json` |

**合并规则**：PR job 在 core 与 rag 均 `overall_pass=true` 时才算绿。

---

## 2. PR Job（eval_core）

### 2.1 步骤

```text
checkout → pip install -r requirements.txt
  → verify_eval_suite.py --profile core
  → verify_eval_suite.py --profile rag --summary-json artifacts/eval/eval-rag-summary.json
  → Job Summary（合并 core + rag）
```

### 2.2 Profile 与套件数

| Profile | 套件数 | CI 是否跑 |
|---|---:|---|
| `core-fast` | 24 | ❌ 仅本地 |
| `core` | **33** | ✅ |
| `rag` | **15** | ✅ |
| `e2e` | 1 | ❌（在 `full` 中） |
| `full` | 51 | Nightly |

Nightly `eval_full_nightly` 额外步骤：

```text
pip install -r requirements-vector.txt
env: RAG_USE_VECTOR=true, RAG_RERANK_ENABLED=true, RAG_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
  → verify_eval_suite.py --profile full [--enable-ragas on schedule]
  → phase4_ragas_nightly 写入 artifacts/eval/rag_metrics/nightly-latest.json
```

---

## 3. core profile（33 套件）

### 3.1 Contract + K/C/S（14）

| 套件 | 脚本 | 验证点 |
|---|---|---|
| `contract_events` | `verify_contract_events.py` | `RuntimeEvent` round-trip |
| `tool_audit_v1` | `verify_tool_audit_v1.py` | Tool 审计 payload |
| `tool_execution_reliability` | `verify_tool_execution_reliability.py` | Tool timeout / retry |
| `tool_side_effect_ledger_v1` | `verify_tool_side_effect_ledger_v1.py` | side-effect ledger |
| `tool_side_effect_governance_v1` | `verify_tool_side_effect_governance_v1.py` | side-effect policy |
| `policy_decision_audit_v1` | `verify_policy_decision_audit_v1.py` | policy decision audit |
| `eval_cases_contract` | `verify_eval_cases_contract.py` | phase4 / golden JSON 契约 |
| `eval_suite_timeout_v1` | `verify_eval_suite_timeout_v1.py` | suite timeout handling |
| `scenario_loader` | `verify_scenario_loader.py` | Scenario 加载、HTTP 白名单 |
| `mcp_capability` | `verify_mcp_capability.py` | MCP mock + stdio |
| `context_manager` | `verify_context_manager.py` | assemble / `context_built` |
| `policy_credentials` | `verify_policy_credentials.py` | Credential + PolicyGate scope + audit |
| `policy_docs_contract` | `verify_policy_docs_contract.py` | Policy 文档与契约一致性 |
| `events_validated` | `verify_events_validated.py` | `contract_validated` 落库 |

### 3.2 Runtime + Memory + Golden（16）

| 套件 | 脚本 |
|---|---|
| `golden_scenarios` | `verify_golden_scenarios.py` |
| `runtime_event_store` | `verify_runtime_event_store.py` |
| `runtime_timeline` | `verify_runtime_timeline.py` |
| `runtime_checkpoint_link` | `verify_runtime_checkpoint_link.py` |
| `runtime_execution_engine` | `verify_runtime_execution_engine.py` |
| `runtime_durability_v1` | `verify_runtime_durability_v1.py` |
| `checkpoint_consistency_v2` | `verify_checkpoint_consistency_v2.py` |
| `observability_correlation` | `verify_observability_correlation.py` |
| `observability_provider` | `verify_observability_provider.py` |
| `observability_cost_v1` | `verify_observability_cost_v1.py` |
| `plan_module` | `verify_plan_module.py` |
| `hitl_checkpoint_resume` | `verify_hitl_checkpoint_resume.py` |
| `session_mvp` | `verify_session_mvp.py` |
| `memory_checkpoint_consistency` | `verify_memory_checkpoint_consistency.py` |
| `memory_production_v1` | `verify_memory_production_v1.py` |
| `memory_production_v2` | `verify_memory_production_v2.py` |

### 3.3 Legacy 图回归（3，原 agent-ci）

| 套件 | 脚本 | PASS 信号 |
|---|---|---|
| `phase3_checkpoint` | `verify_phase3_checkpoint.py` | `phase3_step4=PASS` |
| `phase3_safety_gate` | `verify_phase3_safety_gate.py` | `phase3_safety_gate=PASS` |
| `phase4_dataset` | `verify_phase4_dataset.py` | `phase4_dataset=PASS` |

---

## 4. core-fast profile（24 套件，本地）

不含：`golden_scenarios`、`runtime_checkpoint_link`、`session_mvp`、memory 三件套、`phase3_checkpoint`、`mcp_capability`、`observability_correlation`、`plan_module`。

含 Contract 快集 + `runtime_event_store` / `runtime_timeline` / `runtime_execution_engine` / `runtime_durability_v1` + L7/observability 快检 + `phase3_safety_gate` / `phase4_dataset`。

用途：提交前 **5–15 分钟** 级反馈；发 PR 前仍应跑完整 `core` + `rag`。

---

## 5. rag profile（domain + 专项套件）

| 套件 | 脚本 | 备注 |
|---|---|---|
| `rag_domain` | `verify_rag_domain.py` | 聚合轻量 deterministic RAG case：authority、API path、API ingest、doc security、retrieval scopes、retrieval quality |
| `private_rag_context_guard_v1` | `verify_private_rag_context_guard_v1.py` | untrusted context header |
| `private_rag_output_guard_v1` | `verify_private_rag_output_guard_v1.py` | 敏感输出检测 |
| `phase4_ragas` | `verify_phase4_ragas.py` | PR：`--disable-vector` proxy |
| `phase4_tool_trajectory` | `verify_phase4_tool_trajectory.py` | 28 case L5 图轨迹 |
| `extract_validate` | `verify_extract_validate.py` | |
| `citation_l4` | `verify_citation_l4.py` | |
| `final_answer_l7` | `verify_final_answer_l7.py` | FinalAnswerModel |
| `tool_message_policy` | `verify_tool_message_policy.py` | ToolMessage 摘要策略 |
| `diagnosis_template` | `verify_diagnosis_template.py` | |
| `tool_router` | `verify_tool_router.py` | 28 case 路由分类 |
| `rag_hot_reload` | `verify_rag_hot_reload.py` | 无 docs 时可 SKIP |
| `rag_rerank` | `verify_rag_rerank.py` | 无 rerank 依赖时跳过 rerank 段 |

新增 RAG 轻量验证优先加入 `verify_rag_domain.py`；只有生命周期、真实 Agent loop、外部模型/LLM、跨 API 端到端验证才新增独立脚本。

单 case 调试优先使用：

```powershell
python scripts/verify_rag_domain.py --case api_ingest
python scripts/verify_rag_domain.py --case retrieval_quality
```

### 5.1 Removed RAG wrappers

以下 RAG 单 case wrapper 已删除，统一改用 `verify_rag_domain.py --case <case>`：

| case | 命令 |
|---|---|
| authority dedup | `python scripts/verify_rag_domain.py --case authority_dedup` |
| API path extraction | `python scripts/verify_rag_domain.py --case api_path_extraction` |
| API ingest | `python scripts/verify_rag_domain.py --case api_ingest` |
| doc security ingest | `python scripts/verify_rag_domain.py --case doc_security_ingest` |
| retrieval scopes | `python scripts/verify_rag_domain.py --case retrieval_scopes` |
| retrieval quality | `python scripts/verify_rag_domain.py --case retrieval_quality` |

变更验收建议：`verify_rag_domain.py --case all`、`verify_eval_suite.py --profile rag`、`verify_eval_suite.py --profile core-fast`。

### 5.2 Nightly RAG 深测（Wave A + Wave C）

| 套件 | 脚本 | 备注 |
|---|---|---|
| `phase4_ragas_nightly` | `verify_phase4_ragas.py` | `--enable-vector` + `bge-small-zh-v1.5` + rerank；L2 context metrics |
| `rag_e2e_ragas` | `verify_rag_e2e_ragas.py` | retrieve→LLM→RAGAS + L4；无 API key 时 SKIP |

产物：
- `artifacts/eval/rag_metrics/nightly-latest.json`（proxy + L2）
- `artifacts/eval/rag_metrics/e2e-latest.json`（RAGAS + citation）
- `artifacts/eval/rag_metrics/history/`（timestamped 快照；gold recall 回归 >0.05 告警）

---

## 6. e2e 与 full

| Profile | 内容 |
|---|---|
| `e2e` | `demo_golden_e2e`（Demo 1–6 proxy） |
| `full` | core（33）+ rag（15）+ nightly（2）+ e2e（1）= **51** |

Nightly schedule 默认带 `--enable-ragas`，仅将 `phase4_ragas` 切为 `--mode auto --disable-vector --allow-missing-docs`；其他 RAG 套件保持各自参数，向量 + rerank 趋势仍由 `phase4_ragas_nightly` 负责。

---

## 7. 本地复现

**环境**：仓库根目录 + `conda run -n learnagent312`（或已激活的 learnagent312 环境）。

```powershell
# 本地快检（不进 PR CI）
conda run -n learnagent312 python scripts/verify_eval_suite.py --profile core-fast

# 等价 PR CI
conda run -n learnagent312 python scripts/verify_eval_suite.py --profile core
conda run -n learnagent312 python scripts/verify_eval_suite.py --profile rag --summary-json artifacts/eval/eval-rag-summary.json

# Demo proxy
conda run -n learnagent312 python scripts/verify_eval_suite.py --profile e2e

# 夜跑
conda run -n learnagent312 python scripts/verify_eval_suite.py --profile full --enable-ragas
```

单套件调试：直接运行 `scripts/verify_*.py`；失败时查看 `artifacts/**/**-summary.json`。

---

## 8. 失败排查

| 现象 | 优先检查 |
|---|---|
| `mcp_capability` FAIL | `pip install mcp>=1.6.0`；无 SDK 时 watermark stdio 段为 SKIP 字符串，会导致 FAIL |
| `phase3_safety_gate` FAIL | 脚本需 `settings.copilot_allow_job_post=True`；Policy 拦截文案应含 `gated` |
| `phase4_tool_trajectory` FAIL | `PolicyRegistry` 需挂 `CredentialManager`；见 `verify_phase4_tool_trajectory.py` |
| `session_mvp` FAIL / 超时 | ChatRunner 段需 `copilot_capabilities=rag,http`；`agent_tool_route_enforce=False`；engine task 清理 |
| `phase4_ragas` FAIL | Scenario docs 路径、`ingest`、proxy 阈值 |
| core / rag 聚合 FAIL | 对应 `eval-suite-summary.json` / `eval-rag-summary.json` 的 `failed_suites` |
| 套件超时 | `--suite-timeout-seconds`（默认 180）；聚合层会保留 stdout/stderr tail；RAGAS auto 另有软超时并回退 proxy |

---

## 9. 非目标

- 不在本文维护各 `verify_*.py` 实现细节（见 [eval-design.md](./eval-design.md)）
- 不替代 [agent-learning-guide.md](./agent-learning-guide.md) 的架构与成熟度表
- 已删除脚本（`verify_phase4_overall`、`verify_credentials_m14` 等）不再文档化


---

## Appendix: Checkpoint Consistency v2

`checkpoint_consistency_v2` is covered by `scripts/verify_checkpoint_consistency_v2.py` and is included in `core-fast`. The suite verifies `checkpoint_consistency_checked`, Timeline `checkpoint.consistency_v2`, missing-checkpoint warning semantics, and debug bundle export.

`scripts/export_run_debug_bundle.py` is a local troubleshooting tool, not a default CI gate. It exports EventStore events, Timeline projection, latest consistency payloads, and checkpoint SQLite raw inspection for one run.

