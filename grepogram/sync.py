"""Telegram sync: fetching messages incrementally and mapping them to ``messages`` rows.

:func:`sync_all` is the one entry point every caller (CLI ``sync``, MCP ``sync``, auto-sync in
``search``) goes through: it takes the cross-process :class:`SyncLock`, re-resolves the configured
sources, syncs chats in ``last_sync_at`` order until the :class:`SyncBudget` runs out, runs
:func:`on_chat_synced` — the unit rebuild followed by the lexical index — over every stored row
that no rebuild has covered yet, and finally embeds the dirty units when an embedder is given.
:func:`sync_chat` fetches one chat: new messages after ``last_msg_id`` in batches, then a
re-fetch of the newest messages for edits and reactions. A channel whose source has ``comments``
also gets the comment threads of its new posts — the ones Telegram reports comments on — stored
under the linked discussion group with the post id as ``topic_id``, and on every later run the
threads of its newest posts that grew since. A source that lists the group itself syncs its
whole history as well: both paths write the same rows, and a post id already stored survives the
plain history's upsert (:func:`grepogram.db.upsert_messages`).

Fetching and indexing are decoupled through ``messages.indexed``: every row a batch commits is
flagged until :func:`on_chat_synced` has rebuilt its units and ``msg_fts`` entry, and
:func:`_sync_chats` indexes a chat's pending rows after its fetch whether that returned or raised
(a flood wait, an RPC error, a cancellation) and, at the end of the run, those of the chats it
never reached. A run that dies between a commit and the rebuild therefore leaves nothing behind
that the next run does not pick up (:func:`grepogram.db.unindexed_message_ids`). The rebuild, the
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
from collections.abc import Callable, Iterable, Mapping
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
    Source,
    SyncCfg,
    SyncReport,
    UserRow,
)
from grepogram.paths import FileLock, Paths
from grepogram.sources import parse_since, resolve_sources
from grepogram.units import UNKNOWN_SENDER

log = logging.getLogger(__name__)

BATCH_SIZE = 500
JOIN_TIMEOUT = 60.0
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
    """Wall-clock allowance for one sync run; ``seconds=None`` never expires.

    ``clock`` defaults to :func:`time.monotonic` and is injectable for tests.
    """

    def __init__(
        self, seconds: float | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.seconds = seconds
        self._clock = clock
        self.deadline: float | None = None if seconds is None else clock() + seconds

    @property
    def expired(self) -> bool:
        return self.deadline is not None and self._clock() >= self.deadline

    @property
    def remaining(self) -> float | None:
        """Seconds left, ``None`` for an unlimited budget, never negative."""
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
    sync_cfg: SyncCfg | None = None,
    me: UserRow | None = None,
) -> SyncedChat:
    """Fetch one chat incrementally and store what changed; the client must be connected.

    New messages are read with ``iter_messages(min_id=last_msg_id, reverse=True)`` — on the
    first run ``offset_date=since`` skips older history — and upserted in batches of
    :data:`BATCH_SIZE`, advancing ``last_msg_id`` after every batch so an interrupted run resumes
    where it stopped. A run that finishes re-fetches the newest ``edit_refetch`` messages for
    edits and reactions (only rows that actually differ are written) and stamps
    ``last_sync_at``. Channels whose source has ``comments`` also get the comment threads of each
    new post, stored under the linked discussion chat with ``topic_id`` = the channel post id,
    and the re-fetch pass re-reads the thread of every re-fetched post Telegram reports more
    replies for than are stored, so comments that arrive after the post was indexed follow.

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
            _track(run.changes, await _refetch_edits(run, sync_cfg or SyncCfg()))
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
    """The incremental pass: everything after ``last_msg_id``, committed batch by batch."""
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
    stored = db.count_topic_messages(
        run.conn, run.discussion.id, [row.msg_id for row in batch if run.replies.get(row.msg_id)]
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
    channel post id is kept in ``topic_id`` to tie the thread back to its post. A post without a
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
                rows.append(dataclasses.replace(row, topic_id=post_id))
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


async def _refetch_edits(run: _Run, sync_cfg: SyncCfg) -> list[int]:
    """Re-read the newest ``edit_refetch`` messages and rewrite only stored rows that changed.

    Messages that are not stored — history before ``since``, or anything the incremental pass
    has not reached — are left alone; this pass exists for edits and reactions only. For a
    channel with comments it also refreshes the threads of the re-fetched posts that grew
    (:func:`_refresh_comments`); the ids of those posts are returned along with the edited rows
    so their post-thread units are rebuilt.
    """
    if sync_cfg.edit_refetch <= 0:
        return []
    chat = run.chat
    fresh: list[MessageRow] = []
    replies: dict[int, int] = {}
    async for msg in run.client.iter_messages(chat.id, limit=sync_cfg.edit_refetch):
        row = run.map(msg, chat)
        if row is not None:
            fresh.append(row)
            replies[row.msg_id] = replies_count(msg)
    if not fresh:
        return []
    stored = {
        row.msg_id: row
        for row in db.get_messages(run.conn, chat.id, since_msg_id=min(r.msg_id for r in fresh))
    }
    changed = [row for row in fresh if row.msg_id in stored and _differs(stored[row.msg_id], row)]
    if changed:
        log.debug(
            "chat %s: %d of %d re-fetched messages changed", chat.id, len(changed), len(fresh)
        )
    ids = run.store(changed)
    if run.discussion is not None:
        ids += await _refresh_comments(run, stored, replies)
    return ids


def _differs(stored: MessageRow, fresh: MessageRow) -> bool:
    """Whether storing ``fresh`` would change ``stored``.

    Compared with the ``topic_id`` the upsert would keep: a comment re-read as part of its
    discussion group's own history arrives without its post id and must not count as an edit.
    """
    topic = stored.topic_id if fresh.topic_id is None else fresh.topic_id
    return dataclasses.replace(stored, id=None) != dataclasses.replace(fresh, topic_id=topic)


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
    counted = db.count_topic_messages(run.conn, run.discussion.id, replies)
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

    The row carries ``discussion_of = channel.id``. A group that is already stored — listed by a
    source of its own, or linked in an earlier run — keeps its ``source_id`` and every message it
    holds; a new row gets the channel's ``source_id`` so the comments stored under it are removed
    together with the channel. ``None`` when the channel has no discussion group;
    :class:`DiscussionUnavailable` when Telegram will not resolve the group (private, or the
    account is not a member) — the channel's own errors propagate.
    """
    full = await client(functions.channels.GetFullChannelRequest(channel.id))
    linked = getattr(full.full_chat, "linked_chat_id", None)
    if not linked:
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
            raise DiscussionUnavailable(
                f"cannot resolve discussion group {linked_id}: {exc}"
            ) from exc
    existing = db.get_chat(conn, linked_id)
    source_id = (
        channel.source_id if existing is None or not existing.source_id else existing.source_id
    )
    return db.upsert_chat(conn, _chat_row_from_entity(entity, source_id, discussion_of=channel.id))


def _chat_row_from_entity(
    entity: Any, source_id: str | None, *, discussion_of: int | None = None
) -> ChatRow:
    info = dialogs.dialog_info(entity)
    return ChatRow(
        id=info.id,
        type=info.type,
        title=info.title,
        username=info.username,
        is_forum=info.is_forum,
        source_id=source_id,
        discussion_of=discussion_of,
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


async def _joined_to_thread[T](job: Callable[[], T]) -> T:
    """Run ``job`` on a worker thread and never leave it writing on its own.

    ``await asyncio.to_thread(...)`` submits the job before it suspends, so a cancellation —
    what an ``anyio`` ``CancelScope`` around an MCP tool call delivers — abandons the future
    while the thread carries on committing, and the :class:`SyncLock` the caller releases on its
    way out no longer covers it. The job is shielded from the cancellation and joined through a
    plain :class:`threading.Event` instead: the wait needs no ``await``, which a cancelled scope
    would raise out of immediately, and it is bounded, so a thread that hangs stalls the unwind
    for :data:`JOIN_TIMEOUT` at the most.
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
        if not finished.wait(JOIN_TIMEOUT):
            log.warning(
                "a background index job has not finished after %ss; "
                "the sync lock is released while it runs on",
                JOIN_TIMEOUT,
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
    than abandoned (:func:`_joined_to_thread`).
    """
    for row in (db.get_chat(conn, chat.id), db.get_discussion_chat(conn, chat.id)):
        if row is None:
            continue
        pending = db.unindexed_message_ids(conn, row.id)
        if pending:
            await _joined_to_thread(functools.partial(on_chat_synced, conn, row, cfg, pending))


ConfigSource = Config | Callable[[], Config]
"""A config, or a loader called once the :class:`SyncLock` is held (see :func:`sync_all`)."""


async def sync_all(
    client: Any,
    conn: sqlite3.Connection,
    cfg: ConfigSource,
    paths: Paths,
    budget: SyncBudget,
    embedder: Embedder | None = None,
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

    With an ``embedder`` the run ends by embedding the dirty units under the same budget and
    lock (:func:`~grepogram.index.embed_dirty_units`); units the budget leaves unembedded and a
    changed embedding model become ``warnings`` — the messages are synced either way.
    """
    with SyncLock(paths):
        current = cfg if isinstance(cfg, Config) else cfg()
        report = await _sync_chats(client, conn, current, budget)
        db.set_last_sync_run(conn, int(time.time()))
        if embedder is None:
            return report
        return await _embed_after_sync(conn, embedder, budget, report)


async def _embed_after_sync(
    conn: sqlite3.Connection, embedder: Embedder, budget: SyncBudget, report: SyncReport
) -> SyncReport:
    """Embed what the sync left dirty, off the event loop so the client's keepalives run on.

    Joined like the indexing step (:func:`_joined_to_thread`): a cancelled run releases the
    :class:`SyncLock` only once the worker thread has stopped writing vectors.
    """
    warnings = list(report.warnings)
    try:
        embedded = await _joined_to_thread(
            functools.partial(index.embed_dirty_units, conn, embedder, budget=budget)
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


async def _sync_chats(
    client: Any, conn: sqlite3.Connection, cfg: Config, budget: SyncBudget
) -> SyncReport:
    """The chat loop of :func:`sync_all`.

    The unit rebuild runs on a worker thread so the loop keeps serving the client's keepalives
    while a big chat is cut into units; it runs for every chat once its fetch is over — after
    the last batch, or after the batch a flood wait, an RPC error or a cancellation interrupted,
    which stays committed and searchable either way — and at the end for the chats the budget
    or a flood wait kept the run from reaching, whose pending rows come from an earlier run. A
    chat whose row disappears while it is being fetched — a removal that got past the lock, or
    a hand-edited database — fails its next write with a foreign-key error; that ends the chat
    for this run with a warning, not the run.
    """
    me = _self_row(await client.get_me())
    queue = sorted(await resolve_sources(cfg, client, conn), key=_sync_order)
    sources = {source.id: source for source in cfg.sources}
    done: list[int] = []
    remaining: list[int] = []
    unavailable: list[int] = []
    warnings: list[str] = []
    processed: set[int] = set()
    deferred: list[ChatRow] = []
    new = 0
    while queue:
        chat = queue.pop(0)
        processed.add(chat.id)
        source = sources.get(chat.source_id or "")
        if source is None:
            log.debug("chat %s has no configured source; skipped", chat.id)
            continue
        if budget.expired:
            remaining.append(chat.id)
            deferred.append(chat)
            continue
        _cap_flood_sleep(client, cfg.sync, budget)
        try:
            try:
                synced = await sync_chat(
                    client, conn, chat, source, budget, sync_cfg=cfg.sync, me=me
                )
            finally:
                await index_pending(conn, cfg, chat)
        except errors.FloodWaitError as exc:
            log.warning("flood wait of %ss on chat %s; stopping this run", exc.seconds, chat.id)
            warnings.append(
                f"flood wait: Telegram asks to wait {exc.seconds}s before more history "
                "requests; run sync again later"
            )
            remaining.extend([chat.id, *(c.id for c in queue)])
            break
        except errors.UnauthorizedError:
            raise
        except errors.RPCError as exc:
            log.warning("chat %s (%s): %s; skipped this run", chat.id, chat.title, exc)
            warnings.append(f"chat {chat.id} ({chat.title}): {exc}")
            remaining.append(chat.id)
            continue
        except sqlite3.IntegrityError as exc:
            log.warning("chat %s (%s) was removed during the sync: %s", chat.id, chat.title, exc)
            warnings.append(f"chat {chat.id} ({chat.title}) was removed while it was being synced")
            continue
        new += synced.new
        warnings.extend(synced.warnings)
        if synced.discussion is not None:
            new += synced.discussion.new
        if synced.unavailable:
            unavailable.append(chat.id)
        elif synced.complete:
            done.append(chat.id)
        else:
            remaining.append(chat.id)
        migrated = synced.migrated_to
        if migrated is not None and migrated.id not in processed and _not_queued(migrated, queue):
            queue.append(migrated)
    for chat in [*deferred, *queue]:
        await index_pending(conn, cfg, chat)
    log.info(
        "sync: %d new messages, %d chats done, %d remaining, %d unavailable",
        new,
        len(done),
        len(remaining),
        len(unavailable),
    )
    return SyncReport(
        new=new,
        chats_done=done,
        chats_remaining=remaining,
        unavailable=unavailable,
        warnings=warnings,
    )


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


def _not_queued(chat: ChatRow, queue: list[ChatRow]) -> bool:
    return all(item.id != chat.id for item in queue)


def _self_row(me: Any) -> UserRow | None:
    users = collect_users([me])
    return next(iter(users.values()), None)
