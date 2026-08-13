"""Pure authorization, routing, and session policy for inbound XMPP events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re
import unicodedata

from .models import InboundXmppMessage


_FALLBACK_BARE_JID = re.compile(r"^[^\s@/]+@[^\s@/]+$")
_MENTION_SEPARATORS = " ,:;—-"


@dataclass(frozen=True)
class RoutedMessage:
    """An authorized inbound message with the text intended for Hermes."""

    message: InboundXmppMessage
    body: str


def normalize_bare_jid(value: str) -> str:
    """Return a normalized bare JID, rejecting malformed identities."""
    if not isinstance(value, str):
        raise ValueError("JID must be a string")

    candidate = value.strip()
    if not candidate:
        raise ValueError("JID must not be empty")
    raw_bare = candidate.split("/", 1)[0]
    if not _is_valid_bare_jid(raw_bare):
        raise ValueError("JID must contain a localpart and domain")

    try:
        from slixmpp import JID
    except ImportError:
        bare = candidate.split("/", 1)[0]
    else:
        bare = JID(candidate).bare

    bare = bare.strip()
    if not _is_valid_bare_jid(bare):
        raise ValueError("JID must contain a localpart and domain")
    return bare.casefold()


def parse_allowlist(value: str) -> frozenset[str]:
    """Parse comma-separated allowed bare JIDs."""
    if not isinstance(value, str):
        raise ValueError("allowlist must be a string")
    return frozenset(normalize_bare_jid(item) for item in value.split(",") if item.strip())


def route_direct(message: InboundXmppMessage, allowed_users: Iterable[str]) -> RoutedMessage | None:
    """Authorize an ordinary direct message and remove surrounding whitespace."""
    if not isinstance(message, InboundXmppMessage) or message.is_group is not False:
        return None
    sender = _normalized_or_none(message.sender_jid)
    chat = _normalized_or_none(message.chat_jid)
    if sender is None or chat is None or sender == chat or sender not in _normalize_allowed(allowed_users):
        return None
    body = _clean_body(message.body)
    return RoutedMessage(message, body) if body else None


def route_group(
    message: InboundXmppMessage,
    allowed_users: Iterable[str],
    bot_jid: str,
    bot_nick: str,
    bot_message_ids: Iterable[str],
) -> RoutedMessage | None:
    """Route an authorized MUC mention or reply to one of the bot's messages."""
    if not isinstance(message, InboundXmppMessage) or message.is_group is not True:
        return None
    room = _normalized_or_none(message.chat_jid)
    bot_bare = _normalized_or_none(bot_jid)
    sender = _normalized_or_none(message.sender_jid)
    if room is None or bot_bare is None or sender is None or sender not in _normalize_allowed(allowed_users):
        return None
    if not isinstance(message.sender_nick, str) or not isinstance(bot_nick, str):
        return None
    if message.sender_nick.casefold() == bot_nick.casefold():
        return None

    body = _clean_body(message.body)
    if not body:
        return None
    if _is_cached_reply(message.reply_to_id, bot_message_ids):
        return RoutedMessage(message, body)

    mentioned = _strip_leading_mention(body, bot_bare, bot_nick)
    return RoutedMessage(message, mentioned) if mentioned else None


def session_key(message: InboundXmppMessage) -> str:
    """Build a stable session key from normalized bare JIDs."""
    if not isinstance(message, InboundXmppMessage) or type(message.is_group) is not bool:
        raise ValueError("invalid inbound XMPP event")
    sender = normalize_bare_jid(message.sender_jid)
    chat = normalize_bare_jid(message.chat_jid)
    if not message.is_group:
        if sender == chat:
            raise ValueError("direct event must not be a self-message")
        return f"xmpp:dm:{sender}"
    return f"xmpp:muc:{chat}:{sender}"


def _normalize_allowed(allowed_users: Iterable[str]) -> frozenset[str]:
    try:
        return frozenset(normalize_bare_jid(user) for user in allowed_users)
    except (TypeError, ValueError):
        return frozenset()


def _normalized_or_none(value: object) -> str | None:
    try:
        return normalize_bare_jid(value)  # type: ignore[arg-type]
    except ValueError:
        return None


def _clean_body(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_valid_bare_jid(bare: str) -> bool:
    if not _FALLBACK_BARE_JID.fullmatch(bare) or any(_is_control(char) for char in bare):
        return False
    localpart, domain = bare.split("@", 1)
    if not localpart or not domain:
        return False
    return all(
        label
        and not label.startswith("-")
        and not label.endswith("-")
        and all(char == "-" or char.isalnum() for char in label)
        for label in domain.split(".")
    )


def _is_control(char: str) -> bool:
    return unicodedata.category(char) == "Cc"


def _is_cached_reply(reply_to_id: object, bot_message_ids: object) -> bool:
    if not isinstance(reply_to_id, str) or not reply_to_id or isinstance(bot_message_ids, (str, bytes)):
        return False
    try:
        return reply_to_id in bot_message_ids  # type: ignore[operator]
    except TypeError:
        return False


def _strip_leading_mention(body: str, bot_bare: str, bot_nick: str) -> str | None:
    nick = bot_nick.casefold()
    candidates = (f"@{nick}", nick, bot_bare)
    lowered = body.casefold()
    for candidate in candidates:
        if not lowered.startswith(candidate):
            continue
        if len(body) > len(candidate) and body[len(candidate)] not in _MENTION_SEPARATORS:
            continue
        remainder = body[len(candidate) :].lstrip(_MENTION_SEPARATORS)
        return remainder or None
    return None
