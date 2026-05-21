from __future__ import annotations

import logging
import threading
from typing import Any

from copilot_agent.rag.schema import DocChunk
from copilot_agent.settings import settings

log = logging.getLogger(__name__)

_model_lock = threading.Lock()
_cross_encoder: Any | None = None
_cross_encoder_unavailable = False


def rerank_available() -> bool:
    if _cross_encoder_unavailable:
        return False
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return False
    return True


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _get_cross_encoder() -> Any:
    global _cross_encoder, _cross_encoder_unavailable
    if _cross_encoder is not None:
        return _cross_encoder
    with _model_lock:
        if _cross_encoder is not None:
            return _cross_encoder
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            _cross_encoder_unavailable = True
            raise RuntimeError("sentence-transformers is required for cross-encoder rerank") from exc
        log.info("Loading cross-encoder rerank model: %s", settings.rag_rerank_model)
        _cross_encoder = CrossEncoder(settings.rag_rerank_model)
        return _cross_encoder


def rerank_chunks(query: str, chunks: list[DocChunk], *, top_k: int) -> list[DocChunk]:
    """Re-score fusion candidates with a cross-encoder; return top_k chunks."""
    if not chunks or top_k <= 0:
        return []
    if len(chunks) <= top_k:
        return list(chunks)

    if not settings.rag_rerank_enabled:
        return list(chunks[:top_k])

    if not rerank_available():
        log.warning("RAG rerank enabled but sentence-transformers unavailable; skipping rerank")
        return list(chunks[:top_k])

    try:
        model = _get_cross_encoder()
    except Exception as exc:
        global _cross_encoder_unavailable
        _cross_encoder_unavailable = True
        log.warning("Cross-encoder rerank unavailable; falling back to fusion order: %s", exc)
        return list(chunks[:top_k])

    q = query.strip() or " "
    max_chars = settings.rag_rerank_max_chars
    pairs = [(q, _truncate(chunk.text, max_chars)) for chunk in chunks]
    scores = model.predict(pairs, show_progress_bar=False)
    ranked = sorted(zip(scores, chunks), key=lambda item: (-float(item[0]), item[1].source, item[1].start_line))
    return [chunk for _, chunk in ranked[:top_k]]
