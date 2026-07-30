"""Text normalization used only as an explicit resolver fallback."""

from __future__ import annotations

import re


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
