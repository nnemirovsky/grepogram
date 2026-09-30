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

**Probing** (:func:`probe`, :func:`probe_candidates`) asks Telegram what a candidate *is* —
title, type, size, whether the acting account is a member, whether joining needs an admission
request — and never reads its history: pinned posts and messages need a grant. At most
``probe_limit`` candidates per call, best ranked first; a flood wait stops the pass with a
warning and leaves the rest for the next call. A shared folder's chats become candidates of
their own (``via = shared_folder``, ``parent_id`` the folder), and approving the folder grants
nothing for them.

**Global search** (:func:`global_search`) runs only while ``[research]`` switches it on *and*
the session holds a live ``global_search`` grant. Its results are candidates and evidence in
``research.db`` — never ``messages`` rows, so no sync cursor moves. A post search pays only
when ``paid_stars_max`` allows the price *and* a separate ``paid_search`` grant exists, which
the paid search then consumes.

What this relies on of Telegram's API (core.telegram.org, reverified 2026-09-30, and the TL
classes of Telethon 1.44 / layer 227 for the exact fields):

- ``messages.checkChatInvite(hash)`` answers ``chatInviteAlready`` (``chat``: the account is a
  member), ``chatInvitePeek`` (``chat`` and ``expires``: previewable without joining), or
  ``chatInvite`` (``title``, ``participants_count``, flags ``channel`` / ``broadcast`` /
  ``megagroup`` / ``public`` / ``request_needed``, and no peer at all); errors
  ``INVITE_HASH_EXPIRED``, ``INVITE_HASH_INVALID``, ``INVITE_HASH_EMPTY``, ``CHANNEL_PRIVATE``.
- ``chatlists.checkChatlistInvite(slug)`` answers ``chatlists.chatlistInvite`` (``title`` as
  ``TextWithEntities``, ``peers``, ``chats``, ``users``) or, once the folder is imported,
  ``chatlists.chatlistInviteAlready`` (``filter_id``, ``missing_peers`` not joined yet,
  ``already_peers``); a dead slug is an RPC error Telethon has no class for (``INVITE_SLUG_*``),
  hence the plain ``RPCError`` catch.
- ``messageFwdHeader``: ``from_id`` + ``channel_post`` address a channel post; ``from_name``
  without ``from_id`` is an account hiding itself, which names no peer; ``saved_from_peer`` /
  ``saved_from_msg_id`` are set only for Saved Messages. :func:`grepogram.sync.forward_origin`
  stores exactly that, and a forward origin a probe cannot address — no access hash for this
  account, the usual case for a private channel — is recorded ``unresolvable``, never guessed.
- ``channels.checkSearchPostsFlood(query)`` → ``searchPostsFlood``: ``total_daily``,
  ``remains``, ``wait_till``, ``query_is_free``, ``stars_amount``; the page on search says to
  ask it before ``channels.searchPosts`` (``query``, ``offset_rate``, ``offset_peer``,
  ``offset_id``, ``limit``, ``allow_paid_stars``), which searches every public channel and
  answers ``messages.Messages`` — a post search is free while ``remains`` or ``query_is_free``
  says so and costs ``stars_amount`` otherwise, paid only through ``allow_paid_stars``.
- ``contacts.search(q, limit)`` → ``contacts.found`` with ``my_results`` and ``results`` as
  peers and the ``chats`` / ``users`` behind them; the account's own contacts are excluded.
- ``contacts.resolveUsername`` (through ``client.get_entity``) answers a ``Channel`` whose
  ``left`` flag says the account is not a member; ``channels.getChannels`` needs the account's
  own access hash, so a peer id alone reaches only what the index stored a hash for.

Message text never reaches the log above DEBUG; counts do.
"""

import dataclasses
import logging
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

from telethon import errors, utils
from telethon.tl import functions, types

from grepogram import db, dialogs, leads, research_db, stem, tg
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
    GlobalSearchReport,
    LinkKind,
    MessageRow,
    ProbeOutcome,
    ProbeReport,
    ProbeResult,
    ResearchLimits,
    ResearchSession,
    SearchKind,
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


# --- probing ---------------------------------------------------------------------------------

_PROBED: tuple[CandidateStatus, ...] = ("proposed", "approved")
"""The statuses probing reads: decisions not acted on yet. A joined or fetched chat is known."""
SEARCH_LIMIT = 100
"""The most results one global search asks for; ``max_candidates`` may lower it."""
UNRESOLVABLE_NOTE = (
    "unresolvable: this account holds no access hash for it — a private chat known only by its "
    "id (a forward origin, a t.me/c link); only a link that names it (an invite, a username) "
    "can open it"
)


def _stamp(now: int | None) -> int:
    return int(time.time()) if now is None else now


def _raise_auth(exc: errors.UnauthorizedError, account: str) -> NoReturn:
    """A dead session becomes :class:`~grepogram.tg.AuthRequired` naming ``account``."""
    if isinstance(exc, tg.AUTH_ERRORS):
        raise tg.AuthRequired(f"Telegram rejected the session: {exc}", account) from exc
    raise exc


def _flood_seconds(exc: errors.FloodError) -> int | None:
    seconds = getattr(exc, "seconds", None)
    return int(seconds) if isinstance(seconds, int) else None


def entity_facts(entity: Any) -> dict[str, Any]:
    """What an entity Telegram answered with says about a candidate, as
    :func:`grepogram.research_db.update_candidate` fields; unknown facts are left out so a
    probe never erases what an earlier one learned.

    ``member`` comes from the ``left`` flag (``deactivated`` too for a legacy group); a
    ``*Forbidden`` entity is a chat the account was banned or kicked from. A ``min`` entity's
    access hash cannot address anything and is not kept.
    """
    facts: dict[str, Any] = {
        "peer_id": dialogs.peer_id(entity),
        "type": dialogs.chat_type(entity),
        "title": utils.get_display_name(entity) or None,
    }
    username = dialogs.entity_username(entity)
    if username:
        facts["username"] = username.lower()
    participants = getattr(entity, "participants_count", None)
    if isinstance(participants, int):
        facts["participants"] = participants
    if isinstance(entity, types.Channel):
        facts["member"] = not entity.left
    elif isinstance(entity, types.Chat):
        facts["member"] = not (entity.left or entity.deactivated)
    elif isinstance(entity, types.ChannelForbidden | types.ChatForbidden):
        facts["member"] = False
    access_hash = getattr(entity, "access_hash", None)
    if isinstance(access_hash, int) and not getattr(entity, "min", False):
        facts["access_hash"] = access_hash
    return {key: value for key, value in facts.items() if value is not None}


def _forbidden(entity: Any) -> bool:
    return isinstance(entity, types.ChannelForbidden | types.ChatForbidden)


def entity_target(entity: Any) -> LeadTarget | None:
    """The identity a chat Telegram answered with is proposed under: its ``@username`` when it
    has one — the form a link would name it by — else its marked id."""
    username = dialogs.entity_username(entity)
    if username:
        found = leads.username(username)
        if found is not None:
            return found
    return leads.peer(dialogs.peer_id(entity))


def _stored_hash(conn: sqlite3.Connection, peer_id: int, account: str) -> int | None:
    """The access hash ``account`` stored in the index for ``peer_id``, if any row holds one."""
    for chat in db.chats_for_peer(conn, peer_id):
        stored = db.access_hash(conn, chat.id, account)
        if stored is not None:
            return stored
    return None


def _session_holds(
    conn: sqlite3.Connection, reads: Iterable[int], peer_id: int | None, username: str | None
) -> bool:
    """Whether the chat named by ``peer_id`` / ``username`` is one the session already reads."""
    rows: set[int] = set()
    if peer_id is not None:
        rows.update(chat.id for chat in db.chats_for_peer(conn, peer_id))
    if username:
        rows.update(chat.id for chat in db.chats_for_username(conn, username))
    return bool(rows & set(reads))


def _settle(
    rdb: sqlite3.Connection,
    candidate: Candidate,
    result: ProbeResult,
    stamp: int,
    facts: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> Candidate:
    """Store what a probe learned. Only a ``proposed`` candidate turns ``unavailable``; one a
    human already decided on keeps its status and carries the refusal in its note."""
    fields: dict[str, Any] = dict(facts or {})
    fields["probed_at"] = stamp
    fields["note"] = note
    if result == "unavailable" and candidate.status == "proposed":
        fields["status"] = "unavailable"
    return research_db.update_candidate(rdb, candidate.id, **fields)


async def probe(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    candidate: Candidate,
    *,
    now: int | None = None,
) -> ProbeOutcome:
    """Ask Telegram what ``candidate`` is, as its session's account, and store the answer.

    Read-only metadata: a username is resolved, an invite checked, a shared folder listed, a
    bare peer id looked up with the access hash the index stored for this account — and with
    none, nothing is sent and the candidate is ``unresolvable``. No history is read. A refusal
    (an expired invite, a banned account, a username nobody holds) is ``unavailable`` with
    Telegram's reason in the note. A flood wait propagates to the caller; a dead session
    becomes :class:`~grepogram.tg.AuthRequired`.
    """
    session = research_db.get_session(rdb, candidate.session_id)
    if session is None:
        raise UnknownSession(candidate.session_id)
    stamp = _stamp(now)
    try:
        if candidate.kind == "username":
            return await _probe_username(client, rdb, candidate, stamp)
        if candidate.kind == "peer":
            return await _probe_peer(client, rdb, conn, session, candidate, stamp)
        if candidate.kind == "invite":
            return await _probe_invite(client, rdb, candidate, stamp)
        return await _probe_addlist(client, rdb, conn, session, candidate, stamp)
    except errors.FloodError:
        raise
    except errors.UnauthorizedError as exc:
        _raise_auth(exc, session.account)
    except errors.RPCError as exc:
        refused = _settle(rdb, candidate, "unavailable", stamp, note=f"Telegram refused: {exc}")
        return ProbeOutcome(candidate=refused, result="unavailable")


def _entity_outcome(
    rdb: sqlite3.Connection,
    candidate: Candidate,
    entity: Any,
    stamp: int,
    *,
    member: bool | None = None,
    note: str | None = None,
) -> ProbeOutcome:
    facts = entity_facts(entity)
    if member is not None:
        facts["member"] = member
    if _forbidden(entity):
        note = "Telegram refuses this chat to the account: banned or removed from it"
        stored = _settle(rdb, candidate, "unavailable", stamp, facts, note)
        return ProbeOutcome(candidate=stored, result="unavailable")
    if isinstance(entity, types.User):
        note = note or "a user account, not a group or channel"
    stored = _settle(rdb, candidate, "probed", stamp, facts, note)
    return ProbeOutcome(candidate=stored, result="probed")


async def _probe_username(
    client: Any, rdb: sqlite3.Connection, candidate: Candidate, stamp: int
) -> ProbeOutcome:
    name = candidate.username or candidate.identity.lstrip("@")
    try:
        entity = await client.get_entity(f"@{name}")
    except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
        note = f"no chat or user holds @{name}"
        stored = _settle(rdb, candidate, "unavailable", stamp, note=note)
        return ProbeOutcome(candidate=stored, result="unavailable")
    return _entity_outcome(rdb, candidate, entity, stamp)


async def _probe_peer(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    candidate: Candidate,
    stamp: int,
) -> ProbeOutcome:
    marked = candidate.peer_id
    if marked is None:
        return ProbeOutcome(
            candidate=_settle(rdb, candidate, "unresolvable", stamp, note=UNRESOLVABLE_NOTE),
            result="unresolvable",
        )
    bare, kind = utils.resolve_id(marked)
    if kind is types.PeerChat:
        answer = await client(functions.messages.GetChatsRequest(id=[bare]))
    else:
        access_hash = (
            _stored_hash(conn, marked, session.account)
            if candidate.access_hash is None
            else candidate.access_hash
        )
        if kind is not types.PeerChannel or access_hash is None:
            stored = _settle(rdb, candidate, "unresolvable", stamp, note=UNRESOLVABLE_NOTE)
            return ProbeOutcome(candidate=stored, result="unresolvable")
        channel = types.InputChannel(bare, access_hash)
        answer = await client(functions.channels.GetChannelsRequest(id=[channel]))
    entity = next((e for e in answer.chats if dialogs.peer_id(e) == marked), None)
    if entity is None:
        stored = _settle(rdb, candidate, "unresolvable", stamp, note=UNRESOLVABLE_NOTE)
        return ProbeOutcome(candidate=stored, result="unresolvable")
    return _entity_outcome(rdb, candidate, entity, stamp)


def _invite_type(invite: types.ChatInvite) -> str:
    if invite.megagroup:
        return "supergroup"
    return "channel" if invite.channel else "group"


async def _probe_invite(
    client: Any, rdb: sqlite3.Connection, candidate: Candidate, stamp: int
) -> ProbeOutcome:
    invite_hash = candidate.invite_hash or candidate.identity.lstrip("+")
    answer = await client(functions.messages.CheckChatInviteRequest(hash=invite_hash))
    if isinstance(answer, types.ChatInviteAlready):
        return _entity_outcome(rdb, candidate, answer.chat, stamp, member=True)
    if isinstance(answer, types.ChatInvitePeek):
        expires = answer.expires.date().isoformat() if answer.expires else "unknown"
        peek = f"previewable without joining until {expires}"
        return _entity_outcome(rdb, candidate, answer.chat, stamp, member=False, note=peek)
    facts: dict[str, Any] = {
        "title": answer.title,
        "type": _invite_type(answer),
        "participants": answer.participants_count,
        "member": False,
        "request_needed": bool(answer.request_needed),
    }
    note = "public" if answer.public else None
    stored = _settle(rdb, candidate, "probed", stamp, facts, note)
    return ProbeOutcome(candidate=stored, result="probed")


async def _probe_addlist(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    candidate: Candidate,
    stamp: int,
) -> ProbeOutcome:
    slug = candidate.addlist_slug or candidate.identity.removeprefix("addlist/")
    answer = await client(functions.chatlists.CheckChatlistInviteRequest(slug=slug))
    entities = {dialogs.peer_id(e): e for e in (*answer.chats, *answer.users)}
    joined: dict[int, bool] = {}
    title: str | None
    if isinstance(answer, types.chatlists.ChatlistInviteAlready):
        title = candidate.title
        joined.update((int(utils.get_peer_id(p)), True) for p in answer.already_peers)
        joined.update((int(utils.get_peer_id(p)), False) for p in answer.missing_peers)
        peers = [*answer.already_peers, *answer.missing_peers]
        member = True
    else:
        title = answer.title.text
        peers = list(answer.peers)
        member = False
    targets = scan_targets(rdb, session)
    children: list[int] = []
    people = excluded = in_session = over_cap = 0
    with db.transaction(rdb):
        for peer in peers:
            marked = int(utils.get_peer_id(peer))
            entity = entities.get(marked)
            if entity is None:
                continue
            if isinstance(entity, types.User):
                people += 1
                continue
            facts = entity_facts(entity)
            if marked in joined:
                facts["member"] = joined[marked]
            if _session_holds(conn, targets, marked, facts.get("username")):
                in_session += 1
                continue
            target = entity_target(entity)
            if target is None:
                continue
            existing = research_db.candidate_by_identity(rdb, session.id, target.target)
            if existing is None and len(children) >= session.limits.max_candidates:
                over_cap += 1
                continue
            child = research_db.add_candidate(
                rdb,
                session.id,
                target.target,
                "username" if target.kind == "username" else "peer",
                candidate.depth,
                peer_id=marked,
                username=facts.get("username"),
                parent_id=candidate.id,
                now=stamp,
            )
            if child is None:
                excluded += 1
                continue
            research_db.update_candidate(rdb, child.id, probed_at=stamp, **facts)
            research_db.add_evidence(
                rdb,
                child.id,
                "shared_folder",
                f"addlist:{slug}",
                chat_id=None,
                msg_id=None,
                snippet=title,
                now=stamp,
            )
            children.append(child.id)
        note = f"shared folder of {len(peers)} chat(s)"
        folder_facts: dict[str, Any] = {"member": member}
        if title:
            folder_facts["title"] = title
        stored = _settle(rdb, candidate, "probed", stamp, folder_facts, note)
    return ProbeOutcome(
        candidate=stored,
        result="probed",
        children=tuple(children),
        people=people,
        excluded=excluded,
        in_session=in_session,
        over_cap=over_cap,
    )


async def probe_candidates(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    *,
    limit: int | None = None,
    now: int | None = None,
) -> ProbeReport:
    """Probe the session's candidates no probe has answered yet, best ranked first, at most
    ``limit`` (the session's ``probe_limit`` by default) of them.

    A flood wait stops the pass: the report carries a warning and ``flood_wait_s``, and the
    candidates not reached wait for the next call.
    """
    require_enabled(cfg)
    session = active_session(rdb, session_id)
    report = ProbeReport(session_id=session.id)
    pending = [
        view.candidate
        for view in candidate_views(rdb, conn, session, _PROBED)
        if view.candidate.probed_at is None
    ]
    allowance = session.limits.probe_limit if limit is None else limit
    for candidate in pending[: max(allowance, 0)]:
        try:
            outcome = await probe(client, rdb, conn, candidate, now=now)
        except errors.FloodError as exc:
            report.flood_wait_s = _flood_seconds(exc)
            wait = f"{report.flood_wait_s}s" if report.flood_wait_s is not None else "a while"
            report.warnings.append(
                f"Telegram asks to wait {wait} before probing again; probing stopped"
            )
            break
        {
            "probed": report.probed,
            "unavailable": report.unavailable,
            "unresolvable": report.unresolvable,
        }[outcome.result].append(candidate.id)
        report.children.extend(outcome.children)
    report.remaining = sum(
        1 for c in research_db.list_candidates(rdb, session.id, _PROBED) if c.probed_at is None
    )
    log.info(
        "research session %d: probed %d, %d unavailable, %d unresolvable, %d from folders, %d left",
        session.id,
        len(report.probed),
        len(report.unavailable),
        len(report.unresolvable),
        len(report.children),
        report.remaining,
    )
    return report


# --- global search ---------------------------------------------------------------------------

SEARCH_OFF_HINT = "set `chat_search = true` or `post_search = true` under [research]"
SEARCH_GRANT_HINT = (
    "global search sends the question to Telegram and needs a human's approval for this "
    "session first: grepogram research approve"
)
_SEARCH_KINDS: tuple[SearchKind, ...] = ("chat_search", "post_search")


def search_kinds(cfg: Config) -> list[SearchKind]:
    """The global searches ``[research]`` switches on."""
    return [kind for kind in _SEARCH_KINDS if getattr(cfg.research, kind)]


def search_granted(rdb: sqlite3.Connection, session_id: int, action: str) -> bool:
    """Whether the session holds a live session-wide grant for ``action``."""
    return any(action in grant.actions for grant in research_db.live_grants(rdb, session_id, None))


async def global_search(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    query: str,
    *,
    kinds: Sequence[SearchKind] | None = None,
    now: int | None = None,
) -> list[GlobalSearchReport]:
    """Search Telegram itself for ``query``: public chats by name (``contacts.search``) and
    public channel posts (``channels.searchPosts``), each only while ``[research]`` switches it
    on, and both only while the session holds a live ``global_search`` grant.

    Every chat found becomes a candidate one hop from the question (depth 1) with its result as
    evidence — a post keeps the origin key ``post:<peer>/<msg>`` discovery gives an indexed
    copy of it. Nothing is written to ``index.db``. A post search asks
    ``channels.checkSearchPostsFlood`` first and sends ``allow_paid_stars`` only when the free
    quota is spent, ``paid_stars_max`` covers the price and a ``paid_search`` grant is live —
    consumed before the request goes out, so one approval never pays twice. Each search is
    recorded in ``research.db``, run or not.
    """
    require_enabled(cfg)
    session = active_session(rdb, session_id)
    enabled = search_kinds(cfg)
    wanted = list(dict.fromkeys(kinds)) if kinds is not None else enabled
    off = [kind for kind in wanted if kind not in enabled]
    if not enabled or off:
        raise ResearchError(
            f"global search is off: {', '.join(off or _SEARCH_KINDS)}", SEARCH_OFF_HINT
        )
    if not search_granted(rdb, session.id, "global_search"):
        raise ResearchError("global search is not approved for this session", SEARCH_GRANT_HINT)
    text = " ".join(query.split())
    if not text:
        raise ResearchError("a global search needs a query")
    stamp = _stamp(now)
    reports: list[GlobalSearchReport] = []
    for kind in wanted:
        report = GlobalSearchReport(session_id=session.id, kind=kind, query=text)
        reports.append(report)
        try:
            if kind == "chat_search":
                await _chat_search(client, rdb, conn, session, report, stamp)
            else:
                await _post_search(client, rdb, conn, cfg, session, report, stamp)
        except errors.FloodError as exc:
            report.flood_wait_s = seconds = _flood_seconds(exc)
            wait = f"{seconds}s" if seconds is not None else "a while"
            report.warnings.append(f"{kind}: Telegram asks to wait {wait}; search stopped")
            _record_search(rdb, report, stamp)
            break
        except errors.UnauthorizedError as exc:
            _raise_auth(exc, session.account)
        except errors.RPCError as exc:
            report.warnings.append(f"{kind}: Telegram refused the search: {exc}")
        _record_search(rdb, report, stamp)
    log.debug("research session %d searched Telegram for %r", session.id, text)
    log.info(
        "research session %d: %d global search(es), %d new candidate(s)",
        session.id,
        sum(report.ran for report in reports),
        sum(len(report.new_candidates) for report in reports),
    )
    return reports


def _record_search(rdb: sqlite3.Connection, report: GlobalSearchReport, stamp: int) -> None:
    research_db.record_search(
        rdb,
        report.session_id,
        report.kind,
        report.query,
        results=report.results,
        note="; ".join(report.warnings) or None,
        now=stamp,
    )


def _search_limit(session: ResearchSession) -> int:
    return max(1, min(SEARCH_LIMIT, session.limits.max_candidates))


def _found_chat(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    reads: Iterable[int],
    report: GlobalSearchReport,
    entity: Any,
    *,
    origin_key: str,
    msg_id: int | None,
    snippet_text: str | None,
    stamp: int,
) -> None:
    """Record one chat a global search answered with as a candidate and its evidence."""
    if isinstance(entity, types.User):
        report.people += 1
        return
    facts = entity_facts(entity)
    marked = facts["peer_id"]
    if _session_holds(conn, reads, marked, facts.get("username")):
        report.in_session += 1
        return
    target = entity_target(entity)
    if target is None:
        return
    existing = research_db.candidate_by_identity(rdb, session.id, target.target)
    if existing is None and len(report.new_candidates) >= session.limits.max_candidates:
        report.over_cap += 1
        return
    candidate = research_db.add_candidate(
        rdb,
        session.id,
        target.target,
        "username" if target.kind == "username" else "peer",
        1,
        peer_id=marked,
        username=facts.get("username"),
        now=stamp,
    )
    if candidate is None:
        report.excluded += 1
        return
    research_db.update_candidate(rdb, candidate.id, probed_at=stamp, **facts)
    added = research_db.add_evidence(
        rdb,
        candidate.id,
        report.kind,
        origin_key,
        chat_id=marked,
        msg_id=msg_id,
        snippet=snippet_text,
        now=stamp,
    )
    if existing is None:
        report.new_candidates.append(candidate.id)
    elif added and candidate.id not in report.updated_candidates:
        report.updated_candidates.append(candidate.id)


async def _chat_search(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    report: GlobalSearchReport,
    stamp: int,
) -> None:
    found = await client(
        functions.contacts.SearchRequest(q=report.query, limit=_search_limit(session))
    )
    report.ran = True
    entities = {dialogs.peer_id(e): e for e in (*found.chats, *found.users)}
    peers = [int(utils.get_peer_id(p)) for p in (*found.my_results, *found.results)]
    report.results = len(peers)
    reads = scan_targets(rdb, session)
    with db.transaction(rdb):
        for marked in dict.fromkeys(peers):
            entity = entities.get(marked)
            if entity is None:
                continue
            _found_chat(
                rdb,
                conn,
                session,
                reads,
                report,
                entity,
                origin_key=f"chat_search:{marked}",
                msg_id=None,
                snippet_text=utils.get_display_name(entity) or None,
                stamp=stamp,
            )


async def _post_search(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session: ResearchSession,
    report: GlobalSearchReport,
    stamp: int,
) -> None:
    flood = await client(functions.channels.CheckSearchPostsFloodRequest(query=report.query))
    report.quota_total = flood.total_daily
    report.quota_remains = flood.remains
    report.wait_till = flood.wait_till
    paid: int | None = None
    if not (flood.query_is_free or flood.remains > 0):
        price = int(flood.stars_amount or 0)
        refusal = _paid_refusal(rdb, cfg, session, price)
        if refusal is not None:
            report.warnings.append(f"post_search: free searches are used up; {refusal}")
            return
        grant = next(
            g for g in research_db.live_grants(rdb, session.id, None) if "paid_search" in g.actions
        )
        research_db.consume_grant(rdb, grant.id, now=stamp)
        paid = price
    answer = await client(
        functions.channels.SearchPostsRequest(
            offset_rate=0,
            offset_peer=types.InputPeerEmpty(),
            offset_id=0,
            limit=_search_limit(session),
            query=report.query,
            allow_paid_stars=paid,
        )
    )
    report.ran = True
    report.paid_stars = paid or 0
    entities = {dialogs.peer_id(e): e for e in (*answer.chats, *answer.users)}
    posts = [m for m in answer.messages if isinstance(m, types.Message)]
    report.results = len(posts)
    reads = scan_targets(rdb, session)
    with db.transaction(rdb):
        for post in posts:
            marked = int(utils.get_peer_id(post.peer_id))
            entity = entities.get(marked)
            if entity is None:
                continue
            _found_chat(
                rdb,
                conn,
                session,
                reads,
                report,
                entity,
                origin_key=f"post:{marked}/{post.id}",
                msg_id=post.id,
                snippet_text=snippet(post.message or "", report.query),
                stamp=stamp,
            )


def _paid_refusal(
    rdb: sqlite3.Connection, cfg: Config, session: ResearchSession, price: int
) -> str | None:
    """Why a post search that costs ``price`` stars may not be paid for, or ``None``."""
    ceiling = cfg.research.paid_stars_max
    if ceiling <= 0:
        return "paid search is off (paid_stars_max = 0)"
    if price <= 0:
        return "Telegram offers no paid search now"
    if price > ceiling:
        return f"the next one costs {price} stars, above paid_stars_max = {ceiling}"
    if not search_granted(rdb, session.id, "paid_search"):
        return f"paying {price} stars needs a separate paid_search approval"
    return None


# --- the whole discover call -----------------------------------------------------------------


async def discover(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    client: Any | None = None,
    *,
    now: int | None = None,
) -> DiscoverReport:
    """One discover call: offline discovery, then — with a client — the global searches the
    session may run and has not run for its question yet, then a bounded probing pass.

    Global search runs only while ``[research]`` switches it on and a ``global_search`` grant is
    live; without them this call simply does not search. A flood wait during the search skips
    probing for this call.
    """
    report = discover_offline(rdb, conn, cfg, session_id, now=now)
    if client is None:
        return report
    session = active_session(rdb, session_id)
    searches: list[GlobalSearchReport] = []
    if search_kinds(cfg) and search_granted(rdb, session.id, "global_search"):
        done = {
            (record.kind, record.query)
            for record in research_db.list_searches(rdb, session.id)
            if record.note is None
        }
        question = " ".join(session.question.split())
        kinds = [kind for kind in search_kinds(cfg) if (kind, question) not in done]
        if kinds:
            searches = await global_search(
                client, rdb, conn, cfg, session.id, question, kinds=kinds, now=now
            )
    flooded = any(search.flood_wait_s is not None for search in searches)
    probed = (
        None if flooded else await probe_candidates(client, rdb, conn, cfg, session.id, now=now)
    )
    return dataclasses.replace(report, probe=probed, searches=tuple(searches))
