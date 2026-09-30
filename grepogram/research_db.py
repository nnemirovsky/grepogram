"""``research.db``: what research sessions found and what a human decided about it.

A SQLite file of its own next to ``index.db`` (:attr:`grepogram.paths.Paths.research_db_file`),
written with :data:`~grepogram.paths.PRIVATE_FILE_MODE`. The split is the point: the index is
derived from Telegram and may be deleted and rebuilt with one sync, while this file holds the
user's decisions — approvals, exclusions, session history — which nothing can rebuild. For the
same reason a schema this build cannot read is refused without the index's "delete and sync
again" advice (:class:`SchemaError`).

The tables:

``sessions`` and ``session_seeds``
    a question explored from seed chats by one account, under
    :class:`~grepogram.models.ResearchLimits` fixed at start; ``active`` until stopped, and
    stopping voids every grant not consumed yet (:func:`stop_session`). A seed is named the way
    Telegram names it, :class:`~grepogram.models.ChatKey` — ``(scope, peer_id)`` — and never by
    an index row id: a rebuilt index numbers its rows afresh, and a private chat's synthetic
    row id is handed to whichever account's row is stored second.
``candidates``
    one per ``(session, identity)`` — ``identity`` is a :mod:`grepogram.leads` target string —
    with its depth, status, what a probe learned, and the ``parent_id`` it was found inside.
    Whether the index already holds the chat is asked of ``index.db`` when needed and never
    stored here: the index can be rebuilt under this file.
``evidence``
    every path that led to a candidate; ``origin_key`` is what corroboration counts
    (:func:`corroboration`), so ten forwards of one post are one piece of evidence. The chat a
    path was found in is ``(scope, peer_id)`` too, indexed or not.
``grants``
    one human approval each: the concrete actions on one candidate, or session-wide search
    actions, by one account, **through one channel** — ``elicitation`` or ``cli``, enforced by
    a ``CHECK`` and by :func:`add_grant`, the only writer, which takes it as a required argument.
    A session-wide grant keeps the terms its summary named (step 3: ``search_kinds``,
    ``stars_max``), and a ``join`` or ``request`` the way in it named (step 4: ``join_route``),
    the only one a run takes.
``exclusions``
    identities research never proposes again, in any session; global and persistent.
``searches``
    every Telegram-side search a session ran. Its results are evidence, never ``messages`` rows.
``chat_scans``
    per session and chat it reads (by ``(scope, peer_id)``), the depth that chat's leads are
    found at, how far discovery has read it — a tick of the index's lead clock
    (:func:`grepogram.db.lead_clock`), valid only for the index ``index_id`` names — whether its
    pinned posts were read and whether it is a directory, so a run resumes from this file alone
    and an index rebuilt under it is simply read again from the start.

Step 2 carried a v1 file (development builds only) over: ``chats.id`` values became
``(scope, peer_id)`` — a marked channel id is shared, anything else was the session's account's
— and the ids at or above the synthetic base, which named no peer, were dropped; scan cursors
start again, which costs one re-read and duplicates nothing.

Every writer runs inside :func:`grepogram.db.transaction` on a :class:`grepogram.db.Connection`,
so the connection may be shared across threads exactly like the index's.
"""

import dataclasses
import json
import os
import sqlite3
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, get_args

from grepogram import db, leads
from grepogram.models import (
    Candidate,
    CandidateAction,
    CandidateKind,
    ChatKey,
    Evidence,
    EvidenceVia,
    Exclusion,
    Grant,
    GrantAction,
    GrantChannel,
    ResearchLimits,
    ResearchSession,
    ResearchState,
    ScanCursor,
    SearchKind,
    SearchRecord,
    SessionAction,
)
from grepogram.paths import PRIVATE_FILE_MODE, Paths

META_SCHEMA_VERSION = "schema_version"
_KEEP_HINT = (
    "research.db holds your research sessions, approvals and exclusions and is not rebuilt from "
    "Telegram: upgrade grepogram, or move the file aside to start research afresh"
)

_V1: tuple[str, ...] = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE sessions(
        id INTEGER PRIMARY KEY,
        question TEXT NOT NULL,
        account TEXT NOT NULL,
        seeds TEXT NOT NULL,
        limits TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'active',
        progress TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL,
        stopped_at INTEGER
    )""",
    """CREATE TABLE candidates(
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        identity TEXT NOT NULL,
        kind TEXT NOT NULL,
        depth INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'proposed',
        peer_id INTEGER,
        username TEXT,
        invite_hash TEXT,
        addlist_slug TEXT,
        title TEXT,
        type TEXT,
        participants INTEGER,
        member INTEGER,
        access_hash INTEGER,
        request_needed INTEGER,
        parent_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
        source_id TEXT,
        probed_at INTEGER,
        created_at INTEGER NOT NULL,
        note TEXT,
        UNIQUE (session_id, identity)
    )""",
    "CREATE INDEX candidates_status ON candidates(session_id, status)",
    "CREATE INDEX candidates_identity ON candidates(identity)",
    "CREATE INDEX candidates_parent ON candidates(parent_id) WHERE parent_id IS NOT NULL",
    """CREATE TABLE evidence(
        id INTEGER PRIMARY KEY,
        candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
        via TEXT NOT NULL,
        chat_id INTEGER,
        msg_id INTEGER,
        origin_key TEXT NOT NULL,
        snippet TEXT,
        found_at INTEGER NOT NULL
    )""",
    # one row per path: the same message seen again by a later discover adds nothing
    """CREATE UNIQUE INDEX evidence_path
        ON evidence(candidate_id, via, origin_key, IFNULL(chat_id, 0), IFNULL(msg_id, 0))""",
    """CREATE TABLE grants(
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
        account TEXT NOT NULL,
        actions TEXT NOT NULL,
        via TEXT NOT NULL CHECK (via IN ('elicitation', 'cli')),
        summary TEXT NOT NULL CHECK (summary <> ''),
        granted_at INTEGER NOT NULL,
        consumed_at INTEGER,
        voided_at INTEGER
    )""",
    """CREATE INDEX grants_live ON grants(session_id, candidate_id)
        WHERE consumed_at IS NULL AND voided_at IS NULL""",
    """CREATE TABLE exclusions(
        identity TEXT PRIMARY KEY,
        reason TEXT,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE searches(
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        query TEXT NOT NULL,
        ran_at INTEGER NOT NULL,
        results INTEGER NOT NULL DEFAULT 0,
        note TEXT
    )""",
    """CREATE TABLE scans(
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        chat_id INTEGER NOT NULL,
        depth INTEGER NOT NULL,
        msg_id INTEGER NOT NULL,
        scanned_at INTEGER NOT NULL,
        PRIMARY KEY (session_id, chat_id)
    )""",
)

_LEGACY_SCOPE = """CASE WHEN {id} <= -1000000000000 THEN ''
    ELSE (SELECT account FROM sessions WHERE sessions.id = {session}) END"""
"""The scope of a v1 ``chats.id`` value: a channel's or supergroup's marked id names a shared
row (scope ``''``); anything else was the session's own account's, the only account a v1 file's
chats were stored through."""
_SYNTHETIC = 1 << 62
""":data:`grepogram.db.SYNTHETIC_BASE`: a v1 id at or above it was an index row id and names no
peer at all, so step 2 cannot carry it over."""

_V2: tuple[str, ...] = (
    # a seed chat as Telegram names it — (scope, peer id) — rather than by an index row id,
    # which a rebuilt index or a scoped row deleted and stored again hands to another chat
    """CREATE TABLE session_seeds(
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        scope TEXT NOT NULL,
        peer_id INTEGER NOT NULL,
        PRIMARY KEY (session_id, scope, peer_id))""",
    f"""INSERT OR IGNORE INTO session_seeds(session_id, position, scope, peer_id)
        SELECT s.id, j.key, {_LEGACY_SCOPE.format(id="j.value", session="s.id")}, j.value
        FROM sessions AS s, json_each(s.seeds) AS j WHERE j.value < {_SYNTHETIC}""",
    "ALTER TABLE sessions DROP COLUMN seeds",
    # scan cursors by the same identity, on the index's lead clock (grepogram.db.lead_clock) of
    # the index named index_id — a cursor of another index, or none, reads the chat from the
    # start; pins_read_at and directory are what the pinned-post and directory passes learned
    """CREATE TABLE chat_scans(
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        scope TEXT NOT NULL,
        peer_id INTEGER NOT NULL,
        depth INTEGER NOT NULL,
        index_id TEXT,
        lead_seq INTEGER NOT NULL DEFAULT 0,
        pins_read_at INTEGER,
        directory INTEGER NOT NULL DEFAULT 0,
        scanned_at INTEGER NOT NULL,
        PRIMARY KEY (session_id, scope, peer_id))""",
    f"""INSERT OR IGNORE INTO chat_scans(session_id, scope, peer_id, depth, scanned_at)
        SELECT session_id, {_LEGACY_SCOPE.format(id="chat_id", session="session_id")}, chat_id,
               depth, scanned_at
        FROM scans WHERE chat_id < {_SYNTHETIC}""",
    "DROP TABLE scans",
    # evidence names the chat it was found in the same way: peer_id and scope, whether the chat
    # is indexed (a message discovery read) or not (a global search's result)
    "DROP INDEX evidence_path",
    "ALTER TABLE evidence RENAME COLUMN chat_id TO peer_id",
    "ALTER TABLE evidence ADD COLUMN scope TEXT",
    f"""UPDATE evidence SET scope = CASE WHEN peer_id IS NULL OR peer_id >= {_SYNTHETIC} THEN NULL
        WHEN peer_id <= -1000000000000 THEN ''
        ELSE (SELECT s.account FROM candidates AS c JOIN sessions AS s ON s.id = c.session_id
              WHERE c.id = evidence.candidate_id) END""",
    f"UPDATE evidence SET peer_id = NULL WHERE peer_id >= {_SYNTHETIC}",
    """DELETE FROM evidence WHERE id NOT IN (
        SELECT MIN(id) FROM evidence GROUP BY candidate_id, via, origin_key,
            IFNULL(scope, ''), IFNULL(peer_id, 0), IFNULL(msg_id, 0))""",
    """CREATE UNIQUE INDEX evidence_path ON evidence(candidate_id, via, origin_key,
        IFNULL(scope, ''), IFNULL(peer_id, 0), IFNULL(msg_id, 0))""",
    # when an admission request was sent, so one no admin answers can time out
    "ALTER TABLE candidates ADD COLUMN requested_at INTEGER",
    """UPDATE candidates SET requested_at = COALESCE(probed_at, created_at)
        WHERE status = 'pending_admission'""",
)

_V3: tuple[str, ...] = (
    # the terms a session-wide approval was given on, as its summary named them: the searches a
    # global_search grant covers and the stars a paid_search grant may pay — so raising either in
    # the config later widens nothing a human already approved
    "ALTER TABLE grants ADD COLUMN search_kinds TEXT",
    "ALTER TABLE grants ADD COLUMN stars_max INTEGER",
)

_V4: tuple[str, ...] = (
    # how a join or an admission request gets the account in, as the approval's summary named
    # it: "invite", "username", "id" or "folder:<candidate id>". A run takes that route and no
    # other — a shared folder that lists the chat after the approval adds nothing to it
    "ALTER TABLE grants ADD COLUMN join_route TEXT",
)

MIGRATIONS: dict[int, tuple[str, ...]] = {1: _V1, 2: _V2, 3: _V3, 4: _V4}
"""Schema version → the step that brings the file to it, from 1 without a gap (the test suite
checks that); append-only."""
SCHEMA_VERSION = max(MIGRATIONS)

_CANDIDATE_ACTIONS: frozenset[str] = frozenset(get_args(CandidateAction))
_SESSION_ACTIONS: frozenset[str] = frozenset(get_args(SessionAction))
_CHANNELS: frozenset[str] = frozenset(get_args(GrantChannel))
_SEARCH_KINDS: frozenset[str] = frozenset(get_args(SearchKind))
_EXCLUDABLE = ("proposed", "approved", "skipped")
"""Statuses an exclusion moves to ``excluded``: decisions not acted on yet. A chat already
joined or fetched keeps its status — excluding it undoes nothing on Telegram — but loses every
approval still pending for it (:func:`add_exclusion`)."""

_CANDIDATE_FIELDS = frozenset(
    {
        "status",
        "peer_id",
        "username",
        "invite_hash",
        "addlist_slug",
        "title",
        "type",
        "participants",
        "member",
        "access_hash",
        "request_needed",
        "source_id",
        "probed_at",
        "requested_at",
        "note",
    }
)
"""What :func:`update_candidate` may set; identity, session, kind, depth and parent are fixed."""


class SchemaError(db.SchemaError):
    """``research.db`` has a schema this build cannot use.

    A subclass of the index's error so every handler that turns one into a message rather than
    a traceback catches this too; the message never advises deleting the file.
    """


# --- connection and schema -------------------------------------------------------------------


def connect(target: Paths | str) -> db.Connection:
    """Open ``research.db`` (created with mode 0600 under the 0700 data directory) or a raw
    database string such as ``":memory:"``.

    The file is created by :func:`os.open` with :data:`PRIVATE_FILE_MODE` before SQLite sees it,
    and an existing one is narrowed to it, so the journal files SQLite derives from the database
    file's mode are private too. Foreign keys are on and rows are :class:`sqlite3.Row`.
    """
    if isinstance(target, Paths):
        target.ensure_dirs()
        path = target.research_db_file
        fd = os.open(path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
        os.close(fd)
        os.chmod(path, PRIVATE_FILE_MODE)
        database = str(path)
    else:
        database = target
    conn = sqlite3.connect(
        database, check_same_thread=False, isolation_level=None, factory=db.Connection
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={db.BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def open_store(target: Paths | str) -> db.Connection:
    """:func:`connect` and :func:`migrate`; the connection is closed again if the schema is
    refused."""
    conn = connect(target)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """The version recorded in ``meta``; ``0`` for a file with no ``meta`` table."""
    if not db.has_table(conn, "meta"):
        return 0
    columns = {row[0] for row in conn.execute("SELECT name FROM pragma_table_info('meta')")}
    if not {"key", "value"} <= columns:
        raise SchemaError(f"the meta table of research.db is not grepogram's; {_KEEP_HINT}")
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_SCHEMA_VERSION,)).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except ValueError as exc:
        raise SchemaError(
            f"research.db records {row['value']!r} as its schema version; {_KEEP_HINT}"
        ) from exc


def migrate(conn: sqlite3.Connection) -> int:
    """Bring ``research.db`` to :data:`SCHEMA_VERSION` or refuse it; returns the version.

    An empty file gets the whole schema; one at the version is used as it is; an older one whose
    every step :data:`MIGRATIONS` holds is walked up. Anything else — tables with no recorded
    version, a newer version, one no chain of steps reaches — is :class:`SchemaError`.
    """
    current = schema_version(conn)
    if current == SCHEMA_VERSION:
        return current
    if current == 0 and conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
        raise SchemaError(f"research.db holds tables but records no schema version; {_KEEP_HINT}")
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"research.db schema v{current} is newer than this grepogram supports "
            f"(v{SCHEMA_VERSION}); {_KEEP_HINT}"
        )
    if current < 0:
        raise SchemaError(f"research.db schema v{current} cannot be upgraded; {_KEEP_HINT}")
    with db.transaction(conn):
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (META_SCHEMA_VERSION, str(SCHEMA_VERSION)),
        )
    return SCHEMA_VERSION


# --- helpers ---------------------------------------------------------------------------------


def clock(now: int | None = None) -> int:
    """``now``, or the current unix time when it is ``None``: the one clock ``research.db`` and
    :mod:`grepogram.research` write by, so a test passes the time it wants."""
    return int(time.time()) if now is None else now


def _check(value: str, allowed: frozenset[str], what: str) -> None:
    """Refuse a value a grant may not hold: :func:`add_grant` is the consent record, so it checks
    its channel and actions at run time; every other writer trusts its typed callers."""
    if value not in allowed:
        raise ValueError(f"unknown {what} {value!r}; expected one of {', '.join(sorted(allowed))}")


def _flag(value: object) -> bool | None:
    return None if value is None else bool(value)


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


# --- sessions --------------------------------------------------------------------------------


def _seeds(conn: sqlite3.Connection, session_id: int) -> tuple[ChatKey, ...]:
    rows = conn.execute(
        "SELECT scope, peer_id FROM session_seeds WHERE session_id = ? ORDER BY position",
        (session_id,),
    )
    return tuple(ChatKey(str(row["scope"]), int(row["peer_id"])) for row in rows)


def _session(conn: sqlite3.Connection, row: sqlite3.Row) -> ResearchSession:
    stored = json.loads(row["limits"])
    known = {field.name for field in dataclasses.fields(ResearchLimits)}
    return ResearchSession(
        id=row["id"],
        question=row["question"],
        account=row["account"],
        seeds=_seeds(conn, row["id"]),
        limits=ResearchLimits(**{key: value for key, value in stored.items() if key in known}),
        state=row["state"],
        created_at=row["created_at"],
        stopped_at=row["stopped_at"],
        progress=json.loads(row["progress"]),
    )


def create_session(
    conn: sqlite3.Connection,
    *,
    question: str,
    account: str,
    seeds: Iterable[ChatKey],
    limits: ResearchLimits,
    now: int | None = None,
) -> ResearchSession:
    """Start a session from the chats ``seeds`` name; it is ``active`` until
    :func:`stop_session`."""
    if not question.strip():
        raise ValueError("a research session needs a question")
    keys = list(dict.fromkeys(ChatKey(str(scope), int(peer)) for scope, peer in seeds))
    with db.transaction(conn):
        row = conn.execute(
            "INSERT INTO sessions(question, account, limits, created_at) "
            "VALUES (?, ?, ?, ?) RETURNING *",
            (question.strip(), account, json.dumps(dataclasses.asdict(limits)), clock(now)),
        ).fetchone()
        conn.executemany(
            "INSERT INTO session_seeds(session_id, position, scope, peer_id) VALUES (?, ?, ?, ?)",
            [(row["id"], position, key.scope, key.peer_id) for position, key in enumerate(keys)],
        )
        return _session(conn, row)


def _row_id(value: int) -> bool:
    """Whether ``value`` can be a row id at all: a number typed on a command line or sent to a
    tool is unbounded, and SQLite refuses to bind one past 64 bits rather than find nothing."""
    return 0 < value <= leads.INT64_MAX


def get_session(conn: sqlite3.Connection, session_id: int) -> ResearchSession | None:
    if not _row_id(session_id):
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return None if row is None else _session(conn, row)


def list_sessions(
    conn: sqlite3.Connection, state: ResearchState | None = None
) -> list[ResearchSession]:
    """Every session, newest first; only those in ``state`` when it is given."""
    if state is None:
        rows = conn.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE state = ? ORDER BY id DESC", (state,)
        ).fetchall()
    return [_session(conn, row) for row in rows]


def set_session_progress(
    conn: sqlite3.Connection, session_id: int, progress: Mapping[str, object]
) -> None:
    """Replace what the research loop recorded about a session's runs."""
    with db.transaction(conn):
        cursor = conn.execute(
            "UPDATE sessions SET progress = ? WHERE id = ?",
            (json.dumps(dict(progress)), session_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"no research session {session_id}")


def stop_session(conn: sqlite3.Connection, session_id: int, now: int | None = None) -> int:
    """Mark a session ``stopped`` and void its unconsumed grants in one transaction.

    Returns how many grants were voided. Stopping twice keeps the first ``stopped_at``. Nothing
    else changes: candidates keep their status and every source a run added stays configured.
    """
    stamp = clock(now)
    with db.transaction(conn):
        if get_session(conn, session_id) is None:
            raise KeyError(f"no research session {session_id}")
        conn.execute(
            "UPDATE sessions SET state = 'stopped', stopped_at = COALESCE(stopped_at, ?) "
            "WHERE id = ?",
            (stamp, session_id),
        )
        return void_grants(conn, session_id, now=stamp)


# --- candidates ------------------------------------------------------------------------------


def _candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(
        id=row["id"],
        session_id=row["session_id"],
        identity=row["identity"],
        kind=row["kind"],
        depth=row["depth"],
        status=row["status"],
        peer_id=row["peer_id"],
        username=row["username"],
        invite_hash=row["invite_hash"],
        addlist_slug=row["addlist_slug"],
        title=row["title"],
        type=row["type"],
        participants=row["participants"],
        member=_flag(row["member"]),
        access_hash=row["access_hash"],
        request_needed=_flag(row["request_needed"]),
        parent_id=row["parent_id"],
        source_id=row["source_id"],
        probed_at=row["probed_at"],
        requested_at=row["requested_at"],
        created_at=row["created_at"],
        note=row["note"],
    )


def add_candidate(
    conn: sqlite3.Connection,
    session_id: int,
    identity: str,
    kind: CandidateKind,
    depth: int,
    *,
    peer_id: int | None = None,
    username: str | None = None,
    invite_hash: str | None = None,
    addlist_slug: str | None = None,
    parent_id: int | None = None,
    now: int | None = None,
) -> Candidate | None:
    """The session's candidate for ``identity``, created ``proposed`` when it is new.

    An identity already present keeps its row, status included; it takes the smaller depth and
    fills fields it did not know yet. So does a chat the session already holds under another
    spelling — ``@name``, ``peer:<id>`` and an invite are one chat once a probe tied them
    together, and ``peer_id`` / ``username`` name that chat here (:func:`candidate_for`). A
    candidate's peer id is fixed once known: an identity whose row a probe tied to *another*
    peer (a username that moved since) cannot name ``peer_id``'s chat, which is recorded as
    ``peer:<peer_id>`` instead. A candidate that carries a decision — a grant, ever, or any
    status but ``proposed`` — is never given a parent: the folder it is found in afterwards is
    a way in no approval of it named. An
    excluded chat, under any spelling it is known by (:func:`excluded_by`), gets no row at all
    and answers ``None`` — exclusions are global, so no session proposes one again.
    """
    if depth < 0:
        raise ValueError(f"a candidate's depth cannot be negative: {depth}")
    with db.transaction(conn):
        spelled = candidate_by_identity(conn, session_id, identity)
        if spelled is not None and _other_peer(spelled, peer_id):
            # the spelling already names another chat (a username that moved): this one is
            # recorded under its marked id, never folded into the row a human may have decided on
            assert peer_id is not None
            identity, kind = f"peer:{peer_id}", "peer"
        if excluded_by(conn, identity, peer_id=peer_id, username=username) is not None:
            return None
        known = candidate_for(
            conn,
            session_id,
            identity,
            peer_id=peer_id,
            username=username,
            invite_hash=invite_hash,
        )
        if known is not None and known.identity != identity:
            # the chat is already a candidate under another spelling: that row is the one
            row = conn.execute(
                """UPDATE candidates SET
                       depth = MIN(depth, ?),
                       peer_id = COALESCE(peer_id, ?),
                       username = COALESCE(username, ?),
                       invite_hash = COALESCE(invite_hash, ?),
                       addlist_slug = COALESCE(addlist_slug, ?),
                       parent_id = CASE WHEN status = 'proposed' AND NOT EXISTS (
                           SELECT 1 FROM grants WHERE grants.candidate_id = candidates.id)
                           THEN COALESCE(parent_id, ?) ELSE parent_id END
                   WHERE id = ? RETURNING *""",
                (depth, peer_id, username, invite_hash, addlist_slug, parent_id, known.id),
            ).fetchone()
            return _candidate(row)
        row = conn.execute(
            """INSERT INTO candidates(session_id, identity, kind, depth, peer_id, username,
                   invite_hash, addlist_slug, parent_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, identity) DO UPDATE SET
                   depth = MIN(depth, excluded.depth),
                   peer_id = COALESCE(peer_id, excluded.peer_id),
                   username = COALESCE(username, excluded.username),
                   invite_hash = COALESCE(invite_hash, excluded.invite_hash),
                   addlist_slug = COALESCE(addlist_slug, excluded.addlist_slug),
                   parent_id = CASE WHEN status = 'proposed' AND NOT EXISTS (
                       SELECT 1 FROM grants WHERE grants.candidate_id = candidates.id)
                       THEN COALESCE(parent_id, excluded.parent_id) ELSE parent_id END
               RETURNING *""",
            (
                session_id,
                identity,
                kind,
                depth,
                peer_id,
                username,
                invite_hash,
                addlist_slug,
                parent_id,
                clock(now),
            ),
        ).fetchone()
    return _candidate(row)


def count_candidates(conn: sqlite3.Connection, session_id: int) -> int:
    """How many candidates the session holds, whatever their status — what its
    ``max_session_candidates`` ceiling is counted against."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM candidates WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row["n"])


def get_candidate(conn: sqlite3.Connection, candidate_id: int) -> Candidate | None:
    if not _row_id(candidate_id):
        return None
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    return None if row is None else _candidate(row)


def candidate_by_identity(
    conn: sqlite3.Connection, session_id: int, identity: str
) -> Candidate | None:
    row = conn.execute(
        "SELECT * FROM candidates WHERE session_id = ? AND identity = ?", (session_id, identity)
    ).fetchone()
    return None if row is None else _candidate(row)


def candidate_for(
    conn: sqlite3.Connection,
    session_id: int,
    identity: str,
    *,
    peer_id: int | None = None,
    username: str | None = None,
    invite_hash: str | None = None,
) -> Candidate | None:
    """The session's candidate for the chat ``identity`` names: the row of that identity, else
    the oldest one a probe tied to the same ``peer_id``, ``username`` or ``invite_hash``.

    A row tied to a peer other than ``peer_id`` is another chat, whatever name it shares: a
    username or an invite matches only a row whose peer id is unknown or ``peer_id`` itself."""
    found = candidate_by_identity(conn, session_id, identity)
    if found is not None and not _other_peer(found, peer_id):
        return found
    clauses, params = _same_chat(peer_id, username, invite_hash, strict=True)
    if not clauses:
        return None
    row = conn.execute(
        f"SELECT * FROM candidates WHERE session_id = ? AND ({' OR '.join(clauses)}) "
        "ORDER BY id LIMIT 1",
        (session_id, *params),
    ).fetchone()
    return None if row is None else _candidate(row)


def _other_peer(candidate: Candidate, peer_id: int | None) -> bool:
    """Whether ``candidate`` is tied to a known peer other than ``peer_id``."""
    return peer_id is not None and candidate.peer_id is not None and candidate.peer_id != peer_id


def _same_chat(
    peer_id: int | None,
    username: str | None,
    invite_hash: str | None = None,
    *,
    strict: bool = False,
) -> tuple[list[str], list[Any]]:
    """The ``candidates`` conditions naming the chat ``peer_id`` / ``username`` / an invite
    identify.

    ``strict`` matches a username or an invite only on rows tied to no peer or to ``peer_id``:
    what finds or merges *the* candidate of a chat must never take a row of another chat that
    once went by the same name. An exclusion asks without it, since covering too much is the
    safe side there."""
    clauses: list[str] = []
    params: list[Any] = []
    guard = " AND (peer_id IS NULL OR peer_id = ?)" if strict and peer_id is not None else ""
    if peer_id is not None:
        clauses.append("peer_id = ?")
        params.append(peer_id)
    if username:
        clauses.append(f"(username = ?{guard})")
        params.extend([username.lower(), *([peer_id] if guard else [])])
    if invite_hash:
        clauses.append(f"(invite_hash = ?{guard})")
        params.extend([invite_hash, *([peer_id] if guard else [])])
    return clauses, params


def same_chat_candidates(conn: sqlite3.Connection, candidate: Candidate) -> list[Candidate]:
    """The other candidates of ``candidate``'s session a probe has tied to the same chat."""
    clauses, params = _same_chat(candidate.peer_id, candidate.username, strict=True)
    if not clauses:
        return []
    rows = conn.execute(
        f"SELECT * FROM candidates WHERE session_id = ? AND id <> ? AND ({' OR '.join(clauses)}) "
        "ORDER BY id",
        (candidate.session_id, candidate.id, *params),
    ).fetchall()
    return [_candidate(row) for row in rows]


def renamed_away(
    conn: sqlite3.Connection, session_id: int, identity: str, username: str | None, peer_id: int
) -> list[Candidate]:
    """The candidates of the session that a probe tied to a peer other than ``peer_id`` but that
    go by the name Telegram now gives ``peer_id``'s chat — ``identity`` or ``@username``: the
    name moved away from the chat they were probed as."""
    names = [identity, *([f"@{username.lower()}"] if username else [])]
    rows = conn.execute(
        "SELECT * FROM candidates WHERE session_id = ? AND peer_id IS NOT NULL AND peer_id <> ? "
        f"AND (identity IN ({_placeholders(len(names))}) OR username = ?) ORDER BY id",
        (session_id, peer_id, *names, (username or "").lower() or None),
    ).fetchall()
    return [_candidate(row) for row in rows]


def has_grants(conn: sqlite3.Connection, candidate_id: int) -> bool:
    """Whether any grant — live, consumed or voided — ever named ``candidate_id``."""
    row = conn.execute("SELECT 1 FROM grants WHERE candidate_id = ?", (candidate_id,)).fetchone()
    return row is not None


_MERGED_FIELDS = (
    "peer_id",
    "username",
    "invite_hash",
    "addlist_slug",
    "title",
    "type",
    "participants",
    "member",
    "access_hash",
    "request_needed",
    "probed_at",
)


def merge_candidate(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> Candidate:
    """Fold candidate ``drop_id`` into ``keep_id`` — the same chat reached by two spellings —
    and return what is kept.

    Every piece of evidence moves over (a path both hold counts once), the kept row fills what
    it did not know and takes the smaller depth, the dropped row's folder children follow it,
    and the dropped row goes. Only a candidate no human decided on may be dropped: one with a
    grant, ever, or with any status but ``proposed`` raises :class:`ValueError`, since its
    history is a decision someone made.
    """
    with db.transaction(conn):
        keep = get_candidate(conn, keep_id)
        drop = get_candidate(conn, drop_id)
        if keep is None or drop is None or keep.session_id != drop.session_id or keep.id == drop.id:
            raise KeyError(f"cannot merge candidate {drop_id} into {keep_id}")
        if drop.status != "proposed" or has_grants(conn, drop.id):
            raise ValueError(f"candidate {drop_id} carries a decision and is not merged away")
        conn.execute(
            """INSERT INTO evidence(candidate_id, via, scope, peer_id, msg_id, origin_key,
                   snippet, found_at)
               SELECT ?, via, scope, peer_id, msg_id, origin_key, snippet, found_at
               FROM evidence WHERE candidate_id = ? ORDER BY id
               ON CONFLICT DO NOTHING""",
            (keep.id, drop.id),
        )
        conn.execute(
            "UPDATE candidates SET parent_id = ? WHERE parent_id = ? AND id <> ?",
            (keep.id, drop.id, keep.id),
        )
        conn.execute("DELETE FROM candidates WHERE id = ?", (drop.id,))
        filled = {
            name: getattr(drop, name)
            for name in _MERGED_FIELDS
            if getattr(keep, name) is None and getattr(drop, name) is not None
        }
        if drop.depth < keep.depth:
            conn.execute("UPDATE candidates SET depth = ? WHERE id = ?", (drop.depth, keep.id))
        return update_candidate(conn, keep.id, **filled)


def list_candidates(
    conn: sqlite3.Connection,
    session_id: int,
    statuses: Collection[str] | None = None,
    *,
    parent_id: int | None = None,
) -> list[Candidate]:
    """A session's candidates in the order they were found; narrowed to ``statuses`` and to the
    children of ``parent_id`` when those are given."""
    clauses = ["session_id = ?"]
    params: list[Any] = [session_id]
    if statuses is not None:
        wanted = list(statuses)
        if not wanted:
            return []
        clauses.append(f"status IN ({_placeholders(len(wanted))})")
        params.extend(wanted)
    if parent_id is not None:
        clauses.append("parent_id = ?")
        params.append(parent_id)
    rows = conn.execute(
        f"SELECT * FROM candidates WHERE {' AND '.join(clauses)} ORDER BY id", params
    ).fetchall()
    return [_candidate(row) for row in rows]


def update_candidate(conn: sqlite3.Connection, candidate_id: int, **fields: Any) -> Candidate:
    """Set the named fields of one candidate and return it as stored.

    Only what a probe or a run learns may change (:data:`_CANDIDATE_FIELDS`); a candidate's
    identity, session, kind, depth and parent are what it *is*, and so is its peer id once
    known — setting another one raises :class:`ValueError`.
    """
    unknown = set(fields) - _CANDIDATE_FIELDS
    if unknown:
        raise ValueError(f"cannot set candidate field(s): {', '.join(sorted(unknown))}")
    if fields.get("peer_id") is not None:
        current = get_candidate(conn, candidate_id)
        if current is not None and _other_peer(current, fields["peer_id"]):
            # a candidate's Telegram identity is fixed once a probe learned it: an answer naming
            # another peer is another chat, and a human may have approved this one
            raise ValueError(
                f"candidate {candidate_id} is peer {current.peer_id}; it cannot become "
                f"peer {fields['peer_id']}"
            )
    if not fields:
        found = get_candidate(conn, candidate_id)
        if found is None:
            raise KeyError(f"no research candidate {candidate_id}")
        return found
    names = sorted(fields)
    values = [
        int(fields[name]) if isinstance(fields[name], bool) else fields[name] for name in names
    ]
    with db.transaction(conn):
        row = conn.execute(
            f"UPDATE candidates SET {', '.join(f'{name} = ?' for name in names)} "
            "WHERE id = ? RETURNING *",
            (*values, candidate_id),
        ).fetchone()
    if row is None:
        raise KeyError(f"no research candidate {candidate_id}")
    return _candidate(row)


# --- evidence --------------------------------------------------------------------------------


def _evidence(row: sqlite3.Row) -> Evidence:
    scope, peer = row["scope"], row["peer_id"]
    return Evidence(
        id=row["id"],
        candidate_id=row["candidate_id"],
        via=row["via"],
        origin_key=row["origin_key"],
        chat=None if scope is None or peer is None else ChatKey(str(scope), int(peer)),
        msg_id=row["msg_id"],
        snippet=row["snippet"],
        found_at=row["found_at"],
    )


def add_evidence(
    conn: sqlite3.Connection,
    candidate_id: int,
    via: EvidenceVia,
    origin_key: str,
    *,
    chat: ChatKey | None = None,
    msg_id: int | None = None,
    snippet: str | None = None,
    now: int | None = None,
) -> bool:
    """Record one path to a candidate — found in the message ``msg_id`` of ``chat``, when it was
    found in a message; ``False`` when that exact path is already recorded."""
    if not origin_key:
        raise ValueError("evidence needs an origin key")
    scope, peer = (None, None) if chat is None else (chat.scope, chat.peer_id)
    with db.transaction(conn):
        cursor = conn.execute(
            """INSERT INTO evidence(candidate_id, via, scope, peer_id, msg_id, origin_key,
                   snippet, found_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            (candidate_id, via, scope, peer, msg_id, origin_key, snippet, clock(now)),
        )
    return cursor.rowcount > 0


def evidence_from(conn: sqlite3.Connection, session_id: int, chat: ChatKey) -> list[Evidence]:
    """Every piece of evidence the session's candidates hold that was found in ``chat``."""
    rows = conn.execute(
        """SELECT e.* FROM evidence AS e JOIN candidates AS c ON c.id = e.candidate_id
           WHERE c.session_id = ? AND e.scope = ? AND e.peer_id = ? ORDER BY e.id""",
        (session_id, chat.scope, chat.peer_id),
    ).fetchall()
    return [_evidence(row) for row in rows]


def list_evidence(conn: sqlite3.Connection, candidate_id: int) -> list[Evidence]:
    rows = conn.execute(
        "SELECT * FROM evidence WHERE candidate_id = ? ORDER BY id", (candidate_id,)
    ).fetchall()
    return [_evidence(row) for row in rows]


def corroboration(conn: sqlite3.Connection, candidate_ids: Iterable[int]) -> dict[int, int]:
    """Independent pieces of evidence per candidate: distinct origin keys, so every forward of
    one post counts once. Candidates with no evidence are absent."""
    ids = list(dict.fromkeys(candidate_ids))
    counts: dict[int, int] = {}
    for start in range(0, len(ids), db.IN_BATCH):
        chunk = ids[start : start + db.IN_BATCH]
        for row in conn.execute(
            "SELECT candidate_id, COUNT(DISTINCT origin_key) AS n FROM evidence "
            f"WHERE candidate_id IN ({_placeholders(len(chunk))}) GROUP BY candidate_id",
            chunk,
        ):
            counts[row["candidate_id"]] = row["n"]
    return counts


# --- grants ----------------------------------------------------------------------------------


def _grant(row: sqlite3.Row) -> Grant:
    return Grant(
        id=row["id"],
        session_id=row["session_id"],
        candidate_id=row["candidate_id"],
        account=row["account"],
        actions=tuple(json.loads(row["actions"])),
        via=row["via"],
        summary=row["summary"],
        granted_at=row["granted_at"],
        consumed_at=row["consumed_at"],
        voided_at=row["voided_at"],
        search_kinds=tuple(json.loads(row["search_kinds"] or "[]")),
        stars_max=row["stars_max"],
        join_route=row["join_route"],
    )


def add_grant(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    candidate_id: int | None,
    account: str,
    actions: Sequence[GrantAction],
    via: GrantChannel,
    summary: str,
    search_kinds: Sequence[SearchKind] = (),
    stars_max: int | None = None,
    join_route: str | None = None,
    now: int | None = None,
) -> Grant:
    """Record one human approval — the only way a grant comes to exist.

    ``via`` names the channel the human answered through and has no default: a grant without
    one cannot be written, here or by the ``CHECK`` behind it. ``summary`` is the text the human
    saw. ``candidate_id`` names the candidate the actions apply to (:data:`CandidateAction`
    only); ``None`` makes a session-wide grant (:data:`SessionAction` only). The session must be
    active, ``account`` its own, and the candidate one of its own. Whether the actions suit the
    candidate's state is the caller's to decide; this checks only that they are real actions of
    the right kind.

    A session-wide grant carries the terms its summary named, and only those count when it is
    used: ``search_kinds`` — the searches a ``global_search`` covers, at least one — and
    ``stars_max``, the most a ``paid_search`` may pay. Neither goes on a grant without the
    action it bounds. So does a ``join`` or ``request``: ``join_route`` is the way in its
    summary named (:func:`check_join_route`), and the only one a run may take.
    """
    _check(via, _CHANNELS, "grant channel")
    if not summary.strip():
        raise ValueError("a grant records the approval text the human saw")
    wanted = list(dict.fromkeys(actions))
    if not wanted:
        raise ValueError("a grant needs at least one action")
    allowed = _SESSION_ACTIONS if candidate_id is None else _CANDIDATE_ACTIONS
    what = "session action" if candidate_id is None else "candidate action"
    for action in wanted:
        _check(action, allowed, what)
    kinds = list(dict.fromkeys(search_kinds))
    for kind in kinds:
        _check(kind, _SEARCH_KINDS, "search kind")
    if ("global_search" in wanted) != bool(kinds):
        raise ValueError("a global_search grant names the searches it covers, and only it does")
    if ("paid_search" in wanted) != (stars_max is not None):
        raise ValueError("a paid_search grant names the most it may pay, and only it does")
    if stars_max is not None and stars_max <= 0:
        raise ValueError(f"a paid_search grant pays at least one star, not {stars_max}")
    if bool({"join", "request"} & set(wanted)) != (join_route is not None):
        raise ValueError("a join or request grant names the route it takes, and only it does")
    if join_route is not None:
        check_join_route(join_route)
    with db.transaction(conn):
        session = get_session(conn, session_id)
        if session is None:
            raise KeyError(f"no research session {session_id}")
        if session.state != "active":
            raise ValueError(f"research session {session_id} is stopped and takes no grants")
        if account != session.account:
            raise ValueError(
                f"research session {session_id} acts as {session.account}; a grant for "
                f"{account} would authorize nothing it does"
            )
        if candidate_id is not None:
            candidate = get_candidate(conn, candidate_id)
            if candidate is None or candidate.session_id != session_id:
                raise KeyError(f"no candidate {candidate_id} in research session {session_id}")
        row = conn.execute(
            """INSERT INTO grants(session_id, candidate_id, account, actions, via, summary,
                   granted_at, search_kinds, stars_max, join_route)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (
                session_id,
                candidate_id,
                account,
                json.dumps(wanted),
                via,
                summary,
                clock(now),
                json.dumps(kinds) if kinds else None,
                stars_max,
                join_route,
            ),
        ).fetchone()
    return _grant(row)


JOIN_ROUTES: frozenset[str] = frozenset({"invite", "username", "id"})
"""The ways into a chat a grant can name besides a shared folder, ``folder:<candidate id>``."""
FOLDER_ROUTE = "folder:"


def check_join_route(route: str) -> None:
    """Refuse a ``join_route`` that is none of :data:`JOIN_ROUTES` or ``folder:<id>``."""
    if route in JOIN_ROUTES:
        return
    folder = route.removeprefix(FOLDER_ROUTE)
    if folder != route and folder.isascii() and folder.isdigit() and _row_id(int(folder)):
        return
    raise ValueError(f"unknown join route {route!r}")


def list_grants(
    conn: sqlite3.Connection, session_id: int, *, live_only: bool = False
) -> list[Grant]:
    """A session's grants in the order they were given; only the live ones when asked."""
    live = " AND consumed_at IS NULL AND voided_at IS NULL" if live_only else ""
    rows = conn.execute(
        f"SELECT * FROM grants WHERE session_id = ?{live} ORDER BY id", (session_id,)
    ).fetchall()
    return [_grant(row) for row in rows]


def live_grants(conn: sqlite3.Connection, session_id: int, candidate_id: int | None) -> list[Grant]:
    """Grants neither consumed nor voided for one candidate, or the session-wide ones when
    ``candidate_id`` is ``None``, of a session that is still active."""
    target = "candidate_id IS NULL" if candidate_id is None else "candidate_id = ?"
    params: tuple[int, ...] = (session_id,) if candidate_id is None else (session_id, candidate_id)
    rows = conn.execute(
        f"""SELECT grants.* FROM grants JOIN sessions ON sessions.id = grants.session_id
            WHERE grants.session_id = ? AND {target} AND sessions.state = 'active'
              AND consumed_at IS NULL AND voided_at IS NULL
            ORDER BY grants.id""",
        params,
    ).fetchall()
    return [_grant(row) for row in rows]


def consume_grant(conn: sqlite3.Connection, grant_id: int, now: int | None = None) -> bool:
    """Mark a live grant used up; ``False`` when it was already consumed or voided."""
    with db.transaction(conn):
        cursor = conn.execute(
            "UPDATE grants SET consumed_at = ? "
            "WHERE id = ? AND consumed_at IS NULL AND voided_at IS NULL",
            (clock(now), grant_id),
        )
    return cursor.rowcount > 0


def void_grants(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    candidate_ids: Iterable[int] | None = None,
    now: int | None = None,
) -> int:
    """Void the live grants of a session — all of them, or those of ``candidate_ids`` — and
    return how many."""
    stamp = clock(now)
    with db.transaction(conn):
        if candidate_ids is None:
            cursor = conn.execute(
                "UPDATE grants SET voided_at = ? "
                "WHERE session_id = ? AND consumed_at IS NULL AND voided_at IS NULL",
                (stamp, session_id),
            )
            return cursor.rowcount
        ids = list(dict.fromkeys(candidate_ids))
        voided = 0
        for start in range(0, len(ids), db.IN_BATCH):
            chunk = ids[start : start + db.IN_BATCH]
            cursor = conn.execute(
                "UPDATE grants SET voided_at = ? WHERE session_id = ? "
                f"AND candidate_id IN ({_placeholders(len(chunk))}) "
                "AND consumed_at IS NULL AND voided_at IS NULL",
                (stamp, session_id, *chunk),
            )
            voided += cursor.rowcount
        return voided


# --- exclusions ------------------------------------------------------------------------------


def is_excluded(conn: sqlite3.Connection, identity: str) -> bool:
    """Whether ``identity`` itself is excluded; :func:`excluded_by` also asks every other
    spelling the chat is known by."""
    row = conn.execute("SELECT 1 FROM exclusions WHERE identity = ?", (identity,)).fetchone()
    return row is not None


def _spelled(identity: str) -> tuple[int | None, str | None, str | None]:
    """The peer id, username or invite hash an identity names outright: ``peer:<id>``,
    ``@name``, ``+hash``."""
    if identity.startswith("@"):
        return None, identity[1:].lower() or None, None
    if identity.startswith("+"):
        return None, None, identity[1:] or None
    if identity.startswith("peer:"):
        target = leads.normalize(identity)
        return (None if target is None else target.peer_id), None, None
    return None, None, None


def excluded_by(
    conn: sqlite3.Connection,
    identity: str,
    *,
    peer_id: int | None = None,
    username: str | None = None,
) -> str | None:
    """The exclusion covering the chat ``identity`` names, or ``None``.

    A chat has several spellings — ``@name``, ``peer:<id>``, an invite — and an exclusion
    names one of them. It covers the chat under all of them: the identity itself, the
    ``@username`` and ``peer:<id>`` forms of what is known about it (``peer_id``, ``username``,
    or what ``identity`` spells out), and every spelling of the candidates, in any session, a
    probe tied to the same peer id or username.
    """
    spelled_peer, spelled_name, spelled_invite = _spelled(identity)
    peer_id = peer_id if peer_id is not None else spelled_peer
    username = (username or spelled_name or "").lower() or None
    names = {identity}
    peers: set[int | None] = {peer_id}
    usernames: set[str | None] = {username}
    invites: set[str | None] = {spelled_invite}
    clauses, params = _same_chat(peer_id, username, spelled_invite)
    if clauses:
        for row in conn.execute(
            "SELECT identity, peer_id, username, invite_hash FROM candidates "
            f"WHERE {' OR '.join(clauses)}",
            params,
        ):
            names.add(row["identity"])
            peers.add(row["peer_id"])
            usernames.add(row["username"])
            invites.add(row["invite_hash"])
    names.update(f"@{name}" for name in usernames if name)
    names.update(f"peer:{peer}" for peer in peers if peer is not None)
    names.update(f"+{invite}" for invite in invites if invite)
    wanted = sorted(names)
    row = conn.execute(
        f"SELECT identity FROM exclusions WHERE identity IN ({_placeholders(len(wanted))}) "
        "ORDER BY created_at LIMIT 1",
        wanted,
    ).fetchone()
    return None if row is None else str(row["identity"])


def candidate_excluded(conn: sqlite3.Connection, candidate: Candidate) -> bool:
    """Whether an exclusion covers ``candidate``'s chat under any spelling (:func:`excluded_by`)."""
    found = excluded_by(
        conn, candidate.identity, peer_id=candidate.peer_id, username=candidate.username
    )
    return found is not None


def _covered(conn: sqlite3.Connection, identity: str) -> list[sqlite3.Row]:
    """Every candidate, in any session, of the chat ``identity`` names: that identity, and the
    candidates sharing a peer id, username or invite with it or with a candidate of that
    identity."""
    spelled_peer, spelled_name, spelled_invite = _spelled(identity)
    rows = conn.execute(
        """WITH named(peer_id, username, invite_hash) AS (
               SELECT peer_id, username, invite_hash FROM candidates WHERE identity = ?
               UNION ALL SELECT ?, ?, ?
           )
           SELECT id, session_id, status FROM candidates
           WHERE identity = ? OR EXISTS (
               SELECT 1 FROM named
               WHERE (named.peer_id IS NOT NULL AND named.peer_id = candidates.peer_id)
                  OR (named.username IS NOT NULL AND named.username = candidates.username)
                  OR (named.invite_hash IS NOT NULL AND named.invite_hash = candidates.invite_hash)
           )
           ORDER BY id""",
        (identity, spelled_peer, spelled_name, spelled_invite, identity),
    ).fetchall()
    return list(rows)


def list_exclusions(conn: sqlite3.Connection) -> list[Exclusion]:
    rows = conn.execute("SELECT * FROM exclusions ORDER BY created_at, identity").fetchall()
    return [
        Exclusion(identity=row["identity"], reason=row["reason"], created_at=row["created_at"])
        for row in rows
    ]


def add_exclusion(
    conn: sqlite3.Connection, identity: str, reason: str | None = None, now: int | None = None
) -> int:
    """Exclude ``identity`` from every session, present and future; returns how many existing
    candidates it set aside.

    It covers the chat under every spelling it is known by (:func:`_covered`, the same rule
    :func:`excluded_by` asks with). Candidates no run has acted on yet (:data:`_EXCLUDABLE`) move
    to ``excluded``; a joined one or one waiting for an admission keeps its status, since that
    already happened on Telegram. Either way every live grant of theirs is voided in the same
    transaction — excluding narrows, so it needs no consent and must leave nothing authorized
    behind, not the fetch or source still pending for a chat a run already joined. Excluding
    again keeps the first record.
    """
    if not identity:
        raise ValueError("an exclusion needs an identity")
    stamp = clock(now)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO exclusions(identity, reason, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(identity) DO NOTHING",
            (identity, reason, stamp),
        )
        set_aside = 0
        for row in _covered(conn, identity):
            moved = row["status"] in _EXCLUDABLE
            if moved:
                update_candidate(conn, row["id"], status="excluded")
            voided = void_grants(conn, row["session_id"], candidate_ids=[row["id"]], now=stamp)
            set_aside += bool(moved or voided)
    return set_aside


def remove_exclusion(conn: sqlite3.Connection, identity: str) -> bool:
    """Lift an exclusion; the candidates it moved to ``excluded`` go back to ``proposed`` (their
    voided grants stay void) unless another exclusion still covers them. ``False`` when
    ``identity`` was not excluded."""
    with db.transaction(conn):
        cursor = conn.execute("DELETE FROM exclusions WHERE identity = ?", (identity,))
        if cursor.rowcount == 0:
            return False
        for row in _covered(conn, identity):
            candidate = get_candidate(conn, row["id"])
            if candidate is None or candidate.status != "excluded":
                continue
            if not candidate_excluded(conn, candidate):
                update_candidate(conn, candidate.id, status="proposed")
    return True


# --- searches --------------------------------------------------------------------------------


def _search(row: sqlite3.Row) -> SearchRecord:
    return SearchRecord(
        id=row["id"],
        session_id=row["session_id"],
        kind=row["kind"],
        query=row["query"],
        ran_at=row["ran_at"],
        results=row["results"],
        note=row["note"],
    )


def record_search(
    conn: sqlite3.Connection,
    session_id: int,
    kind: SearchKind,
    query: str,
    *,
    results: int = 0,
    note: str | None = None,
    now: int | None = None,
) -> SearchRecord:
    """Record one Telegram-side search a session ran and how many results it answered with."""
    with db.transaction(conn):
        row = conn.execute(
            "INSERT INTO searches(session_id, kind, query, ran_at, results, note) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
            (session_id, kind, query, clock(now), results, note),
        ).fetchone()
    return _search(row)


def list_searches(conn: sqlite3.Connection, session_id: int) -> list[SearchRecord]:
    rows = conn.execute(
        "SELECT * FROM searches WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    return [_search(row) for row in rows]


# --- scan cursors ----------------------------------------------------------------------------


def _scan(row: sqlite3.Row) -> ScanCursor:
    return ScanCursor(
        session_id=row["session_id"],
        chat=ChatKey(str(row["scope"]), int(row["peer_id"])),
        depth=row["depth"],
        index_id=row["index_id"],
        lead_seq=row["lead_seq"],
        pins_read_at=row["pins_read_at"],
        directory=bool(row["directory"]),
        scanned_at=row["scanned_at"],
    )


def list_scan_cursors(conn: sqlite3.Connection, session_id: int) -> list[ScanCursor]:
    rows = conn.execute(
        "SELECT * FROM chat_scans WHERE session_id = ? ORDER BY scope, peer_id", (session_id,)
    ).fetchall()
    return [_scan(row) for row in rows]


def set_scan_cursor(
    conn: sqlite3.Connection,
    session_id: int,
    chat: ChatKey,
    *,
    depth: int,
    index_id: str,
    lead_seq: int = 0,
    now: int | None = None,
) -> ScanCursor:
    """Record that discovery read ``chat`` up to lead-clock tick ``lead_seq`` of the index
    ``index_id``, at ``depth`` — or, with ``lead_seq`` 0, only that the session reads the chat.

    The depth never grows: a chat reached again by a longer path is still as close to the seeds
    as the shortest one that reached it. The cursor never moves back on the same index; a cursor
    of another index means nothing here and is replaced.
    """
    with db.transaction(conn):
        row = conn.execute(
            """INSERT INTO chat_scans(session_id, scope, peer_id, depth, index_id, lead_seq,
                   scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, scope, peer_id) DO UPDATE SET
                   depth = MIN(depth, excluded.depth),
                   lead_seq = CASE WHEN chat_scans.index_id IS excluded.index_id
                       THEN MAX(chat_scans.lead_seq, excluded.lead_seq)
                       ELSE excluded.lead_seq END,
                   index_id = excluded.index_id,
                   scanned_at = excluded.scanned_at
               RETURNING *""",
            (session_id, chat.scope, chat.peer_id, depth, index_id, lead_seq, clock(now)),
        ).fetchone()
    return _scan(row)


def mark_pins_read(
    conn: sqlite3.Connection, session_id: int, chat: ChatKey, *, depth: int, now: int | None = None
) -> ScanCursor:
    """Record that ``chat``'s pinned posts were read for the session (at ``depth``, when the
    session did not read the chat yet); its cursor is left where it is."""
    stamp = clock(now)
    with db.transaction(conn):
        row = conn.execute(
            """INSERT INTO chat_scans(session_id, scope, peer_id, depth, pins_read_at, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, scope, peer_id) DO UPDATE SET
                   depth = MIN(depth, excluded.depth),
                   pins_read_at = excluded.pins_read_at
               RETURNING *""",
            (session_id, chat.scope, chat.peer_id, depth, stamp, stamp),
        ).fetchone()
    return _scan(row)


def mark_directory(
    conn: sqlite3.Connection, session_id: int, chat: ChatKey, *, depth: int, now: int | None = None
) -> ScanCursor:
    """Record that ``chat`` is a directory — its messages name many chats — for the session."""
    stamp = clock(now)
    with db.transaction(conn):
        row = conn.execute(
            """INSERT INTO chat_scans(session_id, scope, peer_id, depth, directory, scanned_at)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(session_id, scope, peer_id) DO UPDATE SET directory = 1
               RETURNING *""",
            (session_id, chat.scope, chat.peer_id, depth, stamp),
        ).fetchone()
    return _scan(row)
