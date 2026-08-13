import asyncio
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from slixmpp import Message, Presence


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "hermes-xmpp"))

from xmpp_bridge.client import HermesXmppClient, XmppClientConfig
from xmpp_bridge.models import InboundXmppMessage, XmppInvite
from xmpp_bridge.state import RoomState


BOT_JID = "bot@example.com/Hermes"
BOT_BARE = "bot@example.com"
ROOM = "private@conference.example.com"


def make_client(tmp_path, *, on_message=None, on_invite=None):
    return HermesXmppClient(
        XmppClientConfig(BOT_JID, "not-a-real-password", "Hermes", RoomState(tmp_path / "rooms.json")),
        on_message or (lambda event: None),
        on_invite or (lambda event: None),
    )


def direct_stanza(*, sender="Admin@Example.Com/phone", body="hello", message_id="dm-1", reply_id=None):
    stanza = Message()
    stanza["type"] = "chat"
    stanza["from"] = sender
    stanza["to"] = BOT_JID
    stanza["body"] = body
    stanza["id"] = message_id
    if reply_id:
        ET.SubElement(stanza.xml, "{urn:xmpp:reply:0}reply", {"id": reply_id})
    return stanza


def group_stanza(*, sender="private@conference.example.com/Admin", body="hello", message_id="muc-1", delayed=False):
    stanza = Message()
    stanza["type"] = "groupchat"
    stanza["from"] = sender
    stanza["to"] = BOT_JID
    stanza["body"] = body
    stanza["id"] = message_id
    muc_user = ET.SubElement(stanza.xml, "{http://jabber.org/protocol/muc#user}x")
    ET.SubElement(muc_user, "{http://jabber.org/protocol/muc#user}item", {"jid": "Admin@Example.Com/phone"})
    if delayed:
        ET.SubElement(stanza.xml, "{urn:xmpp:delay}delay", {"stamp": "2026-08-12T00:00:00Z"})
    return stanza


def test_direct_message_translates_real_synthetic_stanza_to_model(tmp_path):
    received = []
    client = make_client(tmp_path, on_message=received.append)

    client._handle_direct_message(direct_stanza(reply_id="previous-7"))

    assert received == [
        InboundXmppMessage("dm-1", BOT_BARE, "admin@example.com", "admin", "hello", False, "previous-7")
    ]


@pytest.mark.parametrize(
    "stanza",
    [
        group_stanza(delayed=True),
        direct_stanza(sender="bot@example.com/other-resource"),
        group_stanza(sender="private@conference.example.com/Hermes"),
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
        InboundXmppMessage("muc-1", ROOM, "admin@example.com", "Admin", "hello", True, None, "Hermes_2")
    ]


def test_nick_collision_presence_updates_nick_carried_by_real_group_stanza(tmp_path):
    received = []
    client = make_client(tmp_path, on_message=received.append)
    presence = Presence()
    presence["from"] = f"{ROOM}/Hermes_2"
    presence.enable("muc")
    presence["muc"]["status_codes"] = {110, 210}

    client._handle_group_presence(presence)
    client._handle_group_message(group_stanza(sender=f"{ROOM}/Admin", body="Hermes_2: question"))

    assert received == [
        InboundXmppMessage(
            "muc-1", ROOM, "admin@example.com", "Admin", "Hermes_2: question", True, None, "Hermes_2"
        )
    ]


def mediated_invite():
    stanza = Message()
    stanza["from"] = ROOM
    stanza["to"] = BOT_JID
    stanza.enable("muc")
    stanza["muc"]["invite"]["from"] = "Admin@Example.Com/phone"
    stanza["body"] = "untrusted body"
    return stanza


def direct_invite():
    stanza = Message()
    stanza["from"] = "Admin@Example.Com/phone"
    stanza["to"] = BOT_JID
    stanza.enable("groupchat_invite")
    stanza["groupchat_invite"]["jid"] = ROOM
    stanza["body"] = "untrusted body"
    return stanza


@pytest.mark.parametrize(
    ("stanza_factory", "handler", "expected"),
    [
        (mediated_invite, "_handle_mediated_invite", XmppInvite(ROOM, "admin@example.com", False)),
        (direct_invite, "_handle_direct_invite", XmppInvite(ROOM, "admin@example.com", True)),
    ],
)
def test_invites_extract_addresses_from_extensions_before_any_join_or_state(tmp_path, stanza_factory, handler, expected):
    received = []
    client = make_client(tmp_path, on_invite=received.append)
    client.join_room = lambda room: pytest.fail("raw invite handler must not join")
    client.config.room_state.add = lambda room: pytest.fail("raw invite handler must not persist")

    getattr(client, handler)(stanza_factory())

    assert received == [expected]


def test_session_start_rejoins_saved_rooms_without_history(tmp_path):
    state = RoomState(tmp_path / "rooms.json")
    state.add(ROOM)
    client = HermesXmppClient(XmppClientConfig(BOT_JID, "not-a-real-password", "Hermes", state), lambda event: None, lambda event: None)
    joined = []
    client.send_presence = lambda: joined.append("presence")
    client.get_roster = lambda: joined.append("roster")

    async def join(room_jid):
        joined.append((room_jid, 0))

    client.join_room = join
    asyncio.run(client._session_start(None))

    assert joined == ["presence", "roster", (ROOM, 0)]


def test_client_registers_tls_safe_xeps_and_bounded_reconnect_delays(tmp_path):
    client = make_client(tmp_path)

    assert {"xep_0030", "xep_0045", "xep_0085", "xep_0198", "xep_0249", "xep_0461"} <= set(client.plugin)
    assert client.disable_starttls is False
    assert client.RECONNECT_DELAYS == (1, 2, 5, 10, 30, 60)


def capture_outbound(client):
    sent = []
    client.send = sent.append
    return sent


def test_outbound_splits_at_paragraph_or_whitespace_and_returns_stanza_ids(tmp_path):
    client = make_client(tmp_path)
    sent = capture_outbound(client)
    body = "a" * 3499 + "\n\n" + "b" * 20

    ids = asyncio.run(client.send_direct("Admin@Example.Com/phone", body))

    assert len(ids) == 2
    assert [stanza["id"] for stanza in sent] == ids
    assert [stanza["body"] for stanza in sent] == ["a" * 3499, "b" * 20]
    assert all(len(stanza["body"]) <= 3500 for stanza in sent)
    assert all(stanza["to"] == "admin@example.com" and stanza["type"] == "chat" for stanza in sent)


def test_outbound_splits_single_long_unicode_word_and_marks_group_messages(tmp_path):
    client = make_client(tmp_path)
    sent = capture_outbound(client)

    ids = asyncio.run(client.send_group(ROOM, "я" * 3501))

    assert len(ids) == 2
    assert [len(stanza["body"]) for stanza in sent] == [3500, 1]
    assert all(stanza["id"] for stanza in sent)
    assert all(stanza["to"] == ROOM and stanza["type"] == "groupchat" for stanza in sent)


def test_typing_stanzas_use_chat_states_for_direct_and_group_targets(tmp_path):
    client = make_client(tmp_path)
    sent = capture_outbound(client)

    async def send_states():
        await client.set_typing("admin@example.com", False, True)
        await client.set_typing(ROOM, True, False)

    asyncio.run(send_states())

    assert [(stanza["to"], stanza["type"], stanza["chat_state"]) for stanza in sent] == [
        ("admin@example.com", "chat", "composing"),
        (ROOM, "groupchat", "active"),
    ]


def test_connect_and_wait_waits_for_session_start_not_tcp_future(tmp_path, caplog):
    async def scenario():
        client = HermesXmppClient(
            XmppClientConfig(BOT_JID, "not-a-real-password", "Hermes", RoomState(tmp_path / "rooms.json"), "xmpp.example.test", 5223, True),
            lambda event: None,
            lambda event: None,
        )
        connected = []

        def connect(host=None, port=None, **kwargs):
            connected.append((host, port, kwargs))
            tcp_connected = asyncio.get_running_loop().create_future()
            tcp_connected.set_result(None)
            return tcp_connected

        client.connect = connect
        client.send_presence = lambda: None
        client.get_roster = lambda: None

        ready = asyncio.create_task(client.connect_and_wait())
        await asyncio.sleep(0)
        assert connected == [("xmpp.example.test", 5223, {"use_ssl": True})]
        assert not ready.done()

        client.event("session_start")
        await ready
        await client.stop()

    asyncio.run(scenario())
    assert "not-a-real-password" not in caplog.text


def test_connect_and_wait_remains_pending_until_session_initialization_finishes(tmp_path):
    async def scenario():
        state = RoomState(tmp_path / "rooms.json")
        state.add(ROOM)
        client = HermesXmppClient(
            XmppClientConfig(BOT_JID, "not-a-real-password", "Hermes", state),
            lambda event: None,
            lambda event: None,
        )
        join_started = asyncio.Event()
        release_join = asyncio.Event()

        def connect(host=None, port=None):
            tcp_connected = asyncio.get_running_loop().create_future()
            tcp_connected.set_result(None)
            return tcp_connected

        async def join_room(room_jid):
            assert room_jid == ROOM
            join_started.set()
            await release_join.wait()

        client.connect = connect
        client.send_presence = lambda: None
        client.get_roster = lambda: None
        client.join_room = join_room

        ready = asyncio.create_task(client.connect_and_wait())
        await asyncio.sleep(0)
        client.event("session_start")
        await join_started.wait()

        assert not ready.done()

        release_join.set()
        await ready
        await client.stop()

    asyncio.run(scenario())


def test_per_candidate_connection_failure_does_not_abort_fallback_connect_loop(tmp_path):
    async def scenario():
        client = make_client(tmp_path)
        connection_attempt = asyncio.get_running_loop().create_future()

        def connect(host=None, port=None):
            client._current_connection_attempt = connection_attempt
            client.event("connection_failed", OSError("first address refused"))
            return connection_attempt

        client.connect = connect
        client.send_presence = lambda: None
        client.get_roster = lambda: None

        ready = asyncio.create_task(client.connect_and_wait())
        await asyncio.sleep(0)

        assert not ready.done()
        assert not connection_attempt.cancelled()

        connection_attempt.set_result(None)
        client.event("session_start")
        await ready
        await client.stop()

    asyncio.run(scenario())


def test_connect_and_wait_fails_when_connect_loop_exhausts(tmp_path):
    async def scenario():
        client = make_client(tmp_path)

        def connect(host=None, port=None):
            exhausted = asyncio.get_running_loop().create_future()
            exhausted.set_exception(OSError("all connection candidates exhausted"))
            return exhausted

        client.connect = connect

        with pytest.raises(ConnectionError, match="all connection candidates exhausted"):
            await asyncio.wait_for(client.connect_and_wait(), timeout=0.1)

        await client.stop()

    asyncio.run(scenario())


def test_connect_and_wait_observes_rescheduled_attempt_future_chain(tmp_path):
    async def scenario():
        client = make_client(tmp_path)
        outer_attempt = asyncio.get_running_loop().create_future()
        rescheduled_attempt = asyncio.get_running_loop().create_future()
        client.connect = lambda host=None, port=None: outer_attempt

        ready = asyncio.create_task(client.connect_and_wait())
        await asyncio.sleep(0)
        outer_attempt.set_result(rescheduled_attempt)
        await asyncio.sleep(0)

        assert not ready.done()

        rescheduled_attempt.set_exception(OSError("rescheduled attempt terminated"))
        with pytest.raises(ConnectionError, match="rescheduled attempt terminated"):
            await asyncio.wait_for(ready, timeout=0.1)

        await client.stop()

    asyncio.run(scenario())


def test_session_readiness_wins_over_late_rescheduled_attempt_failure(tmp_path):
    async def scenario():
        client = make_client(tmp_path)
        outer_attempt = asyncio.get_running_loop().create_future()
        rescheduled_attempt = asyncio.get_running_loop().create_future()
        client.connect = lambda host=None, port=None: outer_attempt
        client.send_presence = lambda: None
        client.get_roster = lambda: None

        ready = asyncio.create_task(client.connect_and_wait())
        await asyncio.sleep(0)
        outer_attempt.set_result(rescheduled_attempt)
        await asyncio.sleep(0)

        rescheduled_attempt.set_exception(OSError("concurrent attempt failure"))
        await client.event_async("session_start")
        await asyncio.wait_for(ready, timeout=0.1)

        await asyncio.sleep(0)
        assert rescheduled_attempt.exception().args == ("concurrent attempt failure",)

        await client.stop()

    asyncio.run(scenario())


def test_unexpected_connection_lost_schedules_exact_reconnect_sequence_and_resets_after_session(tmp_path):
    async def scenario():
        client = make_client(tmp_path)
        delays = []
        reconnects = []

        async def controlled_sleep(delay):
            delays.append(delay)

        def connect(host=None, port=None):
            reconnects.append((host, port))
            completed = asyncio.get_running_loop().create_future()
            completed.set_result(None)
            return completed

        client._sleep = controlled_sleep
        client.connect = connect
        client.send_presence = lambda: None
        client.get_roster = lambda: None

        for expected_delay in (1, 2, 5, 10, 30, 60, 60):
            client.connection_lost(ConnectionError("link dropped"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert delays[-1] == expected_delay

        assert len(reconnects) == 7

        await client.event_async("session_start")
        client.connection_lost(ConnectionError("link dropped again"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert delays == [1, 2, 5, 10, 30, 60, 60, 1]
        assert len(reconnects) == 8
        await client.stop()

    asyncio.run(scenario())


def test_direct_tls_reconnect_passes_use_ssl_to_real_connect_call(tmp_path):
    async def scenario():
        client = HermesXmppClient(
            XmppClientConfig(
                BOT_JID,
                "not-a-real-password",
                "Hermes",
                RoomState(tmp_path / "rooms.json"),
                "xmpp.example.test",
                5223,
                True,
            ),
            lambda event: None,
            lambda event: None,
        )
        reconnects = []
        client._sleep = lambda delay: asyncio.sleep(0)

        def connect(host=None, port=None, **kwargs):
            reconnects.append((host, port, kwargs))
            completed = asyncio.get_running_loop().create_future()
            completed.set_result(None)
            return completed

        client.connect = connect

        await client._reconnect_after_delay()

        assert reconnects == [("xmpp.example.test", 5223, {"use_ssl": True})]
        await client.stop()

    asyncio.run(scenario())


def test_stop_cancels_pending_reconnect_and_intentional_disconnect_never_retries(tmp_path):
    async def scenario():
        client = make_client(tmp_path)
        delays = []
        reconnects = []
        release_sleep = asyncio.Event()

        async def blocked_sleep(delay):
            delays.append(delay)
            await release_sleep.wait()

        def connect(host=None, port=None):
            reconnects.append((host, port))
            return asyncio.get_running_loop().create_future()

        client._sleep = blocked_sleep
        client.connect = connect

        client.connection_lost(ConnectionError("link dropped"))
        await asyncio.sleep(0)
        assert delays == [1]

        await client.stop()
        release_sleep.set()
        await asyncio.sleep(0)
        client.connection_lost(None)
        await asyncio.sleep(0)

        assert reconnects == []
        assert delays == [1]

    asyncio.run(scenario())
