"""A v0.2.0 home upgrades by being used.

What v0.2.0 left in the field is a schema-6 ``index.db`` (a synced channel beside imported
chats), one ``session.session`` and a ``config.toml`` with no ``[[accounts]]``. The first
command after the upgrade migrates the index in place, and sync, search and the source commands
then answer as they did before, through the implicit ``default`` account — with no user action
and without touching the imported history Telegram cannot serve again.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grepogram import cli, db, tg
from grepogram.models import DEFAULT_ACCOUNT
from grepogram.paths import Paths
from tests.fakes import FakeClient, FakeWorld, make_channel, make_user
from tests.fixtures import tl

runner = CliRunner()

NEWS = make_channel(700, "News", username="news")
NEWS_PEER = -1000000000700
EXPORT = Path(__file__).resolve().parent / "fixtures" / "tdesktop_export.json"
EXPATS_ID = -1001234567890
BOAT_ID = -987654
IMPORTS = {EXPATS_ID: "import:valencia-expats", BOAT_ID: "import:двое-в-лодке"}
V020_CONFIG = '[telegram]\napi_id = 12345\napi_hash = "fakehash"\n\n[[sources]]\nchat = "@news"\n'
POSTS = {
    1: "Brubank opens accounts without a DNI",
    2: "Mercado Pago raises its transfer fees",
    3: "Uala launches a travel card",
}
V6_TABLES = ("chats", "messages", "units", "msg_fts", "unit_fts", "unit_vec")
"""Every table v0.2.0 wrote rows to; step 7 and step 8 add columns and tables, none of these."""
LATER_META = (
    db.META_SCHEMA_VERSION,
    db.META_LINKS_CAPTURED_FROM,
    db.META_LEAD_CLOCK,
    db.META_INDEX_ID,
    db.META_SYNTHETIC_NEXT,
)
"""The meta keys a v0.2.0 index cannot hold: its version, step 8's capture marker and step 9's
lead clock, index name and synthetic-id mark."""


def _client(posts: int) -> FakeClient:
    """The default account, a member of ``@news``, whose first ``posts`` posts exist."""
    world = FakeWorld(
        entities=[NEWS],
        messages={
            NEWS_PEER: [tl.channel_post(NEWS_PEER, i, POSTS[i]) for i in range(1, posts + 1)]
        },
    )
    return world.client(members=[NEWS], me=make_user(42, "Me"))


def _home(root: Path) -> Paths:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text(V020_CONFIG, encoding="utf-8")
    paths = Paths.under(root)
    paths.ensure_dirs()
    paths.session_file.touch()
    return paths


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _v020_home(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Paths, dict[str, list[tuple[object, ...]]]]:
    """A v0.2.0 home at ``tmp_home``, and the imported chats' rows as they stand in it.

    The rows are the ones grepogram writes: a scratch home syncs ``@news`` (two posts) and
    imports the Telegram Desktop export through the CLI, and the index is then rebuilt at schema
    6 from the released steps, keeping exactly the columns and tables v0.2.0 had — which is what
    an index written by v0.2.0 holds, byte for byte in every column that existed then.
    """
    scratch = _home(tmp_path / "scratch")
    monkeypatch.setenv("GREPOGRAM_HOME", str(scratch.config_file.parent))
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths, account=DEFAULT_ACCOUNT: _client(2))
    synced = runner.invoke(cli.app, ["sync"])
    assert synced.exit_code == 0, synced.output
    imported = runner.invoke(cli.app, ["import", str(EXPORT)])
    assert imported.exit_code == 0, imported.output

    monkeypatch.setenv("GREPOGRAM_HOME", str(tmp_home))
    paths = _home(tmp_home)
    old = db.connect(paths)
    try:
        for version in (db.BASE_VERSION, 6):
            for statement in db.MIGRATIONS[version]:
                old.execute(statement)
        old.execute("ATTACH DATABASE ? AS src", (str(scratch.db_file),))
        (dim,) = old.execute("SELECT length(embedding) / 4 FROM src.unit_vec LIMIT 1").fetchone()
        db.ensure_vec_table(old, dim)
        for table in V6_TABLES:
            columns = ", ".join(["rowid", *_columns(old, "main", table)])
            old.execute(f"INSERT INTO main.{table}({columns}) SELECT {columns} FROM src.{table}")
        old.execute(
            f"INSERT INTO main.meta(key, value) SELECT key, value FROM src.meta "
            f"WHERE key NOT IN ({', '.join('?' for _ in LATER_META)})",
            LATER_META,
        )
        old.execute("DETACH DATABASE src")
        db.set_meta(old, db.META_SCHEMA_VERSION, "6")
        assert db.schema_version(old) == 6
        assert "peer_id" not in _columns(old, "main", "chats")
        assert old.execute("SELECT count(*) FROM messages WHERE indexed = 0").fetchone()[0] == 0
        imports = _import_rows(old)
    finally:
        old.close()
    return paths, imports


def _import_rows(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """The imported chats' v0.2.0 columns, messages, units and index rows."""
    ids = ", ".join(str(chat_id) for chat_id in IMPORTS)
    unit_ids = f"SELECT id FROM units WHERE chat_id IN ({ids})"
    queries = {
        "chats": "SELECT id, type, title, username, source_id, last_msg_id, last_sync_at, "
        f"unavailable FROM chats WHERE id IN ({ids}) ORDER BY id",
        "messages": "SELECT id, chat_id, msg_id, date, text, indexed FROM messages "
        f"WHERE chat_id IN ({ids}) ORDER BY id",
        "units": f"SELECT id, chat_id, kind, text FROM units WHERE chat_id IN ({ids}) ORDER BY id",
        "unit_fts": f"SELECT rowid, raw FROM unit_fts WHERE rowid IN ({unit_ids}) ORDER BY rowid",
        "unit_vec": f"SELECT rowid, embedding FROM unit_vec WHERE rowid IN ({unit_ids}) "
        "ORDER BY rowid",
    }
    return {name: [tuple(row) for row in conn.execute(query)] for name, query in queries.items()}


def _search(*args: str) -> list[dict[str, object]]:
    result = runner.invoke(cli.app, ["search", *args, "--mode", "lexical", "--json"])
    assert result.exit_code == 0, result.output
    hits: list[dict[str, object]] = json.loads(result.stdout)["hits"]
    return hits


def test_a_v020_home_migrates_and_syncs_and_searches_unchanged(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, imports = _v020_home(tmp_home, tmp_path, monkeypatch)

    # the first command migrates in place and answers from what v0.2.0 indexed
    (old,) = _search("Brubank")
    assert (old["chat"]["id"], old["peer_id"], old["accounts"]) == (  # type: ignore[index]
        NEWS_PEER,
        NEWS_PEER,
        [DEFAULT_ACCOUNT],
    )
    text = runner.invoke(cli.app, ["search", "Brubank", "--mode", "lexical"])
    assert text.exit_code == 0 and "via" not in text.stdout, "one account prints no provenance"
    (valencia,) = {h["chat"]["id"] for h in _search("ВНЖ")}  # type: ignore[index]
    assert valencia == EXPATS_ID
    listed = runner.invoke(cli.app, ["sources", "ls"])
    assert listed.exit_code == 0, listed.output
    assert "chat:@news" in listed.stdout and "import:valencia-expats" in listed.stdout

    # the one session syncs as the default account, and says nothing about accounts
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths, account=DEFAULT_ACCOUNT: _client(3))
    synced = runner.invoke(cli.app, ["sync"])
    assert synced.exit_code == 0, synced.output
    assert "new messages: 1" in synced.stdout
    assert "account" not in synced.stderr
    (new,) = _search("Uala")
    assert new["chat"]["id"] == NEWS_PEER and new["accounts"] == [DEFAULT_ACCOUNT]  # type: ignore[index]

    conn = db.connect(paths)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        news = db.get_chat_by_peer(conn, NEWS_PEER, "")
        assert news is not None and news.id == NEWS_PEER and news.source_id == "chat:@news"
        assert news.last_msg_id == 3
        assert db.chat_source_ids(conn, NEWS_PEER) == ["chat:@news"]
        assert db.chat_accounts(conn, NEWS_PEER) == [DEFAULT_ACCOUNT]
        assert db.access_hash(conn, NEWS_PEER, DEFAULT_ACCOUNT) is not None, "learned by the sync"
        assert db.message_counts(conn) == {NEWS_PEER: 3, EXPATS_ID: 3, BOAT_ID: 3}
        # the imports are exactly what v0.2.0 held: no account reaches them, nothing re-cut
        assert _import_rows(conn) == imports
        for chat_id, tag in IMPORTS.items():
            assert db.chat_source_ids(conn, chat_id) == [tag]
            assert db.chat_accounts(conn, chat_id) == []
        boat = db.get_chat(conn, BOAT_ID)
        assert boat is not None and (boat.peer_id, boat.scope) == (BOAT_ID, DEFAULT_ACCOUNT)
        assert [row.name for row in db.list_accounts(conn)] == [DEFAULT_ACCOUNT]
    finally:
        conn.close()

    # no user action: the config is the one v0.2.0 wrote and the one session is still used
    assert paths.config_file.read_text(encoding="utf-8") == V020_CONFIG
    assert not (paths.config_file.parent / "sessions").exists() or not any(
        (paths.config_file.parent / "sessions").iterdir()
    )
    accounts = runner.invoke(cli.app, ["accounts", "ls"])
    assert accounts.exit_code == 0, accounts.output
    header, row = accounts.stdout.splitlines()
    assert row.split()[:3] == [DEFAULT_ACCOUNT, "-", "authorized"]
