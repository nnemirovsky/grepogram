"""Telegram sync: mapping Telethon messages to ``messages`` rows.

:func:`map_message` reads raw TL attributes only — ``msg.message``, ``msg.media``,
``msg.reply_to``, ``msg.fwd_from``, ``msg.reactions``, ``msg.from_id``, ``msg.post``, ``msg.date``,
``msg.edit_date`` — and never the client-bound helpers (``msg.text``, ``msg.file``, ``msg.sender``,
``msg.chat``), so a message built without a client (the test fixtures) maps exactly like one
Telethon yields from ``iter_messages``. Display names come from a ``names`` map built with
:func:`collect_users` and :func:`names_of` out of the users and chats Telegram returns alongside
messages; the same rows feed the ``users`` upsert.
"""

import datetime as dt
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from telethon import utils
from telethon.tl import types

from grepogram.dialogs import entity_username
from grepogram.models import ChatRow, MediaKind, MessageRow, UserRow

log = logging.getLogger(__name__)

SELF_NAME = "me"
UNKNOWN_FORWARD = "unknown"
_LOCATION_MEDIA = (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)
_STICKER_ATTRIBUTES = (types.DocumentAttributeSticker, types.DocumentAttributeCustomEmoji)
_CHAT_ENTITIES = (
    types.Chat,
    types.ChatForbidden,
    types.Channel,
    types.ChannelForbidden,
)


# --- users -----------------------------------------------------------------------------------


def collect_users(entities: Iterable[Any]) -> dict[int, UserRow]:
    """``users`` rows for the peers Telegram returns alongside messages, keyed by marked id.

    Users keep their id; chats and channels — senders of anonymous-admin and "send as channel"
    messages, and forward origins — are stored under their marked ``-…`` id with the title as
    display name. ``None``, ``UserEmpty`` and other objects are skipped. A deleted account has
    no name, so its ``display_name`` is ``None``.
    """
    users: dict[int, UserRow] = {}
    for entity in entities:
        if isinstance(entity, types.User):
            display = str(utils.get_display_name(entity)).strip()
            users[int(entity.id)] = UserRow(
                id=int(entity.id),
                display_name=display or None,
                username=entity_username(entity),
            )
        elif isinstance(entity, _CHAT_ENTITIES):
            marked = int(utils.get_peer_id(entity))
            users[marked] = UserRow(
                id=marked,
                display_name=str(entity.title) or None,
                username=entity_username(entity),
            )
    return users


def names_of(users: Mapping[int, UserRow]) -> dict[int, str]:
    """Marked id → display name, as :func:`map_message` expects; unnamed ids are left out."""
    return {user_id: user.display_name for user_id, user in users.items() if user.display_name}


def sender_of(
    msg: Any, names: Mapping[int, str], *, me: UserRow | None = None
) -> tuple[int | None, str]:
    """The sender's marked id and display name, following Telethon's ``sender_id`` rule.

    ``from_id`` wins; a channel post without it is sent by the channel; an incoming private
    message without it comes from the peer; an outgoing one comes from the account owner —
    ``me`` when given, otherwise ``(None, "me")``. The name is looked up in ``names``, then
    ``me``, then the post signature, and falls back to ``id<n>``.
    """
    if msg.from_id is not None:
        sender_id = int(utils.get_peer_id(msg.from_id))
    elif msg.post or (not msg.out and isinstance(msg.peer_id, types.PeerUser)):
        sender_id = int(utils.get_peer_id(msg.peer_id))
    elif me is not None:
        return me.id, me.display_name or SELF_NAME
    else:
        return None, SELF_NAME
    name = names.get(sender_id)
    if name is None and me is not None and me.id == sender_id:
        name = me.display_name
    return sender_id, name or msg.post_author or unknown_name(sender_id)


def unknown_name(marked_id: int) -> str:
    return f"id{marked_id}"


# --- message mapping -------------------------------------------------------------------------


def map_message(
    msg: Any, chat: ChatRow, names: Mapping[int, str], *, me: UserRow | None = None
) -> MessageRow | None:
    """Turn one Telethon message into a :class:`MessageRow` for ``chat``; ``None`` to skip it.

    Service messages (joins, pins, topic edits) and ``MessageEmpty`` are skipped. The row is
    stored under ``chat.id`` whatever the message's own peer is, which is how channel comments
    land in their discussion chat. Text is the message text or caption; a poll, venue or
    contact — media that carries its content outside the text — contributes its own text when
    the message has none.
    """
    if isinstance(msg, types.MessageService) or not isinstance(msg, types.Message):
        return None
    if msg.date is None:
        log.debug("skipping message %s in chat %s: no date", msg.id, chat.id)
        return None
    from_id, from_name = sender_of(msg, names, me=me)
    if from_id == chat.id and from_id not in names and chat.title:
        from_name = chat.title
    reply_to_msg_id, topic_id = reply_of(msg.reply_to, chat.id)
    media_kind, media_filename = media_of(msg.media)
    return MessageRow(
        chat_id=chat.id,
        msg_id=int(msg.id),
        date=epoch(msg.date),
        edit_date=epoch(msg.edit_date) if msg.edit_date is not None else None,
        from_id=from_id,
        from_name=from_name,
        reply_to_msg_id=reply_to_msg_id,
        topic_id=topic_id,
        fwd_from=forward_of(msg.fwd_from, names),
        text=msg.message or media_text(msg.media),
        media_kind=media_kind,
        media_filename=media_filename,
        reactions_total=reactions_total(msg.reactions),
    )


def epoch(when: dt.datetime) -> int:
    """Unix seconds; Telethon dates are UTC-aware, a naive one is taken as UTC."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return int(when.timestamp())


def reply_of(reply_to: Any, chat_id: int) -> tuple[int | None, int | None]:
    """``(reply_to_msg_id, topic_id)`` from a message's ``reply_to`` header.

    In a forum the header always points at the topic: a message that merely sits in a topic has
    ``forum_topic`` set and ``reply_to_msg_id`` = the topic root with no ``reply_to_top_id`` and
    is not a reply; a real reply inside a topic carries its parent in ``reply_to_msg_id`` and the
    topic root in ``reply_to_top_id``. Story replies and quotes of a message from another chat
    (``reply_to_peer_id`` set to a different peer) are not in-chat replies.
    """
    if not isinstance(reply_to, types.MessageReplyHeader):
        return None, None
    parent: int | None = reply_to.reply_to_msg_id
    topic: int | None = None
    if reply_to.forum_topic:
        topic = reply_to.reply_to_top_id or parent
        if not reply_to.reply_to_top_id:
            parent = None
    other = reply_to.reply_to_peer_id
    if other is not None and int(utils.get_peer_id(other)) != chat_id:
        parent = None
    return parent, topic


def media_of(media: Any) -> tuple[MediaKind | None, str | None]:
    """``(media_kind, media_filename)`` for a ``MessageMedia*`` object (``None`` for no media)."""
    if media is None or isinstance(media, types.MessageMediaEmpty):
        return None, None
    if isinstance(media, types.MessageMediaPhoto):
        return "photo", None
    if isinstance(media, types.MessageMediaDocument):
        return document_kind(media), filename_of(media.document)
    if isinstance(media, types.MessageMediaWebPage):
        return "webpage", None
    if isinstance(media, types.MessageMediaPoll):
        return "poll", None
    if isinstance(media, types.MessageMediaContact):
        return "contact", None
    if isinstance(media, _LOCATION_MEDIA):
        return "location", None
    return "other", None


def document_kind(media: Any) -> MediaKind:
    """Classify a ``MessageMediaDocument`` by its document attributes, then by the media flags.

    Stickers (including video stickers, which also carry a video attribute) come first; a voice
    note is an audio attribute with ``voice``; a video note is a video attribute with
    ``round_message``; GIFs (``DocumentAttributeAnimated``) count as video.
    """
    attributes = list(getattr(media.document, "attributes", None) or ())
    if any(isinstance(attr, _STICKER_ATTRIBUTES) for attr in attributes):
        return "sticker"
    for attr in attributes:
        if isinstance(attr, types.DocumentAttributeAudio):
            return "voice" if attr.voice else "audio"
        if isinstance(attr, types.DocumentAttributeVideo):
            return "video_note" if attr.round_message else "video"
    if any(isinstance(attr, types.DocumentAttributeAnimated) for attr in attributes):
        return "video"
    if media.voice:
        return "voice"
    if media.round:
        return "video_note"
    if media.video:
        return "video"
    return "document"


def filename_of(document: Any) -> str | None:
    for attr in getattr(document, "attributes", None) or ():
        if isinstance(attr, types.DocumentAttributeFilename):
            return str(attr.file_name)
    return None


def media_text(media: Any) -> str:
    """Text a poll, venue or contact carries in place of a message body; ``""`` otherwise."""
    if isinstance(media, types.MessageMediaPoll):
        poll = media.poll
        parts = [text_of(poll.question), *(text_of(answer.text) for answer in poll.answers)]
        return "\n".join(part for part in parts if part)
    if isinstance(media, types.MessageMediaVenue):
        return "\n".join(part for part in (media.title, media.address) if part)
    if isinstance(media, types.MessageMediaContact):
        return " ".join(part for part in (media.first_name, media.last_name) if part)
    return ""


def text_of(value: Any) -> str:
    """Plain text of a ``TextWithEntities`` (current layers) or a bare string (older ones)."""
    if isinstance(value, types.TextWithEntities):
        return str(value.text)
    return str(value or "")


def forward_of(fwd: Any, names: Mapping[int, str]) -> str | None:
    """Who a forwarded message came from: the origin's name from ``names``, else the name
    Telegram attaches for hidden accounts, else the post signature, else ``id<n>``."""
    if fwd is None:
        return None
    if fwd.from_id is not None:
        marked = int(utils.get_peer_id(fwd.from_id))
        return names.get(marked) or fwd.from_name or fwd.post_author or unknown_name(marked)
    return fwd.from_name or fwd.post_author or UNKNOWN_FORWARD


def reactions_total(reactions: Any) -> int:
    if reactions is None:
        return 0
    return sum(int(entry.count) for entry in reactions.results or ())
