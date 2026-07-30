from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
from pathlib import Path

from scripts.publication.policy import PublicationPolicy, load_policy


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _excluded(relative: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(relative, pattern)
        or fnmatch.fnmatchcase("/" + relative, "*/" + pattern)
        for pattern in patterns
    )


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"source link is forbidden: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _sanitize_package_lock(destination: Path) -> None:
    package_path = destination / "package.json"
    lock_path = destination / "package-lock.json"
    if not package_path.is_file() or not lock_path.is_file():
        return
    package = json.loads(package_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    name = str(package["name"])
    version = str(package["version"])
    lock["name"] = name
    lock["version"] = version
    root_package = lock.setdefault("packages", {}).setdefault("", {})
    root_package["name"] = name
    root_package["version"] = version
    root_package["dependencies"] = package.get("dependencies", {})
    root_package.pop("license", None)
    lock_path.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _manifest(destination: Path) -> dict:
    rows = []
    for path in sorted(destination.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name == "PUBLIC_SNAPSHOT_MANIFEST.json":
            continue
        relative = _relative(path, destination)
        data = path.read_bytes()
        rows.append(
            {
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {
        "schema_version": 1,
        "file_count": len(rows),
        "total_bytes": sum(row["bytes"] for row in rows),
        "files": rows,
    }


def build_snapshot(
    source_root: Path,
    confidential_root: Path,
    destination: Path,
    policy: PublicationPolicy,
) -> dict:
    source_root = source_root.resolve()
    confidential_root = confidential_root.resolve()
    destination = destination.resolve()
    if destination == source_root or destination.is_relative_to(source_root):
        raise ValueError("destination must be outside the source")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)

    for root_name in policy.include_roots:
        root = source_root / root_name
        if not root.exists():
            continue
        if root.is_symlink():
            raise ValueError(f"source link is forbidden: {root}")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"source link is forbidden: {path}")
            if not path.is_file():
                continue
            relative = _relative(path, source_root)
            if not _excluded(relative, policy.exclude_globs):
                _copy_file(path, destination / relative)

    for relative in policy.include_files:
        source = source_root / relative
        if source.is_file() and not _excluded(relative, policy.exclude_globs):
            _copy_file(source, destination / relative)

    for name, target in policy.external_markdown.items():
        source = confidential_root / name
        if not source.is_file():
            raise FileNotFoundError(f"approved external Markdown is missing: {name}")
        _copy_file(source, destination / target)

    template_root = source_root / "scripts" / "publication" / "templates"
    template_map = {
        "README.public.md": "README.md",
        "SECURITY.public.md": "SECURITY.md",
        "gitignore.public": ".gitignore",
        "package.public.json": "package.json",
        "architecture.public.md": "docs/architecture.md",
        "evaluation-workflow.public.md": "docs/evaluation-workflow.md",
        "local-data.public.md": "docs/local-data.md",
        "PULL_REQUEST_BODY.public.md": ".github/PULL_REQUEST_BODY.md",
    }
    for template_name, target in template_map.items():
        template = template_root / template_name
        if template.is_file():
            _copy_file(template, destination / target)

    _sanitize_package_lock(destination)
    manifest = _manifest(destination)
    (destination / "PUBLIC_SNAPSHOT_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--confidential-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_snapshot(
        args.source_root,
        args.confidential_root,
        args.destination,
        load_policy(args.policy),
    )
    print(f"SNAPSHOT_FILE_COUNT={manifest['file_count']}")
    print(f"SNAPSHOT_TOTAL_BYTES={manifest['total_bytes']}")
    print("SNAPSHOT_STATUS=PASS")


if __name__ == "__main__":
    main()