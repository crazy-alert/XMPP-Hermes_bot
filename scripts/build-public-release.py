#!/usr/bin/env python3
"""Build a privacy-audited public release tree from the Git index."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys


PUBLIC_FILES = frozenset(
    {
        ".gitattributes",
        ".gitignore",
        "AGENTS.md",
        "DESIGN.md",
        "IMPLEMENTATION_PLAN.md",
        "README.md",
        "installer.sh",
        "deploy/hermes-gateway.service",
        "deploy/hermes.env.example",
        "deploy/install-on-ubuntu.sh",
        "hermes-xmpp/adapter.py",
        "hermes-xmpp/plugin.yaml",
        "hermes-xmpp/xmpp_bridge/__init__.py",
        "hermes-xmpp/xmpp_bridge/admin_state.py",
        "hermes-xmpp/xmpp_bridge/client.py",
        "hermes-xmpp/xmpp_bridge/models.py",
        "hermes-xmpp/xmpp_bridge/policy.py",
        "hermes-xmpp/xmpp_bridge/state.py",
        "scripts/build-public-release.py",
        "tests/test_adapter.py",
        "tests/test_admin_state.py",
        "tests/test_client_events.py",
        "tests/test_deploy_assets.py",
        "tests/test_plugin_manifest.py",
        "tests/test_policy.py",
        "tests/test_public_release.py",
        "tests/test_state.py",
    }
)
EXCLUDED_DEVELOPMENT_DIRECTORIES = (".superpowers/", "docs/superpowers/")
FORBIDDEN_PATH_PARTS = frozenset({".git", ".pytest_cache", "__pycache__"})
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", "-report.md")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?im)^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|API_KEY)[A-Z0-9_]*\s*=\s*[^\s#]+"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxoxb-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),
)


class ReleaseError(Exception):
    pass


def git_tracked_paths(source: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=source,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ReleaseError("source must be a Git worktree")
    return [raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]


def classify_paths(paths: list[str]) -> list[str]:
    published: list[str] = []
    unexpected: list[str] = []
    for relative in paths:
        normalized = Path(relative).as_posix()
        parts = Path(normalized).parts
        if normalized.startswith(EXCLUDED_DEVELOPMENT_DIRECTORIES):
            continue
        if any(part in FORBIDDEN_PATH_PARTS for part in parts) or normalized.endswith(FORBIDDEN_SUFFIXES):
            raise ReleaseError(f"forbidden tracked path: {normalized}")
        if normalized in PUBLIC_FILES:
            published.append(normalized)
        else:
            unexpected.append(normalized)
    if unexpected:
        raise ReleaseError("unexpected tracked path: " + ", ".join(sorted(unexpected)))
    return sorted(published)


def read_denylist(source: Path, path: Path) -> tuple[str, ...]:
    if not path.is_file() or path.is_symlink():
        raise ReleaseError("denylist file must be an external regular file")
    if source == path or source in path.parents:
        relative = path.relative_to(source).as_posix()
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", relative],
            cwd=source,
            check=False,
        )
        if ignored.returncode != 0:
            raise ReleaseError("repository-local denylist file must be ignored")
    values = tuple(
        dict.fromkeys(
            line.strip().casefold()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    )
    if not values:
        raise ReleaseError("denylist file must contain at least one identifier")
    return values


def validate_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise ReleaseError("destination must not be a symlink")
    if destination.exists():
        if not destination.is_dir():
            raise ReleaseError("destination must be a directory")
        if any(destination.iterdir()):
            raise ReleaseError("destination must be new or empty")


def validate_source_files(source: Path, paths: list[str], denylist: tuple[str, ...]) -> None:
    source = source.resolve()
    for relative in paths:
        path = source / relative
        try:
            file_stat = path.lstat()
        except FileNotFoundError as error:
            raise ReleaseError(f"tracked path is missing: {relative}") from error
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise ReleaseError(f"tracked path is not a regular file: {relative}")
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReleaseError(f"tracked text is not UTF-8: {relative}") from error
        folded = text.casefold()
        if any(identifier in folded for identifier in denylist):
            raise ReleaseError(f"private identifier found in: {relative}")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise ReleaseError(f"possible secret found in: {relative}")


def copy_release(source: Path, destination: Path, paths: list[str]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for relative in paths:
        source_path = source / relative
        destination_path = destination / relative
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path, follow_symlinks=False)
        shutil.copymode(source_path, destination_path, follow_symlinks=False)


def build(source: Path, destination: Path, denylist_file: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or source in destination.parents:
        raise ReleaseError("destination must be outside the source worktree")
    validate_destination(destination)
    tracked = git_tracked_paths(source)
    published = classify_paths(tracked)
    denylist = read_denylist(source, denylist_file.resolve())
    validate_source_files(source, published, denylist)
    copy_release(source, destination, published)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--denylist-file", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    denylist_file = args.denylist_file
    if denylist_file is None:
        configured = os.environ.get("HERMES_PUBLIC_RELEASE_DENYLIST_FILE", "").strip()
        if not configured:
            print("release audit requires a nonempty external denylist file", file=sys.stderr)
            return 2
        denylist_file = Path(configured)
    try:
        build(args.source, args.destination, denylist_file)
    except (OSError, ReleaseError, UnicodeError) as error:
        print(f"public release build failed: {error}", file=sys.stderr)
        return 1
    print(f"public release staged at {args.destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
