"""Telegram sync: fetching messages incrementally and mapping them to ``messages`` rows.

:func:`sync_all` is the one entry point every caller (CLI ``sync``, MCP ``sync``, auto-sync in
``search``) goes through: it takes the cross-process :class:`SyncLock`, re-resolves the configured
sources of every account it holds a client for, syncs chats in ``last_sync_at`` order — one queue
per account, the queues concurrently, each chat once — until the :class:`SyncBudget` runs out, runs
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

:func:`map_message` reads raw TL attributes only — ``msg.message``, ``msg.entities``,
``msg.media``, ``msg.reply_markup``, ``msg.reply_to``, ``msg.fwd_from``, ``msg.reactions``,
``msg.from_id``, ``msg.post``, ``msg.date``, ``msg.edit_date`` — and never the client-bound
helpers (``msg.text``, ``msg.file``, ``msg.sender``, ``msg.chat``), so a message built without a
client (the test fixtures) maps exactly like one Telethon yields from ``iter_messages``. Display
names come from a ``names`` map built with :func:`collect_users` out of the users and chats
Telegram returns alongside messages (:func:`peers_of`); the same rows feed the ``users`` upsert.
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
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from telethon import errors, helpers, utils
from telethon.tl import functions, types

from grepogram import db, dialogs, index, leads, tg, units
from grepogram.config import ConfigError
from grepogram.dialogs import entity_username
from grepogram.embed import Embedder
from grepogram.models import (
    DEFAULT_ACCOUNT,
    AccountRow,
    ChatRow,
    Config,
    LinkKind,
    MediaKind,
    MessageRow,
    PruneReport,
    RecaptureReport,
    Source,
    SyncCfg,
    SyncReport,
    UserRow,
    chat_scope,
)
from grepogram.paths import FileLock, Paths
from grepogram.sources import (
    IMPORT_PREFIX,
    discussion_source_id,
    imported_tag,
    parse_since,
    resolve_sources,
    seed_peers,
    source_account,
)
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
    land in their discussion chat; what Telegram says about the chat itself — the peer that sent
    a channel post, the peer a quoted reply points into — is compared with ``chat.peer_id``, the
    id Telegram knows, which a scoped row stored under a synthetic id does not share. Text is the
    message text or caption; a poll, venue or contact — media that carries its content outside
    the text — contributes its own text when the message has none.

    A forward's origin is kept structured as well as named (:func:`forward_origin`), and every
    Telegram destination the message names — in its text, its hyperlinks, its buttons, its link
    preview — becomes a ``links`` pair (:func:`links_of`).
    """
    if isinstance(msg, types.MessageService) or not isinstance(msg, types.Message):
        return None
    if msg.date is None:
        log.debug("skipping message %s in chat %s: no date", msg.id, chat.id)
        return None
    from_id, from_name = sender_of(msg, names, me=me)
    if from_id == chat.peer_id and from_id not in names and chat.title:
        from_name = chat.title
    reply_to_msg_id, topic_id = reply_of(msg.reply_to, chat.peer_id)
    media_kind, media_filename = media_of(msg.media)
    fwd_peer_id, fwd_msg_id, fwd_date = forward_origin(msg.fwd_from)
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
        fwd_peer_id=fwd_peer_id,
        fwd_msg_id=fwd_msg_id,
        fwd_date=fwd_date,
        text=msg.message or media_text(msg.media),
        media_kind=media_kind,
        media_filename=media_filename,
        reactions_total=reactions_total(msg.reactions),
        links=links_of(msg),
    )


def epoch(when: dt.datetime) -> int:
    """Unix seconds; Telethon dates are UTC-aware, a naive one is taken as UTC."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return int(when.timestamp())


def reply_of(reply_to: Any, peer_id: int) -> tuple[int | None, int | None]:
    """``(reply_to_msg_id, topic_id)`` from a message's ``reply_to`` header.

    In a forum the header always points at the topic: a message that merely sits in a topic has
    ``forum_topic`` set and ``reply_to_msg_id`` = the topic root with no ``reply_to_top_id`` and
    is not a reply; a real reply inside a topic carries its parent in ``reply_to_msg_id`` and the
    topic root in ``reply_to_top_id``. Story replies and quotes of a message from another chat
    (``reply_to_peer_id`` set to a different peer than ``peer_id``, the chat's Telegram id)
    are not in-chat replies.
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
    if other is not None and int(utils.get_peer_id(other)) != peer_id:
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


def forward_origin(fwd: Any) -> tuple[int | None, int | None, int | None]:
    """``(fwd_peer_id, fwd_msg_id, fwd_date)``: where a forwarded message came from.

    The address of the original message when Telegram gives one — a channel post
    (``from_id`` + ``channel_post``), else the chat and message it was saved from
    (``saved_from_peer`` + ``saved_from_msg_id``) — so forwards of one post share one origin
    wherever they land; else the author alone (``from_id``, no message), and nothing at all for
    an account that hides itself behind ``from_name``. ``fwd_date`` is when the original was
    sent. Ids are marked peer ids; nothing here asks whether this account can reach them.
    """
    if fwd is None:
        return None, None, None
    date = epoch(fwd.date) if fwd.date is not None else None
    if fwd.from_id is not None and fwd.channel_post is not None:
        return int(utils.get_peer_id(fwd.from_id)), int(fwd.channel_post), date
    if fwd.saved_from_peer is not None and fwd.saved_from_msg_id is not None:
        return int(utils.get_peer_id(fwd.saved_from_peer)), int(fwd.saved_from_msg_id), date
    if fwd.from_id is not None:
        return int(utils.get_peer_id(fwd.from_id)), None, date
    return None, None, date


def links_of(msg: Any) -> tuple[tuple[LinkKind, str], ...]:
    """Every Telegram destination ``msg`` names, as sorted, distinct ``(kind, target)`` pairs.

    Read from the raw attributes only: ``msg.entities`` (a visible URL → ``link``, a hidden
    ``text_url`` hyperlink, an ``@mention`` or a mention by user id → ``mention``),
    ``msg.reply_markup`` (URL buttons) and ``msg.media`` (the link preview's URL → ``webpage``).
    Targets are normalized by :func:`grepogram.leads.normalize`, and a URL that names no
    Telegram destination is dropped. Entity offsets count UTF-16 code units, hence the
    surrogate round trip before slicing the text.
    """
    found: set[tuple[LinkKind, str]] = set()

    def add(kind: LinkKind, value: Any) -> None:
        if isinstance(value, str) and (lead := leads.normalize(value)) is not None:
            found.add((kind, lead.target))

    entities = list(msg.entities or ())
    wide = helpers.add_surrogate(msg.message or "") if entities else ""
    for entity in entities:
        span = helpers.del_surrogate(wide[entity.offset : entity.offset + entity.length])
        if isinstance(entity, types.MessageEntityUrl):
            add("link", span)
        elif isinstance(entity, types.MessageEntityTextUrl):
            add("text_url", entity.url)
        elif isinstance(entity, types.MessageEntityMention):
            add("mention", span)
        elif isinstance(entity, types.MessageEntityMentionName):
            add("mention", f"peer:{int(entity.user_id)}")
    markup = msg.reply_markup
    if isinstance(markup, types.ReplyInlineMarkup):
        for row in markup.rows:
            for button in row.buttons:
                add("button", getattr(button, "url", None))
    if isinstance(msg.media, types.MessageMediaWebPage):
        add("webpage", getattr(msg.media.webpage, "url", None))
    return tuple(sorted(found))


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

    ``messages`` is an optional allowance of newly stored messages (comments included), which a
    research run sets from ``max_messages_per_run``: once :meth:`spend` has counted that many,
    :attr:`exhausted` is true and the fetch stops at its next batch boundary exactly as it does
    when the clock runs out — the batch it is on is committed and the chat resumes next time.
    It stops *fetching* only: :attr:`expired` ignores it, so indexing, the re-cut and embedding,
    which store no message, are paced by the clock alone.

    ``clock`` defaults to :func:`time.monotonic` and is injectable for tests.
    """

    def __init__(
        self,
        seconds: float | None = None,
        *,
        messages: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if messages is not None and messages < 0:
            raise ValueError(f"a message allowance cannot be negative: {messages}")
        self.seconds = seconds
        self.messages = messages
        self.spent = 0
        self.held = 0
        self._clock = clock
        self.deadline: float | None = None if seconds is None else clock() + seconds
        self._cancelled = False

    def spend(self, count: int) -> None:
        """Count ``count`` newly stored messages against the allowance."""
        self.spent += max(0, count)

    def hold(self, wanted: int) -> int:
        """Set aside up to ``wanted`` messages of the allowance for a batch about to be stored,
        and return how many were set aside — ``wanted`` itself without an allowance.

        Several accounts' queues fetch at once against the one budget, and each gathers a batch
        before storing it: without this, two queues reading the same :attr:`messages_left` would
        each store up to that many and overshoot the cap by a batch per queue. What a queue
        holds is out of every other queue's :attr:`messages_left` until :meth:`release` — so it
        stores no more rows than it holds (every stored row costs at most one message), spends
        what was actually new, and then releases the hold.
        """
        if self.messages is None:
            return wanted
        granted = max(0, min(wanted, self.messages - self.spent - self.held))
        self.held += granted
        return granted

    def release(self, held: int) -> None:
        """Give back a :meth:`hold` once its batch is stored (and spent)."""
        if self.messages is not None:
            self.held -= held

    @property
    def messages_left(self) -> int | None:
        """Messages the allowance still admits, ``None`` without one, never negative — what any
        queue holds for a batch it is storing (:meth:`hold`) already taken off."""
        if self.messages is None:
            return None
        return max(0, self.messages - self.spent - self.held)

    @property
    def exhausted(self) -> bool:
        """Whether the message allowance is used up (or held by a batch being stored); never,
        without one."""
        return self.messages is not None and self.messages_left == 0

    @property
    def halted(self) -> bool:
        """Whether fetching must stop: the clock ran out, the run was cancelled, or the message
        allowance is used up. What the fetch loops check; :attr:`expired` is the clock alone."""
        return self.expired or self.exhausted

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
    def cancelled(self) -> bool:
        """Whether the run was cancelled, as opposed to having spent its allowance.

        Both read as ``expired``, and a pass that keeps a slice of work for itself when the
        clock runs out (:func:`recut_pending_chats`) must not keep one when the caller is being
        torn down: there is nothing left to hold the :class:`SyncLock` open for.
        """
        return self._cancelled

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


def forward_peers(messages: Iterable[Any]) -> list[tuple[int, str | None, int | None]]:
    """``(peer_id, username, access_hash)`` of the channels and supergroups ``messages`` were
    forwarded from, as Telegram handed them along with the messages (``msg.forward.chat``, bound
    from the answer's ``chats`` at no cost).

    That is the only moment grepogram learns how to reach a forward's origin: the message
    carries its id alone, and an id is no address — a channel is asked about with this
    account's access hash, or found by its username. A ``min`` entity's access hash addresses
    nothing and is left out; its username is kept. An origin that brings neither is dropped.
    """
    found: dict[int, tuple[int, str | None, int | None]] = {}
    for msg in messages:
        forward = getattr(msg, "forward", None)
        chat = getattr(forward, "chat", None) if forward is not None else None
        if not isinstance(chat, types.Channel):
            continue
        username = entity_username(chat)
        access_hash = None if chat.min else chat.access_hash
        if username or access_hash is not None:
            marked = dialogs.peer_id(chat)
            found[marked] = (marked, username, access_hash)
    return list(found.values())


def remember_forward_peers(
    conn: sqlite3.Connection, account: str, messages: Iterable[Any], now: int | None = None
) -> None:
    """Record in ``peer_cache`` what ``account`` was handed about the origins of the forwards
    among ``messages`` (:func:`forward_peers`), so research can probe and join them."""
    peers = forward_peers(messages)
    if peers:
        db.remember_peers(conn, account, peers, int(time.time()) if now is None else now)


class _PeerBook:
    """Peers met during a fetch: display names for the mapper, pending user rows for the upsert,
    and the forward origins ``account`` was handed (:func:`forward_peers`)."""

    def __init__(self, account: str = DEFAULT_ACCOUNT) -> None:
        self.account = account
        self.users: dict[int, UserRow] = {}
        self.names: dict[int, str] = {}
        self._pending: dict[int, UserRow] = {}
        self._origins: dict[int, tuple[int, str | None, int | None]] = {}

    def add(self, msg: Any) -> None:
        for user_id, user in collect_users(peers_of(msg)).items():
            if self.users.get(user_id) == user:
                continue
            self.users[user_id] = user
            self._pending[user_id] = user
            if user.display_name:
                self.names[user_id] = user.display_name
        for origin in forward_peers([msg]):
            self._origins[origin[0]] = origin

    def flush(self, conn: sqlite3.Connection) -> None:
        if self._pending:
            db.upsert_users(conn, self._pending.values())
            self._pending = {}
        if self._origins:
            db.remember_peers(conn, self.account, self._origins.values(), int(time.time()))
            self._origins = {}


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

    ``cfg`` is the whole config rather than its ``[sync]`` section because :meth:`store` may have
    to cut units again, and those are cut to ``[units]``; a caller with none passes the defaults.
    """

    client: Any
    conn: sqlite3.Connection
    chat: ChatRow
    source: Source
    budget: SyncBudget
    cfg: Config
    me: UserRow | None
    discussion: ChatRow | None = None
    peers: _PeerBook = field(default_factory=_PeerBook)
    """Given the acting account by :func:`sync_chat`, whose forward origins it records."""
    changes: dict[int, None] = field(default_factory=dict)
    comment_ids: dict[int, None] = field(default_factory=dict)
    inserted: dict[int, int] = field(default_factory=dict)
    replies: dict[int, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def map(self, msg: Any, chat: ChatRow) -> MessageRow | None:
        self.peers.add(msg)
        return map_message(msg, chat, self.peers.names, me=self.me)

    async def store(self, rows: list[MessageRow]) -> list[int]:
        """Upsert ``rows`` (all of one chat) with the users met so far; returns their row ids.

        A row whose **attachment was replaced** loses its extracted text in the upsert
        (:data:`grepogram.db._MESSAGE_UPSERT`), and the units cut from that text are this
        method's to invalidate — the upsert cannot, :mod:`grepogram.db` knowing nothing of
        :mod:`grepogram.units`, and the ``indexed = 0`` it raises reaches nothing on its own: a
        rebuild never re-cuts a closed window and :func:`on_chat_synced` clears the flag anyway,
        so the old file's text would stay in the unit and in ``unit_fts`` for good, with no row
        left flagged and no media left pending to lead anything back to it. The same re-cut
        :func:`grepogram.media._recut` does after an extraction, in the other direction.

        Which rows those are has to be read *before* the upsert
        (:func:`grepogram.db.attachment_replaced` over what is stored now), and only the ones
        that actually carried text are worth a re-cut: a replaced attachment nothing was ever
        read off renders exactly the same either way.
        The rows handed to the invalidation are re-read *after* the upsert, so they carry the
        ``topic_id`` and ``comment_of_*`` pair it just coalesced onto them — the second is what
        :func:`_invalidate_comment_posts` follows to the channel post thread quoting a comment,
        the one unit no ``json_each`` over ``units.msg_ids`` can reach.

        The whole write is one transaction, and it goes to a worker thread
        (:func:`_joined_to_thread`) only when there is something to cut — a re-cut runs to the
        end of the chat, which is what :func:`_drop_deleted` is pushed off the event loop for,
        while the ordinary batch is a plain upsert that has always run on it.
        """
        if not rows:
            return []
        chat_id = rows[0].chat_id
        known = db.get_messages_by_msg_id(self.conn, chat_id, [row.msg_id for row in rows])
        stale = [
            row.msg_id
            for row in rows
            if (was := known.get(row.msg_id)) is not None
            and was.extracted_text
            and db.attachment_replaced(was, row)
        ]
        write = functools.partial(self._write, chat_id, rows, stale)
        ids = await _joined_to_thread(write, self.budget.cancel) if stale else write()
        fresh = sum(1 for row in rows if row.msg_id not in known)
        self.inserted[chat_id] = self.inserted.get(chat_id, 0) + fresh
        self.budget.spend(fresh)
        return ids

    async def store_new(self, rows: list[MessageRow]) -> tuple[list[int], int]:
        """:meth:`store` as many of ``rows`` as the message allowance has room for, in order;
        returns their row ids and how many of ``rows`` were stored. Fewer than all of them means
        the allowance ran out: the caller stops fetching and the rest comes next run."""
        held = self.budget.hold(len(rows))
        try:
            return await self.store(rows[:held]), held
        finally:
            self.budget.release(held)

    def _write(self, chat_id: int, rows: list[MessageRow], stale: Sequence[int]) -> list[int]:
        """The upsert and, for the rows whose extraction it just dropped, the re-cut."""
        with db.transaction(self.conn):
            self.peers.flush(self.conn)
            ids = db.upsert_messages(self.conn, rows)
            if stale:
                self._recut_replaced(chat_id, stale)
        return ids

    def _recut_replaced(self, chat_id: int, msg_ids: Sequence[int]) -> None:
        """Cut the units of the messages whose extracted text the upsert just cleared."""
        chat = db.get_chat(self.conn, chat_id)
        assert chat is not None  # the upsert's foreign key would have refused the rows otherwise
        reset = db.get_messages_by_msg_id(self.conn, chat_id, msg_ids)
        rows = [row for _, row in sorted(reset.items())]
        log.info(
            "chat %s: %d messages were re-stored with a different attachment; "
            "the text read off the old one was cut out of their units",
            chat_id,
            len(rows),
        )
        index.index_units(self.conn, units.invalidate_units_for(self.conn, chat, self.cfg, rows))
        _invalidate_comment_posts(self.conn, self.cfg, rows)

    def drop_comments(self, exc: Exception) -> None:
        """Stop fetching comments for this run; the posts themselves go on."""
        warning = (
            f"comments of channel {self.chat.id} ({self.chat.title}) are unavailable "
            f"({exc}); its posts were synced without them"
        )
        log.warning(warning)
        self.warnings.append(warning)
        self.discussion = None


def foreign_scope(chat: ChatRow, source: Source, account: str | None = None) -> str | None:
    """Why ``source`` may not fetch ``chat`` through ``account``, or ``None`` when it may.

    ``account`` is the one whose client would do the fetch, ``source.account`` when omitted. A
    private chat or legacy group is stored under the account whose history it is
    (``chats.scope``), and only that account's client may read into it: another account's
    conversation with the same peer has message ids of its own. A channel or supergroup is
    shared and any account may fetch it.
    """
    acting = source.account if account is None else account
    if not chat.scope or chat.scope == acting:
        return None
    return (
        f"chat {chat.id} ({chat.title}) is {chat.scope}'s own {chat.type}, so source {source.id} "
        f"cannot fetch it through account {acting}"
    )


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
    account: str | None = None,
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

    ``client`` is ``account``'s — ``source.account``'s unless the run falls back to another
    account that reaches a shared chat (:func:`_fetch_chat`) — and every chat stored on the way
    (a migration's supergroup, a channel's discussion group) is filed under that account
    (:func:`grepogram.db.upsert_chat`), while ``source`` still says how the chat is fetched
    (``since``, ``comments``). Telegram is addressed by ``chat.peer_id`` throughout and the rows
    by ``chat.id``. A private chat or legacy group is the account's own, so another account
    asking for one is a ``ValueError`` rather than a fetch through the wrong session.
    """
    acting = source.account if account is None else account
    foreign = foreign_scope(chat, source, acting)
    if foreign is not None:
        raise ValueError(foreign)
    if chat.migrated_to is not None:
        log.debug("chat %s migrated to %s; its history is frozen", chat.id, chat.migrated_to)
        return SyncedChat(chat=chat, migrated_to=db.get_chat(conn, chat.migrated_to))
    run = _Run(
        client=client,
        conn=conn,
        chat=chat,
        source=source,
        budget=budget,
        cfg=cfg or Config(),
        me=me,
        peers=_PeerBook(acting),
    )
    try:
        migrated = (
            await _check_migration(client, conn, chat, acting) if chat.type == "group" else None
        )
        if source.comments and chat.type == "channel":
            try:
                run.discussion = await link_discussion_chat(client, conn, chat, acting)
            except DiscussionUnavailable as exc:
                run.drop_comments(exc)
        fetched = await _fetch_new(run)
        if fetched.complete and chat.last_sync_at is not None:
            _track(run.changes, await _refetch_edits(run, run.cfg))
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
        chat.peer_id, min_id=progress, reverse=True, offset_date=offset_date
    )
    async for msg in iterator:
        seen_up_to = max(seen_up_to, int(msg.id))
        row = run.map(msg, chat)
        if row is not None:
            batch.append(row)
            if discussion is not None:
                run.replies[row.msg_id] = replies_count(msg)
        if len(batch) < _batch_size(run.budget):
            continue
        progress = await _store_batch(run, batch, progress, seen_up_to)
        batch = []
        if run.budget.halted or progress < seen_up_to:
            complete = False
            break
    if complete and (batch or seen_up_to > progress):
        progress = await _store_batch(run, batch, progress, seen_up_to)
        complete = progress >= seen_up_to
    return _Fetched(progress=progress, complete=complete, discussion=discussion)


def _batch_size(budget: SyncBudget) -> int:
    """How many rows :func:`_fetch_new` gathers before storing them: :data:`BATCH_SIZE`, or
    fewer when a message allowance has less left, so a run stops close to its cap rather than
    up to a whole batch past it."""
    left = budget.messages_left
    return BATCH_SIZE if left is None else max(1, min(BATCH_SIZE, left))


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
    ids, kept = await run.store_new(batch)
    _track(run.changes, ids)
    if kept < len(batch):
        # the message allowance ran out inside the batch: what was stored is the progress
        batch = batch[:kept]
        seen_up_to = batch[-1].msg_id if batch else progress
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
            if run.budget.halted:
                break
            if run.discussion is None:
                progress = seen_up_to
                break
            if run.replies.pop(row.msg_id, 0) > stored.get(row.msg_id, 0):
                _track(run.comment_ids, await _fetch_comments(run, row.msg_id))
                if run.budget.exhausted:
                    break  # the cap cut its thread short: the post comes again with it next run
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
    many comments are stored as Telegram reports replies. Comments are new messages like any
    other: the read stops once the message allowance is :attr:`~SyncBudget.exhausted`, and every
    batch is stored within it (:meth:`_Run.store_new`), so a research run's cap holds for a
    channel's threads as it does for its posts. The clock does not cut a thread: it is checked
    between posts (:func:`_store_batch`).
    """
    assert run.discussion is not None
    rows: list[MessageRow] = []
    stored: list[int] = []
    try:
        async for msg in run.client.iter_messages(run.chat.peer_id, reply_to=post_id):
            if run.budget.exhausted:
                break
            row = run.map(msg, run.discussion)
            if row is not None:
                rows.append(
                    dataclasses.replace(
                        row, comment_of_chat_id=run.chat.id, comment_of_msg_id=post_id
                    )
                )
            if len(rows) >= _batch_size(run.budget):
                ids, kept = await run.store_new(rows)
                stored += ids
                rows = rows[kept:]
                if rows:
                    break
    except errors.MsgIdInvalidError:
        log.debug("post %s in channel %s has no comment thread", post_id, run.chat.id)
    except UNAVAILABLE_ERRORS as exc:
        run.drop_comments(exc)
    finally:
        stored += (await run.store_new(rows))[0]
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
    async for msg in run.client.iter_messages(chat.peer_id, limit=cfg.sync.edit_refetch):
        seen.add(int(msg.id))
        row = run.map(msg, chat)
        if row is not None:
            fresh.append(row)
            replies[row.msg_id] = replies_count(msg)
    if not seen:
        return []
    stored = _with_links(run.conn, db.get_messages(run.conn, chat.id, since_msg_id=min(seen)))
    changed = [row for row in fresh if row.msg_id in stored and _differs(stored[row.msg_id], row)]
    if changed:
        log.debug(
            "chat %s: %d of %d re-fetched messages changed", chat.id, len(changed), len(fresh)
        )
    ids = await run.store(changed)
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


def _with_links(conn: sqlite3.Connection, rows: Iterable[MessageRow]) -> dict[int, MessageRow]:
    """``rows`` by ``msg_id``, each carrying its stored links — a row read back from the index
    carries ``None`` there (not read), which :func:`_differs` would take for a change."""
    by_msg_id = {row.msg_id: row for row in rows}
    links = db.message_links(conn, [row.id for row in by_msg_id.values() if row.id is not None])
    return {
        msg_id: dataclasses.replace(row, links=links.get(row.id, ()) if row.id is not None else ())
        for msg_id, row in by_msg_id.items()
    }


def _differs(stored: MessageRow, fresh: MessageRow) -> bool:
    """Whether storing ``fresh`` would change ``stored``.

    Compared with the values the upsert would keep: a comment re-read as part of its discussion
    group's own history arrives with no comment relation — and, outside a forum, with no topic —
    and must not count as an edit for want of what the upsert would have preserved anyway.

    ``extracted_text`` and ``media_state`` are normalised away on both sides instead, because a
    mapped row never carries either: it has no extracted text and :data:`db.MEDIA_PENDING`, so
    every extracted message inside the ``edit_refetch`` window would otherwise count as an edit
    on every sync and be re-cut and re-embedded forever. The "kept" idiom above cannot do it —
    it reads ``None`` as "not supplied", and a fresh row's ``media_state`` is ``0``.

    Normalising them away hides nothing the upsert acts on. The upsert clears them only when the
    attachment itself changed (:data:`db._ATTACHMENT_REPLACED`), and ``media_kind`` /
    ``media_filename`` — the two columns that say so — are compared here in full, so a replaced
    attachment reaches ``store`` as an edit and is cleared there while a caption edit is not and
    keeps its text.

    The forward origin and the ``links`` are compared in full (``stored`` carries its links,
    :func:`_with_links`), so **changed links alone re-store a row**: a bot that swaps its URL
    buttons with ``edit_hide`` set moves no ``edit_date``, and the upsert replaces the stored
    links with exactly what the re-stored row carries — never with less than a comparison saw.
    The first sync after schema step 8 therefore re-stores, once, the rows of each chat's
    ``edit_refetch`` window that name a Telegram destination or were forwarded, which is how
    those rows gain what step 8 captures; rows outside the window keep none — discovery reads
    their text instead (``messages.links_read``) until ``grepogram recapture-links``
    (:func:`recapture_links`) re-reads them.
    """
    kept = {
        field: getattr(stored, field) if getattr(fresh, field) is None else getattr(fresh, field)
        for field in ("topic_id", "comment_of_chat_id", "comment_of_msg_id", "links")
    }
    return _comparable(dataclasses.replace(stored, id=None)) != _comparable(
        dataclasses.replace(fresh, **kept)
    )


def _comparable(row: MessageRow) -> MessageRow:
    """``row`` without the columns :func:`db.upsert_messages` writes from no value of its own —
    see :func:`_differs`."""
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
    reread: list[int] = []
    for post_id in grown:
        if run.budget.halted or run.discussion is None:
            break
        ids = await _fetch_comments(run, post_id)
        _track(run.comment_ids, ids)
        reread += ids
        row_id = stored[post_id].id
        if ids and row_id is not None:
            touched.append(row_id)
    _refresh_comment_reactions(run, reread)
    if grown:
        log.debug(
            "channel %s: %d of %d re-fetched posts had new comments; %d threads re-read",
            run.chat.id,
            len(grown),
            len(replies),
            len(touched),
        )
    return touched


def _refresh_comment_reactions(run: _Run, row_ids: Sequence[int]) -> None:
    """Recompute the discussion group's unit reaction totals over the comments just re-read.

    A closed window is never re-cut and reactions are not part of ``units._content_key``, so
    ``refresh_unit_reactions`` is the only thing that moves a stored total
    (:func:`_refetch_edits` runs it for the source chat). A discussion group known only through
    a channel's link is never a source chat, so nothing else would ever run it there — the
    comment rows would keep the totals they were first fetched with for good.

    Telegram message ids, never the rowids ``_fetch_comments`` returns: ``units.msg_ids`` is the
    other id space, and the two coincide only in a chat whose history starts at 1.
    """
    if run.discussion is None or not row_ids:
        return
    rows = db.get_messages_by_ids(run.conn, row_ids)
    db.refresh_unit_reactions(run.conn, run.discussion.id, [row.msg_id for row in rows])


async def _check_migration(
    client: Any, conn: sqlite3.Connection, chat: ChatRow, account: str
) -> ChatRow | None:
    """Detect a legacy group upgraded to a supergroup; upsert and return the new chat row.

    The group is asked about by its peer id, which a group stored under a synthetic row id does
    not share; the supergroup is a shared chat, found by its Telegram identity and stored through
    ``account``, whose client is the one asking.
    """
    try:
        entity = await client.get_entity(chat.peer_id)
    except ValueError as exc:
        log.warning("chat %s (%s): cannot check for migration: %s", chat.id, chat.title, exc)
        return None
    target = getattr(entity, "migrated_to", None)
    if target is None:
        return None
    new_id = dialogs.peer_id(types.PeerChannel(int(target.channel_id)))
    new_chat = db.get_chat_by_peer(conn, new_id, chat_scope("supergroup", account))
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
        new_chat = db.upsert_chat(
            conn, _chat_row_from_entity(entity, chat.source_id, account), account
        )
    db.set_chat_migrated(conn, chat.id, new_chat.id)
    log.info("chat %s (%s) migrated to supergroup %s", chat.id, chat.title, new_chat.id)
    return new_chat


async def link_discussion_chat(
    client: Any, conn: sqlite3.Connection, channel: ChatRow, account: str
) -> ChatRow | None:
    """Upsert the channel's linked discussion group as its own ``chats`` row.

    ``client`` is ``account``'s, and the group is stored through that account
    (:func:`grepogram.db.upsert_chat`); the channel is asked about by its peer id.

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

    **A group this index holds as a Telegram Desktop import is refused**, the same way and with
    the same message :func:`~grepogram.sources.resolve_sources` uses
    (:func:`~grepogram.sources.imported_tag`): this is the third writer of ``chats.source_id``,
    and :func:`grepogram.db.upsert_chat` overwrites the column, so linking would replace
    ``import:<slug>`` with the channel's source and hand the imported history to every rule
    keyed on that prefix — ``sources rm`` of the channel's source would delete it,
    :func:`~grepogram.sources.prunable` would offer it, and the ``import:`` handle the refusal
    tells the user to remove would be gone. Refusing the link rather than only keeping the tag
    is what also keeps live comments out of a chat marked ``unavailable``, whose rows came from
    an export and which no sweep may re-fetch. It is reachable exactly where the import feature
    is useful: a group the account was kicked from still comes back inside ``full.chats``, so
    nothing else here would ever fail on it.
    """
    full = await client(functions.channels.GetFullChannelRequest(channel.peer_id))
    linked = getattr(full.full_chat, "linked_chat_id", None)
    if not linked:
        _relink_discussion(conn, channel, None)
        log.info(
            "channel %s (%s) has no discussion group; comments skipped", channel.id, channel.title
        )
        return None
    linked_id = dialogs.peer_id(types.PeerChannel(int(linked)))
    held = imported_tag(conn, linked_id)
    if held is not None:
        _drop_stale_link(conn, channel, linked_id)
        raise DiscussionUnavailable(
            f"discussion group {linked_id} is held as {held}, a Telegram Desktop import; "
            f"run `grepogram sources rm {held}` first to sync that group from Telegram"
        )
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
        stored = db.upsert_chat(conn, _chat_row_from_entity(entity, source_id, account), account)
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


def _chat_row_from_entity(entity: Any, source_id: str | None, account: str) -> ChatRow:
    """The row of a Telegram entity reached through ``account``, for :func:`db.upsert_chat`.

    The scope is spelled out rather than left to :class:`ChatRow`'s default, which is the
    default account's: a user, bot or legacy group some other account reached must never be
    filed under ``default``. ``id`` is the peer id only as a proposal — the upsert allocates.
    """
    info = dialogs.dialog_info(entity)
    return ChatRow(
        id=info.id,
        peer_id=info.id,
        scope=chat_scope(info.type, account),
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

    ``abort`` is for a cancellation and nothing else: a job that *raised* has already stopped,
    and the budget ``abort`` expires is the whole run's, shared by every account's queue — one
    chat's failed re-cut would otherwise end every other account's fetch at its next batch.
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
        if finished.is_set():
            raise
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
       them move, while the budget lasts — but **the first one is taken whether or not the
       fetch left anything of it**. This runs after :func:`_sync_chats` on the same budget, and
       on a large index the edit-refetch tail alone can spend all of it with the index otherwise
       up to date, so without that guarantee the MCP ``sync`` tool at its default budget would
       re-cut zero chats every time and the pending re-cut would never end. One chat per run
       still ends it, and the marker makes the next run continue. A budget that was *cancelled*
       keeps nothing back: the caller is going away and the lock with it;
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
    for position, chat in enumerate(pending[:RECUT_CHATS_PER_RUN]):
        if budget.expired and (position or budget.cancelled):
            log.info(
                "unit re-cut: %d chats are still cut by an older recipe and this run's budget "
                "is spent; the next sync carries on from here",
                len(pending) - position,
            )
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
    clients: Mapping[str, Any],
    conn: sqlite3.Connection,
    cfg: ConfigSource,
    paths: Paths,
    budget: SyncBudget,
    embedder: Embedder | None = None,
    recut: bool = True,
    only: Collection[str] | None = None,
) -> SyncReport:
    """Sync every configured source within ``budget``; every client must be connected.

    ``clients`` maps an account to its client, and every account's sources are resolved and
    fetched through its own (:func:`_sync_chats`): one queue per account, run concurrently, each
    chat fetched once — by the account of its primary source, falling back to another account
    that reaches a shared chat when that one is refused. A source whose account has no client in
    the mapping is skipped with a warning (:func:`~grepogram.sources.resolve_sources`). ``only``
    narrows the fetch to the chats the named sources (``Source.id``) cover; every source is still
    resolved, so a chat's primary source is the same whichever sources a run fetches.

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
    Telegram will not let a client sleep through stops that account's queue with a warning and
    leaves the other accounts' going; a chat Telegram refuses is reported in ``unavailable``; a
    session Telegram rejects raises :class:`~grepogram.tg.AuthRequired` naming its account (any
    other ``UnauthorizedError`` propagates as it is, for :func:`~grepogram.tg.connected` to turn
    into one). The end of the run is stamped in ``meta.last_sync_run`` whether or not a chat
    completed, so a caller deciding whether the index is stale does not retry a run that has
    nothing to finish.

    ``recut`` says whether this run may start the one-time unit re-cut
    (:func:`recut_pending_chats`). It is a property of the caller, not of the budget: an
    explicit sync — ``grepogram sync``, the MCP ``sync`` tool — is deliberate and makes whatever
    progress its budget allows, bounded at :data:`RECUT_CHATS_PER_RUN`, never less than one chat
    even when the fetch spent the whole budget, and resumable through the per-chat markers; the
    automatic sync inside an MCP ``search`` passes ``False`` so a search
    never rebuilds units incidentally, however large the user has set
    ``search.auto_sync_budget_s``.

    With an ``embedder`` the run ends by embedding the dirty units under the same budget and
    lock (:func:`~grepogram.index.embed_dirty_units`); units the budget leaves unembedded and a
    changed embedding model become ``warnings`` — the messages are synced either way. The
    re-cut and the embedding run once per run, after every account's queue is done.
    """
    with SyncLock(paths):
        current = cfg if isinstance(cfg, Config) else cfg()
        report = await _sync_chats(clients, conn, current, budget, only)
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
    """What the chat loop accumulates on its way to a :class:`SyncReport`.

    One tally for the whole run, whichever account's queue a chat went through. ``labelled``
    is set when the run holds any account but the default one: each warning then starts with
    ``account <name>:`` so a report of several accounts says whose fetch it is about, while a
    single-account install reads exactly what it always did.
    """

    labelled: bool = False
    new: int = 0
    done: list[int] = field(default_factory=list)
    remaining: list[int] = field(default_factory=list)
    unavailable: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def warn(self, account: str, warning: str) -> None:
        self.warnings.append(account_warning(self.labelled, account, warning))

    def record(self, chat_id: int, synced: SyncedChat, account: str) -> None:
        """Count one finished chat: its new messages, its warnings — labelled with ``account``,
        the one that fetched it — and where it ended up."""
        self.new += synced.new + (0 if synced.discussion is None else synced.discussion.new)
        for warning in synced.warnings:
            self.warn(account, warning)
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


def _record_failure(tally: _Tally, chat: ChatRow, exc: Exception, account: str) -> None:
    """Turn one chat's failure into a warning.

    An RPC error — or a peer the account's client cannot address at all, the plain
    ``ValueError`` Telethon raises for it — costs this chat its run, and a chat whose row
    disappeared under the sync — a removal that got past the lock, or a hand-edited database —
    costs it silently: it is gone, so there is nothing left to resume. A flood wait never
    reaches here (:func:`through_accounts` stops the account instead), nor does an unauthorized
    session, which is re-raised naming its account.
    """
    if isinstance(exc, errors.RPCError | ValueError):
        log.warning("chat %s (%s): %s; skipped this run", chat.id, chat.title, exc)
        tally.warn(account, f"chat {chat.id} ({chat.title}): {exc}")
        tally.remaining.append(chat.id)
        return
    log.warning("chat %s (%s) was removed during the sync: %s", chat.id, chat.title, exc)
    tally.warn(account, f"chat {chat.id} ({chat.title}) was removed while it was being synced")


def labels_accounts(accounts: Collection[str]) -> bool:
    """Whether a pass over ``accounts`` names the account in each warning: whenever it holds any
    account but the default one, so a report of several accounts says whose request a warning is
    about, while a single-account install reads exactly what it always did."""
    return any(account != DEFAULT_ACCOUNT for account in accounts)


def account_warning(labelled: bool, account: str, warning: str) -> str:
    """``warning`` as a report prints it: ``account <name>:`` in front when ``labelled``."""
    return f"account {account}: {warning}" if labelled else warning


def flood_warning(seconds: int | None, before: str, then: str) -> str:
    """The one wording of a flood wait in a report: how long Telegram asks to wait — "a while"
    when it named no time —, before what, and what that means for the caller."""
    wait = "a while" if seconds is None else f"{seconds}s"
    return f"flood wait: Telegram asks to wait {wait} before {before}; {then}"


def _flood_text(seconds: int) -> str:
    return flood_warning(seconds, "more history requests", "run sync again later")


@dataclass(slots=True, eq=False)
class _Lane:
    """One account's share of a run: its client, who it is, and the chats it fetches first.

    Whether the account is stopped — a flood wait in its own queue, in a fallback another queue
    made through it, or while its sources were resolved — is :attr:`_SyncPass.stopped`, which
    ends the queue at its next chat.
    """

    account: str
    client: Any
    me: UserRow | None
    queue: deque[tuple[ChatRow, Source]] = field(default_factory=deque)
    """The chats to fetch, each with the source it is fetched for — checked once, when the chat
    was queued (:func:`_enqueue`)."""


@dataclass(slots=True, eq=False)
class _SyncPass:
    """What every account's queue of one run shares: the index, the budget, the sources, the
    lanes and the tally, the accounts a flood wait stopped, and the chat ids already taken so no
    chat is fetched twice."""

    conn: sqlite3.Connection
    cfg: Config
    budget: SyncBudget
    sources: dict[str, Source]
    lanes: dict[str, _Lane]
    tally: _Tally
    stopped: set[str] = field(default_factory=set)
    processed: set[int] = field(default_factory=set)
    queued: set[int] = field(default_factory=set)
    deferred: list[ChatRow] = field(default_factory=list)
    unfetched: dict[str, int] = field(default_factory=dict)

    @property
    def clients(self) -> dict[str, Any]:
        return {account: lane.client for account, lane in self.lanes.items()}


async def _sync_chats(
    clients: Mapping[str, Any],
    conn: sqlite3.Connection,
    cfg: Config,
    budget: SyncBudget,
    only: Collection[str] | None = None,
) -> SyncReport:
    """The chat loop of :func:`sync_all`, one queue per account.

    Each account first signs its own ``me`` (the sender of its outgoing private messages,
    recorded in ``accounts``), then the sources of every account are resolved once, each through
    its own client (:func:`~grepogram.sources.resolve_sources`). Both steps are guarded per
    account, with the client's flood-sleep threshold capped against the budget first: a flood
    wait or a Telegram error on one account's ``get_me`` or resolve stops *that* account with a
    warning — its sources keep what they covered and stay the primary of their chats — and a
    rejected session is raised as :class:`~grepogram.tg.AuthRequired` naming the account it
    belongs to. A source that does not resolve on its own (its chat or folder no longer names
    anything its account reaches) is a warning in the report too, never only a log line: an
    approved source that never syncs would otherwise look like one with nothing new.

    Every chat goes to the queue of the first account :func:`reaching_accounts` names that is in
    the run and not stopped — its primary source's account when that one can — so a channel two
    accounts' sources cover is one row fetched once, and a chat whose own account has no client
    this run goes through another account that reaches it or, when none does, is reported as
    remaining. The queues run concurrently: each account talks to Telegram over its own
    connection and is rate-limited on its own, and the one database connection serialises their
    writes (:class:`grepogram.db.Connection`), every transaction being free of ``await``.
    Everything the queues share is in :class:`_SyncPass`. A failure that ends the run (an
    unauthorized session) cancels the other queues, which still index what they committed on
    their way out, and is re-raised as itself; a second account failing at the same time is
    logged and noted on the first.

    The unit rebuild runs on a worker thread so the loop keeps serving the clients' keepalives
    while a big chat is cut into units; it runs for every chat once its fetch is over — after
    the last batch, or after the batch a flood wait, an RPC error or a cancellation interrupted,
    which stays committed and searchable either way — and at the end for the chats the budget
    or a flood wait kept the run from reaching, whose pending rows come from an earlier run. The
    run ends with :func:`index_stranded`, a bounded sweep of the chats no source and no link
    leads to any more but that still hold flagged rows — once, after every queue. Failures are
    :func:`_record_failure`'s to describe; the tally becomes the report.
    """
    tally = _Tally(labelled=labels_accounts(clients))
    lanes: dict[str, _Lane] = {}
    stopped: set[str] = set()
    for account, client in clients.items():
        _cap_flood_sleep(client, cfg.sync, budget)
        lanes[account] = _Lane(account=account, client=client, me=None)
        try:
            me = await client.get_me()
        except errors.UnauthorizedError as exc:
            tg.reraise_unauthorized(exc, account)
        except errors.FloodWaitError as exc:
            log.warning(
                "flood wait of %ss on account %s; it sits this run out", exc.seconds, account
            )
            tally.warn(account, _flood_text(exc.seconds))
            stopped.add(account)
            continue
        except errors.RPCError as exc:
            log.warning("account %s could not be read: %s; it sits this run out", account, exc)
            tally.warn(account, f"could not be read ({exc}); its chats wait for the next run")
            stopped.add(account)
            continue
        lanes[account].me = _self_row(me)
        if not _record_account(conn, lanes[account], tally):
            del lanes[account]
    usable = {account: lane.client for account, lane in lanes.items() if account not in stopped}
    resolution = await resolve_sources(cfg, usable, conn)
    stopped |= set(resolution.flooded)
    for account, seconds in resolution.flooded.items():
        tally.warn(account, _flood_text(seconds))
    for account, error in resolution.failed.items():
        tally.warn(account, f"its sources could not be resolved ({error}); they keep what they had")
    for account, source_id, reason in resolution.unresolved:
        tally.warn(
            account,
            f"source {source_id} did not resolve ({reason}); it keeps the chats it already covered",
        )
    run = _SyncPass(
        conn=conn,
        cfg=cfg,
        budget=budget,
        sources={source.id: source for source in cfg.sources},
        lanes=lanes,
        tally=tally,
        stopped=stopped,
    )
    for chat in sorted(_only(conn, resolution.chats, only, run.sources), key=_sync_order):
        _enqueue(run, chat)
    for account, count in run.unfetched.items():
        tally.warn(
            account,
            f"{count} chats were not fetched: no account in this run reaches them, and "
            f"account {account} itself is not signed in or not part of it",
        )
    failures: list[BaseException] = []
    try:
        async with asyncio.TaskGroup() as group:
            for lane in lanes.values():
                if lane.queue:
                    group.create_task(_run_lane(run, lane))
    except BaseExceptionGroup as grouped:
        failures = _flattened(grouped)
    if failures:
        first = failures[0]
        for other in failures[1:]:
            log.warning("sync: another account failed in the same run: %s", other)
            first.add_note(f"another account failed in the same run: {other}")
        raise first
    for chat in run.deferred:
        await index_pending(conn, cfg, chat)
    await index_stranded(conn, cfg)
    return run.tally.report()


def _record_account(conn: sqlite3.Connection, lane: _Lane, tally: _Tally) -> bool:
    """Remember who ``lane.account`` turned out to be, when Telegram said; ``False`` — with a
    warning, and the account left out of the run — when its session is a Telegram user other
    than the one recorded under that name (:func:`other_user`, the one comparison every pass
    makes).

    ``grepogram auth`` refuses such a sign-in, so only a session file swapped by hand gets here;
    fetching with it would store another user's private chats over the recorded user's, under
    the same scoped rows, and address peers with access hashes that are not its own."""
    if lane.me is None:
        return True
    refused = other_user(conn, lane.account, lane.me.id)
    if refused is not None:
        log.warning("%s; it sits this run out", refused.reason)
        tally.warn(lane.account, _other_user_text(refused))
        return False
    db.upsert_account(
        conn,
        AccountRow(
            name=lane.account,
            user_id=lane.me.id,
            display_name=lane.me.display_name,
        ),
    )
    return True


def other_user(conn: sqlite3.Connection, account: str, user_id: int) -> tg.OtherUser | None:
    """:class:`~grepogram.tg.OtherUser` when ``user_id`` is not the Telegram user the index
    recorded under ``account``; ``None`` when it is, or when no user is recorded yet (a
    ``default`` from before accounts existed, a name never synced), which takes whoever signs
    in."""
    recorded = db.other_user(conn, account, user_id)
    if recorded is None or recorded.user_id is None:
        return None
    return tg.OtherUser(account, user_id, recorded.user_id)


def _other_user_text(refused: tg.OtherUser) -> str:
    return (
        f"its session is Telegram user {refused.user_id}, not user {refused.recorded} this index "
        f"recorded for it; nothing was done as it — `grepogram accounts rm {refused.account}` "
        "and sign it in again if the change is meant"
    )


async def check_account(conn: sqlite3.Connection, account: str, client: Any) -> None:
    """Make sure ``client`` — connected, signed in as ``account`` — is the Telegram user the
    index recorded under that name before anything acts through it; raises
    :class:`~grepogram.tg.OtherUser` when it is someone else.

    The one identity check every Telegram-facing pass goes through: a sync
    (:func:`_record_account`, which also records a first sign-in), the passes over stored chats
    (:meth:`StoredPass.start`: ``prune-deleted``, ``extract``, ``recapture-links``), the folder
    read of ``sources prune``, ``grepogram leave``, and every research pass that talks to
    Telegram — a run, a discover, a global search. A session file copied into place by hand, or
    one ``grepogram auth`` committed before this index recorded anyone, would otherwise delete,
    join, leave or ask as a user no one chose. Nothing is sent when no user is recorded yet; a
    rejected session is :class:`~grepogram.tg.AuthRequired` naming ``account``, and any other
    Telegram error propagates."""
    if db.get_account(conn, account) is None:
        return
    try:
        me = await client.get_me()
    except errors.UnauthorizedError as exc:
        tg.reraise_unauthorized(exc, account)
    if me is None:
        raise tg.AuthRequired(account=account)
    refused = other_user(conn, account, int(me.id))
    if refused is not None:
        raise refused


async def checked_accounts(
    conn: sqlite3.Connection, clients: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """``clients`` without the accounts :func:`check_account` refuses or cannot ask, and why
    each one was left out: a session of another Telegram user, or a Telegram error (a flood
    wait included) on the question itself — an account that cannot say who it is is not
    trusted to act. A rejected session is raised as :class:`~grepogram.tg.AuthRequired`."""
    kept: dict[str, Any] = {}
    left_out: dict[str, str] = {}
    for account, client in clients.items():
        try:
            await check_account(conn, account, client)
        except tg.OtherUser as exc:
            log.warning("%s; it sits this pass out", exc.reason)
            left_out[account] = _other_user_text(exc)
            continue
        except errors.FloodWaitError as exc:
            log.warning("flood wait of %ss asking who account %s is", exc.seconds, account)
            left_out[account] = flood_warning(
                int(exc.seconds), "asking who the account is", "it sat this pass out"
            )
            continue
        except errors.RPCError as exc:
            log.warning("account %s could not say who it is: %s", account, exc)
            left_out[account] = f"it could not say who it is ({exc}); it sat this pass out"
            continue
        kept[account] = client
    return kept, left_out


def _only(
    conn: sqlite3.Connection,
    resolved: Sequence[ChatRow],
    only: Collection[str] | None,
    sources: Mapping[str, Source],
) -> list[ChatRow]:
    """The resolved chats a run limited to the sources ``only`` names fetches: those any of them
    covers (``chat_sources``), whichever source is their primary. ``None`` keeps every chat."""
    if only is None:
        return list(resolved)
    wanted = set(only)
    for source_id in sorted(wanted - set(sources)):
        log.warning("sync: %s is not a configured source; nothing of it was fetched", source_id)
    return [chat for chat in resolved if wanted & set(db.chat_source_ids(conn, chat.id))]


def _enqueue(run: _SyncPass, chat: ChatRow) -> None:
    """Put a resolved chat in the queue of the first account that may fetch it.

    A chat with no configured source is skipped, and so is a private chat misfiled under another
    account's source (reported). Otherwise it goes to the first account :func:`_route` names.
    When it names none, the chat's pending rows are still indexed in the run's deferred pass, and
    the report says why it was not fetched: a flood wait stopped every account that reaches it —
    it is remaining, and the next run fetches it — or no account of this run reaches it at all,
    its own having no client (not signed in, or left out), which :attr:`_SyncPass.unfetched`
    counts per account for one warning each rather than listing it as remaining: running the
    sync again would not fetch it.
    """
    source = run.sources.get(chat.source_id or "")
    if source is None:
        log.debug("chat %s has no configured source; skipped", chat.id)
        return
    foreign = foreign_scope(chat, source)
    if foreign is not None:
        log.warning("%s; skipped", foreign)
        run.tally.warn(source.account, foreign)
        return
    route = _route(run, chat)
    if not route:
        run.deferred.append(chat)
        if any(account in run.stopped for account in reaching_accounts(run.conn, chat, run.lanes)):
            run.tally.remaining.append(chat.id)
            return
        log.info(
            "chat %s (%s): no account of this run reaches it (its own is %s); not fetched",
            chat.id,
            chat.title,
            source.account,
        )
        run.unfetched[source.account] = run.unfetched.get(source.account, 0) + 1
        return
    run.lanes[route[0]].queue.append((chat, source))
    run.queued.add(chat.id)


def _route(run: _SyncPass, chat: ChatRow, first: str | None = None) -> list[str]:
    """The accounts of this run to fetch ``chat`` through, in order: ``first`` when given, then
    :func:`reaching_accounts` — the same order every pass over stored chats uses — leaving out
    the accounts a flood wait stopped."""
    order = reaching_accounts(run.conn, chat, run.lanes)
    if first is not None:
        order = [first, *(account for account in order if account != first)]
    return [account for account in order if account not in run.stopped]


def _flattened(grouped: BaseExceptionGroup[BaseException]) -> list[BaseException]:
    """Every failure inside ``grouped``, unwrapped from every nested group, in order."""
    found: list[BaseException] = []
    for exc in grouped.exceptions:
        found += _flattened(exc) if isinstance(exc, BaseExceptionGroup) else [exc]
    return found


async def _run_lane(run: _SyncPass, lane: _Lane) -> None:
    """Fetch ``lane``'s queue, one chat after the other, until it is empty or the account stops.

    A chat the budget or a flood wait keeps from its turn is reported as remaining and handed to
    the run's deferred pass (:func:`index_pending` for what earlier runs left); a chat a fetch
    went through is indexed right after it, however it ended. A legacy group that turns out to
    have migrated has its supergroup appended to this queue, unless some queue has taken it.
    """
    tally = run.tally
    while lane.queue:
        chat, source = lane.queue.popleft()
        run.queued.discard(chat.id)
        run.processed.add(chat.id)
        if lane.account in run.stopped or run.budget.halted:
            tally.remaining.append(chat.id)
            run.deferred.append(chat)
            continue
        try:
            try:
                fetched = await _fetch_chat(run, lane, chat, source)
            finally:
                await index_pending(run.conn, run.cfg, chat)
        except sqlite3.IntegrityError as exc:
            _record_failure(tally, chat, exc, lane.account)
            continue
        if fetched.error is not None:
            _record_failure(tally, chat, fetched.error, fetched.account)
        elif fetched.synced is None:
            tally.remaining.append(chat.id)
        else:
            tally.record(chat.id, fetched.synced, fetched.account)
            migrated = fetched.synced.migrated_to
            if migrated is not None and migrated.id not in run.processed | run.queued:
                _enqueue_migrated(run, lane, migrated)
        if lane.account in run.stopped and lane.queue:
            tally.remaining.extend(c.id for c, _ in lane.queue)
            run.deferred.extend(c for c, _ in lane.queue)
            lane.queue.clear()


def _enqueue_migrated(run: _SyncPass, lane: _Lane, supergroup: ChatRow) -> None:
    """Queue the supergroup a legacy group migrated to on ``lane``, for the source its row is
    filed under. A supergroup is shared, so no account's scope stands in the way; one no
    configured source covers is left alone."""
    source = run.sources.get(supergroup.source_id or "")
    if source is None:
        log.debug("chat %s has no configured source; skipped", supergroup.id)
        return
    lane.queue.append((supergroup, source))
    run.queued.add(supergroup.id)


@dataclass(frozen=True, slots=True)
class _Fetch:
    """How one chat's fetch ended: through ``account`` with ``synced``, with ``error`` (the
    account it came through named for the report), or with neither when no account was asked —
    every one that reaches the chat stopped by a flood wait, or the budget spent."""

    account: str
    synced: SyncedChat | None = None
    error: Exception | None = None


async def _fetch_chat(run: _SyncPass, lane: _Lane, chat: ChatRow, source: Source) -> _Fetch:
    """Fetch one chat through ``lane``'s account, and through another when that one is refused.

    A private chat or legacy group belongs to one account and is fetched through it or not at
    all. A channel or supergroup is one shared row that several accounts may reach, so when the
    first account is refused — the chat went private for it, it was banned
    (:data:`UNAVAILABLE_ERRORS`, which :func:`sync_chat` reports as ``unavailable``), or its
    client cannot address the peer at all (``ValueError``, a chat its source no longer resolves
    through) — or stopped by a flood wait, the other accounts of this run that reach it are tried
    in :func:`reaching_accounts`' order (:func:`through_accounts`, the one retry rule every pass
    shares). Each is warmed first from what the index stores for it (:func:`warm_peer_cache`):
    its client may never have read a dialog this run. The first that fetches it clears the
    ``unavailable`` flag the refusal set, the report says which account stood in, and the chat's
    own warnings are labelled with the account that fetched it. A flood wait on any account
    stops *that* account's queue. When every account is refused, the chat is reported as the
    first refusal left it. The primary source never moves: it says how the chat is fetched
    (``since``, ``comments``), not through whom.
    """
    route = _route(run, chat, lane.account) if chat.is_shared else [lane.account]
    current = chat

    async def fetch(account: str, client: Any) -> SyncedChat:
        nonlocal current
        synced = await sync_chat(
            client,
            run.conn,
            current,
            source,
            run.budget,
            cfg=run.cfg,
            me=run.lanes[account].me,
            account=account,
        )
        current = synced.chat
        return synced

    outcome = await through_accounts(
        run.conn,
        run.clients,
        chat,
        route,
        run.cfg.sync,
        run.budget,
        run.stopped,
        fetch,
        refused=lambda synced: synced.unavailable,
        halted=lambda: run.budget.halted,
        # the primary's client learned the chat resolving its source, or from the stored
        # access hashes seeded before it; any other account is warmed for it first
        warmed={lane.account} if lane.account == source.account else (),
    )
    for account, seconds in outcome.flooded:
        log.warning("chat %s: flood wait of %ss through account %s", chat.id, seconds, account)
        run.tally.warn(account, _flood_text(seconds))
    if outcome.account is not None:
        assert outcome.result is not None
        if outcome.refusals:
            first = outcome.refusals[0]
            run.tally.warn(
                first.account,
                f"chat {chat.id} ({chat.title}): {first.reason} through account "
                f"{first.account}; fetched through account {outcome.account} instead",
            )
        return _Fetch(outcome.account, outcome.result)
    if outcome.failure is not None:
        return _Fetch(outcome.failure[0], error=outcome.failure[1])
    if outcome.refused is not None:
        return _Fetch(outcome.refused[0], outcome.refused[1])
    if outcome.refusals and outcome.refusals[0].error is not None:
        return _Fetch(outcome.refusals[0].account, error=outcome.refusals[0].error)
    return _Fetch(lane.account)


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


# --- which account asks about a chat ---------------------------------------------------------


def recorded_reach(conn: sqlite3.Connection, chat: ChatRow) -> list[str]:
    """The accounts the index records as reaching ``chat``, in the order to ask them.

    A private chat, a bot or a legacy group is its scope account's history and nobody else's —
    another account's chat with the same person carries other message ids. A channel or
    supergroup is one shared row: the account of its primary source goes first (the one a sync
    fetches it through), then every account ``chat_access`` records as reaching it, then — for a
    discussion group, which a sync reaches through its channel's link rather than a resolve of
    its own and so may have no access row — the accounts that reach the channel it holds the
    comments of, and the channel's primary source's. Empty for a shared row nothing ties to any
    account at all (one built by hand, or by a link to a channel the index no longer holds).
    """
    if not chat.is_shared:
        return [chat.scope]
    order: list[str] = []
    if chat.source_id and not chat.source_id.startswith(IMPORT_PREFIX):
        order.append(source_account(chat.source_id))
    order += db.chat_accounts(conn, chat.id)
    if chat.discussion_of is not None:
        order += db.chat_accounts(conn, chat.discussion_of)
        channel = db.get_chat(conn, chat.discussion_of)
        if channel is not None and channel.source_id:
            order.append(source_account(channel.source_id))
    return list(dict.fromkeys(order))


def reaching_accounts(
    conn: sqlite3.Connection, chat: ChatRow, connected: Collection[str]
) -> list[str]:
    """The accounts of ``connected`` a pass may ask about ``chat`` through, in the order to try.

    :func:`recorded_reach`, narrowed to ``connected`` — so a private chat is asked through its
    own account or not at all, never through a defaulted one. A shared row nothing ties to any
    account is anyone's to ask about: every connected account, the default one first. The one
    order a sync (:func:`_fetch_chat`), the extraction pass and the deletion sweep all follow.
    """
    order = recorded_reach(conn, chat)
    if not order:
        order = sorted(connected, key=lambda account: (account != DEFAULT_ACCOUNT, account))
    return [account for account in order if account in connected]


_REROUTE_ERRORS: tuple[type[Exception], ...] = (ValueError, *UNAVAILABLE_ERRORS)
"""What sends :func:`through_accounts` on to the next account that reaches a shared chat: a
refusal, or a peer this account's client cannot address at all."""


@dataclass(frozen=True, slots=True)
class Refusal:
    """One account a shared chat was refused to — why, and the error when it was one."""

    account: str
    reason: str
    error: Exception | None = None


@dataclass(slots=True)
class Attempt[T]:
    """How :func:`through_accounts` went for one chat.

    ``account`` and ``result`` are who answered and what, when someone did. ``refusals`` are the
    accounts it was refused to on the way, in order, and ``refused`` the first refusal that came
    as an answer rather than an error (a :class:`SyncedChat` marked ``unavailable``).
    ``flooded`` holds each account a flood wait stopped, with the seconds asked, and ``failure``
    the error that ended the chat's turn outright.
    """

    account: str | None = None
    result: T | None = None
    refusals: list[Refusal] = field(default_factory=list)
    refused: tuple[str, T] | None = None
    flooded: list[tuple[str, int]] = field(default_factory=list)
    failure: tuple[str, Exception] | None = None


def _never(_: object) -> bool:
    return False


async def through_accounts[T](
    conn: sqlite3.Connection,
    clients: Mapping[str, Any],
    chat: ChatRow,
    route: Sequence[str],
    sync_cfg: SyncCfg,
    budget: SyncBudget,
    stopped: set[str],
    act: Callable[[str, Any], Awaitable[T]],
    *,
    refused: Callable[[T], bool] = _never,
    halted: Callable[[], bool] | None = None,
    warmed: Collection[str] = (),
) -> Attempt[T]:
    """``act(account, client)`` through the first account of ``route`` that answers about
    ``chat`` — the one rule every pass that may take a chat through several accounts follows.

    Accounts in ``stopped`` are passed over. Before each attempt the client's flood-sleep
    threshold is capped against the budget (:func:`_cap_flood_sleep`) and, unless the caller
    warmed it already (``warmed``), the client is taught the chat's peer
    (:func:`warm_peer_cache`). Then:

    * a flood wait stops that account — added to ``stopped`` for the rest of the pass — and the
      next account is tried;
    * a rejected session is raised naming its account (:func:`~grepogram.tg.reraise_unauthorized`);
    * a shared chat refused to the account (:data:`_REROUTE_ERRORS`), or answered with a result
      ``refused`` says is a refusal, is tried through the next account;
    * any other Telegram error, or any refusal of a private chat, ends the chat's turn.

    A :class:`~grepogram.config.ConfigError` is the caller's to raise, and ``halted`` (the
    clock alone by default) ends the walk before the next account — the first is the caller's to
    decide on, which every caller does just before it asks.
    """
    outcome: Attempt[T] = Attempt()
    stop = halted if halted is not None else (lambda: budget.expired)
    tried = False
    for account in route:
        if account in stopped:
            continue
        if tried and stop():
            break
        tried = True
        client = clients[account]
        _cap_flood_sleep(client, sync_cfg, budget)
        try:
            if account not in warmed:
                await warm_peer_cache(client, [chat], conn, account)
            result = await act(account, client)
        except ConfigError:
            raise
        except errors.FloodWaitError as exc:
            log.warning(
                "flood wait of %ss on chat %s through account %s; stopping it for this run",
                exc.seconds,
                chat.id,
                account,
            )
            stopped.add(account)
            outcome.flooded.append((account, int(exc.seconds)))
            continue
        except errors.UnauthorizedError as exc:
            tg.reraise_unauthorized(exc, account)
        except (errors.RPCError, ValueError) as exc:
            log.warning("chat %s (%s) through account %s: %s", chat.id, chat.title, account, exc)
            if chat.is_shared and isinstance(exc, _REROUTE_ERRORS):
                outcome.refusals.append(Refusal(account, str(exc), exc))
                continue
            outcome.failure = (account, exc)
            return outcome
        if chat.is_shared and refused(result):
            outcome.refusals.append(Refusal(account, "Telegram refused it"))
            if outcome.refused is None:
                outcome.refused = (account, result)
            continue
        outcome.account, outcome.result = account, result
        return outcome
    return outcome


@dataclass(slots=True, eq=False)
class StoredPass:
    """Which account a pass over stored chats asks about each chat through.

    :func:`prune_deleted` and :func:`grepogram.media.run` walk ``chats`` rows rather than a
    source list and re-fetch by id, so each chat needs an account that reaches it and a client
    that can address it. :meth:`start` routes every chat (:func:`reaching_accounts`) and warms
    each client with the chats it is asked about first — or, with ``every``, with every chat it
    may be asked about at all (:func:`warm_peer_cache`, seeded from the stored access hashes
    before any request); a chat no connected account reaches is put in ``unreachable`` —
    reported, never an error, and never asked through an account it is not.

    :meth:`visit` asks through the first account left (:func:`through_accounts`): a flood wait
    stops that account for the rest of the pass (its chats move on to the next account that
    reaches them, or wait for the next run), a shared chat its account is refused — or cannot
    address at all — is tried through the next one, warmed for that chat first, and any other
    error costs the chat its turn with a warning. Warnings name their account whenever the pass
    holds one but the default.
    """

    conn: sqlite3.Connection
    clients: Mapping[str, Any]
    sync_cfg: SyncCfg
    budget: SyncBudget
    flood_warning: Callable[[int], str]
    routes: dict[int, list[str]] = field(default_factory=dict)
    unreachable: list[ChatRow] = field(default_factory=list)
    stopped: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    async def start(
        cls,
        conn: sqlite3.Connection,
        clients: Mapping[str, Any],
        chats: Sequence[ChatRow],
        sync_cfg: SyncCfg,
        budget: SyncBudget,
        flood_warning: Callable[[int], str],
        *,
        every: bool = False,
    ) -> "StoredPass":
        """Route ``chats`` and, unless the budget is already spent, warm every client for the
        chats it goes first for — or, with ``every``, for each chat it is on the route of. The
        flood-sleep cap goes on **before** each warm-up, which makes requests of its own.

        Every account is put to :func:`check_account` first: one whose session is another
        Telegram user than the index recorded, or that cannot say who it is, is left out with a
        warning — its chats go through another account that reaches them, or are
        ``unreachable`` — because a deletion sweep run as someone else reads that user's
        answers as the recorded user's history being gone."""
        for client in clients.values():
            _cap_flood_sleep(client, sync_cfg, budget)
        checked, left_out = await checked_accounts(conn, clients)
        state = cls(conn, checked, sync_cfg, budget, flood_warning)
        labelled = labels_accounts(clients)
        state.warnings.extend(
            account_warning(labelled, account, reason) for account, reason in left_out.items()
        )
        clients = checked
        asked: dict[str, list[ChatRow]] = {}
        for chat in chats:
            route = reaching_accounts(conn, chat, clients)
            if not route:
                state.unreachable.append(chat)
                log.info(
                    "chat %s (%s): no connected account reaches it; left alone",
                    chat.id,
                    chat.title,
                )
                continue
            state.routes[chat.id] = route
            for account in route if every else route[:1]:
                asked.setdefault(account, []).append(chat)
        if budget.expired:
            return state
        for account, routed in asked.items():
            client = clients[account]
            _cap_flood_sleep(client, sync_cfg, budget)
            try:
                await warm_peer_cache(client, routed, conn, account)
            except errors.UnauthorizedError as exc:
                tg.reraise_unauthorized(exc, account)
        return state

    def warn(self, account: str, warning: str) -> None:
        self.warnings.append(account_warning(labels_accounts(self.clients), account, warning))

    def stop(self, account: str, seconds: int) -> None:
        """A flood wait of ``seconds`` on ``account``: no more requests through it this pass."""
        self.stopped.add(account)
        self.warn(account, self.flood_warning(seconds))

    async def visit[T](self, chat: ChatRow, act: Callable[[Any], Awaitable[T]]) -> T | None:
        """``act(client)`` for the first account that answers about ``chat``; ``None`` when none
        did — a flood wait, a refusal, an error or the budget ended its turn."""

        async def through(account: str, client: Any) -> T:
            return await act(client)

        return await self.visit_as(chat, through)

    async def visit_as[T](self, chat: ChatRow, act: Callable[[str, Any], Awaitable[T]]) -> T | None:
        """:meth:`visit` for a pass that needs to know which account answered:
        ``act(account, client)``."""
        route = self.routes.get(chat.id, [])
        outcome = await through_accounts(
            self.conn,
            self.clients,
            chat,
            route,
            self.sync_cfg,
            self.budget,
            self.stopped,
            act,
            warmed=route[:1],
        )
        for account, seconds in outcome.flooded:
            self.warn(account, self.flood_warning(seconds))
        if outcome.account is not None:
            return outcome.result
        if outcome.failure is not None:
            account, exc = outcome.failure
            self.warn(account, f"chat {chat.id} ({chat.title}): {exc}")
        elif outcome.refusals:
            last = outcome.refusals[-1]
            self.warn(last.account, f"chat {chat.id} ({chat.title}): {last.reason}")
        return None


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
    unreachable: list[int] = field(default_factory=list)
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
            chats_unreachable=self.unreachable,
            warnings=self.warnings,
        )


async def prune_deleted(
    clients: Mapping[str, Any],
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
    mid-sweep, a flood wait, all end the chat's turn with nothing removed — and so does the
    ``ValueError`` Telethon raises for a peer this account cannot resolve at all, which is a
    plain exception rather than an ``RPCError`` and would otherwise end the whole sweep on the
    first chat that hit it.

    ``clients`` maps an account to its connected client, and every chat is asked about through
    the accounts that reach it (:class:`StoredPass`): a private chat through its own account
    alone, a shared one through **every** account that reaches it, because one account's empty
    slot is not proof of a deletion. An account that joined a group after it began — or one the
    group hides its history from — sees older messages as empty while another account still
    reads them, and the index holds whatever the account that fetched it could see. So a message
    is removed only when every account the index records as reaching the chat
    (:func:`recorded_reach`) answers it empty (:func:`_confirmed_gone`): one of them not in this
    run or stopped by a flood wait leaves the chat untouched with a warning, and an account
    Telegram refuses the chat to outright is no witness either way and is passed over — though
    at least one account must answer. A chat no connected account reaches is left alone and
    reported in ``chats_unreachable`` — its cursor untouched, nothing removed.

    Addressing a chat by its stored id is only possible once the client knows that peer, and a
    client grepogram builds knows none: :func:`warm_peer_cache` seeds the stored access hashes
    and reads the dialog list for the rest, for the reasons written there. A sync gets that for
    free from :func:`~grepogram.sources.resolve_sources`; this pass walks ``chats`` rows instead
    of a source list, so it asks by hand.

    It is a whole-index pass of about one request per hundred stored messages, so it is never
    automatic and never an MCP tool: like ``sources prune``, deleting indexed history stays a
    deliberate CLI action (``grepogram prune-deleted``). ``budget`` is in seconds like every
    other pass, the client's ``flood_sleep_threshold`` is capped against what is left of it
    (:func:`_cap_flood_sleep`) **before the warm-up rather than after it** — the warm-up is a
    request like any other, and a client still carrying the default 120-second threshold would
    sleep through a sub-threshold flood wait far longer than the whole budget before the sweep
    had asked a single question — and the whole sweep runs under the :class:`SyncLock` — it deletes
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
        route = await StoredPass.start(
            conn, clients, targets, cfg.sync, budget, _prune_flood_warning, every=True
        )
        tally = _PruneTally(warnings=route.warnings)
        tally.unreachable.extend(chat.id for chat in route.unreachable)
        routed = [chat for chat in targets if chat.id in route.routes]
        for position, chat in enumerate(routed):
            if budget.expired:
                tally.remaining.extend(rest.id for rest in routed[position:])
                break
            complete = await _sweep_chat(route, conn, cfg, chat, budget, tally)
            (tally.done if complete else tally.remaining).append(chat.id)
        return tally.report()


# --- the link recapture pass -----------------------------------------------------------------


@dataclass(slots=True)
class _RecaptureTally:
    """What the recapture pass accumulates on its way to a :class:`RecaptureReport`."""

    checked: int = 0
    captured: int = 0
    done: list[int] = field(default_factory=list)
    remaining: list[int] = field(default_factory=list)
    unreachable: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def recapture_links(
    clients: Mapping[str, Any],
    conn: sqlite3.Connection,
    cfg: Config,
    paths: Paths,
    budget: SyncBudget,
    *,
    chat_id: int | None = None,
) -> RecaptureReport:
    """Re-read the stored messages whose links were never read and write down what they link to.

    Rows stored before schema step 8 carry neither their links nor their forward origin, and a
    sync only ever re-reads the newest ``edit_refetch`` messages of a chat — so an upgraded index
    would keep the hidden hyperlinks, buttons and forward origins of its older history out of
    research's reach for good, leaving only the visible text for discovery's fallback. This pass
    asks Telegram about exactly those rows (``messages.links_read = 0``) by id, a hundred per
    request (:data:`PRUNE_BATCH`), through an account that reaches each chat
    (:class:`StoredPass`, which also warms each client first — a client grepogram builds knows
    no peer), and writes onto them **only** the ``message_links``, the ``fwd_*`` columns and
    ``links_read`` (:func:`grepogram.db.set_captured_links`): no text, no media, no ``indexed``
    flag and no sync cursor moves — ``chats.last_msg_id`` stays where the sync left it — and no
    unit changes, because a unit renders none of these. Each row it writes takes the next
    lead-clock tick, so research's discovery reads it again. The forward origins the answers
    carry are recorded for the answering account (:func:`remember_forward_peers`).

    A message Telegram no longer has is passed over — telling deletions apart is
    ``prune-deleted``'s — and a chat whose history never came from Telegram (an import) or that
    Telegram refuses is not asked about (:func:`refetchable`). Progress is a ``meta`` cursor per
    chat written with each page, so a run stopped by its budget or a flood wait resumes where it
    stopped. It is a long, flood-exposed network pass over old history, so it runs under the
    :class:`SyncLock` from ``grepogram recapture-links`` alone — never inside a sync and never as
    an MCP tool. ``chat_id`` narrows it to one chat and the discussion group it links.
    """
    with SyncLock(paths):
        targets = _recapture_targets(conn, chat_id)
        route = await StoredPass.start(
            conn, clients, targets, cfg.sync, budget, _recapture_flood_warning
        )
        tally = _RecaptureTally(warnings=route.warnings)
        tally.unreachable.extend(chat.id for chat in route.unreachable)
        routed = [chat for chat in targets if chat.id in route.routes]
        for position, chat in enumerate(routed):
            if budget.expired:
                tally.remaining.extend(rest.id for rest in routed[position:])
                break
            complete = await _recapture_chat(route, conn, chat, budget, tally)
            (tally.done if complete else tally.remaining).append(chat.id)
        left = db.count_unread_links(conn, [chat.id for chat in targets])
    log.info(
        "recapture-links: %d of %d messages re-read, %d chats done, %d left",
        tally.captured,
        tally.checked,
        len(tally.done),
        len(tally.remaining),
    )
    return RecaptureReport(
        checked=tally.checked,
        captured=tally.captured,
        remaining=left,
        chats_done=tally.done,
        chats_remaining=tally.remaining,
        chats_unreachable=tally.unreachable,
        warnings=tally.warnings,
    )


def _recapture_targets(conn: sqlite3.Connection, chat_id: int | None) -> list[ChatRow]:
    """The chats holding rows whose links were never read that a pass may re-fetch, in id order;
    ``chat_id`` narrows them to that chat and its discussion group."""
    wanted = set(db.chats_with_unread_links(conn))
    if chat_id is not None:
        group = db.get_discussion_chat(conn, chat_id)
        wanted &= {chat_id, *([group.id] if group is not None else [])}
    chats = [db.get_chat(conn, found) for found in sorted(wanted)]
    return [chat for chat in chats if chat is not None and refetchable(chat)]


async def _recapture_chat(
    route: StoredPass,
    conn: sqlite3.Connection,
    chat: ChatRow,
    budget: SyncBudget,
    tally: _RecaptureTally,
) -> bool:
    """One chat from its cursor on; ``True`` once no unread row is left above it."""
    cursor = db.recapture_cursor(conn, chat.id)
    while not budget.expired:
        page = db.unread_link_ids(conn, chat.id, cursor, PRUNE_BATCH)
        if not page:
            return True

        async def read(account: str, client: Any, page: list[int] = page) -> tuple[str, Any]:
            return account, await client.get_messages(chat.peer_id, ids=page)

        answered = await route.visit_as(chat, read)
        if answered is None:
            return False
        account, answer = answered
        if not isinstance(answer, list) or len(answer) != len(page):
            route.warn(
                account,
                f"chat {chat.id} ({chat.title}): Telegram's answer did not line up with the "
                f"{len(page)} ids asked about; nothing was written",
            )
            return False
        found = [
            msg for msg in answer if msg is not None and not isinstance(msg, types.MessageEmpty)
        ]
        rows = [row for msg in found if (row := map_message(msg, chat, {})) is not None]
        remember_forward_peers(conn, account, found)
        cursor = page[-1]
        tally.checked += len(page)
        tally.captured += db.set_captured_links(conn, chat.id, rows, cursor)
    return False


def _recapture_flood_warning(seconds: int) -> str:
    return flood_warning(seconds, "more requests", "run `grepogram recapture-links` again later")


def _prune_flood_warning(seconds: int) -> str:
    return flood_warning(seconds, "more requests", "run `grepogram prune-deleted` again later")


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


async def warm_peer_cache(
    client: Any, chats: Sequence[ChatRow], conn: sqlite3.Connection, account: str
) -> None:
    """Teach ``client``, ``account``'s, the peers of ``chats`` before anything addresses them by
    bare id.

    :func:`grepogram.tg.load_session` copies the data centre and the auth key out of the session
    file and nothing else, so **the entity cache of every client grepogram builds starts empty**
    — the docstring there states the rule and every Telegram-facing pass has to honour it. A
    request that names a chat by its stored id and nothing else, which is what
    ``client.get_messages(chat.peer_id, ids=[…])`` is, has no access hash to build an ``InputPeer``
    from: Telethon 1.44 asks the session, gets nothing, and its network fallback
    (``channels.getChannels`` / ``users.getUsers`` with ``access_hash = 0``) is documented to
    answer only for a bot's private chats or a contact. For a user session on a private
    supergroup it ends in a plain ``ValueError: Could not find the input entity``, which is not
    an ``RPCError`` and reaches a caller as a skipped chat or a traceback.

    One ``get_dialogs()`` is the whole fix: Telethon writes the peers of every answer into the
    session (``session.process_entities``), so the dialog list makes every chat the account has
    a dialog with addressable for the rest of the client's life. It is the same warm-up a sync
    gets for free from :func:`~grepogram.sources.resolve_sources` and the reason no path that
    goes through a :class:`~grepogram.dialogs.DialogCatalog` ever had to think about this; the
    two passes that walk stored rows instead of a source list — :func:`prune_deleted` and
    :func:`grepogram.media.run` — are the ones that must ask for it by hand. The client's own
    call rather than that catalog, because neither pass has any use for the folder list the
    catalog reads beside it.

    **What the index already stores comes first, and costs no request at all.** Every resolve
    records the access hash each account addresses a chat by (``chat_access``,
    :func:`grepogram.db.access_hash`), and that hash is all an ``InputPeer`` needs: it is handed
    to the client's session (``session.process_entities``, the call Telethon itself feeds every
    answer through), and a chat seeded that way — or a legacy group, which needs no hash at all
    — is left out of every route below. Only the rest cost the dialog list, and none left means
    no request. A discussion group's channel is seeded the same way, so the third route below can
    name it. The hash is ``account``'s own: another account's would address the peer as a
    different user, and Telegram refuses it.

    The dialog list is not the whole account, though, and the two chats it misses are exactly
    the ones a source list reaches by a **stored handle** rather than by id:

    * A public channel or group the account follows without joining has no dialog at all. A sync
      never notices, because its source is a ``chat = "@name"`` and
      :func:`~grepogram.sources.resolve_sources` resolves the handle — and that handle is stored
      on the row as ``chats.username``, so this can walk the same route with
      ``client.get_entity(chat.username)``. Skipping it left ``extract`` and ``prune-deleted``
      failing every by-id request for precisely the chats the warm-up was added for.
    * A channel's discussion group is indexed through the channel's link
      (:func:`link_discussion_chat`) and may have neither a dialog nor a username of its own.
      ``GetFullChannelRequest`` on the channel answers with the group among ``full.chats``,
      which caches it exactly as the dialog list caches a dialog.

    Both in that order, and the order matters: the *channel* a link-only group hangs off may
    itself be outside the dialog list, and naming it by bare id in ``GetFullChannelRequest``
    would fail for the same reason everything else here does. The username pass runs over the
    whole list first, so a channel with a handle is resolved before its group is asked for.

    ``resolved`` is what the session is known to hold, keyed by what actually came back
    (``utils.get_peer_id``) rather than by what was asked for: a handle that has moved to another
    peer resolves *that* one, and the chat it was stored on still needs its second route.

    Nothing here is worth failing a pass for: a chat no route resolves is left to the caller's
    per-chat handler, which costs that chat its turn and reports it, and a Telegram error during
    the warm-up (a flood wait included) resurfaces on the very next request the pass makes, where
    it is handled properly. An ``UnauthorizedError`` is the exception every handler makes, for
    the reason :func:`_sync_chats` makes it: a session revoked mid-run is not a chat that would
    not resolve, and only re-raising lets :func:`grepogram.tg.wrap_auth_errors` turn it into an
    :class:`~grepogram.tg.AuthRequired` with the ``grepogram auth`` hint instead of a wall of
    per-chat warnings and an exit code of zero.
    """
    seeded = _seed_stored_peers(client, chats, conn, account)
    chats = [chat for chat in chats if chat.peer_id not in seeded]
    if not chats:
        return
    try:
        listed = {int(dialog.id) for dialog in await client.get_dialogs(ignore_migrated=True)}
    except errors.UnauthorizedError:
        raise
    except (errors.RPCError, ValueError) as exc:
        log.warning("could not read the dialog list to resolve %d chats: %s", len(chats), exc)
        return
    log.debug("warmed the entity cache with %d dialogs", len(listed))
    resolved = listed | seeded
    for chat in chats:
        if chat.peer_id in resolved or not chat.username:
            continue
        try:
            entity = await client.get_entity(chat.username)
        except errors.UnauthorizedError:
            raise
        except (errors.RPCError, ValueError) as exc:
            log.warning(
                "chat %s (%s): @%s, the handle it is stored under, could not be resolved, "
                "so the chat may not resolve: %s",
                chat.id,
                chat.title,
                chat.username,
                exc,
            )
            continue
        resolved.add(int(utils.get_peer_id(entity)))
    for chat in chats:
        if chat.peer_id in resolved or chat.discussion_of is None:
            continue
        try:
            await client(functions.channels.GetFullChannelRequest(chat.discussion_of))
        except errors.UnauthorizedError:
            raise
        except (errors.RPCError, ValueError) as exc:
            log.warning(
                "chat %s (%s): channel %s, which it holds the comments of, could not be read, "
                "so the group may not resolve: %s",
                chat.id,
                chat.title,
                chat.discussion_of,
                exc,
            )


def _seed_stored_peers(
    client: Any, chats: Sequence[ChatRow], conn: sqlite3.Connection, account: str
) -> set[int]:
    """Hand ``client``'s session the access hashes ``account`` has stored for ``chats`` and for
    the channels their discussion groups hang off (:func:`~grepogram.sources.seed_peers`);
    returns the peer ids now addressable."""
    wanted = [(chat.id, chat.peer_id) for chat in chats]
    wanted += [(chat.discussion_of, chat.discussion_of) for chat in chats if chat.discussion_of]
    return seed_peers(
        client, [(peer, db.access_hash(conn, row_id, account)) for row_id, peer in wanted]
    )


async def _sweep_chat(
    route: StoredPass,
    conn: sqlite3.Connection,
    cfg: Config,
    chat: ChatRow,
    budget: SyncBudget,
    tally: _PruneTally,
) -> bool:
    """One chat from its cursor on; ``True`` once the sweep has reached the end of its history.

    A page of stored ids, one request per witness, one transaction: the ids every witness
    answered empty (:func:`_confirmed_gone`) are deleted, the units holding them are cut again
    and the cursor moves to the last id of the page. The write goes to a worker thread that is
    joined even under cancellation (:func:`_joined_to_thread`), like every other write a sync
    makes.

    An answer that does not line up with the page is not an answer: the chat's turn ends with its
    cursor untouched, so the next run asks the same page again instead of taking the silence for
    a hundred deletions. ``checked`` counts the pages that *were* answered, for the same reason —
    a refused page has told the report nothing, and the next run asks about it again.
    """
    absent = [account for account in recorded_reach(conn, chat) if account not in route.clients]
    absent += [account for account in route.routes[chat.id] if account in route.stopped]
    if absent:
        who = ", ".join(sorted(set(absent)))
        log.warning(
            "chat %s (%s): account %s reaches it but cannot be asked this run; nothing removed",
            chat.id,
            chat.title,
            who,
        )
        route.warn(
            absent[0],
            f"chat {chat.id} ({chat.title}): account {who} reaches it but is not signed in or "
            "was stopped; nothing was removed, since only every account that reaches a chat "
            "can tell a deletion from history one of them cannot see",
        )
        return False
    witnesses = list(route.routes[chat.id])
    cursor = db.prune_cursor(conn, chat.id)
    while not budget.expired:
        page = db.message_ids_after(conn, chat.id, cursor, PRUNE_BATCH)
        if not page:
            db.clear_prune_cursor(conn, chat.id)
            return True
        gone = await _confirmed_gone(route, chat, page, witnesses)
        if gone is None:
            return False
        tally.checked += len(page)
        cursor = page[-1]
        tally.removed += await _joined_to_thread(
            functools.partial(_prune_batch, conn, cfg, chat, gone, cursor), budget.cancel
        )
    return False


async def _confirmed_gone(
    route: StoredPass, chat: ChatRow, page: Sequence[int], witnesses: list[str]
) -> list[int] | None:
    """The ids of ``page`` every account of ``witnesses`` answered empty, or ``None`` when that
    cannot be told this run — the chat's turn then ends with nothing removed.

    The first witness is asked about the whole page, each next one only about what the ones
    before it answered empty, so a chat one account reaches costs what it always did. A flood
    wait stops the account (:meth:`StoredPass.stop`) and a Telegram error is reported, both
    ending the turn. A shared chat Telegram refuses to one account outright
    (:data:`_REROUTE_ERRORS`) drops that account from ``witnesses`` for the rest of the chat: it
    sees nothing, so it hides nothing either — but when no witness answered at all, nothing is
    removed.
    """
    asked = list(page)
    answered = False
    refusal: Refusal | None = None
    for account in list(witnesses):
        client = route.clients[account]
        _cap_flood_sleep(client, route.sync_cfg, route.budget)
        try:
            answer = await client.get_messages(chat.peer_id, ids=asked)
        except errors.FloodWaitError as exc:
            log.warning(
                "flood wait of %ss on chat %s through account %s", exc.seconds, chat.id, account
            )
            route.stop(account, int(exc.seconds))
            return None
        except errors.UnauthorizedError as exc:
            tg.reraise_unauthorized(exc, account)
        except (errors.RPCError, ValueError) as exc:
            log.warning("chat %s (%s) through account %s: %s", chat.id, chat.title, account, exc)
            if chat.is_shared and isinstance(exc, _REROUTE_ERRORS):
                witnesses.remove(account)
                refusal = Refusal(account, str(exc), exc)
                continue
            route.warn(account, f"chat {chat.id} ({chat.title}): {exc}")
            return None
        empty = _empty_slots(asked, answer)
        if empty is None:
            log.warning(
                "chat %s (%s): Telegram's answer to account %s did not line up with the %d ids "
                "asked about; nothing was removed",
                chat.id,
                chat.title,
                account,
                len(asked),
            )
            route.warn(
                account,
                f"chat {chat.id} ({chat.title}): Telegram's answer did not line up with the "
                f"{len(asked)} ids asked about; nothing was removed",
            )
            return None
        answered = True
        asked = empty
        if not asked:
            break
    if not answered:
        if refusal is not None:
            route.warn(refusal.account, f"chat {chat.id} ({chat.title}): {refusal.reason}")
        return None
    return asked


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
    """Re-cut the post threads of the channels whose comments ``rows`` are.

    The only way from a comment to the unit quoting it, and therefore the follow-through every
    pass that changes a comment outside its own chat owes: :func:`_prune_batch` after a deletion
    and :func:`grepogram.media._recut` after an extraction. A channel's post thread carries the
    post followed by its comments while listing the post alone in ``msg_ids``, so no
    ``json_each`` over ``units.msg_ids`` reaches a comment id and neither the group's own
    invalidation nor any lookup by unit could find that thread; ``comment_of_chat_id`` /
    ``comment_of_msg_id`` on the comment's row is what names it.

    ``rows`` name the comments and nothing else: the posts are re-read from the channel's own
    rows and the threads rebuilt from the comments **stored now**, so a deleted comment is
    already gone from them (this runs after the delete) and an extracted one is already carrying
    its text. Rows that are not comments cost nothing — they name no post.
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
