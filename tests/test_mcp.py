import asyncio
import dataclasses
import fcntl
import json
import logging
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import types as mcp_types
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from telethon import errors
from telethon.tl import functions, types
from telethon.tl.types import messages as tl_messages

from grepogram import config, db, embed, filters, index, research, research_db, units
from grepogram import mcp as tools
from grepogram import rerank as reranking
from grepogram import search as retrieval
from grepogram import sources as sourcing
from grepogram import sync as syncing
from grepogram.embed import FakeEmbedder, ModelUnavailable
from grepogram.filters import InvalidDate, UnknownChat
from grepogram.models import (
    DEFAULT_ACCOUNT,
    AccountCfg,
    ApprovalItem,
    ChatRow,
    Config,
    MessageRow,
    ResearchCfg,
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
from tests.fakes import (
    FakeClient,
    FakeWorld,
    make_channel,
    make_dialog,
    make_folder,
    make_user,
    no_discussion,
)
from tests.fixtures import chat_ru, tl, two_accounts

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


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    home = Paths.under(tmp_path / "home")
    home.ensure_dirs()
    home.session_file.touch()
    return home


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
        state = tools.AppState(paths, cfg, conn, client_factory=lambda cfg, paths, account: chosen)
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

    async def busy(*args: object, **kwargs: object) -> SyncReport:
        calls.append(args)
        raise SyncInProgress("another sync is running (lock held)")

    monkeypatch.setattr(syncing, "sync_all", busy)
    result = await tools.search("DNI", mode="lexical")
    assert "error" not in result and result["hits"]
    assert result["synced"] is False
    assert result["warnings"] == ["auto-sync skipped: another sync is running (lock held)"]
    assert len(calls) == 1
    assert calls[0][0] == {DEFAULT_ACCOUNT: fake}
    assert calls[0][1] is stale.conn and calls[0][3] is paths
    budget = calls[0][4]
    assert isinstance(budget, syncing.SyncBudget)
    assert budget.seconds == CFG.search.auto_sync_budget_s
    assert isinstance(calls[0][5], FakeEmbedder)


async def test_auto_sync_flood_wait_is_a_warning(
    stale: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def flooded(*args: object, **kwargs: object) -> SyncReport:
        raise errors.FloodWaitError(request=None, capture=30)

    monkeypatch.setattr(syncing, "sync_all", flooded)
    result = await tools.search("DNI", mode="lexical")
    assert result["hits"] and len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("auto-sync skipped: telegram error: ")
    assert "30" in result["warnings"][0]


async def test_auto_sync_reports_what_a_partial_run_left(
    stale: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def partial(*args: object, **kwargs: object) -> SyncReport:
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
        tools.AppState(
            paths, CFG, stale.conn, client_factory=lambda cfg, p, account: built.append(cfg)
        )
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

    def factory(cfg: Config, p: Paths, account: str) -> FakeClient:
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


async def test_auto_sync_waits_no_longer_than_its_budget_for_a_running_sync(
    bind: Callable[..., tools.AppState], conn: sqlite3.Connection, fake: FakeClient
) -> None:
    """A stale search spends at most ``auto_sync_budget_s`` on its refresh, waiting for a sync
    another tool call started included; past that it searches the index as it is and says so."""
    chat_ru.load(conn)
    cfg = dataclasses.replace(CFG, search=dataclasses.replace(CFG.search, auto_sync_budget_s=1))
    state = bind(cfg)
    await state.sync_lock.acquire()
    try:
        started = time.monotonic()
        result = await tools.search("DNI", mode="lexical")
        waited = time.monotonic() - started
    finally:
        state.sync_lock.release()
    assert 1 <= waited < 5
    assert result["hits"] and result["synced"] is False
    assert result["warnings"] == [f"auto-sync skipped: {tools.SYNC_RUNNING}"]
    assert _connects(fake) == 0
    again = await tools.search("DNI", mode="lexical")
    assert again["synced"] is True and again["warnings"] == [] and _connects(fake) == 1


async def test_an_unreadable_session_file_is_an_error_with_a_hint(
    stale: tools.AppState, paths: Paths, conn: sqlite3.Connection
) -> None:
    def locked(cfg: Config, p: Paths, account: str) -> FakeClient:
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


async def test_the_auto_sync_inside_a_search_never_starts_a_recut(
    stale: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search refreshes messages; it never rebuilds units.

    Who may start the one-time re-cut is the caller's ``recut`` flag, not the budget: this is the
    one caller that opts out, so raising ``search.auto_sync_budget_s`` — a user-editable number —
    still cannot turn a search into a whole-index rebuild.
    """
    conn = stale.conn
    before = {unit.id for unit in db.get_units(conn, ARG)}
    monkeypatch.setattr(units, "RECIPE_VERSION", units.RECIPE_VERSION + 1)
    result = await tools.search("DNI", mode="lexical")
    assert result["synced"] is True
    assert {unit.id for unit in db.get_units(conn, ARG)} == before
    assert db.unit_recipe(conn) != units.RECIPE_VERSION
    assert db.recut_markers(conn) == {}


async def test_an_explicit_sync_makes_recut_progress_on_its_own_budget(
    state: tools.AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: an explicit ``sync()`` is deliberate, so it re-cuts what its budget
    allows — bounded per run and resumable — however short the window it was given."""
    conn = state.conn
    before = {unit.id for unit in db.get_units(conn, ARG)}
    monkeypatch.setattr(units, "RECIPE_VERSION", units.RECIPE_VERSION + 1)
    report = await tools.sync(budget_s=5)
    assert "error" not in report
    assert {unit.id for unit in db.get_units(conn, ARG)} != before
    assert db.unit_recipe(conn) == units.RECIPE_VERSION


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


async def test_source_removed_while_a_sync_starts_stays_removed(
    state: tools.AppState, fake: FakeClient, conn: sqlite3.Connection, paths: Paths
) -> None:
    """``sync`` reads its config, then loads the model and connects before it takes the sync
    lock; a ``sources_remove`` that runs in that window must win for good — the sync resolves
    its sources from the config as it is once the lock is held, not from its snapshot."""
    fake.messages[ARG] = [tl.message(ARG, 43, NEW_TEXT, sender=1)]
    task = asyncio.create_task(tools.sync(budget_s=10))
    await asyncio.sleep(0)  # the sync has read its config and is loading the embedder
    removed = tools.sources_remove("folder:Argentina")
    assert removed["removed_chat_ids"] == [ARG] and removed["config_updated"] is True
    report = await task
    assert "error" not in report and report["chats_done"] == [GEO]
    assert db.get_chat(conn, ARG) is None and db.get_message(conn, ARG, 43) is None
    assert [s.id for s in config.load(paths).sources] == [f"chat:{GEO}"]
    assert [s["source_id"] for s in tools.sources()["sources"]] == [f"chat:{GEO}"]


async def test_source_removed_while_a_stale_search_starts_its_refresh_stays_removed(
    stale: tools.AppState, fake: FakeClient, conn: sqlite3.Connection, paths: Paths
) -> None:
    fake.messages[ARG] = [tl.message(ARG, 43, NEW_TEXT, sender=1)]
    task = asyncio.create_task(tools.search("TBC", mode="lexical"))
    await asyncio.sleep(0)  # the search holds sync_lock and is loading the embedder
    removed = tools.sources_remove("folder:Argentina")
    assert removed["removed_chat_ids"] == [ARG]
    result = await task
    assert "error" not in result and result["synced"] is True and result["warnings"] == []
    assert db.get_chat(conn, ARG) is None and db.get_message(conn, ARG, 43) is None
    assert [s.id for s in config.load(paths).sources] == [f"chat:{GEO}"]


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


def test_editing_config_holds_the_config_lock_across_processes(
    bind: Callable[..., tools.AppState], paths: Paths
) -> None:
    """Another process's ``ConfigLock`` — a second descriptor, as far as ``flock`` is
    concerned — must wait while the server edits the config, and gets through as soon as the
    block ends."""
    state = bind(Config(telegram=KEYS))
    with state.editing_config() as current:
        assert current.telegram == KEYS
        fd = os.open(paths.config_lock_file, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state.save_config(dataclasses.replace(current, sources=[Source(chat="@news")]))
        finally:
            os.close(fd)
    with config.ConfigLock(paths):
        pass
    assert config.load(paths).sources == [Source(chat="@news")]
    assert stat.S_IMODE(paths.config_lock_file.stat().st_mode) == 0o600


async def test_sources_add_keeps_a_change_another_process_saved_meanwhile(
    bind: Callable[..., tools.AppState], paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the target resolves over the network, ``grepogram sources rm`` in a terminal saves
    the config without the Argentina folder; the add applies to the file as it is by then, not
    to the snapshot it started from, so the removal survives."""
    state = bind(Config(telegram=KEYS, sources=[Source(folder="Argentina")]))
    state.save_config(state.cfg)
    real_add = sourcing.add_source

    async def add_then_lose_the_folder(*args: Any, **kwargs: Any) -> sourcing.Added:
        added = await real_add(*args, **kwargs)
        config.update(paths, lambda current: dataclasses.replace(current, sources=[]))
        return added

    monkeypatch.setattr(sourcing, "add_source", add_then_lose_the_folder)
    result = await tools.sources_add("@news")
    assert "error" not in result
    assert config.load(paths).sources == [Source(chat="@news")]
    assert state.config().sources == [Source(chat="@news")]


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


async def test_a_flood_wait_while_resolving_is_a_warning_not_an_error(
    state: tools.AppState, bind: Callable[..., tools.AppState]
) -> None:
    """A flood wait on one account's resolve stops that account for the run; the sync itself
    answers, and says so, instead of failing every account's run with a Telegram error."""
    bind(CFG, _flooded())
    result = await tools.sync()
    assert "error" not in result
    assert result["warnings"] == [
        "flood wait: Telegram asks to wait 30s before more history requests; run sync again later"
    ]


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
    fake.messages[NEWS_ID] = [tl.channel_post(NEWS_ID, 1, "post 1", replies=1)]
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
        "account": "default",
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


async def test_a_refused_join_link_reaches_the_caller_and_not_the_log(
    bind: Callable[..., tools.AppState], caplog: pytest.LogCaptureFixture
) -> None:
    """An invite or folder link a tool refuses can be a private way in someone wrote in a
    message: the result quotes it for the caller, the log says only what kind of refusal it was
    unless it runs at DEBUG."""
    bind(Config(telegram=KEYS))
    with caplog.at_level(logging.INFO, logger="grepogram"):
        refused = await tools.sources_add("https://t.me/+SecretDoor")
    assert "SecretDoor" in refused["error"]
    assert "sources_add failed: InvalidTarget" in caplog.text
    assert "SecretDoor" not in caplog.text


async def test_sources_add_refuses_a_chat_already_held_as_an_import(
    bind: Callable[..., tools.AppState], paths: Paths, conn: sqlite3.Connection
) -> None:
    """The tool is the second door to the same operation, so it carries the same guard: without
    it ``db.upsert_chat`` would replace ``import:`` with ``chat:@arg_chat`` on the next sync and
    the imported history would become prunable."""
    bind(Config(telegram=KEYS))
    db.upsert_chat(
        conn, ChatRow(id=ARG, type="supergroup", title="Argentina chat", source_id="import:arg")
    )
    refused = await tools.sources_add("@arg_chat")
    assert "already in the index as import:arg" in refused["error"]
    assert config.load(paths).sources == []
    stored = db.get_chat(conn, ARG)
    assert stored is not None and stored.source_id == "import:arg"


async def test_sources_remove_deletes_data_and_saves_the_config(
    state: tools.AppState, paths: Paths, conn: sqlite3.Connection
) -> None:
    through_folder = tools.sources_remove("@arg_chat")
    assert "indexed through folder:Argentina" in through_folder["error"]
    removed = tools.sources_remove("folder:Argentina")
    assert removed == {
        "source_id": "folder:Argentina",
        "removed_chat_ids": [ARG],
        "kept_chat_ids": [],
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
        "chat_id",
        "peer_id",
        "msg_id",
        "date",
        "from_name",
        "text",
        "url",
        "fallback_url",
        "reply_to_msg_id",
        "accounts",
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


def test_thread_of_a_channel_post_names_each_message_chat(
    state: tools.AppState, conn: sqlite3.Connection
) -> None:
    """The top-level ``chat_id`` is the argument; a comment's own is the discussion group, whose
    ids collide with the channel's posts. Only the per-message ``chat_id`` leads back to it."""
    disc_id = -1000000000201
    db.upsert_chat(conn, ChatRow(id=NEWS_ID, type="channel", title="News", username="news"))
    db.upsert_chat(
        conn, ChatRow(id=disc_id, type="supergroup", title="News chat", discussion_of=NEWS_ID)
    )
    db.upsert_messages(
        conn,
        [
            MessageRow(chat_id=NEWS_ID, msg_id=1, date=100, text="post 1"),
            MessageRow(chat_id=NEWS_ID, msg_id=2, date=200, text="post 2"),
            MessageRow(
                chat_id=disc_id,
                msg_id=2,
                date=150,
                text="comment on post 1",
                comment_of_chat_id=NEWS_ID,
                comment_of_msg_id=1,
            ),
        ],
    )
    result = tools.thread(NEWS_ID, 1)
    assert result["chat_id"] == NEWS_ID
    messages = result["messages"]
    assert [(m["chat_id"], m["msg_id"]) for m in messages] == [(NEWS_ID, 1), (disc_id, 2)]
    comment = messages[1]
    assert comment["url"] == "https://t.me/c/201/2"
    back = tools.context(comment["chat_id"], comment["msg_id"])
    assert [m["text"] for m in back["messages"]] == ["comment on post 1"]


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


def test_config_failures_carry_a_hint() -> None:
    plain = config.ConfigError("/x/config.toml: invalid TOML: line 3")
    assert tools.failure(plain) == {"error": str(plain), "hint": tools.CONFIG_HINT}
    keyed = config.ConfigError("/x/config.toml: unknown key: models.k", "upgrade and restart")
    assert tools.failure(keyed) == {"error": str(keyed), "hint": "upgrade and restart"}


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


# --- accounts --------------------------------------------------------------------------------

WORK = "work"
BOB = make_user(2, "Bob")
WORK_ME = make_user(43, "Worker")
TWO_ACCOUNTS = Config(
    telegram=KEYS,
    search=CFG.search,
    units=CFG.units,
    accounts=[AccountCfg(name=WORK, label="work phone")],
    sources=[
        Source(chat="@news"),
        Source(chat="@news", account=WORK),
        Source(chat=2, account=WORK),
    ],
)


class Accounts:
    """The default account and ``work`` over one world: both see the public channel ``@news``,
    ``work`` alone has a private chat with Bob. Every block gets fresh clients, as the server's
    factory builds them; ``built`` records each one per account."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.world = FakeWorld(
            entities=[NEWS, BOB],
            messages={NEWS_ID: [tl.channel_post(NEWS_ID, i, f"news {i}") for i in (1, 2)]},
        )
        self.options: dict[str, dict[str, Any]] = {DEFAULT_ACCOUNT: {}, WORK: {}}
        self.built: dict[str, list[FakeClient]] = {DEFAULT_ACCOUNT: [], WORK: []}
        paths.session_file_for(WORK).parent.mkdir(parents=True, exist_ok=True)
        paths.session_file_for(WORK).touch()

    def factory(self, cfg: Config, paths: Paths, account: str) -> FakeClient:
        if account == WORK:
            client = self.world.client(
                WORK,
                members=[NEWS, BOB],
                me=WORK_ME,
                messages={2: [tl.message(2, 5, "bob at work", sender=2)]},
                **self.options[WORK],
            )
        else:
            client = self.world.client(members=[NEWS], me=ME, **self.options[DEFAULT_ACCOUNT])
        self.built[account].append(client)
        return client


@pytest.fixture
def accounts(paths: Paths, conn: sqlite3.Connection) -> Iterator[Accounts]:
    two = Accounts(paths)
    tools.bind(tools.AppState(paths, TWO_ACCOUNTS, conn, client_factory=two.factory))
    yield two
    tools.unbind()


def _skipped(account: str, error: str) -> dict[str, object]:
    return {"account": account, "error": error, "hint": tools.auth_hint(account)}


async def test_sync_fetches_through_every_signed_in_account(
    accounts: Accounts, conn: sqlite3.Connection
) -> None:
    report = await tools.sync(budget_s=10)
    assert "error" not in report, report
    assert report["new"] == 3 and report["accounts_skipped"] == [] and report["warnings"] == []
    bob = db.get_chat_by_peer(conn, 2, WORK)
    assert bob is not None
    assert db.message_counts(conn) == {NEWS_ID: 2, bob.id: 1}
    assert db.chat_accounts(conn, NEWS_ID) == [DEFAULT_ACCOUNT, WORK]
    assert {name: len(built) for name, built in accounts.built.items()} == {
        DEFAULT_ACCOUNT: 1,
        WORK: 1,
    }
    assert all(not c.is_connected() for built in accounts.built.values() for c in built)
    again = await tools.sync(budget_s=10)
    assert again["new"] == 0
    assert {name: len(built) for name, built in accounts.built.items()} == {
        DEFAULT_ACCOUNT: 2,
        WORK: 2,
    }


async def test_sync_goes_on_without_a_signed_out_account_and_names_its_sign_in(
    accounts: Accounts, conn: sqlite3.Connection, capfd: pytest.CaptureFixture[str]
) -> None:
    accounts.options[WORK] = {"authorized": False}
    report = await tools.sync(budget_s=10)
    assert "error" not in report, report
    assert report["new"] == 2 and db.message_counts(conn) == {NEWS_ID: 2}
    assert report["accounts_skipped"] == [_skipped(WORK, "Telegram session is not authorized")]
    assert tools.auth_hint(WORK) == (
        "sign in from a terminal with `grepogram auth --account work`, then retry"
    )
    assert report["warnings"] == [
        f"account work skipped: Telegram session is not authorized; {tools.auth_hint(WORK)}"
    ]
    assert db.chat_accounts(conn, NEWS_ID) == [DEFAULT_ACCOUNT]

    accounts.options[WORK] = {}
    accounts.paths.session_file_for(WORK).unlink()
    missing = await tools.sync(budget_s=10)
    work_session = accounts.paths.session_file_for(WORK)
    assert missing["accounts_skipped"] == [_skipped(WORK, f"no Telegram session at {work_session}")]
    assert len(accounts.built[WORK]) == 1  # no client is built for a missing session
    assert capfd.readouterr().out == ""


async def test_sync_with_no_account_signed_in_is_an_error_naming_the_source_owner(
    accounts: Accounts, paths: Paths
) -> None:
    paths.session_file.unlink()
    accounts.options[WORK] = {"authorized": False}
    refused = await tools.sync(budget_s=10)
    assert refused["error"] == "account work: Telegram session is not authorized"
    assert refused["hint"] == tools.auth_hint(WORK)
    paths.session_file_for(WORK).unlink()
    missing = await tools.sync(budget_s=10)
    assert missing["error"] == f"no Telegram session at {paths.session_file}"
    assert missing["hint"] == tools.AUTH_HINT  # the default account owns a source too


async def test_a_session_file_only_named_accounts_need_is_not_missed(
    paths: Paths, conn: sqlite3.Connection
) -> None:
    """An install whose sources all belong to ``work`` never had a default session to miss."""
    two = Accounts(paths)
    paths.session_file.unlink()
    cfg = dataclasses.replace(TWO_ACCOUNTS, sources=[Source(chat=2, account=WORK)])
    tools.bind(tools.AppState(paths, cfg, conn, client_factory=two.factory))
    try:
        report = await tools.sync(budget_s=10)
    finally:
        tools.unbind()
    assert "error" not in report, report
    assert report["new"] == 1 and report["accounts_skipped"] == [] and report["warnings"] == []


async def test_auto_sync_refreshes_every_account_and_warns_about_a_refused_one(
    accounts: Accounts, conn: sqlite3.Connection
) -> None:
    await tools.sync(budget_s=10)
    conn.execute("UPDATE chats SET last_sync_at = 1")
    conn.execute("DELETE FROM meta WHERE key LIKE 'last_sync%'")
    assert tools._stale(tools._app(), TWO_ACCOUNTS)
    accounts.world.messages[NEWS_ID].append(tl.channel_post(NEWS_ID, 3, "news 3 Brubank"))
    accounts.options[WORK] = {"authorized": False}
    result = await tools.search("Brubank", mode="lexical")
    assert result["synced"] is True and _has(result, NEWS_ID, 3)
    assert result["warnings"] == [
        "auto-sync: account work skipped: Telegram session is not authorized; "
        f"{tools.auth_hint(WORK)}",
        "auto-sync: 1 chats were not fetched: no account in this run reaches them, and account "
        "work itself is not signed in or not part of it",
    ], "its chat with Bob is not reported as behind: syncing again would not fetch it"


async def test_dialogs_reads_the_account_it_is_given(accounts: Accounts) -> None:
    home = await tools.dialogs("news")
    assert home["account"] == DEFAULT_ACCOUNT
    assert [m["target"] for m in home["matches"]] == ["@news"]
    work = await tools.dialogs("bob", account=WORK)
    assert "error" not in work, work
    assert work["account"] == WORK
    assert [(m["id"], m["target"]) for m in work["matches"]] == [(2, "work/chat:2")]
    assert [m["target"] for m in (await tools.dialogs("news", WORK))["matches"]] == [
        "work/chat:@news"
    ]
    assert len(accounts.built[DEFAULT_ACCOUNT]) == 1 and len(accounts.built[WORK]) == 2
    assert not any(c.is_connected() for built in accounts.built.values() for c in built)
    unknown = await tools.dialogs("bob", account="nobody")
    assert unknown["error"] == f"unknown account 'nobody'; known: {DEFAULT_ACCOUNT}, {WORK}"
    assert unknown["hint"] == tools.auth_hint("nobody")
    accounts.options[WORK] = {"authorized": False}
    refused = await tools.dialogs("bob", account=WORK)
    assert refused == {
        "error": "account work: Telegram session is not authorized",
        "hint": tools.auth_hint(WORK),
    }


async def test_sources_add_for_another_account(paths: Paths, conn: sqlite3.Connection) -> None:
    two = Accounts(paths)
    cfg = dataclasses.replace(TWO_ACCOUNTS, sources=[])
    state = tools.AppState(paths, cfg, conn, client_factory=two.factory)
    tools.bind(state)
    try:
        prefixed = await tools.sources_add("work/chat:2")
        named = await tools.sources_add("@news", account=WORK)
        home = await tools.sources_add("@news")
        clash = await tools.sources_add("work/chat:@news", account=DEFAULT_ACCOUNT)
        unknown = await tools.sources_add("@news", account="nobody")
    finally:
        tools.unbind()
    assert prefixed["source"]["id"] == "work/chat:2" and prefixed["source"]["account"] == WORK
    assert named["source"]["id"] == "work/chat:@news"
    assert home["source"]["id"] == "chat:@news" and home["source"]["account"] == DEFAULT_ACCOUNT
    assert "account default" in clash["error"]
    assert unknown["hint"] == tools.auth_hint("nobody")
    assert [s.id for s in state.config().sources] == [
        "work/chat:2",
        "work/chat:@news",
        "chat:@news",
    ]
    assert len(two.built[WORK]) == 2 and len(two.built[DEFAULT_ACCOUNT]) == 2


async def test_sources_add_for_an_account_removed_meanwhile_saves_nothing(
    paths: Paths, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``grepogram accounts rm work`` saves while the target resolves: the tool answers with an
    error, and the server — and every later command — still loads the config."""
    two = Accounts(paths)
    cfg = dataclasses.replace(TWO_ACCOUNTS, sources=[])
    state = tools.AppState(paths, cfg, conn, client_factory=two.factory)
    state.save_config(state.cfg)
    real_add = sourcing.add_source

    async def add_then_lose_the_account(*args: Any, **kwargs: Any) -> sourcing.Added:
        added = await real_add(*args, **kwargs)
        config.update(paths, lambda current: dataclasses.replace(current, accounts=[]))
        return added

    monkeypatch.setattr(sourcing, "add_source", add_then_lose_the_account)
    tools.bind(state)
    try:
        result = await tools.sources_add("@news", account=WORK)
    finally:
        tools.unbind()
    assert "account 'work' was removed while the source was being added" in result["error"]
    loaded = config.load(paths)
    assert loaded.sources == [] and loaded.accounts == []
    assert state.config() == loaded


async def test_sources_carry_accounts_and_remove_keeps_a_shared_chat(
    accounts: Accounts, conn: sqlite3.Connection
) -> None:
    await tools.sync(budget_s=10)
    bob = db.get_chat_by_peer(conn, 2, WORK)
    assert bob is not None
    listed = {s["source_id"]: s for s in tools.sources()["sources"]}
    assert [(s, listed[s]["account"]) for s in listed] == [
        ("chat:@news", DEFAULT_ACCOUNT),
        ("work/chat:@news", WORK),
        ("work/chat:2", WORK),
    ]
    assert listed["chat:@news"]["chats"][0]["accounts"] == [DEFAULT_ACCOUNT, WORK]
    assert listed["work/chat:2"]["chats"][0]["accounts"] == [WORK]
    removed = tools.sources_remove("chat:@news")
    assert removed["source_id"] == "chat:@news"
    assert removed["removed_chat_ids"] == [] and removed["kept_chat_ids"] == [NEWS_ID]
    dm = tools.sources_remove("work/chat:2")
    assert dm["source_id"] == "work/chat:2" and dm["removed_chat_ids"] == [bob.id]
    assert [s.id for s in tools._app().config().sources] == ["work/chat:@news"]
    assert db.message_counts(conn) == {NEWS_ID: 2}
    (left,) = tools.sources()["sources"]
    assert left["source_id"] == "work/chat:@news" and left["chats"][0]["id"] == NEWS_ID


async def test_accounts_lists_every_account_offline(
    accounts: Accounts, conn: sqlite3.Connection, paths: Paths
) -> None:
    before = tools.accounts()
    assert [(a["name"], a["label"], a["session"]) for a in before["accounts"]] == [
        (DEFAULT_ACCOUNT, None, "present"),
        (WORK, "work phone", "present"),
    ]
    assert before["hint"] == tools.ACCOUNTS_HINT
    await tools.sync(budget_s=10)
    paths.session_file.unlink()
    home, work = tools.accounts()["accounts"]
    assert home == {
        "name": DEFAULT_ACCOUNT,
        "label": None,
        "session": "missing",
        "user_id": 42,
        "display_name": "Me Myself",
        "sources": ["chat:@news"],
        "chats": 1,
        "hint": tools.AUTH_HINT,
    }
    assert work == {
        "name": WORK,
        "label": "work phone",
        "session": "authorized",
        "user_id": 43,
        "display_name": "Worker",
        "sources": ["work/chat:@news", "work/chat:2"],
        "chats": 2,
        "hint": None,
    }
    assert all(not c.is_connected() for built in accounts.built.values() for c in built)


def test_session_hints_name_the_account() -> None:
    assert tools.session_hint() == tools.SESSION_HINT
    assert "`grepogram auth --account work`" in tools.session_hint(WORK)
    work = AuthRequired(account=WORK)
    assert tools.failure(work) == {
        "error": "account work: Telegram session is not authorized",
        "hint": tools.auth_hint(WORK),
    }
    unreadable = SessionError(
        Path("/x/sessions/work.session"), sqlite3.OperationalError("database is locked"), WORK
    )
    assert tools.hint_for(unreadable) == tools.session_hint(WORK), "not the default's hint"


# --- server ----------------------------------------------------------------------------------


def test_instructions_carry_the_playbook() -> None:
    text = tools.INSTRUCTIONS
    assert "2-3 query variants" in text and "Russian and English" in text
    assert "recent" in text and "date" in text
    assert "`thread`" in text and "`context`" in text
    assert "`url`" in text
    assert "say so rather than guess" in text
    assert "`sources`" in text and "`dialogs`" in text and "`sources_add`" in text
    assert "`accounts`" in text and "signs it in" in text
    assert "`research_start`" in text and "`research_approve`" in text
    assert "only their own confirmation grants anything" in text
    assert "never run it for them" in text
    assert "forwards and copies of one post are one source" in text
    assert "not independent confirmation" in text
    assert "Name the account a claim came through" in text


async def test_server_lists_the_eighteen_tools_over_a_session(state: tools.AppState) -> None:
    server = tools.build_server()
    assert server.name == "grepogram" and server.instructions == tools.INSTRUCTIONS
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        by_name = {tool.name: tool for tool in listed.tools}
        assert len(by_name) == 18
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
        assert by_name["accounts"].inputSchema["properties"] == {}
        assert by_name["dialogs"].inputSchema["required"] == ["query"]
        assert "account" in by_name["dialogs"].inputSchema["properties"]
        assert "account" in by_name["sources_add"].inputSchema["properties"]
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


# --- account scopes and provenance -----------------------------------------------------------


@pytest.fixture
def two(bind: Callable[..., tools.AppState], conn: sqlite3.Connection) -> two_accounts.TwoAccounts:
    """Two accounts' chats, synced just now (no auto-sync on search)."""
    loaded = two_accounts.load(conn, synced_at=int(time.time()))
    bind(two_accounts.CFG)
    return loaded


async def test_search_scopes_by_account_and_names_the_accounts_of_every_hit(
    two: two_accounts.TwoAccounts, capsys: pytest.CaptureFixture[str]
) -> None:
    everything = await tools.search("Brubank", mode="lexical")
    reach = {h["chat"]["id"]: (h["peer_id"], h["accounts"]) for h in everything["hits"]}
    assert reach == {
        two.hall.id: (two.hall.id, ["default", "work"]),
        two.hall_chat.id: (two.hall_chat.id, ["default", "work"]),
        two.default_bob.id: (two_accounts.BOB, ["default"]),
        two.work_bob.id: (two_accounts.BOB, ["work"]),
    }
    work = await tools.search("Brubank", mode="lexical", accounts=["work"])
    assert {h["chat"]["id"] for h in work["hits"]} == {
        two.hall.id,
        two.hall_chat.id,
        two.work_bob.id,
    }
    by_spec = await tools.search("Brubank", chats=["account:default"], mode="lexical")
    assert {h["chat"]["id"] for h in by_spec["hits"]} == {
        two.hall.id,
        two.hall_chat.id,
        two.default_bob.id,
    }
    by_peer = await tools.search("Brubank", chats=[f"work/{two_accounts.BOB}"], mode="lexical")
    assert {h["chat"]["id"] for h in by_peer["hits"]} == {two.work_bob.id}
    refused = await tools.search("Brubank", accounts=["home"])
    assert refused["error"].startswith("no indexed chat matches 'account:home'")
    assert refused["hint"] == "no account is named 'home'; known accounts: default, work"
    assert refused["candidates"] == ["account:default", "account:work"]
    assert capsys.readouterr().out == ""


def test_readers_follow_a_synthetic_row_and_report_its_peer(
    two: two_accounts.TwoAccounts, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tools.thread(two.work_bob.id, 7)
    assert result["chat_id"] == two.work_bob.id
    (message,) = result["messages"]
    assert message["chat_id"] == two.work_bob.id and message["peer_id"] == two_accounts.BOB
    assert message["accounts"] == ["work"] and message["text"] == "Brubank payroll moves to Friday"
    around = tools.context(two_accounts.BOB, 7)
    assert [(m["chat_id"], m["msg_id"], m["accounts"]) for m in around["messages"]] == [
        (two_accounts.BOB, 7, ["default"]),
        (two_accounts.BOB, 8, ["default"]),
    ]
    assert capsys.readouterr().out == ""


# --- research --------------------------------------------------------------------------------

RESEARCH_CFG = Config(telegram=KEYS, research=ResearchCfg(enabled=True))
RENT_PEER = -1000000000100
FLATS = make_channel(3001, "Tbilisi flats", username="tb_flats")
FLATS_PEER = -1000000003001
FORM = mcp_types.ClientCapabilities(elicitation=mcp_types.ElicitationCapability())
TERMINAL = "grepogram research approve 1 1:join,fetch,add_source"
RESEARCH_TOOLS = [tool for tool in tools.TOOLS if tool.__name__.startswith("research_")]


@dataclasses.dataclass
class FakeContext:
    """The part of FastMCP's ``Context`` ``research_approve`` uses: the client's declared
    capabilities and ``elicit``, which records what the user was asked and answers ``answer``
    (raised when it is an exception)."""

    capabilities: mcp_types.ClientCapabilities | None = dataclasses.field(
        default_factory=lambda: FORM
    )
    answer: object = dataclasses.field(default_factory=DeclinedElicitation)
    asked: list[tuple[str, type]] = dataclasses.field(default_factory=list)

    @property
    def session(self) -> SimpleNamespace:
        params = (
            None if self.capabilities is None else SimpleNamespace(capabilities=self.capabilities)
        )
        return SimpleNamespace(client_params=params)

    async def elicit(self, message: str, schema: type) -> object:
        self.asked.append((message, schema))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _accept(approve: bool = True) -> AcceptedElicitation[tools.Confirm]:
    return AcceptedElicitation(data=tools.Confirm(approve=approve))


@pytest.fixture
def researching(
    bind: Callable[..., tools.AppState], paths: Paths, conn: sqlite3.Connection
) -> FakeClient:
    """Research switched on over one indexed channel, ``@tbrent``, whose only message mentions
    ``@tb_flats`` — a public channel of two posts the default account is not in."""
    config.save(RESEARCH_CFG, paths)
    db.upsert_chat(
        conn, ChatRow(id=RENT_PEER, type="channel", title="Tbilisi rent", username="tbrent")
    )
    db.upsert_messages(
        conn,
        [
            MessageRow(
                chat_id=RENT_PEER,
                msg_id=1,
                date=1_735_689_600,
                text="flats at @tb_flats",
                links=(("mention", "@tb_flats"),),
            )
        ],
    )
    world = FakeWorld(
        entities=[FLATS],
        messages={FLATS_PEER: [tl.message(FLATS_PEER, i, f"flat {i}") for i in (1, 2)]},
    )
    client = world.client(
        me=make_user(9, "Me"), responses={functions.channels.GetFullChannelRequest: no_discussion}
    )
    bind(RESEARCH_CFG, client)
    return client


async def _discovered() -> None:
    """Session 1 from ``@tbrent``, discovered and probed: candidate 1 is ``@tb_flats``."""
    started = tools.research_start("who rents flats", ["@tbrent"], since_days=3650)
    assert "error" not in started, started
    discovered = await tools.research_discover(started["id"])
    assert "error" not in discovered, discovered
    assert discovered["new_candidates"] == [1]


def _grants(paths: Paths) -> list[Any]:
    rdb = research_db.open_store(paths)
    try:
        return research_db.list_grants(rdb, 1)
    finally:
        rdb.close()


async def test_research_tools_refuse_while_research_is_disabled(
    bind: Callable[..., tools.AppState], paths: Paths
) -> None:
    bind()
    ctx = FakeContext(answer=_accept())
    results = [
        tools.research_start("who rents flats", ["@tbrent"]),
        await tools.research_discover(1),
        tools.research_candidates(1),
        await tools.research_approve(1, ["1"], ctx),  # type: ignore[arg-type]
        tools.research_skip(1, [1]),
        tools.research_exclude(["@tb_flats"]),
        await tools.research_run(1),
        tools.research_status(),
        tools.research_stop(1),
    ]
    assert len(results) == len(RESEARCH_TOOLS)
    for result in results:
        assert result == {"error": "research is disabled", "hint": research.ENABLE_HINT}
    assert ctx.asked == [], "a refusal asks the user nothing"
    assert not paths.research_db_file.exists(), "a refusal opens no research store"


async def test_research_loop_through_the_tools(
    researching: FakeClient, paths: Paths, conn: sqlite3.Connection
) -> None:
    started = tools.research_start("who rents flats", ["@tbrent"], since_days=3650)
    assert (started["id"], started["account"], list(started["seeds"])) == (
        1,
        "default",
        [{"scope": "", "peer_id": RENT_PEER}],
    )
    assert started["limits"]["since_days"] == 3650 and started["horizon"]
    discovered = await tools.research_discover(1)
    assert discovered["new_candidates"] == [1] and discovered["probe"]["probed"] == [1]
    history = [kw for n, kw in researching.calls if n in ("iter_messages", "get_messages")]
    assert [kw for kw in history if kw["filter"] is not types.InputMessagesFilterPinned] == [], (
        "at most a seed's pinned posts are read, and nothing of any candidate"
    )
    listed = tools.research_candidates(1)
    (candidate,) = listed["candidates"]
    assert (candidate["identity"], candidate["title"], candidate["status"]) == (
        "@tb_flats",
        "Tbilisi flats",
        "proposed",
    )
    assert (candidate["member"], candidate["cached"], candidate["authorized"]) == (False, False, [])
    assert "access_hash" not in candidate
    (evidence,) = candidate["evidence"]
    assert evidence["snippet"] == "flats at @tb_flats"
    assert (evidence["scope"], evidence["peer_id"], evidence["chat_id"]) == (
        "",
        RENT_PEER,
        RENT_PEER,
    )

    rdb = research_db.open_store(paths)
    try:
        item = ApprovalItem(candidate_id=1, actions=("join", "fetch", "add_source"))
        summary = research.approval_summary(rdb, conn, RESEARCH_CFG, 1, [item])
    finally:
        rdb.close()
    ctx = FakeContext(answer=_accept())
    approved = await tools.research_approve(1, ["1"], ctx)  # type: ignore[arg-type]
    assert ctx.asked == [(summary, tools.Confirm)], "the user is asked with exactly the summary"
    assert approved["approved"] is True and approved["summary"] == summary
    assert approved["items"] == ["1:join,fetch,add_source"], "a bare id joins, public or not"
    assert approved["grants"] == [
        {
            "id": 1,
            "candidate_id": 1,
            "account": "default",
            "actions": ("join", "fetch", "add_source"),
        }
    ]
    (grant,) = _grants(paths)
    assert (grant.via, grant.summary) == ("elicitation", summary)

    status = tools.research_status(1)
    assert [g["actions"] for g in status["pending_grants"]] == [["join", "fetch", "add_source"]]
    assert tools.research_status()["sessions"][0]["state"] == "active"

    ran = await tools.research_run(1)
    assert (ran["joined"], ran["sources_added"], ran["fetched"], ran["messages"]) == (
        [1],
        [1],
        [1],
        2,
    )
    assert ran["accounts_skipped"] == []
    assert [source.id for source in config.load(paths).sources] == [f"chat:{FLATS_PEER}"]
    after = tools.research_candidates(1, status=["fetched"])["candidates"]
    assert [(c["status"], c["cached"]) for c in after] == [("fetched", True)]

    stopped = tools.research_stop(1)
    assert stopped == {
        "session_id": 1,
        "stopped": True,
        "grants_voided": 0,
        "hint": tools.STOP_HINT,
    }
    assert [source.id for source in config.load(paths).sources] == [f"chat:{FLATS_PEER}"]
    again = await tools.research_run(1)
    assert again["error"] == "research session 1 is stopped"


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (DeclinedElicitation(), "decline"),
        (CancelledElicitation(), "cancel"),
        (_accept(approve=False), "accept"),
    ],
)
async def test_research_approve_grants_nothing_unless_the_user_approves(
    researching: FakeClient, paths: Paths, answer: object, said: str
) -> None:
    await _discovered()
    ctx = FakeContext(answer=answer)

    result = await tools.research_approve(1, ["1:fetch,add_source"], ctx)  # type: ignore[arg-type]

    assert len(ctx.asked) == 1
    assert result["approved"] is False and result["answer"] == said
    assert result["hint"] == tools.DECLINED_HINT and "grants" not in result
    assert _grants(paths) == []
    listed = tools.research_candidates(1)["candidates"]
    assert [(c["status"], c["authorized"]) for c in listed] == [("proposed", [])]


@pytest.mark.parametrize(
    ("meanwhile", "error"),
    [
        (lambda: tools.research_stop(1), "research session 1 is stopped"),
        (lambda: tools.research_exclude(["@tb_flats"]), "candidate 1 (@tb_flats): it is excluded"),
    ],
)
async def test_research_approve_grants_nothing_once_the_session_changed_under_the_question(
    researching: FakeClient,
    paths: Paths,
    meanwhile: Callable[[], tools.ToolResult],
    error: str,
) -> None:
    """The user answers yes while another call stopped the session or excluded the chat: the
    grant is refused as a tool error, with nothing written and no traceback."""
    await _discovered()

    class ChangingContext(FakeContext):
        async def elicit(self, message: str, schema: type) -> object:
            assert "error" not in meanwhile()
            return await super().elicit(message, schema)

    ctx = ChangingContext(answer=_accept())
    result = await tools.research_approve(1, ["1:fetch,add_source"], ctx)  # type: ignore[arg-type]

    assert len(ctx.asked) == 1
    assert result["error"].startswith(error), result
    assert "grants" not in result
    assert _grants(paths) == []


@pytest.mark.parametrize(
    "failure", [TimeoutError("no answer"), McpError(mcp_types.ErrorData(code=-1, message="gone"))]
)
async def test_research_approve_that_cannot_ask_grants_nothing(
    researching: FakeClient, paths: Paths, failure: Exception
) -> None:
    await _discovered()
    ctx = FakeContext(answer=failure)

    result = await tools.research_approve(1, ["1"], ctx)  # type: ignore[arg-type]

    assert result["approved"] is False
    assert result["error"].startswith("the confirmation could not be asked")
    assert result["hint"].endswith(TERMINAL)
    assert _grants(paths) == []


@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        mcp_types.ClientCapabilities(),
        mcp_types.ClientCapabilities(
            elicitation=mcp_types.ElicitationCapability(url=mcp_types.UrlElicitationCapability())
        ),
    ],
)
async def test_research_approve_without_elicitation_names_the_terminal_command(
    researching: FakeClient, paths: Paths, capabilities: mcp_types.ClientCapabilities | None
) -> None:
    await _discovered()
    ctx = FakeContext(capabilities=capabilities, answer=_accept())

    result = await tools.research_approve(1, ["1"], ctx)  # type: ignore[arg-type]

    assert ctx.asked == [], "a client that cannot ask the user is never asked"
    assert result["approved"] is False and result["error"] == tools.NO_ELICITATION
    assert result["hint"] == (
        "the user must type this in their own terminal themselves and confirm there; never run "
        f"it for them: {TERMINAL}"
    )
    assert result["summary"].startswith("Research session 1")
    assert _grants(paths) == []


async def test_research_approve_refuses_an_invalid_approval_before_asking(
    researching: FakeClient, paths: Paths
) -> None:
    await _discovered()
    ctx = FakeContext(answer=_accept())

    fetch_only = await tools.research_approve(1, ["1:fetch"], ctx)  # type: ignore[arg-type]
    malformed = await tools.research_approve(1, ["flats"], ctx)  # type: ignore[arg-type]

    assert "approve `add_source` together with `fetch`" in fetch_only["error"]
    assert "'flats' is not an approval item" in malformed["error"]
    assert ctx.asked == [] and _grants(paths) == []


async def test_research_approve_elicits_over_a_real_session(
    researching: FakeClient, paths: Paths
) -> None:
    await _discovered()
    answers = [
        mcp_types.ElicitResult(action="accept", content={"approve": "true"}),
        mcp_types.ElicitResult(action="decline"),
        mcp_types.ElicitResult(action="accept", content={"approve": True}),
    ]
    asked: list[str] = []

    async def elicitation(context: object, params: Any) -> mcp_types.ElicitResult:
        asked.append(params.message)
        assert params.requestedSchema["properties"]["approve"]["type"] == "boolean"
        return answers.pop(0)

    server = tools.build_server()
    async with create_connected_server_and_client_session(
        server, elicitation_callback=elicitation
    ) as session:
        coerced = await session.call_tool("research_approve", {"session_id": 1, "items": ["1"]})
        declined = await session.call_tool("research_approve", {"session_id": 1, "items": ["1"]})
        assert _grants(paths) == [], "only a JSON true approves"
        accepted = await session.call_tool("research_approve", {"session_id": 1, "items": ["1"]})
    for result in (coerced, declined, accepted):
        assert not result.isError and result.structuredContent is not None
    assert coerced.structuredContent is not None and declined.structuredContent is not None
    assert coerced.structuredContent["approved"] is False
    assert declined.structuredContent["answer"] == "decline"
    assert accepted.structuredContent is not None
    assert accepted.structuredContent["approved"] is True
    (grant,) = _grants(paths)
    assert grant.via == "elicitation" and asked == [grant.summary] * 3


async def test_research_approve_over_a_session_without_elicitation_asks_nobody(
    researching: FakeClient, paths: Paths
) -> None:
    await _discovered()
    async with create_connected_server_and_client_session(tools.build_server()) as session:
        result = await session.call_tool("research_approve", {"session_id": 1, "items": ["1"]})
    assert result.structuredContent is not None
    assert result.structuredContent["error"] == tools.NO_ELICITATION
    assert result.structuredContent["hint"].endswith(TERMINAL)
    assert _grants(paths) == []


async def test_no_research_tool_takes_a_consent_parameter(state: tools.AppState) -> None:
    async with create_connected_server_and_client_session(tools.build_server()) as session:
        listed = {tool.name: tool for tool in (await session.list_tools()).tools}
    approve = listed["research_approve"].inputSchema
    assert set(approve["properties"]) == {"session_id", "items"}
    assert approve["required"] == ["session_id", "items"]
    consent = {"approve", "approved", "confirm", "confirmed", "yes", "force", "consent"}
    for tool in RESEARCH_TOOLS:
        assert not consent & set(listed[tool.__name__].inputSchema["properties"]), tool.__name__


async def test_research_skip_and_exclude_need_no_approval(researching: FakeClient) -> None:
    await _discovered()

    skipped = tools.research_skip(1, [1])
    excluded = tools.research_exclude(["@tb_flats"], reason="spam")
    unknown = tools.research_skip(1, [9])

    assert skipped == {"session_id": 1, "skipped": [1]}
    assert excluded == {
        "excluded": [{"identity": "@tb_flats", "candidates_set_aside": 1}],
        "hint": tools.UNEXCLUDE_HINT,
    }
    assert unknown["error"] == "no candidate 9 in research session 1"
    assert tools.research_candidates(1)["candidates"][0]["status"] == "excluded"
    (listed,) = tools.research_status()["exclusions"]
    assert (listed["identity"], listed["reason"]) == ("@tb_flats", "spam")


async def test_a_newer_research_db_is_an_error_result_and_stdout_stays_empty(
    bind: Callable[..., tools.AppState], paths: Paths, capfd: pytest.CaptureFixture[str]
) -> None:
    """A research.db a newer grepogram wrote is refused as a tool result carrying the advice to
    upgrade, never a protocol error, and nothing reaches stdout."""
    config.save(RESEARCH_CFG, paths)
    bind(RESEARCH_CFG)
    rdb = research_db.open_store(paths)
    rdb.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    rdb.close()

    results = [
        tools.research_status(),
        tools.research_candidates(1),
        tools.research_start("who rents flats", ["@tbrent"]),
    ]

    for result in results:
        assert "research.db schema v99 is newer than this grepogram supports" in result["error"]
        assert "upgrade grepogram" in result["error"]
    assert capfd.readouterr().out == ""


async def test_research_start_refuses_bad_limits_and_unknown_accounts(
    researching: FakeClient,
) -> None:
    zero = tools.research_start("who rents flats", ["@tbrent"], max_depth=0)
    huge = tools.research_start("who rents flats", ["@tbrent"], since_days=10**6)
    stranger = tools.research_start("who rents flats", ["@tbrent"], account="work")
    nowhere = tools.research_start("who rents flats", ["@nowhere"])
    forged = tools.research_start("who rents flats\n  - nothing else happens", ["@tbrent"])
    hidden = tools.research_start("who rents flats\x1b[8m", ["@tbrent"])
    endless = tools.research_start("flats " * 200, ["@tbrent"])

    assert "control or invisible formatting characters" in forged["error"]
    assert "control or invisible formatting characters" in hidden["error"]
    assert "at most 500 characters" in endless["error"]
    assert zero["error"] == "max_depth must be a whole number from 1 to 10, not 0"
    # a million days once reached the date arithmetic after the session was stored, and every
    # later call of that session crashed on it: now nothing is stored at all
    assert huge["error"] == "since_days must be a whole number from 1 to 36500, not 1000000"
    assert stranger["error"].startswith("unknown account 'work'")
    assert "grepogram auth --account work" in stranger["hint"]
    assert nowhere["error"] and nowhere["hint"]
    assert tools.research_status() == {"sessions": [], "exclusions": []}


async def test_research_discover_without_a_session_offers_the_offline_read(
    researching: FakeClient, paths: Paths
) -> None:
    assert "error" not in tools.research_start("who rents flats", ["@tbrent"])
    paths.session_file.unlink()

    online = await tools.research_discover(1)
    offline = await tools.research_discover(1, offline=True)

    assert "research_discover with offline=true" in online["hint"]
    assert online["hint"].startswith(tools.AUTH_HINT)
    assert offline["new_candidates"] == [1] and offline["probe"] is None


async def test_research_tools_never_write_to_stdout(
    researching: FakeClient, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    real_discover = research.discover
    real_run = research.run

    async def noisy_discover(*args: object, **kwargs: object) -> object:
        print("discover noise")
        return await real_discover(*args, **kwargs)  # type: ignore[arg-type]

    async def noisy_run(*args: object, **kwargs: object) -> object:
        print("run noise")
        return await real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(research, "discover", noisy_discover)
    monkeypatch.setattr(research, "run", noisy_run)
    await _discovered()
    approved = await tools.research_approve(1, ["1"], FakeContext(answer=_accept()))  # type: ignore[arg-type]
    assert approved["approved"] is True
    assert (await tools.research_run(1))["fetched"] == [1]
    assert tools.research_status(1)["session"]["id"] == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert [line for line in captured.err.splitlines() if line.endswith("noise")] == [
        "discover noise",
        "run noise",
    ]


async def test_ordinary_search_never_widens_what_research_found(
    researching: FakeClient, paths: Paths, conn: sqlite3.Connection
) -> None:
    """A candidate approved but not yet run stays outside `search`: only `research_run` acts on
    a grant, so a search neither fetches it, nor asks Telegram anything about it, nor spends
    the grant."""
    await _discovered()
    approved = await tools.research_approve(1, ["1"], FakeContext(answer=_accept()))  # type: ignore[arg-type]
    assert approved["approved"] is True
    rent = db.get_chat(conn, RENT_PEER)
    assert rent is not None
    ids = [
        row["id"] for row in conn.execute("SELECT id FROM messages WHERE chat_id = ?", (RENT_PEER,))
    ]
    index.index_chat(conn, rent, ids, units.rebuild_for_chat(conn, rent, RESEARCH_CFG, ids))
    db.set_chat_progress(conn, RENT_PEER, 1, int(time.time()))
    requests, calls = len(researching.requests), len(researching.calls)

    result = await tools.search("flat", mode="lexical")

    assert {hit["chat"]["id"] for hit in result["hits"]} == {RENT_PEER}
    assert researching.requests[requests:] == [] and researching.calls[calls:] == []
    assert db.message_counts(conn) == {RENT_PEER: 1}
    assert config.load(paths).sources == []
    (candidate,) = tools.research_candidates(1)["candidates"]
    assert (candidate["status"], candidate["authorized"]) == (
        "approved",
        ["join", "fetch", "add_source"],
    )


async def test_removing_a_source_research_added_leaves_the_chat_joined(
    researching: FakeClient, paths: Paths, conn: sqlite3.Connection
) -> None:
    """Stopping a session keeps the source its run added; removing that source afterwards
    deletes the config entry and the indexed history and never the membership — leaving a chat
    is `grepogram leave`, a command of its own."""
    await _discovered()
    ctx = FakeContext(answer=_accept())
    approved = await tools.research_approve(1, ["1:join,fetch,add_source"], ctx)  # type: ignore[arg-type]
    assert approved["approved"] is True
    ran = await tools.research_run(1)
    assert (ran["joined"], ran["fetched"]) == ([1], [1])
    assert FLATS_PEER in researching.members
    assert tools.research_stop(1)["stopped"] is True
    assert [source.id for source in config.load(paths).sources] == [f"chat:{FLATS_PEER}"]

    removed = tools.sources_remove(f"chat:{FLATS_PEER}")

    assert removed["removed_chat_ids"] == [FLATS_PEER]
    assert config.load(paths).sources == []
    assert db.get_chat(conn, FLATS_PEER) is None
    leaving = (functions.channels.LeaveChannelRequest, functions.messages.DeleteChatUserRequest)
    assert [r for r in researching.requests if isinstance(r, leaving)] == []
    assert FLATS_PEER in researching.members, "the account is still in the chat"
