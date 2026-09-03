"""SQLite storage: connection setup, versioned schema migrations and typed row accessors.

One file holds everything: ``chats``, ``users``, ``messages``, ``units``, the FTS5 tables and the
sqlite-vec table. FTS and vec rows are keyed by the parent rowid (``messages.id`` / ``units.id``)
so deletes are direct lookups; virtual tables cannot carry foreign keys, so :func:`delete_chat`
removes their rows before the ``chats`` row cascades to ``messages`` and ``units``.

Every writing function is atomic on its own and commits when it finishes, unless a transaction is
already open — wrap several calls in ``with transaction(conn):`` to commit them together.
"""

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager

import sqlite_vec

from grepogram.models import ChatRow, MessageRow, UnitKind, UnitRow, UserRow
from grepogram.paths import Paths

BUSY_TIMEOUT_MS = 5000
IN_BATCH = 500
FTS_TOKENIZE = "unicode61 remove_diacritics 2"
VEC_TABLE = "unit_vec"
META_SCHEMA_VERSION = "schema_version"
META_EMBED_MODEL = "embed_model"
META_EMBED_DIM = "embed_dim"

_V1: tuple[str, ...] = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE chats(
        id INTEGER PRIMARY KEY,
        type TEXT NOT NULL,
        title TEXT,
        username TEXT,
        is_forum INTEGER DEFAULT 0,
        source_id TEXT,
        discussion_of INTEGER,
        last_msg_id INTEGER DEFAULT 0,
        last_sync_at INTEGER,
        unavailable INTEGER DEFAULT 0,
        migrated_to INTEGER)""",
    "CREATE TABLE users(id INTEGER PRIMARY KEY, display_name TEXT, username TEXT)",
    """CREATE TABLE messages(
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
        msg_id INTEGER NOT NULL,
        date INTEGER NOT NULL,
        edit_date INTEGER,
        from_id INTEGER,
        from_name TEXT,
        reply_to_msg_id INTEGER,
        topic_id INTEGER,
        fwd_from TEXT,
        text TEXT NOT NULL DEFAULT '',
        media_kind TEXT,
        media_filename TEXT,
        reactions_total INTEGER DEFAULT 0,
        UNIQUE (chat_id, msg_id))""",
    "CREATE INDEX messages_chat_date ON messages(chat_id, date)",
    "CREATE INDEX messages_reply ON messages(chat_id, reply_to_msg_id)",
    """CREATE TABLE units(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
        topic_id INTEGER,
        kind TEXT NOT NULL,
        msg_id_start INTEGER,
        msg_id_end INTEGER,
        msg_ids TEXT NOT NULL,
        date_start INTEGER,
        date_end INTEGER,
        text TEXT NOT NULL,
        dirty INTEGER DEFAULT 1,
        embedded_model TEXT)""",
    "CREATE INDEX units_chat_kind_range ON units(chat_id, kind, msg_id_start, msg_id_end)",
    f"""CREATE VIRTUAL TABLE msg_fts USING fts5(
        raw, stemmed, chat_id UNINDEXED, date UNINDEXED, tokenize='{FTS_TOKENIZE}')""",
    f"""CREATE VIRTUAL TABLE unit_fts USING fts5(
        raw, stemmed, chat_id UNINDEXED, date_start UNINDEXED, tokenize='{FTS_TOKENIZE}')""",
)

MIGRATIONS: tuple[tuple[str, ...], ...] = (_V1,)
SCHEMA_VERSION = len(MIGRATIONS)

_VEC_DIM_RE = re.compile(r"FLOAT\[(\d+)\]")

_MESSAGE_UPSERT = """
    INSERT INTO messages(chat_id, msg_id, date, edit_date, from_id, from_name, reply_to_msg_id,
                         topic_id, fwd_from, text, media_kind, media_filename, reactions_total)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, msg_id) DO UPDATE SET
        date = excluded.date,
        edit_date = excluded.edit_date,
        from_id = excluded.from_id,
        from_name = excluded.from_name,
        reply_to_msg_id = excluded.reply_to_msg_id,
        topic_id = excluded.topic_id,
        fwd_from = excluded.fwd_from,
        text = excluded.text,
        media_kind = excluded.media_kind,
        media_filename = excluded.media_filename,
        reactions_total = excluded.reactions_total
    RETURNING id"""

_UNIT_INSERT = """
    INSERT INTO units(chat_id, topic_id, kind, msg_id_start, msg_id_end, msg_ids,
                      date_start, date_end, text, dirty, embedded_model)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    RETURNING id"""


class SchemaError(Exception):
    """The database schema cannot be brought to the shape this code expects."""


class VecDimMismatch(SchemaError):
    """``unit_vec`` already exists with a different embedding dimension."""


def connect(target: Paths | str) -> sqlite3.Connection:
    """Open the index database and load sqlite-vec.

    ``target`` is the resolved :class:`Paths` (directories are created) or a raw database string
    such as ``":memory:"``. The connection may be shared between threads, waits up to five seconds
    on a locked database, enforces foreign keys and returns :class:`sqlite3.Row` rows.
    """
    if isinstance(target, Paths):
        target.ensure_dirs()
        database = str(target.db_file)
    else:
        database = target
    conn = sqlite3.connect(database, check_same_thread=False, isolation_level="IMMEDIATE")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the block atomically, joining a transaction that is already open instead of nesting."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --- schema ----------------------------------------------------------------------------------


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def schema_version(conn: sqlite3.Connection) -> int:
    """Version recorded in ``meta``; ``0`` for an empty database."""
    if not has_table(conn, "meta"):
        return 0
    value = get_meta(conn, META_SCHEMA_VERSION)
    return int(value) if value else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations and return the schema version now in place.

    A database already at :data:`SCHEMA_VERSION` is left untouched; one created by a newer
    grepogram raises :class:`SchemaError` rather than being downgraded.
    """
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema v{current} is newer than this grepogram supports (v{SCHEMA_VERSION})"
        )
    if current == SCHEMA_VERSION:
        return current
    with transaction(conn):
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[version - 1]:
                conn.execute(statement)
            set_meta(conn, META_SCHEMA_VERSION, str(version))
    return SCHEMA_VERSION


def has_vec_table(conn: sqlite3.Connection) -> bool:
    return has_table(conn, VEC_TABLE)


def vec_dim(conn: sqlite3.Connection) -> int | None:
    """Embedding dimension declared by ``unit_vec``, or ``None`` before it exists."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (VEC_TABLE,)
    ).fetchone()
    if row is None:
        return None
    match = _VEC_DIM_RE.search(row["sql"])
    if match is None:
        raise SchemaError(f"cannot read the embedding dimension of {VEC_TABLE}: {row['sql']}")
    return int(match.group(1))


def ensure_vec_table(conn: sqlite3.Connection, dim: int, drop: bool = False) -> None:
    """Create ``unit_vec`` for ``dim``-sized embeddings and record ``embed_dim`` in ``meta``.

    An existing table with the same dimension is kept as is. A different dimension raises
    :class:`VecDimMismatch` unless ``drop`` is set, in which case every stored vector is discarded
    and the table is recreated.
    """
    if dim <= 0:
        raise ValueError(f"embedding dimension must be positive, got {dim}")
    current = vec_dim(conn)
    if current == dim:
        return
    if current is not None and not drop:
        raise VecDimMismatch(
            f"{VEC_TABLE} stores {current}-dimensional embeddings, requested {dim}; "
            "rebuild it with drop=True"
        )
    with transaction(conn):
        if current is not None:
            conn.execute(f"DROP TABLE {VEC_TABLE}")
        conn.execute(
            f"CREATE VIRTUAL TABLE {VEC_TABLE} USING vec0("
            "chat_id INTEGER PARTITION KEY, date_start INTEGER, "
            f"embedding FLOAT[{dim}] distance_metric=cosine)"
        )
        set_meta(conn, META_EMBED_DIM, str(dim))


# --- meta ------------------------------------------------------------------------------------


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --- chats -----------------------------------------------------------------------------------


def upsert_chat(conn: sqlite3.Connection, chat: ChatRow) -> ChatRow:
    """Insert ``chat`` or refresh the identity columns of the existing row; returns what is stored.

    Identity columns are ``type``, ``title``, ``username``, ``is_forum``, ``source_id`` and
    ``discussion_of``. Sync state (``last_msg_id``, ``last_sync_at``, ``unavailable``,
    ``migrated_to``) is written on insert only, so re-resolving a source never rewinds a synced
    chat; change it with :func:`set_chat_progress`, :func:`set_chat_unavailable` and
    :func:`set_chat_migrated`.
    """
    with transaction(conn):
        conn.execute(
            """INSERT INTO chats(id, type, title, username, is_forum, source_id, discussion_of,
                                 last_msg_id, last_sync_at, unavailable, migrated_to)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   type = excluded.type,
                   title = excluded.title,
                   username = excluded.username,
                   is_forum = excluded.is_forum,
                   source_id = excluded.source_id,
                   discussion_of = excluded.discussion_of""",
            (
                chat.id,
                chat.type,
                chat.title,
                chat.username,
                int(chat.is_forum),
                chat.source_id,
                chat.discussion_of,
                chat.last_msg_id,
                chat.last_sync_at,
                int(chat.unavailable),
                chat.migrated_to,
            ),
        )
        return _chat_row(conn.execute("SELECT * FROM chats WHERE id = ?", (chat.id,)).fetchone())


def get_chat(conn: sqlite3.Connection, chat_id: int) -> ChatRow | None:
    row = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return None if row is None else _chat_row(row)


def get_discussion_chat(conn: sqlite3.Connection, channel_id: int) -> ChatRow | None:
    """The discussion group linked to a channel (``discussion_of = channel_id``), if stored."""
    row = conn.execute(
        "SELECT * FROM chats WHERE discussion_of = ? ORDER BY id LIMIT 1", (channel_id,)
    ).fetchone()
    return None if row is None else _chat_row(row)


def list_chats(conn: sqlite3.Connection, source_id: str | None = None) -> list[ChatRow]:
    """All chats (or those pulled in by one source) ordered by id."""
    if source_id is None:
        rows = conn.execute("SELECT * FROM chats ORDER BY id")
    else:
        rows = conn.execute("SELECT * FROM chats WHERE source_id = ? ORDER BY id", (source_id,))
    return [_chat_row(row) for row in rows]


def set_chat_progress(
    conn: sqlite3.Connection, chat_id: int, last_msg_id: int, last_sync_at: int | None
) -> None:
    """Record fetch progress; ``last_sync_at`` stays ``None`` while a first sync is incomplete."""
    with transaction(conn):
        conn.execute(
            "UPDATE chats SET last_msg_id = ?, last_sync_at = ? WHERE id = ?",
            (last_msg_id, last_sync_at, chat_id),
        )


def set_chat_unavailable(conn: sqlite3.Connection, chat_id: int, unavailable: bool = True) -> None:
    with transaction(conn):
        conn.execute("UPDATE chats SET unavailable = ? WHERE id = ?", (int(unavailable), chat_id))


def set_chat_migrated(conn: sqlite3.Connection, chat_id: int, migrated_to: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE chats SET migrated_to = ? WHERE id = ?", (migrated_to, chat_id))


def delete_chat(conn: sqlite3.Connection, chat_id: int) -> None:
    """Remove a chat with its messages and units, including their FTS and vector rows.

    The virtual-table rows go first, addressed by rowid (a direct lookup, not a scan); deleting
    the ``chats`` row then cascades to ``messages`` and ``units``.
    """
    with transaction(conn):
        conn.execute(
            "DELETE FROM msg_fts WHERE rowid IN (SELECT id FROM messages WHERE chat_id = ?)",
            (chat_id,),
        )
        conn.execute(
            "DELETE FROM unit_fts WHERE rowid IN (SELECT id FROM units WHERE chat_id = ?)",
            (chat_id,),
        )
        if has_vec_table(conn):
            conn.execute(
                f"DELETE FROM {VEC_TABLE} WHERE rowid IN (SELECT id FROM units WHERE chat_id = ?)",
                (chat_id,),
            )
        conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


# --- users -----------------------------------------------------------------------------------


def upsert_users(conn: sqlite3.Connection, users: Iterable[UserRow]) -> None:
    with transaction(conn):
        conn.executemany(
            "INSERT INTO users(id, display_name, username) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "display_name = excluded.display_name, username = excluded.username",
            [(user.id, user.display_name, user.username) for user in users],
        )


# --- messages --------------------------------------------------------------------------------


def upsert_messages(conn: sqlite3.Connection, batch: Iterable[MessageRow]) -> list[int]:
    """Insert or update messages keyed by ``(chat_id, msg_id)``; returns ``messages.id`` per row.

    Conflicts update in place, so an edited message keeps its ``id`` and therefore its FTS
    rowid. The chat row must exist (foreign key).
    """
    ids: list[int] = []
    with transaction(conn):
        for message in batch:
            row = conn.execute(
                _MESSAGE_UPSERT,
                (
                    message.chat_id,
                    message.msg_id,
                    message.date,
                    message.edit_date,
                    message.from_id,
                    message.from_name,
                    message.reply_to_msg_id,
                    message.topic_id,
                    message.fwd_from,
                    message.text,
                    message.media_kind,
                    message.media_filename,
                    message.reactions_total,
                ),
            ).fetchone()
            ids.append(int(row["id"]))
    return ids


def get_messages(
    conn: sqlite3.Connection,
    chat_id: int,
    since_msg_id: int | None = None,
    topic_id: int | None = None,
) -> list[MessageRow]:
    """Messages of a chat in ``msg_id`` order, from ``since_msg_id`` (inclusive), within a topic."""
    sql = "SELECT * FROM messages WHERE chat_id = ?"
    params: list[int] = [chat_id]
    if since_msg_id is not None:
        sql += " AND msg_id >= ?"
        params.append(since_msg_id)
    if topic_id is not None:
        sql += " AND topic_id = ?"
        params.append(topic_id)
    sql += " ORDER BY msg_id"
    return [_message_row(row) for row in conn.execute(sql, params)]


def get_topic_messages(
    conn: sqlite3.Connection, chat_id: int, topic_ids: Iterable[int]
) -> dict[int, list[MessageRow]]:
    """Messages of a chat grouped by ``topic_id`` for the given topics, each in ``msg_id`` order.

    One query per :data:`IN_BATCH` topics rather than one per topic — a channel rebuild reads the
    comments of every post this way. Topics without messages are absent from the result.
    """
    grouped: dict[int, list[MessageRow]] = {}
    for chunk in _chunks(topic_ids):
        rows = conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? "
            f"AND topic_id IN ({_marks(chunk)}) ORDER BY msg_id",
            [chat_id, *chunk],
        )
        for row in rows:
            grouped.setdefault(int(row["topic_id"]), []).append(_message_row(row))
    return grouped


def get_messages_in_topic(
    conn: sqlite3.Connection,
    chat_id: int,
    topic_id: int | None,
    since_msg_id: int | None = None,
) -> list[MessageRow]:
    """Messages of one topic in ``msg_id`` order, from ``since_msg_id`` (inclusive).

    Unlike :func:`get_messages`, ``topic_id=None`` is a filter here — the messages outside any
    topic (a plain chat, or a forum's General topic) — not the absence of one, so cutting
    windows never mixes them with a topic's.
    """
    sql = "SELECT * FROM messages WHERE chat_id = ? AND topic_id IS ?"
    params: list[int | None] = [chat_id, topic_id]
    if since_msg_id is not None:
        sql += " AND msg_id >= ?"
        params.append(since_msg_id)
    sql += " ORDER BY msg_id"
    return [_message_row(row) for row in conn.execute(sql, params)]


def get_messages_by_ids(conn: sqlite3.Connection, ids: Iterable[int]) -> list[MessageRow]:
    """Rows by ``messages.id`` (any chat) in id order; ids that are not stored are skipped."""
    found: dict[int, MessageRow] = {}
    for chunk in _chunks(ids):
        rows = conn.execute(f"SELECT * FROM messages WHERE id IN ({_marks(chunk)})", chunk)
        for row in rows:
            found[int(row["id"])] = _message_row(row)
    return [found[row_id] for row_id in sorted(found)]


def get_messages_by_msg_id(
    conn: sqlite3.Connection, chat_id: int, msg_ids: Iterable[int]
) -> dict[int, MessageRow]:
    """Messages of one chat keyed by ``msg_id``; ids that are not stored are absent."""
    found: dict[int, MessageRow] = {}
    for chunk in _chunks(msg_ids):
        rows = conn.execute(
            f"SELECT * FROM messages WHERE chat_id = ? AND msg_id IN ({_marks(chunk)})",
            [chat_id, *chunk],
        )
        for row in rows:
            found[int(row["msg_id"])] = _message_row(row)
    return found


def get_replies(
    conn: sqlite3.Connection, chat_id: int, parent_ids: Iterable[int]
) -> list[MessageRow]:
    """Messages of a chat that reply to any of ``parent_ids``, in ``msg_id`` order."""
    found: list[MessageRow] = []
    for chunk in _chunks(parent_ids):
        rows = conn.execute(
            f"SELECT * FROM messages WHERE chat_id = ? AND reply_to_msg_id IN ({_marks(chunk)})",
            [chat_id, *chunk],
        )
        found.extend(_message_row(row) for row in rows)
    return sorted(found, key=lambda msg: msg.msg_id)


def get_message(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> MessageRow | None:
    row = conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id)
    ).fetchone()
    return None if row is None else _message_row(row)


def message_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """Stored messages per chat id; chats without messages are absent."""
    rows = conn.execute("SELECT chat_id, COUNT(*) AS n FROM messages GROUP BY chat_id")
    return {int(row["chat_id"]): int(row["n"]) for row in rows}


# --- units -----------------------------------------------------------------------------------


def get_units(
    conn: sqlite3.Connection, chat_id: int, kind: UnitKind | None = None
) -> list[UnitRow]:
    """Units of a chat, optionally of one kind, in id order."""
    sql = "SELECT * FROM units WHERE chat_id = ?"
    params: list[int | str] = [chat_id]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY id"
    return [_unit_row(row) for row in conn.execute(sql, params)]


def get_units_by_ids(conn: sqlite3.Connection, ids: Iterable[int]) -> list[UnitRow]:
    """Units by id (any chat) in id order; ids that are not stored are skipped."""
    found: dict[int, UnitRow] = {}
    for chunk in _chunks(ids):
        rows = conn.execute(f"SELECT * FROM units WHERE id IN ({_marks(chunk)})", chunk)
        for row in rows:
            found[int(row["id"])] = _unit_row(row)
    return [found[unit_id] for unit_id in sorted(found)]


def insert_units(conn: sqlite3.Connection, units: Iterable[UnitRow]) -> list[int]:
    """Insert units and return their ids in order; a fresh :class:`UnitRow` is ``dirty``."""
    ids: list[int] = []
    with transaction(conn):
        for unit in units:
            row = conn.execute(
                _UNIT_INSERT,
                (
                    unit.chat_id,
                    unit.topic_id,
                    unit.kind,
                    unit.msg_id_start,
                    unit.msg_id_end,
                    json.dumps(unit.msg_ids, separators=(",", ":")),
                    unit.date_start,
                    unit.date_end,
                    unit.text,
                    int(unit.dirty),
                    unit.embedded_model,
                ),
            ).fetchone()
            ids.append(int(row["id"]))
    return ids


def delete_units(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """Delete units by id; their FTS and vector rows are the indexer's to drop by the same ids."""
    with transaction(conn):
        for chunk in _chunks(ids):
            conn.execute(f"DELETE FROM units WHERE id IN ({_marks(chunk)})", chunk)


def open_window(conn: sqlite3.Connection, chat_id: int, topic_id: int | None) -> UnitRow | None:
    """The last window of ``(chat, topic)`` — the one further messages may still extend.

    Windows are cut in message order, so the one reaching the highest ``msg_id`` is the last;
    ``topic_id=None`` addresses the messages outside any topic.
    """
    row = conn.execute(
        "SELECT * FROM units WHERE chat_id = ? AND kind = 'window' AND topic_id IS ? "
        "ORDER BY msg_id_end DESC, id DESC LIMIT 1",
        (chat_id, topic_id),
    ).fetchone()
    return None if row is None else _unit_row(row)


def threads_touching(
    conn: sqlite3.Connection, chat_id: int, msg_ids: Iterable[int]
) -> list[UnitRow]:
    """Thread units of a chat whose ``msg_ids`` include any of ``msg_ids``, in id order."""
    found: dict[int, UnitRow] = {}
    for chunk in _chunks(msg_ids):
        rows = conn.execute(
            "SELECT units.* FROM units WHERE chat_id = ? AND kind = 'thread' AND EXISTS ("
            "SELECT 1 FROM json_each(units.msg_ids) "
            f"WHERE json_each.value IN ({_marks(chunk)}))",
            [chat_id, *chunk],
        )
        for row in rows:
            found[int(row["id"])] = _unit_row(row)
    return [found[unit_id] for unit_id in sorted(found)]


def post_units(conn: sqlite3.Connection, chat_id: int, post_ids: Iterable[int]) -> list[UnitRow]:
    """The ``post`` units of a channel for the given post ids, in id order."""
    found: dict[int, UnitRow] = {}
    for chunk in _chunks(post_ids):
        rows = conn.execute(
            "SELECT * FROM units WHERE chat_id = ? AND kind = 'post' "
            f"AND msg_id_start IN ({_marks(chunk)})",
            [chat_id, *chunk],
        )
        for row in rows:
            found[int(row["id"])] = _unit_row(row)
    return [found[unit_id] for unit_id in sorted(found)]


def mark_dirty(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """Flag units for (re-)embedding."""
    with transaction(conn):
        for chunk in _chunks(ids):
            conn.execute(f"UPDATE units SET dirty = 1 WHERE id IN ({_marks(chunk)})", chunk)


# --- helpers ---------------------------------------------------------------------------------


def _chunks(ids: Iterable[int]) -> Iterator[list[int]]:
    """Distinct ``ids`` in slices of :data:`IN_BATCH`, the most one ``IN (…)`` list carries."""
    distinct = list(dict.fromkeys(ids))
    for start in range(0, len(distinct), IN_BATCH):
        yield distinct[start : start + IN_BATCH]


def _marks(chunk: Sequence[object]) -> str:
    return ", ".join("?" * len(chunk))


# --- row mapping -----------------------------------------------------------------------------


def _chat_row(row: sqlite3.Row) -> ChatRow:
    return ChatRow(
        id=row["id"],
        type=row["type"],
        title=row["title"],
        username=row["username"],
        is_forum=bool(row["is_forum"]),
        source_id=row["source_id"],
        discussion_of=row["discussion_of"],
        last_msg_id=row["last_msg_id"],
        last_sync_at=row["last_sync_at"],
        unavailable=bool(row["unavailable"]),
        migrated_to=row["migrated_to"],
    )


def _message_row(row: sqlite3.Row) -> MessageRow:
    return MessageRow(
        id=row["id"],
        chat_id=row["chat_id"],
        msg_id=row["msg_id"],
        date=row["date"],
        edit_date=row["edit_date"],
        from_id=row["from_id"],
        from_name=row["from_name"],
        reply_to_msg_id=row["reply_to_msg_id"],
        topic_id=row["topic_id"],
        fwd_from=row["fwd_from"],
        text=row["text"],
        media_kind=row["media_kind"],
        media_filename=row["media_filename"],
        reactions_total=row["reactions_total"],
    )


def _unit_row(row: sqlite3.Row) -> UnitRow:
    return UnitRow(
        id=row["id"],
        chat_id=row["chat_id"],
        topic_id=row["topic_id"],
        kind=row["kind"],
        msg_id_start=row["msg_id_start"],
        msg_id_end=row["msg_id_end"],
        msg_ids=json.loads(row["msg_ids"]),
        date_start=row["date_start"],
        date_end=row["date_end"],
        text=row["text"],
        dirty=bool(row["dirty"]),
        embedded_model=row["embedded_model"],
    )
