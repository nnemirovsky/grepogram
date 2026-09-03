import logging
import stat
import sys
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from grepogram import __version__, cli, config, db
from grepogram.config import TEMPLATE
from grepogram.log import shutdown_logging
from grepogram.models import Config
from grepogram.paths import Paths

runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- app -------------------------------------------------------------------------------------


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
    assert _mode(target) == 0o600
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
    assert _mode(home) == 0o700


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


def test_fail_writes_to_stderr_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as excinfo:
        cli.fail("nope", code=3)
    assert excinfo.value.exit_code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: nope\n"
