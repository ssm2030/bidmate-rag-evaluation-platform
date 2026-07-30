from __future__ import annotations

from pathlib import Path

from scripts.run_public_rag_demo import run_demo


def test_public_rag_demo_exercises_index_retrieval_and_generation(tmp_path: Path) -> None:
    result = run_demo(
        output_root=tmp_path / "public-demo",
        question="What is the DemoProject07 budget?",
    )

    assert "DemoProject07" in result.answer
    assert "107 million won" in result.answer
    assert result.retrieved_chunk_ids
    assert result.cost_usd == 0.0
    assert result.debug["offline_demo"] is True
