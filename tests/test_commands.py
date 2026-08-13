import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.commands import CommandRouter, RestartGateway
from xmpp_bridge.models import InboundXmppMessage


@dataclass(frozen=True)
class Config:
    owners: frozenset = frozenset({"owner@example.com"})
    trusted_jids: frozenset = frozenset({"trusted@example.com"})
    model: str | None = None
    endpoint: str | None = None
    token_mask: str | None = None
    token_present: bool = False
    revision: int = 0
    def with_changes(self, **changes): return replace(self, **changes)


class State:
    def __init__(self): self.config = Config(); self.secret = None
    def load(self): return self.config
    def mutate(self, fn): self.config = replace(fn(self.config), revision=self.config.revision + 1); return self.config
    def set_token(self, token): self.secret = token; self.config = replace(self.config, token_present=True, token_mask="***" + token[-4:], revision=self.config.revision + 1); return self.config


def message(sender="owner@example.com", body="ping", group=False):
    return InboundXmppMessage("m", "room@example.com" if group else "bot@example.com", sender, "User", body, group, None)


def test_ping_authorized_dm_only_and_denied_or_muc_is_silent():
    router = CommandRouter(State())
    assert router.handle(message("trusted@example.com", " PING ")).reply == "pong"
    assert router.handle(message("denied@example.com", "ping")).handled is False
    assert router.handle(message("owner@example.com", "ping", True)).handled is False


def test_owner_commands_change_config_without_token_echo():
    state, router = State(), CommandRouter(State())
    state = router.state
    assert router.handle(message(body="/model set model-x")).reply == "Модель обновлена."
    assert router.handle(message(body="/endpoint set https://llm.example/v1")).reply == "Endpoint обновлён."
    result = router.handle(message(body="/token set very-secret-token"))
    assert result.reply == "Токен сохранён: ***oken"
    assert "very-secret" not in repr(result)
    assert (state.config.model, state.config.endpoint, state.secret) == ("model-x", "https://llm.example/v1", "very-secret-token")


def test_owner_only_and_muc_never_administers():
    router = CommandRouter(State())
    assert router.handle(message("trusted@example.com", "/model set bad")).handled is False
    assert router.handle(message("owner@example.com", "/model set bad", True)).handled is False


def test_bare_jid_toggle_requires_same_owner_confirmation_and_rechecks_revision():
    state, router = State(), CommandRouter(State())
    state = router.state
    ask = router.handle(message(body=" New@Example.Com "))
    assert "new@example.com" in ask.reply and "добав" in ask.reply
    assert state.config.trusted_jids == frozenset({"trusted@example.com"})
    assert router.handle(message("trusted@example.com", "yes")).handled is False
    done = router.handle(message(body=" Да "))
    assert done.reply == "Trusted JID обновлён." and "new@example.com" in state.config.trusted_jids
    router.handle(message(body="other@example.com"))
    state.mutate(lambda c: c.with_changes(model="changed"))
    assert "устарел" in router.handle(message(body="yes")).reply


def test_pending_cancel_expiry_replay_and_restart_placeholder():
    clock = [0.0]
    router = CommandRouter(State(), monotonic=lambda: clock[0])
    router.handle(message(body="new@example.com")); clock[0] = 61
    assert router.handle(message(body="yes")).handled is False
    router.handle(message(body="new@example.com")); assert "отмен" in router.handle(message(body="нет")).reply
    assert router.handle(message(body="/restart")).control_event == RestartGateway()


def test_owner_commands_preserve_last_owner_and_ordinary_text_is_unhandled():
    state, router = State(), CommandRouter(State())
    state = router.state
    assert router.handle(message(body="/owner add second@example.com")).reply == "Owner обновлён."
    assert "second@example.com" in state.config.owners
    assert router.handle(message(body="/owner remove owner@example.com")).reply == "Owner обновлён."
    assert state.config.owners == frozenset({"second@example.com"})
    assert "последнего" in router.handle(message("second@example.com", "/owner remove second@example.com")).reply
    assert router.handle(message("second@example.com", "please ask new@example.com later")).handled is False
