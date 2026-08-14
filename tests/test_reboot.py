from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.reboot import HostRebootRequest, RebootConfirmationState


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def machine(clock: Clock, codes: list[str] | None = None) -> RebootConfirmationState:
    values = iter(codes or ["123456"])
    return RebootConfirmationState(clock=clock, code_generator=lambda: next(values), cooldown_seconds=300)


def test_same_owner_dm_confirms_once_and_starts_cooldown() -> None:
    clock = Clock()
    state = machine(clock)
    prompt = state.request("owner@example.com", is_dm=True, is_owner=True)
    assert "123456" in prompt.reply
    assert prompt.event is None

    confirmed = state.confirm("owner@example.com", "123456", is_dm=True, is_owner=True)
    assert confirmed.event == HostRebootRequest(owner_bare_jid="owner@example.com", confirmed_at=100.0)
    assert "123456" not in confirmed.reply
    assert state.status().pending is False
    assert state.status().cooldown_remaining_seconds == 300

    replay = state.confirm("owner@example.com", "123456", is_dm=True, is_owner=True)
    assert replay.event is None
    assert "123456" not in replay.reply
    assert state.request("owner@example.com", is_dm=True, is_owner=True).event is None
    assert "cooldown" in state.request("owner@example.com", is_dm=True, is_owner=True).reply.lower()


@pytest.mark.parametrize(
    ("owner", "is_dm", "is_owner"),
    [("trusted@example.com", True, False), ("owner@example.com", False, True)],
)
def test_request_rejects_trusted_and_muc(owner: str, is_dm: bool, is_owner: bool) -> None:
    state = machine(Clock())
    result = state.request(owner, is_dm=is_dm, is_owner=is_owner)
    assert result.event is None
    assert state.status().pending is False
    assert "123456" not in result.reply


@pytest.mark.parametrize(
    ("operation", "owner", "is_dm", "is_owner"),
    [
        ("confirm", "trusted@example.com", True, False),
        ("confirm", "owner@example.com", False, True),
        ("cancel", "trusted@example.com", True, False),
        ("cancel", "owner@example.com", False, True),
    ],
)
def test_confirm_and_cancel_reject_trusted_and_muc(
    operation: str, owner: str, is_dm: bool, is_owner: bool
) -> None:
    state = machine(Clock())
    state.request("owner@example.com", is_dm=True, is_owner=True)
    if operation == "confirm":
        result = state.confirm(owner, "123456", is_dm=is_dm, is_owner=is_owner)
    else:
        result = state.cancel(owner, is_dm=is_dm, is_owner=is_owner)
    assert result.event is None
    assert state.status().pending is True
    assert "123456" not in result.reply


def test_wrong_owner_and_wrong_code_do_not_leak_or_consume_pending() -> None:
    state = machine(Clock())
    state.request("owner@example.com", is_dm=True, is_owner=True)

    wrong_owner = state.confirm("other@example.com", "123456", is_dm=True, is_owner=True)
    wrong_code = state.confirm("owner@example.com", "000000", is_dm=True, is_owner=True)
    assert wrong_owner.event is wrong_code.event is None
    assert "123456" not in wrong_owner.reply
    assert "123456" not in wrong_code.reply
    assert state.status().pending is True
    assert state.confirm("owner@example.com", "123456", is_dm=True, is_owner=True).event is not None


@pytest.mark.parametrize("bad_code", ["１２３４５６", "abcdef", "12345", "1234567", 123456, None])
def test_malformed_code_is_rejected_without_exception_or_consumption(bad_code: object) -> None:
    state = machine(Clock())
    state.request("owner@example.com", is_dm=True, is_owner=True)
    result = state.confirm("owner@example.com", bad_code, is_dm=True, is_owner=True)  # type: ignore[arg-type]
    assert result.event is None
    assert "123456" not in result.reply
    assert bad_code is None or str(bad_code) not in result.reply
    assert state.status().pending is True


def test_expiry_cancel_and_new_request_rules() -> None:
    clock = Clock()
    state = machine(clock, ["123456", "654321", "123456"])
    state.request("owner@example.com", is_dm=True, is_owner=True)
    busy = state.request("other@example.com", is_dm=True, is_owner=True)
    assert "busy" in busy.reply.lower()
    replacement = state.request("owner@example.com", is_dm=True, is_owner=True)
    assert "654321" in replacement.reply
    assert state.confirm("owner@example.com", "123456", is_dm=True, is_owner=True).event is None

    cancelled = state.cancel("owner@example.com", is_dm=True, is_owner=True)
    assert cancelled.event is None
    assert state.status().pending is False
    assert state.confirm("owner@example.com", "654321", is_dm=True, is_owner=True).event is None

    state.request("owner@example.com", is_dm=True, is_owner=True)
    clock.now += 61
    expired = state.confirm("owner@example.com", "123456", is_dm=True, is_owner=True)
    assert expired.event is None
    assert state.status().pending is False


def test_code_is_absent_from_repr_and_status_and_events_are_immutable() -> None:
    state = machine(Clock())
    result = state.request("owner@example.com", is_dm=True, is_owner=True)
    assert "123456" not in repr(result)
    assert "123456" not in repr(state)
    assert "123456" not in repr(state.status())
    assert not hasattr(state.status(), "code")
    event = state.confirm("owner@example.com", "123456", is_dm=True, is_owner=True).event
    assert event is not None
    with pytest.raises(FrozenInstanceError):
        event.owner_bare_jid = "attacker@example.com"  # type: ignore[misc]


def test_default_generator_produces_six_numeric_digits() -> None:
    result = RebootConfirmationState(clock=Clock()).request("owner@example.com", is_dm=True, is_owner=True)
    code = result.reply.split("/confirm reboot ", 1)[1].split()[0]
    assert len(code) == 6
    assert code.isascii() and code.isdigit()


def test_expiry_and_cooldown_exact_boundaries() -> None:
    clock = Clock()
    state = machine(clock, ["123456", "654321"])
    state.request("owner@example.com", is_dm=True, is_owner=True)
    clock.now = 159.999
    assert state.confirm("owner@example.com", "123456", is_dm=True, is_owner=True).event is not None
    clock.now = 459.998
    assert "cooldown" in state.request("owner@example.com", is_dm=True, is_owner=True).reply.lower()
    clock.now = 459.999
    assert "654321" in state.request("owner@example.com", is_dm=True, is_owner=True).reply

    expiring = machine(clock, ["123456"])
    expiring.request("owner@example.com", is_dm=True, is_owner=True)
    clock.now = 520.0
    assert expiring.confirm("owner@example.com", "123456", is_dm=True, is_owner=True).event is None
    assert expiring.status().pending is False
