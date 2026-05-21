from __future__ import annotations

from pathlib import Path


SOURCE = Path(r"E:\down\miao_li_resume-1  (1).tex")
TARGET = Path(r"E:\down\miao_li_resume_agent_optimized.tex")


SKILLS = r"""\resumeSection{专业技能}
\resumeSkill{编程语言}{熟悉 Python、Java，了解 C\#、Lua、Go；具备后端服务、异步任务、自动化验证脚本和工程化调试经验}
\resumeSkill{LLM / Agent 工程}{熟悉 LangGraph、LangChain、FastAPI、SSE、Tool Calling、RAG、SQLite EventStore；掌握 Thread / Run / Timeline / Checkpoint 等 Agent Runtime 抽象}
\resumeSkill{隐私与安全}{联邦学习、差分隐私、保序加密、零知识证明；关注 Policy-aware RAG、ACL、上下文最小化、脱敏审计和敏感输出防护}
\resumeSkill{系统与工具}{Cursor、VS Code、Codex、Claude Code、Git；熟悉 SQLite、Redis Stream、MySQL、MinIO、Spring Boot 3、PyTorch}
"""


LEARN_AGENT = r"""\resumeSubheading
{}
{LearnAgent：面向水印任务的可信 Agent Runtime 与 Tool Execution 系统}
{独立完成}
{}

\begin{resumeItemList}
\resumeItem{基于 FastAPI + LangGraph + SQLite EventStore 构建本地单用户 Agent Runtime，抽象 Thread / Run / Event Timeline / Checkpoint 等运行时对象，支持后台 Run 创建、状态查询、SSE 输出、取消、审批恢复、归档会话与历史事件回放}

\resumeItem{设计 Run 状态机与 ExecutionEngine，覆盖 queued / running / waiting\_approval / cancelling / cancelled / completed / failed 等状态流转，并通过事件落库保证 token、tool\_start、tool\_end、approval、done、error 可追踪、可回放}

\resumeItem{抽象 ToolRegistry、LLMProvider、MemoryManager、PolicyRegistry 等薄适配层，将工具注册、模型配置、工作记忆 / RAG / EventStore、策略判定从 ChatRunner 中解耦，为后续多模型路由、工具治理和记忆管理演进预留边界}

\resumeItem{实现受控 Tool Calling 与工具审计，封装 \texttt{search\_docs}、\texttt{http\_get}、\texttt{http\_post} 等工具，记录 call id、工具类别、风险等级、参数、结果、耗时和成功 / 失败状态，并对 cookie、token、secret、raw set-cookie 等敏感字段进行脱敏}

\resumeItem{实现 Safety Gate 与 Approval 工作流，对创建水印任务等高风险 POST 操作引入用户确认、暂停恢复、reject / approve API 和事件审计，避免 Agent 在未授权情况下触发真实业务动作}

\resumeItem{建设 Chat MVP 与 Runtime Timeline，支持用户直接输入多轮对话、查看当前 Run 状态、回放工具调用与 memory summary；配套自动化验证脚本覆盖 Run 生命周期、取消、审批、工具审计、Timeline 投影和 Memory v1 摘要}
\end{resumeItemList}"""


PRIVATE_RAG = r"""\resumeSubheading
{}
{Policy-aware Private RAG：面向司法材料确权的安全知识库问答系统}
{独立完成}
{}

\begin{resumeItemList}
\resumeItem{构建面向司法材料确权场景的 RAG 知识库，将水印平台 API 契约、部署手册、安全基线、运维 Runbook、算法说明和测试用例等文档资产切分索引，支撑平台使用、任务排查和部署运维问答}

\resumeItem{实现混合检索链路，结合关键词检索、BM25、RRF 融合、查询改写、文档类型 boost 与可选向量检索，针对“任务一直 QUEUED / PROCESSING 怎么排查”“Redis Stream 默认 key 是什么”“如何配置 Worker”等高频问题提升召回稳定性}

\resumeItem{落地 Policy-aware RAG v1，为 chunk 增加 tenant\_id、doc\_id、ACL、classification、pii\_level、source\_hash、retention\_policy 等安全元数据，检索时执行 pre-filter + post-filter，保证跨租户、越权 ACL、超密级和高敏 PII chunk 不进入 prompt}

\resumeItem{设计 Private RAG Context Guard，将 allowed chunks 进入 prompt 前统一做上下文预算裁剪、不可信资料标记和引用要求约束，避免检索内容覆盖系统策略或扩大上下文暴露面}

\resumeItem{实现 Private RAG Output Guard，在 Run 输出链路的 \texttt{done} 前写入 \texttt{output\_guard\_checked} 事件，对 API key、token、cookie、邮箱、手机号等敏感输出进行确定性检测；命中风险时阻断原始 token 并返回降级说明}

\resumeItem{建设 RAG 评估与回归验证体系，覆盖检索命中率、引用覆盖率、API 选择正确率、危险动作拒识率、越权 chunk 阻断、上下文脱敏和输出守卫；当前 RAG profile 自动化验证保持 retrieval\_hit\_rate 约 0.95}
\end{resumeItemList}"""


def replace_range(text: str, start: int, end: int, replacement: str) -> str:
    if start < 0 or end <= start:
        raise RuntimeError(f"invalid replacement range: {start}..{end}")
    return text[:start] + replacement + text[end:]


def main() -> int:
    text = SOURCE.read_text(encoding="utf-8")

    skills_start = text.index(r"\resumeSection{专业技能}")
    skills_end = text.index(r"\resumeSection{项目经验}", skills_start)
    text = replace_range(text, skills_start, skills_end, SKILLS + "\n\n")

    learn_title = text.index("{LearnAgent")
    learn_start = text.rindex(r"\resumeSubheading", 0, learn_title)
    learn_end_marker = r"\vspace{0.08em}"
    learn_end = text.index(learn_end_marker, learn_title) + len(learn_end_marker)
    text = replace_range(text, learn_start, learn_end, LEARN_AGENT + "\n\n" + learn_end_marker)

    rag_title = text.index("{司法材料确权 RAG")
    rag_start = text.rindex(r"\resumeSubheading", 0, rag_title)
    rag_end = text.index("% ==================== 科研成果", rag_title)
    text = replace_range(text, rag_start, rag_end, PRIVATE_RAG + "\n\n")

    TARGET.write_text(text, encoding="utf-8")
    print(f"optimized_resume={TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
