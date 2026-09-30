"""Probing: asking Telegram what a candidate is, without reading its history.

:func:`probe` answers for one candidate — by username, peer, invite or shared-folder slug — and
:func:`probe_candidates` is the bounded pass over a session's best-ranked ones.
:func:`_record_entity` is how a chat Telegram named (a folder's, a search result) becomes a
candidate with its evidence, shared with the global search.
"""

import logging
import sqlite3
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from telethon import errors, utils
from telethon.tl import functions, types

from grepogram import accounts, db, dialogs, leads, research_db, tg
from grepogram.leads import LeadTarget
from grepogram.models import (
    Candidate,
    CandidateStatus,
    ChatKey,
    Config,
    EvidenceVia,
    GlobalSearchReport,
    ProbeOutcome,
    ProbeReport,
    ProbeResult,
    ResearchSession,
    chat_scope,
)
from grepogram.research.collect import candidate_kind, candidate_views, folder_key, held_rows
from grepogram.research.offline import room, scan_targets
from grepogram.research.sessions import (
    UnknownSession,
    _quoted,
    active_session,
    require_enabled,
    shown,
)

log = logging.getLogger(__name__)


_PROBED: tuple[CandidateStatus, ...] = ("proposed", "approved")
"""The statuses probing reads: decisions not acted on yet. A joined or fetched chat is known."""


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
    if candidate.kind == "peer":
        handle = None
    elif (handle := _handle(candidate)) is None:
        note = f"{candidate.identity!r} names no {candidate.kind} to ask Telegram about"
        stored = _settle(rdb, candidate, "unavailable", stamp, note=note)
        return ProbeOutcome(candidate=stored, result="unavailable")
    try:
        if handle is None:  # a peer: asked about by its id and a stored access hash
            return await _probe_peer(client, rdb, conn, session, candidate, stamp)
        if candidate.kind == "username":
            return await _probe_username(client, rdb, candidate, handle, stamp)
        if candidate.kind == "invite":
            return await _probe_invite(client, rdb, candidate, handle, stamp)
        return await _probe_addlist(client, rdb, conn, session, candidate, handle, stamp)
    except errors.FloodError:
        raise
    except errors.UnauthorizedError as exc:
        tg.reraise_unauthorized(exc, session.account)
    except errors.RPCError as exc:
        refused = _settle(rdb, candidate, "unavailable", stamp, note=f"Telegram refused: {exc}")
        return ProbeOutcome(candidate=refused, result="unavailable")


def _handle(candidate: Candidate) -> str | None:
    """What a probe asks Telegram about ``candidate`` by: the username, invite hash or folder
    slug its row holds, else the one its identity spells — read back through
    :func:`grepogram.leads.normalize`, which every identity research stores comes from."""
    spelled = leads.normalize(candidate.identity)
    if candidate.kind == "username":
        return candidate.username or (None if spelled is None else spelled.username)
    if candidate.kind == "invite":
        return candidate.invite_hash or (None if spelled is None else spelled.invite_hash)
    if candidate.kind == "addlist":
        return candidate.addlist_slug or (None if spelled is None else spelled.slug)
    return None


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
    client: Any, rdb: sqlite3.Connection, candidate: Candidate, name: str, stamp: int
) -> ProbeOutcome:
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
    client: Any, rdb: sqlite3.Connection, candidate: Candidate, invite_hash: str, stamp: int
) -> ProbeOutcome:
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
    slug: str,
    stamp: int,
) -> ProbeOutcome:
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
    left_out: Counter[EntityOutcome] = Counter()
    found = _AnsweredEvidence(
        "shared_folder", folder_key(slug), in_itself=False, msg_id=None, snippet=title
    )
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
                found,
                depth=candidate.depth,
                parent_id=candidate.id,
                room_left=allowance - len(children),
                facts={"member": joined[marked]} if marked in joined else {},
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
        **_left_out_counts(left_out),
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
    candidates not reached wait for the next call. The probing half of
    :func:`~grepogram.research.discovery.discover`, which has put the client to
    :func:`grepogram.accounts.check_account` first.
    """
    require_enabled(cfg)
    session = active_session(rdb, session_id)
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
            report.flood_wait_s = accounts.flood_seconds(exc)
            report.warnings.append(
                accounts.flood_warning(report.flood_wait_s, "probing again", "probing stopped")
            )
            break
        {
            "probed": report.probed,
            "unavailable": report.unavailable,
            "unresolvable": report.unresolvable,
        }[outcome.result].append(outcome.candidate.id)
        report.children.extend(outcome.children)
        _add_left_out(report, {name: getattr(outcome, name) for name in _LEFT_OUT.values()})
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


# --- recording a chat Telegram named --------------------------------------------------------------


EntityOutcome = Literal["person", "in_session", "unnamed", "over_cap", "excluded", "new", "known"]
"""What :func:`_record_entity` did with one chat Telegram answered with: left it out — a person,
a chat the session already reads, one nothing names, one over the candidate cap, an excluded
one — or recorded it as a ``new`` candidate or evidence of a ``known`` one."""


_LEFT_OUT: dict[EntityOutcome, str] = {
    "person": "people",
    "excluded": "excluded",
    "in_session": "in_session",
    "over_cap": "over_cap",
}
"""The outcomes of :func:`_record_entity` that leave a chat out, by the count every report of
chats Telegram answered with keeps of them (:class:`~grepogram.models.ProbeOutcome`,
:class:`~grepogram.models.ProbeReport`, :class:`~grepogram.models.GlobalSearchReport`)."""


def _left_out_counts(outcomes: Mapping[EntityOutcome, int]) -> dict[str, int]:
    """``outcomes`` as the ``people`` / ``excluded`` / ``in_session`` / ``over_cap`` fields of a
    report (:data:`_LEFT_OUT`)."""
    return {name: outcomes.get(outcome, 0) for outcome, name in _LEFT_OUT.items()}


def _add_left_out(report: ProbeReport | GlobalSearchReport, counts: Mapping[str, int]) -> None:
    """Add ``counts`` (:func:`_left_out_counts`) to ``report``'s own."""
    for name, count in counts.items():
        setattr(report, name, getattr(report, name) + count)


@dataclass(frozen=True, slots=True)
class _AnsweredEvidence:
    """How a chat Telegram answered with was found — the evidence :func:`_record_entity` keeps:
    ``via`` which path, under ``origin_key``, ``in_itself`` when it was found in the chat itself
    (a search result: then ``msg_id`` is the post), with a ``snippet`` of what was seen."""

    via: EvidenceVia
    origin_key: str
    in_itself: bool
    msg_id: int | None
    snippet: str | None


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
    found: _AnsweredEvidence,
    *,
    depth: int,
    parent_id: int | None,
    room_left: int,
    facts: Mapping[str, Any],
    stamp: int,
) -> _Recorded:
    """Record one chat Telegram answered with — a chat of a shared folder, a global search's
    result — as a candidate probed on the spot, with its evidence, inside the caller's
    ``research.db`` transaction.

    A person (a user or bot), a chat the session already reads (``reads``, index rows) and an
    excluded chat are left out, and so is a new one once ``room_left`` is spent. What Telegram
    said about the chat is stored as probed facts, ``facts`` on top (a folder knows whether the
    account joined it); the candidate is named by its ``@username`` or else its marked id
    (:func:`entity_target`), ``depth`` hops from the question and inside ``parent_id``, with
    ``found`` as its evidence — found in the chat itself, for a search result, or nowhere
    indexed.

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
    in_chat = ChatKey(chat_scope(known["type"], session.account), marked)
    added = research_db.add_evidence(
        rdb,
        candidate.id,
        found.via,
        found.origin_key,
        chat=in_chat if found.in_itself else None,
        msg_id=found.msg_id,
        snippet=found.snippet,
        now=stamp,
    )
    return _Recorded("new" if existing is None else "known", candidate, added)
