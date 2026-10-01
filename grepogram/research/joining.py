"""Joining: the joins, admission requests and shared-folder joins a run carries out, each
for a candidate a human authorized that very action for."""

import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from telethon import errors, utils
from telethon.tl import functions, types

from grepogram import accounts, db, dialogs, research_db, sync, tg
from grepogram.models import (
    Candidate,
    CandidateAction,
    CandidateStatus,
    ResearchSession,
    RunReport,
    WayIn,
)
from grepogram.research.approval import _way_in_folder
from grepogram.research.grants import authorized
from grepogram.research.probing import entity_facts, probe
from grepogram.research.sessions import _quoted

log = logging.getLogger(__name__)


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

ALREADY_NOTE = "the account was already a member"


def _fresh(rdb: sqlite3.Connection, candidate: Candidate) -> Candidate:
    current = research_db.get_candidate(rdb, candidate.id)
    assert current is not None  # candidates are never deleted while their session exists
    return current


def _is_member(candidate: Candidate) -> bool:
    return candidate.member is True or candidate.status in ("joined", "fetched")


def _flood_note(report: RunReport, exc: errors.FloodError, what: str) -> None:
    report.warnings.append(
        accounts.flood_warning(accounts.flood_seconds(exc), what, "the run stopped there")
    )
    report.stopped_by = "flood"


def _withdrawn(report: RunReport, candidate: Candidate, what: str) -> None:
    """Say that ``candidate``'s ``what`` was never sent: between the check that planned it and
    the request, its approval was withdrawn or the session stopped (:func:`authorized` is asked
    again right before every request that changes membership, after whatever was awaited
    first). The candidate is left as it stands — nothing happened on Telegram."""
    report.warnings.append(
        f"candidate {candidate.id}: its approval ended before the {what} went out; nothing was sent"
    )
    log.info(
        "research candidate %d: approval ended before the %s; nothing sent", candidate.id, what
    )


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


def _approved_entry(rdb: sqlite3.Connection, candidate: Candidate) -> CandidateAction | None:
    """The approved action into ``candidate`` — ``join`` or ``request`` — this run still has
    to take, if any."""
    if candidate.status == "pending_admission" or _is_member(candidate):
        return None
    if authorized(rdb, candidate, "join"):
        return "join"
    if authorized(rdb, candidate, "request"):
        return "request"
    return None


def _granted_way_in(
    rdb: sqlite3.Connection, candidate: Candidate, action: CandidateAction
) -> WayIn | None:
    """The way in the live grant approving ``action`` on ``candidate`` recorded — the newest
    one, should several — or ``None`` when none did (a grant from before routes were
    recorded)."""
    for grant in reversed(research_db.live_grants(rdb, candidate.session_id, candidate.id)):
        if action in grant.actions:
            return grant.join_route
    return None


def _way_in_blocked(
    rdb: sqlite3.Connection, candidate: Candidate, way_in: WayIn | None
) -> str | None:
    """Why the recorded ``way_in`` cannot be taken into ``candidate`` now, or ``None`` when it
    can; a run never swaps it for another way in, since the approval showed this one."""
    if way_in is None:
        return "its approval names no way in"
    if way_in.route == "invite":
        return None if candidate.kind == "invite" and candidate.invite_hash else "no invite link"
    if way_in.route == "username":
        known = candidate.username or candidate.access_hash is not None
        return None if known else "its username is gone"
    if way_in.route == "id":
        known = candidate.peer_id is not None and candidate.access_hash is not None
        return None if known else "no access hash addresses it"
    if _way_in_folder(rdb, candidate, way_in) is None:
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
        action = _approved_entry(rdb, candidate)
        if action is None:
            continue
        current = _fresh(rdb, candidate)
        way_in = _granted_way_in(rdb, current, action)
        blocked = _way_in_blocked(rdb, current, way_in)
        if blocked is not None:
            note = f"{blocked}; nothing was sent — approve it again to see how it would go in now"
            _refuse_candidate(rdb, session, current, "failed", note, report)
            continue
        assert way_in is not None  # _way_in_blocked refuses a missing one
        parent = _way_in_folder(rdb, current, way_in) if action == "join" else None
        if parent is not None:
            folders.setdefault(parent.id, (parent, []))[1].append(current)
            continue
        if budget.expired:
            return False
        try:
            await _join_one(client, rdb, conn, session, current, action, way_in, report, stamp)
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


async def _already_through_invite(
    client: Any,
    rdb: sqlite3.Connection,
    session: ResearchSession,
    candidate: Candidate,
    report: RunReport,
) -> None:
    """Settle an invite join Telegram answered with ``USER_ALREADY_PARTICIPANT``.

    That answer only says the account is in the chat the invite leads to *now*, which need not
    be the chat probed and approved: so the invite is checked again (read-only), and its
    ``ChatInviteAlready`` (or a preview's) chat goes through :func:`_mark_joined`, which takes
    it for the candidate only when it is that very chat and fails the candidate otherwise. A
    check that names no chat, or fails, fails the candidate as well — nothing is fetched or
    added for a chat no one can tell apart. A flood wait propagates, as on the join itself.

    The ``username`` and ``id`` routes need none of this: their join names the probed peer
    itself (:func:`_input_channel`), so Telegram's answer is about that chat.
    """
    assert candidate.invite_hash is not None
    try:
        answer = await client(functions.messages.CheckChatInviteRequest(hash=candidate.invite_hash))
    except errors.FloodError:
        raise
    except errors.UnauthorizedError as exc:
        tg.reraise_unauthorized(exc, session.account)
    except errors.RPCError as exc:
        note = (
            "Telegram says the account is already in the chat the invite leads to, but checking "
            f"the invite again failed: {exc}; nothing was fetched or added"
        )
        _refuse_candidate(rdb, session, candidate, "failed", note, report)
        return
    entity = getattr(answer, "chat", None)
    if entity is None:
        note = (
            "Telegram says the account is already in the chat the invite leads to, but the "
            "invite no longer says which chat that is; nothing was fetched or added — check "
            f"which chats account {session.account} is in now"
        )
        _refuse_candidate(rdb, session, candidate, "failed", note, report)
        return
    _mark_joined(rdb, session, candidate, entity, report, ALREADY_NOTE)


async def _join_one(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    candidate: Candidate,
    action: CandidateAction,
    way_in: WayIn,
    report: RunReport,
    stamp: int,
) -> None:
    """Take ``action`` — ``join`` or ``request``, authorized by :func:`_approved_entry` just
    before — into one chat, by the ``way_in`` its grant recorded (:func:`_granted_way_in`).

    An ``invite`` route goes through ``messages.importChatInvite``, a ``username`` or ``id``
    one through ``channels.joinChannel`` on the probed peer (:func:`_input_channel`); for a chat
    whose admins approve joins either one sends the admission request, which is what a
    ``request`` grant approved.
    """
    account = session.account
    try:
        if way_in.route == "invite" and candidate.invite_hash:
            request: Any = functions.messages.ImportChatInviteRequest(hash=candidate.invite_hash)
        else:
            request = functions.channels.JoinChannelRequest(
                channel=await _input_channel(client, candidate)
            )
        # resolving the username awaited Telegram: the approval is asked about again right
        # before the join, so a stop or a withdrawal meanwhile sends nothing
        if not authorized(rdb, candidate, action):
            _withdrawn(report, candidate, action)
            return
        answer = await client(request)
    except errors.FloodError:
        raise
    except errors.UnauthorizedError as exc:
        tg.reraise_unauthorized(exc, account)
    except errors.UserAlreadyParticipantError:
        if way_in.route == "invite" and candidate.invite_hash:
            await _already_through_invite(client, rdb, session, candidate, report)
            return
        joined = _mark_joined(rdb, session, candidate, None, report, ALREADY_NOTE)
        if joined is not None and joined.peer_id is None:
            await probe(client, rdb, conn, joined, now=stamp)
        return
    except errors.InviteRequestSentError:
        note = PENDING_NOTE
        if action == "join":
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


def _still_approved(
    rdb: sqlite3.Connection, children: Sequence[Candidate], report: RunReport
) -> list[Candidate]:
    """The ``children`` a live grant still approves joining, now; each of the others is
    reported as :func:`_withdrawn`."""
    kept: list[Candidate] = []
    for child in children:
        if authorized(rdb, child, "join"):
            kept.append(child)
        else:
            _withdrawn(report, child, "join")
    return kept


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
    Every child's approval is asked about again (:func:`authorized`) before the check and once
    more after it, right before the join: one withdrawn — or the session stopped — while
    Telegram answered is left out of the request, which is not sent at all when none is left.
    """
    slug = parent.addlist_slug
    assert slug is not None  # _way_in_folder only names folders with a slug
    account = session.account
    children = _still_approved(rdb, children, report)
    if not children:
        return
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
            _mark_joined(rdb, session, child, entity, report, ALREADY_NOTE)
        elif child.peer_id not in offered or entity is None:
            note = f"the shared folder t.me/addlist/{slug} no longer lists it"
            _refuse_candidate(rdb, session, child, "unavailable", note, report)
        else:
            to_join.append((child, entity))
    # the folder check awaited Telegram: a stop or a withdrawn approval meanwhile keeps that
    # chat out of the join, and a join naming none of them is never sent
    approved = {child.id for child in _still_approved(rdb, [c for c, _ in to_join], report)}
    to_join = [(child, entity) for child, entity in to_join if child.id in approved]
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
