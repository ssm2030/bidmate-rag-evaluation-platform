from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence


@dataclass(frozen=True)
class RedactionFinding:
    kind: Literal["phone", "email", "person", "resident_id"]
    source_start: int
    source_end: int


@dataclass(frozen=True)
class ContextWindow:
    window_id: str
    document_id: str
    page_num: int
    source_start: int
    source_end: int
    source_text: str
    outbound_text: str
    section_hint: str | None


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:0\d{1,2}[- )]?\d{3,4}[- ]?\d{4})(?!\d)")
_RESIDENT_ID = re.compile(r"(?<!\d)\d{6}\s*[- ]?\s*[1-8]\d{6}(?!\d)")
_PERSON = re.compile(r"(담당자\s+)([가-힣]{2,4})")


def redact_outbound_text(text: str) -> tuple[str, tuple[RedactionFinding, ...]]:
    """Redact contact values from a copy while reporting offsets in the original text."""
    findings: list[RedactionFinding] = []

    for match in _EMAIL.finditer(text):
        findings.append(RedactionFinding("email", match.start(), match.end()))
    for match in _RESIDENT_ID.finditer(text):
        findings.append(RedactionFinding("resident_id", match.start(), match.end()))
    for match in _PHONE.finditer(text):
        findings.append(RedactionFinding("phone", match.start(), match.end()))
    for match in _PERSON.finditer(text):
        findings.append(RedactionFinding("person", match.start(2), match.end(2)))

    replacements = sorted(findings, key=lambda finding: (finding.source_start, finding.source_end))
    redacted_parts: list[str] = []
    cursor = 0
    marker_by_kind = {
        "email": "[EMAIL_REDACTED]",
        "phone": "[PHONE_REDACTED]",
        "person": "[PERSON_REDACTED]",
        "resident_id": "[RESIDENT_ID_REDACTED]",
    }
    for finding in replacements:
        if finding.source_start < cursor:
            continue
        redacted_parts.append(text[cursor : finding.source_start])
        redacted_parts.append(marker_by_kind[finding.kind])
        cursor = finding.source_end
    redacted_parts.append(text[cursor:])
    return "".join(redacted_parts), tuple(replacements)


def _window_id(document_id: str, page_num: int, start: int, end: int, text: str) -> str:
    canonical = "|".join(
        (document_id, str(page_num), str(start), str(end), hashlib.sha256(text.encode()).hexdigest())
    )
    return "w-" + hashlib.sha256(canonical.encode()).hexdigest()[:20]


def _section_hint(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return None


def build_context_windows(
    document_id: str,
    pages: Sequence[Mapping[str, object]],
    *,
    max_chars: int,
) -> tuple[ContextWindow, ...]:
    if not document_id:
        raise ValueError("document_id is required")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    windows: list[ContextWindow] = []
    for page in pages:
        page_num = page.get("page_num")
        text = page.get("text")
        if not isinstance(page_num, int) or page_num < 1:
            raise ValueError("page_num must be a positive integer")
        if not isinstance(text, str):
            raise ValueError("page text must be a string")
        for start in range(0, len(text), max_chars):
            source_text = text[start : start + max_chars]
            if not source_text:
                continue
            end = start + len(source_text)
            outbound_text, _ = redact_outbound_text(source_text)
            outbound_text = outbound_text[:max_chars]
            windows.append(
                ContextWindow(
                    window_id=_window_id(document_id, page_num, start, end, source_text),
                    document_id=document_id,
                    page_num=page_num,
                    source_start=start,
                    source_end=end,
                    source_text=source_text,
                    outbound_text=outbound_text,
                    section_hint=_section_hint(source_text),
                )
            )
    return tuple(windows)


_SECTION_KEYWORDS = (
    "requirement",
    "requirements",
    "scope",
    "evaluation",
    "security",
    "delivery",
    "proposal",
    "요구",
    "범위",
    "평가",
    "보안",
    "납품",
    "제안",
)


def _keyword_score(window: ContextWindow) -> int:
    haystack = f"{window.section_hint or ''}\n{window.outbound_text}".casefold()
    return sum(haystack.count(keyword) for keyword in _SECTION_KEYWORDS)


def select_ranked_context_windows(
    windows: Sequence[ContextWindow],
    *,
    max_windows: int,
    max_total_chars: int,
) -> tuple[ContextWindow, ...]:
    """Select a deterministic, document-distributed subset under one request budget."""
    if max_windows <= 0:
        raise ValueError("max_windows must be positive")
    if max_total_chars <= 0:
        raise ValueError("max_total_chars must be positive")
    by_document: dict[str, list[ContextWindow]] = {}
    for window in windows:
        by_document.setdefault(window.document_id, []).append(window)
    ranked = {
        document_id: sorted(
            candidates,
            key=lambda window: (
                -_keyword_score(window),
                window.page_num,
                window.source_start,
                window.window_id,
            ),
        )
        for document_id, candidates in by_document.items()
    }
    positions = {document_id: 0 for document_id in ranked}
    selected: list[ContextWindow] = []
    total_chars = 0
    while len(selected) < max_windows:
        made_progress = False
        for document_id, candidates in ranked.items():
            position = positions[document_id]
            while position < len(candidates):
                candidate = candidates[position]
                position += 1
                positions[document_id] = position
                candidate_chars = len(candidate.outbound_text)
                if total_chars + candidate_chars > max_total_chars:
                    continue
                selected.append(candidate)
                total_chars += candidate_chars
                made_progress = True
                break
            if len(selected) >= max_windows:
                break
        if not made_progress:
            break
    return tuple(selected)