"""PDF extraction value objects."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedPage:
    page_number: int
    text: str
