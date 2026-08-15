#!/usr/bin/env python3
"""Build a privacy-audited public release tree from the Git index."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
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
        "deploy/hermes-reboot-helper.sh",
        "deploy/hermes-reboot-helper.service",
        "deploy/hermes-reboot-helper.path",
        "hermes-xmpp/adapter.py",
        "hermes-xmpp/plugin.yaml",
        "hermes-xmpp/xmpp_bridge/__init__.py",
        "hermes-xmpp/xmpp_bridge/admin_state.py",
        "hermes-xmpp/xmpp_bridge/client.py",
        "hermes-xmpp/xmpp_bridge/commands.py",
        "hermes-xmpp/xmpp_bridge/hermes_config.py",
        "hermes-xmpp/xmpp_bridge/models.py",
        "hermes-xmpp/xmpp_bridge/omemo.py",
        "hermes-xmpp/xmpp_bridge/policy.py",
        "hermes-xmpp/xmpp_bridge/reboot.py",
        "hermes-xmpp/xmpp_bridge/state.py",
        "hermes-xmpp/xmpp_bridge/updates.py",
        "hermes-xmpp/xmpp_image_gen/__init__.py",
        "scripts/build-public-release.py",
        "tests/test_adapter.py",
        "tests/test_admin_state.py",
        "tests/test_client_events.py",
        "tests/test_commands.py",
        "tests/test_deploy_assets.py",
        "tests/test_hermes_config.py",
        "tests/test_plugin_manifest.py",
        "tests/test_policy.py",
        "tests/test_public_release.py",
        "tests/test_reboot.py",
        "tests/test_state.py",
        "tests/test_updates.py",
        "tests/test_xmpp_image_gen.py",
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


@dataclass
class BoundDestination:
    path: Path
    identity: tuple[int, int, int, int]
    descriptor: int | None
    parent_path: Path
    parent_identity: tuple[int, int]
    parent_descriptor: int | None
    created_files: list[Path]
    created_dirs: list[Path]


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
    if destination.is_symlink() or _is_reparse(destination.lstat() if destination.exists() or destination.is_symlink() else None):
        raise ReleaseError("destination must not be a symlink")
    if destination.exists():
        raise ReleaseError("destination must not already exist")

    for parent in destination.parents:
        if parent.is_symlink() or _is_reparse(parent.lstat() if parent.exists() or parent.is_symlink() else None):
            raise ReleaseError("destination parent must not be a symlink")
        if parent.exists() and not parent.is_dir():
            raise ReleaseError("destination parent must be a directory")


def _identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)


def _stable_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return (file_stat.st_dev, file_stat.st_ino)


def _create_staging(destination: Path) -> BoundDestination:
    parent = destination.parent
    parent_stat = parent.lstat()
    parent_descriptor: int | None = None
    if os.name != "nt":
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        if _stable_identity(os.fstat(parent_descriptor)) != _stable_identity(parent_stat):
            os.close(parent_descriptor)
            raise ReleaseError("destination parent changed during release build")
    for _ in range(32):
        staging = parent / f".{destination.name}.staging-{secrets.token_hex(16)}"
        try:
            if parent_descriptor is not None:
                os.mkdir(staging.name, 0o700, dir_fd=parent_descriptor)
            else:
                os.mkdir(staging, 0o700)
        except FileExistsError:
            continue
        except OSError as error:
            if parent_descriptor is not None:
                os.close(parent_descriptor)
            raise ReleaseError("could not create private release staging directory") from error
        return _bind_existing_staging(staging, parent, parent_stat, parent_descriptor)
    if parent_descriptor is not None:
        os.close(parent_descriptor)
    raise ReleaseError("could not allocate private release staging directory")


def _bind_existing_staging(
    staging: Path,
    parent: Path,
    parent_stat: os.stat_result,
    parent_descriptor: int | None,
) -> BoundDestination:
    descriptor: int | None = None
    try:
        file_stat = staging.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or _is_reparse(file_stat) or not stat.S_ISDIR(file_stat.st_mode):
            raise ReleaseError("release staging is not a private directory")
        if os.name != "nt":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(staging, flags)
            if _stable_identity(os.fstat(descriptor)) != _stable_identity(file_stat):
                raise ReleaseError("release staging changed while being created")
        return BoundDestination(
            staging,
            _identity(file_stat),
            descriptor,
            parent,
            _stable_identity(parent_stat),
            parent_descriptor,
            [],
            [],
        )
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        try:
            staging.rmdir()
        except OSError:
            pass
        raise


def _check_bound(destination: BoundDestination) -> None:
    try:
        parent_stat = destination.parent_path.lstat()
    except OSError as error:
        raise ReleaseError("destination parent changed during release build") from error
    if (
        stat.S_ISLNK(parent_stat.st_mode)
        or _is_reparse(parent_stat)
        or not stat.S_ISDIR(parent_stat.st_mode)
        or _stable_identity(parent_stat) != destination.parent_identity
    ):
        raise ReleaseError("destination parent changed during release build")
    if destination.parent_descriptor is not None:
        if _stable_identity(os.fstat(destination.parent_descriptor)) != destination.parent_identity:
            raise ReleaseError("destination parent binding changed during release build")
    try:
        file_stat = destination.path.lstat()
    except OSError as error:
        raise ReleaseError("destination changed during release build") from error
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or _is_reparse(file_stat)
        or not stat.S_ISDIR(file_stat.st_mode)
        or _stable_identity(file_stat) != _stable_identity_from_tuple(destination.identity)
    ):
        raise ReleaseError("destination changed during release build")
    if destination.descriptor is not None:
        opened_stat = os.fstat(destination.descriptor)
        if _stable_identity(opened_stat) != _stable_identity_from_tuple(destination.identity):
            raise ReleaseError("destination binding changed during release build")


def _stable_identity_from_tuple(identity: tuple[int, int, int, int]) -> tuple[int, int]:
    return identity[0], identity[1]


def _is_reparse(file_stat: os.stat_result | None) -> bool:
    if file_stat is None:
        return False
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


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


def copy_release(source: Path, destination: BoundDestination, paths: list[VerifiedSource]) -> None:
    try:
        for verified in paths:
            source_path = source / verified.relative
            descriptor = _open_verified(source_path, verified)
            with os.fdopen(descriptor, "rb") as source_handle:
                data = source_handle.read()
            if data != verified.data:
                raise ReleaseError(f"tracked path changed during release build: {verified.relative}")

            _check_bound(destination)
            destination_path = destination.path / verified.relative
            missing_parents = []
            parent = destination_path.parent
            while parent != destination.path and not parent.exists():
                missing_parents.append(parent)
                parent = parent.parent
            for directory in reversed(missing_parents):
                directory.mkdir()
                destination.created_dirs.append(directory)
                _check_bound(destination)
            if parent.is_symlink() or not parent.is_dir():
                raise ReleaseError(f"unsafe destination path: {verified.relative}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                if destination.descriptor is not None:
                    output = _open_destination_relative(destination, verified.relative, flags, verified.mode)
                else:
                    _check_bound(destination)
                    output = os.open(destination_path, flags, verified.mode)
                    _check_bound(destination)
            except OSError as error:
                raise ReleaseError(f"unsafe destination path: {verified.relative}") from error
            destination.created_files.append(destination_path)
            with os.fdopen(output, "wb") as destination_handle:
                destination_handle.write(data)
            os.chmod(destination_path, verified.mode, follow_symlinks=False)
            _check_bound(destination)
    except OSError as error:
        raise ReleaseError("destination changed during release build") from error


def _open_destination_relative(destination: BoundDestination, relative: str, flags: int, mode: int) -> int:
    assert destination.descriptor is not None
    current = os.dup(destination.descriptor)
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            os.close(current)
            current = next_descriptor
        return os.open(parts[-1], flags, mode, dir_fd=current)
    finally:
        os.close(current)


def _audit_destination(destination: BoundDestination, expected: list[VerifiedSource]) -> None:
    _check_bound(destination)
    actual = sorted(
        path.relative_to(destination.path).as_posix()
        for path in destination.path.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    wanted = sorted(item.relative for item in expected)
    if actual != wanted or any(path.is_symlink() for path in destination.path.rglob("*")):
        raise ReleaseError("destination tree changed during release build")
    _check_bound(destination)


def _cleanup_destination(destination: BoundDestination) -> None:
    """Never delete after failure: a hostile rename could redirect path cleanup."""
    return


def _publish_destination(destination: BoundDestination, final_path: Path) -> None:
    _check_bound(destination)
    if final_path.exists() or final_path.is_symlink():
        raise ReleaseError("destination appeared during release build")
    try:
        if sys.platform.startswith("linux"):
            if destination.parent_descriptor is None:
                raise ReleaseError("destination parent is not bound")
            libc = ctypes.CDLL(None, use_errno=True)
            try:
                renameat2 = libc.renameat2
            except AttributeError as error:
                raise ReleaseError("secure no-replace publication is unavailable on this Linux") from error
            renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            renameat2.restype = ctypes.c_int
            result = renameat2(
                destination.parent_descriptor,
                os.fsencode(destination.path.name),
                destination.parent_descriptor,
                os.fsencode(final_path.name),
                1,  # RENAME_NOREPLACE
            )
            if result != 0:
                raise OSError(ctypes.get_errno(), "renameat2(RENAME_NOREPLACE) failed")
        else:
            destination.path.rename(final_path)
    except OSError as error:
        raise ReleaseError("destination appeared during release build") from error
    destination.path = final_path
    _check_bound(destination)


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
    bound = _create_staging(destination)
    try:
        copy_release(source, bound, verified)
        _audit_destination(bound, verified)
        _publish_destination(bound, destination)
    except BaseException as error:
        _cleanup_destination(bound)
        if isinstance(error, ReleaseError):
            raise ReleaseError(f"{error}; private staging retained at: {bound.path}") from error
        raise
    finally:
        if bound.descriptor is not None:
            os.close(bound.descriptor)
        if bound.parent_descriptor is not None:
            os.close(bound.parent_descriptor)


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
