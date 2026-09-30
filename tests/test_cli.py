import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, get_args

import pytest
import typer
from telethon import errors as tg_errors
from telethon import functions, types
from typer.testing import CliRunner

from grepogram import (
    __version__,
    cli,
    config,
    db,
    embed,
    index,
    media,
    research,
    research_db,
    search,
    sync,
    tg,
    units,
)
from grepogram.config import TEMPLATE
from grepogram.embed import ModelUnavailable
from grepogram.models import (
    DEFAULT_ACCOUNT,
    ApprovalItem,
    ChatRow,
    Config,
    MediaReport,
    MessageRow,
    PruneReport,
    SearchMode,
)
from grepogram.paths import Paths
from tests.conftest import file_mode
from tests.fakes import (
    FakeClient,
    FakeWorld,
    make_channel,
    make_dialog,
    make_group,
    make_user,
    no_discussion,
)
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
    monkeypatch.setattr(tg, "make_client", lambda *_: client)
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

    monkeypatch.setattr(tg, "make_client", lambda *_: FakeClient())
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
        return MediaReport(unreachable=4, chats_unreachable=[77])

    monkeypatch.setattr(tg, "make_client", lambda *_: FakeClient())
    monkeypatch.setattr(media, "run", parked)
    result = runner.invoke(cli.app, ["extract"])
    assert result.exit_code == 0, result.output
    assert "in chats nothing can re-fetch: 4" in result.stdout
    assert "chats no signed-in account reaches: 1 (77)" in result.stdout
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
    monkeypatch.setattr(tg, "make_client", lambda *_: FakeClient())
    with sync.SyncLock(paths):
        result = runner.invoke(cli.app, ["extract"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "another sync is running" in result.stderr


def test_extract_reports_a_telegram_error_as_a_clean_error(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)

    def broken(*_: object) -> FakeClient:
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
    monkeypatch.setattr(tg, "make_client", lambda *_: client)
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
        return PruneReport(
            removed=0,
            checked=4,
            chats_remaining=[PRUNE_ID],
            chats_unreachable=[77],
            warnings=["careful"],
        )

    monkeypatch.setattr(tg, "make_client", lambda *_: FakeClient())
    monkeypatch.setattr(sync, "prune_deleted", record)
    result = runner.invoke(cli.app, ["prune-deleted", "--chat", str(PRUNE_ID), "--budget", "7"])
    assert result.exit_code == 0, result.output
    assert seen == {"chat_id": PRUNE_ID, "seconds": 7}
    assert f"chats not finished: 1 ({PRUNE_ID})" in result.stdout
    assert "chats no signed-in account reaches: 1 (77)" in result.stdout
    assert "warning: careful" in result.stderr


def test_prune_deleted_with_an_unknown_chat_is_a_clean_error(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _signed_in(tmp_home)
    _prune_chat(paths)
    monkeypatch.setattr(tg, "make_client", lambda *_: FakeClient())
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
    monkeypatch.setattr(tg, "make_client", lambda *_: FakeClient())
    with sync.SyncLock(paths):
        result = runner.invoke(cli.app, ["prune-deleted"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "another sync is running" in result.stderr


def test_prune_deleted_reports_a_telegram_error_as_a_clean_error(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)

    def broken(*_: object) -> FakeClient:
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


# --- accounts --------------------------------------------------------------------------------


WORK = "work"
TWO_ACCOUNTS = EXTRACT_KEYS + '\n[[accounts]]\nname = "work"\nlabel = "work phone"\n'
NEWS_ENTITY = make_channel(700, "News", username="news")
NEWS_PEER = -1000000000700
CLUB = make_group(710, "Old club")
CLUB_PEER = -710
BOB = make_user(2, "Bob")
HOME_ME = make_user(42, "Me")
WORK_ME = make_user(43, "Worker")


class Terminal:
    """The controlling terminal as a test answers it: ``answer`` is what the human types, and
    everything the command asks is kept in ``asked``."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked: list[str] = []
        self.closed = False

    def write(self, text: str) -> int:
        self.asked.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        return self.answer

    def __enter__(self) -> "Terminal":
        return self

    def __exit__(self, *_: object) -> None:
        self.closed = True


def _answer(monkeypatch: pytest.MonkeyPatch, answer: str) -> Terminal:
    terminal = Terminal(answer)
    monkeypatch.setattr(cli, "_open_terminal", lambda: terminal)
    return terminal


def _two_account_home(tmp_home: Path, sources_toml: str = "") -> Paths:
    (tmp_home / "config.toml").write_text(TWO_ACCOUNTS + sources_toml, encoding="utf-8")
    paths = Paths.from_env()
    paths.ensure_dirs()
    paths.session_file.touch()
    paths.session_file_for(WORK).touch()
    return paths


def _two_clients(**work_kwargs: Any) -> dict[str, FakeClient]:
    """The default account and ``work``: both see the public channel ``@news``, and ``work``
    alone has a private chat with Bob."""
    world = FakeWorld(
        entities=[NEWS_ENTITY, BOB],
        messages={NEWS_PEER: [tl.channel_post(NEWS_PEER, i, f"news {i}") for i in (1, 2)]},
    )
    home = world.client(members=[NEWS_ENTITY], me=HOME_ME)
    work = world.client(
        WORK,
        members=[NEWS_ENTITY, BOB],
        me=WORK_ME,
        messages={2: [tl.message(2, 5, "bob at work", sender=2)]},
        **work_kwargs,
    )
    return {DEFAULT_ACCOUNT: home, WORK: work}


def _per_account(monkeypatch: pytest.MonkeyPatch, clients: dict[str, FakeClient]) -> None:
    monkeypatch.setattr(
        tg, "make_client", lambda cfg, paths, account=DEFAULT_ACCOUNT: clients[account]
    )


SHARED_SOURCES = (
    '\n[[sources]]\nchat = "@news"\n'
    '\n[[sources]]\nchat = "@news"\naccount = "work"\n'
    '\n[[sources]]\nchat = 2\naccount = "work"\n'
)


def _synced_two_accounts(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    """Both accounts synced into one index through the CLI: the channel both cover, and the
    work account's private chat with Bob."""
    paths = _two_account_home(tmp_home, SHARED_SOURCES)
    _per_account(monkeypatch, _two_clients())
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    return paths


def test_auth_signs_in_a_second_account_and_records_it(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_home / "config.toml").write_text(EXTRACT_KEYS, encoding="utf-8")
    fake = FakeClient(authorized=False, me=make_user(43, "Worker", "Bee"))
    seen: list[str] = []

    def login_client(cfg: Config, paths: Paths, account: str) -> FakeClient:
        seen.append(account)
        return fake

    monkeypatch.setattr(tg, "make_login_client", login_client)
    result = runner.invoke(
        cli.app, ["auth", "--account", WORK, "--label", "work phone"], input="+15550001111\n4242\n"
    )
    assert result.exit_code == 0, result.output
    assert seen == [WORK]
    assert "signed in as Worker Bee (account work)" in result.stdout
    session = tmp_home / "sessions" / "work.session"
    assert str(session) in result.stdout
    assert file_mode(session) == 0o600 and file_mode(tmp_home / "sessions") == 0o700
    assert not (tmp_home / "session.session").exists()
    assert fake.start_inputs == {"phone": "+15550001111", "code": "4242"}
    loaded = config.load(Paths.from_env())
    assert [(a.name, a.label) for a in loaded.accounts] == [(WORK, "work phone")]
    conn = db.connect(Paths.from_env())
    try:
        [row] = db.list_accounts(conn)
    finally:
        conn.close()
    assert (row.name, row.user_id, row.display_name) == (WORK, 43, "Worker Bee")
    again = runner.invoke(cli.app, ["auth", "--account", WORK], input="")
    assert again.exit_code == 0, again.output
    assert [(a.name, a.label) for a in config.load(Paths.from_env()).accounts] == [
        (WORK, "work phone")
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["auth", "--account", "Work!"], "invalid account name 'Work!'"),
        (["auth", "--label", "mine"], "the default account has no [[accounts]] entry"),
    ],
    ids=["bad-name", "label-on-default"],
)
def test_auth_refuses_a_name_it_cannot_store_before_signing_in(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], message: str
) -> None:
    (tmp_home / "config.toml").write_text(EXTRACT_KEYS, encoding="utf-8")

    def never(*_: object) -> FakeClient:
        raise AssertionError("no sign-in for a refused account")

    monkeypatch.setattr(tg, "make_login_client", never)
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert message in result.stderr
    assert not (tmp_home / "sessions").exists() or not any((tmp_home / "sessions").iterdir())
    assert config.load(Paths.from_env()).accounts == []


def test_auth_of_a_failed_second_account_adds_no_entry(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_home / "config.toml").write_text(EXTRACT_KEYS, encoding="utf-8")
    fake = FakeClient(authorized=False)

    async def failing_start(*_: object, **__: object) -> FakeClient:
        raise tg_errors.PhoneNumberInvalidError(request=None)

    monkeypatch.setattr(fake, "start", failing_start)
    monkeypatch.setattr(tg, "make_login_client", lambda *_: fake)
    result = runner.invoke(cli.app, ["auth", "--account", WORK], input="+1\n")
    assert result.exit_code == 1
    assert "sign-in failed" in result.stderr
    assert config.load(Paths.from_env()).accounts == []


def test_sync_uses_every_signed_in_account(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _two_account_home(tmp_home, SHARED_SOURCES)
    clients = _two_clients()
    _per_account(monkeypatch, clients)
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "new messages: 3" in result.stdout
    assert all(("disconnect", {}) in client.calls for client in clients.values())
    conn = db.connect(paths)
    try:
        bob = db.get_chat_by_peer(conn, 2, WORK)
        assert bob is not None
        assert db.message_counts(conn) == {NEWS_PEER: 2, bob.id: 1}
        assert db.chat_accounts(conn, NEWS_PEER) == [DEFAULT_ACCOUNT, WORK]
        assert [row.name for row in db.list_accounts(conn)] == [DEFAULT_ACCOUNT, WORK]
    finally:
        conn.close()


def test_sync_goes_on_without_an_account_that_is_not_signed_in(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second account whose session is missing — or that Telegram signed out — costs its own
    sources a warning, and the default account's sync still runs."""
    paths = _two_account_home(tmp_home, SHARED_SOURCES)
    paths.session_file_for(WORK).unlink()
    clients = _two_clients()
    _per_account(monkeypatch, clients)
    missing = runner.invoke(cli.app, ["sync"])
    assert missing.exit_code == 0, missing.output
    assert "warning: account work: no Telegram session" in missing.stderr
    assert "grepogram auth --account work" in missing.stderr
    assert "new messages: 2" in missing.stdout

    paths.session_file_for(WORK).touch()
    _per_account(monkeypatch, _two_clients(authorized=False))
    refused = runner.invoke(cli.app, ["sync"])
    assert refused.exit_code == 0, refused.output
    assert "warning: account work: Telegram session is not authorized" in refused.stderr


def test_sync_with_no_signed_in_account_names_the_one_to_sign_in(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _two_account_home(tmp_home, '\n[[sources]]\nchat = 2\naccount = "work"\n')
    paths.session_file.unlink()
    paths.session_file_for(WORK).unlink()
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 1
    assert "hint: run: grepogram auth --account work" in result.stderr


def test_extract_and_prune_deleted_hand_every_account_to_the_pass(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_account_home(tmp_home)
    _per_account(monkeypatch, _two_clients())
    seen: list[list[str]] = []

    async def extract(conn: object, clients: Any, *_: object, **__: object) -> MediaReport:
        seen.append(sorted(clients))
        return MediaReport()

    async def prune(clients: Any, *_: object, **__: object) -> PruneReport:
        seen.append(sorted(clients))
        return PruneReport(removed=0, checked=0)

    monkeypatch.setattr(media, "run", extract)
    monkeypatch.setattr(sync, "prune_deleted", prune)
    assert runner.invoke(cli.app, ["extract"]).exit_code == 0
    assert runner.invoke(cli.app, ["prune-deleted"]).exit_code == 0
    assert seen == [[DEFAULT_ACCOUNT, WORK], [DEFAULT_ACCOUNT, WORK]]


def test_dialogs_reads_the_named_account(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _two_account_home(tmp_home)
    clients = _two_clients()
    _per_account(monkeypatch, clients)
    result = runner.invoke(cli.app, ["dialogs", "bob", "--account", WORK])
    assert result.exit_code == 0, result.output
    assert "Bob" in result.stdout
    assert clients[DEFAULT_ACCOUNT].calls == []
    home = runner.invoke(cli.app, ["dialogs", "bob"])
    assert "no dialogs or folders match 'bob'" in home.stdout


def test_an_unknown_account_is_refused_by_name(tmp_home: Path) -> None:
    _two_account_home(tmp_home)
    for args in (
        ["dialogs", "x", "--account", "home"],
        ["sources", "add", "@xyz_chat", "-a", "home"],
    ):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1
        assert "unknown account 'home'; known: default, work" in result.stderr
        assert "grepogram auth --account home" in result.stderr


def test_sources_add_for_an_account_writes_its_source(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_account_home(tmp_home)
    clients = _two_clients()
    _per_account(monkeypatch, clients)
    result = runner.invoke(cli.app, ["sources", "add", "2", "--account", WORK])
    assert result.exit_code == 0, result.output
    assert "added work/chat:2: user 'Bob' (id 2)" in result.stdout
    prefixed = runner.invoke(cli.app, ["sources", "add", "work/chat:@news"])
    assert prefixed.exit_code == 0, prefixed.output
    assert clients[DEFAULT_ACCOUNT].calls == []
    loaded = config.load(Paths.from_env())
    assert [(s.id, s.account) for s in loaded.sources] == [
        ("work/chat:2", WORK),
        ("work/chat:@news", WORK),
    ]
    listed = runner.invoke(cli.app, ["sources", "ls"])
    assert [line.split()[:2] for line in listed.stdout.splitlines()[1:]] == [
        [WORK, "work/chat:2"],
        [WORK, "work/chat:@news"],
    ]


def test_import_for_an_account_stores_its_private_chat_beside_the_default_one(
    tmp_home: Path,
) -> None:
    """The same person's exported chat from two accounts is two histories: the second import
    takes a row of its own and runs again without a second copy."""
    _two_account_home(tmp_home)
    directory = _single_chat_export(tmp_home, name="Nina", type="personal_chat", id=777)
    home = runner.invoke(cli.app, ["import", str(directory)])
    assert home.exit_code == 0, home.output
    for _ in range(2):
        work = runner.invoke(cli.app, ["import", str(directory), "--account", WORK])
        assert work.exit_code == 0, work.output
    conn = db.connect(Paths.from_env())
    try:
        rows = db.chats_for_peer(conn, 777)
        assert [(row.scope, row.source_id) for row in rows] == [
            (DEFAULT_ACCOUNT, "import:nina"),
            (WORK, "import:nina-777"),
        ]
        assert rows[1].id >= db.SYNTHETIC_BASE
        assert db.message_counts(conn) == {rows[0].id: 1, rows[1].id: 1}
    finally:
        conn.close()


def test_sources_prune_checks_each_folder_through_its_own_account(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder is one account's: with that account signed out it is unchecked, never read
    through another account's folder of the same name."""
    paths = _two_account_home(tmp_home, '\n[[sources]]\nfolder = "Work"\naccount = "work"\n')
    paths.session_file_for(WORK).unlink()
    clients = _two_clients()
    _per_account(monkeypatch, clients)
    result = runner.invoke(cli.app, ["sources", "prune"])
    assert result.exit_code == 1
    assert "no Telegram session" in result.stderr
    paths.session_file_for(WORK).touch()
    _per_account(monkeypatch, _two_clients(authorized=False))
    refused = runner.invoke(cli.app, ["sources", "prune"])
    assert refused.exit_code == 1
    assert "grepogram auth --account work" in refused.stderr


def test_accounts_ls_lists_sessions_users_sources_and_chats(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _synced_two_accounts(tmp_home, monkeypatch)
    paths.session_file.unlink()
    result = runner.invoke(cli.app, ["accounts", "ls"])
    assert result.exit_code == 0, result.output
    rows = [line.split("  ") for line in result.stdout.splitlines()]
    cells = [[cell.strip() for cell in row if cell.strip()] for row in rows]
    assert cells == [
        ["account", "label", "session", "user", "sources", "chats"],
        [DEFAULT_ACCOUNT, "-", "missing", "Me (42)", "1", "1"],
        [WORK, "work phone", "authorized", "Worker (43)", "2", "2"],
    ]


def test_accounts_rm_refuses_without_a_terminal_and_changes_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _synced_two_accounts(tmp_home, monkeypatch)
    before = paths.config_file.read_text()
    chats = _chats(paths)
    result = runner.invoke(cli.app, ["accounts", "rm", WORK], input="y\n")
    assert result.exit_code == 1
    assert "accounts rm asks for a confirmation on a terminal, and there is none" in result.stderr
    assert paths.config_file.read_text() == before
    assert _chats(paths) == chats
    assert paths.session_file_for(WORK).exists()


def test_accounts_rm_keeps_shared_chats_and_deletes_the_accounts_own(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _synced_two_accounts(tmp_home, monkeypatch)
    conn = db.connect(paths)
    bob = db.get_chat_by_peer(conn, 2, WORK)
    conn.close()
    assert bob is not None
    terminal = _answer(monkeypatch, "y\n")
    result = runner.invoke(cli.app, ["accounts", "rm", WORK])
    assert result.exit_code == 0, result.output
    assert terminal.asked == ["remove account work? [y/N]: "] and terminal.closed
    assert "remove 2 sources: work/chat:@news, work/chat:2" in result.stdout
    assert "removed account work (1 chats deleted)" in result.stdout
    loaded = config.load(paths)
    assert loaded.accounts == [] and [s.id for s in loaded.sources] == ["chat:@news"]
    assert not paths.session_file_for(WORK).exists() and paths.session_file.exists()
    conn = db.connect(paths)
    try:
        assert db.get_chat(conn, bob.id) is None
        news = db.get_chat(conn, NEWS_PEER)
        assert news is not None and news.source_id == "chat:@news"
        assert db.message_counts(conn) == {NEWS_PEER: 2}
        assert db.chat_accounts(conn, NEWS_PEER) == [DEFAULT_ACCOUNT]
        assert db.chat_source_ids(conn, NEWS_PEER) == ["chat:@news"]
        assert [row.name for row in db.list_accounts(conn)] == [DEFAULT_ACCOUNT]
    finally:
        conn.close()


def test_accounts_rm_moves_a_shared_chat_to_the_account_that_stays(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the default account, whose source is the channel's primary, keeps the channel
    under the work account's source."""
    paths = _synced_two_accounts(tmp_home, monkeypatch)
    _answer(monkeypatch, "yes\n")
    result = runner.invoke(cli.app, ["accounts", "rm", DEFAULT_ACCOUNT])
    assert result.exit_code == 0, result.output
    assert "removed account default (0 chats deleted, 1 kept under another source)" in (
        result.stdout
    )
    assert not paths.session_file.exists()
    conn = db.connect(paths)
    try:
        news = db.get_chat(conn, NEWS_PEER)
        assert news is not None and news.source_id == "work/chat:@news"
        assert db.chat_accounts(conn, NEWS_PEER) == [WORK]
    finally:
        conn.close()
    assert [a.name for a in config.load(paths).accounts] == [WORK]


def test_accounts_rm_answered_no_removes_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _synced_two_accounts(tmp_home, monkeypatch)
    before = paths.config_file.read_text()
    _answer(monkeypatch, "\n")
    result = runner.invoke(cli.app, ["accounts", "rm", WORK])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "nothing removed"
    assert paths.config_file.read_text() == before
    assert paths.session_file_for(WORK).exists()


def test_accounts_rm_refuses_the_only_account_and_an_unknown_one(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)
    _answer(monkeypatch, "y\n")
    only = runner.invoke(cli.app, ["accounts", "rm", DEFAULT_ACCOUNT])
    assert only.exit_code == 1
    assert "the default account is the only account" in only.stderr
    unknown = runner.invoke(cli.app, ["accounts", "rm", WORK])
    assert unknown.exit_code == 1
    assert "unknown account 'work'" in unknown.stderr
    assert Paths.from_env().session_file.exists()


def test_accounts_rm_refuses_while_a_sync_runs(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _synced_two_accounts(tmp_home, monkeypatch)
    before = paths.config_file.read_text()
    _answer(monkeypatch, "y\n")
    with sync.SyncLock(paths):
        result = runner.invoke(cli.app, ["accounts", "rm", WORK])
    assert result.exit_code == 1
    assert "another sync is running" in result.stderr
    assert paths.config_file.read_text() == before
    assert paths.session_file_for(WORK).exists()


def _leaves(client: FakeClient) -> list[Any]:
    """The requests of ``client`` that change membership, apart from the reads a resolve makes."""
    kinds = (functions.channels.LeaveChannelRequest, functions.messages.DeleteChatUserRequest)
    return [request for request in client.requests if isinstance(request, kinds)]


def _leave_clients() -> dict[str, FakeClient]:
    clients = _two_clients(
        responses={
            functions.channels.LeaveChannelRequest: True,
            functions.messages.DeleteChatUserRequest: True,
        }
    )
    clients[WORK].dialogs.append(make_dialog(CLUB))
    clients[WORK].entities[CLUB_PEER] = CLUB
    return clients


def test_leave_refuses_without_a_terminal_and_never_edits_config(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _synced_two_accounts(tmp_home, monkeypatch)
    before = paths.config_file.read_text()
    clients = _leave_clients()
    _per_account(monkeypatch, clients)
    result = runner.invoke(cli.app, ["leave", "@news", "--account", WORK], input="y\n")
    assert result.exit_code == 1
    assert "leave asks for a confirmation on a terminal, and there is none" in result.stderr
    assert clients[WORK].calls == [] and clients[WORK].requests == []
    assert paths.config_file.read_text() == before


def test_leave_leaves_a_channel_and_keeps_its_source_and_history(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _synced_two_accounts(tmp_home, monkeypatch)
    before = paths.config_file.read_text()
    indexed = _chats(paths)
    clients = _leave_clients()
    _per_account(monkeypatch, clients)
    terminal = _answer(monkeypatch, "y\n")
    result = runner.invoke(cli.app, ["leave", "@news", "--account", WORK])
    assert result.exit_code == 0, result.output
    assert terminal.asked == [f"leave channel 'News' (id {NEWS_PEER}) as account work? [y/N]: "]
    assert f"left channel 'News' (id {NEWS_PEER}) as account work" in result.stdout
    [request] = _leaves(clients[WORK])
    assert isinstance(request, functions.channels.LeaveChannelRequest)
    assert request.channel.id == 700
    assert request.channel.access_hash == FakeWorld.access_hash(WORK, NEWS_PEER)
    assert clients[DEFAULT_ACCOUNT].calls == []
    assert paths.config_file.read_text() == before
    assert _chats(paths) == indexed


def test_leave_a_legacy_group_removes_the_account_from_it(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_account_home(tmp_home)
    clients = _leave_clients()
    _per_account(monkeypatch, clients)
    _answer(monkeypatch, "y\n")
    result = runner.invoke(cli.app, ["leave", "-a", WORK, "--", str(CLUB_PEER)])
    assert result.exit_code == 0, result.output
    [request] = _leaves(clients[WORK])
    assert isinstance(request, functions.messages.DeleteChatUserRequest)
    assert request.chat_id == 710 and isinstance(request.user_id, types.InputUserSelf)


def test_leave_answered_no_or_naming_a_private_chat_leaves_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_account_home(tmp_home)
    clients = _leave_clients()
    _per_account(monkeypatch, clients)
    _answer(monkeypatch, "n\n")
    declined = runner.invoke(cli.app, ["leave", "@news", "-a", WORK])
    assert declined.exit_code == 0, declined.output
    assert declined.stdout.strip() == "nothing changed"
    private = runner.invoke(cli.app, ["leave", "2", "-a", WORK])
    assert private.exit_code == 1
    assert "there is nothing to leave" in private.stderr
    assert _leaves(clients[WORK]) == []


# --- research --------------------------------------------------------------------------------


RESEARCH_ON = "\n[research]\nenabled = true\n"
RENT_PEER = -1000000000100
FLATS = make_channel(3001, "Tbilisi flats", username="tb_flats")
FLATS_PEER = -1000000003001


def _research_home(tmp_home: Path, extra: str = RESEARCH_ON) -> Paths:
    """A signed-in home with research switched on and one indexed channel, ``@tbrent``, whose
    only message mentions ``@tb_flats``."""
    paths = _signed_in(tmp_home, extra)
    conn = db.connect(paths)
    try:
        db.migrate(conn)
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
    finally:
        conn.close()
    return paths


def _research_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """The default account, not a member of the public channel ``@tb_flats`` (two posts)."""
    world = FakeWorld(
        entities=[FLATS],
        messages={FLATS_PEER: [tl.message(FLATS_PEER, i, f"flat {i}") for i in (1, 2)]},
    )
    client = world.client(
        me=make_user(9, "Me"), responses={functions.channels.GetFullChannelRequest: no_discussion}
    )
    _per_account(monkeypatch, {DEFAULT_ACCOUNT: client})
    return client


def _started(paths: Paths) -> int:
    """Start a session from ``@tbrent`` with a horizon reaching the fixture's 2025 posts."""
    result = runner.invoke(
        cli.app,
        ["research", "start", "who rents flats", "-s", "@tbrent", "--since-days", "3650"],
    )
    assert result.exit_code == 0, result.output
    assert "started research session 1 as account default" in result.stdout
    return 1


def _stores(paths: Paths) -> tuple[Any, Any]:
    conn = db.connect(paths)
    db.migrate(conn)
    return conn, research_db.open_store(paths)


def test_research_refuses_every_command_while_disabled(tmp_home: Path) -> None:
    paths = _research_home(tmp_home, extra="")
    for args in (["status"], ["candidates", "1"], ["approve", "1", "2"], ["stop", "1"]):
        result = runner.invoke(cli.app, ["research", *args])
        assert result.exit_code == 1, args
        assert "error: research is disabled" in result.stderr
        assert "enabled = true" in result.stderr
    assert not paths.research_db_file.exists(), "a refusal opens no research store"


def test_research_loop_through_the_cli(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _research_home(tmp_home)
    client = _research_client(monkeypatch)
    session_id = _started(paths)

    discovered = runner.invoke(cli.app, ["research", "discover", str(session_id)])
    assert discovered.exit_code == 0, discovered.output
    assert "1 leads, 1 new candidates" in discovered.stdout
    assert "probed 1: 0 unavailable" in discovered.stdout
    assert [n for n, _ in client.calls if n in ("iter_messages", "get_messages")] == []

    listed = runner.invoke(cli.app, ["research", "candidates", str(session_id)])
    assert listed.exit_code == 0, listed.output
    lines = listed.stdout.splitlines()
    assert lines[0] == '1. @tb_flats  "Tbilisi flats"  channel'
    assert lines[1] == "   status proposed, depth 1, corroboration 1, question overlap 1"
    assert lines[2] == "   member: no  cached: no  authorized: -"
    assert lines[3] == f"   mention {RENT_PEER}/1: flats at @tb_flats"

    stopped = runner.invoke(cli.app, ["research", "run", str(session_id)])
    assert stopped.exit_code == 0, stopped.output
    assert "0 messages stored" in stopped.stdout, "nothing approved, nothing fetched"
    assert config.load(paths).sources == []

    terminal = _answer(monkeypatch, "y\n")
    conn, rdb = _stores(paths)
    try:
        item = ApprovalItem(candidate_id=1, actions=("fetch", "add_source"))
        cfg = config.load(paths)
        summary = research.approval_summary(rdb, conn, cfg, session_id, [item])
    finally:
        rdb.close()
        conn.close()
    approved = runner.invoke(cli.app, ["research", "approve", str(session_id), "1"])
    assert approved.exit_code == 0, approved.output
    assert terminal.asked[0] == f"{summary}\n\n", "the terminal shows exactly the summary"
    assert terminal.asked[1] == "approve all of the above? [y/N]: "
    assert "approved for candidate 1: fetch, add_source" in approved.stdout

    status = runner.invoke(cli.app, ["research", "status", str(session_id)])
    assert status.exit_code == 0, status.output
    assert "approved, not carried out yet: candidate 1 (@tb_flats): fetch, add_source" in (
        status.stdout
    )

    ran = runner.invoke(cli.app, ["research", "run", str(session_id), "--json"])
    assert ran.exit_code == 0, ran.output
    report = json.loads(ran.stdout)
    assert (report["sources_added"], report["fetched"], report["messages"]) == ([1], [1], 2)
    assert [source.id for source in config.load(paths).sources] == ["chat:@tb_flats"]

    listed_json = runner.invoke(cli.app, ["research", "candidates", "1", "--json"])
    (candidate,) = json.loads(listed_json.stdout)["candidates"]
    assert (candidate["status"], candidate["cached"], candidate["authorized"]) == (
        "fetched",
        True,
        [],
    )

    ended = runner.invoke(cli.app, ["research", "stop", str(session_id)])
    assert ended.exit_code == 0, ended.output
    assert "stopped research session 1; 0 unused approvals voided" in ended.stdout
    assert [source.id for source in config.load(paths).sources] == ["chat:@tb_flats"], (
        "stopping keeps the sources a run added"
    )
    again = runner.invoke(cli.app, ["research", "run", str(session_id)])
    assert again.exit_code == 1
    assert "research session 1 is stopped" in again.stderr
    brief = runner.invoke(cli.app, ["research", "status", "--json"])
    assert [s["state"] for s in json.loads(brief.stdout)["sessions"]] == ["stopped"]


def _discovered_home(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    paths = _research_home(tmp_home)
    _research_client(monkeypatch)
    _started(paths)
    result = runner.invoke(cli.app, ["research", "discover", "1"])
    assert result.exit_code == 0, result.output
    return paths


def _grants(paths: Paths) -> list[Any]:
    conn, rdb = _stores(paths)
    try:
        return research_db.list_grants(rdb, 1)
    finally:
        rdb.close()
        conn.close()


def test_research_approve_refuses_without_a_terminal(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _discovered_home(tmp_home, monkeypatch)

    result = runner.invoke(cli.app, ["research", "approve", "1", "1"], input="y\n")

    assert result.exit_code == 1
    assert "asks for a confirmation on a terminal, and there is none" in result.stderr
    assert "run `grepogram research approve 1 1:fetch,add_source` yourself" in result.stderr
    assert _grants(paths) == [], "stdin never answers for the human"


def test_research_approve_grants_nothing_on_a_no(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _discovered_home(tmp_home, monkeypatch)
    _answer(monkeypatch, "n\n")

    result = runner.invoke(cli.app, ["research", "approve", "1", "1:fetch,add_source"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "nothing approved"
    assert _grants(paths) == []


def test_research_approve_records_the_cli_channel_and_the_text_shown(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _discovered_home(tmp_home, monkeypatch)
    terminal = _answer(monkeypatch, "yes\n")

    result = runner.invoke(cli.app, ["research", "approve", "1", "1:fetch,add_source"])

    assert result.exit_code == 0, result.output
    (grant,) = _grants(paths)
    assert (grant.via, grant.actions) == ("cli", ("fetch", "add_source"))
    assert terminal.asked[0] == f"{grant.summary}\n\n"
    assert "next: grepogram research run 1" in result.stdout


def test_research_approve_has_no_option_that_answers_for_the_human(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _discovered_home(tmp_home, monkeypatch)
    for flag in ("--yes", "-y", "--force"):
        result = runner.invoke(cli.app, ["research", "approve", "1", "1", flag])
        assert result.exit_code != 0, flag
    assert _grants(paths) == []


def test_research_approve_refuses_an_invalid_approval_before_asking(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _discovered_home(tmp_home, monkeypatch)
    terminal = _answer(monkeypatch, "y\n")

    fetch_only = runner.invoke(cli.app, ["research", "approve", "1", "1:fetch"])
    malformed = runner.invoke(cli.app, ["research", "approve", "1", "flats"])

    assert fetch_only.exit_code == malformed.exit_code == 1
    assert "approve `add_source` together with `fetch`" in fetch_only.stderr
    assert "hint: approve it as 1:fetch,add_source" in fetch_only.stderr
    assert "'flats' is not an approval item" in malformed.stderr
    assert terminal.asked == [] and _grants(paths) == []


def test_research_skip_exclude_and_unexclude_need_no_terminal(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _discovered_home(tmp_home, monkeypatch)

    skipped = runner.invoke(cli.app, ["research", "skip", "1", "1"])
    excluded = runner.invoke(cli.app, ["research", "exclude", "@tb_flats", "--reason", "spam"])
    lifted = runner.invoke(cli.app, ["research", "unexclude", "1", "--session", "1"])
    again = runner.invoke(cli.app, ["research", "unexclude", "@tb_flats"])

    assert skipped.stdout.strip() == "skipped: 1"
    assert (
        excluded.stdout.strip() == "excluded @tb_flats from every session (1 candidates set aside)"
    )
    assert "no longer excluded: @tb_flats" in lifted.stdout
    assert again.stdout.strip() == "none of them was excluded"
    listed = runner.invoke(cli.app, ["research", "candidates", "1", "--status", "proposed"])
    assert listed.stdout.startswith("1. @tb_flats")
    unknown = runner.invoke(cli.app, ["research", "candidates", "1", "--status", "maybe"])
    assert unknown.exit_code == 1 and "unknown candidate status 'maybe'" in unknown.stderr


def test_research_status_lists_sessions(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _research_home(tmp_home)
    empty = runner.invoke(cli.app, ["research", "status"])
    assert empty.exit_code == 0, empty.output
    assert empty.stdout.startswith("no research sessions")
    _started(paths)

    listed = runner.invoke(cli.app, ["research", "status"])
    one = runner.invoke(cli.app, ["research", "status", "1"])
    unknown = runner.invoke(cli.app, ["research", "status", "9"])

    assert listed.stdout.splitlines() == [
        "session  state   account  candidates  runs  question",
        "1        active  default  0           0     who rents flats",
    ]
    assert one.stdout.splitlines()[:2] == [
        'research session 1 (active): "who rents flats"',
        f"account: default; seed chats: {RENT_PEER}",
    ]
    assert "sources since" in one.stdout and "candidates: none yet" in one.stdout
    assert unknown.exit_code == 1 and "no research session 9" in unknown.stderr


def test_research_start_refuses_an_unknown_seed_or_account(tmp_home: Path) -> None:
    _research_home(tmp_home)

    seed = runner.invoke(cli.app, ["research", "start", "q", "-s", "@nowhere"])
    account = runner.invoke(cli.app, ["research", "start", "q", "-s", "@tbrent", "-a", "work"])

    assert seed.exit_code == 1 and "no indexed chat matches '@nowhere'" in seed.stderr
    assert account.exit_code == 1 and "unknown account 'work'" in account.stderr


def test_research_discover_offline_asks_telegram_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _research_home(tmp_home)
    _started(paths)

    def no_client(*args: object) -> None:
        raise AssertionError("an offline discover builds no client")

    monkeypatch.setattr(tg, "make_client", no_client)
    result = runner.invoke(cli.app, ["research", "discover", "1", "--offline", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert (report["new_candidates"], report["probe"]) == ([1], None)


def test_research_discover_without_a_session_points_at_offline(tmp_home: Path) -> None:
    paths = _research_home(tmp_home)
    _started(paths)
    paths.session_file.unlink()

    result = runner.invoke(cli.app, ["research", "discover", "1"])

    assert result.exit_code == 1
    assert "run: grepogram auth" in result.stderr
    assert "grepogram research discover 1 --offline" in result.stderr
