from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostRebootRequest:
    owner_bare_jid: str
    confirmed_at: float


@dataclass(frozen=True)
class RebootResult:
    reply: str = field(repr=False)
    event: HostRebootRequest | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(reply=<redacted>, event={self.event!r})"


@dataclass(frozen=True)
class RebootStatus:
    pending: bool
    pending_owner: str | None
    expires_in_seconds: int | None
    cooldown_remaining_seconds: int


@dataclass(frozen=True, repr=False)
class _Pending:
    owner: str
    code: str = field(repr=False)
    expires_at: float


def _secure_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


class RebootConfirmationState:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        code_generator: Callable[[], str] = _secure_code,
        ttl_seconds: float = 60,
        cooldown_seconds: float = 300,
    ) -> None:
        if ttl_seconds <= 0 or cooldown_seconds < 0:
            raise ValueError("reboot timing values must be non-negative")
        self._clock = clock
        self._code_generator = code_generator
        self._ttl_seconds = ttl_seconds
        self._cooldown_seconds = cooldown_seconds
        self._pending: _Pending | None = None
        self._cooldown_until = 0.0

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status()!r})"

    @staticmethod
    def _authorized(is_dm: bool, is_owner: bool) -> bool:
        return is_dm and is_owner

    def _expire(self, now: float) -> None:
        if self._pending is not None and now >= self._pending.expires_at:
            self._pending = None

    def request(self, owner_bare_jid: str, *, is_dm: bool, is_owner: bool) -> RebootResult:
        now = self._clock()
        self._expire(now)
        if not self._authorized(is_dm, is_owner):
            return RebootResult("Команда недоступна.")
        owner = owner_bare_jid.strip().lower()
        if now < self._cooldown_until:
            return RebootResult("Host reboot cooldown активен.")
        if self._pending is not None and self._pending.owner != owner:
            return RebootResult("Host reboot busy: ожидается другое подтверждение.")
        code = self._code_generator()
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            raise ValueError("confirmation generator must return six ASCII digits")
        self._pending = _Pending(owner, code, now + self._ttl_seconds)
        return RebootResult(f"Подтвердите перезагрузку: /confirm reboot {code} в течение 60 секунд.")

    def confirm(
        self,
        owner_bare_jid: str,
        code: object,
        *,
        is_dm: bool,
        is_owner: bool,
    ) -> RebootResult:
        now = self._clock()
        self._expire(now)
        if not self._authorized(is_dm, is_owner):
            return RebootResult("Подтверждение отклонено.")
        owner = owner_bare_jid.strip().lower()
        pending = self._pending
        code_is_valid = (
            isinstance(code, str)
            and len(code) == 6
            and all("0" <= character <= "9" for character in code)
        )
        if pending is None or pending.owner != owner or not code_is_valid or not secrets.compare_digest(pending.code, code):
            return RebootResult("Подтверждение недействительно или истекло.")
        self._pending = None
        self._cooldown_until = now + self._cooldown_seconds
        return RebootResult("Перезагрузка хоста подтверждена.", HostRebootRequest(owner, now))

    def cancel(self, owner_bare_jid: str, *, is_dm: bool, is_owner: bool) -> RebootResult:
        now = self._clock()
        self._expire(now)
        if not self._authorized(is_dm, is_owner):
            return RebootResult("Отмена отклонена.")
        owner = owner_bare_jid.strip().lower()
        if self._pending is None or self._pending.owner != owner:
            return RebootResult("Ожидающее подтверждение не найдено.")
        self._pending = None
        return RebootResult("Перезагрузка отменена.")

    def status(self) -> RebootStatus:
        now = self._clock()
        self._expire(now)
        pending = self._pending
        return RebootStatus(
            pending=pending is not None,
            pending_owner=pending.owner if pending is not None else None,
            expires_in_seconds=max(0, math.ceil(pending.expires_at - now)) if pending is not None else None,
            cooldown_remaining_seconds=max(0, math.ceil(self._cooldown_until - now)),
        )
