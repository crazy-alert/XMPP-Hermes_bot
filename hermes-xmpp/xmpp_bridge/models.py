from dataclasses import dataclass
@dataclass(frozen=True)
class InboundXmppMessage:
    message_id: str; chat_jid: str; sender_jid: str; sender_nick: str; body: str; is_group: bool; reply_to_id: str | None; room_nick: str | None = None; encrypted: bool = False
@dataclass(frozen=True)
class XmppInvite:
    room_jid: str; inviter_jid: str; is_direct: bool
@dataclass(frozen=True)
class DeliveryTarget:
    chat_jid: str; is_group: bool
