import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.commands import CommandRouter, RestartGateway
from xmpp_bridge.admin_state import ConfigValidationError
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


def test_pending_owner_ping_cancels_while_other_authorized_pings_stay_pong():
    state = State(); router = CommandRouter(state)
    router.handle(message(body="new@example.com"))
    assert router.handle(message(body="ping")).reply == "Операция отменена."
    assert router.handle(message(body="yes")).handled is False
    assert "new@example.com" not in state.config.trusted_jids
    assert router.handle(message(body="ping")).reply == "pong"
    assert router.handle(message("trusted@example.com", "ping")).reply == "pong"


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


def test_pending_expires_at_exact_sixty_seconds_and_replay_is_unhandled():
    clock = [0.0]
    state = State(); router = CommandRouter(state, monotonic=lambda: clock[0])
    router.handle(message(body="new@example.com")); clock[0] = 60.0
    assert router.handle(message(body="yes")).handled is False
    assert "new@example.com" not in state.config.trusted_jids
    assert router.handle(message(body="yes")).handled is False


@pytest.mark.parametrize("body", ["x" * 4097, "x" * 4097 + "@example.com"])
def test_overlong_pending_reply_cancels_and_cannot_be_replayed(body):
    state = State(); router = CommandRouter(state)
    router.handle(message(body="new@example.com"))
    assert router.handle(message(body=body)).reply == "Операция отменена."
    assert router.handle(message(body="yes")).handled is False
    assert "new@example.com" not in state.config.trusted_jids


def test_owner_commands_preserve_last_owner_and_ordinary_text_is_unhandled():
    state, router = State(), CommandRouter(State())
    state = router.state
    assert router.handle(message(body="/owner add second@example.com")).reply == "Owner обновлён."
    assert "second@example.com" in state.config.owners
    assert router.handle(message(body="/owner remove owner@example.com")).reply == "Owner обновлён."
    assert state.config.owners == frozenset({"second@example.com"})
    assert "последнего" in router.handle(message("second@example.com", "/owner remove second@example.com")).reply
    assert router.handle(message("second@example.com", "please ask new@example.com later")).handled is False


@pytest.mark.parametrize("answer", ["да", " YES ", "Y"])
def test_pending_accepts_all_confirmation_variants(answer):
    state = State(); router = CommandRouter(state)
    router.handle(message(body="new@example.com"))
    assert router.handle(message(body=answer)).reply == "Trusted JID обновлён."
    assert "new@example.com" in state.config.trusted_jids


def test_new_bare_jid_replaces_pending_but_sentence_or_resource_cancels():
    state = State(); router = CommandRouter(state)
    router.handle(message(body="first@example.com"))
    replacement = router.handle(message(body="second@example.com"))
    assert "second@example.com" in replacement.reply
    assert router.handle(message(body="yes")).reply == "Trusted JID обновлён."
    assert "second@example.com" in state.config.trusted_jids and "first@example.com" not in state.config.trusted_jids
    router.handle(message(body="third@example.com"))
    assert router.handle(message(body="third@example.com/resource")).reply == "Операция отменена."
    assert router.handle(message(body="yes")).handled is False


def test_pending_stale_check_happens_inside_mutate_snapshot():
    class RacingState(State):
        def mutate(self, fn):
            self.config = replace(self.config, revision=self.config.revision + 1)
            return super().mutate(fn)
    state = RacingState(); router = CommandRouter(state)
    router.handle(message(body="new@example.com"))
    result = router.handle(message(body="yes"))
    assert "устарел" in result.reply and "new@example.com" not in state.config.trusted_jids


@pytest.mark.parametrize("body", ["/model SET Model-X", "/ENDPOINT set https://llm.example/v1", "/TOKEN SET secret-value"])
def test_commands_and_subcommands_are_casefolded(body):
    router = CommandRouter(State())
    assert router.handle(message(body=body)).handled is True


@pytest.mark.parametrize("body", ["/model set " + "x" * 513, "/endpoint set " + "x" * 513, "/token set " + "x" * 513])
def test_oversize_or_invalid_input_returns_sanitized_reply(body):
    result = CommandRouter(State()).handle(message(body=body))
    assert result.reply is not None and body not in result.reply


@pytest.mark.parametrize("body", ["user@example.com/resource", "user @example.com", "please user@example.com", "/owner add user@example.com/resource"])
def test_only_an_entire_strict_bare_jid_is_accepted(body):
    result = CommandRouter(State()).handle(message(body=body))
    assert result.handled is False or result.reply == "Команда не выполнена."


def test_state_validation_errors_are_sanitized_without_token_or_input():
    class RejectingState(State):
        def mutate(self, _fn):
            raise ConfigValidationError("contains secret-token")
        def set_token(self, _token):
            raise ConfigValidationError("contains secret-token")
    router = CommandRouter(RejectingState())
    for body in ("/model set secret-token", "/token set secret-token", "/owner add second@example.com"):
        result = router.handle(message(body=body))
        assert result.reply == "Команда не выполнена."
        assert "secret-token" not in repr(result)


def test_lists_status_help_unknown_and_doctor_callback_are_safe():
    state = State(); router = CommandRouter(state, doctor=lambda: "ok")
    assert "/model" in router.handle(message(body="/help")).reply
    assert "model=" in router.handle(message(body="/status")).reply
    assert router.handle(message(body="/config")).handled is True
    assert "trusted@example.com" in router.handle(message(body="/trust LIST")).reply
    assert "owner@example.com" in router.handle(message(body="/owner list")).reply
    assert router.handle(message(body="/doctor")).reply == "ok"
    assert "Неизвестная" in router.handle(message(body="/wat")).reply
    assert CommandRouter(state, doctor=lambda: (_ for _ in ()).throw(RuntimeError("secret"))).handle(message(body="/doctor")).reply == "Проверка недоступна."


def test_owner_service_commands_accept_a_missing_slash_except_restart():
    router = CommandRouter(State())

    assert "model=" in router.handle(message(body="status")).reply
    assert router.handle(message(body="config")).handled is True
    assert "/status\n/config" in router.handle(message(body="help")).reply
    assert router.handle(message(body="restart")).handled is False


def test_owner_can_toggle_trust_with_an_xmpp_uri_jid():
    state = State()
    router = CommandRouter(state)

    assert router.handle(message(body="xmpp:new@example.com")).handled is True
    assert router.handle(message(body="yes")).handled is True
    assert "new@example.com" in state.config.trusted_jids
