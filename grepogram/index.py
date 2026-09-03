"""Lexical index: keeping ``msg_fts`` and ``unit_fts`` in step with ``messages`` and ``units``.

Both FTS5 tables are keyed by the parent rowid (``msg_fts.rowid = messages.id``,
``unit_fts.rowid = units.id``) and carry two indexed columns — ``raw``, the text as written, and
``stemmed``, its :func:`~grepogram.stem.stem_text` form — plus the ``chat_id`` and date columns
search filters on as plain ``AND`` predicates. FTS5 has no upsert and rejects a duplicate rowid,
so every (re-)index deletes the old row first: ``DELETE … WHERE rowid = ?`` is a direct lookup,
unlike a delete by an UNINDEXED column, which scans the whole table, and an edit therefore
replaces the row in place under the rowid the message or unit has always had.

:func:`index_chat` is the per-chat step :func:`grepogram.sync.on_chat_synced` runs right after
the unit rebuild: it indexes the messages a sync inserted or edited and applies the rebuild's
:class:`~grepogram.units.UnitDelta` to ``unit_fts``, dropping the vectors of deleted units as
well once ``unit_vec`` exists so a re-cut window's stale embedding can never resurface in a KNN.
"""

import logging
import sqlite3
from collections.abc import Iterable

from grepogram import db
from grepogram.models import ChatRow
from grepogram.stem import stem_text
from grepogram.units import UnitDelta

log = logging.getLogger(__name__)

_MSG_INSERT = "INSERT INTO msg_fts(rowid, raw, stemmed, chat_id, date) VALUES (?, ?, ?, ?, ?)"
_UNIT_INSERT = (
    "INSERT INTO unit_fts(rowid, raw, stemmed, chat_id, date_start) VALUES (?, ?, ?, ?, ?)"
)


def index_messages(conn: sqlite3.Connection, message_ids: Iterable[int]) -> int:
    """(Re-)index these ``messages.id`` rows in ``msg_fts``; returns how many were indexed.

    Every id is removed from the index first, then the rows with text are inserted under their
    own rowid. A message without text (media without a caption) stays out of the index, and an
    edit that emptied one drops its old row. Ids that are not stored are just removed.
    """
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return 0
    rows = [
        (msg.id, msg.text, stem_text(msg.text), msg.chat_id, msg.date)
        for msg in db.get_messages_by_ids(conn, ids)
        if msg.text.strip()
    ]
    with db.transaction(conn):
        conn.executemany("DELETE FROM msg_fts WHERE rowid = ?", [(msg_id,) for msg_id in ids])
        conn.executemany(_MSG_INSERT, rows)
    return len(rows)


def index_units(conn: sqlite3.Connection, delta: UnitDelta) -> int:
    """Apply a unit rebuild to ``unit_fts``: drop the deleted units, index the inserted ones.

    The inserted ids are removed before insertion too, so applying the same delta twice is
    harmless. Deleted units also lose their vectors (:func:`delete_unit_vectors`); inserted ones
    have none yet and are left to the embedding step. Returns how many units were indexed.
    """
    stale = [*delta.deleted_ids, *delta.inserted_ids]
    if not stale:
        return 0
    with db.transaction(conn):
        conn.executemany("DELETE FROM unit_fts WHERE rowid = ?", [(unit_id,) for unit_id in stale])
        delete_unit_vectors(conn, delta.deleted_ids)
        rows = [
            (unit.id, unit.text, stem_text(unit.text), unit.chat_id, unit.date_start)
            for unit in db.get_units_by_ids(conn, delta.inserted_ids)
        ]
        conn.executemany(_UNIT_INSERT, rows)
    return len(rows)


def delete_unit_vectors(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """Drop the vectors of these units from ``unit_vec``; a no-op while the table does not exist."""
    rows = [(unit_id,) for unit_id in dict.fromkeys(ids)]
    if not rows or not db.has_vec_table(conn):
        return
    with db.transaction(conn):
        conn.executemany(f"DELETE FROM {db.VEC_TABLE} WHERE rowid = ?", rows)


def index_chat(
    conn: sqlite3.Connection, chat: ChatRow, message_ids: Iterable[int], delta: UnitDelta
) -> None:
    """Index what one sync changed in ``chat``: its messages and the units the rebuild reported."""
    with db.transaction(conn):
        messages = index_messages(conn, message_ids)
        indexed = index_units(conn, delta)
    log.debug(
        "chat %s: indexed %d messages, %d units (%d deleted)",
        chat.id,
        messages,
        indexed,
        len(delta.deleted_ids),
    )
