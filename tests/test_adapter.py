import asyncio
import importlib.util
import logging
import os
import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
PLUGIN_ROOT = ROOT / "hermes-xmpp"
sys.path.insert(0, str(PLUGIN_ROOT))


# Hermes is intentionally absent from the local test environment.  Install a
# faithful public-contract shim before adapter.py imports gateway.*.
gateway = types.ModuleType("gateway")
gateway.__path__ = []
gateway_config = types.ModuleType("gateway.config")
gateway_platforms = types.ModuleType("gateway.platforms")
gateway_platforms.__path__ = []
gateway_base = types.ModuleType("gateway.platforms.base")


class Platform(str):
    pass


@dataclass
class PlatformConfig:
    enabled: bool = True
    extra: dict = field(default_factory=dict)


class MessageType(Enum):
    TEXT = "text"


@dataclass
class SessionSource:
    platform: Platform
    chat_id: str
    chat_name: str | None
    chat_type: str
    user_id: str | None
    user_name: str | None
    thread_id: str | None


@dataclass
class MessageEvent:
    text: str
    message_type: MessageType = MessageType.TEXT
    user_id: str | None = None
    user_name: str | None = None
    source: SessionSource | None = None
    raw_message: object = None
    message_id: str | None = None
    reply_to_message_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None
    raw_response: object = None
    continuation_message_ids: tuple = ()


class BasePlatformAdapter:
    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self.connected_marks = 0
        self.disconnected_marks = 0
        self.events = []

    def _mark_connected(self):
        self.connected_marks += 1

    def _mark_disconnected(self):
        self.disconnected_marks += 1

    def build_source(self, *, chat_id, chat_name=None, chat_type="dm", user_id=None, user_name=None, thread_id=None):
        return SessionSource(self.platform, str(chat_id), chat_name, chat_type, user_id, user_name, thread_id)

    async def handle_message(self, event):
        self.events.append(event)


gateway_config.Platform = Platform
gateway_config.PlatformConfig = PlatformConfig
gateway_base.BasePlatformAdapter = BasePlatformAdapter
gateway_base.MessageEvent = MessageEvent
gateway_base.MessageType = MessageType
gateway_base.SendResult = SendResult
sys.modules.update(
    {
        "gateway": gateway,
        "gateway.config": gateway_config,
        "gateway.platforms": gateway_platforms,
        "gateway.platforms.base": gateway_base,
    }
)

spec = importlib.util.spec_from_file_location("xmpp_plugin_adapter", PLUGIN_ROOT / "adapter.py")
adapter_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter_module
spec.loader.exec_module(adapter_module)

from xmpp_bridge.models import InboundXmppMessage, XmppInvite
from xmpp_bridge.admin_state import AdminConfig
from xmpp_bridge.commands import CommandResult, RestartGateway


BOT = "bot@example.com/Hermes"
ADMIN = "admin@example.com"
ROOM = "private@conference.example.com"
PASSWORD = "super-secret-password"


class MemoryAdminState:
    def __init__(self, owners=frozenset({ADMIN}), trusted=frozenset()):
        self.config = AdminConfig(frozenset(owners), frozenset(trusted), None, None, None, False)

    def load(self):
        return self.config


class RecordingRouter:
    def __init__(self, result):
        self.result = result
        self.messages = []

    def handle(self, message):
        self.messages.append(message)
        return self.result


class FakeClient:
    instances = []

    def __init__(self, config, on_message, on_invite):
        self.config = config
        self.on_message = on_message
        self.on_invite = on_invite
        self.calls = []
        self.direct_ids = ["direct-1"]
        self.group_ids = ["group-1", "group-2"]
        self.join_error = None
        self.__class__.instances.append(self)

    async def connect_and_wait(self):
        self.calls.append(("connect",))

    async def disconnect(self):
        self.calls.append(("disconnect",))

    async def send_direct(self, jid, body):
        self.calls.append(("send_direct", jid, body))
        return list(self.direct_ids)

    async def send_direct_omemo(self, jid, body):
        self.calls.append(("send_direct_omemo", jid, body))
        return list(self.direct_ids)

    async def send_group(self, jid, body):
        self.calls.append(("send_group", jid, body))
        return list(self.group_ids)

    async def set_typing(self, jid, is_group, active):
        self.calls.append(("typing", jid, is_group, active))

    async def join_room(self, room):
        self.calls.append(("join", room))
        if self.join_error:
            raise self.join_error


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    for name in (
        "XMPP_JID",
        "XMPP_PASSWORD",
        "XMPP_ALLOWED_USERS",
        "XMPP_HOST",
        "XMPP_PORT",
        "XMPP_NICK",
        "XMPP_STATE_PATH",
        "HERMES_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XMPP_JID", BOT)
    monkeypatch.setenv("XMPP_PASSWORD", PASSWORD)
    monkeypatch.setenv("XMPP_ALLOWED_USERS", ADMIN)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    FakeClient.instances.clear()


def make_adapter(**kwargs):
    kwargs.setdefault("admin_state", MemoryAdminState())
    return adapter_module.XmppPlatformAdapter(PlatformConfig(), client_factory=FakeClient, **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_contract_connect_disconnect_marks_and_uses_verified_client_config():
    adapter = make_adapter()
    client = FakeClient.instances[-1]

    assert run(adapter.connect()) is True
    run(adapter.disconnect())

    assert client.calls == [
        ("connect",),
        ("send_direct", ADMIN, adapter_module._WELCOME_TEXT),
        ("disconnect",),
    ]
    assert adapter.connected_marks == 1
    assert adapter.disconnected_marks == 1
    assert client.config.bot_jid == BOT
    assert client.config.password == PASSWORD
    assert client.config.nick == "Hermes"
    assert client.config.host is None and client.config.port is None
    assert client.config.direct_tls is False
    assert client.config.room_state.path == Path(os.environ["HERMES_HOME"]) / "xmpp" / "rooms.json"


def test_contract_send_selects_dm_or_muc_and_returns_all_stanza_ids():
    adapter = make_adapter()
    client = FakeClient.instances[-1]
    adapter.room_state.add(ROOM)

    direct = run(adapter.send("Admin@Example.Com/phone", "hello"))
    group = run(adapter.send(ROOM, "group hello"))

    assert client.calls == [
        ("send_direct", ADMIN, "hello"),
        ("send_group", ROOM, "group hello"),
    ]
    assert direct == SendResult(success=True, message_id="direct-1")
    assert group == SendResult(
        success=True,
        message_id="group-2",
        continuation_message_ids=("group-1",),
    )


def test_encrypted_dm_marks_recipient_so_the_following_response_is_encrypted():
    adapter = make_adapter()
    client = FakeClient.instances[-1]

    run(adapter._dispatch_message(InboundXmppMessage("encrypted-1", BOT, ADMIN, "Admin", "question", False, None, encrypted=True)))
    result = run(adapter.send(ADMIN, "answer"))

    assert adapter.events[0].metadata["xmpp_omemo"] is True
    assert client.calls == [("send_direct_omemo", ADMIN, "answer")]
    assert result == SendResult(success=True, message_id="direct-1")


def test_contract_forbidden_dm_and_muc_never_reach_handle_message():
    adapter = make_adapter()

    async def scenario():
        await adapter._dispatch_message(InboundXmppMessage("d1", BOT, "intruder@example.com", "Intruder", "hi", False, None))
        await adapter._dispatch_message(InboundXmppMessage("g1", ROOM, "intruder@example.com", "Intruder", "Hermes: hi", True, None))
        await adapter._dispatch_message(InboundXmppMessage("g2", ROOM, "not-a-verifiable-jid", "Admin", "Hermes: hi", True, None))

    run(scenario())
    assert adapter.events == []


def test_dm_command_reply_bypasses_failing_hermes_and_calls_supervisor_after_send():
    ordering = []
    router = RecordingRouter(CommandResult(True, "ready", RestartGateway()))
    adapter = make_adapter(command_router=router, supervisor=lambda event: ordering.append(("control", event)))
    client = FakeClient.instances[-1]
    original_send = client.send_direct

    async def send_direct(jid, body):
        ordering.append(("reply", jid, body))
        return await original_send(jid, body)

    client.send_direct = send_direct

    async def exploding_hermes(_event):
        raise AssertionError("command must not create Hermes MessageEvent")

    adapter.handle_message = exploding_hermes
    run(adapter._dispatch_message(InboundXmppMessage("command-1", BOT, ADMIN, "Admin", "ping", False, None)))

    assert router.messages and adapter.events == []
    assert ordering == [("reply", ADMIN, "ready"), ("control", RestartGateway())]


@pytest.mark.parametrize(("body", "expected"), [("  PiNg  ", "pong"), ("/status", "model=")])
def test_real_admin_router_works_without_provider_or_hermes_dispatch(body, expected):
    adapter = make_adapter(command_router=None)

    async def exploding_hermes(_event):
        raise AssertionError("admin command must not create Hermes MessageEvent")

    adapter.handle_message = exploding_hermes
    run(adapter._dispatch_message(InboundXmppMessage("admin-1", BOT, ADMIN, "Admin", body, False, None)))

    call = FakeClient.instances[-1].calls[0]
    assert call[:2] == ("send_direct", ADMIN)
    assert expected in call[2]
    assert adapter.events == []


@pytest.mark.parametrize("result", [object(), CommandResult(True, None, RestartGateway())])
def test_invalid_or_replyless_command_result_fails_closed(result):
    controls = []
    adapter = make_adapter(
        command_router=RecordingRouter(result),
        supervisor=lambda event: controls.append(event),
    )

    run(adapter._dispatch_message(InboundXmppMessage("invalid-result", BOT, ADMIN, "Admin", "/restart", False, None)))

    assert adapter.events == []
    assert FakeClient.instances[-1].calls == []
    assert controls == []


def test_failed_command_reply_never_emits_control_event():
    controls = []
    adapter = make_adapter(
        command_router=RecordingRouter(CommandResult(True, "reply", RestartGateway())),
        supervisor=lambda event: controls.append(event),
    )

    async def failed_send(*_args, **_kwargs):
        return SendResult(False, error="delivery failed")

    adapter.send = failed_send
    run(adapter._dispatch_message(InboundXmppMessage("failed-reply", BOT, ADMIN, "Admin", "/restart", False, None)))

    assert controls == []
    assert adapter.events == []


def test_foreign_control_event_never_reaches_supervisor():
    controls = []
    adapter = make_adapter(
        command_router=RecordingRouter(CommandResult(True, "reply", object())),
        supervisor=lambda event: controls.append(event),
    )

    run(adapter._dispatch_message(InboundXmppMessage("foreign-control", BOT, ADMIN, "Admin", "/restart", False, None)))

    assert FakeClient.instances[-1].calls == [("send_direct", ADMIN, "reply")]
    assert controls == []


def test_live_admin_snapshot_authorizes_next_dm_muc_and_invite_without_static_overwrite():
    state = MemoryAdminState()
    router = RecordingRouter(CommandResult(False))
    adapter = make_adapter(admin_state=state, command_router=router)
    client = FakeClient.instances[-1]
    state.config = AdminConfig(frozenset({ADMIN}), frozenset({"live@example.com"}), None, None, None, False, 1)

    async def scenario():
        await adapter._dispatch_message(InboundXmppMessage("live-dm", BOT, "live@example.com", "Live", "question", False, None))
        await adapter._dispatch_message(InboundXmppMessage("live-muc", "any@conference.example.com", "live@example.com", "Live", "Hermes: question", True, None))
        await adapter._dispatch_message(InboundXmppMessage("anon", "any@conference.example.com", "not-a-jid", "Anon", "Hermes: denied", True, None))
        await adapter._accept_invite(XmppInvite("new@conference.example.com", "live@example.com", True))

    run(scenario())
    assert [event.message_id for event in adapter.events] == ["live-dm", "live-muc"]
    assert client.calls[-1] == ("join", "new@conference.example.com")
    assert adapter.room_state.load() == frozenset({"new@conference.example.com"})


def test_static_allowlist_seeds_new_admin_state_once_without_restoring_removed_member(monkeypatch):
    persisted = {}

    class SeedState(MemoryAdminState):
        def __init__(self, path, first_owner):
            self.path = path
            super().__init__(frozenset({first_owner}))

        def load(self):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
            self.config = persisted.setdefault(self.path, self.config)
            return self.config

        def mutate(self, transform):
            self.config = transform(self.config)
            persisted[self.path] = self.config
            return self.config

    monkeypatch.setattr(adapter_module, "AdminState", SeedState)
    monkeypatch.setenv("XMPP_ALLOWED_USERS", "admin@example.com,legacy@example.com")
    first = adapter_module.XmppPlatformAdapter(PlatformConfig(), client_factory=FakeClient)
    first._snapshot()
    assert first.admin_state.config.trusted_jids == frozenset({"legacy@example.com"})
    first.admin_state.config = AdminConfig(frozenset({ADMIN}), frozenset(), None, None, None, False, 2)
    persisted[first.admin_state.path] = first.admin_state.config
    second = adapter_module.XmppPlatformAdapter(PlatformConfig(), client_factory=FakeClient)
    assert second._snapshot().trusted_jids == frozenset()


def test_two_adapters_created_before_bootstrap_cannot_reseed_deleted_trusted_jid(monkeypatch):
    persisted = {}

    class RacingSeedState(MemoryAdminState):
        def __init__(self, path, first_owner):
            self.path = path
            super().__init__(frozenset({first_owner}))

        def load(self):
            self.config = persisted.setdefault(self.path, self.config)
            return self.config

        def mutate(self, transform):
            current = persisted.setdefault(self.path, self.config)
            proposed = transform(current)
            self.config = AdminConfig(
                proposed.owners,
                proposed.trusted_jids,
                proposed.model,
                proposed.endpoint,
                proposed.token_mask,
                proposed.token_present,
                current.revision + 1,
            )
            persisted[self.path] = self.config
            return self.config

    monkeypatch.setattr(adapter_module, "AdminState", RacingSeedState)
    monkeypatch.setenv("XMPP_ALLOWED_USERS", "admin@example.com,legacy@example.com")
    first = adapter_module.XmppPlatformAdapter(PlatformConfig(), client_factory=FakeClient)
    delayed = adapter_module.XmppPlatformAdapter(PlatformConfig(), client_factory=FakeClient)

    assert first._snapshot().trusted_jids == frozenset({"legacy@example.com"})
    first.admin_state.mutate(lambda current: current.with_changes(trusted_jids=frozenset()))

    assert delayed._snapshot().trusted_jids == frozenset()


@pytest.mark.parametrize(
    ("missing", "value"),
    [
        ("XMPP_JID", ""),
        ("XMPP_PASSWORD", ""),
        ("XMPP_ALLOWED_USERS", ""),
        ("XMPP_ALLOWED_USERS", " , "),
    ],
)
def test_config_requires_jid_password_and_nonempty_allowlist(monkeypatch, missing, value):
    monkeypatch.setenv(missing, value)
    assert adapter_module.validate_config(PlatformConfig()) is False
    with pytest.raises(ValueError):
        make_adapter()


def test_config_direct_tls_defaults_port_only_with_explicit_host(monkeypatch, tmp_path):
    monkeypatch.setenv("XMPP_HOST", "xmpp.example.test")
    direct = make_adapter()
    assert FakeClient.instances[-1].config.host == "xmpp.example.test"
    assert FakeClient.instances[-1].config.port == 5223
    assert FakeClient.instances[-1].config.direct_tls is True

    monkeypatch.setenv("XMPP_PORT", "7443")
    explicit = make_adapter()
    assert FakeClient.instances[-1].config.port == 7443

    monkeypatch.delenv("XMPP_HOST")
    monkeypatch.delenv("XMPP_PORT")
    monkeypatch.setenv("XMPP_NICK", " Relay ")
    monkeypatch.setenv("XMPP_STATE_PATH", str(tmp_path / "custom" / "rooms.json"))
    discovery = make_adapter()
    cfg = FakeClient.instances[-1].config
    assert cfg.host is None and cfg.port is None
    assert cfg.direct_tls is False
    assert cfg.nick == "Relay"
    assert discovery.room_state.path == tmp_path / "custom" / "rooms.json"


@pytest.mark.parametrize("port", ["0", "65536", "not-an-int"])
def test_config_rejects_invalid_direct_tls_port(monkeypatch, port):
    monkeypatch.setenv("XMPP_HOST", "xmpp.example.test")
    monkeypatch.setenv("XMPP_PORT", port)
    assert adapter_module.validate_config(PlatformConfig()) is False
    with pytest.raises(ValueError):
        make_adapter()


def test_config_rejects_port_without_direct_host(monkeypatch):
    monkeypatch.setenv("XMPP_PORT", "5223")
    assert adapter_module.validate_config(PlatformConfig()) is False
    with pytest.raises(ValueError):
        make_adapter()


def test_register_uses_official_platform_contract(monkeypatch):
    calls = []
    ctx = types.SimpleNamespace(register_platform=lambda **kwargs: calls.append(kwargs))

    adapter_module.register(ctx)

    assert len(calls) == 1
    registered = calls[0]
    assert {
        "name": registered["name"],
        "label": registered["label"],
        "required_env": registered["required_env"],
        "allowed_users_env": registered["allowed_users_env"],
        "max_message_length": registered["max_message_length"],
    } == {
        "name": "xmpp",
        "label": "XMPP",
        "required_env": ["XMPP_JID", "XMPP_PASSWORD", "XMPP_ALLOWED_USERS"],
        "allowed_users_env": "XMPP_ALLOWED_USERS",
        "max_message_length": 3500,
    }
    assert registered["check_fn"]() is True
    assert registered["validate_config"](PlatformConfig()) is True
    seed = registered["env_enablement_fn"]()
    assert seed == {"jid": BOT, "password": PASSWORD, "allowed_users": ADMIN}
    assert isinstance(registered["adapter_factory"](PlatformConfig()), adapter_module.XmppPlatformAdapter)

    for name in ("XMPP_JID", "XMPP_PASSWORD", "XMPP_ALLOWED_USERS"):
        monkeypatch.delenv(name)
    assert registered["check_fn"]() is True
    assert registered["validate_config"](PlatformConfig()) is False


def test_group_routing_uses_actual_room_nick_after_collision():
    adapter = make_adapter()

    async def scenario():
        await adapter._dispatch_message(
            InboundXmppMessage(
                "collision-1", ROOM, ADMIN, "Admin", "Hermes_2: actual nick", True, None, "Hermes_2"
            )
        )
        await adapter._dispatch_message(
            InboundXmppMessage(
                "collision-2", ROOM, ADMIN, "Admin", "Hermes: configured nick", True, None, "Hermes_2"
            )
        )

    run(scenario())

    assert [(event.message_id, event.text, event.user_id) for event in adapter.events] == [
        ("collision-1", "actual nick", ADMIN)
    ]


def test_dm_and_muc_events_have_exact_hermes_source_and_secret_free_fields():
    adapter = make_adapter()
    adapter.room_state.add(ROOM)

    async def scenario():
        await adapter._dispatch_message(InboundXmppMessage("dm-7", BOT, ADMIN, "Admin", "  direct  ", False, "older"))
        await adapter._dispatch_message(InboundXmppMessage("muc-7", ROOM, ADMIN, "Admin", "Hermes: group", True, None))

    run(scenario())

    dm, muc = adapter.events
    assert (dm.text, dm.message_type, dm.user_id, dm.user_name, dm.message_id, dm.reply_to_message_id) == (
        "direct", MessageType.TEXT, ADMIN, "Admin", "dm-7", "older"
    )
    assert dm.source == SessionSource(Platform("xmpp"), ADMIN, "Admin", "dm", ADMIN, "Admin", None)
    assert dm.metadata == {"xmpp_chat_jid": BOT, "xmpp_is_group": False, "xmpp_omemo": False}
    assert dm.raw_message == {
        "message_id": "dm-7",
        "chat_jid": BOT,
        "sender_jid": ADMIN,
        "sender_nick": "Admin",
        "is_group": False,
        "reply_to_id": "older",
    }
    assert (muc.text, muc.source.chat_id, muc.source.chat_name, muc.source.chat_type) == (
        "group", ROOM, ROOM, "group"
    )
    assert muc.metadata == {"xmpp_chat_jid": ROOM, "xmpp_is_group": True, "xmpp_omemo": False}
    assert PASSWORD not in repr((dm.metadata, dm.raw_message, muc.metadata, muc.raw_message))


def test_group_requires_mention_or_reply_to_cached_bot_id_in_same_room():
    adapter = make_adapter()
    adapter.room_state.add(ROOM)
    other = "other@conference.example.com"
    adapter.room_state.add(other)
    client = FakeClient.instances[-1]
    client.group_ids = ["same-id"]
    run(adapter.send(ROOM, "answer"))

    async def scenario():
        await adapter._dispatch_message(InboundXmppMessage("a", ROOM, ADMIN, "Admin", "plain", True, None))
        await adapter._dispatch_message(InboundXmppMessage("b", ROOM, ADMIN, "Admin", "plain reply", True, "same-id"))
        await adapter._dispatch_message(InboundXmppMessage("c", other, ADMIN, "Admin", "cross-room", True, "same-id"))

    run(scenario())
    assert [event.text for event in adapter.events] == ["plain reply"]


def test_invite_is_authorized_persisted_before_join_and_retained_on_failure(caplog):
    adapter = make_adapter()
    client = FakeClient.instances[-1]
    client.join_error = RuntimeError(f"must not log {PASSWORD}")
    ordering = []
    real_add = adapter.room_state.add

    def add(room):
        ordering.append(("persist", room))
        return real_add(room)

    adapter.room_state.add = add

    async def scenario():
        with caplog.at_level(logging.ERROR):
            await adapter._accept_invite(XmppInvite(ROOM.upper(), "Admin@Example.Com/phone", True))
            await adapter._accept_invite(XmppInvite("denied@conference.example.com", "intruder@example.com", True))

    run(scenario())
    ordering.extend(client.calls)
    assert ordering == [("persist", ROOM), ("join", ROOM)]
    assert adapter.room_state.load() == frozenset({ROOM})
    assert ROOM in caplog.text and "RuntimeError" in caplog.text
    assert PASSWORD not in caplog.text and "must not log" not in caplog.text


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_inbound_dedup_ttl_capacity_and_cross_chat_keys():
    clock = Clock()
    adapter = make_adapter(monotonic=clock, cache_capacity=2)

    async def scenario():
        message = lambda mid, chat=BOT: InboundXmppMessage(mid, chat, ADMIN, "Admin", "hello", False, None)
        await adapter._dispatch_message(message("same"))
        await adapter._dispatch_message(message("same"))
        await adapter._dispatch_message(message("same", "bot2@example.com"))
        await adapter._dispatch_message(message("second"))
        await adapter._dispatch_message(message("same"))  # evicted by capacity
        clock.now = 601
        await adapter._dispatch_message(message("same"))  # previous entry expired

    run(scenario())
    assert [event.message_id for event in adapter.events] == ["same", "same", "second", "same", "same"]


def test_outbound_reply_ids_expire_after_24_hours():
    clock = Clock()
    adapter = make_adapter(monotonic=clock)
    adapter.room_state.add(ROOM)
    client = FakeClient.instances[-1]
    client.group_ids = ["reply-anchor"]
    run(adapter.send(ROOM, "answer"))

    async def scenario():
        await adapter._dispatch_message(InboundXmppMessage("before", ROOM, ADMIN, "Admin", "plain", True, "reply-anchor"))
        clock.now = 86401
        await adapter._dispatch_message(InboundXmppMessage("after", ROOM, ADMIN, "Admin", "plain", True, "reply-anchor"))

    run(scenario())
    assert [event.message_id for event in adapter.events] == ["before"]


def test_send_failure_typing_and_chat_info_contracts():
    adapter = make_adapter()
    adapter.room_state.add(ROOM)
    client = FakeClient.instances[-1]
    client.direct_ids = []

    failed = run(adapter.send(ADMIN, "empty result"))
    run(adapter.send_typing(ADMIN))
    run(adapter.send_typing(ROOM))

    assert failed.success is False and failed.message_id is None
    assert client.calls == [
        ("send_direct", ADMIN, "empty result"),
        ("typing", ADMIN, False, True),
        ("typing", ROOM, True, True),
    ]
    assert run(adapter.get_chat_info(ADMIN)) == {"chat_id": ADMIN, "name": ADMIN, "type": "dm"}
    assert run(adapter.get_chat_info(ROOM)) == {"chat_id": ROOM, "name": ROOM, "type": "group"}
