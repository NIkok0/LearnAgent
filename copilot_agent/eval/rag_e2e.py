from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from copilot_agent.eval.llm_client import get_eval_chat_model
from copilot_agent.rag.citations import citations_from_chunks
from copilot_agent.rag.context_guard import build_guarded_context
from copilot_agent.rag.retriever import RagStore
from copilot_agent.settings import settings


@dataclass(frozen=True)
class RagE2EResult:
    question: str
    answer: str
    contexts: list[str]
    retrieved_sources: list[str]
    citations: list[dict[str, object]]
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "contexts": self.contexts,
            "retrieved_sources": self.retrieved_sources,
            "citations": self.citations,
            "truncated": self.truncated,
        }


def retrieve_and_answer(
    question: str,
    store: RagStore,
    *,
    top_k: int = 6,
    llm: Any | None = None,
    budget_chars: int | None = None,
) -> RagE2EResult:
    budget = budget_chars or settings.rag_context_budget_chars
    parts = store.search(question, top_k=top_k)
    guarded = build_guarded_context(
        parts,
        max_chars=budget,
        require_citations=settings.private_rag_require_citations,
    )
    citations = [item.model_dump(exclude_none=True) for item in citations_from_chunks(guarded.chunks)]
    retrieved_sources = list(dict.fromkeys(chunk.source for chunk in guarded.chunks))
    contexts = [chunk.text for chunk in guarded.chunks]
    context_block = guarded.markdown

    if not context_block.strip():
        return RagE2EResult(
            question=question,
            answer="No relevant documentation was retrieved for this question.",
            contexts=[],
            retrieved_sources=[],
            citations=[],
            truncated=False,
        )

    llm_client = llm or get_eval_chat_model()
    prompt = (
        "Answer the question using ONLY the retrieved documentation below. "
        "Cite source file names explicitly in your answer.\n\n"
        f"Question: {question}\n\n"
        f"Documentation:\n{context_block}\n\n"
        "Answer:"
    )
    response = llm_client.invoke(prompt)
    answer = str(getattr(response, "content", response) or "").strip()
    return RagE2EResult(
        question=question,
        answer=answer,
        contexts=contexts,
        retrieved_sources=retrieved_sources,
        citations=citations,
        truncated=guarded.truncated,
    )
