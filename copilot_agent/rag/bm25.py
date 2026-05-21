from __future__ import annotations

import math

from copilot_agent.rag.schema import DocChunk
from copilot_agent.rag.tokenize import tokenize

DEFAULT_K1 = 1.5
DEFAULT_B = 0.75


class BM25Index:
    """Lightweight Okapi BM25 over in-memory DocChunk corpus."""

    def __init__(
        self,
        chunks: list[DocChunk],
        *,
        k1: float = DEFAULT_K1,
        b: float = DEFAULT_B,
    ) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self._doc_tokens = [tokenize(c.text) for c in chunks]
        lengths = [len(t) for t in self._doc_tokens]
        self._avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
        self._df: dict[str, int] = {}
        for tokens in self._doc_tokens:
            for term in set(tokens):
                self._df[term] = self._df.get(term, 0) + 1
        self._n = len(chunks)

    def scores(self, query: str) -> dict[tuple[str, int], float]:
        q_terms = tokenize(query)
        if not q_terms or not self.chunks:
            return {}
        raw: dict[tuple[str, int], float] = {}
        for idx, chunk in enumerate(self.chunks):
            tokens = self._doc_tokens[idx]
            if not tokens:
                continue
            dl = len(tokens)
            tf_map: dict[str, int] = {}
            for t in tokens:
                tf_map[t] = tf_map.get(t, 0) + 1
            score = 0.0
            for term in q_terms:
                tf = tf_map.get(term, 0)
                if tf == 0:
                    continue
                df = self._df.get(term, 0)
                idf = math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / self._avgdl)
                score += idf * (tf * (self.k1 + 1.0)) / denom
            if score > 0:
                raw[chunk.key] = score
        if not raw:
            return {}
        max_s = max(raw.values()) or 1.0
        return {k: v / max_s for k, v in raw.items()}
