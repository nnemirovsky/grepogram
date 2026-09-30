"""The run: carrying out what a human approved and nothing else (:func:`run`).

Joins go through :mod:`grepogram.research.joining`; here the approved sources are added, the
approved chats fetched through the ordinary :func:`grepogram.sync.sync_all`, and the chats the
run stored into read by discovery one hop deeper.
"""

import dataclasses
import functools
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from telethon import errors

from grepogram import accounts, config, db, research_db, sources, sync, tg
from grepogram.embed import Embedder
from grepogram.models import (
    CachedPeer,
    Candidate,
    CandidateStatus,
    ChatKey,
    ChatRow,
    ChatType,
    Config,
    GrantAction,
    ResearchSession,
    RunReport,
    Source,
    SyncReport,
    chat_scope,
)
from grepogram.paths import Paths
from grepogram.research.approval import _configured
from grepogram.research.collect import candidate_views, chat_key
from grepogram.research.grants import _CANDIDATE_ORDER, authorized
from grepogram.research.joining import (
    _flood_note,
    _fresh,
    _is_member,
    _join_all,
    _OtherChat,
    _recheck_admissions,
    _refuse_candidate,
    _resolve_approved,
)
from grepogram.research.offline import discover_offline
from grepogram.research.pins import read_pins
from grepogram.research.sessions import ResearchError, active_session, horizon, require_enabled

log = logging.getLogger(__name__)


_ACTIONABLE: tuple[CandidateStatus, ...] = ("approved", "joined", "pending_admission")
"""Statuses a run acts on: approved and not acted on yet, joined but not fetched, or waiting
for an admission. The rest are decided (skipped, excluded), done (fetched) or refused."""


PARTIAL_NOTE = "part of its history is fetched; the next run goes on from there"


def _granted(rdb: sqlite3.Connection, candidate: Candidate) -> bool:
    return any(authorized(rdb, candidate, action) for action in _CANDIDATE_ORDER)


# --- sources --------------------------------------------------------------------------------------


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
        CachedPeer(candidate.peer_id, candidate.username, candidate.access_hash)
        for candidate in planned
        if not _is_member(candidate)
        and candidate.peer_id is not None
        and candidate.access_hash is not None
    ]
    if peers:
        db.remember_peers(conn, session.account, peers, research_db.clock())


# --- fetches --------------------------------------------------------------------------------------


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
    candidate's depth (:func:`~grepogram.research.offline.scan_targets`), named by ``(scope,
    peer_id)``. Returns the chats registered."""
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


def _done(candidate: Candidate, action: GrantAction) -> bool:
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


# --- the run --------------------------------------------------------------------------------------


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
    request is ``pending_admission`` (asked about again next run), a chat that refuses the account
    is ``unavailable``, and an account at its channel limit is ``failed`` until approved again. A
    flood wait ends the run's Telegram work; a clock or message cap that runs out leaves the fetch
    resumable. Grants whose actions are all done are consumed and the rest stay for the next run,
    which is resumable from ``research.db`` alone; progress is recorded on the session. A stopped
    session refuses to run (:class:`~grepogram.research.sessions.SessionStopped`), and every source
    a run added stays when the session stops. A session whose account is signed in as another
    Telegram user than the index recorded (:func:`grepogram.accounts.check_account`) refuses to run
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
    await accounts.check_account(conn, session.account, client)
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
