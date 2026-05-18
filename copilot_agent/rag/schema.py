from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DocChunk:
    source: str
    start_line: int
    text: str

    @property
    def key(self) -> tuple[str, int]:
        return (self.source, self.start_line)


def format_chunks_for_prompt(parts: list[DocChunk], max_chars: int = 12000) -> str:
    blocks = []
    n = 0
    for p in parts:
        header = f"--- {p.source} (line ~{p.start_line}) ---\n"
        block = header + p.text
        if n + len(block) > max_chars:
            break
        blocks.append(block)
        n += len(block)
    return "\n\n".join(blocks)
