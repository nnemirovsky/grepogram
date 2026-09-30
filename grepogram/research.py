"""Research: find chats the index does not hold yet, starting from a question and seed chats.

This module is the one both the CLI and the MCP server drive; what it decides is kept in
``research.db`` (:mod:`grepogram.research_db`), what it reads is ``index.db``. Every entry point
refuses while ``[research] enabled`` is false (:func:`require_enabled`).

**Offline discovery** (:func:`discover_offline`) talks to no one. It reads the messages the
index already holds of a session's seed chats — and of any chat a later run fetched for it, at
the depth that chat was found at — and turns every Telegram destination they name into a
*candidate* one hop further out:

- the links a message was stored with (``message_links``: visible URLs, hidden ``text_url``
  hyperlinks, ``@mentions``, URL buttons, link previews);
- a forward's structured origin (``messages.fwd_peer_id``);
- for a row stored before links were captured (``meta['links_captured_from']``), whatever
  :func:`grepogram.leads.text_leads` finds in its text — visible URLs and mentions only, which
  the report counts as ``text_fallback`` so a caller can say that hidden links were not seen.

A candidate is a chat, not a post: ``@name/123`` and ``t.me/c/<id>/<post>`` lead to ``@name``
and ``peer:<marked id>``, the post staying in the evidence. A user id (a mention by id, a
forward from a person) names a person rather than a chat to index and is not a candidate; a
chat the session already reads (a seed, or one it fetched) is not one either.

Three facts about a candidate are never conflated: whether the acting account is a **member**
(a probe's answer, ``candidates.member``), whether the chat is **cached** — already in
``index.db``, and through which accounts (:func:`cached_in`, asked of the index every time and
never stored, since the index can be rebuilt under ``research.db``) — and whether a human
**authorized** anything for it (a live grant).

**Corroboration** counts distinct *origin keys*, not messages: every copy of one post — the
post itself where it is indexed and each forward of it, wherever it landed — shares the key
``post:<peer>/<msg>``, so a post forwarded into ten chats is one piece of evidence rather than
ten. Candidates rank by corroboration, then by how many of the question's terms their evidence
snippets share, then by depth. ``max_candidates`` caps how many *new* candidates one call adds;
a chat whose leads the cap cut keeps its scan cursor, so the next call reads them again.

Message text never reaches the log above DEBUG; counts do.
"""

import logging
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from grepogram import db, leads, research_db, stem
from grepogram.filters import resolve_chats
from grepogram.leads import LeadTarget
from grepogram.models import (
    Candidate,
    CandidateKind,
    CandidateStatus,
    CandidateView,
    ChatRow,
    Config,
    DiscoverReport,
    EvidenceVia,
    LinkKind,
    MessageRow,
    ResearchLimits,
    ResearchSession,
)

log = logging.getLogger(__name__)

ENABLE_HINT = "set `enabled = true` under [research] in config.toml to allow research"
SNIPPET_CHARS = 240
"""The most of a message's text one piece of evidence keeps."""
_MIN_TERM = 3
"""Question tokens shorter than this ("a", "in", "из") say nothing about relevance."""

_VIA_OF_LINK: dict[LinkKind, EvidenceVia] = {
    "link": "link",
    "text_url": "text_url",
    "mention": "mention",
    "button": "button",
    "webpage": "webpage",
}


class ResearchError(Exception):
    """A research request that cannot be carried out; ``hint`` says what to do about it."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class ResearchDisabled(ResearchError):
    """``[research] enabled`` is false, the default: research refuses every request."""

    def __init__(self) -> None:
        super().__init__("research is disabled", ENABLE_HINT)


class UnknownSession(ResearchError):
    def __init__(self, session_id: int) -> None:
        super().__init__(
            f"no research session {session_id}", "list sessions with `grepogram research status`"
        )


class SessionStopped(ResearchError):
    def __init__(self, session_id: int) -> None:
        super().__init__(
            f"research session {session_id} is stopped",
            "start a new session to explore further; a stopped one keeps its history",
        )


def require_enabled(cfg: Config) -> None:
    """Raise :class:`ResearchDisabled` unless ``[research] enabled`` is true."""
    if not cfg.research.enabled:
        raise ResearchDisabled


# --- sessions --------------------------------------------------------------------------------


def start_session(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    question: str,
    seeds: Sequence[str],
    account: str,
    limits: ResearchLimits | None = None,
    *,
    now: int | None = None,
) -> ResearchSession:
    """Start exploring ``question`` from the indexed chats ``seeds`` select, as ``account``.

    ``seeds`` are chat specs as ``search --chat`` takes them (:func:`grepogram.filters.
    resolve_chats`: an id, ``@name``, a link, a folder, free text, ``account:<name>``); one that
    selects nothing raises :class:`~grepogram.filters.UnknownChat`. ``account`` is the account a
    later run joins and fetches as, and must be one the config knows. ``limits`` default to the
    ``[research]`` section's. Nothing touches Telegram.
    """
    require_enabled(cfg)
    if not question.strip():
        raise ResearchError("a research session needs a question")
    known = cfg.account_names()
    if account not in known:
        raise ResearchError(
            f"unknown account {account!r}; known: {', '.join(known)}",
            f"sign it in with `grepogram auth --account {account}` first",
        )
    if not seeds:
        raise ResearchError(
            "a research session needs at least one seed chat",
            "name indexed chats to start from, as `search --chat` takes them",
        )
    chosen = sorted(resolve_chats(conn, cfg, seeds))
    session = research_db.create_session(
        rdb,
        question=question,
        account=account,
        seeds=chosen,
        limits=limits or cfg.research.limits(),
        now=now,
    )
    log.info(
        "research session %d started as %s from %d seed chat(s)", session.id, account, len(chosen)
    )
    return session


def active_session(rdb: sqlite3.Connection, session_id: int) -> ResearchSession:
    """The session ``session_id``, which must exist and still be active."""
    session = research_db.get_session(rdb, session_id)
    if session is None:
        raise UnknownSession(session_id)
    if session.state != "active":
        raise SessionStopped(session_id)
    return session


# --- leads -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Lead:
    """One path from a stored message to a chat: the candidate ``identity`` it names (chat-level,
    a :mod:`grepogram.leads` target), how (``via``), where (``chat_id`` — the index row — and
    Telegram ``msg_id``), the ``origin_key`` corroboration counts, and a snippet of the text."""

    identity: str
    kind: CandidateKind
    target: LeadTarget
    """What the message named exactly, a post included."""
    chat: LeadTarget
    """The chat ``target`` is in: ``target`` itself unless it names a post."""
    via: EvidenceVia
    chat_id: int
    msg_id: int
    origin_key: str
    snippet: str | None = None


@dataclass(slots=True, kw_only=True)
class LeadScan:
    """What :func:`collect_leads` read: the leads, the newest ``msg_id`` seen per chat (what a
    scan cursor moves to), how many messages carried a lead and how many of those were read by
    the text fallback alone."""

    leads: list[Lead] = field(default_factory=list)
    newest: dict[int, int] = field(default_factory=dict)
    messages: int = 0
    text_fallback: int = 0
    people: int = 0
    """Leads naming a person (a user id) rather than a chat, left out."""


def chat_level(target: LeadTarget) -> tuple[CandidateKind, str, LeadTarget] | None:
    """The chat a lead names, as ``(kind, identity, chat target)``; ``None`` for a person.

    A post leads to its chat; a peer named by a positive (user) id is a person, not a chat.
    """
    if target.kind == "username":
        return "username", target.target, target
    if target.kind == "post" and target.username is not None:
        chat = leads.username(target.username)
        return None if chat is None else ("username", chat.target, chat)
    if target.kind in ("peer", "private_post") and target.peer_id is not None:
        if target.peer_id > 0:
            return None
        chat = leads.peer(target.peer_id)
        return None if chat is None else ("peer", chat.target, chat)
    if target.kind == "invite":
        return "invite", target.target, target
    if target.kind == "addlist":
        return "addlist", target.target, target
    return None


def origin_key(message: MessageRow, chat: ChatRow) -> str:
    """The key every copy of ``message``'s content shares.

    A forward of a post is ``post:<origin peer>/<origin msg>``, as is that post where it is
    itself indexed (a channel or supergroup, whose ``msg_id`` is global); a forward known only by
    its author is ``fwd:<author>@<original date>``; any other message is ``msg:<chat row>/<msg>``.
    """
    if message.fwd_peer_id is not None and message.fwd_msg_id is not None:
        return f"post:{message.fwd_peer_id}/{message.fwd_msg_id}"
    if message.fwd_peer_id is not None:
        return f"fwd:{message.fwd_peer_id}@{message.fwd_date or 0}"
    if chat.is_shared:
        return f"post:{chat.peer_id}/{message.msg_id}"
    return f"msg:{chat.id}/{message.msg_id}"


def snippet(text: str, needle: str | None = None) -> str | None:
    """At most :data:`SNIPPET_CHARS` of ``text``, whitespace collapsed, around ``needle`` when
    the text holds it; ``None`` for no text."""
    flat = " ".join(text.split())
    if not flat:
        return None
    if len(flat) <= SNIPPET_CHARS:
        return flat
    at = flat.lower().find(needle.lower()) if needle else -1
    start = 0 if at < 0 else max(0, min(at - SNIPPET_CHARS // 3, len(flat) - SNIPPET_CHARS))
    piece = flat[start : start + SNIPPET_CHARS]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET_CHARS < len(flat) else "")


def _needle(target: LeadTarget) -> str | None:
    return target.username or target.invite_hash or target.slug


def collect_leads(
    conn: sqlite3.Connection, chat_ids: Iterable[int], since_msg_ids: Mapping[int, int]
) -> LeadScan:
    """Every lead the stored messages of ``chat_ids`` above ``since_msg_ids[chat]`` name.

    Links come from ``message_links`` and forward origins from ``messages.fwd_peer_id``; a row
    stored before capture (:func:`grepogram.db.links_captured_from`) that has neither is read by
    :func:`grepogram.leads.text_leads`. A lead to the very chat it was found in is not one. Only
    rows up to the newest ``msg_id`` seen when the chat was first asked are read, so a sync
    storing rows meanwhile cannot slip a message past the cursor this returns.
    """
    scan = LeadScan()
    captured_from = db.links_captured_from(conn)
    for chat_id in dict.fromkeys(chat_ids):
        chat = db.get_chat(conn, chat_id)
        if chat is None:
            continue
        after = since_msg_ids.get(chat_id, 0)
        newest = db.newest_msg_id(conn, chat_id)
        scan.newest[chat_id] = max(after, newest)
        rows = [
            m for m in db.lead_messages(conn, chat_id, after, captured_from) if m.msg_id <= newest
        ]
        stored = db.message_links(conn, (m.id for m in rows if m.id is not None))
        own = {f"peer:{chat.peer_id}", *([f"@{chat.username.lower()}"] if chat.username else [])}
        for message in rows:
            found = _message_leads(message, chat, stored.get(message.id or 0), captured_from, scan)
            kept = [lead for lead in found if lead.identity not in own]
            if found:
                scan.messages += 1
            scan.leads.extend(kept)
    return scan


def _message_leads(
    message: MessageRow,
    chat: ChatRow,
    stored: tuple[tuple[LinkKind, str], ...] | None,
    captured_from: int,
    scan: LeadScan,
) -> list[Lead]:
    key = origin_key(message, chat)
    named: list[tuple[EvidenceVia, LeadTarget]] = []
    links = stored
    if links is None and message.id is not None and message.id < captured_from:
        links = leads.text_leads(message.text)
        if links:
            scan.text_fallback += 1
    for link_kind, value in links or ():
        target = leads.normalize(value)
        if target is not None:
            named.append((_VIA_OF_LINK[link_kind], target))
    if message.fwd_peer_id is not None:
        origin = leads.peer(message.fwd_peer_id)
        if origin is not None:
            named.append(("forward", origin))
    found: list[Lead] = []
    for via, target in named:
        level = chat_level(target)
        if level is None:
            scan.people += 1
            continue
        kind, identity, chat_target = level
        found.append(
            Lead(
                identity=identity,
                kind=kind,
                target=target,
                chat=chat_target,
                via=via,
                chat_id=chat.id,
                msg_id=message.msg_id,
                origin_key=key,
                snippet=snippet(message.text, _needle(target)),
            )
        )
    return found


# --- cached ----------------------------------------------------------------------------------


def cached_in(conn: sqlite3.Connection, candidate: Candidate) -> tuple[list[int], list[str]]:
    """The index rows already holding ``candidate``'s chat and the accounts they came through
    (:func:`grepogram.db.chat_reach`), both empty when the index does not hold it.

    Asked by peer id and by ``@username``; an invite or a folder link is known by neither until
    a probe learns the chat behind it. A chat held only through a Telegram Desktop import is
    cached with no account.
    """
    rows: dict[int, ChatRow] = {}
    if candidate.peer_id is not None:
        rows.update((chat.id, chat) for chat in db.chats_for_peer(conn, candidate.peer_id))
    if candidate.username:
        rows.update((chat.id, chat) for chat in db.chats_for_username(conn, candidate.username))
    accounts: dict[str, None] = {}
    for chat_id in sorted(rows):
        accounts.update(dict.fromkeys(db.chat_reach(conn, chat_id)))
    return sorted(rows), list(accounts)


def _cached_ids(conn: sqlite3.Connection, chat: LeadTarget) -> set[int]:
    found: set[int] = set()
    if chat.peer_id is not None:
        found.update(row.id for row in db.chats_for_peer(conn, chat.peer_id))
    if chat.username:
        found.update(row.id for row in db.chats_for_username(conn, chat.username))
    return found


# --- ranking ---------------------------------------------------------------------------------


def question_terms(question: str) -> frozenset[str]:
    """The stems of ``question``'s words that can say something about relevance."""
    return frozenset(
        stem.stem_token(token)
        for token in stem.tokenize(question)
        if len(token) >= _MIN_TERM and not token.isdigit()
    )


def overlap(terms: frozenset[str], snippets: Iterable[str | None]) -> int:
    """How many of ``terms`` the snippets share, together."""
    if not terms:
        return 0
    seen = {stem.stem_token(token) for text in snippets if text for token in stem.tokenize(text)}
    return len(terms & seen)


def candidate_views(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    statuses: Iterable[CandidateStatus] | None = None,
) -> list[CandidateView]:
    """A session's candidates with their evidence, corroboration, question overlap and what the
    index already holds of each, best first: most corroboration, then most overlap, then the
    shallowest, then the first found."""
    candidates = research_db.list_candidates(
        rdb, session.id, None if statuses is None else list(statuses)
    )
    counts = research_db.corroboration(rdb, (c.id for c in candidates))
    terms = question_terms(session.question)
    views: list[CandidateView] = []
    for candidate in candidates:
        evidence = research_db.list_evidence(rdb, candidate.id)
        chat_ids, accounts = cached_in(conn, candidate)
        views.append(
            CandidateView(
                candidate=candidate,
                corroboration=counts.get(candidate.id, 0),
                overlap=overlap(terms, (e.snippet for e in evidence)),
                cached_chats=tuple(chat_ids),
                cached_accounts=tuple(accounts),
                evidence=tuple(evidence),
            )
        )
    views.sort(key=lambda v: (-v.corroboration, -v.overlap, v.candidate.depth, v.candidate.id))
    return views


# --- discovery -------------------------------------------------------------------------------


@dataclass(slots=True)
class _Found:
    """Every lead to one identity in this call, and the shallowest depth any of them gives."""

    first: Lead
    depth: int
    leads: list[Lead] = field(default_factory=list)

    def rank(self, terms: frozenset[str]) -> tuple[int, int, int]:
        return (
            -len({lead.origin_key for lead in self.leads}),
            -overlap(terms, (lead.snippet for lead in self.leads)),
            self.depth,
        )


def scan_targets(rdb: sqlite3.Connection, session: ResearchSession) -> dict[int, tuple[int, int]]:
    """``chat_id → (depth, msg_id cursor)`` for every chat the session reads: its seeds at
    depth 0 and every chat a run fetched for it, at the depth that chat was found at."""
    targets = {seed: (0, 0) for seed in session.seeds}
    for cursor in research_db.list_scan_cursors(rdb, session.id):
        depth = min(cursor.depth, targets.get(cursor.chat_id, (cursor.depth, 0))[0])
        targets[cursor.chat_id] = (depth, cursor.msg_id)
    return targets


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
    hop deeper than the chat they were found in — never beyond ``max_depth``, never an excluded
    one, at most ``max_candidates`` per call, best corroborated first — and every lead is kept as
    evidence, on an existing candidate as on a new one. The candidates, their evidence and the
    moved scan cursors are written in one transaction. See the module docstring for what counts
    as a lead and as corroboration.
    """
    require_enabled(cfg)
    session = active_session(rdb, session_id)
    limits = session.limits
    stamp = int(time.time()) if now is None else now
    targets = scan_targets(rdb, session)
    scan = collect_leads(conn, targets, {chat: cursor for chat, (_, cursor) in targets.items()})

    found: dict[str, _Found] = {}
    in_session = 0
    for lead in scan.leads:
        if _cached_ids(conn, lead.chat) & targets.keys():
            in_session += 1
            continue
        depth = targets[lead.chat_id][0] + 1
        entry = found.setdefault(lead.identity, _Found(first=lead, depth=depth))
        entry.depth = min(entry.depth, depth)
        entry.leads.append(lead)

    terms = question_terms(session.question)
    fresh: list[_Found] = []
    new: list[int] = []
    updated: list[int] = []
    beyond_depth = excluded = 0
    with db.transaction(rdb):
        for identity, entry in found.items():
            existing = research_db.candidate_by_identity(rdb, session.id, identity)
            if existing is not None:
                if _record(rdb, existing, entry, stamp):
                    updated.append(existing.id)
            elif entry.depth > limits.max_depth:
                beyond_depth += 1
            elif research_db.is_excluded(rdb, identity):
                excluded += 1
            else:
                fresh.append(entry)
        fresh.sort(key=lambda entry: entry.rank(terms))
        kept, over = fresh[: limits.max_candidates], fresh[limits.max_candidates :]
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
                new.append(candidate.id)
        held_back = {lead.chat_id for entry in over for lead in entry.leads}
        for chat_id, newest in scan.newest.items():
            if chat_id not in held_back:
                research_db.set_scan_cursor(
                    rdb, session.id, chat_id, depth=targets[chat_id][0], msg_id=newest, now=stamp
                )
    report = DiscoverReport(
        session_id=session.id,
        chats_scanned=len(scan.newest),
        messages_scanned=scan.messages,
        text_fallback=scan.text_fallback,
        leads=len(scan.leads) - in_session,
        in_session=in_session,
        people=scan.people,
        new_candidates=new,
        updated_candidates=updated,
        beyond_depth=beyond_depth,
        excluded=excluded,
        over_cap=len(over),
        truncated=bool(over),
    )
    log.info(
        "research session %d: %d chat(s) read, %d lead(s), %d new candidate(s), "
        "%d beyond depth, %d excluded, %d over the cap",
        session.id,
        report.chats_scanned,
        report.leads,
        len(new),
        beyond_depth,
        excluded,
        len(over),
    )
    return report


def _record(rdb: sqlite3.Connection, candidate: Candidate, entry: _Found, now: int) -> bool:
    """Add every lead of ``entry`` as evidence of ``candidate``; whether any path was new."""
    added = False
    for lead in entry.leads:
        added |= research_db.add_evidence(
            rdb,
            candidate.id,
            lead.via,
            lead.origin_key,
            chat_id=lead.chat_id,
            msg_id=lead.msg_id,
            snippet=lead.snippet,
            now=now,
        )
    return added
