"""Explicit loopback configuration for the local review API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReviewSettings:
    database_path: Path
    pdf_root: Path
    host: str = "127.0.0.1"
    port: int = 8101

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("review API is loopback-only")
