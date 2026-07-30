from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.publication.build_public_snapshot import build_snapshot
from scripts.publication.policy import PublicationPolicy
from scripts.publication.verify_public_snapshot import scan_worktree


def _policy() -> PublicationPolicy:
    return PublicationPolicy(
        include_roots=("src",),
        include_files=("P1_C1_F1_S1_T1.md",),
        exclude_globs=("**/__pycache__/**",),
        external_markdown={"RAG_Evaluation_SOP.md": "docs/reference/RAG_Evaluation_SOP.md"},
        prompt_paths=("P1_C1_F1_S1_T1.md",),
        expected_prompt_count=1,
        max_prompt_count=2,
        max_file_bytes=1024,
        forbidden_extensions=frozenset({".pdf", ".hwp", ".zip"}),
        allowed_loopback_hosts=frozenset({"127.0.0.1", "localhost"}),
    )


def test_builder_requires_empty_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    confidential = tmp_path / "confidential"
    destination = tmp_path / "public"
    source.mkdir()
    confidential.mkdir()
    destination.mkdir()
    (destination / "existing.txt").write_text("stop", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        build_snapshot(source, confidential, destination, _policy())


def test_builder_copies_only_named_external_markdown(tmp_path: Path) -> None:
    source = tmp_path / "source"
    confidential = tmp_path / "confidential"
    destination = tmp_path / "public"
    (source / "src").mkdir(parents=True)
    confidential.mkdir()
    (source / "src" / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (source / "P1_C1_F1_S1_T1.md").write_text("prompt\n", encoding="utf-8")
    (confidential / "RAG_Evaluation_SOP.md").write_text("approved\n", encoding="utf-8")
    (confidential / "private.pdf").write_bytes(b"%PDF-1.4\n")

    manifest = build_snapshot(source, confidential, destination, _policy())

    assert (destination / "src/app.py").read_text(encoding="utf-8") == "print('safe')\n"
    assert (destination / "docs/reference/RAG_Evaluation_SOP.md").read_text(
        encoding="utf-8"
    ) == "approved\n"
    assert not (destination / "private.pdf").exists()
    assert {row["path"] for row in manifest["files"]} == {
        "P1_C1_F1_S1_T1.md",
        "docs/reference/RAG_Evaluation_SOP.md",
        "src/app.py",
    }


def test_guard_rejects_pdf_signature_with_safe_extension(tmp_path: Path) -> None:
    root = tmp_path / "public"
    root.mkdir()
    (root / "P1_C1_F1_S1_T1.md").write_text("prompt\n", encoding="utf-8")
    (root / "renamed.bin").write_bytes(b"%PDF-1.4\n")

    findings = scan_worktree(root, _policy())

    assert any(finding.rule == "pdf-signature" for finding in findings)

def test_builder_sanitizes_public_package_lock_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    confidential = tmp_path / "confidential"
    destination = tmp_path / "public"
    templates = source / "scripts" / "publication" / "templates"
    (source / "src").mkdir(parents=True)
    templates.mkdir(parents=True)
    confidential.mkdir()
    (source / "P1_C1_F1_S1_T1.md").write_text("prompt\n", encoding="utf-8")
    (confidential / "RAG_Evaluation_SOP.md").write_text("approved\n", encoding="utf-8")
    (source / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "old-team-repository",
                "version": "0.0.1",
                "packages": {
                    "": {
                        "name": "old-team-repository",
                        "version": "0.0.1",
                        "license": "ISC",
                        "dependencies": {"safe-package": "1.0.0"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (templates / "package.public.json").write_text(
        json.dumps(
            {
                "name": "bidmate-rag-evaluation-platform",
                "version": "1.0.0",
                "private": True,
                "dependencies": {"safe-package": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    policy = replace(
        _policy(),
        include_files=("P1_C1_F1_S1_T1.md", "package-lock.json"),
    )

    build_snapshot(source, confidential, destination, policy)

    lock = json.loads((destination / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["name"] == "bidmate-rag-evaluation-platform"
    assert lock["version"] == "1.0.0"
    assert lock["packages"][""]["name"] == "bidmate-rag-evaluation-platform"
    assert lock["packages"][""]["version"] == "1.0.0"
    assert "license" not in lock["packages"][""]


def test_builder_maps_public_guides_and_pull_request_body(tmp_path: Path) -> None:
    source = tmp_path / "source"
    confidential = tmp_path / "confidential"
    destination = tmp_path / "public"
    templates = source / "scripts" / "publication" / "templates"
    (source / "src").mkdir(parents=True)
    templates.mkdir(parents=True)
    confidential.mkdir()
    (source / "P1_C1_F1_S1_T1.md").write_text("prompt\n", encoding="utf-8")
    (confidential / "RAG_Evaluation_SOP.md").write_text("approved\n", encoding="utf-8")
    expected = {
        "architecture.public.md": "docs/architecture.md",
        "evaluation-workflow.public.md": "docs/evaluation-workflow.md",
        "local-data.public.md": "docs/local-data.md",
        "PULL_REQUEST_BODY.public.md": ".github/PULL_REQUEST_BODY.md",
    }
    for template_name in expected:
        (templates / template_name).write_text(template_name + "\n", encoding="utf-8")

    build_snapshot(source, confidential, destination, _policy())

    for template_name, relative in expected.items():
        assert (destination / relative).read_text(encoding="utf-8") == template_name + "\n"
