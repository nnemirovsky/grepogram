import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from telethon import errors
from telethon.tl import functions, types
from telethon.tl.types import messages as tl_messages

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
from grepogram.tg import AuthRequired, SessionError, SessionMissing
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


async def test_search_without_rerank_never_loads_the_reranker(
    state: tools.AppState, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.embed_dirty_units(conn, FakeEmbedder())

    def never(cfg: Config) -> FakeReranker:
        raise AssertionError("rerank=False must not load the reranker")

    monkeypatch.setattr(reranking, "load_reranker", never)
    result = await tools.search(PAIR.query_en, rerank=False)
    assert result["hits"] and result["warnings"] == []
    assert state.rerank_error is None


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


async def test_auto_sync_under_a_held_lock_is_a_warning(
    stale: tools.AppState, fake: FakeClient, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []

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


async def test_auto_sync_flood_wait_is_a_warning(
    stale: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def flooded(*args: object) -> SyncReport:
        raise errors.FloodWaitError(request=None, capture=30)

    monkeypatch.setattr(syncing, "sync_all", flooded)
    result = await tools.search("DNI", mode="lexical")
    assert result["hits"] and len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("auto-sync skipped: telegram error: ")
    assert "30" in result["warnings"][0]


async def test_auto_sync_reports_what_a_partial_run_left(
    stale: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def partial(*args: object) -> SyncReport:
        return SyncReport(
            new=3, chats_done=[GEO], chats_remaining=[ARG], unavailable=[7], warnings=["slow"]
        )

    monkeypatch.setattr(syncing, "sync_all", partial)
    result = await tools.search("DNI", mode="lexical")
    assert result["synced"] is True and result["hits"]
    assert result["warnings"] == [
        "auto-sync: slow",
        f"auto-sync stopped after {CFG.search.auto_sync_budget_s}s with 1 chats still behind; "
        "call sync to finish",
        "auto-sync: 1 chats are unavailable on Telegram (7); their stored messages are still "
        "searched",
    ]


async def test_auto_sync_without_a_session_is_a_warning(
    stale: tools.AppState, fake: FakeClient, paths: Paths, bind: Callable[..., tools.AppState]
) -> None:
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
    built: list[Config] = []
    tools.bind(
        tools.AppState(paths, CFG, stale.conn, client_factory=lambda cfg, p: built.append(cfg))
    )
    paths.session_file.unlink()
    result = await tools.search("DNI", mode="lexical")
    assert result["hits"] and result["synced"] is False
    assert result["warnings"] == [f"auto-sync skipped: no Telegram session at {paths.session_file}"]
    assert built == []  # the session file is checked before any client exists


async def test_auto_sync_is_not_repeated_while_no_chat_can_complete(
    stale: tools.AppState, fake: FakeClient, conn: sqlite3.Connection
) -> None:
    fake.failures[ARG] = errors.ChannelPrivateError(request=None)
    fake.failures[GEO] = errors.ChannelPrivateError(request=None)
    first = await tools.search("DNI", mode="lexical")
    assert first["synced"] is True and first["hits"]
    assert first["index_age_min"] > CFG.search.auto_sync_after_min
    assert len(first["warnings"]) == 1 and "2 chats are unavailable" in first["warnings"][0]
    assert str(ARG) in first["warnings"][0] and str(GEO) in first["warnings"][0]
    again = await tools.search("DNI", mode="lexical")
    assert again["synced"] is False and again["warnings"] == [] and again["hits"]
    assert _connects(fake) == 1


async def test_index_exactly_at_the_threshold_is_not_stale(
    bind: Callable[..., tools.AppState], conn: sqlite3.Connection, fake: FakeClient
) -> None:
    chat_ru.load(conn, synced_at=int(time.time()) - CFG.search.auto_sync_after_min * 60)
    bind()
    result = await tools.search("DNI", mode="lexical")
    assert result["synced"] is False and result["hits"]
    assert result["index_age_min"] == CFG.search.auto_sync_after_min
    assert _connects(fake) == 0


async def test_each_telegram_call_builds_a_fresh_client(
    paths: Paths, conn: sqlite3.Connection
) -> None:
    chat_ru.load(conn)
    clients = [_client(authorized=False), _client()]
    built: list[FakeClient] = []

    def factory(cfg: Config, p: Paths) -> FakeClient:
        built.append(clients[len(built)])
        return built[-1]

    tools.bind(tools.AppState(paths, CFG, conn, client_factory=factory))
    denied = await tools.dialogs("arg")
    assert denied["error"] == "Telegram session is not authorized"
    assert denied["hint"] == tools.AUTH_HINT
    recovered = await tools.dialogs("arg")
    assert "error" not in recovered and recovered["matches"]
    assert built == clients
    assert all(not client.is_connected() for client in clients)
    assert [name for name, _ in clients[0].calls] == ["connect", "is_user_authorized", "disconnect"]


async def test_concurrent_stale_searches_run_one_sync(
    stale: tools.AppState, fake: FakeClient, conn: sqlite3.Connection
) -> None:
    """The second search waits for the first one's refresh and finds the index fresh, rather
    than opening a second client and tripping over the sync lock."""
    fake.messages[ARG] = [tl.message(ARG, 43, NEW_TEXT, sender=1)]
    results = await asyncio.gather(
        tools.search("Brubank", mode="lexical"),
        tools.search("TBC", mode="lexical"),
        tools.search("DNI", mode="lexical"),
    )
    assert all("error" not in result and result["hits"] for result in results)
    assert sorted(result["synced"] for result in results) == [False, False, True]
    assert all(result["warnings"] == [] for result in results)
    assert _connects(fake) == 1
    assert db.get_message(conn, ARG, 43) is not None


async def test_sync_and_a_stale_search_queue_instead_of_colliding(
    stale: tools.AppState, fake: FakeClient, conn: sqlite3.Connection
) -> None:
    fake.messages[ARG] = [tl.message(ARG, 43, NEW_TEXT, sender=1)]
    report, result = await asyncio.gather(
        tools.sync(budget_s=10), tools.search("Brubank", mode="lexical")
    )
    assert "error" not in report and "error" not in result
    assert not any(
        "another sync" in warning for warning in [*report["warnings"], *result["warnings"]]
    )
    assert db.get_message(conn, ARG, 43) is not None and _has(result, ARG, 43)


async def test_an_unreadable_session_file_is_an_error_with_a_hint(
    stale: tools.AppState, paths: Paths, conn: sqlite3.Connection
) -> None:
    def locked(cfg: Config, p: Paths) -> FakeClient:
        raise SessionError(p.session_file, sqlite3.OperationalError("database is locked"))

    tools.bind(tools.AppState(paths, CFG, conn, client_factory=locked))
    denied = await tools.dialogs("arg")
    assert denied["error"] == (
        f"cannot read the Telegram session at {paths.session_file}: database is locked"
    )
    assert denied["hint"] == tools.SESSION_HINT
    searched = await tools.search("DNI", mode="lexical")
    assert searched["hits"] and searched["synced"] is False
    assert searched["warnings"] == [
        f"auto-sync skipped: cannot read the Telegram session at {paths.session_file}: "
        "database is locked"
    ]


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


async def test_sync_rejects_a_non_positive_budget(state: tools.AppState) -> None:
    assert (await tools.sync(budget_s=0))["error"].startswith("budget_s must be")


async def test_sync_under_a_held_lock_carries_the_lock_hint(
    state: tools.AppState, paths: Paths
) -> None:
    with SyncLock(paths):
        busy = await tools.sync()
    assert busy["error"].startswith("another sync is running")
    assert busy["hint"] == tools.LOCK_HINT


async def test_sources_remove_under_a_held_lock_carries_the_lock_hint(
    state: tools.AppState, paths: Paths, conn: sqlite3.Connection
) -> None:
    with SyncLock(paths):
        busy = tools.sources_remove("folder:Argentina")
    assert busy["error"].startswith("another sync is running")
    assert busy["hint"] == tools.LOCK_HINT
    assert db.get_chat(conn, ARG) is not None
    assert state.config().sources == CFG.sources
    freed = tools.sources_remove("folder:Argentina")
    assert freed["removed_chat_ids"] == [ARG]


async def test_concurrent_source_changes_all_reach_the_config(
    bind: Callable[..., tools.AppState], paths: Paths, fake: FakeClient
) -> None:
    """Two ``sources_add`` calls resolve their targets at the same time; each saves onto the
    config as it is by then, not onto the snapshot it started from."""
    bind(Config(telegram=KEYS))
    added = await asyncio.gather(tools.sources_add("georgia"), tools.sources_add("@news"))
    assert all("error" not in result for result in added)
    assert {s.id for s in config.load(paths).sources} == {f"chat:{GEO}", "chat:@news"}
    again = await asyncio.gather(
        tools.sources_add("folder:Argentina"), asyncio.to_thread(tools.sources_remove, "@news")
    )
    assert all("error" not in result for result in again)
    assert {s.id for s in config.load(paths).sources} == {f"chat:{GEO}", "folder:Argentina"}


def _offline() -> FakeClient:
    client = _client()

    async def failing_connect() -> None:
        raise ConnectionError("offline")

    client.connect = failing_connect  # type: ignore[method-assign]
    return client


def _flooded() -> FakeClient:
    return _client(
        responses={
            functions.messages.GetDialogFiltersRequest: errors.FloodWaitError(
                request=None, capture=30
            )
        }
    )


UNSET = Config(search=CFG.search, units=CFG.units, sources=CFG.sources)


@pytest.mark.parametrize(
    ("cfg", "client", "error", "hint"),
    [
        pytest.param(
            CFG,
            lambda: _client(authorized=False),
            "Telegram session is not authorized",
            tools.AUTH_HINT,
            id="unauthorized",
        ),
        pytest.param(CFG, _flooded, "telegram error: ", None, id="flood-wait"),
        pytest.param(CFG, _offline, "connection error: offline", None, id="offline"),
        pytest.param(UNSET, _client, "[telegram] api_id and api_hash", tools.SETUP_HINT, id="keys"),
        pytest.param(
            Config(telegram=KEYS),
            _client,
            "no sources are configured",
            tools.NO_SOURCES_HINT,
            id="no-sources",
        ),
    ],
)
async def test_sync_errors_carry_hints(
    state: tools.AppState,
    bind: Callable[..., tools.AppState],
    cfg: Config,
    client: Callable[[], FakeClient],
    error: str,
    hint: str | None,
) -> None:
    bind(cfg, client())
    result = await tools.sync()
    assert result["error"].startswith(error)
    assert result["hint"] == hint
    assert "new" not in result


async def test_sync_without_a_session_file_carries_the_auth_hint(
    state: tools.AppState, paths: Paths
) -> None:
    paths.session_file.unlink()
    missing = await tools.sync()
    assert missing["error"] == f"no Telegram session at {paths.session_file}"
    assert missing["hint"] == tools.AUTH_HINT


async def test_sync_warnings_include_refused_comment_threads(
    state: tools.AppState, fake: FakeClient, conn: sqlite3.Connection
) -> None:
    db.upsert_chat(conn, ChatRow(id=NEWS_ID, type="channel", title="News", username="news"))
    fake.messages[NEWS_ID] = [tl.channel_post(NEWS_ID, 1, "post 1")]
    fake.failures[(NEWS_ID, 1)] = errors.ChannelPrivateError(request=None)
    fake.responses[functions.channels.GetFullChannelRequest] = tl_messages.ChatFull(
        full_chat=types.ChannelFull(
            id=1000000300,
            about="",
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=0,
            chat_photo=types.PhotoEmpty(0),
            notify_settings=types.PeerNotifySettings(),
            bot_info=[],
            pts=0,
            linked_chat_id=1000000400,
        ),
        chats=[NEWS, make_channel(1000000400, "News chat", megagroup=True)],
        users=[],
    )
    cfg = dataclasses.replace(CFG, sources=[*CFG.sources, Source(chat="@news", comments=True)])
    state.cfg = cfg
    report = await tools.sync(budget_s=10)
    assert "error" not in report and NEWS_ID in report["chats_done"]
    assert len(report["warnings"]) == 1 and "comments of channel" in report["warnings"][0]
    assert db.get_message(conn, NEWS_ID, 1) is not None


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
    assert _dialog_reads(fake) == 3
    assert _connects(fake) == 3
    fake.authorized = False
    denied = await tools.dialogs("arg")
    assert denied["hint"] == tools.AUTH_HINT and "matches" not in denied


async def test_sources_add_fuzzy_writes_config_and_reads_dialogs_afresh(
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
    assert _dialog_reads(fake) == 2
    await tools.dialogs("arg")
    assert _dialog_reads(fake) == 3
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


async def test_open_message_returns_the_url_used(
    state: tools.AppState, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Link] = []

    def open_link(link: Link) -> str:
        opened.append(link)
        return link.url

    monkeypatch.setattr(links, "open_link", open_link)
    assert await tools.open_message(ARG, 5) == {
        "chat_id": ARG,
        "msg_id": 5,
        "url": "https://t.me/arg_chat/5",
        "fallback_url": None,
        "opened": True,
    }
    assert opened == [Link("https://t.me/arg_chat/5")]
    assert (await tools.open_message(GEO, 3))["url"] == "https://t.me/c/1000000200/3"
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
    assert (await tools.open_message(FORUM, 105))["url"] == "https://t.me/forum_chat/100/105"
    private = await tools.open_message(7, 9)
    assert private["url"] == "tg://openmessage?user_id=7&message_id=9"
    assert private["fallback_url"] == "tg://user?id=7"
    assert (await tools.open_message(ARG, 999))["hint"] == tools.MESSAGE_HINT
    assert len(opened) == 4


async def test_open_message_keeps_the_url_when_open_fails(
    state: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(link: Link) -> str:
        raise OpenFailed(f"open failed for {link.url}: no application")

    monkeypatch.setattr(links, "open_link", broken)
    result = await tools.open_message(ARG, 5)
    assert result["url"] == "https://t.me/arg_chat/5" and result["opened"] is False
    assert result["error"].startswith("open failed") and result["hint"] == tools.OPEN_HINT

    def elsewhere(link: Link) -> str:
        raise NotImplementedError("opening links needs macOS 'open' (linux)")

    monkeypatch.setattr(links, "open_link", elsewhere)
    result = await tools.open_message(ARG, 5)
    assert result["opened"] is False and "macOS" in result["error"]
    assert result["url"] == "https://t.me/arg_chat/5"


async def test_open_message_reports_a_hung_open_with_the_url(
    state: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hung(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(list(args), links.OPEN_TIMEOUT_S)

    real_open = links.open_link
    monkeypatch.setattr(
        links, "open_link", lambda link: real_open(link, runner=hung, platform="darwin")
    )
    result = await tools.open_message(ARG, 5)
    assert result["url"] == "https://t.me/arg_chat/5" and result["opened"] is False
    assert "did not finish within" in result["error"] and result["hint"] == tools.OPEN_HINT


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
        (
            SessionError(
                Path("/x/session.session"), sqlite3.OperationalError("database is locked")
            ),
            "cannot read the Telegram session at /x/session.session: database is locked",
            tools.SESSION_HINT,
        ),
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


async def test_concurrent_tool_calls_share_the_connection_safely(
    state: tools.AppState, conn: sqlite3.Connection, fake: FakeClient
) -> None:
    index.embed_dirty_units(conn, FakeEmbedder())
    queries = ["DNI", "Brubank", PAIR.query_en, "TBC", "SIM", "Galicia", "Santander", "visa"]
    for round_ in range(3):
        fake.messages[ARG] = [tl.message(ARG, 43 + round_, f"{NEW_TEXT} {round_}", sender=1)]
        results = await asyncio.gather(
            *(tools.search(query) for query in queries),
            tools.sync(budget_s=10),
            asyncio.to_thread(tools.thread, ARG, 5),
            asyncio.to_thread(tools.sources),
            asyncio.to_thread(tools.context, GEO, 3),
        )
        for result in results:
            assert "error" not in result, result
        searches = results[: len(queries)]
        assert all(result["hits"] for result in searches)
        assert results[len(queries)]["new"] == 1
    assert db.get_message(conn, ARG, 45) is not None


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
    assert sys.stdout is not sys.stderr
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
        assert sys.stdout is sys.stderr
        print("inside")
    assert sys.stdout is before
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
        assert len(by_name) == 9
        assert sorted(by_name) == sorted(tool.__name__ for tool in tools.TOOLS)
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


def _rpc(method: str, request_id: int | None = None, **params: object) -> str:
    frame: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        frame["id"] = request_id
    return json.dumps(frame)


def test_server_speaks_json_rpc_over_real_stdio(tmp_home: Path) -> None:
    config.save(Config(telegram=KEYS), Paths.from_env())
    frames = [
        _rpc(
            "initialize",
            1,
            protocolVersion="2025-06-18",
            capabilities={},
            clientInfo={"name": "test", "version": "0"},
        ),
        _rpc("notifications/initialized"),
        _rpc("tools/list", 2),
    ]
    completed = subprocess.run(
        [sys.executable, "-c", "from grepogram.mcp import main; main()"],
        input="\n".join(frames) + "\n",
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GREPOGRAM_HOME": str(tmp_home), "GREPOGRAM_FAKE_MODELS": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    replies = [json.loads(line) for line in lines]
    by_id = {reply["id"]: reply for reply in replies if "id" in reply}
    assert by_id[1]["result"]["serverInfo"]["name"] == "grepogram"
    assert by_id[1]["result"]["instructions"] == tools.INSTRUCTIONS
    listed = by_id[2]["result"]["tools"]
    assert sorted(tool["name"] for tool in listed) == sorted(tool.__name__ for tool in tools.TOOLS)
    assert "grepogram-mcp serving" in completed.stderr
