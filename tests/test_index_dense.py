import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grepogram import cli, db, embed, filters, index, sync, tg, units
from grepogram.embed import FAKE_DIM, FakeEmbedder, ModelUnavailable
from grepogram.index import EmbeddingSpaceMismatch
from grepogram.log import shutdown_logging
from grepogram.models import ChatRow, Config, Filters, MessageRow, Source, UnitRow, UnitsCfg
from grepogram.paths import Paths
from grepogram.sync import SyncBudget, SyncLock
from tests.fakes import FakeClient, make_channel, make_dialog, make_folder, make_user
from tests.fixtures import chat_ru, tl

ARG = chat_ru.ARG_ID
GEO = chat_ru.GEO_ID
CFG = chat_ru.CFG
ALL = Filters()
FANOUT = CFG.search.vec_fanout_max
JUNE = filters.parse_when("2024-06")

BASE = 1_705_314_600  # 2024-01-15 10:30:00 UTC
CHAT = -1001000000300
SYNC_CFG = Config(
    units=UnitsCfg(window_gap_min=30, window_max_msgs=5, window_max_chars=400, thread_max_msgs=4),
    sources=[Source(folder="Argentina")],
)
CONFIG = '[telegram]\napi_id = 12345\napi_hash = "fakehash"\n\n[[sources]]\nfolder = "Argentina"\n'

runner = CliRunner()


class CountingEmbedder(FakeEmbedder):
    """The fake, recording every slice of texts it embeds."""

    def __init__(self, dim: int = FAKE_DIM) -> None:
        super().__init__(dim=dim)
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return super().embed(texts)

    @property
    def embedded(self) -> list[str]:
        return [text for batch in self.batches for text in batch]


class OtherModel(FakeEmbedder):
    name = "other-model"


class ShortEmbedder(FakeEmbedder):
    """Answers one vector too few."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return super().embed(texts[:-1])


class Clock:
    """An injectable monotonic clock for :class:`SyncBudget`."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


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
def embedder() -> CountingEmbedder:
    return CountingEmbedder()


@pytest.fixture
def embedded(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedder: CountingEmbedder
) -> CountingEmbedder:
    """The fixture chats embedded with the fake."""
    index.embed_dirty_units(conn, embedder)
    embedder.batches.clear()
    return embedder


@pytest.fixture
def chat(conn: sqlite3.Connection) -> ChatRow:
    return db.upsert_chat(
        conn, ChatRow(id=CHAT, type="supergroup", title="Test chat", source_id="folder:Argentina")
    )


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths.under(tmp_path / "home")


def _msg(msg_id: int, minutes: int = 0, text: str = "", reply_to: int | None = None) -> MessageRow:
    return MessageRow(
        chat_id=CHAT,
        msg_id=msg_id,
        date=BASE + minutes * 60,
        from_id=1,
        from_name="Alice",
        reply_to_msg_id=reply_to,
        text=text,
    )


def _unit(msg_ids: list[int], text: str) -> UnitRow:
    return UnitRow(
        chat_id=CHAT,
        kind="window",
        msg_id_start=min(msg_ids),
        msg_id_end=max(msg_ids),
        msg_ids=msg_ids,
        date_start=BASE,
        date_end=BASE + 60,
        text=text,
    )


def _sync(conn: sqlite3.Connection, chat: ChatRow, rows: Iterable[MessageRow]) -> list[int]:
    """Store ``rows``, rebuild the units and index them, as one sync of ``chat`` does."""
    ids = db.upsert_messages(conn, rows)
    delta = units.rebuild_for_chat(conn, chat, CFG, ids)
    index.index_chat(conn, chat, ids, delta)
    return ids


def _vec_rowids(conn: sqlite3.Connection) -> list[int]:
    if not db.has_vec_table(conn):
        return []
    return sorted(int(row[0]) for row in conn.execute("SELECT rowid FROM unit_vec"))


def _unit_ids(conn: sqlite3.Connection) -> set[int]:
    return {int(row[0]) for row in conn.execute("SELECT id FROM units")}


def _units(conn: sqlite3.Connection) -> dict[int, UnitRow]:
    return {u.id: u for u in db.get_units_by_ids(conn, _unit_ids(conn)) if u.id is not None}


def _units_with(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> set[int]:
    """Ids of every unit of a chat whose ``msg_ids`` include ``msg_id``."""
    return {u.id for u in db.get_units(conn, chat_id) if u.id is not None and msg_id in u.msg_ids}


def _ids(hits: list[tuple[int, float]]) -> list[int]:
    return [rowid for rowid, _ in hits]


@contextmanager
def _knn_statements(conn: sqlite3.Connection) -> Iterator[list[str]]:
    """Collects the vec0 KNN statements (bound values expanded) run inside the block."""
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        yield seen
    finally:
        conn.set_trace_callback(None)
        seen[:] = [sql for sql in seen if "MATCH" in sql]


# --- ensure_embedding_space ------------------------------------------------------------------


def test_ensure_embedding_space_creates_the_table_and_records_the_space(
    conn: sqlite3.Connection,
) -> None:
    index.ensure_embedding_space(conn, FakeEmbedder())
    assert db.vec_dim(conn) == FAKE_DIM
    assert db.get_meta(conn, db.META_EMBED_MODEL) == "fake"
    assert db.get_meta(conn, db.META_EMBED_DIM) == str(FAKE_DIM)
    assert not conn.in_transaction


def test_ensure_embedding_space_keeps_a_matching_space(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    before = _vec_rowids(conn)
    index.ensure_embedding_space(conn, FakeEmbedder())
    assert _vec_rowids(conn) == before
    assert db.count_dirty_units(conn) == 0


def test_ensure_embedding_space_rejects_another_model_or_width(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    before = _vec_rowids(conn)
    with pytest.raises(EmbeddingSpaceMismatch, match=r"fake \(256-d\).*fake \(8-d\).*--reembed"):
        index.ensure_embedding_space(conn, FakeEmbedder(dim=8))
    with pytest.raises(EmbeddingSpaceMismatch, match="other-model"):
        index.ensure_embedding_space(conn, OtherModel())
    with pytest.raises(EmbeddingSpaceMismatch):
        index.embed_dirty_units(conn, OtherModel())
    assert _vec_rowids(conn) == before
    assert db.vec_dim(conn) == FAKE_DIM
    assert db.get_meta(conn, db.META_EMBED_MODEL) == "fake"
    assert db.count_dirty_units(conn) == 0


def test_ensure_embedding_space_rejects_a_table_of_another_width(conn: sqlite3.Connection) -> None:
    db.ensure_vec_table(conn, 4)
    with pytest.raises(EmbeddingSpaceMismatch, match=r"an unknown model \(4-d\)"):
        index.ensure_embedding_space(conn, FakeEmbedder())
    assert db.get_meta(conn, db.META_EMBED_MODEL) is None


def test_ensure_embedding_space_reembed_rebuilds_for_a_new_width(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    all_ids = _unit_ids(conn)
    small = FakeEmbedder(dim=8)
    index.ensure_embedding_space(conn, small, reembed=True)
    assert db.vec_dim(conn) == 8
    assert _vec_rowids(conn) == []
    assert db.count_dirty_units(conn) == len(all_ids)
    assert all(u.embedded_model is None for u in _units(conn).values())
    assert db.get_meta(conn, db.META_EMBED_DIM) == "8"
    assert index.embed_dirty_units(conn, small) == len(all_ids)
    assert _vec_rowids(conn) == sorted(all_ids)
    assert index.knn(conn, small.embed_query("ВНЖ"), ALL, 3, FANOUT) != []


def test_ensure_embedding_space_reembed_clears_a_matching_space_too(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    all_ids = _unit_ids(conn)
    index.ensure_embedding_space(conn, embedded, reembed=True)
    assert db.vec_dim(conn) == FAKE_DIM
    assert _vec_rowids(conn) == []
    assert db.count_dirty_units(conn) == len(all_ids)
    assert index.embed_dirty_units(conn, embedded) == len(all_ids)
    assert _vec_rowids(conn) == sorted(all_ids)


# --- embed_dirty_units -----------------------------------------------------------------------


def test_embed_dirty_units_embeds_every_unit_once(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedder: CountingEmbedder
) -> None:
    all_units = _units(conn)
    assert all(u.dirty and u.embedded_model is None for u in all_units.values())
    assert index.embed_dirty_units(conn, embedder) == len(all_units)
    assert _vec_rowids(conn) == sorted(all_units)
    assert embedder.embedded == [all_units[unit_id].text for unit_id in sorted(all_units)]
    after = _units(conn)
    assert all(not u.dirty and u.embedded_model == "fake" for u in after.values())
    assert db.count_dirty_units(conn) == 0
    embedder.batches.clear()
    assert index.embed_dirty_units(conn, embedder) == 0
    assert embedder.batches == []
    assert _vec_rowids(conn) == sorted(all_units)
    assert not conn.in_transaction


def test_embed_dirty_units_walks_the_units_in_slices(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedder: CountingEmbedder
) -> None:
    total = len(_unit_ids(conn))
    assert index.embed_dirty_units(conn, embedder, batch=10) == total
    assert [len(batch) for batch in embedder.batches] == [10, 10, total - 20]
    assert _vec_rowids(conn) == sorted(_unit_ids(conn))
    with pytest.raises(ValueError, match="batch"):
        index.embed_dirty_units(conn, embedder, batch=0)


def test_embed_dirty_units_stops_between_slices_when_the_budget_expires(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, monkeypatch: pytest.MonkeyPatch
) -> None:
    total = len(_unit_ids(conn))
    clock = Clock()
    embedder = CountingEmbedder()
    original = embedder.embed

    def slow_embed(texts: list[str]) -> list[list[float]]:
        clock.now += 20
        return original(texts)

    monkeypatch.setattr(embedder, "embed", slow_embed)
    budget = SyncBudget(10, clock=clock)
    assert index.embed_dirty_units(conn, embedder, batch=10, budget=budget) == 10
    assert len(_vec_rowids(conn)) == 10
    assert db.count_dirty_units(conn) == total - 10
    assert index.embed_dirty_units(conn, embedder, batch=10, budget=SyncBudget(0)) == 0
    embedder.batches.clear()
    assert index.embed_dirty_units(conn, embedder) == total - 10
    assert len(embedder.embedded) == total - 10
    assert db.count_dirty_units(conn) == 0
    assert _vec_rowids(conn) == sorted(_unit_ids(conn))


def test_embed_dirty_units_re_embeds_only_units_marked_dirty_again(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    all_units = _units(conn)
    target = min(all_units)
    db.mark_dirty(conn, [target])
    assert index.embed_dirty_units(conn, embedded) == 1
    assert embedded.batches == [[all_units[target].text]]
    assert _vec_rowids(conn) == sorted(all_units)
    assert db.count_dirty_units(conn) == 0


def test_embed_dirty_units_does_not_store_zero_vectors(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    embedder = FakeEmbedder()
    stamps = "2024 01 15 10 30"
    assert embedder.embed_query(stamps) == [0.0] * FAKE_DIM
    ids = db.insert_units(conn, [_unit([1], stamps), _unit([2], "hello world")])
    assert index.embed_dirty_units(conn, embedder) == 2
    assert _vec_rowids(conn) == [ids[1]]
    assert db.count_dirty_units(conn) == 0
    with db.transaction(conn):
        conn.execute("UPDATE units SET text = ? WHERE id = ?", (stamps, ids[1]))
    db.mark_dirty(conn, [ids[1]])
    assert index.embed_dirty_units(conn, embedder) == 1
    assert _vec_rowids(conn) == []
    assert index.knn(conn, embedder.embed_query("hello world"), ALL, 5, FANOUT) == []


def test_embed_dirty_units_rejects_a_short_answer(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    with pytest.raises(RuntimeError, match="vectors for"):
        index.embed_dirty_units(conn, ShortEmbedder())
    assert _vec_rowids(conn) == []
    assert db.count_dirty_units(conn) == len(_unit_ids(conn))
    assert not conn.in_transaction


# --- vectors follow the units ----------------------------------------------------------------


def test_recut_window_drops_the_old_vector_and_knn_stays_within_units(
    conn: sqlite3.Connection, chat: ChatRow, embedder: CountingEmbedder
) -> None:
    _sync(conn, chat, [_msg(1, 0, "где открыть счёт?"), _msg(2, 1, "в Galicia", reply_to=1)])
    assert index.embed_dirty_units(conn, embedder) == 2
    old_ids = _unit_ids(conn)
    assert _vec_rowids(conn) == sorted(old_ids)
    query = embedder.embed_query("счёт в Galicia")
    assert set(_ids(index.knn(conn, query, ALL, 10, FANOUT))) == old_ids

    _sync(conn, chat, [_msg(3, 2, "спасибо")])
    new_ids = _unit_ids(conn)
    recut = old_ids - new_ids
    assert len(recut) == 1
    assert {_units(conn)[unit_id].kind for unit_id in old_ids & new_ids} == {"thread"}
    assert set(_vec_rowids(conn)) == old_ids & new_ids
    assert set(_ids(index.knn(conn, query, ALL, 10, FANOUT))) == old_ids & new_ids

    assert index.embed_dirty_units(conn, embedder) == 1
    assert _vec_rowids(conn) == sorted(new_ids)
    hits = set(_ids(index.knn(conn, query, ALL, 10, FANOUT)))
    assert hits == new_ids
    assert hits & _units_with(conn, CHAT, 3)


def test_delete_chat_removes_the_chats_vectors(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    arg_ids = {u.id for u in db.get_units(conn, ARG)}
    query = embedded.embed_query("TBC Magti Tbilisi")
    assert {_units(conn)[r].chat_id for r in _ids(index.knn(conn, query, ALL, 3, FANOUT))} == {GEO}
    db.delete_chat(conn, GEO)
    assert set(_vec_rowids(conn)) == arg_ids
    hits = index.knn(conn, query, ALL, 10, FANOUT)
    assert hits and set(_ids(hits)) <= arg_ids


def test_delete_unit_vectors_with_vectors_present(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    all_ids = _unit_ids(conn)
    query = embedded.embed_query("ВНЖ")
    top = _ids(index.knn(conn, query, ALL, 3, FANOUT))
    index.delete_unit_vectors(conn, top[:2])
    assert set(_vec_rowids(conn)) == all_ids - set(top[:2])
    after = _ids(index.knn(conn, query, ALL, 3, FANOUT))
    assert after[0] == top[2]
    assert not set(top[:2]) & set(after)
    assert _unit_ids(conn) == all_ids


# --- knn -------------------------------------------------------------------------------------


def test_knn_is_empty_without_a_table_or_vectors(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    query = FakeEmbedder().embed_query("ВНЖ")
    assert index.knn(conn, query, ALL, 5, FANOUT) == []
    db.ensure_vec_table(conn, FAKE_DIM)
    assert index.knn(conn, query, ALL, 5, FANOUT) == []
    assert not conn.in_transaction


def test_knn_ranks_by_cosine_distance_and_bridges_the_paraphrase(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    pair = chat_ru.PARAPHRASE
    ru = index.knn(conn, embedded.embed_query(pair.query_ru), ALL, 5, FANOUT)
    en = index.knn(conn, embedded.embed_query(pair.query_en), ALL, 5, FANOUT)
    assert _ids(ru) == _ids(en)
    assert len(ru) == 5
    distances = [distance for _, distance in ru]
    assert distances == sorted(distances)
    assert all(0.0 <= distance <= 2.0 for distance in distances)
    assert set(_ids(ru)) <= _unit_ids(conn)
    arg_top = _ids(
        index.knn(conn, embedded.embed_query(pair.query_ru), Filters(chat_ids={ARG}), 3, FANOUT)
    )
    assert _units_with(conn, ARG, pair.en_msg_id) & set(arg_top)
    assert _units_with(conn, ARG, pair.ru_msg_id) & set(arg_top)


def test_knn_fans_out_per_chat_and_merges(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    all_units = _units(conn)
    query = embedded.embed_query("SIM card Claro")
    with _knn_statements(conn) as seen:
        hits = index.knn(conn, query, Filters(chat_ids={ARG, GEO}), 5, FANOUT)
    assert len(seen) == 2
    assert all("chat_id = " in sql and " k = 5" in sql for sql in seen)
    assert len(hits) == 5
    assert [d for _, d in hits] == sorted(d for _, d in hits)
    assert {all_units[r].chat_id for r in _ids(hits)} == {ARG, GEO}
    only_geo = _ids(index.knn(conn, query, Filters(chat_ids={GEO}), 5, FANOUT))
    assert all(all_units[r].chat_id == GEO for r in only_geo)
    geo_in_merge = [r for r in _ids(hits) if all_units[r].chat_id == GEO]
    assert geo_in_merge == only_geo[: len(geo_in_merge)]
    assert index.knn(conn, query, Filters(chat_ids={ARG, GEO}), 1, FANOUT) == hits[:1]


def test_knn_over_fetches_and_post_filters_beyond_the_fan_out(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    all_units = _units(conn)
    query = embedded.embed_query("SIM card Claro")
    with _knn_statements(conn) as seen:
        hits = index.knn(conn, query, Filters(chat_ids={ARG, GEO}), 5, fanout_max=1)
    assert len(seen) == 1
    assert "chat_id = " not in seen[0] and " k = 20" in seen[0]
    assert hits == index.knn(conn, query, ALL, 5, FANOUT)
    with _knn_statements(conn) as seen:
        geo = index.knn(conn, query, Filters(chat_ids={GEO}), 5, fanout_max=0)
    assert len(seen) == 1 and "chat_id = " not in seen[0] and " k = 20" in seen[0]
    assert geo and len(geo) <= 5
    assert all(all_units[r].chat_id == GEO for r in _ids(geo))
    with _knn_statements(conn) as seen:
        index.knn(conn, query, ALL, 5, FANOUT)
    assert len(seen) == 1
    assert "chat_id = " not in seen[0] and " k = 5" in seen[0]


@pytest.mark.parametrize(
    ("chat_ids", "fanout"),
    [(None, FANOUT), ({ARG, GEO}, FANOUT), ({ARG, GEO}, 1)],
    ids=["plain", "per-chat", "post-filter"],
)
def test_knn_applies_date_bounds_as_metadata_constraints(
    conn: sqlite3.Connection,
    loaded: chat_ru.Loaded,
    embedded: CountingEmbedder,
    chat_ids: set[int] | None,
    fanout: int,
) -> None:
    all_units = _units(conn)
    query = embedded.embed_query("Galicia счёт")
    with _knn_statements(conn) as seen:
        recent = index.knn(conn, query, Filters(chat_ids=chat_ids, since=JUNE), 5, fanout)
    assert seen and all(f"date_start >= {JUNE}" in sql for sql in seen)
    assert recent and all(all_units[r].date_start >= JUNE for r in _ids(recent))
    with _knn_statements(conn) as seen:
        older = index.knn(conn, query, Filters(chat_ids=chat_ids, until=JUNE - 1), 5, fanout)
    assert seen and all(f"date_start <= {JUNE - 1}" in sql for sql in seen)
    assert len(older) == 5 and all(all_units[r].date_start < JUNE for r in _ids(older))
    assert not set(_ids(recent)) & set(_ids(older))
    window = Filters(chat_ids=chat_ids, since=JUNE - 1, until=JUNE - 1)
    assert index.knn(conn, query, window, 5, fanout) == []


def test_knn_edge_cases(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, embedded: CountingEmbedder
) -> None:
    query = embedded.embed_query("ВНЖ")
    assert index.knn(conn, query, ALL, 0, FANOUT) == []
    assert index.knn(conn, query, Filters(chat_ids=set()), 5, FANOUT) == []
    assert index.knn(conn, [0.0] * FAKE_DIM, ALL, 5, FANOUT) == []
    with pytest.raises(EmbeddingSpaceMismatch, match="8 dimensions"):
        index.knn(conn, FakeEmbedder(dim=8).embed_query("ВНЖ"), ALL, 5, FANOUT)
    assert not conn.in_transaction


# --- wiring into sync ------------------------------------------------------------------------


ALICE = make_user(1, "Alice", "Liddell", username="alice")
BOB = make_user(2, "Bob")
ARG_CHANNEL = make_channel(1000000300, "Argentina chat", username="arg_chat", megagroup=True)


def _client(messages: list[object]) -> FakeClient:
    return FakeClient(
        dialogs=[make_dialog(ALICE), make_dialog(BOB), make_dialog(ARG_CHANNEL)],
        folders=[make_folder(3, "Argentina", include=[ARG_CHANNEL])],
        messages={CHAT: messages},  # type: ignore[dict-item]
        me=make_user(42, "Me"),
    )


async def _run(
    client: FakeClient,
    conn: sqlite3.Connection,
    paths: Paths,
    embedder: FakeEmbedder | None = None,
    budget: SyncBudget | None = None,
) -> sync.SyncReport:
    async with tg.connected(client):
        return await sync.sync_all(client, conn, SYNC_CFG, paths, budget or SyncBudget(), embedder)


async def test_sync_all_embeds_the_units_it_built(
    conn: sqlite3.Connection, paths: Paths, embedder: CountingEmbedder
) -> None:
    client = _client(
        [
            tl.message(CHAT, 1, "where do I open an account?", sender=1),
            tl.message(CHAT, 2, "try Galicia", sender=2, reply_to=tl.reply_header(1)),
            tl.message(CHAT, 3, "anyone around?", sender=2, date=tl.at(200)),
        ]
    )
    report = await _run(client, conn, paths, embedder)
    assert report.new == 3 and report.chats_done == [CHAT]
    assert report.warnings == []
    ids = _unit_ids(conn)
    assert ids and _vec_rowids(conn) == sorted(ids)
    assert db.count_dirty_units(conn) == 0
    assert len(embedder.embedded) == len(ids)
    assert db.get_meta(conn, db.META_EMBED_MODEL) == "fake"
    query = embedder.embed_query("open an account")
    assert _ids(index.knn(conn, query, ALL, 1, FANOUT))[0] in _units_with(conn, CHAT, 1)

    old_open = _units_with(conn, CHAT, 3)
    embedder.batches.clear()
    client.messages[CHAT].append(tl.message(CHAT, 4, "yes, still here", sender=1, date=tl.at(202)))
    report = await _run(client, conn, paths, embedder)
    assert report.new == 1 and report.warnings == []
    new_ids = _unit_ids(conn)
    assert not old_open & new_ids
    assert _vec_rowids(conn) == sorted(new_ids)
    fresh = new_ids - ids
    assert fresh == _units_with(conn, CHAT, 4)
    assert embedder.embedded == [u.text for u in db.get_units_by_ids(conn, fresh)]
    assert db.count_dirty_units(conn) == 0


async def test_sync_all_without_an_embedder_leaves_units_dirty(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    report = await _run(_client([tl.message(CHAT, 1, "hello", sender=1)]), conn, paths)
    assert report.new == 1 and report.warnings == []
    assert db.count_dirty_units(conn) == len(_unit_ids(conn)) > 0
    assert not db.has_vec_table(conn)


async def test_sync_all_reports_a_changed_model_as_a_warning(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client([tl.message(CHAT, 1, "hello", sender=1)])
    first = await _run(client, conn, paths, FakeEmbedder())
    assert first.warnings == []
    client.messages[CHAT].append(tl.message(CHAT, 2, "world", sender=2, date=tl.at(1)))
    report = await _run(client, conn, paths, FakeEmbedder(dim=8))
    assert report.new == 1 and report.chats_done == [CHAT]
    (warning,) = report.warnings
    assert warning.startswith("dense index not updated:") and "--reembed" in warning
    assert db.vec_dim(conn) == FAKE_DIM
    assert db.count_dirty_units(conn) > 0
    assert [m.text for m in db.get_messages(conn, CHAT)] == ["hello", "world"]


async def test_sync_all_passes_its_budget_on_and_warns_about_units_left_over(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def stub(
        c: sqlite3.Connection,
        embedder: FakeEmbedder,
        batch: int = index.EMBED_BATCH,
        budget: object = None,
    ) -> int:
        seen.append(budget)
        return 0

    monkeypatch.setattr(index, "embed_dirty_units", stub)
    budget = SyncBudget()
    client = _client([tl.message(CHAT, 1, "hello", sender=1)])
    report = await _run(client, conn, paths, FakeEmbedder(), budget)
    assert seen == [budget]
    (warning,) = report.warnings
    pending = db.count_dirty_units(conn)
    assert pending > 0
    assert warning.startswith(f"{pending} units are not embedded yet")
    assert "grepogram embed" in warning


# --- cli -------------------------------------------------------------------------------------


def _cli_db(tmp_home: Path) -> Paths:
    """The fixture chats loaded into the index under ``GREPOGRAM_HOME``."""
    paths = Paths.from_env()
    connection = cli._open_db(paths)
    try:
        chat_ru.load(connection)
    finally:
        connection.close()
    return paths


def _inspect(paths: Paths) -> tuple[int | None, int, int, int]:
    """``(vec_dim, dirty units, stored vectors, units)`` of the index on disk."""
    connection = db.connect(paths)
    try:
        return (
            db.vec_dim(connection),
            db.count_dirty_units(connection),
            len(_vec_rowids(connection)),
            len(_unit_ids(connection)),
        )
    finally:
        connection.close()


def test_cli_embed_embeds_dirty_units(tmp_home: Path) -> None:
    paths = _cli_db(tmp_home)
    total = _inspect(paths)[3]
    result = runner.invoke(cli.app, ["embed"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"embedded {total} units (fake, {FAKE_DIM}-d)"
    assert _inspect(paths) == (FAKE_DIM, 0, total, total)
    again = runner.invoke(cli.app, ["embed"])
    assert again.exit_code == 0, again.output
    assert again.stdout.strip() == f"dense index is up to date (fake, {FAKE_DIM}-d)"


def test_cli_embed_reembed_rebuilds_for_a_new_model(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _cli_db(tmp_home)
    total = _inspect(paths)[3]
    assert runner.invoke(cli.app, ["embed"]).exit_code == 0
    monkeypatch.setattr(embed, "load_embedder", lambda cfg: FakeEmbedder(dim=8))
    refused = runner.invoke(cli.app, ["embed"])
    assert refused.exit_code == 1
    assert "fake (8-d)" in refused.stderr and "--reembed" in refused.stderr
    assert _inspect(paths) == (FAKE_DIM, 0, total, total)
    rebuilt = runner.invoke(cli.app, ["embed", "--reembed"])
    assert rebuilt.exit_code == 0, rebuilt.output
    assert rebuilt.stdout.strip() == f"embedded {total} units (fake, 8-d)"
    assert _inspect(paths) == (8, 0, total, total)


def test_cli_embed_fails_without_the_model(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _cli_db(tmp_home)

    def unavailable(cfg: Config) -> FakeEmbedder:
        raise ModelUnavailable("torch is not installed")

    monkeypatch.setattr(embed, "load_embedder", unavailable)
    result = runner.invoke(cli.app, ["embed"])
    assert result.exit_code == 1
    assert "error: cannot load the embedding model: torch is not installed" in result.stderr
    assert _inspect(paths)[0] is None


def test_cli_embed_refuses_while_a_sync_runs(tmp_home: Path) -> None:
    paths = _cli_db(tmp_home)
    with SyncLock(paths):
        result = runner.invoke(cli.app, ["embed"])
    assert result.exit_code == 1
    assert "another sync is running" in result.stderr
    assert _inspect(paths)[0] is None


def test_cli_sync_embeds_and_only_warns_without_the_model(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths.from_env()
    paths.ensure_dirs()
    paths.config_file.write_text(CONFIG)
    paths.session_file.touch()
    client = _client([tl.message(CHAT, 1, "hello", sender=1)])
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: client)
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "new messages: 1" in result.stdout
    assert "warning" not in result.stderr
    assert _inspect(paths) == (FAKE_DIM, 0, 1, 1)

    def unavailable(cfg: Config) -> FakeEmbedder:
        raise ModelUnavailable("torch is not installed")

    monkeypatch.setattr(embed, "load_embedder", unavailable)
    client.messages[CHAT].append(tl.message(CHAT, 2, "world", sender=2, date=tl.at(1)))
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "new messages: 1" in result.stdout
    assert "warning: dense index not updated: torch is not installed" in result.stderr
    assert _inspect(paths) == (FAKE_DIM, 1, 0, 1)
