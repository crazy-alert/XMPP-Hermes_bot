import asyncio
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from slixmpp import Message


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.client import HermesXmppClient, XmppClientConfig
from xmpp_bridge.models import InboundXmppMessage, XmppInvite
from xmpp_bridge.state import RoomState


BOT_JID = "hermes@aversa.run/Hermes"
BOT_BARE = "hermes@aversa.run"
ROOM = "private@conference.aversa.run"


def make_client(tmp_path, *, on_message=None, on_invite=None):
    return HermesXmppClient(
        XmppClientConfig(BOT_JID, "not-a-real-password", "Hermes", RoomState(tmp_path / "rooms.json")),
        on_message or (lambda event: None),
        on_invite or (lambda event: None),
    )


def direct_stanza(*, sender="Admin@Aversa.Run/phone", body="hello", message_id="dm-1", reply_id=None):
    stanza = Message()
    stanza["type"] = "chat"
    stanza["from"] = sender
    stanza["to"] = BOT_JID
    stanza["body"] = body
    stanza["id"] = message_id
    if reply_id:
        ET.SubElement(stanza.xml, "{urn:xmpp:reply:0}reply", {"id": reply_id})
    return stanza


def group_stanza(*, sender="private@conference.aversa.run/Admin", body="hello", message_id="muc-1", delayed=False):
    stanza = Message()
    stanza["type"] = "groupchat"
    stanza["from"] = sender
    stanza["to"] = BOT_JID
    stanza["body"] = body
    stanza["id"] = message_id
    if delayed:
        ET.SubElement(stanza.xml, "{urn:xmpp:delay}delay", {"stamp": "2026-08-12T00:00:00Z"})
    return stanza


def test_direct_message_translates_real_synthetic_stanza_to_model(tmp_path):
    received = []
    client = make_client(tmp_path, on_message=received.append)

    client._handle_direct_message(direct_stanza(reply_id="previous-7"))

    assert received == [
        InboundXmppMessage("dm-1", BOT_BARE, "admin@aversa.run", "Admin", "hello", False, "previous-7")
    ]


@pytest.mark.parametrize(
    "stanza",
    [
        group_stanza(delayed=True),
        direct_stanza(sender="hermes@aversa.run/other-resource"),
        group_stanza(sender="private@conference.aversa.run/Hermes"),
    ],
)
def test_direct_or_group_history_and_self_messages_are_not_delivered(tmp_path, stanza):
    received = []
    client = make_client(tmp_path, on_message=received.append)

    client._handle_group_message(stanza) if stanza["type"] == "groupchat" else client._handle_direct_message(stanza)

    assert received == []


def test_group_message_uses_room_bare_sender_bare_and_actual_own_nick(tmp_path):
    received = []
    client = make_client(tmp_path, on_message=received.append)
    client._room_nicks[ROOM] = "Hermes_2"

    client._handle_group_message(group_stanza(sender=f"{ROOM}/Admin"))
    client._handle_group_message(group_stanza(sender=f"{ROOM}/Hermes_2", message_id="own"))

    assert received == [
        InboundXmppMessage("muc-1", ROOM, "admin@aversa.run", "Admin", "hello", True, None)
    ]


def mediated_invite():
    stanza = Message()
    stanza["from"] = ROOM
    stanza["to"] = BOT_JID
    stanza.enable("muc")
    stanza["muc"]["invite"]["from"] = "Admin@Aversa.Run/phone"
    stanza["body"] = "untrusted body"
    return stanza


def direct_invite():
    stanza = Message()
    stanza["from"] = "Admin@Aversa.Run/phone"
    stanza["to"] = BOT_JID
    stanza.enable("groupchat_invite")
    stanza["groupchat_invite"]["jid"] = ROOM
    stanza["body"] = "untrusted body"
    return stanza


@pytest.mark.parametrize(
    ("stanza_factory", "handler", "expected"),
    [
        (mediated_invite, "_handle_mediated_invite", XmppInvite(ROOM, "admin@aversa.run", False)),
        (direct_invite, "_handle_direct_invite", XmppInvite(ROOM, "admin@aversa.run", True)),
    ],
)
def test_invites_extract_addresses_from_extensions_before_any_join_or_state(tmp_path, stanza_factory, handler, expected):
    received = []
    client = make_client(tmp_path, on_invite=received.append)
    client.join_room = lambda room: pytest.fail("raw invite handler must not join")
    client.config.room_state.add = lambda room: pytest.fail("raw invite handler must not persist")

    getattr(client, handler)(stanza_factory())

    assert received == [expected]


@pytest.mark.asyncio
async def test_session_start_rejoins_saved_rooms_without_history(tmp_path):
    state = RoomState(tmp_path / "rooms.json")
    state.add(ROOM)
    client = HermesXmppClient(XmppClientConfig(BOT_JID, "not-a-real-password", "Hermes", state), lambda event: None, lambda event: None)
    joined = []
    client.send_presence = lambda: joined.append("presence")
    client.get_roster = lambda: joined.append("roster")

    async def join(room_jid):
        joined.append((room_jid, 0))

    client.join_room = join
    await client._session_start(None)

    assert joined == ["presence", "roster", (ROOM, 0)]


def test_client_registers_tls_safe_xeps_and_bounded_reconnect_delays(tmp_path):
    client = make_client(tmp_path)

    assert {"xep_0030", "xep_0045", "xep_0085", "xep_0198", "xep_0249", "xep_0461"} <= set(client.plugin)
    assert client.disable_starttls is False
    assert client.RECONNECT_DELAYS == (1, 2, 5, 10, 30, 60)
    assert client._next_reconnect_delay() == 1
    assert client._next_reconnect_delay() == 2
    assert [client._next_reconnect_delay() for _ in range(5)] == [5, 10, 30, 60, 60]


def capture_outbound(client):
    sent = []
    client.send = sent.append
    return sent


def test_outbound_splits_at_paragraph_or_whitespace_and_returns_stanza_ids(tmp_path):
    client = make_client(tmp_path)
    sent = capture_outbound(client)
    body = "a" * 3499 + "\n\n" + "b" * 20

    ids = client.send_direct("Admin@Aversa.Run/phone", body)

    assert len(ids) == 2
    assert [stanza["id"] for stanza in sent] == ids
    assert [stanza["body"] for stanza in sent] == ["a" * 3499, "b" * 20]
    assert all(len(stanza["body"]) <= 3500 for stanza in sent)
    assert all(stanza["to"] == "admin@aversa.run" and stanza["type"] == "chat" for stanza in sent)


def test_outbound_splits_single_long_unicode_word_and_marks_group_messages(tmp_path):
    client = make_client(tmp_path)
    sent = capture_outbound(client)

    ids = client.send_group(ROOM, "я" * 3501)

    assert len(ids) == 2
    assert [len(stanza["body"]) for stanza in sent] == [3500, 1]
    assert all(stanza["id"] for stanza in sent)
    assert all(stanza["to"] == ROOM and stanza["type"] == "groupchat" for stanza in sent)


def test_typing_stanzas_use_chat_states_for_direct_and_group_targets(tmp_path):
    client = make_client(tmp_path)
    sent = capture_outbound(client)

    client.set_typing("admin@aversa.run", False, True)
    client.set_typing(ROOM, True, False)

    assert [(stanza["to"], stanza["type"], stanza["chat_state"]) for stanza in sent] == [
        ("admin@aversa.run", "chat", "composing"),
        (ROOM, "groupchat", "active"),
    ]


@pytest.mark.asyncio
async def test_connect_and_disconnect_use_optional_host_and_port_without_logging_password(tmp_path, caplog):
    client = HermesXmppClient(
        XmppClientConfig(BOT_JID, "not-a-real-password", "Hermes", RoomState(tmp_path / "rooms.json"), "xmpp.example.test", 5223),
        lambda event: None,
        lambda event: None,
    )
    connected = []
    client.connect = lambda host=None, port=None: connected.append((host, port))
    client.disconnect = lambda: connected.append("disconnected")

    await client.connect_and_wait()
    await client.stop()

    assert connected == [("xmpp.example.test", 5223), "disconnected"]
    assert "not-a-real-password" not in caplog.text
