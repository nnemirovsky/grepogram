"""Command-line interface: ``grepogram <command>``.

Commands print plain text to stdout and diagnostics to stderr; logging goes to stderr and the log
file. ``config``, ``sources``, ``accounts`` and ``research`` are sub-apps; ``search --json``
prints the :class:`~grepogram.models.SearchResult` and nothing else on stdout. ``search`` fuses
lexical and dense retrieval by default (``--mode``) and reranks unless ``--no-rerank``; when the
dense index or a model is unavailable it falls back to lexical and prints a warning on stderr.
``sync`` embeds new units when the embedding model loads and only warns when it does not;
``embed`` insists on the model.

Every command that talks to Telegram does it as an account: ``auth``, ``dialogs``, ``sources
add``, ``leave`` and ``research start`` take ``--account`` (``default`` when omitted, the account
a config without ``[[accounts]]`` has always had), while ``sync``, ``extract``,
``prune-deleted``, ``recapture-links`` and ``sources prune`` use every signed-in account at
once; the offline ``import --account`` names the account an export was made from. ``accounts
rm`` and ``leave`` change things a config edit cannot undo, so they ask on the controlling terminal
(:func:`_terminal`) and refuse without one; the human confirms by typing back a random code the
question shows (:func:`_ask`), and no option answers for them.
``research`` drives :mod:`grepogram.research` over ``research.db`` and refuses every command while
``[research] enabled`` is false. ``research approve`` is the CLI's consent channel: it writes the
exact :func:`~grepogram.research.approval_summary` to the controlling terminal and reads the answer
there, so the text the grant records is the text the human read; with no terminal it refuses, and
no option approves in its place. What that guards against is an MCP-only agent, a pipe and a
blind ``yes``: an agent that can run shell commands can give the command a terminal of its own
and read the code off it, so the CLI's confirmation relies on the human being the one who runs
it. Its ``--json`` readers print the documents the MCP research tools answer with
(``research.*_document``).
One search spans every account's chats: ``search --account`` scopes it to what an account reaches
(a scope, not isolation) and every hit and message names the accounts its chat came through.

``thread`` and ``context`` are the readers a hit leads to, the CLI half of the MCP tools of the
same names: they take the chat specs ``search -c`` takes (through
:func:`grepogram.filters.resolve_chat`, which insists on one chat) and print the messages around
one, or with ``--json`` the same document those tools return.
"""

import asyncio
import contextlib
import dataclasses
import datetime as dt
import functools
import io
import json
import logging
import secrets
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn, TextIO

import typer
from telethon import TelegramClient, functions, types, utils
from telethon import errors as tg_errors

from grepogram import (
    __version__,
    config,
    db,
    dialogs,
    embed,
    filters,
    index,
    media,
    research,
    research_db,
    search,
    sources,
    sync,
    tdesktop,
    tg,
)
from grepogram.config import TEMPLATE, ConfigError
from grepogram.dialogs import DialogInfo, FolderInfo, Match
from grepogram.embed import Embedder, ModelUnavailable
from grepogram.filters import FilterError
from grepogram.log import setup_logging
from grepogram.models import (
    DEFAULT_ACCOUNT,
    AccountCfg,
    AccountRow,
    ChatRow,
    Config,
    DiscoverReport,
    MediaReport,
    MessageView,
    PruneReport,
    RecaptureReport,
    ResearchSession,
    RunReport,
    SearchResult,
    SyncReport,
)
from grepogram.paths import Paths
from grepogram.search import UnknownMessage

HELP = "Local hybrid search over opt-in Telegram chats, exposed to Claude Code through MCP."
_CHAT_HELP = (
    "The chat the message is in, naming exactly one indexed chat: id, <account>/<id>, "
    "@username, t.me link, folder:<name>, import:<slug> or a title (put -- before a negative id)."
)

app = typer.Typer(name="grepogram", help=HELP, no_args_is_help=True, add_completion=False)
config_app = typer.Typer(help="Show or create the config file.", no_args_is_help=True)
sources_app = typer.Typer(help="Manage indexed sources (folders and chats).", no_args_is_help=True)
accounts_app = typer.Typer(help="List or remove signed-in Telegram accounts.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(sources_app, name="sources")
research_app = typer.Typer(
    help="Explore beyond the indexed chats: discover, approve and fetch new ones (opt-in).",
    no_args_is_help=True,
)
app.add_typer(accounts_app, name="accounts")
app.add_typer(research_app, name="research")

TERMINAL = "/dev/tty"
"""Where :func:`_terminal` asks a confirmation: the controlling terminal, never stdin, so
nothing piped into the command answers for the human."""
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
"""What a confirmation code is drawn from: lowercase letters and digits a human cannot misread
for one another (no ``i``, ``l``, ``o``, ``0``, ``1``)."""
CODE_LENGTH = 5

_ACCOUNT_HELP = "The account to act as, as `grepogram accounts ls` lists it (default: default)."
AccountOption = Annotated[str | None, typer.Option("--account", "-a", help=_ACCOUNT_HELP)]


class NoTerminal(Exception):
    """A command that must ask a human first has no terminal to ask on."""

    def __init__(self, command: str) -> None:
        self.hint = f"the user must run `{command}` in their own terminal themselves"
        super().__init__(f"{command} asks for a confirmation on a terminal, and there is none")


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
def auth(
    account: Annotated[
        str,
        typer.Option(
            "--account",
            "-a",
            help="Sign in this account: a new name (lowercase letters, digits, '_' or '-') is "
            "added to \\[\\[accounts]] once the sign-in succeeds.",
        ),
    ] = DEFAULT_ACCOUNT,
    label: Annotated[
        str | None,
        typer.Option("--label", help="A note for `accounts ls`, such as 'work phone'."),
    ] = None,
) -> None:
    """Sign in to Telegram (phone, login code, optional 2FA password) and store the session.

    Every account keeps its own session file; several can be signed in at once, and `sync`
    fetches every account's sources. An account name is one Telegram user: signing in under it
    as another user than the one recorded is refused and leaves its session as it was.
    """
    paths = Paths.from_env()
    cfg = _load_config(paths)
    _require_api_keys(cfg, paths)
    if account != DEFAULT_ACCOUNT:
        try:
            config.check_account_name(account)
        except ConfigError as exc:
            fail(str(exc), hint=exc.hint)
    elif label is not None:
        fail("the default account has no [[accounts]] entry to label; label a named account")
    again = "grepogram " + " ".join(_auth_args(account))
    # the sign-in writes a staged copy of the session: the account's own file is replaced only
    # once the user it signed in as is known to be the one the index recorded under this name
    staged = tg.stage_login(paths, account)
    try:
        try:
            client = tg.make_login_client(cfg, paths, account, path=staged)
            who = asyncio.run(
                tg.login(client, phone=_ask_phone, code=_ask_code, password=_ask_password)
            )
        except tg.SessionError as exc:
            fail(f"{exc}; if the file is damaged, delete it and run {again} again")
        except (
            tg.AuthRequired,
            tg_errors.RPCError,
            ConnectionError,
            RuntimeError,
            sqlite3.Error,
        ) as exc:
            fail(f"sign-in failed: {exc}")
        refusal = _another_user(paths, account, who)
        if refusal is not None:
            # the sign-in is not kept, so the authorization it made on Telegram's side must not
            # outlive the staged file that holds its only key
            if who.fresh and not asyncio.run(tg.log_out(client)):
                typer.echo(
                    "warning: Telegram could not be told to end the refused sign-in; end it in "
                    "Telegram under Settings → Devices",
                    err=True,
                )
            message, hint = refusal
            fail(message, hint=hint)
        tg.commit_login(paths, account, staged)
    finally:
        staged.unlink(missing_ok=True)
    tg.ensure_session_mode(paths, account)
    if account != DEFAULT_ACCOUNT:
        try:
            config.update(paths, lambda current: _with_account(current, account, label))
        except ConfigError as exc:
            fail(str(exc), hint=exc.hint)
    _remember_account(paths, account, who)
    named = "" if account == DEFAULT_ACCOUNT else f" (account {account})"
    typer.echo(f"signed in as {who.name}{named}")
    typer.echo(f"session stored at {paths.session_file_for(account)}")


def _auth_args(account: str) -> list[str]:
    return ["auth"] if account == DEFAULT_ACCOUNT else ["auth", "--account", account]


def _with_account(cfg: Config, name: str, label: str | None) -> Config:
    """``cfg`` with an ``[[accounts]]`` entry for ``name``: appended when it has none, its label
    replaced when ``label`` is given, and unchanged otherwise."""
    entry = AccountCfg(name=name, label=label)
    if all(known.name != name for known in cfg.accounts):
        return dataclasses.replace(cfg, accounts=[*cfg.accounts, entry])
    if label is None:
        return cfg
    accounts = [entry if known.name == name else known for known in cfg.accounts]
    return dataclasses.replace(cfg, accounts=accounts)


def _another_user(paths: Paths, account: str, who: tg.SignedIn) -> tuple[str, str] | None:
    """Why a sign-in under ``account`` as ``who`` must not be kept — ``(message, hint)`` — or
    ``None`` when it may.

    Everything tied to the name — its private chats, the access hashes it stored, its research
    approvals — belongs to the Telegram user the index recorded for it, and another one must not
    inherit it. A name with no user recorded yet (a ``default`` from before accounts existed,
    one never synced) takes whoever signs in. An index that cannot be read cannot say who the
    name is, and that refuses the sign-in too: a session committed unchecked is exactly what the
    passes that act on Telegram would later run as someone else."""
    try:
        conn = _open_db(paths)
        try:
            recorded = db.other_user(conn, account, who.user_id)
        finally:
            conn.close()
    except (db.SchemaError, db.ExtensionsUnsupported, sqlite3.Error) as exc:
        return (
            f"the index could not say who account {account} is ({exc}); the sign-in was not "
            "stored and its session file is as it was",
            f"make the index readable (`grepogram sync` names what is wrong), then run "
            f"{tg.auth_command(account)} again",
        )
    if recorded is None:
        return None
    before = f"{recorded.display_name} " if recorded.display_name else ""
    return (
        f"account {account} is {before}(Telegram user {recorded.user_id}) in this index, but "
        f"this sign-in is {who.name} (user {who.user_id}); nothing was changed and its session "
        "file still holds the earlier sign-in",
        "sign the other user in under a name of its own (`grepogram auth --account <name>`), "
        f"or remove this account first with `grepogram accounts rm {account}`",
    )


def _remember_account(paths: Paths, account: str, who: tg.SignedIn) -> None:
    """Record in the index who ``account`` signed in as. The session is what the sign-in was
    for, so an index that cannot be opened costs a warning and not the sign-in."""
    try:
        conn = _open_db(paths)
        try:
            db.upsert_account(
                conn,
                AccountRow(
                    name=account,
                    user_id=who.user_id,
                    display_name=who.name,
                ),
            )
        finally:
            conn.close()
    except (db.SchemaError, db.ExtensionsUnsupported, sqlite3.Error) as exc:
        typer.echo(f"warning: the index did not record the account: {exc}", err=True)


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
    account: AccountOption = None,
) -> None:
    """Find chats and folders of an account whose name matches QUERY; needs a signed-in
    session."""
    paths = Paths.from_env()
    cfg = _load_config(paths)
    _require_api_keys(cfg, paths)
    name = _known_account(cfg, account or DEFAULT_ACCOUNT)
    try:
        tg.ensure_session_mode(paths, name)
        client = tg.make_client(cfg, paths, name)
        matches = asyncio.run(_match_dialogs(client, query, limit, name))
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


async def _match_dialogs(
    client: TelegramClient, query: str, limit: int, account: str = DEFAULT_ACCOUNT
) -> list[Match]:
    async with tg.connected(client, account):
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
    """Fetch new messages from every configured source of every signed-in account and embed
    them; needs a session."""
    paths, cfg, conn = _load()
    _require_api_keys(cfg, paths)
    if not cfg.sources:
        conn.close()
        fail("no sources configured; add one with: grepogram sources add <target>")
    try:
        accounts = tg.make_clients(cfg, paths)
        embedder = _optional_embedder(cfg)
        current = functools.partial(config.load, paths)
        report = asyncio.run(_run_sync(accounts, conn, current, paths, budget, embedder))
    except (tg.AuthRequired, tg.SessionError, sync.SyncInProgress, ConfigError) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    finally:
        conn.close()
    _print_report(report)


@contextlib.asynccontextmanager
async def _connected(accounts: tg.Accounts) -> AsyncIterator[dict[str, TelegramClient]]:
    """Connect every account of ``accounts`` (:func:`grepogram.tg.connected_all`) and warn about
    each one left out — no session, or one Telegram refuses — while the others carry on."""
    async with tg.connected_all(accounts.clients, accounts.skipped) as live:
        for name, exc in live.skipped.items():
            typer.echo(f"warning: account {name}: {exc}; skipped this run", err=True)
        yield live.clients


def _optional_embedder(cfg: Config) -> Embedder | None:
    """The configured embedder, or ``None`` with a warning when the model cannot load."""
    try:
        return embed.load_embedder(cfg)
    except ModelUnavailable as exc:
        typer.echo(f"warning: dense index not updated: {exc}", err=True)
        return None


async def _run_sync(
    accounts: tg.Accounts,
    conn: sqlite3.Connection,
    cfg: sync.ConfigSource,
    paths: Paths,
    budget: int | None,
    embedder: Embedder | None,
) -> SyncReport:
    """Connect every account and run :func:`grepogram.sync.sync_all`; ``cfg`` is the config
    loader so the sources come from the file as it is once the sync lock is held, not from the
    snapshot the command started with (a ``sources rm`` may have run while the model loaded)."""
    async with _connected(accounts) as live:
        return await sync.sync_all(live, conn, cfg, paths, sync.SyncBudget(budget), embedder)


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


@app.command("extract")
def extract_cmd(
    budget: Annotated[
        int | None,
        typer.Option(
            "--budget",
            min=1,
            help="Stop after this many seconds; what is left resumes on the next run.",
        ),
    ] = None,
    retry_failed: Annotated[
        bool,
        typer.Option(
            "--retry-failed",
            help="Queue the media an earlier run could not read again, and the media this "
            "build had no extractor for (installing the 'media' extra is what fixes those).",
        ),
    ] = False,
) -> None:
    """Read text out of stored media — photos through OCR, PDFs and DOCX; needs a session.

    A network pass, not an offline one: Telethon downloads from a message Telegram just
    returned, so every pending message is re-fetched by id first. Run it after `grepogram sync`;
    it never runs inside one, because a 400-page PDF must not eat a sync's budget.
    """
    paths, cfg, conn = _load()
    _require_api_keys(cfg, paths)
    try:
        accounts = tg.make_clients(cfg, paths)
        with sync.SyncLock(paths):
            report = asyncio.run(_run_extract(accounts, conn, cfg, budget, retry_failed))
    except (tg.AuthRequired, tg.SessionError, sync.SyncInProgress, ConfigError) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    finally:
        conn.close()
    _print_media_report(report)


async def _run_extract(
    accounts: tg.Accounts,
    conn: sqlite3.Connection,
    cfg: Config,
    budget: int | None,
    retry_failed: bool,
) -> MediaReport:
    """Connect every account and run :func:`grepogram.media.run` under the sync lock the caller
    holds; each chat is read through an account that reaches it."""
    async with _connected(accounts) as live:
        return await media.run(
            conn,
            live,
            cfg,
            sync.SyncBudget(budget),
            retry_failed=retry_failed,
        )


def _print_media_report(report: MediaReport) -> None:
    typer.echo(f"media read: {report.extracted}")
    for label, count in (
        ("too large to download", report.skipped),
        ("could not be read", report.failed),
        ("no extractor here", report.unsupported),
        ("switched off in [media]", report.disabled),
        ("queued again", report.requeued),
        ("in chats nothing can re-fetch", report.unreachable),
    ):
        if count:
            typer.echo(f"{label}: {count}")
    if report.chats_unreachable:
        ids = ", ".join(str(chat_id) for chat_id in report.chats_unreachable)
        typer.echo(f"chats no signed-in account reaches: {len(report.chats_unreachable)} ({ids})")
    if report.remaining:
        typer.echo(f"media pending: {report.remaining}; run extract again")
    if report.extracted:
        typer.echo("next: grepogram sync (to re-cut and embed the units that changed)")
    for warning in report.warnings:
        typer.echo(f"warning: {warning}", err=True)


@app.command("prune-deleted")
def prune_deleted_cmd(
    chat: Annotated[
        str | None,
        typer.Option(
            "--chat",
            help="Sweep only this chat and the discussion group it links; "
            + _CHAT_HELP.removeprefix("The chat the message is in, "),
        ),
    ] = None,
    budget: Annotated[
        int | None,
        typer.Option(
            "--budget",
            min=1,
            help="Stop after this many seconds; the sweep resumes where it stopped.",
        ),
    ] = None,
) -> None:
    """Ask Telegram about every indexed message and drop the ones it no longer has; needs a
    session.

    The full sweep, about one request per hundred stored messages, so it is run by hand and never
    by a sync — `sync` notices only the deletions among the newest messages of a chat. It is
    resumable: a run stopped by `--budget` or by a flood wait keeps every batch it finished and
    the next one carries on from there.
    """
    paths, cfg, conn = _load()
    _require_api_keys(cfg, paths)
    try:
        chat_id = None if chat is None else filters.resolve_chat(conn, cfg, chat)
        accounts = tg.make_clients(cfg, paths)
        report = asyncio.run(_run_prune(accounts, conn, cfg, paths, budget, chat_id))
    except FilterError as exc:
        fail(str(exc))
    except (tg.AuthRequired, tg.SessionError, sync.SyncInProgress, ConfigError) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    finally:
        conn.close()
    _print_prune_report(report)


async def _run_prune(
    accounts: tg.Accounts,
    conn: sqlite3.Connection,
    cfg: Config,
    paths: Paths,
    budget: int | None,
    chat_id: int | None,
) -> PruneReport:
    """Connect every account and run :func:`grepogram.sync.prune_deleted`, which takes the sync
    lock itself."""
    async with _connected(accounts) as live:
        return await sync.prune_deleted(
            live, conn, cfg, paths, sync.SyncBudget(budget), chat_id=chat_id
        )


def _print_prune_report(report: PruneReport) -> None:
    typer.echo(f"messages removed: {report.removed}")
    typer.echo(f"messages checked: {report.checked}")
    typer.echo(f"chats swept: {len(report.chats_done)}")
    if report.chats_remaining:
        ids = ", ".join(str(chat_id) for chat_id in report.chats_remaining)
        typer.echo(
            f"chats not finished: {len(report.chats_remaining)} ({ids}); "
            "run prune-deleted again to carry on"
        )
    if report.chats_unreachable:
        ids = ", ".join(str(chat_id) for chat_id in report.chats_unreachable)
        typer.echo(f"chats no signed-in account reaches: {len(report.chats_unreachable)} ({ids})")
    for warning in report.warnings:
        typer.echo(f"warning: {warning}", err=True)


@app.command("recapture-links")
def recapture_links_cmd(
    chat: Annotated[
        str | None,
        typer.Option(
            "--chat",
            help="Re-read only this chat and the discussion group it links; "
            + _CHAT_HELP.removeprefix("The chat the message is in, "),
        ),
    ] = None,
    budget: Annotated[
        int | None,
        typer.Option(
            "--budget",
            min=1,
            help="Stop after this many seconds; the pass resumes where it stopped.",
        ),
    ] = None,
) -> None:
    """Re-read the indexed messages whose links were never captured and store what they link to;
    needs a session.

    Messages stored before this version were stored without their hidden hyperlinks, URL
    buttons and forward origins, so research could only read the links visible in their text.
    This asks Telegram about exactly those messages, a hundred per request, and writes down their
    links and forward origins — nothing else of them changes, and no sync resumes from anywhere
    else. It is resumable: a run stopped by `--budget` or by a flood wait carries on next time.
    """
    paths, cfg, conn = _load()
    _require_api_keys(cfg, paths)
    try:
        chat_id = None if chat is None else filters.resolve_chat(conn, cfg, chat)
        accounts = tg.make_clients(cfg, paths)
        report = asyncio.run(_run_recapture(accounts, conn, cfg, paths, budget, chat_id))
    except FilterError as exc:
        fail(str(exc))
    except (tg.AuthRequired, tg.SessionError, sync.SyncInProgress, ConfigError) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    finally:
        conn.close()
    _print_recapture_report(report)


async def _run_recapture(
    accounts: tg.Accounts,
    conn: sqlite3.Connection,
    cfg: Config,
    paths: Paths,
    budget: int | None,
    chat_id: int | None,
) -> RecaptureReport:
    """Connect every account and run :func:`grepogram.sync.recapture_links`, which takes the
    sync lock itself."""
    async with _connected(accounts) as live:
        return await sync.recapture_links(
            live, conn, cfg, paths, sync.SyncBudget(budget), chat_id=chat_id
        )


def _print_recapture_report(report: RecaptureReport) -> None:
    typer.echo(f"messages re-read: {report.captured}")
    typer.echo(f"messages asked about: {report.checked}")
    typer.echo(f"chats done: {len(report.chats_done)}")
    if report.chats_remaining:
        ids = ", ".join(str(chat_id) for chat_id in report.chats_remaining)
        typer.echo(
            f"chats not finished: {len(report.chats_remaining)} ({ids}); "
            "run recapture-links again to carry on"
        )
    if report.chats_unreachable:
        ids = ", ".join(str(chat_id) for chat_id in report.chats_unreachable)
        typer.echo(f"chats no signed-in account reaches: {len(report.chats_unreachable)} ({ids})")
    if report.remaining:
        typer.echo(f"messages whose links are still unread: {report.remaining}")
    for warning in report.warnings:
        typer.echo(f"warning: {warning}", err=True)


@app.command("import")
def import_cmd(
    path: Annotated[
        Path,
        typer.Argument(
            help="The export directory Telegram Desktop wrote, or the JSON file inside it.",
            exists=True,
            readable=True,
        ),
    ],
    chat_title: Annotated[
        str | None,
        typer.Option(
            "--chat-title",
            help="Title for the chat this export holds; single-chat exports often carry none.",
        ),
    ] = None,
    account: Annotated[
        str | None,
        typer.Option(
            "--account",
            "-a",
            help="The account the export was made from: its private chats and legacy groups "
            "are stored as that account's (default: default).",
        ),
    ] = None,
) -> None:
    """Index a Telegram Desktop export of a chat this account can no longer open (offline).

    Export the chat from Telegram Desktop as JSON (Settings → Advanced → Export Telegram data,
    machine-readable format), then point this at the directory it wrote. The messages are stored,
    cut into units and indexed exactly as a sync's are, so `search` answers from them at once;
    the chat is tagged `import:<slug>` and marked unavailable, so no sync ever fetches it and no
    prune ever offers it. Running the same import again is safe: it updates what it already
    stored rather than adding a second copy.
    """
    paths, cfg, conn = _load()
    try:
        name = _known_account(cfg, account or DEFAULT_ACCOUNT)
        export = tdesktop.read_export(path)
        for warning in export.warnings:
            typer.echo(f"warning: {warning}", err=True)
        entries = _retitled(export.chats, chat_title)
        embedder = _optional_embedder(cfg)
        with sync.SyncLock(paths):
            stored = _store_import(conn, cfg, entries, name)
            embedded = _embed_imported(conn, embedder)
    except tdesktop.ExportError as exc:
        fail(str(exc))
    except (sources.SourceError, sync.SyncInProgress) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    finally:
        conn.close()
    _print_import(export, stored, embedded)


def _store_import(
    conn: sqlite3.Connection,
    cfg: Config,
    entries: Sequence[tdesktop.ImportedChat],
    account: str = DEFAULT_ACCOUNT,
) -> list[sources.Imported]:
    """Store an export and cut its units in **one** transaction: all of it or none of it.

    `sources.import_chats` commits on its own and the rebuild that makes the rows searchable
    comes after it, so anything the rebuild could not do left the chats committed with
    ``indexed = 0`` and the command ending in a traceback. That state is not a failed import the
    user can retry — it is a chat every later `sync` reaches again through
    :func:`grepogram.sync.index_stranded` and fails on in the same way, which takes the MCP
    ``sync`` tool and every ``search`` old enough to auto-sync down with it. The same atomicity
    :func:`grepogram.sync.on_chat_synced` already gives its own three steps, one level up.

    The embedding stays outside: it is a long model run that must not hold the write lock, and a
    missing or mismatched model is a warning the import survives (:func:`_embed_imported`).
    """
    with db.transaction(conn):
        stored = sources.import_chats(conn, entries, account)
        for item in stored:
            pending = db.unindexed_message_ids(conn, item.chat.id)
            sync.on_chat_synced(conn, item.chat, cfg, pending)
    return stored


def _retitled(
    entries: Sequence[tdesktop.ImportedChat], title: str | None
) -> list[tdesktop.ImportedChat]:
    """``entries`` with ``--chat-title`` applied, which only a single-chat export can take.

    The title decides the ``import:<slug>`` tag as well as what ``sources ls`` shows, so an
    account export holding many chats has no one place to put it and says so instead of
    renaming an arbitrary one.
    """
    if not entries:
        fail("the export holds no chat this version can read")
    if title is None:
        return list(entries)
    if len(entries) > 1:
        fail(
            f"--chat-title names one chat and this export holds {len(entries)}; "
            "import it without the option and the export's own names are used"
        )
    entry = entries[0]
    return [dataclasses.replace(entry, chat=dataclasses.replace(entry.chat, title=title))]


def _embed_imported(conn: sqlite3.Connection, embedder: Embedder | None) -> int | None:
    """Embed the units the import just cut; ``None`` when no model was there to do it.

    The import is offline and the messages are searchable lexically the moment they are indexed,
    so a missing model is a warning and never a failure — `grepogram embed` finishes the job.
    """
    if embedder is None:
        return None
    try:
        index.ensure_embedding_space(conn, embedder)
        return index.embed_dirty_units(conn, embedder)
    except index.EmbeddingSpaceMismatch as exc:
        typer.echo(f"warning: dense index not updated: {exc}", err=True)
        return None


def _print_import(
    export: tdesktop.Export, stored: Sequence[sources.Imported], embedded: int | None
) -> None:
    typer.echo(f"read {export.path}")
    _print_table(
        ("id", "type", "title", "messages", "source"),
        [
            (
                str(item.chat.id),
                item.chat.type,
                item.chat.title or "-",
                str(item.messages),
                item.source_id,
            )
            for item in stored
        ],
    )
    total = sum(item.messages for item in stored)
    typer.echo(f"imported {total} messages into {len(stored)} chats")
    if export.service:
        typer.echo(f"service messages skipped: {export.service}")
    if export.skipped:
        typer.echo(f"entries that could not be read: {export.skipped}")
    if embedded is None:
        typer.echo("next: grepogram embed (to make the imported chats searchable by meaning)")
    else:
        typer.echo(f"embedded {embedded} units")


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
            help="Search only these chats: id, <account>/<id>, @username, folder:<name>, "
            "import:<slug>, account:<name> or a title (repeatable).",
        ),
    ] = None,
    account: Annotated[
        list[str] | None,
        typer.Option(
            "--account",
            "-a",
            help="Search only the chats this account reaches (repeatable); a scope, not "
            "isolation: a channel two accounts reach is in both.",
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
    """Search the indexed chats (offline); every hit carries a link that opens the message and
    names the accounts that reach its chat."""
    _, cfg, conn = _load()
    try:
        selected = filters.resolve_filters(conn, cfg, chat, since, until)
        result = search.search(
            conn,
            cfg,
            query,
            selected,
            k,
            mode=mode.value,
            full=full,
            rerank=rerank,
            accounts=account,
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
            f"{_via(hit.accounts)}"
        )
        typer.echo(f"   {hit.url}")
        if hit.fallback_url:
            typer.echo(f"   fallback: {hit.fallback_url}")
        body = hit.snippet if hit.text is None else hit.text
        for line in body.splitlines():
            typer.echo(f"   {line}")


def _via(accounts: Sequence[str]) -> str:
    """``  via a, b`` naming the accounts a result came through, when any of them is not the
    default one; a single-account install prints what it always printed."""
    if all(account == DEFAULT_ACCOUNT for account in accounts):
        return ""
    return f"  via {', '.join(accounts)}"


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
        typer.echo(
            f"{n}. {view.chat_id}/{view.msg_id}  {_moment(view.date)}  {who}{_via(view.accounts)}"
        )
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
    account: AccountOption = None,
) -> None:
    """Add a folder or chat of an account to the indexed sources and save the config; needs
    that account's session. A `<account>/` prefix on the target names the account too."""
    paths, cfg, conn = _load()
    _require_api_keys(cfg, paths)
    try:
        parsed = sources.parse_target(target)
        name = _known_account(cfg, account or parsed.account or DEFAULT_ACCOUNT)
        tg.ensure_session_mode(paths, name)
        client = tg.make_client(cfg, paths, name)
        added = asyncio.run(_add_source(client, cfg, parsed, since, comments, name))
        # the index is opened for this one check: a chat held as a Telegram Desktop import must
        # not gain a live source, because `db.upsert_chat` overwrites `source_id` and the next
        # sync would drop the `import:` tag every protection of that history keys on
        sources.refuse_imported(conn, added.dialogs, name)
        # the target was resolved over the network; the source is applied to the file as it is
        # by now, under the config lock, not to the snapshot read before the round trip — the
        # MCP server may have saved a change (a removed source) in the meantime
        dialog = None if added.folder is not None else added.dialogs[0]
        config.update(paths, lambda current: sources.with_source(current, added.source, dialog))
    except (sources.SourceError, tg.AuthRequired, tg.SessionError, ConfigError) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    finally:
        conn.close()
    if added.folder is not None:
        what = f"folder {added.title!r} with {len(added.dialogs)} chats"
    else:
        chat = added.dialogs[0]
        what = f"{chat.type} {chat.title!r} (id {chat.id})"
    typer.echo(f"added {added.source.id}: {what}")
    typer.echo("next: grepogram sync")


async def _add_source(
    client: TelegramClient,
    cfg: Config,
    target: sources.Target,
    since: str | None,
    comments: bool,
    account: str = DEFAULT_ACCOUNT,
) -> sources.Added:
    async with tg.connected(client, account):
        catalog = dialogs.DialogCatalog(client)
        return await sources.add_source(
            cfg, target, catalog, since=since, comments=comments, account=account
        )


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
        owner = status.account or "-"
        if not status.chats:
            rows.append(
                (owner, status.source_id, "-", "-", "-", "-", "0", "never", "not synced yet")
            )
        for chat in status.chats:
            rows.append(
                (
                    owner,
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
        ("account", "source", "id", "type", "title", "username", "messages", "last sync", "status"),
        rows,
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
    """Remove a source and delete the messages and index data of the chats no other source
    covers; a chat another source still covers is kept and reported (offline; refuses while a
    sync is running, and refuses a chat indexed through a folder or as a channel's discussion
    group). Never leaves a chat on Telegram."""
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
        fail(str(exc), hint=getattr(exc, "hint", None))
    finally:
        conn.close()
    kept = (
        f", {len(removed.kept_chat_ids)} kept under another source" if removed.kept_chat_ids else ""
    )
    typer.echo(f"removed {removed.source_id} ({len(removed.chat_ids)} chats deleted{kept})")


@sources_app.command("prune")
def sources_prune(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would go and change nothing.")
    ] = False,
) -> None:
    """Delete indexed chats their folder source no longer lists; needs a session.

    What a folder holds right now is only knowable from Telegram, so the folders are read over
    the network first and the sync lock is taken afterwards, for the deletion alone; there is no
    config to save, because a chat that left a folder changes no source entry. Nothing goes
    without a confirmation, and a source Telegram will not answer for stops the prune — a folder
    that failed to resolve is not a folder that lists nothing.

    The scan and the confirmation both predate the lock, so every chat is put to the offer's
    terms again under it (:func:`grepogram.sources.prune_chats`) and one another process changed
    meanwhile survives; that is why the count printed at the end can be lower than the table's.
    """
    paths, cfg, conn = _load()
    _require_api_keys(cfg, paths)
    if not cfg.sources:
        conn.close()
        fail("no sources configured; add one with: grepogram sources add <target>")
    try:
        folder_accounts = {source.account for source in cfg.sources if source.folder is not None}
        accounts = tg.make_clients(cfg, paths, sorted(folder_accounts))
        scan = sources.prunable(cfg, conn, asyncio.run(_folder_membership(accounts, cfg, conn)))
        for candidate in scan.kept:
            typer.echo(f"kept {_chat_label(candidate.chat)}: {candidate.reason}")
        if scan.unresolved:
            fail(
                "nothing was pruned, these sources could not be checked: "
                + "; ".join(scan.unresolved),
                hint="a source that does not resolve is not a source that lists nothing; "
                "run `grepogram sources prune` again once Telegram answers for it",
            )
        if not scan.prunable:
            typer.echo("nothing to prune")
            return
        _print_prune(scan.prunable)
        if dry_run:
            typer.echo(f"--dry-run: nothing removed ({len(scan.prunable)} chats would go)")
            return
        if not typer.confirm(
            f"delete these {len(scan.prunable)} chats and everything indexed from them?"
        ):
            typer.echo("nothing removed")
            return
        with sync.SyncLock(paths):
            removed = sources.prune_chats(conn, scan.prunable)
        typer.echo(f"removed {len(removed)} chats")
        if len(removed) < len(scan.prunable):
            typer.echo(
                f"{len(scan.prunable) - len(removed)} changed since the scan and were kept; "
                "run `grepogram sources prune` again to see them",
                err=True,
            )
    except (tg.AuthRequired, tg.SessionError, sync.SyncInProgress, ConfigError) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    finally:
        conn.close()


async def _folder_membership(
    accounts: tg.Accounts, cfg: Config, conn: sqlite3.Connection
) -> sources.FolderMembership:
    """Connect every account that owns a folder source and read what each of its folders lists
    right now; the folder of an account that is not connected — or whose session is another
    Telegram user than the index recorded (:func:`grepogram.sync.check_account`), whose folders
    say nothing about the recorded user's — is recorded as unchecked."""
    if not accounts.clients:
        return await sources.folder_membership(cfg, {})
    async with _connected(accounts) as live:
        checked, left_out = await sync.checked_accounts(conn, live)
        for name, reason in left_out.items():
            typer.echo(f"warning: account {name}: {reason}", err=True)
        catalogs = {name: dialogs.DialogCatalog(client) for name, client in checked.items()}
        return await sources.folder_membership(cfg, catalogs)


def _chat_label(chat: ChatRow) -> str:
    return f"{chat.title or chat.type} (id {chat.id})"


def _print_prune(candidates: Sequence[sources.PruneCandidate]) -> None:
    _print_table(
        ("id", "type", "title", "messages", "reason"),
        [
            (str(c.chat.id), c.chat.type, c.chat.title or "-", str(c.messages), c.reason)
            for c in candidates
        ],
    )


@accounts_app.command("ls")
def accounts_ls() -> None:
    """List the accounts: session file, who signed in, sources and reachable chats (offline).

    `session` is `missing` with no session file, `authorized` when the account was signed in
    the last time grepogram used it (`auth` or a sync recorded who it is), and `present` for a
    file no run has confirmed yet.
    """
    paths, cfg, conn = _load()
    try:
        listed = sources.accounts_status(cfg, paths, conn)
    finally:
        conn.close()
    rows = [
        (
            status.name,
            status.label or "-",
            status.session,
            "-" if status.user_id is None else f"{status.display_name or '-'} ({status.user_id})",
            str(len(status.sources)),
            str(status.chats),
        )
        for status in listed
    ]
    _print_table(("account", "label", "session", "user", "sources", "chats"), rows)


@accounts_app.command("rm")
def accounts_rm(
    name: Annotated[str, typer.Argument(help="The account to remove, as `accounts ls` lists it.")],
) -> None:
    """Remove an account: its sources, the chats only they cover, and its session file.

    Asks on the terminal first and refuses without one. A chat another account's source still
    covers stays indexed (a channel both accounts configured); the account only stops being
    recorded as reaching it. Its research sessions are stopped, so no approval given to it
    outlives it. Nothing is changed on Telegram — `leave` is its own command.
    """
    paths, cfg, conn = _load()
    try:
        if name not in cfg.account_names():
            fail(f"unknown account {name!r}; known: {', '.join(cfg.account_names())}")
        if name == DEFAULT_ACCOUNT and not cfg.accounts:
            fail(
                "the default account is the only account and cannot be removed",
                hint="delete its sources with `grepogram sources rm` instead",
            )
        owned = [source.id for source in cfg.sources if source.account == name]
        session = paths.session_file_for(name)
        typer.echo(f"removing account {name} will:")
        typer.echo(f"  remove {len(owned)} sources{': ' + ', '.join(owned) if owned else ''}")
        typer.echo("  delete the chats no other source covers, with everything indexed from them")
        typer.echo("  forget which chats this account reaches")
        typer.echo("  stop its research sessions and void their unused approvals")
        typer.echo(f"  delete its session file {session}")
        with _terminal("grepogram accounts rm") as tty:
            confirmed = _ask(tty, f"remove account {name}?")
        if not confirmed:
            typer.echo("nothing removed")
            return
        # the config is read, edited and saved under the same locks as the deletion: a sync
        # that starts once they are free reads a config without these sources and cannot
        # re-create their chats, and an MCP edit saved in between is not overwritten
        with sync.SyncLock(paths), config.ConfigLock(paths):
            stopped = _stop_research_of(paths, name)
            deleted, kept = _drop_account(config.load(paths), conn, paths, name)
        session.unlink(missing_ok=True)
    except NoTerminal as exc:
        fail(str(exc), hint=exc.hint)
    except (sources.SourceError, sync.SyncInProgress, ConfigError) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    except (db.SchemaError, sqlite3.Error) as exc:
        fail(f"cannot stop the research sessions of account {name}: {exc}; nothing was removed")
    finally:
        conn.close()
    kept_note = f", {len(kept)} kept under another source" if kept else ""
    stopped_note = f", {len(stopped)} research sessions stopped" if stopped else ""
    typer.echo(f"removed account {name} ({len(deleted)} chats deleted{kept_note}{stopped_note})")


def _stop_research_of(paths: Paths, name: str) -> list[int]:
    """Stop every active research session of account ``name``, voiding the grants it has not
    used; returns their ids. Whether ``[research]`` is enabled does not matter — an approval must
    not outlive the account it was given to, and a later sign-in under the same name may be
    someone else. A ``research.db`` that does not exist holds nothing to stop."""
    if not paths.research_db_file.exists():
        return []
    rdb = research_db.open_store(paths)
    try:
        active = [s.id for s in research_db.list_sessions(rdb, "active") if s.account == name]
        for session_id in active:
            research_db.stop_session(rdb, session_id)
    finally:
        rdb.close()
    return active


def _drop_account(
    current: Config, conn: sqlite3.Connection, paths: Paths, name: str
) -> tuple[list[int], list[int]]:
    """Remove every source of ``name`` from ``current`` through the ordinary source-removal
    rules, forget its access, drop its ``[[accounts]]`` entry and save the config. Returns the
    chats deleted and those kept under another source. The caller holds both locks.

    One transaction: every removal joins it (:func:`grepogram.db.transaction`), and the config
    is saved inside it, last. A failure anywhere — the save included — rolls every deletion
    back with the config untouched, so no chat is ever deleted while the source that fetched it
    stays configured and a sync fetches it all over again.
    """
    deleted: list[int] = []
    kept: list[int] = []
    with db.transaction(conn):
        for source in [s for s in current.sources if s.account == name]:
            removed = sources.remove_source_id(current, conn, source.id)
            current = removed.config
            deleted += removed.chat_ids
            kept += removed.kept_chat_ids
        db.forget_account(conn, name)
        accounts = [entry for entry in current.accounts if entry.name != name]
        config.save(dataclasses.replace(current, accounts=accounts), paths)
    gone = set(deleted)
    return sorted(gone), sorted({chat_id for chat_id in kept if chat_id not in gone})


@app.command("leave")
def leave_cmd(
    target: Annotated[
        str,
        typer.Argument(help="The group or channel: id, @username, t.me link or a title (fuzzy)."),
    ],
    account: AccountOption = None,
) -> None:
    """Leave a group or channel on Telegram as an account; asks on the terminal first.

    This is the one command that changes the account on Telegram, and it changes nothing here:
    the sources stay in the config and everything indexed from the chat stays searchable
    (`grepogram sources rm` removes those). Removing a source never leaves a chat.
    """
    paths = Paths.from_env()
    cfg = _load_config(paths)
    _require_api_keys(cfg, paths)
    try:
        parsed = sources.parse_target(target)
        if account and parsed.account and account.casefold() != parsed.account:
            raise sources.InvalidTarget(
                f"{target!r} names a chat of account {parsed.account}, but --account is {account}"
            )
        name = _known_account(cfg, account or parsed.account or DEFAULT_ACCOUNT)
        tg.ensure_session_mode(paths, name)
        with _terminal("grepogram leave") as tty:
            client = tg.make_client(cfg, paths, name)
            left = asyncio.run(_leave(client, parsed, name, functools.partial(_ask, tty)))
    except NoTerminal as exc:
        fail(str(exc), hint=exc.hint)
    except (sources.SourceError, tg.AuthRequired, tg.SessionError) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    if left is None:
        typer.echo("nothing changed")
        return
    typer.echo(f"left {left.type} {left.title!r} (id {left.id}) as account {name}")
    typer.echo("its sources and indexed history are unchanged")


async def _leave(
    client: TelegramClient,
    target: sources.Target,
    account: str,
    confirm: Callable[[str], bool],
) -> DialogInfo | None:
    """Resolve ``target`` among ``account``'s dialogs, ask ``confirm``, and leave it; ``None``
    when the answer was no. A folder, a private chat and a bot are refused: there is nothing
    to leave."""
    async with tg.connected(client, account):
        catalog = dialogs.DialogCatalog(client)
        found = await sources.resolve_target(target, catalog)
        if isinstance(found, FolderInfo):
            raise sources.InvalidTarget(
                f"{found.title!r} is a folder; leave takes one group or channel"
            )
        if found.type in ("user", "bot"):
            raise sources.InvalidTarget(
                f"{found.title!r} is a private chat with a {found.type}; there is nothing to leave"
            )
        if not confirm(f"leave {found.type} {found.title!r} (id {found.id}) as account {account}?"):
            return None
        if found.type == "group":
            chat_id, _ = utils.resolve_id(found.id)
            await client(
                functions.messages.DeleteChatUserRequest(
                    chat_id=chat_id, user_id=types.InputUserSelf()
                )
            )
        else:
            channel = await catalog.entity(found.id)
            await client(functions.channels.LeaveChannelRequest(channel=channel))
    return found


# --- research ----------------------------------------------------------------------------------

_SESSION_HELP = "The research session, as `grepogram research status` lists it."
SessionArg = Annotated[int, typer.Argument(help=_SESSION_HELP, min=1)]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Print the result as JSON and nothing else.")
]
_RESEARCH_ERRORS = (research.ResearchError, FilterError, ConfigError)


@contextlib.contextmanager
def _research_store() -> Iterator[tuple[Paths, Config, sqlite3.Connection, sqlite3.Connection]]:
    """The paths, config, index and ``research.db`` a research command works on; refuses
    before opening anything more while ``[research] enabled`` is false."""
    paths, cfg, conn = _load()
    try:
        research.require_enabled(cfg)
        rdb = research_db.open_store(paths)
    except research.ResearchError as exc:
        conn.close()
        fail(str(exc), hint=exc.hint)
    except (db.SchemaError, sqlite3.Error) as exc:
        conn.close()
        fail(str(exc))
    try:
        yield paths, cfg, conn, rdb
    finally:
        rdb.close()
        conn.close()


def _echo_json(document: object) -> None:
    typer.echo(json.dumps(document, ensure_ascii=False, indent=2))


def _ids(values: Iterable[int]) -> str:
    return ", ".join(str(value) for value in values) or "-"


@research_app.command("start")
def research_start(
    question: Annotated[str, typer.Argument(help="What the research is about, in your words.")],
    seed: Annotated[
        list[str],
        typer.Option(
            "--seed",
            "-s",
            help="An indexed chat to start from, as `search --chat` takes it (repeatable).",
        ),
    ],
    account: AccountOption = None,
    max_depth: Annotated[
        int | None, typer.Option("--max-depth", help="Hops from a seed (\\[research]).")
    ] = None,
    max_candidates: Annotated[
        int | None,
        typer.Option("--max-candidates", help="New candidates per discover call."),
    ] = None,
    probe_limit: Annotated[
        int | None, typer.Option("--probe-limit", help="Probes per discover call.")
    ] = None,
    since_days: Annotated[
        int | None,
        typer.Option("--since-days", help="History horizon of the sources a run adds."),
    ] = None,
    max_messages: Annotated[
        int | None, typer.Option("--max-messages", help="Messages one run may store.")
    ] = None,
    budget: Annotated[
        int | None, typer.Option("--budget", help="Seconds one run may take.")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Start a research session: a question, the indexed chats to start from and the account
    that later joins and fetches (offline). Limits default to the \\[research] section."""
    overrides = {
        "max_depth": max_depth,
        "max_candidates": max_candidates,
        "probe_limit": probe_limit,
        "since_days": since_days,
        "max_messages_per_run": max_messages,
        "run_budget_s": budget,
    }
    with _research_store() as (_, cfg, conn, rdb):
        try:
            name = _known_account(cfg, account or DEFAULT_ACCOUNT)
            session = research.start_session(rdb, conn, cfg, question, seed, name, overrides)
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    if as_json:
        _echo_json(research.session_document(session))
        return
    typer.echo(f"started research session {session.id} as account {session.account}")
    typer.echo(f"seed chats: {_ids(seed.peer_id for seed in session.seeds)}")
    typer.echo(f"next: grepogram research discover {session.id}")


@research_app.command("discover")
def research_discover(
    session_id: SessionArg,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Only read the indexed messages; ask Telegram nothing (no probing, no search).",
        ),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Find the chats the session's chats lead to, then probe the best of them on Telegram
    (title, size, membership — never their history) and run the global searches a human
    approved. Every find is only proposed."""
    with _research_store() as (paths, cfg, conn, rdb):
        try:
            session = research.active_session(rdb, session_id)
            if offline:
                report = research.discover_offline(rdb, conn, cfg, session.id)
            else:
                _require_api_keys(cfg, paths)
                tg.ensure_session_mode(paths, session.account)
                client = tg.make_client(cfg, paths, session.account)
                report = asyncio.run(_discover(client, rdb, conn, cfg, session))
        except tg.OtherUser as exc:
            fail(str(exc), hint=exc.hint)
        except (tg.AuthRequired, tg.SessionError) as exc:
            fail(
                str(exc),
                hint=f"to read the index alone: grepogram research discover {session_id} --offline",
            )
        except (tg_errors.RPCError, ConnectionError) as exc:
            fail(f"telegram error: {exc}")
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    if as_json:
        _echo_json(research.report_document(report))
        return
    _print_discover(report)


async def _discover(
    client: TelegramClient,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    session: ResearchSession,
) -> DiscoverReport:
    async with tg.connected(client, session.account):
        return await research.discover(rdb, conn, cfg, session.id, client)


def _print_discover(report: DiscoverReport) -> None:
    typer.echo(
        f"read {report.chats_scanned} chats ({report.messages_scanned} messages): "
        f"{report.leads} leads, {len(report.new_candidates)} new candidates, "
        f"{len(report.updated_candidates)} with new evidence"
    )
    if report.text_fallback:
        typer.echo(
            f"note: {report.text_fallback} messages stored without their links were read by "
            "their visible text only; their hidden links and buttons were not seen "
            "(grepogram recapture-links reads them again)"
        )
    if report.directories:
        typer.echo(f"directories (chats that list many others): {_ids(report.directories)}")
    if report.beyond_depth or report.excluded or report.over_cap:
        more = "; the next discover reads the rest again" if report.truncated else ""
        if report.session_full:
            more = "; the session holds as many candidates as it may"
        typer.echo(
            f"left out: {report.beyond_depth} beyond the depth limit, {report.excluded} "
            f"excluded, {report.over_cap} over the candidate cap{more}"
        )
    pins = report.pins
    if pins is not None:
        typer.echo(
            f"pinned posts: {pins.messages} read in {len(pins.chats)} chats, "
            f"{len(pins.new_candidates)} new candidates; {pins.remaining} chats left"
        )
        for warning in pins.warnings:
            typer.echo(f"warning: {warning}", err=True)
    for search_report in report.searches:
        state = f"{search_report.results} results" if search_report.ran else "not run"
        typer.echo(
            f"{search_report.kind} {search_report.query!r}: {state}, "
            f"{len(search_report.new_candidates)} new candidates"
        )
        for warning in search_report.warnings:
            typer.echo(f"warning: {warning}", err=True)
    probe = report.probe
    if probe is not None:
        typer.echo(
            f"probed {len(probe.probed)}: {len(probe.unavailable)} unavailable, "
            f"{len(probe.unresolvable)} unresolvable, {len(probe.children)} found in shared "
            f"folders; {probe.remaining} left to probe"
        )
        folder_left_out = probe.people + probe.excluded + probe.in_session + probe.over_cap
        if folder_left_out:
            typer.echo(
                f"left out of shared folders: {probe.people} people, {probe.excluded} excluded, "
                f"{probe.in_session} the session already reads, {probe.over_cap} over the "
                "candidate cap"
            )
        for warning in probe.warnings:
            typer.echo(f"warning: {warning}", err=True)
    typer.echo(f"next: grepogram research candidates {report.session_id}")


@research_app.command("candidates")
def research_candidates(
    session_id: SessionArg,
    status: Annotated[
        list[str] | None,
        typer.Option("--status", help="Only candidates in this status (repeatable)."),
    ] = None,
    evidence: Annotated[
        int, typer.Option("--evidence", min=0, help="Evidence lines shown per candidate.")
    ] = 3,
    as_json: JsonOption = False,
) -> None:
    """List a session's candidates, best corroborated first, with the evidence that led to each
    and three separate facts: member (the account is in it), cached (the index holds it) and
    authorized (what a human approved) (offline)."""
    with _research_store() as (_, cfg, conn, rdb):
        try:
            document = research.candidates_document(rdb, conn, cfg, session_id, status)
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    if as_json:
        _echo_json(document)
        return
    _print_candidates(document, evidence)


_YES_NO = {True: "yes", False: "no", None: "unknown"}


def _print_candidates(document: Mapping[str, Any], shown: int) -> None:
    candidates: list[dict[str, Any]] = document["candidates"]
    if not candidates:
        typer.echo(f"no candidates; run: grepogram research discover {document['session_id']}")
        return
    for n, c in enumerate(candidates):
        if n:
            typer.echo("")
        facts = [c["type"] or "not probed yet"]
        if c["participants"] is not None:
            facts.append(f"{c['participants']:,} members")
        if c["request_needed"]:
            facts.append("admins approve who joins")
        title = f'  "{c["title"]}"' if c["title"] else ""
        typer.echo(f"{c['id']}. {c['identity']}{title}  {', '.join(facts)}")
        typer.echo(
            f"   status {c['status']}, depth {c['depth']}, corroboration {c['corroboration']}, "
            f"question overlap {c['overlap']}"
        )
        if c["cached"]:
            via = ", ".join(c["cached_accounts"]) or "an import"
            cached = f"yes (via {via})"
        else:
            cached = "no"
        typer.echo(
            f"   member: {_YES_NO[c['member']]}  cached: {cached}  "
            f"authorized: {', '.join(c['authorized']) or '-'}"
        )
        if c["note"]:
            typer.echo(f"   note: {c['note']}")
        found: list[dict[str, Any]] = c["evidence"]
        for item in found[:shown]:
            where = "" if item["peer_id"] is None else f" {item['peer_id']}"
            where += "" if item["msg_id"] is None else f"/{item['msg_id']}"
            text = " ".join((item["snippet"] or "").split())
            typer.echo(f"   {item['via']}{where}: {text or '-'}")
        if len(found) > shown:
            typer.echo(f"   … {len(found) - shown} more (--evidence or --json)")


@research_app.command("approve")
def research_approve(
    session_id: SessionArg,
    items: Annotated[
        list[str],
        typer.Argument(
            help="ID:action,… per candidate (join, request, fetch, add_source; a bare ID "
            "approves joining it, or asking to, and fetching it as a source; ID:fetch,add_source "
            "reads a public chat without joining), or global_search / paid_search for the "
            "session."
        ),
    ],
) -> None:
    """Approve named candidates and actions, after reading exactly what they do.

    The summary is shown and the answer read on the controlling terminal, never stdin: you
    confirm by typing back the code it shows, and without a terminal this refuses. Run it
    yourself — an agent with a shell could give it a terminal of its own. Approving a chat
    approves nothing found inside it.
    """
    with _research_store() as (_, cfg, conn, rdb):
        try:
            approval = research.prepare_approval(rdb, conn, cfg, session_id, items)
            with _terminal(approval.command) as tty:
                tty.write(f"{approval.summary}\n\n")
                confirmed = _ask(tty, "approve all of the above?")
            if not confirmed:
                typer.echo("nothing approved")
                return
            granted = research.grant(
                rdb, conn, cfg, session_id, approval.items, via="cli", summary=approval.summary
            )
        except NoTerminal as exc:
            fail(str(exc), hint=exc.hint)
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    for approved in granted:
        target = (
            "the session"
            if approved.candidate_id is None
            else (f"candidate {approved.candidate_id}")
        )
        typer.echo(f"approved for {target}: {', '.join(approved.actions)}")
    typer.echo(f"next: grepogram research run {session_id}")


@research_app.command("skip")
def research_skip(
    session_id: SessionArg,
    candidate_ids: Annotated[list[int], typer.Argument(help="Candidates to set aside.")],
) -> None:
    """Set candidates aside; their approvals are voided. Needs no confirmation: it only
    narrows what the session does."""
    with _research_store() as (_, cfg, _conn, rdb):
        try:
            skipped = research.skip(rdb, cfg, session_id, candidate_ids)
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    typer.echo(f"skipped: {_ids(skipped)}")


_REFS_HELP = "Candidate ids (with --session), @usernames, t.me links or marked chat ids."
_SESSION_OPTION_HELP = "The session the candidate ids belong to."


@research_app.command("exclude")
def research_exclude(
    refs: Annotated[list[str], typer.Argument(help=_REFS_HELP)],
    session_id: Annotated[
        int | None, typer.Option("--session", "-s", min=1, help=_SESSION_OPTION_HELP)
    ] = None,
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why, for `research status` later.")
    ] = None,
) -> None:
    """Never propose these chats again, in any session; their approvals are voided."""
    with _research_store() as (_, cfg, _conn, rdb):
        try:
            excluded = research.exclude(rdb, cfg, refs, session_id=session_id, reason=reason)
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    for identity, moved in excluded.items():
        typer.echo(f"excluded {identity} from every session ({moved} candidates set aside)")


@research_app.command("unexclude")
def research_unexclude(
    refs: Annotated[list[str], typer.Argument(help=_REFS_HELP)],
    session_id: Annotated[
        int | None, typer.Option("--session", "-s", min=1, help=_SESSION_OPTION_HELP)
    ] = None,
) -> None:
    """Lift exclusions: the chats are proposed again, and nothing is approved."""
    with _research_store() as (_, cfg, _conn, rdb):
        try:
            lifted = research.unexclude(rdb, cfg, refs, session_id=session_id)
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    if not lifted:
        typer.echo("none of them was excluded")
    for identity in lifted:
        typer.echo(f"no longer excluded: {identity} (proposed again, nothing approved)")


@research_app.command("run")
def research_run(session_id: SessionArg, as_json: JsonOption = False) -> None:
    """Carry out what a human approved — joins, admission requests, new sources and their
    history — within the session's time and message budgets, then look one hop further (and
    only propose). Resumable: run it again to go on."""
    with _research_store() as (paths, cfg, conn, rdb):
        try:
            session = research.active_session(rdb, session_id)
            _require_api_keys(cfg, paths)
            accounts = tg.make_clients(cfg, paths)
            embedder = _optional_embedder(cfg)
            report = asyncio.run(_research_run(accounts, rdb, conn, cfg, paths, session, embedder))
        except (tg.AuthRequired, tg.SessionError) as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
        except (tg_errors.RPCError, ConnectionError) as exc:
            fail(f"telegram error: {exc}")
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    if as_json:
        _echo_json(research.report_document(report))
        return
    _print_run(report)


async def _research_run(
    accounts: tg.Accounts,
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    paths: Paths,
    session: ResearchSession,
    embedder: Embedder | None,
) -> RunReport:
    async with _connected(accounts) as live:
        return await research.run(rdb, conn, cfg, paths, live, session.id, embedder=embedder)


def _print_run(report: RunReport) -> None:
    typer.echo(f"research session {report.session_id}: {report.messages} messages stored")
    for label, ids in (
        ("admitted", report.admitted),
        ("joined", report.joined),
        ("waiting for admission", report.pending_admission),
        ("sources added", report.sources_added),
        ("fetched", report.fetched),
        ("partly fetched, the next run goes on", report.partial),
        ("unavailable", report.unavailable),
        ("failed", report.failed),
    ):
        if ids:
            typer.echo(f"{label}: {_ids(ids)}")
    if report.stopped_by is not None:
        typer.echo(f"stopped by: {report.stopped_by}; run it again to go on")
    if report.pins is not None and report.pins.new_candidates:
        typer.echo(f"proposed from pinned posts: {len(report.pins.new_candidates)}")
        for warning in report.pins.warnings:
            typer.echo(f"warning: {warning}", err=True)
    if report.discovery is not None:
        typer.echo(f"new candidates proposed: {len(report.discovery.new_candidates)}")
    for warning in report.warnings:
        typer.echo(f"warning: {warning}", err=True)
    typer.echo(f"next: grepogram research candidates {report.session_id}")


@research_app.command("status")
def research_status(
    session_id: Annotated[
        int | None, typer.Argument(help="One session in full; every session when omitted.")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Every research session in brief with the chats excluded from all of them (and why), or
    one session in full: its progress, the approvals a run has yet to carry out and the
    admission requests still waiting (offline)."""
    with _research_store() as (_, cfg, _conn, rdb):
        try:
            document = research.status_document(rdb, cfg, session_id)
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    if as_json:
        _echo_json(document)
        return
    if session_id is None:
        _print_sessions(document["sessions"])
        _print_exclusions(document["exclusions"])
    else:
        _print_session_status(document)


def _print_sessions(sessions: Sequence[Mapping[str, Any]]) -> None:
    if not sessions:
        typer.echo(
            'no research sessions; start one: grepogram research start "<question>" -s <chat>'
        )
        return
    rows = [
        (
            str(s["id"]),
            s["state"],
            s["account"],
            str(s["candidates"]),
            str(s["runs"]),
            s["question"],
        )
        for s in sessions
    ]
    _print_table(("session", "state", "account", "candidates", "runs", "question"), rows)


def _print_exclusions(exclusions: Sequence[Mapping[str, Any]]) -> None:
    if not exclusions:
        return
    typer.echo("")
    typer.echo("excluded from every session (grepogram research unexclude lifts one):")
    for excluded in exclusions:
        reason = f": {excluded['reason']}" if excluded["reason"] else ""
        typer.echo(f"  {excluded['identity']}{reason}")


def _print_session_status(document: Mapping[str, Any]) -> None:
    session = document["session"]
    limits = session["limits"]
    progress = session["progress"]
    typer.echo(f'research session {session["id"]} ({session["state"]}): "{session["question"]}"')
    seeds = _ids(seed["peer_id"] for seed in session["seeds"])
    typer.echo(f"account: {session['account']}; seed chats: {seeds}")
    typer.echo(
        f"limits: depth {limits['max_depth']}, {limits['max_candidates']} candidates and "
        f"{limits['probe_limit']} probes per discover, sources since {session['horizon']}, "
        f"{limits['max_messages_per_run']} messages and {limits['run_budget_s']} s per run"
    )
    counts = ", ".join(f"{n} {status}" for status, n in document["candidates"].items())
    typer.echo(f"candidates: {counts or 'none yet'}")
    runs = progress.get("runs", 0)
    if runs:
        last = progress.get("last_run", {})
        stopped = last.get("stopped_by")
        tail = f", last stopped by {stopped}" if stopped else ""
        typer.echo(f"runs: {runs}, {progress.get('messages', 0)} messages stored{tail}")
    for pending in document["pending_grants"]:
        target = (
            "the session"
            if pending["candidate_id"] is None
            else f"candidate {pending['candidate_id']} ({pending['identity']})"
        )
        typer.echo(f"approved, not carried out yet: {target}: {', '.join(pending['actions'])}")
    for waiting in document["pending_admission"]:
        typer.echo(f"waiting for admission: candidate {waiting['id']} ({waiting['identity']})")


@research_app.command("stop")
def research_stop(session_id: SessionArg) -> None:
    """Stop a session: it explores no further and approvals it has not used are voided. Every
    source its runs added stays; remove one with `grepogram sources rm`."""
    with _research_store() as (_, cfg, _conn, rdb):
        try:
            voided = research.stop(rdb, cfg, session_id)
        except _RESEARCH_ERRORS as exc:
            fail(str(exc), hint=getattr(exc, "hint", None))
    typer.echo(f"stopped research session {session_id}; {voided} unused approvals voided")
    typer.echo("the sources its runs added stay configured")


@config_app.command("path")
def config_path() -> None:
    """Print the resolved file locations (GREPOGRAM_HOME overrides them)."""
    paths = Paths.from_env()
    rows = (
        ("config", paths.config_file),
        ("session", paths.session_file),
        ("sessions", paths.sessions_dir),
        ("index", paths.db_file),
        ("research", paths.research_db_file),
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


def fail(message: str, code: int = 1, *, hint: str | None = None) -> NoReturn:
    """Print ``error: <message>`` to stderr, then ``hint: <hint>`` when there is one, and exit."""
    typer.echo(f"error: {message}", err=True)
    if hint:
        typer.echo(f"hint: {hint}", err=True)
    raise typer.Exit(code)


def _known_account(cfg: Config, name: str) -> str:
    """``name`` when the config knows it (:meth:`~grepogram.models.Config.account_names`), else
    exit naming the ones it knows and how to add it."""
    known = cfg.account_names()
    if name not in known:
        fail(
            f"unknown account {name!r}; known: {', '.join(known)}",
            hint=f"sign it in first: grepogram auth --account {name}",
        )
    return name


def _open_terminal() -> TextIO:
    """The controlling terminal, read and written; ``OSError`` when the process has none.

    Opened unbuffered in binary and wrapped for text: a text-mode ``r+`` open wants a seekable
    file, which a terminal is not, and would fail on every real one.
    """
    raw = open(TERMINAL, "r+b", buffering=0)  # noqa: SIM115 - closed with the wrapper
    try:
        return io.TextIOWrapper(raw, encoding="utf-8", errors="replace", write_through=True)
    except BaseException:
        raw.close()
        raise


@contextlib.contextmanager
def _terminal(command: str) -> Iterator[TextIO]:
    """The terminal a confirmation of ``command`` is asked on, or :class:`NoTerminal`.

    It is the controlling terminal and never stdin, so nothing piped into the command answers,
    and there is no option that skips the question. It does not stop an agent that can run
    shell commands, which can give the command a terminal of its own: the confirmation holds
    against an MCP-only agent, a pipe and a blind ``yes``, and otherwise relies on the human
    being the one who runs the command.
    """
    try:
        tty = _open_terminal()
    except OSError as exc:
        raise NoTerminal(command) from exc
    with tty:
        yield tty


def _confirmation_code() -> str:
    """A fresh random code a confirmation asks the human to type back."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _ask(tty: TextIO, question: str) -> bool:
    """Ask ``question`` on ``tty``; a yes is the random code it shows typed back, nothing else.

    A code rather than ``y``: a ``yes |`` or an answer written into the command blindly, before
    the question was ever shown, cannot guess it.
    """
    code = _confirmation_code()
    tty.write(f"{question}\ntype {code} to confirm, anything else cancels: ")
    tty.flush()
    return tty.readline().strip().casefold() == code


def _load_config(paths: Paths) -> Config:
    """Read the config file (defaults when absent), exiting on a broken one."""
    try:
        return config.load(paths)
    except ConfigError as exc:
        fail(str(exc), hint=exc.hint)


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
