from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AutomationSettings:
    pdf_root: Path
    mock_enabled: bool = False
