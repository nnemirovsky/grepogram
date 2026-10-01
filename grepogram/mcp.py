"""The MCP server: the search engine as tools for Claude Code, spoken over stdio.

:func:`build_server` registers the eighteen tools of the contract on a
:class:`~mcp.server.fastmcp.FastMCP` named ``grepogram`` whose ``instructions`` carry the agent
playbook (:data:`INSTRUCTIONS`); :func:`main` is the ``grepogram-mcp`` entry point. Every tool is
a thin wrapper over the library — :mod:`grepogram.search`, :mod:`grepogram.sync`,
:mod:`grepogram.sources`, :mod:`grepogram.dialogs` — that takes what it needs from the bound
:class:`AppState` (:func:`bind`) and returns a JSON-serialisable dict. Expected failures — no
session, another sync running, an unknown chat or message, a model that cannot load — come back as
a result with ``error`` and ``hint`` (plus ``candidates`` when there is something to choose from),
never as an exception, so Claude can act on them; anything else propagates and FastMCP reports it
as a tool error.

stdout is the protocol. Logging goes to stderr and the log file only, :func:`main` redirects
stdout to stderr while the server starts, and :data:`stdout_to_stderr` does the same around
every tool body (:func:`guarded` / :func:`guarded_async`), so a stray ``print`` deep in a library
cannot corrupt a JSON-RPC frame.

The tools that talk to Telegram (``sync``, ``dialogs``, ``sources_add``, ``research_discover``,
``research_run``), ``research_approve``, which waits for the user's answer, and ``search``, which
may refresh a stale index first, are coroutines; the offline readers are plain functions. Several
Telegram accounts may be signed in at once: ``sync`` and the refresh inside ``search`` connect
every one of them (:meth:`AppState.telegrams`) and go on without an account whose session is
missing or signed out, reporting it with the command that signs it in; ``dialogs`` and
``sources_add`` act as the one account they are given (:meth:`AppState.telegram`). Retrieval
and embedding run in worker threads so the event loop keeps answering while they work — the SQLite
connection serialises its statements across threads (:class:`grepogram.db.Connection`) and the
models serialise their own calls. Tool calls arrive concurrently, so nothing that one call could
close under another is shared: every Telegram-using block builds and disconnects its own clients on
private in-memory copies of the sessions (:func:`grepogram.tg.make_client`), syncs queue on
``AppState.sync_lock`` instead of failing each other with ``SyncInProgress``, and config changes
go through ``AppState.editing_config`` — the process-wide lock plus the cross-process
:class:`grepogram.config.ConfigLock` — so neither two ``sources_add`` calls nor a CLI command in
another terminal can overwrite each other's save.

The ``research_*`` tools drive :mod:`grepogram.research` over ``research.db``
(:meth:`AppState.research_store`) and answer with the documents ``grepogram research … --json``
prints. Each refuses while ``[research] enabled`` is false. ``research_approve`` puts the exact
:func:`~grepogram.research.approval_summary` to the user: through MCP elicitation when the client
can show a dialog, granting (``via="elicitation"``) only on an accepted answer whose ``approve``
is ``true``; otherwise it answers with the summary and a ``confirm`` token bound to it
(:mod:`grepogram.consent`), and a second call with that token grants (``via="confirm"``). The
token only binds the confirmation to exactly what was shown; the agent is expected to show the
user the summary and confirm on their say-so, and a token whose summary no longer matches is
refused. A client with a dialog cannot use the token.
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
from typing import Any, TextIO, TypedDict

from mcp.server.elicitation import AcceptedElicitation
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field
from telethon import errors as tg_errors

from grepogram import config, consent, db, embed, filters, research, research_db, tg
from grepogram import rerank as reranking
from grepogram import search as retrieval
from grepogram import sources as sourcing
from grepogram import sync as syncing
from grepogram.config import ConfigError
from grepogram.dialogs import DialogCatalog, Match
from grepogram.dialogs import match as match_dialogs
from grepogram.embed import Embedder, ModelUnavailable
from grepogram.filters import FilterError, UnknownChat
from grepogram.log import setup_logging
from grepogram.models import (
    DEFAULT_ACCOUNT,
    Config,
    Filters,
    Grant,
    LimitOverrides,
    MessageView,
    SearchMode,
    SearchResult,
)
from grepogram.paths import Paths
from grepogram.rerank import Reranker
from grepogram.search import UnknownMessage
from grepogram.sources import AmbiguousTarget, SourceError
from grepogram.sync import SyncBudget, SyncInProgress, SyncLock
from grepogram.tg import AuthRequired, OtherUser, SessionError

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
usually sits in the replies. Pass a message's own `chat_id` back with its `msg_id`: a channel \
post's comments come from the discussion group, and both chats number their messages from 1.
- Cite the hit's `url` for every claim: it is a clickable link to the message in Telegram.
- If nothing relevant comes back, say so rather than guess — after trying other variants, \
filters or chats.
- When the user names a chat that is not indexed yet, call `sources` to see what is indexed and \
`dialogs` to find the chat or folder, then `sources_add` and `sync`.
- Several Telegram accounts may be signed in: `accounts` lists them. `dialogs` and `sources_add` \
act as one account (`account`, the default one when omitted), and a match's `target` already \
names it; `sync` fetches through every signed-in account and names any account it had to skip, \
with the command that signs it in. One search spans every account; each hit's `accounts` says \
which ones reach its chat, and `search(accounts=[...])` or a `chats` entry `account:<name>` \
narrows it to what an account reaches — a scope, not isolation. Name the account a claim came \
through when the accounts differ.
- Research finds chats the user does not index yet; its tools refuse until the user sets \
`[research] enabled = true`. The playbook: `research_start` with the question, indexed seed \
chats and the `account` that will join and fetch (what a run adds is reached through it); \
`research_discover`; `research_candidates`, reading each candidate's `evidence` (a message \
it came from is read with `thread` / `context` by the evidence's `chat_id`; `null` means the \
index does not hold that chat) and its three separate facts — `member`, `cached` (and through \
which accounts), `authorized`; tell the \
user what was found and why, and ask which to approve (only a candidate an online \
`research_discover` probed can be approved; one found offline or past `probe_limit` needs \
another online discover first); `research_approve` either shows the user the \
exact summary in a dialog, or answers with `needs_confirmation`, the `summary` and a `confirm` \
token — then show the user that summary verbatim, and only when they agree call \
`research_approve` again with the same `items` and `confirm` (a token whose summary changed is \
refused with a fresh one; never confirm on your own judgement); \
`research_run`; analyse what it fetched with `search`, `thread` and `context`; `research_stop` \
when done (the sources stay). Approving a chat approves nothing found inside it. \
`research_skip` and `research_exclude` only narrow and need no approval.
- Corroboration counts distinct origins: forwards and copies of one post are one source, not \
independent confirmation, however many chats repeat them. Say so when a claim rests on one \
forwarded post, and prefer evidence from independent chats.
- A result with `error` explains what went wrong and `hint` what to do next; `warnings` are \
advisory and the hits alongside them are valid.
"""

ToolResult = dict[str, Any]

SETUP_HINT = (
    "create an application at https://my.telegram.org/apps, run `grepogram config init`, fill "
    "in [telegram] api_id and api_hash, then `grepogram auth`"
)
LOCK_HINT = "another grepogram process (the CLI or a second server) is syncing; retry when it ends"
SYNC_RUNNING = (
    "a sync started by another tool call is still running; the hits come from the index as it "
    "is — search again when it ends"
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
CONFIG_HINT = (
    "fix the file `grepogram config path` prints; its keys are the ones README's Configuration "
    "section documents"
)
NO_SOURCES_HINT = "find chats with dialogs, add them with sources_add, then sync"
ACCOUNTS_HINT = "`accounts` lists the accounts; a new one is signed in from a terminal"
SYNC_NEXT_HINT = "call sync to fetch and index its history"
CONFIRM_HINT = (
    "nothing was granted yet: show the user `summary` exactly as written and ask whether to "
    "approve it; only once they agree, call research_approve again with the same items and "
    "`confirm` set to this result's `confirm` token. The token approves exactly this summary "
    "and is refused once it would say anything else"
)
DIALOG_FAILED_HINT = (
    "nothing was granted: show the user `summary` exactly as written; once they agree, "
    "`command` confirms exactly it from a shell"
)
ELICIT_INSTEAD = (
    "this MCP client asks the user through its own dialog, so a confirmation token is not taken "
    "here"
)
ELICIT_INSTEAD_HINT = "call research_approve again without `confirm`; the user answers the dialog"
DECLINED_HINT = (
    "nothing was granted; ask the user what they want instead — research_skip sets a candidate "
    "aside, research_exclude never proposes it again"
)
RUN_NEXT_HINT = "call research_run to carry it out"
STOP_HINT = "the sources its runs added stay configured; sources_remove drops one"
UNEXCLUDE_HINT = "an exclusion is lifted from a terminal: `grepogram research unexclude`"

TOOL_ERRORS: tuple[type[Exception], ...] = (
    AuthRequired,
    SessionError,
    SyncInProgress,
    ConfigError,
    ModelUnavailable,
    FilterError,
    SourceError,
    UnknownMessage,
    research.ResearchError,
    research_db.SchemaError,
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


def auth_hint(account: str = DEFAULT_ACCOUNT) -> str:
    """What to do about ``account``'s missing or rejected session: sign it in from a terminal
    with its own command (:func:`grepogram.tg.auth_command`)."""
    return f"sign in from a terminal with `{tg.auth_command(account)}`, then retry"


def session_hint(account: str = DEFAULT_ACCOUNT) -> str:
    """What to do about ``account``'s session file that cannot be read."""
    return (
        "another grepogram process is writing the session file (a `grepogram auth` in "
        "progress); retry when it ends — if the file is damaged, delete it and sign in again "
        f"with `{tg.auth_command(account)}`"
    )


AUTH_HINT = auth_hint()
SESSION_HINT = session_hint()


class SkippedAccount(TypedDict):
    """An account a multi-account block went on without: why, and what to do about it."""

    account: str
    error: str
    hint: str | None


def skipped_list(accounts: tg.Accounts) -> list[SkippedAccount]:
    """Each account a multi-account block went on without, with its error and the hint that
    names its sign-in."""
    return [
        {"account": name, "error": _reason(exc), "hint": _account_hint(name, exc)}
        for name, exc in accounts.skipped.items()
    ]


def skipped_warnings(accounts: tg.Accounts) -> list[str]:
    """One warning per left-out account: its name, why, and how to sign it in."""
    return [
        f"account {item['account']} skipped: {item['error']}"
        + (f"; {item['hint']}" if item["hint"] else "")
        for item in skipped_list(accounts)
    ]


def _reason(exc: Exception) -> str:
    """Why an account was left out, without the account name the caller already gives."""
    return exc.reason if isinstance(exc, AuthRequired) else describe(exc)


def _account_hint(account: str, exc: Exception) -> str | None:
    if isinstance(exc, SessionError):
        return session_hint(account)
    return hint_for(exc)


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
    :meth:`telegram` builds one per block for one account, :meth:`telegrams` one per signed-in
    account, both through ``client_factory`` (:func:`grepogram.tg.make_client` unless a test
    injects a fake). ``sync_lock`` lets one sync
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
        client_factory: tg.ClientFactory = tg.make_client,
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
        self._research: sqlite3.Connection | None = None
        self._research_lock = threading.Lock()

    @classmethod
    def open(cls, paths: Paths) -> "AppState":
        """Read the config and open the index database (migrating it) at ``paths``."""
        cfg = config.load(paths)
        conn = db.connect(paths)
        db.migrate(conn)
        return cls(paths, cfg, conn)

    def close(self) -> None:
        if self._research is not None:
            self._research.close()
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

    def research_store(self, cfg: Config) -> sqlite3.Connection:
        """``research.db`` for a research tool, opened on first use and kept.

        Refuses with :class:`~grepogram.research.ResearchDisabled` while ``[research] enabled``
        is false, before the file is opened or created. The connection is shared like the
        index's: :class:`grepogram.db.Connection` serialises its statements across threads.
        """
        research.require_enabled(cfg)
        with self._research_lock:
            if self._research is None:
                self._research = research_db.open_store(self.paths)
            return self._research

    def account(self, cfg: Config, name: str | None) -> str:
        """``name`` (the default account when ``None``) if ``cfg`` knows it, else
        :class:`~grepogram.config.UnknownAccount` naming the known ones
        (:func:`grepogram.config.require_account`)."""
        return config.require_account(cfg, name or DEFAULT_ACCOUNT)

    def _require_keys(self, cfg: Config) -> None:
        if cfg.telegram.api_id == 0 or not cfg.telegram.api_hash:
            raise NotConfigured(self.paths.config_file)

    @asynccontextmanager
    async def telegram(self, account: str = DEFAULT_ACCOUNT) -> AsyncIterator[Any]:
        """A connected, authorized client of ``account`` for the block, built for it and
        disconnected on exit.

        A fresh ``TelegramClient`` per block reads the session file as it is now — so a session
        created with ``grepogram auth`` after a failed call works without a restart, and a
        client that once found itself unauthorized (Telethon remembers that per instance) is
        never asked again — on a private in-memory copy, so concurrent blocks (and a CLI sync
        in another process) share neither a connection nor a session database. Raises
        :class:`~grepogram.config.UnknownAccount` for an account the config does not know,
        :class:`NotConfigured` without API keys, :class:`~grepogram.tg.SessionMissing` before
        any client is built when there is no session file, :class:`~grepogram.tg.SessionError`
        when the file cannot be read, and :class:`~grepogram.tg.AuthRequired` for a session
        Telegram rejects — each naming ``account``.
        """
        cfg = self.config()
        self.account(cfg, account)
        self._require_keys(cfg)
        tg.ensure_session_mode(self.paths, account)
        client = self.client_factory(cfg, self.paths, account)
        async with tg.connected(client, account) as connected:
            yield connected

    @asynccontextmanager
    async def telegrams(self) -> AsyncIterator[tg.Accounts]:
        """A connected client of every signed-in account for the block — each built for it, on
        its own copy of its session, and all disconnected on exit — as :meth:`telegram` builds
        one; the same rules as ``grepogram sync`` (:func:`grepogram.tg.make_clients`,
        :func:`grepogram.tg.connected_all`).

        An account whose session file is missing or unreadable is left out, and reported in
        :attr:`~grepogram.tg.Accounts.skipped` when it owns a configured source; an account
        Telegram refuses is left out and reported too. One account's failure never stops the
        others. Only when no account can connect at all is a reason raised — a source owner's
        first, so a single-account install reads what it always did.
        """
        cfg = self.config()
        self._require_keys(cfg)
        built = tg.make_clients(cfg, self.paths, factory=self.client_factory)
        async with tg.connected_all(built.clients, built.skipped) as live:
            yield live

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
    """The ``error`` text for an expected failure; a session failure of an account other than
    the default one names the account."""
    if isinstance(exc, AuthRequired):
        if exc.account != DEFAULT_ACCOUNT:
            return f"account {exc.account}: {exc.reason}"
        return exc.reason
    if isinstance(exc, tg_errors.RPCError):
        return f"telegram error: {exc}"
    if isinstance(exc, ConnectionError):
        return f"connection error: {exc}"
    return str(exc)


def hint_for(exc: BaseException) -> str | None:
    """What to do about an expected failure, when there is something to do."""
    if isinstance(exc, OtherUser):
        return exc.hint
    if isinstance(exc, AuthRequired):
        return auth_hint(exc.account)
    if isinstance(exc, NotConfigured):
        return SETUP_HINT
    if isinstance(exc, config.UnknownAccount):
        return auth_hint(exc.name)
    if isinstance(exc, ConfigError):
        return exc.hint or CONFIG_HINT
    if isinstance(exc, SessionError):
        return session_hint(exc.account)
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
    if isinstance(exc, research.ResearchError):
        return exc.hint
    return None


def failure(exc: BaseException) -> ToolResult:
    """The result a tool returns for an expected failure: ``error``, ``hint`` and, for an
    unknown or ambiguous chat, the ``candidates`` to pick from."""
    result: ToolResult = {"error": describe(exc), "hint": hint_for(exc)}
    candidates = getattr(exc, "candidates", None)
    if candidates:
        result["candidates"] = list(candidates)
    return result


def tool_failure(name: str, exc: Exception) -> ToolResult:
    """Log a tool's expected failure and turn it into the result the caller sees.

    A research refusal or a source target's can quote what someone else wrote — an invite or
    shared-folder link out of a message, which is a private way in — so its text is logged at
    DEBUG, like message text, and only its kind above it; the caller gets it all either way."""
    if isinstance(exc, research.ResearchError | SourceError):
        log.warning("%s failed: %s", name, type(exc).__name__)
        log.debug("%s failed: %s", name, exc)
    else:
        log.warning("%s failed: %s", name, exc)
    return failure(exc)


def guarded[**P](fn: Callable[P, ToolResult]) -> Callable[P, ToolResult]:
    """Wrap a synchronous tool: stdout silenced, expected failures turned into results."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
        with stdout_to_stderr:
            try:
                return fn(*args, **kwargs)
            except TOOL_ERRORS as exc:
                return tool_failure(fn.__name__, exc)

    return wrapper


def guarded_async[**P](
    fn: Callable[P, Awaitable[ToolResult]],
) -> Callable[P, Coroutine[Any, Any, ToolResult]]:
    """:func:`guarded` for a coroutine tool; the ``async def`` is all that differs."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
        with stdout_to_stderr:
            try:
                return await fn(*args, **kwargs)
            except TOOL_ERRORS as exc:
                return tool_failure(fn.__name__, exc)

    return wrapper


# --- tools -----------------------------------------------------------------------------------


@guarded_async
async def search(
    query: str,
    chats: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    k: int = 10,
    mode: SearchMode = "hybrid",
    rerank: bool = True,
    full: bool = False,
    accounts: list[str] | None = None,
) -> ToolResult:
    """Search the indexed Telegram chats; returns ranked hits with deep links.

    Hits are conversation units — time windows, reply threads, channel posts — with `chat`,
    `peer_id` (the chat's Telegram id), `accounts` (the signed-in accounts that reach the chat;
    empty for an import), `kind`, `date_start`/`date_end` (unix seconds, UTC), `anchor_msg_id`
    (the message the link opens), `url`, `snippet` and `msg_ids`; `full=true` adds the whole
    unit `text`. `chats` restricts the search: each entry is a chat id, `@username`, t.me link,
    `folder:<name>`, `import:<slug>` (a source id `sources` reports), `account:<name>` (every
    chat that account reaches), `<account>/<id>` (that account's private chat with a person) or
    a chat / folder title (fuzzy). `accounts` narrows the search to the chats those accounts
    reach — a scope, not isolation: a channel two accounts reach is in both scopes. `since` /
    `until` take an ISO date (2025-06-01), month (2025-06), datetime (2025-06-01T14:30) or an
    age such as 7d, 3w, 6m, 1y; `until` is inclusive. `mode`: `hybrid` fuses stemmed BM25 with
    dense embeddings (default), `lexical` is BM25 only (exact tokens, names, numbers), `dense`
    is embeddings only (paraphrase); without vectors or the model every mode falls back to
    lexical and says so in `warnings`. `rerank` re-scores the top candidates with a
    cross-encoder. A stale index is refreshed briefly first
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
    result = await asyncio.to_thread(
        _retrieve, state, cfg, query, selected, k, mode, rerank, full, accounts
    )
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
    wait, a network error — is a warning and the search goes on over the index as it is. Every
    signed-in account takes part; one whose session is missing or refused is a warning naming
    it, and the others refresh. A sync
    already running in this process is waited for instead, but no longer than the budget: a
    short refresh leaves the index fresh and nothing is fetched again, while a long explicit
    ``sync`` is reported as a warning and the search runs on the index as it is. The sources
    are resolved from the config as it is once the sync lock is held (``state.config``), so a
    source removed while this call was loading its model or connecting stays removed.

    It passes ``recut=False``, as a research run (``research.running.run``) does — the only two
    callers that do: a search refreshes messages and never starts the one-time unit re-cut,
    whatever ``search.auto_sync_budget_s`` is set to.
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
        async with state.telegrams() as accounts:
            report = await syncing.sync_all(
                accounts.clients,
                state.conn,
                state.config,
                state.paths,
                SyncBudget(budget_s),
                embedder,
                recut=False,
            )
    except AUTO_SYNC_ERRORS as exc:
        log.warning("auto-sync skipped: %s", exc)
        return False, [f"auto-sync skipped: {describe(exc)}"]
    finally:
        state.sync_lock.release()
    warnings = [
        f"auto-sync: {warning}" for warning in [*skipped_warnings(accounts), *report.warnings]
    ]
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
    mode: SearchMode,
    rerank: bool,
    full: bool,
    accounts: Sequence[str] | None = None,
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
        accounts=accounts,
        load_embedder=state.load_embedder,
        load_reranker=state.load_reranker,
    )


@guarded
def thread(chat_id: int, msg_id: int) -> ToolResult:
    """The whole reply thread a message belongs to, root first, chronological.

    For a channel post: the post followed by its comments from the linked discussion chat. Each
    message has `chat_id`, `peer_id` (the chat's Telegram id), `msg_id`, `date` (unix seconds,
    UTC), `from_name`, `text`, `url`, `fallback_url`, `reply_to_msg_id` and `accounts` (the
    accounts that reach its chat). A message's own `chat_id` is the one to pass back to
    `context` with its `msg_id` — comments carry the discussion group's id, not the channel's,
    and the two number their messages from 1 alike. Read it before concluding from a snippet;
    the arguments come from a hit's `chat.id` and `anchor_msg_id`.
    """
    return _messages_result(chat_id, msg_id, retrieval.thread(_app().conn, chat_id, msg_id))


@guarded
def context(chat_id: int, msg_id: int, before: int = 15, after: int = 15) -> ToolResult:
    """The messages around one in its chat (or forum topic), in order: up to `before` earlier
    and `after` later ones, the message itself included. Same message fields as `thread`, every
    one of them in the chat asked about; use it when a hit needs the surrounding conversation
    rather than the reply chain.
    """
    views = retrieval.context(_app().conn, chat_id, msg_id, before, after)
    return _messages_result(chat_id, msg_id, views)


def _messages_result(chat_id: int, msg_id: int, views: Sequence[MessageView]) -> ToolResult:
    """What ``thread`` and ``context`` both answer: the message they were asked about and the
    :class:`~grepogram.models.MessageView` list around it.

    The top-level ``chat_id`` is the argument, not where every message lives: ``thread`` mixes a
    channel's post with its discussion group's comments, and each message carries its own
    ``chat_id`` for that reason."""
    return {"chat_id": chat_id, "msg_id": msg_id, "messages": [asdict(view) for view in views]}


@guarded_async
async def sync(budget_s: int = 45) -> ToolResult:
    """Fetch new messages from every configured source of every signed-in account into the
    index. Runs for at most `budget_s` seconds and stops cleanly: `chats_remaining` lists
    what is still behind — call again to continue. Returns `new` (messages stored),
    `chats_done`, `chats_remaining`, `unavailable` (chats Telegram refused), `warnings`,
    `accounts_skipped` and `index_age_min`. An account whose session is missing or signed out
    does not stop the others: it is listed in `accounts_skipped` with its `error` and the
    `hint` that signs it in, and its sources wait for the next sync. New units are embedded
    when the model is available.

    This call is also what lets a pending one-time unit re-cut make progress: it is a deliberate
    act, like `grepogram sync` in a terminal, so it re-cuts a few chats a run (bounded and
    resumable) while the automatic sync inside `search` never starts one. Call it again until
    the warning `search` returns about a pending re-cut is gone.
    """
    if budget_s <= 0:
        raise ValueError(f"budget_s must be a positive number of seconds, got {budget_s}")
    state = _app()
    cfg = state.config()
    if not cfg.sources:
        return {"error": "no sources are configured", "hint": NO_SOURCES_HINT}
    embedder = await asyncio.to_thread(state.embedder)
    async with state.sync_lock, state.telegrams() as accounts:
        report = await syncing.sync_all(
            accounts.clients,
            state.conn,
            state.config,
            state.paths,
            SyncBudget(budget_s),
            embedder,
        )
    warnings = [*skipped_warnings(accounts), *report.warnings]
    if embedder is None:
        warnings.append(f"dense index not updated: {state.embed_error}")
    return {
        **asdict(report),
        "warnings": warnings,
        "accounts_skipped": skipped_list(accounts),
        "index_age_min": retrieval.index_age_min(state.conn),
    }


@guarded
def sources() -> ToolResult:
    """List every source the index holds chats under, with the `account` it belongs to and the
    chats indexed through each: `id`, `title`, `type`, `username`, `message_count`,
    `last_sync_at` (unix seconds, null before the first sync), `unavailable` and `accounts` (the
    accounts that reach the chat). A chat several sources cover is listed under each. A source
    with no chats has not been synced yet. `index_age_min` is minutes since the last completed
    sync (null before the first).

    The configured sources come first, then any other `source_id` still in the database. An
    `import:<slug>` is a Telegram Desktop export the user indexed from a file: those chats are
    `unavailable` and no sync ever fetches them, but they are searched like any other.
    """
    state = _app()
    statuses = sourcing.sources_status(state.config(), state.conn)
    return {
        "sources": [asdict(status) for status in statuses],
        "index_age_min": retrieval.index_age_min(state.conn),
    }


@guarded_async
async def dialogs(query: str, account: str | None = None) -> ToolResult:
    """Find chats and folders of a signed-in Telegram account whose title, `@username` or
    folder name matches `query` (substring first, then fuzzy). `account` names the account to
    look in (`accounts` lists them); omitted, it is the default one. Each match has `kind`
    (`dialog` or `folder`), `id`, `title`, `type` (user, bot, group, supergroup, channel or
    folder), `username`, `folders` (the folders a chat is in), `score` and `target`, the value
    to pass to `sources_add` — for an account other than the default one it carries the
    `<account>/` prefix, so the source is added for the account that found it. Use it when the
    user names a chat that is not indexed yet.
    """
    state = _app()
    name = state.account(state.config(), account)
    async with state.telegram(name) as client:
        catalog = DialogCatalog(client)
        found = match_dialogs(query, await catalog.list_dialogs(), await catalog.list_folders())
    return {
        "query": query,
        "account": name,
        "matches": [_match_dict(found_match, name) for found_match in found],
    }


def _match_dict(found: Match, account: str = DEFAULT_ACCOUNT) -> ToolResult:
    if found.folder is not None:
        return {
            "kind": "folder",
            "id": found.id,
            "title": found.title,
            "type": "folder",
            "username": None,
            "folders": [],
            "score": round(found.score, 3),
            "target": _target(account, f"{sourcing.FOLDER_PREFIX}{found.title}"),
        }
    dialog = found.dialog
    assert dialog is not None
    chat = f"@{dialog.username}" if dialog.username else str(dialog.id)
    return {
        "kind": "dialog",
        "id": dialog.id,
        "title": dialog.title,
        "type": dialog.type,
        "username": dialog.username,
        "folders": list(dialog.folders),
        "score": round(found.score, 3),
        "target": chat if account == DEFAULT_ACCOUNT else _target(account, f"chat:{chat}"),
    }


def _target(account: str, target: str) -> str:
    """``target`` as ``sources_add`` reads it for ``account``: an ``<account>/`` prefix for any
    account but the default one (:func:`grepogram.sources.parse_target`)."""
    return target if account == DEFAULT_ACCOUNT else f"{account}/{target}"


@guarded_async
async def sources_add(
    target: str, since: str | None = None, comments: bool = False, account: str | None = None
) -> ToolResult:
    """Add a Telegram folder or chat of an account to the indexed sources and save the config
    (needs that account's signed-in session). `target` is a chat id or `@username` (as
    `dialogs` reports them), a t.me link, `folder:<name>`, or a chat / folder title (fuzzy; an
    ambiguous one comes back as `error` with `candidates`). `account` names the account the
    source belongs to; omitted, it is the one an `<account>/` prefix on `target` names (a
    `dialogs` target carries it), else the default one. `since` (YYYY-MM-DD) skips older
    history on the first sync;
    `comments=true` (channels only) also indexes the linked discussion threads. Returns the
    stored `source` and the `chats` it covers; call `sync` afterwards. A chat already in the
    index as a Telegram Desktop import comes back as `error`: a live source would take it over
    on the next sync and the imported history would be lost.
    """
    state = _app()
    parsed = sourcing.parse_target(target)
    name = state.account(state.config(), account or parsed.account)
    async with state.telegram(name) as client:
        catalog = DialogCatalog(client)
        added = await sourcing.add_source(
            state.config(), parsed, catalog, since=since, comments=comments, account=name
        )
    # `db.upsert_chat` overwrites `source_id`, so a live source over an imported chat would drop
    # the `import:` tag every protection of that history keys on; the CLI refuses the same way
    sourcing.refuse_imported(state.conn, added.dialogs, name)
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
    """Remove a source and delete the messages and index data of the chats no other source
    covers (offline; never leaves a chat on Telegram). `target` is a
    source id from `sources` (`folder:Argentina`, `chat:@name`, `work/chat:@name` for a source
    of the account `work`), a folder name, a chat id / `@username`, or a fuzzy title; without
    an `<account>/` prefix the default account's source is meant first. A chat that came in
    through a folder cannot be removed on its own (remove the folder source or take the chat
    out of the folder in Telegram), nor can a channel's discussion group indexed through the
    channel's source. A chat another source
    still covers (a channel a second account also configured) is kept and listed in
    `kept_chat_ids`; `removed_chat_ids` are the chats deleted. `undecided_chat_ids` are the
    kept ones only a source not synced yet might cover — a folder of an account that has a
    session, or a fuzzy-title entry their stored title matches: they wait under it, and once it
    syncs, the CLI's `grepogram sources prune` offers the ones it does not cover. A `target`
    naming one chat held under a `chat:` source that resolved to another chat deletes that
    chat alone: `removed_chat_ids` holds it, `source_id` is the source it was held under, which
    stays, and `config_updated` is false. Refused with `error` while a sync is running.
    """
    state = _app()
    parsed = sourcing.parse_target(target)
    with SyncLock(state.paths), state.editing_config() as current:
        removed = sourcing.remove_source(
            current, state.conn, parsed, has_session=sourcing.with_session(state.paths)
        )
        if removed.source is not None:
            state.save_config(removed.config)
    if removed.stray:
        log.info(
            "removed chat %s, held under %s which does not cover it; that source stays",
            removed.chat_ids[0],
            removed.source_id,
        )
    else:
        log.info("source %s removed (%d chats)", removed.source_id, len(removed.chat_ids))
    return {
        "source_id": removed.source_id,
        "removed_chat_ids": removed.chat_ids,
        "kept_chat_ids": removed.kept_chat_ids,
        "undecided_chat_ids": removed.undecided_chat_ids,
        "config_updated": removed.source is not None,
    }


@guarded
def accounts() -> ToolResult:
    """List the Telegram accounts grepogram knows (offline, read-only): `name`, `label`,
    `session` (`missing` with no session file, `authorized` when the account was signed in the
    last time grepogram used it, `present` for a file no run has confirmed yet), `user_id` and
    `display_name` (null until confirmed), `sources` (the ids of its configured sources),
    `chats` (how many indexed chats it reaches) and `hint` (how to sign it in, when its session
    is missing). Accounts are added, removed and signed in from a terminal only.
    """
    state = _app()
    listed = [
        {**asdict(status), "hint": auth_hint(status.name) if status.session == "missing" else None}
        for status in sourcing.accounts_status(state.config(), state.paths, state.conn)
    ]
    return {"accounts": listed, "hint": ACCOUNTS_HINT}


# --- research --------------------------------------------------------------------------------


class Confirm(BaseModel):
    """The one answer an approval elicitation asks for: a box the user ticks.

    Strict, so only a JSON ``true`` approves — never a string or a number a client coerced."""

    model_config = ConfigDict(strict=True)

    approve: bool = Field(
        default=False,
        title="Approve",
        description="Approve everything the message lists, exactly as written",
    )


def _can_elicit(ctx: Context) -> bool:  # type: ignore[type-arg]
    """Whether the client declared form elicitation when it connected.

    An empty ``elicitation`` capability is form mode (the shape clients sent before modes were
    named); a client that names its modes must name ``form``. No request context, no client
    parameters: no.
    """
    try:
        params = ctx.session.client_params
    except (ValueError, AttributeError):
        return False
    if params is None:
        return False
    capability = params.capabilities.elicitation
    if capability is None:
        return False
    return capability.form is not None or capability.url is None


@guarded
def research_start(
    question: str,
    seeds: list[str],
    account: str | None = None,
    max_depth: int | None = None,
    max_candidates: int | None = None,
    probe_limit: int | None = None,
    since_days: int | None = None,
    max_messages_per_run: int | None = None,
    run_budget_s: int | None = None,
) -> ToolResult:
    """Start a research session (offline): a `question` in the user's words (one line of plain
    text, at most 500 characters — it is shown in every approval), the indexed chats
    to start from (`seeds`, each a chat spec as `search`'s `chats` takes it) and the `account`
    that later joins and fetches (the default one when omitted). The limits default to the
    config's [research] section: `max_depth` hops from a seed, `max_candidates` new candidates
    and `probe_limit` probes per discover call, `since_days` of history for every source a run
    adds, `max_messages_per_run` and `run_budget_s` per run (the session's own ceiling,
    `max_session_candidates`, and `admission_timeout_days` come from the config). Returns the
    session (`id`, `seeds` as `{scope, peer_id}` — each chat as Telegram names it, so the
    session outlives a rebuilt index —, `limits`, `state`, `horizon` — the date added sources
    start from). Next: `research_discover`.
    """
    state = _app()
    cfg = state.config()
    rdb = state.research_store(cfg)
    name = account or DEFAULT_ACCOUNT
    overrides = LimitOverrides(
        max_depth=max_depth,
        max_candidates=max_candidates,
        probe_limit=probe_limit,
        since_days=since_days,
        max_messages_per_run=max_messages_per_run,
        run_budget_s=run_budget_s,
    )
    session = research.start_session(rdb, state.conn, cfg, question, seeds, name, overrides)
    return research.session_document(session)


@guarded_async
async def research_discover(session_id: int, offline: bool = False) -> ToolResult:
    """Find the chats a research session's chats lead to — links, hidden hyperlinks, mentions,
    buttons, forward origins, shared folders — and propose them as candidates, marking a chat
    that lists many others a directory (`directories`; its finds carry a `directory` path); then,
    unless `offline`, read the pinned posts of the session's own chats once (`pins`: their leads
    are evidence only, never indexed messages), probe the best candidates on Telegram as the
    session's account (title, type, size, membership, whether admins approve joins — never their
    history) and run the global searches the user approved. Nothing is joined or fetched: every
    find is only proposed. Returns the report: `leads`, `new_candidates`, `updated_candidates`,
    what was left out (`beyond_depth`, `excluded`, `over_cap`, `session_full` when the session's
    ceiling is reached), `pins`, `probe` and `searches`. Next: `research_candidates`.
    """
    state = _app()
    cfg = state.config()
    rdb = state.research_store(cfg)
    session = research.active_session(rdb, session_id)
    if offline:
        report = await syncing.joined_to_thread(
            functools.partial(research.discover_offline, rdb, state.conn, cfg, session.id)
        )
        return research.report_document(report)
    try:
        async with state.telegram(session.account) as client:
            report = await research.discover(rdb, state.conn, cfg, session.id, client)
    except (AuthRequired, SessionError) as exc:
        result = tool_failure("research_discover", exc)
        offline_hint = "or call research_discover with offline=true to read the index alone"
        result["hint"] = f"{result['hint']}; {offline_hint}" if result["hint"] else offline_hint
        return result
    return research.report_document(report)


@guarded
def research_candidates(session_id: int, status: list[str] | None = None) -> ToolResult:
    """A research session's candidates, best corroborated first (offline), optionally only those
    in the given `status`es (proposed, approved, skipped, excluded, joined, pending_admission,
    fetched, unavailable, failed). Each has `id`, `identity`, `title`, `type`, `participants`,
    `request_needed`, `depth`, `status`, `note`, `corroboration` (distinct origins — forwards
    of one post count once), `overlap` (question terms in the evidence) and three separate
    facts: `member` (the session's account is in it; null before a probe), `cached` (the index
    already holds it, through `cached_accounts`) and `authorized` (the actions the user
    approved and no run has carried out yet). `evidence` lists every path that led to it:
    `via` (a `pinned` post, a `directory`, a link kind, a forward, a search…), the chat it was
    found in as Telegram names it (`scope`, `peer_id`) and as the index holds it now (`chat_id`,
    what `thread` and `context` take; `null` for a chat the index does not hold, such as a
    search result), `msg_id`, `origin_key` and a `snippet`.
    """
    state = _app()
    cfg = state.config()
    rdb = state.research_store(cfg)
    return research.candidates_document(rdb, state.conn, cfg, session_id, status)


@guarded_async
async def research_approve(
    session_id: int,
    items: list[str],
    ctx: Context,  # type: ignore[type-arg]
    confirm: str | None = None,
) -> ToolResult:
    """Ask the user to approve candidates and actions. `items` name them: `ID:join,fetch,…` per
    candidate (actions: `join`, `request` — an admission request —, `fetch`, `add_source`), a
    bare `ID` for joining it (or requesting to, where its admins approve joins) and fetching it
    as an ongoing source — `ID:fetch,add_source` reads a public chat without joining, and only
    when the user asks for that —, `global_search` / `paid_search` for the session.

    `summary` in the result is the exact text being approved: each target, the account,
    membership, every action in words, that an added source is ongoing. A client with form
    elicitation shows it to the user in a dialog and only their answer there grants anything
    (`confirm` is refused on such a client). A client without one answers with
    `needs_confirmation=true`, the `summary`, a `confirm` token bound to that exact summary and
    the equivalent shell `command`: show the user the summary as written, and only after they
    agree call this again with the same `items` and `confirm` — a token whose summary no longer
    matches (a probe changed a candidate, other items) is refused with the fresh summary and
    token. `approved=true` comes with `grants`; `approved=false` means nothing was granted
    (`answer` says whether the user declined or cancelled the dialog). Approving a chat
    approves nothing found inside it. Next: `research_run`.

    Refused before anything is asked: a candidate not probed yet (run `research_discover`
    online first), a person or a bot, a shared folder itself (approve its chats), an
    excluded, unavailable or fetched candidate; `fetch` without `add_source`; `join` for a chat
    whose admins approve joins (`request` instead) and `request` for any other; `join` and
    `request` together; `fetch` / `add_source` for a private chat the account is not in without
    `join` or `request`. `paid_search` needs `post_search` on, `paid_stars_max` above 0 and
    `global_search` approved too.
    """
    state = _app()
    cfg = state.config()
    rdb = state.research_store(cfg)
    approval = research.prepare_approval(rdb, state.conn, cfg, session_id, items)
    summary = approval.summary
    asked: ToolResult = {
        "session_id": session_id,
        "items": research.approval_args(approval.items),
        "summary": summary,
        "approved": False,
    }
    to_confirm: ToolResult = {
        **asked,
        "needs_confirmation": True,
        "confirm": approval.token,
        "command": approval.confirm_command,
    }
    if _can_elicit(ctx):
        if confirm is not None:
            return {**asked, "error": ELICIT_INSTEAD, "hint": ELICIT_INSTEAD_HINT}
        return await _elicit_approval(ctx, state, cfg, rdb, session_id, approval, asked, to_confirm)
    if confirm is None:
        return {**to_confirm, "hint": CONFIRM_HINT}
    refused = consent.problem(confirm, approval.token)
    if refused:
        log.info("research session %d: a confirmation token was refused", session_id)
        return {**to_confirm, "error": refused, "hint": CONFIRM_HINT}
    granted = research.grant(
        rdb, state.conn, cfg, session_id, approval.items, via="confirm", summary=summary
    )
    return _approved(rdb, session_id, granted, asked)


async def _elicit_approval(
    ctx: Context,  # type: ignore[type-arg]
    state: "AppState",
    cfg: Config,
    rdb: sqlite3.Connection,
    session_id: int,
    approval: research.Approval,
    asked: ToolResult,
    to_confirm: ToolResult,
) -> ToolResult:
    """Put ``approval`` to the user in a dialog and grant (``via="elicitation"``) only on an
    accepted answer whose ``approve`` is ``true``. A dialog that cannot be shown grants nothing
    and hands back the token with the shell command that confirms it."""
    try:
        answer = await ctx.elicit(message=approval.summary, schema=Confirm)
    except Exception as exc:  # any failure to ask is a refusal, never a grant
        log.warning("research_approve: the confirmation could not be asked: %s", exc)
        return {
            **to_confirm,
            "error": f"the confirmation could not be asked: {exc}",
            "hint": DIALOG_FAILED_HINT,
        }
    approved = isinstance(answer, AcceptedElicitation) and answer.data.approve is True
    if not approved:
        log.info("research session %d: the user did not approve (%s)", session_id, answer.action)
        return {**asked, "answer": answer.action, "hint": DECLINED_HINT}
    granted = research.grant(
        rdb,
        state.conn,
        cfg,
        session_id,
        approval.items,
        via="elicitation",
        summary=approval.summary,
    )
    return {**_approved(rdb, session_id, granted, asked), "answer": answer.action}


def _approved(
    rdb: sqlite3.Connection, session_id: int, granted: Sequence[Grant], asked: ToolResult
) -> ToolResult:
    """What ``research_approve`` answers once ``granted`` is recorded."""
    return {
        **asked,
        "approved": True,
        "grants": research.grant_documents(
            granted, {c.id: c for c in research_db.list_candidates(rdb, session_id)}
        ),
        "hint": RUN_NEXT_HINT,
    }


@guarded
def research_skip(session_id: int, candidate_ids: list[int]) -> ToolResult:
    """Set candidates of a research session aside (offline); approvals they hold are voided.
    Needs no confirmation: it only narrows what the session does, and a later approval can take
    a skipped candidate back. Returns the `skipped` ids."""
    state = _app()
    cfg = state.config()
    rdb = state.research_store(cfg)
    skipped = research.skip(rdb, cfg, session_id, candidate_ids)
    return {"session_id": session_id, "skipped": skipped}


@guarded
def research_exclude(
    targets: list[str], session_id: int | None = None, reason: str | None = None
) -> ToolResult:
    """Never propose these chats again, in any research session (offline); their approvals are
    voided. `targets` are candidate ids (with `session_id`), `@usernames`, t.me links or marked
    chat ids; `reason` is kept and listed by `research_status`. Needs no confirmation: it only
    narrows. Returns `excluded`: each identity with how many candidates it set aside."""
    state = _app()
    cfg = state.config()
    rdb = state.research_store(cfg)
    excluded = research.exclude(rdb, cfg, targets, session_id=session_id, reason=reason)
    return {
        "excluded": [
            {"identity": identity, "candidates_set_aside": moved}
            for identity, moved in excluded.items()
        ],
        "hint": UNEXCLUDE_HINT,
    }


@guarded_async
async def research_run(session_id: int) -> ToolResult:
    """Carry out what the user approved for a research session — joins, admission requests, new
    sources and their history — within the session's time and message budgets, then look one hop
    further from what it fetched and only propose. Every signed-in account connects (a chat
    another account reaches is fetched through it when the session's account is refused); one
    that cannot is in `accounts_skipped`. Returns the run report: `admitted`, `joined`,
    `pending_admission`, `sources_added`, `fetched`, `partial`, `unavailable`, `failed`
    (candidate ids; an admission request no admin answered in time is `failed`), `messages`,
    `stopped_by` (the budget, a flood wait, a busy sync — run it again to go on), `pins` (the
    pinned posts of what it fetched, read for leads), `discovery` and `warnings`. Then analyse
    with `search`, `thread` and `context`.
    """
    state = _app()
    cfg = state.config()
    rdb = state.research_store(cfg)
    session = research.active_session(rdb, session_id)
    embedder = await asyncio.to_thread(state.embedder)
    async with state.sync_lock, state.telegrams() as accounts:
        report = await research.run(
            rdb, state.conn, cfg, state.paths, accounts.clients, session.id, embedder=embedder
        )
    document = research.report_document(report)
    return {
        **document,
        "warnings": [*skipped_warnings(accounts), *document["warnings"]],
        "accounts_skipped": skipped_list(accounts),
    }


@guarded
def research_status(session_id: int | None = None) -> ToolResult:
    """Every research session in brief (`sessions`: id, question, account, state, candidates,
    runs) and every exclusion (`exclusions`: `identity`, the `reason` given, `created_at`), or
    one in full (offline): the `session` with its limits, progress and `horizon`,
    `candidates` counted by status, `pending_grants` (approved, not carried out yet) and
    `pending_admission` (admission requests a chat's admins have not answered)."""
    state = _app()
    cfg = state.config()
    rdb = state.research_store(cfg)
    return research.status_document(rdb, cfg, session_id)


@guarded
def research_stop(session_id: int) -> ToolResult:
    """Stop a research session (offline): it explores no further and approvals it has not used
    are voided. Every source its runs added stays configured and searched; `sources_remove`
    drops one. Returns `grants_voided`."""
    state = _app()
    cfg = state.config()
    rdb = state.research_store(cfg)
    voided = research.stop(rdb, cfg, session_id)
    return {"session_id": session_id, "stopped": True, "grants_voided": voided, "hint": STOP_HINT}


TOOLS: tuple[Callable[..., Any], ...] = (
    search,
    thread,
    context,
    sync,
    sources,
    dialogs,
    sources_add,
    sources_remove,
    accounts,
    research_start,
    research_discover,
    research_candidates,
    research_approve,
    research_skip,
    research_exclude,
    research_run,
    research_status,
    research_stop,
)


# --- server ----------------------------------------------------------------------------------


def build_server() -> FastMCP[Any]:
    """A ``grepogram`` FastMCP server with the eighteen tools; docstrings are the descriptions."""
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
        except (ConfigError, db.SchemaError, db.ExtensionsUnsupported) as exc:
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
