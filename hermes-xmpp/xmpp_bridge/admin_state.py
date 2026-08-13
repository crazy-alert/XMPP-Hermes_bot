"""Atomic, private configuration storage for XMPP administrative commands."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
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
        _reject_unsafe(self.path, file=True)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigValidationError("invalid admin state") from error
        return _validate(payload)

    def _first_payload(self) -> dict[str, object]:
        return {"version": 1, "revision": 0, "owners": [self._first_owner], "trusted_jids": [], "model": None, "endpoint": None, "token": None}

    def _write(self, config: AdminConfig, token: str | None) -> None:
        parent = self.path.parent
        _ensure_safe_parent(parent)
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
            os.replace(temporary_name, self.path)
            temporary_name = None
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
        _reject_unsafe(lock, file=True, allow_missing=True)
        with lock.open("a+b") as handle:
            if os.name != "nt":
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name != "nt":
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def _fsync_dir(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
