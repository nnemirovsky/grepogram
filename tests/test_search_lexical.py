import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grepogram import cli, config, db, filters, search, sync
from grepogram.models import ChatRow, Config, Filters, MessageRow, SearchCfg, Source, UnitRow
from grepogram.paths import Paths
from grepogram.search import Match
from grepogram.stem import fts_query, stem_text
from grepogram.units import render_line
from tests.fixtures import chat_ru

ARG = chat_ru.ARG_ID
GEO = chat_ru.GEO_ID
CFG = chat_ru.CFG
RAW = Config(search=SearchCfg(dedup_overlap=1.5), units=CFG.units, sources=CFG.sources)
"""The fixture config with dedup switched off, to look at the fused ranking itself."""
ALL = Filters()
JUNE = filters.parse_when("2024-06")

runner = CliRunner()


@pytest.fixture
def loaded(conn: sqlite3.Connection) -> chat_ru.Loaded:
    return chat_ru.load(conn)


def _unit(conn: sqlite3.Connection, unit_id: int) -> UnitRow:
    (unit,) = db.get_units_by_ids(conn, [unit_id])
    return unit


def _units_with(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> set[int]:
    """Ids of every unit of a chat whose ``msg_ids`` include ``msg_id``."""
    return {u.id for u in db.get_units(conn, chat_id) if u.id is not None and msg_id in u.msg_ids}


def _fts_ids(conn: sqlite3.Connection, table: str, match: str | None) -> set[int]:
    """Rowids a raw MATCH selects, unranked."""
    assert match is not None
    return {
        int(r[0])
        for r in conn.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (match,))
    }


def _msg_id(conn: sqlite3.Connection, row_id: int) -> int:
    (msg,) = db.get_messages_by_ids(conn, [row_id])
    return msg.msg_id


def _ids(matches: list[Match]) -> list[int]:
    return [m.unit_id for m in matches]


# --- fixture ---------------------------------------------------------------------------------


def test_fixture_runs_the_real_pipeline(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 62
    assert len(loaded.row_ids[ARG]) == 42 and len(loaded.row_ids[GEO]) == 20
    for chat_id in (ARG, GEO):
        kinds = {u.kind for u in db.get_units(conn, chat_id)}
        assert kinds == {"window", "thread"}
    assert conn.execute("SELECT count(*) FROM unit_fts").fetchone()[0] == len(
        db.get_units(conn, ARG) + db.get_units(conn, GEO)
    )
    assert conn.execute("SELECT count(*) FROM msg_fts").fetchone()[0] == 61  # one media-only
    assert db.last_sync_at(conn) == chat_ru.SYNCED_AT
    assert any(len(u.msg_ids) == chat_ru.CFG.units.thread_max_msgs for u in db.get_units(conn, ARG))


# --- lexical_units ---------------------------------------------------------------------------


def test_lexical_units_finds_inflected_forms(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    matches = search.lexical_units(conn, "открыть счета", ALL, 40)
    assert matches
    both = _fts_ids(conn, "unit_fts", fts_query("открыть счета", "AND"))
    assert set(_ids(matches)[: len(both)]) == both
    assert _units_with(conn, ARG, 1) & both  # "где открыть счёт"
    for unit_id in both:
        stemmed = stem_text(_unit(conn, unit_id).text).split()
        assert "откр" in stemmed and "счет" in stemmed
    only_accounts = search.lexical_units(conn, "счетов", ALL, 40)
    assert set(_ids(only_accounts)) == _fts_ids(conn, "unit_fts", '"счет"')
    assert set(_ids(only_accounts)) >= _units_with(conn, GEO, 5)  # "счета в лари"


def test_lexical_units_scores_are_negated_bm25_in_rank_order(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    matches = search.lexical_units(conn, "Galicia", ALL, 40)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)
    assert all(score > 0 for score in scores)
    assert all(m.anchor_msg_id is None for m in matches)
    assert len(set(_ids(matches))) == len(matches)


def test_and_hits_precede_or_fallback(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    both = _fts_ids(conn, "unit_fts", fts_query("Galicia DNI", "AND"))
    either = _fts_ids(conn, "unit_fts", fts_query("Galicia DNI", "OR"))
    assert both and both < either
    ids = _ids(search.lexical_units(conn, "Galicia DNI", ALL, 40))
    assert set(ids[: len(both)]) == both
    assert set(ids) == either
    assert len(ids) == len(set(ids))
    assert set(_ids(search.lexical_units(conn, "Galicia DNI", ALL, len(both)))) == both
    one_more = _ids(search.lexical_units(conn, "Galicia DNI", ALL, len(both) + 1))
    assert set(one_more[: len(both)]) == both and one_more[-1] in either - both


def test_or_fallback_when_terms_never_cooccur(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    assert _fts_ids(conn, "unit_fts", fts_query("Claro Magti", "AND")) == set()
    matches = search.lexical_units(conn, "Claro Magti", ALL, 40)
    chats = {_unit(conn, m.unit_id).chat_id for m in matches}
    assert chats == {ARG, GEO}


def test_lexical_units_empty_cases(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    assert search.lexical_units(conn, "🙂 !!!", ALL, 10) == []
    assert search.lexical_units(conn, "Galicia", Filters(chat_ids=set()), 10) == []
    assert search.lexical_units(conn, "Galicia", ALL, 0) == []
    assert search.lexical_units(conn, "unicorn", ALL, 10) == []


def test_chat_filter_excludes_other_chat(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    everything = set(_ids(search.lexical_units(conn, "счёт", ALL, 40)))
    georgia = set(_ids(search.lexical_units(conn, "счёт", Filters(chat_ids={GEO}), 40)))
    argentina = set(_ids(search.lexical_units(conn, "счёт", Filters(chat_ids={ARG}), 40)))
    assert georgia and argentina
    assert {_unit(conn, u).chat_id for u in georgia} == {GEO}
    assert {_unit(conn, u).chat_id for u in argentina} == {ARG}
    assert georgia | argentina == everything
    assert (
        set(_ids(search.lexical_units(conn, "счёт", Filters(chat_ids={ARG, GEO}), 40)))
        == everything
    )


def test_date_filter_bounds_units_by_start(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    def starts(flt: Filters) -> list[int]:
        return [
            _unit(conn, m.unit_id).date_start for m in search.lexical_units(conn, "DNI", flt, 40)
        ]

    unfiltered = starts(ALL)
    assert min(unfiltered) < JUNE <= max(unfiltered)
    since_june = starts(filters.resolve_filters(conn, CFG, None, "2024-06", None))
    assert since_june and all(start >= JUNE for start in since_june)
    assert sorted(since_june) == sorted(s for s in unfiltered if s >= JUNE)
    january = filters.resolve_filters(conn, CFG, None, None, "2024-01-31")
    until_january = starts(january)
    assert until_january and all(start <= january.until for start in until_january if january.until)
    window = filters.resolve_filters(conn, CFG, None, "2024-02", "2024-03")
    assert window.since is not None and window.until is not None
    assert all(window.since <= start <= window.until for start in starts(window))
    assert starts(filters.resolve_filters(conn, CFG, None, "2025-01", None)) == []


# --- lexical_messages ------------------------------------------------------------------------


def test_lexical_messages_map_to_containing_window_with_anchor(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    (match,) = search.lexical_messages(conn, "Recoleta", ALL, 40)
    unit = _unit(conn, match.unit_id)
    assert unit.kind == "window" and unit.chat_id == ARG and 37 in unit.msg_ids
    assert match.anchor_msg_id == 37 and match.score > 0
    containing = db.containing_unit(conn, ARG, 37, None)
    assert containing is not None and containing.id == unit.id


def test_lexical_messages_list_each_unit_once_with_its_best_message(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    matches = search.lexical_messages(conn, "TBC", ALL, 40)
    ids = _ids(matches)
    assert len(ids) == len(set(ids)) and len(ids) >= 2
    ranked = [
        _msg_id(conn, int(row[0]))
        for row in conn.execute(
            "SELECT rowid FROM msg_fts WHERE msg_fts MATCH ? "
            "ORDER BY bm25(msg_fts, 2.0, 1.0) ASC, rowid ASC",
            (fts_query("TBC"),),
        )
    ]
    for match in matches:
        unit = _unit(conn, match.unit_id)
        assert unit.chat_id == GEO and unit.kind == "window"
        assert match.anchor_msg_id == next(m for m in ranked if m in unit.msg_ids)
        assert "TBC" in chat_ru.message(GEO, match.anchor_msg_id or 0).text


def test_lexical_messages_respect_filters_and_skip_unitless_messages(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    assert search.lexical_messages(conn, "паспорту", Filters(chat_ids={ARG}), 40)
    assert all(
        _unit(conn, m.unit_id).chat_id == GEO
        for m in search.lexical_messages(conn, "паспорту", Filters(chat_ids={GEO}), 40)
    )
    assert search.lexical_messages(conn, "🙂", ALL, 40) == []
    stray = MessageRow(chat_id=ARG, msg_id=99, date=chat_ru.SYNCED_AT, text="unicorn sighting")
    from grepogram import index

    index.index_messages(conn, db.upsert_messages(conn, [stray]))
    assert _fts_ids(conn, "msg_fts", '"unicorn"')
    assert search.lexical_messages(conn, "unicorn", ALL, 40) == []


# --- search ----------------------------------------------------------------------------------


def test_search_hit_carries_url_anchor_and_snippet(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    result = search.search(conn, CFG, "Recoleta", ALL, mode="lexical")
    assert result.warnings == [] and result.synced is False
    hit = result.hits[0]
    assert hit.kind == "window" and hit.chat == loaded.chats[ARG]
    assert hit.anchor_msg_id == 37
    assert hit.url == "https://t.me/arg_chat/37" and hit.fallback_url is None
    assert hit.msg_ids == list(range(35, 43))
    assert hit.snippet.splitlines()[0] == render_line(chat_ru.message(ARG, 37))
    assert len(hit.snippet) <= search.SNIPPET_CHARS
    assert hit.text is None
    unit = _unit(
        conn, next(u for u in _units_with(conn, ARG, 37) if _unit(conn, u).kind == "window")
    )
    assert hit.date_start == unit.date_start and hit.date_end == unit.date_end

    (private,) = search.search(conn, CFG, "Silknet", ALL).hits
    assert private.chat.id == GEO and private.anchor_msg_id == 14
    assert private.url == "https://t.me/c/1000000200/14"


def test_search_ranks_units_found_by_both_lists_first(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    hits = search.search(conn, RAW, "Recoleta", ALL, mode="lexical", rerank=False).hits
    assert [h.kind for h in hits] == ["window", "thread"]
    assert all(37 in h.msg_ids for h in hits)
    by_unit = _ids(search.lexical_units(conn, "Recoleta", ALL, 40))
    by_message = _ids(search.lexical_messages(conn, "Recoleta", ALL, 40))
    assert [_unit(conn, u).kind for u in by_unit] == [
        "thread",
        "window",
    ]  # bm25 likes the short one
    assert [_unit(conn, u).kind for u in by_message] == ["window"]
    rrf_k = CFG.search.rrf_k
    assert hits[0].score == pytest.approx(1 / (rrf_k + 2) + 1 / (rrf_k + 1))
    assert hits[1].score == pytest.approx(1 / (rrf_k + 1))
    deduped = search.search(conn, CFG, "Recoleta", ALL, mode="lexical", rerank=False).hits
    assert [h.kind for h in deduped] == ["window"]  # the thread lies inside the window
    assert deduped[0].score == hits[0].score


def test_search_anchors_unit_hits_at_their_best_message(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    found = search.search(conn, RAW, "Recoleta", ALL, mode="lexical").hits
    thread = next(h for h in found if h.kind == "thread")
    assert thread.anchor_msg_id == 37 and thread.url == "https://t.me/arg_chat/37"
    hits = search.search(conn, RAW, "открыть счёт", ALL, mode="lexical").hits
    continuation = next(h for h in hits if h.kind == "thread" and h.msg_ids == [1, 7, 10])
    assert continuation.anchor_msg_id == 1
    assert continuation.snippet.startswith(render_line(chat_ru.message(ARG, 1)))


def test_search_applies_filters(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    georgia = search.search(conn, CFG, "счёт", Filters(chat_ids={GEO}), 40).hits
    assert georgia and {h.chat.id for h in georgia} == {GEO}
    assert all(h.url.startswith("https://t.me/c/1000000200/") for h in georgia)
    june = search.search(conn, CFG, "DNI", Filters(since=JUNE), 40).hits
    assert june and all(h.date_start >= JUNE for h in june)
    assert search.search(conn, CFG, "DNI", Filters(until=JUNE - 1, chat_ids={GEO}), 40).hits == []


def test_search_warns_on_query_without_words(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    result = search.search(conn, CFG, "🙂🙂 …", ALL, mode="lexical", now=chat_ru.SYNCED_AT + 120)
    assert result.hits == []
    assert len(result.warnings) == 1 and "no searchable words" in result.warnings[0]
    assert result.index_age_min == 2


def test_search_reports_index_age(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    assert search.search(conn, CFG, "DNI", ALL, now=chat_ru.SYNCED_AT + 5 * 60).index_age_min == 5
    assert search.search(conn, CFG, "DNI", ALL, now=chat_ru.SYNCED_AT + 59).index_age_min == 0
    assert search.search(conn, CFG, "DNI", ALL, now=chat_ru.SYNCED_AT - 100).index_age_min == 0
    assert search.index_age_min(conn, chat_ru.SYNCED_AT + 3600) == 60
    db.set_chat_progress(conn, ARG, 42, None)
    db.set_chat_progress(conn, GEO, 20, None)
    assert search.search(conn, CFG, "DNI", ALL).index_age_min is None


def test_search_on_an_empty_index_warns(conn: sqlite3.Connection) -> None:
    result = search.search(conn, CFG, "DNI", ALL)
    assert result.hits == [] and result.index_age_min is None
    assert result.warnings == [search.NOTHING_INDEXED]
    unconfigured = search.search(conn, Config(), "DNI", ALL)
    assert unconfigured.hits == [] and unconfigured.warnings == [search.NO_SOURCES]


def test_search_rejects_bad_modes_and_k(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    with pytest.raises(ValueError, match="unknown search mode"):
        # the type says otherwise; the check is for the callers that are not type-checked
        search.search(conn, CFG, "DNI", ALL, mode="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        search.search(conn, CFG, "DNI", ALL, k=0)


def test_search_k_and_full(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    assert len(search.search(conn, CFG, "DNI", ALL, 3).hits) == 3
    assert len(search.search(conn, CFG, "DNI", ALL, 40).hits) > 3
    small = Config(search=SearchCfg(k=2), units=CFG.units, sources=CFG.sources)
    assert len(search.search(conn, small, "DNI").hits) == 2
    (hit,) = search.search(conn, CFG, "Silknet", ALL, full=True).hits
    unit = _unit(
        conn, next(u for u in _units_with(conn, GEO, 14) if _unit(conn, u).kind == "window")
    )
    assert hit.text == unit.text
    assert hit.snippet in unit.text or hit.snippet.splitlines()[0] in unit.text


# --- rrf, snippet, build_hit -----------------------------------------------------------------


def test_rrf_sums_reciprocal_ranks_in_first_seen_order() -> None:
    fused = search.rrf([[1, 2, 3], [2, 1], [4]], 60)
    assert fused == pytest.approx({1: 1 / 61 + 1 / 62, 2: 1 / 62 + 1 / 61, 3: 1 / 63, 4: 1 / 61})
    assert list(fused) == [1, 2, 3, 4]
    assert sorted(fused, key=lambda i: -fused[i]) == [1, 2, 4, 3]
    assert search.rrf([], 60) == {}
    assert search.rrf([[], []], 60) == {}


def _line_msgs(texts: list[str]) -> list[MessageRow]:
    return [
        MessageRow(chat_id=ARG, msg_id=i + 1, date=1_700_000_000 + i * 60, from_name="A", text=t)
        for i, t in enumerate(texts)
    ]


def test_snippet_leads_with_anchor_and_fills_neighbours_within_budget() -> None:
    msgs = _line_msgs(["one", "two", "three", "four", "five"])
    lines = [render_line(m) for m in msgs]
    assert search.snippet(msgs, 3) == "\n".join([lines[2], lines[0], lines[1], lines[3], lines[4]])
    assert search.snippet(msgs, 1) == "\n".join(lines)
    assert search.snippet(msgs, 999).splitlines()[0] == lines[0]
    assert search.snippet([], 1) == ""
    budget = len(lines[2]) + 1 + len(lines[3]) + 1 + len(lines[1])
    assert search.snippet(msgs, 3, limit=budget) == "\n".join([lines[2], lines[1], lines[3]])
    assert search.snippet(msgs, 3, limit=len(lines[2])) == lines[2]


def test_snippet_clips_long_lines() -> None:
    msgs = _line_msgs(["x" * 1000, "short"])
    clipped = search.snippet(msgs, 1)
    assert len(clipped) == search.SNIPPET_CHARS and clipped.endswith(search.ELLIPSIS)
    short = search.snippet(msgs, 2)
    assert short == render_line(msgs[1])
    long_after = _line_msgs(["short", "y" * 700])
    assert search.snippet(long_after, 1) == render_line(long_after[0])


def test_build_hit_falls_back_to_the_first_message(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    unit = _unit(conn, next(iter(_units_with(conn, GEO, 19))))
    hit = search.build_hit(conn, unit, None, 0.5)
    assert hit.anchor_msg_id == unit.msg_ids[0] and hit.score == 0.5
    assert hit.url == f"https://t.me/c/1000000200/{unit.msg_ids[0]}"
    assert search.build_hit(conn, unit, 999, 0.5).anchor_msg_id == unit.msg_ids[0]
    assert search.build_hit(conn, unit, 20, 0.5).anchor_msg_id == 20
    assert search.build_hit(conn, unit, 20, 0.5, full=True).text == unit.text
    assert search.best_anchor(conn, unit, "border") == 20
    assert search.best_anchor(conn, unit, "🙂") == unit.msg_ids[0]
    assert search.best_anchor(conn, unit, "unicorn") == unit.msg_ids[0]


NEWS = -1001000000300
NEWS_CHAT = -1001000000301


def _channel_with_a_comment(conn: sqlite3.Connection) -> tuple[Config, UnitRow]:
    """A channel post with one comment stored under its discussion group, both indexed as a
    sync would, and the post-thread unit that carries the comment in its text alone."""
    cfg = Config(sources=[Source(chat="@news", comments=True)])
    db.upsert_chat(
        conn,
        ChatRow(id=NEWS, type="channel", title="News", username="news", source_id="chat:@news"),
    )
    db.upsert_chat(
        conn,
        ChatRow(
            id=NEWS_CHAT,
            type="supergroup",
            title="News chat",
            source_id="chat:@news",
            discussion_of=NEWS,
        ),
    )
    post = db.upsert_messages(
        conn,
        [
            MessageRow(
                chat_id=NEWS,
                msg_id=1,
                date=1_700_000_000,
                from_name="News",
                text="Announcing the new office hours",
            )
        ],
    )
    comment = db.upsert_messages(
        conn,
        [
            MessageRow(
                chat_id=NEWS_CHAT,
                msg_id=5,
                date=1_700_000_060,
                from_id=2,
                from_name="Bob",
                topic_id=1,
                text="Does the Brubank branch accept CUIT without DNI?",
            )
        ],
    )
    for chat_id, ids in ((NEWS, post), (NEWS_CHAT, comment)):
        chat = db.get_chat(conn, chat_id)
        assert chat is not None
        sync.on_chat_synced(conn, chat, cfg, ids)
    (thread,) = [u for u in db.get_units(conn, NEWS) if u.kind == "thread"]
    return cfg, thread


def test_post_thread_snippet_leads_with_the_comment_that_matched(
    conn: sqlite3.Connection,
) -> None:
    """The comments of a post thread are not among its messages, so the snippet is cut from the
    unit text around the line the query matches; the anchor and the link stay on the post."""
    cfg, thread = _channel_with_a_comment(conn)
    post_line, comment_line = thread.text.splitlines()
    assert thread.msg_ids == [1]
    only_news = Filters(chat_ids={NEWS})
    (hit,) = search.search(conn, cfg, "Brubank CUIT", only_news, mode="lexical", rerank=False).hits
    assert hit.kind == "thread" and hit.msg_ids == [1] and hit.text is None
    assert hit.anchor_msg_id == 1 and hit.url == "https://t.me/news/1"
    assert hit.snippet == f"{comment_line}\n{post_line}"
    (about_post,) = search.search(
        conn, cfg, "office hours", only_news, mode="lexical", rerank=False
    ).hits
    assert about_post.snippet.splitlines()[0] == post_line
    plain = search.build_hit(conn, thread, None, 0.5)
    assert plain.snippet == f"{post_line}\n{comment_line}" and plain.anchor_msg_id == 1
    full = search.build_hit(conn, thread, 1, 0.5, full=True, query="CUIT DNI")
    assert full.snippet.splitlines()[0] == comment_line and full.text == thread.text
    (post_unit,) = [u for u in db.get_units(conn, NEWS) if u.kind == "post"]
    assert search.build_hit(conn, post_unit, 1, 0.5, query="CUIT").snippet == post_line


def test_best_line_picks_the_line_sharing_the_most_stems_with_the_query() -> None:
    lines = [
        "[t] News: Announcing the new office hours",
        "[t] Bob: Does the Brubank branch accept CUIT?",
        "[t] Ann: CUIT and DNI at Brubank branches",
    ]
    assert search.best_line(lines, "Brubank CUIT DNI") == 2
    assert search.best_line(lines, "brubank") == 1
    assert search.best_line(lines, "branch") == 1
    assert search.best_line(lines, "offices") == 0
    assert search.best_line(lines, "unicorn") == 0
    assert search.best_line(lines, "🙂") == 0
    assert search.best_line(lines, None) == 0
    assert search.best_line([], "x") == 0
    assert search.snippet_lines([], 0) == ""
    assert search.snippet_lines(lines, 1, limit=len(lines[1])) == lines[1]
    assert search.snippet_lines(lines, 2) == "\n".join([lines[2], lines[0], lines[1]])


# --- CLI -------------------------------------------------------------------------------------


@pytest.fixture
def seeded_home(tmp_home: Path) -> Path:
    conn = db.connect(Paths.from_env())
    db.migrate(conn)
    chat_ru.load(conn)
    conn.close()
    return tmp_home


def test_cli_search_json_prints_the_result_and_nothing_else(seeded_home: Path) -> None:
    result = runner.invoke(
        cli.app, ["search", "открыть счёт", "--json", "-k", "2", "--mode", "lexical"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"hits", "warnings", "index_age_min", "synced"}
    assert payload["warnings"] == [] and payload["synced"] is False
    assert isinstance(payload["index_age_min"], int) and payload["index_age_min"] > 0
    assert len(payload["hits"]) == 2
    hit = payload["hits"][0]
    assert set(hit) == {
        "score",
        "chat",
        "kind",
        "date_start",
        "date_end",
        "anchor_msg_id",
        "url",
        "fallback_url",
        "snippet",
        "msg_ids",
        "text",
    }
    assert hit["url"] == f"https://t.me/arg_chat/{hit['anchor_msg_id']}"
    assert hit["chat"]["id"] == ARG and hit["chat"]["title"] == "Argentina chat"
    assert "счёт" in hit["snippet"] and hit["text"] is None
    full = runner.invoke(cli.app, ["search", "Silknet", "--json", "--full"])
    (only,) = json.loads(full.stdout)["hits"]
    assert only["text"] and "Silknet" in only["text"]


def test_cli_search_text_output_has_url_per_hit(seeded_home: Path) -> None:
    result = runner.invoke(cli.app, ["search", "Recoleta", "--mode", "lexical"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith("1. ") and "window" in lines[0] and "Argentina chat" in lines[0]
    assert lines[1] == "   https://t.me/arg_chat/37"
    assert lines[2].startswith("   [2024-06-20 18:04] Alice: Confirmed today at the Recoleta")
    assert not any(line.startswith("2. ") for line in lines)  # the thread inside it is a duplicate
    assert "note: the index is" in result.stderr and "grepogram sync" in result.stderr
    assert "note:" not in result.stdout
    two = runner.invoke(cli.app, ["search", "DNI", "--mode", "lexical", "-k", "2"])
    lines = two.stdout.splitlines()
    assert lines[0].startswith("1. ")
    assert "" in lines and lines[lines.index("") + 1].startswith("2. ")
    assert sum(line.startswith("   https://t.me/arg_chat/") for line in lines) == 2


def test_cli_search_filters(seeded_home: Path) -> None:
    georgia = runner.invoke(cli.app, ["search", "счёт", "--chat", "georgia", "--json"])
    assert georgia.exit_code == 0, georgia.output
    hits = json.loads(georgia.stdout)["hits"]
    assert hits and {h["chat"]["id"] for h in hits} == {GEO}
    folder = runner.invoke(
        cli.app, ["search", "DNI", "-c", "folder:Argentina", "--since", "2024-06", "--json"]
    )
    hits = json.loads(folder.stdout)["hits"]
    assert hits and all(h["chat"]["id"] == ARG and h["date_start"] >= JUNE for h in hits)
    nothing = runner.invoke(cli.app, ["search", "DNI", "--until", "2023-12-31"])
    assert nothing.exit_code == 0 and nothing.stdout == "no hits\n"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["search", "DNI", "--chat", "nowhere"], "no indexed chat matches"),
        (["search", "DNI", "--since", "yesterday"], "cannot read date"),
        (["search", "DNI", "--since", "2024-06", "--until", "2024-01"], "lies after"),
    ],
)
def test_cli_search_errors_go_to_stderr(seeded_home: Path, args: list[str], message: str) -> None:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert message in result.stderr


def test_cli_search_rejects_bad_options(seeded_home: Path) -> None:
    assert runner.invoke(cli.app, ["search", "DNI", "--mode", "bogus"]).exit_code != 0
    assert runner.invoke(cli.app, ["search", "DNI", "-k", "0"]).exit_code != 0
    assert runner.invoke(cli.app, ["search"]).exit_code != 0


def test_cli_search_on_a_fresh_home_warns(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["search", "DNI"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "no hits\n"
    assert "no sources are configured" in result.stderr
    as_json = runner.invoke(cli.app, ["search", "DNI", "--json"])
    payload = json.loads(as_json.stdout)
    assert payload["hits"] == [] and payload["warnings"] == [search.NO_SOURCES]
    assert payload["index_age_min"] is None
    config.save(CFG, Paths.from_env())
    unsynced = runner.invoke(cli.app, ["search", "DNI"])
    assert unsynced.stdout == "no hits\n" and "nothing is indexed yet" in unsynced.stderr
