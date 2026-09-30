"""Pinned posts: reading, once each, the pinned posts of the chats a session reads.

Their leads become evidence (``via = pinned``) in ``research.db`` only; the posts are never
stored as messages and no sync cursor moves (:func:`read_pins`).
"""

import logging
import sqlite3
from collections.abc import Collection, Mapping
from typing import Any

from telethon import errors
from telethon.tl import types

from grepogram import accounts, db, research_db, sync, tg
from grepogram.models import (
    ChatKey,
    ChatRow,
    Config,
    PinReport,
    ResearchSession,
)
from grepogram.research.collect import Lead, LeadScan, _own, chat_key, message_leads
from grepogram.research.offline import ScanTarget, _directories, _propose, scan_targets
from grepogram.research.sessions import active_session, require_enabled

log = logging.getLogger(__name__)


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
        await accounts.warm_peer_cache(
            client, [target.chat for target in asked], conn, session.account
        )
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
            report.flood_wait_s = accounts.flood_seconds(exc)
            report.warnings.append(
                accounts.flood_warning(report.flood_wait_s, "reading more pinned posts", "stopped")
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
