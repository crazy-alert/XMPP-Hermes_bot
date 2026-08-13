"""Owner-only DM command router, deliberately independent from Hermes/provider runtime."""

from __future__ import annotations

from dataclasses import dataclass
import time

from .models import InboundXmppMessage
from .policy import normalize_bare_jid


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


class CommandRouter:
    def __init__(self, state, *, monotonic=time.monotonic) -> None:
        self.state = state
        self._clock = monotonic
        self._pending: dict[str, _PendingToggle] = {}

    def handle(self, message: InboundXmppMessage) -> CommandResult:
        if not isinstance(message, InboundXmppMessage) or message.is_group or not isinstance(message.body, str):
            return CommandResult(False)
        try:
            sender = normalize_bare_jid(message.sender_jid)
        except ValueError:
            return CommandResult(False)
        config = self.state.load()
        allowed = config.owners | config.trusted_jids
        body = message.body.strip()
        if sender not in allowed:
            return CommandResult(False)
        if body.casefold() == "ping":
            return CommandResult(True, "pong")
        if sender not in config.owners:
            return CommandResult(False)
        pending = self._pending.get(sender)
        if pending is not None:
            return self._confirm(sender, body, config, pending)
        if not body.startswith("/"):
            return self._start_toggle(sender, body, config)
        return self._command(body, config)

    def _confirm(self, sender: str, body: str, config, pending: _PendingToggle) -> CommandResult:
        del self._pending[sender]
        if self._clock() > pending.expires_at:
            return CommandResult(False)
        if body.casefold() not in {"да", "yes", "y"}:
            return CommandResult(True, "Операция отменена.")
        current_present = pending.jid in config.trusted_jids
        if config.revision != pending.revision or current_present != pending.present:
            return CommandResult(True, "Запрос устарел; повторите команду.")
        self.state.mutate(lambda value: value.with_changes(trusted_jids=(value.trusted_jids | {pending.jid}) if pending.add else (value.trusted_jids - {pending.jid})))
        return CommandResult(True, "Trusted JID обновлён.")

    def _start_toggle(self, sender: str, body: str, config) -> CommandResult:
        try:
            jid = normalize_bare_jid(body)
        except ValueError:
            return CommandResult(False)
        present = jid in config.trusted_jids
        self._pending[sender] = _PendingToggle(jid, not present, config.revision, present, self._clock() + 60)
        action = "добавить" if not present else "удалить"
        return CommandResult(True, f"Подтвердите: {action} {jid}? Ответьте да/yes/y.")

    def _command(self, body: str, config) -> CommandResult:
        command, _, argument = body.partition(" ")
        command, argument = command.casefold(), argument.strip()
        if command == "/help":
            return CommandResult(True, "/status /config /model set /endpoint set /token set /trust list /owner list /doctor /restart")
        if command in {"/status", "/config"}:
            model = config.model or "не задана"
            endpoint = config.endpoint or "не задан"
            token = config.token_mask or "нет"
            return CommandResult(True, f"model={model}; endpoint={endpoint}; token={token}")
        if command == "/trust" and argument.casefold() == "list":
            return CommandResult(True, ", ".join(sorted(config.trusted_jids)) or "Нет trusted JID.")
        if command == "/owner" and argument.casefold() == "list":
            return CommandResult(True, ", ".join(sorted(config.owners)))
        if command == "/owner" and argument.startswith("add "):
            try:
                owner = normalize_bare_jid(argument[4:].strip())
            except ValueError:
                return CommandResult(True, "Некорректный JID.")
            self.state.mutate(lambda value: value.with_changes(owners=value.owners | {owner}))
            return CommandResult(True, "Owner обновлён.")
        if command == "/owner" and argument.startswith("remove "):
            try:
                owner = normalize_bare_jid(argument[7:].strip())
            except ValueError:
                return CommandResult(True, "Некорректный JID.")
            if owner not in config.owners:
                return CommandResult(True, "Owner не найден.")
            if len(config.owners) == 1:
                return CommandResult(True, "Нельзя удалить последнего owner.")
            self.state.mutate(lambda value: value.with_changes(owners=value.owners - {owner}))
            return CommandResult(True, "Owner обновлён.")
        if command == "/model" and argument.startswith("set "):
            self.state.mutate(lambda value: value.with_changes(model=argument[4:].strip()))
            return CommandResult(True, "Модель обновлена.")
        if command == "/endpoint" and argument.startswith("set "):
            self.state.mutate(lambda value: value.with_changes(endpoint=argument[4:].strip()))
            return CommandResult(True, "Endpoint обновлён.")
        if command == "/token" and argument.startswith("set "):
            updated = self.state.set_token(argument[4:].strip())
            return CommandResult(True, f"Токен сохранён: {updated.token_mask}")
        if command == "/doctor":
            return CommandResult(True, "Проверка конфигурации доступна после интеграции supervisor.")
        if command == "/restart":
            return CommandResult(True, "Запрошено применение конфигурации.", RestartGateway())
        return CommandResult(True, "Неизвестная команда. Используйте /help.")
