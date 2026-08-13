"""Hermes platform adapter for direct and private-room XMPP messages."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import os
from pathlib import Path
import time
from typing import Callable

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from xmpp_bridge.client import HermesXmppClient, XmppClientConfig
from xmpp_bridge.models import InboundXmppMessage, XmppInvite
from xmpp_bridge.policy import normalize_bare_jid, parse_allowlist, route_direct, route_group
from xmpp_bridge.state import RoomState

logger = logging.getLogger(__name__)
_CAPACITY = 4096


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
    return validate_config(PlatformConfig())


def _env_enablement():
    if not validate_config(PlatformConfig()):
        return None
    return {"jid": os.environ["XMPP_JID"].strip(), "password": os.environ["XMPP_PASSWORD"],
            "allowed_users": os.environ["XMPP_ALLOWED_USERS"].strip()}


class XmppPlatformAdapter(BasePlatformAdapter):
    splits_long_messages = True

    def __init__(self, config, *, client_factory=HermesXmppClient,
                 monotonic: Callable[[], float] = time.monotonic, cache_capacity=_CAPACITY):
        super().__init__(config, Platform("xmpp"))
        jid, password, self.allowed_users, self.nick, host, port, state_path = _settings()
        self.bot_jid = normalize_bare_jid(jid)
        self.room_state = RoomState(state_path)
        self._inbound = _TtlCache(600, cache_capacity, monotonic)
        self._outbound = _TtlCache(86400, cache_capacity, monotonic)
        client_config = XmppClientConfig(jid, password, self.nick, self.room_state, host, port)
        self.client = client_factory(client_config, self._schedule_message, self._schedule_invite)

    def _schedule_message(self, message):
        asyncio.get_running_loop().create_task(self._dispatch_message(message))

    def _schedule_invite(self, invite):
        asyncio.get_running_loop().create_task(self._accept_invite(invite))

    async def connect(self, *, is_reconnect=False):
        await self.client.connect_and_wait()
        self._mark_connected()
        return True

    async def disconnect(self):
        await self.client.disconnect()
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        target = normalize_bare_jid(str(chat_id))
        is_group = target in self.room_state.load()
        try:
            ids = await (self.client.send_group(target, content) if is_group
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
        routed = (route_group(message, self.allowed_users, self.bot_jid, self.nick, self._outbound.ids_for(chat))
                  if message.is_group else route_direct(message, self.allowed_users))
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
                             metadata={"xmpp_chat_jid": routing_chat, "xmpp_is_group": message.is_group})
        await self.handle_message(event)

    async def _accept_invite(self, invite: XmppInvite):
        try:
            inviter = normalize_bare_jid(invite.inviter_jid)
            room = normalize_bare_jid(invite.room_jid)
        except ValueError:
            return
        if inviter not in self.allowed_users:
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
