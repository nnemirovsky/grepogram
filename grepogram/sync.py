"""Telegram sync: fetching messages incrementally and mapping them to ``messages`` rows.

:func:`sync_all` is the one entry point every caller (CLI ``sync``, MCP ``sync``, auto-sync in
``search``) goes through: it takes the cross-process :class:`SyncLock`, re-resolves the configured
sources, syncs chats in ``last_sync_at`` order until the :class:`SyncBudget` runs out, runs
:func:`on_chat_synced` — the unit rebuild followed by the lexical index — over every stored row
that no rebuild has covered yet, and finally embeds the dirty units when an embedder is given.
:func:`sync_chat` fetches one chat: new messages after ``last_msg_id`` in batches, then a
re-fetch of the newest messages for edits and reactions. A channel whose source has ``comments``
also gets the comment threads of its new posts — the ones Telegram reports comments on — stored
under the linked discussion group naming the channel and the post, and on every later run the
threads of its newest posts that grew since. A source that lists the group itself syncs its
whole history as well: both paths write the same rows, and a comment relation already stored
survives the plain history's upsert (:func:`grepogram.db.upsert_messages`).

Fetching and indexing are decoupled through ``messages.indexed``: every row a batch commits is
flagged until :func:`on_chat_synced` has rebuilt its units and ``msg_fts`` entry, and
:func:`_sync_chats` indexes a chat's pending rows after its fetch whether that returned or raised
(a flood wait, an RPC error, a cancellation) and, at the end of the run, those of the chats it
never reached and of a bounded number of chats nothing leads to any more (:func:`index_stranded`).
The run then re-cuts a bounded number of chats whose units predate this build's unit recipe
(:func:`recut_pending_chats`), which is how a change to what a unit *is* reaches history no
incremental rebuild can touch.
:func:`prune_deleted` is the pass beside all this: the full sweep that asks Telegram about every
stored id and drops the messages it no longer has, driven by ``grepogram prune-deleted`` and never
by a sync — it costs about one request per hundred stored messages, so it is resumable through a
``meta`` cursor per chat and always deliberate.

A run that dies between a commit and the rebuild therefore leaves nothing behind that the next run
does not pick up (:func:`grepogram.db.unindexed_message_ids`). The rebuild, the
indexing and the flag are one transaction, so the flag never clears over derived data that is not
there; the worker thread they run on is joined even when the surrounding tool call is cancelled
(:func:`_joined_to_thread`), so the :class:`SyncLock` outlives every write it covers.

:func:`map_message` reads raw TL attributes only — ``msg.message``, ``msg.media``,
``msg.reply_to``, ``msg.fwd_from``, ``msg.reactions``, ``msg.from_id``, ``msg.post``, ``msg.date``,
``msg.edit_date`` — and never the client-bound helpers (``msg.text``, ``msg.file``, ``msg.sender``,
``msg.chat``), so a message built without a client (the test fixtures) maps exactly like one
Telethon yields from ``iter_messages``. Display names come from a ``names`` map built with
:func:`collect_users` out of the users and chats Telegram returns alongside messages
(:func:`peers_of`); the same rows feed the ``users`` upsert.
"""

import asyncio
import dataclasses
import datetime as dt
import functools
import logging
import math
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from telethon import errors, utils
from telethon.tl import functions, types

from grepogram import db, dialogs, index, units
from grepogram.config import ConfigError
from grepogram.dialogs import entity_username
from grepogram.embed import Embedder
from grepogram.models import (
    ChatRow,
    Config,
    MediaKind,
    MessageRow,
    PruneReport,
    Source,
    SyncCfg,
    SyncReport,
    UserRow,
)
from grepogram.paths import FileLock, Paths
from grepogram.sources import IMPORT_PREFIX, discussion_source_id, parse_since, resolve_sources
from grepogram.units import UNKNOWN_SENDER

log = logging.getLogger(__name__)

BATCH_SIZE = 500
JOIN_LOG_EVERY = 30.0
STRANDED_CHATS = 4
"""Chats outside the run's own that :func:`index_stranded` repairs per run."""
RECUT_CHATS_PER_RUN = 4
"""Chats :func:`recut_pending_chats` re-cuts per run.

A re-cut deletes and re-inserts every unit of a chat and drops their vectors, so the chat is
re-embedded afterwards — the expensive half. Bounding it per run is what keeps a recipe bump
from turning one sync into a full-index rebuild, and the per-chat markers are what let the next
run carry on where this one stopped."""
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


def peers_of(msg: Any) -> list[Any]:
    """The entities Telethon bound to a message out of the peers Telegram returned with it.

    ``iter_messages`` attaches the sender, the chat and the forward origin from the ``users`` and
    ``chats`` lists of each history chunk; reading them costs no request. A message built without
    a client has none, and the mapping then falls back to ``id<n>`` names.
    """
    peers = [getattr(msg, "sender", None), getattr(msg, "chat", None)]
    forward = getattr(msg, "forward", None)
    if forward is not None:
        peers += [getattr(forward, "sender", None), getattr(forward, "chat", None)]
    return [peer for peer in peers if peer is not None]


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
    return fwd.from_name or fwd.post_author or UNKNOWN_SENDER


def reactions_total(reactions: Any) -> int:
    if reactions is None:
        return 0
    return sum(int(entry.count) for entry in reactions.results or ())


def replies_count(msg: Any) -> int:
    """How many replies Telegram reports on a message (``msg.replies.replies``); 0 without.

    On a channel post with a linked discussion group this is the size of its comment thread.
    """
    replies = getattr(msg, "replies", None)
    if not isinstance(replies, types.MessageReplies):
        return 0
    return int(replies.replies)


# --- budget and lock -------------------------------------------------------------------------


class SyncInProgress(Exception):
    """Another grepogram process holds the sync lock."""


class SyncBudget:
    """Wall-clock allowance for one sync run; ``seconds=None`` never expires on its own.

    ``clock`` defaults to :func:`time.monotonic` and is injectable for tests.
    """

    def __init__(
        self, seconds: float | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.seconds = seconds
        self._clock = clock
        self.deadline: float | None = None if seconds is None else clock() + seconds
        self._cancelled = False

    @property
    def expired(self) -> bool:
        if self._cancelled:
            return True
        return self.deadline is not None and self._clock() >= self.deadline

    def cancel(self) -> None:
        """Expire the budget now, whatever the clock says and whatever it was given.

        How a cancelled run stops the work a worker thread is pacing against this budget at its
        next boundary instead of waiting the whole of it out (:func:`_joined_to_thread`).
        """
        self._cancelled = True

    @property
    def remaining(self) -> float | None:
        """Seconds left, ``None`` for an unlimited budget, never negative, ``0`` once cancelled."""
        if self._cancelled:
            return 0.0
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - self._clock())


class SyncLock(FileLock):
    """Exclusive ``flock`` on ``paths.lock_file`` so two processes never sync the same index.

    Non-blocking: entering while another process (or another open descriptor in this one) holds
    the lock raises :class:`SyncInProgress`.
    """

    blocking = False

    def __init__(self, paths: Paths) -> None:
        super().__init__(paths.lock_file)

    def busy(self) -> Exception:
        return SyncInProgress(
            f"another sync is running (lock held on {self.path}); wait for it to finish"
        )


# --- one chat --------------------------------------------------------------------------------


UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    errors.ChannelPrivateError,
    errors.ChatAdminRequiredError,
    errors.ChannelInvalidError,
    errors.ChatForbiddenError,
)


class DiscussionUnavailable(Exception):
    """A channel's linked discussion group cannot be resolved, though the channel itself can."""


_DISCUSSION_ERRORS: tuple[type[Exception], ...] = (ValueError, *UNAVAILABLE_ERRORS)


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncedChat:
    """What :func:`sync_chat` did for one chat.

    ``new_msg_ids`` are the ``messages.id`` rowids of every row inserted or changed (new messages
    and edits alike), in fetch order — the ids ``msg_fts`` is keyed by. ``new`` counts messages
    seen for the first time: rows that were not stored before this run. ``complete`` is ``False``
    when the budget cut the fetch short; the stored progress lets the next run resume.
    ``discussion`` carries the same for the linked discussion chat when comments were fetched;
    ``migrated_to`` is the supergroup a legacy group was upgraded to, freshly upserted so the
    caller can sync it too. ``warnings`` are for the report: so far only comment threads Telegram
    refused while the posts themselves went through.
    """

    chat: ChatRow
    new_msg_ids: list[int] = field(default_factory=list)
    new: int = 0
    complete: bool = True
    unavailable: bool = False
    discussion: "SyncedChat | None" = None
    migrated_to: ChatRow | None = None
    warnings: list[str] = field(default_factory=list)


class _PeerBook:
    """Users met during a fetch: display names for the mapper, pending rows for the upsert."""

    def __init__(self) -> None:
        self.users: dict[int, UserRow] = {}
        self.names: dict[int, str] = {}
        self._pending: dict[int, UserRow] = {}

    def add(self, msg: Any) -> None:
        for user_id, user in collect_users(peers_of(msg)).items():
            if self.users.get(user_id) == user:
                continue
            self.users[user_id] = user
            self._pending[user_id] = user
            if user.display_name:
                self.names[user_id] = user.display_name

    def flush(self, conn: sqlite3.Connection) -> None:
        if self._pending:
            db.upsert_users(conn, self._pending.values())
            self._pending = {}


@dataclass(slots=True, kw_only=True)
class _Run:
    """State one chat's fetch passes between its steps.

    ``changes`` and ``comment_ids`` are the ordered, deduplicated ``messages.id`` values touched
    in the chat and in its discussion chat (dicts used as ordered sets); ``inserted`` counts the
    rows stored for the first time per chat id. ``discussion`` is the linked discussion chat of
    a channel with comments, ``None`` when there is none or comments are off; it is dropped when
    Telegram refuses a thread mid-run, with the reason kept in ``warnings``. ``replies`` holds,
    per post the incremental pass mapped and has not stored yet, the reply count Telegram
    reported on it, so :func:`_store_batch` asks for the threads of posts that have one.
    """

    client: Any
    conn: sqlite3.Connection
    chat: ChatRow
    source: Source
    budget: SyncBudget
    me: UserRow | None
    discussion: ChatRow | None = None
    peers: _PeerBook = field(default_factory=_PeerBook)
    changes: dict[int, None] = field(default_factory=dict)
    comment_ids: dict[int, None] = field(default_factory=dict)
    inserted: dict[int, int] = field(default_factory=dict)
    replies: dict[int, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def map(self, msg: Any, chat: ChatRow) -> MessageRow | None:
        self.peers.add(msg)
        return map_message(msg, chat, self.peers.names, me=self.me)

    def store(self, rows: list[MessageRow]) -> list[int]:
        """Upsert ``rows`` (all of one chat) with the users met so far; returns their row ids."""
        if not rows:
            return []
        chat_id = rows[0].chat_id
        with db.transaction(self.conn):
            known = db.get_messages_by_msg_id(self.conn, chat_id, [row.msg_id for row in rows])
            self.peers.flush(self.conn)
            ids = db.upsert_messages(self.conn, rows)
        fresh = sum(1 for row in rows if row.msg_id not in known)
        self.inserted[chat_id] = self.inserted.get(chat_id, 0) + fresh
        return ids

    def drop_comments(self, exc: Exception) -> None:
        """Stop fetching comments for this run; the posts themselves go on."""
        warning = (
            f"comments of channel {self.chat.id} ({self.chat.title}) are unavailable "
            f"({exc}); its posts were synced without them"
        )
        log.warning(warning)
        self.warnings.append(warning)
        self.discussion = None


def _track(seen: dict[int, None], ids: Iterable[int]) -> None:
    for row_id in ids:
        seen.setdefault(row_id, None)


def since_of(source: Source | None) -> dt.datetime | None:
    """``source.since`` as a UTC midnight datetime, ``None`` when unset."""
    if source is None or not source.since:
        return None
    try:
        day = parse_since(source.since)
    except ValueError as exc:
        raise ConfigError(f"source {source.id}: {exc}") from None
    assert day is not None
    return dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)


async def sync_chat(
    client: Any,
    conn: sqlite3.Connection,
    chat: ChatRow,
    source: Source,
    budget: SyncBudget,
    *,
    cfg: Config | None = None,
    me: UserRow | None = None,
) -> SyncedChat:
    """Fetch one chat incrementally and store what changed; the client must be connected.

    New messages are read with ``iter_messages(min_id=last_msg_id, reverse=True)`` — on the
    first run ``offset_date=since`` skips older history — and upserted in batches of
    :data:`BATCH_SIZE`, advancing ``last_msg_id`` after every batch so an interrupted run resumes
    where it stopped. A run that finishes re-fetches the newest ``edit_refetch`` messages for
    edits and reactions (only rows that actually differ are written) and stamps
    ``last_sync_at``. That pass also drops the messages deleted in Telegram, which it costs no
    request at all to notice: they are the stored ids inside the range it covered that it did not
    return (:func:`_drop_deleted`). Channels whose source has ``comments`` also get the comment
    threads of each new post, stored under the linked discussion chat as comments on that post,
    and the re-fetch pass re-reads the thread of every re-fetched post Telegram reports more
    replies for than are stored, so comments that arrive after the post was indexed follow.

    ``cfg`` is the whole config rather than its ``[sync]`` section because a deletion re-cuts the
    units holding the message, which is cut to ``[units]``; without one the defaults are used.

    A chat Telegram refuses (:data:`UNAVAILABLE_ERRORS`) is marked ``unavailable`` and reported,
    not raised; the same errors on the discussion group alone (a private one, say) switch the
    comments off for the run with a warning while the posts are synced. A legacy group upgraded
    to a supergroup has ``migrated_to`` set and its history left alone from then on, while the
    result keeps pointing at the supergroup so :func:`sync_all` syncs that one.
    :class:`~telethon.errors.FloodWaitError` beyond the client's sleep threshold and
    authorization errors propagate after the current batch is committed.
    """
    if chat.migrated_to is not None:
        log.debug("chat %s migrated to %s; its history is frozen", chat.id, chat.migrated_to)
        return SyncedChat(chat=chat, migrated_to=db.get_chat(conn, chat.migrated_to))
    run = _Run(client=client, conn=conn, chat=chat, source=source, budget=budget, me=me)
    try:
        migrated = await _check_migration(client, conn, chat) if chat.type == "group" else None
        if source.comments and chat.type == "channel":
            try:
                run.discussion = await link_discussion_chat(client, conn, chat)
            except DiscussionUnavailable as exc:
                run.drop_comments(exc)
        fetched = await _fetch_new(run)
        if fetched.complete and chat.last_sync_at is not None:
            _track(run.changes, await _refetch_edits(run, cfg or Config()))
    except UNAVAILABLE_ERRORS as exc:
        log.warning("chat %s (%s) is unavailable: %s", chat.id, chat.title, exc)
        db.set_chat_unavailable(conn, chat.id, True)
        return SyncedChat(chat=_refresh(conn, chat), unavailable=True)
    run.peers.flush(conn)
    if fetched.complete:
        db.set_chat_progress(conn, chat.id, fetched.progress, int(time.time()))
    if chat.unavailable:
        db.set_chat_unavailable(conn, chat.id, False)
    log.info(
        "chat %s (%s): %d new messages%s",
        chat.id,
        chat.title,
        run.inserted.get(chat.id, 0),
        "" if fetched.complete else " (budget expired, will resume)",
    )
    discussion = None
    if fetched.discussion is not None:
        discussion = SyncedChat(
            chat=_refresh(conn, fetched.discussion),
            new_msg_ids=list(run.comment_ids),
            new=run.inserted.get(fetched.discussion.id, 0),
        )
    return SyncedChat(
        chat=_refresh(conn, chat),
        new_msg_ids=list(run.changes),
        new=run.inserted.get(chat.id, 0),
        complete=fetched.complete,
        discussion=discussion,
        migrated_to=migrated,
        warnings=run.warnings,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Fetched:
    """Where the incremental pass ended; ``discussion`` is the linked chat it started with."""

    progress: int
    complete: bool
    discussion: ChatRow | None


async def _fetch_new(run: _Run) -> _Fetched:
    """The incremental pass: everything after ``last_msg_id``, committed batch by batch.

    ``offset_date`` is the source's ``since`` and goes out on the first run only: from the second
    on there is a ``min_id``, which Telethon turns into an ``offset_id`` the server gives
    priority over the date. Under ``reverse=True`` the date bound is inclusive, so a message
    stamped exactly at that midnight is fetched. Telethon 1.44 hands ``offset_date`` straight to
    ``GetHistoryRequest`` with ``add_offset=-limit`` and filters no message by date itself
    (``_MessagesIter._init`` / ``_message_in_range``), so a reversed chunk is the complement of
    the server's exclusive "before this date" cut — everything from that second on. That
    ``_init`` compensates the *id* offset by hand for the same reason (``offset_id += 1`` under
    ``reverse``, so it stays exclusive) is what shows an uncompensated reversed bound keeps its
    boundary message.
    """
    chat = run.chat
    discussion = run.discussion
    progress = chat.last_msg_id
    complete = True
    batch: list[MessageRow] = []
    seen_up_to = progress
    offset_date = since_of(run.source) if progress == 0 else None
    iterator = run.client.iter_messages(
        chat.id, min_id=progress, reverse=True, offset_date=offset_date
    )
    async for msg in iterator:
        seen_up_to = max(seen_up_to, int(msg.id))
        row = run.map(msg, chat)
        if row is not None:
            batch.append(row)
            if discussion is not None:
                run.replies[row.msg_id] = replies_count(msg)
        if len(batch) < BATCH_SIZE:
            continue
        progress = await _store_batch(run, batch, progress, seen_up_to)
        batch = []
        if run.budget.expired:
            complete = False
            break
    if complete and (batch or seen_up_to > progress):
        progress = await _store_batch(run, batch, progress, seen_up_to)
        complete = progress >= seen_up_to
    return _Fetched(progress=progress, complete=complete, discussion=discussion)


async def _store_batch(run: _Run, batch: list[MessageRow], progress: int, seen_up_to: int) -> int:
    """Upsert one batch (and, for channels with comments, each post's thread) and record progress.

    Returns the new progress. Without comments it jumps to the highest message id seen, skipped
    service messages included. With comments it advances post by post, so a budget expiry
    between two posts leaves the later posts to be re-fetched next time together with their
    threads; when Telegram refuses the threads on the way the posts count as done. Only a post
    Telegram reports replies on costs a ``GetReplies`` request, and only while more replies are
    reported than are stored (the rule :func:`_refresh_comments` uses): a channel of thousands of
    posts with a handful of threads makes a handful of requests, not thousands, and a run that
    stopped halfway through a batch does not replay the threads it already has. A post whose
    thread starts later is caught by :func:`_refresh_comments` while it is among the newest.

    The progress write is in a ``finally``: the rows are committed before the threads are read,
    so a flood wait on one post's thread — which propagates out of the whole run — must still
    leave the posts before it behind, or every run replays the same satisfied requests against a
    channel Telegram is already rate-limiting.
    """
    chat = run.chat
    _track(run.changes, run.store(batch))
    if run.discussion is None:
        run.replies.clear()
        db.set_chat_progress(run.conn, chat.id, seen_up_to, chat.last_sync_at)
        return seen_up_to
    stored = db.count_comment_messages(
        run.conn,
        run.discussion.id,
        chat.id,
        [row.msg_id for row in batch if run.replies.get(row.msg_id)],
    )
    try:
        for row in batch:
            if run.budget.expired:
                break
            if run.discussion is None:
                progress = seen_up_to
                break
            if run.replies.pop(row.msg_id, 0) > stored.get(row.msg_id, 0):
                _track(run.comment_ids, await _fetch_comments(run, row.msg_id))
            progress = row.msg_id
        else:
            progress = seen_up_to
    finally:
        db.set_chat_progress(run.conn, chat.id, progress, chat.last_sync_at)
    return progress


async def _fetch_comments(run: _Run, post_id: int) -> list[int]:
    """Store the comment thread of one channel post under the discussion chat.

    Comments live in the discussion group with their own message ids; their reply headers
    point at the discussion-side copy of the post, which Telegram does not return here, so the
    channel and the post id are kept in ``comment_of_chat_id`` / ``comment_of_msg_id`` to tie the
    thread back to its post. They go in columns of their own rather than in ``topic_id``: a
    discussion group can be a forum, and there a comment's ``topic_id`` is a forum topic drawn
    from an id space that numbers from 1 just like the channel's posts. A post without a
    thread (``MsgIdInvalidError``) is skipped; a thread Telegram refuses switches the comments
    off for the rest of the run (:meth:`_Run.drop_comments`).

    The rows are stored in batches and the tail in a ``finally``, so a thread cut short — a flood
    wait partway through a long one, a cancellation — keeps the prefix it fetched instead of
    dropping everything on the floor and starting from the same place on every later run. The
    next run re-reads the thread from the top (an upsert, so nothing is stored twice) until as
    many comments are stored as Telegram reports replies.
    """
    assert run.discussion is not None
    rows: list[MessageRow] = []
    stored: list[int] = []
    try:
        async for msg in run.client.iter_messages(run.chat.id, reply_to=post_id):
            row = run.map(msg, run.discussion)
            if row is not None:
                rows.append(
                    dataclasses.replace(
                        row, comment_of_chat_id=run.chat.id, comment_of_msg_id=post_id
                    )
                )
            if len(rows) >= BATCH_SIZE:
                stored += run.store(rows)
                rows = []
    except errors.MsgIdInvalidError:
        log.debug("post %s in channel %s has no comment thread", post_id, run.chat.id)
    except UNAVAILABLE_ERRORS as exc:
        run.drop_comments(exc)
    finally:
        stored += run.store(rows)
    return stored


async def _refetch_edits(run: _Run, cfg: Config) -> list[int]:
    """Re-read the newest ``edit_refetch`` messages and rewrite only stored rows that changed.

    Messages that are not stored — history before ``since``, or anything the incremental pass
    has not reached — are left alone; this pass exists for edits, reactions and deletions only.
    For a channel with comments it also refreshes the threads of the re-fetched posts that grew
    (:func:`_refresh_comments`); the ids of those posts are returned along with the edited rows
    so their post-thread units are rebuilt.

    The stored units' reaction totals are brought up to date here as well
    (:func:`grepogram.db.refresh_unit_reactions`) and not through the rebuild that follows: a
    reaction is not part of a unit's content, so :func:`grepogram.units._apply` keeps the stored
    row when it re-cuts an identical unit, and the closed window nearly every re-fetched message
    sits in is never re-cut in the first place.

    ``seen`` is every id the iteration yielded, service messages included — :meth:`_Run.map`
    turns those into ``None`` and they are never stored, but they are still ids Telegram
    answered with, and they bound the range this pass can say anything about
    (:func:`_drop_deleted`).
    """
    if cfg.sync.edit_refetch <= 0:
        return []
    chat = run.chat
    fresh: list[MessageRow] = []
    replies: dict[int, int] = {}
    seen: set[int] = set()
    async for msg in run.client.iter_messages(chat.id, limit=cfg.sync.edit_refetch):
        seen.add(int(msg.id))
        row = run.map(msg, chat)
        if row is not None:
            fresh.append(row)
            replies[row.msg_id] = replies_count(msg)
    if not seen:
        return []
    stored = {row.msg_id: row for row in db.get_messages(run.conn, chat.id, since_msg_id=min(seen))}
    changed = [row for row in fresh if row.msg_id in stored and _differs(stored[row.msg_id], row)]
    if changed:
        log.debug(
            "chat %s: %d of %d re-fetched messages changed", chat.id, len(changed), len(fresh)
        )
    ids = run.store(changed)
    # Telegram msg ids, never the rowids `store` just returned: `units.msg_ids` is the other
    # space, and the two coincide only in a chat whose history starts at 1 (see
    # `db.refresh_unit_reactions`).
    db.refresh_unit_reactions(run.conn, chat.id, [row.msg_id for row in changed])
    await _drop_deleted(run, cfg, stored, seen)
    if run.discussion is not None:
        ids += await _refresh_comments(run, stored, replies)
    return ids


async def _drop_deleted(
    run: _Run, cfg: Config, stored: Mapping[int, MessageRow], seen: set[int]
) -> list[int]:
    """Remove the rows of the messages this re-fetch proves are gone, and re-cut their units.

    A deletion is a **set difference**, never an empty slot: ``iter_messages`` simply omits a
    deleted message rather than yielding a hole for it, so what says a stored message is gone is
    that the iteration reached its id and did not return it. The comparison is bounded to
    ``[min(seen), max(seen)]``, the range the iteration actually covered — a stored id below
    where it stopped, or above where it started, was never asked about and is evidence of
    nothing. Service messages cannot make a false positive: :func:`map_message` returns ``None``
    for them so they are never stored, and their ids are in ``seen`` regardless.

    Rows that are **comments** (``comment_of_chat_id`` set) are left alone even in a discussion
    group a source lists directly, where this pass does run over them. Dropping one here would
    re-cut the group's own window while the channel's post thread kept its text for good: a post
    thread lists only the post in ``msg_ids``, so no ``json_each`` over ``units.msg_ids`` reaches
    a comment id and nothing here could invalidate it. ``grepogram prune-deleted`` follows the
    ``comment_of_*`` pair instead and is where a deleted comment belongs.

    The order is read the rows, delete them, then re-cut — one transaction, and the rows are read
    first because :func:`grepogram.units.invalidate_units_for` needs the topic a window is scoped
    by, which lives on a row that no longer exists by then. It renders nothing from them, so a
    message that heads a reply thread does not come back in the unit its removal rebuilds; a unit
    left holding no messages is dropped instead of being rebuilt empty. Returns the ids removed.

    That transaction goes to a worker thread joined even under cancellation
    (:func:`_joined_to_thread`), like every other write a sync makes:
    :func:`grepogram.units.invalidate_units_for` re-cuts every window from the deleted message to
    the end of the chat, which is exactly the work :func:`on_chat_synced` is pushed off the event
    loop for — the client's keepalives run on while it happens.
    """
    lo, hi = min(seen), max(seen)
    gone = [
        row
        for msg_id, row in sorted(stored.items())
        if lo <= msg_id <= hi and msg_id not in seen and row.comment_of_chat_id is None
    ]
    if not gone:
        return []
    msg_ids = [row.msg_id for row in gone]
    await _joined_to_thread(
        functools.partial(_apply_drop, run, cfg, gone, msg_ids), run.budget.cancel
    )
    log.info(
        "chat %s (%s): %d messages were deleted in Telegram and dropped from the index",
        run.chat.id,
        run.chat.title,
        len(gone),
    )
    return msg_ids


def _apply_drop(run: _Run, cfg: Config, gone: Sequence[MessageRow], msg_ids: Sequence[int]) -> None:
    """Delete one re-fetch's proven-gone rows and re-cut what held them — one transaction."""
    with db.transaction(run.conn):
        db.delete_messages(run.conn, run.chat.id, list(msg_ids))
        index.index_units(run.conn, units.invalidate_units_for(run.conn, run.chat, cfg, gone))


def _differs(stored: MessageRow, fresh: MessageRow) -> bool:
    """Whether storing ``fresh`` would change ``stored``.

    Compared with the values the upsert would keep: a comment re-read as part of its discussion
    group's own history arrives with no comment relation — and, outside a forum, with no topic —
    and must not count as an edit for want of what the upsert would have preserved anyway.

    ``extracted_text`` and ``media_state`` are normalised away on both sides instead, because the
    upsert does not write them at all: a message Telegram maps carries no extracted text and
    :data:`db.MEDIA_PENDING`, so every extracted message inside the ``edit_refetch`` window would
    otherwise count as an edit on every sync and be re-cut and re-embedded forever. The "kept"
    idiom above cannot do it — it reads ``None`` as "not supplied", and a fresh row's
    ``media_state`` is ``0``.
    """
    kept = {
        field: getattr(stored, field) if getattr(fresh, field) is None else getattr(fresh, field)
        for field in ("topic_id", "comment_of_chat_id", "comment_of_msg_id")
    }
    return _comparable(dataclasses.replace(stored, id=None)) != _comparable(
        dataclasses.replace(fresh, **kept)
    )


def _comparable(row: MessageRow) -> MessageRow:
    """``row`` without the columns :func:`db.upsert_messages` never writes — see
    :func:`_differs`."""
    return dataclasses.replace(row, extracted_text=None, media_state=db.MEDIA_PENDING)


async def _refresh_comments(
    run: _Run, stored: Mapping[int, MessageRow], replies: Mapping[int, int]
) -> list[int]:
    """Re-read the comment threads of the stored posts with more replies than comments stored.

    Telegram's reply count on a post is compared with the comments held under the discussion
    chat for that post; a thread that grew is fetched again through :func:`_fetch_comments`
    (an upsert, so nothing is stored twice). Returns the ``messages.id`` of the posts whose
    threads were re-read. Stops when the budget expires or the threads become unavailable. The
    grown posts are flagged for a rebuild before their threads are read, so the post-thread
    unit follows the comments that were stored even when the read stops halfway.
    """
    assert run.discussion is not None
    counted = db.count_comment_messages(run.conn, run.discussion.id, run.chat.id, replies)
    grown = [
        post_id
        for post_id, total in replies.items()
        if post_id in stored and total > counted.get(post_id, 0)
    ]
    db.mark_unindexed(run.conn, [row_id for p in grown if (row_id := stored[p].id) is not None])
    touched: list[int] = []
    for post_id in grown:
        if run.budget.expired or run.discussion is None:
            break
        ids = await _fetch_comments(run, post_id)
        _track(run.comment_ids, ids)
        row_id = stored[post_id].id
        if ids and row_id is not None:
            touched.append(row_id)
    if grown:
        log.debug(
            "channel %s: %d of %d re-fetched posts had new comments; %d threads re-read",
            run.chat.id,
            len(grown),
            len(replies),
            len(touched),
        )
    return touched


async def _check_migration(client: Any, conn: sqlite3.Connection, chat: ChatRow) -> ChatRow | None:
    """Detect a legacy group upgraded to a supergroup; upsert and return the new chat row."""
    try:
        entity = await client.get_entity(chat.id)
    except ValueError as exc:
        log.warning("chat %s (%s): cannot check for migration: %s", chat.id, chat.title, exc)
        return None
    target = getattr(entity, "migrated_to", None)
    if target is None:
        return None
    new_id = dialogs.peer_id(types.PeerChannel(int(target.channel_id)))
    new_chat = db.get_chat(conn, new_id)
    if new_chat is None:
        try:
            entity = await client.get_entity(new_id)
        except ValueError as exc:
            log.warning(
                "chat %s (%s) migrated to %s, which cannot be resolved: %s",
                chat.id,
                chat.title,
                new_id,
                exc,
            )
            return None
        new_chat = db.upsert_chat(conn, _chat_row_from_entity(entity, chat.source_id))
    db.set_chat_migrated(conn, chat.id, new_id)
    log.info("chat %s (%s) migrated to supergroup %s", chat.id, chat.title, new_id)
    return new_chat


async def link_discussion_chat(
    client: Any, conn: sqlite3.Connection, channel: ChatRow
) -> ChatRow | None:
    """Upsert the channel's linked discussion group as its own ``chats`` row.

    The row carries ``discussion_of = channel.id`` and the ``source_id``
    :func:`~grepogram.sources.discussion_source_id` decides: a group a source covers on its own
    keeps that source, and a group known only through this link takes the linking channel's, so
    it moves along when another channel takes it over and is removed together with the source
    whose comments it holds. Every message it holds stays either way. ``None`` when the channel
    has no discussion group; :class:`DiscussionUnavailable` when Telegram will not resolve the
    group (private, or the account is not a member) — the channel's own errors propagate.

    ``GetFullChannelRequest`` answers a channel without a discussion group with
    ``linked_chat_id = None``, and one whose group changed with the new id, so both cases are
    read off the same field: the link is re-pointed or cleared here (:func:`_relink_discussion`)
    rather than left where an earlier run put it. A group Telegram names but will not resolve
    still clears a link that points at a *different* group — that one is demonstrably not the
    channel's any more — while a link to the very group that failed to resolve is left untouched
    and retried next run.
    """
    full = await client(functions.channels.GetFullChannelRequest(channel.id))
    linked = getattr(full.full_chat, "linked_chat_id", None)
    if not linked:
        _relink_discussion(conn, channel, None)
        log.info(
            "channel %s (%s) has no discussion group; comments skipped", channel.id, channel.title
        )
        return None
    linked_id = dialogs.peer_id(types.PeerChannel(int(linked)))
    entity = next((c for c in full.chats if dialogs.peer_id(c) == linked_id), None)
    if entity is None:
        try:
            entity = await client.get_entity(linked_id)
        except _DISCUSSION_ERRORS as exc:
            _drop_stale_link(conn, channel, linked_id)
            raise DiscussionUnavailable(
                f"cannot resolve discussion group {linked_id}: {exc}"
            ) from exc
    with db.transaction(conn):
        source_id = discussion_source_id(db.get_chat(conn, linked_id), channel)
        stored = db.upsert_chat(conn, _chat_row_from_entity(entity, source_id))
        _relink_discussion(conn, channel, stored.id)
    return _refresh(conn, stored)


def _drop_stale_link(conn: sqlite3.Connection, channel: ChatRow, linked_id: int) -> None:
    """Unlink the stored discussion group of ``channel`` when it is not ``linked_id``.

    The group Telegram now names could not be resolved, so it cannot be stored — but the one
    stored is not the channel's any more, and keeping the link would leave its comments in
    :func:`grepogram.search.thread` and in the channel's post threads for as long as the group
    stays unresolvable. A link to ``linked_id`` itself is a group that is still the channel's and
    only unreachable this run: it is left alone rather than dropped and rebuilt on every retry.
    """
    stored = db.get_discussion_chat(conn, channel.id)
    if stored is not None and stored.id != linked_id:
        _relink_discussion(conn, channel, None)


def _relink_discussion(conn: sqlite3.Connection, channel: ChatRow, keep: int | None) -> None:
    """Point the channel's ``discussion_of`` at ``keep`` and undo what a former group left.

    A group Telegram unlinked, or replaced with another one, keeps every message it holds: they
    are a real group's real messages, and the group stays indexed as the chat it is. They stop
    being the channel's comments, though, so the post threads they fed are dropped here, with
    their index rows, and the posts they hang under are flagged for a rebuild — the next
    :func:`index_pending` cuts those posts again, with the new group's comments or with none
    (:func:`_drop_comment_units`). Waiting for that rebuild to drop the threads would leave them
    quoting a group that can be deleted in the meantime, and then nothing would say they exist.
    The comments stop naming the channel and its post in the same step
    (:func:`grepogram.db._clear_comment_mapping`) — that pair names a post in the old channel's
    id space, and post ids start at 1 in every channel. Nothing of the group is rebuilt for it:
    a comment's windows and threads never read the comment relation, so every one of them stays
    where it is and stays searchable through it from the moment this commits.

    A group can only be linked to one channel at a time, so ``keep`` may be the group another
    channel held until now; that channel's post threads, and the mapping that made the group's
    rows its comments, go the same way — before the link moves, while the old channel's posts
    are still there to say which topics were its.

    The whole transition is one transaction — the link that moves and the ``indexed = 0`` flags
    that say which posts it invalidated — so a process killed inside it leaves either both or
    neither. Half of it would be a channel whose post threads still hold the comments of a group
    that is not its own, with nothing left to say those posts need a rebuild: the next run reads
    the link, finds it already where it belongs and rebuilds nothing.
    """
    with db.transaction(conn):
        if keep is not None:
            taken_from = db.get_chat(conn, keep)
            if taken_from is not None and taken_from.discussion_of not in (None, channel.id):
                _drop_comment_units(conn, taken_from.discussion_of, keep)
        for dropped in db.set_discussion_chat(conn, channel.id, keep):
            _drop_comment_units(conn, channel.id, dropped)


def _drop_comment_units(conn: sqlite3.Connection, channel_id: int | None, group_id: int) -> None:
    """Drop the post threads of ``channel_id`` that carry ``group_id``'s comments, and the
    mapping that made them comments.

    :func:`grepogram.db.drop_comment_units` deletes them with their index rows, flags the
    posts for a rebuild and clears the post id off the group's rows — the same call
    :func:`grepogram.db.delete_chat` makes when the group itself goes. The threads cannot be left
    to the rebuild the flag asks for: nothing in a thread names the group it quotes, only the
    link does, so a group deleted between the unlink and that rebuild would leave them with no
    link to find them by.
    """
    if channel_id is None:
        return
    flagged = db.drop_comment_units(conn, channel_id, group_id)
    log.info(
        "channel %s no longer has discussion group %s; %d of its posts are rebuilt without the "
        "comments stored there",
        channel_id,
        group_id,
        flagged,
    )


def _chat_row_from_entity(entity: Any, source_id: str | None) -> ChatRow:
    info = dialogs.dialog_info(entity)
    return ChatRow(
        id=info.id,
        type=info.type,
        title=info.title,
        username=info.username,
        is_forum=info.is_forum,
        source_id=source_id,
    )


def _refresh(conn: sqlite3.Connection, chat: ChatRow) -> ChatRow:
    return db.get_chat(conn, chat.id) or chat


# --- all chats -------------------------------------------------------------------------------


def on_chat_synced(
    conn: sqlite3.Connection, chat: ChatRow, cfg: Config, new_msg_ids: list[int]
) -> None:
    """Bring the derived data of a chat up to date after these ``messages.id`` rows changed.

    ``new_msg_ids`` are the rowids of the inserted and edited rows. The step is one ordered
    function rather than a list of independent hooks because the indexer needs what the unit
    rebuild returns: :func:`~grepogram.units.rebuild_for_chat` first, then
    :func:`~grepogram.index.index_chat` over the same messages and the rebuild's
    :class:`~grepogram.units.UnitDelta`, and finally the rows are marked indexed
    (:func:`grepogram.db.mark_indexed`) so a later run does not rebuild them again. Embedding
    the dirty units is not per chat — it runs once at the end of :func:`sync_all` when an
    embedder is given.

    All three run in one transaction, which is what makes ``indexed`` a true two-phase marker:
    the units, the index rows and the cleared flag become visible together, so a run that dies
    anywhere in here leaves the rows flagged and nothing half-derived behind. The connection's
    lock is held throughout — about 3.8 s for a first sync of 100 000 messages, against 2.1 s for
    the rebuild alone — so an in-process search waits for the step instead of reading an index
    that is missing the units it just wrote.
    """
    with db.transaction(conn):
        delta = units.rebuild_for_chat(conn, chat, cfg, new_msg_ids)
        index.index_chat(conn, chat, new_msg_ids, delta)
        db.mark_indexed(conn, new_msg_ids)


async def _joined_to_thread[T](job: Callable[[], T], abort: Callable[[], None] | None = None) -> T:
    """Run ``job`` on a worker thread and never leave it writing on its own.

    ``await asyncio.to_thread(...)`` submits the job before it suspends, so a cancellation —
    what an ``anyio`` ``CancelScope`` around an MCP tool call delivers — abandons the future
    while the thread carries on committing, and the :class:`SyncLock` the caller releases on its
    way out no longer covers it. The job is shielded from the cancellation and joined through a
    plain :class:`threading.Event` instead: the wait needs no ``await``, which a cancelled scope
    would raise out of immediately.

    The wait has no bound, it only logs every :data:`JOIN_LOG_EVERY` seconds. A bound would give
    the lock up over a live writer exactly when the writer is slowest — the rebuild of a huge
    chat, an embedding backlog — which is the case it exists for; the next process would then
    start syncing, removing or embedding against a database this one is still writing. ``abort``
    is what keeps the wait short instead: it asks the job to stop at its next boundary
    (:meth:`SyncBudget.cancel` for the embedding step, which checks the budget between batches),
    while the indexing step is a single transaction that ends on its own. A writer is detached
    only when the process is killed outright, and then the ``messages.indexed`` flags its
    transaction never cleared make the next run rebuild and repair what it left
    (:func:`index_pending`, :func:`grepogram.index.repair_unit_index`).
    """
    finished = threading.Event()

    def run() -> T:
        try:
            return job()
        finally:
            finished.set()

    future = asyncio.get_running_loop().run_in_executor(None, run)
    try:
        return await asyncio.shield(future)
    except BaseException:
        if abort is not None:
            abort()
        while not finished.wait(JOIN_LOG_EVERY):
            log.warning(
                "a background index job is still running after the run was cancelled; "
                "the sync lock is held until it finishes"
            )
        raise


async def index_pending(conn: sqlite3.Connection, cfg: Config, chat: ChatRow) -> None:
    """Rebuild and index every stored row of ``chat`` — and of its discussion group, when it is
    a channel — that no rebuild has covered yet, off the event loop.

    Runs after every fetch, finished or not: the rows a batch committed before a flood wait,
    an RPC error or a cancellation get their units and ``msg_fts`` entries now, and rows a crash
    left behind get them on the next run. The chat rows are re-read, since a fetch may have
    changed them or linked the discussion group; a chat removed meanwhile has nothing to index.
    This is the step a cancelled sync runs on its way out, so the worker thread is joined rather
    than abandoned (:func:`_joined_to_thread`). Rows flagged in a chat this run holds no handle on
    — a discussion group it was unlinked from meanwhile — are :func:`index_stranded`'s to repair
    at the end of the run.
    """
    for row in (db.get_chat(conn, chat.id), db.get_discussion_chat(conn, chat.id)):
        if row is None:
            continue
        pending = db.unindexed_message_ids(conn, row.id)
        if pending:
            await _joined_to_thread(functools.partial(on_chat_synced, conn, row, cfg, pending))


async def index_stranded(
    conn: sqlite3.Connection, cfg: Config, limit: int = STRANDED_CHATS
) -> None:
    """Rebuild and index rows left flagged in chats the run itself never went through.

    :func:`index_pending` covers a chat and the discussion group it holds *now*, which is every
    chat a run writes to — but not every chat the flag can be set in. A group a channel was
    unlinked from, or that another channel took over, is no longer reachable from either the
    source list or the link, so rows a killed run left behind in it would stay ``indexed = 0``
    for good and its units would keep whatever that run half-wrote. ``messages.indexed`` says
    where the work is whatever stranded it (:func:`grepogram.db.chats_with_unindexed`), and this
    is the sweep that acts on it.

    At most ``limit`` chats per run, in id order, so a database with a wide backlog — a run
    killed mid-rebuild leaves every chat it had reached flagged — heals over a few runs instead
    of turning each one into a full rebuild. It runs after the per-chat passes, which have
    cleared the run's own chats by then, so what is left is genuinely stranded.
    """
    for chat_id in db.chats_with_unindexed(conn)[:limit]:
        chat = db.get_chat(conn, chat_id)
        pending = [] if chat is None else db.unindexed_message_ids(conn, chat_id)
        if chat is None or not pending:
            continue
        log.info(
            "chat %s (%s): %d rows an earlier run left unindexed; rebuilding them",
            chat.id,
            chat.title,
            len(pending),
        )
        await _joined_to_thread(functools.partial(on_chat_synced, conn, chat, cfg, pending))


async def recut_pending_chats(
    conn: sqlite3.Connection,
    cfg: Config,
    budget: SyncBudget,
    embedder: Embedder | None = None,
) -> int:
    """Re-cut the chats whose units predate :data:`grepogram.units.RECIPE_VERSION`; how many moved.

    Its own step after :func:`index_stranded`, so that sweep cannot rebuild a chat this pass has
    just finished, and never hooked into :func:`on_chat_synced`: nothing is left flagged outside
    the transaction that rebuilds it, so no later run — least of all a 20-second auto-sync inside
    a ``search`` — inherits a whole-index backlog to drain. Who may start one is decided by the
    caller, not by how many seconds are left: :func:`sync_all` takes ``recut``, and the automatic
    sync inside an MCP ``search`` is the one caller that passes ``False``.

    The procedure, in order:

    #. a recorded recipe equal to :data:`~grepogram.units.RECIPE_VERSION` means there is nothing
       to do. ``None`` — a v0.1.1 index — is a mismatch, not a fresh database:
       :func:`grepogram.db.migrate` stamps the ones built from empty;
    #. the candidates are **every** chat in the index, not the run's queue: a channel's
       discussion group known only through the link never appears in ``resolve_sources``' output,
       and its windows hold every comment the index has. At most :data:`RECUT_CHATS_PER_RUN` of
       them move, while the budget lasts;
    #. each chat is one transaction on a worker thread joined even under cancellation
       (:func:`_joined_to_thread`, with :meth:`SyncBudget.cancel` as the abort): re-read the chat
       row and skip it when it is gone, :func:`grepogram.units.recut_chat`, index the delta,
       repair the unit index, write the marker. The indexing lives here rather than in
       :mod:`grepogram.units`, which cannot import :mod:`grepogram.index`, and **no message ids
       are passed**: a re-cut changes no message text, so rewriting an ``msg_fts`` row per id
       would be the largest wasted cost available;
    #. once every chat carries the marker, the recipe is recorded and the markers are dropped in
       one transaction — tidy-up, not correctness. Until then a run cut short leaves the finished
       chats marked and the next one picks up only the rest.
    """
    target = units.RECIPE_VERSION
    if db.unit_recipe(conn) == target:
        return 0
    marker = str(target)
    markers = db.recut_markers(conn)
    pending = [chat for chat in db.list_chats(conn) if markers.get(chat.id) != marker]
    recut = 0
    for chat in pending[:RECUT_CHATS_PER_RUN]:
        if budget.expired:
            break
        done = await _joined_to_thread(
            functools.partial(_recut_one, conn, cfg, chat), budget.cancel
        )
        recut += int(done)
    if recut and embedder is None:
        log.warning(
            "%d chats were re-cut and their old units' vectors went with them, and this run has "
            "no embedding model; run `grepogram embed` to make them searchable by meaning again",
            recut,
        )
    _finish_recut(conn, target)
    return recut


def _recut_one(conn: sqlite3.Connection, cfg: Config, chat: ChatRow) -> bool:
    """Re-cut one chat, index the result and mark it done; ``False`` when the chat is gone.

    The chat row is re-read the way :func:`index_pending` does it: a chat removed under the pass
    would otherwise reach :func:`grepogram.db.insert_units` with no parent row and raise
    ``IntegrityError`` outside the chat loop's guard, escaping :func:`sync_all` as a traceback.
    """
    with db.transaction(conn):
        row = db.get_chat(conn, chat.id)
        if row is None:
            log.debug("chat %s was removed before its re-cut; skipped", chat.id)
            return False
        delta = units.recut_chat(conn, row, cfg)
        index.index_units(conn, delta)
        index.repair_unit_index(conn, row.id)
        db.set_recut_marker(conn, row.id, units.RECIPE_VERSION)
    log.info(
        "chat %s (%s): re-cut %d units into %d for unit recipe v%s",
        chat.id,
        chat.title,
        len(delta.deleted_ids),
        len(delta.inserted_ids),
        units.RECIPE_VERSION,
    )
    return True


def _finish_recut(conn: sqlite3.Connection, target: int) -> None:
    """Record the recipe and drop the markers once no chat is left unmarked.

    Completion is "no chat is unmarked", so an index with no chats at all is complete on the
    spot. The two writes are one transaction: a recorded recipe next to stale markers would make
    the next bump skip the chats those markers name.
    """
    marker = str(target)
    markers = db.recut_markers(conn)
    if any(markers.get(chat.id) != marker for chat in db.list_chats(conn)):
        return
    with db.transaction(conn):
        db.set_unit_recipe(conn, target)
        db.clear_recut_markers(conn)
    log.info("every chat is cut with unit recipe v%s", target)


ConfigSource = Config | Callable[[], Config]
"""A config, or a loader called once the :class:`SyncLock` is held (see :func:`sync_all`)."""


async def sync_all(
    client: Any,
    conn: sqlite3.Connection,
    cfg: ConfigSource,
    paths: Paths,
    budget: SyncBudget,
    embedder: Embedder | None = None,
    recut: bool = True,
) -> SyncReport:
    """Sync every configured source within ``budget``; the client must be connected.

    Takes the :class:`SyncLock` (:class:`SyncInProgress` when another process syncs), resolves
    the sources into chats, and processes them oldest-synced first (never-synced chats before
    all others) until the budget expires — the current batch is always committed, and the chats
    not finished are reported in ``chats_remaining``. ``cfg`` may be a loader instead of a
    config: it is called after the lock is taken, so a source removed while the caller was still
    loading its model or connecting — ``sources_remove`` holds the same lock for its delete and
    its config save — is not resolved and fetched again from a stale snapshot. Callers that hold
    a config they own (tests, a one-shot script) pass it as it is. Every stored row no rebuild
    has covered yet goes through :func:`on_chat_synced` (:func:`index_pending`) — after each
    chat's fetch, however it ended, and for the chats the run did not reach. A flood wait
    Telegram will not let the client sleep through stops the run with a warning; a chat Telegram
    refuses is reported in ``unavailable``; an
    unauthorized session raises :class:`~telethon.errors.UnauthorizedError`, which the
    :func:`~grepogram.tg.connected` block every caller runs in turns into
    :class:`~grepogram.tg.AuthRequired`. The end of the run is stamped in ``meta.last_sync_run``
    whether or not a chat completed, so a caller deciding whether the index is stale does not
    retry a run that has nothing to finish.

    ``recut`` says whether this run may start the one-time unit re-cut
    (:func:`recut_pending_chats`). It is a property of the caller, not of the budget: an
    explicit sync — ``grepogram sync``, the MCP ``sync`` tool — is deliberate and makes whatever
    progress its budget allows, bounded at :data:`RECUT_CHATS_PER_RUN` and resumable through the
    per-chat markers; the automatic sync inside an MCP ``search`` passes ``False`` so a search
    never rebuilds units incidentally, however large the user has set
    ``search.auto_sync_budget_s``.

    With an ``embedder`` the run ends by embedding the dirty units under the same budget and
    lock (:func:`~grepogram.index.embed_dirty_units`); units the budget leaves unembedded and a
    changed embedding model become ``warnings`` — the messages are synced either way.
    """
    with SyncLock(paths):
        current = cfg if isinstance(cfg, Config) else cfg()
        report = await _sync_chats(client, conn, current, budget)
        if recut:
            await recut_pending_chats(conn, current, budget, embedder)
        db.set_last_sync_run(conn, int(time.time()))
        if embedder is None:
            return report
        return await _embed_after_sync(conn, embedder, budget, report)


async def _embed_after_sync(
    conn: sqlite3.Connection, embedder: Embedder, budget: SyncBudget, report: SyncReport
) -> SyncReport:
    """Embed what the sync left dirty, off the event loop so the client's keepalives run on.

    Joined like the indexing step (:func:`_joined_to_thread`): a cancelled run releases the
    :class:`SyncLock` only once the worker thread has stopped writing vectors. This is the long
    job of the two, so the cancellation expires the budget it paces itself against
    (:meth:`SyncBudget.cancel`) and it stops after the batch it is on rather than after the
    backlog.
    """
    warnings = list(report.warnings)
    try:
        embedded = await _joined_to_thread(
            functools.partial(index.embed_dirty_units, conn, embedder, budget=budget),
            budget.cancel,
        )
    except index.EmbeddingSpaceMismatch as exc:
        log.warning("dense index not updated: %s", exc)
        warnings.append(f"dense index not updated: {exc}")
        return dataclasses.replace(report, warnings=warnings)
    pending = db.count_dirty_units(conn)
    if pending:
        warnings.append(
            f"{pending} units are not embedded yet (sync budget expired); "
            "run `grepogram embed` or sync again"
        )
    log.info("sync: embedded %d units, %d pending", embedded, pending)
    return dataclasses.replace(report, warnings=warnings)


@dataclass(slots=True)
class _Tally:
    """What the chat loop accumulates on its way to a :class:`SyncReport`."""

    new: int = 0
    done: list[int] = field(default_factory=list)
    remaining: list[int] = field(default_factory=list)
    unavailable: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record(self, chat_id: int, synced: SyncedChat) -> None:
        """Count one finished chat: its new messages, its warnings and where it ended up."""
        self.new += synced.new + (0 if synced.discussion is None else synced.discussion.new)
        self.warnings.extend(synced.warnings)
        if synced.unavailable:
            self.unavailable.append(chat_id)
        elif synced.complete:
            self.done.append(chat_id)
        else:
            self.remaining.append(chat_id)

    def report(self) -> SyncReport:
        log.info(
            "sync: %d new messages, %d chats done, %d remaining, %d unavailable",
            self.new,
            len(self.done),
            len(self.remaining),
            len(self.unavailable),
        )
        return SyncReport(
            new=self.new,
            chats_done=self.done,
            chats_remaining=self.remaining,
            unavailable=self.unavailable,
            warnings=self.warnings,
        )


def _record_failure(tally: _Tally, chat: ChatRow, exc: Exception) -> bool:
    """Turn one chat's failure into warnings; returns whether the whole run must stop.

    A flood wait is about the account rather than the chat, so the run ends and every chat left
    is reported as remaining. An RPC error costs this chat its run, and a chat whose row
    disappeared under the sync — a removal that got past the lock, or a hand-edited database —
    costs it silently: it is gone, so there is nothing left to resume. An unauthorized session
    never reaches here; :func:`_sync_chats` re-raises it.
    """
    if isinstance(exc, errors.FloodWaitError):
        log.warning("flood wait of %ss on chat %s; stopping this run", exc.seconds, chat.id)
        tally.warnings.append(
            f"flood wait: Telegram asks to wait {exc.seconds}s before more history "
            "requests; run sync again later"
        )
        return True
    if isinstance(exc, errors.RPCError):
        log.warning("chat %s (%s): %s; skipped this run", chat.id, chat.title, exc)
        tally.warnings.append(f"chat {chat.id} ({chat.title}): {exc}")
        tally.remaining.append(chat.id)
        return False
    log.warning("chat %s (%s) was removed during the sync: %s", chat.id, chat.title, exc)
    tally.warnings.append(f"chat {chat.id} ({chat.title}) was removed while it was being synced")
    return False


async def _sync_chats(
    client: Any, conn: sqlite3.Connection, cfg: Config, budget: SyncBudget
) -> SyncReport:
    """The chat loop of :func:`sync_all`.

    The unit rebuild runs on a worker thread so the loop keeps serving the client's keepalives
    while a big chat is cut into units; it runs for every chat once its fetch is over — after
    the last batch, or after the batch a flood wait, an RPC error or a cancellation interrupted,
    which stays committed and searchable either way — and at the end for the chats the budget
    or a flood wait kept the run from reaching, whose pending rows come from an earlier run. The
    run ends with :func:`index_stranded`, a bounded sweep of the chats no source and no link
    leads to any more but that still hold flagged rows. Failures are :func:`_record_failure`'s to
    describe; the tally becomes the report.
    """
    me = _self_row(await client.get_me())
    queue = deque(sorted(await resolve_sources(cfg, client, conn), key=_sync_order))
    queued = {chat.id for chat in queue}
    sources = {source.id: source for source in cfg.sources}
    tally = _Tally()
    processed: set[int] = set()
    deferred: list[ChatRow] = []
    while queue:
        chat = queue.popleft()
        queued.discard(chat.id)
        processed.add(chat.id)
        source = sources.get(chat.source_id or "")
        if source is None:
            log.debug("chat %s has no configured source; skipped", chat.id)
            continue
        if budget.expired:
            tally.remaining.append(chat.id)
            deferred.append(chat)
            continue
        _cap_flood_sleep(client, cfg.sync, budget)
        try:
            try:
                synced = await sync_chat(client, conn, chat, source, budget, cfg=cfg, me=me)
            finally:
                await index_pending(conn, cfg, chat)
        except errors.UnauthorizedError:
            raise
        except (errors.RPCError, sqlite3.IntegrityError) as exc:
            if _record_failure(tally, chat, exc):
                tally.remaining.extend([chat.id, *(c.id for c in queue)])
                break
            continue
        tally.record(chat.id, synced)
        migrated = synced.migrated_to
        if migrated is not None and migrated.id not in processed and migrated.id not in queued:
            queue.append(migrated)
            queued.add(migrated.id)
    for chat in [*deferred, *queue]:
        await index_pending(conn, cfg, chat)
    await index_stranded(conn, cfg)
    return tally.report()


def _cap_flood_sleep(client: Any, sync_cfg: SyncCfg, budget: SyncBudget) -> None:
    """Never let Telethon sleep through a flood wait longer than the budget has left.

    ``flood_sleep_threshold`` is what the client sleeps through on its own; a wait above it
    raises :class:`~telethon.errors.FloodWaitError`, which :func:`_sync_chats` turns into a
    warning. Inside a bounded run the threshold shrinks with the time left, so a 20-second
    auto-sync never blocks a search for the two minutes the config allows an unattended sync.
    """
    remaining = budget.remaining
    threshold = sync_cfg.flood_sleep_threshold
    if remaining is not None:
        threshold = min(threshold, math.ceil(remaining))
    client.flood_sleep_threshold = threshold


def _sync_order(chat: ChatRow) -> tuple[int, int, int]:
    """Never-synced chats first, then by ``last_sync_at`` ascending, ties by id."""
    if chat.last_sync_at is None:
        return (0, 0, chat.id)
    return (1, chat.last_sync_at, chat.id)


def _self_row(me: Any) -> UserRow | None:
    users = collect_users([me])
    return next(iter(users.values()), None)


# --- the deletion sweep ----------------------------------------------------------------------


PRUNE_BATCH = 100
"""Stored ids one request of the sweep asks about — the most ``messages.getMessages`` takes."""


@dataclass(slots=True)
class _PruneTally:
    """What the sweep accumulates on its way to a :class:`PruneReport`."""

    removed: int = 0
    checked: int = 0
    done: list[int] = field(default_factory=list)
    remaining: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def report(self) -> PruneReport:
        log.info(
            "prune-deleted: %d of %d checked messages were gone, %d chats swept, %d left",
            self.removed,
            self.checked,
            len(self.done),
            len(self.remaining),
        )
        return PruneReport(
            removed=self.removed,
            checked=self.checked,
            chats_done=self.done,
            chats_remaining=self.remaining,
            warnings=self.warnings,
        )


async def prune_deleted(
    client: Any,
    conn: sqlite3.Connection,
    cfg: Config,
    paths: Paths,
    budget: SyncBudget,
    *,
    chat_id: int | None = None,
) -> PruneReport:
    """Ask Telegram about every stored message and drop the ones it no longer has.

    What :func:`_drop_deleted` cannot reach. That pass sees only the ``edit_refetch`` newest
    messages of a chat and reads a deletion as a set difference, because ``iter_messages`` omits
    a deleted message rather than yielding a hole for it. This one asks by
    id — ``client.get_messages(chat_id, ids=[…])`` answers one slot per id and fills a deleted one
    with ``MessageEmpty`` — so **here an empty slot is the deletion signal** (:func:`_empty_slots`
    is where that is read, and where an answer that does not line up with the question is refused
    instead). Nothing else counts as evidence: an ``RPCError``, a chat that went private
    mid-sweep, a flood wait, all end the chat's turn with nothing removed.

    It is a whole-index pass of about one request per hundred stored messages, so it is never
    automatic and never an MCP tool: like ``sources prune``, deleting indexed history stays a
    deliberate CLI action (``grepogram prune-deleted``). ``budget`` is in seconds like every
    other pass, the client's ``flood_sleep_threshold`` is capped against what is left of it
    (:func:`_cap_flood_sleep`), and the whole sweep runs under the :class:`SyncLock` — it deletes
    messages, cuts units and writes index rows, and ``db.Connection``'s lock only serialises
    threads within one process.

    Progress is a ``meta`` cursor per chat (:func:`grepogram.db.prune_cursor`) written in the
    same transaction as the removals it earned, so a run stopped by its budget or by a flood wait
    keeps every batch it finished and the next one carries on from the id it reached rather than
    from the top.

    ``chat_id`` narrows the sweep to one chat **and the discussion group it links**, because a
    deleted comment is only reachable from the group: a post thread lists the post alone in
    ``msg_ids``, so no ``json_each`` over ``units.msg_ids`` finds a comment id, and the way back
    to the thread is the ``comment_of_*`` pair on the comment's own row
    (:func:`_invalidate_comment_posts`).
    """
    with SyncLock(paths):
        targets = _sweep_targets(conn, chat_id)
        tally = _PruneTally()
        for position, chat in enumerate(targets):
            if budget.expired:
                tally.remaining.extend(rest.id for rest in targets[position:])
                break
            _cap_flood_sleep(client, cfg.sync, budget)
            try:
                complete = await _sweep_chat(client, conn, cfg, chat, budget, tally)
            except errors.FloodWaitError as exc:
                log.warning("flood wait of %ss on chat %s; stopping this run", exc.seconds, chat.id)
                tally.warnings.append(
                    f"flood wait: Telegram asks to wait {exc.seconds}s before more requests; "
                    "run `grepogram prune-deleted` again later"
                )
                tally.remaining.extend(rest.id for rest in targets[position:])
                break
            except errors.RPCError as exc:
                log.warning(
                    "chat %s (%s): %s; nothing was removed from it", chat.id, chat.title, exc
                )
                tally.warnings.append(f"chat {chat.id} ({chat.title}): {exc}")
                tally.remaining.append(chat.id)
                continue
            (tally.done if complete else tally.remaining).append(chat.id)
        return tally.report()


def _sweep_targets(conn: sqlite3.Connection, chat_id: int | None) -> list[ChatRow]:
    """The chats one sweep walks, in id order; ``chat_id`` narrows it to one and its group.

    Every indexed chat by default, discussion groups included — a group known only through a
    channel's link is in ``db.list_chats`` like any other chat, which is what makes a deleted
    comment this pass's to find without a special case.

    Two kinds of chat are left out rather than asked about, because Telegram's answer for them
    would say nothing about deletions: an ``import:`` source, where every id would come back
    empty and the whole chat would be dropped, and one already known to be unavailable
    (:func:`refetchable`).
    """
    if chat_id is None:
        chats = db.list_chats(conn)
    else:
        found = (db.get_chat(conn, chat_id), db.get_discussion_chat(conn, chat_id))
        chats = sorted((chat for chat in found if chat is not None), key=lambda chat: chat.id)
    return [chat for chat in chats if refetchable(chat)]


def refetchable(chat: ChatRow) -> bool:
    """Whether a pass may ask Telegram about this chat's stored messages again.

    Two kinds of chat are left alone rather than asked about. One whose history never came from
    Telegram at all (an ``import:`` source) has no ids Telegram knows: the sweep would read every
    empty slot as a deletion and drop the whole chat, and the extraction pass would re-fetch a
    peer the account cannot even resolve — for which Telethon raises a plain ``ValueError``. One
    already known to be unavailable would cost a refused request per batch.

    Shared by :func:`_sweep_targets` and :func:`grepogram.media.run`, the two passes that
    re-fetch stored rows by id rather than following a chat's history forward.
    """
    if chat.unavailable:
        log.debug("chat %s is unavailable; it was left alone", chat.id)
        return False
    if (chat.source_id or "").startswith(IMPORT_PREFIX):
        log.debug("chat %s was imported, not synced; it was left alone", chat.id)
        return False
    return True


async def _sweep_chat(
    client: Any,
    conn: sqlite3.Connection,
    cfg: Config,
    chat: ChatRow,
    budget: SyncBudget,
    tally: _PruneTally,
) -> bool:
    """One chat from its cursor on; ``True`` once the sweep has reached the end of its history.

    A page of stored ids, one request, one transaction: the ids that came back empty are deleted,
    the units holding them are cut again and the cursor moves to the last id of the page. The
    write goes to a worker thread that is joined even under cancellation
    (:func:`_joined_to_thread`), like every other write a sync makes.

    An answer that does not line up with the page is not an answer: the chat's turn ends with its
    cursor untouched, so the next run asks the same page again instead of taking the silence for
    a hundred deletions. ``checked`` counts the pages that *were* answered, for the same reason —
    a refused page has told the report nothing, and the next run asks about it again.
    """
    cursor = db.prune_cursor(conn, chat.id)
    while not budget.expired:
        page = db.message_ids_after(conn, chat.id, cursor, PRUNE_BATCH)
        if not page:
            db.clear_prune_cursor(conn, chat.id)
            return True
        gone = _empty_slots(page, await client.get_messages(chat.id, ids=page))
        if gone is None:
            log.warning(
                "chat %s (%s): Telegram's answer did not line up with the %d ids asked about; "
                "nothing was removed",
                chat.id,
                chat.title,
                len(page),
            )
            tally.warnings.append(
                f"chat {chat.id} ({chat.title}): Telegram's answer did not line up with the "
                f"{len(page)} ids asked about; nothing was removed"
            )
            return False
        tally.checked += len(page)
        cursor = page[-1]
        tally.removed += await _joined_to_thread(
            functools.partial(_prune_batch, conn, cfg, chat, gone, cursor), budget.cancel
        )
    return False


def _empty_slots(page: Sequence[int], answer: Any) -> list[int] | None:
    """The ids of ``page`` Telegram answered nothing for, or ``None`` when it did not answer.

    ``messages.getMessages`` returns one slot per id asked about and fills the slot of a message
    that is gone with ``MessageEmpty``, which Telethon hands over as ``None`` — so an empty slot
    is a deletion, the one place in grepogram where that is true (:func:`_drop_deleted` reads a
    set difference instead, because ``iter_messages`` yields no slot at all for a deleted
    message).

    That reading only holds while the answer lines up with the question. Anything else — a
    shorter list, a single message where a list was asked for, ``None`` — is read as no answer
    and removes nothing; the ids of a shortened answer would otherwise all look deleted at once.
    """
    if not isinstance(answer, list) or len(answer) != len(page):
        return None
    alive = {
        int(msg.id) for msg in answer if msg is not None and not isinstance(msg, types.MessageEmpty)
    }
    return [msg_id for msg_id in page if msg_id not in alive]


def _prune_batch(
    conn: sqlite3.Connection,
    cfg: Config,
    chat: ChatRow,
    gone: Sequence[int],
    cursor: int,
) -> int:
    """Drop one page's deleted messages, re-cut what held them, move the cursor — one transaction.

    The order is :func:`_drop_deleted`'s: read the rows, delete them, then invalidate. The rows
    are read first because :func:`grepogram.units.invalidate_units_for` needs the topic a window
    is scoped by, which lives on a row that no longer exists by then; it renders nothing from
    them, so a deleted message that heads a reply thread does not come straight back in the unit
    its removal rebuilds. The cursor is written here rather than after the commit, so a crash
    never leaves a chat marked past ids whose deletions were rolled back.
    """
    with db.transaction(conn):
        rows = [row for _, row in sorted(db.get_messages_by_msg_id(conn, chat.id, gone).items())]
        if rows:
            db.delete_messages(conn, chat.id, [row.msg_id for row in rows])
            index.index_units(conn, units.invalidate_units_for(conn, chat, cfg, rows))
            _invalidate_comment_posts(conn, cfg, rows)
            log.info(
                "chat %s (%s): %d messages were deleted in Telegram and dropped from the index",
                chat.id,
                chat.title,
                len(rows),
            )
        db.set_prune_cursor(conn, chat.id, cursor)
    return len(rows)


def _invalidate_comment_posts(
    conn: sqlite3.Connection, cfg: Config, rows: Sequence[MessageRow]
) -> None:
    """Re-cut the post threads of the channels whose comments these deleted rows were.

    The only way from a deleted comment to the unit quoting it. A channel's post thread carries
    the post followed by its comments while listing the post alone in ``msg_ids``, so no
    ``json_each`` over ``units.msg_ids`` reaches a comment id and neither the group's own
    invalidation nor any lookup by unit could find that thread; ``comment_of_chat_id`` /
    ``comment_of_msg_id`` on the comment's row is what names it.

    The posts are re-read from the channel's own rows and handed to the same primitive, which
    rebuilds the thread from the comments still stored — this runs after the delete, so the one
    that has just gone is not among them.
    """
    posts: dict[int, set[int]] = {}
    for row in rows:
        if row.comment_of_chat_id is not None and row.comment_of_msg_id is not None:
            posts.setdefault(row.comment_of_chat_id, set()).add(row.comment_of_msg_id)
    for channel_id, post_ids in posts.items():
        channel = db.get_chat(conn, channel_id)
        if channel is None:
            continue
        stored = db.get_messages_by_msg_id(conn, channel_id, sorted(post_ids))
        if not stored:
            continue
        delta = units.invalidate_units_for(
            conn, channel, cfg, [row for _, row in sorted(stored.items())]
        )
        index.index_units(conn, delta)
