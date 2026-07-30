"""Path-safe handoff from the reviewer to the local PDF resolver."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def resolve_local_pdf_path(pdf_root: Path | str, relative_pdf_path: str) -> Path:
    """Return a local PDF only when it remains confined to the configured root."""
    root = Path(pdf_root).resolve()
    pure = PurePosixPath(relative_pdf_path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative_pdf_path:
        raise ValueError("requested PDF path is outside configured local root")
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("requested PDF path is outside configured local root") from error
    if candidate.suffix.lower() != ".pdf":
        raise ValueError("requested local evidence must be a PDF")
    return candidate
