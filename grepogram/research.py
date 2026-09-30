"""Research: find chats the index does not hold yet, starting from a question and seed chats.

This module is the one both the CLI and the MCP server drive; what it decides is kept in
``research.db`` (:mod:`grepogram.research_db`), what it reads is ``index.db``. Every entry point
refuses while ``[research] enabled`` is false (:func:`require_enabled`).

**Offline discovery** (:func:`discover_offline`) talks to no one. It reads the messages the
index already holds of a session's seed chats — and of any chat a later run fetched for it, at
the depth that chat was found at, a channel's discussion group always beside its channel — and
turns every Telegram destination they name into a *candidate* one hop further out:

- the links a message was stored with (``message_links``: visible URLs, hidden ``text_url``
  hyperlinks, ``@mentions``, URL buttons, link previews);
- a forward's structured origin (``messages.fwd_peer_id``);
- for a row whose links were never read (``messages.links_read``: stored before link capture,
  or by an import whose export spelled no entities), whatever
  :func:`grepogram.leads.text_leads` finds in its text — visible URLs and mentions only, which
  the report counts as ``text_fallback`` so a caller can say that hidden links were not seen
  (``grepogram recapture-links`` reads such rows again, :func:`grepogram.sync.recapture_links`).

Where it has read to is a cursor on the index's lead clock (:func:`grepogram.db.lead_clock`),
not a message id: a discussion group stores comments out of ``msg_id`` order, and an edit or a
recapture gives an old row links it did not have — both are rows whose leads changed since the
cursor. Every chat is named in ``research.db`` as Telegram names it, ``(scope, peer_id)``
(:class:`~grepogram.models.ChatKey`), and found in the index as it is now, so a rebuilt index
or a private chat stored again under another row id is still the same conversation; a cursor
taken on another index (:func:`grepogram.db.index_id`) reads the chat from the start.

A chat whose messages name at least :data:`DIRECTORY_MIN_CHATS` distinct chats is a
**directory**: every lead found in it also carries a ``directory`` path, which shares the
lead's origin key and so adds no corroboration. Approving a directory approves nothing it lists.

**Pinned posts** (:func:`read_pins`) of the chats a session reads — its seeds, the user's own
indexed sources, and the chats runs fetched under a grant — are read once each, whatever their
age: a directory often keeps its index in a post pinned long before any ``since``. Their leads
are evidence (``via = pinned``) in ``research.db`` only; the posts are never stored as messages
and no sync cursor moves.

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
a chat whose leads that cap cut keeps its scan cursor, so the next call reads them again.
``max_session_candidates`` caps the whole session; what it cuts no later call proposes either.

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

**A run** (:func:`run`) carries out what a human approved and nothing else: each join,
admission request, shared-folder join, source added and fetch is preceded by :func:`authorized`
for that very candidate and action, the chats it fetches are synced through the ordinary
:func:`grepogram.sync.sync_all` narrowed with ``only``, and the chats it stored into are read by
discovery one hop deeper, which only ever proposes. A shared folder the account already imported
takes its missing chats through ``chatlists.joinChatlistUpdates`` (core.telegram.org, "Shared
folders": ``missing_peers`` of ``chatlistInviteAlready`` are passed to that method), one not
imported yet through ``chatlists.joinChatlistInvite``, each naming exactly the approved peers.

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
- ``messages.search`` with ``inputMessagesFilterPinned`` (what ``iter_messages(filter=…)``
  sends) answers a chat's pinned messages, newest first, whatever their date.
- ``messageFwdHeader``: ``from_id`` + ``channel_post`` address a channel post; ``from_name``
  without ``from_id`` is an account hiding itself, which names no peer; ``saved_from_peer`` /
  ``saved_from_msg_id`` are set only for Saved Messages. :func:`grepogram.sync.forward_origin`
  stores exactly that, and the origin chat Telegram hands along with the message (in the
  answer's ``chats``; ``min`` when the account may only see it, whose access hash addresses
  nothing) leaves its username and this account's access hash in ``peer_cache``
  (:func:`grepogram.sync.forward_peers`). A forward origin a probe cannot address even so — no
  access hash for this account and no username, the usual case for a private channel — is
  recorded ``unresolvable``, never guessed.
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
import functools
import json
import logging
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, NoReturn, get_args

from telethon import errors, utils
from telethon.tl import functions, types

from grepogram import config, db, dialogs, leads, research_db, sources, stem, sync, tg
from grepogram.embed import Embedder
from grepogram.filters import resolve_chats
from grepogram.leads import LeadTarget
from grepogram.models import (
    PEOPLE_CHAT_TYPES,
    RESEARCH_LIMIT_MAX,
    SHARED_CHAT_TYPES,
    ApprovalItem,
    Candidate,
    CandidateAction,
    CandidateKind,
    CandidateStatus,
    CandidateView,
    ChatKey,
    ChatRow,
    ChatType,
    Config,
    DiscoverReport,
    Evidence,
    EvidenceVia,
    GlobalSearchReport,
    Grant,
    GrantAction,
    GrantChannel,
    LinkKind,
    MessageRow,
    PinReport,
    ProbeOutcome,
    ProbeReport,
    ProbeResult,
    ResearchLimits,
    ResearchSession,
    RunReport,
    ScanCursor,
    SearchKind,
    SessionAction,
    Source,
    SyncReport,
    chat_scope,
)
from grepogram.paths import Paths

log = logging.getLogger(__name__)

ENABLE_HINT = "set `enabled = true` under [research] in config.toml to allow research"
QUESTION_MAX_CHARS = 500
"""The longest question a session takes: it is shown whole in every approval summary."""
_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
"""Unicode categories an approval summary never prints as they are: control characters (the
escape that starts a terminal sequence, line breaks), format characters (bidi overrides,
zero-width marks), surrogates and line/paragraph separators."""
SNIPPET_CHARS = 240
"""The most of a message's text one piece of evidence keeps."""
_MIN_TERM = 3
"""Question tokens shorter than this ("a", "in", "из") say nothing about relevance."""


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
    overrides: Mapping[str, int | None] | None = None,
    *,
    now: int | None = None,
) -> ResearchSession:
    """Start exploring ``question`` from the indexed chats ``seeds`` select, as ``account``.

    ``seeds`` are chat specs as ``search --chat`` takes them (:func:`grepogram.filters.
    resolve_chats`: an id, ``@name``, a link, a folder, free text, ``account:<name>``); one that
    selects nothing raises :class:`~grepogram.filters.UnknownChat`. ``account`` is the account a
    later run joins and fetches as, and must be one the config knows. The limits are the
    ``[research]`` section's with ``overrides`` (:class:`ResearchLimits` field → value, ``None``
    for "keep the config's") applied — the one place a front end's limits are checked
    (:func:`session_limits`). The question is shown in every approval summary, so it is one
    line of plain text of at most :data:`QUESTION_MAX_CHARS` characters. Nothing touches
    Telegram.
    """
    require_enabled(cfg)
    if not question.strip():
        raise ResearchError("a research session needs a question")
    if len(question) > QUESTION_MAX_CHARS:
        raise ResearchError(
            f"a research question is at most {QUESTION_MAX_CHARS} characters; this one has "
            f"{len(question)}",
            "ask it in fewer words",
        )
    if any(unicodedata.category(ch) in _HIDDEN for ch in question):
        raise ResearchError(
            "the question holds control or invisible formatting characters (a line break, a "
            "terminal escape, a direction override)",
            "write it as one line of plain text",
        )
    known = cfg.account_names()
    if account not in known:
        raise ResearchError(
            f"unknown account {account!r}; known: {', '.join(known)}",
            f"sign it in with `{tg.auth_command(account)}` first",
        )
    limits = session_limits(cfg, overrides)
    if not seeds:
        raise ResearchError(
            "a research session needs at least one seed chat",
            "name indexed chats to start from, as `search --chat` takes them",
        )
    chosen = [db.get_chat(conn, chat_id) for chat_id in sorted(resolve_chats(conn, cfg, seeds))]
    session = research_db.create_session(
        rdb,
        question=question,
        account=account,
        seeds=[chat_key(chat) for chat in chosen if chat is not None],
        limits=limits,
        now=now,
    )
    log.info(
        "research session %d started as %s from %d seed chat(s)", session.id, account, len(chosen)
    )
    return session


def session_limits(
    cfg: Config, overrides: Mapping[str, int | None] | None = None
) -> ResearchLimits:
    """The ``[research]`` limits with ``overrides`` applied; each given value must be a whole
    number from 1 to its :data:`~grepogram.models.RESEARCH_LIMIT_MAX`, or
    :class:`ResearchError` says which one is not."""
    given = {name: value for name, value in (overrides or {}).items() if value is not None}
    for name, value in given.items():
        high = RESEARCH_LIMIT_MAX.get(name)
        if high is None:
            raise ResearchError(
                f"unknown research limit {name!r}", f"limits: {', '.join(RESEARCH_LIMIT_MAX)}"
            )
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
            raise ResearchError(f"{name} must be a whole number from 1 to {high}, not {value!r}")
    return dataclasses.replace(cfg.research.limits(), **given)


def active_session(rdb: sqlite3.Connection, session_id: int) -> ResearchSession:
    """The session ``session_id``, which must exist and still be active."""
    session = research_db.get_session(rdb, session_id)
    if session is None:
        raise UnknownSession(session_id)
    if session.state != "active":
        raise SessionStopped(session_id)
    return session


def shown(text: str) -> str:
    """``text`` as an approval summary prints it: one line, with every control or invisible
    formatting character (:data:`_HIDDEN`) made visible as U+FFFD and whitespace collapsed.

    A title, a username or a question comes from someone else — a chat's owner, an agent — and
    reaches a terminal or a consent dialog; a terminal escape or a line break could otherwise
    hide the real action lines or forge new ones.
    """
    cleaned = "".join(
        (" " if ch.isspace() else "\ufffd") if unicodedata.category(ch) in _HIDDEN else ch
        for ch in text
    )
    return " ".join(cleaned.split())


def _quoted(text: str) -> str:
    """``text`` :func:`shown` and in double quotes, a quote inside it escaped, so it cannot
    close the quote early and pass what follows off as grepogram's own words."""
    return json.dumps(shown(text), ensure_ascii=False)


# --- leads -----------------------------------------------------------------------------------


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
        return f"post:{message.fwd_peer_id}/{message.fwd_msg_id}"
    if message.fwd_peer_id is not None:
        return f"fwd:{message.fwd_peer_id}@{message.fwd_date or 0}"
    if chat.is_shared:
        return f"post:{chat.peer_id}/{message.msg_id}"
    return f"msg:{chat.scope}:{chat.peer_id}/{message.msg_id}"


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
    return {f"peer:{chat.peer_id}", *([f"@{chat.username.lower()}"] if chat.username else [])}


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


# --- cached ----------------------------------------------------------------------------------


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
    and every lead is kept as evidence, on an existing candidate as on a new one. A forward's
    origin is proposed by its peer id alone even when a sync saw it under a username: that name
    is a hint the probe checks (:func:`_probe_named_peer`), never an identity — a stale one
    would fold two chats into one candidate.
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
    ``directory`` path shares the lead's origin key, so it shows where a candidate came from
    without counting as another piece of corroboration. Nothing is approved by it: a directory
    grants nothing for what it lists, like every chat (:func:`authorized`).
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


# --- pinned posts ----------------------------------------------------------------------------

PIN_CHATS_PER_CALL = 20
"""Chats whose pinned posts one discover call or run reads — one request each."""
PINNED_PER_CHAT = 50
"""The most pinned posts read of one chat."""


def _pin_reader(chat: ChatRow, session: ResearchSession) -> bool:
    """Whether the session's account may ask about ``chat``'s pinned posts: a shared chat it
    reaches or may try, or a private chat of its own — never another account's private chat."""
    return chat.is_shared or chat.scope == session.account


def _pins_unread(
    targets: Mapping[int, ScanTarget], only: Collection[ChatKey] | None
) -> list[ScanTarget]:
    """The targets whose pinned posts the session has not read yet, narrowed to ``only``."""
    return [
        target
        for target in targets.values()
        if (target.cursor is None or target.cursor.pins_read_at is None)
        and (only is None or chat_key(target.chat) in only)
    ]


async def read_pins(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    *,
    only: Collection[ChatKey] | None = None,
    limit: int = PIN_CHATS_PER_CALL,
    now: int | None = None,
) -> PinReport:
    """Read the pinned posts of the chats the session reads whose pins it has not read yet, and
    propose what they lead to (``via = pinned``); ``only`` narrows it to those chats.

    A chat the session reads is a seed — an indexed source of the user's own — or a chat a run
    fetched under a human's grant, so reading its pinned posts needs no approval of its own, and
    nothing else is ever asked about. A pin is read whatever its age: a directory often keeps its
    index in a post pinned years before any ``since``. The posts are read
    (``messages.search`` with ``inputMessagesFilterPinned``, through ``iter_messages``) and
    their leads kept as evidence in ``research.db`` only: **they are not stored as messages** and
    no sync cursor moves — a sparse read must never pass for the history before it. At most
    ``limit`` chats per call, :data:`PINNED_PER_CHAT` posts each; a flood wait stops the pass
    with a warning and the rest wait for the next call. A chat that refuses the account (or that
    it cannot address) is marked read with a warning, and so is another account's private chat,
    which the session's account is never asked about.
    """
    require_enabled(cfg)
    session = active_session(rdb, session_id)
    stamp = research_db.clock(now)
    report = PinReport(session_id=session.id)
    targets = scan_targets(rdb, conn, session)
    pending = sorted(_pins_unread(targets, only), key=lambda target: (target.depth, target.chat.id))
    foreign: list[ScanTarget] = []
    asked: list[ScanTarget] = []
    for target in pending:
        (asked if _pin_reader(target.chat, session) else foreign).append(target)
    del asked[max(limit, 0) :]
    if asked:
        await sync.warm_peer_cache(client, [target.chat for target in asked], conn, session.account)
    scan = LeadScan()
    done: list[ScanTarget] = list(foreign)
    extra: dict[int, set[str]] = {}
    for target in asked:
        chat = target.chat
        try:
            posts = [
                message
                async for message in client.iter_messages(
                    chat.peer_id, limit=PINNED_PER_CHAT, filter=types.InputMessagesFilterPinned
                )
            ]
        except errors.FloodError as exc:
            report.flood_wait_s = sync.flood_seconds(exc)
            report.warnings.append(
                sync.flood_warning(report.flood_wait_s, "reading more pinned posts", "stopped")
            )
            break
        except errors.UnauthorizedError as exc:
            tg.reraise_unauthorized(exc, session.account)
        except (errors.RPCError, ValueError) as exc:
            report.warnings.append(f"the pinned posts of chat {chat.id} could not be read: {exc}")
            done.append(target)
            continue
        sync.remember_forward_peers(conn, session.account, posts, stamp)
        own = _own(chat)
        found: list[Lead] = []
        for post in posts:
            row = sync.map_message(post, chat, {})
            if row is None:
                continue
            report.messages += 1
            found += [
                lead
                for lead in message_leads(row, chat, row.links, scan, via="pinned")
                if lead.identity not in own
            ]
        scan.leads += found
        extra[chat.id] = {lead.identity for lead in found}
        report.chats.append(chat.id)
        done.append(target)
    for target in foreign:
        report.warnings.append(
            f"chat {target.chat.id} is account {target.chat.scope}'s own; its pinned posts are "
            f"not read as {session.account}"
        )
    with db.transaction(rdb):
        proposal = _propose(rdb, conn, session, targets, scan.leads, stamp)
        _directories(rdb, conn, session, targets, extra, extra, stamp)
        for target in done:
            if target.chat.id not in proposal.held_back:
                research_db.mark_pins_read(
                    rdb, session.id, chat_key(target.chat), depth=target.depth, now=stamp
                )
    report.leads = len(scan.leads) - proposal.in_session
    report.new_candidates = proposal.new
    report.updated_candidates = proposal.updated
    report.over_cap = proposal.over_cap
    report.remaining = len(_pins_unread(scan_targets(rdb, conn, session), only))
    log.info(
        "research session %d: pinned posts of %d chat(s) read, %d new candidate(s), %d left",
        session.id,
        len(report.chats),
        len(proposal.new),
        report.remaining,
    )
    return report


# --- probing ---------------------------------------------------------------------------------

_PROBED: tuple[CandidateStatus, ...] = ("proposed", "approved")
"""The statuses probing reads: decisions not acted on yet. A joined or fetched chat is known."""
SEARCH_LIMIT = 100
"""The most results one global search asks for; ``max_candidates`` may lower it."""
UNRESOLVABLE_NOTE = (
    "unresolvable: this account holds no access hash for it and knows no username it goes by — "
    "a private chat known only by its id (a forward origin, a t.me/c link); only a link that "
    "names it (an invite, a username) can open it"
)


def entity_facts(entity: Any) -> dict[str, Any]:
    """What an entity Telegram answered with says about a candidate, as
    :func:`grepogram.research_db.update_candidate` fields; unknown facts are left out so a
    probe never erases what an earlier one learned.

    ``member`` comes from the ``left`` flag (``deactivated`` too for a legacy group), and
    ``request_needed`` from a channel's ``join_request`` flag — its admins approve who joins, so
    ``channels.joinChannel`` sends an admission request rather than joining; a ``*Forbidden``
    entity is a chat the account was banned or kicked from. A ``min`` entity's
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
        facts["request_needed"] = bool(entity.join_request)
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
    with db.transaction(rdb):
        stored = research_db.update_candidate(rdb, candidate.id, **fields)
        return _reconcile(rdb, stored)


def _reconcile(rdb: sqlite3.Connection, candidate: Candidate) -> Candidate:
    """What a probe that tied ``candidate`` to a peer id or username means for the session.

    Another candidate of the session already tied to the same chat is the same chat under
    another spelling (``@name``, ``peer:<id>``, an invite): the two become one, so
    corroboration is not split between them — the undecided one of them (``proposed``, never
    granted anything, the newer when both are) is folded into the other
    (:func:`grepogram.research_db.merge_candidate`), and two that both carry a decision stay
    apart. An exclusion naming the chat under any of its spellings then covers the result: an
    undecided one turns ``excluded`` and nothing stays authorized for it. Returns the candidate
    that stands for the chat afterwards.
    """
    current = candidate
    for other in research_db.same_chat_candidates(rdb, current):
        droppable = [
            c
            for c in (current, other)
            if c.status == "proposed" and not research_db.has_grants(rdb, c.id)
        ]
        if not droppable:
            continue
        drop = max(droppable, key=lambda c: c.id)
        keep = other if drop.id == current.id else current
        current = research_db.merge_candidate(rdb, keep.id, drop.id)
        log.info("research candidate %d is the same chat as %d; merged", drop.id, keep.id)
    if research_db.candidate_excluded(rdb, current):
        if current.status in ("proposed", "approved", "skipped"):
            current = research_db.update_candidate(rdb, current.id, status="excluded")
        research_db.void_grants(rdb, current.session_id, candidate_ids=[current.id])
    return current


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
    bare peer id looked up with the access hash the index stored for this account — for a chat
    it holds, or for a peer a sync was handed alongside a forward (``peer_cache``) — or else by
    the username such a peer was seen under; with neither, nothing is sent and the candidate is
    ``unresolvable``. No history is read. A refusal
    (an expired invite, a banned account, a username nobody holds) is ``unavailable`` with
    Telegram's reason in the note. A flood wait propagates to the caller; a dead session
    becomes :class:`~grepogram.tg.AuthRequired`.
    """
    session = research_db.get_session(rdb, candidate.session_id)
    if session is None:
        raise UnknownSession(candidate.session_id)
    stamp = research_db.clock(now)
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
        tg.reraise_unauthorized(exc, session.account)
    except errors.RPCError as exc:
        refused = _settle(rdb, candidate, "unavailable", stamp, note=f"Telegram refused: {exc}")
        return ProbeOutcome(candidate=refused, result="unavailable")


_NAME_MOVED_TO: dict[CandidateStatus, CandidateStatus] = {
    "proposed": "unavailable",
    "approved": "failed",
    "joined": "failed",
    "pending_admission": "failed",
}
"""What a candidate becomes once the name it was found by names another chat: an undecided one
is ``unavailable`` under that name, one a run was about to act on ``failed``; the others keep
their status and take the note."""


def _name_moved(
    rdb: sqlite3.Connection, candidate: Candidate, entity: Any, stamp: int
) -> Candidate:
    """Record that the name ``candidate`` was found by now leads to ``entity``, a chat other
    than the one it was probed as, inside the caller's transaction or its own.

    Its peer id is never rewritten — the chat a human saw and approved is the probed one — and
    nothing is done for it any more: its grants are voided and a run it was waiting on skips it.
    A new approval, once someone has looked again, is what brings a ``failed`` one back."""
    facts = entity_facts(entity)
    where = f" {_quoted(facts['title'])}" if facts.get("title") else ""
    name = f"@{candidate.username}" if candidate.username else candidate.identity
    note = (
        f"{shown(name)} now leads to a different chat{where} (id {facts.get('peer_id')}) than "
        f"the one probed (id {candidate.peer_id}); nothing is done for it — look again and "
        "approve anew if it is still wanted"
    )
    fields: dict[str, Any] = {"note": note}
    if candidate.status in _NAME_MOVED_TO:
        fields["status"] = _NAME_MOVED_TO[candidate.status]
    with db.transaction(rdb):
        moved = research_db.update_candidate(rdb, candidate.id, **fields)
        research_db.void_grants(rdb, candidate.session_id, candidate_ids=[candidate.id], now=stamp)
    log.info("research candidate %d: its name now leads to another chat", candidate.id)
    return moved


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
    if candidate.peer_id is not None and facts["peer_id"] != candidate.peer_id:
        # the username or invite leads elsewhere now: that chat is not this candidate's
        moved = _name_moved(rdb, candidate, entity, stamp)
        return ProbeOutcome(candidate=moved, result="unavailable")
    if member is not None:
        facts["member"] = member
    if candidate.kind == "invite":
        # an invite link says itself whether it needs the admins' approval (_probe_invite);
        # the chat's join_request flag is about joining by its username, not through this link
        facts.pop("request_needed", None)
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
        access_hash = candidate.access_hash
        if access_hash is None:
            access_hash = _stored_hash(conn, marked, session.account)
        if access_hash is None:
            access_hash = db.cached_peer_hash(conn, marked, session.account)
        username = candidate.username or db.cached_peer_username(conn, marked)
        if kind is types.PeerChannel and access_hash is None and username:
            return await _probe_named_peer(client, rdb, candidate, username, stamp)
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


async def _probe_named_peer(
    client: Any, rdb: sqlite3.Connection, candidate: Candidate, username: str, stamp: int
) -> ProbeOutcome:
    """Probe a peer known by id through the username the index saw it under — a forward's
    origin channel, whose username a sync recorded (:func:`grepogram.db.cached_peer_username`).
    The answer counts only when it is that very peer: a username that moved to another chat
    since leaves the candidate unresolvable."""
    try:
        entity = await client.get_entity(f"@{username}")
    except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
        entity = None
    if entity is None or dialogs.peer_id(entity) != candidate.peer_id:
        note = f"{UNRESOLVABLE_NOTE}; @{username}, the name it was seen under, no longer names it"
        stored = _settle(rdb, candidate, "unresolvable", stamp, note=note)
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
    targets = scan_targets(rdb, conn, session)
    allowance = room(rdb, session)
    children: list[int] = []
    left_out: Counter[str] = Counter()
    with db.transaction(rdb):
        for peer in peers:
            marked = int(utils.get_peer_id(peer))
            entity = entities.get(marked)
            if entity is None:
                continue
            recorded = _record_entity(
                rdb,
                conn,
                session,
                targets.keys(),
                entity,
                depth=candidate.depth,
                parent_id=candidate.id,
                room_left=allowance - len(children),
                facts={"member": joined[marked]} if marked in joined else {},
                via="shared_folder",
                origin_key=f"addlist:{slug}",
                in_itself=False,
                msg_id=None,
                snippet=title,
                stamp=stamp,
            )
            if recorded.candidate is None:
                left_out[recorded.outcome] += 1
            else:
                children.append(recorded.candidate.id)
        note = f"shared folder of {len(peers)} chat(s)"
        folder_facts: dict[str, Any] = {"member": member}
        if title:
            folder_facts["title"] = title
        stored = _settle(rdb, candidate, "probed", stamp, folder_facts, note)
    return ProbeOutcome(
        candidate=stored,
        result="probed",
        children=tuple(children),
        people=left_out["person"],
        excluded=left_out["excluded"],
        in_session=left_out["in_session"],
        over_cap=left_out["over_cap"],
    )


async def probe_candidates(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    *,
    now: int | None = None,
) -> ProbeReport:
    """Probe the session's candidates no probe has answered yet, best ranked first, at most the
    session's ``probe_limit`` of them.

    A flood wait stops the pass: the report carries a warning and ``flood_wait_s``, and the
    candidates not reached wait for the next call.
    """
    require_enabled(cfg)
    return await _probe_pass(client, rdb, conn, active_session(rdb, session_id), now)


async def _probe_pass(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    now: int | None,
) -> ProbeReport:
    report = ProbeReport(session_id=session.id)
    pending = [
        view.candidate
        for view in candidate_views(rdb, conn, session, _PROBED)
        if view.candidate.probed_at is None
    ]
    for candidate in pending[: session.limits.probe_limit]:
        try:
            outcome = await probe(client, rdb, conn, candidate, now=now)
        except errors.FloodError as exc:
            report.flood_wait_s = sync.flood_seconds(exc)
            report.warnings.append(
                sync.flood_warning(report.flood_wait_s, "probing again", "probing stopped")
            )
            break
        {
            "probed": report.probed,
            "unavailable": report.unavailable,
            "unresolvable": report.unresolvable,
        }[outcome.result].append(outcome.candidate.id)
        report.children.extend(outcome.children)
        report.people += outcome.people
        report.excluded += outcome.excluded
        report.in_session += outcome.in_session
        report.over_cap += outcome.over_cap
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
    on, and both only while the session holds a live ``global_search`` grant. ``query`` must be
    the session's question — the one query that grant's summary names — or nothing is sent.

    Every chat found becomes a candidate one hop from the question (depth 1) with its result as
    evidence — a post keeps the origin key ``post:<peer>/<msg>`` discovery gives an indexed
    copy of it. Nothing is written to ``index.db``. A post search asks
    ``channels.checkSearchPostsFlood`` first and sends ``allow_paid_stars`` only when the free
    quota is spent, ``paid_stars_max`` covers the price and a ``paid_search`` grant is live —
    consumed atomically before the request goes out, so one approval never pays twice, not even
    for two discover calls running at once; a request Telegram refuses after that leaves the
    approval spent and says so. Each search is recorded in ``research.db``, run or not.
    """
    require_enabled(cfg)
    session = active_session(rdb, session_id)
    enabled = search_kinds(cfg)
    granted = granted_kinds(rdb, session.id)
    wanted: list[SearchKind] = (
        list(dict.fromkeys(kinds))
        if kinds is not None
        else [kind for kind in enabled if kind in granted]
    )
    off = [kind for kind in wanted if kind not in enabled]
    if not enabled or off:
        raise ResearchError(
            f"global search is off: {', '.join(off or _SEARCH_KINDS)}", SEARCH_OFF_HINT
        )
    if not granted:
        raise ResearchError("global search is not approved for this session", SEARCH_GRANT_HINT)
    uncovered = [kind for kind in wanted if kind not in granted]
    if uncovered or not wanted:
        raise ResearchError(
            f"this session's global_search approval does not cover "
            f"{', '.join(uncovered or enabled)}: it names {', '.join(granted)}",
            SEARCH_GRANT_HINT,
        )
    text = " ".join(query.split())
    if not text:
        raise ResearchError("a global search needs a query")
    if text != " ".join(session.question.split()):
        raise ResearchError(
            "a global search sends only the session's question: that is the query its approval "
            "named",
            "start a session with this question to search for it",
        )
    await sync.check_account(conn, session.account, client)
    return await _search_telegram(client, rdb, conn, cfg, session, text, wanted, now)


async def _search_telegram(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session: ResearchSession,
    text: str,
    wanted: Sequence[SearchKind],
    now: int | None,
) -> list[GlobalSearchReport]:
    """Run the searches ``wanted`` for ``text`` — whose switches, grant and query the caller
    checked — one report each; a flood wait ends the rest."""
    stamp = research_db.clock(now)
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
            report.flood_wait_s = sync.flood_seconds(exc)
            report.warnings.append(
                f"{kind}: {sync.flood_warning(report.flood_wait_s, 'searching', 'search stopped')}"
            )
            _record_search(rdb, report, stamp)
            break
        except errors.UnauthorizedError as exc:
            tg.reraise_unauthorized(exc, session.account)
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


EntityOutcome = Literal["person", "in_session", "unnamed", "over_cap", "excluded", "new", "known"]
"""What :func:`_record_entity` did with one chat Telegram answered with: left it out — a person,
a chat the session already reads, one nothing names, one over the candidate cap, an excluded
one — or recorded it as a ``new`` candidate or evidence of a ``known`` one."""


@dataclass(frozen=True, slots=True)
class _Recorded:
    outcome: EntityOutcome
    candidate: Candidate | None = None
    added: bool = False
    """Whether the evidence was a path not recorded before."""


def _record_entity(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    reads: Collection[int],
    entity: Any,
    *,
    depth: int,
    parent_id: int | None,
    room_left: int,
    facts: Mapping[str, Any],
    via: EvidenceVia,
    origin_key: str,
    in_itself: bool,
    msg_id: int | None,
    snippet: str | None,
    stamp: int,
) -> _Recorded:
    """Record one chat Telegram answered with — a chat of a shared folder, a global search's
    result — as a candidate probed on the spot, with its evidence, inside the caller's
    ``research.db`` transaction.

    A person (a user or bot), a chat the session already reads (``reads``, index rows) and an
    excluded chat are left out, and so is a new one once ``room_left`` is spent. What Telegram
    said about the chat is stored as probed facts, ``facts`` on top (a folder knows whether the
    account joined it); the candidate is named by its ``@username`` or else its marked id
    (:func:`entity_target`), ``depth`` hops from the question and inside ``parent_id``. The
    evidence was found ``in_itself`` — in the chat, for a search result — or nowhere indexed.

    A candidate probed as another peer that goes by this chat's name is a name that moved: it
    is set aside (:func:`_name_moved`) and this chat becomes a candidate of its own, under its
    marked id when the name is taken (:func:`grepogram.research_db.add_candidate`) — never
    written over the row a human may have approved.
    """
    if isinstance(entity, types.User):
        return _Recorded("person")
    known = {**entity_facts(entity), **facts}
    marked = known["peer_id"]
    if set(reads) & set(held_rows(conn, marked, known.get("username"))):
        return _Recorded("in_session")
    target = entity_target(entity)
    if target is None:
        return _Recorded("unnamed")
    for moved in research_db.renamed_away(
        rdb, session.id, target.target, known.get("username"), marked
    ):
        # the chat Telegram answered with goes by a name another candidate was probed under
        _name_moved(rdb, moved, entity, stamp)
    existing = research_db.candidate_for(
        rdb, session.id, target.target, peer_id=marked, username=known.get("username")
    )
    if existing is None and room_left <= 0:
        return _Recorded("over_cap")
    candidate = research_db.add_candidate(
        rdb,
        session.id,
        target.target,
        candidate_kind(target),
        depth,
        peer_id=marked,
        username=known.get("username"),
        parent_id=parent_id,
        now=stamp,
    )
    if candidate is None:
        return _Recorded("excluded")
    candidate = _reconcile(
        rdb, research_db.update_candidate(rdb, candidate.id, probed_at=stamp, **known)
    )
    added = research_db.add_evidence(
        rdb,
        candidate.id,
        via,
        origin_key,
        chat=ChatKey(chat_scope(known["type"], session.account), marked) if in_itself else None,
        msg_id=msg_id,
        snippet=snippet,
        now=stamp,
    )
    return _Recorded("new" if existing is None else "known", candidate, added)


def _found_chat(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    reads: Collection[int],
    report: GlobalSearchReport,
    entity: Any,
    *,
    origin_key: str,
    msg_id: int | None,
    snippet_text: str | None,
    stamp: int,
) -> None:
    """Record one chat a global search answered with (:func:`_record_entity`) in ``report``."""
    recorded = _record_entity(
        rdb,
        conn,
        session,
        reads,
        entity,
        depth=1,
        parent_id=None,
        room_left=room(rdb, session),
        facts={},
        via=report.kind,
        origin_key=origin_key,
        in_itself=True,
        msg_id=msg_id,
        snippet=snippet_text,
        stamp=stamp,
    )
    candidate = recorded.candidate
    if recorded.outcome == "person":
        report.people += 1
    elif recorded.outcome == "in_session":
        report.in_session += 1
    elif recorded.outcome == "over_cap":
        report.over_cap += 1
    elif recorded.outcome == "excluded":
        report.excluded += 1
    elif candidate is not None and recorded.outcome == "new":
        report.new_candidates.append(candidate.id)
    elif candidate is not None and recorded.added and candidate.id not in report.updated_candidates:
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
    reads = scan_targets(rdb, conn, session)
    with db.transaction(rdb):
        for marked in dict.fromkeys(peers):
            entity = entities.get(marked)
            if entity is None:
                continue
            _found_chat(
                rdb,
                conn,
                session,
                reads.keys(),
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
        if not _consume_paid_grant(rdb, session, price, stamp):
            report.warnings.append(
                "post_search: free searches are used up and the paid_search approval was used "
                "by another search meanwhile; nothing was paid"
            )
            return
        paid = price
    try:
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
    except errors.RPCError:
        if paid is not None:
            report.warnings.append(
                "post_search: the paid search failed and its paid_search approval is spent; "
                "approve paid_search again to try once more"
            )
        raise
    report.ran = True
    report.paid_stars = paid or 0
    entities = {dialogs.peer_id(e): e for e in (*answer.chats, *answer.users)}
    posts = [m for m in answer.messages if isinstance(m, types.Message)]
    report.results = len(posts)
    reads = scan_targets(rdb, conn, session)
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
                reads.keys(),
                report,
                entity,
                origin_key=f"post:{marked}/{post.id}",
                msg_id=post.id,
                snippet_text=snippet(post.message or "", report.query),
                stamp=stamp,
            )


def _consume_paid_grant(
    rdb: sqlite3.Connection, session: ResearchSession, price: int, stamp: int
) -> bool:
    """Use up one live ``paid_search`` grant of ``session`` that allows ``price`` stars;
    ``False`` when none is left.

    :func:`grepogram.research_db.consume_grant` is one conditional ``UPDATE``, so of two
    searches racing for the same grant exactly one gets it, and only that one may pay.
    """
    for grant in _session_grants(rdb, session.id, "paid_search"):
        if (grant.stars_max or 0) >= price and research_db.consume_grant(rdb, grant.id, now=stamp):
            return True
    return False


def _paid_refusal(
    rdb: sqlite3.Connection, cfg: Config, session: ResearchSession, price: int
) -> str | None:
    """Why a post search that costs ``price`` stars may not be paid for, or ``None``: the price
    must fit both ``paid_stars_max`` as it is now and the ceiling the approval named."""
    ceiling = cfg.research.paid_stars_max
    if ceiling <= 0:
        return "paid search is off (paid_stars_max = 0)"
    if price <= 0:
        return "Telegram offers no paid search now"
    if price > ceiling:
        return f"the next one costs {price} stars, above paid_stars_max = {ceiling}"
    if not search_granted(rdb, session.id, "paid_search"):
        return f"paying {price} stars needs a separate paid_search approval"
    approved = _paid_ceiling(rdb, session.id)
    if price > approved:
        return (
            f"the next one costs {price} stars, above the {approved} the paid_search approval "
            "allows; approve paid_search again to pay more"
        )
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
    """One discover call: offline discovery, then — with a client — the pinned posts of the
    session's chats not read yet (:func:`read_pins`), the global searches the session may run and
    has not run for its question yet, then a bounded probing pass.

    The offline half reads the whole index and runs on a worker thread, so a server's event loop
    stays free meanwhile. Global search runs only while ``[research]`` switches it on and a
    ``global_search`` grant is live; without them this call simply does not search. A flood wait
    while reading pins or searching skips what follows for this call. Nothing is sent before
    :func:`grepogram.sync.check_account` has made sure the client is the Telegram user the index
    recorded for the session's account (:class:`~grepogram.tg.OtherUser` otherwise).
    """
    report = await sync.joined_to_thread(
        functools.partial(discover_offline, rdb, conn, cfg, session_id, now=now)
    )
    if client is None:
        return report
    session = active_session(rdb, session_id)
    await sync.check_account(conn, session.account, client)
    pins = await read_pins(client, rdb, conn, cfg, session.id, now=now)
    if pins.flood_wait_s is not None:
        return dataclasses.replace(report, pins=pins)
    searches: list[GlobalSearchReport] = []
    granted = granted_kinds(rdb, session.id)
    if granted:
        done = {
            (record.kind, record.query)
            for record in research_db.list_searches(rdb, session.id)
            if record.note is None
        }
        question = " ".join(session.question.split())
        kinds = [
            kind for kind in search_kinds(cfg) if kind in granted and (kind, question) not in done
        ]
        if kinds:
            searches = await _search_telegram(client, rdb, conn, cfg, session, question, kinds, now)
    flooded = any(search.flood_wait_s is not None for search in searches)
    probed = None if flooded else await _probe_pass(client, rdb, conn, session, now)
    return dataclasses.replace(report, pins=pins, probe=probed, searches=tuple(searches))


# --- approval --------------------------------------------------------------------------------

_CANDIDATE_ORDER: tuple[CandidateAction, ...] = ("join", "request", "fetch", "add_source")
_SESSION_ORDER: tuple[SessionAction, ...] = ("global_search", "paid_search")
_GRANTABLE: frozenset[CandidateStatus] = frozenset(
    {"proposed", "approved", "skipped", "failed", "joined", "pending_admission"}
)
"""Statuses a grant may be given in: not acted on yet, or part-way (joined, waiting for an
admin). An excluded, unavailable or fetched candidate takes none (:data:`_REFUSED`)."""
_REFUSED: dict[str, str] = {
    "excluded": "it is excluded; lift that with `grepogram research unexclude` first",
    "unavailable": "Telegram refused it when it was probed",
    "fetched": "a run already fetched it and added it as a source",
}
_TO_APPROVED: frozenset[CandidateStatus] = frozenset({"proposed", "skipped", "failed"})
"""Statuses a grant moves to ``approved``; a joined or waiting candidate keeps what it is."""
_SKIPPABLE: frozenset[CandidateStatus] = frozenset(
    {"proposed", "approved", "skipped", "failed", "unavailable", "joined", "pending_admission"}
)
"""Statuses skip takes: all but the settled ones. A joined or waiting candidate stays in the chat
on Telegram, but what is still approved for it — its fetch, its source — is withdrawn."""
APPROVE_HINT = "review the summary and approve again"
DESCENDANTS_NOTE = (
    "Nothing found inside these chats is approved by this: every chat discovered through them "
    "is proposed for an approval of its own."
)


class UnknownCandidate(ResearchError):
    def __init__(self, session_id: int, candidate_id: int) -> None:
        super().__init__(
            f"no candidate {candidate_id} in research session {session_id}",
            f"list them with `grepogram research candidates {session_id}`",
        )


def horizon(session: ResearchSession) -> str:
    """The date a source a run adds for ``session`` starts from: ``since_days`` before the
    session started, so the date an approval names is the one the run uses, whenever it runs.

    Never before the first date there is: a session stored before ``since_days`` had a ceiling
    may ask for more days than the calendar holds, and must still answer rather than raise."""
    started = datetime.fromtimestamp(session.created_at, UTC).date()
    days = min(session.limits.since_days, (started - date.min).days)
    return (started - timedelta(days=days)).isoformat()


def authorized(
    rdb: sqlite3.Connection, target: Candidate | ResearchSession, action: GrantAction
) -> bool:
    """Whether a human approved ``action`` for ``target`` and that approval is still live — the
    one check everything that acts on a candidate, or searches for a session, goes through.

    Only a grant naming ``target`` itself counts: approving a folder or a chat authorizes
    nothing found inside it. The session must be active (stopping voids its grants) — every
    grant of a session is given to its one account (:func:`grant`) — and a candidate skipped
    since, or a chat an exclusion covers under any of its spellings, is not authorized whatever
    its grants say.
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


@dataclass(frozen=True, slots=True)
class _Entry:
    """One approval target after validation: the candidate (``None`` for the session), the
    actions this approval adds and those an earlier live grant already holds."""

    candidate: Candidate | None
    actions: tuple[GrantAction, ...]
    already: tuple[GrantAction, ...]


@dataclass(frozen=True, slots=True)
class _Approval:
    session: ResearchSession
    entries: tuple[_Entry, ...]
    summary: str


def _live_actions(
    rdb: sqlite3.Connection, session: ResearchSession, candidate_id: int | None
) -> set[str]:
    return {
        action
        for grant in research_db.live_grants(rdb, session.id, candidate_id)
        for action in grant.actions
    }


def _way_in_route(rdb: sqlite3.Connection, candidate: Candidate, action: str) -> str | None:
    """How a run would take ``action`` — ``join`` or ``request`` — into ``candidate`` as things
    stand, as a grant records it (:func:`grepogram.research_db.check_join_route`): its invite
    link, its public username, the shared folder it was found in (a join only), or its id and
    the access hash its probe stored; ``None`` when there is no way in. What an approval
    records is what the run takes (:func:`_granted_route`), whatever the candidate learns later.
    """
    if candidate.kind == "invite" and candidate.invite_hash:
        return "invite"
    if candidate.username:
        return "username"
    if action == "join" and candidate.parent_id is not None:
        parent = research_db.get_candidate(rdb, candidate.parent_id)
        if parent is not None and parent.kind == "addlist" and parent.addlist_slug:
            return f"{research_db.FOLDER_ROUTE}{parent.id}"
    if candidate.type in SHARED_CHAT_TYPES and candidate.access_hash is not None:
        return "id"
    return None


def _route_folder(rdb: sqlite3.Connection, candidate: Candidate, route: str) -> Candidate | None:
    """The shared folder a ``folder:<id>`` route names, while it is still one of the candidate's
    session with a link to join through; ``None`` for any other route."""
    folder_id = route.removeprefix(research_db.FOLDER_ROUTE)
    if folder_id == route:
        return None
    parent = research_db.get_candidate(rdb, int(folder_id))
    if parent is None or parent.session_id != candidate.session_id:
        return None
    if parent.kind != "addlist" or not parent.addlist_slug:
        return None
    return parent


def _route_words(rdb: sqlite3.Connection, candidate: Candidate, route: str) -> str:
    """``route`` in the words an approval summary shows."""
    if route == "invite":
        return f"through the invite link t.me/+{shown(candidate.invite_hash or '')}"
    if route == "username":
        return f"through its public username @{shown(candidate.username or '')}"
    if route == "id":
        return "by its id"
    parent = _route_folder(rdb, candidate, route)
    assert parent is not None and parent.addlist_slug is not None  # only built for a live folder
    folder = f"{_quoted(parent.title)} " if parent.title else ""
    return (
        f"through the shared folder {folder}(t.me/addlist/{shown(parent.addlist_slug)}); "
        "Telegram also adds that folder to the account's chat folders"
    )


def _candidate_entry(
    rdb: sqlite3.Connection,
    session: ResearchSession,
    candidate_id: int,
    requested: Sequence[str],
) -> _Entry:
    candidate = research_db.get_candidate(rdb, candidate_id)
    if candidate is None or candidate.session_id != session.id:
        raise UnknownCandidate(session.id, candidate_id)
    label = f"candidate {candidate.id} ({candidate.identity})"

    def refuse(reason: str, hint: str | None = None) -> NoReturn:
        raise ResearchError(f"{label}: {reason}", hint)

    for action in requested:
        if action in _SESSION_ORDER:
            refuse(f"{action} is approved for the session, not for one chat")
        if action not in _CANDIDATE_ORDER:
            refuse(f"unknown action {action!r}; expected {', '.join(_CANDIDATE_ORDER)}")
    if candidate.status in _REFUSED:
        refuse(_REFUSED[candidate.status])
    if candidate.status not in _GRANTABLE:  # pragma: no cover - every status is one or the other
        refuse(f"a {candidate.status} candidate takes no approval")
    if candidate.kind == "addlist":
        refuse(
            "a shared folder is approved chat by chat: once probed, each of its chats is a "
            "candidate of its own, and approving the folder would approve nothing inside it"
        )
    if candidate.probed_at is None:
        refuse(
            "not probed yet, so there is nothing to show what it is",
            f"run `grepogram research discover {session.id}` first",
        )
    if candidate.type in PEOPLE_CHAT_TYPES:
        refuse("a person's account, not a group or channel")
    live = _live_actions(rdb, session, candidate.id)
    effective = live | set(requested)
    new = [action for action in _CANDIDATE_ORDER if action in requested and action not in live]
    member = candidate.member is True or candidate.status == "joined"
    waiting = candidate.status == "pending_admission"
    if "join" in new:
        if member:
            refuse("the account is already a member; there is nothing to join")
        if waiting:
            refuse("an admission request is already waiting for the chat's admins")
        if candidate.request_needed:
            refuse("joining it needs its admins' approval: approve `request` instead of `join`")
        if _way_in_route(rdb, candidate, "join") is None:
            refuse("there is no way to join it: no username, invite link or shared folder names it")
    if "request" in new:
        if member:
            refuse("the account is already a member; there is nothing to request")
        if waiting:
            refuse("an admission request is already waiting for the chat's admins")
        if not candidate.request_needed:
            refuse("`request` is only for a chat whose invite asks for its admins' approval")
        if _way_in_route(rdb, candidate, "request") is None:
            refuse("there is no way to ask to join it: no username or invite link names it")
    if {"join", "request"} <= effective:
        refuse("`join` and `request` are two ways in; approve one of them")
    if "fetch" in effective and "add_source" not in effective:
        refuse(
            "fetching makes it an ongoing source: approve `add_source` together with `fetch`",
            f"approve it as {candidate.id}:fetch,add_source",
        )
    reads = member or waiting or bool({"join", "request"} & effective) or bool(candidate.username)
    if {"fetch", "add_source"} & set(new) and not reads:
        refuse("the account is not a member of this private chat: approve `join` or `request` too")
    kept = tuple(action for action in _CANDIDATE_ORDER if action in live)
    return _Entry(candidate=candidate, actions=tuple(new), already=kept)


def _session_entry(
    rdb: sqlite3.Connection, cfg: Config, session: ResearchSession, requested: Sequence[str]
) -> _Entry:
    def refuse(reason: str, hint: str | None = None) -> NoReturn:
        raise ResearchError(f"session {session.id}: {reason}", hint)

    for action in requested:
        if action in _CANDIDATE_ORDER:
            refuse(f"{action} is approved for a candidate; name its id")
        if action not in _SESSION_ORDER:
            refuse(f"unknown action {action!r}; expected {', '.join(_SESSION_ORDER)}")
    live = _live_actions(rdb, session, None)
    if not set(search_kinds(cfg)) <= set(granted_kinds(rdb, session.id)):
        # a search switched on since was never approved: asking again names every search
        live.discard("global_search")
    if _paid_ceiling(rdb, session.id) < cfg.research.paid_stars_max:
        live.discard("paid_search")  # paying more than approved needs a new approval
    effective = live | set(requested)
    new = [action for action in _SESSION_ORDER if action in requested and action not in live]
    if "global_search" in new and not search_kinds(cfg):
        refuse("global search is off", SEARCH_OFF_HINT)
    if "paid_search" in new:
        if cfg.research.paid_stars_max <= 0:
            refuse(
                "paid search is off (paid_stars_max = 0)",
                "set `paid_stars_max` under [research] to the most one search may cost",
            )
        if not cfg.research.post_search:
            refuse("paid search pays for post search, which is off", SEARCH_OFF_HINT)
        if "global_search" not in effective:
            refuse("paid search needs `global_search` approved too")
    kept = tuple(action for action in _SESSION_ORDER if action in live)
    return _Entry(candidate=None, actions=tuple(new), already=kept)


def _target_line(conn: sqlite3.Connection, session: ResearchSession, c: Candidate) -> list[str]:
    handle = (
        f"@{shown(c.username)}"
        if c.username
        else f"invite link t.me/+{shown(c.invite_hash)}"
        if c.kind == "invite" and c.invite_hash
        else f"id {c.peer_id}"
        if c.peer_id is not None
        else shown(c.identity)
    )
    name = f"{_quoted(c.title)} ({handle})" if c.title else handle
    facts: list[str] = [c.type or "chat of unknown type"]
    if c.participants is not None:
        facts.append(f"{c.participants:,} members")
    if c.request_needed:
        facts.append("its admins approve who joins")
    member = {True: "is a member", False: "is not a member", None: "membership unknown"}[
        True if c.status == "joined" else c.member
    ]
    if c.status == "pending_admission":
        member = "has an admission request waiting"
    chat_ids, accounts = cached_in(conn, c)
    cached = (
        "not in the index yet"
        if not chat_ids
        else f"already in the index through {', '.join(accounts)}"
        if accounts
        else "already in the index from an import"
    )
    return [
        f"Candidate {c.id}: {name}, {', '.join(facts)}",
        f"  account {session.account} {member}; {cached}",
    ]


def _covering_source(
    conn: sqlite3.Connection, cfg: Config, session: ResearchSession, c: Candidate
) -> Source | None:
    """The configured source a run's fetch of ``c`` would actually go through, if one already
    covers the chat: the source the index files the chat under (its primary — a narrowed sync
    reads a chat through its primary source's account, ``since`` and ``comments``), else a chat
    source of the session's account that names it. ``None`` when the fetch goes through the
    source the run adds."""
    by_id = {source.id: source for source in cfg.sources}
    chat_ids, _ = cached_in(conn, c)
    for chat_id in chat_ids:
        row = db.get_chat(conn, chat_id)
        if row is None or row.scope not in ("", session.account):
            continue
        if row.source_id in by_id:
            return by_id[row.source_id]
    return _configured(cfg, session.account, c)


def _discussion(c: Candidate, comments: bool) -> str:
    if c.type != "channel":
        return ""
    return f"{'with' if comments else 'without'} the comments of its discussion group"


def _action_line(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session: ResearchSession,
    c: Candidate,
    action: str,
    approved: Collection[str],
) -> str:
    """One action in words, as the run will carry it out; ``approved`` is every action the
    candidate holds once this approval is granted, the ones already live included."""
    account = session.account
    since = horizon(session)
    if action in ("join", "request"):
        route = _way_in_route(rdb, c, action)
        way = "" if route is None else f" {_route_words(rdb, c, route)}"
        if action == "join":
            return f"join it as {account}{way}; the account becomes a member, visible to its admins"
        return (
            f"send a request to join it as {account}{way}; its admins see the request and "
            "decide, and the account joins only once they admit it"
        )
    covering = _covering_source(conn, cfg, session, c)
    if action == "fetch":
        if covering is not None:
            comments = _discussion(c, covering.comments)
            return (
                f"fetch its history into the local index through {covering.id}, the source that "
                f"already covers it: as account {covering.account}, since "
                f"{covering.since or 'its first message'}{', ' + comments if comments else ''}"
            )
        comments = " with the comments of its discussion group" if c.type == "channel" else ""
        inside = c.member is True or c.status in ("joined", "pending_admission")
        outside = (
            ""
            if inside or {"join", "request"} & set(approved)
            else (", reading it as a public chat without joining it")
        )
        return (
            f"fetch its history since {since}{comments} as {account} into the local index{outside}"
        )
    existing = _configured(cfg, account, c)
    if existing is not None:
        return (
            f"it is already {existing.id}, a source of account {account}: that source is kept as "
            "it is and nothing is added to the config"
        )
    comments = _discussion(c, True)
    pinned = (
        ""
        if c.peer_id is None
        else f"; the source names the chat by its id {c.peer_id}, so it stays this chat whatever "
        "its username does later"
    )
    return (
        f"add it as an ongoing source of account {account} (since {since}"
        f"{', ' + comments if comments else ''}): regular sync and search will include it from "
        f"now on, and stopping this research session does not remove it{pinned}"
    )


def _session_line(cfg: Config, session: ResearchSession, action: str) -> str:
    account = session.account
    if action == "global_search":
        where = {
            "chat_search": "Telegram's search for public chats by name (contacts.search)",
            "post_search": "Telegram's search of public channel posts (channels.searchPosts)",
        }
        kinds = " and ".join(where[kind] for kind in search_kinds(cfg))
        return (
            f"send this session's question {_quoted(session.question)} as {account} to {kinds}: "
            "the query leaves this computer and reaches Telegram, and the results may include "
            "snippets from channels and groups you have never joined or indexed; they are kept "
            "as evidence in research.db only, and later discover calls reuse this approval "
            "until the session stops"
        )
    return (
        f"pay up to {cfg.research.paid_stars_max} Telegram Stars from {account}'s balance for "
        "one public post search once the free daily quota is spent; this approval pays once"
    )


def _summary(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session: ResearchSession,
    entries: Sequence[_Entry],
) -> str:
    """The approval text. Every value someone else chose — the question, a title, a username —
    goes through :func:`shown`, so it cannot break a line or send a terminal escape."""
    lines = [
        f"Research session {session.id}: {_quoted(session.question)}",
        f"Acting account: {session.account}",
    ]
    repeats: list[str] = []
    for entry in entries:
        c = entry.candidate
        if not entry.actions:
            repeats.append("session searches" if c is None else f"candidate {c.id}")
            continue
        lines.append("")
        if c is None:
            lines.append(f"Session {session.id} (every discover call of it)")
            lines.extend(f"  - {_session_line(cfg, session, a)}" for a in entry.actions)
        else:
            approved = {*entry.actions, *entry.already}
            lines.extend(_target_line(conn, session, c))
            lines.extend(
                f"  - {_action_line(rdb, conn, cfg, session, c, a, approved)}"
                for a in entry.actions
            )
        if entry.already:
            lines.append(f"  (already approved, not asked again: {', '.join(entry.already)})")
    if repeats:
        lines.extend(["", f"Already approved and not asked again: {', '.join(repeats)}."])
    lines.extend(["", DESCENDANTS_NOTE])
    return "\n".join(lines)


def _prepare(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    items: Sequence[ApprovalItem],
) -> _Approval:
    require_enabled(cfg)
    session = active_session(rdb, session_id)
    merged: dict[int | None, list[str]] = {}
    for item in items:
        merged.setdefault(item.candidate_id, []).extend(item.actions)
    if not merged:
        raise ResearchError("nothing to approve", "name candidates and the actions to approve")
    entries: list[_Entry] = []
    for candidate_id, actions in merged.items():
        if not actions:
            what = "the session" if candidate_id is None else f"candidate {candidate_id}"
            raise ResearchError(f"no action named for {what}")
        wanted = list(dict.fromkeys(actions))
        entries.append(
            _session_entry(rdb, cfg, session, wanted)
            if candidate_id is None
            else _candidate_entry(rdb, session, candidate_id, wanted)
        )
    if not any(entry.actions for entry in entries):
        raise ResearchError(
            "everything named is already approved; nothing to ask",
            f"run it with `grepogram research run {session.id}`",
        )
    return _Approval(session, tuple(entries), _summary(rdb, conn, cfg, session, entries))


def approval_summary(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    items: Sequence[ApprovalItem],
) -> str:
    """The exact text a human reads before approving ``items``, or :class:`ResearchError` when
    the approval would be refused (an unknown candidate, an action its state rules out).

    Per target it names the chat, the acting account, whether that account is a member, whether
    the index already holds it, and each action in plain words — the join route, an admission
    request, the history horizon, and that ``add_source`` makes it an ongoing source regular sync
    and search include. A session-wide search approval says the question leaves the computer and
    what Telegram may answer with; a paid one says how many stars it may spend. Nothing is
    written.
    """
    return _prepare(rdb, conn, cfg, session_id, items).summary


def grant(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    items: Sequence[ApprovalItem],
    *,
    via: GrantChannel,
    summary: str,
    now: int | None = None,
) -> list[Grant]:
    """Record what a human approved after reading ``summary`` through ``via``.

    Only the two consent channels call this — the CLI after reading the controlling terminal
    (``cli``) and the MCP server after an accepted elicitation (``elicitation``) — and ``via``
    has no default. The approval is validated again and its summary rebuilt: when it no longer
    matches the text the human saw (a probe changed what a candidate is, another approval landed
    meanwhile) nothing is granted. One grant per candidate holds the actions not already live,
    and one per session action; a ``proposed``, ``skipped`` or ``failed`` candidate becomes
    ``approved``. The validation and the writing are one ``research.db`` transaction.
    """
    stamp = research_db.clock(now)
    granted: list[Grant] = []
    with db.transaction(rdb):
        # validated inside the writing transaction: a skip or an exclusion landing between the
        # check and the write would otherwise be overwritten with a live grant
        approval = _prepare(rdb, conn, cfg, session_id, items)
        if approval.summary != summary:
            raise ResearchError(
                "what this approval covers changed since its summary was shown; nothing was "
                "granted",
                APPROVE_HINT,
            )
        for entry in approval.entries:
            if not entry.actions:
                continue
            candidate = entry.candidate
            # a session action is a grant of its own: paying for one search consumes the
            # paid_search grant and must leave the global_search approval standing
            groups = [entry.actions] if candidate is not None else [(a,) for a in entry.actions]
            for actions in groups:
                # a session grant keeps the terms the summary named (_session_line): the kinds
                # of search and the stars, read from the config at the time the human read them;
                # a join or request keeps the way in its line named, the only one a run takes
                way_in = next((a for a in actions if a in ("join", "request")), None)
                route = (
                    None
                    if candidate is None or way_in is None
                    else _way_in_route(rdb, candidate, way_in)
                )
                granted.append(
                    research_db.add_grant(
                        rdb,
                        session_id=approval.session.id,
                        candidate_id=None if candidate is None else candidate.id,
                        account=approval.session.account,
                        actions=actions,
                        via=via,
                        summary=summary,
                        search_kinds=search_kinds(cfg) if "global_search" in actions else (),
                        stars_max=(
                            cfg.research.paid_stars_max if "paid_search" in actions else None
                        ),
                        join_route=route,
                        now=stamp,
                    )
                )
            if candidate is not None and candidate.status in _TO_APPROVED:
                research_db.update_candidate(rdb, candidate.id, status="approved")
    log.info(
        "research session %d: %d grant(s) recorded through %s",
        approval.session.id,
        len(granted),
        via,
    )
    return granted


def skip(
    rdb: sqlite3.Connection, cfg: Config, session_id: int, candidate_ids: Sequence[int]
) -> list[int]:
    """Set candidates aside: ``skipped``, with any live grant of theirs voided — a chat a run
    already joined or asked to join included, so its pending fetch and source never happen (the
    account stays in it; ``grepogram leave`` leaves). Skipping only narrows, so it needs no
    consent; a later approval can take a skipped candidate back."""
    require_enabled(cfg)
    session = active_session(rdb, session_id)
    wanted = list(dict.fromkeys(candidate_ids))
    for candidate_id in wanted:
        candidate = research_db.get_candidate(rdb, candidate_id)
        if candidate is None or candidate.session_id != session.id:
            raise UnknownCandidate(session.id, candidate_id)
        if candidate.status not in _SKIPPABLE:
            raise ResearchError(
                f"candidate {candidate_id} is {candidate.status}; skipping it would undo nothing",
                "leave a joined chat with `grepogram leave`, drop a source with `sources rm`",
            )
    with db.transaction(rdb):
        for candidate_id in wanted:
            research_db.update_candidate(rdb, candidate_id, status="skipped")
        research_db.void_grants(rdb, session.id, candidate_ids=wanted)
    return wanted


def _is_number(text: str) -> bool:
    """Whether ``text`` is a whole number spelled in ASCII digits — what ``int`` reads back
    exactly. ``str.isdigit`` also takes ``²`` and other digits ``int`` refuses."""
    return text.isascii() and text.isdecimal()


def target_identities(
    rdb: sqlite3.Connection, refs: Sequence[str], session_id: int | None = None
) -> list[str]:
    """The candidate identities ``refs`` name: a positive number is a candidate of
    ``session_id``; anything else is a link, ``@name``, ``+hash``, ``addlist/<slug>`` or marked
    peer id, taken to the chat it names."""
    identities: list[str] = []
    for ref in refs:
        text = ref.strip()
        if _is_number(text):
            if session_id is None:
                raise ResearchError(
                    f"{shown(text)} is a candidate id and needs a session", "name the session too"
                )
            if research_db.get_session(rdb, session_id) is None:
                raise UnknownSession(session_id)
            number = leads.number(text)
            if number is None:  # longer than any id: no row holds it
                raise ResearchError(
                    f"no candidate {shown(text)} in research session {session_id}",
                    f"list them with `grepogram research candidates {session_id}`",
                )
            candidate = research_db.get_candidate(rdb, number)
            if candidate is None or candidate.session_id != session_id:
                raise UnknownCandidate(session_id, number)
            identities.append(candidate.identity)
            continue
        target = leads.normalize(text)
        bare = leads.number(text.removeprefix("-")) if _is_number(text.removeprefix("-")) else None
        if target is None and bare is not None:
            target = leads.peer(-bare if text.startswith("-") else bare)
        chat = None if target is None else chat_level(target)
        if chat is None:
            raise ResearchError(
                f"{ref!r} names no chat",
                "give a candidate id, an @username, a t.me link or a marked chat id",
            )
        identities.append(chat.target)
    return list(dict.fromkeys(identities))


def exclude(
    rdb: sqlite3.Connection,
    cfg: Config,
    refs: Sequence[str],
    *,
    session_id: int | None = None,
    reason: str | None = None,
) -> dict[str, int]:
    """Exclude the targets ``refs`` name (:func:`target_identities`) from every session, present
    and future; ``identity → candidates moved to excluded``. Narrowing needs no consent, and the
    live grants of those candidates are voided with it."""
    require_enabled(cfg)
    identities = target_identities(rdb, refs, session_id)
    return {identity: research_db.add_exclusion(rdb, identity, reason) for identity in identities}


def unexclude(
    rdb: sqlite3.Connection, cfg: Config, refs: Sequence[str], *, session_id: int | None = None
) -> list[str]:
    """Lift the exclusions ``refs`` name; the identities that were excluded. Their candidates are
    proposed again and nothing is approved: the grants the exclusion voided stay void."""
    require_enabled(cfg)
    identities = target_identities(rdb, refs, session_id)
    return [identity for identity in identities if research_db.remove_exclusion(rdb, identity)]


def stop(rdb: sqlite3.Connection, cfg: Config, session_id: int) -> int:
    """Stop a session: it explores no further and every grant it has not used is voided; the
    sources its runs added stay. Returns how many grants were voided."""
    require_enabled(cfg)
    if research_db.get_session(rdb, session_id) is None:
        raise UnknownSession(session_id)
    voided = research_db.stop_session(rdb, session_id)
    log.info("research session %d stopped, %d grant(s) voided", session_id, voided)
    return voided


# --- the run ---------------------------------------------------------------------------------

_ACTIONABLE: tuple[CandidateStatus, ...] = ("approved", "joined", "pending_admission")
"""Statuses a run acts on: approved and not acted on yet, joined but not fetched, or waiting
for an admission. The rest are decided (skipped, excluded), done (fetched) or refused."""
_JOIN_REFUSED: tuple[type[Exception], ...] = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChatForbiddenError,
    errors.InviteHashExpiredError,
    errors.InviteHashInvalidError,
    errors.InviteHashEmptyError,
)
"""A join Telegram refuses because of the chat: it went private, the link died, the account is
banned from it. The candidate is ``unavailable``; no later run would do better."""
_JOIN_UNREACHABLE: tuple[type[Exception], ...] = (*_JOIN_REFUSED, ValueError)
"""Those, and a chat this account cannot address at all (a username no longer held)."""
PENDING_NOTE = "an admission request is waiting for the chat's admins"
PARTIAL_NOTE = "part of its history is fetched; the next run goes on from there"


def _granted(rdb: sqlite3.Connection, candidate: Candidate) -> bool:
    return any(authorized(rdb, candidate, action) for action in _CANDIDATE_ORDER)


def _fresh(rdb: sqlite3.Connection, candidate: Candidate) -> Candidate:
    current = research_db.get_candidate(rdb, candidate.id)
    assert current is not None  # candidates are never deleted while their session exists
    return current


def _is_member(candidate: Candidate) -> bool:
    return candidate.member is True or candidate.status in ("joined", "fetched")


def _flood_note(report: RunReport, exc: errors.FloodError, what: str) -> None:
    report.warnings.append(
        sync.flood_warning(sync.flood_seconds(exc), what, "the run stopped there")
    )
    report.stopped_by = "flood"


def _refuse_candidate(
    rdb: sqlite3.Connection,
    session: ResearchSession,
    candidate: Candidate,
    status: CandidateStatus,
    note: str,
    report: RunReport,
) -> None:
    """Record a refusal honestly and void the candidate's grants: a ``failed`` one takes a new
    approval once the cause is gone, an ``unavailable`` one none at all."""
    with db.transaction(rdb):
        research_db.update_candidate(rdb, candidate.id, status=status, note=note)
        research_db.void_grants(rdb, session.id, candidate_ids=[candidate.id])
    (report.unavailable if status == "unavailable" else report.failed).append(candidate.id)
    # a note can quote a shared folder's or an invite's link — a private way in that came out
    # of someone's message — so it stays at DEBUG, like message text
    log.info("research candidate %d is %s", candidate.id, status)
    log.debug("research candidate %d: %s", candidate.id, note)


class _OtherChat(ValueError):
    """Telegram named a different chat than the one a human approved: a username moved to
    another chat since the probe, or an invite now leads elsewhere."""


def _mark_joined(
    rdb: sqlite3.Connection,
    session: ResearchSession,
    candidate: Candidate,
    entity: Any,
    report: RunReport,
    note: str | None = None,
) -> Candidate | None:
    """Record that the account is in ``candidate``'s chat; ``entity`` is what the join answered
    with. An answer naming a chat other than the one probed and approved is not taken for it —
    the candidate's peer id is never overwritten — and the candidate is ``failed`` instead, with
    a note naming the chat the account is now in; ``None`` then."""
    facts = entity_facts(entity) if entity is not None else {}
    landed = facts.get("peer_id")
    if candidate.peer_id is not None and landed is not None and landed != candidate.peer_id:
        where = f" {_quoted(facts['title'])}" if facts.get("title") else ""
        wrong = (
            f"Telegram answered the join with a different chat{where} (id {landed}) than the one "
            f"approved (id {candidate.peer_id}); nothing was fetched or added — leave it with "
            f"`grepogram leave --account {session.account} -- {landed}` if the account should "
            "not be there"
        )
        _refuse_candidate(rdb, session, candidate, "failed", wrong, report)
        return None
    facts["member"] = True
    joined = research_db.update_candidate(rdb, candidate.id, status="joined", note=note, **facts)
    report.joined.append(candidate.id)
    return joined


def _joined_entity(answer: Any, candidate: Candidate) -> Any:
    """The chat a join answered with.

    For a candidate whose peer id a probe learned: that chat when the answer holds it, else the
    first group or channel of the answer — which :func:`_mark_joined` refuses to take for it.
    For one whose peer id no probe learned (an invite that only showed a title): the one chat of
    the answer that is what the probe saw — its type, and its title when two are alike — or
    ``None`` when the answer does not say which chat the invite led to; the old group a
    supergroup was migrated from is never it."""
    chats = [
        chat
        for chat in getattr(answer, "chats", None) or ()
        if isinstance(chat, types.Channel | types.Chat)
    ]
    if candidate.peer_id is not None:
        for chat in chats:
            if dialogs.peer_id(chat) == candidate.peer_id:
                return chat
        return chats[0] if chats else None
    alike = [
        chat
        for chat in chats
        if not getattr(chat, "migrated_to", None)
        and (candidate.type is None or dialogs.chat_type(chat) == candidate.type)
    ]
    if len(alike) > 1 and candidate.title:
        alike = [chat for chat in alike if utils.get_display_name(chat) == candidate.title]
    return alike[0] if len(alike) == 1 else None


async def _recheck_admissions(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    report: RunReport,
    stamp: int,
) -> bool:
    """Ask again about every candidate waiting for an admission; ``True`` when a flood wait
    stopped the run. A probe reads metadata only — whether the account is in now — so it needs
    no grant; an admitted candidate is ``joined`` and the rest of its grant runs on.

    A request no admin answered within the session's ``admission_timeout_days`` is given up:
    the candidate is ``failed`` with a note and its grants are voided, so it is not asked about
    forever, and a new approval may send the request again."""
    timeout = session.limits.admission_timeout_days * 86400
    for candidate in research_db.list_candidates(rdb, session.id, ["pending_admission"]):
        if candidate.requested_at is None:
            research_db.update_candidate(rdb, candidate.id, requested_at=stamp)
        elif stamp - candidate.requested_at >= timeout:
            note = (
                f"the admission request got no answer in {session.limits.admission_timeout_days} "
                "days and was given up; approve `request` again to send another"
            )
            _refuse_candidate(rdb, session, candidate, "failed", note, report)
            continue
        try:
            outcome = await probe(client, rdb, conn, candidate, now=stamp)
        except errors.FloodError as exc:
            _flood_note(report, exc, "checking admission requests")
            return True
        current = outcome.candidate
        if current.status != "pending_admission":
            # the name it was requested by leads elsewhere now (_name_moved): set aside
            report.failed.append(current.id)
            continue
        if current.member:
            research_db.update_candidate(
                rdb, current.id, status="joined", note="admitted by the chat's admins"
            )
            report.admitted.append(current.id)
        elif outcome.result == "probed":
            research_db.update_candidate(rdb, current.id, note=PENDING_NOTE)
    return False


def _way_in(rdb: sqlite3.Connection, candidate: Candidate) -> CandidateAction | None:
    """The approved way into ``candidate`` this run still has to take, if any."""
    if candidate.status == "pending_admission" or _is_member(candidate):
        return None
    if authorized(rdb, candidate, "join"):
        return "join"
    if authorized(rdb, candidate, "request"):
        return "request"
    return None


def _granted_route(rdb: sqlite3.Connection, candidate: Candidate, action: str) -> str | None:
    """The way in the live grant approving ``action`` on ``candidate`` recorded — the newest
    one, should several — or ``None`` when none did (a grant from before routes were
    recorded)."""
    for grant in reversed(research_db.live_grants(rdb, candidate.session_id, candidate.id)):
        if action in grant.actions:
            return grant.join_route
    return None


def _route_blocked(rdb: sqlite3.Connection, candidate: Candidate, route: str | None) -> str | None:
    """Why the recorded ``route`` cannot be taken into ``candidate`` now, or ``None`` when it
    can; a run never swaps it for another way in, since the approval showed this one."""
    if route is None:
        return "its approval names no way in"
    if route == "invite":
        return None if candidate.kind == "invite" and candidate.invite_hash else "no invite link"
    if route == "username":
        known = candidate.username or candidate.access_hash is not None
        return None if known else "its username is gone"
    if route == "id":
        known = candidate.peer_id is not None and candidate.access_hash is not None
        return None if known else "no access hash addresses it"
    if _route_folder(rdb, candidate, route) is None:
        return "the shared folder it was approved through is gone"
    return None


async def _join_all(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    work: Sequence[Candidate],
    report: RunReport,
    budget: sync.SyncBudget,
    stamp: int,
) -> bool:
    """Join, or ask to join, every candidate of ``work`` a live grant says to, **by the route
    that grant recorded** and no other; ``True`` when a flood wait stopped the run. The chats
    of one shared folder go in one request naming exactly the approved ones. A candidate whose
    recorded route cannot be taken any more — or whose grant recorded none — is ``failed`` for
    a new approval that shows the way in there is now: a chat approved to join by its id is
    never joined through a folder that listed it afterwards, which would add that folder to the
    account's chat folders without anyone having read so."""
    folders: dict[int, tuple[Candidate, list[Candidate]]] = {}
    for candidate in work:
        action = _way_in(rdb, candidate)
        if action is None:
            continue
        current = _fresh(rdb, candidate)
        route = _granted_route(rdb, current, action)
        blocked = _route_blocked(rdb, current, route)
        if blocked is not None:
            note = f"{blocked}; nothing was sent — approve it again to see how it would go in now"
            _refuse_candidate(rdb, session, current, "failed", note, report)
            continue
        assert route is not None  # _route_blocked refuses a missing one
        parent = _route_folder(rdb, current, route) if action == "join" else None
        if parent is not None:
            folders.setdefault(parent.id, (parent, []))[1].append(current)
            continue
        if budget.expired:
            return False
        try:
            await _join_one(client, rdb, conn, session, current, action, route, report, stamp)
        except errors.FloodError as exc:
            _flood_note(report, exc, "joining more chats")
            return True
    for parent, children in folders.values():
        if budget.expired:
            return False
        try:
            await _join_folder(client, rdb, session, parent, children, report)
        except errors.FloodError as exc:
            _flood_note(report, exc, "joining more chats")
            return True
    return False


async def _input_channel(client: Any, candidate: Candidate) -> Any:
    """The chat a join addresses: the very peer the probe saw, by its id and the access hash
    this account holds for it, whenever both are known — a username is resolved only when they
    are not, and then must still name the probed peer (:func:`_resolve_approved`)."""
    if candidate.peer_id is not None and candidate.access_hash is not None:
        bare, kind = utils.resolve_id(candidate.peer_id)
        if kind is types.PeerChannel:
            return types.InputChannel(bare, candidate.access_hash)
    if candidate.username:
        return utils.get_input_channel(await _resolve_approved(client, candidate))
    raise ValueError("nothing addresses this chat: no username and no access hash")


async def _resolve_approved(client: Any, candidate: Candidate) -> Any:
    """``candidate``'s ``@username`` resolved, refused with :class:`_OtherChat` when it now
    names a chat other than the one probed and approved."""
    entity = await client.get_entity(f"@{candidate.username}")
    found = dialogs.peer_id(entity)
    if candidate.peer_id is not None and found != candidate.peer_id:
        raise _OtherChat(
            f"@{candidate.username} now names a different chat (id {found}) than the one "
            f"approved (id {candidate.peer_id})"
        )
    return entity


async def _join_one(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    candidate: Candidate,
    route: CandidateAction,
    way: str,
    report: RunReport,
    stamp: int,
) -> None:
    """Take ``route`` — ``join`` or ``request``, authorized by :func:`_way_in` just before — into
    one chat, the ``way`` its grant recorded (:func:`_granted_route`).

    An ``invite`` way goes through ``messages.importChatInvite``, a ``username`` or ``id`` one
    through ``channels.joinChannel`` on the probed peer (:func:`_input_channel`); for a chat
    whose admins approve joins either one sends the admission request, which is what a
    ``request`` grant approved.
    """
    account = session.account
    try:
        if way == "invite" and candidate.invite_hash:
            request: Any = functions.messages.ImportChatInviteRequest(hash=candidate.invite_hash)
        else:
            request = functions.channels.JoinChannelRequest(
                channel=await _input_channel(client, candidate)
            )
        answer = await client(request)
    except errors.FloodError:
        raise
    except errors.UnauthorizedError as exc:
        tg.reraise_unauthorized(exc, account)
    except errors.UserAlreadyParticipantError:
        joined = _mark_joined(
            rdb, session, candidate, None, report, "the account was already a member"
        )
        if joined is not None and joined.peer_id is None:
            await probe(client, rdb, conn, joined, now=stamp)
        return
    except errors.InviteRequestSentError:
        note = PENDING_NOTE
        if route == "join":
            note += "; Telegram turned the approved join into an admission request"
        research_db.update_candidate(
            rdb,
            candidate.id,
            status="pending_admission",
            member=False,
            note=note,
            requested_at=stamp,
        )
        report.pending_admission.append(candidate.id)
        return
    except errors.ChannelsTooMuchError:
        note = (
            f"account {account} is in as many channels and groups as Telegram allows; leave "
            "some and approve this one again"
        )
        _refuse_candidate(rdb, session, candidate, "failed", note, report)
        return
    except _OtherChat as exc:
        note = f"{exc}; nothing was joined"
        _refuse_candidate(rdb, session, candidate, "unavailable", note, report)
        return
    except _JOIN_UNREACHABLE as exc:
        note = f"Telegram refused the join: {exc}"
        _refuse_candidate(rdb, session, candidate, "unavailable", note, report)
        return
    except errors.RPCError as exc:
        _refuse_candidate(rdb, session, candidate, "failed", f"the join failed: {exc}", report)
        return
    entity = _joined_entity(answer, candidate)
    if entity is None and candidate.peer_id is None:
        title = _quoted(candidate.title) if candidate.title else None
        seen = " ".join(part for part in (candidate.type, title) if part)
        note = (
            "Telegram's answer to the join does not say which chat the invite led to — none of "
            f"the chats it named is the {seen or 'chat'} the probe saw; nothing was fetched or "
            f"added — check which chats account {account} is in now"
        )
        _refuse_candidate(rdb, session, candidate, "failed", note, report)
        return
    if _mark_joined(rdb, session, candidate, entity, report):
        log.info(
            "research session %d: joined candidate %d as %s", session.id, candidate.id, account
        )


async def _join_folder(
    client: Any,
    rdb: sqlite3.Connection,
    session: ResearchSession,
    parent: Candidate,
    children: Sequence[Candidate],
    report: RunReport,
) -> None:
    """Join exactly ``children`` — each approved for ``join`` on its own — through their shared
    folder's link.

    The folder is checked again first, so the peers go out with the access hashes this account
    holds now: a folder not imported yet is joined with ``chatlists.joinChatlistInvite``, one
    already imported gets its missing chats through ``chatlists.joinChatlistUpdates``. A chat
    the folder no longer lists is ``unavailable``; one the account is already in is ``joined``.
    """
    slug = parent.addlist_slug
    assert slug is not None  # _route_folder only names folders with a slug
    account = session.account
    try:
        answer = await client(functions.chatlists.CheckChatlistInviteRequest(slug=slug))
    except errors.FloodError:
        raise
    except errors.UnauthorizedError as exc:
        tg.reraise_unauthorized(exc, account)
    except errors.RPCError as exc:
        for child in children:
            note = f"its shared folder t.me/addlist/{slug} is refused: {exc}"
            _refuse_candidate(rdb, session, child, "unavailable", note, report)
        return
    entities = {dialogs.peer_id(e): e for e in (*answer.chats, *answer.users)}
    filter_id: int | None = None
    already: set[int] = set()
    if isinstance(answer, types.chatlists.ChatlistInviteAlready):
        filter_id = answer.filter_id
        already = {int(utils.get_peer_id(p)) for p in answer.already_peers}
        offered = already | {int(utils.get_peer_id(p)) for p in answer.missing_peers}
    else:
        offered = {int(utils.get_peer_id(p)) for p in answer.peers}
    to_join: list[tuple[Candidate, Any]] = []
    for child in children:
        entity = None if child.peer_id is None else entities.get(child.peer_id)
        if child.peer_id in already:
            _mark_joined(rdb, session, child, entity, report, "the account was already a member")
        elif child.peer_id not in offered or entity is None:
            note = f"the shared folder t.me/addlist/{slug} no longer lists it"
            _refuse_candidate(rdb, session, child, "unavailable", note, report)
        else:
            to_join.append((child, entity))
    if not to_join:
        return
    peers = [utils.get_input_peer(entity) for _, entity in to_join]
    request: Any = (
        functions.chatlists.JoinChatlistInviteRequest(slug=slug, peers=peers)
        if filter_id is None
        else functions.chatlists.JoinChatlistUpdatesRequest(
            chatlist=types.InputChatlistDialogFilter(filter_id=filter_id), peers=peers
        )
    )
    try:
        result = await client(request)
    except errors.FloodError:
        raise
    except errors.UnauthorizedError as exc:
        tg.reraise_unauthorized(exc, account)
    except errors.ChannelsTooMuchError:
        for child, _ in to_join:
            note = (
                f"account {account} is in as many channels and groups as Telegram allows; "
                "leave some and approve this one again"
            )
            _refuse_candidate(rdb, session, child, "failed", note, report)
        return
    except errors.RPCError as exc:
        for child, _ in to_join:
            note = f"joining through t.me/addlist/{slug} failed: {exc}"
            _refuse_candidate(rdb, session, child, "failed", note, report)
        return
    for child, entity in to_join:
        _mark_joined(rdb, session, child, _joined_entity(result, child) or entity, report)
    log.info(
        "research session %d: joined %d chat(s) of a shared folder as %s",
        session.id,
        len(to_join),
        account,
    )


def _configured(cfg: Config, account: str, candidate: Candidate) -> Source | None:
    """The chat source of ``account`` that already names ``candidate``'s chat, by id or name."""
    wanted: list[sources.Target] = []
    if candidate.peer_id is not None:
        wanted.append(sources.Target(kind="id", value=candidate.peer_id))
    if candidate.username:
        wanted.append(sources.Target(kind="username", value=candidate.username))
    for source in cfg.sources:
        if source.account != account or source.chat is None:
            continue
        try:
            target = sources.parse_target(str(source.chat))
        except sources.InvalidTarget:
            continue
        if any(sources.same_target(target, other) for other in wanted):
            return source
    return None


def _planned_source(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    candidate: Candidate,
    report: RunReport,
) -> Source | None:
    """The source a run adds for ``candidate``, or ``None`` when it adds none this time.

    Only with a live ``add_source`` grant, once the account can read the chat — a member, or a
    public chat anyone reads — and never over a chat the index holds as a Telegram Desktop
    import (:func:`grepogram.sources.imported_tag`), whose history a live source would take over.
    The source names the chat by its peer id — a member's and a public chat read without
    joining alike — so it is the chat that was approved on every later sync whatever its
    username does: a ``chat = "@name"`` source follows the handle, and whoever registers a
    freed name would be fetched by every ordinary sync after it. A public chat outside the
    account's dialogs is addressed through the access hash the probe stored
    (:func:`_address_public` seeds it for this run; :func:`_remember_read_without_joining` keeps
    it in ``peer_cache`` for every later one, fetched this run or not, and the first sync that
    reaches it stores it in ``chat_access``). A candidate no probe tied to a peer id is
    ``failed``. Comments come along for a channel and nothing else.
    """
    if candidate.source_id is not None or not authorized(rdb, candidate, "add_source"):
        return None
    if candidate.status == "pending_admission":
        return None
    if not (_is_member(candidate) or candidate.username):
        return None
    if candidate.peer_id is None:
        note = (
            "no probe tied it to a chat id, and a source by its username would follow the name "
            "to whichever chat holds it later; approve it again once it is probed"
        )
        _refuse_candidate(rdb, session, candidate, "failed", note, report)
        return None
    # an unknown type is looked up in both scopes rather than guessed
    kinds: tuple[ChatType, ...] = (
        (candidate.type,) if candidate.type is not None else ("channel", "group")
    )
    for kind in kinds:
        held = sources.imported_tag(
            conn, candidate.peer_id, scope=chat_scope(kind, session.account)
        )
        if held is not None:
            note = (
                f"the index holds this chat as {held}, a Telegram Desktop import; a live "
                f"source would take it over, so none was added — `grepogram sources rm "
                f"{held}` first"
            )
            _refuse_candidate(rdb, session, candidate, "failed", note, report)
            return None
    # the very peer that was probed and approved, never a handle that may move to another chat
    return Source(
        chat=candidate.peer_id,
        since=horizon(session),
        comments=candidate.type == "channel",
        account=session.account,
    )


async def _address_public(
    client: Any,
    rdb: sqlite3.Connection,
    session: ResearchSession,
    work: Sequence[Candidate],
    report: RunReport,
) -> bool:
    """Make every public chat this run reads without joining addressable by its id; ``True``
    when a flood wait stopped the run.

    Such a chat's source names it by its peer id (:func:`_planned_source`) and the account has
    no dialog for it, so the sync reaches it only through an access hash its client already
    holds. The one the probe stored for this account is handed to the client's session
    (:func:`grepogram.sources.seed_peers`) with no request at all; :func:`_add_sources` keeps
    it in ``peer_cache`` (:func:`_remember_read_without_joining`) and the sync in
    ``chat_access``, and every later sync seeds from both. Only a candidate probed without
    one has its username resolved, and that answer counts only while the name still names the
    probed peer: one that moved since makes the candidate ``unavailable`` and voids its grants
    — the chat approved is not the one the name leads to now.
    """
    for candidate in work:
        current = _fresh(rdb, candidate)
        if current.peer_id is None or _is_member(current):
            continue
        if current.status == "pending_admission" or current.status not in _ACTIONABLE:
            continue
        if current.source_id is None and not authorized(rdb, current, "add_source"):
            continue
        if current.source_id is not None and not authorized(rdb, current, "fetch"):
            continue
        if current.access_hash is not None:
            sources.seed_peers(client, [(current.peer_id, current.access_hash)])
            continue
        if not current.username:
            continue
        try:
            entity = await _resolve_approved(client, current)
        except errors.FloodError as exc:
            _flood_note(report, exc, "checking the chats to read")
            return True
        except errors.UnauthorizedError as exc:
            tg.reraise_unauthorized(exc, session.account)
        except _OtherChat as exc:
            note = f"{exc}; nothing was added or fetched"
            _refuse_candidate(rdb, session, current, "unavailable", note, report)
            continue
        except (ValueError, errors.RPCError) as exc:
            note = f"its username no longer resolves: {exc}"
            _refuse_candidate(rdb, session, current, "unavailable", note, report)
            continue
        found = getattr(entity, "access_hash", None)
        if found is not None:
            research_db.update_candidate(rdb, current.id, access_hash=int(found))
    return False


def _add_sources(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    paths: Paths,
    session: ResearchSession,
    work: Sequence[Candidate],
    report: RunReport,
) -> None:
    """Add the approved sources in one config write, under the :class:`~grepogram.sync.SyncLock`
    and then the :class:`~grepogram.config.ConfigLock`, and record on each candidate the source
    it is fetched through — a chat source of the account that already names it is reused rather
    than doubled. No Telegram request runs under either lock.

    The approval is checked again under both locks, on the config as it is by then: a session
    ``accounts rm`` (or a stop) ended while the run was planning — the account's
    ``[[accounts]]`` entry gone with it — adds nothing, writes no access hash for the forgotten
    account, and its candidates say why (:func:`_lapsed`). Only the sources actually added have
    their access hash kept (:func:`_remember_read_without_joining`), before the config is saved.
    :class:`~grepogram.sync.SyncInProgress` propagates."""
    planned: list[tuple[Candidate, Source]] = []
    for candidate in work:
        current = _fresh(rdb, candidate)
        source = _planned_source(rdb, conn, session, current, report)
        if source is not None:
            planned.append((current, source))
    if not planned:
        return
    chosen: dict[int, tuple[str, bool]] = {}
    lapsed: list[Candidate] = []
    with sync.SyncLock(paths), config.ConfigLock(paths):
        cfg = config.load(paths)
        known = cfg.account_names()
        adding: list[Candidate] = []
        for candidate, source in planned:
            if source.account not in known or not authorized(rdb, candidate, "add_source"):
                lapsed.append(candidate)
                continue
            existing = _configured(cfg, source.account, candidate)
            if existing is not None:
                chosen[candidate.id] = (existing.id, False)
                continue
            cfg = sources.with_source(cfg, source, None)
            chosen[candidate.id] = (source.id, True)
            adding.append(candidate)
        if adding:
            _remember_read_without_joining(conn, session, adding)
            config.save(cfg, paths)
    with db.transaction(rdb):
        for candidate_id, (source_id, added) in chosen.items():
            research_db.update_candidate(rdb, candidate_id, source_id=source_id)
            if added:
                report.sources_added.append(candidate_id)
    _lapsed(rdb, session, lapsed, report)
    log.info("research session %d: %d source(s) added", session.id, len(report.sources_added))


def _lapsed(
    rdb: sqlite3.Connection,
    session: ResearchSession,
    candidates: Sequence[Candidate],
    report: RunReport,
) -> None:
    """Note on each of ``candidates`` that its approval ended before its source was written —
    the session was stopped, its account removed, or the candidate skipped or excluded since
    the run planned it — and void what is left of its grants. Its status stays what it was:
    nothing was done to the chat, and a skip or an exclusion keeps its own note."""
    note = (
        "its approval ended before its source was saved (session stopped or account removed); "
        "nothing was added"
    )
    noted: list[int] = []
    with db.transaction(rdb):
        for candidate in candidates:
            research_db.void_grants(rdb, session.id, candidate_ids=[candidate.id])
            current = research_db.get_candidate(rdb, candidate.id)
            # a skip or an exclusion since is a human's decision and already says why
            if current is not None and current.status not in ("skipped", "excluded"):
                research_db.update_candidate(rdb, candidate.id, note=note)
                noted.append(candidate.id)
    report.warnings.extend(f"candidate {candidate_id}: {note}" for candidate_id in noted)


def _remember_read_without_joining(
    conn: sqlite3.Connection, session: ResearchSession, planned: Sequence[Candidate]
) -> None:
    """Keep in ``peer_cache``, under the session's account, the access hash of every public chat
    among ``planned`` that the account reads without joining, before its source is written.

    Such a source names the chat by peer id (:func:`_planned_source`) and the account has no
    dialog for it, so a fresh client reaches it only through an access hash its session was
    handed. :func:`_address_public` hands the probe's to this run's client alone, and the sync
    stores it in ``chat_access`` only once it resolves the source — a run that adds the source
    and fetches nothing (an ``add_source`` approval alone, or a fetch the budget, a busy sync or
    a flood wait deferred to a session that is then stopped) would leave every later sync
    unable to address the chat at all. :func:`grepogram.sources.resolve_sources` seeds these
    (:func:`grepogram.db.cached_peers`), so the source syncs from then on whatever the run did.
    Written under the locks and before the config, for the sources about to be added only, so
    a failed config write leaves one spare hash of a live account rather than a source nothing
    can address. The hash is the account's own: the probe ran as it."""
    peers = [
        (candidate.peer_id, candidate.username, candidate.access_hash)
        for candidate in planned
        if not _is_member(candidate)
        and candidate.peer_id is not None
        and candidate.access_hash is not None
    ]
    if peers:
        db.remember_peers(conn, session.account, peers, research_db.clock())


def _to_fetch(
    rdb: sqlite3.Connection,
    session: ResearchSession,
    cfg: Config,
    work: Sequence[Candidate],
    report: RunReport,
) -> list[Candidate]:
    """The candidates this run fetches: a live ``fetch`` grant and a source that is still
    configured. A source someone removed since is never added back — removing it was a
    decision — and the candidate's grants are voided with a note saying so."""
    configured = {source.id for source in cfg.sources}
    fetching: list[Candidate] = []
    for candidate in work:
        current = _fresh(rdb, candidate)
        if current.source_id is None or current.status not in _ACTIONABLE:
            continue
        if current.status == "pending_admission" or not authorized(rdb, current, "fetch"):
            continue
        if current.source_id not in configured:
            note = f"its source {current.source_id} was removed from the config; not fetched"
            with db.transaction(rdb):
                research_db.update_candidate(rdb, current.id, note=note)
                research_db.void_grants(rdb, session.id, candidate_ids=[current.id])
            report.warnings.append(f"candidate {current.id}: {note}")
            continue
        fetching.append(current)
    return fetching


def _fetched_chat(conn: sqlite3.Connection, candidate: Candidate) -> ChatRow | None:
    """The index row of the chat ``candidate``'s source covers: the approved peer when its id is
    known — a row of any other peer is not it, whatever its username — else the row its
    username names, or the source's only row."""
    assert candidate.source_id is not None
    rows = [db.get_chat(conn, chat_id) for chat_id in db.source_chat_ids(conn, candidate.source_id)]
    stored = [row for row in rows if row is not None]
    if candidate.peer_id is not None:
        return next((row for row in stored if row.peer_id == candidate.peer_id), None)
    for row in stored:
        if candidate.username and (row.username or "").lower() == candidate.username.lower():
            return row
    return stored[0] if len(stored) == 1 else None


def _settle_fetches(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    fetching: Sequence[Candidate],
    synced: SyncReport,
    report: RunReport,
    stamp: int,
) -> list[ChatKey]:
    """Record what the sync did for each fetched candidate and register every chat it stored
    into for discovery one hop deeper — the chat and a channel's discussion group, at the
    candidate's depth (:func:`scan_targets`), named by ``(scope, peer_id)``. Returns the chats
    registered."""
    index = db.index_id(conn)
    registered: list[ChatKey] = []
    for candidate in fetching:
        chat = _fetched_chat(conn, candidate)
        if chat is None:
            note = "its source did not resolve to the chat this run"
            research_db.update_candidate(rdb, candidate.id, note=note)
            report.warnings.append(f"candidate {candidate.id}: {note}")
            continue
        if chat.id in synced.unavailable:
            note = "Telegram refused its history to the account"
            _refuse_candidate(rdb, session, candidate, "unavailable", note, report)
            continue
        attempted = chat.id in synced.chats_done or chat.id in synced.chats_remaining
        if attempted:
            chats = [chat]
            discussion = db.get_discussion_chat(conn, chat.id)
            if discussion is not None:
                chats.append(discussion)
            for row in chats:
                research_db.set_scan_cursor(
                    rdb, session.id, chat_key(row), depth=candidate.depth, index_id=index, now=stamp
                )
                registered.append(chat_key(row))
        if chat.id in synced.chats_done:
            research_db.update_candidate(rdb, candidate.id, status="fetched", note=None)
            report.fetched.append(candidate.id)
        elif attempted:
            research_db.update_candidate(rdb, candidate.id, note=PARTIAL_NOTE)
            report.partial.append(candidate.id)
    return registered


def _done(candidate: Candidate, action: str) -> bool:
    if action == "join":
        return _is_member(candidate)
    if action == "request":
        return _is_member(candidate) or candidate.status == "pending_admission"
    if action == "add_source":
        return candidate.source_id is not None
    return candidate.status == "fetched"


def _consume_done(
    rdb: sqlite3.Connection, session: ResearchSession, work: Sequence[Candidate], stamp: int
) -> int:
    """Consume every live grant whose actions are all carried out; the rest stay live for the
    next run, which nobody has to approve again. Returns how many candidates still hold one."""
    waiting = 0
    for candidate in work:
        current = _fresh(rdb, candidate)
        live = research_db.live_grants(rdb, session.id, current.id)
        for grant in live:
            if all(_done(current, action) for action in grant.actions):
                research_db.consume_grant(rdb, grant.id, now=stamp)
        if research_db.live_grants(rdb, session.id, current.id):
            waiting += 1
    return waiting


def _record_progress(
    rdb: sqlite3.Connection, session: ResearchSession, report: RunReport, stamp: int
) -> None:
    """Count the run on the session — ``runs``, ``messages`` — and keep what it did as
    ``last_run``: the report without its session id and the pinned-post and discovery passes,
    whose new candidates it keeps."""
    stored = research_db.get_session(rdb, session.id)
    progress: dict[str, Any] = dict((stored or session).progress)
    last_run = {
        name: value
        for name, value in dataclasses.asdict(report).items()
        if name not in ("session_id", "pins", "discovery")
    }
    last_run["new_candidates"] = [] if report.discovery is None else report.discovery.new_candidates
    progress.update(
        runs=progress.get("runs", 0) + 1,
        messages=progress.get("messages", 0) + report.messages,
        last_run_at=stamp,
        last_run=last_run,
    )
    research_db.set_session_progress(rdb, session.id, progress)


async def run(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    paths: Paths,
    clients: Mapping[str, Any],
    session_id: int,
    budget: sync.SyncBudget | None = None,
    *,
    embedder: Embedder | None = None,
    now: int | None = None,
) -> RunReport:
    """Carry out what a human approved for a session, within its budgets, and look one hop
    further from what it fetched.

    In order: the admission requests still waiting are asked about again (an admitted chat is
    ``joined``, one no admin answered within ``admission_timeout_days`` is ``failed``); every
    candidate with a live grant is joined or asked to join exactly as
    approved — the chats of one shared folder in one request naming only them — each outward
    step behind :func:`authorized` for that very candidate and action; the approved sources are
    added to the config (account, ``since`` = :func:`horizon`, comments for a channel) in one
    locked write; those chats — and nothing else — are synced through
    :func:`grepogram.sync.sync_all` with ``only``, under ``budget`` (the session's
    ``run_budget_s`` and ``max_messages_per_run`` by default); and every chat the sync stored
    into is registered for discovery — its pinned posts read (:func:`read_pins`), its messages
    read on a worker thread — which runs over it at depth + 1 and only ever *proposes* what it
    finds. Nothing discovered inside an approved chat is acted on.

    Telegram's answers are recorded as they are: already a member is ``joined``, an admission
    request is ``pending_admission`` (asked about again next run), a chat that refuses the
    account is ``unavailable``, and an account at its channel limit is ``failed`` until
    approved again. A flood wait ends the run's Telegram work; a clock or message cap that runs
    out leaves the fetch resumable. Grants whose actions are all done are consumed and the rest
    stay for the next run, which is resumable from ``research.db`` alone; progress is recorded
    on the session. A stopped session refuses to run (:class:`SessionStopped`), and every source
    a run added stays when the session stops. A session whose account is signed in as another
    Telegram user than the index recorded (:func:`grepogram.sync.check_account`) refuses to run
    with :class:`~grepogram.tg.OtherUser` before anything is sent.
    """
    require_enabled(cfg)
    session = active_session(rdb, session_id)
    client = clients.get(session.account)
    if client is None:
        raise ResearchError(
            f"account {session.account} is not signed in for this run",
            tg.auth_hint(session.account),
        )
    # a run joins, asks and fetches as this account: never as a Telegram user no approval named
    await sync.check_account(conn, session.account, client)
    limits = session.limits
    if budget is None:
        budget = sync.SyncBudget(limits.run_budget_s, messages=limits.max_messages_per_run)
    stamp = research_db.clock(now)
    spent_before = budget.spent
    report = RunReport(session_id=session.id)
    flooded = await _recheck_admissions(client, rdb, conn, session, report, stamp)
    work = [
        view.candidate
        for view in candidate_views(rdb, conn, session, _ACTIONABLE)
        if _granted(rdb, view.candidate)
    ]
    if not flooded:
        flooded = await _join_all(client, rdb, conn, session, work, report, budget, stamp)
    if not flooded and not budget.expired:
        flooded = await _address_public(client, rdb, session, work, report)
    registered: list[ChatKey] = []
    if not flooded and not budget.expired:
        try:
            _add_sources(rdb, conn, paths, session, work, report)
            current = config.load(paths)
            fetching = _to_fetch(rdb, session, current, work, report)
            if fetching and not budget.halted:
                synced = await sync.sync_all(
                    clients,
                    conn,
                    functools.partial(config.load, paths),
                    paths,
                    budget,
                    embedder,
                    recut=False,
                    only=sorted({c.source_id for c in fetching if c.source_id is not None}),
                )
                report.warnings.extend(synced.warnings)
                registered = _settle_fetches(rdb, conn, session, fetching, synced, report, stamp)
        except sync.SyncInProgress as exc:
            report.warnings.append(f"{exc}; the approved sources wait for the next run")
            report.stopped_by = "sync_busy"
    report.messages = budget.spent - spent_before
    waiting = _consume_done(rdb, session, work, stamp)
    if report.stopped_by is None and waiting and budget.halted:
        report.stopped_by = "messages" if budget.exhausted else "time"
    if registered and not flooded and report.stopped_by != "flood":
        report.pins = await read_pins(
            client, rdb, conn, cfg, session.id, only=registered, now=stamp
        )
    if registered:
        report.discovery = await sync.joined_to_thread(
            functools.partial(discover_offline, rdb, conn, cfg, session.id, now=stamp)
        )
    _record_progress(rdb, session, report, stamp)
    log.info(
        "research session %d run: %d joined, %d waiting for admission, %d source(s) added, "
        "%d fetched, %d partly, %d message(s)%s",
        session.id,
        len(report.joined),
        len(report.pending_admission),
        len(report.sources_added),
        len(report.fetched),
        len(report.partial),
        report.messages,
        "" if report.stopped_by is None else f", stopped by {report.stopped_by}",
    )
    return report


# --- what the CLI and the MCP server answer with ---------------------------------------------

APPROVE_COMMAND = "grepogram research approve"
APPROVAL_GRAMMAR = (
    "name each target as ID:action,action (actions: join, request, fetch, add_source; a bare ID "
    "approves joining it, or asking to, and fetching it as a source; ID:fetch,add_source reads "
    "a public chat without joining) or a session action (global_search, paid_search)"
)
_STATUS_NAMES: tuple[CandidateStatus, ...] = get_args(CandidateStatus)


def parse_approval(tokens: Sequence[str]) -> list[ApprovalItem]:
    """The approval items a command line names, the inverse of :func:`approval_args`.

    ``ID:join,fetch,add_source`` names candidate ``ID`` and those actions, a bare ``ID`` the
    candidate with no action yet (:func:`with_default_actions` fills them in), and
    ``global_search`` / ``paid_search`` a session-wide action. Nothing is checked against the
    session here; :func:`approval_summary` does that.
    """
    items: list[ApprovalItem] = []
    for token in tokens:
        text = token.strip()
        head, sep, tail = text.partition(":")
        if not sep and head in _SESSION_ORDER:
            items.append(ApprovalItem(candidate_id=None, actions=(head,)))
            continue
        actions = tuple(action.strip() for action in tail.split(",") if action.strip())
        candidate_id = leads.number(head) if _is_number(head) else None
        if candidate_id is None or (sep and not actions):
            raise ResearchError(f"{token!r} is not an approval item", APPROVAL_GRAMMAR)
        items.append(ApprovalItem(candidate_id=candidate_id, actions=actions))
    if not items:
        raise ResearchError("nothing to approve", APPROVAL_GRAMMAR)
    return items


def approval_args(items: Sequence[ApprovalItem]) -> list[str]:
    """``items`` as the arguments :func:`parse_approval` reads back."""
    args: list[str] = []
    for item in items:
        if item.candidate_id is None:
            args.extend(item.actions)
        elif item.actions:
            args.append(f"{item.candidate_id}:{','.join(item.actions)}")
        else:
            args.append(str(item.candidate_id))
    return args


def approve_command(session_id: int, items: Sequence[ApprovalItem]) -> str:
    """The command a human runs in a terminal to approve exactly ``items``."""
    return " ".join([APPROVE_COMMAND, str(session_id), *approval_args(items)])


def default_actions(candidate: Candidate) -> tuple[CandidateAction, ...]:
    """What a bare candidate id approves: fetching the chat and adding it as an ongoing source,
    and — for any chat the account is not in, a public one included — the one way in it offers
    (``request`` where its admins approve who joins, ``join`` otherwise). Reading a public chat
    without joining it is a choice made explicitly, as ``ID:fetch,add_source``."""
    inside = candidate.member is True or candidate.status in ("joined", "pending_admission")
    if inside:
        return ("fetch", "add_source")
    if candidate.request_needed:
        return ("request", "fetch", "add_source")
    return ("join", "fetch", "add_source")


def with_default_actions(
    rdb: sqlite3.Connection, session_id: int, items: Sequence[ApprovalItem]
) -> list[ApprovalItem]:
    """``items`` with :func:`default_actions` given to every candidate named without actions."""
    filled: list[ApprovalItem] = []
    for item in items:
        if item.candidate_id is None or item.actions:
            filled.append(item)
            continue
        candidate = research_db.get_candidate(rdb, item.candidate_id)
        if candidate is None or candidate.session_id != session_id:
            raise UnknownCandidate(session_id, item.candidate_id)
        filled.append(dataclasses.replace(item, actions=default_actions(candidate)))
    return filled


@dataclass(frozen=True, slots=True)
class Approval:
    """An approval ready to put to a human: the items with their default actions filled in,
    the exact :func:`approval_summary` to show, and the terminal command that asks for it."""

    items: tuple[ApprovalItem, ...]
    summary: str
    command: str


def prepare_approval(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session_id: int,
    tokens: Sequence[str],
) -> Approval:
    """What both consent channels show before :func:`grant`: ``tokens`` in the approval grammar
    (:func:`parse_approval`) with :func:`with_default_actions` applied, their summary, and the
    command (:func:`approve_command`) the terminal channel asks through."""
    items = with_default_actions(rdb, session_id, parse_approval(tokens))
    summary = approval_summary(rdb, conn, cfg, session_id, items)
    return Approval(tuple(items), summary, approve_command(session_id, items))


def known_session(rdb: sqlite3.Connection, session_id: int) -> ResearchSession:
    """The session ``session_id``, active or stopped; :class:`UnknownSession` when there is none."""
    session = research_db.get_session(rdb, session_id)
    if session is None:
        raise UnknownSession(session_id)
    return session


def authorized_actions(rdb: sqlite3.Connection, candidate: Candidate) -> list[CandidateAction]:
    """The actions :func:`authorized` allows for ``candidate`` right now, in the order a run
    takes them."""
    return [action for action in _CANDIDATE_ORDER if authorized(rdb, candidate, action)]


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
