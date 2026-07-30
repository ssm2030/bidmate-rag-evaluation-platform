"""Run the real BidMate RAG pipeline against generated synthetic documents."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from bidmate_rag.demo.corpus import create_public_demo_corpus
from bidmate_rag.pipelines.runtime import build_runtime_pipeline
from bidmate_rag.schema import GenerationResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_demo(
    *,
    output_root: Path | str = Path("artifacts/public_demo"),
    question: str = "What is the DemoProject07 budget?",
) -> GenerationResult:
    """Generate the corpus, index it, retrieve evidence, and answer offline."""

    corpus = create_public_demo_corpus(output_root, document_count=15)
    previous = os.environ.get("BIDMATE_AUTO_BUILD_INDEX")
    os.environ["BIDMATE_AUTO_BUILD_INDEX"] = "1"
    try:
        pipeline, _, embedder, _ = build_runtime_pipeline(
            base_config_path=PROJECT_ROOT / "configs" / "base.yaml",
            provider_config_path=PROJECT_ROOT
            / "configs"
            / "providers"
            / "deterministic_demo.yaml",
            retrieval_config_path=PROJECT_ROOT / "configs" / "retrieval_public_demo.yaml",
            persist_dir=corpus.output_root / "rag" / "chroma",
            metadata_path=corpus.metadata_path,
            chunks_path=corpus.chunks_path,
        )
    finally:
        if previous is None:
            os.environ.pop("BIDMATE_AUTO_BUILD_INDEX", None)
        else:
            os.environ["BIDMATE_AUTO_BUILD_INDEX"] = previous

    return pipeline.answer(
        question,
        top_k=3,
        question_id="q-public-demo",
        scenario="public-demo",
        run_id="run-public-demo",
        embedding_provider=embedder.provider_name,
        embedding_model=embedder.model_name,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/public_demo"))
    parser.add_argument(
        "--question",
        default="What is the DemoProject07 budget?",
    )
    args = parser.parse_args()
    result = run_demo(output_root=args.output, question=args.question)
    print(
        json.dumps(
            {
                "answer": result.answer,
                "retrieved_chunk_ids": result.retrieved_chunk_ids,
                "retrieved_doc_ids": result.retrieved_doc_ids,
                "cost_usd": result.cost_usd,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
