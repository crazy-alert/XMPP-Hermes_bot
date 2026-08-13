"""TLS-safe Slixmpp transport that translates XMPP stanzas into bridge models."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from xml.etree import ElementTree as ET

from slixmpp import ClientXMPP, JID

from .models import InboundXmppMessage, XmppInvite
from .policy import normalize_bare_jid
from .state import RoomState


_DELAY_NAMESPACE = "urn:xmpp:delay"
_REPLY_NAMESPACE = "urn:xmpp:reply:0"


@dataclass(frozen=True)
class XmppClientConfig:
    """Only the connection values needed by the XMPP transport."""

    bot_jid: str
    password: str = field(repr=False)
    nick: str
    room_state: RoomState
    host: str | None = None
    port: int | None = None
    direct_tls: bool = False

    def __post_init__(self) -> None:
        normalize_bare_jid(self.bot_jid)
        if not isinstance(self.password, str) or not self.password:
            raise ValueError("XMPP password must not be empty")
        if not isinstance(self.nick, str) or not self.nick.strip():
            raise ValueError("XMPP nick must not be empty")
        if self.host is not None and (not isinstance(self.host, str) or not self.host.strip()):
            raise ValueError("XMPP host must be nonempty when supplied")
        if self.port is not None and (type(self.port) is not int or not 1 <= self.port <= 65535):
            raise ValueError("XMPP port must be between 1 and 65535")
        if type(self.direct_tls) is not bool:
            raise ValueError("XMPP direct TLS flag must be a boolean")


MessageCallback = Callable[[InboundXmppMessage], object]
InviteCallback = Callable[[XmppInvite], object]


class HermesXmppClient(ClientXMPP):
    """Translate direct, MUC, and invitation stanzas without making policy decisions."""

    RECONNECT_DELAYS = (1, 2, 5, 10, 30, 60)

    def __init__(self, config: XmppClientConfig, on_message: MessageCallback, on_invite: InviteCallback) -> None:
        self.config = config
        self._on_message = on_message
        self._on_invite = on_invite
        self._room_nicks: dict[str, str] = {}
        self._reconnect_index = 0
        self._reconnect_task: asyncio.Task[None] | None = None
        self._intentional_disconnect = False
        self._sleep = asyncio.sleep
        self._session_ready: asyncio.Future[None] | None = None
        super().__init__(config.bot_jid, config.password)

        # Slixmpp uses enable_starttls; retain the explicit policy name too.
        self.disable_starttls = False
        self.enable_starttls = True
        for plugin in ("xep_0030", "xep_0045", "xep_0085", "xep_0198", "xep_0249", "xep_0461"):
            self.register_plugin(plugin)

        self.add_event_handler("session_start", self._session_start)
        self.add_event_handler("message", self._handle_direct_message)
        self.add_event_handler("groupchat_message", self._handle_group_message)
        self.add_event_handler("groupchat_invite", self._handle_mediated_invite)
        self.add_event_handler("groupchat_direct_invite", self._handle_direct_invite)
        self.add_event_handler("groupchat_presence", self._handle_group_presence)

    async def connect_and_wait(self) -> None:
        self._intentional_disconnect = False
        ready = asyncio.get_running_loop().create_future()
        self._session_ready = ready

        def terminal_failure(error: object) -> None:
            if not ready.done():
                ready.set_exception(ConnectionError(str(error)))

        async def observe_connection_attempt(attempt: Awaitable[object]) -> None:
            current = attempt
            while inspect.isawaitable(current):
                try:
                    current = await current
                except asyncio.CancelledError:
                    return
                except BaseException as error:
                    if not ready.done():
                        ready.set_exception(ConnectionError(str(error)))
                    return

        error_events = ("failed_auth", "ssl_invalid_chain")
        for event_name in error_events:
            self.add_event_handler(event_name, terminal_failure)
        try:
            result = self.connect(self.config.host, self.config.port, **self._connect_kwargs())
            if inspect.isawaitable(result):
                attempt_observer = asyncio.create_task(observe_connection_attempt(result))
            else:
                attempt_observer = None
            await ready
        except BaseException:
            self.cancel_connection_attempt()
            raise
        finally:
            if attempt_observer is not None and not attempt_observer.done():
                attempt_observer.cancel()
                with suppress(asyncio.CancelledError):
                    await attempt_observer
            if self._session_ready is ready:
                self._session_ready = None
            for event_name in error_events:
                self.del_event_handler(event_name, terminal_failure)

    async def disconnect(self) -> None:  # type: ignore[override]
        self._intentional_disconnect = True
        retry_task = self._reconnect_task
        self._reconnect_task = None
        if retry_task is not None and not retry_task.done():
            retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await retry_task
        self.cancel_connection_attempt()
        result = super().disconnect()
        if inspect.isawaitable(result):
            await result

    async def stop(self) -> None:
        """Compatibility helper for callers that need an awaitable shutdown."""
        result = self.disconnect()
        if inspect.isawaitable(result):
            await result

    async def join_room(self, room_jid: str) -> None:
        room = normalize_bare_jid(room_jid)
        result = await self["xep_0045"].join_muc_wait(room, self.config.nick, maxstanzas=0)
        actual_nick = JID(result[0]["from"]).resource
        self._room_nicks[room] = actual_nick or self.config.nick

    async def send_direct(self, jid: str, body: str) -> list[str]:
        return self._send_chunks(normalize_bare_jid(jid), body, "chat")

    async def send_group(self, room_jid: str, body: str) -> list[str]:
        return self._send_chunks(normalize_bare_jid(room_jid), body, "groupchat")

    async def set_typing(self, jid: str, is_group: bool, active: bool) -> None:
        message_type = "groupchat" if is_group else "chat"
        stanza = self.make_message(normalize_bare_jid(jid), mtype=message_type)
        stanza["chat_state"] = "composing" if active else "active"
        self.send(stanza)

    async def _session_start(self, _event: object) -> None:
        try:
            self._reconnect_index = 0
            self.send_presence()
            roster = self.get_roster()
            if inspect.isawaitable(roster):
                await roster
            for room in self.config.room_state.load():
                await self.join_room(room)
        except BaseException as error:
            if self._session_ready is not None and not self._session_ready.done():
                self._session_ready.set_exception(error)
            raise
        else:
            if self._session_ready is not None and not self._session_ready.done():
                self._session_ready.set_result(None)

    def _handle_direct_message(self, stanza: object) -> None:
        if not self._is_message(stanza) or stanza["type"] == "groupchat":
            return
        sender = self._bare_or_none(stanza["from"])
        if sender is None or sender == self._bot_bare:
            return
        body = stanza["body"]
        if not isinstance(body, str) or not body:
            return
        message_id = stanza["id"]
        if not isinstance(message_id, str) or not message_id:
            return
        self._emit_message(
            InboundXmppMessage(message_id, self._bot_bare, sender, JID(stanza["from"]).user or "", body, False, self._reply_id(stanza))
        )

    def _handle_group_message(self, stanza: object) -> None:
        if not self._is_message(stanza) or stanza["type"] != "groupchat" or self._has_delay(stanza):
            return
        room = self._bare_or_none(stanza["from"])
        if room is None:
            return
        nick = JID(stanza["from"]).resource or ""
        if nick.casefold() == self._room_nicks.get(room, self.config.nick).casefold():
            return
        body, message_id = stanza["body"], stanza["id"]
        if not isinstance(body, str) or not body or not isinstance(message_id, str) or not message_id:
            return
        sender = self._muc_sender(stanza)
        if sender is None:
            return
        self._emit_message(
            InboundXmppMessage(
                message_id,
                room,
                sender,
                nick,
                body,
                True,
                self._reply_id(stanza),
                self._room_nicks.get(room, self.config.nick),
            )
        )

    def _handle_mediated_invite(self, stanza: object) -> None:
        if not self._is_message(stanza):
            return
        room = self._bare_or_none(stanza["from"])
        invite = stanza.get_plugin("muc", check=True)
        inviter = self._bare_or_none(invite["invite"]["from"]) if invite is not None else None
        if room is not None and inviter is not None:
            self._emit_invite(XmppInvite(room, inviter, False))

    def _handle_direct_invite(self, stanza: object) -> None:
        if not self._is_message(stanza):
            return
        inviter = self._bare_or_none(stanza["from"])
        invite = stanza.get_plugin("groupchat_invite", check=True)
        room = self._bare_or_none(invite["jid"]) if invite is not None else None
        if room is not None and inviter is not None:
            self._emit_invite(XmppInvite(room, inviter, True))

    def _handle_group_presence(self, presence: object) -> None:
        if not hasattr(presence, "get_plugin"):
            return
        muc = presence.get_plugin("muc", check=True)
        if muc is None or 110 not in muc["status_codes"]:
            return
        room = self._bare_or_none(presence["from"])
        nick = JID(presence["from"]).resource
        if room is not None and nick:
            self._room_nicks[room] = nick

    def connection_lost(self, exception: BaseException | None) -> None:
        super().connection_lost(exception)
        if self._intentional_disconnect:
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.ensure_future(self._reconnect_after_delay(), loop=self.loop)

    async def _reconnect_after_delay(self) -> None:
        try:
            await self._sleep(self._next_reconnect_delay())
            if self._intentional_disconnect:
                return
            result = self.connect(self.config.host, self.config.port, **self._connect_kwargs())
            if inspect.isawaitable(result):
                await result
        finally:
            if self._reconnect_task is asyncio.current_task():
                self._reconnect_task = None

    def reschedule_connection_attempt(self):  # type: ignore[override]
        """Use a bounded retry sequence instead of Slixmpp's unbounded backoff."""
        if self._intentional_disconnect or self._current_connection_attempt is None:
            return None
        self._connect_loop_wait = self._next_reconnect_delay()
        self._current_connection_attempt = asyncio.ensure_future(self._connect_loop(), loop=self.loop)
        return self._current_connection_attempt

    def _next_reconnect_delay(self) -> int:
        delay = self.RECONNECT_DELAYS[min(self._reconnect_index, len(self.RECONNECT_DELAYS) - 1)]
        self._reconnect_index += 1
        return delay

    def _connect_kwargs(self) -> dict[str, bool]:
        return {"use_ssl": True} if self.config.direct_tls else {}

    def _send_chunks(self, recipient: str, body: str, message_type: str) -> list[str]:
        if not isinstance(body, str) or not body:
            return []
        stanza_ids: list[str] = []
        for chunk in _split_chunks(body):
            stanza = self.make_message(recipient, mbody=chunk, mtype=message_type)
            stanza["id"] = self.new_id()
            self.send(stanza)
            stanza_ids.append(stanza["id"])
        return stanza_ids

    @property
    def _bot_bare(self) -> str:
        return normalize_bare_jid(self.config.bot_jid)

    @staticmethod
    def _is_message(value: object) -> bool:
        return hasattr(value, "get_plugin") and hasattr(value, "xml")

    @staticmethod
    def _bare_or_none(value: object) -> str | None:
        try:
            return normalize_bare_jid(str(value))
        except ValueError:
            return None

    @staticmethod
    def _has_delay(stanza: object) -> bool:
        return stanza.get_plugin("delay", check=True) is not None or stanza.xml.find(f"{{{_DELAY_NAMESPACE}}}delay") is not None

    @staticmethod
    def _reply_id(stanza: object) -> str | None:
        reply = stanza.get_plugin("reply", check=True)
        if reply is not None and isinstance(reply["id"], str) and reply["id"]:
            return reply["id"]
        element = stanza.xml.find(f"{{{_REPLY_NAMESPACE}}}reply")
        return element.get("id") if element is not None and element.get("id") else None

    @staticmethod
    def _muc_sender(stanza: object) -> str | None:
        muc = stanza.get_plugin("muc", check=True)
        if muc is not None:
            return HermesXmppClient._bare_or_none(muc["item"]["jid"])
        item = stanza.xml.find("{http://jabber.org/protocol/muc#user}x/{http://jabber.org/protocol/muc#user}item")
        return HermesXmppClient._bare_or_none(item.get("jid")) if item is not None else None

    def _emit_message(self, event: InboundXmppMessage) -> None:
        self._on_message(event)

    def _emit_invite(self, event: XmppInvite) -> None:
        self._on_invite(event)


def _split_chunks(body: str, limit: int = 3500) -> list[str]:
    """Split Unicode text at paragraph, then whitespace, without truncating a word."""
    chunks: list[str] = []
    remainder = body
    while remainder:
        if len(remainder) <= limit:
            chunks.append(remainder)
            break
        window = remainder[: limit + 1]
        paragraph = window.rfind("\n\n")
        whitespace = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
        cut = paragraph if paragraph > 0 else whitespace
        if cut <= 0:
            cut = limit
        chunk = remainder[:cut].rstrip()
        if not chunk:
            cut = limit
            chunk = remainder[:cut]
        chunks.append(chunk)
        remainder = remainder[cut:].lstrip()
    return chunks
