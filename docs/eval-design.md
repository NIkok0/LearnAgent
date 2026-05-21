# LearnAgent Eval 设计

> 说明 LearnAgent 如何组织自动化评测：分层职责、聚合协议、场景数据与 CI 策略。  
> 关联文档：[agent-learning-guide.md](./agent-learning-guide.md)、[rag-design.md](./rag-design.md)、[runtime-design.md](./runtime-design.md)、[data-flow-design.md](./data-flow-design.md)、[tech-selection-design.md](./tech-selection-design.md)、[demo-requirements-design.md](./demo-requirements-design.md)、[ci-design.md](./ci-design.md)

---

## 1. 设计目标

Eval 体系要解决的不是「有没有测试脚本」，而是四类问题：

| 问题 | 设计回应 |
|---|---|
| 模块改一处、多处断言口径不一致 | 统一 `checks` + 聚合 `eval-suite-summary.json` |
| Runtime 契约与产品语义难以回归 | 分层：`contract` → `runtime` → `golden` → `rag` |
| PR 门禁与深度评测需求冲突 | **PR 跑 deterministic core**；RAG / judge 走 full 或夜跑 |
| 失败难以定位 | 子套件独立 `summary_json`，聚合层输出 `failed_suites` / `contract_metrics` |

门禁原则：**主路径不依赖 LLM、不依赖外网**；语义质量评测作为增强轨道，默认不阻塞主干合并。

---

## 2. 分层架构

```text
                    verify_eval_suite.py  （聚合入口）
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   Contract 层           Runtime 层            场景 / RAG 层
   （契约形状）          （模块行为）           （数据驱动 / 检索质量）
        │                     │                     │
   contract_events      event_store            golden_scenarios
   tool_audit_v1          timeline             eval_cases_contract
   eval_cases_contract    checkpoint_link      phase4_ragas (rag)
                        run_manager
                        session_mvp
                        memory_checkpoint_...
```

### 2.1 Contract 层

验证 [data-flow-design.md](./data-flow-design.md) 中的契约在落库前后是否成立：

- `RuntimeEvent` round-trip（`contracts/validate.py`）
- `ToolResultModel` 审计字段（`success`、`sanitized_result`）
- `eval/phase4-eval-cases.json` 与 `eval/golden/*.json` 中声明的事件 kind 可解析

特点：**无 LLM、无真实 Agent 对话**，速度快，适合作为 core 门禁前几项。

### 2.2 Runtime 层

针对 EventStore、Timeline、ExecutionEngine、Session、Memory 等模块的**确定性**行为：

- 事件 schema、`tool_start`/`tool_end` 成对
- Timeline 投影（含 `retrieval`、`approval`、checkpoint 元数据）
- Run 状态机、审批、取消、checkpoint 与 thread 生命周期

特点：多用内存/SQLite 夹具，断言精确，与产品 Runtime 语义紧耦合。

### 2.3 Golden 场景层

`eval/golden/runtime-golden-scenarios.json` 描述「一次 Run 应出现哪些事件、终态是什么」：

```json
{
  "id": "runtime_dangerous_post_requires_approval",
  "input": { "thread_id": "...", "messages": [...], "confirm_dangerous": false },
  "must_have_events": ["approval_required", "run_checkpoint_meta"],
  "must_not_have_events": ["tool_start", "tool_end"],
  "expected_run_status": "waiting_approval",
  "notes": "..."
}
```

当前以**数据集结构校验 + 契约事件 kind 校验**为主；完整「跑 Agent + 断言事件流」的 E2E 仍依赖 `verify_mvp_runtime_acceptance.py` 或后续 Promptfoo 编排。

### 2.4 RAG 层

`scripts/verify_phase4_ragas.py` 覆盖检索与回答质量（proxy 或 RAGAS 实评）：

- `profile=rag`：仅 RAG 套件
- `profile=full`：core + RAG（可选 `--enable-ragas` 尝试实评分）

---

## 3. 评测框架定位（横向对比）

| 方案 | 角色 | 说明 |
|---|---|---|
| 自研 `verify_*` | **主门禁** | 与 EventStore / Timeline / Contract 紧耦合，断言可重复 |
| Promptfoo | 场景编排层（规划） | 适合把 golden case 扩成声明式批量评测 |
| RAGAS | RAG 专项 | 检索命中率、faithfulness 等；full  profile 可选启用 |
| DeepEval | 质量增强（规划） | LLM-as-judge，成本高、波动大，宜夜跑 |
| LangSmith Evals | 可选 | 样本与 trace 管理强，但偏 SaaS，非本地首要路径 |

推荐组合：**`verify_eval_suite`（deterministic）+ RAGAS（RAG）+ 后置 Promptfoo / DeepEval**。

---

## 4. Profile 与聚合协议

### 4.1 Profile

| Profile | 包含套件 | 典型用途 |
|---|---|---|
| `core` | contract + runtime + golden 结构 | PR 必跑、本地提交前 |
| `rag` | phase4_ragas + tool_trajectory + api_ingest + citation + diagnosis 等 **6 套件** | RAG + Tool-grounded 专项 |
| `e2e` | `demo_golden_e2e`（Demo 1–6 proxy） | Demo 验收 |
| `full` | core + rag + e2e | 夜跑、发版前 |

入口：`scripts/verify_eval_suite.py --profile {core|rag|full}`

### 4.2 子套件 summary（约定）

每个 `verify_*.py` 宜写出独立 `artifacts/**/**-summary.json`，并尽量包含 `checks` 字典，例如：

```json
{
  "suite_name": "runtime_timeline",
  "status": "PASS",
  "duration_ms": 1200,
  "summary_json": "artifacts/runtime/timeline-summary.json",
  "checks": {
    "schema_ok": true,
    "retrieval_present": true
  },
  "errors": []
}
```

### 4.3 聚合 summary（`artifacts/eval/eval-suite-summary.json`）

聚合层除各子套件结果外，还汇总：

| 字段 | 含义 |
|---|---|
| `overall_pass` | 是否全部非 FAIL 套件通过 |
| `failed_suites` | 失败套件名列表 |
| `failed_scenarios` | 与 golden 相关的失败项 |
| `runtime_contract_breaks` | runtime_* / session 类失败 |
| `contract_schema_ok` | contract 三件套是否全部通过 |
| `contract_metrics` | 各 contract 套件 checks 明细 |
| `rag_metrics` | `phase4_ragas` 的 proxy/RAGAS 指标 |

控制台以 `eval_suite=PASS|FAIL` 作为总判定，便于 CI 解析。

### 4.4 CI 策略

工作流：`.github/workflows/eval-ci.yml`

| 触发 | Profile | 说明 |
|---|---|---|
| PR / push main | `core` | 稳定、无 LLM |
| schedule / manual full | `full`（可选 `--enable-ragas`） | 增强洞察，允许 SKIP（如无 docs / 无 RAGAS） |

Job Summary 展示 `overall_pass`、`contract_schema_ok`、`failed_suites` 等；工作流与本地复现见 [ci-design.md](./ci-design.md)。

---

## 5. 数据集与 Case 设计

### 5.1 Phase4 工具/RAG case（`eval/phase4-eval-cases.json`）

面向「该用什么工具、是否应拦截」的**期望声明**，字段包括：

- `id`、`question`、`category`
- `expected_tools` / `forbidden_tools`
- `required_sources`（RAG）
- `expect_blocked`

用于文档化 Demo 验收意图；契约脚本校验字段完整性与样本事件可解析。

### 5.2 Golden Runtime case（`eval/golden/runtime-golden-scenarios.json`）

面向 **Run 级事件契约**，建议覆盖：

- 正常 `search_docs` 路径（含 `retrieval_completed`）
- 危险 POST → `approval_required` / approve / reject
- cancel 生命周期
- Timeline checkpoint 元数据
- memory 注入预算
- thread ended → archived

扩展新场景时：只改 JSON + 必要时补 `verify_*` 断言，避免在多个脚本里复制同一语义。

---

## 6. 本地使用（速查）

```powershell
# PR 级门禁等价
python scripts/verify_eval_suite.py --profile core

# RAG 专项
python scripts/verify_eval_suite.py --profile rag

# 发版前全量（RAGAS 可选）
python scripts/verify_eval_suite.py --profile full --enable-ragas
```

单套件调试时可直接调用对应 `scripts/verify_*.py`，聚合入口负责统一口径与产物路径。

---

## 7. 未来优化方向

### 7.1 场景与 E2E

- L5 **工具轨迹 proxy** ✅：`verify_phase4_tool_trajectory.py`（28 case）
- Demo 1–6 **golden proxy** ✅：`verify_demo_golden_e2e.py`，`eval/golden/demo-golden-scenarios.json`，`--profile e2e`
- **真实 LLM** E2E：`verify_demo_golden_e2e.py --mode live`（有 API key 时；否则 SKIP）
- Promptfoo 编排层（可选）：与 golden JSON 统一维护

### 7.2 质量与 RAG

- `phase4_ragas` 在 CI 夜跑稳定启用 RAGAS 实评（非仅 proxy），并记录历史趋势
- 引入 **DeepEval** 或轻量 judge，输出 `artifacts/eval/judge-summary.json`，初期只报警
- required_sources 命中率门禁阈值随 9 份文档语料稳定后收紧（当前 proxy 已覆盖 20 docs case）

### 7.3 工程化

- 子套件 summary 全部收敛到统一 `checks` schema，减少聚合层对 stdout key 的启发式解析
- Eval 产物上传与 [ci-design.md](./ci-design.md) / README 徽章联动（按 suite 展示趋势，而非单点 PASS）
- 超长套件（如 `session_mvp`）拆分或并行，缩短 core profile 总耗时
- `verify_mvp_runtime_acceptance` 纳入 eval_suite 的 `e2e` profile（需稳定 mock LLM 或固定 API）

### 7.4 与 Observability 打通

- 失败 case 自动附带 `thread_id` / `run_id` 与 EventStore 导出片段，便于和 Langfuse trace 对照
- 聚合 summary 增加 `flaky_suites` 与重试策略（针对 asyncio 资源未释放导致的超时）

### 7.5 八层栈改造分配（横切）

Eval 不单独占一层，但为各层改造提供 **回归门禁**。路线图见 [agent-learning-guide.md](./agent-learning-guide.md) §7。

| 波次 | 覆盖层 | 任务 | 状态 | 验收 |
|------|--------|------|------|------|
| **1** | L1–L4 | ingest/validate 新脚本纳入 `--profile rag` | ✅ | `verify_eval_suite.py --profile rag` |
| **2** | L5–L6 | `--mode live` Demo E2E；`verify_mvp_runtime_acceptance` 进 e2e | `--profile e2e` |
| **2** | L5 | plan/route golden 扩面 | `eval/golden/` |
| **3** | L7–L8 | 失败 case 自动附 `run_id` + timeline 片段；RAGAS 夜跑趋势 JSON | `--profile full` |
| **3** | L8 | `flaky_suites` + 套件并行化 | CI job 耗时 |
| **4** | 全栈 | Promptfoo 与 golden JSON 统一（可选） | 选型后单轨 |

---

## 8. 遗留问题

| 问题 | 影响 | 说明 |
|---|---|---|
| Golden 未接真实 LLM | LLM 选错工具未发现 | L5 + Demo golden proxy ✅；`--mode live` 待接 |
| Phase4 case | — | **28 条**（20 docs + 8 api/safety）✅ |
| RAGAS 默认 proxy / SKIP | full 夜跑对语义质量覆盖有限 | 无完整文档时 `allow-missing-docs` 会弱化断言 |
| `session_mvp` 等套件耗时长 | core profile 可达数分钟 | 存在「逻辑已 PASS 但进程超时」的边界情况 |
| 无 LLM-as-judge 硬指标 | 回答质量、引用忠实度靠人工 | L4-lite ✅；RAGAS/DeepEval 非 PR 门禁 |
| Promptfoo 未落地 | 声明式场景编排仍停留在选型 | 与 golden JSON 重复维护风险待统一 |
| E2E 与 deterministic 分裂 | `verify_mvp_runtime_acceptance` 未纳入 eval_suite | 需 API Key，与 PR 无密钥环境策略需对齐 |
| 失败诊断仍偏日志 | 缺少「一键导出失败 run 的 timeline.json」 | 排障依赖人工查 SQLite / UI |

---

## 9. 非目标

- 不在本文档定义具体脚本实现细节（见各 `scripts/verify_*.py` 与 [data-flow-design.md](./data-flow-design.md)）
- 不把 LLM judge 作为 PR 硬门禁（除非后续明确调整策略）
- 不替代 Langfuse / 生产监控；Eval 面向回归与发布门禁
