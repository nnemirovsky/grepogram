import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, get_args

import pytest
import typer
from telethon import errors as tg_errors
from typer.testing import CliRunner

from grepogram import (
    __version__,
    cli,
    config,
    db,
    embed,
    index,
    media,
    search,
    sync,
    tg,
    units,
)
from grepogram.config import TEMPLATE
from grepogram.embed import ModelUnavailable
from grepogram.models import ChatRow, Config, MediaReport, MessageRow, PruneReport, SearchMode
from grepogram.paths import Paths
from tests.conftest import file_mode
from tests.fakes import FakeClient, make_channel, make_dialog
from tests.fixtures import chat_ru, tl

runner = CliRunner()

SAMPLE_PDF = Path(__file__).resolve().parent / "fixtures" / "sample.pdf"
EXTRACT_ID = -1000000000900


# --- app -------------------------------------------------------------------------------------


def test_cli_search_modes_mirror_the_search_mode_literal() -> None:
    """One vocabulary: the ``--mode`` enum and :data:`SearchMode` never drift apart."""
    assert sorted(mode.value for mode in cli.Mode) == sorted(get_args(SearchMode))
    assert sorted(search.MODES) == sorted(get_args(SearchMode))


def test_version_prints_package_version() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"grepogram {__version__}"


def test_version_does_not_touch_the_filesystem(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert list(tmp_home.iterdir()) == []


def test_bare_invocation_shows_usage() -> None:
    result = runner.invoke(cli.app, [])
    assert "Usage" in result.output
    assert "config" in result.output
    assert "sources" in result.output


def test_unknown_command_exits_non_zero() -> None:
    result = runner.invoke(cli.app, ["bogus"])
    assert result.exit_code != 0
    assert "bogus" in result.output


def test_sub_apps_are_registered() -> None:
    for name in ("config", "sources"):
        result = runner.invoke(cli.app, [name, "--help"])
        assert result.exit_code == 0, result.output
        assert "Usage" in result.output
    result = runner.invoke(cli.app, ["config", "--help"])
    assert "path" in result.output
    assert "init" in result.output


def test_commands_set_up_logging_to_stderr_and_file(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["config", "path"])
    assert result.exit_code == 0, result.output
    root = logging.getLogger()
    assert root.level == logging.INFO
    files = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert [Path(h.baseFilename) for h in files] == [tmp_home / "logs" / "grepogram.log"]
    assert all(getattr(h, "stream", None) is not sys.stdout for h in root.handlers)


def test_verbose_flag_lowers_level_to_debug(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["--verbose", "config", "path"])
    assert result.exit_code == 0, result.output
    assert logging.getLogger().level == logging.DEBUG
    result = runner.invoke(cli.app, ["-v", "config", "path"])
    assert result.exit_code == 0, result.output
    assert logging.getLogger().level == logging.DEBUG


# --- config path -----------------------------------------------------------------------------


def test_config_path_respects_grepogram_home(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["config", "path"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 5
    listed = {line.split(maxsplit=1)[0]: Path(line.split(maxsplit=1)[1]) for line in lines}
    assert listed == {
        "config": tmp_home / "config.toml",
        "session": tmp_home / "session.session",
        "index": tmp_home / "index.db",
        "lock": tmp_home / "sync.lock",
        "log": tmp_home / "logs" / "grepogram.log",
    }


def test_config_path_moves_with_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("GREPOGRAM_HOME", str(other))
    result = runner.invoke(cli.app, ["config", "path"])
    assert result.exit_code == 0, result.output
    assert str(other / "config.toml") in result.output
    assert str(tmp_path / "grepogram-home") not in result.output


def test_a_broken_config_prints_the_error_and_its_hint(tmp_home: Path) -> None:
    """A key a newer grepogram wrote reaches the terminal with what to do about it."""
    paths = Paths.from_env()
    paths.ensure_dirs()
    paths.config_file.write_text("[models]\nmax_seq_length_v2 = 1\n")
    result = runner.invoke(cli.app, ["sources", "ls"])
    assert result.exit_code == 1, result.output
    assert "unknown key: models.max_seq_length_v2" in result.output
    assert "hint: " in result.output and "restart" in result.output


def test_a_broken_config_without_a_hint_prints_only_the_error(tmp_home: Path) -> None:
    paths = Paths.from_env()
    paths.ensure_dirs()
    paths.config_file.write_text("[search]\nk = '10'\n")
    result = runner.invoke(cli.app, ["sources", "ls"])
    assert result.exit_code == 1, result.output
    assert "invalid value for search.k" in result.output
    assert "hint: " not in result.output


# --- config init -----------------------------------------------------------------------------


def test_config_init_writes_template_with_mode_0600(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 0, result.output
    target = tmp_home / "config.toml"
    assert target.read_text(encoding="utf-8") == TEMPLATE
    assert file_mode(target) == 0o600
    assert str(target) in result.output
    assert "my.telegram.org" in result.output
    assert config.load(Paths.from_env()) == Config()


def test_config_init_creates_missing_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "not" / "yet" / "there"
    monkeypatch.setenv("GREPOGRAM_HOME", str(home))
    result = runner.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 0, result.output
    assert (home / "config.toml").read_text(encoding="utf-8") == TEMPLATE
    assert file_mode(home) == 0o700


def test_config_init_refuses_to_overwrite(tmp_home: Path) -> None:
    target = tmp_home / "config.toml"
    target.write_text("[telegram]\napi_id = 42\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["config", "init"])
    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert str(target) in result.stderr
    assert result.stdout == ""
    assert target.read_text(encoding="utf-8") == "[telegram]\napi_id = 42\n"


# --- helpers ---------------------------------------------------------------------------------


def test_open_db_connects_and_migrates(tmp_home: Path) -> None:
    conn = cli._open_db(Paths.from_env())
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        assert db.has_table(conn, "unit_fts")
    finally:
        conn.close()
    assert (tmp_home / "index.db").exists()


def test_load_returns_paths_config_and_connection(tmp_home: Path) -> None:
    (tmp_home / "config.toml").write_text("[search]\nk = 3\n", encoding="utf-8")
    paths, cfg, conn = cli._load()
    try:
        assert paths == Paths.from_env()
        assert cfg.search.k == 3
        assert cfg.telegram == Config().telegram
        assert db.schema_version(conn) == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_load_defaults_without_config_file(tmp_home: Path) -> None:
    _, cfg, conn = cli._load()
    conn.close()
    assert cfg == Config()


def test_load_exits_on_broken_config(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_home / "config.toml").write_text("[search]\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(typer.Exit) as excinfo:
        cli._load()
    assert excinfo.value.exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "search.bogus" in captured.err


def test_load_exits_on_newer_schema(tmp_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conn = db.connect(Paths.from_env())
    db.migrate(conn)
    db.set_meta(conn, db.META_SCHEMA_VERSION, str(db.SCHEMA_VERSION + 1))
    conn.close()
    with pytest.raises(typer.Exit) as excinfo:
        cli._load()
    assert excinfo.value.exit_code == 1
    assert "newer" in capsys.readouterr().err


def test_load_exits_on_a_schema_version_it_cannot_read(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt recorded version reaches the user as the rebuild instruction, like every other
    schema refusal, and not as a traceback out of ``int()``."""
    conn = db.connect(Paths.from_env())
    db.migrate(conn)
    db.set_meta(conn, db.META_SCHEMA_VERSION, "five")
    conn.close()
    with pytest.raises(typer.Exit) as excinfo:
        cli._load()
    assert excinfo.value.exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "delete index.db and run `grepogram sync`" in captured.err


def test_load_exits_when_the_interpreter_cannot_load_extensions(
    tmp_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install under a Python without loadable extensions is a broken install, and every
    command says so with the interpreter and the reinstall command instead of a traceback."""
    monkeypatch.setattr(db, "_extensions_supported", lambda: False)
    with pytest.raises(typer.Exit) as excinfo:
        cli._load()
    assert excinfo.value.exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: this Python cannot load SQLite extensions" in captured.err
    assert sys.executable in captured.err
    assert "uv tool install --managed-python" in captured.err


def test_fail_writes_to_stderr_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as excinfo:
        cli.fail("nope", code=3)
    assert excinfo.value.exit_code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: nope\n"


# --- added by the review fixes --------------------------------------------------------------


def test_load_creates_the_home_on_a_fresh_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "none" / "yet"
    monkeypatch.setenv("GREPOGRAM_HOME", str(home))
    paths, cfg, conn = cli._load()
    try:
        assert paths.db_file.is_file() and cfg == Config()
        assert file_mode(home) == 0o700
    finally:
        conn.close()
    result = runner.invoke(cli.app, ["sources", "ls"])
    assert result.exit_code == 0, result.output
    assert "no sources configured" in result.stdout


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (1_705_314_600, 1_705_318_200, "2024-01-15 10:30–11:30 UTC"),
        (1_705_314_600, 1_705_401_000, "2024-01-15 10:30 – 2024-01-16 10:30 UTC"),
    ],
    ids=["same-day", "across-days"],
)
def test_span_formats_same_day_and_multi_day_ranges(start: int, end: int, expected: str) -> None:
    assert cli._span(start, end) == expected


def test_search_text_output_prints_the_fallback_link_for_private_chats(tmp_home: Path) -> None:
    paths = Paths.from_env()
    conn = db.connect(paths)
    try:
        db.migrate(conn)
        chat = db.upsert_chat(conn, ChatRow(id=7, type="user", title="Bob", source_id="chat:7"))
        ids = db.upsert_messages(
            conn, [MessageRow(chat_id=7, msg_id=9, date=1_705_314_600, text="hello there")]
        )
        delta = units.rebuild_for_chat(conn, chat, Config(), ids)
        index.index_chat(conn, chat, ids, delta)
    finally:
        conn.close()
    result = runner.invoke(cli.app, ["search", "hello", "--mode", "lexical", "--no-rerank"])
    assert result.exit_code == 0, result.output
    assert "   tg://openmessage?user_id=7&message_id=9" in result.stdout
    assert "   fallback: tg://user?id=7" in result.stdout


# --- thread and context ----------------------------------------------------------------------

NEWS = -1001000000300
NEWS_CHAT = -1001000000301


def _seed_chat_ru() -> None:
    """The bilingual fixture corpus in the ``tmp_home`` index, as a sync would have left it."""
    conn = db.connect(Paths.from_env())
    try:
        db.migrate(conn)
        chat_ru.load(conn)
    finally:
        conn.close()


def _seed_channel_with_a_comment() -> None:
    """A channel post and one comment on it in the linked discussion group, both numbered 1:
    the collision the per-message ``chat_id`` exists for."""
    conn = db.connect(Paths.from_env())
    try:
        db.migrate(conn)
        db.upsert_chat(
            conn,
            ChatRow(id=NEWS, type="channel", title="News", username="news", source_id="chat:@news"),
        )
        db.upsert_chat(
            conn,
            ChatRow(
                id=NEWS_CHAT,
                type="supergroup",
                title="News chat",
                source_id="chat:@news",
                discussion_of=NEWS,
            ),
        )
        db.upsert_messages(
            conn,
            [
                MessageRow(
                    chat_id=NEWS,
                    msg_id=1,
                    date=1_700_000_000,
                    from_name="News",
                    text="Announcing the new office hours",
                ),
                MessageRow(
                    chat_id=NEWS_CHAT,
                    msg_id=1,
                    date=1_700_000_060,
                    from_name="Bob",
                    comment_of_chat_id=NEWS,
                    comment_of_msg_id=1,
                    text="Which branch keeps them?",
                ),
            ],
        )
    finally:
        conn.close()


def test_thread_prints_one_block_per_message_of_the_reply_chain(tmp_home: Path) -> None:
    _seed_chat_ru()
    result = runner.invoke(cli.app, ["thread", "@arg_chat", "5"])
    assert result.exit_code == 0, result.output
    blocks = result.stdout.strip().split("\n\n")
    assert len(blocks) == 8
    first = blocks[0].splitlines()
    assert first[0] == f"1. {chat_ru.ARG_ID}/1  2024-01-15 10:00 UTC  Ольга"
    assert first[1] == "   https://t.me/arg_chat/1"
    assert first[2] == f"   {chat_ru.message(chat_ru.ARG_ID, 1).text}"
    assert blocks[-1].splitlines()[0].startswith(f"8. {chat_ru.ARG_ID}/10  ")


def test_context_prints_the_messages_around_one(tmp_home: Path) -> None:
    _seed_chat_ru()
    result = runner.invoke(cli.app, ["context", "@arg_chat", "5", "--before", "1", "--after", "1"])
    assert result.exit_code == 0, result.output
    urls = [line.strip() for line in result.stdout.splitlines() if line.startswith("   https")]
    assert urls == [f"https://t.me/arg_chat/{n}" for n in (4, 5, 6)]
    alone = runner.invoke(cli.app, ["context", "@arg_chat", "5", "--before", "0", "--after", "0"])
    assert alone.exit_code == 0, alone.output
    assert alone.stdout.splitlines()[0] == f"1. {chat_ru.ARG_ID}/5  2024-01-15 10:07 UTC  Alice"


def test_context_refuses_a_negative_count_before_it_reaches_the_reader(tmp_home: Path) -> None:
    _seed_chat_ru()
    result = runner.invoke(cli.app, ["context", "@arg_chat", "5", "--before", "-1"])
    assert result.exit_code == 2
    assert "--before" in result.output


def test_thread_json_prints_the_mcp_document_and_nothing_else(tmp_home: Path) -> None:
    _seed_chat_ru()
    result = runner.invoke(cli.app, ["thread", "@arg_chat", "5", "--json"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert set(document) == {"chat_id", "msg_id", "messages"}
    assert document["chat_id"] == chat_ru.ARG_ID and document["msg_id"] == 5
    assert [m["msg_id"] for m in document["messages"]] == [1, 2, 3, 4, 5, 6, 7, 10]
    assert set(document["messages"][0]) == {
        "chat_id",
        "msg_id",
        "date",
        "from_name",
        "text",
        "url",
        "fallback_url",
        "reply_to_msg_id",
    }


def test_thread_of_a_channel_post_names_the_chat_of_every_comment(tmp_home: Path) -> None:
    """The comments come from the discussion group, whose message ids number from 1 exactly as
    the channel's posts do: only the per-message ``chat_id`` leads back to them, and both output
    forms carry it."""
    _seed_channel_with_a_comment()
    result = runner.invoke(cli.app, ["thread", "@news", "1", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["chat_id"] == NEWS and document["msg_id"] == 1
    messages = document["messages"]
    assert [(m["chat_id"], m["msg_id"]) for m in messages] == [(NEWS, 1), (NEWS_CHAT, 1)]
    assert messages[1]["url"] == "https://t.me/c/1000000301/1"
    text = runner.invoke(cli.app, ["thread", "@news", "1"])
    assert text.exit_code == 0, text.output
    headers = [line for line in text.stdout.splitlines() if not line.startswith(" ")]
    assert headers == [
        f"1. {NEWS}/1  2023-11-14 22:13 UTC  News",
        "",
        f"2. {NEWS_CHAT}/1  2023-11-14 22:14 UTC  Bob",
    ]


def test_thread_and_context_read_a_chat_by_id_and_by_title(tmp_home: Path) -> None:
    _seed_chat_ru()
    by_id = runner.invoke(cli.app, ["context", "--", str(chat_ru.GEO_ID), "3"])
    assert by_id.exit_code == 0, by_id.output
    assert f"   https://t.me/c/{abs(chat_ru.GEO_ID) - 1000000000000}/3" in by_id.stdout
    by_title = runner.invoke(cli.app, ["thread", "Georgia", "3"])
    assert by_title.exit_code == 0, by_title.output
    assert by_title.stdout.splitlines()[0].startswith(f"1. {chat_ru.GEO_ID}/")


@pytest.mark.parametrize("command", ["thread", "context"])
def test_readers_exit_1_on_an_unknown_chat(tmp_home: Path, command: str) -> None:
    _seed_chat_ru()
    result = runner.invoke(cli.app, [command, "@nobody", "5"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: no indexed chat matches '@nobody'")


@pytest.mark.parametrize("command", ["thread", "context"])
def test_readers_exit_1_on_a_chat_spec_naming_several(tmp_home: Path, command: str) -> None:
    _seed_chat_ru()
    result = runner.invoke(cli.app, [command, "chat", "5"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "error: 'chat' matches several indexed chats" in result.stderr
    assert "name one of them" in result.stderr


@pytest.mark.parametrize("command", ["thread", "context"])
def test_readers_exit_1_on_an_unknown_message(tmp_home: Path, command: str) -> None:
    _seed_chat_ru()
    result = runner.invoke(cli.app, [command, "@arg_chat", "9999"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (f"error: message 9999 of chat {chat_ru.ARG_ID} is not indexed")


# --- extract ---------------------------------------------------------------------------------


EXTRACT_KEYS = '[telegram]\napi_id = 12345\napi_hash = "fakehash"\n'


def _signed_in(tmp_home: Path, extra: str = "") -> Paths:
    (tmp_home / "config.toml").write_text(EXTRACT_KEYS + extra, encoding="utf-8")
    paths = Paths.from_env()
    paths.session_file.touch()
    return paths


def _extract_chat(paths: Paths) -> FakeClient:
    """One indexed chat holding one pending PDF, and the client that answers for it.

    Through ``on_chat_synced``, so the chat carries units: a chat whose rows were stored and
    never cut is what ``media._recut`` keeps the ``indexed`` flag raised for.
    """
    conn = db.connect(paths)
    db.migrate(conn)
    chat = db.upsert_chat(
        conn, ChatRow(id=EXTRACT_ID, type="supergroup", title="Chat", source_id="x")
    )
    ids = db.upsert_messages(
        conn,
        [
            MessageRow(
                chat_id=EXTRACT_ID,
                msg_id=1,
                date=1_700_000_000,
                media_kind="document",
                media_filename="note.pdf",
            )
        ],
    )
    sync.on_chat_synced(conn, chat, Config(), ids)
    conn.close()
    return FakeClient(
        dialogs=[make_dialog(make_channel(900, "Chat", megagroup=True))],
        messages={EXTRACT_ID: [tl.document_message(EXTRACT_ID, 1, "note.pdf")]},
        downloads={(EXTRACT_ID, 1): SAMPLE_PDF.read_bytes()},
    )


def test_extract_reads_the_media_and_reports_what_it_did(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _signed_in(tmp_home)
    client = _extract_chat(paths)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: client)
    result = runner.invoke(cli.app, ["extract", "--budget", "30"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "media read: 1"
    assert any("grepogram sync" in line for line in lines)
    assert client.calls[-1] == ("disconnect", {})
    conn = db.connect(paths)
    row = conn.execute("SELECT extracted_text, media_state, indexed FROM messages").fetchone()
    conn.close()
    assert row["media_state"] == db.MEDIA_EXTRACTED
    assert row["indexed"] == 1, "the pass rebuilds the row it flagged, in the same transaction"
    assert "sample pdf" in str(row["extracted_text"]).lower()


def test_extract_passes_retry_failed_through(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)
    seen: dict[str, object] = {}

    async def record(
        conn: object, client: object, cfg: object, budget: object, **kw: object
    ) -> Any:
        seen.update(kw)
        seen["seconds"] = budget.seconds  # type: ignore[attr-defined]
        return MediaReport(unsupported=3, remaining=2, warnings=["careful"])

    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: FakeClient())
    monkeypatch.setattr(media, "run", record)
    result = runner.invoke(cli.app, ["extract", "--retry-failed", "--budget", "7"])
    assert result.exit_code == 0, result.output
    assert seen == {"retry_failed": True, "seconds": 7}
    assert "no extractor here: 3" in result.stdout
    assert "media pending: 2; run extract again" in result.stdout
    assert "warning: careful" in result.stderr


def test_extract_reports_unreachable_media_apart_from_the_queue(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Media in an imported or unavailable chat sits at pending for good, so it is never what
    "run extract again" is offered for: that line is the pass's only completion signal, and a
    script looping until it stops appearing would never stop."""
    _signed_in(tmp_home)

    async def parked(
        conn: object, client: object, cfg: object, budget: object, **kw: object
    ) -> Any:
        return MediaReport(unreachable=4)

    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: FakeClient())
    monkeypatch.setattr(media, "run", parked)
    result = runner.invoke(cli.app, ["extract"])
    assert result.exit_code == 0, result.output
    assert "in chats nothing can re-fetch: 4" in result.stdout
    assert "run extract again" not in result.stdout


def test_extract_without_api_keys_is_a_clean_error(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["extract"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "api_id and api_hash are not set" in result.stderr


def test_extract_without_a_session_is_a_clean_error(tmp_home: Path) -> None:
    (tmp_home / "config.toml").write_text(EXTRACT_KEYS, encoding="utf-8")
    result = runner.invoke(cli.app, ["extract"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "grepogram auth" in result.stderr


def test_extract_reports_a_held_sync_lock_as_a_clean_error(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _signed_in(tmp_home)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: FakeClient())
    with sync.SyncLock(paths):
        result = runner.invoke(cli.app, ["extract"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "another sync is running" in result.stderr


def test_extract_reports_a_telegram_error_as_a_clean_error(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)

    def broken(cfg: object, paths: object) -> FakeClient:
        raise tg_errors.RPCError(request=None, message="nope")

    monkeypatch.setattr(tg, "make_client", broken)
    result = runner.invoke(cli.app, ["extract"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "telegram error:" in result.stderr


# --- prune-deleted ---------------------------------------------------------------------------


PRUNE_ID = -1000000000901


def _prune_chat(paths: Paths) -> FakeClient:
    """One indexed chat of two messages, and a client that has lost the second of them."""
    conn = db.connect(paths)
    db.migrate(conn)
    db.upsert_chat(conn, ChatRow(id=PRUNE_ID, type="supergroup", title="Chat", source_id="x"))
    db.upsert_messages(
        conn,
        [
            MessageRow(chat_id=PRUNE_ID, msg_id=101, date=1_700_000_000, text="kept"),
            MessageRow(chat_id=PRUNE_ID, msg_id=102, date=1_700_000_060, text="deleted"),
        ],
    )
    conn.close()
    return FakeClient(
        dialogs=[make_dialog(make_channel(901, "Chat", megagroup=True))],
        messages={PRUNE_ID: [tl.message(PRUNE_ID, 101, "kept", sender=1)]},
    )


def test_prune_deleted_removes_what_telegram_no_longer_has(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _signed_in(tmp_home)
    client = _prune_chat(paths)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: client)
    result = runner.invoke(cli.app, ["prune-deleted", "--budget", "30"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "messages removed: 1"
    assert lines[1] == "messages checked: 2"
    assert lines[2] == "chats swept: 1"
    assert client.calls[-1] == ("disconnect", {})
    conn = db.connect(paths)
    stored = [row["msg_id"] for row in conn.execute("SELECT msg_id FROM messages")]
    conn.close()
    assert stored == [101]


def test_prune_deleted_passes_the_chat_and_the_budget_through(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _signed_in(tmp_home)
    _prune_chat(paths)
    seen: dict[str, object] = {}

    async def record(
        client: object, conn: object, cfg: object, paths: object, budget: Any, **kw: Any
    ) -> PruneReport:
        seen.update(kw)
        seen["seconds"] = budget.seconds
        return PruneReport(removed=0, checked=4, chats_remaining=[PRUNE_ID], warnings=["careful"])

    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: FakeClient())
    monkeypatch.setattr(sync, "prune_deleted", record)
    result = runner.invoke(cli.app, ["prune-deleted", "--chat", str(PRUNE_ID), "--budget", "7"])
    assert result.exit_code == 0, result.output
    assert seen == {"chat_id": PRUNE_ID, "seconds": 7}
    assert f"chats not finished: 1 ({PRUNE_ID})" in result.stdout
    assert "warning: careful" in result.stderr


def test_prune_deleted_with_an_unknown_chat_is_a_clean_error(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _signed_in(tmp_home)
    _prune_chat(paths)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: FakeClient())
    result = runner.invoke(cli.app, ["prune-deleted", "--chat", "@nowhere"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "@nowhere" in result.stderr


def test_prune_deleted_without_api_keys_is_a_clean_error(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["prune-deleted"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "api_id and api_hash are not set" in result.stderr


def test_prune_deleted_reports_a_held_sync_lock_as_a_clean_error(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _signed_in(tmp_home)
    _prune_chat(paths)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: FakeClient())
    with sync.SyncLock(paths):
        result = runner.invoke(cli.app, ["prune-deleted"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "another sync is running" in result.stderr


def test_prune_deleted_reports_a_telegram_error_as_a_clean_error(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)

    def broken(cfg: object, paths: object) -> FakeClient:
        raise tg_errors.RPCError(request=None, message="nope")

    monkeypatch.setattr(tg, "make_client", broken)
    result = runner.invoke(cli.app, ["prune-deleted"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "telegram error:" in result.stderr


# --- import ----------------------------------------------------------------------------------


EXPORT = Path(__file__).resolve().parent / "fixtures" / "tdesktop_export.json"
EXPATS_ID = -1001234567890
BOAT_ID = -987654


def _single_chat_export(tmp_path: Path, **over: Any) -> Path:
    """A one-chat ``messages.json``, the shape Telegram Desktop writes for a single export."""
    entry: dict[str, Any] = {
        "name": "Old group",
        "type": "private_group",
        "id": 555,
        "messages": [
            {
                "id": 1,
                "type": "message",
                "date": "2024-03-01T09:00:00",
                "date_unixtime": "1709283600",
                "from": "Nina",
                "from_id": "user777000",
                "text": "cita previa extranjeria",
            }
        ],
    }
    entry.update(over)
    directory = tmp_path / "export"
    directory.mkdir(exist_ok=True)
    (directory / "messages.json").write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
    return directory


def _chats(paths: Paths) -> dict[int, ChatRow]:
    conn = db.connect(paths)
    try:
        return {chat.id: chat for chat in db.list_chats(conn)}
    finally:
        conn.close()


def test_import_stores_a_searchable_chat_tagged_as_an_import(tmp_home: Path) -> None:
    """The whole point: an export the account can no longer open answers `search` at once, and
    the chat carries the `import:` tag every protection of that history keys on."""
    result = runner.invoke(cli.app, ["import", str(EXPORT)])
    assert result.exit_code == 0, result.output
    assert "imported 6 messages into 2 chats" in result.stdout
    assert "service messages skipped: 1" in result.stdout
    assert "entries that could not be read: 1" in result.stdout
    assert "embedded 3 units" in result.stdout
    chats = _chats(Paths.from_env())
    assert chats[EXPATS_ID].source_id == "import:valencia-expats"
    assert chats[BOAT_ID].source_id == "import:двое-в-лодке"
    # nothing was fetched from Telegram, so no sync may ever resume from these rows
    assert all(chat.unavailable and chat.last_msg_id == 0 for chat in chats.values())
    found = runner.invoke(cli.app, ["search", "ВНЖ", "--mode", "lexical", "--no-rerank"])
    assert found.exit_code == 0, found.output
    assert "Valencia Expats" in found.stdout
    assert f"https://t.me/c/{abs(EXPATS_ID) - 1000000000000}/2" in found.stdout


def test_import_is_idempotent(tmp_home: Path) -> None:
    """Re-running an import is how a partial one is finished: the ids come from the export, so
    the second run updates the same rows instead of storing a second copy."""
    first = runner.invoke(cli.app, ["import", str(EXPORT)])
    assert first.exit_code == 0, first.output
    second = runner.invoke(cli.app, ["import", str(EXPORT)])
    assert second.exit_code == 0, second.output
    assert "imported 6 messages into 2 chats" in second.stdout
    # the units were already embedded, so the second run has nothing left to embed
    assert "embedded 0 units" in second.stdout
    conn = db.connect(Paths.from_env())
    try:
        assert db.message_counts(conn) == {EXPATS_ID: 3, BOAT_ID: 3}
        assert [chat.source_id for chat in db.list_chats(conn)] == [
            "import:valencia-expats",
            "import:двое-в-лодке",
        ]
    finally:
        conn.close()


def test_import_over_a_chat_synced_from_telegram_is_refused_by_name(tmp_home: Path) -> None:
    """`db.upsert_chat` overwrites `source_id`, so an import over a live chat would retag it and
    hide it from the source that fetches it."""
    paths = Paths.from_env()
    conn = db.connect(paths)
    db.migrate(conn)
    db.upsert_chat(
        conn, ChatRow(id=EXPATS_ID, type="supergroup", title="Live", source_id="folder:Spain")
    )
    conn.close()
    result = runner.invoke(cli.app, ["import", str(EXPORT)])
    assert result.exit_code == 1
    assert "already indexed from Telegram through folder:Spain" in result.stderr
    # refused as a whole, before a row of any chat of the export was written
    assert _chats(paths)[EXPATS_ID].source_id == "folder:Spain"
    conn = db.connect(paths)
    try:
        assert db.message_counts(conn) == {}
    finally:
        conn.close()


def test_import_applies_chat_title_to_a_single_chat_export(tmp_home: Path) -> None:
    directory = _single_chat_export(tmp_path=tmp_home, name=None)
    result = runner.invoke(cli.app, ["import", str(directory), "--chat-title", "Пикник 2019"])
    assert result.exit_code == 0, result.output
    stored = _chats(Paths.from_env())[-555]
    assert stored.title == "Пикник 2019"
    assert stored.source_id == "import:пикник-2019"


def test_import_refuses_chat_title_for_a_multi_chat_export(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["import", str(EXPORT), "--chat-title", "One"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "--chat-title names one chat and this export holds 2" in result.stderr
    assert _chats(Paths.from_env()) == {}


def test_import_of_a_directory_without_an_export_is_a_clean_error(tmp_home: Path) -> None:
    empty = tmp_home / "elsewhere"
    empty.mkdir()
    result = runner.invoke(cli.app, ["import", str(empty)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "holds no result.json or messages.json" in result.stderr


def test_import_of_an_export_with_no_readable_chat_is_a_clean_error(tmp_home: Path) -> None:
    directory = _single_chat_export(tmp_path=tmp_home, type="channel_of_the_future")
    result = runner.invoke(cli.app, ["import", str(directory)])
    assert result.exit_code == 1
    assert "unknown export type" in result.stderr
    assert "the export holds no chat this version can read" in result.stderr


def test_import_reports_a_held_sync_lock_as_a_clean_error(tmp_home: Path) -> None:
    paths = Paths.from_env()
    with sync.SyncLock(paths):
        result = runner.invoke(cli.app, ["import", str(EXPORT)])
    assert result.exit_code == 1
    assert "another sync is running" in result.stderr
    assert _chats(paths) == {}


def test_import_that_fails_to_index_leaves_nothing_behind(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows and the units they are cut into are one transaction, all of it or none.

    ``import_chats`` used to commit on its own with the rebuild coming after, so anything the
    rebuild could not do left the chats stored with ``indexed = 0`` and the command ending in a
    traceback — and that is not a failed import a user can retry: every later `sync` reaches the
    chat again through ``index_stranded`` and fails the same way, which takes the MCP `sync`
    tool and every `search` old enough to auto-sync down with it.
    """
    paths = Paths.from_env()

    def refuse(*_: object, **__: object) -> None:
        raise ValueError("year 3170843 is out of range")

    monkeypatch.setattr(sync, "on_chat_synced", refuse)
    with pytest.raises(ValueError, match="out of range"):
        runner.invoke(cli.app, ["import", str(EXPORT)], catch_exceptions=False)

    assert _chats(paths) == {}
    conn = db.connect(paths)
    try:
        assert db.chats_with_unindexed(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    finally:
        conn.close()


def test_import_keeps_the_messages_when_the_dense_index_was_built_elsewhere(
    tmp_home: Path,
) -> None:
    """A model change is the embedding step's problem, never the import's: the export is stored
    and searchable lexically, and the mismatch is a warning naming the way out."""
    conn = db.connect(Paths.from_env())
    db.migrate(conn)
    db.set_meta(conn, db.META_EMBED_MODEL, "some-other-model")
    conn.close()
    result = runner.invoke(cli.app, ["import", str(EXPORT)])
    assert result.exit_code == 0, result.output
    assert "warning: dense index not updated:" in result.stderr
    assert "embed --reembed" in result.stderr
    assert "next: grepogram embed" in result.stdout
    assert _chats(Paths.from_env())[EXPATS_ID].source_id == "import:valencia-expats"


def test_import_without_an_embedding_model_says_what_finishes_the_job(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import is offline and its messages are searchable lexically the moment they land, so a
    missing model is a warning and the chat is still imported."""

    def unavailable(cfg: object) -> Any:
        raise ModelUnavailable("no torch here")

    monkeypatch.setattr(embed, "load_embedder", unavailable)
    result = runner.invoke(cli.app, ["import", str(EXPORT)])
    assert result.exit_code == 0, result.output
    assert "warning: dense index not updated: no torch here" in result.stderr
    assert "next: grepogram embed" in result.stdout
    assert _chats(Paths.from_env())[EXPATS_ID].source_id == "import:valencia-expats"
