"""Canonical JSON helpers shared by package producers and consumers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def canonical_jsonl(records: Iterable[Any]) -> bytes:
    return b"".join(canonical_json(record) for record in records)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
