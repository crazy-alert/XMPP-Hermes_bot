"""Owner-only XMPP DM command router; it never invokes a shell or echoes tokens."""

from __future__ import annotations

from dataclasses import dataclass
import time

from .admin_state import AdminStateError, ConfigValidationError
from .models import InboundXmppMessage
from .policy import normalize_bare_jid


MAX_BODY_LENGTH = 4096
MAX_JID_LENGTH = 512
MAX_MODEL_LENGTH = 512
MAX_ENDPOINT_LENGTH = 512
MAX_TOKEN_LENGTH = 4096


@dataclass(frozen=True)
class RestartGateway:
    """Typed supervisor request; this router never invokes a shell."""


@dataclass(frozen=True, repr=False)
class CommandResult:
    handled: bool
    reply: str | None = None
    control_event: object | None = None


@dataclass(frozen=True)
class _PendingToggle:
    jid: str
    add: bool
    revision: int
    present: bool
    expires_at: float


class _StaleToggle(Exception):
    """The state snapshot changed while a confirmation waited for its owner."""


class CommandRouter:
    def __init__(self, state, *, monotonic=time.monotonic, doctor=None) -> None:
        self.state = state
        self._clock = monotonic
        self._doctor = doctor
        self._pending: dict[str, _PendingToggle] = {}

    def handle(self, message: InboundXmppMessage) -> CommandResult:
        if not isinstance(message, InboundXmppMessage) or message.is_group or not isinstance(message.body, str):
            return CommandResult(False)
        try:
            sender = normalize_bare_jid(message.sender_jid)
            config = self.state.load()
        except (AdminStateError, ConfigValidationError, ValueError, OSError):
            return CommandResult(False)
        body = message.body.strip()
        if sender not in config.owners | config.trusted_jids:
            return CommandResult(False)
        if len(body) > MAX_BODY_LENGTH:
            return CommandResult(True, "Команда слишком длинная.") if sender in config.owners else CommandResult(False)
        if body.casefold() == "ping":
            return CommandResult(True, "pong")
        if sender not in config.owners:
            return CommandResult(False)

        pending = self._pending.get(sender)
        if pending is not None:
            # A whole, strict bare JID starts a new request instead of being a
            # confirmation/cancellation reply.  Anything else reaches _confirm.
            try:
                jid = self._exact_bare_jid(body)
            except ValueError:
                return self._confirm(sender, body, pending)
            return self._start_toggle(sender, jid, config)
        if not body.startswith("/"):
            return self._start_toggle(sender, body, config)
        return self._command(body, config)

    @staticmethod
    def _exact_bare_jid(text: str) -> str:
        """Accept a complete canonical bare JID, never a resource or sentence."""
        if not isinstance(text, str) or not text or len(text) > MAX_JID_LENGTH:
            raise ValueError("invalid JID")
        if "/" in text or any(char.isspace() or ord(char) < 32 for char in text):
            raise ValueError("JID must be a single bare identifier")
        normalized = normalize_bare_jid(text)
        if normalized != text.casefold():
            raise ValueError("JID normalization is not whole-input canonical")
        return normalized

    def _confirm(self, sender: str, body: str, pending: _PendingToggle) -> CommandResult:
        del self._pending[sender]
        if self._clock() >= pending.expires_at:
            return CommandResult(False)
        if body.casefold() not in {"да", "yes", "y"}:
            return CommandResult(True, "Операция отменена.")

        def transform(value):
            present = pending.jid in value.trusted_jids
            if value.revision != pending.revision or present != pending.present:
                raise _StaleToggle
            trusted = value.trusted_jids | {pending.jid} if pending.add else value.trusted_jids - {pending.jid}
            return value.with_changes(trusted_jids=trusted)

        try:
            self.state.mutate(transform)
        except _StaleToggle:
            return CommandResult(True, "Запрос устарел; повторите команду.")
        except (AdminStateError, ConfigValidationError, ValueError, OSError):
            return CommandResult(True, "Команда не выполнена.")
        return CommandResult(True, "Trusted JID обновлён.")

    def _start_toggle(self, sender: str, body: str, config) -> CommandResult:
        try:
            jid = self._exact_bare_jid(body)
        except ValueError:
            return CommandResult(False)
        present = jid in config.trusted_jids
        self._pending[sender] = _PendingToggle(jid, not present, config.revision, present, self._clock() + 60)
        action = "добавить" if not present else "удалить"
        return CommandResult(True, f"Подтвердите: {action} {jid}? Ответьте да/yes/y.")

    def _command(self, body: str, config) -> CommandResult:
        parts = body.split(None, 2)
        command = parts[0].casefold()
        subcommand = parts[1].casefold() if len(parts) > 1 else ""
        value = parts[2].strip() if len(parts) > 2 else ""
        try:
            if command == "/help":
                return CommandResult(True, "/status /config /model set /endpoint set /token set /trust list /owner list /doctor /restart")
            if command in {"/status", "/config"}:
                model = config.model or "не задана"
                endpoint = config.endpoint or "не задан"
                token = config.token_mask or "нет"
                return CommandResult(True, f"model={model}; endpoint={endpoint}; token={token}")
            if command == "/trust" and subcommand == "list" and not value:
                return CommandResult(True, ", ".join(sorted(config.trusted_jids)) or "Нет trusted JID.")
            if command == "/owner" and subcommand == "list" and not value:
                return CommandResult(True, ", ".join(sorted(config.owners)))
            if command == "/owner" and subcommand in {"add", "remove"}:
                owner = self._exact_bare_jid(value)
                if subcommand == "remove":
                    if owner not in config.owners:
                        return CommandResult(True, "Owner не найден.")
                    if len(config.owners) == 1:
                        return CommandResult(True, "Нельзя удалить последнего owner.")
                    self.state.mutate(lambda current: current.with_changes(owners=current.owners - {owner}))
                else:
                    self.state.mutate(lambda current: current.with_changes(owners=current.owners | {owner}))
                return CommandResult(True, "Owner обновлён.")
            if command == "/model" and subcommand == "set":
                self._bounded(value, MAX_MODEL_LENGTH)
                self.state.mutate(lambda current: current.with_changes(model=value))
                return CommandResult(True, "Модель обновлена.")
            if command == "/endpoint" and subcommand == "set":
                self._bounded(value, MAX_ENDPOINT_LENGTH)
                self.state.mutate(lambda current: current.with_changes(endpoint=value))
                return CommandResult(True, "Endpoint обновлён.")
            if command == "/token" and subcommand == "set":
                self._bounded(value, MAX_TOKEN_LENGTH)
                updated = self.state.set_token(value)
                return CommandResult(True, f"Токен сохранён: {updated.token_mask}")
            if command == "/doctor":
                return CommandResult(True, self._run_doctor())
            if command == "/restart":
                return CommandResult(True, "Запрошено применение конфигурации.", RestartGateway())
        except (AdminStateError, ConfigValidationError, ValueError, OSError):
            return CommandResult(True, "Команда не выполнена.")
        return CommandResult(True, "Неизвестная команда. Используйте /help.")

    @staticmethod
    def _bounded(value: str, maximum: int) -> None:
        if not value or len(value) > maximum:
            raise ValueError("invalid command value")

    def _run_doctor(self) -> str:
        if self._doctor is None:
            return "Проверка конфигурации доступна после интеграции supervisor."
        try:
            result = self._doctor()
            return result if isinstance(result, str) and len(result) <= MAX_BODY_LENGTH else "Проверка недоступна."
        except Exception:
            return "Проверка недоступна."
