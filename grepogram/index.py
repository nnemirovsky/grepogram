"""Index maintenance: the FTS5 tables and the sqlite-vec table, kept in step with ``units``.

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

The dense side is ``unit_vec``, a ``vec0`` table keyed by ``units.id`` as well, with ``chat_id``
as its partition key and ``date_start`` as metadata. Units are inserted ``dirty`` and
:func:`embed_dirty_units` — run once at the end of a sync, or by ``grepogram embed`` — turns them
into vectors slice by slice; a vec0 row can neither be updated in place (the partition key is
immutable) nor inserted twice under one rowid, so a re-embed deletes and inserts.
:func:`ensure_embedding_space` guards the invariant every distance depends on: all stored vectors
come from the model recorded in ``meta.embed_model`` at the width ``unit_vec`` declares, and a
changed model is an :class:`EmbeddingSpaceMismatch` until ``grepogram embed --reembed`` rebuilds
the table (:func:`check_embedding_space` is the read-only half search runs before a query).
:func:`knn` queries it under the config's fan-out rule: the partition key supports
``=`` only, so a filter over a few chats runs one KNN per chat, while a filter over many runs a
single KNN over-fetched :data:`KNN_OVERFETCH` times deeper and drops the other chats afterwards.
"""

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

import sqlite_vec

from grepogram import db
from grepogram.embed import Embedder
from grepogram.models import ChatRow, Filters
from grepogram.stem import stem_text
from grepogram.units import UnitDelta

log = logging.getLogger(__name__)

EMBED_BATCH = 256
KNN_OVERFETCH = 4
_MSG_INSERT = "INSERT INTO msg_fts(rowid, raw, stemmed, chat_id, date) VALUES (?, ?, ?, ?, ?)"
_UNIT_INSERT = (
    "INSERT INTO unit_fts(rowid, raw, stemmed, chat_id, date_start) VALUES (?, ?, ?, ?, ?)"
)
_VEC_INSERT = (
    f"INSERT INTO {db.VEC_TABLE}(rowid, chat_id, date_start, embedding) VALUES (?, ?, ?, ?)"
)
_VEC_DELETE = f"DELETE FROM {db.VEC_TABLE} WHERE rowid = ?"


class EmbeddingSpaceMismatch(Exception):
    """The stored vectors come from another model or width than the configured embedder."""


class Budget(Protocol):
    """What :func:`embed_dirty_units` needs from a :class:`~grepogram.sync.SyncBudget`."""

    @property
    def expired(self) -> bool: ...


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
        conn.executemany(_VEC_DELETE, rows)


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


# --- dense -----------------------------------------------------------------------------------


def check_embedding_space(conn: sqlite3.Connection, embedder: Embedder) -> None:
    """Raise :class:`EmbeddingSpaceMismatch` unless the stored space is ``embedder``'s.

    Read-only: compares ``meta.embed_model`` and the width ``unit_vec`` declares with the
    embedder's name and ``dim``; a database never embedded passes. This is what search checks
    before a KNN, since a model change at the same width would otherwise go unnoticed.
    """
    stored_model = db.get_meta(conn, db.META_EMBED_MODEL)
    table_dim = db.vec_dim(conn)
    if stored_model not in (None, embedder.name) or table_dim not in (None, embedder.dim):
        raise EmbeddingSpaceMismatch(
            f"the dense index was built with {stored_model or 'an unknown model'} "
            f"({table_dim}-d) but the configured model is {embedder.name} "
            f"({embedder.dim}-d); run `grepogram embed --reembed` to rebuild it"
        )


def ensure_embedding_space(
    conn: sqlite3.Connection, embedder: Embedder, reembed: bool = False
) -> None:
    """Make ``unit_vec`` and ``meta`` agree with ``embedder``; ``reembed`` starts from scratch.

    A database never embedded gets its table created and ``embed_model`` recorded. One
    embedded with another model or width raises :class:`EmbeddingSpaceMismatch`
    — vectors from two spaces cannot be compared — unless ``reembed`` is set, which drops the
    vectors, flags every unit dirty and records the new space; ``reembed`` does the same for a
    matching space, which is how a deliberate full re-embed begins.
    """
    try:
        check_embedding_space(conn, embedder)
    except EmbeddingSpaceMismatch:
        if not reembed:
            raise
    with db.transaction(conn):
        if reembed:
            db.ensure_vec_table(conn, embedder.dim, drop=True)
            db.reset_embedded(conn)
            log.info("dense index reset for %s (%d-d)", embedder.name, embedder.dim)
        else:
            db.ensure_vec_table(conn, embedder.dim)
        db.set_meta(conn, db.META_EMBED_MODEL, embedder.name)


def embed_dirty_units(
    conn: sqlite3.Connection,
    embedder: Embedder,
    batch: int = EMBED_BATCH,
    budget: Budget | None = None,
) -> int:
    """Embed every unit flagged ``dirty`` and store the vectors; returns how many were embedded.

    Units are read in id order in slices of ``batch``, embedded together and written in one
    transaction per slice — the old vector of a re-embedded unit is deleted first, since vec0
    neither updates in place nor accepts a duplicate rowid — after which they are marked clean
    with ``embedded_model``. A zero vector (a fake embedder's answer to a text without features)
    is not stored: it has no cosine distance and would hold a KNN slot with a ``NULL`` one; the
    unit still counts as embedded. Stops between slices once ``budget`` expires, leaving the
    rest flagged for the next run. Raises :class:`EmbeddingSpaceMismatch` when the stored
    vectors belong to another model.
    """
    if batch <= 0:
        raise ValueError(f"batch must be positive, got {batch}")
    ensure_embedding_space(conn, embedder)
    done = 0
    after_id = 0
    while budget is None or not budget.expired:
        units = db.get_dirty_units(conn, batch, after_id)
        if not units:
            break
        vectors = embedder.embed([unit.text for unit in units])
        if len(vectors) != len(units):
            raise RuntimeError(
                f"embedder {embedder.name} returned {len(vectors)} vectors for {len(units)} texts"
            )
        ids = [unit.id for unit in units if unit.id is not None]
        with db.transaction(conn):
            conn.executemany(_VEC_DELETE, [(unit_id,) for unit_id in ids])
            conn.executemany(
                _VEC_INSERT,
                [
                    (unit.id, unit.chat_id, unit.date_start, sqlite_vec.serialize_float32(vector))
                    for unit, vector in zip(units, vectors, strict=True)
                    if any(vector)
                ],
            )
            db.set_embedded(conn, ids, embedder.name)
        done += len(units)
        after_id = ids[-1]
    if done:
        log.info("embedded %d units with %s", done, embedder.name)
    return done


@dataclass(frozen=True, slots=True)
class _Neighbour:
    rowid: int
    chat_id: int
    distance: float


def knn(
    conn: sqlite3.Connection,
    qvec: Sequence[float],
    filters: Filters,
    k: int,
    fanout_max: int,
) -> list[tuple[int, float]]:
    """The ``k`` units nearest to ``qvec`` under ``filters``, as ``(unit id, cosine distance)``
    nearest first.

    ``[]`` when ``unit_vec`` does not exist or is empty, when the filter selects no chat, and
    for a zero query vector (which has no cosine distance). ``chat_id`` is the partition key and
    supports ``=`` only: a filter over at most ``fanout_max`` chats runs one KNN per chat and
    merges them; a wider filter runs a single KNN ``k *`` :data:`KNN_OVERFETCH` deep and drops
    the other chats' rows afterwards, which may leave fewer than ``k``; no chat filter is one
    plain KNN. ``since`` / ``until`` become ``date_start`` constraints inside the KNN. Raises
    :class:`EmbeddingSpaceMismatch` when ``qvec`` has another width than the table.
    """
    if k <= 0 or not any(qvec) or (filters.chat_ids is not None and not filters.chat_ids):
        return []
    if not db.has_vectors(conn):
        return []
    dim = db.vec_dim(conn)
    if dim != len(qvec):
        raise EmbeddingSpaceMismatch(
            f"the query vector has {len(qvec)} dimensions but {db.VEC_TABLE} stores {dim}; "
            "the embedding model changed, run `grepogram embed --reembed`"
        )
    query = sqlite_vec.serialize_float32(list(qvec))
    chat_ids = None if filters.chat_ids is None else sorted(filters.chat_ids)
    if chat_ids is not None and len(chat_ids) <= fanout_max:
        found = [n for chat_id in chat_ids for n in _neighbours(conn, query, k, filters, chat_id)]
    else:
        found = _neighbours(conn, query, k if chat_ids is None else k * KNN_OVERFETCH, filters)
        if chat_ids is not None:
            wanted = set(chat_ids)
            found = [n for n in found if n.chat_id in wanted]
    found.sort(key=lambda n: (n.distance, n.rowid))
    return [(n.rowid, n.distance) for n in found[:k]]


def _neighbours(
    conn: sqlite3.Connection,
    query: bytes,
    k: int,
    filters: Filters,
    chat_id: int | None = None,
) -> list[_Neighbour]:
    """One vec0 KNN: ``k`` rows nearest to ``query``, within one partition when ``chat_id`` is
    given, within the date bounds of ``filters``; rows without a distance (zero vectors) are
    dropped."""
    sql = f"SELECT rowid, chat_id, distance FROM {db.VEC_TABLE} WHERE embedding MATCH ? AND k = ?"
    params: list[object] = [query, k]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params.append(chat_id)
    if filters.since is not None:
        sql += " AND date_start >= ?"
        params.append(filters.since)
    if filters.until is not None:
        sql += " AND date_start <= ?"
        params.append(filters.until)
    return [
        _Neighbour(int(row["rowid"]), int(row["chat_id"]), float(row["distance"]))
        for row in conn.execute(sql, params)
        if row["distance"] is not None
    ]
