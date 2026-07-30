from pathlib import Path

from scripts.publication.policy import load_policy


def test_publication_policy_has_fixed_sensitive_boundaries() -> None:
    policy = load_policy(Path("configs/publication/public_snapshot.json"))
    assert policy.expected_prompt_count == 16
    assert policy.max_prompt_count == 19
    assert policy.max_file_bytes == 10 * 1024 * 1024
    assert policy.forbidden_extensions >= {
        ".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".zip", ".7z", ".rar"
    }
    assert set(policy.external_markdown) == {
        "Master_QA_Print_Sheet_v6.md",
        "Master_QA_Print_Sheet_v7.md",
        "Project-structure.md",
        "RAG_Batch_Control_Plan.md",
        "RAG_Evaluation_SOP.md",
        "평가 로직 수정.md",
    }

def test_publication_policy_includes_only_existing_source_files() -> None:
    root = Path(".")
    policy = load_policy(root / "configs/publication/public_snapshot.json")
    missing = [relative for relative in policy.include_files if not (root / relative).is_file()]
    assert missing == []
