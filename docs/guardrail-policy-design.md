# LearnAgent Guardrail 与 Policy 设计

> 说明工具调用前的风险判定、审批（HITL）、HTTP 边界与脱敏策略；不重复 Tool 结果契约与 Run FSM。  
> 关联文档：[agent-learning-guide.md](./agent-learning-guide.md)、[runtime-design.md](./runtime-design.md)、[data-flow-design.md](./data-flow-design.md)、[tech-selection-design.md](./tech-selection-design.md) §5

---

## 1. 设计动机

Agent 能调 API 即具备「越权执行」能力。仅依赖 LLM「自觉」或单一拦截点会在换模型、改 prompt、加工具后失效。

| 风险 | 若无 Guardrail |
|------|----------------|
| 任意 URL / 路径 | SSRF、访问内网或未授权接口 |
| 危险写操作（创建水印任务） | 未确认即改生产数据 |
| 敏感参数进日志 / timeline | Cookie、密钥泄露 |
| 审批与 Run 状态脱节 | UI 显示 running 但图已 interrupt，无法复盘 |

本设计覆盖 **M12 Guardrail / Policy**（`policy/`、`safety_gate`、HTTP 白名单、环境开关），并与 **M11 Tool**（注册与 handler）分层：

- **Tool 层**：提供能力、执行 HTTP、产出结果 → 契约见 [data-flow-design.md](./data-flow-design.md)
- **Policy + Guardrail 层**：决定**是否允许进入 ToolNode**、是否**中断等人批**
- **Runtime 层**：`waiting_approval`、approve/reject API → 见 [runtime-design.md](./runtime-design.md)

---

## 2. 防护分层（纵深）

```text
LLM 产出 tool_calls (AIMessage)
    |
    v
[编排] safety_gate 节点 (agent/nodes.py)
    |-- PolicyRegistry.evaluate_tool_calls()
    |     |-- 部署开关 COPILOT_ALLOW_JOB_POST
    |     |-- 请求级 confirm_dangerous
    |     |-- ToolSpec.requires_approval（按 path）
    |-- 不允许 -> AIMessage 说明（不 interrupt，不执行工具）
    |-- 需审批 -> interrupt(payload) -> GraphInterrupted
    v
[产品] ExecutionEngine -> waiting_approval + approval_required 事件
    |-- POST /v1/runs/{id}/approve | reject
    |-- Command(resume=True|False) 续跑
    v
[编排] ToolNode 执行（仅当图未 interrupt 或 resume 批准）
    |
    v
[执行] http_get / http_post (tools/http_tools.py)
    |-- validate_get_path / validate_post_path（白名单）
    |-- watermark job POST 再次校验 allow_job_post + confirm_dangerous
    v
[契约] ToolResultModel + sanitize -> tool_end 审计
```

**原则**：图级闸门优先于工具执行；HTTP 层白名单是**最后一道**硬边界（即使用户强行构造参数，也无法走出允许路径集合）。

---

## 3. 核心组件与边界

| 组件 | 路径 | 职责 | 不负责 |
|------|------|------|--------|
| `PolicyRegistry` | `policy/registry.py` | 对 `tool_calls` 批量判定 `allowed` / `requires_approval` / 文案 | HTTP 请求发送、事件落库 |
| `safety_gate` | `agent/nodes.py` | 调用 Policy；`interrupt()` 或返回拦截 AIMessage | Run 状态机更新 |
| `ToolSpec` | `tools/registry.py` | 工具元数据：`category`、`risk_level`、`requires_approval` | 运行时策略版本、租户隔离 |
| HTTP 白名单 | `tools/whitelist.py` | GET/POST 路径正则与精确匹配 | 业务审批语义 |
| `WatermarkHttpTools` | `tools/http_tools.py` | 仅请求 `WATERMARK_API_BASE_URL`；路径校验；危险 POST 二次校验 | LangGraph 路由 |
| `sanitize_tool_payload` | `tools/sanitize.py` | 审批与审计中的参数脱敏 | 输出内容语义审查 |
| `ExecutionEngine` | `runtime/execution_engine.py` | 捕获 `GraphInterrupted`；审批续跑 | 工具风险规则定义 |

---

## 4. Policy 判定逻辑

### 4.1 `PolicyDecision`

```text
PolicyDecision
├── allowed: bool          # False 时 safety_gate 直接返回说明 AIMessage，不调用工具
├── requires_approval: bool
├── message: str           # 用户可见说明
└── reason: str            # 如 job_post_disabled、dangerous_tool_requires_approval
```

### 4.2 危险 POST（`/api/v1/jobs/watermark`）

当前 `PolicyRegistry` 对 `http_post` 且 path 为 job 路径时：

| 条件 | 结果 |
|------|------|
| `COPILOT_ALLOW_JOB_POST=false`（`settings.copilot_allow_job_post`） | `allowed=false`，提示开启部署开关 |
| `confirm_dangerous=false`（请求体 / Run 创建参数） | `allowed=true`，`requires_approval=true`（走 interrupt） |
| 开关开启且 `confirm_dangerous=true` | `allowed=true`，`requires_approval` 由 `ToolSpec.requires_approval_for(args)` 决定（job path 为 true） |

`ToolRegistry` 注册时 `http_post` 的 `requires_approval` 为可调用规则 `_requires_dangerous_post_approval`（path 为 job 路径时为 true）。

### 4.3 其他工具

- `search_docs`：`risk_level=low`，无需审批。
- `http_get`：`risk_level=medium`，无需图级审批；仍受白名单约束。

扩展新工具时：在 `ToolRegistry.register_async` 声明 `risk_level` 与 `requires_approval`，并在 `PolicyRegistry.evaluate_tool_calls` 中补充路径/名称规则（若需特殊逻辑）。

---

## 5. 审批（HITL）与 Run 协作

### 5.1 编排层：interrupt

`safety_gate` 在 `requires_approval` 时调用 `interrupt({ required, reason, message, tool_calls })`。  
`GraphEventMapper` 识别 interrupt → 发出 `approval_required` → 抛 `GraphInterrupted`。

`tool_calls` 经 `sanitize_tool_payload` 脱敏后进入 payload，供 UI 展示。

### 5.2 产品层：Run 状态

| 阶段 | Run 状态 | 事件 |
|------|----------|------|
| interrupt 后 | `waiting_approval` | `approval_required` |
| 用户 approve | `running` → 续跑 | `approval_resolved{approved:true}`、`run_started{resumed:true}` |
| 用户 reject | 完成 | `approval_resolved{approved:false}`，`Command(resume=False)` |
| 用户 cancel | `cancelled` | `cancel_requested`、`cancelled` |

细节与重启 rehydrate 见 [runtime-design.md](./runtime-design.md) §5.3–5.4。

### 5.3 语义约定（Run 内自主 vs 边界 HITL）

- **Run 内**：`search_docs`、普通 `http_get` 在策略允许时由图自动执行，无需逐次人工点击。
- **边界 HITL**：改变生产任务状态的危险 `http_post` 必须经 **部署开关 +（可选）请求 confirm + interrupt 审批**。
- `CreateRunRequest.confirm_dangerous=true` 可在一开始标记 Run 已获用户确认，减少重复审批（Engine 在 create 时设置 `managed.approved`）。

---

## 6. HTTP 白名单

### 6.1 设计目标

- 仅允许访问水印 Java API 的**显式路径集合**，禁止 scheme、路径穿越、`//`。
- GET：`/actuator/health` 与若干 `/api/v1/...` 正则（jobs UUID、files、admin 等）。
- POST：仅 `/api/v1/auth/login` 与 `/api/v1/jobs/watermark`。

### 6.2 实现

- 校验函数：`validate_get_path`、`validate_post_path`（`tools/whitelist.py`）。
- 调用点：`WatermarkHttpTools` 在发起 httpx 前校验；失败返回 `{ok: false, error: ...}`（Adapter 映射为 `ToolResultModel.success=false`）。

LLM **无法**通过 `http_get("https://evil.example/...")` 绕过：path 带 scheme 会被判为 invalid。

---

## 7. 配置与环境变量

| 配置 | 默认 | 作用 |
|------|------|------|
| `COPILOT_ALLOW_JOB_POST` / `copilot_allow_job_post` | `false` | 部署级是否允许 enqueue 水印任务 |
| `confirm_dangerous`（API 字段） | `false` | 单次 Run/聊天是否声明用户已确认危险操作 |
| `WATERMARK_API_BASE_URL` | — | HTTP 客户端 base，非 Guardrail 但限定出站目标 |

传入图：`RunnableConfig.configurable` 中的 `confirm_dangerous`、`allow_job_post`（与 `runner` / `tool_handlers` 一致）。

---

## 8. 审计与脱敏（与 Guardrail 的交界）

Guardrail 不负责完整 `ToolResult` 形状，但负责**进入审批与 EventStore 的工具参数**不脱敏：

| 数据 | 处理 |
|------|------|
| `cookie_header` | `redact_cookie_header`；日志与 observability 脱敏 |
| `set-cookie` | 工具结果中 `_raw_set_cookie_for_store_only` 仅服务端存会话，不进 LLM 可见字段 |
| `approval_required.tool_calls` | `sanitize_tool_payload` 后写入事件 |

输出侧 **无** Guardrail：模型回复未经 PII/幻觉 API 的自动拦截（遗留项）。

---

## 9. 与相关文档的衔接

| 主题 | 本文档 | 详见 |
|------|--------|------|
| `approval_*` / `cancel_*` 事件与 FSM | §5 | [runtime-design.md](./runtime-design.md) |
| `tool_start` / `tool_end` / `ToolResultModel` | §8 边界 | [data-flow-design.md](./data-flow-design.md) |
| Tool 注册、timeout 元数据 | §3 | `tools/registry.py`；**执行层 timeout 未强制** |
| 选型与四列对照 | — | [tech-selection-design.md](./tech-selection-design.md) §5（Tool 治理、Approval 行） |
| Phase3 safety_gate 回归 | — | [ci-design.md](./ci-design.md)、`scripts/verify_phase3_safety_gate.py` |

---

## 10. 未来优化方向

### 10.1 策略可配置化

- 将「工具 × 路径 × 风险 × 是否审批」抽为 YAML/JSON **策略表**（版本号、生效时间），`PolicyRegistry` 只读配置。
- 支持按 `thread_id` / 未来 `tenant_id` 覆盖 tool 集（与 guide 中多租户后置一致）。

### 10.2 输入 / 输出 Guard

- **输入**：用户消息 PII/注入检测（Presidio 或轻量规则）在进入图之前。
- **输出**：回复中 secret、内部 URL、raw cookie 的模式检测；可写 `output_blocked` 事件。

### 10.3 执行层与工具扩展

- `ToolSpec.timeout_seconds` 在 ExecutionEngine 或 wrapper 层 `asyncio.wait_for` 强制。
- MCP 工具纳入同一 `PolicyRegistry` 评估接口（name + resource URI 规则）。

### 10.4 可观测与评测

- `approval_required` / `approval_resolved` 纳入 golden scenario 断言（部分已在 `eval/golden`）。
- Policy 决策写 `policy_decision` 调试事件（可选，默认关闭以免噪声）。

### 10.5 八层栈改造分配

路线图见 [agent-learning-guide.md](./agent-learning-guide.md) §7 第 2–3 波（L6–L7）。

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **2** | L6 Tool | 策略表 YAML/JSON + `policy_version` 写入 EventStore | `verify_phase3_safety_gate.py` 扩展 |
| **2** | L6 | `ToolSpec.timeout_seconds` → `asyncio.wait_for` 强制 | runtime + tool 单测 |
| **3** | L7 Output | 输出 Guard：secret/cookie/内部 URL 模式检测 → `output_blocked` 事件 | 新 verify |
| **3** | L6 | 输入 Guard PoC（PII/注入规则） | PoC script |
| **4** | L6 | MCP 工具纳入 `PolicyRegistry`（name + resource URI） | PoC + tech-selection |
| **4** | L4 | 多租户 `tenant_id` 裁剪 tool 集 | 与 memory user scope 联动 |

---

## 11. 遗留问题

| 问题 | 影响 | 说明 |
|------|------|------|
| 无统一策略表文件 | 改规则需改代码 | 逻辑分散在 `PolicyRegistry`、`whitelist`、`http_tools` |
| `timeout_seconds` 未 enforce |  hung 工具拖垮 Run | 仅元数据；Run 级 `run_timeout_seconds` 可兜底整 Run |
| 无输出 Guard | 模型仍可能泄露响应中的敏感信息 | 仅工具参数与 cookie 脱敏 |
| 无输入 Guard | 恶意 prompt 仍可诱导多次安全工具调用 | 依赖 LLM + 白名单 |
| Policy 与 `http_tools` 双重校验 job POST | 维护两处 | 故意纵深；变更 path 需同步两处 |
| 非 HTTP 工具无 Sandbox | 无文件/终端/exec | 见 tech-selection §5 Sandbox 行 |
| 审批策略不可版本审计 | 合规场景难回放「当时规则」 | 无 `policy_version` 写入 EventStore |

---

## 12. 非目标

- 不定义 `ToolResultModel` 字段与 Adapter（见 [data-flow-design.md](./data-flow-design.md)）
- 不定义 LangGraph 图拓扑（见 `agent/graph.py`）
- 不实现完整 RBAC / 多租户 IAM（M14 Session 仅内存 Cookie）
- 不在本文档描述 RAG 内容安全（检索 poison 属 RAG 专题）
