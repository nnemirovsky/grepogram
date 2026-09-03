"""Command-line interface: ``grepogram <command>``.

Commands print plain text to stdout and diagnostics to stderr; logging goes to stderr and the log
file. ``config`` and ``sources`` are sub-apps; later tasks add ``dialogs``, ``sync``, ``search``
and ``embed`` and fill in the ``sources`` commands.
"""

import asyncio
import logging
import sqlite3
from typing import Annotated, NoReturn

import typer
from telethon import errors as tg_errors

from grepogram import __version__, config, db, tg
from grepogram.config import TEMPLATE, ConfigError
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
    try:
        cfg = config.load(paths)
    except ConfigError as exc:
        fail(str(exc))
    if cfg.telegram.api_id == 0 or not cfg.telegram.api_hash:
        fail(
            f"[telegram] api_id and api_hash are not set in {paths.config_file}: create an "
            "application at https://my.telegram.org/apps and fill them in "
            "(run `grepogram config init` first if the file does not exist)"
        )
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


def _open_db(paths: Paths) -> sqlite3.Connection:
    """Open the index database and bring its schema up to date."""
    conn = db.connect(paths)
    db.migrate(conn)
    return conn


def _load() -> tuple[Paths, Config, sqlite3.Connection]:
    """Resolve paths, read the config and open the database for a command."""
    paths = Paths.from_env()
    try:
        cfg = config.load(paths)
        conn = _open_db(paths)
    except (ConfigError, db.SchemaError) as exc:
        fail(str(exc))
    return paths, cfg, conn
