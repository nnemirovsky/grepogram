import dataclasses
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest
import sqlite_vec

from grepogram import db
from grepogram.models import ChatRow, MessageRow, UnitRow, UserRow
from grepogram.paths import Paths

TABLES = {"meta", "chats", "users", "messages", "units", "msg_fts", "unit_fts"}
INDEXES = {
    "messages_chat_date",
    "messages_reply",
    "units_chat_kind_range",
    "messages_unindexed",
    "chats_discussion_of",
    "messages_comments",
}


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
    assert conn.isolation_level is None
    assert isinstance(conn, db.Connection)
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
    assert db.migrate(connection) == db.SCHEMA_VERSION == 4
    assert TABLES <= _names(connection, "table")
    assert INDEXES <= _names(connection, "index")
    assert db.schema_version(connection) == 4
    assert db.get_meta(connection, "schema_version") == "4"
    assert not db.has_vec_table(connection)
    assert not connection.in_transaction
    messages_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'messages'"
    ).fetchone()[0]
    assert "UNIQUE (chat_id, msg_id)" in messages_sql
    assert "REFERENCES chats(id) ON DELETE CASCADE" in messages_sql
    assert "indexed INTEGER NOT NULL DEFAULT 0" in messages_sql
    assert "comment_of_chat_id INTEGER" in messages_sql
    assert "comment_of_msg_id INTEGER" in messages_sql
    units_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'units'").fetchone()
    assert "AUTOINCREMENT" in units_sql[0]
    for fts in ("msg_fts", "unit_fts"):
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name = ?", (fts,)).fetchone()
        assert "unicode61 remove_diacritics 2" in sql[0]


def test_migrate_twice_is_noop(conn: sqlite3.Connection) -> None:
    before = _schema(conn)
    assert db.migrate(conn) == 4
    assert _schema(conn) == before
    assert db.get_meta(conn, "schema_version") == "4"


def test_migrate_v1_to_v2_flags_every_stored_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A v1 index is upgraded in place, and its rows start unindexed so the first sync after
    the upgrade rebuilds what an interrupted run may have left without units."""
    connection = db.connect(":memory:")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
    monkeypatch.setattr(db, "SCHEMA_VERSION", 1)
    assert db.migrate(connection) == 1
    assert "messages_unindexed" not in _names(connection, "index")
    connection.execute("INSERT INTO chats(id, type) VALUES (1, 'supergroup')")
    connection.execute("INSERT INTO messages(chat_id, msg_id, date, text) VALUES (1, 1, 10, 'a')")
    connection.execute("INSERT INTO messages(chat_id, msg_id, date, text) VALUES (1, 2, 20, 'b')")
    monkeypatch.undo()
    assert db.migrate(connection) == 4
    assert db.schema_version(connection) == 4
    assert "messages_unindexed" in _names(connection, "index")
    assert db.unindexed_message_ids(connection, 1) == [1, 2]
    assert not connection.in_transaction


def test_migrate_v2_to_v3_leaves_one_discussion_group_per_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database written before the link was re-pointed can hold several groups for one
    channel; the upgrade keeps the lowest id, which is the row it used to be answered with."""
    connection = db.connect(":memory:")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:2])
    monkeypatch.setattr(db, "SCHEMA_VERSION", 2)
    assert db.migrate(connection) == 2
    connection.execute("INSERT INTO chats(id, type) VALUES (1, 'channel')")
    for group in (-3, -2, -1):
        connection.execute(
            "INSERT INTO chats(id, type, discussion_of) VALUES (?, 'supergroup', 1)", (group,)
        )
    monkeypatch.undo()
    assert db.migrate(connection) == 4
    kept = db.get_discussion_chat(connection, 1)
    assert kept is not None and kept.id == -3
    assert [chat.id for chat in db.list_chats(connection) if chat.discussion_of == 1] == [-3]


def _at_v3(connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bring a fresh database to v3 and let the caller write pre-v4 rows into it."""
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:3])
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    assert db.migrate(connection) == 3
    monkeypatch.undo()


def test_migrate_v3_to_v4_moves_the_comments_of_a_plain_discussion_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before v4 a comment kept its post id in ``topic_id``. A discussion group that is not a
    forum has no topics of its own, so every one of them is a post of the channel linking it and
    moves to the comment columns unambiguously; the rows are flagged so what was cut from them
    is cut again."""
    connection = db.connect(":memory:")
    _at_v3(connection, monkeypatch)
    connection.execute("INSERT INTO chats(id, type) VALUES (1, 'channel')")
    connection.execute(
        "INSERT INTO chats(id, type, is_forum, discussion_of) VALUES (2, 'supergroup', 0, 1)"
    )
    connection.execute("INSERT INTO chats(id, type, is_forum) VALUES (3, 'supergroup', 1)")
    connection.execute(
        "INSERT INTO messages(chat_id, msg_id, date, text, topic_id, indexed) "
        "VALUES (2, 5, 10, 'comment', 10, 1)"
    )
    connection.execute(
        "INSERT INTO messages(chat_id, msg_id, date, text, indexed) VALUES (2, 6, 11, 'talk', 1)"
    )
    connection.execute(
        "INSERT INTO messages(chat_id, msg_id, date, text, topic_id, indexed) "
        "VALUES (3, 7, 12, 'in a topic', 10, 1)"
    )
    assert db.migrate(connection) == 4
    comment = db.get_message(connection, 2, 5)
    assert comment is not None
    assert comment.topic_id is None
    assert (comment.comment_of_chat_id, comment.comment_of_msg_id) == (1, 10)
    assert db.stored_comment_post_ids(connection, 2, 1) == [10]
    assert db.unindexed_message_ids(connection, 2) == [comment.id]
    forum = db.get_message(connection, 3, 7)
    assert forum is not None and forum.topic_id == 10
    assert forum.comment_of_chat_id is None and forum.comment_of_msg_id is None
    assert db.unindexed_message_ids(connection, 3) == []
    connection.close()


def test_migrate_v3_to_v4_flags_a_forum_discussion_group_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group that is both a forum and a comment store wrote forum topic roots and channel post
    ids into the same column, and nothing in the row says which is which. Nothing is moved and
    nothing is attributed: the rows keep their ``topic_id`` and are flagged, and so are the posts
    of the channel linking the group, whose post threads are therefore cut again without
    comments until the next sync re-reads the threads under the new columns.
    """
    connection = db.connect(":memory:")
    _at_v3(connection, monkeypatch)
    connection.execute("INSERT INTO chats(id, type) VALUES (1, 'channel')")
    connection.execute(
        "INSERT INTO chats(id, type, is_forum, discussion_of) VALUES (2, 'supergroup', 1, 1)"
    )
    connection.execute(
        "INSERT INTO messages(chat_id, msg_id, date, text, topic_id, indexed) "
        "VALUES (2, 5, 10, 'comment or topic message', 10, 1)"
    )
    connection.execute(
        "INSERT INTO messages(chat_id, msg_id, date, text, indexed) VALUES (2, 6, 11, 'talk', 1)"
    )
    connection.execute(
        "INSERT INTO messages(chat_id, msg_id, date, text, indexed) VALUES (1, 10, 12, 'post', 1)"
    )
    assert db.migrate(connection) == 4
    ambiguous = db.get_message(connection, 2, 5)
    assert ambiguous is not None and ambiguous.topic_id == 10
    assert ambiguous.comment_of_chat_id is None and ambiguous.comment_of_msg_id is None
    assert db.stored_comment_post_ids(connection, 2, 1) == []
    assert db.unindexed_message_ids(connection, 2) == [ambiguous.id]
    post = db.get_message(connection, 1, 10)
    assert post is not None
    assert db.unindexed_message_ids(connection, 1) == [post.id]
    connection.close()


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
    assert conn.execute("SELECT count(*) FROM unit_vec").fetchone()[0] == 0
    conn.execute(
        "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (1, 1, 1, ?)",
        (sqlite_vec.serialize_float32([1.0] + [0.0] * 7),),
    )


def test_ensure_vec_table_drop_recreates_even_with_the_same_dim(conn: sqlite3.Connection) -> None:
    db.ensure_vec_table(conn, 4)
    conn.execute(
        "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (1, 1, 1, ?)",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    )
    db.ensure_vec_table(conn, 4, drop=True)
    assert db.vec_dim(conn) == 4
    assert conn.execute("SELECT count(*) FROM unit_vec").fetchone()[0] == 0


def test_has_vectors(conn: sqlite3.Connection) -> None:
    assert not db.has_vectors(conn)
    db.ensure_vec_table(conn, 4)
    assert not db.has_vectors(conn)
    conn.execute(
        "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (1, 1, 1, ?)",
        (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
    )
    assert db.has_vectors(conn)


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


def test_bare_statements_autocommit_and_nested_transactions_join(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES ('a', '1')")
    assert not conn.in_transaction
    conn.rollback()
    assert db.get_meta(conn, "a") == "1"
    with pytest.raises(RuntimeError), db.transaction(conn):
        with db.transaction(conn):
            db.set_meta(conn, "b", "2")
        assert conn.in_transaction
        raise RuntimeError("boom")
    assert not conn.in_transaction
    assert db.get_meta(conn, "b") is None


def test_transaction_serialises_threads_and_hides_uncommitted_rows(
    conn: sqlite3.Connection,
) -> None:
    inside = threading.Event()
    release = threading.Event()
    seen: list[ChatRow | None] = []

    def writer() -> None:
        with db.transaction(conn):
            db.upsert_chat(conn, _chat(1))
            inside.set()
            release.wait(5)

    def reader() -> None:
        inside.wait(5)
        seen.append(db.get_chat(conn, 1))

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    inside.wait(5)
    time.sleep(0.05)
    assert seen == []
    release.set()
    for thread in threads:
        thread.join(5)
    assert seen == [_chat(1)]


def test_concurrent_statements_on_one_connection_do_not_interfere(
    conn: sqlite3.Connection,
) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_messages(conn, [_message(1, i) for i in range(1, 201)])
    errors: list[BaseException] = []

    def work(offset: int) -> None:
        try:
            for i in range(50):
                assert db.last_sync_at(conn) is None
                assert len(db.get_messages(conn, 1)) == 200
                db.upsert_messages(conn, [_message(1, offset + i, text="edit")])
                assert db.get_message(conn, 1, offset + i) is not None
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in (1, 51, 101, 151)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 200


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


def test_last_sync_at_is_the_latest_completed_sync(conn: sqlite3.Connection) -> None:
    assert db.last_sync_at(conn) is None
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    assert db.last_sync_at(conn) is None
    db.set_chat_progress(conn, 1, 10, 500)
    db.set_chat_progress(conn, 2, 10, None)
    assert db.last_sync_at(conn) == 500
    db.set_chat_progress(conn, 2, 20, 900)
    assert db.last_sync_at(conn) == 900
    db.set_chat_progress(conn, 2, 20, 300)
    assert db.last_sync_at(conn) == 500


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


def _channel_with_comments(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """A channel with two posts, a discussion group holding a comment on the first, and the
    channel's post thread over both — with its ``unit_fts`` and ``unit_vec`` rows. Returns the
    ids of the post thread, the channel's own post unit and the group's window."""
    db.ensure_vec_table(conn, 4)
    db.upsert_chat(conn, _chat(1, type="channel"))
    db.upsert_chat(conn, _chat(2, discussion_of=1, source_id="chat:2"))
    db.upsert_messages(conn, [_message(1, 10), _message(1, 11)])
    db.upsert_messages(conn, [_message(2, 5, comment_of_chat_id=1, comment_of_msg_id=10)])
    ids = db.insert_units(
        conn,
        [
            _unit(1, [10], kind="thread", text="post 10\ncomment 5"),
            _unit(1, [10], kind="post", text="post 10"),
            _unit(2, [5], text="comment 5"),
        ],
    )
    for unit_id in ids:
        conn.execute(
            "INSERT INTO unit_fts(rowid, raw, stemmed, chat_id, date_start) "
            "VALUES (?, 'a', 'a', 1, 1)",
            (unit_id,),
        )
        conn.execute(
            "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (?, 1, 1, ?)",
            (unit_id, sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0])),
        )
    stored = db.get_messages(conn, 1) + db.get_messages(conn, 2)
    db.mark_indexed(conn, [m.id for m in stored if m.id is not None])
    conn.commit()
    return ids[0], ids[1], ids[2]


def test_delete_chat_drops_the_post_threads_of_a_deleted_discussion_group(
    conn: sqlite3.Connection,
) -> None:
    thread_id, post_id, window_id = _channel_with_comments(conn)
    db.delete_chat(conn, 2)
    assert [u.id for u in db.get_units(conn, 1)] == [post_id]
    assert [r[0] for r in conn.execute("SELECT rowid FROM unit_fts")] == [post_id]
    assert [r[0] for r in conn.execute("SELECT rowid FROM unit_vec")] == [post_id]
    assert thread_id not in {window_id, post_id}
    post = db.get_message(conn, 1, 10)
    assert post is not None
    assert db.unindexed_message_ids(conn, 1) == [post.id]


def test_drop_comment_units_unmaps_the_comments_it_undoes(conn: sqlite3.Connection) -> None:
    """The rows stop being comments, not only the threads built from them: a comment left naming
    a channel would be handed back to whichever channel links the group next, and post ids start
    at 1 in every channel. The messages stay — the group's own — and so does everything cut from
    them: no unit of a group reads the comment relation, so nothing about the group needs a
    rebuild and nothing of it is dropped. A comment on another channel's post is none of this
    channel's business and is left alone.
    """
    thread_id, _post_id, window_id = _channel_with_comments(conn)
    db.upsert_messages(conn, [_message(2, 6, comment_of_chat_id=99, comment_of_msg_id=999)])
    own = db.get_message(conn, 2, 6)
    assert own is not None and own.id is not None
    db.mark_indexed(conn, [own.id])
    kept = db.insert_units(conn, [_unit(2, [6])])
    assert db.drop_comment_units(conn, 1, 2) == 1
    comment = db.get_message(conn, 2, 5)
    assert comment is not None
    assert comment.comment_of_chat_id is None and comment.comment_of_msg_id is None
    assert db.get_comment_messages(conn, 2, 1, [10]) == {}
    assert db.stored_comment_post_ids(conn, 2, 1) == []
    assert db.stored_comment_post_ids(conn, 2, 99) == [999]
    assert db.unindexed_message_ids(conn, 2) == []
    assert [u.id for u in db.get_units(conn, 2)] == [window_id, *kept]
    assert thread_id not in {window_id, *kept}


def test_drop_comment_units_leaves_a_forum_topic_of_the_same_group_alone(
    conn: sqlite3.Connection,
) -> None:
    """A discussion group can be a forum, and a topic root is numbered from 1 just like a channel
    post. The topic whose root is the number of a stored post keeps its messages, its topic and
    its window; only the rows that name the channel stop being its comments."""
    thread_id, _post_id, window_id = _channel_with_comments(conn)
    db.upsert_chat(conn, _chat(2, is_forum=True, discussion_of=1, source_id="chat:2"))
    db.upsert_messages(conn, [_message(2, 20, topic_id=10), _message(2, 21, topic_id=10)])
    topic = [m for m in db.get_messages(conn, 2) if m.msg_id in (20, 21)]
    db.mark_indexed(conn, [m.id for m in topic if m.id is not None])
    (topic_window,) = db.insert_units(conn, [_unit(2, [20, 21], topic_id=10)])
    assert db.drop_comment_units(conn, 1, 2) == 1
    assert [m.msg_id for m in db.get_messages_in_topic(conn, 2, 10)] == [20, 21]
    assert db.unindexed_message_ids(conn, 2) == []
    assert [u.id for u in db.get_units(conn, 2)] == [window_id, topic_window]
    assert thread_id not in {window_id, topic_window}


def test_drop_comment_units_unmaps_a_comment_whose_post_row_is_gone(
    conn: sqlite3.Connection,
) -> None:
    """The mapping is keyed by the channel, not by which of its posts are still stored, so a
    comment on a post the channel has dropped is unmapped like any other — otherwise the next
    channel to link the group would inherit it through its own post of that number."""
    _thread_id, _post_id, _window_id = _channel_with_comments(conn)
    db.upsert_messages(conn, [_message(2, 7, comment_of_chat_id=1, comment_of_msg_id=42)])
    assert db.get_message(conn, 1, 42) is None
    assert db.drop_comment_units(conn, 1, 2) == 1
    orphan = db.get_message(conn, 2, 7)
    assert orphan is not None
    assert orphan.comment_of_chat_id is None and orphan.comment_of_msg_id is None
    assert db.stored_comment_post_ids(conn, 2, 1) == []


def test_delete_chat_clears_the_link_of_a_group_that_outlives_its_channel(
    conn: sqlite3.Connection,
) -> None:
    """The group keeps its messages and its window, and stops holding comments: they named a post
    of a channel that is gone, and the next channel to link the group numbers its own posts from
    1 as well. The window stays — a cleared comment is searchable through it the moment the
    delete commits, with no rebuild owed."""
    _thread_id, _post_id, window_id = _channel_with_comments(conn)
    db.delete_chat(conn, 1)
    group = db.get_chat(conn, 2)
    assert group is not None and group.discussion_of is None
    assert db.get_discussion_chat(conn, 1) is None
    assert [u.id for u in db.get_units(conn, 2)] == [window_id]
    comment = db.get_message(conn, 2, 5)
    assert comment is not None
    assert comment.comment_of_chat_id is None and comment.comment_of_msg_id is None
    assert db.stored_comment_post_ids(conn, 2, 1) == []
    assert db.unindexed_message_ids(conn, 2) == []


def _dump(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """Every row a delete could touch, for a before/after comparison."""
    dumped = {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("chats", "messages", "units")
    }
    for virtual in ("msg_fts", "unit_fts", "unit_vec"):
        rows = conn.execute(f"SELECT rowid FROM {virtual} ORDER BY rowid")
        dumped[virtual] = [tuple(row) for row in rows]
    return dumped


def test_delete_chat_leaves_every_row_alone_for_a_chat_it_never_held(
    conn: sqlite3.Connection,
) -> None:
    """An id this index does not hold is a no-op, ``discussion_of`` included: a group whose
    channel is not stored is a live chat of its own, and deleting the id it names must not
    unlink it on the way past."""
    _channel_with_comments(conn)
    db.upsert_chat(conn, _chat(3, discussion_of=404, source_id="chat:3"))
    before = _dump(conn)
    db.delete_chat(conn, 404)
    db.delete_chat(conn, 3_000_000)
    assert _dump(conn) == before
    assert not conn.in_transaction


def test_delete_chat_fts_cleanup_is_rowid_lookup(conn: sqlite3.Connection) -> None:
    plan = conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM msg_fts WHERE rowid IN "
        "(SELECT id FROM messages WHERE chat_id = ?)",
        (1,),
    ).fetchall()
    # fts5's idxStr ends in "=" for a rowid lookup and is bare for a full scan; the rest of the
    # line is SQLite's wording and changes between versions
    fts_steps = [row["detail"] for row in plan if "msg_fts" in row["detail"]]
    assert fts_steps and all(step.endswith("INDEX 0:=") for step in fts_steps)
    scan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM msg_fts WHERE chat_id = ?", (1,)).fetchall()
    assert scan and all(row["detail"].endswith("INDEX 0:") for row in scan)


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


def test_upsert_flags_rows_until_they_are_marked_indexed(conn: sqlite3.Connection) -> None:
    """Every row written — inserted or updated, changed or not — waits for a rebuild; the
    flag is cleared per row, in chunks larger than one ``IN`` list, and can be raised again for
    a row whose derived units went stale without the row itself changing."""
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    ids = db.upsert_messages(conn, [_message(1, i) for i in range(1, db.IN_BATCH + 102)])
    other = db.upsert_messages(conn, [_message(2, 1)])
    assert db.unindexed_message_ids(conn, 1) == ids
    assert db.unindexed_message_ids(conn, 2) == other
    db.mark_indexed(conn, ids)
    assert db.unindexed_message_ids(conn, 1) == []
    assert db.unindexed_message_ids(conn, 2) == other
    edited = db.upsert_messages(conn, [_message(1, 5, text="edited"), _message(1, 6)])
    assert db.unindexed_message_ids(conn, 1) == edited
    db.mark_indexed(conn, edited)
    db.mark_unindexed(conn, ids[:2])
    assert db.unindexed_message_ids(conn, 1) == ids[:2]
    db.mark_indexed(conn, [])
    db.mark_unindexed(conn, [])
    assert db.unindexed_message_ids(conn, 1) == ids[:2]
    assert not conn.in_transaction


def test_chats_with_unindexed_names_every_chat_holding_pending_rows(
    conn: sqlite3.Connection,
) -> None:
    """The flag is the only thing that says where a rebuild is owed: a chat no source and no
    discussion link leads to is found through this and nothing else."""
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    first = db.upsert_messages(conn, [_message(1, 1)])
    second = db.upsert_messages(conn, [_message(2, 1)])
    assert db.chats_with_unindexed(conn) == [1, 2]
    db.mark_indexed(conn, first)
    assert db.chats_with_unindexed(conn) == [2]
    db.mark_indexed(conn, second)
    assert db.chats_with_unindexed(conn) == []


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


def test_a_channel_cannot_hold_two_discussion_groups(conn: sqlite3.Connection) -> None:
    """What makes :func:`db.get_discussion_chat` a lookup rather than a pick between rows."""
    db.upsert_chat(conn, _chat(1, type="channel"))
    db.upsert_chat(conn, _chat(2, discussion_of=1))
    with pytest.raises(sqlite3.IntegrityError, match="chats.discussion_of"):
        db.upsert_chat(conn, _chat(3, discussion_of=1))


def test_set_discussion_chat_re_points_the_link_and_reports_what_it_dropped(
    conn: sqlite3.Connection,
) -> None:
    db.upsert_chat(conn, _chat(1, type="channel"))
    for group in (2, 3):
        db.upsert_chat(conn, _chat(group))
    assert db.set_discussion_chat(conn, 1, 2) == []
    assert db.get_discussion_chat(conn, 1) == db.get_chat(conn, 2)
    assert db.set_discussion_chat(conn, 1, 2) == []
    assert db.set_discussion_chat(conn, 1, 3) == [2]
    moved = db.get_chat(conn, 2)
    assert moved is not None and moved.discussion_of is None
    assert db.get_discussion_chat(conn, 1) == db.get_chat(conn, 3)
    assert db.set_discussion_chat(conn, 1, None) == [3]
    assert db.get_discussion_chat(conn, 1) is None
    assert db.list_chats(conn) == [db.get_chat(conn, 1), db.get_chat(conn, 2), db.get_chat(conn, 3)]


def test_stored_comment_post_ids_are_distinct_ordered_and_per_channel(
    conn: sqlite3.Connection,
) -> None:
    """Only the comments of the channel asked about, and never a forum topic of the group: a
    topic root and a post id are separate id spaces that both number from 1."""
    db.upsert_chat(conn, _chat(1))
    db.upsert_messages(
        conn,
        [
            _message(1, 10, comment_of_chat_id=5, comment_of_msg_id=7),
            _message(1, 11, comment_of_chat_id=5, comment_of_msg_id=7),
            _message(1, 12, comment_of_chat_id=5, comment_of_msg_id=3),
            _message(1, 13, comment_of_chat_id=6, comment_of_msg_id=3),
            _message(1, 14, topic_id=7),
            _message(1, 15),
        ],
    )
    assert db.stored_comment_post_ids(conn, 1, 5) == [3, 7]
    assert db.stored_comment_post_ids(conn, 1, 6) == [3]
    assert db.stored_comment_post_ids(conn, 1, 9) == []
    assert db.stored_comment_post_ids(conn, 2, 5) == []


def test_get_comment_messages_groups_and_orders(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    db.upsert_messages(
        conn,
        [
            _message(1, 30, comment_of_chat_id=5, comment_of_msg_id=7),
            _message(1, 10, comment_of_chat_id=5, comment_of_msg_id=7),
            _message(1, 20, comment_of_chat_id=5, comment_of_msg_id=9),
            _message(1, 40),
            _message(1, 45, topic_id=7),
            _message(1, 46, comment_of_chat_id=6, comment_of_msg_id=7),
            _message(1, 50, comment_of_chat_id=5, comment_of_msg_id=11),
            _message(2, 15, comment_of_chat_id=5, comment_of_msg_id=7),
        ],
    )
    grouped = db.get_comment_messages(conn, 1, 5, [9, 7, 7, 12])
    assert {post: [m.msg_id for m in rows] for post, rows in grouped.items()} == {
        7: [10, 30],
        9: [20],
    }
    assert all(m.chat_id == 1 for rows in grouped.values() for m in rows)
    assert db.get_comment_messages(conn, 1, 5, []) == {}
    assert db.get_comment_messages(conn, 3, 5, [7]) == {}
    assert {
        post: [m.msg_id for m in rows]
        for post, rows in db.get_comment_messages(conn, 1, 6, [7]).items()
    } == {7: [46]}


def test_get_comment_messages_batches_long_post_lists(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_messages(
        conn,
        [
            _message(1, 1, comment_of_chat_id=5, comment_of_msg_id=1),
            _message(1, 2, comment_of_chat_id=5, comment_of_msg_id=600),
            _message(1, 3, comment_of_chat_id=5, comment_of_msg_id=1001),
        ],
    )
    assert len(range(1, 1002)) > 2 * db.IN_BATCH
    grouped = db.get_comment_messages(conn, 1, 5, range(1, 1002))
    assert {topic: [m.msg_id for m in rows] for topic, rows in grouped.items()} == {
        1: [1],
        600: [2],
        1001: [3],
    }


def test_get_messages_in_topic_treats_none_as_a_filter(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_messages(
        conn,
        [
            _message(1, 10, topic_id=7),
            _message(1, 20),
            _message(1, 30, topic_id=7),
            _message(1, 40),
        ],
    )
    assert [m.msg_id for m in db.get_messages_in_topic(conn, 1, None)] == [20, 40]
    assert [m.msg_id for m in db.get_messages_in_topic(conn, 1, 7)] == [10, 30]
    assert [m.msg_id for m in db.get_messages_in_topic(conn, 1, 7, since_msg_id=30)] == [30]
    assert [m.msg_id for m in db.get_messages_in_topic(conn, 1, None, since_msg_id=21)] == [40]
    assert db.get_messages_in_topic(conn, 1, 8) == []


def test_get_messages_by_ids_spans_chats_and_batches(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    ids = db.upsert_messages(conn, [_message(1, i) for i in range(1, 601)])
    (other,) = db.upsert_messages(conn, [_message(2, 1)])
    assert db.get_messages_by_ids(conn, []) == []
    rows = db.get_messages_by_ids(conn, [other, ids[5], ids[5], 999_999, ids[0]])
    assert [(m.chat_id, m.msg_id) for m in rows] == [(1, 1), (1, 6), (2, 1)]
    assert [m.id for m in db.get_messages_by_ids(conn, reversed(ids))] == ids


def test_get_messages_by_msg_id_is_per_chat(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    db.upsert_messages(conn, [_message(1, 10), _message(1, 20), _message(2, 10)])
    found = db.get_messages_by_msg_id(conn, 1, [10, 30, 10])
    assert set(found) == {10}
    assert found[10].chat_id == 1 and found[10].text == "message 10"
    assert db.get_messages_by_msg_id(conn, 3, [10]) == {}


def test_get_replies_orders_by_msg_id_and_stays_in_chat(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    db.upsert_messages(
        conn,
        [
            _message(1, 1),
            _message(1, 2, reply_to_msg_id=1),
            _message(1, 3, reply_to_msg_id=2),
            _message(1, 4, reply_to_msg_id=1),
            _message(2, 5, reply_to_msg_id=1),
        ],
    )
    assert [m.msg_id for m in db.get_replies(conn, 1, [1])] == [2, 4]
    assert [m.msg_id for m in db.get_replies(conn, 1, [2, 1])] == [2, 3, 4]
    assert db.get_replies(conn, 1, [9]) == []
    assert db.get_replies(conn, 1, []) == []


def _unit(chat_id: int, msg_ids: list[int], **overrides: object) -> UnitRow:
    fields: dict[str, object] = {
        "chat_id": chat_id,
        "kind": "window",
        "msg_id_start": min(msg_ids),
        "msg_id_end": max(msg_ids),
        "msg_ids": msg_ids,
        "date_start": 100,
        "date_end": 200,
        "text": "unit text",
    }
    fields.update(overrides)
    return UnitRow(**fields)  # type: ignore[arg-type]


def test_insert_and_get_units_roundtrip(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    window = _unit(1, [3, 1, 2], topic_id=5)
    thread = _unit(1, [1, 4], kind="thread", dirty=False, embedded_model="m")
    ids = db.insert_units(conn, [window, thread, _unit(2, [9])])
    assert ids == [1, 2, 3]
    stored = db.get_units(conn, 1)
    assert stored == [
        dataclasses.replace(window, id=1),
        dataclasses.replace(thread, id=2),
    ]
    assert stored[0].dirty is True and stored[1].dirty is False
    assert conn.execute("SELECT msg_ids FROM units WHERE id = 1").fetchone()[0] == "[3,1,2]"
    assert [u.id for u in db.get_units(conn, 1) if u.kind == "thread"] == [2]
    assert db.get_units(conn, 3) == []
    assert db.insert_units(conn, []) == []


def test_delete_units_by_id(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    ids = db.insert_units(conn, [_unit(1, [1]), _unit(1, [2]), _unit(1, [3])])
    db.delete_units(conn, [ids[0], ids[2], 999])
    assert [u.id for u in db.get_units(conn, 1)] == [ids[1]]
    db.delete_units(conn, [])
    assert [u.id for u in db.get_units(conn, 1)] == [ids[1]]


def test_get_units_by_ids_spans_chats_and_batches(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    ids = db.insert_units(conn, [_unit(1, [i]) for i in range(1, 601)])
    (other,) = db.insert_units(conn, [_unit(2, [1], kind="thread")])
    assert db.get_units_by_ids(conn, []) == []
    rows = db.get_units_by_ids(conn, [other, ids[5], ids[5], 999_999, ids[0]])
    assert [(u.chat_id, u.msg_ids) for u in rows] == [(1, [1]), (1, [6]), (2, [1])]
    assert rows[2].kind == "thread"
    assert [u.id for u in db.get_units_by_ids(conn, reversed(ids))] == ids


def test_open_window_is_the_last_window_of_the_topic(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    assert db.open_window(conn, 1, None) is None
    ids = db.insert_units(
        conn,
        [
            _unit(1, [5, 6]),
            _unit(1, [1, 2]),
            _unit(1, [7, 8], kind="thread"),
            _unit(1, [3], topic_id=9),
            _unit(1, [9, 10], topic_id=9, kind="post"),
        ],
    )
    general = db.open_window(conn, 1, None)
    assert general is not None and general.id == ids[0] and general.msg_ids == [5, 6]
    topic = db.open_window(conn, 1, 9)
    assert topic is not None and topic.id == ids[3]
    assert db.open_window(conn, 1, 4) is None
    assert db.open_window(conn, 2, None) is None


def test_windowed_msg_ids_is_membership_within_the_topic(conn: sqlite3.Connection) -> None:
    """A window over 1, 2 and 25 spans 3..24 without holding them; only listed ids count."""
    db.upsert_chat(conn, _chat(1))
    db.insert_units(
        conn,
        [
            _unit(1, [1, 2, 25]),
            _unit(1, [40, 41]),
            _unit(1, [3, 4], topic_id=9),
            _unit(1, [5, 6], kind="thread"),
        ],
    )
    assert db.windowed_msg_ids(conn, 1, None, range(1, 45)) == {1, 2, 25, 40, 41}
    assert db.windowed_msg_ids(conn, 1, None, [3, 4, 5, 6]) == set()
    assert db.windowed_msg_ids(conn, 1, 9, [3, 4, 5]) == {3, 4}
    assert db.windowed_msg_ids(conn, 1, None, []) == set()
    assert db.windowed_msg_ids(conn, 2, None, [1]) == set()


def test_window_before_and_windows_from(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    ids = db.insert_units(
        conn,
        [
            _unit(1, [1, 2]),
            _unit(1, [10, 12]),
            _unit(1, [30]),
            _unit(1, [5], topic_id=9),
            _unit(1, [11], kind="thread"),
        ],
    )

    def before(msg_id: int, topic_id: int | None = None) -> int | None:
        unit = db.window_before(conn, 1, topic_id, msg_id)
        return None if unit is None else unit.id

    assert before(0) is None
    assert before(1) == ids[0]
    assert before(3) == ids[0]
    assert before(10) == ids[1]
    assert before(11) == ids[1]
    assert before(99) == ids[2]
    assert before(5) == ids[0]
    assert before(5, 9) == ids[3]
    assert [u.id for u in db.windows_from(conn, 1, None, 0)] == ids[:3]
    assert [u.id for u in db.windows_from(conn, 1, None, 2)] == ids[:3]
    assert [u.id for u in db.windows_from(conn, 1, None, 3)] == ids[1:3]
    assert [u.id for u in db.windows_from(conn, 1, None, 12)] == ids[1:3]
    assert db.windows_from(conn, 1, None, 31) == []
    assert [u.id for u in db.windows_from(conn, 1, 9, 1)] == [ids[3]]


def test_threads_touching_matches_any_listed_message(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    db.upsert_chat(conn, _chat(2))
    ids = db.insert_units(
        conn,
        [
            _unit(1, [1, 2, 3], kind="thread"),
            _unit(1, [1, 7], kind="thread"),
            _unit(1, [1, 2, 3]),
            _unit(1, [12], kind="post"),
            _unit(1, [20, 21], kind="thread"),
            _unit(2, [1, 2], kind="thread"),
        ],
    )
    assert [u.id for u in db.threads_touching(conn, 1, [1])] == ids[:2]
    assert [u.id for u in db.threads_touching(conn, 1, [7, 12])] == [ids[1]]
    assert [u.id for u in db.threads_touching(conn, 1, [2, 21])] == [ids[0], ids[4]]
    assert [u.id for u in db.threads_touching(conn, 1, range(1, 1001))] == [ids[0], ids[1], ids[4]]
    assert db.threads_touching(conn, 1, [99]) == []
    assert db.threads_touching(conn, 1, []) == []


def test_post_units_by_post_id(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    ids = db.insert_units(
        conn, [_unit(1, [1], kind="post"), _unit(1, [2], kind="post"), _unit(1, [1], kind="thread")]
    )
    assert [u.id for u in db.post_units(conn, 1, [1, 3])] == [ids[0]]
    assert [u.id for u in db.post_units(conn, 1, [2, 1])] == ids[:2]
    assert db.post_units(conn, 1, []) == []


def test_containing_unit_by_topic_range_then_post(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    ids = db.insert_units(
        conn,
        [
            _unit(1, [1, 3, 5]),
            _unit(1, [2, 4, 6], topic_id=7),
            _unit(1, [1, 2, 3], kind="thread"),
            _unit(1, [9], kind="post"),
            _unit(1, [9], kind="thread"),
        ],
    )

    def containing(msg_id: int, topic_id: int | None) -> int | None:
        unit = db.containing_unit(conn, 1, msg_id, topic_id)
        return None if unit is None else unit.id

    assert containing(3, None) == ids[0]
    assert containing(4, None) == ids[0]
    assert containing(4, 7) == ids[1]
    assert containing(2, 7) == ids[1]
    assert containing(6, 7) == ids[1]
    assert containing(3, 8) is None
    assert containing(9, None) == ids[3]
    assert containing(9, 7) == ids[3]
    assert containing(8, None) is None
    assert db.containing_unit(conn, 2, 1, None) is None


def test_dirty_unit_accessors(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    ids = db.insert_units(conn, [_unit(1, [1]), _unit(1, [2]), _unit(1, [3])])
    assert db.count_dirty_units(conn) == 3
    assert [u.id for u in db.get_dirty_units(conn, 2)] == ids[:2]
    assert [u.id for u in db.get_dirty_units(conn, 2, after_id=ids[0])] == ids[1:]
    db.set_embedded(conn, ids[:2], "fake")
    assert db.count_dirty_units(conn) == 1
    assert [u.id for u in db.get_dirty_units(conn, 10)] == ids[2:]
    first, second, third = db.get_units_by_ids(conn, ids)
    assert (first.dirty, first.embedded_model) == (False, "fake")
    assert (second.dirty, second.embedded_model) == (False, "fake")
    assert (third.dirty, third.embedded_model) == (True, None)
    db.reset_embedded(conn)
    assert db.count_dirty_units(conn) == 3
    assert all(u.dirty and u.embedded_model is None for u in db.get_units_by_ids(conn, ids))
    assert not conn.in_transaction


# --- added by the review fixes --------------------------------------------------------------


def test_connect_creates_missing_directories_private(tmp_path: Path) -> None:
    paths = Paths.under(tmp_path / "fresh" / "nested")
    connection = db.connect(paths)
    try:
        assert paths.db_file.is_file()
        assert stat.S_IMODE(paths.db_file.parent.stat().st_mode) == 0o700
    finally:
        connection.close()


def test_last_sync_run_round_trip(conn: sqlite3.Connection) -> None:
    assert db.last_sync_run(conn) is None
    db.set_last_sync_run(conn, 1_700_000_000)
    assert db.last_sync_run(conn) == 1_700_000_000
    db.set_last_sync_run(conn, 1_700_000_060)
    assert db.last_sync_run(conn) == 1_700_000_060


def test_upsert_chat_keeps_discussion_of_when_the_new_row_has_none(
    conn: sqlite3.Connection,
) -> None:
    db.upsert_chat(conn, ChatRow(id=2, type="channel"))
    db.upsert_chat(conn, ChatRow(id=1, type="supergroup", source_id="a", discussion_of=2))
    kept = db.upsert_chat(conn, ChatRow(id=1, type="supergroup", title="t", source_id="b"))
    assert (kept.discussion_of, kept.source_id, kept.title) == (2, "b", "t")
    moved = db.upsert_chat(conn, ChatRow(id=1, type="supergroup", source_id="b", discussion_of=3))
    assert moved.discussion_of == 3
    plain = db.upsert_chat(conn, ChatRow(id=5, type="supergroup"))
    assert plain.discussion_of is None


def test_upsert_messages_rewrites_every_column_on_conflict(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat(1))
    first = MessageRow(chat_id=1, msg_id=5, date=100, from_id=1, from_name="Ann", text="a")
    (row_id,) = db.upsert_messages(conn, [first])
    second = MessageRow(
        chat_id=1,
        msg_id=5,
        date=101,
        edit_date=150,
        from_id=2,
        from_name="Bob",
        reply_to_msg_id=3,
        topic_id=9,
        fwd_from="Carol",
        text="b",
        media_kind="photo",
        media_filename="x.jpg",
        reactions_total=4,
    )
    assert db.upsert_messages(conn, [second]) == [row_id]
    assert db.get_message(conn, 1, 5) == dataclasses.replace(second, id=row_id)


def test_upsert_messages_keeps_a_stored_topic_and_comment_relation_when_a_row_has_none(
    conn: sqlite3.Connection,
) -> None:
    """A comment stored as one, re-read as part of the group's plain history: that read knows
    nothing of the channel it comments on, and must not unmap it."""
    db.upsert_chat(conn, _chat(1))
    comment = MessageRow(
        chat_id=1,
        msg_id=5,
        date=100,
        topic_id=9,
        comment_of_chat_id=77,
        comment_of_msg_id=4,
        text="a",
    )
    (row_id,) = db.upsert_messages(conn, [comment])
    plain = MessageRow(chat_id=1, msg_id=5, date=100, text="a (edited)")
    assert db.upsert_messages(conn, [plain]) == [row_id]
    assert db.get_message(conn, 1, 5) == dataclasses.replace(
        plain, id=row_id, topic_id=9, comment_of_chat_id=77, comment_of_msg_id=4
    )
    moved = dataclasses.replace(plain, topic_id=11, comment_of_chat_id=78, comment_of_msg_id=5)
    db.upsert_messages(conn, [moved])
    assert db.get_message(conn, 1, 5) == dataclasses.replace(moved, id=row_id)
    fresh = MessageRow(chat_id=1, msg_id=6, date=101, text="b")
    db.upsert_messages(conn, [fresh])
    stored = db.get_message(conn, 1, 6)
    assert stored is not None and stored.topic_id is None
    assert stored.comment_of_chat_id is None and stored.comment_of_msg_id is None
