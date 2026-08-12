import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.models import InboundXmppMessage
from xmpp_bridge.policy import (
    normalize_bare_jid,
    parse_allowlist,
    route_direct,
    route_group,
    session_key,
)


def direct(sender="admin@aversa.run", body="Вопрос", chat="hermes@aversa.run"):
    return InboundXmppMessage("m1", chat, sender, "Admin", body, False, None)


def group(sender="admin@aversa.run", body="Hermes, вопрос", nick="Admin", reply=None):
    return InboundXmppMessage("m2", "room@conference.aversa.run", sender, nick, body, True, reply)


ALLOWED = frozenset({"admin@aversa.run", "yuklya@aversa.run", "julia@aversa.run"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Admin@Aversa.Run/phone", "admin@aversa.run"),
        ("  YUKLYA@AVERSA.RUN  ", "yuklya@aversa.run"),
    ],
)
def test_normalize_bare_jid_removes_resource_and_casefolds(value, expected):
    assert normalize_bare_jid(value) == expected


@pytest.mark.parametrize("value", ["", "  ", "no-at-sign", "@aversa.run", "admin@", "a@b@c", "a b@c"])
def test_normalize_bare_jid_rejects_malformed_or_empty_values(value):
    with pytest.raises(ValueError):
        normalize_bare_jid(value)


def test_parse_allowlist_normalizes_entries_and_ignores_empty_items():
    assert parse_allowlist(" Admin@Aversa.Run/mobile, , yuklya@aversa.run ") == frozenset(
        {"admin@aversa.run", "yuklya@aversa.run"}
    )


def test_direct_route_accepts_allowed_sender_and_strips_body():
    routed = route_direct(direct(body="  Вопрос  "), ALLOWED)
    assert routed is not None
    assert routed.message == direct(body="  Вопрос  ")
    assert routed.body == "Вопрос"


@pytest.mark.parametrize(
    "message, allowed",
    [
        (direct(sender="intruder@aversa.run"), ALLOWED),
        (direct(body=" \t "), ALLOWED),
        (direct(sender="hermes@aversa.run", chat="hermes@aversa.run"), ALLOWED | {"hermes@aversa.run"}),
    ],
)
def test_direct_route_rejects_denied_empty_and_self_messages(message, allowed):
    assert route_direct(message, allowed) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Hermes, вопрос", "вопрос"),
        ("@Hermes вопрос", "вопрос"),
        ("hermes@aversa.run вопрос", "вопрос"),
    ],
)
def test_group_route_accepts_leading_case_insensitive_mentions_and_removes_marker(body, expected):
    routed = route_group(group(body=body), ALLOWED, "hermes@aversa.run", "Hermes", set())
    assert routed is not None
    assert routed.body == expected


@pytest.mark.parametrize(
    "body",
    [
        "обычный текст", 
        "shermes вопрос",
        "Hermesговорит вопрос",
        "спроси Hermes, вопрос",
        "Hermes,   ",
    ],
)
def test_group_route_rejects_nonleading_or_substring_mentions_and_empty_remainder(body):
    assert route_group(group(body=body), ALLOWED, "hermes@aversa.run", "Hermes", set()) is None


def test_group_route_accepts_reply_to_recent_bot_message_without_mention():
    routed = route_group(group(body="обычный ответ", reply="bot-42"), ALLOWED, "hermes@aversa.run", "Hermes", {"bot-42"})
    assert routed is not None
    assert routed.body == "обычный ответ"


@pytest.mark.parametrize(
    "message, bot_message_ids",
    [
        (group(sender="intruder@aversa.run", reply="bot-42"), {"bot-42"}),
        (group(body="обычный ответ", reply="not-cached"), {"bot-42"}),
        (group(nick="hErMeS"), set()),
    ],
)
def test_group_route_checks_allowlist_before_activation_and_rejects_own_nick(message, bot_message_ids):
    assert route_group(message, ALLOWED, "hermes@aversa.run", "Hermes", bot_message_ids) is None


def test_session_key_uses_normalized_bare_jids_for_direct_and_group_messages():
    assert session_key(direct(sender="Admin@Aversa.Run/resource")) == "xmpp:dm:admin@aversa.run"
    assert session_key(group(sender="Admin@Aversa.Run/resource").__class__(
        "m2", "Room@Conference.Aversa.Run/desktop", "Admin@Aversa.Run/resource", "Admin", "body", True, None
    )) == "xmpp:muc:room@conference.aversa.run:admin@aversa.run"


@pytest.mark.parametrize(
    "message",
    [
        direct(sender="not-a-jid"),
        InboundXmppMessage("m", "not-a-room", "admin@aversa.run", "Admin", "body", True, None),
        InboundXmppMessage("m", "hermes@aversa.run", "admin@aversa.run", "Admin", "body", "yes", None),
    ],
)
def test_session_key_rejects_invalid_events(message):
    with pytest.raises(ValueError):
        session_key(message)
