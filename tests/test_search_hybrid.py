import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grepogram import cli, db, embed, filters, index, search
from grepogram import rerank as reranking
from grepogram.embed import FAKE_DIM, FakeEmbedder, ModelUnavailable
from grepogram.index import EmbeddingSpaceMismatch
from grepogram.log import shutdown_logging
from grepogram.models import ChatRow, Config, Filters, Hit, SearchCfg, UnitRow
from grepogram.paths import Paths
from grepogram.rerank import FakeReranker
from grepogram.search import DenseUnavailable, Match
from tests.fixtures import chat_ru

ARG = chat_ru.ARG_ID
GEO = chat_ru.GEO_ID
CFG = chat_ru.CFG
RAW = Config(search=SearchCfg(dedup_overlap=1.5), units=CFG.units, sources=CFG.sources)
"""The fixture config with dedup switched off, to look at the fused ranking itself."""
ALL = Filters()
PAIR = chat_ru.PARAPHRASE
JUNE = filters.parse_when("2024-06")
RRF_MAX = 3 / (CFG.search.rrf_k + 1)
"""The largest fused score three lists can give: rank 1 on each of them."""
CANDIDATES = max(CFG.search.k, CFG.search.rerank_top)
NO_VECTORS = f"dense search unavailable: {search.NO_VECTORS}"

runner = CliRunner()


class CountingEmbedder(FakeEmbedder):
    """The fake, recording every query it embeds."""

    def __init__(self, dim: int = FAKE_DIM) -> None:
        super().__init__(dim=dim)
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return super().embed_query(text)


class RecoletaEmbedder(FakeEmbedder):
    """Answers every query with the vector of ``Recoleta`` — a dense side with an opinion."""

    def embed_query(self, text: str) -> list[float]:
        return super().embed_query("Recoleta")


class OtherModel(FakeEmbedder):
    name = "other-model"


class CountingReranker(FakeReranker):
    """The fake, recording every ``score`` call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        return super().score(query, texts)


class ShortReranker(FakeReranker):
    """Answers one score too few."""

    def score(self, query: str, texts: list[str]) -> list[float]:
        return super().score(query, texts)[:-1]


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def loaded(conn: sqlite3.Connection) -> chat_ru.Loaded:
    return chat_ru.load(conn)


@pytest.fixture
def embedded(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> CountingEmbedder:
    """The fixture chats embedded with the fake."""
    embedder = CountingEmbedder()
    index.embed_dirty_units(conn, embedder)
    return embedder


def _unit(conn: sqlite3.Connection, unit_id: int) -> UnitRow:
    (unit,) = db.get_units_by_ids(conn, [unit_id])
    return unit


def _units_with(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> set[int]:
    """Ids of every unit of a chat whose ``msg_ids`` include ``msg_id``."""
    return {u.id for u in db.get_units(conn, chat_id) if u.id is not None and msg_id in u.msg_ids}


def _ids(matches: list[Match]) -> list[int]:
    return [m.unit_id for m in matches]


def _keys(hits: list[Hit]) -> list[tuple[int, tuple[int, ...]]]:
    """``(chat id, msg_ids)`` per hit — what identifies a unit across searches."""
    return [(h.chat.id, tuple(h.msg_ids)) for h in hits]


def _has(hits: list[Hit], chat_id: int, msg_id: int) -> bool:
    return any(h.chat.id == chat_id and msg_id in h.msg_ids for h in hits)


def _hit(score: float, msg_ids: list[int], chat: ChatRow = chat_ru.ARG) -> Hit:
    anchor = msg_ids[0] if msg_ids else 0
    return Hit(
        score=score,
        chat=chat,
        kind="window",
        date_start=0,
        date_end=0,
        anchor_msg_id=anchor,
        url=f"https://t.me/x/{anchor}",
        snippet="",
        msg_ids=list(msg_ids),
    )


# --- rrf -------------------------------------------------------------------------------------


def test_rrf_over_three_lists_by_hand() -> None:
    fused = search.rrf([[5, 1, 2], [1, 3], [2, 5, 4]], 60)
    assert fused == pytest.approx(
        {5: 1 / 61 + 1 / 62, 1: 1 / 62 + 1 / 61, 2: 1 / 63 + 1 / 61, 3: 1 / 62, 4: 1 / 63}
    )
    assert sorted(fused, key=lambda i: -fused[i]) == [5, 1, 2, 3, 4]
    assert search.rrf([[7], [7], [7]], 60)[7] == pytest.approx(RRF_MAX)
    assert search.rrf([[1, 2], [], [2, 1]], 0) == pytest.approx({1: 1.5, 2: 1.5})


# --- dedup -----------------------------------------------------------------------------------


def test_dedup_drops_a_hit_inside_a_better_one_and_keeps_the_higher_score() -> None:
    window = _hit(0.9, list(range(1, 9)))
    thread = _hit(0.5, [1, 2, 3])
    assert search.dedup([thread, window], 0.5) == [window]
    assert search.dedup([window, thread], 0.5) == [window]
    better_thread = _hit(0.9, [1, 2, 3])
    worse_window = _hit(0.5, list(range(1, 9)))
    assert search.dedup([worse_window, better_thread], 0.5) == [better_thread, worse_window]
    continuation = _hit(0.4, [1, 7, 10])
    assert search.dedup([window, continuation], 0.5) == [window]
    assert search.dedup([better_thread, continuation], 0.5) == [better_thread, continuation]


def test_dedup_threshold_is_inclusive_and_above_one_disables_it() -> None:
    a = _hit(0.9, [1, 2, 3, 4])
    b = _hit(0.8, [3, 4, 9, 10])
    inside = _hit(0.7, [3, 4])
    assert search.dedup([a, b], 0.5) == [a]
    assert search.dedup([a, b], 0.51) == [a, b]
    assert search.dedup([a, b, inside], 1.0) == [a, b]
    assert search.dedup([a, b, inside], 1.5) == [a, b, inside]


def test_dedup_compares_within_a_chat_and_against_kept_hits_only() -> None:
    arg = _hit(0.9, [1, 2, 3])
    geo = _hit(0.8, [1, 2, 3], chat=chat_ru.GEO)
    assert search.dedup([geo, arg], 0.5) == [arg, geo]
    a = _hit(0.9, [1, 2, 3, 4])
    b = _hit(0.8, [3, 4, 5, 6])
    c = _hit(0.7, [5, 6, 7, 8])
    assert search.dedup([a, b, c], 0.5) == [a, c]
    empty = _hit(0.5, [])
    assert search.dedup([a, empty], 0.5) == [a, empty]
    assert search.dedup([], 0.5) == []


def test_dedup_is_stable_on_ties() -> None:
    first = _hit(1.0, [1, 2])
    second = _hit(1.0, [10, 11])
    third = _hit(1.0, [20, 21])
    assert search.dedup([second, first, third], 0.5) == [second, first, third]
    assert search.dedup([third, first], 0.5) == [third, first]


def test_overlap_ratio_is_the_share_of_own_messages() -> None:
    window = _hit(0.9, list(range(1, 9)))
    thread = _hit(0.5, [1, 2, 3])
    assert search.overlap_ratio(thread, window) == 1.0
    assert search.overlap_ratio(window, thread) == pytest.approx(3 / 8)
    assert search.overlap_ratio(_hit(0.5, [1, 7, 10]), window) == pytest.approx(2 / 3)
    assert search.overlap_ratio(thread, _hit(0.5, [1, 2, 3], chat=chat_ru.GEO)) == 0.0
    assert search.overlap_ratio(_hit(0.5, []), window) == 0.0


# --- dense_units -----------------------------------------------------------------------------


def test_dense_units_score_by_cosine_similarity(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    matches = search.dense_units(conn, CFG, "Recoleta", ALL, 40, embedded)
    assert embedded.queries == ["Recoleta"]
    assert set(_ids(matches)) == _units_with(conn, ARG, 37)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 < score < 1.0 for score in scores)
    assert all(m.anchor_msg_id is None for m in matches)
    knn = index.knn(conn, embedded.embed_query("Recoleta"), ALL, 40, CFG.search.vec_fanout_max)
    assert [(m.unit_id, m.score) for m in matches] == [
        (unit_id, pytest.approx(1.0 - distance)) for unit_id, distance in knn if distance < 1.0
    ]
    assert len(knn) > len(matches)  # orthogonal units are not matches
    assert _ids(search.dense_units(conn, CFG, "Recoleta", ALL, 1, embedded)) == _ids(matches)[:1]
    assert search.dense_units(conn, CFG, "Recoleta", Filters(chat_ids={GEO}), 40, embedded) == []
    assert search.dense_units(conn, CFG, "Recoleta", Filters(chat_ids=set()), 40, embedded) == []
    assert search.dense_units(conn, CFG, "Recoleta", ALL, 0, embedded) == []
    assert search.dense_units(conn, CFG, "🙂", ALL, 40, embedded) == []


def test_dense_units_loads_the_configured_embedder_when_none_is_given(
    conn: sqlite3.Connection,
    loaded: chat_ru.Loaded,
    embedded: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given = _ids(search.dense_units(conn, CFG, "Recoleta", ALL, 40, embedded))
    assert _ids(search.dense_units(conn, CFG, "Recoleta", ALL, 40)) == given
    calls: list[Config] = []

    def loader(cfg: Config) -> FakeEmbedder:
        calls.append(cfg)
        return FakeEmbedder()

    monkeypatch.setattr(embed, "load_embedder", loader)
    assert _ids(search.dense_units(conn, CFG, "Recoleta", ALL, 40)) == given
    assert calls == [CFG]


def test_dense_units_unavailable_without_vectors(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(cfg: Config) -> FakeEmbedder:
        raise AssertionError("no model must load while there is nothing to search")

    monkeypatch.setattr(embed, "load_embedder", never)
    with pytest.raises(DenseUnavailable, match="no units are embedded yet"):
        search.dense_units(conn, CFG, "Recoleta", ALL, 40)
    db.ensure_vec_table(conn, FAKE_DIM)
    with pytest.raises(DenseUnavailable, match="no units are embedded yet"):
        search.dense_units(conn, CFG, "Recoleta", ALL, 40)


def test_dense_units_unavailable_without_a_model_or_in_another_space(
    conn: sqlite3.Connection,
    loaded: chat_ru.Loaded,
    embedded: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def offline(cfg: Config) -> FakeEmbedder:
        raise ModelUnavailable("torch is not installed")

    monkeypatch.setattr(embed, "load_embedder", offline)
    with pytest.raises(DenseUnavailable, match="torch is not installed") as info:
        search.dense_units(conn, CFG, "Recoleta", ALL, 40)
    assert isinstance(info.value.__cause__, ModelUnavailable)
    with pytest.raises(DenseUnavailable, match=r"fake \(8-d\).*--reembed"):
        search.dense_units(conn, CFG, "Recoleta", ALL, 40, FakeEmbedder(dim=8))
    with pytest.raises(DenseUnavailable, match="other-model") as info:
        search.dense_units(conn, CFG, "Recoleta", ALL, 40, OtherModel())
    assert isinstance(info.value.__cause__, EmbeddingSpaceMismatch)


# --- search: hybrid --------------------------------------------------------------------------


def test_hybrid_finds_the_paraphrase_lexical_misses(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    ru_units = _units_with(conn, PAIR.chat_id, PAIR.ru_msg_id)
    assert ru_units and not set(_ids(search.lexical_units(conn, PAIR.query_en, ALL, 40))) & ru_units
    lexical = search.search(conn, CFG, PAIR.query_en, ALL, mode="lexical")
    hybrid = search.search(conn, CFG, PAIR.query_en, ALL, mode="hybrid")
    assert lexical.warnings == [] and hybrid.warnings == []
    assert lexical.hits and not _has(lexical.hits, PAIR.chat_id, PAIR.ru_msg_id)
    assert _has(hybrid.hits, PAIR.chat_id, PAIR.ru_msg_id)
    found = next(
        h for h in hybrid.hits if h.chat.id == PAIR.chat_id and PAIR.ru_msg_id in h.msg_ids
    )
    assert "ВНЖ" in found.snippet and found.url == f"https://t.me/arg_chat/{found.anchor_msg_id}"
    assert hybrid.hits[0].score == 1.0

    en_only = {
        u
        for u in _units_with(conn, PAIR.chat_id, PAIR.en_msg_id)
        if PAIR.query_ru not in _unit(conn, u).text
    }
    assert en_only
    lexical_ru = search.search(conn, RAW, PAIR.query_ru, ALL, mode="lexical")
    hybrid_ru = search.search(conn, RAW, PAIR.query_ru, ALL, mode="hybrid")
    en_keys = {(PAIR.chat_id, tuple(_unit(conn, u).msg_ids)) for u in en_only}
    assert not en_keys & set(_keys(lexical_ru.hits))
    assert en_keys & set(_keys(hybrid_ru.hits))


def test_hybrid_scores_come_from_the_reranker_and_ties_keep_the_fused_order(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    raw = search.search(conn, RAW, PAIR.query_en, ALL, 40, rerank=False, embedder=embedded)
    reranked = search.search(conn, RAW, PAIR.query_en, ALL, 40, embedder=embedded)
    assert raw.warnings == [] and reranked.warnings == []
    assert raw.hits and all(0.0 < h.score <= RRF_MAX for h in raw.hits)
    assert [h.score for h in raw.hits] == sorted((h.score for h in raw.hits), reverse=True)
    assert set(_keys(raw.hits)) == set(_keys(reranked.hits))
    assert reranked.hits[0].score == 1.0
    assert [h.score for h in reranked.hits] == sorted(
        (h.score for h in reranked.hits), reverse=True
    )
    fake = FakeReranker()
    for hit in reranked.hits:
        unit = next(u for u in db.get_units(conn, hit.chat.id) if u.msg_ids == hit.msg_ids and u.id)
        assert hit.score == fake.score(PAIR.query_en, [unit.text])[0]
    top = [
        key
        for key, hit in zip(_keys(reranked.hits), reranked.hits, strict=True)
        if hit.score == 1.0
    ]
    assert len(top) > 1
    assert top == [key for key in _keys(raw.hits) if key in set(top)]


def test_rerank_false_skips_the_reranker(
    conn: sqlite3.Connection,
    loaded: chat_ru.Loaded,
    embedded: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(cfg: Config) -> FakeReranker:
        raise AssertionError("the reranker must not load")

    monkeypatch.setattr(reranking, "load_reranker", never)
    reranker = CountingReranker()
    off = search.search(conn, CFG, "Recoleta", ALL, rerank=False, reranker=reranker)
    assert reranker.calls == []
    assert off.hits and all(h.score <= RRF_MAX for h in off.hits)
    assert off.warnings == []
    on = search.search(conn, CFG, "Recoleta", ALL, reranker=reranker)
    ((query, texts),) = reranker.calls
    assert query == "Recoleta" and 0 < len(texts) <= CANDIDATES
    assert on.hits[0].score == 1.0
    assert _keys(on.hits) == _keys(off.hits) == [(ARG, tuple(range(35, 43)))]
    assert search.search(conn, CFG, "Recoleta", ALL, rerank=False).warnings == []


def test_reranker_is_loaded_from_config_and_degrades_with_a_warning(
    conn: sqlite3.Connection,
    loaded: chat_ru.Loaded,
    embedded: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_rerankers: list[CountingReranker] = []

    def loader(cfg: Config) -> CountingReranker:
        loaded_rerankers.append(CountingReranker())
        return loaded_rerankers[-1]

    monkeypatch.setattr(reranking, "load_reranker", loader)
    result = search.search(conn, CFG, "Recoleta", ALL)
    assert len(loaded_rerankers) == 1 and len(loaded_rerankers[0].calls) == 1
    assert result.hits[0].score == 1.0 and result.warnings == []

    def offline(cfg: Config) -> CountingReranker:
        raise ModelUnavailable("torch is not installed")

    monkeypatch.setattr(reranking, "load_reranker", offline)
    degraded = search.search(conn, CFG, "Recoleta", ALL)
    assert degraded.warnings == ["reranking unavailable: torch is not installed"]
    assert _keys(degraded.hits) == _keys(result.hits)
    assert all(h.score <= RRF_MAX for h in degraded.hits)
    with pytest.raises(RuntimeError, match="scores for"):
        search.search(conn, CFG, "Recoleta", ALL, reranker=ShortReranker())


def test_dense_unavailable_falls_back_to_lexical_with_a_warning(
    conn: sqlite3.Connection,
    loaded: chat_ru.Loaded,
    embedded: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical = search.search(conn, CFG, "DNI", ALL, mode="lexical")
    assert lexical.hits and lexical.warnings == []

    def offline(cfg: Config) -> FakeEmbedder:
        raise ModelUnavailable("torch is not installed")

    monkeypatch.setattr(embed, "load_embedder", offline)
    for mode in ("hybrid", "dense"):
        result = search.search(conn, CFG, "DNI", ALL, mode=mode)
        assert result.warnings == ["dense search unavailable: torch is not installed"]
        assert _keys(result.hits) == _keys(lexical.hits)
        assert [h.score for h in result.hits] == [h.score for h in lexical.hits]
    mismatched = search.search(conn, CFG, "DNI", ALL, embedder=FakeEmbedder(dim=8))
    assert len(mismatched.warnings) == 1
    assert mismatched.warnings[0].startswith("dense search unavailable: the dense index was built")
    assert "--reembed" in mismatched.warnings[0]
    assert _keys(mismatched.hits) == _keys(lexical.hits)


def test_hybrid_on_a_never_embedded_db_is_lexical_with_a_warning(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(cfg: Config) -> FakeEmbedder:
        raise AssertionError("no model must load while there is nothing to search")

    monkeypatch.setattr(embed, "load_embedder", never)
    lexical = search.search(conn, CFG, "DNI", ALL, mode="lexical")
    assert lexical.hits and lexical.warnings == []
    hybrid = search.search(conn, CFG, "DNI", ALL)
    assert hybrid.warnings == [NO_VECTORS]
    assert _keys(hybrid.hits) == _keys(lexical.hits)
    db.ensure_vec_table(conn, FAKE_DIM)
    dense = search.search(conn, CFG, "DNI", ALL, mode="dense")
    assert dense.warnings == [NO_VECTORS] and _keys(dense.hits) == _keys(lexical.hits)


def test_dense_mode_uses_only_the_dense_list(
    conn: sqlite3.Connection,
    loaded: chat_ru.Loaded,
    embedded: CountingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_lexical(*args: object, **kwargs: object) -> list[Match]:
        raise AssertionError("a lexical list was requested in dense mode")

    monkeypatch.setattr(search, "lexical_units", no_lexical)
    monkeypatch.setattr(search, "lexical_messages", no_lexical)
    result = search.search(conn, CFG, "Recoleta", ALL, mode="dense", embedder=embedded)
    assert result.warnings == []
    assert _keys(result.hits) == [(ARG, (35, 36, 37)), (ARG, tuple(range(35, 43)))]
    assert result.hits[0].anchor_msg_id == 37
    assert result.hits[0].url == "https://t.me/arg_chat/37"
    assert result.hits[0].score == 1.0
    empty = search.search(conn, CFG, "🙂", ALL, mode="dense", embedder=embedded)
    assert empty.hits == [] and empty.warnings == []
    assert embedded.queries == ["Recoleta", "🙂"]


def test_hybrid_query_without_words_searches_dense_only(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    result = search.search(conn, CFG, "🙂🙂 …", ALL, embedder=RecoletaEmbedder())
    assert result.warnings == [
        "query '🙂🙂 …' has no searchable words (letters or digits); "
        "only the dense index was searched"
    ]
    assert _keys(result.hits) == [(ARG, (35, 36, 37)), (ARG, tuple(range(35, 43)))]
    assert all(h.score == 0.0 for h in result.hits)  # the fake reranker has no terms to find
    lexical = search.search(conn, CFG, "🙂🙂 …", ALL, mode="lexical")
    assert lexical.hits == []
    assert lexical.warnings == ["query '🙂🙂 …' has no searchable words (letters or digits)"]


def test_hybrid_without_words_or_vectors_warns_about_both(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    result = search.search(conn, CFG, "🙂", ALL)
    assert result.hits == []
    assert result.warnings == [
        NO_VECTORS,
        "query '🙂' has no searchable words (letters or digits)",
    ]


def test_hybrid_applies_filters_to_both_sides(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    geo = search.search(conn, CFG, PAIR.query_en, Filters(chat_ids={GEO}), 40).hits
    assert geo and {h.chat.id for h in geo} == {GEO}
    since_june = Filters(chat_ids={ARG}, since=JUNE)
    june = search.search(conn, CFG, PAIR.query_en, since_june, 40).hits
    assert june and all(h.chat.id == ARG and h.date_start >= JUNE for h in june)
    assert _has(june, ARG, PAIR.ru_msg_id)
    assert search.lexical_units(conn, PAIR.query_en, since_june, 40) == []  # dense found it
    nothing = Filters(chat_ids={GEO}, since=JUNE)
    assert search.search(conn, CFG, PAIR.query_en, nothing, 40).hits == []


def test_search_returns_k_deduplicated_hits(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    hits = search.search(conn, CFG, "DNI", ALL, 40).hits
    assert len(hits) > 3
    for i, later in enumerate(hits):
        for earlier in hits[:i]:
            assert search.overlap_ratio(later, earlier) < CFG.search.dedup_overlap
    raw = search.search(conn, RAW, "DNI", ALL, 40).hits
    assert len(raw) > len(hits)
    kept = set(_keys(hits))
    assert _keys(hits) == [key for key in dict.fromkeys(_keys(raw)) if key in kept]
    assert len(search.search(conn, CFG, "DNI", ALL, 3).hits) == 3
    assert len(search.search(conn, CFG, "DNI", ALL, 1).hits) == 1
    assert search.search(conn, CFG, "DNI", ALL, 1).hits == hits[:1]


def test_search_default_mode_is_hybrid(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    default = search.search(conn, CFG, PAIR.query_en, ALL, embedder=embedded)
    hybrid = search.search(conn, CFG, PAIR.query_en, ALL, mode="hybrid", embedder=embedded)
    assert _keys(default.hits) == _keys(hybrid.hits)
    assert embedded.queries == [PAIR.query_en, PAIR.query_en]
    assert search.search(conn, CFG, PAIR.query_en, ALL, mode="lexical", embedder=embedded)
    assert embedded.queries == [PAIR.query_en, PAIR.query_en]  # lexical never embeds


# --- CLI -------------------------------------------------------------------------------------


@pytest.fixture
def seeded_home(tmp_home: Path) -> Path:
    conn = db.connect(Paths.from_env())
    db.migrate(conn)
    chat_ru.load(conn)
    conn.close()
    return tmp_home


@pytest.fixture
def embedded_home(seeded_home: Path) -> Path:
    result = runner.invoke(cli.app, ["embed"])
    assert result.exit_code == 0, result.output
    return seeded_home


def _hits(args: list[str]) -> tuple[list[dict[str, object]], list[str]]:
    result = runner.invoke(cli.app, ["search", *args, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    return payload["hits"], payload["warnings"]


def test_cli_search_defaults_to_hybrid_with_rerank(embedded_home: Path) -> None:
    hits, warnings = _hits([PAIR.query_en])
    assert warnings == []
    assert any(h["chat"]["id"] == ARG and PAIR.ru_msg_id in h["msg_ids"] for h in hits)
    assert hits[0]["score"] == 1.0
    lexical, warnings = _hits([PAIR.query_en, "--mode", "lexical"])
    assert warnings == [] and lexical
    assert not any(h["chat"]["id"] == ARG and PAIR.ru_msg_id in h["msg_ids"] for h in lexical)
    unranked, warnings = _hits([PAIR.query_en, "--no-rerank"])
    assert warnings == []
    assert all(0.0 < h["score"] <= RRF_MAX for h in unranked)
    assert {(h["chat"]["id"], tuple(h["msg_ids"])) for h in unranked} == {
        (h["chat"]["id"], tuple(h["msg_ids"])) for h in hits
    }
    dense, warnings = _hits(["Recoleta", "--mode", "dense"])
    assert warnings == []
    assert [h["anchor_msg_id"] for h in dense] == [37, 37]
    assert dense[0]["url"] == "https://t.me/arg_chat/37"


def test_cli_search_without_vectors_falls_back_and_warns(seeded_home: Path) -> None:
    result = runner.invoke(cli.app, ["search", "DNI"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("1. ")
    assert f"warning: {NO_VECTORS}" in result.stderr
    hits, warnings = _hits(["DNI"])
    assert hits and warnings == [NO_VECTORS]
    hits, warnings = _hits(["DNI", "--mode", "dense"])
    assert hits and warnings == [NO_VECTORS]
    hits, warnings = _hits(["DNI", "--mode", "lexical"])
    assert hits and warnings == []


def test_cli_search_without_the_model_falls_back_and_warns(
    embedded_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def offline(cfg: Config) -> FakeEmbedder:
        raise ModelUnavailable("torch is not installed")

    monkeypatch.setattr(embed, "load_embedder", offline)
    result = runner.invoke(cli.app, ["search", "DNI"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("1. ")
    assert "warning: dense search unavailable: torch is not installed" in result.stderr
    hits, warnings = _hits(["DNI"])
    assert hits and warnings == ["dense search unavailable: torch is not installed"]
