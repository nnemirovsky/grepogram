"""Offline discovery: turning the leads of a session's chats into candidates, talking to no
one.

:func:`scan_targets` names the chats a session reads, :func:`discover_offline` scans them and
proposes the best-corroborated new candidates within the session's caps (:func:`_propose`), and
:func:`_directories` marks the chats that list enough others to be directories.
"""

import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from grepogram import db, leads, research_db
from grepogram.models import (
    Candidate,
    ChatKey,
    ChatRow,
    Config,
    DiscoverReport,
    ResearchSession,
    ScanCursor,
)
from grepogram.research.collect import (
    Lead,
    _own,
    chat_key,
    chat_level,
    chat_of,
    collect_leads,
    held_rows,
    overlap,
    question_terms,
)
from grepogram.research.sessions import active_session, require_enabled

log = logging.getLogger(__name__)


DIRECTORY_MIN_CHATS = 10
"""How many distinct chats a chat's messages must name before discovery calls it a *directory*
— a channel or group that exists to list others — and records ``directory`` evidence on every
lead found in it. The count is over its stored links and forward origins and, once read, its
pinned posts' leads; the flag is kept for the session once it is set."""


@dataclass(slots=True)
class _Found:
    """Every lead to one identity in this call, and the shallowest depth any of them gives."""

    depth: int
    leads: list[Lead] = field(default_factory=list)

    @property
    def first(self) -> Lead:
        return self.leads[0]

    def rank(self, terms: frozenset[str]) -> tuple[int, int, int]:
        return (
            -len({lead.origin_key for lead in self.leads}),
            -overlap(terms, (lead.snippet for lead in self.leads)),
            self.depth,
        )


@dataclass(frozen=True, slots=True)
class ScanTarget:
    """One chat a session reads: its index row now, the depth its leads are found at, and the
    lead-clock tick discovery has read it to on this index (``0``: from the start)."""

    chat: ChatRow
    depth: int
    after: int
    cursor: ScanCursor | None


def scan_targets(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, session: ResearchSession
) -> dict[int, ScanTarget]:
    """``chat row → ScanTarget`` for every chat the session reads: its seeds at depth 0 and
    every chat a run fetched for it, at the depth that chat was found at — each with the
    discussion group of a channel among them, at the channel's depth, since a channel's comments
    are part of what it says. Chats are named by ``(scope, peer_id)`` in ``research.db`` and
    found in the index as it is now; one the index does not hold (yet, or any more) is not read.
    A cursor kept for another index (:func:`grepogram.db.index_id`) reads the chat from the
    start."""
    index = db.index_id(conn)
    cursors = {cursor.chat: cursor for cursor in research_db.list_scan_cursors(rdb, session.id)}
    depths: dict[ChatKey, int] = dict.fromkeys(session.seeds, 0)
    for key, cursor in cursors.items():
        depths[key] = min(cursor.depth, depths.get(key, cursor.depth))
    targets: dict[int, ScanTarget] = {}

    def add(chat: ChatRow, depth: int) -> None:
        known = targets.get(chat.id)
        if known is not None and known.depth <= depth:
            return
        cursor = cursors.get(chat_key(chat))
        after = cursor.lead_seq if cursor is not None and cursor.index_id == index else 0
        targets[chat.id] = ScanTarget(chat=chat, depth=depth, after=after, cursor=cursor)

    for key, depth in sorted(depths.items(), key=lambda item: item[1]):
        chat = chat_of(conn, key)
        if chat is None:
            continue
        add(chat, depth)
        if chat.type == "channel":
            group = db.get_discussion_chat(conn, chat.id)
            if group is not None:
                add(group, depth)
    return targets


@dataclass(slots=True)
class _Proposal:
    """What proposing one batch of leads did (:func:`_propose`)."""

    new: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    in_session: int = 0
    beyond_depth: int = 0
    excluded: int = 0
    over_cap: int = 0
    held_back: set[int] = field(default_factory=set)
    """Chats (index rows) some of whose leads the per-call cap cut: their cursors stay."""
    full: bool = False
    """The session's ``max_session_candidates`` ceiling cut something."""


def room(rdb: sqlite3.Connection, session: ResearchSession) -> int:
    """How many new candidates one call may still add: ``max_candidates``, or fewer when the
    session's ``max_session_candidates`` ceiling is closer."""
    left = session.limits.max_session_candidates - research_db.count_candidates(rdb, session.id)
    return max(0, min(session.limits.max_candidates, left))


def _propose(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    targets: Mapping[int, ScanTarget],
    found_leads: Iterable[Lead],
    stamp: int,
) -> _Proposal:
    """Turn leads into candidates and evidence, inside the caller's ``research.db`` transaction.

    A lead to a chat the session already reads is ``in_session``; a new identity becomes a
    ``proposed`` candidate one hop deeper than the chat it was found in — never beyond
    ``max_depth``, never an excluded one, at most :func:`room` of them, best corroborated first —
    and every lead is kept as evidence, on an existing candidate as on a new one. A forward's origin
    is proposed by its peer id alone even when a sync saw it under a username: that name is a hint
    the probe checks (:func:`~grepogram.research.probing._probe_named_peer`), never an identity — a
    stale one would fold two chats into one candidate.
    """
    limits = session.limits
    proposal = _Proposal()
    found: dict[str, _Found] = {}
    for lead in found_leads:
        if targets.keys() & set(held_rows(conn, lead.chat.peer_id, lead.chat.username)):
            proposal.in_session += 1
            continue
        depth = targets[lead.row_id].depth + 1
        entry = found.setdefault(lead.identity, _Found(depth=depth))
        entry.depth = min(entry.depth, depth)
        entry.leads.append(lead)
    terms = question_terms(session.question)
    fresh: list[_Found] = []
    for identity, entry in found.items():
        chat = entry.first.chat
        existing = research_db.candidate_for(
            rdb,
            session.id,
            identity,
            peer_id=chat.peer_id,
            username=chat.username,
            invite_hash=chat.invite_hash,
        )
        if existing is not None:
            if _record(rdb, existing, entry, stamp) and existing.id not in proposal.updated:
                proposal.updated.append(existing.id)
        elif entry.depth > limits.max_depth:
            proposal.beyond_depth += 1
        elif research_db.excluded_by(rdb, identity, peer_id=chat.peer_id, username=chat.username):
            proposal.excluded += 1
        else:
            fresh.append(entry)
    fresh.sort(key=lambda entry: entry.rank(terms))
    cap = room(rdb, session)
    kept, over = fresh[:cap], fresh[cap:]
    for entry in kept:
        chat = entry.first.chat
        candidate = research_db.add_candidate(
            rdb,
            session.id,
            entry.first.identity,
            entry.first.kind,
            entry.depth,
            peer_id=chat.peer_id,
            username=chat.username,
            invite_hash=chat.invite_hash,
            addlist_slug=chat.slug,
            now=stamp,
        )
        if candidate is not None:  # None only for an excluded identity, asked above
            _record(rdb, candidate, entry, stamp)
            proposal.new.append(candidate.id)
    proposal.over_cap = len(over)
    if over and cap < limits.max_candidates:
        # the session's ceiling, not this call's cap: nothing held back could ever be proposed
        proposal.full = True
    else:
        proposal.held_back = {lead.row_id for entry in over for lead in entry.leads}
    return proposal


def _record(rdb: sqlite3.Connection, candidate: Candidate, entry: _Found, now: int) -> bool:
    """Add every lead of ``entry`` as evidence of ``candidate``; whether any path was new."""
    added = False
    for lead in entry.leads:
        added |= research_db.add_evidence(
            rdb,
            candidate.id,
            lead.via,
            lead.origin_key,
            chat=lead.found_in,
            msg_id=lead.msg_id,
            snippet=lead.snippet,
            now=now,
        )
    return added


def _directories(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    targets: Mapping[int, ScanTarget],
    chats: Iterable[int],
    extra: Mapping[int, set[str]],
    stamp: int,
) -> list[int]:
    """Tell which of ``chats`` are directories (:data:`DIRECTORY_MIN_CHATS`) and put a
    ``directory`` path beside every lead the session found in one; returns the directory rows.

    ``extra`` adds identities read off messages the index does not store (pinned posts). The
    ``directory`` path shares the lead's origin key, so it shows where a candidate came from without
    counting as another piece of corroboration. Nothing is approved by it: a directory grants
    nothing for what it lists, like every chat (:func:`~grepogram.research.grants.authorized`).
    """
    found: list[int] = []
    for chat_id in dict.fromkeys(chats):
        target = targets.get(chat_id)
        if target is None:
            continue
        key = chat_key(target.chat)
        if not (target.cursor is not None and target.cursor.directory):
            named = set(extra.get(chat_id, ()))
            for value in db.chat_link_targets(conn, chat_id):
                parsed = leads.normalize(value)
                chat = None if parsed is None else chat_level(parsed)
                if chat is not None:
                    named.add(chat.target)
            named -= _own(target.chat)
            if len(named) < DIRECTORY_MIN_CHATS:
                continue
            research_db.mark_directory(rdb, session.id, key, depth=target.depth, now=stamp)
            log.info("research session %d: chat %s is a directory", session.id, chat_id)
        found.append(chat_id)
        for evidence in research_db.evidence_from(rdb, session.id, key):
            if evidence.via != "directory":
                research_db.add_evidence(
                    rdb,
                    evidence.candidate_id,
                    "directory",
                    evidence.origin_key,
                    chat=key,
                    msg_id=evidence.msg_id,
                    snippet=evidence.snippet,
                    now=stamp,
                )
    return found


def discover_offline(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    *,
    now: int | None = None,
) -> DiscoverReport:
    """Read the session's chats from where it last stopped and record what they lead to.

    Offline: nothing here talks to Telegram. New identities become ``proposed`` candidates one
    hop deeper than the chat they were found in (:func:`_propose`), a chat whose messages name
    many others is marked a directory (:func:`_directories`), and the candidates, their evidence
    and the moved scan cursors are written in one transaction. See the module docstring for
    what counts as a lead and as corroboration.
    """
    require_enabled(cfg)
    session = active_session(rdb, session_id)
    stamp = research_db.clock(now)
    index = db.index_id(conn)
    targets = scan_targets(rdb, conn, session)
    scan = collect_leads(conn, {chat_id: target.after for chat_id, target in targets.items()})
    with db.transaction(rdb):
        proposal = _propose(rdb, conn, session, targets, scan.leads, stamp)
        read = {lead.row_id for lead in scan.leads}
        directories = _directories(rdb, conn, session, targets, read, {}, stamp)
        for chat_id, newest in scan.newest.items():
            if chat_id not in proposal.held_back:
                target = targets[chat_id]
                research_db.set_scan_cursor(
                    rdb,
                    session.id,
                    chat_key(target.chat),
                    depth=target.depth,
                    index_id=index,
                    lead_seq=newest,
                    now=stamp,
                )
    report = DiscoverReport(
        session_id=session.id,
        chats_scanned=len(scan.newest),
        messages_scanned=scan.messages,
        text_fallback=scan.text_fallback,
        leads=len(scan.leads) - proposal.in_session,
        in_session=proposal.in_session,
        people=scan.people,
        new_candidates=proposal.new,
        updated_candidates=proposal.updated,
        beyond_depth=proposal.beyond_depth,
        excluded=proposal.excluded,
        over_cap=proposal.over_cap,
        truncated=bool(proposal.held_back),
        session_full=proposal.full,
        directories=directories,
    )
    log.info(
        "research session %d: %d chat(s) read, %d lead(s), %d new candidate(s), "
        "%d beyond depth, %d excluded, %d over the cap",
        session.id,
        report.chats_scanned,
        report.leads,
        len(proposal.new),
        proposal.beyond_depth,
        proposal.excluded,
        proposal.over_cap,
    )
    return report
