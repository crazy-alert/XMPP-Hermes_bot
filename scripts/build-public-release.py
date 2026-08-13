#!/usr/bin/env python3
"""Build a privacy-audited public release tree from the Git index."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
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


@dataclass(frozen=True)
class VerifiedSource:
    relative: str
    identity: tuple[int, int, int, int]
    data: bytes
    mode: int


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

    for parent in destination.parents:
        if parent.is_symlink():
            raise ReleaseError("destination parent must not be a symlink")
        if parent.exists() and not parent.is_dir():
            raise ReleaseError("destination parent must be a directory")


def _identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)


def _open_verified(path: Path, expected: VerifiedSource) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReleaseError(f"tracked path changed during release build: {expected.relative}") from error
    try:
        path_stat = path.lstat()
        opened_stat = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or not stat.S_ISREG(opened_stat.st_mode)
            or _identity(path_stat) != expected.identity
            or _identity(opened_stat) != expected.identity
        ):
            raise ReleaseError(f"tracked path changed during release build: {expected.relative}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def validate_source_files(source: Path, paths: list[str], denylist: tuple[str, ...]) -> list[VerifiedSource]:
    source = source.resolve()
    verified: list[VerifiedSource] = []
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
            verified.append(VerifiedSource(relative, _identity(file_stat), data, stat.S_IMODE(file_stat.st_mode)))
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
        verified.append(VerifiedSource(relative, _identity(file_stat), data, stat.S_IMODE(file_stat.st_mode)))
    return verified


def copy_release(source: Path, destination: Path, paths: list[VerifiedSource]) -> None:
    destination_existed = destination.exists()
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    try:
        if not destination_existed:
            destination.mkdir()
        for verified in paths:
            source_path = source / verified.relative
            descriptor = _open_verified(source_path, verified)
            with os.fdopen(descriptor, "rb") as source_handle:
                data = source_handle.read()
            if data != verified.data:
                raise ReleaseError(f"tracked path changed during release build: {verified.relative}")

            destination_path = destination / verified.relative
            missing_parents = []
            parent = destination_path.parent
            while parent != destination and not parent.exists():
                missing_parents.append(parent)
                parent = parent.parent
            for directory in reversed(missing_parents):
                directory.mkdir()
                created_dirs.append(directory)
            if parent.is_symlink() or not parent.is_dir():
                raise ReleaseError(f"unsafe destination path: {verified.relative}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                output = os.open(destination_path, flags, verified.mode)
            except OSError as error:
                raise ReleaseError(f"unsafe destination path: {verified.relative}") from error
            created_files.append(destination_path)
            with os.fdopen(output, "wb") as destination_handle:
                destination_handle.write(data)
            os.chmod(destination_path, verified.mode, follow_symlinks=False)
    except BaseException:
        for path in reversed(created_files):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for path in reversed(created_dirs):
            try:
                path.rmdir()
            except (FileNotFoundError, OSError):
                pass
        if not destination_existed:
            try:
                destination.rmdir()
            except (FileNotFoundError, OSError):
                pass
        raise


def build(source: Path, destination: Path, denylist_file: Path) -> None:
    source = source.resolve()
    destination = Path(os.path.abspath(destination))
    if source == destination or source in destination.parents:
        raise ReleaseError("destination must be outside the source worktree")
    validate_destination(destination)
    tracked = git_tracked_paths(source)
    published = classify_paths(tracked)
    denylist = read_denylist(source, Path(os.path.abspath(denylist_file)))
    verified = validate_source_files(source, published, denylist)
    copy_release(source, destination, verified)


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
