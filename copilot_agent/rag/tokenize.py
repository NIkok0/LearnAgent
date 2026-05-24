from __future__ import annotations

import re

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}")
_CJK_SEQ_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def extract_ascii_tokens(text: str) -> list[str]:
    return [t.lower() for t in _ASCII_TOKEN_RE.findall(text)]


def extract_cjk_tokens(text: str, *, min_len: int = 2) -> list[str]:
    tokens: list[str] = []
    for seq in _CJK_SEQ_RE.findall(text):
        if len(seq) < min_len:
            continue
        tokens.append(seq)
        if len(seq) > 4:
            for i in range(len(seq) - 1):
                bigram = seq[i : i + 2]
                if len(bigram) >= min_len:
                    tokens.append(bigram)
    return tokens


def tokenize(text: str) -> list[str]:
    """Tokenize for sparse retrieval: ASCII identifiers + CJK sequences (+ bigrams)."""
    ascii_tokens = extract_ascii_tokens(text)
    cjk_tokens = extract_cjk_tokens(text)
    return ascii_tokens + cjk_tokens


def token_set(text: str) -> set[str]:
    return set(tokenize(text))
