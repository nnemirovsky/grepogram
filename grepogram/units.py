"""Search units: the conversation-sized chunks that get indexed and embedded.

Single messages are too small to embed and too many to store as vectors, so search runs over
*units*: time windows of a chat, reply threads and channel posts. A unit's ``text`` is one
rendered line per message — ``[YYYY-MM-DD HH:MM] name: text`` — with a ``[photo]``-style marker
for what a message attached and whatever an extractor read off it after that marker, and its
``msg_ids`` keep the mapping back to the original messages for deep links. ``reactions`` sums
what those messages collected — a ranking signal, refreshed in place by
:func:`grepogram.db.refresh_unit_reactions` because nothing about it is part of a unit's content.

The builders are functions over :class:`~grepogram.models.MessageRow` lists; only the channel
side touches the database, to read a post's comments. :func:`cut_windows` walks one
``(chat, topic)`` in chronological order and starts a new window after a pause longer than
``window_gap_min`` minutes, once the open window holds ``window_max_msgs`` messages, or before a
message that would take its rendered text past ``window_max_chars`` — a ceiling, not a floor;
forum chats are split into topics first with :func:`group_by_topic`. :func:`build_threads`
follows ``reply_to_msg_id`` from every root (a replied-to message with no parent in the chat)
and caps a thread at ``thread_max_msgs`` messages, continuing in further units that repeat the
root. :func:`build_posts` makes one ``post`` per channel message and, when comments are synced,
a ``thread`` of the post with its comments read from the linked discussion chat.
:func:`units_for_chat` picks the builders for a chat's kind.

:data:`RECIPE_VERSION` names how this build cuts and renders units, and :func:`recut_chat` cuts a
whole chat again when the two disagree — the only thing that reaches a closed window.

:func:`invalidate_units_for` is the other one: it re-cuts the units holding a handful of named
messages whatever window they sit in, which is what makes an extractor's text — and a message
deleted in Telegram (:func:`grepogram.sync._drop_deleted`) — reach the history a sync's
incremental rebuild will never touch again.

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

RECIPE_VERSION = 4
"""How this build cuts and renders units, recorded as ``meta.unit_recipe``.

Bumped by every change that would make a stored unit differ from what this code cuts today: a
window boundary rule, what :func:`render_line` puts on a line, a column a unit carries. The
stored units cannot notice such a change on their own — an incremental rebuild never re-cuts a
closed window (:func:`_recut_start`), and closed windows are almost all of a chat's history — so
the version is what says a re-cut is owed.

A bump is not free: it re-cuts and re-embeds every chat in the index, about an hour on 47k units.
Everything one release changes about units therefore shares a single bump, and the re-cut runs
chat by chat inside a budget (:func:`grepogram.sync.recut_pending_chats`), never as a global
delete-all. A database built from empty is stamped at this version by
:func:`grepogram.db.migrate`, so a fresh install never re-cuts what it has just cut correctly."""


# --- rendering -------------------------------------------------------------------------------


def render_line(msg: MessageRow) -> str:
    """``[YYYY-MM-DD HH:MM] name: text`` for one message; the stamp is UTC.

    Media renders as ``[photo]`` / ``[voice]`` / ``[document: name.pdf]`` so the line still tells
    what was posted, and what an extractor read off that media follows the marker — a
    photographed announcement becomes ``[photo] ОТКРЫТО с 9:00`` and a contract
    ``[document: contract.pdf] …``. The marker stays whatever was read: a reader has to be able
    to tell that a machine took those words off an image rather than someone typing them, and a
    caption keeps its own text in front of it.

    Nothing changes for media no text was read from — the caption alone where there is one, the
    bare placeholder where there is not — and a message with neither text nor media still renders
    as ``[empty]``.
    """
    stamp = dt.datetime.fromtimestamp(msg.date, tz=dt.UTC).strftime(STAMP_FORMAT)
    return f"[{stamp}] {sender_name(msg)}: {message_body(msg)}"


def message_body(msg: MessageRow) -> str:
    """What one message says on its line: its caption, its media marker and the text read off it.

    The marker joins a caption only when something was read off the media — a captioned photo
    nothing was extracted from renders exactly as it always has, which is what keeps the change
    to what extraction actually adds.
    """
    caption = msg.text.strip()
    read = extracted_line(msg) if msg.media_kind is not None else ""
    if read:
        return f"{caption} {media_placeholder(msg)} {read}".lstrip()
    return caption or media_placeholder(msg)


def extracted_line(msg: MessageRow) -> str:
    """``msg.extracted_text`` folded onto one line; ``""`` when nothing was read off the media.

    :func:`grepogram.extract._capped` keeps a form's or a price list's own line breaks and says
    that rendering them is the renderer's decision. This is that decision: a unit is one line per
    message, ``msg_ids`` maps back position by position, and a 400-page PDF's newlines would turn
    one message into hundreds of lines nothing could map.
    """
    return " ".join((msg.extracted_text or "").split())


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
    """One unit over ``messages``, its ``reactions`` the sum of what they collected.

    The sum is over the very messages ``msg_ids`` lists, which is what lets
    :func:`grepogram.db.refresh_unit_reactions` recompute the same number in place later: a
    reaction arrives long after the unit was cut, and no rebuild reaches a closed window.
    """
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
        reactions=sum(msg.reactions_total for msg in messages),
    )


# --- windows ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _OpenWindow:
    messages: list[MessageRow] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    chars: int = 0

    def must_cut_before(self, msg: MessageRow, line: str, cfg: UnitsCfg) -> bool:
        """Whether this window must close before ``line`` (``msg`` rendered) is appended.

        The character limit is a ceiling on the finished text, so it is tested against the
        length the window *would* have: ``line`` and the newline joining it to what is already
        there. An empty window never cuts — that is what lets a single message longer than
        ``window_max_chars`` form a window of its own instead of no window at all.
        """
        if not self.messages:
            return False
        return (
            msg.date - self.messages[-1].date > cfg.window_gap_min * 60
            or len(self.messages) >= cfg.window_max_msgs
            or self.chars + 1 + len(line) > cfg.window_max_chars
        )

    def add(self, msg: MessageRow, line: str) -> None:
        """Append ``msg``, already rendered as ``line`` by the caller — rendered once, not twice."""
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
    previous one, when it already holds ``window_max_msgs`` messages, or when appending that
    message *would* take its rendered text past ``window_max_chars``. The character limit is
    therefore a ceiling: a finished window never exceeds it, and the one exception is a single
    message longer than the whole budget, which forms a window of its own rather than none.
    """
    windows: list[UnitRow] = []
    window = _OpenWindow()
    for msg in chronological(messages):
        line = render_line(msg)
        if window.must_cut_before(msg, line, cfg):
            windows.append(window.close(chat_id, topic_id))
            window = _OpenWindow()
        window.add(msg, line)
    if window.messages:
        windows.append(window.close(chat_id, topic_id))
    return windows


def window_topic(chat: ChatRow, msg: MessageRow) -> int | None:
    """The topic whose windows hold ``msg``: its ``topic_id`` in a forum, ``None`` anywhere else.

    Outside forums a chat is one linear conversation and its windows carry no topic — and
    ``msg.topic_id`` is deliberately ignored there, because Telegram sets it outside forums as
    well: a legacy message thread puts its root in ``reply_to.reply_to_top_id``, so an ordinary
    supergroup carries one on a minority of its rows. Windowing such a chat by that id would cut
    its history into fragments nobody reads as separate. A comment a channel stores in its
    discussion group names its post in ``comment_of_msg_id``, never in ``topic_id``, so it sits
    in the group's windows exactly where its own forum topic — or the absence of one — puts it,
    like any other message the group holds.
    """
    return msg.topic_id if chat.is_forum else None


def group_by_topic(
    chat: ChatRow, messages: Iterable[MessageRow]
) -> dict[int | None, list[MessageRow]]:
    """Messages bucketed by :func:`window_topic`, first seen first."""
    groups: dict[int | None, list[MessageRow]] = {}
    for msg in messages:
        groups.setdefault(window_topic(chat, msg), []).append(msg)
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
    naming this channel and the post in ``comment_of_chat_id`` / ``comment_of_msg_id``, and are
    read back by that pair when ``comments`` is set — never by the post id alone, which is a bare
    number that a forum topic of the same group, or a comment left over from a channel that held
    the group before, would answer to just as well. The thread
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
    by_post = db.get_comment_messages(conn, discussion.id, chat.id, [post.msg_id for post in posts])
    for post in posts:
        replies = chronological(by_post.get(post.msg_id, []))
        for chunk in thread_chunks(post, replies, cfg.thread_max_msgs):
            units.append(_post_thread(post, chunk, chat.id))
    return units


def _post_thread(post: MessageRow, chunk: Sequence[MessageRow], chat_id: int) -> UnitRow:
    """One piece of a post's comment thread: the post's ``msg_ids``, the chunk's text.

    ``reactions`` is the post's own total and not the chunk's, because ``msg_ids`` lists the post
    alone — comment ids belong to the discussion group's id space and no ``json_each`` over
    ``units.msg_ids`` reaches them, so :func:`grepogram.db.refresh_unit_reactions` can only ever
    recompute the post's. The comments' own reactions are carried by the discussion group's
    window units, which hold those messages. This is built by hand rather than through
    :func:`_unit` — the text spans the comments while the ids do not — so the sum has to be
    written out here or a channel's most-reacted threads would all sit at zero.
    """
    return UnitRow(
        chat_id=chat_id,
        kind="thread",
        msg_id_start=post.msg_id,
        msg_id_end=post.msg_id,
        msg_ids=[post.msg_id],
        date_start=post.date,
        date_end=max(msg.date for msg in chunk),
        text="\n".join(render_line(msg) for msg in chunk),
        reactions=post.reactions_total,
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
    else — private chats, groups, forums and the discussion groups of channels alike — gets
    windows (per topic in a forum, one linear run otherwise) followed by reply threads.
    """
    if chat.is_broadcast:
        return build_posts(conn, messages, chat, comments_enabled(cfg, chat), cfg.units)
    messages = list(messages)
    windows = [
        window
        for topic_id, group in group_by_topic(chat, messages).items()
        for window in cut_windows(group, cfg.units, chat.id, topic_id)
    ]
    return windows + build_threads(messages, cfg.units, chat.id)


# --- incremental maintenance -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitDelta:
    """Unit ids a rebuild inserted and deleted; the indexer keeps FTS and vectors in step."""

    inserted_ids: list[int] = field(default_factory=list)
    deleted_ids: list[int] = field(default_factory=list)


def recut_chat(conn: sqlite3.Connection, chat: ChatRow, cfg: Config) -> UnitDelta:
    """Cut every unit of ``chat`` again from its stored messages, replacing what is there.

    What a :data:`RECIPE_VERSION` bump runs. An incremental rebuild cannot do it: closed windows
    are never re-cut for a change inside them (:func:`_recut_start` returns ``None``), and they
    hold nearly all of a chat's history.

    It goes through :func:`units_for_chat`, not :func:`rebuild_for_chat`. After the delete-all
    every stale lookup a rebuild makes is empty by construction — :func:`grepogram.db.open_window`
    is ``None``, ``post_units`` and ``threads_touching`` return nothing — so a rebuild reaches
    the same units the long way, loading the chat twice and walking one ``get_descendants`` per
    reply chain, and its :func:`_apply` reports ``deleted_ids = []``, which is not the delta the
    indexer has to act on.

    No ``messages`` row is written. Unit boundaries change here and message text does not, so
    ``msg_fts`` needs no rewrite and ``messages.indexed`` needs no flagging: flagging inside the
    caller's transaction recovers nothing (it rolls back with everything else) while writing the
    whole of ``messages``, and flagging outside one hands the next run a whole-index backlog that
    :func:`grepogram.sync._sync_chats`'s unbudgeted deferred pass would drain in full.

    The delete and the insert are one transaction; the caller indexes the returned delta, since
    :mod:`grepogram.units` cannot import :mod:`grepogram.index` (it imports :class:`UnitDelta`
    from here).
    """
    stale = [unit.id for unit in db.get_units(conn, chat.id) if unit.id is not None]
    with db.transaction(conn):
        db.delete_units(conn, stale)
        fresh = units_for_chat(conn, db.get_messages(conn, chat.id), chat, cfg)
        inserted = db.insert_units(conn, fresh)
    return UnitDelta(inserted_ids=inserted, deleted_ids=stale)


def invalidate_units_for(
    conn: sqlite3.Connection, chat: ChatRow, cfg: Config, rows: Sequence[MessageRow]
) -> UnitDelta:
    """Cut every unit holding one of ``rows`` again, closed windows included.

    The primitive for a change that happened *outside* a sync: an extractor reading text off a
    photo posted two years ago, or a message that has been deleted. A sync's incremental rebuild
    cannot serve either — :func:`_recut_start` returns ``None`` when every changed message sits
    in a closed window, and closed windows are all but the last of a chat's history — so
    ``messages.indexed = 0`` alone would clear on the next ``mark_indexed`` with nothing rebuilt
    and the change would be lost in silence.

    ``rows`` are :class:`~grepogram.models.MessageRow` and not ids because the topic a window is
    scoped by has to come off the row, and the caller that deletes messages no longer has the row
    to read by the time this runs. **They supply nothing else.** Every message this renders is
    re-read from the database: :func:`_chain_tops` would make a passed row a thread top when its
    parent is unstored and :func:`build_threads` would render it at the head, and
    :func:`_rebuild_posts` renders what it is handed — so rendering from ``rows`` would write a
    deleted message straight back into a fresh unit, and would re-render an extracted photo with
    the stale ``[photo]`` its caller read before the extraction.

    The topic is :func:`window_topic`, never ``msg.topic_id``: windows outside a forum carry
    ``topic_id = NULL`` while Telegram populates the column for legacy threads there anyway, and
    ``db``'s window scope matches it with ``topic_id IS ?``, so the raw value would find no
    window and this would quietly do nothing on exactly the chats it exists for.

    Call it once per chat per batch. :func:`grepogram.db.windows_from` returns every window from
    the earliest touched one to the end of the chat, so a call per message would re-cut and
    re-embed the same tail once per message. Units whose content did not change keep their row
    and their embedding (:func:`_apply`), and a stale thread whose root is no longer stored is
    dropped rather than rebuilt. Everything is one transaction, and the caller hands the returned
    delta to :func:`grepogram.index.index_units` — this module cannot import the indexer, which
    imports :class:`UnitDelta` from here.
    """
    if not rows:
        return UnitDelta()
    with db.transaction(conn):
        if chat.is_broadcast:
            return _invalidate_posts(conn, chat, cfg, rows)
        return _invalidate_conversation(conn, chat, cfg, rows)


def _invalidate_conversation(
    conn: sqlite3.Connection, chat: ChatRow, cfg: Config, rows: Sequence[MessageRow]
) -> UnitDelta:
    stale: list[UnitRow] = []
    fresh: list[UnitRow] = []
    for topic_id, group in group_by_topic(chat, rows).items():
        start = _invalidation_start(conn, chat.id, topic_id, group)
        if start is None:
            continue
        stale += db.windows_from(conn, chat.id, topic_id, start)
        fresh += cut_windows(
            _stored_since(conn, chat, topic_id, start), cfg.units, chat.id, topic_id
        )
    old, new = _invalidate_threads(conn, chat, cfg.units, rows)
    return _apply(conn, stale + old, fresh + new)


def _invalidation_start(
    conn: sqlite3.Connection, chat_id: int, topic_id: int | None, rows: Sequence[MessageRow]
) -> int | None:
    """Where the re-cut of ``(chat, topic)`` begins; ``None`` when the topic holds no window yet.

    The earliest start among the windows holding these messages — a closed window as readily as
    the open one, which is the whole point. A message no window's range covers takes the start of
    the window before it, or its own id when none precedes it, exactly as :func:`_recut_start`
    treats one that arrived below the open window.

    A topic with no windows at all is left to the next rebuild: there is nothing stale to replace
    and cutting only the tail from here would leave the messages before it in no window.
    """
    if db.open_window(conn, chat_id, topic_id) is None:
        return None
    starts: list[int] = []
    for msg in rows:
        unit = db.containing_unit(conn, chat_id, msg.msg_id, topic_id)
        if unit is not None and unit.kind == "window":
            starts.append(unit.msg_id_start)
            continue
        before = db.window_before(conn, chat_id, topic_id, msg.msg_id)
        starts.append(msg.msg_id if before is None else before.msg_id_start)
    return min(starts) if starts else None


def _invalidate_threads(
    conn: sqlite3.Connection, chat: ChatRow, cfg: UnitsCfg, rows: Sequence[MessageRow]
) -> tuple[list[UnitRow], list[UnitRow]]:
    """The thread units holding these messages and their replacements, re-read from the database.

    A thread is found through the units that quote it and rebuilt from its root down —
    ``msg_ids[0]`` names the root in every chunk (:func:`thread_chunks`) — so the root is looked
    up rather than taken from ``rows``, and a thread whose root is gone yields no replacement at
    all. The members of the rebuilt threads join the ids the stale lookup runs over, because a
    thread longer than ``thread_max_msgs`` spans several units and only one of them holds the
    message this started from.
    """
    touched = [msg.msg_id for msg in rows]
    quoting = db.threads_touching(conn, chat.id, touched)
    roots = {unit.msg_ids[0] for unit in quoting if unit.msg_ids}
    if not roots:
        return [], []
    stored = db.get_messages_by_msg_id(conn, chat.id, roots)
    fresh: list[UnitRow] = []
    members: list[int] = []
    for root_id in sorted(roots):
        root = stored.get(root_id)
        if root is None:
            continue
        replies = db.get_descendants(conn, chat.id, root_id)
        if not replies:
            continue
        members += [root_id, *(reply.msg_id for reply in replies)]
        fresh += build_threads([root, *replies], cfg, chat.id, roots=[root_id])
    return db.threads_touching(conn, chat.id, [*touched, *members]), fresh


def _invalidate_posts(
    conn: sqlite3.Connection, chat: ChatRow, cfg: Config, rows: Sequence[MessageRow]
) -> UnitDelta:
    """A channel's ``post`` units and the post threads that quote them, both re-read and re-cut.

    A post thread carries the post followed by its comments, so it holds the post's own rendered
    line and goes stale with it; ``db.containing_unit`` answers with the post unit alone, which
    is why :func:`db.threads_touching` runs here as well. A comment's own text is another matter
    — no ``json_each`` over ``units.msg_ids`` reaches a comment id, so a comment that changes is
    followed through ``comment_of_chat_id`` / ``comment_of_msg_id`` and not from here.
    """
    post_ids = sorted({msg.msg_id for msg in rows})
    stale = db.post_units(conn, chat.id, post_ids) + db.threads_touching(conn, chat.id, post_ids)
    stored = db.get_messages_by_msg_id(conn, chat.id, post_ids)
    posts = [stored[post_id] for post_id in post_ids if post_id in stored]
    fresh = build_posts(conn, posts, chat, comments_enabled(cfg, chat), cfg.units) if posts else []
    return _apply(conn, stale, fresh)


def rebuild_for_chat(
    conn: sqlite3.Connection, chat: ChatRow, cfg: Config, new_msg_ids: Iterable[int]
) -> UnitDelta:
    """Bring the units of ``chat`` up to date after the rows with these ``messages.id`` changed.

    ``new_msg_ids`` are what a sync reports: inserted and edited rows alike. Windows: for every
    touched ``(chat, topic)`` the open window — the last one — is deleted and re-cut from its
    ``msg_id_start`` over everything stored since, so a message that continues it joins it and
    one after a long pause starts the next; a changed message that arrived below the open window
    and that no window holds yet (comments a channel stored ahead of its group's history, a late
    comment on an old post) re-cuts from the window before it instead (:func:`_recut_windows`).
    Closed windows are never re-cut for an edit: a changed text inside one does not reach that
    window (a v1 limitation), though it still rebuilds the reply thread the message belongs to.
    Threads: every thread reachable from a
    changed message — walking up ``reply_to_msg_id`` to the root — is rebuilt with all its
    replies. Channels: the ``post`` units (and post threads, when comments are on) of the changed
    posts are rebuilt. Units whose content did not change keep their row and embedding; the
    rest are deleted and re-inserted dirty. Everything happens in one transaction.
    """
    changed = [m for m in db.get_messages_by_ids(conn, new_msg_ids) if m.chat_id == chat.id]
    if not changed:
        return UnitDelta()
    with db.transaction(conn):
        if chat.is_broadcast:
            return _rebuild_posts(conn, chat, cfg, changed)
        return _rebuild_conversation(conn, chat, cfg, changed)


def _rebuild_conversation(
    conn: sqlite3.Connection, chat: ChatRow, cfg: Config, changed: list[MessageRow]
) -> UnitDelta:
    stale: list[UnitRow] = []
    fresh: list[UnitRow] = []
    for topic_id, group in group_by_topic(chat, changed).items():
        old, new = _recut_windows(conn, chat, cfg.units, topic_id, group)
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


def _recut_windows(
    conn: sqlite3.Connection,
    chat: ChatRow,
    cfg: UnitsCfg,
    topic_id: int | None,
    changed: Sequence[MessageRow],
) -> tuple[list[UnitRow], list[UnitRow]]:
    """The windows of ``(chat, topic)`` a rebuild replaces and their replacements.

    The recut starts at the open window — the last one — and runs over every message stored
    from its ``msg_id_start`` on, so a message that continues it joins it and one after a long
    pause starts the next; nothing is recut when every changed message sits inside a closed
    window. Messages do not always arrive in id order, though: a channel stores a comment in its
    discussion group before the group's own history reaches that id, and a late comment on an
    old post lands below comments already windowed. A changed message that no window holds and
    that lies below the open window's start therefore moves the start down to the window before
    it (:func:`_recut_start`), and every window from there on is recut.
    """
    window = db.open_window(conn, chat.id, topic_id)
    if window is None:
        return [], cut_windows(_stored_since(conn, chat, topic_id, None), cfg, chat.id, topic_id)
    start = _recut_start(conn, chat.id, topic_id, window, changed)
    if start is None:
        return [], []
    stale = db.windows_from(conn, chat.id, topic_id, start)
    return stale, cut_windows(_stored_since(conn, chat, topic_id, start), cfg, chat.id, topic_id)


def _recut_start(
    conn: sqlite3.Connection,
    chat_id: int,
    topic_id: int | None,
    window: UnitRow,
    changed: Sequence[MessageRow],
) -> int | None:
    """Where the recut of ``(chat, topic)`` begins given its open ``window``; ``None`` when
    every changed message is held by a closed window.

    Normally the open window's start. When a changed message no window holds lies below it,
    the start of the window before that message — its boundary depends only on older messages,
    which are all in place — or the message itself when no window precedes it.
    """
    ids = sorted({msg.msg_id for msg in changed})
    held = db.windowed_msg_ids(conn, chat_id, topic_id, ids)
    loose = next((msg_id for msg_id in ids if msg_id not in held), None)
    if loose is not None and loose < window.msg_id_start:
        before = db.window_before(conn, chat_id, topic_id, loose)
        return loose if before is None else before.msg_id_start
    if ids[-1] < window.msg_id_start:
        return None
    return window.msg_id_start


def _stored_since(
    conn: sqlite3.Connection, chat: ChatRow, topic_id: int | None, start: int | None
) -> list[MessageRow]:
    """The messages of ``(chat, topic)`` from ``start`` on (everything when ``None``)."""
    if chat.is_forum:
        return db.get_messages_in_topic(conn, chat.id, topic_id, since_msg_id=start)
    return db.get_messages(conn, chat.id, since_msg_id=start)


def _rebuild_threads(
    conn: sqlite3.Connection, chat: ChatRow, cfg: UnitsCfg, changed: Sequence[MessageRow]
) -> tuple[list[UnitRow], list[UnitRow]]:
    """The thread units the changed messages belong to and their rebuilt replacements.

    Stale are the units holding a changed message or any member of a rebuilt thread — not only
    its root: when a root arrives after its replies (the group's copy of a post, stored after
    the comments the channel put under it), the unit its first reply used to head shares no
    message with the changed root and would linger next to the rebuilt thread otherwise.
    """
    threads = _thread_members(conn, chat.id, changed)
    touched = [msg.msg_id for msg in changed]
    touched += [member.msg_id for members in threads.values() for member in members]
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

    ``reactions`` is deliberately outside :func:`_content_key`, and keeping the stored row is
    exactly what preserves a total :func:`grepogram.db.refresh_unit_reactions` wrote after the
    unit was cut. Putting it in the key would look like the fix and be the opposite of one: with
    ``edit_refetch`` re-reading 200 messages per chat per sync, every reaction anyone adds would
    delete, re-insert and re-embed the unit holding it, for ever.
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
