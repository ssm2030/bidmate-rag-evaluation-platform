"""Generate the synthetic public BidMate demo corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bidmate_rag.demo.corpus import create_public_demo_corpus


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/public_demo"))
    parser.add_argument("--documents", type=int, default=15)
    args = parser.parse_args()
    corpus = create_public_demo_corpus(args.output, document_count=args.documents)
    print(
        json.dumps(
            {
                "documents": len(corpus.documents),
                "source_root": str(corpus.source_root),
                "chunks_path": str(corpus.chunks_path),
                "metadata_path": str(corpus.metadata_path),
                "source_set_sha256": corpus.source_set_sha256,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
