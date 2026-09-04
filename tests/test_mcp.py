import asyncio
import json
import logging
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from telethon import errors
from telethon.tl import functions

from grepogram import config, db, embed, filters, index, links
from grepogram import mcp as tools
from grepogram import rerank as reranking
from grepogram import search as retrieval
from grepogram import sources as sourcing
from grepogram import sync as syncing
from grepogram.embed import FakeEmbedder, ModelUnavailable
from grepogram.filters import InvalidDate, UnknownChat
from grepogram.links import OpenFailed
from grepogram.log import shutdown_logging
from grepogram.models import (
    ChatRow,
    Config,
    Link,
    MessageRow,
    Source,
    SyncReport,
    TelegramCfg,
)
from grepogram.paths import Paths
from grepogram.rerank import FakeReranker
from grepogram.search import UnknownMessage
from grepogram.sources import AmbiguousTarget
from grepogram.sync import SyncInProgress, SyncLock
from grepogram.tg import AuthRequired, SessionMissing
from tests.fakes import FakeClient, make_channel, make_dialog, make_folder, make_user
from tests.fixtures import chat_ru, tl

ARG = chat_ru.ARG_ID
GEO = chat_ru.GEO_ID
NEWS_ID = -1001000000300
FORUM = -1001000000500
ARG_ENTITY = make_channel(1000000100, "Argentina chat", username="arg_chat", megagroup=True)
GEO_ENTITY = make_channel(1000000200, "Грузия | Georgia chat", megagroup=True)
NEWS = make_channel(1000000300, "News", username="news")
ME = make_user(42, "Me", "Myself", username="me")
KEYS = TelegramCfg(api_id=12345, api_hash="fakehash")
CFG = Config(
    telegram=KEYS, search=chat_ru.CFG.search, units=chat_ru.CFG.units, sources=chat_ru.CFG.sources
)
PAIR = chat_ru.PARAPHRASE
JUNE = filters.parse_when("2024-06")
NEW_TEXT = "Brubank теперь открывает счёт без DNI за час"
NO_VECTORS = f"dense search unavailable: {retrieval.NO_VECTORS}"


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    home = Paths.under(tmp_path / "home")
    home.ensure_dirs()
    home.session_file.touch()
    return home


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


def _client(**kwargs: object) -> FakeClient:
    dialogs = [make_dialog(ARG_ENTITY), make_dialog(GEO_ENTITY), make_dialog(NEWS)]
    kwargs.setdefault("folders", [make_folder(3, "Argentina", include=[ARG_ENTITY])])
    kwargs.setdefault("me", ME)
    return FakeClient(dialogs=dialogs, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def fake() -> FakeClient:
    return _client()


@pytest.fixture
def bind(
    paths: Paths, conn: sqlite3.Connection, fake: FakeClient
) -> Iterator[Callable[..., tools.AppState]]:
    """Bind an in-memory state for the tools; ``bind(cfg, client)`` rebinds with another one."""

    def make(cfg: Config = CFG, client: FakeClient | None = None) -> tools.AppState:
        chosen = fake if client is None else client
        state = tools.AppState(paths, cfg, conn, client_factory=lambda cfg, paths: chosen)
        tools.bind(state)
        return state

    yield make
    tools.unbind()


@pytest.fixture
def state(bind: Callable[..., tools.AppState], conn: sqlite3.Connection) -> tools.AppState:
    """The fixture chats, synced just now (no auto-sync on search)."""
    chat_ru.load(conn, synced_at=int(time.time()))
    return bind()


@pytest.fixture
def stale(bind: Callable[..., tools.AppState], conn: sqlite3.Connection) -> tools.AppState:
    """The fixture chats as synced in 2024, so every search wants a refresh first."""
    chat_ru.load(conn)
    return bind()


def _has(result: dict[str, object], chat_id: int, msg_id: int) -> bool:
    hits = result["hits"]
    assert isinstance(hits, list)
    return any(h["chat"]["id"] == chat_id and msg_id in h["msg_ids"] for h in hits)


def _connects(fake: FakeClient) -> int:
    return sum(1 for name, _ in fake.calls if name == "connect")


def _dialog_reads(fake: FakeClient) -> int:
    return sum(1 for name, _ in fake.calls if name == "get_dialogs")


# --- search ----------------------------------------------------------------------------------


async def test_search_returns_hits_with_urls(state: tools.AppState) -> None:
    result = await tools.search("DNI", mode="lexical")
    assert "error" not in result
    assert result["hits"] and result["warnings"] == [] and result["synced"] is False
    assert result["index_age_min"] == 0
    for hit in result["hits"]:
        assert hit["url"].startswith("https://t.me/")
        assert hit["chat"]["id"] in (ARG, GEO)
        assert hit["snippet"] and hit["text"] is None
        assert hit["anchor_msg_id"] in hit["msg_ids"]
    assert len((await tools.search("DNI", k=2, mode="lexical"))["hits"]) == 2
    arg_only = await tools.search("DNI", chats=["folder:Argentina"], mode="lexical")
    assert arg_only["hits"] and {h["chat"]["id"] for h in arg_only["hits"]} == {ARG}
    june = await tools.search("DNI", since="2024-06", mode="lexical", full=True)
    assert june["hits"] and all(h["date_start"] >= JUNE for h in june["hits"])
    assert all(h["text"] for h in june["hits"])
    assert json.loads(json.dumps(june, ensure_ascii=False)) == june


async def test_search_hybrid_loads_each_model_once(
    state: tools.AppState, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.embed_dirty_units(conn, FakeEmbedder())
    loads = {"embed": 0, "rerank": 0}

    def load_embedder(cfg: Config) -> FakeEmbedder:
        loads["embed"] += 1
        return FakeEmbedder()

    def load_reranker(cfg: Config) -> FakeReranker:
        loads["rerank"] += 1
        return FakeReranker()

    monkeypatch.setattr(embed, "load_embedder", load_embedder)
    monkeypatch.setattr(reranking, "load_reranker", load_reranker)
    first = await tools.search(PAIR.query_en)
    second = await tools.search(PAIR.query_en)
    assert first["warnings"] == [] and _has(first, PAIR.chat_id, PAIR.ru_msg_id)
    assert first["hits"][0]["score"] == 1.0
    assert second["hits"] == first["hits"]
    assert loads == {"embed": 1, "rerank": 1}
    lexical = await tools.search(PAIR.query_en, mode="lexical", rerank=False)
    assert lexical["hits"] and not _has(lexical, PAIR.chat_id, PAIR.ru_msg_id)
    assert loads == {"embed": 1, "rerank": 1}


async def test_search_without_vectors_never_loads_the_embedder(
    state: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(cfg: Config) -> FakeEmbedder:
        raise AssertionError("no vectors, no model")

    monkeypatch.setattr(embed, "load_embedder", never)
    result = await tools.search("DNI")
    assert result["hits"] and result["warnings"] == [NO_VECTORS]
    dense = await tools.search("DNI", mode="dense")
    assert dense["hits"] and dense["warnings"] == [NO_VECTORS]


async def test_search_without_models_warns_and_does_not_retry(
    state: tools.AppState, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.embed_dirty_units(conn, FakeEmbedder())
    attempts: list[Config] = []

    def offline(cfg: Config) -> FakeEmbedder:
        attempts.append(cfg)
        raise ModelUnavailable("torch is not installed")

    monkeypatch.setattr(embed, "load_embedder", offline)
    monkeypatch.setattr(reranking, "load_reranker", offline)
    for _ in range(2):
        result = await tools.search("DNI")
        assert result["hits"]
        assert result["warnings"] == [
            "dense search unavailable: torch is not installed",
            "reranking unavailable: torch is not installed",
        ]
    assert len(attempts) == 2
    assert state.embed_error == state.rerank_error == "torch is not installed"


async def test_search_bad_arguments_are_errors(state: tools.AppState) -> None:
    unknown = await tools.search("DNI", chats=["nowhere"])
    assert unknown["error"].startswith("no indexed chat matches 'nowhere'")
    assert unknown["hint"] == tools.CHAT_HINT
    assert any("Argentina chat" in c for c in unknown["candidates"])
    assert "hits" not in unknown
    bad_date = await tools.search("DNI", since="yesterday")
    assert "cannot read date 'yesterday'" in bad_date["error"] and "7d" in bad_date["error"]
    assert bad_date["hint"] is None
    inverted = await tools.search("DNI", since="2025-01-01", until="2024-01-01")
    assert "lies after" in inverted["error"]
    bogus = await tools.search("DNI", mode="bogus")  # type: ignore[arg-type]
    assert "unknown search mode" in bogus["error"]
    assert "k must be positive" in (await tools.search("DNI", k=0))["error"]


# --- staleness -------------------------------------------------------------------------------


async def test_search_on_an_empty_index_names_what_is_missing(
    bind: Callable[..., tools.AppState],
) -> None:
    bind(Config(telegram=KEYS))
    unconfigured = await tools.search("DNI")
    assert unconfigured["hits"] == [] and unconfigured["warnings"] == [retrieval.NO_SOURCES]
    assert unconfigured["synced"] is False and unconfigured["index_age_min"] is None
    bind()
    unsynced = await tools.search("DNI")
    assert unsynced["hits"] == [] and unsynced["warnings"] == [retrieval.NOTHING_INDEXED]


async def test_stale_index_is_synced_once_before_searching(
    stale: tools.AppState, fake: FakeClient, conn: sqlite3.Connection
) -> None:
    fake.messages[ARG] = [tl.message(ARG, 43, NEW_TEXT, sender=1)]
    result = await tools.search("Brubank", mode="lexical")
    assert "error" not in result
    assert result["synced"] is True and result["warnings"] == []
    assert result["index_age_min"] == 0
    assert _has(result, ARG, 43)
    assert _connects(fake) == 1
    assert db.has_vectors(conn)
    again = await tools.search("Brubank", mode="lexical")
    assert again["synced"] is False and _has(again, ARG, 43)
    assert _connects(fake) == 1


async def test_failing_auto_sync_yields_warnings_not_errors(
    stale: tools.AppState,
    fake: FakeClient,
    paths: Paths,
    bind: Callable[..., tools.AppState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    real_sync = syncing.sync_all

    async def busy(*args: object) -> SyncReport:
        calls.append(args)
        raise SyncInProgress("another sync is running (lock held)")

    monkeypatch.setattr(syncing, "sync_all", busy)
    result = await tools.search("DNI", mode="lexical")
    assert "error" not in result and result["hits"]
    assert result["synced"] is False
    assert result["warnings"] == ["auto-sync skipped: another sync is running (lock held)"]
    assert len(calls) == 1
    assert calls[0][0] is fake and calls[0][1] is stale.conn and calls[0][3] is paths
    budget = calls[0][4]
    assert isinstance(budget, syncing.SyncBudget)
    assert budget.seconds == CFG.search.auto_sync_budget_s
    assert isinstance(calls[0][5], FakeEmbedder)

    async def flooded(*args: object) -> SyncReport:
        raise errors.FloodWaitError(request=None, capture=30)

    monkeypatch.setattr(syncing, "sync_all", flooded)
    result = await tools.search("DNI", mode="lexical")
    assert result["hits"] and len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("auto-sync skipped: telegram error: ")
    assert "30" in result["warnings"][0]

    async def partial(*args: object) -> SyncReport:
        return SyncReport(new=3, chats_done=[GEO], chats_remaining=[ARG], warnings=["slow"])

    monkeypatch.setattr(syncing, "sync_all", partial)
    result = await tools.search("DNI", mode="lexical")
    assert result["synced"] is True and result["hits"]
    assert result["warnings"] == [
        "auto-sync: slow",
        f"auto-sync stopped after {CFG.search.auto_sync_budget_s}s with 1 chats still behind; "
        "call sync to finish",
    ]

    monkeypatch.setattr(syncing, "sync_all", real_sync)
    fake.authorized = False
    result = await tools.search("DNI", mode="lexical")
    assert result["hits"] and result["synced"] is False
    assert result["warnings"] == ["auto-sync skipped: Telegram session is not authorized"]
    fake.authorized = True

    bind(Config(search=CFG.search, units=CFG.units, sources=CFG.sources))
    result = await tools.search("DNI", mode="lexical")
    assert result["hits"] and result["warnings"] == [
        f"auto-sync skipped: [telegram] api_id and api_hash are not set in {paths.config_file}"
    ]

    bind()
    paths.session_file.unlink()
    result = await tools.search("DNI", mode="lexical")
    assert result["hits"] and result["synced"] is False
    assert result["warnings"] == [f"auto-sync skipped: no Telegram session at {paths.session_file}"]


# --- sync ------------------------------------------------------------------------------------


async def test_sync_fetches_new_messages_and_reports(
    state: tools.AppState, fake: FakeClient, conn: sqlite3.Connection
) -> None:
    fake.messages[ARG] = [tl.message(ARG, 43, NEW_TEXT, sender=1)]
    report = await tools.sync(budget_s=10)
    assert "error" not in report
    assert report["new"] == 1 and set(report["chats_done"]) == {ARG, GEO}
    assert report["chats_remaining"] == [] and report["unavailable"] == []
    assert report["warnings"] == [] and report["index_age_min"] == 0
    assert db.get_message(conn, ARG, 43) is not None
    assert db.has_vectors(conn)
    assert fake.calls[-1] == ("disconnect", {})
    assert _has(await tools.search("Brubank", mode="lexical"), ARG, 43)


async def test_sync_without_the_model_warns(
    state: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def offline(cfg: Config) -> FakeEmbedder:
        raise ModelUnavailable("torch is not installed")

    monkeypatch.setattr(embed, "load_embedder", offline)
    report = await tools.sync()
    assert "error" not in report
    assert report["warnings"] == ["dense index not updated: torch is not installed"]
    assert not db.has_vectors(state.conn)


async def test_sync_errors_carry_hints(
    state: tools.AppState,
    fake: FakeClient,
    paths: Paths,
    bind: Callable[..., tools.AppState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (await tools.sync(budget_s=0))["error"].startswith("budget_s must be")
    with SyncLock(paths):
        busy = await tools.sync()
    assert busy["error"].startswith("another sync is running")
    assert busy["hint"] == tools.LOCK_HINT
    fake.authorized = False
    assert await tools.sync() == {
        "error": "Telegram session is not authorized",
        "hint": tools.AUTH_HINT,
    }
    fake.authorized = True
    flooded = _client(
        responses={
            functions.messages.GetDialogFiltersRequest: errors.FloodWaitError(
                request=None, capture=30
            )
        }
    )
    bind(CFG, flooded)
    result = await tools.sync()
    assert result["error"].startswith("telegram error: ") and "30" in result["error"]
    assert result["hint"] is None
    offline = _client()

    async def failing_connect() -> None:
        raise ConnectionError("offline")

    monkeypatch.setattr(offline, "connect", failing_connect)
    bind(CFG, offline)
    assert (await tools.sync())["error"] == "connection error: offline"
    bind(Config(search=CFG.search, units=CFG.units, sources=CFG.sources))
    unset = await tools.sync()
    assert "api_id and api_hash are not set" in unset["error"]
    assert "my.telegram.org" in unset["hint"]
    bind(Config(telegram=KEYS))
    assert await tools.sync() == {
        "error": "no sources are configured",
        "hint": tools.NO_SOURCES_HINT,
    }
    bind()
    paths.session_file.unlink()
    missing = await tools.sync()
    assert missing["error"] == f"no Telegram session at {paths.session_file}"
    assert missing["hint"] == tools.AUTH_HINT


# --- sources and dialogs ---------------------------------------------------------------------


def test_sources_lists_status(state: tools.AppState) -> None:
    result = tools.sources()
    assert result["index_age_min"] == 0
    by_id = {s["source_id"]: s for s in result["sources"]}
    assert list(by_id) == ["folder:Argentina", f"chat:{GEO}"]
    (arg,) = by_id["folder:Argentina"]["chats"]
    assert arg["id"] == ARG and arg["message_count"] == 42
    assert arg["username"] == "arg_chat" and arg["unavailable"] is False
    assert (
        arg["last_sync_at"]
        == state.conn.execute("SELECT last_sync_at FROM chats WHERE id = ?", (ARG,)).fetchone()[0]
    )
    (geo,) = by_id[f"chat:{GEO}"]["chats"]
    assert geo["message_count"] == 20 and geo["type"] == "supergroup"


async def test_dialogs_matches_chats_and_folders(state: tools.AppState, fake: FakeClient) -> None:
    result = await tools.dialogs("arg")
    assert result["query"] == "arg"
    by_key = {(m["kind"], m["title"]): m for m in result["matches"]}
    folder = by_key[("folder", "Argentina")]
    assert folder["target"] == "folder:Argentina" and folder["type"] == "folder"
    assert folder["username"] is None and folder["folders"] == []
    chat = by_key[("dialog", "Argentina chat")]
    assert chat == {
        "kind": "dialog",
        "id": ARG,
        "title": "Argentina chat",
        "type": "supergroup",
        "username": "arg_chat",
        "folders": ["Argentina"],
        "score": chat["score"],
        "target": "@arg_chat",
    }
    assert 0 < chat["score"] <= 1
    assert (await tools.dialogs("zzz"))["matches"] == []
    georgia = (await tools.dialogs("georgia"))["matches"]
    assert [m["target"] for m in georgia] == [str(GEO)]
    assert _dialog_reads(fake) == 1
    assert _connects(fake) == 3
    fake.authorized = False
    denied = await tools.dialogs("arg")
    assert denied["hint"] == tools.AUTH_HINT and "matches" not in denied


async def test_sources_add_fuzzy_writes_config_and_invalidates_the_catalog(
    bind: Callable[..., tools.AppState], paths: Paths, fake: FakeClient
) -> None:
    state = bind(Config(telegram=KEYS))
    await tools.dialogs("arg")
    assert _dialog_reads(fake) == 1
    added = await tools.sources_add("georgia")
    assert "error" not in added
    assert added["source"] == {
        "id": f"chat:{GEO}",
        "folder": None,
        "chat": GEO,
        "since": None,
        "comments": False,
    }
    assert added["kind"] == "chat" and added["title"] == "Грузия | Georgia chat"
    assert [c["id"] for c in added["chats"]] == [GEO]
    assert added["hint"] == tools.SYNC_NEXT_HINT
    assert config.load(paths).sources == [Source(chat=GEO)]
    assert state.cfg.sources == [Source(chat=GEO)]
    assert stat.S_IMODE(paths.config_file.stat().st_mode) == 0o600
    assert _dialog_reads(fake) == 1
    await tools.dialogs("arg")
    assert _dialog_reads(fake) == 2
    folder = await tools.sources_add("folder:Argentina", since="2024-01-01", comments=True)
    assert folder["kind"] == "folder" and folder["source"]["id"] == "folder:Argentina"
    assert folder["title"] == "Argentina" and [c["id"] for c in folder["chats"]] == [ARG]
    expected = [Source(chat=GEO), Source(folder="Argentina", since="2024-01-01", comments=True)]
    assert config.load(paths).sources == expected
    ambiguous = await tools.sources_add("arg")
    assert "be more specific" in ambiguous["error"] and ambiguous["hint"] == tools.PICK_HINT
    assert any(c.startswith("folder 'Argentina'") for c in ambiguous["candidates"])
    duplicate = await tools.sources_add(str(GEO))
    assert duplicate["error"] == f"chat:{GEO} is already a source"
    assert "invite links" in (await tools.sources_add("https://t.me/+abc"))["error"]
    assert "channels only" in (await tools.sources_add("@arg_chat", comments=True))["error"]
    assert "ISO date" in (await tools.sources_add("@news", since="jan"))["error"]
    assert config.load(paths).sources == expected


async def test_sources_remove_deletes_data_and_saves_the_config(
    state: tools.AppState, paths: Paths, conn: sqlite3.Connection
) -> None:
    through_folder = tools.sources_remove("@arg_chat")
    assert "indexed through folder:Argentina" in through_folder["error"]
    removed = tools.sources_remove("folder:Argentina")
    assert removed == {
        "source_id": "folder:Argentina",
        "removed_chat_ids": [ARG],
        "config_updated": True,
    }
    assert db.get_chat(conn, ARG) is None and db.get_chat(conn, GEO) is not None
    assert config.load(paths).sources == [Source(chat=GEO)]
    assert state.cfg.sources == [Source(chat=GEO)]
    assert (await tools.search("DNI", mode="lexical"))["hits"] == []
    result = await tools.search("TBC", mode="lexical")
    assert result["hits"] and {h["chat"]["id"] for h in result["hits"]} == {GEO}
    unknown = tools.sources_remove("folder:Nowhere")
    assert unknown["error"].startswith("no folder source named 'Nowhere'")
    assert unknown["hint"] is None


def test_config_is_reread_when_the_file_changes(state: tools.AppState, paths: Paths) -> None:
    assert [s["source_id"] for s in tools.sources()["sources"]] == [
        "folder:Argentina",
        f"chat:{GEO}",
    ]
    config.save(Config(telegram=KEYS, sources=[Source(chat="@news")]), paths)
    assert [s["source_id"] for s in tools.sources()["sources"]] == [
        "chat:@news",
        f"chat:{GEO}",
        "folder:Argentina",
    ]
    assert state.cfg.sources == [Source(chat="@news")]
    paths.config_file.write_text("[telegram\n", encoding="utf-8")
    assert "invalid TOML" in tools.sources()["error"]


def test_app_state_open_reads_config_and_migrates(paths: Paths) -> None:
    config.save(Config(telegram=KEYS), paths)
    state = tools.AppState.open(paths)
    try:
        assert state.cfg.telegram == KEYS
        assert db.schema_version(state.conn) >= 1
        assert paths.db_file.exists()
    finally:
        state.close()


# --- readers and links -----------------------------------------------------------------------


def test_thread_and_context_read_messages(state: tools.AppState) -> None:
    result = tools.thread(ARG, 5)
    assert result["chat_id"] == ARG and result["msg_id"] == 5
    assert [m["msg_id"] for m in result["messages"]] == [1, 2, 3, 4, 5, 6, 7, 10]
    assert result["messages"][0]["url"] == "https://t.me/arg_chat/1"
    assert set(result["messages"][0]) == {
        "msg_id",
        "date",
        "from_name",
        "text",
        "url",
        "fallback_url",
        "reply_to_msg_id",
    }
    around = tools.context(ARG, 5, before=1, after=1)
    assert [m["msg_id"] for m in around["messages"]] == [4, 5, 6]
    assert len(tools.context(ARG, 20)["messages"]) == 31
    assert tools.thread(ARG, 999) == {
        "error": f"message 999 of chat {ARG} is not indexed",
        "hint": tools.MESSAGE_HINT,
    }
    assert tools.context(12345, 1)["hint"] == tools.MESSAGE_HINT
    assert "negative" in tools.context(ARG, 5, before=-1)["error"]


def test_open_message_returns_the_url_used(
    state: tools.AppState, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Link] = []

    def open_link(link: Link) -> str:
        opened.append(link)
        return link.url

    monkeypatch.setattr(links, "open_link", open_link)
    assert tools.open_message(ARG, 5) == {
        "chat_id": ARG,
        "msg_id": 5,
        "url": "https://t.me/arg_chat/5",
        "fallback_url": None,
        "opened": True,
    }
    assert opened == [Link("https://t.me/arg_chat/5")]
    assert tools.open_message(GEO, 3)["url"] == "https://t.me/c/1000000200/3"
    db.upsert_chat(
        conn,
        ChatRow(id=FORUM, type="supergroup", title="Forum", username="forum_chat", is_forum=True),
    )
    db.upsert_chat(conn, ChatRow(id=7, type="user", title="Bob"))
    db.upsert_messages(
        conn,
        [
            MessageRow(chat_id=FORUM, msg_id=105, date=1_700_000_000, text="hi", topic_id=100),
            MessageRow(chat_id=7, msg_id=9, date=1_700_000_000, text="hi"),
        ],
    )
    assert tools.open_message(FORUM, 105)["url"] == "https://t.me/forum_chat/100/105"
    private = tools.open_message(7, 9)
    assert private["url"] == "tg://openmessage?user_id=7&message_id=9"
    assert private["fallback_url"] == "tg://user?id=7"
    assert tools.open_message(ARG, 999)["hint"] == tools.MESSAGE_HINT
    assert len(opened) == 4


def test_open_message_keeps_the_url_when_open_fails(
    state: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(link: Link) -> str:
        raise OpenFailed(f"open failed for {link.url}: no application")

    monkeypatch.setattr(links, "open_link", broken)
    result = tools.open_message(ARG, 5)
    assert result["url"] == "https://t.me/arg_chat/5" and result["opened"] is False
    assert result["error"].startswith("open failed") and result["hint"] == tools.OPEN_HINT

    def elsewhere(link: Link) -> str:
        raise NotImplementedError("opening links needs macOS 'open' (linux)")

    monkeypatch.setattr(links, "open_link", elsewhere)
    result = tools.open_message(ARG, 5)
    assert result["opened"] is False and "macOS" in result["error"]
    assert result["url"] == "https://t.me/arg_chat/5"


# --- failures --------------------------------------------------------------------------------


FLOOD = errors.FloodWaitError(request=None, capture=30)


@pytest.mark.parametrize(
    ("exc", "error", "hint"),
    [
        (AuthRequired(), "Telegram session is not authorized", tools.AUTH_HINT),
        (
            SessionMissing(Path("/x/session.session")),
            "no Telegram session at /x/session.session",
            tools.AUTH_HINT,
        ),
        (
            tools.NotConfigured(Path("/x/config.toml")),
            "[telegram] api_id and api_hash are not set in /x/config.toml",
            tools.SETUP_HINT,
        ),
        (SyncInProgress("busy"), "busy", tools.LOCK_HINT),
        (ModelUnavailable("no torch"), "no torch", tools.MODEL_HINT),
        (UnknownMessage(1, 2), "message 2 of chat 1 is not indexed", tools.MESSAGE_HINT),
        (InvalidDate("bad"), "bad", None),
        (FLOOD, f"telegram error: {FLOOD}", None),
        (ConnectionError("offline"), "connection error: offline", None),
        (ValueError("k must be positive"), "k must be positive", None),
    ],
)
def test_failure_results(exc: Exception, error: str, hint: str | None) -> None:
    assert tools.failure(exc) == {"error": error, "hint": hint}


def test_failure_results_carry_candidates() -> None:
    unknown = UnknownChat("x", ["folder:A (1 chats)", "'B' (id 2)"])
    assert tools.failure(unknown) == {
        "error": str(unknown),
        "hint": tools.CHAT_HINT,
        "candidates": ["folder:A (1 chats)", "'B' (id 2)"],
    }
    hinted = UnknownChat("x", [], hint="source folder:A is configured but has no indexed chats")
    assert tools.failure(hinted) == {"error": str(hinted), "hint": hinted.hint}
    ambiguous = AmbiguousTarget("arg", ["a", "b"])
    assert tools.failure(ambiguous) == {
        "error": str(ambiguous),
        "hint": tools.PICK_HINT,
        "candidates": ["a", "b"],
    }


def test_unexpected_errors_propagate(
    state: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("bug")

    monkeypatch.setattr(retrieval, "thread", boom)
    with pytest.raises(RuntimeError, match="bug"):
        tools.thread(ARG, 5)
    tools.unbind()
    with pytest.raises(RuntimeError, match="no AppState is bound"):
        tools.sources()


# --- threads and stdout ----------------------------------------------------------------------


def test_tools_work_from_a_worker_thread(state: tools.AppState) -> None:
    results: dict[str, dict[str, object]] = {}

    def work() -> None:
        results["thread"] = tools.thread(ARG, 5)
        results["search"] = asyncio.run(tools.search("DNI", mode="lexical"))
        results["sources"] = tools.sources()

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    assert len(results["thread"]["messages"]) == 8  # type: ignore[arg-type]
    assert results["search"]["hits"] and "error" not in results["search"]
    assert results["sources"]["sources"]


async def test_tools_never_write_to_stdout(
    state: tools.AppState, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    real_search = retrieval.search
    real_sync = syncing.sync_all
    real_add = sourcing.add_source

    def noisy_search(*args: object, **kwargs: object) -> object:
        print("search noise")
        return real_search(*args, **kwargs)  # type: ignore[arg-type]

    async def noisy_sync(*args: object, **kwargs: object) -> object:
        print("sync noise")
        return await real_sync(*args, **kwargs)  # type: ignore[arg-type]

    async def noisy_add(*args: object, **kwargs: object) -> object:
        print("add noise")
        return await real_add(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(retrieval, "search", noisy_search)
    monkeypatch.setattr(syncing, "sync_all", noisy_sync)
    monkeypatch.setattr(sourcing, "add_source", noisy_add)
    assert (await tools.search("DNI", mode="lexical"))["hits"]
    assert "error" not in await tools.sync()
    assert "error" not in await tools.sources_add("@news")
    captured = capfd.readouterr()
    assert captured.out == ""
    noise = [line for line in captured.err.splitlines() if line.endswith("noise")]
    assert noise == ["search noise", "sync noise", "add noise"]
    assert not tools.stdout_to_stderr.active
    print("after")
    assert capfd.readouterr().out == "after\n"


def test_stdout_guard_holds_until_the_last_block_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    guard = tools.StdoutGuard()
    before = sys.stdout
    with guard:
        with guard:
            pass
        assert guard.active and sys.stdout is sys.stderr
        print("inside")
    assert not guard.active and sys.stdout is before
    print("outside")
    captured = capsys.readouterr()
    assert captured.out == "outside\n" and captured.err == "inside\n"
    guard.__enter__()
    guard.__enter__()
    guard.__exit__(None, None, None)
    assert sys.stdout is sys.stderr
    guard.__exit__(None, None, None)
    assert sys.stdout is before


# --- server ----------------------------------------------------------------------------------


def test_instructions_carry_the_playbook() -> None:
    text = tools.INSTRUCTIONS
    assert "2-3 query variants" in text and "Russian and English" in text
    assert "recent" in text and "date" in text
    assert "`thread`" in text and "`context`" in text
    assert "`url`" in text
    assert "say so rather than guess" in text
    assert "`sources`" in text and "`dialogs`" in text and "`sources_add`" in text


async def test_server_lists_the_nine_tools_over_a_session(state: tools.AppState) -> None:
    server = tools.build_server()
    assert server.name == "grepogram" and server.instructions == tools.INSTRUCTIONS
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        by_name = {tool.name: tool for tool in listed.tools}
        assert len(by_name) == 9 and sorted(by_name) == sorted(tools.TOOL_NAMES)
        search_tool = by_name["search"]
        assert search_tool.description is not None
        assert search_tool.description.startswith("Search the indexed Telegram chats")
        assert "7d" in search_tool.description and "folder:<name>" in search_tool.description
        properties = search_tool.inputSchema["properties"]
        assert properties["mode"]["enum"] == ["hybrid", "lexical", "dense"]
        assert properties["k"]["default"] == 10 and properties["rerank"]["default"] is True
        assert search_tool.inputSchema["required"] == ["query"]
        assert by_name["sync"].inputSchema["properties"]["budget_s"]["default"] == 45
        assert by_name["context"].inputSchema["properties"]["before"]["default"] == 15
        assert by_name["sources"].inputSchema["properties"] == {}
        for tool in listed.tools:
            assert tool.description
        result = await session.call_tool("search", {"query": "DNI", "mode": "lexical", "k": 2})
        assert not result.isError
        assert result.structuredContent is not None
        assert len(result.structuredContent["hits"]) == 2
        text = json.loads(result.content[0].text)  # type: ignore[union-attr]
        assert text["hits"][0]["url"].startswith("https://t.me/")
        failed = await session.call_tool("thread", {"chat_id": ARG, "msg_id": 999})
        assert not failed.isError and failed.structuredContent is not None
        assert failed.structuredContent["error"].endswith("is not indexed")
        invalid = await session.call_tool("search", {"query": "DNI", "mode": "bogus"})
        assert invalid.isError
        status = await session.call_tool("sources", {})
        assert status.structuredContent is not None
        assert [s["source_id"] for s in status.structuredContent["sources"]] == [
            "folder:Argentina",
            f"chat:{GEO}",
        ]


def test_main_binds_the_state_and_serves_stdio(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    paths = Paths.from_env()
    config.save(Config(telegram=KEYS), paths)
    seen: dict[str, object] = {}

    def fake_run(self: FastMCP, transport: str = "stdio", mount_path: str | None = None) -> None:
        seen["transport"] = transport
        seen["bound"] = tools._bound
        seen["redirected"] = sys.stdout is sys.stderr
        seen["instructions"] = self.instructions
        print("protocol")

    real_build = tools.build_server

    def noisy_build() -> FastMCP:
        print("startup noise")
        return real_build()

    monkeypatch.setattr(FastMCP, "run", fake_run)
    monkeypatch.setattr(tools, "build_server", noisy_build)
    mcp_logger = logging.getLogger("mcp")
    level = mcp_logger.level
    try:
        tools.main([])
    finally:
        mcp_logger.setLevel(level)
    assert seen["transport"] == "stdio" and seen["redirected"] is False
    assert seen["instructions"] == tools.INSTRUCTIONS
    bound = seen["bound"]
    assert isinstance(bound, tools.AppState) and bound.cfg.telegram == KEYS
    assert tools._bound is None
    assert paths.db_file.exists() and paths.log_file.exists()
    captured = capfd.readouterr()
    assert captured.out == "protocol\n"
    assert "startup noise" in captured.err
    root = logging.getLogger()
    assert root.handlers
    assert all(getattr(h, "stream", None) is not sys.stdout for h in root.handlers)


def test_main_stops_on_a_broken_config(tmp_home: Path, capfd: pytest.CaptureFixture[str]) -> None:
    Paths.from_env().config_file.write_text("[telegram\n", encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        tools.main([])
    assert "invalid TOML" in str(info.value)
    assert tools._bound is None
    assert capfd.readouterr().out == ""
