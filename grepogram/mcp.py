"""The MCP server: the search engine as tools for Claude Code, spoken over stdio.

:func:`build_server` registers the nine tools of the contract on a
:class:`~mcp.server.fastmcp.FastMCP` named ``grepogram`` whose ``instructions`` carry the agent
playbook (:data:`INSTRUCTIONS`); :func:`main` is the ``grepogram-mcp`` entry point. Every tool is
a thin wrapper over the library — :mod:`grepogram.search`, :mod:`grepogram.sync`,
:mod:`grepogram.sources`, :mod:`grepogram.dialogs`, :mod:`grepogram.links` — that takes what it
needs from the bound :class:`AppState` (:func:`bind`) and returns a JSON-serialisable dict.
Expected failures — no session, another sync running, an unknown chat or message, a model that
cannot load — come back as a result with ``error`` and ``hint`` (plus ``candidates`` when there
is something to choose from), never as an exception, so Claude can act on them; anything else
propagates and FastMCP reports it as a tool error.

stdout is the protocol. Logging goes to stderr and the log file only, :func:`main` redirects
stdout to stderr while the server starts, and :data:`stdout_to_stderr` does the same around
every tool body (:func:`guarded` / :func:`guarded_async`), so a stray ``print`` deep in a library
cannot corrupt a JSON-RPC frame.

The tools that talk to Telegram (``sync``, ``dialogs``, ``sources_add``), ``search``, which may
refresh a stale index first, and ``open_message``, which waits for macOS ``open``, are
coroutines; the offline readers are plain functions. Retrieval, embedding and ``open`` run in
worker threads so the event loop keeps answering while they work — the SQLite connection
serialises its statements across threads (:class:`grepogram.db.Connection`) and the models
serialise their own calls. Tool calls arrive concurrently, so nothing that one call could close
under another is shared: every Telegram-using block builds and disconnects its own client on a
private in-memory copy of the session (:func:`grepogram.tg.make_client`), syncs queue on
``AppState.sync_lock`` instead of failing each other with ``SyncInProgress``, and config changes
go through ``AppState.editing_config`` — the process-wide lock plus the cross-process
:class:`grepogram.config.ConfigLock` — so neither two ``sources_add`` calls nor a CLI command in
another terminal can overwrite each other's save.
"""

import argparse
import asyncio
import contextlib
import dataclasses
import functools
import inspect
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, TextIO

from mcp.server.fastmcp import FastMCP
from telethon import errors as tg_errors

from grepogram import config, db, embed, filters, links, tg
from grepogram import rerank as reranking
from grepogram import search as retrieval
from grepogram import sources as sourcing
from grepogram import sync as syncing
from grepogram.config import ConfigError
from grepogram.dialogs import DialogCatalog, Match
from grepogram.dialogs import match as match_dialogs
from grepogram.embed import Embedder, ModelUnavailable
from grepogram.filters import FilterError, UnknownChat
from grepogram.links import OpenFailed
from grepogram.log import setup_logging
from grepogram.models import Config, Filters, SearchResult
from grepogram.paths import Paths
from grepogram.rerank import Reranker
from grepogram.search import UnknownMessage
from grepogram.sources import AmbiguousTarget, SourceError
from grepogram.sync import SyncBudget, SyncInProgress, SyncLock
from grepogram.tg import AuthRequired, SessionError

log = logging.getLogger(__name__)

SERVER_NAME = "grepogram"
INSTRUCTIONS = """\
grepogram searches the Telegram chats the user opted in to, locally: hybrid retrieval (stemmed \
BM25 fused with dense embeddings, then a cross-encoder rerank) over conversation units — time \
windows, reply threads and channel posts — in Russian and English. How to use it well:
- Run 2-3 query variants per question: Russian and English, the specific term and the concept, \
synonyms (for example "ВНЖ", "residence permit", "residencia"). Prefer mode "lexical" for exact \
tokens such as bank names, IDs or prices.
- Chat knowledge is time-sensitive: prefer recent hits for anything regulatory, procedural or \
price-related, filter with `since` when it matters, and state the date of the evidence \
(`date_start`/`date_end` are unix seconds, UTC).
- Call `thread` or `context` on a hit before drawing a conclusion from its snippet; the answer \
usually sits in the replies.
- Cite the hit's `url` for every claim so the user can open the message in Telegram \
(`open_message` opens it directly).
- If nothing relevant comes back, say so rather than guess — after trying other variants, \
filters or chats.
- When the user names a chat that is not indexed yet, call `sources` to see what is indexed and \
`dialogs` to find the chat or folder, then `sources_add` and `sync`.
- A result with `error` explains what went wrong and `hint` what to do next; `warnings` are \
advisory and the hits alongside them are valid.
"""

Mode = Literal["hybrid", "lexical", "dense"]
ToolResult = dict[str, Any]
ClientFactory = Callable[[Config, Paths], Any]

AUTH_HINT = "sign in from a terminal with `grepogram auth`, then retry"
SETUP_HINT = (
    "create an application at https://my.telegram.org/apps, run `grepogram config init`, fill "
    "in [telegram] api_id and api_hash, then `grepogram auth`"
)
LOCK_HINT = "another grepogram process (the CLI or a second server) is syncing; retry when it ends"
SYNC_RUNNING = (
    "a sync started by another tool call is still running; the hits come from the index as it "
    "is — search again when it ends"
)
SESSION_HINT = (
    "another grepogram process is writing the session file (a `grepogram auth` in progress); "
    "retry when it ends — if the file is damaged, delete it and sign in again with `grepogram auth`"
)
MODEL_HINT = (
    "install the dense extra (`uv sync --extra dense`) and restart the MCP server, or search "
    'with mode="lexical"'
)
CHAT_HINT = (
    "pick one of the indexed chats or folders in candidates; sources_add + sync index a new one"
)
PICK_HINT = "retry with one of the candidates: an id, @username or folder:<name>"
MESSAGE_HINT = "chat_id and msg_id come from a hit (chat.id and anchor_msg_id) or a message view"
OPEN_HINT = "open the url yourself: paste it into a browser or the Telegram app"
NO_OPEN_ERROR = f"opening links is switched off ({links.NO_OPEN_ENV} is set)"
NO_SOURCES_HINT = "find chats with dialogs, add them with sources_add, then sync"
SYNC_NEXT_HINT = "call sync to fetch and index its history"

TOOL_ERRORS: tuple[type[Exception], ...] = (
    AuthRequired,
    SessionError,
    SyncInProgress,
    ConfigError,
    ModelUnavailable,
    FilterError,
    SourceError,
    UnknownMessage,
    OpenFailed,
    ValueError,
    tg_errors.RPCError,
    ConnectionError,
)
"""Failures a tool answers with ``error``/``hint``; anything else is a bug and propagates.

``ValueError`` covers the argument checks the library raises (an unknown search mode, ``k`` or
``budget_s`` at zero, a negative ``context`` count); ``ConnectionError`` is what Telethon raises
when Telegram cannot be reached at all. Other ``OSError`` are environment failures and propagate.
"""

AUTO_SYNC_ERRORS: tuple[type[Exception], ...] = (
    AuthRequired,
    SessionError,
    SyncInProgress,
    ConfigError,
    tg_errors.RPCError,
    OSError,
)
"""Failures of the automatic refresh inside ``search``; they become warnings, the search runs.

Wider than :data:`TOOL_ERRORS` on purpose: a refresh is a courtesy, and no I/O failure in it
may cost the caller the search itself.
"""


class NotConfigured(ConfigError):
    """``[telegram] api_id`` / ``api_hash`` are not filled in, so no client can be built."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"[telegram] api_id and api_hash are not set in {path}")


# --- stdout hygiene --------------------------------------------------------------------------


class StdoutGuard:
    """Send everything printed to stdout to stderr while any guarded block runs.

    A plain ``contextlib.redirect_stdout`` per block restores the previous stream in exit order,
    which goes wrong when two tool calls interleave on the event loop (the first to finish would
    hand the real stdout back while the second still runs). The guard counts the active blocks
    and keeps one ``redirect_stdout(sys.stderr)`` up from the first entry to the last exit.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._depth = 0
        self._redirect: contextlib.redirect_stdout[TextIO] | None = None

    def __enter__(self) -> None:
        with self._lock:
            if self._depth == 0:
                self._redirect = contextlib.redirect_stdout(sys.stderr)
                self._redirect.__enter__()
            self._depth += 1

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with self._lock:
            self._depth -= 1
            if self._depth == 0 and self._redirect is not None:
                self._redirect.__exit__(None, None, None)
                self._redirect = None


stdout_to_stderr = StdoutGuard()
"""The one guard every tool body runs under."""


# --- shared state ----------------------------------------------------------------------------


class AppState:
    """What the tools share: paths, config and the database, plus the embedder and the reranker,
    each loaded on first use and kept.

    ``config()`` re-reads ``config.toml`` on every call, so a source added with the CLI (or by
    hand) while the server runs is picked up by the next tool call. A model that fails to load
    is not retried: the reason is kept in ``embed_error`` / ``rerank_error`` and repeated in
    every result's ``warnings`` until the server restarts. Telegram clients are not kept:
    :meth:`telegram` builds one per block through ``client_factory``
    (:func:`grepogram.tg.make_client` unless a test injects a fake). ``sync_lock`` lets one sync
    run at a time in this process — the explicit ``sync`` tool and the refresh inside ``search``
    queue behind each other rather than tripping over the cross-process
    :class:`~grepogram.sync.SyncLock` — and :meth:`editing_config` serialises config changes.
    """

    def __init__(
        self,
        paths: Paths,
        cfg: Config,
        conn: sqlite3.Connection,
        *,
        client_factory: ClientFactory = tg.make_client,
    ) -> None:
        self.paths = paths
        self.cfg = cfg
        self.conn = conn
        self.client_factory = client_factory
        self.sync_lock = asyncio.Lock()
        self._embedder: Embedder | None = None
        self._reranker: Reranker | None = None
        self.embed_error: str | None = None
        self.rerank_error: str | None = None
        self._models_lock = threading.Lock()
        self._config_lock = threading.Lock()

    @classmethod
    def open(cls, paths: Paths) -> "AppState":
        """Read the config and open the index database (migrating it) at ``paths``."""
        cfg = config.load(paths)
        conn = db.connect(paths)
        db.migrate(conn)
        return cls(paths, cfg, conn)

    def close(self) -> None:
        self.conn.close()

    def config(self) -> Config:
        """The current config: ``config.toml`` as it is now, or the one given when there is no
        file yet."""
        if self.paths.config_file.exists():
            self.cfg = config.load(self.paths)
        return self.cfg

    def save_config(self, cfg: Config) -> None:
        """Write ``cfg`` to ``config.toml`` and make it the current one."""
        config.save(cfg, self.paths)
        self.cfg = cfg

    @contextmanager
    def editing_config(self) -> Iterator[Config]:
        """The config as it is now, for a read-modify-write that ends in :meth:`save_config`.

        Tool calls run concurrently, and a call that resolved its target over the network must
        not save the snapshot it started from — another call, or ``grepogram sources add`` /
        ``rm`` in a terminal, may have saved in between. The block holds the one lock every
        config change in this process goes through and, inside it, the cross-process
        :class:`~grepogram.config.ConfigLock` the CLI takes for its own edits, so the config it
        reads is the one its save replaces. Keep the block short and free of awaits.
        """
        with self._config_lock, config.ConfigLock(self.paths):
            yield self.config()

    @asynccontextmanager
    async def telegram(self) -> AsyncIterator[Any]:
        """A connected, authorized client for the block, built for it and disconnected on exit.

        A fresh ``TelegramClient`` per block reads the session file as it is now — so a session
        created with ``grepogram auth`` after a failed call works without a restart, and a
        client that once found itself unauthorized (Telethon remembers that per instance) is
        never asked again — on a private in-memory copy, so concurrent blocks (and a CLI sync
        in another process) share neither a connection nor a session database. Raises
        :class:`NotConfigured` without API keys, :class:`~grepogram.tg.SessionMissing` before
        any client is built when there is no session file, :class:`~grepogram.tg.SessionError`
        when the file cannot be read, and :class:`~grepogram.tg.AuthRequired` for a session
        Telegram rejects.
        """
        cfg = self.config()
        if cfg.telegram.api_id == 0 or not cfg.telegram.api_hash:
            raise NotConfigured(self.paths.config_file)
        tg.ensure_session_mode(self.paths)
        client = self.client_factory(cfg, self.paths)
        async with tg.connected(client) as connected:
            yield connected

    def load_embedder(self, cfg: Config) -> Embedder:
        """The embedding model, loaded once and kept (a :data:`grepogram.search.EmbedderLoader`).

        A load that failed is not retried: the same :class:`~grepogram.embed.ModelUnavailable`
        is raised again from ``embed_error``.
        """
        with self._models_lock:
            if self._embedder is None:
                if self.embed_error is not None:
                    raise ModelUnavailable(self.embed_error)
                try:
                    self._embedder = embed.load_embedder(cfg)
                except ModelUnavailable as exc:
                    self.embed_error = str(exc)
                    log.warning("embedding model unavailable: %s", exc)
                    raise
            return self._embedder

    def load_reranker(self, cfg: Config) -> Reranker:
        """The cross-encoder, loaded once and kept; a failure is remembered like the embedder's."""
        with self._models_lock:
            if self._reranker is None:
                if self.rerank_error is not None:
                    raise ModelUnavailable(self.rerank_error)
                try:
                    self._reranker = reranking.load_reranker(cfg)
                except ModelUnavailable as exc:
                    self.rerank_error = str(exc)
                    log.warning("reranker model unavailable: %s", exc)
                    raise
            return self._reranker

    def embedder(self) -> Embedder | None:
        """The embedding model for a sync, ``None`` (see ``embed_error``) when it cannot load."""
        try:
            return self.load_embedder(self.config())
        except ModelUnavailable:
            return None


_bound: AppState | None = None


def bind(state: AppState) -> None:
    """Make ``state`` the one the tools use; :func:`main` binds the real one, tests a fake."""
    global _bound
    _bound = state


def unbind() -> None:
    global _bound
    _bound = None


def _app() -> AppState:
    if _bound is None:
        raise RuntimeError("no AppState is bound; call grepogram.mcp.bind() first")
    return _bound


# --- failures as results ---------------------------------------------------------------------


def describe(exc: BaseException) -> str:
    """The ``error`` text for an expected failure."""
    if isinstance(exc, AuthRequired):
        return exc.reason
    if isinstance(exc, tg_errors.RPCError):
        return f"telegram error: {exc}"
    if isinstance(exc, ConnectionError):
        return f"connection error: {exc}"
    return str(exc)


def hint_for(exc: BaseException) -> str | None:
    """What to do about an expected failure, when there is something to do."""
    if isinstance(exc, AuthRequired):
        return AUTH_HINT
    if isinstance(exc, NotConfigured):
        return SETUP_HINT
    if isinstance(exc, SessionError):
        return SESSION_HINT
    if isinstance(exc, SyncInProgress):
        return LOCK_HINT
    if isinstance(exc, ModelUnavailable):
        return MODEL_HINT
    if isinstance(exc, UnknownChat):
        return exc.hint or CHAT_HINT
    if isinstance(exc, AmbiguousTarget):
        return PICK_HINT
    if isinstance(exc, UnknownMessage):
        return MESSAGE_HINT
    return None


def failure(exc: BaseException) -> ToolResult:
    """The result a tool returns for an expected failure: ``error``, ``hint`` and, for an
    unknown or ambiguous chat, the ``candidates`` to pick from."""
    result: ToolResult = {"error": describe(exc), "hint": hint_for(exc)}
    candidates = getattr(exc, "candidates", None)
    if candidates:
        result["candidates"] = list(candidates)
    return result


def guarded[**P](fn: Callable[P, ToolResult]) -> Callable[P, ToolResult]:
    """Wrap a synchronous tool: stdout silenced, expected failures turned into results."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
        with stdout_to_stderr:
            try:
                return fn(*args, **kwargs)
            except TOOL_ERRORS as exc:
                log.warning("%s failed: %s", fn.__name__, exc)
                return failure(exc)

    return wrapper


def guarded_async[**P](
    fn: Callable[P, Awaitable[ToolResult]],
) -> Callable[P, Coroutine[Any, Any, ToolResult]]:
    """:func:`guarded` for a coroutine tool."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
        with stdout_to_stderr:
            try:
                return await fn(*args, **kwargs)
            except TOOL_ERRORS as exc:
                log.warning("%s failed: %s", fn.__name__, exc)
                return failure(exc)

    return wrapper


# --- tools -----------------------------------------------------------------------------------


@guarded_async
async def search(
    query: str,
    chats: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    k: int = 10,
    mode: Mode = "hybrid",
    rerank: bool = True,
    full: bool = False,
) -> ToolResult:
    """Search the indexed Telegram chats; returns ranked hits with deep links.

    Hits are conversation units — time windows, reply threads, channel posts — with `chat`,
    `kind`, `date_start`/`date_end` (unix seconds, UTC), `anchor_msg_id` (the message the link
    opens), `url`, `snippet` and `msg_ids`; `full=true` adds the whole unit `text`. `chats`
    restricts the search: each entry is a chat id, `@username`, t.me link, `folder:<name>` or
    a chat / folder title (fuzzy). `since` / `until` take an ISO date (2025-06-01), month
    (2025-06), datetime (2025-06-01T14:30) or an age such as 7d, 3w, 6m, 1y; `until` is
    inclusive. `mode`: `hybrid` fuses stemmed BM25 with dense embeddings (default), `lexical` is
    BM25 only (exact tokens, names, numbers), `dense` is embeddings only (paraphrase); without
    vectors or the model every mode falls back to lexical and says so in `warnings`. `rerank`
    re-scores the top candidates with a cross-encoder. A stale index is refreshed briefly first
    (`synced=true`); problems with that refresh are `warnings`, the hits are still valid.
    `index_age_min` is the age of the index. A bad filter comes back as `error` with `hint` and
    `candidates`.
    """
    state = _app()
    cfg = state.config()
    warnings: list[str] = []
    synced = False
    if _stale(state, cfg):
        synced, warnings = await _auto_sync(state, cfg)
    selected = filters.resolve_filters(state.conn, cfg, chats, since, until)
    result = await asyncio.to_thread(_retrieve, state, cfg, query, selected, k, mode, rerank, full)
    result = dataclasses.replace(result, warnings=[*warnings, *result.warnings], synced=synced)
    return asdict(result)


def _stale(state: AppState, cfg: Config) -> bool:
    """Whether the last sync run — or, before any run was stamped, the last completed chat sync —
    is older than ``auto_sync_after_min``; a never-synced index is not stale, it is empty."""
    latest = db.last_sync_run(state.conn)
    if latest is None:
        latest = db.last_sync_at(state.conn)
    if latest is None:
        return False
    return max(0, int(time.time()) - latest) // 60 > cfg.search.auto_sync_after_min


async def _auto_sync(state: AppState, cfg: Config) -> tuple[bool, list[str]]:
    """Refresh a stale index within ``auto_sync_budget_s``; ``(synced, warnings)``.

    Every expected failure — no session, a sync already running in another process, a flood
    wait, a network error — is a warning and the search goes on over the index as it is. A sync
    already running in this process is waited for instead, but no longer than the budget: a
    short refresh leaves the index fresh and nothing is fetched again, while a long explicit
    ``sync`` is reported as a warning and the search runs on the index as it is. The sources
    are resolved from the config as it is once the sync lock is held (``state.config``), so a
    source removed while this call was loading its model or connecting stays removed.
    """
    budget_s = cfg.search.auto_sync_budget_s
    try:
        async with asyncio.timeout(budget_s):
            await state.sync_lock.acquire()
    except TimeoutError:
        log.warning("auto-sync skipped: a sync in this process still runs after %ss", budget_s)
        return False, [f"auto-sync skipped: {SYNC_RUNNING}"]
    try:
        if not _stale(state, cfg):
            log.debug("auto-sync: the index was refreshed while this call waited")
            return False, []
        embedder = await asyncio.to_thread(state.embedder)
        async with state.telegram() as client:
            report = await syncing.sync_all(
                client, state.conn, state.config, state.paths, SyncBudget(budget_s), embedder
            )
    except AUTO_SYNC_ERRORS as exc:
        log.warning("auto-sync skipped: %s", exc)
        return False, [f"auto-sync skipped: {describe(exc)}"]
    finally:
        state.sync_lock.release()
    warnings = [f"auto-sync: {warning}" for warning in report.warnings]
    if report.chats_remaining:
        warnings.append(
            f"auto-sync stopped after {budget_s}s with {len(report.chats_remaining)} chats "
            "still behind; call sync to finish"
        )
    if report.unavailable:
        ids = ", ".join(str(chat_id) for chat_id in report.unavailable)
        warnings.append(
            f"auto-sync: {len(report.unavailable)} chats are unavailable on Telegram ({ids}); "
            "their stored messages are still searched"
        )
    log.info(
        "auto-sync: %d new messages, %d chats done, %d remaining",
        report.new,
        len(report.chats_done),
        len(report.chats_remaining),
    )
    return True, warnings


def _retrieve(
    state: AppState,
    cfg: Config,
    query: str,
    selected: Filters,
    k: int,
    mode: str,
    rerank: bool,
    full: bool,
) -> SearchResult:
    """The search proper, on a worker thread, with the state's model loaders.

    :func:`grepogram.search.search` asks for a model only when it needs one (no vectors, no
    embedder) and degrades with a warning when the loader raises; the state's loaders keep a
    loaded model and remember a failure, so the degradation is the same on every call.
    """
    return retrieval.search(
        state.conn,
        cfg,
        query,
        selected,
        k,
        mode=mode,
        full=full,
        rerank=rerank,
        load_embedder=state.load_embedder,
        load_reranker=state.load_reranker,
    )


@guarded
def thread(chat_id: int, msg_id: int) -> ToolResult:
    """The whole reply thread a message belongs to, root first, chronological.

    For a channel post: the post followed by its comments from the linked discussion chat. Each
    message has `msg_id`, `date` (unix seconds, UTC), `from_name`, `text`, `url`, `fallback_url`
    and `reply_to_msg_id`. Read it before concluding from a snippet; `chat_id` and `msg_id` come
    from a hit's `chat.id` and `anchor_msg_id`.
    """
    state = _app()
    views = retrieval.thread(state.conn, chat_id, msg_id)
    return {"chat_id": chat_id, "msg_id": msg_id, "messages": [asdict(view) for view in views]}


@guarded
def context(chat_id: int, msg_id: int, before: int = 15, after: int = 15) -> ToolResult:
    """The messages around one in its chat (or forum topic), in order: up to `before` earlier
    and `after` later ones, the message itself included. Same message fields as `thread`; use it
    when a hit needs the surrounding conversation rather than the reply chain.
    """
    state = _app()
    views = retrieval.context(state.conn, chat_id, msg_id, before, after)
    return {"chat_id": chat_id, "msg_id": msg_id, "messages": [asdict(view) for view in views]}


@guarded_async
async def sync(budget_s: int = 45) -> ToolResult:
    """Fetch new messages from every configured source into the index (needs a signed-in
    session). Runs for at most `budget_s` seconds and stops cleanly: `chats_remaining` lists
    what is still behind — call again to continue. Returns `new` (messages stored),
    `chats_done`, `chats_remaining`, `unavailable` (chats Telegram refused), `warnings` and
    `index_age_min`. New units are embedded when the model is available.
    """
    state = _app()
    if budget_s <= 0:
        raise ValueError(f"budget_s must be a positive number of seconds, got {budget_s}")
    cfg = state.config()
    if not cfg.sources:
        return {"error": "no sources are configured", "hint": NO_SOURCES_HINT}
    embedder = await asyncio.to_thread(state.embedder)
    async with state.sync_lock, state.telegram() as client:
        report = await syncing.sync_all(
            client, state.conn, state.config, state.paths, SyncBudget(budget_s), embedder
        )
    warnings = list(report.warnings)
    if embedder is None:
        warnings.append(f"dense index not updated: {state.embed_error}")
    return {
        **asdict(report),
        "warnings": warnings,
        "index_age_min": retrieval.index_age_min(state.conn),
    }


@guarded
def sources() -> ToolResult:
    """List the configured sources with the chats indexed through each: `id`, `title`, `type`,
    `username`, `message_count`, `last_sync_at` (unix seconds, null before the first sync) and
    `unavailable`. A source with no chats has not been synced yet. `index_age_min` is minutes
    since the last completed sync (null before the first).
    """
    state = _app()
    statuses = sourcing.sources_status(state.config(), state.conn)
    return {
        "sources": [asdict(status) for status in statuses],
        "index_age_min": retrieval.index_age_min(state.conn),
    }


@guarded_async
async def dialogs(query: str) -> ToolResult:
    """Find chats and folders of the signed-in Telegram account whose title, `@username` or
    folder name matches `query` (substring first, then fuzzy). Each match has `kind` (`dialog`
    or `folder`), `id`, `title`, `type` (user, bot, group, supergroup, channel or folder),
    `username`, `folders` (the folders a chat is in), `score` and `target`, the value to pass to
    `sources_add`. Use it when the user names a chat that is not indexed yet.
    """
    state = _app()
    async with state.telegram() as client:
        catalog = DialogCatalog(client)
        found = match_dialogs(query, await catalog.list_dialogs(), await catalog.list_folders())
    return {"query": query, "matches": [_match_dict(found_match) for found_match in found]}


def _match_dict(found: Match) -> ToolResult:
    if found.folder is not None:
        return {
            "kind": "folder",
            "id": found.id,
            "title": found.title,
            "type": "folder",
            "username": None,
            "folders": [],
            "score": round(found.score, 3),
            "target": f"{sourcing.FOLDER_PREFIX}{found.title}",
        }
    dialog = found.dialog
    assert dialog is not None
    return {
        "kind": "dialog",
        "id": dialog.id,
        "title": dialog.title,
        "type": dialog.type,
        "username": dialog.username,
        "folders": list(dialog.folders),
        "score": round(found.score, 3),
        "target": f"@{dialog.username}" if dialog.username else str(dialog.id),
    }


@guarded_async
async def sources_add(target: str, since: str | None = None, comments: bool = False) -> ToolResult:
    """Add a Telegram folder or chat to the indexed sources and save the config (needs a
    signed-in session). `target` is a chat id or `@username` (as `dialogs` reports them), a
    t.me link, `folder:<name>`, or a chat / folder title (fuzzy; an ambiguous one comes back as
    `error` with `candidates`). `since` (YYYY-MM-DD) skips older history on the first sync;
    `comments=true` (channels only) also indexes the linked discussion threads. Returns the
    stored `source` and the `chats` it covers; call `sync` afterwards.
    """
    state = _app()
    parsed = sourcing.parse_target(target)
    async with state.telegram() as client:
        catalog = DialogCatalog(client)
        added = await sourcing.add_source(
            state.config(), parsed, catalog, since=since, comments=comments
        )
    dialog = None if added.folder is not None else added.dialogs[0]
    with state.editing_config() as current:
        state.save_config(sourcing.with_source(current, added.source, dialog))
    log.info("source %s added (%s)", added.source.id, added.title)
    return {
        "source": {"id": added.source.id, **asdict(added.source)},
        "kind": "folder" if added.folder is not None else "chat",
        "title": added.title,
        "chats": [asdict(dialog) for dialog in added.dialogs],
        "hint": SYNC_NEXT_HINT,
    }


@guarded
def sources_remove(target: str) -> ToolResult:
    """Remove a source and delete its chats' messages and index data (offline). `target` is a
    source id from `sources` (`folder:Argentina`, `chat:@name`), a folder name, a chat id /
    `@username`, or a fuzzy title. A chat that came in through a folder cannot be removed on
    its own (remove the folder source or take the chat out of the folder in Telegram), nor can
    a channel's discussion group indexed through the channel's source. Refused with `error`
    while a sync is running.
    """
    state = _app()
    parsed = sourcing.parse_target(target)
    with SyncLock(state.paths), state.editing_config() as current:
        removed = sourcing.remove_source(current, state.conn, parsed)
        if removed.source is not None:
            state.save_config(removed.config)
    log.info("source %s removed (%d chats)", removed.source_id, len(removed.chat_ids))
    return {
        "source_id": removed.source_id,
        "removed_chat_ids": removed.chat_ids,
        "config_updated": removed.source is not None,
    }


@guarded_async
async def open_message(chat_id: int, msg_id: int) -> ToolResult:
    """Open a stored message in the Telegram app on this Mac and return the `url` used
    (`opened=true`). When the app cannot be launched — or opening is switched off with
    `GREPOGRAM_NO_OPEN` — the result still carries the `url` (and `fallback_url` for private
    chats) with `error` and `hint`, so the link can be shown instead.
    """
    state = _app()
    chat = db.get_chat(state.conn, chat_id)
    message = db.get_message(state.conn, chat_id, msg_id)
    if chat is None or message is None:
        raise UnknownMessage(chat_id, msg_id)
    link = links.message_url(chat, msg_id, message.topic_id)
    result: ToolResult = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "url": link.url,
        "fallback_url": link.fallback_url,
    }
    if links.opening_disabled():
        result.update(opened=False, error=NO_OPEN_ERROR, hint=OPEN_HINT)
        return result
    try:
        result["url"] = await asyncio.to_thread(links.open_link, link)
    except (OpenFailed, NotImplementedError) as exc:
        log.warning("cannot open %s: %s", link.url, exc)
        result.update(opened=False, error=str(exc), hint=OPEN_HINT)
        return result
    result["opened"] = True
    return result


TOOLS: tuple[Callable[..., Any], ...] = (
    search,
    thread,
    context,
    sync,
    sources,
    dialogs,
    sources_add,
    sources_remove,
    open_message,
)


# --- server ----------------------------------------------------------------------------------


def build_server() -> FastMCP[Any]:
    """A ``grepogram`` FastMCP server with the nine tools; docstrings are the descriptions."""
    server: FastMCP[Any] = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    for tool in TOOLS:
        server.add_tool(tool, description=inspect.cleandoc(tool.__doc__ or ""))
    return server


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of ``grepogram-mcp``: serve the tools over stdio until the client hangs up.

    Logging goes to stderr and the log file; stdout is redirected to stderr while the state
    loads and the server is built, and again while it shuts down, so nothing but the protocol
    reaches it. A broken config or an index from a newer schema stops the start with a message.
    """
    parser = argparse.ArgumentParser(
        prog="grepogram-mcp", description="grepogram MCP server for Claude Code (stdio)."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Log at DEBUG level instead of INFO."
    )
    args = parser.parse_args(argv)
    paths = Paths.from_env()
    setup_logging(paths, logging.DEBUG if args.verbose else logging.INFO)
    if not args.verbose:
        logging.getLogger("mcp").setLevel(logging.WARNING)
    with contextlib.redirect_stdout(sys.stderr):
        try:
            state = AppState.open(paths)
        except (ConfigError, db.SchemaError) as exc:
            raise SystemExit(f"error: {exc}") from exc
        bind(state)
        server = build_server()
    log.info("grepogram-mcp serving %s over stdio", paths.db_file)
    try:
        server.run(transport="stdio")
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            unbind()
            state.close()
        log.info("grepogram-mcp stopped")
