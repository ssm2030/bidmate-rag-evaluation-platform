"""Metadata returned for deterministic approved-snapshot exports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApprovedExport:
    """A server-rooted, byte-stable legacy export bundle."""

    export_id: str
    standard: Path
    safety: Path
    checksum: str
    item_count: int
    relative_path: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "export_id": self.export_id,
            "kind": "legacy_v1",
            "relative_path": self.relative_path,
            "checksum": self.checksum,
            "item_count": self.item_count,
            "standard": f"{self.relative_path}/legacy/standard/{self.standard.name}",
            "safety": f"{self.relative_path}/legacy/abstention_safety/{self.safety.name}",
        }
