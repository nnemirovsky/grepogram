"""Real Telethon TL message objects built without a client.

Every builder returns the ``types.Message`` / ``types.MessageService`` Telethon yields from
``iter_messages``, minus the client binding, so ``sync.map_message`` and ``FakeClient`` exercise
the raw attributes the real client fills in. ``chat_id`` and ``sender`` are marked ids (positive
for users, ``-…`` for groups, ``-100…`` for channels and supergroups) or entities. Dates default
to :data:`EPOCH` plus ``msg_id`` minutes so ascending ids are chronological.
"""

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from telethon import utils
from telethon.tl import types

EPOCH = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)


def at(minutes: float) -> dt.datetime:
    """``EPOCH`` shifted by ``minutes``."""
    return EPOCH + dt.timedelta(minutes=minutes)


def peer(target: int | Any) -> Any:
    """``PeerUser`` / ``PeerChat`` / ``PeerChannel`` for a marked id or an entity."""
    return utils.get_peer(target)


# --- media parts -----------------------------------------------------------------------------


def document(
    *attributes: Any, mime_type: str = "application/octet-stream", doc_id: int = 1
) -> types.Document:
    return types.Document(
        id=doc_id,
        access_hash=doc_id,
        file_reference=b"",
        date=EPOCH,
        mime_type=mime_type,
        size=1024,
        dc_id=2,
        attributes=list(attributes),
    )


def photo(photo_id: int = 1) -> types.Photo:
    return types.Photo(
        id=photo_id,
        access_hash=photo_id,
        file_reference=b"",
        date=EPOCH,
        sizes=[types.PhotoSize("m", 320, 240, 1000)],
        dc_id=2,
    )


def reactions(counts: Mapping[str, int]) -> types.MessageReactions:
    """``MessageReactions`` with one ``ReactionCount`` per emoji."""
    return types.MessageReactions(
        results=[
            types.ReactionCount(reaction=types.ReactionEmoji(emoji), count=count)
            for emoji, count in counts.items()
        ]
    )


def reply_header(
    reply_to_msg_id: int | None, *, topic_id: int | None = None, reply_to_peer: Any = None
) -> types.MessageReplyHeader:
    """A reply header as Telegram sends it.

    ``topic_id`` alone → a message that merely sits in that forum topic; ``reply_to_msg_id`` +
    ``topic_id`` → a reply inside the topic; ``reply_to_msg_id`` alone → a plain reply;
    ``reply_to_peer`` → a quote of a message in that other chat.
    """
    if topic_id is not None and reply_to_msg_id is None:
        return types.MessageReplyHeader(reply_to_msg_id=topic_id, forum_topic=True)
    return types.MessageReplyHeader(
        reply_to_msg_id=reply_to_msg_id,
        reply_to_top_id=topic_id,
        forum_topic=topic_id is not None or None,
        reply_to_peer_id=peer(reply_to_peer) if reply_to_peer is not None else None,
    )


def forward_header(
    origin: int | Any | None = None,
    *,
    origin_name: str | None = None,
    channel_post: int | None = None,
    post_author: str | None = None,
    date: dt.datetime | None = None,
) -> types.MessageFwdHeader:
    """``MessageFwdHeader``: ``origin`` is the original sender (id or entity), ``origin_name`` the
    name Telegram shows for a hidden account, ``channel_post`` the id in the origin channel."""
    return types.MessageFwdHeader(
        date=date or EPOCH,
        from_id=peer(origin) if origin is not None else None,
        from_name=origin_name,
        channel_post=channel_post,
        post_author=post_author,
    )


# --- messages --------------------------------------------------------------------------------


def message(
    chat_id: int | Any,
    msg_id: int,
    text: str = "",
    *,
    sender: int | Any | None = None,
    date: dt.datetime | None = None,
    edit_date: dt.datetime | None = None,
    media: Any = None,
    reply_to: Any = None,
    fwd_from: types.MessageFwdHeader | None = None,
    reactions: types.MessageReactions | None = None,
    post: bool = False,
    post_author: str | None = None,
    out: bool = False,
    **extra: Any,
) -> types.Message:
    """A regular message; every richer builder ends up here."""
    return types.Message(
        id=msg_id,
        peer_id=peer(chat_id),
        date=date or at(msg_id),
        message=text,
        from_id=peer(sender) if sender is not None else None,
        edit_date=edit_date,
        media=media,
        reply_to=reply_to,
        fwd_from=fwd_from,
        reactions=reactions,
        post=post or None,
        post_author=post_author,
        out=out or None,
        **extra,
    )


def text_message(
    chat_id: int | Any, msg_id: int, text: str, *, sender: int | Any | None = None, **kw: Any
) -> types.Message:
    return message(chat_id, msg_id, text, sender=sender, **kw)


def photo_message(chat_id: int | Any, msg_id: int, caption: str = "", **kw: Any) -> types.Message:
    return message(chat_id, msg_id, caption, media=types.MessageMediaPhoto(photo=photo()), **kw)


def voice_message(
    chat_id: int | Any, msg_id: int, *, duration: int = 5, **kw: Any
) -> types.Message:
    media = types.MessageMediaDocument(
        voice=True,
        document=document(
            types.DocumentAttributeAudio(duration=duration, voice=True), mime_type="audio/ogg"
        ),
    )
    return message(chat_id, msg_id, media=media, **kw)


def video_message(chat_id: int | Any, msg_id: int, caption: str = "", **kw: Any) -> types.Message:
    media = types.MessageMediaDocument(
        video=True,
        document=document(
            types.DocumentAttributeVideo(duration=12.0, w=1280, h=720),
            types.DocumentAttributeFilename("clip.mp4"),
            mime_type="video/mp4",
        ),
    )
    return message(chat_id, msg_id, caption, media=media, **kw)


def video_note_message(chat_id: int | Any, msg_id: int, **kw: Any) -> types.Message:
    media = types.MessageMediaDocument(
        round=True,
        video=True,
        document=document(
            types.DocumentAttributeVideo(duration=8.0, w=384, h=384, round_message=True),
            mime_type="video/mp4",
        ),
    )
    return message(chat_id, msg_id, media=media, **kw)


def audio_message(
    chat_id: int | Any, msg_id: int, *, title: str = "Song", performer: str = "Band", **kw: Any
) -> types.Message:
    media = types.MessageMediaDocument(
        document=document(
            types.DocumentAttributeAudio(duration=180, title=title, performer=performer),
            types.DocumentAttributeFilename("song.mp3"),
            mime_type="audio/mpeg",
        )
    )
    return message(chat_id, msg_id, media=media, **kw)


def document_message(
    chat_id: int | Any,
    msg_id: int,
    filename: str,
    caption: str = "",
    *,
    mime_type: str = "application/pdf",
    **kw: Any,
) -> types.Message:
    media = types.MessageMediaDocument(
        document=document(types.DocumentAttributeFilename(filename), mime_type=mime_type)
    )
    return message(chat_id, msg_id, caption, media=media, **kw)


def sticker_message(
    chat_id: int | Any, msg_id: int, *, alt: str = "👍", video: bool = False, **kw: Any
) -> types.Message:
    attributes: list[Any] = [
        types.DocumentAttributeSticker(alt=alt, stickerset=types.InputStickerSetEmpty())
    ]
    if video:
        attributes.append(types.DocumentAttributeVideo(duration=3.0, w=512, h=512))
    media = types.MessageMediaDocument(
        document=document(*attributes, mime_type="video/webm" if video else "image/webp")
    )
    return message(chat_id, msg_id, media=media, **kw)


def gif_message(chat_id: int | Any, msg_id: int, **kw: Any) -> types.Message:
    media = types.MessageMediaDocument(
        document=document(
            types.DocumentAttributeAnimated(),
            types.DocumentAttributeFilename("funny.gif"),
            mime_type="video/mp4",
        )
    )
    return message(chat_id, msg_id, media=media, **kw)


def poll_message(
    chat_id: int | Any, msg_id: int, question: str, answers: Iterable[str], **kw: Any
) -> types.Message:
    poll = types.Poll(
        id=msg_id,
        question=types.TextWithEntities(question, []),
        answers=[
            types.PollAnswer(text=types.TextWithEntities(answer, []), option=bytes([index]))
            for index, answer in enumerate(answers)
        ],
        hash=0,
    )
    media = types.MessageMediaPoll(poll=poll, results=types.PollResults())
    return message(chat_id, msg_id, media=media, **kw)


def webpage_message(
    chat_id: int | Any, msg_id: int, text: str, *, url: str = "https://example.com", **kw: Any
) -> types.Message:
    webpage = types.WebPage(
        id=1, url=url, display_url=url.removeprefix("https://"), hash=0, title="Example"
    )
    return message(chat_id, msg_id, text, media=types.MessageMediaWebPage(webpage=webpage), **kw)


def contact_message(
    chat_id: int | Any,
    msg_id: int,
    first_name: str,
    last_name: str = "",
    *,
    user_id: int = 0,
    **kw: Any,
) -> types.Message:
    media = types.MessageMediaContact(
        phone_number="+10000000000",
        first_name=first_name,
        last_name=last_name,
        vcard="",
        user_id=user_id,
    )
    return message(chat_id, msg_id, media=media, **kw)


def location_message(chat_id: int | Any, msg_id: int, **kw: Any) -> types.Message:
    return message(chat_id, msg_id, media=types.MessageMediaGeo(geo=geo_point()), **kw)


def venue_message(
    chat_id: int | Any, msg_id: int, title: str, address: str, **kw: Any
) -> types.Message:
    media = types.MessageMediaVenue(
        geo=geo_point(),
        title=title,
        address=address,
        provider="foursquare",
        venue_id="v1",
        venue_type="cafe",
    )
    return message(chat_id, msg_id, media=media, **kw)


def geo_point() -> types.GeoPoint:
    return types.GeoPoint(long=-58.38, lat=-34.6, access_hash=0)


def reply_message(
    chat_id: int | Any,
    msg_id: int,
    text: str,
    *,
    reply_to: int,
    topic_id: int | None = None,
    **kw: Any,
) -> types.Message:
    """A reply to ``reply_to``; inside a forum topic when ``topic_id`` is given."""
    return message(chat_id, msg_id, text, reply_to=reply_header(reply_to, topic_id=topic_id), **kw)


def topic_message(
    chat_id: int | Any, msg_id: int, text: str, *, topic_id: int, **kw: Any
) -> types.Message:
    """A message posted in a forum topic without replying to anyone."""
    return message(chat_id, msg_id, text, reply_to=reply_header(None, topic_id=topic_id), **kw)


def forwarded_message(
    chat_id: int | Any,
    msg_id: int,
    text: str,
    *,
    origin: int | Any | None = None,
    origin_name: str | None = None,
    channel_post: int | None = None,
    post_author: str | None = None,
    **kw: Any,
) -> types.Message:
    header = forward_header(
        origin, origin_name=origin_name, channel_post=channel_post, post_author=post_author
    )
    return message(chat_id, msg_id, text, fwd_from=header, **kw)


def channel_post(
    channel_id: int | Any,
    msg_id: int,
    text: str,
    *,
    post_author: str | None = None,
    views: int = 100,
    **kw: Any,
) -> types.Message:
    """A broadcast channel post: ``post`` set, no ``from_id``."""
    return message(channel_id, msg_id, text, post=True, post_author=post_author, views=views, **kw)


def service_message(
    chat_id: int | Any,
    msg_id: int,
    action: Any = None,
    *,
    sender: int | Any | None = None,
    date: dt.datetime | None = None,
) -> types.MessageService:
    """A service message (pin, join, topic edit …), which the index skips."""
    return types.MessageService(
        id=msg_id,
        peer_id=peer(chat_id),
        date=date or at(msg_id),
        action=action or types.MessageActionPinMessage(),
        from_id=peer(sender) if sender is not None else None,
    )
