# LearnAgent Eval 设计

> 自动化评测的分层职责、Profile 协议、数据集与 CI 策略。  
> **套件权威列表**：`scripts/verify_eval_suite.py`；CI 行为见 [ci-design.md](./ci-design.md)。

---

## 0. 实现状态

| 项 | 状态 | 验收 |
|---|---|---|
| 聚合入口 | ✅ | `verify_eval_suite.py` |
| PR 门禁 | ✅ | `--profile core` + `--profile rag` |
| K/C/S Contract 套件 | ✅ | 9 项在 `CONTRACT_SUITES` |
| L5 工具轨迹 | ✅ | `phase4_tool_trajectory`（28 case） |
| Demo golden proxy | ✅ | `--profile e2e` |
| RAGAS PR 硬门禁 | ❌ | 见 [guide §2.8](./agent-learning-guide.md) |
| 真实 LLM E2E | ❌ | 见 [guide §2.8](./agent-learning-guide.md) |
| Promptfoo 编排 | ❌ | 见 [guide §2.8](./agent-learning-guide.md) |

---

## 1. 设计目标

| 问题 | 设计回应 |
|---|---|
| 断言口径不一致 | 子套件 `checks` + 聚合 `eval-suite-summary.json` |
| Runtime 语义难回归 | contract → runtime → golden → rag 分层 |
| PR 与深度评测冲突 | PR：deterministic core + rag；语义：full + RAGAS |
| 失败难定位 | `failed_suites` / `contract_metrics` / 各套件 `summary_json` |

**原则**：主路径不依赖 LLM、不依赖外网；LLM judge / RAGAS 为增强轨。

---

## 2. 聚合架构

```text
                 verify_eval_suite.py
                         │
     ┌───────────────────┼───────────────────┐
     ▼                   ▼                   ▼
 core-fast (13)      core (21)          rag (11)
 本地快检            PR 门禁之一          PR 门禁之二
     │                   │                   │
 Contract 快集      Contract 全量         RAG + L5 + 路由
 Runtime 核心       Runtime/Memory        ingest/citation
 Phase3/4 部分      Golden + Legacy       hot_reload/rerank
                         │
                    e2e (1) ──► full (33) = core + rag + e2e
```

---

## 3. Profile 定义

| Profile | 套件数 | 用途 | CI |
|---|---:|---|---|
| `core-fast` | 13 | 本地快检 | ❌ |
| `core` | 21 | Contract + Runtime + Memory + Golden + Legacy | ✅ PR |
| `rag` | 11 | RAG + Tool-grounded + L5 轨迹 | ✅ PR |
| `e2e` | 1 | Demo 1–6 golden proxy | Nightly（full） |
| `full` | 33 | 发版 / 夜跑 | ✅ schedule |

**套件枚举与本地命令**（SSOT）：[ci-design.md](./ci-design.md) §3–§7；最短操作入口：[README.md](../README.md) §6。

---

## 4. 分层职责

### 4.1 Contract 层

验证 [data-flow-design.md](./data-flow-design.md) 契约：

- `RuntimeEvent` / payload schema（`contracts/validate.py`）
- `ToolResultModel` 审计字段
- eval JSON 与事件 kind 可解析

**K/C/S 专项**（均在 `core` 的 `CONTRACT_SUITES`）：

| 套件 | 职责 |
|---|---|
| `scenario_loader` | Scenario YAML、HttpPathPolicy、docs manifest |
| `mcp_capability` | MCP 注册与 mock/stdio |
| `context_manager` | `ContextManager.assemble`、`context_built` | 见 [context-manager-design.md](./context-manager-design.md) |
| `policy_credentials` | CredentialManager + PolicyGate `required_scopes` + `credential_binding_audit` |
| `policy_docs_contract` | Policy 设计文档与实现契约 |

单跑调试：`verify_policy_credentials.py`、`verify_context_manager.py`。

### 4.2 Runtime 层

EventStore、Timeline、ExecutionEngine、Session、Memory 的确定性行为：

- 事件 FSM、审批/取消、checkpoint 链接
- `session_mvp`：ExecutionEngine 并发/超时/rehydrate + ChatRunner 危险 POST 审批链

### 4.3 Golden 层

- `eval/golden/runtime-golden-scenarios.json`：Run 级事件契约声明
- `eval_cases_contract`：JSON 形状 + 样本 payload 可校验
- 完整 Agent E2E：`verify_demo_golden_e2e.py`（e2e profile）

### 4.4 RAG / L5 层

| 类型 | 代表套件 | 与单测脚本关系 |
|---|---|---|
| 检索质量 | `phase4_ragas`、`rag_retrieval_quality` | 后者含 query router 权重 |
| 路由分类 | `tool_router` | 28 case，**不跑图** |
| 工具轨迹 | `phase4_tool_trajectory` | 28 case，**跑完整 L5 图** |
| Ingest / 引用 | `rag_api_ingest`、`citation_l4` | |
| 运维 | `rag_hot_reload`、`rag_rerank` | 可 SKIP 段 |

**分工**：`tool_router` = 路由决策；`phase4_tool_trajectory` = 路由 + safety_gate + 工具执行序列。

---

## 5. 聚合协议

### 5.1 子套件约定

每个 `verify_*.py` 宜输出：

- stdout：`suite_name=PASS|FAIL|SKIP` 或 `verify_*=PASS`
- 可选：`summary_json=artifacts/...`

`checks` 字典示例：

```json
{
  "suite_name": "policy_credentials",
  "status": "PASS",
  "checks": {
    "credentials_binding_id_from_scenario": true,
    "policy_scope_allowed_emits_audit": true
  }
}
```

### 5.2 聚合 summary 字段

| 字段 | 含义 |
|---|---|
| `overall_pass` | 无 FAIL 套件 |
| `failed_suites` | 失败套件名 |
| `skipped_suites` | SKIP 套件名 |
| `contract_schema_ok` | Contract 套件 schema 通过 |
| `contract_metrics` | 各 Contract 套件 checks |
| `rag_metrics` | `phase4_ragas` 的 proxy/RAGAS 指标 |
| `runtime_contract_breaks` | runtime_* / session_mvp 失败 |
| `eval_suite` | 控制台总判定 `PASS|FAIL` |

PR 跑两次 profile 时，rag 使用独立路径：`artifacts/eval/eval-rag-summary.json`。

### 5.3 超时与 flaky

- 默认 `--suite-timeout-seconds=180`
- 部分脚本逻辑 PASS 但进程未退出时，聚合层可能记 `timeout_after_pass_signal`（仍可按 PASS 处理）

---

## 6. 数据集

### 6.1 Phase4（`eval/phase4-eval-cases.json`）

28 条：20 docs + 5 api + 3 safety。字段：

- `expected_tools` / `forbidden_tools` / `required_sources`
- `expect_blocked`

用于：`phase4_dataset`、`eval_cases_contract`、`tool_router`、`phase4_tool_trajectory`、`phase4_ragas`。

### 6.2 Golden Runtime（`eval/golden/runtime-golden-scenarios.json`）

Run 级 `must_have_events` / `expected_run_status`；由 `golden_scenarios` 套件校验。

### 6.3 Demo Golden（`eval/golden/demo-golden-scenarios.json`）

Demo 1–6；由 `demo_golden_e2e`（e2e profile）校验。

---

## 7. 评测框架定位

| 方案 | 角色 |
|---|---|
| 自研 `verify_*` | **主门禁** |
| RAGAS | RAG 专项；full + `--enable-ragas` |
| Promptfoo | 规划中的场景编排层 |
| DeepEval / LangSmith | 可选增强，非 PR 门禁 |

---

## 8. 本地速查

见 [README.md](../README.md) §6（最短命令）与 [ci-design.md](./ci-design.md) §7–§8（完整复现 + 失败排查）。

---

## 9. 已知缺口

模块独有项见各 design doc。跨模块清单见 [agent-learning-guide §2.8](./agent-learning-guide.md)。

| 项 | 说明 |
|---|---|
| `verify_mvp_runtime_acceptance` | 未纳入 eval_suite；需 API key |
| `session_mvp` 耗时长 | core 可达数分钟 |

---

## 10. 非目标

- 不重复 [ci-design.md](./ci-design.md) 的 Job 步骤与排查表
- 不把 LLM judge 作为 PR 硬门禁（除非策略变更）
- 不替代 Langfuse / 生产监控
