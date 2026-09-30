"""Global search: asking Telegram's own search for chats and posts under a live grant.

Its results are candidates and evidence in ``research.db`` — never ``messages`` rows
(:func:`search_telegram`). A paid post search consumes the ``paid_search`` grant it ran under.
"""

import logging
import sqlite3
from collections.abc import Collection, Sequence
from typing import Any

from telethon import errors, utils
from telethon.tl import functions, types

from grepogram import accounts, db, dialogs, research_db, tg
from grepogram.models import (
    Config,
    GlobalSearchReport,
    ResearchSession,
    SearchKind,
)
from grepogram.research.collect import chat_search_key, post_key, snippet
from grepogram.research.grants import (
    _paid_ceiling,
    _session_grants,
    granted_kinds,
    search_granted,
)
from grepogram.research.offline import room, scan_targets
from grepogram.research.probing import (
    _LEFT_OUT,
    _add_left_out,
    _AnsweredEvidence,
    _left_out_counts,
    _record_entity,
)

log = logging.getLogger(__name__)


SEARCH_LIMIT = 100
"""The most results one global search asks for; ``max_candidates`` may lower it."""


async def search_telegram(
    client: Any,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session: ResearchSession,
    text: str,
    wanted: Sequence[SearchKind],
    now: int | None,
) -> list[GlobalSearchReport]:
    """Search Telegram itself for ``text``, one report per search ``wanted``: public chats by
    name (``contacts.search``) and public channel posts (``channels.searchPosts``); a flood wait
    ends the rest. The global-search half of :func:`~grepogram.research.discovery.discover`, which
    decides what may run — the session's question alone, the one query its ``global_search`` grant's
    summary names, for the searches ``[research]`` switches on *and* that grant covers — and puts
    the client to :func:`grepogram.accounts.check_account` first.

    Every chat found becomes a candidate one hop from the question (depth 1) with its result as
    evidence — a post keeps the origin key ``post:<peer>/<msg>`` discovery gives an indexed
    copy of it. Nothing is written to ``index.db``. A post search asks
    ``channels.checkSearchPostsFlood`` first and sends ``allow_paid_stars`` only when the free
    quota is spent, ``paid_stars_max`` covers the price and a ``paid_search`` grant is live —
    consumed atomically before the request goes out, so one approval never pays twice, not even
    for two discover calls running at once; a request Telegram refuses after that leaves the
    approval spent and says so. The ``global_search`` approval is asked about again right before
    every request (:func:`_still_granted`), the quota check included and once more after it, so
    research stopped or the approval withdrawn while Telegram answered sends nothing further.
    Each search is recorded in ``research.db``, run or not."""
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
            report.flood_wait_s = accounts.flood_seconds(exc)
            stopped = accounts.flood_warning(report.flood_wait_s, "searching", "search stopped")
            report.warnings.append(f"{kind}: {stopped}")
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


def _still_granted(rdb: sqlite3.Connection, report: GlobalSearchReport) -> bool:
    """Whether the session's live ``global_search`` approval still covers ``report``'s search,
    asked right before each request goes out — after whatever was awaited first, so research
    stopped or an approval withdrawn meanwhile sends nothing more. A ``False`` answer is
    recorded in ``report`` as a warning."""
    if report.kind in granted_kinds(rdb, report.session_id):
        return True
    report.warnings.append(
        f"{report.kind}: the session's global_search approval ended before the search went out; "
        "nothing was sent"
    )
    return False


def _search_limit(session: ResearchSession) -> int:
    return max(1, min(SEARCH_LIMIT, session.limits.max_candidates))


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
    found = _AnsweredEvidence(
        report.kind, origin_key, in_itself=True, msg_id=msg_id, snippet=snippet_text
    )
    recorded = _record_entity(
        rdb,
        conn,
        session,
        reads,
        entity,
        found,
        depth=1,
        parent_id=None,
        room_left=room(rdb, session),
        facts={},
        stamp=stamp,
    )
    candidate = recorded.candidate
    if recorded.outcome in _LEFT_OUT:
        _add_left_out(report, _left_out_counts({recorded.outcome: 1}))
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
    if not _still_granted(rdb, report):
        return
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
                origin_key=chat_search_key(marked),
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
    if not _still_granted(rdb, report):
        return
    flood = await client(functions.channels.CheckSearchPostsFloodRequest(query=report.query))
    report.quota_total = flood.total_daily
    report.quota_remains = flood.remains
    report.wait_till = flood.wait_till
    # the quota check awaited Telegram: the grant is asked about again before anything is
    # paid for or searched, so research stopped meanwhile fetches no post
    if not _still_granted(rdb, report):
        return
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
                origin_key=post_key(marked, post.id),
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
