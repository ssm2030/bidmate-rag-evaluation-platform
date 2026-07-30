from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def schema_v2_fixture(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "schema_v2_valid"
    fixture = tmp_path / "packages" / "schema_v2_valid"
    fixture.mkdir(parents=True)
    for path in source.iterdir():
        if path.is_file():
            fixture.joinpath(path.name).write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    return fixture
