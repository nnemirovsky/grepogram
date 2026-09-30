"""The whole discover call: offline discovery, then — with a client — pinned posts, probing
and the global search the session is granted (:func:`discover`)."""

import dataclasses
import functools
import sqlite3
from typing import Any

from grepogram import accounts, research_db, sync
from grepogram.models import (
    Config,
    DiscoverReport,
    GlobalSearchReport,
)
from grepogram.research.grants import granted_kinds, search_kinds
from grepogram.research.offline import discover_offline
from grepogram.research.pins import read_pins
from grepogram.research.probing import probe_candidates
from grepogram.research.searching import search_telegram
from grepogram.research.sessions import active_session


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
    :func:`grepogram.accounts.check_account` has made sure the client is the Telegram user the index
    recorded for the session's account (:class:`~grepogram.tg.OtherUser` otherwise).
    """
    report = await sync.joined_to_thread(
        functools.partial(discover_offline, rdb, conn, cfg, session_id, now=now)
    )
    if client is None:
        return report
    session = active_session(rdb, session_id)
    await accounts.check_account(conn, session.account, client)
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
            searches = await search_telegram(client, rdb, conn, cfg, session, question, kinds, now)
    flooded = any(search.flood_wait_s is not None for search in searches)
    probed = (
        None if flooded else await probe_candidates(client, rdb, conn, cfg, session.id, now=now)
    )
    return dataclasses.replace(report, pins=pins, probe=probed, searches=tuple(searches))
