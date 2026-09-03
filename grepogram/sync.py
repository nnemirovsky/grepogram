"""Telegram sync: fetching messages incrementally and mapping them to ``messages`` rows.

:func:`sync_all` is the one entry point every caller (CLI ``sync``, MCP ``sync``, auto-sync in
``search``) goes through: it takes the cross-process :class:`SyncLock`, re-resolves the configured
sources, syncs chats in ``last_sync_at`` order until the :class:`SyncBudget` runs out, and runs
:func:`on_chat_synced` — the unit rebuild followed by the lexical index — for every chat that
changed. :func:`sync_chat` fetches one chat: new messages after
``last_msg_id`` in batches, then a re-fetch of the newest messages for edits and reactions, plus
channel comments stored under the linked discussion chat.

:func:`map_message` reads raw TL attributes only — ``msg.message``, ``msg.media``,
``msg.reply_to``, ``msg.fwd_from``, ``msg.reactions``, ``msg.from_id``, ``msg.post``, ``msg.date``,
``msg.edit_date`` — and never the client-bound helpers (``msg.text``, ``msg.file``, ``msg.sender``,
``msg.chat``), so a message built without a client (the test fixtures) maps exactly like one
Telethon yields from ``iter_messages``. Display names come from a ``names`` map built with
:func:`collect_users` and :func:`names_of` out of the users and chats Telegram returns alongside
messages (:func:`peers_of`); the same rows feed the ``users`` upsert.
"""

import dataclasses
import datetime as dt
import fcntl
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

from telethon import errors, utils
from telethon.tl import functions, types

from grepogram import db, dialogs, index, tg, units
from grepogram.config import ConfigError
from grepogram.dialogs import entity_username
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
from grepogram.paths import Paths
from grepogram.sources import resolve_sources

log = logging.getLogger(__name__)

BATCH_SIZE = 500
LOCK_MODE = 0o600
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

    def __repr__(self) -> str:
        return f"SyncBudget(seconds={self.seconds!r})"


class SyncLock:
    """Exclusive ``flock`` on ``paths.lock_file`` so two processes never sync the same index.

    Non-blocking: entering while another process (or another open descriptor in this one) holds
    the lock raises :class:`SyncInProgress`. The lock file is never deleted — unlinking a file
    another process is about to lock would let both proceed.
    """

    def __init__(self, paths: Paths) -> None:
        self.path = paths.lock_file
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, LOCK_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise SyncInProgress(
                f"another sync is running (lock held on {self.path}); wait for it to finish"
            ) from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None


# --- one chat --------------------------------------------------------------------------------


UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    errors.ChannelPrivateError,
    errors.ChatAdminRequiredError,
    errors.ChannelInvalidError,
    errors.ChatForbiddenError,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncedChat:
    """What :func:`sync_chat` did for one chat.

    ``new_msg_ids`` are the ``messages.id`` rowids of every row inserted or changed (new messages
    and edits alike), in fetch order — the ids ``msg_fts`` is keyed by. ``new`` counts messages
    seen for the first time. ``complete`` is ``False`` when the budget cut the fetch short; the
    stored progress lets the next run resume. ``discussion`` carries the same for the linked
    discussion chat when comments were fetched; ``migrated_to`` is the supergroup a legacy group
    was upgraded to, freshly upserted so the caller can sync it too.
    """

    chat: ChatRow
    new_msg_ids: list[int] = field(default_factory=list)
    new: int = 0
    complete: bool = True
    unavailable: bool = False
    discussion: "SyncedChat | None" = None
    migrated_to: ChatRow | None = None


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


class _Changes:
    """Ordered, deduplicated ``messages.id`` values touched by one chat's sync."""

    def __init__(self) -> None:
        self._ids: dict[int, None] = {}

    def add(self, ids: Iterable[int]) -> None:
        for row_id in ids:
            self._ids.setdefault(row_id, None)

    @property
    def ids(self) -> list[int]:
        return list(self._ids)


@dataclass(slots=True, kw_only=True)
class _Run:
    """State one chat's fetch passes between its steps."""

    client: Any
    conn: sqlite3.Connection
    chat: ChatRow
    source: Source
    budget: SyncBudget
    me: UserRow | None
    discussion: ChatRow | None = None
    peers: _PeerBook = field(default_factory=_PeerBook)
    changes: _Changes = field(default_factory=_Changes)
    comments: _Changes = field(default_factory=_Changes)

    def map(self, msg: Any, chat: ChatRow) -> MessageRow | None:
        self.peers.add(msg)
        return map_message(msg, chat, self.peers.names, me=self.me)

    def store(self, rows: list[MessageRow]) -> list[int]:
        with db.transaction(self.conn):
            self.peers.flush(self.conn)
            return db.upsert_messages(self.conn, rows)


def since_of(source: Source | None) -> dt.datetime | None:
    """``source.since`` as a UTC midnight datetime, ``None`` when unset."""
    if source is None or not source.since:
        return None
    try:
        day = dt.date.fromisoformat(source.since)
    except ValueError:
        raise ConfigError(
            f"source {source.id}: since must be an ISO date (YYYY-MM-DD), got {source.since!r}"
        ) from None
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
    new post, stored under the linked discussion chat with ``topic_id`` = the channel post id.

    A chat Telegram refuses (:data:`UNAVAILABLE_ERRORS`) is marked ``unavailable`` and reported,
    not raised; a legacy group upgraded to a supergroup has ``migrated_to`` set and its history
    left alone from then on, while the result keeps pointing at the supergroup so
    :func:`sync_all` syncs that one. :class:`~telethon.errors.FloodWaitError` beyond the
    client's sleep threshold and authorization errors propagate after the current batch is
    committed.
    """
    if chat.migrated_to is not None:
        log.debug("chat %s migrated to %s; its history is frozen", chat.id, chat.migrated_to)
        return SyncedChat(chat=chat, migrated_to=db.get_chat(conn, chat.migrated_to))
    run = _Run(client=client, conn=conn, chat=chat, source=source, budget=budget, me=me)
    try:
        migrated = await _check_migration(client, conn, chat) if chat.type == "group" else None
        if source.comments and chat.type == "channel":
            run.discussion = await link_discussion_chat(client, conn, chat)
        fetched = await _fetch_new(run)
        if fetched.complete and chat.last_sync_at is not None:
            run.changes.add(await _refetch_edits(run, sync_cfg or SyncCfg()))
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
        fetched.new,
        "" if fetched.complete else " (budget expired, will resume)",
    )
    return SyncedChat(
        chat=_refresh(conn, chat),
        new_msg_ids=run.changes.ids,
        new=fetched.new,
        complete=fetched.complete,
        discussion=fetched.discussion,
        migrated_to=migrated,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Fetched:
    progress: int
    new: int
    complete: bool
    discussion: SyncedChat | None


async def _fetch_new(run: _Run) -> _Fetched:
    """The incremental pass: everything after ``last_msg_id``, committed batch by batch."""
    chat = run.chat
    progress = chat.last_msg_id
    new = 0
    complete = True
    comment_count = 0
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
        if len(batch) < BATCH_SIZE:
            continue
        progress, done = await _store_batch(run, batch, progress, seen_up_to)
        new += len(batch)
        comment_count += done
        batch = []
        if run.budget.expired:
            complete = False
            break
    if complete and (batch or seen_up_to > progress):
        progress, done = await _store_batch(run, batch, progress, seen_up_to)
        new += len(batch)
        comment_count += done
        complete = progress >= seen_up_to
    synced_discussion = None
    if run.discussion is not None:
        synced_discussion = SyncedChat(
            chat=_refresh(run.conn, run.discussion), new_msg_ids=run.comments.ids, new=comment_count
        )
    return _Fetched(progress=progress, new=new, complete=complete, discussion=synced_discussion)


async def _store_batch(
    run: _Run, batch: list[MessageRow], progress: int, seen_up_to: int
) -> tuple[int, int]:
    """Upsert one batch (and, for channels with comments, each post's thread) and record progress.

    Returns ``(progress, comments_stored)``. Without comments progress jumps to the highest
    message id seen, skipped service messages included. With comments it advances post by post,
    so a budget expiry between two posts leaves the later posts to be re-fetched next time
    together with their threads.
    """
    chat = run.chat
    run.changes.add(run.store(batch))
    if run.discussion is None:
        db.set_chat_progress(run.conn, chat.id, seen_up_to, chat.last_sync_at)
        return seen_up_to, 0
    stored = 0
    for row in batch:
        if run.budget.expired:
            break
        ids = await _fetch_comments(run, row.msg_id)
        run.comments.add(ids)
        stored += len(ids)
        progress = row.msg_id
    else:
        progress = seen_up_to
    db.set_chat_progress(run.conn, chat.id, progress, chat.last_sync_at)
    return progress, stored


async def _fetch_comments(run: _Run, post_id: int) -> list[int]:
    """Store the comment thread of one channel post under the discussion chat.

    Comments live in the discussion group with their own message ids; their reply headers
    point at the discussion-side copy of the post, which Telegram does not return here, so the
    channel post id is kept in ``topic_id`` to tie the thread back to its post. A post without a
    thread (``MsgIdInvalidError``) is skipped.
    """
    assert run.discussion is not None
    rows: list[MessageRow] = []
    try:
        async for msg in run.client.iter_messages(run.chat.id, reply_to=post_id):
            row = run.map(msg, run.discussion)
            if row is not None:
                rows.append(dataclasses.replace(row, topic_id=post_id))
    except errors.MsgIdInvalidError:
        log.debug("post %s in channel %s has no comment thread", post_id, run.chat.id)
        return []
    return run.store(rows) if rows else []


async def _refetch_edits(run: _Run, sync_cfg: SyncCfg) -> list[int]:
    """Re-read the newest ``edit_refetch`` messages and rewrite only stored rows that changed.

    Messages that are not stored — history before ``since``, or anything the incremental pass
    has not reached — are left alone; this pass exists for edits and reactions only.
    """
    if sync_cfg.edit_refetch <= 0:
        return []
    chat = run.chat
    fresh: list[MessageRow] = []
    async for msg in run.client.iter_messages(chat.id, limit=sync_cfg.edit_refetch):
        row = run.map(msg, chat)
        if row is not None:
            fresh.append(row)
    if not fresh:
        return []
    stored = {
        row.msg_id: dataclasses.replace(row, id=None)
        for row in db.get_messages(run.conn, chat.id, since_msg_id=min(r.msg_id for r in fresh))
    }
    changed = [row for row in fresh if row.msg_id in stored and stored[row.msg_id] != row]
    if not changed:
        return []
    log.debug("chat %s: %d of %d re-fetched messages changed", chat.id, len(changed), len(fresh))
    return run.store(changed)


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
        new_chat = db.upsert_chat(conn, _chat_row(await client.get_entity(new_id), chat.source_id))
    db.set_chat_migrated(conn, chat.id, new_id)
    log.info("chat %s (%s) migrated to supergroup %s", chat.id, chat.title, new_id)
    return new_chat


async def link_discussion_chat(
    client: Any, conn: sqlite3.Connection, channel: ChatRow
) -> ChatRow | None:
    """Upsert the channel's linked discussion group as its own ``chats`` row.

    The row carries ``discussion_of = channel.id`` and the channel's ``source_id`` so the
    comments stored under it are removed together with the channel. ``None`` when the channel
    has no discussion group.
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
        entity = await client.get_entity(linked_id)
    row = _chat_row(entity, channel.source_id, discussion_of=channel.id)
    return db.upsert_chat(conn, row)


def _chat_row(entity: Any, source_id: str | None, *, discussion_of: int | None = None) -> ChatRow:
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
    :class:`~grepogram.units.UnitDelta`. Embedding the dirty units is not per chat — it runs
    once at the end of :func:`sync_all` when an embedder is given.
    """
    delta = units.rebuild_for_chat(conn, chat, cfg, new_msg_ids)
    index.index_chat(conn, chat, new_msg_ids, delta)


async def sync_all(
    client: Any,
    conn: sqlite3.Connection,
    cfg: Config,
    paths: Paths,
    budget: SyncBudget,
    embedder: object | None = None,
) -> SyncReport:
    """Sync every configured source within ``budget``; the client must be connected.

    Takes the :class:`SyncLock` (:class:`SyncInProgress` when another process syncs), resolves
    the sources into chats, and processes them oldest-synced first (never-synced chats before
    all others) until the budget expires — the current batch is always committed, and the chats
    not finished are reported in ``chats_remaining``. Each chat with changes goes through
    :func:`on_chat_synced`. A flood wait Telegram will not let the client sleep through stops
    the run with a warning; a chat Telegram refuses is reported in ``unavailable``; an
    unauthorized session raises :class:`~grepogram.tg.AuthRequired`. ``embedder`` is reserved
    for the dense indexing step wired in later.
    """
    with SyncLock(paths):
        async with tg.wrap_auth_errors(client):
            return await _sync_chats(client, conn, cfg, budget)


async def _sync_chats(
    client: Any, conn: sqlite3.Connection, cfg: Config, budget: SyncBudget
) -> SyncReport:
    me = _self_row(await client.get_me())
    queue = sorted(await resolve_sources(cfg, client, conn), key=_sync_order)
    sources = {source.id: source for source in cfg.sources}
    done: list[int] = []
    remaining: list[int] = []
    unavailable: list[int] = []
    warnings: list[str] = []
    new = 0
    while queue:
        chat = queue.pop(0)
        source = sources.get(chat.source_id or "")
        if source is None:
            log.debug("chat %s has no configured source; skipped", chat.id)
            continue
        if budget.expired:
            remaining.append(chat.id)
            continue
        try:
            synced = await sync_chat(client, conn, chat, source, budget, sync_cfg=cfg.sync, me=me)
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
        new += synced.new
        for part in (synced, synced.discussion):
            if part is not None and part.new_msg_ids:
                on_chat_synced(conn, part.chat, cfg, part.new_msg_ids)
        if synced.discussion is not None:
            new += synced.discussion.new
        if synced.unavailable:
            unavailable.append(chat.id)
        elif synced.complete:
            done.append(chat.id)
        else:
            remaining.append(chat.id)
        if synced.migrated_to is not None and _not_queued(synced.migrated_to, queue, done):
            queue.append(synced.migrated_to)
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


def _sync_order(chat: ChatRow) -> tuple[int, int, int]:
    """Never-synced chats first, then by ``last_sync_at`` ascending, ties by id."""
    if chat.last_sync_at is None:
        return (0, 0, chat.id)
    return (1, chat.last_sync_at, chat.id)


def _not_queued(chat: ChatRow, queue: list[ChatRow], done: list[int]) -> bool:
    return chat.id not in done and all(item.id != chat.id for item in queue)


def _self_row(me: Any) -> UserRow | None:
    users = collect_users([me])
    return next(iter(users.values()), None)
