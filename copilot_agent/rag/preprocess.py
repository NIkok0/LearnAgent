from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreprocessedSection:
    start_line: int
    text: str
    section_title: str = ""
    heading_path: str = ""
    source_format: str = "markdown"
    page_number: int | None = None
    ocr_used: bool = False
    ocr_required: bool = False


@dataclass(frozen=True)
class PreprocessedDocument:
    source_format: str
    sections: tuple[PreprocessedSection, ...]
    ocr_required_pages: tuple[int, ...] = ()


class DocumentPreprocessor:
    """Normalize supported document formats into section-like text blocks."""

    def preprocess(self, path: Path) -> PreprocessedDocument:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return self._preprocess_pdf(path)
        return PreprocessedDocument(
            source_format="markdown",
            sections=tuple(_split_markdown_sections(path.read_text(encoding="utf-8").splitlines())),
        )

    def _preprocess_pdf(self, path: Path) -> PreprocessedDocument:
        try:
            from pypdf import PdfReader
        except ImportError:
            log.warning("pypdf is not installed; skipping PDF ingest for %s", path)
            return PreprocessedDocument(source_format="pdf", sections=tuple(), ocr_required_pages=tuple())

        sections: list[PreprocessedSection] = []
        ocr_required_pages: list[int] = []
        try:
            reader = PdfReader(str(path))
        except Exception as exc:
            log.warning("Failed to read PDF %s: %s", path, exc)
            return PreprocessedDocument(source_format="pdf", sections=tuple(), ocr_required_pages=tuple())

        for index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                log.warning("Failed to extract text from %s page %s: %s", path, index, exc)
                text = ""
            normalized = _normalize_pdf_text(text)
            if len(normalized.strip()) < 20:
                ocr_required_pages.append(index)
                continue
            title = _pdf_page_title(normalized, index)
            sections.append(
                PreprocessedSection(
                    start_line=index,
                    text=f"# {title}\n\n{normalized}",
                    section_title=title,
                    heading_path=title,
                    source_format="pdf",
                    page_number=index,
                    ocr_used=False,
                    ocr_required=False,
                )
            )
        if ocr_required_pages:
            log.info("PDF %s has pages requiring OCR: %s", path.name, ocr_required_pages)
        return PreprocessedDocument(
            source_format="pdf",
            sections=tuple(sections),
            ocr_required_pages=tuple(ocr_required_pages),
        )


def _heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    return len(stripped) - len(stripped.lstrip("#"))


def _heading_title(line: str) -> str:
    return line.lstrip("#").strip()


def _update_heading_stack(stack: list[tuple[int, str]], line: str) -> None:
    level = _heading_level(line)
    if level == 0:
        return
    title = _heading_title(line)
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))


def _heading_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def _split_markdown_sections(lines: list[str]) -> list[PreprocessedSection]:
    heading_stack: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    section_title = ""
    heading_path = ""
    sections: list[PreprocessedSection] = []

    for i, line in enumerate(lines, start=1):
        if line.startswith("#"):
            _update_heading_stack(heading_stack, line)
            if buf:
                sections.append(
                    PreprocessedSection(
                        start_line=start,
                        text="\n".join(buf),
                        section_title=section_title,
                        heading_path=heading_path,
                    )
                )
            buf = [line]
            start = i
            section_title = _heading_title(line)
            heading_path = _heading_path(heading_stack)
        else:
            if not buf:
                buf = [line]
                start = i
            else:
                buf.append(line)

    if buf:
        sections.append(
            PreprocessedSection(
                start_line=start,
                text="\n".join(buf),
                section_title=section_title,
                heading_path=heading_path,
            )
        )
    return sections


def _normalize_pdf_text(text: str) -> str:
    lines = [line.strip() for line in str(text or "").replace("\r", "\n").splitlines()]
    return "\n".join(line for line in lines if line)


def _pdf_page_title(text: str, page_number: int) -> str:
    for line in text.splitlines():
        title = line.strip().strip("#")
        if title:
            return title[:120]
    return f"Page {page_number}"
