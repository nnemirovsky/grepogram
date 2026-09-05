"""Command-line interface: ``grepogram <command>``.

Commands print plain text to stdout and diagnostics to stderr; logging goes to stderr and the log
file. ``config`` and ``sources`` are sub-apps; ``search --json`` prints the
:class:`~grepogram.models.SearchResult` and nothing else on stdout. ``search`` fuses lexical and
dense retrieval by default (``--mode``) and reranks unless ``--no-rerank``; when the dense index
or a model is unavailable it falls back to lexical and prints a warning on stderr. ``sync`` embeds
new units when the embedding model loads and only warns when it does not; ``embed`` insists on
the model.

``thread`` and ``context`` are the readers a hit leads to, the CLI half of the MCP tools of the
same names: they take the chat specs ``search -c`` takes (through
:func:`grepogram.filters.resolve_chat`, which insists on one chat) and print the messages around
one, or with ``--json`` the same document those tools return.
"""

import asyncio
import datetime as dt
import functools
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from enum import StrEnum
from typing import Annotated, NoReturn

import typer
from telethon import TelegramClient
from telethon import errors as tg_errors

from grepogram import (
    __version__,
    config,
    db,
    dialogs,
    embed,
    filters,
    index,
    search,
    sources,
    sync,
    tg,
)
from grepogram.config import TEMPLATE, ConfigError
from grepogram.dialogs import Match
from grepogram.embed import Embedder, ModelUnavailable
from grepogram.filters import FilterError
from grepogram.log import setup_logging
from grepogram.models import Config, MessageView, SearchResult, SyncReport
from grepogram.paths import Paths
from grepogram.search import UnknownMessage

HELP = "Local hybrid search over opt-in Telegram chats, exposed to Claude Code through MCP."
_CHAT_HELP = (
    "The chat the message is in, naming exactly one indexed chat: id, @username, t.me link, "
    "folder:<name> or a title (put -- before a negative id)."
)

app = typer.Typer(name="grepogram", help=HELP, no_args_is_help=True, add_completion=False)
config_app = typer.Typer(help="Show or create the config file.", no_args_is_help=True)
sources_app = typer.Typer(help="Manage indexed sources (folders and chats).", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(sources_app, name="sources")


class Mode(StrEnum):
    """:data:`~grepogram.models.SearchMode` as the enum Typer needs to render ``--mode``.

    The members mirror it one for one; ``tests/test_cli.py`` holds them to that.
    """

    lexical = "lexical"
    hybrid = "hybrid"
    dense = "dense"


def _version(value: bool) -> None:
    if value:
        typer.echo(f"grepogram {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Log at DEBUG level instead of INFO.")
    ] = False,
) -> None:
    setup_logging(Paths.from_env(), logging.DEBUG if verbose else logging.INFO)


@app.command()
def auth() -> None:
    """Sign in to Telegram (phone, login code, optional 2FA password) and store the session."""
    paths = Paths.from_env()
    cfg = _load_config(paths)
    _require_api_keys(cfg, paths)
    tg.prepare_session(paths)
    try:
        client = tg.make_login_client(cfg, paths)
        name = asyncio.run(
            tg.login(client, phone=_ask_phone, code=_ask_code, password=_ask_password)
        )
    except tg.SessionError as exc:
        fail(f"{exc}; if the file is damaged, delete it and run grepogram auth again")
    except (
        tg.AuthRequired,
        tg_errors.RPCError,
        ConnectionError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        fail(f"sign-in failed: {exc}")
    tg.ensure_session_mode(paths)
    typer.echo(f"signed in as {name}")
    typer.echo(f"session stored at {paths.session_file}")


def _ask_phone() -> str:
    return str(typer.prompt("Phone number in international format (e.g. +5491112345678)"))


def _ask_code() -> str:
    return str(typer.prompt("Login code sent by Telegram"))


def _ask_password() -> str:
    return str(typer.prompt("Two-step verification password", hide_input=True))


@app.command("dialogs")
def dialogs_cmd(
    query: Annotated[str, typer.Argument(help="Chat title, @username or folder name (fuzzy).")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum number of matches.")] = 10,
) -> None:
    """Find chats and folders whose name matches QUERY; needs a signed-in session."""
    paths = Paths.from_env()
    cfg = _load_config(paths)
    _require_api_keys(cfg, paths)
    try:
        tg.ensure_session_mode(paths)
        client = tg.make_client(cfg, paths)
        matches = asyncio.run(_match_dialogs(client, query, limit))
    except (tg.AuthRequired, tg.SessionError) as exc:
        fail(str(exc))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    if not matches:
        typer.echo(f"no dialogs or folders match {query!r}")
        return
    rows = [
        (
            m.kind,
            str(m.id),
            m.dialog.type if m.dialog else "-",
            m.title,
            f"@{m.dialog.username}" if m.dialog and m.dialog.username else "-",
            ", ".join(m.dialog.folders) if m.dialog and m.dialog.folders else "-",
            f"{m.score:.2f}",
        )
        for m in matches
    ]
    _print_table(("kind", "id", "type", "title", "username", "folders", "score"), rows)


async def _match_dialogs(client: TelegramClient, query: str, limit: int) -> list[Match]:
    async with tg.connected(client):
        catalog = dialogs.DialogCatalog(client)
        found = await catalog.list_dialogs()
        folders = await catalog.list_folders()
    return dialogs.match(query, found, folders, limit=limit)


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]
    for line in (headers, *rows):
        cells = [cell.ljust(w) for cell, w in zip(line, widths, strict=True)]
        typer.echo("  ".join(cells).rstrip())


@app.command("sync")
def sync_cmd(
    budget: Annotated[
        int | None,
        typer.Option(
            "--budget",
            min=1,
            help="Stop after this many seconds; unfinished chats resume on the next run.",
        ),
    ] = None,
) -> None:
    """Fetch new messages from every configured source and embed them; needs a session."""
    paths, cfg, conn = _load()
    _require_api_keys(cfg, paths)
    if not cfg.sources:
        conn.close()
        fail("no sources configured; add one with: grepogram sources add <target>")
    try:
        tg.ensure_session_mode(paths)
        client = tg.make_client(cfg, paths)
        embedder = _optional_embedder(cfg)
        current = functools.partial(config.load, paths)
        report = asyncio.run(_run_sync(client, conn, current, paths, budget, embedder))
    except (tg.AuthRequired, tg.SessionError, sync.SyncInProgress, ConfigError) as exc:
        fail(str(exc))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    finally:
        conn.close()
    _print_report(report)


def _optional_embedder(cfg: Config) -> Embedder | None:
    """The configured embedder, or ``None`` with a warning when the model cannot load."""
    try:
        return embed.load_embedder(cfg)
    except ModelUnavailable as exc:
        typer.echo(f"warning: dense index not updated: {exc}", err=True)
        return None


async def _run_sync(
    client: TelegramClient,
    conn: sqlite3.Connection,
    cfg: sync.ConfigSource,
    paths: Paths,
    budget: int | None,
    embedder: Embedder | None,
) -> SyncReport:
    """Connect and run :func:`grepogram.sync.sync_all`; ``cfg`` is the config loader so the
    sources come from the file as it is once the sync lock is held, not from the snapshot the
    command started with (a ``sources rm`` may have run while the model loaded)."""
    async with tg.connected(client):
        return await sync.sync_all(client, conn, cfg, paths, sync.SyncBudget(budget), embedder)


def _print_report(report: SyncReport) -> None:
    typer.echo(f"new messages: {report.new}")
    typer.echo(f"chats synced: {len(report.chats_done)}")
    if report.chats_remaining:
        ids = ", ".join(str(chat_id) for chat_id in report.chats_remaining)
        typer.echo(f"chats remaining: {len(report.chats_remaining)} ({ids}); run sync again")
    if report.unavailable:
        ids = ", ".join(str(chat_id) for chat_id in report.unavailable)
        typer.echo(f"chats unavailable: {len(report.unavailable)} ({ids})")
    for warning in report.warnings:
        typer.echo(f"warning: {warning}", err=True)


@app.command("embed")
def embed_cmd(
    reembed: Annotated[
        bool,
        typer.Option(
            "--reembed",
            help="Discard every stored vector and embed all units again with the configured "
            "model (required after changing \\[models] embed).",
        ),
    ] = False,
) -> None:
    """Embed the units the dense index does not hold yet (offline; needs the embedding model)."""
    paths, cfg, conn = _load()
    try:
        try:
            embedder = embed.load_embedder(cfg)
        except ModelUnavailable as exc:
            fail(f"cannot load the embedding model: {exc}")
        try:
            with sync.SyncLock(paths):
                index.ensure_embedding_space(conn, embedder, reembed=reembed)
                count = index.embed_dirty_units(conn, embedder)
        except (sync.SyncInProgress, index.EmbeddingSpaceMismatch) as exc:
            fail(str(exc))
    finally:
        conn.close()
    space = f"({embedder.name}, {embedder.dim}-d)"
    if count:
        typer.echo(f"embedded {count} units {space}")
    else:
        typer.echo(f"dense index is up to date {space}")


@app.command("search")
def search_cmd(
    query: Annotated[
        str, typer.Argument(help="What to look for; Russian and English words are stemmed.")
    ],
    chat: Annotated[
        list[str] | None,
        typer.Option(
            "--chat",
            "-c",
            help="Search only these chats: id, @username, folder:<name> or a title (repeatable).",
        ),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help=f"Skip units starting before this: {filters.WHEN_GRAMMAR}."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Skip units starting after this (same forms, inclusive)."),
    ] = None,
    mode: Annotated[
        Mode,
        typer.Option(
            "--mode",
            help="hybrid = lexical and dense retrieval fused; lexical = BM25 over stems only; "
            "dense = embeddings only. Without vectors or the model, every mode falls back "
            "to lexical.",
        ),
    ] = Mode.hybrid,
    k: Annotated[
        int | None,
        typer.Option("-k", "--limit", min=1, help="Number of hits (default: \\[search] k)."),
    ] = None,
    rerank: Annotated[
        bool,
        typer.Option(
            "--rerank/--no-rerank",
            help="Re-score the top candidates with the cross-encoder (skipped when it cannot "
            "load).",
        ),
    ] = True,
    full: Annotated[
        bool, typer.Option("--full", help="Include each hit's full unit text.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON and nothing else.")
    ] = False,
) -> None:
    """Search the indexed chats (offline); every hit carries a link that opens the message."""
    _, cfg, conn = _load()
    try:
        selected = filters.resolve_filters(conn, cfg, chat, since, until)
        result = search.search(
            conn, cfg, query, selected, k, mode=mode.value, full=full, rerank=rerank
        )
    except FilterError as exc:
        fail(str(exc))
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return
    _print_hits(result, cfg)


def _print_hits(result: SearchResult, cfg: Config) -> None:
    for warning in result.warnings:
        typer.echo(f"warning: {warning}", err=True)
    age = result.index_age_min
    if age is not None and age > cfg.search.auto_sync_after_min:
        typer.echo(f"note: the index is {age} min old; run: grepogram sync", err=True)
    if not result.hits:
        typer.echo("no hits")
        return
    for n, hit in enumerate(result.hits, start=1):
        if n > 1:
            typer.echo("")
        title = hit.chat.title or f"chat {hit.chat.id}"
        typer.echo(
            f"{n}. {hit.score:.4f}  {hit.kind}  {title}  {_span(hit.date_start, hit.date_end)}"
        )
        typer.echo(f"   {hit.url}")
        if hit.fallback_url:
            typer.echo(f"   fallback: {hit.fallback_url}")
        body = hit.snippet if hit.text is None else hit.text
        for line in body.splitlines():
            typer.echo(f"   {line}")


def _span(start: int, end: int) -> str:
    first = dt.datetime.fromtimestamp(start, dt.UTC)
    last = dt.datetime.fromtimestamp(end, dt.UTC)
    if first.date() == last.date():
        return f"{first:%Y-%m-%d %H:%M}–{last:%H:%M} UTC"
    return f"{first:%Y-%m-%d %H:%M} – {last:%Y-%m-%d %H:%M} UTC"


@app.command("thread")
def thread_cmd(
    chat: Annotated[str, typer.Argument(help=_CHAT_HELP)],
    msg_id: Annotated[int, typer.Argument(help="Message id, as a hit's link and JSON carry it.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON and nothing else.")
    ] = False,
) -> None:
    """Print the whole reply thread a message belongs to, root first (offline); for a channel
    post, the post followed by its comments from the linked discussion group."""
    _, cfg, conn = _load()
    try:
        chat_id = filters.resolve_chat(conn, cfg, chat)
        views = search.thread(conn, chat_id, msg_id)
    except (FilterError, UnknownMessage) as exc:
        fail(str(exc))
    finally:
        conn.close()
    _print_messages(chat_id, msg_id, views, as_json=as_json)


@app.command("context")
def context_cmd(
    chat: Annotated[str, typer.Argument(help=_CHAT_HELP)],
    msg_id: Annotated[int, typer.Argument(help="Message id, as a hit's link and JSON carry it.")],
    before: Annotated[
        int, typer.Option("--before", min=0, help="How many earlier messages to print.")
    ] = 15,
    after: Annotated[
        int, typer.Option("--after", min=0, help="How many later messages to print.")
    ] = 15,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON and nothing else.")
    ] = False,
) -> None:
    """Print the messages around one in its chat or forum topic, the message itself included
    (offline)."""
    _, cfg, conn = _load()
    try:
        chat_id = filters.resolve_chat(conn, cfg, chat)
        views = search.context(conn, chat_id, msg_id, before, after)
    except (FilterError, UnknownMessage) as exc:
        fail(str(exc))
    finally:
        conn.close()
    _print_messages(chat_id, msg_id, views, as_json=as_json)


def _print_messages(
    chat_id: int, msg_id: int, views: Sequence[MessageView], *, as_json: bool
) -> None:
    """What ``thread`` and ``context`` print: one block per message, or the JSON document the
    MCP tools of the same names answer with.

    ``chat_id`` and ``msg_id`` are the message that was asked about; each block leads with the
    chat and id of the message it shows, because a channel post's thread carries the comments of
    its discussion group and those ids are only meaningful together with that group's own.
    """
    if as_json:
        document = {
            "chat_id": chat_id,
            "msg_id": msg_id,
            "messages": [asdict(view) for view in views],
        }
        typer.echo(json.dumps(document, ensure_ascii=False, indent=2))
        return
    for n, view in enumerate(views, start=1):
        if n > 1:
            typer.echo("")
        who = view.from_name or "-"
        typer.echo(f"{n}. {view.chat_id}/{view.msg_id}  {_moment(view.date)}  {who}")
        typer.echo(f"   {view.url}")
        if view.fallback_url:
            typer.echo(f"   fallback: {view.fallback_url}")
        for line in view.text.splitlines():
            typer.echo(f"   {line}")


def _moment(timestamp: int) -> str:
    return f"{dt.datetime.fromtimestamp(timestamp, dt.UTC):%Y-%m-%d %H:%M} UTC"


@sources_app.command("add")
def sources_add(
    target: Annotated[
        str,
        typer.Argument(
            help="Chat id (as printed by `grepogram dialogs`), @username, t.me link, "
            "folder:<name>, or a chat / folder title (fuzzy)."
        ),
    ],
    since: Annotated[
        str | None,
        typer.Option("--since", help="Skip history before this date (YYYY-MM-DD) on first sync."),
    ] = None,
    comments: Annotated[
        bool, typer.Option("--comments", help="Channels only: also index the discussion threads.")
    ] = False,
) -> None:
    """Add a folder or chat to the indexed sources and save the config; needs a session."""
    paths = Paths.from_env()
    cfg = _load_config(paths)
    _require_api_keys(cfg, paths)
    try:
        parsed = sources.parse_target(target)
        tg.ensure_session_mode(paths)
        client = tg.make_client(cfg, paths)
        added = asyncio.run(_add_source(client, cfg, parsed, since, comments))
        # the target was resolved over the network; the source is applied to the file as it is
        # by now, under the config lock, not to the snapshot read before the round trip — the
        # MCP server may have saved a change (a removed source) in the meantime
        dialog = None if added.folder is not None else added.dialogs[0]
        config.update(paths, lambda current: sources.with_source(current, added.source, dialog))
    except (sources.SourceError, tg.AuthRequired, tg.SessionError, ConfigError) as exc:
        fail(str(exc))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    if added.folder is not None:
        what = f"folder {added.title!r} with {len(added.dialogs)} chats"
    else:
        chat = added.dialogs[0]
        what = f"{chat.type} {chat.title!r} (id {chat.id})"
    typer.echo(f"added {added.source.id}: {what}")
    typer.echo("next: grepogram sync")


async def _add_source(
    client: TelegramClient, cfg: Config, target: sources.Target, since: str | None, comments: bool
) -> sources.Added:
    async with tg.connected(client):
        catalog = dialogs.DialogCatalog(client)
        return await sources.add_source(cfg, target, catalog, since=since, comments=comments)


def _when(timestamp: int | None) -> str:
    if timestamp is None:
        return "never"
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).astimezone().strftime("%Y-%m-%d %H:%M")


@sources_app.command("ls")
def sources_ls() -> None:
    """List configured sources with their indexed chats and sync state (offline)."""
    _, cfg, conn = _load()
    try:
        statuses = sources.sources_status(cfg, conn)
    finally:
        conn.close()
    if not statuses:
        typer.echo("no sources configured; add one with: grepogram sources add <target>")
        return
    rows: list[tuple[str, ...]] = []
    for status in statuses:
        if not status.chats:
            rows.append((status.source_id, "-", "-", "-", "-", "0", "never", "not synced yet"))
        for chat in status.chats:
            rows.append(
                (
                    status.source_id,
                    str(chat.id),
                    chat.type,
                    chat.title or "-",
                    f"@{chat.username}" if chat.username else "-",
                    str(chat.message_count),
                    _when(chat.last_sync_at),
                    "unavailable" if chat.unavailable else "ok",
                )
            )
    _print_table(
        ("source", "id", "type", "title", "username", "messages", "last sync", "status"), rows
    )


@sources_app.command("rm")
def sources_rm(
    target: Annotated[
        str,
        typer.Argument(
            help="Source id from `sources ls`, folder name, chat id, @username or a fuzzy title."
        ),
    ],
) -> None:
    """Remove a source and delete its chats' messages and index data (offline; refuses while a
    sync is running, and refuses a chat indexed through a folder or as a channel's discussion
    group)."""
    paths, _, conn = _load()
    try:
        # the config is read and saved under the same lock as the delete, so a sync that starts
        # as soon as the lock is free reads a config without this source and cannot re-create
        # its chats; the config lock keeps an MCP `sources_add` saving in between from being
        # overwritten by a snapshot that predates it
        with sync.SyncLock(paths), config.ConfigLock(paths):
            current = config.load(paths)
            removed = sources.remove_source(current, conn, sources.parse_target(target))
            if removed.source is not None:
                config.save(removed.config, paths)
    except (sources.SourceError, sync.SyncInProgress, ConfigError) as exc:
        fail(str(exc))
    finally:
        conn.close()
    typer.echo(f"removed {removed.source_id} ({len(removed.chat_ids)} chats deleted)")


@config_app.command("path")
def config_path() -> None:
    """Print the resolved file locations (GREPOGRAM_HOME overrides them)."""
    paths = Paths.from_env()
    rows = (
        ("config", paths.config_file),
        ("session", paths.session_file),
        ("index", paths.db_file),
        ("lock", paths.lock_file),
        ("log", paths.log_file),
    )
    for label, path in rows:
        typer.echo(f"{label:<8} {path}")


@config_app.command("init")
def config_init() -> None:
    """Write the annotated config template; refuses to overwrite an existing file."""
    paths = Paths.from_env()
    if paths.config_file.exists():
        fail(f"{paths.config_file} already exists; edit it in place or delete it first")
    paths.ensure_dirs()
    config.write_private(paths.config_file, TEMPLATE)
    typer.echo(f"wrote {paths.config_file}")
    typer.echo(
        "next: set [telegram] api_id and api_hash from https://my.telegram.org/apps, "
        "then run: grepogram auth"
    )


def fail(message: str, code: int = 1) -> NoReturn:
    """Print ``error: <message>`` to stderr and exit with ``code``."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _load_config(paths: Paths) -> Config:
    """Read the config file (defaults when absent), exiting on a broken one."""
    try:
        return config.load(paths)
    except ConfigError as exc:
        fail(str(exc))


def _require_api_keys(cfg: Config, paths: Paths) -> None:
    """Exit with setup instructions unless ``[telegram]`` api_id and api_hash are filled in."""
    if cfg.telegram.api_id == 0 or not cfg.telegram.api_hash:
        fail(
            f"[telegram] api_id and api_hash are not set in {paths.config_file}: create an "
            "application at https://my.telegram.org/apps and fill them in "
            "(run `grepogram config init` first if the file does not exist)"
        )


def _open_db(paths: Paths) -> sqlite3.Connection:
    """Open the index database and bring its schema up to date."""
    conn = db.connect(paths)
    db.migrate(conn)
    return conn


def _load() -> tuple[Paths, Config, sqlite3.Connection]:
    """Resolve paths, read the config and open the database for a command."""
    paths = Paths.from_env()
    cfg = _load_config(paths)
    try:
        conn = _open_db(paths)
    except (db.SchemaError, db.ExtensionsUnsupported) as exc:
        fail(str(exc))
    return paths, cfg, conn
