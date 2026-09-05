"""Search over the index: lexical and dense retrieval, rank fusion, reranking and dedup.

The lexical side asks FTS5 twice. ``unit_fts`` ranks whole units — windows, threads, posts —
with ``bm25(unit_fts, 2.0, 1.0)`` (the ``raw`` column weighted twice the ``stemmed`` one), and
``msg_fts`` ranks single messages the same way, so a message that packs every query term into
one line still stands out among long windows; each message hit is then mapped to the unit that
holds it (:func:`grepogram.db.containing_unit`). Both lists run the ``AND`` form of
:func:`~grepogram.stem.fts_query` first and fall back to ``OR`` when fewer rows than wanted
match, the ``AND`` rows keeping their place at the top. ``bm25()`` is negative — better is more
negative — so rows are ordered ascending and the score is negated. Chat and date filters are
plain ``AND`` predicates on the UNINDEXED columns, never part of the ``MATCH`` expression.

The dense side (:func:`dense_units`) embeds the query with the configured
:class:`~grepogram.embed.Embedder` and runs :func:`grepogram.index.knn` over ``unit_vec``; it
catches paraphrase the stems cannot connect. It only works while vectors exist and come from the
configured model: when they do not — nothing embedded yet, the ``dense`` extra missing, another
model recorded in ``meta`` — :func:`search` falls back to the lexical lists and says so in
``warnings`` instead of failing.

The lists are fused with Reciprocal Rank Fusion (:func:`rrf`), which needs no score calibration
between tables: a unit near the top of two lists outranks one found by a single list. The fused
top ``rerank_top`` then go through the cross-encoder (:class:`~grepogram.rerank.Reranker`,
skipped with ``rerank=False`` or when it cannot load) and are re-sorted by its scores, after
which :func:`dedup` drops every hit that mostly repeats a better one — a thread inside a window
already shown — and the top ``k`` survivors are returned. ``mode`` picks the retrieval lists
(``hybrid`` fuses both sides, ``lexical`` and ``dense`` use one) while reranking and dedup apply
to all of them, so a fallback from ``hybrid`` to ``lexical`` changes what is retrieved and
nothing else.

Every hit carries an *anchor*, the message its deep link opens: the matched message for a
message-level hit, and for every other hit the unit's best message under the query according to
``msg_fts`` (:func:`best_anchor`), else the unit's first message. The snippet leads with the
anchor's rendered line and adds its neighbours from within the unit while the total stays under
:data:`SNIPPET_CHARS`; ``full`` adds the unit's whole text. A channel's post thread is the one
unit whose text says more than its messages — the comments live in the discussion chat and only
the post is in ``msg_ids`` — so its snippet is cut from the unit text instead, leading with the
line that shares the most stems with the query (:func:`best_line`), while the anchor and the link
stay on the post.

Two readers let the caller see past a snippet: :func:`thread` returns the whole reply thread a
message belongs to (for a channel post, the post with its comments from the linked discussion
chat) and :func:`context` the messages around one in its topic, both as
:class:`~grepogram.models.MessageView` lists with deep links.
"""

import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import get_args

from grepogram import db, embed, index, links, units
from grepogram import rerank as reranking
from grepogram.embed import Embedder, ModelUnavailable
from grepogram.index import EmbeddingSpaceMismatch
from grepogram.models import (
    ChatRow,
    Config,
    Filters,
    Hit,
    MessageRow,
    MessageView,
    SearchMode,
    SearchResult,
    UnitRow,
)
from grepogram.rerank import Reranker
from grepogram.stem import FtsOp, fts_query, stem_token, tokenize
from grepogram.units import chronological, media_placeholder, render_line, window_topic

log = logging.getLogger(__name__)

SNIPPET_CHARS = 600
ELLIPSIS = "…"
MODES = get_args(SearchMode)
NO_SOURCES = (
    "no sources are configured: add one with `grepogram sources add <target>` "
    "and run `grepogram sync`"
)
NOTHING_INDEXED = "nothing is indexed yet: run `grepogram sync`"
NO_VECTORS = "no units are embedded yet; run `grepogram sync` or `grepogram embed`"
RECUT_PENDING = (
    "this index was cut by an older grepogram and its units are being re-cut a few chats "
    "per sync; run `grepogram sync` until it finishes for the best results"
)
"""Warning for the unit recipe a sync has not caught up with yet.

The condition is re-derived here rather than flagged anywhere: a run whose budget is below
:data:`grepogram.sync.RECUT_MIN_BUDGET_S` writes nothing, and an MCP-only user — whose syncs
are the 20-second ones inside a ``search`` call — is exactly who never sees the log line."""
_OPS: tuple[FtsOp, ...] = ("AND", "OR")

EmbedderLoader = Callable[[Config], Embedder]
RerankerLoader = Callable[[Config], Reranker]
"""How :func:`search` obtains a model it was not handed: :func:`grepogram.embed.load_embedder`
and :func:`grepogram.rerank.load_reranker` by default; the MCP server passes loaders that keep
the loaded model and remember a failure, so a model that cannot load is tried once."""


class DenseUnavailable(Exception):
    """The dense side cannot run: no vectors, no embedding model, or a mismatched space."""


class UnknownMessage(LookupError):
    """No stored message has this ``(chat_id, msg_id)``."""

    def __init__(self, chat_id: int, msg_id: int) -> None:
        super().__init__(f"message {msg_id} of chat {chat_id} is not indexed")
        self.chat_id = chat_id
        self.msg_id = msg_id


@dataclass(frozen=True, slots=True)
class Match:
    """One retrieved unit: ``score`` is ``-bm25`` on a lexical list and the cosine similarity
    on the dense list; ``anchor_msg_id`` is the matched message of a message-level hit and
    ``None`` when the unit matched as a whole."""

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
    chats: dict[int, ChatRow | None] = {}
    found: dict[int, Match] = {}
    for rowid, score in ranked:
        msg = messages.get(rowid)
        if msg is None:
            continue
        if msg.chat_id not in chats:
            chats[msg.chat_id] = db.get_chat(conn, msg.chat_id)
        chat = chats[msg.chat_id]
        topic_id = msg.topic_id if chat is None else window_topic(chat, msg)
        unit = db.containing_unit(conn, msg.chat_id, msg.msg_id, topic_id)
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


# --- dense list ------------------------------------------------------------------------------


def dense_units(
    conn: sqlite3.Connection,
    cfg: Config,
    query: str,
    filters: Filters,
    limit: int,
    embedder: Embedder | None = None,
    *,
    load_embedder: EmbedderLoader | None = None,
) -> list[Match]:
    """Up to ``limit`` units nearest to ``query`` in the dense index, best first, scored by
    cosine similarity; a unit with no similarity at all (cosine ≤ 0) is not a match.

    Without an ``embedder`` one is loaded through ``load_embedder`` (default
    :func:`grepogram.embed.load_embedder`), which is not called while ``unit_vec`` is missing or
    empty — nothing to search, no point loading a model. Raises :class:`DenseUnavailable` in
    that case, when the model cannot load, and when the stored vectors come from another model
    or width than the embedder.
    """
    if not db.has_vectors(conn):
        raise DenseUnavailable(NO_VECTORS)
    if limit <= 0 or (filters.chat_ids is not None and not filters.chat_ids):
        return []
    if embedder is None:
        try:
            embedder = (embed.load_embedder if load_embedder is None else load_embedder)(cfg)
        except ModelUnavailable as exc:
            raise DenseUnavailable(str(exc)) from exc
    try:
        index.check_embedding_space(conn, embedder)
        found = index.knn(
            conn, embedder.embed_query(query), filters, limit, cfg.search.vec_fanout_max
        )
    except EmbeddingSpaceMismatch as exc:
        raise DenseUnavailable(str(exc)) from exc
    return [Match(unit_id, 1.0 - distance) for unit_id, distance in found if distance < 1.0]


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


def dedup(hits: Sequence[Hit], overlap: float) -> list[Hit]:
    """Best hits first, minus every hit that mostly repeats a better one.

    Hits are taken in descending score (a stable sort, so ties keep their order) and a hit is
    dropped when at least ``overlap`` of its own message ids already belong to a kept hit of the
    same chat (:func:`overlap_ratio`): a thread inside a window that ranks higher is dropped,
    while a window that extends a higher-ranked thread survives, since most of it is new. A
    dropped hit shields nothing — later hits are compared with the kept ones only. A threshold
    above 1.0 keeps everything.
    """
    kept: list[Hit] = []
    for hit in sorted(hits, key=lambda h: -h.score):
        if not any(overlap_ratio(hit, other) >= overlap for other in kept):
            kept.append(hit)
    return kept


def overlap_ratio(hit: Hit, other: Hit) -> float:
    """The share of ``hit``'s message ids that ``other`` also carries; 0 across chats, whose
    message ids live in separate number spaces."""
    ids = set(hit.msg_ids)
    if hit.chat.id != other.chat.id or not ids:
        return 0.0
    return len(ids & set(other.msg_ids)) / len(ids)


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
    pos = next((i for i, msg in enumerate(messages) if msg.msg_id == anchor_msg_id), 0)
    return snippet_lines([render_line(msg) for msg in messages], pos, limit)


def snippet_lines(lines: Sequence[str], pos: int, limit: int = SNIPPET_CHARS) -> str:
    """:func:`snippet` over rendered lines: ``lines[pos]`` first, then its neighbours in order."""
    if not lines:
        return ""
    anchor = lines[pos]
    if len(anchor) > limit:
        return clip(anchor, limit)
    chosen = sorted(_neighbours(lines, pos, limit - len(anchor)))
    return "\n".join([anchor, *(lines[i] for i in chosen)])


def best_line(lines: Sequence[str], query: str | None) -> int:
    """The index of the line sharing the most stems with ``query``; the first line on a tie or
    when the query has no tokens (a post thread then leads with the post)."""
    if query is None or not lines:
        return 0
    wanted = {stem_token(token) for token in tokenize(query)}
    if not wanted:
        return 0
    overlaps = [len(wanted & {stem_token(t) for t in tokenize(line)}) for line in lines]
    return max(range(len(lines)), key=lambda i: (overlaps[i], -i))


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
    query: str | None = None,
) -> Hit:
    """A :class:`~grepogram.models.Hit` for ``unit`` anchored at ``anchor_msg_id`` (the unit's
    first message when ``None`` or not part of it), linked through
    :func:`grepogram.links.message_url`; ``full`` copies the unit text into ``text``.

    The snippet is built from the unit's stored messages around the anchor, except for a
    channel's post thread, whose comments are not among its messages: there it is cut from the
    unit text around the line ``query`` matches best (:func:`best_line`), so a hit that owes its
    rank to a comment shows that comment, while the anchor and the link stay on the post.
    """
    chat = db.get_chat(conn, unit.chat_id)
    if chat is None:
        raise LookupError(f"unit {unit.id} belongs to unknown chat {unit.chat_id}")
    if anchor_msg_id is None or anchor_msg_id not in unit.msg_ids:
        anchor_msg_id = unit.msg_ids[0]
    link = links.message_url(chat, anchor_msg_id, unit.topic_id)
    if _is_post_thread(chat, unit):
        lines = unit.text.splitlines()
        excerpt = snippet_lines(lines, best_line(lines, query))
    else:
        messages = unit_messages(conn, unit)
        excerpt = snippet(messages, anchor_msg_id) if messages else clip(unit.text, SNIPPET_CHARS)
    return Hit(
        score=score,
        chat=chat,
        kind=unit.kind,
        date_start=unit.date_start,
        date_end=unit.date_end,
        anchor_msg_id=anchor_msg_id,
        url=link.url,
        fallback_url=link.fallback_url,
        snippet=excerpt,
        msg_ids=list(unit.msg_ids),
        text=unit.text if full else None,
    )


def _is_post_thread(chat: ChatRow, unit: UnitRow) -> bool:
    """A ``thread`` of a channel: the post with its comments (:func:`grepogram.units.build_posts`),
    the only unit whose ``msg_ids`` do not cover its text."""
    return unit.kind == "thread" and chat.is_broadcast


# --- entry point -----------------------------------------------------------------------------


def index_age_min(conn: sqlite3.Connection, now: int | None = None) -> int | None:
    """Minutes since the latest completed sync, or ``None`` when no chat has been synced."""
    latest = db.last_sync_at(conn)
    if latest is None:
        return None
    now = int(time.time()) if now is None else now
    return max(0, now - latest) // 60


@dataclass(frozen=True, slots=True)
class _Candidate:
    unit_id: int
    unit: UnitRow
    score: float


def search(
    conn: sqlite3.Connection,
    cfg: Config,
    query: str,
    filters: Filters | None = None,
    k: int | None = None,
    mode: SearchMode = "hybrid",
    full: bool = False,
    now: int | None = None,
    *,
    rerank: bool = True,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    load_embedder: EmbedderLoader | None = None,
    load_reranker: RerankerLoader | None = None,
) -> SearchResult:
    """Run ``query`` over the index and return the top ``k`` hits (``[search] k`` by default).

    ``mode`` picks the retrieval lists: ``hybrid`` fuses the two lexical lists with the dense
    list, ``lexical`` and ``dense`` use one side only; anything else is a ``ValueError``. When
    the dense side is unavailable — no vectors yet, the model cannot load, vectors from another
    model — the search falls back to ``lexical`` and explains why in ``warnings``. A query
    without searchable words is answered by the dense side alone in ``hybrid`` mode and yields
    no hits (plus a warning) in ``lexical`` mode.

    Every list is fetched ``max(k, rerank_top)`` deep and the lists are fused with :func:`rrf`
    using ``rrf_k``; the fused top ``max(k, rerank_top)`` are re-scored by the cross-encoder
    when ``rerank`` is set and it loads (a hit's ``score`` is then the reranker's, otherwise the
    fused score), :func:`dedup` drops the near-duplicates at ``dedup_overlap``, and the top
    ``k`` survivors become hits. ``embedder`` and ``reranker`` stand in for the models
    ``load_embedder`` and ``load_reranker`` (:data:`EmbedderLoader`, :data:`RerankerLoader`;
    the package loaders by default) would load. An index with no chats yields no hits and a
    warning — that no source is configured, or that the configured ones are not synced yet;
    ``index_age_min`` is filled in either way.
    """
    if mode not in MODES:
        raise ValueError(f"unknown search mode {mode!r}; expected one of {', '.join(MODES)}")
    k = cfg.search.k if k is None else k
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    filters = Filters() if filters is None else filters
    warnings: list[str] = []
    age = index_age_min(conn, now)
    if not db.list_chats(conn):
        warnings.append(NOTHING_INDEXED if cfg.sources else NO_SOURCES)
        return SearchResult(hits=[], warnings=warnings, index_age_min=age)
    if db.unit_recipe(conn) != units.RECIPE_VERSION:
        warnings.append(RECUT_PENDING)
    limit = max(k, cfg.search.rerank_top)
    retrieved = _retrieve(conn, cfg, query, filters, limit, mode, warnings, embedder, load_embedder)
    candidates = _fuse(conn, cfg, retrieved, limit)
    if rerank and candidates:
        candidates = _rerank(cfg, query, candidates, reranker, warnings, load_reranker)
    hits = dedup(_hits(conn, query, retrieved, candidates, full), cfg.search.dedup_overlap)[:k]
    log.debug(
        "%s search: %d unit, %d message, %d dense matches; %d candidates, %d hits",
        retrieved.mode,
        len(retrieved.units),
        len(retrieved.messages),
        len(retrieved.dense),
        len(candidates),
        len(hits),
    )
    return SearchResult(hits=hits, warnings=warnings, index_age_min=age)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Retrieved:
    """The ranked lists one search fetched, under the mode it ended up running in.

    ``mode`` is what ran, which is not always what the caller asked for: an unavailable dense
    side degrades to ``lexical`` and a query without searchable words falls back to ``dense``.
    """

    mode: SearchMode
    units: list[Match] = field(default_factory=list)
    messages: list[Match] = field(default_factory=list)
    dense: list[Match] = field(default_factory=list)


def _retrieve(
    conn: sqlite3.Connection,
    cfg: Config,
    query: str,
    filters: Filters,
    limit: int,
    mode: SearchMode,
    warnings: list[str],
    embedder: Embedder | None,
    load_embedder: EmbedderLoader | None,
) -> _Retrieved:
    """Fetch the lists ``mode`` calls for, degrading when a side cannot answer.

    No vectors, no model or vectors from another model take the dense side out and leave a
    warning; a query with no letters or digits takes the lexical side out. A ``lexical`` search
    of such a query has nothing left to run and comes back empty.
    """
    effective = mode
    dense: list[Match] = []
    if effective != "lexical":
        try:
            dense = dense_units(
                conn, cfg, query, filters, limit, embedder, load_embedder=load_embedder
            )
        except DenseUnavailable as exc:
            warnings.append(f"dense search unavailable: {exc}")
            effective = "lexical"
    if effective != "dense" and fts_query(query) is None:
        note = f"query {query!r} has no searchable words (letters or digits)"
        if effective == "lexical":
            warnings.append(note)
            return _Retrieved(mode=effective)
        warnings.append(f"{note}; only the dense index was searched")
        effective = "dense"
    if effective == "dense":
        return _Retrieved(mode=effective, dense=dense)
    return _Retrieved(
        mode=effective,
        units=lexical_units(conn, query, filters, limit),
        messages=lexical_messages(conn, query, filters, limit),
        dense=dense,
    )


def _fuse(
    conn: sqlite3.Connection, cfg: Config, retrieved: _Retrieved, limit: int
) -> list[_Candidate]:
    """The retrieved lists fused with :func:`rrf`, deepest ``limit`` first, units attached.

    A unit an index still lists but the table no longer holds is logged and skipped; the repair
    in :func:`grepogram.index.repair_unit_index` drops such a row on the next sync.
    """
    fused = rrf(
        [
            [m.unit_id for m in retrieved.units],
            [m.unit_id for m in retrieved.messages],
            [m.unit_id for m in retrieved.dense],
        ],
        cfg.search.rrf_k,
    )
    order = sorted(fused, key=lambda unit_id: -fused[unit_id])[:limit]
    units = {unit.id: unit for unit in db.get_units_by_ids(conn, order)}
    candidates: list[_Candidate] = []
    for unit_id in order:
        unit = units.get(unit_id)
        if unit is None:
            log.warning("unit %d is indexed but not stored; skipping", unit_id)
            continue
        candidates.append(_Candidate(unit_id, unit, fused[unit_id]))
    return candidates


def _hits(
    conn: sqlite3.Connection,
    query: str,
    retrieved: _Retrieved,
    candidates: list[_Candidate],
    full: bool,
) -> list[Hit]:
    """The candidates as hits, anchored on the message the lexical pass matched when there is
    one and on the unit's own best line otherwise."""
    anchors = {m.unit_id: m.anchor_msg_id for m in retrieved.messages}
    hits: list[Hit] = []
    for candidate in candidates:
        anchor = anchors.get(candidate.unit_id)
        if anchor is None:
            anchor = best_anchor(conn, candidate.unit, query)
        hits.append(build_hit(conn, candidate.unit, anchor, candidate.score, full, query=query))
    return hits


def _rerank(
    cfg: Config,
    query: str,
    candidates: list[_Candidate],
    reranker: Reranker | None,
    warnings: list[str],
    load_reranker: RerankerLoader | None = None,
) -> list[_Candidate]:
    """The candidates re-scored by the cross-encoder and sorted by that score, ties keeping
    their fused order; when the reranker cannot load or score, a warning is added and the
    candidates come back untouched."""
    try:
        if reranker is None:
            reranker = (reranking.load_reranker if load_reranker is None else load_reranker)(cfg)
        scores = reranker.score(query, [candidate.unit.text for candidate in candidates])
    except ModelUnavailable as exc:
        warnings.append(f"reranking unavailable: {exc}")
        return candidates
    if len(scores) != len(candidates):
        # not redundant with the model's own check: any Reranker may answer the wrong number of
        # scores, and only a RuntimeError says "bug" — a ValueError out of zip(strict=True) is a
        # tool error the MCP server would report to the caller as its own fault.
        raise RuntimeError(
            f"reranker {reranker.name} returned {len(scores)} scores for {len(candidates)} texts"
        )
    rescored = [
        _Candidate(candidate.unit_id, candidate.unit, score)
        for candidate, score in zip(candidates, scores, strict=True)
    ]
    rescored.sort(key=lambda candidate: -candidate.score)
    return rescored


# --- readers ---------------------------------------------------------------------------------


def message_view(chat: ChatRow, msg: MessageRow) -> MessageView:
    """``msg`` as the caller sees it: linked through :func:`grepogram.links.message_url`, with a
    ``[photo]``-style placeholder as ``text`` when it has media and no caption and ``chat.id``
    as ``chat_id`` — the chat the message is really in, which :func:`thread` mixes."""
    link = links.message_url(chat, msg.msg_id, msg.topic_id)
    return MessageView(
        chat_id=chat.id,
        msg_id=msg.msg_id,
        date=msg.date,
        from_name=msg.from_name,
        text=msg.text.strip() or media_placeholder(msg),
        url=link.url,
        fallback_url=link.fallback_url,
        reply_to_msg_id=msg.reply_to_msg_id,
    )


def thread(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> list[MessageView]:
    """The reply thread holding ``msg_id`` in ``chat_id``, chronological, root first.

    The thread is the root reached by walking ``reply_to_msg_id`` upwards and every reply below
    it (:func:`grepogram.db.get_thread_messages`); a message nobody replied to that replies to
    nothing is a thread of one. For a channel post the comments stored under the linked
    discussion chat follow the post, each linked into that chat — the ones naming *this* channel
    and this post, so a forum topic of that group and a comment left by a channel that held the
    group before are not among them. Such a list spans two chats, and each view's ``chat_id``
    is what says which: a comment's ``msg_id`` is only meaningful together with the discussion
    group's id, because it numbers from 1 exactly as the channel's posts do. Raises
    :class:`UnknownMessage` when the message is not indexed.
    """
    chat = _locate(conn, chat_id, msg_id)
    views = [message_view(chat, msg) for msg in db.get_thread_messages(conn, chat_id, msg_id)]
    if chat.is_broadcast:
        discussion = db.get_discussion_chat(conn, chat.id)
        if discussion is not None:
            by_post = db.get_comment_messages(conn, discussion.id, chat.id, [msg_id])
            comments = chronological(by_post.get(msg_id, []))
            views += [message_view(discussion, msg) for msg in comments]
    return views


def context(
    conn: sqlite3.Connection, chat_id: int, msg_id: int, before: int = 15, after: int = 15
) -> list[MessageView]:
    """``msg_id`` with up to ``before`` messages preceding and ``after`` following it in the
    same topic of ``chat_id``, in ``msg_id`` order (:func:`grepogram.db.get_context_messages`).
    Raises :class:`UnknownMessage` when the message is not indexed and ``ValueError`` for a
    negative count."""
    chat = _locate(conn, chat_id, msg_id)
    messages = db.get_context_messages(conn, chat_id, msg_id, before, after)
    return [message_view(chat, msg) for msg in messages]


def _locate(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> ChatRow:
    """The chat row of a stored message, or :class:`UnknownMessage`."""
    chat = db.get_chat(conn, chat_id)
    if chat is None or db.get_message(conn, chat_id, msg_id) is None:
        raise UnknownMessage(chat_id, msg_id)
    return chat
