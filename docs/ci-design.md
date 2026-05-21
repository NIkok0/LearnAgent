# LearnAgent CI 设计

> GitHub Actions 工作流、本地复现命令与失败排查。**套件清单以** `scripts/verify_eval_suite.py` **为准**。  
> Eval 分层见 [eval-design.md](./eval-design.md)；模块地图见 [agent-learning-guide.md](./agent-learning-guide.md)。

---

## 0. 实现状态

| 项 | 状态 | 说明 |
|---|---|---|
| 单一 CI 工作流 | ✅ | `.github/workflows/eval-ci.yml`（已移除 `agent-ci.yml`） |
| PR 门禁 | ✅ | `--profile core` + `--profile rag` |
| Nightly | ✅ | `--profile full`（schedule 默认 `--enable-ragas`） |
| 本地快检 profile | ✅ | `--profile core-fast`（**不进 PR CI**，仅本地） |
| 环境 | ✅ | Python 3.12 + `requirements.txt`；本地建议 `conda run -n learnagent312` |

---

## 1. 工作流一览

| Job | 触发 | 命令 | 产物 |
|---|---|---|---|
| `eval_core` | PR / push（非 schedule） | `core` → `rag` | `eval-suite-summary.json` + `eval-rag-summary.json` |
| `eval_full_nightly` | cron / `workflow_dispatch` | `full`（可选 `--enable-ragas`） | `eval-suite-summary.json` |

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
| `core-fast` | 13 | ❌ 仅本地 |
| `core` | **21** | ✅ |
| `rag` | **11** | ✅ |
| `e2e` | 1 | ❌（在 `full` 中） |
| `full` | 33 | Nightly |

---

## 3. core profile（21 套件）

### 3.1 Contract + K/C/S（9）

| 套件 | 脚本 | 验证点 |
|---|---|---|
| `contract_events` | `verify_contract_events.py` | `RuntimeEvent` round-trip |
| `tool_audit_v1` | `verify_tool_audit_v1.py` | Tool 审计 payload |
| `eval_cases_contract` | `verify_eval_cases_contract.py` | phase4 / golden JSON 契约 |
| `scenario_loader` | `verify_scenario_loader.py` | Scenario 加载、HTTP 白名单 |
| `mcp_capability` | `verify_mcp_capability.py` | MCP mock + stdio |
| `context_manager` | `verify_context_manager.py` | assemble / `context_built` |
| `policy_credentials` | `verify_policy_credentials.py` | Credential + PolicyGate scope + audit |
| `policy_docs_contract` | `verify_policy_docs_contract.py` | Policy 文档与契约一致性 |
| `events_validated` | `verify_events_validated.py` | `contract_validated` 落库 |

### 3.2 Runtime + Memory + Golden（9）

| 套件 | 脚本 |
|---|---|
| `golden_scenarios` | `verify_golden_scenarios.py` |
| `runtime_event_store` | `verify_runtime_event_store.py` |
| `runtime_timeline` | `verify_runtime_timeline.py` |
| `runtime_checkpoint_link` | `verify_runtime_checkpoint_link.py` |
| `runtime_execution_engine` | `verify_runtime_execution_engine.py` |
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

## 4. core-fast profile（13 套件，本地）

不含：`golden_scenarios`、`runtime_checkpoint_link`、`session_mvp`、memory 三件套、`phase3_checkpoint`、`mcp_capability`。

含 Contract 核心 + `runtime_event_store` / `runtime_timeline` / `runtime_execution_engine` + `phase3_safety_gate` / `phase4_dataset`。

用途：提交前 **5–15 分钟** 级反馈；发 PR 前仍应跑完整 `core` + `rag`。

---

## 5. rag profile（11 套件）

| 套件 | 脚本 | 备注 |
|---|---|---|
| `phase4_ragas` | `verify_phase4_ragas.py` | 默认 proxy；`--allow-missing-docs` 可 SKIP |
| `phase4_tool_trajectory` | `verify_phase4_tool_trajectory.py` | 28 case L5 图轨迹 |
| `rag_api_path_extraction` | `verify_rag_api_path_extraction.py` | |
| `rag_api_ingest` | `verify_rag_api_ingest.py` | |
| `extract_validate` | `verify_extract_validate.py` | |
| `rag_retrieval_quality` | `verify_rag_retrieval_quality.py` | 含 query router 权重 |
| `citation_l4` | `verify_citation_l4.py` | |
| `diagnosis_template` | `verify_diagnosis_template.py` | |
| `tool_router` | `verify_tool_router.py` | 28 case 路由分类 |
| `rag_hot_reload` | `verify_rag_hot_reload.py` | 无 docs 时可 SKIP |
| `rag_rerank` | `verify_rag_rerank.py` | 无 rerank 依赖时跳过 rerank 段 |

---

## 6. e2e 与 full

| Profile | 内容 |
|---|---|
| `e2e` | `demo_golden_e2e`（Demo 1–6 proxy） |
| `full` | core（21）+ rag（11）+ e2e（1）= **33** |

Nightly schedule 默认带 `--enable-ragas`，将 `phase4_ragas` 切为 `--mode auto`。

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
| 套件超时 | `--suite-timeout-seconds`（默认 180）；长套件：`session_mvp`、memory 系列 |

---

## 9. 非目标

- 不在本文维护各 `verify_*.py` 实现细节（见 [eval-design.md](./eval-design.md)）
- 不替代 [agent-learning-guide.md](./agent-learning-guide.md) 的架构与成熟度表
- 已删除脚本（`verify_phase4_overall`、`verify_credentials_m14` 等）不再文档化
