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


def direct(sender="admin@example.com", body="Вопрос", chat="bot@example.com"):
    return InboundXmppMessage("m1", chat, sender, "Admin", body, False, None)


def group(sender="admin@example.com", body="Hermes, вопрос", nick="Admin", reply=None):
    return InboundXmppMessage("m2", "room@conference.example.com", sender, nick, body, True, reply)


ALLOWED = frozenset({"admin@example.com", "alice@example.com", "bob@example.com"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Admin@Example.Com/phone", "admin@example.com"),
        ("  ALICE@EXAMPLE.COM  ", "alice@example.com"),
    ],
)
def test_normalize_bare_jid_removes_resource_and_casefolds(value, expected):
    assert normalize_bare_jid(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "", "  ", "no-at-sign", "@example.com", "admin@", "a@b@c", "a b@c",
        "a@.", "a@.example.com", "a@example.com.", "a@example..com",
        "a@example\x00.com", "a@example\x1f.com", "a\x7f@example.com",
    ],
)
def test_normalize_bare_jid_rejects_malformed_or_empty_values(value):
    with pytest.raises(ValueError):
        normalize_bare_jid(value)


def test_parse_allowlist_normalizes_entries_and_ignores_empty_items():
    assert parse_allowlist(" Admin@Example.Com/mobile, , alice@example.com ") == frozenset(
        {"admin@example.com", "alice@example.com"}
    )


def test_direct_route_accepts_allowed_sender_and_strips_body():
    routed = route_direct(direct(body="  Вопрос  "), ALLOWED)
    assert routed is not None
    assert routed.message == direct(body="  Вопрос  ")
    assert routed.body == "Вопрос"


@pytest.mark.parametrize(
    "message, allowed",
    [
        (direct(sender="intruder@example.com"), ALLOWED),
        (direct(body=" \t "), ALLOWED),
        (direct(sender="bot@example.com", chat="bot@example.com"), ALLOWED | {"bot@example.com"}),
    ],
)
def test_direct_route_rejects_denied_empty_and_self_messages(message, allowed):
    assert route_direct(message, allowed) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Hermes, вопрос", "вопрос"),
        ("@Hermes вопрос", "вопрос"),
        ("bot@example.com вопрос", "вопрос"),
    ],
)
def test_group_route_accepts_leading_case_insensitive_mentions_and_removes_marker(body, expected):
    routed = route_group(group(body=body), ALLOWED, "bot@example.com", "Hermes", set())
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
    assert route_group(group(body=body), ALLOWED, "bot@example.com", "Hermes", set()) is None


def test_group_route_accepts_reply_to_recent_bot_message_without_mention():
    routed = route_group(group(body="обычный ответ", reply="bot-42"), ALLOWED, "bot@example.com", "Hermes", {"bot-42"})
    assert routed is not None
    assert routed.body == "обычный ответ"


@pytest.mark.parametrize(
    "message, bot_message_ids",
    [
        (group(sender="intruder@example.com", reply="bot-42"), {"bot-42"}),
        (group(body="обычный ответ", reply="not-cached"), {"bot-42"}),
        (group(nick="hErMeS"), set()),
    ],
)
def test_group_route_checks_allowlist_before_activation_and_rejects_own_nick(message, bot_message_ids):
    assert route_group(message, ALLOWED, "bot@example.com", "Hermes", bot_message_ids) is None


@pytest.mark.parametrize(
    "message, bot_jid, bot_message_ids",
    [
        (InboundXmppMessage("m", "not-a-room", "admin@example.com", "Admin", "reply", True, "bot-42"), "bot@example.com", {"bot-42"}),
        (group(body="reply", reply="bot-42"), "not-a-jid", {"bot-42"}),
        (group(body="reply", reply=""), "bot@example.com", {""}),
        (group(body="reply", reply="bot-42"), "bot@example.com", "bot-42"),
    ],
)
def test_group_route_rejects_invalid_room_bot_or_reply_id_cache_before_reply_activation(message, bot_jid, bot_message_ids):
    assert route_group(message, ALLOWED, bot_jid, "Hermes", bot_message_ids) is None


def test_session_key_uses_normalized_bare_jids_for_direct_and_group_messages():
    assert session_key(direct(sender="Admin@Example.Com/resource")) == "xmpp:dm:admin@example.com"
    assert session_key(group(sender="Admin@Example.Com/resource").__class__(
        "m2", "Room@Conference.Example.Com/desktop", "Admin@Example.Com/resource", "Admin", "body", True, None
    )) == "xmpp:muc:room@conference.example.com:admin@example.com"


def test_session_key_rejects_direct_event_with_malformed_chat_jid():
    with pytest.raises(ValueError):
        session_key(direct(chat="not-a-chat"))


def test_session_key_rejects_direct_event_addressed_from_its_own_chat_jid():
    with pytest.raises(ValueError):
        session_key(direct(sender="bot@example.com", chat="bot@example.com"))


@pytest.mark.parametrize(
    "message",
    [
        direct(sender="not-a-jid"),
        InboundXmppMessage("m", "not-a-room", "admin@example.com", "Admin", "body", True, None),
        InboundXmppMessage("m", "bot@example.com", "admin@example.com", "Admin", "body", "yes", None),
    ],
)
def test_session_key_rejects_invalid_events(message):
    with pytest.raises(ValueError):
        session_key(message)
