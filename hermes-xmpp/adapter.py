"""Hermes platform adapter for direct and private-room XMPP messages."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import inspect
import logging
import os
from pathlib import Path
import time
from typing import Callable

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
try:
    from .xmpp_bridge.admin_state import AdminState, AdminStateError, ConfigValidationError
    from .xmpp_bridge.client import HermesXmppClient, XmppClientConfig
    from .xmpp_bridge.commands import CommandRouter, RestartGateway
    from .xmpp_bridge.models import InboundXmppMessage, XmppInvite
    from .xmpp_bridge.policy import normalize_bare_jid, parse_allowlist, route_direct, route_group, snapshot_allowlist
    from .xmpp_bridge.state import RoomState
except ImportError:  # Direct module import used by focused adapter tests.
    from xmpp_bridge.admin_state import AdminState, AdminStateError, ConfigValidationError
    from xmpp_bridge.client import HermesXmppClient, XmppClientConfig
    from xmpp_bridge.commands import CommandRouter, RestartGateway
    from xmpp_bridge.models import InboundXmppMessage, XmppInvite
    from xmpp_bridge.policy import normalize_bare_jid, parse_allowlist, route_direct, route_group, snapshot_allowlist
    from xmpp_bridge.state import RoomState

logger = logging.getLogger(__name__)
_CAPACITY = 4096
_WELCOME_TEXT = "Welcome!\n/status\n/config\n/model set <model>\n/endpoint set <url>\n/token set <token>\n/trust list\n/owner list\n/doctor\n/restart\n\nConfigure AI: set model, endpoint, and token."


class _BootstrapAlreadyChanged(Exception):
    """The admin state has already advanced beyond its initial revision."""


class _TtlCache:
    def __init__(self, ttl, capacity, monotonic):
        self.ttl, self.capacity, self.monotonic = ttl, capacity, monotonic
        self.entries = OrderedDict()

    def _expire(self):
        now = self.monotonic()
        for key in [key for key, deadline in self.entries.items() if deadline <= now]:
            self.entries.pop(key, None)

    def contains(self, key):
        self._expire()
        return key in self.entries

    def add(self, key):
        self._expire()
        self.entries.pop(key, None)
        self.entries[key] = self.monotonic() + self.ttl
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)

    def ids_for(self, chat):
        self._expire()
        return frozenset(mid for (key_chat, mid) in self.entries if key_chat == chat)


def _settings():
    raw_jid = os.getenv("XMPP_JID", "").strip()
    password = os.getenv("XMPP_PASSWORD", "")
    allowed_raw = os.getenv("XMPP_ALLOWED_USERS", "")
    allowed = parse_allowlist(allowed_raw)
    if not raw_jid or not password or not allowed:
        raise ValueError("XMPP_JID, XMPP_PASSWORD and XMPP_ALLOWED_USERS are required")
    nick = os.getenv("XMPP_NICK", "Hermes").strip() or "Hermes"
    bare = normalize_bare_jid(raw_jid)
    jid = raw_jid if "/" in raw_jid else f"{bare}/{nick}"
    host = os.getenv("XMPP_HOST", "").strip() or None
    raw_port = os.getenv("XMPP_PORT", "").strip()
    if raw_port and host is None:
        raise ValueError("XMPP_PORT requires XMPP_HOST")
    try:
        port = int(raw_port) if raw_port else (5223 if host else None)
    except ValueError as exc:
        raise ValueError("invalid XMPP_PORT") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid XMPP_PORT")
    state_value = os.getenv("XMPP_STATE_PATH", "").strip()
    home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    state_path = Path(state_value) if state_value else home / "xmpp" / "rooms.json"
    return jid, password, allowed, nick, host, port, state_path


def validate_config(config):
    try:
        _settings()
    except (TypeError, ValueError):
        return False
    return True


def check_requirements():
    try:
        import slixmpp  # noqa: F401
    except ImportError:
        return False
    return True


def _env_enablement():
    if not validate_config(PlatformConfig()):
        return None
    return {"jid": os.environ["XMPP_JID"].strip(), "password": os.environ["XMPP_PASSWORD"],
            "allowed_users": os.environ["XMPP_ALLOWED_USERS"].strip()}


class XmppPlatformAdapter(BasePlatformAdapter):
    splits_long_messages = True

    def __init__(self, config, *, client_factory=HermesXmppClient,
                 monotonic: Callable[[], float] = time.monotonic, cache_capacity=_CAPACITY,
                 admin_state=None, command_router=None, supervisor=None):
        super().__init__(config, Platform("xmpp"))
        jid, password, self.allowed_users, self.nick, host, port, state_path = _settings()
        self.bot_jid = normalize_bare_jid(jid)
        self.room_state = RoomState(state_path)
        self._welcome_marker = state_path.with_name("welcome.sent")
        admin_path = Path(os.getenv("XMPP_ADMIN_STATE_PATH", "").strip() or state_path.with_name("admin.json"))
        self._seed_admin_state = admin_state is None and not admin_path.exists()
        first_owner = sorted(self.allowed_users)[0]
        self.admin_state = admin_state or AdminState(admin_path, first_owner)
        self.command_router = command_router or CommandRouter(self.admin_state)
        self._supervisor = supervisor
        self._inbound = _TtlCache(600, cache_capacity, monotonic)
        self._outbound = _TtlCache(86400, cache_capacity, monotonic)
        self._omemo_recipients = _TtlCache(600, cache_capacity, monotonic)
        client_config = XmppClientConfig(jid, password, self.nick, self.room_state, host, port, host is not None, omemo_enabled=True)
        self.client = client_factory(client_config, self._schedule_message, self._schedule_invite)

    def _schedule_message(self, message):
        return asyncio.get_running_loop().create_task(self._dispatch_message(message))

    def _schedule_invite(self, invite):
        asyncio.get_running_loop().create_task(self._accept_invite(invite))

    async def connect(self, *, is_reconnect=False):
        await self.client.connect_and_wait()
        self._mark_connected()
        if not is_reconnect:
            await self._send_first_owner_welcome()
        return True

    async def _send_first_owner_welcome(self):
        if self._welcome_marker.exists():
            return
        try:
            owner = sorted(self._snapshot().owners)[0]
        except (AdminStateError, ConfigValidationError, IndexError, OSError, ValueError):
            return
        if not (await self.send(owner, _WELCOME_TEXT)).success:
            return
        try:
            self._welcome_marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._welcome_marker.open("x", encoding="utf-8") as marker:
                marker.write("1\n")
        except FileExistsError:
            return
        except OSError:
            logger.warning("Could not persist XMPP welcome marker")

    async def disconnect(self):
        await self.client.disconnect()
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        target = normalize_bare_jid(str(chat_id))
        is_group = target in self.room_state.load()
        try:
            ids = await (self.client.send_group(target, content) if is_group
                         else self.client.send_direct_omemo(target, content) if self._omemo_recipients.contains(target)
                         else self.client.send_direct(target, content))
        except Exception as exc:
            return SendResult(success=False, error=exc.__class__.__name__)
        if not ids:
            return SendResult(success=False, error="XMPP send returned no stanza ID")
        for message_id in ids:
            self._outbound.add((target, message_id))
        return SendResult(success=True, message_id=ids[-1], continuation_message_ids=tuple(ids[:-1]))

    async def send_typing(self, chat_id, *args, **kwargs):
        target = normalize_bare_jid(str(chat_id))
        await self.client.set_typing(target, target in self.room_state.load(), True)

    async def get_chat_info(self, chat_id):
        target = normalize_bare_jid(str(chat_id))
        kind = "group" if target in self.room_state.load() else "dm"
        return {"chat_id": target, "name": target, "type": kind}

    async def _dispatch_message(self, message: InboundXmppMessage):
        try:
            chat = normalize_bare_jid(message.chat_jid)
        except ValueError:
            return
        key = (chat, message.message_id)
        if not message.message_id or self._inbound.contains(key):
            return
        if message.encrypted and not message.is_group:
            try:
                self._omemo_recipients.add(normalize_bare_jid(message.sender_jid))
            except ValueError:
                return
        if not message.is_group:
            try:
                self._snapshot()
            except (AdminStateError, ConfigValidationError, OSError, ValueError):
                return
            try:
                command = self.command_router.handle(message)
                handled = command.handled
                reply = command.reply
                control_event = command.control_event
                if type(handled) is not bool or (handled and not isinstance(reply, str)):
                    return
            except Exception:
                self._inbound.add(key)
                return
            if handled:
                self._inbound.add(key)
                delivered = bool(reply) and (await self.send(message.sender_jid, reply)).success
                if delivered and control_event is not None:
                    await self._emit_control(control_event)
                return
        try:
            allowed_users = snapshot_allowlist(self._snapshot())
        except (AdminStateError, ConfigValidationError, OSError, ValueError):
            return
        room_nick = message.room_nick or self.nick
        routed = (route_group(message, allowed_users, self.bot_jid, room_nick, self._outbound.ids_for(chat))
                  if message.is_group else route_direct(message, allowed_users))
        if routed is None:
            return
        self._inbound.add(key)
        sender = normalize_bare_jid(message.sender_jid)
        event_chat = chat if message.is_group else sender
        source = self.build_source(chat_id=event_chat, chat_name=chat if message.is_group else message.sender_nick,
                                   chat_type="group" if message.is_group else "dm", user_id=sender,
                                   user_name=message.sender_nick, thread_id=None)
        routing_chat = message.chat_jid
        raw = {"message_id": message.message_id, "chat_jid": routing_chat, "sender_jid": sender,
               "sender_nick": message.sender_nick, "is_group": message.is_group,
               "reply_to_id": message.reply_to_id}
        event = MessageEvent(text=routed.body, message_type=MessageType.TEXT, user_id=sender,
                             user_name=message.sender_nick, source=source, raw_message=raw,
                             message_id=message.message_id, reply_to_message_id=message.reply_to_id,
                             metadata={"xmpp_chat_jid": routing_chat, "xmpp_is_group": message.is_group,
                                       "xmpp_omemo": message.encrypted})
        await self.handle_message(event)

    def _snapshot(self):
        snapshot = self.admin_state.load()
        if self._seed_admin_state:
            def seed_initial(current):
                if current.revision != 0:
                    raise _BootstrapAlreadyChanged
                return current.with_changes(
                    trusted_jids=current.trusted_jids | (self.allowed_users - current.owners),
                )

            try:
                snapshot = self.admin_state.mutate(seed_initial)
            except _BootstrapAlreadyChanged:
                snapshot = self.admin_state.load()
            self._seed_admin_state = False
        return snapshot

    async def _emit_control(self, event) -> None:
        if self._supervisor is None or not isinstance(event, RestartGateway):
            return
        try:
            result = self._supervisor(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.error("XMPP supervisor callback failed: %s", exc.__class__.__name__)

    async def _accept_invite(self, invite: XmppInvite):
        try:
            inviter = normalize_bare_jid(invite.inviter_jid)
            room = normalize_bare_jid(invite.room_jid)
        except ValueError:
            return
        try:
            allowed_users = snapshot_allowlist(self._snapshot())
        except (AdminStateError, ConfigValidationError, OSError, ValueError):
            return
        if inviter not in allowed_users:
            return
        self.room_state.add(room)
        try:
            await self.client.join_room(room)
        except Exception as exc:
            logger.error("XMPP room join failed for %s: %s", room, exc.__class__.__name__)


def register(ctx):
    ctx.register_platform(name="xmpp", label="XMPP", adapter_factory=lambda cfg: XmppPlatformAdapter(cfg),
                          check_fn=check_requirements, validate_config=validate_config,
                          required_env=["XMPP_JID", "XMPP_PASSWORD", "XMPP_ALLOWED_USERS"],
                          env_enablement_fn=_env_enablement, allowed_users_env="XMPP_ALLOWED_USERS",
                          max_message_length=3500, emoji="💬")
