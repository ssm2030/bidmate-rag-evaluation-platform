from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PublicationPolicy:
    include_roots: tuple[str, ...]
    include_files: tuple[str, ...]
    exclude_globs: tuple[str, ...]
    external_markdown: dict[str, str]
    prompt_paths: tuple[str, ...]
    expected_prompt_count: int
    max_prompt_count: int
    max_file_bytes: int
    forbidden_extensions: frozenset[str]
    allowed_loopback_hosts: frozenset[str]


def load_policy(path: Path) -> PublicationPolicy:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported publication policy schema")
    policy = PublicationPolicy(
        include_roots=tuple(payload["include_roots"]),
        include_files=tuple(payload["include_files"]),
        exclude_globs=tuple(payload["exclude_globs"]),
        external_markdown=dict(payload["external_markdown"]),
        prompt_paths=tuple(payload["prompt_paths"]),
        expected_prompt_count=int(payload["expected_prompt_count"]),
        max_prompt_count=int(payload["max_prompt_count"]),
        max_file_bytes=int(payload["max_file_bytes"]),
        forbidden_extensions=frozenset(str(value).lower() for value in payload["forbidden_extensions"]),
        allowed_loopback_hosts=frozenset(payload["allowed_loopback_hosts"]),
    )
    if len(policy.prompt_paths) != policy.expected_prompt_count:
        raise ValueError("prompt inventory count does not match expected_prompt_count")
    if len(set(policy.prompt_paths)) != len(policy.prompt_paths):
        raise ValueError("prompt inventory contains duplicate paths")
    if policy.expected_prompt_count > policy.max_prompt_count:
        raise ValueError("prompt inventory exceeds max_prompt_count")
    return policy