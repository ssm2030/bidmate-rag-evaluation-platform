from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _script(name: str) -> str:
    return (PROJECT_ROOT / "scripts" / name).read_text(encoding="utf-8-sig")


def test_public_eval_launchers_never_default_to_sibling_data() -> None:
    for name in (
        "start_eval_tools.ps1",
        "start_eval_review.ps1",
        "test_eval_automation_mock.ps1",
        "test_eval_review_e2e.ps1",
    ):
        source = _script(name)
        assert "Split-Path $projectRoot -Parent" not in source
        assert "Split-Path $root -Parent" not in source
        assert r"artifacts\public_demo\source" in source


def test_public_eval_launchers_prepare_synthetic_data_when_needed() -> None:
    for name in (
        "start_eval_tools.ps1",
        "start_eval_review.ps1",
        "test_eval_automation_mock.ps1",
        "test_eval_review_e2e.ps1",
    ):
        assert "prepare_public_demo.py" in _script(name)

    assert "[string]$DataRoot = """ in _script("start_eval_tools.ps1")
