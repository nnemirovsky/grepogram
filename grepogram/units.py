"""Search units: the conversation-sized chunks that get indexed and embedded.

Single messages are too small to embed and too many to store as vectors, so search runs over
*units*: time windows of a chat (this module), reply threads and channel posts (added later in
the same module). A unit's ``text`` is one rendered line per message —
``[YYYY-MM-DD HH:MM] name: text`` — with a ``[photo]``-style placeholder for media without a
caption, and its ``msg_ids`` keep the mapping back to the original messages for deep links.

Everything here is a pure function over :class:`~grepogram.models.MessageRow` lists: no database,
no Telegram. :func:`cut_windows` walks one ``(chat, topic)`` in chronological order and starts a
new window after a pause longer than ``window_gap_min`` minutes, or once the open window holds
``window_max_msgs`` messages or ``window_max_chars`` characters of rendered text. Forum chats are
split into topics first with :func:`group_by_topic`.
"""

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from grepogram.models import MessageRow, UnitKind, UnitRow, UnitsCfg

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
