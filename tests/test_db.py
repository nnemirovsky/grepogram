import sqlite3
import threading
from pathlib import Path

import pytest
import sqlite_vec

from grepogram import db
from grepogram.models import ChatRow, MessageRow, UserRow
from grepogram.paths import Paths

TABLES = {"meta", "chats", "users", "messages", "units", "msg_fts", "unit_fts"}
INDEXES = {"messages_chat_date", "messages_reply", "units_chat_kind_range"}


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


def _names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {row["name"] for row in rows}


def _schema(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
    return [(row["type"], row["name"], row["sql"]) for row in rows]


def _chat(chat_id: int = 1, **overrides: object) -> ChatRow:
    fields: dict[str, object] = {
        "id": chat_id,
        "type": "supergroup",
        "title": f"chat {chat_id}",
        "source_id": "folder:Test",
    }
    fields.update(overrides)
    return ChatRow(**fields)  # type: ignore[arg-type]


def _message(chat_id: int, msg_id: int, **overrides: object) -> MessageRow:
    fields: dict[str, object] = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "date": 1_700_000_000 + msg_id,
        "text": f"message {msg_id}",
    }
    fields.update(overrides)
    return MessageRow(**fields)  # type: ignore[arg-type]


def _insert_unit(conn: sqlite3.Connection, chat_id: int, msg_ids: list[int]) -> int:
    row = conn.execute(
        "INSERT INTO units(chat_id, kind, msg_id_start, msg_id_end, msg_ids, date_start, date_end,"
        " text) VALUES (?, 'window', ?, ?, ?, 1, 2, 'unit text') RETURNING id",
        (chat_id, msg_ids[0], msg_ids[-1], str(msg_ids)),
    ).fetchone()
    return int(row["id"])


def _populate(conn: sqlite3.Connection, chat_id: int) -> tuple[list[int], int]:
    """A chat with two messages, one unit, and matching msg_fts / unit_fts / unit_vec rows."""
    db.upsert_chat(conn, _chat(chat_id))
    message_ids = db.upsert_messages(conn, [_message(chat_id, 1), _message(chat_id, 2)])
    for message_id in message_ids:
        conn.execute(
            "INSERT INTO msg_fts(rowid, raw, stemmed, chat_id, date) VALUES (?, 'a', 'a', ?, 1)",
            (message_id, chat_id),
        )
    unit_id = _insert_unit(conn, chat_id, [1, 2])
    conn.execute(
        "INSERT INTO unit_fts(rowid, raw, stemmed, chat_id, date_start) VALUES (?, 'a', 'a', ?, 1)",
        (unit_id, chat_id),
    )
    conn.execute(
        "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (?, ?, 1, ?)",
        (unit_id, chat_id, sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0])),
    )
    conn.commit()
    return message_ids, unit_id


# --- connect ---------------------------------------------------------------------------------


def test_connect_configures_connection(conn: sqlite3.Connection) -> None:
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("SELECT vec_version()").fetchone()[0].startswith("v")
    assert conn.isolation_level == "IMMEDIATE"
    with pytest.raises(sqlite3.OperationalError):
        conn.load_extension(sqlite_vec.loadable_path())


def test_connect_with_paths_creates_wal_database(tmp_home: Path) -> None:
    paths = Paths.from_env()
    connection = db.connect(paths)
    try:
        assert paths.db_file.is_file()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        connection.close()


def test_connection_usable_from_second_thread(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    seen: list[ChatRow | None] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            db.upsert_messages(conn, [_message(1, 1)])
            seen.append(db.get_chat(conn, 1))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert errors == []
    assert seen == [_chat(1)]
    assert db.get_message(conn, 1, 1) is not None


# --- migrations ------------------------------------------------------------------------------


def test_fresh_migrate_creates_schema() -> None:
    connection = db.connect(":memory:")
    assert db.schema_version(connection) == 0
    assert db.migrate(connection) == db.SCHEMA_VERSION == 1
    assert TABLES <= _names(connection, "table")
    assert INDEXES <= _names(connection, "index")
    assert db.schema_version(connection) == 1
    assert db.get_meta(connection, "schema_version") == "1"
    assert not db.has_vec_table(connection)
    assert not connection.in_transaction
    messages_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'messages'"
    ).fetchone()[0]
    assert "UNIQUE (chat_id, msg_id)" in messages_sql
    assert "REFERENCES chats(id) ON DELETE CASCADE" in messages_sql
    units_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'units'").fetchone()
    assert "AUTOINCREMENT" in units_sql[0]
    for fts in ("msg_fts", "unit_fts"):
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name = ?", (fts,)).fetchone()
        assert "unicode61 remove_diacritics 2" in sql[0]


def test_migrate_twice_is_noop(conn: sqlite3.Connection) -> None:
    before = _schema(conn)
    assert db.migrate(conn) == 1
    assert _schema(conn) == before
    assert db.get_meta(conn, "schema_version") == "1"


def test_migrate_refuses_newer_schema(conn: sqlite3.Connection) -> None:
    db.set_meta(conn, "schema_version", str(db.SCHEMA_VERSION + 1))
    with pytest.raises(db.SchemaError, match="newer"):
        db.migrate(conn)


def test_migrate_rolls_back_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = db.connect(":memory:")
    monkeypatch.setattr(db, "MIGRATIONS", (db.MIGRATIONS[0][:3] + ("CREATE TABLE ?",)))
    monkeypatch.setattr(db, "SCHEMA_VERSION", 1)
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(connection)
    assert _names(connection, "table") == set()
    assert db.schema_version(connection) == 0
    assert not connection.in_transaction


def test_fts_tables_accept_rowid_keyed_rows_and_unindexed_filters(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO msg_fts(rowid, raw, stemmed, chat_id, date) VALUES (7, 'café', 'cafe', 5, 10)"
    )
    conn.execute(
        "INSERT INTO unit_fts(rowid, raw, stemmed, chat_id, date_start)"
        " VALUES (9, 'привет мир', 'привет мир', 6, 20)"
    )
    cafe = conn.execute("SELECT rowid FROM msg_fts WHERE msg_fts MATCH '\"cafe\"'").fetchone()
    assert cafe[0] == 7
    hits = conn.execute(
        "SELECT rowid FROM unit_fts WHERE unit_fts MATCH '\"привет\"' AND chat_id = ? "
        "AND date_start BETWEEN ? AND ?",
        (6, 0, 100),
    ).fetchall()
    assert [row[0] for row in hits] == [9]
    assert (
        conn.execute(
            "SELECT rowid FROM unit_fts WHERE unit_fts MATCH '\"привет\"' AND chat_id = 5"
        ).fetchall()
        == []
    )


# --- vec table -------------------------------------------------------------------------------


def test_ensure_vec_table_creates_vec0_with_dim(conn: sqlite3.Connection) -> None:
    assert db.vec_dim(conn) is None
    db.ensure_vec_table(conn, 4)
    assert db.has_vec_table(conn)
    assert db.vec_dim(conn) == 4
    assert db.get_meta(conn, "embed_dim") == "4"
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'unit_vec'").fetchone()[0]
    assert "vec0" in sql
    assert "chat_id INTEGER PARTITION KEY" in sql
    assert "FLOAT[4] distance_metric=cosine" in sql
    conn.execute(
        "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (1, 1, 1, ?)",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    )
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (2, 1, 1, ?)",
            (sqlite_vec.serialize_float32([1.0, 0.0]),),
        )


def test_ensure_vec_table_same_dim_keeps_rows(conn: sqlite3.Connection) -> None:
    db.ensure_vec_table(conn, 4)
    conn.execute(
        "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (1, 1, 1, ?)",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    )
    conn.commit()
    db.ensure_vec_table(conn, 4)
    assert conn.execute("SELECT count(*) FROM unit_vec").fetchone()[0] == 1


def test_ensure_vec_table_refuses_other_dim_unless_dropped(conn: sqlite3.Connection) -> None:
    db.ensure_vec_table(conn, 4)
    conn.execute(
        "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (1, 1, 1, ?)",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    )
    conn.commit()
    with pytest.raises(db.VecDimMismatch, match=r"4-dimensional.*requested 8.*drop=True"):
        db.ensure_vec_table(conn, 8)
    assert db.vec_dim(conn) == 4
    assert conn.execute("SELECT count(*) FROM unit_vec").fetchone()[0] == 1
    db.ensure_vec_table(conn, 8, drop=True)
    assert db.vec_dim(conn) == 8
    assert db.get_meta(conn, "embed_dim") == "8"
    assert conn.execute("SELECT count(*) FROM unit_vec").fetchone()[0] == 0
    conn.execute(
        "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (1, 1, 1, ?)",
        (sqlite_vec.serialize_float32([1.0] + [0.0] * 7),),
    )


def test_ensure_vec_table_rejects_bad_dim(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="positive"):
        db.ensure_vec_table(conn, 0)
    assert not db.has_vec_table(conn)


# --- meta ------------------------------------------------------------------------------------


def test_meta_roundtrip(conn: sqlite3.Connection) -> None:
    assert db.get_meta(conn, "embed_model") is None
    db.set_meta(conn, "embed_model", "BAAI/bge-m3")
    db.set_meta(conn, "embed_model", "other/model")
    assert db.get_meta(conn, "embed_model") == "other/model"
    assert not conn.in_transaction


# --- transaction -----------------------------------------------------------------------------


def test_transaction_commits_and_rolls_back(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        db.upsert_chat(conn, _chat(1))
        assert conn.in_transaction
    assert not conn.in_transaction
    assert db.get_chat(conn, 1) is not None
    with pytest.raises(RuntimeError), db.transaction(conn):
        db.upsert_chat(conn, _chat(2))
        db.set_meta(conn, "k", "v")
        raise RuntimeError("boom")
    assert not conn.in_transaction
    assert db.get_chat(conn, 2) is None
    assert db.get_meta(conn, "k") is None


def test_transaction_joins_python_implicit_transaction(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES ('a', '1')")
    assert conn.in_transaction
    db.set_meta(conn, "b", "2")
    assert conn.in_transaction
    conn.rollback()
    assert db.get_meta(conn, "a") is None
    assert db.get_meta(conn, "b") is None


# --- chats -----------------------------------------------------------------------------------


def test_upsert_chat_inserts_full_row(conn: sqlite3.Connection) -> None:
    chat = _chat(
        -1001,
        type="channel",
        username="news",
        is_forum=True,
        discussion_of=None,
        last_msg_id=42,
        last_sync_at=1000,
        unavailable=True,
        migrated_to=None,
    )
    assert db.upsert_chat(conn, chat) == chat
    assert db.get_chat(conn, -1001) == chat
    assert db.get_chat(conn, 7) is None
    assert not conn.in_transaction


def test_upsert_chat_updates_identity_and_keeps_sync_state(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1, last_msg_id=50, last_sync_at=1000, unavailable=True))
    stored = db.upsert_chat(
        conn,
        _chat(
            1,
            type="channel",
            title="renamed",
            username="new",
            is_forum=True,
            source_id="chat:@new",
            discussion_of=9,
        ),
    )
    assert stored.type == "channel"
    assert stored.title == "renamed"
    assert stored.username == "new"
    assert stored.is_forum is True
    assert stored.source_id == "chat:@new"
    assert stored.discussion_of == 9
    assert stored.last_msg_id == 50
    assert stored.last_sync_at == 1000
    assert stored.unavailable is True
    assert conn.execute("SELECT count(*) FROM chats").fetchone()[0] == 1


def test_chat_state_setters(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.set_chat_progress(conn, 1, last_msg_id=99, last_sync_at=2000)
    db.set_chat_unavailable(conn, 1)
    db.set_chat_migrated(conn, 1, migrated_to=-1002)
    expected = _chat(1, last_msg_id=99, last_sync_at=2000, unavailable=True, migrated_to=-1002)
    assert db.get_chat(conn, 1) == expected
    db.set_chat_unavailable(conn, 1, unavailable=False)
    assert db.get_chat(conn, 1) == _chat(1, last_msg_id=99, last_sync_at=2000, migrated_to=-1002)


def test_list_chats_orders_by_id_and_filters_by_source(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(3, source_id="folder:B"))
    db.upsert_chat(conn, _chat(-1001, source_id="folder:A"))
    db.upsert_chat(conn, _chat(2, source_id="folder:A"))
    assert [chat.id for chat in db.list_chats(conn)] == [-1001, 2, 3]
    assert [chat.id for chat in db.list_chats(conn, "folder:A")] == [-1001, 2]
    assert db.list_chats(conn, "folder:none") == []


def test_delete_chat_cascades_and_removes_virtual_rows(conn: sqlite3.Connection) -> None:
    db.ensure_vec_table(conn, 4)
    _populate(conn, 1)
    kept_message_ids, kept_unit_id = _populate(conn, 2)
    db.delete_chat(conn, 1)
    assert db.get_chat(conn, 1) is None
    assert db.get_messages(conn, 1) == []
    assert conn.execute("SELECT count(*) FROM units WHERE chat_id = 1").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT rowid FROM msg_fts")] == kept_message_ids
    assert [r[0] for r in conn.execute("SELECT rowid FROM unit_fts")] == [kept_unit_id]
    assert [r[0] for r in conn.execute("SELECT rowid FROM unit_vec")] == [kept_unit_id]
    assert db.get_chat(conn, 2) is not None
    assert len(db.get_messages(conn, 2)) == 2
    assert not conn.in_transaction


def test_delete_chat_without_vec_table_and_unknown_chat(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    message_ids = db.upsert_messages(conn, [_message(1, 1)])
    conn.execute(
        "INSERT INTO msg_fts(rowid, raw, stemmed, chat_id, date) VALUES (?, 'a', 'a', 1, 1)",
        (message_ids[0],),
    )
    db.delete_chat(conn, 404)
    assert db.get_chat(conn, 1) is not None
    db.delete_chat(conn, 1)
    assert db.get_chat(conn, 1) is None
    assert conn.execute("SELECT count(*) FROM msg_fts").fetchone()[0] == 0


def test_delete_chat_fts_cleanup_is_rowid_lookup(conn: sqlite3.Connection) -> None:
    plan = conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM msg_fts WHERE rowid IN "
        "(SELECT id FROM messages WHERE chat_id = ?)",
        (1,),
    ).fetchall()
    fts_steps = [row["detail"] for row in plan if "msg_fts" in row["detail"]]
    assert fts_steps == ["SCAN msg_fts VIRTUAL TABLE INDEX 0:="]
    scan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM msg_fts WHERE chat_id = ?", (1,)).fetchall()
    assert [row["detail"] for row in scan] == ["SCAN msg_fts VIRTUAL TABLE INDEX 0:"]


# --- users -----------------------------------------------------------------------------------


def test_upsert_users_inserts_and_updates(conn: sqlite3.Connection) -> None:
    db.upsert_users(conn, [UserRow(id=1, display_name="Ann", username="ann"), UserRow(id=2)])
    db.upsert_users(conn, [UserRow(id=1, display_name="Ann B.", username=None)])
    rows = conn.execute("SELECT id, display_name, username FROM users ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [(1, "Ann B.", None), (2, None, None)]
    db.upsert_users(conn, [])
    assert not conn.in_transaction


# --- messages --------------------------------------------------------------------------------


def test_upsert_messages_returns_ids_and_stores_all_columns(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    message = _message(
        1,
        10,
        edit_date=5,
        from_id=77,
        from_name="Ann",
        reply_to_msg_id=3,
        topic_id=2,
        fwd_from="Some Channel",
        media_kind="document",
        media_filename="rules.pdf",
        reactions_total=4,
    )
    ids = db.upsert_messages(conn, [message, _message(1, 11)])
    assert len(ids) == 2 and ids[0] != ids[1]
    stored = db.get_message(conn, 1, 10)
    assert stored is not None
    assert stored.id == ids[0]
    assert stored == MessageRow(
        id=ids[0],
        chat_id=1,
        msg_id=10,
        date=1_700_000_010,
        edit_date=5,
        from_id=77,
        from_name="Ann",
        reply_to_msg_id=3,
        topic_id=2,
        fwd_from="Some Channel",
        text="message 10",
        media_kind="document",
        media_filename="rules.pdf",
        reactions_total=4,
    )
    assert db.get_message(conn, 1, 12) is None
    assert db.upsert_messages(conn, []) == []


def test_upsert_messages_preserves_id_across_edit(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    (first_id,) = db.upsert_messages(conn, [_message(1, 5, text="original")])
    db.upsert_messages(conn, [_message(2, 5, text="other chat, same msg_id")])
    (again_id,) = db.upsert_messages(
        conn, [_message(1, 5, text="edited", edit_date=123, reactions_total=2)]
    )
    assert again_id == first_id
    stored = db.get_message(conn, 1, 5)
    assert stored is not None
    assert (stored.id, stored.text, stored.edit_date, stored.reactions_total) == (
        first_id,
        "edited",
        123,
        2,
    )
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
    other = db.get_message(conn, 2, 5)
    assert other is not None and other.text == "other chat, same msg_id"


def test_upsert_messages_requires_chat_row(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_messages(conn, [_message(999, 1)])
    assert not conn.in_transaction


def test_upsert_messages_is_atomic_per_batch(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_messages(conn, [_message(1, 1), _message(999, 2)])
    assert db.get_messages(conn, 1) == []


def test_get_messages_ordering_since_and_topic(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    db.upsert_messages(
        conn,
        [
            _message(1, 30, topic_id=7),
            _message(1, 10, topic_id=7),
            _message(1, 20),
            _message(2, 15),
        ],
    )
    assert [m.msg_id for m in db.get_messages(conn, 1)] == [10, 20, 30]
    assert [m.msg_id for m in db.get_messages(conn, 1, since_msg_id=20)] == [20, 30]
    assert [m.msg_id for m in db.get_messages(conn, 1, topic_id=7)] == [10, 30]
    assert [m.msg_id for m in db.get_messages(conn, 1, since_msg_id=11, topic_id=7)] == [30]
    assert db.get_messages(conn, 3) == []


def test_get_discussion_chat(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1, type="channel"))
    assert db.get_discussion_chat(conn, 1) is None
    db.upsert_chat(conn, _chat(2, discussion_of=1))
    db.upsert_chat(conn, _chat(3, discussion_of=7))
    discussion = db.get_discussion_chat(conn, 1)
    assert discussion is not None
    assert (discussion.id, discussion.discussion_of) == (2, 1)
    assert db.get_discussion_chat(conn, 2) is None


def test_get_topic_messages_groups_and_orders(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    db.upsert_messages(
        conn,
        [
            _message(1, 30, topic_id=7),
            _message(1, 10, topic_id=7),
            _message(1, 20, topic_id=9),
            _message(1, 40),
            _message(1, 50, topic_id=11),
            _message(2, 15, topic_id=7),
        ],
    )
    grouped = db.get_topic_messages(conn, 1, [9, 7, 7, 12])
    assert {topic: [m.msg_id for m in rows] for topic, rows in grouped.items()} == {
        7: [10, 30],
        9: [20],
    }
    assert all(m.chat_id == 1 for rows in grouped.values() for m in rows)
    assert db.get_topic_messages(conn, 1, []) == {}
    assert db.get_topic_messages(conn, 3, [7]) == {}


def test_get_topic_messages_batches_long_topic_lists(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_messages(
        conn,
        [_message(1, 1, topic_id=1), _message(1, 2, topic_id=600), _message(1, 3, topic_id=1001)],
    )
    assert len(range(1, 1002)) > 2 * db.IN_BATCH
    grouped = db.get_topic_messages(conn, 1, range(1, 1002))
    assert {topic: [m.msg_id for m in rows] for topic, rows in grouped.items()} == {
        1: [1],
        600: [2],
        1001: [3],
    }
