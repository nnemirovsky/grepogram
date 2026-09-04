import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
import sqlite_vec

from grepogram import db, index, stem, sync, tg, units
from grepogram.models import ChatRow, Config, MessageRow, Source, SyncReport, UnitRow, UnitsCfg
from grepogram.paths import Paths
from grepogram.sync import SyncBudget
from grepogram.units import UnitDelta
from tests.fakes import FakeClient, make_channel, make_dialog, make_folder, make_user
from tests.fixtures import tl

BASE = 1_705_314_600  # 2024-01-15 10:30:00 UTC
CHAT = -1000000000100
OTHER = -1000000000101
UNITS = UnitsCfg(window_gap_min=30, window_max_msgs=5, window_max_chars=400, thread_max_msgs=4)
FOLDER = Source(folder="Argentina")
CFG = Config(units=UNITS, sources=[FOLDER])

FtsRow = tuple[str, str, int, int]


def _msg(
    msg_id: int,
    minutes: int = 0,
    text: str | None = None,
    chat_id: int = CHAT,
    reply_to: int | None = None,
    **overrides: object,
) -> MessageRow:
    fields: dict[str, object] = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "date": BASE + minutes * 60,
        "from_id": 1,
        "from_name": "Alice",
        "reply_to_msg_id": reply_to,
        "text": f"message {msg_id}" if text is None else text,
    }
    fields.update(overrides)
    return MessageRow(**fields)  # type: ignore[arg-type]


def _chat(chat_id: int = CHAT, **overrides: object) -> ChatRow:
    fields: dict[str, object] = {
        "id": chat_id,
        "type": "supergroup",
        "title": f"chat {chat_id}",
        "source_id": FOLDER.id,
    }
    fields.update(overrides)
    return ChatRow(**fields)  # type: ignore[arg-type]


def _unit(msg_ids: list[int], text: str, chat_id: int = CHAT, date_start: int = BASE) -> UnitRow:
    return UnitRow(
        chat_id=chat_id,
        kind="window",
        msg_id_start=min(msg_ids),
        msg_id_end=max(msg_ids),
        msg_ids=msg_ids,
        date_start=date_start,
        date_end=date_start + 60,
        text=text,
    )


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def chat(conn: sqlite3.Connection) -> ChatRow:
    return db.upsert_chat(conn, _chat())


@pytest.fixture
def other(conn: sqlite3.Connection) -> ChatRow:
    return db.upsert_chat(conn, _chat(OTHER))


def _rows(conn: sqlite3.Connection, table: str) -> dict[int, FtsRow]:
    """``rowid → (raw, stemmed, chat_id, date)`` of every row in an FTS table."""
    date_col = "date" if table == "msg_fts" else "date_start"
    rows = conn.execute(f"SELECT rowid, raw, stemmed, chat_id, {date_col} AS d FROM {table}")
    return {
        int(row["rowid"]): (row["raw"], row["stemmed"], int(row["chat_id"]), int(row["d"]))
        for row in rows
    }


def _match(
    conn: sqlite3.Connection,
    table: str,
    query: str | None,
    chat_id: int | None = None,
    since: int | None = None,
    until: int | None = None,
) -> list[int]:
    """Rowids matching ``query``, filtered on the UNINDEXED columns with plain predicates."""
    assert query is not None
    date_col = "date" if table == "msg_fts" else "date_start"
    sql = f"SELECT rowid FROM {table} WHERE {table} MATCH ?"
    params: list[object] = [query]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params.append(chat_id)
    if since is not None:
        sql += f" AND {date_col} >= ?"
        params.append(since)
    if until is not None:
        sql += f" AND {date_col} <= ?"
        params.append(until)
    return sorted(int(row[0]) for row in conn.execute(sql, params))


def _plan(conn: sqlite3.Connection, sql: str) -> str:
    return " | ".join(str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", (1,)))


def _ids(conn: sqlite3.Connection, sql: str) -> set[int]:
    return {int(row[0]) for row in conn.execute(sql)}


def _assert_in_step(conn: sqlite3.Connection) -> None:
    """The FTS tables mirror their parents: every unit, every message with text, nothing else."""
    assert set(_rows(conn, "unit_fts")) == _ids(conn, "SELECT id FROM units")
    assert set(_rows(conn, "msg_fts")) == _ids(
        conn, "SELECT id FROM messages WHERE trim(text) != ''"
    )


def _vec(conn: sqlite3.Connection, rowid: int, chat_id: int = CHAT) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (?, ?, ?, ?)",
            (rowid, chat_id, BASE, sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0])),
        )


def _vec_rowids(conn: sqlite3.Connection) -> list[int]:
    return sorted(_ids(conn, "SELECT rowid FROM unit_vec"))


# --- index_messages --------------------------------------------------------------------------


def test_index_messages_keys_rows_by_message_id(conn: sqlite3.Connection, chat: ChatRow) -> None:
    russian = "Открыл счёт в банке Galicia"
    ids = db.upsert_messages(
        conn, [_msg(1, text=russian), _msg(2, 1, text="Try Galicia, accounts are free")]
    )
    assert index.index_messages(conn, ids) == 2
    assert _rows(conn, "msg_fts") == {
        ids[0]: (russian, "откр счет в банк galicia", CHAT, BASE),
        ids[1]: ("Try Galicia, accounts are free", "tri galicia account are free", CHAT, BASE + 60),
    }
    assert _rows(conn, "msg_fts")[ids[0]][1] == stem.stem_text(russian)


def test_index_messages_skips_messages_without_text(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    ids = db.upsert_messages(
        conn,
        [
            _msg(1, text="", media_kind="photo"),
            _msg(2, 1, text="   \n"),
            _msg(3, 2, text="a caption", media_kind="photo"),
        ],
    )
    assert index.index_messages(conn, ids) == 1
    assert set(_rows(conn, "msg_fts")) == {ids[2]}


def test_index_messages_with_nothing_to_index(conn: sqlite3.Connection, chat: ChatRow) -> None:
    assert index.index_messages(conn, []) == 0
    assert index.index_messages(conn, [999]) == 0
    assert _rows(conn, "msg_fts") == {}
    assert not conn.in_transaction


def test_stemmed_match_finds_inflected_forms(conn: sqlite3.Connection, chat: ChatRow) -> None:
    ids = db.upsert_messages(
        conn, [_msg(1, text="Открыл счёт в банке Galicia"), _msg(2, 1, text="Two accounts opened")]
    )
    index.index_messages(conn, ids)
    assert _match(conn, "msg_fts", stem.fts_query("счета банков")) == [ids[0]]
    assert _match(conn, "msg_fts", stem.fts_query("account")) == [ids[1]]
    assert _match(conn, "msg_fts", stem.fts_query("счета банков accounts", "OR")) == sorted(ids)
    assert _match(conn, "msg_fts", stem.fts_query("visa")) == []


def test_raw_column_keeps_exact_spelling(conn: sqlite3.Connection, chat: ChatRow) -> None:
    (rowid,) = db.upsert_messages(conn, [_msg(1, text="счёт в Galicia")])
    index.index_messages(conn, [rowid])
    assert _match(conn, "msg_fts", '"счёт"') == [rowid]
    assert _match(conn, "msg_fts", 'raw : "galicia"') == [rowid]
    assert _match(conn, "msg_fts", 'raw : "счет"') == []


def test_chat_and_date_filters_are_plain_predicates(
    conn: sqlite3.Connection, chat: ChatRow, other: ChatRow
) -> None:
    ids = db.upsert_messages(
        conn, [_msg(1, 0, text="visa run"), _msg(1, 90, text="visa run", chat_id=OTHER)]
    )
    index.index_messages(conn, ids)
    query = stem.fts_query("visas")
    assert _match(conn, "msg_fts", query) == sorted(ids)
    assert _match(conn, "msg_fts", query, chat_id=CHAT) == [ids[0]]
    assert _match(conn, "msg_fts", query, chat_id=OTHER) == [ids[1]]
    assert _match(conn, "msg_fts", query, since=BASE + 1) == [ids[1]]
    assert _match(conn, "msg_fts", query, until=BASE) == [ids[0]]
    assert _match(conn, "msg_fts", query, chat_id=CHAT, since=BASE + 1) == []


def test_reindex_is_idempotent(conn: sqlite3.Connection, chat: ChatRow) -> None:
    ids = db.upsert_messages(conn, [_msg(1, text="hello"), _msg(2, 1, text="world")])
    index.index_messages(conn, ids)
    before = _rows(conn, "msg_fts")
    assert index.index_messages(conn, ids) == 2
    assert index.index_messages(conn, [*ids, *ids]) == 2
    assert _rows(conn, "msg_fts") == before
    assert conn.execute("SELECT count(*) FROM msg_fts").fetchone()[0] == 2


def test_edit_replaces_the_row_under_the_same_rowid(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    (rowid,) = db.upsert_messages(conn, [_msg(1, text="opened an account")])
    index.index_messages(conn, [rowid])
    edited = _msg(1, text="closed the account", edit_date=BASE + 5)
    assert db.upsert_messages(conn, [edited]) == [rowid]
    index.index_messages(conn, [rowid])
    assert _match(conn, "msg_fts", stem.fts_query("opened")) == []
    assert _match(conn, "msg_fts", stem.fts_query("closed")) == [rowid]
    assert list(_rows(conn, "msg_fts")) == [rowid]
    db.upsert_messages(conn, [_msg(1, text="", media_kind="photo", edit_date=BASE + 9)])
    index.index_messages(conn, [rowid])
    assert _rows(conn, "msg_fts") == {}


def test_index_messages_batches_beyond_the_in_limit(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    count = db.IN_BATCH + 100
    ids = db.upsert_messages(conn, [_msg(i, text=f"message {i}") for i in range(1, count + 1)])
    assert index.index_messages(conn, ids) == count
    assert set(_rows(conn, "msg_fts")) == set(ids)
    assert _match(conn, "msg_fts", stem.fts_query(f"message {count}")) == [ids[-1]]


@pytest.mark.parametrize("table", ["msg_fts", "unit_fts"])
def test_delete_by_rowid_is_a_lookup_not_a_scan(conn: sqlite3.Connection, table: str) -> None:
    lookup = _plan(conn, f"DELETE FROM {table} WHERE rowid = ?")
    scan = _plan(conn, f"DELETE FROM {table} WHERE chat_id = ?")
    assert lookup.endswith("VIRTUAL TABLE INDEX 0:=")
    assert scan.endswith("VIRTUAL TABLE INDEX 0:")


# --- index_units -----------------------------------------------------------------------------


def test_index_units_indexes_inserted_and_drops_deleted(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    first = "[2024-01-15 10:30] Alice: открыли счета"
    second = "[2024-01-15 11:30] Bob: visas"
    ids = db.insert_units(conn, [_unit([1, 2], first), _unit([3], second, date_start=BASE + 3600)])
    assert index.index_units(conn, UnitDelta(inserted_ids=ids)) == 2
    assert _rows(conn, "unit_fts") == {
        ids[0]: (first, stem.stem_text(first), CHAT, BASE),
        ids[1]: (second, "2024 01 15 11 30 bob visa", CHAT, BASE + 3600),
    }
    assert _match(conn, "unit_fts", stem.fts_query("счёт")) == [ids[0]]
    assert _match(conn, "unit_fts", stem.fts_query("visa"), since=BASE + 1) == [ids[1]]
    db.delete_units(conn, [ids[0]])
    assert index.index_units(conn, UnitDelta(deleted_ids=[ids[0]])) == 0
    assert set(_rows(conn, "unit_fts")) == {ids[1]}
    assert _match(conn, "unit_fts", stem.fts_query("счёт")) == []


def test_index_units_is_idempotent_and_skips_unknown_ids(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    ids = db.insert_units(conn, [_unit([1], "one")])
    delta = UnitDelta(inserted_ids=[*ids, 999], deleted_ids=[998])
    assert index.index_units(conn, delta) == 1
    before = _rows(conn, "unit_fts")
    assert index.index_units(conn, delta) == 1
    assert _rows(conn, "unit_fts") == before
    assert index.index_units(conn, UnitDelta()) == 0
    assert not conn.in_transaction


# --- delete_unit_vectors ---------------------------------------------------------------------


def test_delete_unit_vectors_is_a_noop_without_the_table(conn: sqlite3.Connection) -> None:
    index.delete_unit_vectors(conn, [1, 2])
    assert not db.has_vec_table(conn)
    assert not conn.in_transaction


def test_delete_unit_vectors_drops_rows_by_rowid(conn: sqlite3.Connection) -> None:
    db.ensure_vec_table(conn, 4)
    for rowid in (1, 2, 3):
        _vec(conn, rowid)
    index.delete_unit_vectors(conn, [1, 3, 3, 999])
    assert _vec_rowids(conn) == [2]
    index.delete_unit_vectors(conn, [])
    assert _vec_rowids(conn) == [2]
    assert not conn.in_transaction


def test_index_units_drops_vectors_of_deleted_units_only(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    db.ensure_vec_table(conn, 4)
    ids = db.insert_units(conn, [_unit([1], "one"), _unit([2], "two")])
    index.index_units(conn, UnitDelta(inserted_ids=ids))
    for unit_id in ids:
        _vec(conn, unit_id)
    db.delete_units(conn, [ids[0]])
    new = db.insert_units(conn, [_unit([1, 3], "one three")])
    index.index_units(conn, UnitDelta(inserted_ids=new, deleted_ids=[ids[0]]))
    assert _vec_rowids(conn) == [ids[1]]
    assert set(_rows(conn, "unit_fts")) == {ids[1], *new}


# --- index_chat with the unit rebuild --------------------------------------------------------


def _sync(
    conn: sqlite3.Connection, chat: ChatRow, rows: Iterable[MessageRow], cfg: Config = CFG
) -> list[int]:
    """Store ``rows``, rebuild the units and index both, as one sync of ``chat`` does."""
    ids = db.upsert_messages(conn, rows)
    delta = units.rebuild_for_chat(conn, chat, cfg, ids)
    index.index_chat(conn, chat, ids, delta)
    return ids


def _unit_ids(conn: sqlite3.Connection, chat_id: int, kind: str) -> dict[tuple[int, ...], int]:
    return {tuple(u.msg_ids): u.id for u in db.get_units(conn, chat_id) if u.kind == kind and u.id}


def test_index_chat_keeps_fts_in_step_with_the_rebuild(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    ids = _sync(
        conn,
        chat,
        [
            _msg(1, 0, text="где открыть счёт?"),
            _msg(2, 1, text="в Galicia", reply_to=1),
            _msg(3, 2, text="", media_kind="photo"),
        ],
    )
    _assert_in_step(conn)
    old_window = _unit_ids(conn, CHAT, "window")[(1, 2, 3)]
    thread = _unit_ids(conn, CHAT, "thread")[(1, 2)]
    assert _match(conn, "unit_fts", stem.fts_query("счета")) == sorted([old_window, thread])
    assert _match(conn, "unit_fts", stem.fts_query("photo")) == [old_window]
    assert _match(conn, "msg_fts", stem.fts_query("счета")) == [ids[0]]

    _sync(conn, chat, [_msg(4, 3, text="спасибо")])
    _assert_in_step(conn)
    new_window = _unit_ids(conn, CHAT, "window")[(1, 2, 3, 4)]
    assert old_window not in _rows(conn, "unit_fts")
    assert _match(conn, "unit_fts", stem.fts_query("спасибо")) == [new_window]
    assert _unit_ids(conn, CHAT, "thread")[(1, 2)] == thread

    edited = _msg(2, 1, text="в Galicia или Santander", reply_to=1, edit_date=BASE + 100)
    assert _sync(conn, chat, [edited]) == [ids[1]]
    _assert_in_step(conn)
    assert _match(conn, "msg_fts", stem.fts_query("santander")) == [ids[1]]
    assert _match(conn, "unit_fts", stem.fts_query("santander")) == sorted(
        _unit_ids(conn, CHAT, kind)[key]
        for kind, key in (("window", (1, 2, 3, 4)), ("thread", (1, 2)))
    )
    assert thread not in _rows(conn, "unit_fts")


def test_index_chat_rolls_back_as_a_whole(
    conn: sqlite3.Connection, chat: ChatRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = db.upsert_messages(conn, [_msg(1, text="hello")])
    delta = units.rebuild_for_chat(conn, chat, CFG, ids)

    def explode(*_: object) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(index, "index_units", explode)
    with pytest.raises(RuntimeError, match="boom"):
        index.index_chat(conn, chat, ids, delta)
    assert not conn.in_transaction
    assert _rows(conn, "msg_fts") == {}


# --- repairing a torn index ------------------------------------------------------------------


def _orphan(conn: sqlite3.Connection, rowid: int, chat_id: int = CHAT) -> None:
    """An ``unit_fts`` row for a unit that does not exist, the way a torn rebuild leaves one."""
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO unit_fts(rowid, raw, stemmed, chat_id, date_start) VALUES (?, ?, ?, ?, ?)",
            (rowid, "ghost text", "ghost text", chat_id, BASE),
        )


def _drop_fts(conn: sqlite3.Connection, rowid: int) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM unit_fts WHERE rowid = ?", (rowid,))


def test_unit_index_gaps_reports_both_directions_per_chat(
    conn: sqlite3.Connection, chat: ChatRow, other: ChatRow
) -> None:
    _sync(conn, chat, [_msg(1, text="hello")])
    _sync(conn, other, [_msg(1, text="hola", chat_id=OTHER)])
    assert index.unit_index_gaps(conn, CHAT) == index.IndexGaps()
    assert not index.unit_index_gaps(conn, CHAT)

    (unit,) = db.get_units(conn, CHAT)
    assert unit.id is not None
    _drop_fts(conn, unit.id)
    _orphan(conn, 9001)
    _orphan(conn, 9002, chat_id=OTHER)
    gaps = index.unit_index_gaps(conn, CHAT)
    assert gaps == index.IndexGaps(missing=[unit.id], orphans=[9001])
    assert bool(gaps)
    assert index.unit_index_gaps(conn, OTHER) == index.IndexGaps(orphans=[9002])


def test_repair_unit_index_restores_missing_rows_and_drops_orphans(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    db.ensure_vec_table(conn, 4)
    _sync(conn, chat, [_msg(1, text="открыть счёт в Galicia")])
    (unit,) = db.get_units(conn, CHAT)
    assert unit.id is not None
    _drop_fts(conn, unit.id)
    _orphan(conn, 9001)
    _vec(conn, 9001)

    assert index.repair_unit_index(conn, CHAT) == index.IndexGaps(missing=[unit.id], orphans=[9001])
    _assert_in_step(conn)
    assert _match(conn, "unit_fts", stem.fts_query("счета")) == [unit.id]
    assert _vec_rowids(conn) == []
    assert index.repair_unit_index(conn, CHAT) == index.IndexGaps()


def test_a_rebuild_killed_before_the_index_is_repaired_by_the_next_run(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    """The old sequence committed the rebuild and the index apart; a rerun must close the gap.

    The rerun's rebuild re-cuts identical units and reports an empty delta, so only the check
    against the stored units restores the coverage the killed run never wrote.
    """
    rows = [_msg(i, i, text=f"message {i} about durability") for i in range(1, 8)]
    db.upsert_messages(conn, rows)
    pending = db.unindexed_message_ids(conn, CHAT)
    units.rebuild_for_chat(conn, chat, CFG, pending)  # the rebuild commit, then the process dies
    assert db.get_units(conn, CHAT) and _rows(conn, "unit_fts") == {}
    assert db.unindexed_message_ids(conn, CHAT) == pending

    sync.on_chat_synced(conn, chat, CFG, db.unindexed_message_ids(conn, CHAT))
    _assert_in_step(conn)
    assert db.unindexed_message_ids(conn, CHAT) == []
    assert _match(conn, "unit_fts", stem.fts_query("durability")) == sorted(
        u.id for u in db.get_units(conn, CHAT) if u.id is not None
    )


def test_a_killed_incremental_rebuild_leaves_no_orphan_behind(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    _sync(conn, chat, [_msg(1, text="alpha")])
    (before,) = db.get_units(conn, CHAT)

    db.upsert_messages(conn, [_msg(2, 1, text="bravocode")])
    units.rebuild_for_chat(conn, chat, CFG, db.unindexed_message_ids(conn, CHAT))
    assert before.id not in {u.id for u in db.get_units(conn, CHAT)}
    assert index.unit_index_gaps(conn, CHAT).orphans == [before.id]

    sync.on_chat_synced(conn, chat, CFG, db.unindexed_message_ids(conn, CHAT))
    _assert_in_step(conn)
    assert index.unit_index_gaps(conn, CHAT) == index.IndexGaps()
    assert _match(conn, "unit_fts", stem.fts_query("bravocode")) == [
        _unit_ids(conn, CHAT, "window")[(1, 2)]
    ]


# --- wiring into sync ------------------------------------------------------------------------


def test_on_chat_synced_rebuilds_then_indexes_with_the_delta(
    conn: sqlite3.Connection, chat: ChatRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, object]] = []
    delta = UnitDelta(inserted_ids=[7], deleted_ids=[3])

    def fake_rebuild(
        c: sqlite3.Connection, ch: ChatRow, cfg: Config, ids: Iterable[int]
    ) -> UnitDelta:
        calls.append(("rebuild", (ch.id, cfg, list(ids))))
        return delta

    def fake_index(c: sqlite3.Connection, ch: ChatRow, ids: Iterable[int], d: UnitDelta) -> None:
        calls.append(("index", (ch.id, list(ids), d)))

    monkeypatch.setattr(units, "rebuild_for_chat", fake_rebuild)
    monkeypatch.setattr(index, "index_chat", fake_index)
    sync.on_chat_synced(conn, chat, CFG, [11, 12])
    assert calls == [("rebuild", (CHAT, CFG, [11, 12])), ("index", (CHAT, [11, 12], delta))]


def test_on_chat_synced_is_one_transaction(
    conn: sqlite3.Connection, chat: ChatRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rebuild, the indexing and the flag commit together, so nothing half-derived survives
    a run that dies in between and the next run picks the rows up again."""
    ids = db.upsert_messages(conn, [_msg(1, text="hello")])

    def explode(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(index, "index_chat", explode)
    with pytest.raises(RuntimeError, match="boom"):
        sync.on_chat_synced(conn, chat, CFG, ids)
    assert not conn.in_transaction
    assert db.get_units(conn, CHAT) == []
    assert db.unindexed_message_ids(conn, CHAT) == ids

    monkeypatch.undo()
    sync.on_chat_synced(conn, chat, CFG, ids)
    _assert_in_step(conn)
    assert db.unindexed_message_ids(conn, CHAT) == []


def test_on_chat_synced_populates_both_indexes(conn: sqlite3.Connection, chat: ChatRow) -> None:
    ids = db.upsert_messages(
        conn, [_msg(1, text="SIM карта Claro"), _msg(2, 1, text="Movistar лучше", reply_to=1)]
    )
    sync.on_chat_synced(conn, chat, CFG, ids)
    _assert_in_step(conn)
    assert _match(conn, "msg_fts", stem.fts_query("карты claro")) == [ids[0]]
    assert _match(conn, "unit_fts", stem.fts_query("movistar")) == sorted(
        u.id for u in db.get_units(conn, CHAT) if u.id
    )


ALICE = make_user(1, "Alice", "Liddell", username="alice")
BOB = make_user(2, "Bob")
ARG = make_channel(100, "Argentina chat", username="arg_chat", megagroup=True)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths.under(tmp_path / "home")


def _client(messages: list[object]) -> FakeClient:
    return FakeClient(
        dialogs=[make_dialog(ALICE), make_dialog(BOB), make_dialog(ARG)],
        folders=[make_folder(3, "Argentina", include=[ARG])],
        messages={CHAT: messages},
        me=make_user(42, "Me"),
    )


async def _run(client: FakeClient, conn: sqlite3.Connection, paths: Paths) -> SyncReport:
    async with tg.connected(client):
        return await sync.sync_all(client, conn, CFG, paths, SyncBudget())


async def test_sync_all_maintains_the_lexical_index(conn: sqlite3.Connection, paths: Paths) -> None:
    client = _client(
        [
            tl.message(CHAT, 1, "where do I open an account?", sender=1),
            tl.message(CHAT, 2, "try Galicia", sender=2, reply_to=tl.reply_header(1)),
            tl.message(CHAT, 3, "anyone around?", sender=2, date=tl.at(200)),
        ]
    )
    report = await _run(client, conn, paths)
    assert report.new == 3 and report.chats_done == [CHAT]
    _assert_in_step(conn)
    (hit,) = _match(conn, "msg_fts", stem.fts_query("accounts"))
    assert db.get_messages_by_ids(conn, [hit])[0].msg_id == 1
    assert _match(conn, "unit_fts", stem.fts_query("galicia"), chat_id=CHAT) == sorted(
        [_unit_ids(conn, CHAT, "window")[(1, 2)], _unit_ids(conn, CHAT, "thread")[(1, 2)]]
    )
    old_open = _unit_ids(conn, CHAT, "window")[(3,)]

    client.messages[CHAT].append(tl.message(CHAT, 4, "yes, still here", sender=1, date=tl.at(202)))
    report = await _run(client, conn, paths)
    assert report.new == 1
    _assert_in_step(conn)
    assert old_open not in _rows(conn, "unit_fts")
    assert _match(conn, "unit_fts", stem.fts_query("still here")) == [
        _unit_ids(conn, CHAT, "window")[(3, 4)]
    ]
    assert _match(conn, "msg_fts", stem.fts_query("here"), since=int(tl.at(202).timestamp())) == [
        db.get_messages_by_msg_id(conn, CHAT, [4])[4].id
    ]
