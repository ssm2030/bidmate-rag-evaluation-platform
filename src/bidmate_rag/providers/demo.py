"""Deterministic zero-cost providers used by the public portfolio demo."""

from __future__ import annotations

import hashlib
import math
import re
from time import perf_counter

from bidmate_rag.providers.embeddings.base import BaseEmbeddingProvider
from bidmate_rag.providers.llm.base import BaseLLMProvider, RewriteResponse
from bidmate_rag.schema import GenerationResult, RetrievedChunk

_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣]+")


class DeterministicDemoEmbedder(BaseEmbeddingProvider):
    """Feature-hashing embedder with stable output and no model download."""

    provider_name = "deterministic-demo"
    model_name = "sha256-feature-hash-v1"

    def __init__(self, dimensions: int = 64) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be at least 8")
        self.dimensions = dimensions
        self.cumulative_tokens = 0

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = _TOKEN_PATTERN.findall(text.casefold())
        self.cumulative_tokens += len(tokens)
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._embed(query)


class DeterministicDemoLLM(BaseLLMProvider):
    """Evidence-forward deterministic answer generator for offline demos."""

    provider_name = "deterministic-demo"
    model_name = "evidence-first-v1"

    def generate(
        self,
        question: str,
        context_chunks: list[RetrievedChunk],
        history: list[dict],
        generation_config: dict,
        system_prompt: str,
    ) -> GenerationResult:
        del history, system_prompt
        started = perf_counter()
        visible = context_chunks[: int(generation_config.get("max_demo_chunks", 3))]
        if visible:
            evidence = visible[0].chunk.text.strip()
            answer = f"Based on the top retrieved evidence: {evidence}"
        else:
            answer = "No supporting evidence was retrieved."
        return GenerationResult(
            question_id=str(generation_config.get("question_id", "q-demo")),
            question=question,
            scenario=str(generation_config.get("scenario", "public-demo")),
            run_id=str(generation_config.get("run_id", "run-demo")),
            embedding_provider=str(
                generation_config.get("embedding_provider", "deterministic-demo")
            ),
            embedding_model=str(
                generation_config.get("embedding_model", "sha256-feature-hash-v1")
            ),
            llm_provider=self.provider_name,
            llm_model=self.model_name,
            answer=answer,
            retrieved_chunk_ids=[item.chunk.chunk_id for item in visible],
            retrieved_doc_ids=[item.chunk.doc_id for item in visible],
            retrieved_chunks=visible,
            latency_ms=round((perf_counter() - started) * 1000, 3),
            token_usage={"prompt": 0, "completion": 0, "total": 0},
            cost_usd=0.0,
            context="\n\n".join(item.chunk.text for item in visible),
            debug={"offline_demo": True},
        )

    def rewrite(
        self,
        prompt: str,
        *,
        max_tokens: int = 16000,
        timeout: int | None = 30,
    ) -> RewriteResponse:
        del max_tokens, timeout
        text = " ".join(prompt.split())
        return RewriteResponse(text=text[-512:])
