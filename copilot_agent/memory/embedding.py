from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from copilot_agent.settings import apply_hf_home, settings

_EMBED_MODEL: Any | None = None


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def deterministic_embedding(text: str, *, dims: int = 64) -> list[float]:
    """Stable pseudo-embedding for tests and offline deterministic recall."""
    vector = [0.0] * dims
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not normalized:
        return vector
    for token in normalized.split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dims
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return vector
    return [value / norm for value in vector]


def _get_model_embedder() -> Any:
    global _EMBED_MODEL
    if _EMBED_MODEL is not None:
        return _EMBED_MODEL
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding

    apply_hf_home(settings.hf_home)
    _EMBED_MODEL = HuggingFaceEmbedding(model_name=settings.memory_embedding_model)
    return _EMBED_MODEL


def embed_text(text: str, *, use_vector: bool, deterministic: bool = False) -> list[float] | None:
    normalized = (text or "").strip()
    if not normalized or not use_vector:
        return None
    if deterministic or settings.memory_embedding_deterministic:
        return deterministic_embedding(normalized)
    try:
        model = _get_model_embedder()
        vector = model.get_text_embedding(normalized)
        return [float(value) for value in vector]
    except Exception:
        return deterministic_embedding(normalized)
