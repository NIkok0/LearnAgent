# LearnAgent Guardrail 与 Policy 设计

> 说明工具调用前的风险判定、审批（HITL）、HTTP 边界与脱敏策略；不重复 Tool 结果契约与 Run FSM。  
> 关联文档：[agent-learning-guide.md](./agent-learning-guide.md)、[runtime-design.md](./runtime-design.md)、[data-flow-design.md](./data-flow-design.md)、[tech-selection-design.md](./tech-selection-design.md) §5

**K/C/S 位置**：Kernel **M12** PolicyGate（统一裁决）；Scenario 提供 `policy` / HTTP 白名单 / credential 声明；Capability 声明 `ToolSpec.required_scopes`。详见 [guide §2.4.3·§2.7](./agent-learning-guide.md)。

**本文负责**：PolicyGate、HITL、HTTP path policy、credential scope 裁决、安全审计与脱敏。  
**本文不负责**：Tool handler 实现、RAG 检索算法、Run FSM 细节、契约字段总表。  
**权威来源**：模块边界与全局缺口见 [agent-learning-guide.md](./agent-learning-guide.md)；Tool 注册见 [tool-design.md](./tool-design.md)。

---

## 0. 实现状态

| 项 | 状态 | 验收脚本 |
|---|---|---|
| Scenario `policy.tool_allowlist` | ✅ | `verify_scenario_loader.py` |
| `HttpPathPolicy` + Scenario HTTP 白名单 | ✅ | `verify_scenario_loader.py`、`verify_rag_domain.py --case api_path_extraction` |
| `required_scopes` + PolicyGate 裁决 | ✅ | `verify_policy_credentials.py` |
| `credential_binding_audit` EventStore | ✅ | `verify_policy_credentials.py` |
| `safety_gate` 节点（job_post / dangerous path） | ✅ | `verify_phase3_safety_gate.py` |
| Phase4 tool 轨迹 + scope（28 case） | ✅ | `verify_phase4_tool_trajectory.py` |
| Tool timeout 强制 | ✅ | `verify_tool_execution_reliability.py` |
| Policy decision audit v1 | ✅ | `verify_policy_decision_audit_v1.py` |
| 策略表 YAML 版本化 + EventStore `policy_version` | ❌ | — |
| 输出 Guard（secret/PII 模式） | ⚠️ 部分 | Private RAG 输出检测：`verify_private_rag_output_guard_v1.py`；通用输出 Guard 待建 |

全局缺口见 [agent-learning-guide §2.8](./agent-learning-guide.md)。套件见 [ci-design.md](./ci-design.md)。

---

## 1. 设计动机

Agent 能调 API 即具备「越权执行」能力。仅依赖 LLM「自觉」或单一拦截点会在换模型、改 prompt、加工具后失效。

| 风险 | 若无 Guardrail |
|------|----------------|
| 任意 URL / 路径 | SSRF、访问内网或未授权接口 |
| 危险写操作（创建水印任务） | 未确认即改生产数据 |
| 敏感参数进日志 / timeline | Cookie、密钥泄露 |
| 审批与 Run 状态脱节 | UI 显示 running 但图已 interrupt，无法复盘 |

本设计覆盖 **M12 Guardrail / Policy**（`policy/`、`safety_gate`、Scenario HTTP 白名单、`required_scopes`、环境开关），并与 **M11 Tool**（注册与 handler）、**M14 Credential**（binding 存储，不含裁决）分层：

- **Capability / Tool 层**：声明 `ToolSpec`（含 `required_scopes`）、执行 HTTP、产出结果 → 契约见 [data-flow-design.md](./data-flow-design.md)
- **Scenario 层**：`policy.tool_allowlist`、MCP allowlist、`resources.http_*_paths`、`resources.credential_*`（只声明，不存 secret）
- **Policy + Guardrail 层（Kernel）**：决定**是否允许进入 ToolNode**、是否**中断等人批**、**binding scope 是否满足**
- **Runtime 层**：`waiting_approval`、approve/reject API → 见 [runtime-design.md](./runtime-design.md)

---

## 2. 防护分层（纵深）

```text
LLM 产出 tool_calls (AIMessage)
    |
    v
[编排] safety_gate 节点 (agent/nodes.py)
    |-- PolicyRegistry.evaluate_tool_calls()
    |     |-- Scenario policy：tool_allowlist / denylist / mcp_* allowlist
    |     |-- ToolSpec.required_scopes vs CredentialManager（统一 scope 裁决）
    |     |-- 部署开关 COPILOT_ALLOW_JOB_POST
    |     |-- 请求级 confirm_dangerous
    |     |-- ToolSpec.requires_approval（按 path / Scenario dangerous_paths）
    |-- credential_binding_audit -> EventStore（scope_allowed / scope_denied，无 secret）
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
[执行] http_get / http_post (ScenarioHttpClient, tools/http_tools.py)
    |-- validate_get_path / validate_post_path -> HttpPathPolicy（Scenario resources 注入）
    |-- dangerous POST 再次校验 allow_job_post + confirm_dangerous
    v
[Handler] login 存 cookie -> CredentialManager.set_cookie -> credential_binding_audit(credential_set)
    v
[契约] ToolResultModel + sanitize -> tool_end 审计
```

**原则**：图级闸门优先于工具执行；HTTP 层白名单是**最后一道**硬边界（即使用户强行构造参数，也无法走出允许路径集合）。

---

## 3. 核心组件与边界

| 组件 | 路径 | 职责 | 不负责 |
|------|------|------|--------|
| `PolicyRegistry` | `policy/registry.py` | 对 `tool_calls` 批量判定：Scenario allowlist、**required_scopes**、dangerous POST、MCP allowlist；产出 `PolicyDecision.credential_audits` | HTTP 请求发送、secret 存储 |
| `safety_gate` | `agent/nodes.py` | 调用 Policy；写 `credential_binding_audit`；`interrupt()` 或返回拦截 AIMessage | Run 状态机更新 |
| `ToolSpec` | `tools/registry.py` | 工具元数据：`category`、`risk_level`、`required_scopes`、`requires_approval` | 运行时策略版本、租户隔离、最终授权 |
| `HttpPathPolicy` | `scenario/http_paths.py` | 从 Scenario `resources.http_*_paths` 构建 GET/POST 白名单；`bootstrap` 时 `bind_http_path_policy()` | 业务审批语义 |
| HTTP 校验入口 | `tools/whitelist.py` | 薄封装：`get_http_path_policy().validate_*` | 路径规则定义（在 Scenario） |
| `ScenarioHttpClient` | `tools/http_tools.py` | 仅请求 Scenario 解析的 `api_base_url`；路径校验；危险 POST 二次校验 | LangGraph 路由、PolicyGate |
| `CredentialManager` | `credentials/manager.py` | binding 元数据、进程内 secret 存取、`authorize_scopes()` | **PolicyGate 裁决**（M12 负责） |
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
├── reason: str            # 如 job_post_disabled、credential_scope_denied、scenario_tool_not_allowed
└── credential_audits: list[dict]  # 供 safety_gate 写入 credential_binding_audit（无 secret）
```

### 4.2 Scenario policy（收紧，不可放宽）

`PolicyRegistry` 在评估每个 tool call 时读取 `LoadedScenario.policy`：

| 规则 | 行为 |
|------|------|
| `tool_denylist` | 命中则 `allowed=false`，`reason=scenario_tool_denied` |
| `tool_allowlist` 非空且 name 不在其中 | `allowed=false`，`reason=scenario_tool_not_allowed` |
| MCP：`mcp_server_allowlist` / `mcp_tool_allowlist` | 未列入则 deny |

Scenario **不能**放宽 Kernel 默认安全边界；换业务时只改 `config/<name>.yaml`。

### 4.3 `required_scopes`（M11 声明 → M12 裁决）

Capability 在 `ToolRegistry.register_async` 声明 `required_scopes`（示例）：

| Tool | `required_scopes` |
|------|-------------------|
| `search_docs` | （空） |
| `http_get` | `http:read` |
| `http_post` | `http:write` |
| MCP tools | 可选，来自 `McpToolDefinition.required_scopes` |

`PolicyRegistry.evaluate_required_scopes()` 对照 `CredentialManager.authorize_scopes()`：

| 条件 | 结果 |
|------|------|
| 工具无 `required_scopes` | 跳过 scope 检查 |
| 无 `CredentialManager` 但工具需要 scope | `allowed=false`，`reason=credential_binding_missing` |
| binding 未授予所需 scope | `allowed=false`，`reason=credential_scope_denied` |
| scope 满足 | 继续后续规则；写入 audit `action=scope_allowed` |

Handler 层 `get_cookie(..., required_scopes=...)` 为**纵深防御**；**最终裁决权在 PolicyGate**（见 [agent-learning-guide.md](./agent-learning-guide.md) §2.7.3）。

### 4.4 危险 POST（Scenario `policy.dangerous_paths`）

当前 watermark Demo 的 job 路径为 `/api/v1/jobs/watermark`（声明在 Scenario YAML，非 Kernel 硬编码）。`PolicyRegistry` 对 `http_post` 且 path 命中 `dangerous_paths` 时：

| 条件 | 结果 |
|------|------|
| `COPILOT_ALLOW_JOB_POST=false`（`settings.copilot_allow_job_post`） | `allowed=false`，提示开启部署开关 |
| `confirm_dangerous=false`（请求体 / Run 创建参数） | `allowed=true`，`requires_approval=true`（走 interrupt） |
| 开关开启且 `confirm_dangerous=true` | `allowed=true`，`requires_approval` 由 `ToolSpec.requires_approval_for(args)` 决定（job path 为 true） |

`ToolRegistry` 注册时 `http_post` 的 `requires_approval` 为 `dangerous_post_approval_rule(scenario)`（path 命中 Scenario `dangerous_paths` 时为 true）。

### 4.5 其他工具

- `search_docs`：`risk_level=low`，无 `required_scopes`，无需审批。
- `http_get`：`risk_level=medium`，`required_scopes=(http:read,)`，无需图级审批；仍受 Scenario HTTP 白名单约束。

扩展新工具时：在 Capability 注册 `risk_level`、`required_scopes`、`requires_approval`；若需 Scenario 级 deny/allow，只改 Scenario policy，**优先**不扩 Kernel 特例逻辑。

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

## 6. HTTP 白名单（Scenario 驱动）

### 6.1 设计目标

- 路径集合由 **Scenario `resources`** 声明，Kernel 通过 `HttpPathPolicy` 执行，禁止 scheme、路径穿越、`//`。
- watermark Demo 示例：
  - GET：`http_get_actuator_paths` + `http_get_patterns`（jobs UUID、files、admin 等正则）
  - POST：`http_post_paths`（如 `/api/v1/auth/login`、危险 job path）
- **换业务 = 改 Scenario YAML**，不改 `whitelist.py` 内硬编码列表。

### 6.2 实现

- **策略来源**：`HttpPathPolicy.from_resources(scenario.resources)`（`scenario/http_paths.py`）。
- **激活**：`kernel/bootstrap.py` → `bind_http_path_policy(path_policy)`。
- **校验入口**：`validate_get_path` / `validate_post_path`（`tools/whitelist.py`）委托 active policy。
- **调用点**：`ScenarioHttpClient` 在发起 httpx 前校验；失败返回 `{ok: false, error: ...}`（Adapter 映射为 `ToolResultModel.success=false`）。

LLM **无法**通过 `http_get("https://evil.example/...")` 绕过：path 带 scheme 会被判为 invalid。

**验收**：`verify_scenario_loader.py`（allowlist 探针）、`verify_rag_domain.py --case api_path_extraction`（需 `apply_scenario_environment`）。

---

## 7. 配置与环境变量

| 配置 | 默认 | 作用 |
|------|------|------|
| `SCENARIO` / `settings.scenario` | `minimal` | 加载哪份 Scenario overlay；watermark 作为显式 Demo scenario |
| `COPILOT_CAPABILITIES` | `rag,http,mcp` | 部署启用哪些 Capability pack（与 Scenario allowlist 取交集） |
| `COPILOT_ALLOW_JOB_POST` / `copilot_allow_job_post` | `false` | 部署级是否允许 enqueue 危险 job POST |
| `confirm_dangerous`（API 字段） | `false` | 单次 Run/聊天是否声明用户已确认危险操作 |
| Scenario `resources.api_base_url_env` | 如 `WATERMARK_API_BASE_URL` | HTTP 客户端 base（`resolve_api_base_url`），限定出站目标 |
| Scenario `resources.credential_*` | 见 `config/watermark.yaml` | binding id、cookie 名、granted scopes（不存 secret） |

传入图：`RunnableConfig.configurable` 中的 `confirm_dangerous`、`allow_job_post`（与 `runner` / `tool_handlers` 一致）。

---

## 8. 审计与脱敏（与 Guardrail 的交界）

Guardrail 不负责完整 `ToolResult` 形状，但负责**进入审批与 EventStore 的工具参数**不脱敏：

| 数据 | 处理 |
|------|------|
| `cookie_header` | `redact_cookie_header`；日志与 observability 脱敏 |
| `set-cookie` | 工具结果中 `_raw_set_cookie_for_store_only` 仅服务端存会话，不进 LLM 可见字段 |
| `approval_required.tool_calls` | `sanitize_tool_payload` 后写入事件 |
| **Policy scope 裁决** | `credential_binding_audit`：`binding_id`、`granted_scopes`、`required_scopes`、`action`；**不含 secret** |
| **登录存 cookie** | `credential_set` audit（`tool_handlers.http_post`） |

EventStore payload 契约见 [data-flow-design.md](./data-flow-design.md) §2.5。

输出侧 **无** Guardrail：模型回复未经 PII/幻觉 API 的自动拦截（遗留项）。

---

## 9. 文档关系

- **上游**：[data-flow-design.md](./data-flow-design.md)（`ToolResultModel`、audit payload）、[runtime-design.md](./runtime-design.md)（Run FSM、`waiting_approval`）
- **下游**：`safety_gate` 节点、M11 tool handlers（不得自授权）
- **全量索引**：[agent-learning-guide §6](./agent-learning-guide.md)

---

## 10. 未来优化方向

### 10.1 策略可配置化

- 将「工具 × 路径 × 风险 × 是否审批」抽为 YAML/JSON **策略表**（版本号、生效时间），`PolicyRegistry` 只读配置。
- 支持按 `thread_id` / 未来 `tenant_id` 覆盖 tool 集（与 guide 中多租户后置一致）。

### 10.2 输入 / 输出 Guard

- **输入**：用户消息 PII/注入检测（Presidio 或轻量规则）在进入图之前。
- **输出**：回复中 secret、内部 URL、raw cookie 的模式检测；可写 `output_blocked` 事件。

### 10.3 执行层与工具扩展

- ~~`ToolSpec.timeout_seconds` 在 ToolRegistry wrapper 层 `asyncio.wait_for` 强制。~~ ✅ `verify_tool_execution_reliability.py`
- ~~MCP 工具纳入同一 `PolicyRegistry` 评估接口~~ ✅ Scenario MCP allowlist + `register_mcp_tools`；MCP server 脚本外置 `scenarios/<name>/mcp/`。

### 10.4 可观测与评测

- `approval_required` / `approval_resolved` 纳入 golden scenario 断言（部分已在 `eval/golden`）。
- ~~Policy scope 决策写 EventStore~~ ✅ `credential_binding_audit`（`verify_policy_credentials.py`）。
- ~~Policy decision 审计事件~~ ✅ `policy_decision_audit` v1（`verify_policy_decision_audit_v1.py`）。

### 10.5 八层栈改造分配（待办）

Wave2 已完成项见 **§0**。路线图索引：[agent-learning-guide §7](./agent-learning-guide.md)。

| 波次 | 层 | 任务 | 验收 |
|------|-----|------|------|
| **2** | L6 Tool | 策略表 YAML/JSON + `policy_version` 写入 EventStore | `verify_phase3_safety_gate.py` 扩展 |
| **2** | L6 | ~~`ToolSpec.timeout_seconds` → `asyncio.wait_for` 强制~~ | ✅ `verify_tool_execution_reliability.py` |
| **3** | L7 Output | 输出 Guard：secret/cookie/内部 URL 模式检测 → `output_blocked` 事件 | 新 verify |
| **3** | L6 | 输入 Guard PoC（PII/注入规则） | PoC script |
| **4** | L6 | shell/git Capability + sandbox hooks | PoC + tech-selection |
| **4** | L4 | 多租户 `tenant_id` 裁剪 tool 集 / credential 撤销 | 与 memory user scope 联动 |

---

## 11. 遗留问题

| 问题 | 影响 | 说明 |
|------|------|------|
| 无统一策略表文件 | 改规则需改代码或 Scenario YAML | HTTP 白名单与 allowlist 已 Scenario 化；危险 path 仍分散在 Policy + handler 纵深 |
| 通用输出 Guard 未覆盖所有回答 | 模型仍可能泄露响应中的敏感信息 | Private RAG 输出检测已覆盖；通用 final answer guard 待建 |
| 无输入 Guard | 恶意 prompt 仍可诱导多次安全工具调用 | 依赖 LLM + 白名单 + scope gate |
| Policy 与 `http_tools` 双重校验 job POST | 维护两处 | 故意纵深；dangerous_paths 以 Scenario 为单一声明源 |
| 非 HTTP 工具无 Sandbox | 无文件/终端/exec | 见 tech-selection §5 Sandbox 行 |
| 审批策略不可版本审计 | 合规场景难回放「当时规则」 | 无 `policy_version` 写入 EventStore；scope 裁决已有 `credential_binding_audit` |
| Demo 与 Kernel 默认混用 | 新部署易误用 Demo 配置 | Kernel 默认 `minimal`；watermark 必须显式 `SCENARIO=watermark` |
| M14 进程内 memory | 多实例/重启丢 session | single-process MVP；长期加密/外部 secret manager |

---

## 12. 非目标

- 不定义 `ToolResultModel` 字段与 Adapter（见 [data-flow-design.md](./data-flow-design.md)）
- 不定义 LangGraph 图拓扑（见 `agent/graph.py`）
- 不实现完整 RBAC / 多租户 IAM（M14 长期项；当前 MVP 为 binding + scope gate + audit）
- 不在本文档描述 RAG 内容安全（检索 poison 属 RAG 专题）
