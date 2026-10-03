"""Authorization: whether a human approved an action for a candidate or a session, and which
searches a session may run.

:func:`authorized` is the one check everything that acts on a candidate, or searches for a
session, goes through; approval, the global search, the run and the documents all read it.
"""

import sqlite3

from grepogram import research_db
from grepogram.models import (
    Candidate,
    CandidateAction,
    Config,
    Grant,
    GrantAction,
    ResearchSession,
    SearchKind,
    SessionAction,
)

_CANDIDATE_ORDER: tuple[CandidateAction, ...] = ("join", "request", "fetch", "add_source")


_SESSION_ORDER: tuple[SessionAction, ...] = ("global_search", "paid_search")


SEARCH_OFF_HINT = "set `chat_search = true` or `post_search = true` under [research]"


_SEARCH_KINDS: tuple[SearchKind, ...] = ("chat_search", "post_search")


def authorized(
    rdb: sqlite3.Connection, target: Candidate | ResearchSession, action: GrantAction
) -> bool:
    """Whether a human approved ``action`` for ``target`` and that approval is still live — the
    one check everything that acts on a candidate, or searches for a session, goes through.

    Only a grant naming ``target`` itself counts: approving a folder or a chat authorizes nothing
    found inside it. The session must be active (stopping voids its grants) — every grant of a
    session is given to its one account (:func:`~grepogram.research.approval.grant`) — and a
    candidate skipped since, or a chat an exclusion covers under any of its spellings, is not
    authorized whatever its grants say.
    """
    if isinstance(target, Candidate):
        session = research_db.get_session(rdb, target.session_id)
        current = research_db.get_candidate(rdb, target.id)
        if current is None or current.status in ("skipped", "excluded"):
            return False
        if research_db.candidate_excluded(rdb, current):
            return False
        candidate_id: int | None = current.id
    else:
        session = research_db.get_session(rdb, target.id)
        candidate_id = None
    if session is None or session.state != "active":
        return False
    return any(
        action in grant.actions for grant in research_db.live_grants(rdb, session.id, candidate_id)
    )


def authorized_actions(rdb: sqlite3.Connection, candidate: Candidate) -> list[CandidateAction]:
    """The actions :func:`authorized` allows for ``candidate`` right now, in the order a run
    takes them."""
    return [action for action in _CANDIDATE_ORDER if authorized(rdb, candidate, action)]


def search_kinds(cfg: Config) -> list[SearchKind]:
    """The global searches ``[research]`` switches on."""
    return [kind for kind in _SEARCH_KINDS if getattr(cfg.research, kind)]


def search_granted(rdb: sqlite3.Connection, session_id: int, action: SessionAction) -> bool:
    """Whether the session holds a live session-wide grant for ``action`` (:func:`authorized`)."""
    session = research_db.get_session(rdb, session_id)
    return session is not None and authorized(rdb, session, action)


def _session_grants(rdb: sqlite3.Connection, session_id: int, action: SessionAction) -> list[Grant]:
    """The live session-wide grants of an active session that hold ``action``."""
    session = research_db.get_session(rdb, session_id)
    if session is None or session.state != "active":
        return []
    return [g for g in research_db.live_grants(rdb, session.id, None) if action in g.actions]


def granted_kinds(rdb: sqlite3.Connection, session_id: int) -> list[SearchKind]:
    """The searches the session's live ``global_search`` approvals cover — the kinds their
    summaries named, whatever ``[research]`` switches on since."""
    covered = {
        kind for g in _session_grants(rdb, session_id, "global_search") for kind in g.search_kinds
    }
    return [kind for kind in _SEARCH_KINDS if kind in covered]


def _paid_ceiling(rdb: sqlite3.Connection, session_id: int) -> int:
    """The most a live ``paid_search`` approval of the session pays, as its summary named it;
    0 without one."""
    return max(
        (g.stars_max or 0 for g in _session_grants(rdb, session_id, "paid_search")), default=0
    )
