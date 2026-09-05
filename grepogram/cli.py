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
import dataclasses
import datetime as dt
import functools
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
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
    media,
    search,
    sources,
    sync,
    tdesktop,
    tg,
)
from grepogram.config import TEMPLATE, ConfigError
from grepogram.dialogs import Match
from grepogram.embed import Embedder, ModelUnavailable
from grepogram.filters import FilterError
from grepogram.log import setup_logging
from grepogram.models import (
    ChatRow,
    Config,
    MediaReport,
    MessageView,
    PruneReport,
    SearchResult,
    SyncReport,
)
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
        fail(str(exc), hint=getattr(exc, "hint", None))
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
        tg.ensure_session_mode(paths)
        client = tg.make_client(cfg, paths)
        with sync.SyncLock(paths):
            report = asyncio.run(_run_extract(client, conn, cfg, budget, retry_failed))
    except (tg.AuthRequired, tg.SessionError, sync.SyncInProgress, ConfigError) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    except (tg_errors.RPCError, ConnectionError) as exc:
        fail(f"telegram error: {exc}")
    finally:
        conn.close()
    _print_media_report(report)


async def _run_extract(
    client: TelegramClient,
    conn: sqlite3.Connection,
    cfg: Config,
    budget: int | None,
    retry_failed: bool,
) -> MediaReport:
    """Connect and run :func:`grepogram.media.run` under the sync lock the caller holds."""
    async with tg.connected(client):
        return await media.run(
            conn, client, cfg, sync.SyncBudget(budget), retry_failed=retry_failed
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
        tg.ensure_session_mode(paths)
        client = tg.make_client(cfg, paths)
        report = asyncio.run(_run_prune(client, conn, cfg, paths, budget, chat_id))
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
    client: TelegramClient,
    conn: sqlite3.Connection,
    cfg: Config,
    paths: Paths,
    budget: int | None,
    chat_id: int | None,
) -> PruneReport:
    """Connect and run :func:`grepogram.sync.prune_deleted`, which takes the sync lock itself."""
    async with tg.connected(client):
        return await sync.prune_deleted(
            client, conn, cfg, paths, sync.SyncBudget(budget), chat_id=chat_id
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
        export = tdesktop.read_export(path)
        for warning in export.warnings:
            typer.echo(f"warning: {warning}", err=True)
        entries = _retitled(export.chats, chat_title)
        embedder = _optional_embedder(cfg)
        with sync.SyncLock(paths):
            stored = _store_import(conn, cfg, entries)
            embedded = _embed_imported(conn, embedder)
    except tdesktop.ExportError as exc:
        fail(str(exc))
    except (sources.SourceError, sync.SyncInProgress) as exc:
        fail(str(exc), hint=getattr(exc, "hint", None))
    finally:
        conn.close()
    _print_import(export, stored, embedded)


def _store_import(
    conn: sqlite3.Connection, cfg: Config, entries: Sequence[tdesktop.ImportedChat]
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
        stored = sources.import_chats(conn, entries)
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
    paths, cfg, conn = _load()
    _require_api_keys(cfg, paths)
    try:
        parsed = sources.parse_target(target)
        tg.ensure_session_mode(paths)
        client = tg.make_client(cfg, paths)
        added = asyncio.run(_add_source(client, cfg, parsed, since, comments))
        # the index is opened for this one check: a chat held as a Telegram Desktop import must
        # not gain a live source, because `db.upsert_chat` overwrites `source_id` and the next
        # sync would drop the `import:` tag every protection of that history keys on
        sources.refuse_imported(conn, added.dialogs)
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
        fail(str(exc), hint=getattr(exc, "hint", None))
    finally:
        conn.close()
    typer.echo(f"removed {removed.source_id} ({len(removed.chat_ids)} chats deleted)")


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
        tg.ensure_session_mode(paths)
        client = tg.make_client(cfg, paths)
        scan = sources.prunable(cfg, conn, asyncio.run(_folder_membership(client, cfg)))
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


async def _folder_membership(client: TelegramClient, cfg: Config) -> sources.FolderMembership:
    """Connect and read what every folder source lists right now."""
    async with tg.connected(client):
        return await sources.folder_membership(cfg, dialogs.DialogCatalog(client))


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


def fail(message: str, code: int = 1, *, hint: str | None = None) -> NoReturn:
    """Print ``error: <message>`` to stderr, then ``hint: <hint>`` when there is one, and exit."""
    typer.echo(f"error: {message}", err=True)
    if hint:
        typer.echo(f"hint: {hint}", err=True)
    raise typer.Exit(code)


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
