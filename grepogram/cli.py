"""Command-line interface: ``grepogram <command>``.

Commands print plain text to stdout and diagnostics to stderr; logging goes to stderr and the log
file. ``config`` and ``sources`` are sub-apps; later tasks add ``sync``, ``search`` and ``embed``.
"""

import asyncio
import datetime as dt
import logging
import sqlite3
from collections.abc import Sequence
from typing import Annotated, NoReturn

import typer
from telethon import TelegramClient
from telethon import errors as tg_errors

from grepogram import __version__, config, db, dialogs, sources, tg
from grepogram.config import TEMPLATE, ConfigError
from grepogram.dialogs import Match
from grepogram.log import setup_logging
from grepogram.models import Config
from grepogram.paths import Paths

HELP = "Local hybrid search over opt-in Telegram chats, exposed to Claude Code through MCP."

app = typer.Typer(name="grepogram", help=HELP, no_args_is_help=True, add_completion=False)
config_app = typer.Typer(help="Show or create the config file.", no_args_is_help=True)
sources_app = typer.Typer(help="Manage indexed sources (folders and chats).", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(sources_app, name="sources")


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
    client = tg.make_client(cfg, paths)
    try:
        name = asyncio.run(
            tg.login(client, phone=_ask_phone, code=_ask_code, password=_ask_password)
        )
    except (tg.AuthRequired, tg_errors.RPCError, ConnectionError, RuntimeError) as exc:
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
    except tg.AuthRequired as exc:
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
    except (sources.SourceError, tg.AuthRequired) as exc:
        fail(str(exc))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    config.save(added.config, paths)
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
    """Remove a source and delete its chats' messages and index data (offline)."""
    paths, cfg, conn = _load()
    try:
        removed = sources.remove_source(cfg, conn, sources.parse_target(target))
    except sources.SourceError as exc:
        fail(str(exc))
    finally:
        conn.close()
    if removed.source is not None:
        config.save(removed.config, paths)
    typer.echo(f"removed {removed.source_id} ({len(removed.chat_ids)} chats deleted)")


def _when(timestamp: int | None) -> str:
    if timestamp is None:
        return "never"
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).astimezone().strftime("%Y-%m-%d %H:%M")


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
    except db.SchemaError as exc:
        fail(str(exc))
    return paths, cfg, conn
