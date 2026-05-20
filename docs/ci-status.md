# copilot-agent CI 状态与指标

本文档用于说明 Phase 3 编排回归的 CI 检查项、PASS/FAIL 口径、以及失败时排查步骤。

> 工作流：`.github/workflows/copilot-agent-phase3-ci.yml`（`copilot-agent Phase3 orchestration CI`）

---

## 1. 当前接入的回归项

### Phase 3 checkpoint 回归

- 脚本：`scripts/verify_phase3_checkpoint.py`
- 目的：验证 LangGraph 在同一 `thread_id` 下可恢复状态，并且 SQLite checkpoint 成功落盘。

CI 会在 Job Summary 输出如下关键指标：

- `PASS/FAIL`（来自 `phase3_step4=PASS|FAIL`）
- `state_resumed`（第二轮消息数是否高于第一轮）
- `tool_path_executed`（是否经过 tool 调用路径）
- `checkpoint_file_exists`（checkpoint 文件是否存在）
- `messages_after_turn1`
- `messages_after_turn2`

并产出结构化文件（统一在 `artifacts/phase3/` 目录）：

- `artifacts/phase3/phase3-checkpoint-summary.json`（便于 dashboard/历史趋势分析）

### Phase 3 safety_gate 回归

- 脚本：`scripts/verify_phase3_safety_gate.py`
- 目的：验证危险 `http_post`（`/api/v1/jobs/watermark`）在 `confirm_dangerous=false` 时会被图级 `safety_gate` 拦截，且不会执行工具节点。

CI 会在 Job Summary 输出如下关键指标：

- `PASS/FAIL`（来自 `phase3_safety_gate=PASS|FAIL`）
- `blocked_by_gate`（是否命中闸门拦截文案）
- `tool_not_called`（危险工具是否未执行）
- `tool_message_seen`（输出消息中是否出现 ToolMessage，期望 `false`）
- `http_post_calls`（危险工具调用次数，期望 `0`）

并产出结构化文件（统一在 `artifacts/phase3/` 目录）：

- `artifacts/phase3/phase3-safety-gate-summary.json`

---

## 2. 指标口径（判定规则）

`scripts/verify_phase3_checkpoint.py` 的通过条件：

- `ok_state_grew`: `messages_after_turn2 > messages_after_turn1 >= 3`
- `ok_checkpoint_file`: checkpoint 文件存在
- `ok_tool_path`: 第一轮中出现 `ToolMessage`

三者同时满足则输出 `phase3_step4=PASS` 并返回 0，否则返回非 0。

`scripts/verify_phase3_safety_gate.py` 的通过条件：

- `blocked_by_gate`: 拦截消息包含 `gated` 关键字
- `tool_not_called`: 危险 `http_post` 未被调用（调用次数为 `0`）
- `tool_message_seen`: 输出中未出现 `ToolMessage`

三者同时满足则输出 `phase3_safety_gate=PASS` 并返回 0，否则返回非 0。

---

## 3. 本地复现命令

```powershell
cd E:\code\watermarking\copilot-agent
conda run -n myenv39 python scripts/verify_phase3_checkpoint.py
```

可指定 checkpoint 路径与 thread id：

```powershell
conda run -n myenv39 python scripts/verify_phase3_checkpoint.py `
  --checkpoint-path "storage/langgraph-checkpoints.sqlite" `
  --thread-id "manual-check-001"
```

也可显式输出结构化摘要：

```powershell
conda run -n myenv39 python scripts/verify_phase3_checkpoint.py `
  --summary-json "artifacts/phase3/phase3-checkpoint-summary.json"
```

单独运行 safety gate 回归：

```powershell
cd E:\code\watermarking\copilot-agent
conda run -n myenv39 python scripts/verify_phase3_safety_gate.py
```

也可显式输出结构化摘要：

```powershell
conda run -n myenv39 python scripts/verify_phase3_safety_gate.py `
  --summary-json "artifacts/phase3/phase3-safety-gate-summary.json"
```

---

## 4. 失败排查建议

1. **依赖问题**
   - 检查是否安装：`langgraph`、`langgraph-checkpoint-sqlite`、`langchain-core`
2. **文件权限/路径问题**
   - 确认 `storage/` 可写
   - 确认 `--checkpoint-path` 路径合法
3. **图编排回归问题**
   - 检查 `copilot_agent/agent/graph.py`
   - 检查 `copilot_agent/agent/state.py`
4. **安全闸门行为异常**
   - 检查 `copilot_agent/agent/runner.py` 中 `_safety_gate_node` 对 `http_post` 与 `confirm_dangerous` 的判断
   - 检查脚本 `scripts/verify_phase3_safety_gate.py` 是否仍在验证“拦截且不执行工具”
5. **脚本逻辑问题**
   - 检查 `scripts/verify_phase3_checkpoint.py` 中 `ok_*` 条件与消息计数逻辑

---

## 5. 后续扩展建议

- 把 CI summary 输出为统一 JSON，供后续 dashboard/历史趋势分析使用。

---

## 6. LearnAgent Eval CI（新增）

> 工作流：`.github/workflows/eval-ci.yml`（`LearnAgent Eval CI`）

当前 Eval CI 分为两层：

- PR/Push：`python scripts/verify_eval_suite.py --profile core`
- Nightly/手动：`python scripts/verify_eval_suite.py --profile full [--enable-ragas]`

统一聚合产物：

- `artifacts/eval/eval-suite-summary.json`

关键汇总字段：

- `overall_pass`
- `suites_total`
- `suites_failed`
- `skipped_suites`
- `failed_scenarios`
- `runtime_contract_breaks`
- `rag_metrics`

### RAG 前置条件与 SKIP 语义

`phase4_ragas` 依赖以下文档源文件：

- `DEPLOY-SERVER.md`
- `REQUIREMENTS-CHECKLIST-AND-TEST-CASES.md`
- `watermark-java-backend-tech-selection.md`

解析路径来源：

1. 环境变量 `WATERMARK_DOCS_PATH`
2. 仓库内 `docs/source`
3. 上级 `backend-java/docs`

如果文档前置条件不满足且启用 `--allow-missing-docs`，脚本 `verify_phase4_ragas.py` 会输出 `phase4_ragas=SKIP` 并返回 0，避免因环境不完整导致 PR 门禁误报。Nightly 环境建议配置完整文档路径以获得真实 `rag_metrics`。
