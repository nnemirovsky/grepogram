"""Deep links that address a stored message in Telegram.

Telegram addresses a message differently per chat type. Public channels and supergroups have a
web link, ``https://t.me/<username>/<msg>``; private ones use ``https://t.me/c/<id>/<msg>``,
where ``<id>`` is Telegram's bare channel id — the marked ``-(1000000000000 + id)`` form the
``chats`` table stores is a Telethon convention no Telegram URL understands, so
:func:`strip_channel_prefix` undoes it. Forum supergroups insert the topic: ``…/<topic>/<msg>``.
Private chats and legacy groups have no web form at all; the ``tg://openmessage`` scheme reaches
the message on mobile, and the desktop clients at least open the conversation through
``tg://user?id=`` — which is why :class:`~grepogram.models.Link` carries a ``fallback_url`` and
every hit and message view passes both on.

Both are links to display and cite. grepogram hands them to the reader and launches nothing
itself: terminals and MCP clients make a link clickable, and a search answers with ten hits, not
with one message to open.
"""

from telethon import types, utils

from grepogram.models import ChatRow, Link


def strip_channel_prefix(chat_id: int) -> int:
    """Telegram's bare channel id from Telethon's marked one: ``-1000000001234`` → ``1234``.

    The mark is arithmetic — Telethon builds it as ``-(1000000000000 + channel_id)`` — so
    :func:`telethon.utils.resolve_id` is what undoes it, never string surgery on the ``-100``
    prefix: a channel id below ten digits leaves zeros right behind that prefix
    (``-1000123456789`` is channel ``123456789``) and a lexical rule reading the digits after
    ``-100`` either loses them or refuses the id outright. Only a channel or supergroup mark is
    accepted; a user id (positive) or a legacy group id (``-1234``) raises ``ValueError`` rather
    than producing a link to the wrong place.
    """
    bare, kind = utils.resolve_id(chat_id)
    if kind is not types.PeerChannel:
        raise ValueError(f"not a marked channel id: {chat_id}")
    return int(bare)


def message_url(chat: ChatRow, msg_id: int, topic_id: int | None = None) -> Link:
    """The link that addresses ``msg_id`` in ``chat``, plus a fallback where it is mobile-only.

    ``topic_id`` is inserted only for forum supergroups, where it is a topic root: outside a
    forum the same column may still carry a legacy thread id, which no link form uses. A
    comment's channel post id lives in ``comment_of_msg_id`` and never reaches here.
    """
    if chat.type in ("channel", "supergroup"):
        topic = topic_id if chat.is_forum and topic_id is not None else None
        if chat.username:
            base = f"https://t.me/{chat.username}"
        else:
            base = f"https://t.me/c/{strip_channel_prefix(chat.id)}"
        return Link(f"{base}/{msg_id}" if topic is None else f"{base}/{topic}/{msg_id}")
    if chat.type in ("user", "bot"):
        return Link(
            f"tg://openmessage?user_id={chat.id}&message_id={msg_id}", f"tg://user?id={chat.id}"
        )
    if chat.type == "group":
        return Link(f"tg://openmessage?chat_id={abs(chat.id)}&message_id={msg_id}")
    raise ValueError(f"unknown chat type: {chat.type!r}")
