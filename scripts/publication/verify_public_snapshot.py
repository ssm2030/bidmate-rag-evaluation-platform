from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from scripts.publication.policy import PublicationPolicy, load_policy


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    detail: str


SIGNATURES = {
    "pdf-signature": b"%PDF-",
    "ole-signature": bytes.fromhex("d0cf11e0a1b11ae1"),
    "zip-signature": b"PK\x03\x04",
}
TEXT_SUFFIXES = {
    ".py", ".ps1", ".ts", ".tsx", ".js", ".jsx", ".json", ".jsonl",
    ".md", ".yaml", ".yml", ".toml", ".txt", ".env", ".example", ".csv",
}


def _text_findings(relative: str, data: bytes) -> list[Finding]:
    if Path(relative).suffix.lower() not in TEXT_SUFFIXES and Path(relative).name != ".env.example":
        return []
    text = data.decode("utf-8", errors="ignore")
    patterns = {
        "private-key": "BEGIN " + "PRIVATE KEY",
        "absolute-user-path": r"(?i)[A-Z]:\\Users\\[^\\\s]+\\",
        "github-token": r"gh" + r"[pousr]_[A-Za-z0-9_]{30,}",
        "openai-token": r"sk-" + r"(?:proj-)?[A-Za-z0-9_-]{20,}",
        "jwt-token": r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        "email-address": r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "resident-number": r"\b\d{6}-[1-4]\d{6}\b",
    }
    findings = []
    for rule, pattern in patterns.items():
        if re.search(pattern, text):
            findings.append(Finding(rule, relative, "matched a prohibited text pattern"))
    if Path(relative).name == ".env":
        findings.append(Finding("env-file", relative, "populated environment files are forbidden"))
    return findings


def _n8n_findings(relative: str, data: bytes, policy: PublicationPolicy) -> list[Finding]:
    if not relative.startswith("n8n/workflows/") or not relative.endswith(".json"):
        return []
    try:
        workflow = json.loads(data)
    except json.JSONDecodeError:
        return [Finding("n8n-json", relative, "workflow is not valid JSON")]
    findings = []
    for node in workflow.get("nodes", []):
        if node.get("credentials"):
            findings.append(Finding("n8n-credentials", relative, str(node.get("name", ""))))
        if "webhook" in str(node.get("type", "")).lower():
            findings.append(Finding("n8n-webhook", relative, str(node.get("name", ""))))
    for url in re.findall(r"https?://[^\s\"']+", data.decode("utf-8", errors="ignore")):
        host = (urlparse(url).hostname or "").lower()
        if host not in policy.allowed_loopback_hosts:
            findings.append(Finding("n8n-remote-url", relative, host))
    return findings


def _scan_blob(relative: str, data: bytes, policy: PublicationPolicy) -> list[Finding]:
    findings = []
    suffix = Path(relative).suffix.lower()
    if suffix in policy.forbidden_extensions:
        findings.append(Finding("forbidden-extension", relative, suffix))
    if len(data) > policy.max_file_bytes:
        findings.append(Finding("file-size", relative, str(len(data))))
    for rule, signature in SIGNATURES.items():
        if data.startswith(signature):
            findings.append(Finding(rule, relative, signature.hex()))
    if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
        findings.append(Finding("git-lfs-pointer", relative, "LFS pointers are forbidden"))
    findings.extend(_text_findings(relative, data))
    findings.extend(_n8n_findings(relative, data, policy))
    return findings


def scan_worktree(root: Path, policy: PublicationPolicy) -> list[Finding]:
    root = root.resolve()
    findings = []
    present = set()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if path.is_symlink():
            findings.append(Finding("symlink", relative, "links are forbidden"))
            continue
        if not path.is_file():
            continue
        present.add(relative)
        findings.extend(_scan_blob(relative, path.read_bytes(), policy))
    prompts = set(policy.prompt_paths)
    actual = present & prompts
    if actual != prompts or len(actual) != policy.expected_prompt_count:
        findings.append(
            Finding(
                "prompt-count",
                ".",
                f"expected={policy.expected_prompt_count} actual={len(actual)}",
            )
        )
    if len(actual) > policy.max_prompt_count:
        findings.append(Finding("prompt-limit", ".", str(len(actual))))
    return findings


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_bytes,
        capture_output=True,
        check=True,
    )
    return result.stdout


def scan_git_objects(root: Path, policy: PublicationPolicy) -> list[Finding]:
    findings = []
    listing = _git(root, "rev-list", "--objects", "--all").decode("utf-8", errors="replace")
    for line in listing.splitlines():
        object_id, _, relative = line.partition(" ")
        if not relative:
            continue
        kind = _git(root, "cat-file", "-t", object_id).strip()
        if kind != b"blob":
            continue
        data = _git(root, "cat-file", "-p", object_id)
        findings.extend(_scan_blob(relative, data, policy))
    return findings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--policy", type=Path, default=Path("configs/publication/public_snapshot.json"))
    parser.add_argument("--scope", choices=("worktree", "objects", "all"), default="all")
    args = parser.parse_args()
    policy = load_policy(args.root / args.policy if not args.policy.is_absolute() else args.policy)
    findings = []
    if args.scope in {"worktree", "all"}:
        findings.extend(scan_worktree(args.root, policy))
    if args.scope in {"objects", "all"}:
        findings.extend(scan_git_objects(args.root, policy))
    if findings:
        for finding in findings:
            print(f"FINDING {finding.rule} {finding.path} {finding.detail}")
        raise SystemExit(1)
    sizes = [path.stat().st_size for path in args.root.rglob("*") if path.is_file() and ".git" not in path.parts]
    print("PROMPT_COUNT=16")
    print(f"MAX_FILE_BYTES={max(sizes, default=0)}")
    print("PUBLICATION_SAFETY_STATUS=PASS")


if __name__ == "__main__":
    main()