"""Search units: the conversation-sized chunks that get indexed and embedded.

Single messages are too small to embed and too many to store as vectors, so search runs over
*units*: time windows of a chat, reply threads and channel posts. A unit's ``text`` is one
rendered line per message — ``[YYYY-MM-DD HH:MM] name: text`` — with a ``[photo]``-style
placeholder for media without a caption, and its ``msg_ids`` keep the mapping back to the
original messages for deep links.

The builders are functions over :class:`~grepogram.models.MessageRow` lists; only the channel
side touches the database, to read a post's comments. :func:`cut_windows` walks one
``(chat, topic)`` in chronological order and starts a new window after a pause longer than
``window_gap_min`` minutes, or once the open window holds ``window_max_msgs`` messages or
``window_max_chars`` characters of rendered text; forum chats are split into topics first with
:func:`group_by_topic`. :func:`build_threads` follows ``reply_to_msg_id`` from every root (a
replied-to message with no parent in the chat) and caps a thread at ``thread_max_msgs``
messages, continuing in further units that repeat the root. :func:`build_posts` makes one
``post`` per channel message and, when comments are synced, a ``thread`` of the post with its
comments read from the linked discussion chat. :func:`units_for_chat` picks the builders for a
chat's kind.

:func:`rebuild_for_chat` keeps the stored units in step with a sync: it re-cuts the open window
of every touched ``(chat, topic)``, rebuilds the reply threads reachable from the changed
messages and the post units of changed channel posts, and reports the inserted and deleted unit
ids as a :class:`UnitDelta` for the indexer.
"""

import datetime as dt
import sqlite3
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from grepogram import db
from grepogram.models import ChatRow, Config, MessageRow, UnitKind, UnitRow, UnitsCfg

UNKNOWN_SENDER = "unknown"
EMPTY_PLACEHOLDER = "[empty]"
STAMP_FORMAT = "%Y-%m-%d %H:%M"


# --- rendering -------------------------------------------------------------------------------


def render_line(msg: MessageRow) -> str:
    """``[YYYY-MM-DD HH:MM] name: text`` for one message; the stamp is UTC.

    Media without a caption renders as ``[photo]`` / ``[voice]`` / ``[document: name.pdf]`` so
    the line still tells what was posted; a message with neither text nor media renders as
    ``[empty]``.
    """
    stamp = dt.datetime.fromtimestamp(msg.date, tz=dt.UTC).strftime(STAMP_FORMAT)
    text = msg.text.strip() or media_placeholder(msg)
    return f"[{stamp}] {sender_name(msg)}: {text}"


def sender_name(msg: MessageRow) -> str:
    if msg.from_name:
        return msg.from_name
    if msg.from_id is not None:
        return f"id{msg.from_id}"
    return UNKNOWN_SENDER


def media_placeholder(msg: MessageRow) -> str:
    if msg.media_kind is None:
        return EMPTY_PLACEHOLDER
    if msg.media_filename:
        return f"[{msg.media_kind}: {msg.media_filename}]"
    return f"[{msg.media_kind}]"


def chronological(messages: Iterable[MessageRow]) -> list[MessageRow]:
    """Messages ordered by ``(date, msg_id)`` — the order every unit builder works in."""
    return sorted(messages, key=lambda msg: (msg.date, msg.msg_id))


def build_unit(
    kind: UnitKind,
    messages: Sequence[MessageRow],
    chat_id: int,
    topic_id: int | None = None,
) -> UnitRow:
    """One unit over ``messages`` (already in the order they should be rendered)."""
    if not messages:
        raise ValueError("a unit needs at least one message")
    return _unit(kind, messages, [render_line(msg) for msg in messages], chat_id, topic_id)


def _unit(
    kind: UnitKind,
    messages: Sequence[MessageRow],
    lines: Sequence[str],
    chat_id: int,
    topic_id: int | None,
) -> UnitRow:
    msg_ids = [msg.msg_id for msg in messages]
    return UnitRow(
        chat_id=chat_id,
        topic_id=topic_id,
        kind=kind,
        msg_id_start=min(msg_ids),
        msg_id_end=max(msg_ids),
        msg_ids=msg_ids,
        date_start=min(msg.date for msg in messages),
        date_end=max(msg.date for msg in messages),
        text="\n".join(lines),
    )


# --- windows ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _OpenWindow:
    messages: list[MessageRow] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    chars: int = 0

    def must_cut_before(self, msg: MessageRow, cfg: UnitsCfg) -> bool:
        if not self.messages:
            return False
        return (
            msg.date - self.messages[-1].date > cfg.window_gap_min * 60
            or len(self.messages) >= cfg.window_max_msgs
            or self.chars >= cfg.window_max_chars
        )

    def add(self, msg: MessageRow) -> None:
        line = render_line(msg)
        if self.lines:
            self.chars += 1
        self.chars += len(line)
        self.messages.append(msg)
        self.lines.append(line)

    def close(self, chat_id: int, topic_id: int | None) -> UnitRow:
        return _unit("window", self.messages, self.lines, chat_id, topic_id)


def cut_windows(
    messages: Iterable[MessageRow],
    cfg: UnitsCfg,
    chat_id: int,
    topic_id: int | None = None,
) -> list[UnitRow]:
    """Cut the messages of one ``(chat, topic)`` into ``window`` units.

    The input is sorted chronologically first, so the result does not depend on its order. A
    window closes before a message that arrives more than ``window_gap_min`` minutes after the
    previous one, or when it already holds ``window_max_msgs`` messages or ``window_max_chars``
    characters of rendered text; a single oversized message therefore forms a window of its own.
    """
    windows: list[UnitRow] = []
    window = _OpenWindow()
    for msg in chronological(messages):
        if window.must_cut_before(msg, cfg):
            windows.append(window.close(chat_id, topic_id))
            window = _OpenWindow()
        window.add(msg)
    if window.messages:
        windows.append(window.close(chat_id, topic_id))
    return windows


def group_by_topic(messages: Iterable[MessageRow]) -> dict[int | None, list[MessageRow]]:
    """Messages bucketed by ``topic_id`` (all under ``None`` outside forums), first seen first."""
    groups: dict[int | None, list[MessageRow]] = {}
    for msg in messages:
        groups.setdefault(msg.topic_id, []).append(msg)
    return groups


# --- threads ---------------------------------------------------------------------------------


class _ReplyIndex:
    """Reply graph of one chat: which messages are present and who replied to whom."""

    def __init__(self, messages: Iterable[MessageRow]) -> None:
        self.by_id = {msg.msg_id: msg for msg in messages}
        self.children: dict[int, list[MessageRow]] = {}
        for msg in self.by_id.values():
            parent = self.parent_of(msg)
            if parent is not None:
                self.children.setdefault(parent, []).append(msg)

    def parent_of(self, msg: MessageRow) -> int | None:
        """The message ``msg`` replies to, when that message is in the chat and not itself."""
        parent = msg.reply_to_msg_id
        if parent is None or parent == msg.msg_id or parent not in self.by_id:
            return None
        return parent

    def is_root(self, msg: MessageRow) -> bool:
        return msg.msg_id in self.children and self.parent_of(msg) is None

    def roots(self) -> list[MessageRow]:
        """Messages that head a thread — replied to, with no parent in the chat — in date order."""
        return [msg for msg in chronological(self.by_id.values()) if self.is_root(msg)]

    def descendants(self, root_id: int) -> list[MessageRow]:
        """Every reply below ``root_id`` (breadth-first), returned in chronological order."""
        seen = {root_id}
        queue = deque([root_id])
        found: list[MessageRow] = []
        while queue:
            for child in self.children.get(queue.popleft(), ()):
                if child.msg_id not in seen:
                    seen.add(child.msg_id)
                    found.append(child)
                    queue.append(child.msg_id)
        return chronological(found)


def thread_chunks(
    root: MessageRow, replies: Sequence[MessageRow], cap: int
) -> Iterator[list[MessageRow]]:
    """``[root, *replies]`` split into pieces of at most ``cap`` messages, root repeated in each.

    A root without replies yields nothing: a thread needs at least one reply.
    """
    step = max(cap - 1, 1)
    for start in range(0, len(replies), step):
        yield [root, *replies[start : start + step]]


def build_threads(
    messages: Iterable[MessageRow],
    cfg: UnitsCfg,
    chat_id: int,
    roots: Iterable[int] | None = None,
) -> list[UnitRow]:
    """Cut the reply chains of one chat into ``thread`` units.

    A thread starts at a root — a message with at least one reply whose own parent is not among
    ``messages`` (a reply to a deleted or unfetched message counts as a root) — and holds the root
    followed by every descendant reachable over ``reply_to_msg_id``, chronologically. A thread
    longer than ``thread_max_msgs`` continues in further units that repeat the root as their
    first message, so ``msg_ids[0]`` names the root in every piece; the unit carries the root's
    ``topic_id``. ``roots`` limits the result to the threads headed by those message ids.
    """
    index = _ReplyIndex(messages)
    wanted = None if roots is None else set(roots)
    units: list[UnitRow] = []
    for root in index.roots():
        if wanted is not None and root.msg_id not in wanted:
            continue
        for chunk in thread_chunks(root, index.descendants(root.msg_id), cfg.thread_max_msgs):
            units.append(build_unit("thread", chunk, chat_id, root.topic_id))
    return units


# --- posts -----------------------------------------------------------------------------------


def build_posts(
    conn: sqlite3.Connection,
    messages: Iterable[MessageRow],
    chat: ChatRow,
    comments: bool,
    cfg: UnitsCfg,
) -> list[UnitRow]:
    """One ``post`` unit per channel message, plus a ``thread`` for every post with comments.

    Comments are stored under the linked discussion chat (``chats.discussion_of = chat.id``)
    with ``topic_id`` = the post id and are read from there when ``comments`` is set. The thread
    belongs to the channel and lists only the post in ``msg_ids`` — comment ids live in the
    discussion chat's id space and would not open from a channel link — while its text carries
    the post followed by its comments in order and ``date_end`` reaches the last comment. Long
    comment threads are split at ``thread_max_msgs`` messages like reply threads, each piece
    repeating the post.
    """
    posts = chronological(messages)
    units = [build_unit("post", [post], chat.id) for post in posts]
    discussion = db.get_discussion_chat(conn, chat.id) if comments else None
    if discussion is None or not posts:
        return units
    by_post = db.get_topic_messages(conn, discussion.id, [post.msg_id for post in posts])
    for post in posts:
        replies = chronological(by_post.get(post.msg_id, []))
        for chunk in thread_chunks(post, replies, cfg.thread_max_msgs):
            units.append(_post_thread(post, chunk, chat.id))
    return units


def _post_thread(post: MessageRow, chunk: Sequence[MessageRow], chat_id: int) -> UnitRow:
    return UnitRow(
        chat_id=chat_id,
        kind="thread",
        msg_id_start=post.msg_id,
        msg_id_end=post.msg_id,
        msg_ids=[post.msg_id],
        date_start=post.date,
        date_end=max(msg.date for msg in chunk),
        text="\n".join(render_line(msg) for msg in chunk),
    )


# --- per chat --------------------------------------------------------------------------------


def comments_enabled(cfg: Config, chat: ChatRow) -> bool:
    """Whether the source that pulled ``chat`` in asks for channel comments."""
    return any(source.id == chat.source_id and source.comments for source in cfg.sources)


def units_for_chat(
    conn: sqlite3.Connection, messages: Iterable[MessageRow], chat: ChatRow, cfg: Config
) -> list[UnitRow]:
    """Every unit of ``chat`` over ``messages``, chosen by the chat's kind.

    Channels get posts, plus post threads when their source has ``comments`` on. Everything
    else — private chats, groups, forums and the discussion chats of channels alike — gets
    windows per topic followed by reply threads.
    """
    if chat.type == "channel" and chat.discussion_of is None:
        return build_posts(conn, messages, chat, comments_enabled(cfg, chat), cfg.units)
    messages = list(messages)
    windows = [
        window
        for topic_id, group in group_by_topic(messages).items()
        for window in cut_windows(group, cfg.units, chat.id, topic_id)
    ]
    return windows + build_threads(messages, cfg.units, chat.id)


# --- incremental maintenance -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitDelta:
    """Unit ids a rebuild inserted and deleted; the indexer keeps FTS and vectors in step."""

    inserted_ids: list[int] = field(default_factory=list)
    deleted_ids: list[int] = field(default_factory=list)


def rebuild_for_chat(
    conn: sqlite3.Connection, chat: ChatRow, cfg: Config, new_msg_ids: Iterable[int]
) -> UnitDelta:
    """Bring the units of ``chat`` up to date after the rows with these ``messages.id`` changed.

    ``new_msg_ids`` are what a sync reports: inserted and edited rows alike. Windows: for every
    touched ``(chat, topic)`` the open window — the last one — is deleted and re-cut from its
    ``msg_id_start`` over everything stored since, so a message that continues it joins it and
    one after a long pause starts the next. Closed windows are never re-cut: an edit to a
    message inside one does not reach that window's text (a v1 limitation), though it still
    rebuilds the reply thread the message belongs to. Threads: every thread reachable from a
    changed message — walking up ``reply_to_msg_id`` to the root — is rebuilt with all its
    replies. Channels: the ``post`` units (and post threads, when comments are on) of the changed
    posts are rebuilt. Units whose content did not change keep their row and embedding; the
    rest are deleted and re-inserted dirty. Everything happens in one transaction.
    """
    changed = [m for m in db.get_messages_by_ids(conn, new_msg_ids) if m.chat_id == chat.id]
    if not changed:
        return UnitDelta()
    with db.transaction(conn):
        if chat.type == "channel" and chat.discussion_of is None:
            return _rebuild_posts(conn, chat, cfg, changed)
        return _rebuild_conversation(conn, chat, cfg, changed)


def _rebuild_conversation(
    conn: sqlite3.Connection, chat: ChatRow, cfg: Config, changed: list[MessageRow]
) -> UnitDelta:
    stale: list[UnitRow] = []
    fresh: list[UnitRow] = []
    for topic_id, group in group_by_topic(changed).items():
        old, new = _recut_open_window(conn, chat, cfg.units, topic_id, group)
        stale += old
        fresh += new
    old, new = _rebuild_threads(conn, chat, cfg.units, changed)
    return _apply(conn, stale + old, fresh + new)


def _rebuild_posts(
    conn: sqlite3.Connection, chat: ChatRow, cfg: Config, changed: list[MessageRow]
) -> UnitDelta:
    post_ids = [post.msg_id for post in changed]
    stale = db.post_units(conn, chat.id, post_ids) + db.threads_touching(conn, chat.id, post_ids)
    fresh = build_posts(conn, changed, chat, comments_enabled(cfg, chat), cfg.units)
    return _apply(conn, stale, fresh)


def _recut_open_window(
    conn: sqlite3.Connection,
    chat: ChatRow,
    cfg: UnitsCfg,
    topic_id: int | None,
    changed: Sequence[MessageRow],
) -> tuple[list[UnitRow], list[UnitRow]]:
    """The open window of ``(chat, topic)`` and its replacements, cut over the messages since
    its start; nothing when every changed message sits inside a closed window."""
    window = db.open_window(conn, chat.id, topic_id)
    start = None if window is None else window.msg_id_start
    if start is not None and all(msg.msg_id < start for msg in changed):
        return [], []
    messages = db.get_messages_in_topic(conn, chat.id, topic_id, since_msg_id=start)
    stale = [] if window is None else [window]
    return stale, cut_windows(messages, cfg, chat.id, topic_id)


def _rebuild_threads(
    conn: sqlite3.Connection, chat: ChatRow, cfg: UnitsCfg, changed: Sequence[MessageRow]
) -> tuple[list[UnitRow], list[UnitRow]]:
    """The thread units the changed messages belong to and their rebuilt replacements."""
    threads = _thread_members(conn, chat.id, changed)
    touched = [msg.msg_id for msg in changed] + list(threads)
    stale = db.threads_touching(conn, chat.id, touched)
    fresh = [
        unit
        for root_id, members in threads.items()
        for unit in build_threads(members, cfg, chat.id, roots=[root_id])
    ]
    return stale, fresh


def _thread_members(
    conn: sqlite3.Connection, chat_id: int, changed: Sequence[MessageRow]
) -> dict[int, list[MessageRow]]:
    """Root id → root and every reply below it, for each thread a changed message belongs to."""
    threads: dict[int, list[MessageRow]] = {}
    for top in _chain_tops(conn, chat_id, changed):
        replies = db.get_descendants(conn, chat_id, top.msg_id)
        if replies:
            threads[top.msg_id] = [top, *replies]
    return threads


def _chain_tops(
    conn: sqlite3.Connection, chat_id: int, changed: Sequence[MessageRow]
) -> list[MessageRow]:
    """Where each changed message's reply chain ends: the ancestor whose parent is not stored.

    Chains climb level by level in batched lookups, with the same parent rule as
    :class:`_ReplyIndex` (a reply to itself or to an unstored message has no parent). A chain
    that reaches a message another chain already passed stops there — the first chain carries
    on to the shared top — and a reply cycle therefore yields no top at all.
    """
    visited = {msg.msg_id for msg in changed}
    frontier = list(changed)
    tops: list[MessageRow] = []
    while frontier:
        wanted = {p for msg in frontier if (p := _parent_id(msg)) is not None}
        parents = db.get_messages_by_msg_id(conn, chat_id, wanted)
        climbing: list[MessageRow] = []
        for msg in frontier:
            parent_id = _parent_id(msg)
            parent = None if parent_id is None else parents.get(parent_id)
            if parent is None:
                tops.append(msg)
            elif parent.msg_id not in visited:
                visited.add(parent.msg_id)
                climbing.append(parent)
        frontier = climbing
    return tops


def _parent_id(msg: MessageRow) -> int | None:
    parent = msg.reply_to_msg_id
    return None if parent is None or parent == msg.msg_id else parent


def _apply(
    conn: sqlite3.Connection, stale: Sequence[UnitRow], fresh: Sequence[UnitRow]
) -> UnitDelta:
    """Replace ``stale`` with ``fresh`` in the database, keeping rows whose content is unchanged.

    A rebuilt unit identical to a stored one (same kind, topic, messages, dates and text — a
    reaction count changing on a message inside it, say) keeps its id and its embedding.
    """
    kept: dict[tuple[object, ...], UnitRow] = {}
    for unit in {unit.id: unit for unit in stale}.values():
        kept.setdefault(_content_key(unit), unit)
    to_insert: list[UnitRow] = []
    for unit in fresh:
        if kept.pop(_content_key(unit), None) is None:
            to_insert.append(unit)
    deleted = [unit.id for unit in kept.values() if unit.id is not None]
    inserted = db.insert_units(conn, to_insert)
    db.delete_units(conn, deleted)
    return UnitDelta(inserted_ids=inserted, deleted_ids=deleted)


def _content_key(unit: UnitRow) -> tuple[object, ...]:
    return (
        unit.kind,
        unit.topic_id,
        tuple(unit.msg_ids),
        unit.date_start,
        unit.date_end,
        unit.text,
    )
