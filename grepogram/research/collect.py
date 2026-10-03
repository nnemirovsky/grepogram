"""Leads: the Telegram destinations the messages of the index name, and how the candidates
they lead to are cached and ranked.

:func:`collect_leads` reads the rows a session's chats gained since its cursor and turns each
destination they name into a :class:`Lead`; :func:`origin_key` and its family
(:func:`post_key`, :func:`folder_key`, :func:`chat_search_key`) are the one spelling of the
evidence corroboration counts. :func:`cached_in` asks the index which accounts already hold a
candidate, and :func:`candidate_views` ranks candidates for a report.
"""

import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from grepogram import db, leads, research_db, stem
from grepogram.leads import LeadTarget
from grepogram.models import (
    Candidate,
    CandidateKind,
    CandidateStatus,
    CandidateView,
    ChatKey,
    ChatRow,
    EvidenceVia,
    LinkKind,
    MessageRow,
    ResearchSession,
)

log = logging.getLogger(__name__)


SNIPPET_CHARS = 240
"""The most of a message's text one piece of evidence keeps."""


_MIN_TERM = 3
"""Question tokens shorter than this ("a", "in", "из") say nothing about relevance."""


# --- leads ----------------------------------------------------------------------------------------


def chat_key(chat: ChatRow) -> ChatKey:
    """``chat`` as ``research.db`` names it: ``(scope, peer_id)``, never its row id."""
    return ChatKey(chat.scope, chat.peer_id)


def chat_of(conn: sqlite3.Connection, key: ChatKey) -> ChatRow | None:
    """The index row ``key`` names now, if the index holds that chat."""
    return db.get_chat_by_peer(conn, key.peer_id, key.scope)


@dataclass(frozen=True, slots=True, kw_only=True)
class Lead:
    """One path from a stored message to a chat: the chat it names (``chat``, whose target is
    the candidate :attr:`identity`), how (``via``), where (``found_in`` — the chat as Telegram
    names it — ``row_id``, its index row now, and Telegram ``msg_id``), the ``origin_key``
    corroboration counts, and a snippet of the text."""

    target: LeadTarget
    """What the message named exactly, a post included."""
    chat: LeadTarget
    """The chat ``target`` is in (:func:`chat_level`): ``target`` itself unless it names a
    post."""
    via: EvidenceVia
    found_in: ChatKey
    row_id: int
    msg_id: int
    origin_key: str
    snippet: str | None = None

    @property
    def identity(self) -> str:
        """The candidate identity the lead names: its chat's target."""
        return self.chat.target

    @property
    def kind(self) -> CandidateKind:
        return candidate_kind(self.chat)


@dataclass(slots=True, kw_only=True)
class LeadScan:
    """What :func:`collect_leads` read: the leads, the lead-clock tick each chat was read to
    (what a scan cursor moves to), how many messages carried a lead and how many of those were
    read by the text fallback alone."""

    leads: list[Lead] = field(default_factory=list)
    newest: dict[int, int] = field(default_factory=dict)
    messages: int = 0
    text_fallback: int = 0
    people: int = 0
    """Leads naming a person (a user id) rather than a chat, left out."""


def chat_level(target: LeadTarget) -> LeadTarget | None:
    """The chat a lead names — a username, a peer, an invite or a shared folder, whose target is
    its candidate identity — or ``None`` for a person.

    A post leads to its chat; a peer named by a positive (user) id is a person, not a chat.
    """
    if target.kind in ("username", "invite", "addlist"):
        return target
    if target.kind == "post" and target.username is not None:
        return leads.username(target.username)
    if target.kind in ("peer", "private_post") and target.peer_id is not None:
        return None if target.peer_id > 0 else leads.peer(target.peer_id)
    return None


def candidate_kind(chat: LeadTarget) -> CandidateKind:
    """The candidate kind of a chat :func:`chat_level` answered with: its own lead kind."""
    kind = chat.kind
    assert kind in ("username", "peer", "invite", "addlist"), kind
    return kind


def origin_key(message: MessageRow, chat: ChatRow) -> str:
    """The key every copy of ``message``'s content shares.

    A forward of a post is ``post:<origin peer>/<origin msg>``, as is that post where it is
    itself indexed (a channel or supergroup, whose ``msg_id`` is global); a forward known only by
    its author is ``fwd:<author>@<original date>``; any other message is
    ``msg:<scope>:<peer>/<msg>`` — the chat as Telegram names it, so the key outlives the index
    row it was read from.
    """
    if message.fwd_peer_id is not None and message.fwd_msg_id is not None:
        return post_key(message.fwd_peer_id, message.fwd_msg_id)
    if message.fwd_peer_id is not None:
        return f"fwd:{message.fwd_peer_id}@{message.fwd_date or 0}"
    if chat.is_shared:
        return post_key(chat.peer_id, message.msg_id)
    return f"msg:{chat.scope}:{chat.peer_id}/{message.msg_id}"


def post_key(peer_id: int, msg_id: int) -> str:
    """The origin key of post ``msg_id`` of the channel or supergroup ``peer_id``: the same for
    a forward of it, for the post where it is itself indexed (:func:`origin_key`) and for a
    global post search's result — which is what lets them corroborate each other."""
    return f"post:{peer_id}/{msg_id}"


def folder_key(slug: str) -> str:
    """The origin key of what a shared folder (``addlist/<slug>``) listed."""
    return f"addlist:{slug}"


def chat_search_key(peer_id: int) -> str:
    """The origin key of a chat Telegram's own chat search answered with."""
    return f"chat_search:{peer_id}"


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


def _own(chat: ChatRow) -> set[str]:
    """The identities that name ``chat`` itself: a lead to the chat it was found in is none."""
    own = {leads.peer_identity(chat.peer_id)}
    return own | ({leads.username_identity(chat.username)} if chat.username else set())


def collect_leads(conn: sqlite3.Connection, after: Mapping[int, int]) -> LeadScan:
    """Every lead the stored messages of the chats ``after`` names (index row → lead-clock tick
    read so far) name, among the rows whose leads changed since that tick.

    Links come from ``message_links`` and forward origins from ``messages.fwd_peer_id``; a row
    whose links were never read (``messages.links_read``) and that has neither is read by
    :func:`grepogram.leads.text_leads`. A lead to the very chat it was found in is not one. Only
    rows up to the chat's newest tick when it was first asked are read, so a sync storing rows
    meanwhile cannot slip a message past the cursor this returns.
    """
    scan = LeadScan()
    for chat_id, since in after.items():
        chat = db.get_chat(conn, chat_id)
        if chat is None:
            continue
        upto = db.newest_lead_seq(conn, chat_id)
        scan.newest[chat_id] = max(since, upto)
        rows = db.lead_messages(conn, chat_id, since, upto)
        stored = db.message_links(conn, (m.id for m, _ in rows if m.id is not None))
        own = _own(chat)
        for message, read in rows:
            links = stored.get(message.id or 0)
            if links is None and not read:
                links = leads.text_leads(message.text)
                if links:
                    scan.text_fallback += 1
            found = message_leads(message, chat, links, scan)
            if found:
                scan.messages += 1
            scan.leads.extend(lead for lead in found if lead.identity not in own)
    return scan


def message_leads(
    message: MessageRow,
    chat: ChatRow,
    links: Iterable[tuple[LinkKind, str]] | None,
    scan: LeadScan,
    *,
    via: EvidenceVia | None = None,
) -> list[Lead]:
    """The leads one message of ``chat`` carries — its ``links`` and its forward origin — each
    found ``via`` its own kind of link, or ``via`` for all of them when given (a pinned post)."""
    key = origin_key(message, chat)
    named: list[tuple[EvidenceVia, LeadTarget]] = []
    for link_kind, value in links or ():
        try:
            target = leads.normalize(value)
        except (ValueError, OverflowError):
            # one link nothing reads must not keep every other lead of the chat from being read
            log.debug("research: a stored link of chat %d could not be read: %r", chat.id, value)
            continue
        if target is not None:
            named.append((via or link_kind, target))
    if message.fwd_peer_id is not None:
        origin = leads.peer(message.fwd_peer_id)
        if origin is not None:
            named.append((via or "forward", origin))
    found: list[Lead] = []
    for path, target in named:
        chat_target = chat_level(target)
        if chat_target is None:
            scan.people += 1
            continue
        found.append(
            Lead(
                target=target,
                chat=chat_target,
                via=path,
                found_in=chat_key(chat),
                row_id=chat.id,
                msg_id=message.msg_id,
                origin_key=key,
                snippet=snippet(message.text, _needle(target)),
            )
        )
    return found


# --- cached ---------------------------------------------------------------------------------------


def cached_in(conn: sqlite3.Connection, candidate: Candidate) -> tuple[list[int], list[str]]:
    """The index rows already holding ``candidate``'s chat and the accounts they came through
    (:func:`grepogram.db.chat_reach`), both empty when the index does not hold it.

    Asked by peer id and by ``@username``; an invite or a folder link is known by neither until
    a probe learns the chat behind it. A chat held only through a Telegram Desktop import is
    cached with no account.
    """
    rows = held_rows(conn, candidate.peer_id, candidate.username)
    accounts: dict[str, None] = {}
    for chat_id in rows:
        accounts.update(dict.fromkeys(db.chat_reach(conn, chat_id)))
    return rows, list(accounts)


def held_rows(conn: sqlite3.Connection, peer_id: int | None, username: str | None) -> list[int]:
    """The index rows holding the chat ``peer_id`` / ``username`` name, in id order — every
    account's row of a private peer, and the row of whatever chat goes by that username."""
    found: set[int] = set()
    if peer_id is not None:
        found.update(row.id for row in db.chats_for_peer(conn, peer_id))
    if username:
        found.update(row.id for row in db.chats_for_username(conn, username))
    return sorted(found)


# --- ranking --------------------------------------------------------------------------------------


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
