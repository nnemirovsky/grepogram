"""``research.db``: what research sessions found and what a human decided about it.

A SQLite file of its own next to ``index.db`` (:attr:`grepogram.paths.Paths.research_db_file`),
written with :data:`~grepogram.paths.PRIVATE_FILE_MODE`. The split is the point: the index is
derived from Telegram and may be deleted and rebuilt with one sync, while this file holds the
user's decisions — approvals, exclusions, session history — which nothing can rebuild. For the
same reason a schema this build cannot read is refused without the index's "delete and sync
again" advice (:class:`SchemaError`).

The tables:

``sessions``
    a question explored from seed chats (``chats.id`` of the index) by one account, under
    :class:`~grepogram.models.ResearchLimits` fixed at start; ``active`` until stopped, and
    stopping voids every grant not consumed yet (:func:`stop_session`).
``candidates``
    one per ``(session, identity)`` — ``identity`` is a :mod:`grepogram.leads` target string —
    with its depth, status, what a probe learned, and the ``parent_id`` it was found inside.
    Whether the index already holds the chat is asked of ``index.db`` when needed and never
    stored here: the index can be rebuilt under this file.
``evidence``
    every path that led to a candidate; ``origin_key`` is what corroboration counts
    (:func:`corroboration`), so ten forwards of one post are one piece of evidence.
``grants``
    one human approval each: the concrete actions on one candidate, or session-wide search
    actions, by one account, **through one channel** — ``elicitation`` or ``cli``, enforced by
    a ``CHECK`` and by :func:`add_grant`, the only writer, which takes it as a required argument.
``exclusions``
    identities research never proposes again, in any session; global and persistent.
``searches``
    every Telegram-side search a session ran. Its results are evidence, never ``messages`` rows.
``scans``
    per session and indexed chat, the newest Telegram ``msg_id`` discovery has read and the
    depth that chat's leads are found at, so a run resumes from this file alone.

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

from grepogram import db
from grepogram.models import (
    Candidate,
    CandidateAction,
    CandidateKind,
    CandidateStatus,
    ChatType,
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
    is_account_name,
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

MIGRATIONS: dict[int, tuple[str, ...]] = {1: _V1}
"""Schema version → the step that brings the file to it, from 1 without a gap; append-only."""
SCHEMA_VERSION = max(MIGRATIONS)

_STATES: frozenset[str] = frozenset(get_args(ResearchState))
_KINDS: frozenset[str] = frozenset(get_args(CandidateKind))
_STATUSES: frozenset[str] = frozenset(get_args(CandidateStatus))
_VIAS: frozenset[str] = frozenset(get_args(EvidenceVia))
_CANDIDATE_ACTIONS: frozenset[str] = frozenset(get_args(CandidateAction))
_SESSION_ACTIONS: frozenset[str] = frozenset(get_args(SessionAction))
_CHANNELS: frozenset[str] = frozenset(get_args(GrantChannel))
_SEARCH_KINDS: frozenset[str] = frozenset(get_args(SearchKind))
_EXCLUDABLE = ("proposed", "approved", "skipped")
"""Statuses an exclusion moves to ``excluded``: decisions not acted on yet. A chat already
joined or fetched stays what it is — excluding it narrows future discovery, it undoes nothing."""

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
    pending = range(current + 1, SCHEMA_VERSION + 1)
    if current < 0 or any(version not in MIGRATIONS for version in pending):
        raise SchemaError(f"research.db schema v{current} cannot be upgraded; {_KEEP_HINT}")
    with db.transaction(conn):
        for version in pending:
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (META_SCHEMA_VERSION, str(SCHEMA_VERSION)),
        )
    return SCHEMA_VERSION


# --- helpers ---------------------------------------------------------------------------------


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


def _check(value: str, allowed: frozenset[str], what: str) -> None:
    if value not in allowed:
        raise ValueError(f"unknown {what} {value!r}; expected one of {', '.join(sorted(allowed))}")


def _flag(value: object) -> bool | None:
    return None if value is None else bool(value)


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


# --- sessions --------------------------------------------------------------------------------


def _session(row: sqlite3.Row) -> ResearchSession:
    stored = json.loads(row["limits"])
    known = {field.name for field in dataclasses.fields(ResearchLimits)}
    return ResearchSession(
        id=row["id"],
        question=row["question"],
        account=row["account"],
        seeds=tuple(json.loads(row["seeds"])),
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
    seeds: Iterable[int],
    limits: ResearchLimits,
    now: int | None = None,
) -> ResearchSession:
    """Start a session; it is ``active`` until :func:`stop_session`."""
    if not question.strip():
        raise ValueError("a research session needs a question")
    if not is_account_name(account):
        raise ValueError(f"invalid account name: {account!r}")
    seed_ids = list(dict.fromkeys(int(seed) for seed in seeds))
    with db.transaction(conn):
        row = conn.execute(
            "INSERT INTO sessions(question, account, seeds, limits, created_at) "
            "VALUES (?, ?, ?, ?, ?) RETURNING *",
            (
                question.strip(),
                account,
                json.dumps(seed_ids),
                json.dumps(dataclasses.asdict(limits)),
                _now(now),
            ),
        ).fetchone()
    return _session(row)


def get_session(conn: sqlite3.Connection, session_id: int) -> ResearchSession | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return None if row is None else _session(row)


def list_sessions(
    conn: sqlite3.Connection, state: ResearchState | None = None
) -> list[ResearchSession]:
    """Every session, newest first; only those in ``state`` when it is given."""
    if state is None:
        rows = conn.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()
    else:
        _check(state, _STATES, "session state")
        rows = conn.execute(
            "SELECT * FROM sessions WHERE state = ? ORDER BY id DESC", (state,)
        ).fetchall()
    return [_session(row) for row in rows]


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
    stamp = _now(now)
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
    fills fields it did not know yet. An excluded identity gets no row at all and answers
    ``None`` — exclusions are global, so no session proposes one again.
    """
    _check(kind, _KINDS, "candidate kind")
    if depth < 0:
        raise ValueError(f"a candidate's depth cannot be negative: {depth}")
    with db.transaction(conn):
        if is_excluded(conn, identity):
            return None
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
                   parent_id = COALESCE(parent_id, excluded.parent_id)
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
                _now(now),
            ),
        ).fetchone()
    return _candidate(row)


def get_candidate(conn: sqlite3.Connection, candidate_id: int) -> Candidate | None:
    row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    return None if row is None else _candidate(row)


def candidate_by_identity(
    conn: sqlite3.Connection, session_id: int, identity: str
) -> Candidate | None:
    row = conn.execute(
        "SELECT * FROM candidates WHERE session_id = ? AND identity = ?", (session_id, identity)
    ).fetchone()
    return None if row is None else _candidate(row)


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
        for status in wanted:
            _check(status, _STATUSES, "candidate status")
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
    identity, session, kind, depth and parent are what it *is*.
    """
    unknown = set(fields) - _CANDIDATE_FIELDS
    if unknown:
        raise ValueError(f"cannot set candidate field(s): {', '.join(sorted(unknown))}")
    if "status" in fields:
        _check(fields["status"], _STATUSES, "candidate status")
    if "type" in fields and fields["type"] is not None:
        _check(fields["type"], frozenset(get_args(ChatType)), "chat type")
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
    return Evidence(
        id=row["id"],
        candidate_id=row["candidate_id"],
        via=row["via"],
        origin_key=row["origin_key"],
        chat_id=row["chat_id"],
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
    chat_id: int | None = None,
    msg_id: int | None = None,
    snippet: str | None = None,
    now: int | None = None,
) -> bool:
    """Record one path to a candidate; ``False`` when that exact path is already recorded."""
    _check(via, _VIAS, "evidence path")
    if not origin_key:
        raise ValueError("evidence needs an origin key")
    with db.transaction(conn):
        cursor = conn.execute(
            """INSERT INTO evidence(candidate_id, via, chat_id, msg_id, origin_key, snippet,
                   found_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            (candidate_id, via, chat_id, msg_id, origin_key, snippet, _now(now)),
        )
    return cursor.rowcount > 0


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
    now: int | None = None,
) -> Grant:
    """Record one human approval — the only way a grant comes to exist.

    ``via`` names the channel the human answered through and has no default: a grant without
    one cannot be written, here or by the ``CHECK`` behind it. ``summary`` is the text the human
    saw. ``candidate_id`` names the candidate the actions apply to (:data:`CandidateAction`
    only); ``None`` makes a session-wide grant (:data:`SessionAction` only). The session must be
    active and the candidate one of its own. Whether the actions suit the candidate's state is
    the caller's to decide; this checks only that they are real actions of the right kind.
    """
    _check(via, _CHANNELS, "grant channel")
    if not summary.strip():
        raise ValueError("a grant records the approval text the human saw")
    if not is_account_name(account):
        raise ValueError(f"invalid account name: {account!r}")
    wanted = list(dict.fromkeys(actions))
    if not wanted:
        raise ValueError("a grant needs at least one action")
    allowed = _SESSION_ACTIONS if candidate_id is None else _CANDIDATE_ACTIONS
    what = "session action" if candidate_id is None else "candidate action"
    for action in wanted:
        _check(action, allowed, what)
    with db.transaction(conn):
        session = get_session(conn, session_id)
        if session is None:
            raise KeyError(f"no research session {session_id}")
        if session.state != "active":
            raise ValueError(f"research session {session_id} is stopped and takes no grants")
        if candidate_id is not None:
            candidate = get_candidate(conn, candidate_id)
            if candidate is None or candidate.session_id != session_id:
                raise KeyError(f"no candidate {candidate_id} in research session {session_id}")
        row = conn.execute(
            """INSERT INTO grants(session_id, candidate_id, account, actions, via, summary,
                   granted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (session_id, candidate_id, account, json.dumps(wanted), via, summary, _now(now)),
        ).fetchone()
    return _grant(row)


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
            (_now(now), grant_id),
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
    stamp = _now(now)
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
    row = conn.execute("SELECT 1 FROM exclusions WHERE identity = ?", (identity,)).fetchone()
    return row is not None


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
    candidates it moved to ``excluded``.

    Those are the candidates no run has acted on yet (:data:`_EXCLUDABLE`), in every session,
    and their live grants are voided in the same transaction — excluding narrows, so it needs no
    consent and must leave nothing authorized behind. Excluding again keeps the first record.
    """
    if not identity:
        raise ValueError("an exclusion needs an identity")
    stamp = _now(now)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO exclusions(identity, reason, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(identity) DO NOTHING",
            (identity, reason, stamp),
        )
        rows = conn.execute(
            f"""UPDATE candidates SET status = 'excluded'
                WHERE identity = ? AND status IN ({_placeholders(len(_EXCLUDABLE))})
                RETURNING id, session_id""",
            (identity, *_EXCLUDABLE),
        ).fetchall()
        for row in rows:
            void_grants(conn, row["session_id"], candidate_ids=[row["id"]], now=stamp)
    return len(rows)


def remove_exclusion(conn: sqlite3.Connection, identity: str) -> bool:
    """Lift an exclusion; the candidates it moved to ``excluded`` go back to ``proposed`` (their
    voided grants stay void). ``False`` when ``identity`` was not excluded."""
    with db.transaction(conn):
        cursor = conn.execute("DELETE FROM exclusions WHERE identity = ?", (identity,))
        if cursor.rowcount == 0:
            return False
        conn.execute(
            "UPDATE candidates SET status = 'proposed' WHERE identity = ? AND status = 'excluded'",
            (identity,),
        )
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
    _check(kind, _SEARCH_KINDS, "search kind")
    with db.transaction(conn):
        row = conn.execute(
            "INSERT INTO searches(session_id, kind, query, ran_at, results, note) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
            (session_id, kind, query, _now(now), results, note),
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
        chat_id=row["chat_id"],
        depth=row["depth"],
        msg_id=row["msg_id"],
        scanned_at=row["scanned_at"],
    )


def scan_cursor(conn: sqlite3.Connection, session_id: int, chat_id: int) -> ScanCursor | None:
    row = conn.execute(
        "SELECT * FROM scans WHERE session_id = ? AND chat_id = ?", (session_id, chat_id)
    ).fetchone()
    return None if row is None else _scan(row)


def list_scan_cursors(conn: sqlite3.Connection, session_id: int) -> list[ScanCursor]:
    rows = conn.execute(
        "SELECT * FROM scans WHERE session_id = ? ORDER BY chat_id", (session_id,)
    ).fetchall()
    return [_scan(row) for row in rows]


def set_scan_cursor(
    conn: sqlite3.Connection,
    session_id: int,
    chat_id: int,
    *,
    depth: int,
    msg_id: int,
    now: int | None = None,
) -> ScanCursor:
    """Record that discovery read ``chat_id`` up to ``msg_id`` at ``depth``.

    The cursor never moves back and the depth never grows: a chat reached again by a longer
    path is still as close to the seeds as the shortest one that reached it.
    """
    with db.transaction(conn):
        row = conn.execute(
            """INSERT INTO scans(session_id, chat_id, depth, msg_id, scanned_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id, chat_id) DO UPDATE SET
                   depth = MIN(depth, excluded.depth),
                   msg_id = MAX(msg_id, excluded.msg_id),
                   scanned_at = excluded.scanned_at
               RETURNING *""",
            (session_id, chat_id, depth, msg_id, _now(now)),
        ).fetchone()
    return _scan(row)
