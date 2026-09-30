"""What the CLI and the MCP server answer with: the JSON documents of sessions, candidates,
grants and reports."""

import dataclasses
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, get_args

from grepogram import research_db
from grepogram.models import (
    Candidate,
    CandidateStatus,
    CandidateView,
    Config,
    DiscoverReport,
    Evidence,
    Grant,
    ResearchSession,
    RunReport,
)
from grepogram.research.collect import candidate_views, chat_of
from grepogram.research.grants import authorized_actions
from grepogram.research.sessions import ResearchError, horizon, known_session, require_enabled

_STATUS_NAMES: tuple[CandidateStatus, ...] = get_args(CandidateStatus)


def session_document(session: ResearchSession) -> dict[str, Any]:
    """A session as the CLI's ``--json`` and the MCP tools show it, with its history
    :func:`horizon`; each seed is the chat as Telegram names it, ``{scope, peer_id}``."""
    document = dataclasses.asdict(session)
    document["seeds"] = [{"scope": key.scope, "peer_id": key.peer_id} for key in session.seeds]
    return {**document, "horizon": horizon(session)}


def evidence_document(conn: sqlite3.Connection, evidence: Evidence) -> dict[str, Any]:
    """One piece of evidence as the documents show it. The chat it was found in is ``scope`` and
    ``peer_id`` — Telegram's id, whether the index holds the chat or not — and ``chat_id`` is
    that chat's index row *now*, the id ``thread`` and ``context`` take, or ``None`` when the
    index does not hold it (a global search's result)."""
    key = evidence.chat
    row = None if key is None else chat_of(conn, key)
    return {
        "id": evidence.id,
        "candidate_id": evidence.candidate_id,
        "via": evidence.via,
        "origin_key": evidence.origin_key,
        "scope": None if key is None else key.scope,
        "peer_id": None if key is None else key.peer_id,
        "chat_id": None if row is None else row.id,
        "msg_id": evidence.msg_id,
        "snippet": evidence.snippet,
        "found_at": evidence.found_at,
    }


def candidate_document(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, view: CandidateView
) -> dict[str, Any]:
    """One candidate with the three facts kept apart — ``member`` (a probe's answer), ``cached``
    (the index holds it, through ``cached_accounts``) and ``authorized`` (the actions a live
    grant allows) — and every piece of evidence (:func:`evidence_document`). The acting
    account's access hash stays out."""
    document = dataclasses.asdict(view.candidate)
    del document["access_hash"]
    document.update(
        corroboration=view.corroboration,
        overlap=view.overlap,
        cached=view.cached,
        cached_chats=list(view.cached_chats),
        cached_accounts=list(view.cached_accounts),
        authorized=authorized_actions(rdb, view.candidate),
        evidence=[evidence_document(conn, evidence) for evidence in view.evidence],
    )
    return document


def candidates_document(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    statuses: Sequence[str] | None = None,
) -> dict[str, Any]:
    """A session's candidates, best first (:func:`candidate_views`), narrowed to ``statuses``.
    A stopped session still lists what it found."""
    require_enabled(cfg)
    session = known_session(rdb, session_id)
    wanted: list[CandidateStatus] | None = None
    if statuses:
        unknown = [status for status in statuses if status not in _STATUS_NAMES]
        if unknown:
            raise ResearchError(
                f"unknown candidate status {', '.join(map(repr, unknown))}",
                f"statuses: {', '.join(_STATUS_NAMES)}",
            )
        wanted = [status for status in _STATUS_NAMES if status in statuses]
    views = candidate_views(rdb, conn, session, wanted)
    return {
        "session_id": session.id,
        "question": session.question,
        "account": session.account,
        "state": session.state,
        "candidates": [candidate_document(rdb, conn, view) for view in views],
    }


def status_document(
    rdb: sqlite3.Connection, cfg: Config, session_id: int | None = None
) -> dict[str, Any]:
    """Every session in brief, newest first, with every exclusion (``identity``, the ``reason``
    it was given, ``created_at``) — exclusions hold across sessions, so this is where they are
    seen — or one session in full: its limits and progress, how many candidates are in each
    status, the approvals a run has yet to carry out, and the admission requests waiting for a
    chat's admins."""
    require_enabled(cfg)
    if session_id is None:
        listed = []
        for session in research_db.list_sessions(rdb):
            counts = _status_counts(rdb, session.id)
            listed.append(
                {
                    "id": session.id,
                    "question": session.question,
                    "account": session.account,
                    "state": session.state,
                    "created_at": session.created_at,
                    "stopped_at": session.stopped_at,
                    "candidates": sum(counts.values()),
                    "runs": session.progress.get("runs", 0),
                }
            )
        exclusions = [dataclasses.asdict(e) for e in research_db.list_exclusions(rdb)]
        return {"sessions": listed, "exclusions": exclusions}
    session = known_session(rdb, session_id)
    candidates = {c.id: c for c in research_db.list_candidates(rdb, session.id)}
    grants = grant_documents(research_db.list_grants(rdb, session.id, live_only=True), candidates)
    waiting = [
        {"id": c.id, "identity": c.identity, "title": c.title}
        for c in candidates.values()
        if c.status == "pending_admission"
    ]
    return {
        "session": session_document(session),
        "candidates": _status_counts(rdb, session.id),
        "pending_grants": grants,
        "pending_admission": waiting,
    }


def grant_documents(
    grants: Iterable[Grant], candidates: Mapping[int, Candidate]
) -> list[dict[str, Any]]:
    """Grants as ``research_status`` and ``research_approve`` both show one: the candidate it
    names by identity and title as well as by id (``None`` for a session-wide grant), and who
    gave it, how and when. ``candidates`` are the session's, by id."""
    documents: list[dict[str, Any]] = []
    for grant in grants:
        target = None if grant.candidate_id is None else candidates.get(grant.candidate_id)
        documents.append(
            {
                "id": grant.id,
                "candidate_id": grant.candidate_id,
                "identity": None if target is None else target.identity,
                "title": None if target is None else target.title,
                "account": grant.account,
                "actions": list(grant.actions),
                "via": grant.via,
                "granted_at": grant.granted_at,
            }
        )
    return documents


def _status_counts(rdb: sqlite3.Connection, session_id: int) -> dict[str, int]:
    counts = Counter(c.status for c in research_db.list_candidates(rdb, session_id))
    return {status: counts[status] for status in _STATUS_NAMES if counts[status]}


def report_document(report: DiscoverReport | RunReport) -> dict[str, Any]:
    """A discover or run report as the CLI's ``--json`` and the MCP tools answer with it."""
    return dataclasses.asdict(report)
