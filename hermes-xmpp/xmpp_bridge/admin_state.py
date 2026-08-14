"""Atomic XMPP admin state; persistent operations are supported only on POSIX/Linux.

Windows development hosts may import this module for validation and Win32 ABI
checks, but every persistent ``AdminState`` method fails closed before opening a
configured state path.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import threading
import time
from urllib.parse import urlparse

from .policy import normalize_bare_jid


_PERSISTENT_PLATFORM = os.name != "nt"


class AdminStateError(RuntimeError):
    """A filesystem safety error in the admin configuration store."""


class ConfigValidationError(AdminStateError):
    """An admin configuration did not satisfy the complete schema."""


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


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
        _require_persistent_platform()
        with self._locked():
            if not _child_exists(self.path.name, self._binding):
                config, token = _validate(self._first_payload())
                self._write(config, token)
                return config
            config, _ = self._read()
            return config

    def token(self) -> str | None:
        _require_persistent_platform()
        with self._locked():
            if not _child_exists(self.path.name, self._binding):
                return None
            _, token = self._read()
            return token

    def mutate(self, transform) -> AdminConfig:
        _require_persistent_platform()
        with self._locked():
            config, token = self._read_or_initial()
            proposed = transform(config)
            if not isinstance(proposed, AdminConfig):
                raise ConfigValidationError("configuration transform must return AdminConfig")
            persisted, secret = _validate(_payload(proposed, token, config.revision + 1))
            self._write(persisted, secret)
            return persisted

    def set_token(self, secret: str) -> AdminConfig:
        _require_persistent_platform()
        if not isinstance(secret, str) or not secret:
            raise ConfigValidationError("token must be nonempty")
        with self._locked():
            config, _ = self._read_or_initial()
            persisted, actual_secret = _validate(_payload(config, secret, config.revision + 1))
            self._write(persisted, actual_secret)
            return persisted

    def _read_or_initial(self) -> tuple[AdminConfig, str | None]:
        if not _child_exists(self.path.name, self._binding):
            config, token = _validate(self._first_payload())
            return config, token
        return self._read()

    def _read(self) -> tuple[AdminConfig, str | None]:
        self._check_parent()
        try:
            with _open_regular(self.path.name, os.O_RDONLY, self._binding) as source:
                payload = json.loads(source.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigValidationError("invalid admin state") from error
        return _validate(payload)

    def _first_payload(self) -> dict[str, object]:
        return {"version": 1, "revision": 0, "owners": [self._first_owner], "trusted_jids": [], "model": None, "endpoint": None, "token": None}

    def _write(self, config: AdminConfig, token: str | None) -> None:
        parent = self.path.parent
        self._check_parent()
        if self._binding is None:
            raise AdminStateError("missing admin state parent binding")
        contents = json.dumps(_payload(config, token, config.revision), separators=(",", ":"), ensure_ascii=False) + "\n"
        temporary_name = f".{self.path.name}.{os.urandom(16).hex()}.tmp"
        temporary_fd: int | None = None
        try:
            temporary_fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._binding._dir_fd)
            os.write(temporary_fd, contents.encode("utf-8"))
            os.fchmod(temporary_fd, 0o600)
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            os.replace(temporary_name, self.path.name, src_dir_fd=self._binding._dir_fd, dst_dir_fd=self._binding._dir_fd)
            temporary_name = ""
            _chmod_child(self.path.name, 0o600, self._binding)
            os.fsync(self._binding._dir_fd)
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_name is not None:
                try:
                    if temporary_name:
                        os.unlink(temporary_name, dir_fd=self._binding._dir_fd)
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
def _open_regular(name: str, flags: int, binding: _ParentBinding | None = None):
    """Open a regular non-link file and bind the checked identity to its handle."""
    if binding is None or binding._dir_fd is None:
        raise AdminStateError("missing admin state parent binding")
    open_flags = flags
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    descriptor = os.open(name, open_flags, dir_fd=binding._dir_fd)
    try:
        actual = os.fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode):
            raise AdminStateError("unsafe admin state path")
        yield os.fdopen(descriptor, "rb", closefd=False)
    finally:
        os.close(descriptor)


def _ensure_safe_parent(parent: Path) -> None:
    """Persistent state requires a pre-provisioned, non-link parent directory."""
    if not parent.exists() or parent.is_symlink():
        raise AdminStateError("admin state parent must be a pre-provisioned directory")
    _reject_unsafe(parent, file=False)


def _require_persistent_platform() -> None:
    if not _PERSISTENT_PLATFORM:
        raise AdminStateError("persistent state requires POSIX/Linux")


def _child_exists(name: str, binding: _ParentBinding | None) -> bool:
    if binding is None or binding._dir_fd is None:
        raise AdminStateError("missing admin state parent binding")
    try:
        os.stat(name, dir_fd=binding._dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _chmod_child(name: str, mode: int, binding: _ParentBinding) -> None:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=binding._dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AdminStateError("unsafe admin state path")
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


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
        self._dir_fd: int | None = None
        self._identity: tuple[int, int, int] | tuple[int, int] | None = None

    def __enter__(self):
        self._handle, self._identity = _open_parent_identity(self.path)
        if os.name != "nt":
            flags = os.O_RDONLY | os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            self._dir_fd = os.open(self.path, flags)
            info = os.fstat(self._dir_fd)
            if (info.st_dev, info.st_ino) != self._identity:
                os.close(self._dir_fd)
                self._dir_fd = None
                raise AdminStateError("admin state parent changed while binding")
        return self

    def check(self) -> None:
        handle, identity = _open_parent_identity(self.path)
        try:
            if identity != self._identity:
                raise AdminStateError("admin state parent changed during operation")
        finally:
            _close_parent_handle(handle)

    def __exit__(self, *_exc):
        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None
        if self._handle is not None:
            _close_parent_handle(self._handle)
            self._handle = None

    def child(self, path: Path) -> tuple[str, int | None]:
        if path.parent != self.path:
            raise AdminStateError("state child escapes bound parent")
        return path.name, self._dir_fd


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
    info = _BY_HANDLE_FILE_INFORMATION()
    if not ctypes.windll.kernel32.GetFileInformationByHandle(ctypes.c_void_p(handle), ctypes.byref(info)):
        raise AdminStateError("unable to inspect admin state path")
    if not info.dwFileAttributes & 0x10:
        raise AdminStateError("admin state parent is not a directory")
    return (info.dwVolumeSerialNumber, (info.nFileIndexHigh << 32) | info.nFileIndexLow, info.dwFileAttributes)


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
        # A second lookup catches a deterministic swap immediately after a seam
        # check before any name-based operation is attempted.
        binding.check()
        try:
            descriptor = os.open(lock.name, flags, 0o600, dir_fd=binding._dir_fd)
        except FileExistsError:
            if _recover_dead_lock(lock, binding):
                continue
            if time.monotonic() >= deadline:
                raise AdminStateError("admin state lock is busy")
            time.sleep(0.01)
            continue
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise AdminStateError("unsafe admin state lock")
        metadata = json.dumps({"pid": os.getpid(), "nonce": os.urandom(16).hex()}, separators=(",", ":")) + "\n"
        os.write(descriptor, metadata.encode("utf-8"))
        os.fsync(descriptor)
        return descriptor


def _release_lock(lock: Path, descriptor: int, binding: _ParentBinding) -> None:
    try:
        created = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        binding.check()
        binding.check()
        current = os.stat(lock.name, dir_fd=binding._dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
        os.unlink(lock.name, dir_fd=binding._dir_fd)


def _recover_dead_lock(lock: Path, binding: _ParentBinding) -> bool:
    """Remove only a definitely dead, unchanged lock file; unknown locks stay live."""
    try:
        original = os.stat(lock.name, dir_fd=binding._dir_fd, follow_symlinks=False)
        with _open_regular(lock.name, os.O_RDONLY, binding) as source:
            data = json.loads(source.read().decode("utf-8"))
        pid = data.get("pid") if isinstance(data, dict) else None
        if type(pid) is not int or pid <= 0 or _pid_exists(pid):
            return False
        binding.check()
        binding.check()
        current = os.stat(lock.name, dir_fd=binding._dir_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            return False
        os.unlink(lock.name, dir_fd=binding._dir_fd)
        return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _pid_exists(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
