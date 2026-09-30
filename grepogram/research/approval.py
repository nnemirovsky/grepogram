"""Approval: the summaries a human approves, the grants that approval records, and the
grammar of an approval on the command line.

:func:`approval_summary` renders exactly what :func:`grant` will record for the same items, and
:func:`grant` refuses when the summary it was approved from no longer matches. :func:`skip`,
:func:`exclude`, :func:`unexclude` and :func:`stop` are the other decisions a human makes.
"""

import dataclasses
import logging
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import NoReturn

from grepogram import db, leads, research_db, sources
from grepogram.models import (
    PEOPLE_CHAT_TYPES,
    SHARED_CHAT_TYPES,
    ApprovalItem,
    Candidate,
    CandidateAction,
    CandidateStatus,
    Config,
    Grant,
    GrantAction,
    GrantChannel,
    ResearchSession,
    Source,
    WayIn,
)
from grepogram.research.collect import cached_in, chat_level
from grepogram.research.grants import (
    _CANDIDATE_ORDER,
    _SESSION_ORDER,
    SEARCH_OFF_HINT,
    _paid_ceiling,
    granted_kinds,
    search_kinds,
)
from grepogram.research.sessions import (
    ResearchError,
    UnknownCandidate,
    UnknownSession,
    _quoted,
    active_session,
    horizon,
    require_enabled,
    shown,
)

log = logging.getLogger(__name__)


_GRANTABLE: frozenset[CandidateStatus] = frozenset(
    {"proposed", "approved", "skipped", "failed", "joined", "pending_admission"}
)
"""Statuses a grant may be given in: not acted on yet, or part-way (joined, waiting for an
admin). An excluded, unavailable or fetched candidate takes none (:data:`_REFUSED`)."""


_REFUSED: dict[CandidateStatus, str] = {
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


APPROVE_COMMAND = "grepogram research approve"


APPROVAL_GRAMMAR = (
    "name each target as ID:action,action (actions: join, request, fetch, add_source; a bare ID "
    "approves joining it, or asking to, and fetching it as a source; ID:fetch,add_source reads "
    "a public chat without joining) or a session action (global_search, paid_search)"
)


# --- summaries ------------------------------------------------------------------------------------


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
) -> set[GrantAction]:
    return {
        action
        for grant in research_db.live_grants(rdb, session.id, candidate_id)
        for action in grant.actions
    }


def _way_in(rdb: sqlite3.Connection, candidate: Candidate, action: CandidateAction) -> WayIn | None:
    """How a run would take ``action`` — ``join`` or ``request`` — into ``candidate`` as things
    stand, as a grant records it (:func:`grepogram.research_db.check_way_in`): its invite link, its
    public username, the shared folder it was found in (a join only), or its id and the access hash
    its probe stored; ``None`` when there is no way in. What an approval records is what the run
    takes (:func:`~grepogram.research.joining._granted_way_in`), whatever the candidate learns
    later.
    """
    if candidate.kind == "invite" and candidate.invite_hash:
        return WayIn("invite")
    if candidate.username:
        return WayIn("username")
    if action == "join" and candidate.parent_id is not None:
        parent = research_db.get_candidate(rdb, candidate.parent_id)
        if parent is not None and parent.kind == "addlist" and parent.addlist_slug:
            return WayIn("folder", parent.id)
    if candidate.type in SHARED_CHAT_TYPES and candidate.access_hash is not None:
        return WayIn("id")
    return None


def _way_in_folder(
    rdb: sqlite3.Connection, candidate: Candidate, way_in: WayIn
) -> Candidate | None:
    """The shared folder a ``folder`` way in names, while it is still one of the candidate's
    session with a link to join through; ``None`` for any other route."""
    if way_in.route != "folder" or way_in.folder_id is None:
        return None
    parent = research_db.get_candidate(rdb, way_in.folder_id)
    if parent is None or parent.session_id != candidate.session_id:
        return None
    if parent.kind != "addlist" or not parent.addlist_slug:
        return None
    return parent


def _way_in_words(rdb: sqlite3.Connection, candidate: Candidate, way_in: WayIn) -> str:
    """``way_in`` in the words an approval summary shows."""
    if way_in.route == "invite":
        return f"through the invite link t.me/+{shown(candidate.invite_hash or '')}"
    if way_in.route == "username":
        return f"through its public username @{shown(candidate.username or '')}"
    if way_in.route == "id":
        return "by its id"
    parent = _way_in_folder(rdb, candidate, way_in)
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
        if _way_in(rdb, candidate, "join") is None:
            refuse("there is no way to join it: no username, invite link or shared folder names it")
    if "request" in new:
        if member:
            refuse("the account is already a member; there is nothing to request")
        if waiting:
            refuse("an admission request is already waiting for the chat's admins")
        if not candidate.request_needed:
            refuse("`request` is only for a chat whose invite asks for its admins' approval")
        if _way_in(rdb, candidate, "request") is None:
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
    action: GrantAction,
    approved: Collection[GrantAction],
) -> str:
    """One action in words, as the run will carry it out; ``approved`` is every action the
    candidate holds once this approval is granted, the ones already live included."""
    account = session.account
    since = horizon(session)
    if action == "join" or action == "request":
        way_in = _way_in(rdb, c, action)
        way = "" if way_in is None else f" {_way_in_words(rdb, c, way_in)}"
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


def _session_line(cfg: Config, session: ResearchSession, action: GrantAction) -> str:
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


# --- decisions ------------------------------------------------------------------------------------


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
                joining = next((a for a in actions if a == "join" or a == "request"), None)
                way_in = (
                    None
                    if candidate is None or joining is None
                    else _way_in(rdb, candidate, joining)
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
                        join_route=way_in,
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


# --- the approval grammar -------------------------------------------------------------------------


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
