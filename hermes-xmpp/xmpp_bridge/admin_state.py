"""Atomic, private configuration storage for XMPP administrative commands."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from urllib.parse import urlparse

from .policy import normalize_bare_jid


class AdminStateError(RuntimeError):
    """A filesystem safety error in the admin configuration store."""


class ConfigValidationError(AdminStateError):
    """An admin configuration did not satisfy the complete schema."""


@dataclass(frozen=True, repr=False)
class AdminConfig:
    owners: frozenset[str]
    trusted_jids: frozenset[str]
    model: str | None
    endpoint: str | None
    token_mask: str | None
    token_present: bool
    revision: int = 0

    def with_changes(
        self,
        *,
        owners: object = None,
        trusted_jids: object = None,
        model: object = None,
        endpoint: object = None,
    ) -> "AdminConfig":
        return AdminConfig(
            self.owners if owners is None else _jids(owners, "owners"),
            self.trusted_jids if trusted_jids is None else _jids(trusted_jids, "trusted_jids"),
            self.model if model is None else _optional_text(model, "model"),
            self.endpoint if endpoint is None else _endpoint(endpoint),
            self.token_mask,
            self.token_present,
            self.revision,
        )


class AdminState:
    """Read and atomically update the versioned service-user admin configuration."""

    def __init__(self, path: Path, first_owner: str) -> None:
        self.path = Path(path)
        self._first_owner = normalize_bare_jid(first_owner)
        self._binding: _ParentBinding | None = None

    def load(self) -> AdminConfig:
        with self._locked():
            if not self.path.exists() and not self.path.is_symlink():
                config, token = _validate(self._first_payload())
                self._write(config, token)
                return config
            config, _ = self._read()
            return config

    def token(self) -> str | None:
        with self._locked():
            if not self.path.exists() and not self.path.is_symlink():
                return None
            _, token = self._read()
            return token

    def mutate(self, transform) -> AdminConfig:
        with self._locked():
            config, token = self._read_or_initial()
            proposed = transform(config)
            if not isinstance(proposed, AdminConfig):
                raise ConfigValidationError("configuration transform must return AdminConfig")
            persisted, secret = _validate(_payload(proposed, token, config.revision + 1))
            self._write(persisted, secret)
            return persisted

    def set_token(self, secret: str) -> AdminConfig:
        if not isinstance(secret, str) or not secret:
            raise ConfigValidationError("token must be nonempty")
        with self._locked():
            config, _ = self._read_or_initial()
            persisted, actual_secret = _validate(_payload(config, secret, config.revision + 1))
            self._write(persisted, actual_secret)
            return persisted

    def _read_or_initial(self) -> tuple[AdminConfig, str | None]:
        if not self.path.exists() and not self.path.is_symlink():
            config, token = _validate(self._first_payload())
            return config, token
        return self._read()

    def _read(self) -> tuple[AdminConfig, str | None]:
        self._check_parent()
        try:
            with _open_regular(self.path, os.O_RDONLY) as source:
                payload = json.loads(source.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigValidationError("invalid admin state") from error
        return _validate(payload)

    def _first_payload(self) -> dict[str, object]:
        return {"version": 1, "revision": 0, "owners": [self._first_owner], "trusted_jids": [], "model": None, "endpoint": None, "token": None}

    def _write(self, config: AdminConfig, token: str | None) -> None:
        parent = self.path.parent
        _ensure_safe_parent(parent)
        self._check_parent()
        _reject_unsafe(self.path, file=True, allow_missing=True)
        contents = json.dumps(_payload(config, token, config.revision), separators=(",", ":"), ensure_ascii=False) + "\n"
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=parent, prefix=f".{self.path.name}.", suffix=".tmp", delete=False) as temporary:
                temporary_name = temporary.name
                if os.name != "nt":
                    os.chmod(temporary_name, 0o600)
                temporary.write(contents)
                temporary.flush()
                os.fsync(temporary.fileno())
            self._check_parent()
            os.replace(temporary_name, self.path)
            temporary_name = None
            self._check_parent()
            if os.name != "nt":
                os.chmod(self.path, 0o600)
                _fsync_dir(parent)
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    @contextmanager
    def _locked(self):
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        _ensure_safe_parent(lock.parent)
        with _ParentBinding(lock.parent) as binding:
            self._binding = binding
            descriptor = _acquire_lock(lock, binding)
            try:
                yield
            finally:
                _release_lock(lock, descriptor, binding)
                self._binding = None

    def _check_parent(self) -> None:
        if self._binding is not None:
            self._binding.check()


def _payload(config: AdminConfig, token: str | None, revision: int) -> dict[str, object]:
    return {"version": 1, "revision": revision, "owners": sorted(config.owners), "trusted_jids": sorted(config.trusted_jids), "model": config.model, "endpoint": config.endpoint, "token": token}


def _validate(payload: object) -> tuple[AdminConfig, str | None]:
    if not isinstance(payload, dict) or set(payload) != {"version", "revision", "owners", "trusted_jids", "model", "endpoint", "token"}:
        raise ConfigValidationError("invalid admin state schema")
    if payload["version"] != 1 or type(payload["revision"]) is not int or payload["revision"] < 0:
        raise ConfigValidationError("unsupported admin state version")
    owners = _jids(payload["owners"], "owners")
    if not owners:
        raise ConfigValidationError("at least one owner is required")
    trusted = _jids(payload["trusted_jids"], "trusted_jids")
    model = _optional_text(payload["model"], "model")
    endpoint = _endpoint(payload["endpoint"])
    token = payload["token"]
    if token is not None and (not isinstance(token, str) or not token):
        raise ConfigValidationError("invalid token")
    return AdminConfig(owners, trusted, model, endpoint, _mask(token), token is not None, payload["revision"]), token


def _jids(value: object, field: str) -> frozenset[str]:
    if isinstance(value, (str, bytes)):
        raise ConfigValidationError(f"invalid {field}")
    try:
        result = frozenset(normalize_bare_jid(item) for item in value)
    except (TypeError, ValueError) as error:
        raise ConfigValidationError(f"invalid {field}") from error
    return result


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not (result := value.strip()) or len(result) > 512:
        raise ConfigValidationError(f"invalid {field}")
    return result


def _endpoint(value: object) -> str | None:
    endpoint = _optional_text(value, "endpoint")
    if endpoint is None:
        return None
    parsed = urlparse(endpoint)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if any(char.isspace() for char in endpoint) or not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)):
        raise ConfigValidationError("endpoint must use HTTPS or loopback HTTP")
    return endpoint


def _mask(token: str | None) -> str | None:
    if token is None:
        return None
    return "***" + token[-4:] if len(token) > 4 else "***"


def _reject_unsafe(path: Path, *, file: bool, allow_missing: bool = False) -> None:
    if not path.exists() and not path.is_symlink():
        if allow_missing:
            return
        return
    if path.is_symlink() or (not path.is_file() if file else not path.is_dir()):
        raise AdminStateError("unsafe admin state path")


@contextmanager
def _open_regular(path: Path, flags: int):
    """Open a regular non-link file and bind the checked identity to its handle."""
    expected = os.lstat(path)
    if not stat.S_ISREG(expected.st_mode):
        raise AdminStateError("unsafe admin state path")
    open_flags = flags
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    descriptor = os.open(path, open_flags)
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino) or not stat.S_ISREG(actual.st_mode):
            raise AdminStateError("admin state path changed while opening")
        yield os.fdopen(descriptor, "rb", closefd=False)
    finally:
        os.close(descriptor)


def _ensure_safe_parent(parent: Path) -> None:
    """Create a private parent only after rejecting every existing symlink ancestor."""
    for ancestor in (parent, *parent.parents):
        if ancestor.exists() or ancestor.is_symlink():
            _reject_unsafe(ancestor, file=False)
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
        if os.name != "nt":
            os.chmod(parent, 0o700)
    _reject_unsafe(parent, file=False)


class _ParentBinding:
    """Bind a checked parent directory across state operations.

    Windows opens the directory with FILE_FLAG_OPEN_REPARSE_POINT, so a junction
    or symlink is rejected instead of traversed. Each name-based operation opens
    the current parent the same way and compares stable file identity to detect a
    concurrent rename/reparse swap. POSIX uses lstat identity plus O_NOFOLLOW on
    child files; the binding is deliberately small so native primitives stay here.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: int | None = None
        self._identity: tuple[int, int, int] | tuple[int, int] | None = None

    def __enter__(self):
        self._handle, self._identity = _open_parent_identity(self.path)
        return self

    def check(self) -> None:
        handle, identity = _open_parent_identity(self.path)
        try:
            if identity != self._identity:
                raise AdminStateError("admin state parent changed during operation")
        finally:
            _close_parent_handle(handle)

    def __exit__(self, *_exc):
        if self._handle is not None:
            _close_parent_handle(self._handle)
            self._handle = None


def _open_parent_identity(path: Path) -> tuple[int | None, tuple[int, int, int] | tuple[int, int]]:
    if os.name != "nt":
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise AdminStateError("unsafe admin state parent")
        return None, (info.st_dev, info.st_ino)
    handle = _win_open_directory(path)
    try:
        return handle, _win_identity(handle)
    except BaseException:
        _close_parent_handle(handle)
        raise


def _close_parent_handle(handle: int | None) -> None:
    if handle is not None and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def _win_open_directory(path: Path) -> int:
    kernel32 = ctypes.windll.kernel32
    create = kernel32.CreateFileW
    create.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    create.restype = ctypes.c_void_p
    handle = create(str(path), 0x80, 0x7, None, 3, 0x02000000 | 0x00200000, None)
    raw = ctypes.cast(handle, ctypes.c_void_p).value
    if raw in (None, ctypes.c_void_p(-1).value):
        raise AdminStateError("unable to safely open admin state parent")
    identity = _win_identity(raw)
    if identity[2] & 0x400:
        _close_parent_handle(raw)
        raise AdminStateError("unsafe admin state reparse parent")
    return raw


def _win_identity(handle: int) -> tuple[int, int, int]:
    class INFO(ctypes.Structure):
        _fields_ = [("attrs", ctypes.c_uint32), ("ctime", ctypes.c_uint64), ("atime", ctypes.c_uint64), ("wtime", ctypes.c_uint64), ("volume", ctypes.c_uint32), ("size_hi", ctypes.c_uint32), ("size_lo", ctypes.c_uint32), ("links", ctypes.c_uint32), ("index_hi", ctypes.c_uint32), ("index_lo", ctypes.c_uint32)]
    info = INFO()
    if not ctypes.windll.kernel32.GetFileInformationByHandle(ctypes.c_void_p(handle), ctypes.byref(info)):
        raise AdminStateError("unable to inspect admin state path")
    if not info.attrs & 0x10:
        raise AdminStateError("admin state parent is not a directory")
    return (info.volume, (info.index_hi << 32) | info.index_lo, info.attrs)


def _fsync_dir(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _acquire_lock(lock: Path, binding: _ParentBinding) -> int:
    """Create an exclusive lockfile; O_EXCL serializes processes on Windows too."""
    deadline = time.monotonic() + 10
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    while True:
        binding.check()
        try:
            descriptor = os.open(lock, flags, 0o600)
        except FileExistsError:
            _reject_unsafe(lock, file=True)
            if time.monotonic() >= deadline:
                raise AdminStateError("admin state lock is busy")
            time.sleep(0.01)
            continue
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise AdminStateError("unsafe admin state lock")
        return descriptor


def _release_lock(lock: Path, descriptor: int, binding: _ParentBinding) -> None:
    try:
        created = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        binding.check()
        current = lock.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
        os.unlink(lock)
