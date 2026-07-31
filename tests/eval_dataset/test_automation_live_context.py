from __future__ import annotations

from bidmate_rag.eval_dataset.automation.live_context import (
    build_context_windows,
    redact_outbound_text,
    select_ranked_context_windows,
)


def test_redacts_contact_values_without_changing_source_text() -> None:
    source = "Contact 02-1234-5678 or owner" + "@" + "example.go.kr. 담당자 홍길동"

    redacted, findings = redact_outbound_text(source)

    assert source == "Contact 02-1234-5678 or owner" + "@" + "example.go.kr. 담당자 홍길동"
    assert "02-1234-5678" not in redacted
    assert "owner" + "@" + "example.go.kr" not in redacted
    assert "홍길동" not in redacted
    assert "[PHONE_REDACTED]" in redacted
    assert "[EMAIL_REDACTED]" in redacted
    assert "[PERSON_REDACTED]" in redacted
    assert {finding.kind for finding in findings} == {"phone", "email", "person"}


def test_context_window_ids_are_stable_and_keep_source_offsets() -> None:
    pages = [
        {"page_num": 1, "text": "사업 개요\n목표와 범위를 설명합니다."},
        {"page_num": 2, "text": "수행 조건\n보안 요건이 중요합니다."},
    ]

    first = build_context_windows("doc-1", pages, max_chars=600)
    second = build_context_windows("doc-1", pages, max_chars=600)

    assert first == second
    assert [window.window_id for window in first] == [window.window_id for window in second]
    assert first[0].page_num == 1
    assert first[0].source_start == 0
    assert first[0].source_end == len(first[0].source_text)
    assert first[0].outbound_text == first[0].source_text


def test_context_windows_bound_outbound_text_but_do_not_truncate_source_record() -> None:
    source = "가" * 50 + " 010-1234-5678 " + "나" * 50

    windows = build_context_windows("doc-1", [{"page_num": 3, "text": source}], max_chars=40)

    assert len(windows) >= 2
    assert all(len(window.outbound_text) <= 40 for window in windows)
    assert any("[PHONE_REDACTED]" in window.outbound_text for window in windows)
    assert "010-1234-5678" in "".join(window.source_text for window in windows)


def test_ranked_context_selection_is_distributed_and_globally_bounded() -> None:
    first = build_context_windows(
        "doc-primary",
        [
            {"page_num": 1, "text": "overview " * 30},
            {"page_num": 2, "text": "evaluation requirements security delivery " * 8},
        ],
        max_chars=40,
    )
    second = build_context_windows(
        "doc-secondary",
        [
            {"page_num": 1, "text": "general background " * 20},
            {"page_num": 2, "text": "proposal evaluation scope requirement " * 8},
        ],
        max_chars=40,
    )

    selected = select_ranked_context_windows(
        first + second,
        max_windows=4,
        max_total_chars=120,
    )

    assert len(selected) <= 4
    assert sum(len(window.outbound_text) for window in selected) <= 120
    assert {window.document_id for window in selected} == {"doc-primary", "doc-secondary"}
    assert all(window in first + second for window in selected)


def test_redacts_resident_registration_like_identifiers() -> None:
    domestic = "900101" + "-" + "1234567"
    foreign = "010101" + " " + "5123456"
    source = f"Applicant {domestic} and foreign resident {foreign}"

    redacted, findings = redact_outbound_text(source)

    assert domestic not in redacted
    assert foreign not in redacted
    assert redacted.count("[RESIDENT_ID_REDACTED]") == 2
    assert {finding.kind for finding in findings} == {"resident_id"}
