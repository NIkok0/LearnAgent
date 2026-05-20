# Agent 开发学习指南

> 学习笔记：Agent 开发「最难的部分」讨论 + LearnAgent 模块成熟度矩阵。  
> **~~删除线~~** 表示 LearnAgent 侧已落地或已阶段性完成（截至 Execution Engine P0–P2、Memory v1.1）。

---

## Agent 开发最难的部分是哪几个

这是偏经验判断，没有唯一标准答案；结合你们在做 LearnAgent、以及面试知识库里的模块，**真正难的不是「接 LangChain」**，而是下面这几块——往往占项目 80% 的坑。

---

### 1. 可靠性与可控性（最难、也最常低估）

**问题**：LLM 非确定性 + 多步 tool → 同一条输入，今天能跑、明天换模型/温度就偏了。

难在：

- 不是单点 bug，是 **整条链路的方差**（选错 tool、参数 hallucination、过早结束、死循环）
- 传统软件是「错了必现」；Agent 是 **「看起来能跑，上线才暴雷」**

要补的东西：Guardrail、recursion/timeout、幂等、失败降级、**评测集 + 回归**（你们 README 里 eval 仍偏后置，但生产里它和功能代码同等重要）。

**LearnAgent 对应**：`safety_gate`、~~Run 超时~~、tool audit —— 只解决了 **一小条**；「整体任务成功率稳定 >90%」仍是长期战。

---

### 2. 上下文与记忆治理（第二难）

**问题**：窗口有限，但对话、工具结果、RAG、摘要、checkpoint 全往 context 里塞。

难在：

- **写什么、何时写、读什么、读多少** —— ~~Memory orchestration policy（你们刚做的 v1.1 就在啃这个）~~
- Working（checkpoint）vs Episodic（EventStore summary）vs Semantic（RAG）**三套历史经常不一致**
- 「记住了」和「记对了、没污染当前回答」是两回事

**LearnAgent 对应**：Checkpoint + Memory policy + 客户端 messages 三轨 —— 这是典型痛点，不是你们独有。

---

### 3. 产品级 Runtime 契约（第三难，框架不教）

**问题**：LangGraph 只管图怎么跑，不管 **thread/run/event/API/cancel/approval** 语义。

难在：

- Run FSM、事件 schema、Timeline 投影、SSE 兼容、多 tab 并发 —— **都要自己设计**
- Checkpoint 与 EventStore **各管一半**，对齐是架构题不是库能选的

**LearnAgent 对应**：ExecutionEngine + EventStore —— 这正是你们在练的核心；很多 demo 项目 **根本没有这一层**，所以「看起来简单」。

---

### 4. 工具与权限治理（安全边界）

**问题**：Agent 能调 API = 能闯祸。

难在：

- 白名单、风险分级、审批、参数校验、结果脱敏、审计可追溯
- **Run 内自主、Run 级才 HITL** —— 策略要清晰（你刚说的原则）
- 工具一多，LLM **选错 tool** 的概率上升（注意力稀释）

**LearnAgent 对应**：`ToolRegistry`、`PolicyRegistry`、dangerous POST approval —— 方向对；难的是 **策略持续演进**（PII、输出校验、按租户 tool 集）。

---

### 5. 可观测与排障（线上才知道多痛）

**问题**：用户说「它昨天胡说」—— 你怎么复现？

难在：

- `thread_id` / `run_id` / `tool_call_id` / trace 要 **串成一条链**
- 不能只有日志，要有 **可回放 timeline**（你们 EventStore 的价值在这里）
- token/cost/latency 与业务指标（任务完成率）要一起看

**LearnAgent 对应**：EventStore + Langfuse —— 关联还没做满（README 里的 observability correlation）。

---

### 6. 评测与迭代（决定能不能上生产）

**问题**：改 prompt / 换模型 / 加 tool 后，**有没有变差**？

难在：

- 没有标准答案的任务（开放问答、运维 copilot）很难自动打分
- 需要 **场景集 + 通过准则 + CI 回归**（你们有 verify 脚本，但是 MVP 级）
- RAG 还要单独评 recall/precision

很多团队 **功能做完了 30%，评测与迭代做了剩下 70% 还停在 0%**。

---

### 7. 相对「没那么难」、但容易占时间的

| 看起来难 | 其实 |
|----------|------|
| 接 LLM API | 成熟，文档多 |
| LangGraph 搭 ReAct 环 | 教程多，1–2 天能跑 |
| 接 RAG | 模式固定，坑在 **chunk/评测** 不在接库 |
| Multi-Agent 框架选型 | **难在协作语义和运维**，不在 import CrewAI |

---

### 一张优先级（做产品 Agent 时）

```text
难且必须长期投入：
  ① 可靠性 + 评测闭环
  ② Memory / Context 治理
  ③ Runtime 契约 + 可观测

难但可阶段性收敛：
  ④ Tool / 安全治理
  ⑤ 成本与并发（限流、队列）

相对后置（除非业务需要）：
  ⑥ Multi-Agent 拆分
  ⑦ 外部 Memory 服务（Mem0/Zep）
```

---

### 和你在学的八股怎么对应

| 八股章节 | 难在哪 |
|----------|--------|
| Prompt / Context | 不是写文案，是 **窗口经济学** |
| Memory | **策略** 比存储难 |
| RAG | **检索质量 + 与 Memory 分工** |
| Tool / MCP | **治理与审计** |
| Planning | 计划 **漂移、与执行脱节** |
| Multi-Agent | **状态一致、调试、成本** |
| 评测 | 往往 **整个团队最缺的一章** |
| 安全 | **边界定义** 比实现难 |

---

### 面试/自评一句话

> Agent 开发最难的三件事：**让多步行为稳定可预期、让记忆和上下文不打架、让 Run 从 demo 变成可审计的产品 runtime**；框架只解决「能跑一轮」，不解决「能上线、能复盘、能迭代」。

---

## LearnAgent 模块成熟度矩阵

按 README §3 / §7 模块，加上几个 **横切能力**（很多项目单独写在 roadmap 外，但决定能不能上线）。评分说明：

- **行业难度 1–5**：做「产品级、可长期维护」时的普遍难度（5 最难）
- **完成度**：LearnAgent **当前** 离「单用户 MVP 够用 → 可上线雏形」还有多远（不是代码行数）

---

### 总览表

| 模块 | 行业难度 | 完成度 | 你已有 | 你还缺什么 |
|------|:--------:|:------:|--------|------------|
| **Runtime 契约（EventStore + API）** | 4 | **80%** | thread/run/event、Timeline、Thread 生命周期、REST/SSE/WS、~~event schema v1~~、~~事件分页~~、~~run↔checkpoint 元数据~~、~~archive 清理 checkpoint~~ | UI 长 Run cursor 拉取；完整 checkpoint↔timeline 互跳 |
| **Execution Engine** | 4 | **55%** | asyncio Run、cancel/approve、SSE、orphan cleanup、~~run timeout~~、~~全局限流~~、~~interrupt + Command(resume)~~、~~waiting_approval rehydrate~~ | ~~并发上限~~、幂等、进程重启后 **running** run 恢复；~~approval interrupt resume（原为整图重跑）~~ |
| **Memory + Memory Manager** | 5 | **45%** | 三层划分、~~v1.1 policy~~、~~preview API~~、summary 事件 | **working memory 真相源**（checkpoint vs 客户端 messages）；checkpoint **压缩**；向量 episodic；与 checkpoint 一致性校验 |
| **Guardrail + Policy** | 4 | **35%** | safety_gate、危险 POST 审批、HTTP 白名单、tool 脱敏 | 输入/输出校验、PII/secret 策略、策略版本与审计；~~Run 内自主 / 边界 HITL 的 **策略表文档化**~~（README §6 已更新 approval 语义） |
| **Tool + ToolRegistry** | 3 | **55%** | Registry、Schema、audit envelope、3 个工具 | **timeout/retry 真正执行**；失败路径审计全覆盖；MCP；按角色/租户 tool 白名单 |
| **Observability** | 4 | **40%** | EventStore timeline、Langfuse、tool audit | **thread/run/tool_call ↔ trace** 统一；token/cost/latency 进 event 或 metrics；一致性告警 |
| **Planning** | 4 | **25%** | ReAct 环、observe-only planner、`plan_created` | `plan_updated`、步骤 outcome、Plan-and-Execute；计划与执行 **可对照复盘** |
| **RAG（Semantic Memory）** | 3 | **50%** | 关键词 + 可选向量、混合检索配置 | 索引/检索 **评测**（RAGAS 等 CI）；与 episodic 注入边界在 prompt 里固化 |
| **LLM / LLMProvider** | 2 | **60%** | OpenAI-compatible、薄 Provider、~~MAX_LLM_INFLIGHT 限流~~ | fallback、路由、**cost/token 统计**、prompt 版本 |
| **评测与可靠性（Eval）** | 5 | **15%** | 多个 verify 脚本、部分 phase4 RAG eval、~~verify_session_mvp（timeout/interrupt/并发/rehydrate）~~ | **端到端场景集**、换模型/改 prompt **回归**；任务成功率指标；MVP acceptance 需稳定跑在 learnagent312 |
| **Multi-Agent / 多租户 / 外部队列** | 4–5 | **0–5%** | 文档与选型 | 故意未做；上生产前再碰 |

---

### 雷达图（文字版）

```text
                    Eval(15%) ←── 最大缺口
                       │
    Guardrail(35%) ────┼──── Memory(45%)
                       │
Observability(40%) ────┼──── Execution(55%)
                       │
         Planning(25%) ─ Runtime/API(80%) ←── 相对最强
```

**结论**：骨架（Runtime + 图 + 工具 + 基础安全）有了；**缺的是「稳、准、可迭代」**——Memory 治理、Eval、Observability 关联、Guardrail 深度。

---

### 分模块：你缺什么（可执行清单）

#### 1. Runtime 契约 — 完成度 80%（难度 4）

**已有**：EventStore 事实源、Run FSM、Timeline 投影、Thread active/ended/archived、~~`payload.schema_version: 1`~~、~~事件 cursor 分页~~、~~`run_completed_meta` / `run_checkpoint_meta`~~、~~archive 时 `CheckpointStore.purge_thread`~~。

**缺**：
- ~~`payload` 无 `schema_version`，演进易碎~~
- ~~Checkpoint 与 Run **无互指**~~ → `run_checkpoint_meta` + `run_completed_meta` 已写摘要；仍缺 UI 一键跳 graph state
- ~~事件列表无分页（长 Run 会胀）~~ → API 已支持 `after_id`/`limit`；UI 仍可能一次拉全量
- ~~Thread archived **不清理** checkpoint~~

**建议**：P3 UI 对长 Run 用 cursor 循环拉 events；Eval 脚本覆盖 checkpoint link。

---

#### 2. Execution Engine — 55%（难度 4）

**已有**：单进程 Run、cancel、approval 暂停、cooperative cancel、~~run timeout~~、~~MAX_CONCURRENT_RUNS / MAX_LLM_INFLIGHT~~、~~GraphInterrupted 单路径~~、~~Command(resume=True/False)~~、~~waiting_approval 重启 rehydrate~~。

**缺**：
- ~~多 tab / 高并发 **无限流**~~
- 服务重启 **running run 不能 durable resume**（`waiting_approval` 已可 rehydrate）
- ~~Approval **整图重跑**~~（费钱、timeline 重复、和「Run 内自主」理念不完全一致）

**建议**：~~先 **timeout + MAX_CONCURRENT_RUNS**~~；~~再 PoC LangGraph interrupt 只拦危险 tool~~。

---

#### 3. Memory + Memory Manager — 45%（难度 5）⭐ 核心短板

**已有**：~~v1.1 policy~~、episodic 注入、~~`GET .../memory`~~、failed/cancelled 排除。

**缺**（和我们聊过的三轨问题）：
- 客户端 `messages[]`、Checkpoint、Episodic 摘要 **没有单一真相源**
- Checkpoint **messages 无限涨**，无压缩
- Keyword recall 精度有限；无向量 episodic
- Thread archived ~~不清理~~ checkpoint（Runtime 已 purge；Memory 文档仍建议核对 episodic 边界）

**建议**（优先级最高之一）：
1. 下轮只收 **最后一条 user message**，history 从 checkpoint 读  
2. Run 末或 idle 时 **checkpoint 摘要/截断**  
3. 脚本：checkpoint 条数 vs EventStore token 事件 **一致性检查**

---

#### 4. Guardrail — 35%（难度 4）

**已有**：危险 POST + `confirm_dangerous`、路径白名单、审批 Run。

**缺**：
- 无 **输出** guard（幻觉 API、泄露 cookie 的回复）
- 无系统化 **PII/secret** 检测（仅 audit sanitizer 一部分）
- Policy 不可配置/versioned；~~无「Run 内自主、仅边界 HITL」的 **显式策略表**~~

**建议**：把「哪些 tool 永远自动、哪些必须 approval」写成 `PolicyRegistry` 配置 + README 一张表。

---

#### 5. Tool — 55%（难度 3）

**已有**：ToolSpec、StructuredTool、audit v1。

**缺**：
- `timeout_seconds` **未 enforce**
- 工具失败时的 **统一 error envelope** 给 LLM 仍不完整
- 无 MCP、无 tool 版本

**建议**：Execution 层统一 tool timeout；补失败 tool 的 verify 用例。

---

#### 6. Observability — 40%（难度 4）

**已有**：Langfuse + EventStore 双轨。

**缺**：
- 双轨 **未关联**（排障时要人工对 id）
- 无 cost/token **聚合**
- 无「EventStore vs LangGraph state」不一致 warning

**建议**：run meta 事件写入 `trace_id`；README 已有方向，代码未落地。

---

#### 7. Planning — 25%（难度 4）

**已有**：ReAct = 隐式 planning；planner 只打 log 型 `plan_created`。

**缺**：
- 无显式 plan 步骤、无 `plan_updated`、无步骤成败
- Timeline 里 **看不出「计划 vs 实际」**

**建议**：MVP 后可做；**不要早于 Memory/Eval**。

---

#### 8. RAG — 50%（难度 3）

**已有**：build_index、keyword/vector、混合权重。

**缺**：
- **检索质量未进 CI 门禁**（有 ragas 脚本但非主线）
- chunk 策略、top_k 缺 **场景化调参记录**

**建议**：固定 10 条运维问答做 golden set，改 RAG 必跑。

---

#### 9. LLM — 60%（难度 2）

**已有**：DeepSeek/OpenAI 兼容、LLMProvider 薄封装、~~LLM inflight 限流~~。

**缺**：fallback、路由、**每次 run 的 token/cost**。

**建议**：与 Observability 一起做，改动小、收益直观。

---

#### 10. 评测与可靠性（Eval）— 15%（难度 5）⭐ 最大缺口

**已有**：`verify_*` 脚本（偏组件测试）、~~`verify_session_mvp`（timeout / interrupt resume / 并发 / rehydrate）~~。

**缺**：
- **没有**「用户问 Java 是否存活 → 必须调 http_get」类 **端到端行为集**
- 改 graph/prompt/tool **无回归分数**
- `verify_mvp_runtime_acceptance` 依赖 LLM + 环境，**未形成稳定 CI 习惯**

**建议**：这是「Agent 最难部分」在你项目里的投影——**优先补 5–10 条 golden scenario + PASS/FAIL 汇总**。  
实施细化见：[docs/eval-implementation-plan.md](./eval-implementation-plan.md)

---

### 你还完全没做、但 README 已声明后置的

| 能力 | 难度 | 说明 |
|------|:----:|------|
| 多用户 / 多租户 | 5 | `tenant_id`、Memory/RAG 隔离、配额 |
| 外部队列（Temporal/Celery） | 4 | 长 Run、重启恢复 |
| Multi-Agent | 4 | Supervisor 子图即可，不必换框架 |
| Mem0/Zep | 3 | 等 Memory 策略稳定再 PoC |
| Sandbox（代码/终端） | 5 | 与当前 HTTP tool 模型不同 |

这些 **不是 MVP 缺项**，别焦虑；上 B 端或多 Agent 再上。

---

### 建议啃的顺序（结合难度 × 缺口 × 已有基础）

```text
第 1 波（把 MVP 变成「能信」）          难度高但你们已有底子
  ① Eval：golden scenarios + mvp acceptance 稳定 CI
  ② Memory：checkpoint 为 working memory 真相 + 压缩
  ③ Execution：~~timeout + 并发上限~~ + tool timeout enforce

第 2 波（能排障、能迭代）
  ④ Observability：trace_id ↔ run_id 关联 + token/cost
  ⑤ Guardrail：策略表 + 输出/PII 最小校验
  ⑥ EventStore：schema version + run↔checkpoint 元数据

第 3 波（增强，非阻塞）
  ⑦ RAG eval 进 CI
  ⑧ ~~Approval → LangGraph interrupt~~
  ⑨ Planning plan_updated / Plan-and-Execute
  ⑩ LLM fallback / LiteLLM PoC
```

---

### 一句话：你缺什么

> **不缺「能跑的 Agent」；缺「能证明没变坏、记忆不打架、出事能查、边界可控」的四件套——Eval、Memory/Checkpoint 统一、Observability 关联、Guardrail 深化。**  

