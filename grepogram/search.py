"""Search over the index: lexical retrieval over units and messages, rank fusion and hits.

The lexical side asks FTS5 twice. ``unit_fts`` ranks whole units — windows, threads, posts —
with ``bm25(unit_fts, 2.0, 1.0)`` (the ``raw`` column weighted twice the ``stemmed`` one), and
``msg_fts`` ranks single messages the same way, so a message that packs every query term into
one line still stands out among long windows; each message hit is then mapped to the unit that
holds it (:func:`grepogram.db.containing_unit`). Both lists run the ``AND`` form of
:func:`~grepogram.stem.fts_query` first and fall back to ``OR`` when fewer rows than wanted
match, the ``AND`` rows keeping their place at the top. ``bm25()`` is negative — better is more
negative — so rows are ordered ascending and the score is negated. Chat and date filters are
plain ``AND`` predicates on the UNINDEXED columns, never part of the ``MATCH`` expression.

The lists are fused with Reciprocal Rank Fusion (:func:`rrf`), which needs no score calibration
between tables: a unit near the top of both lists outranks one found by a single list. The dense
list joins the same fusion once the vector index exists, followed by reranking and dedup; until
then ``hybrid`` and ``dense`` raise ``NotImplementedError``.

Every hit carries an *anchor*, the message its deep link opens: the matched message for a
message-level hit, and for a unit-level hit the unit's best message under the query according to
``msg_fts`` (:func:`best_anchor`), else the unit's first message. The snippet leads with the
anchor's rendered line and adds its neighbours from within the unit while the total stays under
:data:`SNIPPET_CHARS`; ``full`` adds the unit's whole text.
"""

import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass

from grepogram import db, links
from grepogram.models import Config, Filters, Hit, MessageRow, SearchResult, UnitRow
from grepogram.stem import FtsOp, fts_query
from grepogram.units import render_line

log = logging.getLogger(__name__)

SNIPPET_CHARS = 600
ELLIPSIS = "…"
MODES = ("lexical", "hybrid", "dense")
NOTHING_INDEXED = (
    "nothing is indexed yet: add a source with `grepogram sources add <target>` "
    "and run `grepogram sync`"
)
_OPS: tuple[FtsOp, ...] = ("AND", "OR")


@dataclass(frozen=True, slots=True)
class Match:
    """One retrieved unit: ``score`` is ``-bm25`` on a lexical list; ``anchor_msg_id`` is the
    matched message of a message-level hit and ``None`` when the unit matched as a whole."""

    unit_id: int
    score: float
    anchor_msg_id: int | None = None


# --- lexical lists ---------------------------------------------------------------------------


def lexical_units(conn: sqlite3.Connection, q: str, filters: Filters, limit: int) -> list[Match]:
    """Up to ``limit`` units matching ``q``, best first; ``[]`` when ``q`` has no tokens."""
    ranked = _fts_rank(conn, "unit_fts", "date_start", q, filters, limit)
    return [Match(rowid, score) for rowid, score in ranked]


def lexical_messages(conn: sqlite3.Connection, q: str, filters: Filters, limit: int) -> list[Match]:
    """Units holding the up to ``limit`` messages that match ``q`` best, each with its anchor.

    Several matching messages may sit in one window; the unit is listed once, with the
    best-ranked of them as anchor. A message not in any unit yet is skipped.
    """
    ranked = _fts_rank(conn, "msg_fts", "date", q, filters, limit)
    if not ranked:
        return []
    messages = {msg.id: msg for msg in db.get_messages_by_ids(conn, [rowid for rowid, _ in ranked])}
    found: dict[int, Match] = {}
    for rowid, score in ranked:
        msg = messages.get(rowid)
        if msg is None:
            continue
        unit = db.containing_unit(conn, msg.chat_id, msg.msg_id, msg.topic_id)
        if unit is None or unit.id is None or unit.id in found:
            continue
        found[unit.id] = Match(unit.id, score, msg.msg_id)
    return list(found.values())


def _fts_rank(
    conn: sqlite3.Connection,
    table: str,
    date_col: str,
    q: str,
    filters: Filters,
    limit: int,
) -> list[tuple[int, float]]:
    """``(rowid, -bm25)`` of the best ``limit`` rows of an FTS table under ``q``.

    The ``AND`` query goes first; when it leaves room, the ``OR`` query fills the remaining
    ranks with rows not seen yet (a row matching every term scores the same under both, so the
    ``AND`` rows keep their scores). A single-term query is not run twice.
    """
    if limit <= 0 or (filters.chat_ids is not None and not filters.chat_ids):
        return []
    ranked: dict[int, float] = {}
    tried: set[str] = set()
    for op in _OPS:
        match = fts_query(q, op)
        if match is None or match in tried:
            break
        tried.add(match)
        for rowid, score in _fts_rows(conn, table, date_col, match, filters, limit):
            ranked.setdefault(rowid, score)
        if len(ranked) >= limit:
            break
    return list(ranked.items())[:limit]


def _fts_rows(
    conn: sqlite3.Connection,
    table: str,
    date_col: str,
    match: str,
    filters: Filters,
    limit: int,
) -> list[tuple[int, float]]:
    sql = f"SELECT rowid, bm25({table}, 2.0, 1.0) AS s FROM {table} WHERE {table} MATCH ?"
    params: list[object] = [match]
    if filters.chat_ids is not None:
        chat_ids = sorted(filters.chat_ids)
        sql += f" AND chat_id IN ({', '.join('?' * len(chat_ids))})"
        params.extend(chat_ids)
    if filters.since is not None and filters.until is not None:
        sql += f" AND {date_col} BETWEEN ? AND ?"
        params.extend((filters.since, filters.until))
    elif filters.since is not None:
        sql += f" AND {date_col} >= ?"
        params.append(filters.since)
    elif filters.until is not None:
        sql += f" AND {date_col} <= ?"
        params.append(filters.until)
    sql += " ORDER BY s ASC, rowid ASC LIMIT ?"
    params.append(limit)
    return [(int(row["rowid"]), -float(row["s"])) for row in conn.execute(sql, params)]


# --- fusion ----------------------------------------------------------------------------------


def rrf(rankings: Sequence[Sequence[int]], k: int) -> dict[int, float]:
    """Reciprocal Rank Fusion: every id scores ``Σ 1 / (k + rank)`` over the lists it appears
    in, ranks starting at 1. The result keeps first-seen order, so sorting it by score leaves
    ties in the order the lists produced them."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, unit_id in enumerate(ranking, start=1):
            scores[unit_id] = scores.get(unit_id, 0.0) + 1.0 / (k + rank)
    return scores


# --- hits ------------------------------------------------------------------------------------


def unit_messages(conn: sqlite3.Connection, unit: UnitRow) -> list[MessageRow]:
    """The unit's stored messages in the order its text renders them."""
    found = db.get_messages_by_msg_id(conn, unit.chat_id, unit.msg_ids)
    return [found[msg_id] for msg_id in unit.msg_ids if msg_id in found]


def best_anchor(conn: sqlite3.Connection, unit: UnitRow, query: str) -> int:
    """The unit's message ``msg_fts`` ranks best under any term of ``query``, else its first."""
    match = fts_query(query, "OR")
    if match is not None and unit.msg_ids:
        marks = ", ".join("?" * len(unit.msg_ids))
        row = conn.execute(
            "SELECT messages.msg_id FROM msg_fts JOIN messages ON messages.id = msg_fts.rowid "
            f"WHERE msg_fts MATCH ? AND messages.chat_id = ? AND messages.msg_id IN ({marks}) "
            "ORDER BY bm25(msg_fts, 2.0, 1.0) ASC, msg_fts.rowid ASC LIMIT 1",
            [match, unit.chat_id, *unit.msg_ids],
        ).fetchone()
        if row is not None:
            return int(row["msg_id"])
    return unit.msg_ids[0]


def snippet(messages: Sequence[MessageRow], anchor_msg_id: int, limit: int = SNIPPET_CHARS) -> str:
    """The anchor's line first, then its neighbours in the unit, chronological, within ``limit``.

    Neighbours are taken alternately after and before the anchor while each fits; an anchor
    line longer than the limit is clipped with an ellipsis.
    """
    if not messages:
        return ""
    lines = [render_line(msg) for msg in messages]
    pos = next((i for i, msg in enumerate(messages) if msg.msg_id == anchor_msg_id), 0)
    anchor = lines[pos]
    if len(anchor) > limit:
        return clip(anchor, limit)
    chosen = sorted(_neighbours(lines, pos, limit - len(anchor)))
    return "\n".join([anchor, *(lines[i] for i in chosen)])


def _neighbours(lines: Sequence[str], pos: int, budget: int) -> list[int]:
    """Indexes around ``pos`` whose lines fit ``budget`` (with a newline each), alternating
    after and before; a side stops at the first line that does not fit."""
    chosen: list[int] = []
    sides = [iter(range(pos + 1, len(lines))), iter(range(pos - 1, -1, -1))]
    while sides:
        for side in list(sides):
            i = next(side, None)
            if i is None or budget < 1 + len(lines[i]):
                sides.remove(side)
                continue
            budget -= 1 + len(lines[i])
            chosen.append(i)
    return chosen


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + ELLIPSIS


def build_hit(
    conn: sqlite3.Connection,
    unit: UnitRow,
    anchor_msg_id: int | None,
    score: float,
    full: bool = False,
) -> Hit:
    """A :class:`~grepogram.models.Hit` for ``unit`` anchored at ``anchor_msg_id`` (the unit's
    first message when ``None`` or not part of it), linked through
    :func:`grepogram.links.message_url`; ``full`` copies the unit text into ``text``."""
    chat = db.get_chat(conn, unit.chat_id)
    if chat is None:
        raise LookupError(f"unit {unit.id} belongs to unknown chat {unit.chat_id}")
    if anchor_msg_id is None or anchor_msg_id not in unit.msg_ids:
        anchor_msg_id = unit.msg_ids[0]
    messages = unit_messages(conn, unit)
    link = links.message_url(chat, anchor_msg_id, unit.topic_id)
    return Hit(
        score=score,
        chat=chat,
        kind=unit.kind,
        date_start=unit.date_start,
        date_end=unit.date_end,
        anchor_msg_id=anchor_msg_id,
        url=link.url,
        fallback_url=link.fallback_url,
        snippet=snippet(messages, anchor_msg_id) if messages else clip(unit.text, SNIPPET_CHARS),
        msg_ids=list(unit.msg_ids),
        text=unit.text if full else None,
    )


# --- entry point -----------------------------------------------------------------------------


def index_age_min(conn: sqlite3.Connection, now: int | None = None) -> int | None:
    """Minutes since the latest completed sync, or ``None`` when no chat has been synced."""
    latest = db.last_sync_at(conn)
    if latest is None:
        return None
    now = int(time.time()) if now is None else now
    return max(0, now - latest) // 60


def search(
    conn: sqlite3.Connection,
    cfg: Config,
    query: str,
    filters: Filters | None = None,
    k: int | None = None,
    mode: str = "lexical",
    full: bool = False,
    now: int | None = None,
) -> SearchResult:
    """Run ``query`` over the index and return the top ``k`` hits (``[search] k`` by default).

    ``mode`` is ``lexical`` for now; ``hybrid`` and ``dense`` raise ``NotImplementedError``
    until the dense index exists, and anything else ``ValueError``. Both lexical lists are
    fetched ``max(k, rerank_top)`` deep, fused with :func:`rrf` using ``rrf_k``, and the top
    ``k`` become hits. A query without searchable words yields no hits and a warning, as does
    an index with no chats; ``index_age_min`` is filled in either way.
    """
    if mode not in MODES:
        raise ValueError(f"unknown search mode {mode!r}; expected one of {', '.join(MODES)}")
    if mode != "lexical":
        raise NotImplementedError(
            f"{mode} search needs the dense index, which is not built yet; use mode='lexical'"
        )
    k = cfg.search.k if k is None else k
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    filters = Filters() if filters is None else filters
    warnings: list[str] = []
    age = index_age_min(conn, now)
    if not db.list_chats(conn):
        warnings.append(NOTHING_INDEXED)
    if fts_query(query) is None:
        warnings.append(f"query {query!r} has no searchable words (letters or digits)")
        return SearchResult(hits=[], warnings=warnings, index_age_min=age)
    limit = max(k, cfg.search.rerank_top)
    unit_matches = lexical_units(conn, query, filters, limit)
    message_matches = lexical_messages(conn, query, filters, limit)
    fused = rrf(
        [[m.unit_id for m in unit_matches], [m.unit_id for m in message_matches]],
        cfg.search.rrf_k,
    )
    anchors = {m.unit_id: m.anchor_msg_id for m in message_matches}
    top = sorted(fused, key=lambda unit_id: -fused[unit_id])[:k]
    units = {unit.id: unit for unit in db.get_units_by_ids(conn, top)}
    hits: list[Hit] = []
    for unit_id in top:
        unit = units.get(unit_id)
        if unit is None:
            log.warning("unit %d is indexed but not stored; skipping", unit_id)
            continue
        anchor = anchors.get(unit_id)
        if anchor is None:
            anchor = best_anchor(conn, unit, query)
        hits.append(build_hit(conn, unit, anchor, fused[unit_id], full))
    log.debug(
        "lexical search: %d unit matches, %d message matches, %d hits",
        len(unit_matches),
        len(message_matches),
        len(hits),
    )
    return SearchResult(hits=hits, warnings=warnings, index_age_min=age)
