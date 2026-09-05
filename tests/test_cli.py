import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import get_args

import pytest
import typer
from typer.testing import CliRunner

from grepogram import __version__, cli, config, db, index, search, units
from grepogram.config import TEMPLATE
from grepogram.models import ChatRow, Config, MessageRow, SearchMode
from grepogram.paths import Paths
from tests.conftest import file_mode
from tests.fixtures import chat_ru

runner = CliRunner()


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
