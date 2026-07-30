from __future__ import annotations

import math
from pathlib import Path

from bidmate_rag.demo.corpus import create_public_demo_corpus
from bidmate_rag.providers.demo import DeterministicDemoEmbedder, DeterministicDemoLLM
from bidmate_rag.schema import Chunk, RetrievedChunk


def test_demo_corpus_is_deterministic(tmp_path: Path) -> None:
    first = create_public_demo_corpus(tmp_path / "first", document_count=15)
    second = create_public_demo_corpus(tmp_path / "second", document_count=15)

    assert first.source_set_sha256 == second.source_set_sha256
    assert len(first.documents) == 15
    assert len(list(first.pdf_root.glob("*.pdf"))) == 15
    assert len(list(first.json_root.glob("*.json"))) == 15
    assert first.batch_config.name == "Batch_config.json"
    assert all(path.read_bytes().startswith(b"%PDF-") for path in first.pdf_root.glob("*.pdf"))
    assert first.chunks_path.is_file()
    assert first.metadata_path.is_file()


def test_demo_provider_is_deterministic_and_zero_cost() -> None:
    embedder = DeterministicDemoEmbedder(dimensions=64)
    first = embedder.embed_query("DemoProject07 budget requirements")
    second = embedder.embed_query("DemoProject07 budget requirements")
    assert first == second
    assert len(first) == 64
    assert math.isclose(sum(value * value for value in first), 1.0, rel_tol=1e-9)

    chunk = RetrievedChunk(
        rank=1,
        score=0.9,
        chunk=Chunk(
            chunk_id="demo-07",
            doc_id="demo-07",
            text="DemoProject07 budget is 107 million won and encryption is required.",
            text_with_meta="DemoProject07 budget is 107 million won and encryption is required.",
            char_count=70,
            section="requirements",
            chunk_index=0,
        ),
    )
    result = DeterministicDemoLLM().generate(
        question="What is the DemoProject07 budget?",
        context_chunks=[chunk],
        history=[],
        generation_config={"question_id": "q-demo", "run_id": "run-demo"},
        system_prompt="Use supplied evidence.",
    )
    assert "DemoProject07" in result.answer
    assert "107 million won" in result.answer
    assert result.cost_usd == 0.0
    assert result.retrieved_chunk_ids == ["demo-07"]