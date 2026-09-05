"""SQLite storage: connection setup, versioned schema migrations and typed row accessors.

One file holds everything: ``chats``, ``users``, ``messages``, ``units``, the FTS5 tables and the
sqlite-vec table. FTS and vec rows are keyed by the parent rowid (``messages.id`` / ``units.id``)
so deletes are direct lookups; virtual tables cannot carry foreign keys, so :func:`delete_chat`
removes their rows before the ``chats`` row cascades to ``messages`` and ``units``.

Every writing function is atomic on its own and commits when it finishes, unless a transaction is
already open — wrap several calls in ``with transaction(conn):`` to commit them together.

``messages.indexed`` ties the raw rows to what is derived from them: a row is written with the
flag at 0 and :func:`mark_indexed` sets it once its units and ``msg_fts`` entry exist, so a sync
that stopped between a batch commit and the rebuild — a flood wait, a cancelled tool call, a
crash — leaves rows :func:`unindexed_message_ids` reports and the next run picks up.

One connection serves the whole process, including the worker threads the MCP server runs
retrieval and embedding on, so :class:`Connection` serialises its use: every statement runs and
is fetched to completion under one re-entrant lock (:class:`_Cursor`), and :func:`transaction`
holds that lock from ``BEGIN`` to ``COMMIT``. Another thread therefore never steps a statement
while one is in flight, never joins a transaction it did not open and never reads rows that are
not committed yet. The connection runs in autocommit mode (``isolation_level=None``): a bare
statement commits on its own instead of opening the implicit transaction the ``sqlite3`` module
would otherwise leave behind for the next ``transaction()`` to join.
"""

import json
import re
import sqlite3
import sys
import threading
from collections import deque
from collections.abc import Collection, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

import sqlite_vec

from grepogram.models import ChatRow, MessageRow, UnitRow, UserRow
from grepogram.paths import Paths

BUSY_TIMEOUT_MS = 5000
IN_BATCH = 500
FTS_TOKENIZE = "unicode61 remove_diacritics 2"
VEC_TABLE = "unit_vec"
_WINDOW_SCOPE = "chat_id = ? AND kind = 'window' AND topic_id IS ?"
"""The windows of one ``(chat, topic)``; the ranges of a forum's topics interleave, so both
are part of every window lookup."""
META_SCHEMA_VERSION = "schema_version"
META_EMBED_MODEL = "embed_model"
META_LAST_SYNC_RUN = "last_sync_run"
META_UNIT_RECIPE = "unit_recipe"
"""Recipe version the stored units were cut with (:data:`grepogram.units.RECIPE_VERSION`)."""
META_RECUT_PREFIX = "unit_recut:"
"""Prefix of the per-chat marker a re-cut writes, ``unit_recut:<chat_id>``."""
META_PRUNE_PREFIX = "prune_sweep:"
"""Prefix of the deletion sweep's cursor, ``prune_sweep:<chat_id>`` (:func:`prune_cursor`)."""

_V5: tuple[str, ...] = (
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
    # a channel has one discussion group at a time — sync.link_discussion_chat re-points the link
    # when Telegram reports another group and clears it when the channel loses its own — so
    # get_discussion_chat has a single row to answer with
    """CREATE UNIQUE INDEX chats_discussion_of ON chats(discussion_of)
       WHERE discussion_of IS NOT NULL""",
    "CREATE TABLE users(id INTEGER PRIMARY KEY, display_name TEXT, username TEXT)",
    # comment_of_chat_id / comment_of_msg_id name the channel post a message is a comment on and
    # are kept apart from topic_id, Telegram's thread/topic id: a discussion group can be a forum,
    # and a forum topic root and a channel post are separate id spaces that both start at 1, so
    # one column cannot say which of the two a number is. Both comment columns are NULL on every
    # row that is not a comment. topic_id is only meaningful inside a forum — outside one it may
    # still hold a legacy thread id, which units.window_topic ignores.
    # indexed is 0 while the units and the msg_fts row derived from a message are behind it: set
    # by every insert and update, cleared by the rebuild.
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
        comment_of_chat_id INTEGER,
        comment_of_msg_id INTEGER,
        fwd_from TEXT,
        text TEXT NOT NULL DEFAULT '',
        media_kind TEXT,
        media_filename TEXT,
        reactions_total INTEGER DEFAULT 0,
        indexed INTEGER NOT NULL DEFAULT 0,
        UNIQUE (chat_id, msg_id))""",
    "CREATE INDEX messages_chat_date ON messages(chat_id, date)",
    "CREATE INDEX messages_reply ON messages(chat_id, reply_to_msg_id)",
    "CREATE INDEX messages_unindexed ON messages(chat_id, id) WHERE indexed = 0",
    """CREATE INDEX messages_comments
       ON messages(chat_id, comment_of_chat_id, comment_of_msg_id)
       WHERE comment_of_chat_id IS NOT NULL""",
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

_V6: tuple[str, ...] = (
    # what an extractor read out of a message's media, and how far the extraction pass got with
    # that message (the MEDIA_* states below). Both are the pass's alone to write: the message
    # upsert names neither column, so a re-store of a re-read message keeps what was extracted.
    "ALTER TABLE messages ADD COLUMN extracted_text TEXT",
    "ALTER TABLE messages ADD COLUMN media_state INTEGER NOT NULL DEFAULT 0",
    # reactions on the messages a unit holds, summed when it is cut and refreshed in place
    "ALTER TABLE units ADD COLUMN reactions INTEGER NOT NULL DEFAULT 0",
    # the extraction pass's work queue. The predicate carries `media_kind IS NOT NULL` as well as
    # the state: without it the index would cover every row of the table forever — text messages
    # sit at MEDIA_PENDING and never leave it — and the pending query would stay a scan.
    """CREATE INDEX messages_media_pending ON messages(chat_id, id)
       WHERE media_state = 0 AND media_kind IS NOT NULL""",
)

MEDIA_PENDING = 0
"""``messages.media_state``: media nothing has looked at yet — the extraction pass's queue."""
MEDIA_EXTRACTED = 1
"""Extraction ran; ``extracted_text`` may still be empty, an image holding no text."""
MEDIA_UNSUPPORTED = 2
"""No extractor is registered for this kind, so there is nothing to retry."""
MEDIA_FAILED = 3
"""Extraction was attempted and failed — a timeout, a corrupt file; retryable."""
MEDIA_SKIPPED = 4
"""Larger than ``media.max_download_mb``, so it was never downloaded."""
MEDIA_DISABLED = 5
"""The kind is switched off in config; re-queued to :data:`MEDIA_PENDING` when it comes back, so
a disabled kind drains once instead of being re-read on every pass."""

BASE_VERSION = 5
"""The version :data:`_V5` alone produces — the lowest number this build ever records.

It is an identity, not a count. Development builds before the first release walked a database up
through versions 1, 2, 3 and 4, and their ``schema_version = 1`` names a ``messages`` table with
neither ``indexed`` nor the ``comment_of_*`` columns. A number the old chain also wrote could not
tell such a file from a finished one, and :func:`migrate` would take it for one and leave the
rest of the code querying columns that are not there. Every version below this one therefore
belongs to that chain and is refused outright; :func:`migrate` upgrades only from a version this
build itself wrote."""

MIGRATIONS: dict[int, tuple[str, ...]] = {BASE_VERSION: _V5, 6: _V6}
"""The schema, keyed by the version each step brings a database to.

:data:`BASE_VERSION` builds it from nothing and only an empty file gets that step;
:data:`SCHEMA_VERSION` is the last of them, and a release that has to change the schema of a
database in the field appends a step above the base rather than editing one. There is no step
that transforms rows written by a development build — an index whose version this build did not
write is rebuilt from Telegram, see :func:`migrate`."""
SCHEMA_VERSION = max(MIGRATIONS)
_REBUILD_HINT = "delete index.db and run `grepogram sync` to build it again"

_EXTENSIONS_HINT = (
    "Reinstall under an interpreter that can: uv's own managed builds and Homebrew's python@3.12 "
    "can, python.org's macOS installer build (/usr/local/bin/python3.12, which is also what "
    "actions/setup-python installs) and Apple's system Python cannot. From a checkout, "
    "`uv tool install --managed-python --python 3.12 '.[dense]'` (or `--python "
    "/opt/homebrew/bin/python3.12`); without installing, `uv sync --managed-python`."
)
"""What to do about :class:`ExtensionsUnsupported`, verified against the builds it names.

The ``--python`` is not redundant: ``uv tool install`` reuses an environment whose interpreter
still satisfies the request, so ``--managed-python`` alone leaves a tool installed under the
wrong Python exactly as it is. ``uv sync`` re-creates the ``.venv`` on its own."""

_VEC_DIM_RE = re.compile(r"FLOAT\[(\d+)\]")

_ATTACHMENT_REPLACED = (
    "(excluded.media_kind IS NOT messages.media_kind "
    "OR excluded.media_filename IS NOT messages.media_filename)"
)
"""Whether a re-stored message carries a *different* attachment from the one already stored.

``IS NOT`` and not ``<>``: both columns are nullable, and a message that never had media has
``NULL`` on both sides of every sync. See :data:`_MESSAGE_UPSERT` for what it decides."""

_MESSAGE_UPSERT = f"""
    INSERT INTO messages(chat_id, msg_id, date, edit_date, from_id, from_name, reply_to_msg_id,
                         topic_id, comment_of_chat_id, comment_of_msg_id, fwd_from, text,
                         media_kind, media_filename, reactions_total)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chat_id, msg_id) DO UPDATE SET
        date = excluded.date,
        edit_date = excluded.edit_date,
        from_id = excluded.from_id,
        from_name = excluded.from_name,
        reply_to_msg_id = excluded.reply_to_msg_id,
        topic_id = COALESCE(excluded.topic_id, messages.topic_id),
        comment_of_chat_id = COALESCE(excluded.comment_of_chat_id, messages.comment_of_chat_id),
        comment_of_msg_id = COALESCE(excluded.comment_of_msg_id, messages.comment_of_msg_id),
        fwd_from = excluded.fwd_from,
        text = excluded.text,
        media_kind = excluded.media_kind,
        media_filename = excluded.media_filename,
        reactions_total = excluded.reactions_total,
        extracted_text = CASE WHEN {_ATTACHMENT_REPLACED}
            THEN NULL ELSE messages.extracted_text END,
        media_state = CASE WHEN {_ATTACHMENT_REPLACED}
            THEN {MEDIA_PENDING} ELSE messages.media_state END,
        indexed = 0
    RETURNING id"""
"""Store a message, keeping what only the extraction pass knows — unless the attachment changed.

``extracted_text`` and ``media_state`` are absent from the column list and are written by the
SET clause **only** when the message no longer carries the attachment they were read off, so
Telegram re-reading a message cannot undo the extraction and a message whose file was replaced
cannot keep the previous file's text. The ``COALESCE`` idiom the topic and comment columns use
cannot serve either half: it reads ``None`` as "not supplied", and ``media_state`` is ``NOT NULL
DEFAULT 0`` — a freshly mapped row carries :data:`MEDIA_PENDING` and would reset every extracted
message to pending on every sync.

"Changed" is ``media_kind`` or ``media_filename`` differing (:data:`_ATTACHMENT_REPLACED`), the
whole of what a stored row says about its attachment. Editing a **caption** changes neither, so
an edit costs no re-download — which is the point: ``edit_refetch`` re-reads the newest messages
of every chat on every sync, and keying this on ``edit_date`` or on ``text`` would re-queue and
re-download every extracted photo in the index for a typo fix. The cost of reading so little is
that a photo swapped for another photo is invisible here (neither column moves — Telegram names
no file for a photo), as is a document replaced by one of the same name; ``extract
--retry-failed`` does not reach those either, and re-syncing the chat from scratch is what
clears them. Resetting to :data:`MEDIA_PENDING` rather than to the state the row held puts the
row back at the top of the pass, where the offline half parks it again if the new kind has no
extractor or is switched off."""

_UNIT_INSERT = """
    INSERT INTO units(chat_id, topic_id, kind, msg_id_start, msg_id_end, msg_ids,
                      date_start, date_end, text, reactions, dirty, embedded_model)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    RETURNING id"""


class SchemaError(Exception):
    """The database schema cannot be brought to the shape this code expects."""


class VecDimMismatch(SchemaError):
    """``unit_vec`` already exists with a different embedding dimension."""


class ExtensionsUnsupported(Exception):
    """The running interpreter's ``sqlite3`` cannot load extensions, so sqlite-vec cannot load.

    CPython compiles :meth:`sqlite3.Connection.enable_load_extension` in only when it was
    configured with ``--enable-loadable-sqlite-extensions``, and sqlite-vec is a loadable
    extension: on an interpreter built without it no index can be opened at all. :func:`connect`
    raises this before it touches the file, in place of the bare ``AttributeError`` the missing
    method would otherwise raise from the middle of the connection setup.
    """

    def __init__(self) -> None:
        super().__init__(
            "this Python cannot load SQLite extensions, so sqlite-vec cannot be loaded: "
            f"{sys.executable} (Python {sys.version.split()[0]}) was built without "
            f"--enable-loadable-sqlite-extensions. {_EXTENSIONS_HINT}"
        )


class Connection(sqlite3.Connection):
    """A connection whose statements run one at a time across threads (see the module docstring).

    ``lock`` is the re-entrant lock every statement and every :func:`transaction` holds; a thread
    that holds it may keep executing (nested ``transaction()`` blocks join), any other waits.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        cursor = self.cursor(_Cursor)
        cursor.execute(sql, parameters)
        return cursor

    def executemany(self, sql: str, parameters: Iterable[Any], /) -> sqlite3.Cursor:
        cursor = self.cursor(_Cursor)
        cursor.executemany(sql, parameters)
        return cursor


class _Cursor(sqlite3.Cursor):
    """A cursor that fetches every row while it holds the connection's lock.

    ``sqlite3`` steps a statement lazily as rows are read, which is exactly what must not
    interleave with another thread's statement on the same connection; reading the rows up front
    leaves nothing in flight once the lock is released. The rows are then served from memory.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        self._rows: deque[Any] = deque()

    def execute(self, sql: str, parameters: Any = (), /) -> "_Cursor":
        with _lock_of(self.connection):
            super().execute(sql, parameters)
            self._rows = deque(super().fetchall())
        return self

    def executemany(self, sql: str, parameters: Iterable[Any], /) -> "_Cursor":
        with _lock_of(self.connection):
            super().executemany(sql, parameters)
            self._rows = deque()
        return self

    def fetchone(self) -> Any:
        return self._rows.popleft() if self._rows else None

    def fetchmany(self, size: int | None = None) -> list[Any]:
        count = self.arraysize if size is None else size
        return [self._rows.popleft() for _ in range(min(count, len(self._rows)))]

    def fetchall(self) -> list[Any]:
        rows = list(self._rows)
        self._rows.clear()
        return rows

    def __iter__(self) -> "_Cursor":
        return self

    def __next__(self) -> Any:
        if not self._rows:
            raise StopIteration
        return self._rows.popleft()


def _lock_of(conn: sqlite3.Connection) -> AbstractContextManager[Any]:
    return conn.lock if isinstance(conn, Connection) else nullcontext()


def _extensions_supported() -> bool:
    """Whether this interpreter's ``sqlite3`` was built with loadable-extension support.

    The method is compiled in or out as a whole, so its presence on the class is the capability
    (see :class:`ExtensionsUnsupported`); it is asked of the class so nothing has to be opened
    first, and so a test can take the capability away without a second interpreter.
    """
    return hasattr(sqlite3.Connection, "enable_load_extension")


def connect(target: Paths | str) -> Connection:
    """Open the index database and load sqlite-vec.

    ``target`` is the resolved :class:`Paths` (directories are created) or a raw database string
    such as ``":memory:"``. The connection may be shared between threads (see :class:`Connection`),
    waits up to five seconds on a locked database, enforces foreign keys and returns
    :class:`sqlite3.Row` rows.

    Raises :class:`ExtensionsUnsupported`, before creating or opening anything, when the
    interpreter cannot load extensions at all.
    """
    if not _extensions_supported():
        raise ExtensionsUnsupported()
    if isinstance(target, Paths):
        target.ensure_dirs()
        database = str(target.db_file)
    else:
        database = target
    conn = sqlite3.connect(
        database, check_same_thread=False, isolation_level=None, factory=Connection
    )
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
    """Run the block atomically, joining a transaction that is already open instead of nesting.

    The connection's lock is held for the whole block, so the open transaction a nested block
    joins is always this thread's own.
    """
    with _lock_of(conn):
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


def _meta_is_grepogram(conn: sqlite3.Connection) -> bool:
    """Whether ``meta`` carries the ``key`` and ``value`` columns :func:`get_meta` reads.

    Asking the table for its columns is what tells a file some other program wrote apart from a
    database that cannot be read at all: a locked or corrupt file raises out of the read itself
    and means something else entirely, so it is never classified here.
    """
    columns = {row[0] for row in conn.execute("SELECT name FROM pragma_table_info('meta')")}
    return {"key", "value"} <= columns


def schema_version(conn: sqlite3.Connection) -> int:
    """Version recorded in ``meta``; ``0`` for an empty database.

    A ``meta`` table this build cannot read a version out of — one of another program's making,
    or a value that is not a number — raises :class:`SchemaError` carrying the same rebuild
    instruction as every refusal in :func:`migrate`, instead of the bare ``sqlite3`` or ``int()``
    error that would reach the user as a traceback past the handlers in the CLI and the MCP
    server. A database that cannot be read at all fails as it always did.
    """
    if not has_table(conn, "meta"):
        return 0
    if not _meta_is_grepogram(conn):
        raise SchemaError(
            f"the meta table of this database is not the one grepogram writes; {_REBUILD_HINT}"
        )
    value = get_meta(conn, META_SCHEMA_VERSION)
    if not value:
        return 0
    try:
        return int(value)
    except ValueError as exc:
        raise SchemaError(
            f"database records {value!r} as its schema version, which is not a number; "
            f"{_REBUILD_HINT}"
        ) from exc


def _is_empty(conn: sqlite3.Connection) -> bool:
    """Whether the database holds no schema objects at all — what :func:`migrate` builds in."""
    return conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None


def _missing_steps(first: int) -> list[int]:
    """Versions from ``first`` up to :data:`SCHEMA_VERSION` that :data:`MIGRATIONS` holds no step
    for; empty when the chain reaches the head without a hole.

    Whether it does is a property of this module and not of any database, so both paths into
    :func:`_apply` ask before they apply: a step keyed with a gap must not stamp an empty database
    at a version an existing one is refused at.
    """
    return [version for version in range(first, SCHEMA_VERSION + 1) if version not in MIGRATIONS]


def _apply(conn: sqlite3.Connection, steps: Sequence[tuple[str, ...]]) -> int:
    """Run ``steps`` and record :data:`SCHEMA_VERSION` in one transaction, all of it or none."""
    with transaction(conn):
        for step in steps:
            for statement in step:
                conn.execute(statement)
        set_meta(conn, META_SCHEMA_VERSION, str(SCHEMA_VERSION))
    return SCHEMA_VERSION


def migrate(conn: sqlite3.Connection) -> int:
    """Bring a database to :data:`SCHEMA_VERSION`, or refuse it, and return the version in place.

    Every database lands in exactly one of these cases, none of them by falling through:

    * a file with no schema objects at all gets the whole schema and the version recorded;
    * one already at :data:`SCHEMA_VERSION` is used as it is;
    * one between :data:`BASE_VERSION` and :data:`SCHEMA_VERSION` that :data:`MIGRATIONS` holds
      every step for is walked up to it;
    * anything else raises :class:`SchemaError` telling the user to rebuild the index: tables
      carrying no recorded version, a version newer than this build knows, a version below
      :data:`BASE_VERSION` (every number the pre-release development chain wrote — a different
      schema behind numbers this build no longer uses), a version no chain of steps reaches, and
      a version :func:`schema_version` cannot read at all.

    Rows are never transformed on a guess: the index is derived from Telegram and a rebuild costs
    one sync. A gap in :data:`MIGRATIONS` itself is refused on both paths, the empty file's
    included: a mis-keyed step is a bug here, and an empty database stamped at the head while
    every existing one is turned away would hide it.
    """
    current = schema_version(conn)
    if current == SCHEMA_VERSION:
        return current
    if current == 0:
        if not _is_empty(conn):
            raise SchemaError(f"database records no schema version; {_REBUILD_HINT}")
        gap = _missing_steps(BASE_VERSION)
        if gap:
            raise SchemaError(
                "this grepogram holds no schema step for "
                + ", ".join(f"v{version}" for version in gap)
                + f": MIGRATIONS has to run from v{BASE_VERSION} to v{SCHEMA_VERSION} without a "
                "gap, and a mis-keyed step is a bug in grepogram, not in the database"
            )
        # one transaction for the schema and the stamp: a crash between them would leave a
        # fresh index with the full schema and no `unit_recipe`, which reads as a v0.1.1 index
        # and costs the first sync a full re-cut and re-embed of the units it just cut correctly
        with transaction(conn):
            version = _apply(
                conn, [MIGRATIONS[version] for version in range(BASE_VERSION, SCHEMA_VERSION + 1)]
            )
            _stamp_unit_recipe(conn)
        return version
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema v{current} is newer than this grepogram supports "
            f"(v{SCHEMA_VERSION}); {_REBUILD_HINT}"
        )
    if current < BASE_VERSION:
        raise SchemaError(
            f"database schema v{current} was written by a development build from before the "
            f"first release and is not the schema behind that number any more; {_REBUILD_HINT}"
        )
    pending = range(current + 1, SCHEMA_VERSION + 1)
    if _missing_steps(current + 1):
        raise SchemaError(
            f"database schema v{current} is not one this grepogram can upgrade to "
            f"v{SCHEMA_VERSION}; {_REBUILD_HINT}"
        )
    return _apply(conn, [MIGRATIONS[version] for version in pending])


def _stamp_unit_recipe(conn: sqlite3.Connection) -> None:
    """Record the unit recipe a database built from empty already satisfies.

    It holds no units, so nothing in it was cut by an older rule, and the decision cannot be
    left to the sync-time pass: by the time that runs, :func:`grepogram.sync.index_pending` has
    cut units for every chat the run fetched, so "the database holds no units" is never true and
    a brand-new index would re-cut everything it has just cut correctly. Without the stamp the
    first sync of a fresh install pays a full re-cut and re-embed of its own work. Its caller
    runs it inside the transaction that writes the schema, for the same reason: a stamp that can
    be lost on its own is a decision the code below can no longer make.

    :mod:`grepogram.units` imports this module, so the version is read inside the function.
    """
    from grepogram.units import RECIPE_VERSION

    set_unit_recipe(conn, RECIPE_VERSION)


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
    """Create ``unit_vec`` for ``dim``-sized embeddings; the table's DDL records the dimension.

    An existing table with the same dimension is kept as is; a different dimension raises
    :class:`VecDimMismatch`. With ``drop`` set every stored vector is discarded and the table is
    recreated whatever its dimension — the clean slate a re-embed starts from.
    """
    if dim <= 0:
        raise ValueError(f"embedding dimension must be positive, got {dim}")
    current = vec_dim(conn)
    if current == dim and not drop:
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


def has_vectors(conn: sqlite3.Connection) -> bool:
    """True when ``unit_vec`` exists and holds at least one vector."""
    if not has_vec_table(conn):
        return False
    return conn.execute(f"SELECT rowid FROM {VEC_TABLE} LIMIT 1").fetchone() is not None


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


def unit_recipe(conn: sqlite3.Connection) -> int | None:
    """The recipe the stored units were cut with, or ``None`` when none is recorded.

    ``None`` names a v0.1.1 index — units cut before the recipe existed — and never a fresh one:
    :func:`migrate` stamps a database it builds from empty, so "no recipe recorded" is a real
    mismatch and the whole re-cut is not silently skipped. A value that is not a number is read
    the same way, so a hand-edited marker costs one re-cut instead of a traceback.
    """
    value = get_meta(conn, META_UNIT_RECIPE)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def set_unit_recipe(conn: sqlite3.Connection, version: int) -> None:
    """Record that every stored unit was cut with recipe ``version``."""
    set_meta(conn, META_UNIT_RECIPE, str(version))


def recut_markers(conn: sqlite3.Connection) -> dict[int, str]:
    """Chat id → the recipe version that chat was last re-cut at, from ``meta.unit_recut:<id>``.

    The marker holds a value rather than being a presence flag: a run that dies between the last
    chat and the cleanup leaves markers behind, and a presence flag would then make the *next*
    bump skip exactly the chats that are already done. The comparison is on the string, so a
    marker this build cannot read means "not re-cut yet". A key whose suffix is not a chat id is
    skipped rather than raised over.

    The prefix match is ``substr``, not ``LIKE``: ``_`` is a single-character wildcard in
    ``LIKE`` and the prefix carries two of them.
    """
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE substr(key, 1, ?) = ?",
        (len(META_RECUT_PREFIX), META_RECUT_PREFIX),
    ).fetchall()
    markers: dict[int, str] = {}
    for row in rows:
        suffix = str(row["key"])[len(META_RECUT_PREFIX) :]
        try:
            markers[int(suffix)] = str(row["value"])
        except ValueError:
            continue
    return markers


def set_recut_marker(conn: sqlite3.Connection, chat_id: int, version: int) -> None:
    """Mark ``chat_id`` as re-cut at recipe ``version``."""
    set_meta(conn, f"{META_RECUT_PREFIX}{chat_id}", str(version))


def prune_cursor(conn: sqlite3.Connection, chat_id: int) -> int:
    """How far the deletion sweep got in ``chat_id``; ``0`` before it has ever run there.

    The value is a **Telegram** ``msg_id``, never a ``messages.id``. ``messages.id`` is an
    ``INTEGER PRIMARY KEY`` without ``AUTOINCREMENT``, so SQLite hands the rowids a sweep frees
    straight to the next insert: a rowid cursor would be unstable across exactly the operation
    that writes it. (``units.id`` *is* ``AUTOINCREMENT``, which is why unit ids are safe; nothing
    generalises from that.) A marker this build cannot read starts the chat again rather than
    raising.
    """
    value = get_meta(conn, f"{META_PRUNE_PREFIX}{chat_id}")
    if value is None:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def set_prune_cursor(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> None:
    """Record that the sweep of ``chat_id`` has asked Telegram about every stored id up to
    ``msg_id`` — written in the same transaction as the removals that id range earned."""
    set_meta(conn, f"{META_PRUNE_PREFIX}{chat_id}", str(msg_id))


def clear_prune_cursor(conn: sqlite3.Connection, chat_id: int) -> None:
    """Forget where the sweep of ``chat_id`` got to, so the next one starts at its oldest
    message; what a sweep that reached the end of a chat leaves behind."""
    with transaction(conn):
        conn.execute("DELETE FROM meta WHERE key = ?", (f"{META_PRUNE_PREFIX}{chat_id}",))


def clear_recut_markers(conn: sqlite3.Connection) -> None:
    """Drop every per-chat re-cut marker; the tidy-up once the recipe itself is recorded."""
    with transaction(conn):
        conn.execute(
            "DELETE FROM meta WHERE substr(key, 1, ?) = ?",
            (len(META_RECUT_PREFIX), META_RECUT_PREFIX),
        )


# --- chats -----------------------------------------------------------------------------------


def upsert_chat(conn: sqlite3.Connection, chat: ChatRow) -> ChatRow:
    """Insert ``chat`` or refresh the identity columns of the existing row; returns what is stored.

    Identity columns are ``type``, ``title``, ``username``, ``is_forum``, ``source_id`` and
    ``discussion_of`` — the last one only when the new row carries it, so a discussion chat that
    is resolved again as an ordinary source chat keeps its channel. Sync state (``last_msg_id``,
    ``last_sync_at``, ``unavailable``, ``migrated_to``) is written on insert only, so re-resolving
    a source never rewinds a synced chat; change it with :func:`set_chat_progress`,
    :func:`set_chat_unavailable` and :func:`set_chat_migrated`.
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
                   discussion_of = COALESCE(excluded.discussion_of, chats.discussion_of)""",
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
    """The discussion group linked to a channel (``discussion_of = channel_id``), if stored.

    There is at most one: the partial unique index on ``discussion_of`` says so, and
    :func:`set_discussion_chat` is the one way the link moves or goes away.
    """
    row = conn.execute("SELECT * FROM chats WHERE discussion_of = ?", (channel_id,)).fetchone()
    return None if row is None else _chat_row(row)


def set_discussion_chat(
    conn: sqlite3.Connection, channel_id: int, group_id: int | None
) -> list[int]:
    """Make ``group_id`` the discussion group of ``channel_id`` — ``None`` when it has none.

    Returns the chats that were the channel's discussion group and are not any more: Telegram
    unlinked the group, or the channel was given another one. Their rows keep their messages —
    they are a real group's real messages — and only stop counting as this channel's comments,
    which is what the caller rebuilds the channel's post threads for.

    This is the only way ``discussion_of`` is cleared: :func:`upsert_chat` COALESCEs the column
    so that re-resolving the group as a source chat of its own never drops the link, and
    :func:`delete_chat` clears the link of a group outliving its channel through here too. The
    old rows are cleared before the new one is written, so the unique index never sees two.
    """
    with transaction(conn):
        dropped = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM chats WHERE discussion_of = ? AND id IS NOT ?",
                (channel_id, group_id),
            ).fetchall()
        ]
        conn.executemany(
            "UPDATE chats SET discussion_of = NULL WHERE id = ?", [(row_id,) for row_id in dropped]
        )
        if group_id is not None:
            conn.execute("UPDATE chats SET discussion_of = ? WHERE id = ?", (channel_id, group_id))
    return dropped


def list_chats(conn: sqlite3.Connection, source_id: str | None = None) -> list[ChatRow]:
    """All chats (or those pulled in by one source) ordered by id."""
    if source_id is None:
        rows = conn.execute("SELECT * FROM chats ORDER BY id")
    else:
        rows = conn.execute("SELECT * FROM chats WHERE source_id = ? ORDER BY id", (source_id,))
    return [_chat_row(row) for row in rows]


def last_sync_at(conn: sqlite3.Connection) -> int | None:
    """When the most recently completed chat sync finished, or ``None`` before the first one."""
    row = conn.execute("SELECT max(last_sync_at) AS latest FROM chats").fetchone()
    return None if row["latest"] is None else int(row["latest"])


def last_sync_run(conn: sqlite3.Connection) -> int | None:
    """When the last sync run ended (``meta.last_sync_run``), whether or not it finished a chat."""
    value = get_meta(conn, META_LAST_SYNC_RUN)
    return None if value is None else int(value)


def set_last_sync_run(conn: sqlite3.Connection, when: int) -> None:
    set_meta(conn, META_LAST_SYNC_RUN, str(when))


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
    """Remove a stored chat with its messages and units, including their FTS and vector rows.

    A chat this index does not hold is a no-op: deleting an id that was never stored must not
    reach the rows of the chats that are, and clearing ``discussion_of`` on a live group because
    the id matches the channel it names would be exactly that.

    The virtual-table rows go first, addressed by rowid (a direct lookup, not a scan); deleting
    the ``chats`` row then cascades to ``messages`` and ``units``.

    Units of *other* chats can quote this one, and they go in the same transaction: a channel's
    post threads carry the comments its discussion group holds, so deleting a group drops the
    post threads its comments fed and flags those posts ``indexed = 0``
    (:func:`drop_comment_units`). The flag on its own would not do — the channel may never
    resolve again, and until it does the index would answer with rows that are gone. The mirror
    case is a channel deleted while its group lives on under a source of its own: the group keeps
    every message it holds, and only the link goes — with the comment mapping under it, which
    :func:`drop_comment_units` clears for the groups the delete unlinks.

    The chat's two per-chat ``meta`` markers go too, because nothing else would ever remove them
    and a chat id can come back — re-added after a ``sources rm``, or re-listed by a folder after
    a ``sources prune``. A surviving ``prune_sweep:`` cursor would make
    :func:`grepogram.sync.prune_deleted` resume the fresh history from the old chat's high-water
    mark, report the chat done and never ask about anything below it; a surviving ``unit_recut:``
    marker would make the next recipe bump skip the chat outright.
    """
    with transaction(conn):
        chat = get_chat(conn, chat_id)
        if chat is None:
            return
        drop_comment_units(conn, chat.discussion_of, chat.id)
        for unlinked in set_discussion_chat(conn, chat_id, None):
            drop_comment_units(conn, chat_id, unlinked)
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
        conn.execute(
            "DELETE FROM meta WHERE key IN (?, ?)",
            (f"{META_PRUNE_PREFIX}{chat_id}", f"{META_RECUT_PREFIX}{chat_id}"),
        )
        conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


def drop_comment_units(conn: sqlite3.Connection, channel_id: int | None, group_id: int) -> int:
    """Undo what a discussion group's comments left in a channel's post threads, and stop the
    rows being that channel's comments at all.

    Returns how many of the channel's stored posts were flagged for a rebuild. This is the one
    answer to "which units quote this group": a channel's post threads are the only units built
    from another chat's rows (:func:`grepogram.units.build_posts`), and they hang under the posts
    the group's rows name in ``comment_of_msg_id`` — so only a broadcast channel, the one shape
    that has them, is touched. The threads themselves name no comment: each lists only the post
    in ``msg_ids`` (comment ids live in the group's id space), so nothing in the rows ties a
    thread back to the group whose text it carries. The link is that tie, and it is why every
    place the link goes cleans up under it right there — :func:`delete_chat` when the group is
    deleted, :func:`grepogram.sync._drop_comment_units` when a channel is unlinked from it or
    another channel takes it over. Flagging the posts and leaving the threads for the next
    rebuild would not do: delete the group in between and no link is left to find them by.

    The stale threads go with their ``unit_fts`` and vector rows, and the posts are flagged
    ``indexed = 0`` so the next rebuild cuts them again — without those comments, or with the
    ones the new group holds. The mapping that made them comments goes too
    (:func:`_clear_comment_mapping`), or the next channel to hold the group would inherit them;
    it goes for every comment of this channel, whether or not the post it hangs under is still
    stored, so a post the channel has since dropped leaves nothing behind either. Nothing to do
    when the channel is not stored, when ``channel_id`` is ``None`` (a group no channel links) or
    when the chat is not a broadcast channel: no other shape holds another chat's rows.
    """
    channel = None if channel_id is None else get_chat(conn, channel_id)
    if channel is None or not channel.is_broadcast:
        return 0
    post_ids = stored_comment_post_ids(conn, group_id, channel.id)
    if not post_ids:
        return 0
    with transaction(conn):
        stale = threads_touching(conn, channel.id, post_ids)
        _drop_units_and_index(conn, [unit.id for unit in stale if unit.id is not None])
        posts = get_messages_by_msg_id(conn, channel.id, post_ids)
        mark_unindexed(conn, [post.id for post in posts.values() if post.id is not None])
        _clear_comment_mapping(conn, group_id, channel.id)
        return len(posts)


def _clear_comment_mapping(conn: sqlite3.Connection, group_id: int, channel_id: int) -> None:
    """Un-designate every comment ``group_id`` holds on ``channel_id``; they stay group messages.

    Only the comment relation is cleared, and only this channel's: a forum topic of the group
    carries none, and a comment on another channel's post names that channel, so neither is
    touched. Clearing it where the link goes is what keeps "they stop being the channel's
    comments" true of the rows and not only of the units built from them —
    :func:`grepogram.units.build_posts` and :func:`grepogram.search.thread` would otherwise hand
    them to whichever channel links the group next.

    Nothing derived from a group's rows is rebuilt, because nothing derived from them reads the
    comment relation: its windows are cut per forum topic (:func:`grepogram.units.window_topic`)
    and its threads follow ``reply_to_msg_id``, both untouched here. So every cleared comment
    stays in the window it was already in and is searchable through it the moment this commits,
    with no rebuild owed and nothing to strand until a later sync.
    """
    conn.execute(
        "UPDATE messages SET comment_of_chat_id = NULL, comment_of_msg_id = NULL "
        "WHERE chat_id = ? AND comment_of_chat_id = ?",
        (group_id, channel_id),
    )


def _drop_units_and_index(conn: sqlite3.Connection, ids: Sequence[int]) -> None:
    """Delete these units with their ``unit_fts`` and vector rows, every one by rowid."""
    if not ids:
        return
    rows = [(unit_id,) for unit_id in ids]
    with transaction(conn):
        conn.executemany("DELETE FROM unit_fts WHERE rowid = ?", rows)
        if has_vec_table(conn):
            conn.executemany(f"DELETE FROM {VEC_TABLE} WHERE rowid = ?", rows)
        delete_units(conn, ids)


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
    rowid. A ``topic_id`` and a ``comment_of_*`` pair already stored survive a row without them:
    a channel's comment fetch stores a discussion group's message as a comment on one of its
    posts, and the group's own history sync stores the same message with no comment relation at
    all — either may arrive first. ``extracted_text`` and ``media_state`` are not written here at
    all (see :data:`_MESSAGE_UPSERT`): they belong to the extraction pass, which owns them through
    its own writers. Every row written, new or updated, is flagged ``indexed = 0`` until a rebuild
    covers it (:func:`mark_indexed`). The chat row must exist (foreign key).
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
                    message.comment_of_chat_id,
                    message.comment_of_msg_id,
                    message.fwd_from,
                    message.text,
                    message.media_kind,
                    message.media_filename,
                    message.reactions_total,
                ),
            ).fetchone()
            ids.append(int(row["id"]))
    return ids


def unindexed_message_ids(conn: sqlite3.Connection, chat_id: int) -> list[int]:
    """``messages.id`` of every row of ``chat_id`` whose units and ``msg_fts`` row are behind.

    Rows are flagged by :func:`upsert_messages` and :func:`mark_unindexed` and cleared by
    :func:`mark_indexed` once :func:`grepogram.sync.on_chat_synced` has rebuilt them; a partial
    index keeps the query cheap while — the normal case — nothing is pending.
    """
    rows = conn.execute(
        "SELECT id FROM messages WHERE chat_id = ? AND indexed = 0 ORDER BY id", (chat_id,)
    )
    return [int(row["id"]) for row in rows]


def chats_with_unindexed(conn: sqlite3.Connection) -> list[int]:
    """``chat_id`` of every chat holding rows whose units and ``msg_fts`` rows are behind.

    The same partial index :func:`unindexed_message_ids` reads answers this one, so it costs
    nothing while — the normal case — nothing is pending. The flag is where the work is, whatever
    put it there: a chat no source lists any more, a discussion group a channel was unlinked
    from, is reachable through this and through nothing else
    (:func:`grepogram.sync.index_stranded`).
    """
    rows = conn.execute(
        "SELECT DISTINCT chat_id FROM messages WHERE indexed = 0 ORDER BY chat_id"
    ).fetchall()
    return [int(row["chat_id"]) for row in rows]


def mark_indexed(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """Record that the units and ``msg_fts`` rows of these ``messages.id`` are up to date."""
    with transaction(conn):
        for chunk in _chunks(ids):
            conn.execute(f"UPDATE messages SET indexed = 1 WHERE id IN ({_marks(chunk)})", chunk)


def mark_unindexed(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """Flag these ``messages.id`` for the next rebuild without changing the rows themselves —
    a channel post whose comment thread grew, whose own text did not change."""
    with transaction(conn):
        for chunk in _chunks(ids):
            conn.execute(f"UPDATE messages SET indexed = 0 WHERE id IN ({_marks(chunk)})", chunk)


def delete_messages(conn: sqlite3.Connection, chat_id: int, msg_ids: Iterable[int]) -> int:
    """Remove these **Telegram message ids** of ``chat_id`` with their ``msg_fts`` rows.

    What a message deleted in Telegram costs the index. The ids are the ``msg_id`` space, the one
    a sync compares against what Telegram returned, not the ``messages.id`` rowids
    :func:`mark_indexed` and friends take. The FTS rows go first, addressed by the rowid they are
    keyed on (:func:`grepogram.index.index_messages` writes them under ``messages.id``), because
    an ``fts5`` row is only reachable through that rowid and deleting the message would leave it
    behind for the next insert to collide with.

    The units these messages were part of are **not** touched here: they are cut again by
    :func:`grepogram.units.invalidate_units_for`, which the caller runs in this same transaction
    with the rows it read *before* the delete — the topic a window is scoped by lives on a row
    that no longer exists once this has run. Returns how many message rows went.
    """
    removed = 0
    with transaction(conn):
        for chunk in _chunks(msg_ids):
            conn.execute(
                f"DELETE FROM msg_fts WHERE rowid IN (SELECT id FROM messages "
                f"WHERE chat_id = ? AND msg_id IN ({_marks(chunk)}))",
                [chat_id, *chunk],
            )
            cursor = conn.execute(
                f"DELETE FROM messages WHERE chat_id = ? AND msg_id IN ({_marks(chunk)})",
                [chat_id, *chunk],
            )
            removed += max(cursor.rowcount, 0)
    return removed


def message_ids_after(
    conn: sqlite3.Connection, chat_id: int, after_msg_id: int, limit: int
) -> list[int]:
    """The next ``limit`` stored ``msg_id``s of ``chat_id`` above ``after_msg_id``, ascending.

    One page of the deletion sweep, which walks a chat oldest first and asks Telegram about the
    ids it reads here (:func:`grepogram.sync.prune_deleted`). The ``(chat_id, msg_id)`` unique
    index answers it, so a page costs the same on a chat of ten messages and one of a hundred
    thousand.
    """
    rows = conn.execute(
        "SELECT msg_id FROM messages WHERE chat_id = ? AND msg_id > ? ORDER BY msg_id LIMIT ?",
        (chat_id, after_msg_id, limit),
    ).fetchall()
    return [int(row["msg_id"]) for row in rows]


# --- media extraction ------------------------------------------------------------------------


def messages_pending_media(conn: sqlite3.Connection, limit: int, chat_id: int) -> list[MessageRow]:
    """One chat's extraction queue: its rows whose media nothing has looked at yet, oldest first.

    Always one chat, never the whole index: a batch is re-fetched through a single
    ``client.get_messages(chat_id, ids=[…])`` and cannot mix chats, so a whole-index page would
    be a queue no caller could use. :func:`chats_with_pending_media` is what says which chats to
    ask for. The predicate is spelled the way ``count_pending_media`` is, :data:`MEDIA_PENDING`
    inlined rather than bound, because SQLite only uses a partial index when the query's
    ``WHERE`` provably implies the index's own — a parameter proves nothing at prepare time.
    """
    rows = conn.execute(
        f"SELECT * FROM messages WHERE media_state = {MEDIA_PENDING} "
        "AND media_kind IS NOT NULL AND chat_id = ? ORDER BY id LIMIT ?",
        (chat_id, limit),
    )
    return [_message_row(row) for row in rows]


def chats_with_pending_media(conn: sqlite3.Connection) -> list[int]:
    """``chat_id`` of every chat still holding media the extraction pass has not looked at."""
    rows = conn.execute(
        f"SELECT DISTINCT chat_id FROM messages WHERE media_state = {MEDIA_PENDING} "
        "AND media_kind IS NOT NULL ORDER BY chat_id"
    ).fetchall()
    return [int(row["chat_id"]) for row in rows]


def count_pending_media(conn: sqlite3.Connection, chat_ids: Sequence[int] | None = None) -> int:
    """How many rows are left in the extraction queue; ``chat_ids`` scopes it to those chats.

    The scoped figure is what ``grepogram extract`` reports as ``remaining``, over the chats the
    pass can actually re-fetch (:func:`grepogram.media._fetchable_chats`). A row in an imported
    or unavailable chat never leaves :data:`MEDIA_PENDING`, so the index-wide count would tell
    the user to "run extract again" for work no run can ever do, and a script looping until it
    reaches zero would never stop. An empty ``chat_ids`` is an empty scope, not the whole index.
    """
    if chat_ids is None:
        scope: str = ""
        params: tuple[int, ...] = ()
    elif chat_ids:
        scope = f" AND chat_id IN ({','.join('?' * len(chat_ids))})"
        params = tuple(chat_ids)
    else:
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM messages WHERE media_state = {MEDIA_PENDING} "
        f"AND media_kind IS NOT NULL{scope}",
        params,
    ).fetchone()
    return int(row["n"])


def set_media_text(conn: sqlite3.Connection, row_id: int, text: str) -> None:
    """Store what an extractor read out of one message's media and flag the row for a rebuild.

    The one writer here that touches ``indexed``: this text is rendered into the message's line
    (``units.render_line``) and indexed in ``msg_fts``
    (``index.message_index_text``), so both are behind until the row is rebuilt. Its twin
    :func:`set_media_state` writes the state byte alone, because every other transition changes
    no rendered text at all.

    The flag is meant to be short-lived: ``media._recut`` does that rebuild and calls
    :func:`mark_indexed` in the same transaction. What is left flagged afterwards is only what
    that rebuild could not cover, which is what :func:`grepogram.sync.index_stranded` is for — a
    flag carried across a whole extraction run would be a whole-index backlog for the
    *unbudgeted* rebuild loops a later sync ends with.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE messages SET extracted_text = ?, media_state = ?, indexed = 0 WHERE id = ?",
            (text, MEDIA_EXTRACTED, row_id),
        )


def set_media_state(conn: sqlite3.Connection, ids: Iterable[int], state: int) -> None:
    """Record how far the extraction pass got with these ``messages.id`` — and nothing else.

    ``indexed`` is deliberately left alone. None of the states this writes (unsupported, failed,
    skipped, disabled) changes a rendered line, and flagging would hand the deferred, unbudgeted
    ``index_pending`` loop :func:`grepogram.sync._sync_chats` ends with a whole-index backlog to
    rebuild and re-embed — the next 20-second auto-sync inside a ``search`` would drain it in
    full. :func:`set_media_text` is the writer for the one transition that does change a line.
    """
    with transaction(conn):
        for chunk in _chunks(ids):
            conn.execute(
                f"UPDATE messages SET media_state = ? WHERE id IN ({_marks(chunk)})",
                [state, *chunk],
            )


def move_media_state(conn: sqlite3.Connection, kinds: Collection[str], *, frm: int, to: int) -> int:
    """Move every message of these media kinds from one ``media_state`` to another; how many moved.

    The offline half of the extraction pass, and the reason it is offline: which kinds have an
    extractor and which are switched off in the config depends on the stored ``media_kind``
    alone, so tens of thousands of rows are parked without a single Telegram request. Like
    :func:`set_media_state` it never touches ``indexed``, for the same reason and more so — most
    kinds (``video``, ``sticker``, ``audio``, ``webpage``, ``poll``, ``contact``, ``location``,
    ``other`` and, until v0.3.0, ``voice`` and ``video_note``) have no extractor at all.
    """
    listed = list(dict.fromkeys(kinds))
    if not listed:
        return 0
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE messages SET media_state = ? WHERE media_state = ? "
            f"AND media_kind IN ({_marks(listed)})",
            [to, frm, *listed],
        )
        return int(cursor.rowcount)


def park_unreadable_documents(conn: sqlite3.Connection, suffixes: Collection[str]) -> int:
    """Park pending ``document`` rows whose filename ends in none of ``suffixes``; how many moved.

    ``document`` is ``sync.document_kind``'s fallback, so a ``.xlsx``, a ``.zip`` or an ``.apk``
    carries the one kind that *does* have an extractor — and the dispatcher that would refuse it
    only sees the file once it is downloaded. ``messages.media_filename`` decides the same thing
    with no request at all, which is what the whole offline half is for
    (:func:`move_media_state`), and the state is ``MEDIA_UNSUPPORTED`` rather than
    ``MEDIA_FAILED`` because no retry can change it — only a build that reads more formats can,
    and ``extract --retry-failed`` re-queues both.

    A row with no filename is left where it is: nothing about it can be decided from here.
    ``indexed`` is untouched, exactly as in :func:`move_media_state`.
    """
    listed = [suffix.lower() for suffix in dict.fromkeys(suffixes)]
    if not listed:
        return 0
    clause = " AND ".join("lower(media_filename) NOT LIKE ?" for _ in listed)
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE messages SET media_state = {MEDIA_UNSUPPORTED} "
            f"WHERE media_state = {MEDIA_PENDING} AND media_kind = 'document' "
            f"AND media_filename IS NOT NULL AND media_filename <> '' AND {clause}",
            [f"%{suffix}" for suffix in listed],
        )
        return int(cursor.rowcount)


def get_messages(
    conn: sqlite3.Connection, chat_id: int, since_msg_id: int | None = None
) -> list[MessageRow]:
    """Messages of a chat in ``msg_id`` order, from ``since_msg_id`` (inclusive)."""
    sql = "SELECT * FROM messages WHERE chat_id = ?"
    params: list[int] = [chat_id]
    if since_msg_id is not None:
        sql += " AND msg_id >= ?"
        params.append(since_msg_id)
    sql += " ORDER BY msg_id"
    return [_message_row(row) for row in conn.execute(sql, params)]


def get_comment_messages(
    conn: sqlite3.Connection, group_id: int, channel_id: int, post_ids: Iterable[int]
) -> dict[int, list[MessageRow]]:
    """The comments ``group_id`` holds on those posts of ``channel_id``, grouped by post id and
    each group in ``msg_id`` order.

    One query per :data:`IN_BATCH` posts rather than one per post — a channel rebuild reads the
    comments of every post this way. Posts without comments are absent from the result.

    The channel is part of the key, not implied by the group: a discussion group can be a forum
    and can have held another channel's comments before, and a post id is only a number until
    something says whose id space it comes from. ``comment_of_chat_id`` says it, so a forum topic
    of the group and a comment on some other channel's post of the same number are both invisible
    here.
    """
    grouped: dict[int, list[MessageRow]] = {}
    for chunk in _chunks(post_ids):
        rows = conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND comment_of_chat_id = ? "
            f"AND comment_of_msg_id IN ({_marks(chunk)}) ORDER BY msg_id",
            [group_id, channel_id, *chunk],
        )
        for row in rows:
            grouped.setdefault(int(row["comment_of_msg_id"]), []).append(_message_row(row))
    return grouped


def stored_comment_post_ids(conn: sqlite3.Connection, group_id: int, channel_id: int) -> list[int]:
    """The distinct posts of ``channel_id`` that ``group_id`` holds comments on, ascending.

    This is what a channel that is losing that group rebuilds its post threads from. A forum
    topic of the group is not one of these however its root is numbered, and neither is a
    comment left over from a channel that held the group before.
    """
    rows = conn.execute(
        "SELECT DISTINCT comment_of_msg_id FROM messages "
        "WHERE chat_id = ? AND comment_of_chat_id = ? AND comment_of_msg_id IS NOT NULL "
        "ORDER BY comment_of_msg_id",
        (group_id, channel_id),
    ).fetchall()
    return [int(row["comment_of_msg_id"]) for row in rows]


def count_comment_messages(
    conn: sqlite3.Connection, group_id: int, channel_id: int, post_ids: Iterable[int]
) -> dict[int, int]:
    """Comments stored per post of ``channel_id``; posts without comments are absent.

    A channel with comments compares this with the reply counts Telegram reports on its posts to
    find the threads that grew since they were stored.
    """
    counts: dict[int, int] = {}
    for chunk in _chunks(post_ids):
        rows = conn.execute(
            "SELECT comment_of_msg_id, count(*) AS n FROM messages "
            "WHERE chat_id = ? AND comment_of_chat_id = ? "
            f"AND comment_of_msg_id IN ({_marks(chunk)}) GROUP BY comment_of_msg_id",
            [group_id, channel_id, *chunk],
        )
        for row in rows:
            counts[int(row["comment_of_msg_id"])] = int(row["n"])
    return counts


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


def get_descendants(conn: sqlite3.Connection, chat_id: int, root_id: int) -> list[MessageRow]:
    """Every reply below ``root_id`` in ``chat_id``, breadth-first over stored replies, each
    message once even when replies form a cycle; the root itself is not included."""
    seen = {root_id}
    frontier = [root_id]
    found: list[MessageRow] = []
    while frontier:
        level = [
            reply for reply in get_replies(conn, chat_id, frontier) if reply.msg_id not in seen
        ]
        seen.update(reply.msg_id for reply in level)
        found += level
        frontier = [reply.msg_id for reply in level]
    return found


def get_thread_messages(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> list[MessageRow]:
    """The reply thread holding ``msg_id``: its root and every reply below the root, in
    ``(date, msg_id)`` order; ``[]`` when the message is not stored.

    The root is found by walking ``reply_to_msg_id`` upwards until a message replies to nothing,
    to itself, to something not stored or to a message already passed (a reply cycle). A
    message that neither replies nor is replied to is a thread of one.
    """
    msg = get_message(conn, chat_id, msg_id)
    if msg is None:
        return []
    root = _thread_root(conn, msg)
    thread = [root, *get_descendants(conn, chat_id, root.msg_id)]
    return sorted(thread, key=lambda m: (m.date, m.msg_id))


def _thread_root(conn: sqlite3.Connection, msg: MessageRow) -> MessageRow:
    seen = {msg.msg_id}
    while msg.reply_to_msg_id is not None and msg.reply_to_msg_id not in seen:
        parent = get_message(conn, msg.chat_id, msg.reply_to_msg_id)
        if parent is None:
            break
        seen.add(parent.msg_id)
        msg = parent
    return msg


def get_context_messages(
    conn: sqlite3.Connection, chat_id: int, msg_id: int, before: int, after: int
) -> list[MessageRow]:
    """``msg_id`` with up to ``before`` stored messages preceding and ``after`` following it in
    ``msg_id`` order, all sharing its ``topic_id`` (``topic_id IS`` the message's own, so one
    forum topic never bleeds into a neighbour); ``[]`` when the message is not stored. Negative
    counts are a ``ValueError``.

    Outside a forum the column is not always NULL — Telegram sets ``reply_to_top_id`` for legacy
    message threads too — so the context of such a message is bounded to that thread rather than
    to the whole chat. That is deliberate: it is the conversation the message sits in. Windowing
    ignores the same column outside a forum (:func:`grepogram.units.window_topic`), so the
    message is still found by a linear window.

    A discussion group's comments are not a topic: they are that group's own linear
    conversation, cut into the same windows as everything else it holds, so the neighbours of a
    comment are the group's — its comments on other posts included, and the post's own thread is
    what :func:`grepogram.search.thread` is for.
    """
    if before < 0 or after < 0:
        raise ValueError(f"before and after must not be negative, got {before} and {after}")
    msg = get_message(conn, chat_id, msg_id)
    if msg is None:
        return []
    preceding = conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND topic_id IS ? AND msg_id < ? "
        "ORDER BY msg_id DESC LIMIT ?",
        (chat_id, msg.topic_id, msg_id, before),
    ).fetchall()
    following = conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND topic_id IS ? AND msg_id > ? "
        "ORDER BY msg_id ASC LIMIT ?",
        (chat_id, msg.topic_id, msg_id, after),
    ).fetchall()
    return [
        *(_message_row(row) for row in reversed(preceding)),
        msg,
        *(_message_row(row) for row in following),
    ]


def message_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """Stored messages per chat id; chats without messages are absent."""
    rows = conn.execute("SELECT chat_id, COUNT(*) AS n FROM messages GROUP BY chat_id")
    return {int(row["chat_id"]): int(row["n"]) for row in rows}


# --- units -----------------------------------------------------------------------------------


def get_units(conn: sqlite3.Connection, chat_id: int) -> list[UnitRow]:
    """Units of a chat in id order."""
    rows = conn.execute("SELECT * FROM units WHERE chat_id = ? ORDER BY id", (chat_id,))
    return [_unit_row(row) for row in rows]


def get_units_by_ids(conn: sqlite3.Connection, ids: Iterable[int]) -> list[UnitRow]:
    """Units by id (any chat) in id order; ids that are not stored are skipped."""
    return _units_by_chunks(conn, "SELECT * FROM units WHERE id IN ({marks})", [], ids)


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
                    unit.reactions,
                    int(unit.dirty),
                    unit.embedded_model,
                ),
            ).fetchone()
            ids.append(int(row["id"]))
    return ids


_REFRESH_REACTIONS = """
    UPDATE units SET reactions = COALESCE((
        SELECT sum(messages.reactions_total) FROM json_each(units.msg_ids)
        JOIN messages ON messages.chat_id = units.chat_id
                     AND messages.msg_id = json_each.value), 0)
    WHERE chat_id = ? AND EXISTS (
        SELECT 1 FROM json_each(units.msg_ids) WHERE json_each.value IN ({marks}))"""


def refresh_unit_reactions(conn: sqlite3.Connection, chat_id: int, msg_ids: Iterable[int]) -> int:
    """Recompute ``units.reactions`` for the units of ``chat_id`` holding any of ``msg_ids``.

    ``msg_ids`` are **Telegram message ids** — the space ``units.msg_ids`` stores
    (:func:`grepogram.units._unit`). Every caller on the ``edit_refetch`` path carries
    ``messages.id`` rowids instead (:meth:`grepogram.sync._Run.store` returns them), and in a
    fixture chat both spaces start at 1 and coincide, so a caller that hands over rowids passes
    its tests and refreshes nothing — or the wrong units — against real history. Convert first.

    This is a direct ``UPDATE`` rather than anything the unit rebuild does, and it has to be:
    reactions are not part of :func:`grepogram.units._content_key`, so a rebuild that re-cuts an
    identical unit keeps the stored row, and a closed window is never re-cut at all
    (:func:`grepogram.units._recut_start`). The total is summed over the messages the unit lists,
    within the unit's own chat — a channel's post thread therefore reflects the post alone, its
    comments' reactions being carried by the discussion group's own window units. Nothing here
    touches ``text``, so no unit is flagged ``dirty``: a reaction changes the ranking, not the
    embedding. Returns how many unit rows the update covered.
    """
    updated = 0
    with transaction(conn):
        for chunk in _chunks(msg_ids):
            cursor = conn.execute(_REFRESH_REACTIONS.format(marks=_marks(chunk)), [chat_id, *chunk])
            updated += max(cursor.rowcount, 0)
    return updated


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
        f"SELECT * FROM units WHERE {_WINDOW_SCOPE} ORDER BY msg_id_end DESC, id DESC LIMIT 1",
        (chat_id, topic_id),
    ).fetchone()
    return None if row is None else _unit_row(row)


def windowed_msg_ids(
    conn: sqlite3.Connection, chat_id: int, topic_id: int | None, msg_ids: Iterable[int]
) -> set[int]:
    """Those of ``msg_ids`` that a window of ``(chat, topic)`` lists in its ``msg_ids``.

    Membership, not range: a window cut while some ids in its span were not stored yet reaches
    across them without holding them, and those are exactly the messages a rebuild must still
    reach. Only windows whose range overlaps the ids are expanded.
    """
    found: set[int] = set()
    for chunk in _chunks(msg_ids):
        rows = conn.execute(
            "SELECT json_each.value AS msg_id FROM units, json_each(units.msg_ids) "
            "WHERE units.chat_id = ? AND units.kind = 'window' AND units.topic_id IS ? "
            "AND units.msg_id_start <= ? AND units.msg_id_end >= ? "
            f"AND json_each.value IN ({_marks(chunk)})",
            [chat_id, topic_id, max(chunk), min(chunk), *chunk],
        )
        found.update(int(row["msg_id"]) for row in rows)
    return found


def window_before(
    conn: sqlite3.Connection, chat_id: int, topic_id: int | None, msg_id: int
) -> UnitRow | None:
    """The window of ``(chat, topic)`` that starts last among those starting at or before
    ``msg_id``; ``None`` when ``msg_id`` precedes every window."""
    row = conn.execute(
        f"SELECT * FROM units WHERE {_WINDOW_SCOPE} "
        "AND msg_id_start <= ? ORDER BY msg_id_start DESC, id DESC LIMIT 1",
        (chat_id, topic_id, msg_id),
    ).fetchone()
    return None if row is None else _unit_row(row)


def windows_from(
    conn: sqlite3.Connection, chat_id: int, topic_id: int | None, msg_id: int
) -> list[UnitRow]:
    """Windows of ``(chat, topic)`` that reach ``msg_id`` or beyond (``msg_id_end >= msg_id``),
    in ``msg_id_start`` order — the ones a recut starting at ``msg_id`` replaces."""
    rows = conn.execute(
        f"SELECT * FROM units WHERE {_WINDOW_SCOPE} AND msg_id_end >= ? ORDER BY msg_id_start, id",
        (chat_id, topic_id, msg_id),
    )
    return [_unit_row(row) for row in rows]


def containing_unit(
    conn: sqlite3.Connection, chat_id: int, msg_id: int, topic_id: int | None
) -> UnitRow | None:
    """The window holding ``msg_id`` in ``(chat, topic)``, or a channel's ``post`` unit for it.

    A window is found by its ``msg_id`` range within the topic — the ranges of a forum's topics
    interleave, so the topic is part of the lookup; outside forums windows carry no topic and
    the caller passes ``None`` (:func:`grepogram.units.window_topic`). Channels have no windows,
    so a post's own unit stands in. ``None`` while the message is in no unit yet.
    """
    row = conn.execute(
        "SELECT * FROM units WHERE kind = 'window' AND chat_id = ? "
        "AND ? BETWEEN msg_id_start AND msg_id_end AND topic_id IS ? "
        "ORDER BY msg_id_start DESC LIMIT 1",
        (chat_id, msg_id, topic_id),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM units WHERE kind = 'post' AND chat_id = ? AND msg_id_start = ? "
            "ORDER BY id LIMIT 1",
            (chat_id, msg_id),
        ).fetchone()
    return None if row is None else _unit_row(row)


def threads_touching(
    conn: sqlite3.Connection, chat_id: int, msg_ids: Iterable[int]
) -> list[UnitRow]:
    """Thread units of a chat whose ``msg_ids`` include any of ``msg_ids``, in id order."""
    return _units_by_chunks(
        conn,
        "SELECT units.* FROM units WHERE chat_id = ? AND kind = 'thread' AND EXISTS ("
        "SELECT 1 FROM json_each(units.msg_ids) WHERE json_each.value IN ({marks}))",
        [chat_id],
        msg_ids,
    )


def post_units(conn: sqlite3.Connection, chat_id: int, post_ids: Iterable[int]) -> list[UnitRow]:
    """The ``post`` units of a channel for the given post ids, in id order."""
    return _units_by_chunks(
        conn,
        "SELECT * FROM units WHERE chat_id = ? AND kind = 'post' AND msg_id_start IN ({marks})",
        [chat_id],
        post_ids,
    )


def get_dirty_units(conn: sqlite3.Connection, limit: int, after_id: int = 0) -> list[UnitRow]:
    """Up to ``limit`` units flagged for embedding with ``id > after_id``, in id order.

    ``after_id`` lets a caller walk the flagged units slice by slice without rescanning the
    ones it already cleared.
    """
    rows = conn.execute(
        "SELECT * FROM units WHERE dirty = 1 AND id > ? ORDER BY id LIMIT ?", (after_id, limit)
    )
    return [_unit_row(row) for row in rows]


def count_dirty_units(conn: sqlite3.Connection) -> int:
    """How many units still wait for embedding."""
    return int(conn.execute("SELECT count(*) FROM units WHERE dirty = 1").fetchone()[0])


def set_embedded(conn: sqlite3.Connection, ids: Iterable[int], model: str) -> None:
    """Record that these units were embedded with ``model`` and clear their flag."""
    with transaction(conn):
        for chunk in _chunks(ids):
            conn.execute(
                f"UPDATE units SET dirty = 0, embedded_model = ? WHERE id IN ({_marks(chunk)})",
                [model, *chunk],
            )


def reset_embedded(conn: sqlite3.Connection) -> None:
    """Flag every unit for re-embedding and forget which model embedded it."""
    with transaction(conn):
        conn.execute("UPDATE units SET dirty = 1, embedded_model = NULL")


# --- helpers ---------------------------------------------------------------------------------


def _units_by_chunks(
    conn: sqlite3.Connection, sql: str, head: list[int], ids: Iterable[int]
) -> list[UnitRow]:
    """Units of a query whose last parameter list is ``ids``, deduplicated and in id order.

    ``sql`` carries a ``{marks}`` placeholder for the ``IN (…)`` list, which is filled per chunk
    of :data:`IN_BATCH` ids; ``head`` are the parameters before it.
    """
    found: dict[int, UnitRow] = {}
    for chunk in _chunks(ids):
        rows = conn.execute(sql.format(marks=_marks(chunk)), [*head, *chunk])
        for row in rows:
            found[int(row["id"])] = _unit_row(row)
    return [found[unit_id] for unit_id in sorted(found)]


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
        comment_of_chat_id=row["comment_of_chat_id"],
        comment_of_msg_id=row["comment_of_msg_id"],
        fwd_from=row["fwd_from"],
        text=row["text"],
        media_kind=row["media_kind"],
        media_filename=row["media_filename"],
        reactions_total=row["reactions_total"],
        extracted_text=row["extracted_text"],
        media_state=row["media_state"],
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
        reactions=row["reactions"],
        dirty=bool(row["dirty"]),
        embedded_model=row["embedded_model"],
    )
