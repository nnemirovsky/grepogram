import dataclasses
import fcntl
import logging
import os
import re
import stat
import sys
import threading
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from grepogram import config
from grepogram.config import TEMPLATE, ConfigError
from grepogram.log import redact, setup_logging, shutdown_logging
from grepogram.models import Config, SearchCfg, Source, TelegramCfg
from grepogram.paths import Paths, env_flag


@pytest.fixture
def paths(tmp_home: Path) -> Paths:
    return Paths.from_env()


@pytest.fixture
def clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- paths -----------------------------------------------------------------------------------


def test_paths_follow_grepogram_home(tmp_home: Path, paths: Paths) -> None:
    assert paths.config_file == tmp_home / "config.toml"
    assert paths.session_file == tmp_home / "session.session"
    assert paths.db_file == tmp_home / "index.db"
    assert paths.lock_file == tmp_home / "sync.lock"
    assert paths.config_lock_file == tmp_home / "config.lock"
    assert paths.log_dir == tmp_home / "logs"
    assert paths.log_file == tmp_home / "logs" / "grepogram.log"


def test_paths_from_explicit_mapping_expands_user(tmp_path: Path) -> None:
    paths = Paths.from_env({"GREPOGRAM_HOME": str(tmp_path / "h")})
    assert paths.db_file == tmp_path / "h" / "index.db"
    assert Paths.from_env({"GREPOGRAM_HOME": "~/x"}).config_file == Path.home() / "x/config.toml"


def test_paths_macos_defaults_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GREPOGRAM_HOME", raising=False)
    paths = Paths.from_env()
    home = Path.home()
    assert paths.config_file == home / ".config" / "grepogram" / "config.toml"
    assert paths.session_file == home / ".config" / "grepogram" / "session.session"
    assert paths.db_file == home / "Library" / "Application Support" / "grepogram" / "index.db"
    assert paths.lock_file == home / "Library" / "Application Support" / "grepogram" / "sync.lock"
    assert paths.config_lock_file == home / ".config" / "grepogram" / "config.lock"
    assert paths.log_dir == home / "Library" / "Logs" / "grepogram"
    assert Paths.from_env({"GREPOGRAM_HOME": "  "}) == paths


def test_session_file_always_ends_with_session_suffix(tmp_path: Path) -> None:
    assert Paths.under(tmp_path).session_file.suffix == ".session"
    assert Paths.macos_default(tmp_path).session_file.suffix == ".session"
    with pytest.raises(ValueError, match=r"\.session"):
        Paths(
            config_file=tmp_path / "config.toml",
            session_file=tmp_path / "session",
            db_file=tmp_path / "index.db",
            lock_file=tmp_path / "sync.lock",
            log_dir=tmp_path / "logs",
        )


def test_env_flag_is_true_for_true_values_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "true", " Yes ", "ON"):
        assert env_flag("X", {"X": value})
    for value in ("", "0", "no", "off", "2"):
        assert not env_flag("X", {"X": value})
    assert not env_flag("X", {})
    monkeypatch.setenv("GREPOGRAM_PROBE_FLAG", "yes")
    assert env_flag("GREPOGRAM_PROBE_FLAG")
    monkeypatch.delenv("GREPOGRAM_PROBE_FLAG")
    assert not env_flag("GREPOGRAM_PROBE_FLAG")


def test_ensure_dirs_creates_private_directories(tmp_path: Path) -> None:
    paths = Paths.macos_default(tmp_path / "home")
    paths.ensure_dirs()
    paths.ensure_dirs()
    for directory in paths.directories:
        assert directory.is_dir()
        assert _mode(directory) == 0o700
    assert len(paths.directories) == 3


# --- config ----------------------------------------------------------------------------------


def test_load_without_file_returns_defaults(paths: Paths) -> None:
    assert config.load(paths) == Config()
    assert not paths.config_file.exists()


def test_partial_file_keeps_other_defaults(paths: Paths) -> None:
    paths.config_file.write_text('[telegram]\napi_id = 42\napi_hash = "h"\n')
    cfg = config.load(paths)
    assert cfg.telegram == TelegramCfg(api_id=42, api_hash="h")
    assert cfg.search == SearchCfg()
    assert cfg.sources == []


def test_save_load_round_trip(paths: Paths) -> None:
    cfg = Config(
        telegram=TelegramCfg(api_id=12345, api_hash="abc"),
        search=SearchCfg(k=5, dedup_overlap=0.75),
        sources=[
            Source(folder="Argentina"),
            Source(chat="@ru_georgia", since="2024-01-01", comments=True),
            Source(chat=123456789),
        ],
    )
    config.save(cfg, paths)
    assert config.load(paths) == cfg


def test_save_writes_mode_0600_even_over_a_permissive_file(paths: Paths) -> None:
    paths.config_file.write_text("")
    paths.config_file.chmod(0o644)
    config.save(Config(), paths)
    assert _mode(paths.config_file) == 0o600
    assert not paths.config_file.with_name(".config.toml.tmp").exists()


def test_save_is_comment_lossy(paths: Paths) -> None:
    paths.config_file.write_text(TEMPLATE)
    config.save(config.load(paths), paths)
    text = paths.config_file.read_text()
    assert "#" not in text
    assert "sources" not in text


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("foo = 1\n", "foo"),
        ("[search]\nfoo = 1\n", "search.foo"),
        ("[[sources]]\nfolder = 'x'\nbar = 2\n", "sources[0].bar"),
    ],
)
def test_unknown_key_names_the_key(text: str, key: str) -> None:
    with pytest.raises(ConfigError, match=rf"unknown key: {re.escape(key)}"):
        config.loads(text)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[search]\nk = '10'\n", "search.k"),
        ("[telegram]\napi_id = true\n", "telegram.api_id"),
        ("[units]\nwindow_gap_min = 1.5\n", "units.window_gap_min"),
        ("[models]\ndevice = 1\n", "models.device"),
        ("[[sources]]\nchat = true\n", r"sources\[0\].chat"),
        ("[[sources]]\nfolder = 'x'\ncomments = 'yes'\n", r"sources\[0\].comments"),
        ("search = 1\n", "search"),
        ("sources = 1\n", "sources"),
        ("sources = [1]\n", r"sources\[0\]"),
    ],
)
def test_wrong_type_names_the_key(text: str, key: str) -> None:
    with pytest.raises(ConfigError, match=rf"invalid value for {key}"):
        config.loads(text)


def test_float_field_accepts_integer_literal() -> None:
    value = config.loads("[search]\ndedup_overlap = 1\n").search.dedup_overlap
    assert value == 1.0 and isinstance(value, float)


@pytest.mark.parametrize(
    "text", ["[[sources]]\nsince = '2024'\n", "[[sources]]\nfolder = 'a'\nchat = 1\n"]
)
def test_source_needs_exactly_one_target(text: str) -> None:
    with pytest.raises(ConfigError, match=r"sources\[0\]: .*exactly one"):
        config.loads(text)


def test_source_since_accepts_bare_toml_date() -> None:
    cfg = config.loads("[[sources]]\nchat = 1\nsince = 2024-01-01\n")
    assert cfg.sources[0].since == "2024-01-01"


def test_duplicate_sources_rejected() -> None:
    with pytest.raises(ConfigError, match=r"sources\[1\]: duplicate source 'folder:A'"):
        config.loads("[[sources]]\nfolder = 'A'\n[[sources]]\nfolder = 'A'\n")


def test_invalid_toml_reports_file(paths: Paths) -> None:
    paths.config_file.write_text("[search\n")
    with pytest.raises(ConfigError, match=r"invalid TOML") as info:
        config.load(paths)
    assert str(paths.config_file) in str(info.value)


def test_source_ids_are_stable() -> None:
    assert Source(folder="Argentina").id == "folder:Argentina"
    assert Source(chat="@ru_georgia").id == "chat:@ru_georgia"
    assert Source(chat=123456789).id == "chat:123456789"
    with pytest.raises(ValueError):
        Source()


def test_template_parses_to_defaults() -> None:
    assert config.loads(TEMPLATE) == Config()
    assert "[[sources]]" in TEMPLATE


# --- config lock -----------------------------------------------------------------------------


def _free(path: Path) -> bool:
    """Whether a fresh descriptor — another process, as far as ``flock`` is concerned — can take
    the lock right now."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def test_config_lock_is_exclusive_and_blocks_until_released(paths: Paths) -> None:
    entered = threading.Event()

    def other_process() -> None:
        with config.ConfigLock(paths):
            entered.set()

    waiter = threading.Thread(target=other_process)
    with config.ConfigLock(paths) as lock:
        assert lock.path == paths.config_lock_file and lock.path.is_file()
        assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600
        assert not _free(lock.path)
        waiter.start()
        assert not entered.wait(0.2)
    waiter.join(5)
    assert entered.is_set()
    assert _free(paths.config_lock_file) and paths.config_lock_file.is_file()


def test_update_applies_the_change_to_the_stored_config_under_the_lock(paths: Paths) -> None:
    stored = Config(telegram=TelegramCfg(api_id=1, api_hash="h"), sources=[Source(chat="@a")])
    config.save(stored, paths)
    seen: list[Config] = []

    def add_b(current: Config) -> Config:
        seen.append(current)
        assert not _free(paths.config_lock_file)
        return dataclasses.replace(current, sources=[*current.sources, Source(chat="@b")])

    updated = config.update(paths, add_b)
    assert seen == [stored]
    assert updated.sources == [Source(chat="@a"), Source(chat="@b")]
    assert config.load(paths) == updated
    assert _free(paths.config_lock_file)
    assert stat.S_IMODE(paths.config_file.stat().st_mode) == 0o600


def test_update_starts_from_the_defaults_without_a_file(paths: Paths) -> None:
    assert not paths.config_file.exists()
    updated = config.update(paths, lambda current: dataclasses.replace(current, sources=[]))
    assert updated == Config() and config.load(paths) == Config()


def test_update_leaves_the_file_alone_and_frees_the_lock_when_the_change_fails(
    paths: Paths,
) -> None:
    config.save(Config(sources=[Source(chat="@a")]), paths)
    before = paths.config_file.read_text()

    def broken(current: Config) -> Config:
        raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        config.update(paths, broken)
    assert paths.config_file.read_text() == before
    assert _free(paths.config_lock_file)
    paths.config_file.write_text("[search\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        config.update(paths, lambda current: current)
    assert _free(paths.config_lock_file)


# --- logging ---------------------------------------------------------------------------------


def _stream_handlers() -> list[logging.StreamHandler]:  # type: ignore[type-arg]
    return [h for h in logging.getLogger().handlers if isinstance(h, logging.StreamHandler)]


def test_setup_logging_uses_stderr_and_file_never_stdout(paths: Paths, clean_logging: None) -> None:
    setup_logging(paths, "DEBUG")
    handlers = _stream_handlers()
    assert all(h.stream is not sys.stdout for h in handlers)
    assert any(h.stream is sys.stderr for h in handlers)
    files = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    assert len(files) == 1
    assert files[0].baseFilename == str(paths.log_file)
    assert (files[0].maxBytes, files[0].backupCount) == (5 * 1024 * 1024, 3)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("telethon").level == logging.INFO


def test_setup_logging_without_stderr_writes_file_only(paths: Paths, clean_logging: None) -> None:
    before = _stream_handlers()
    setup_logging(paths, logging.INFO, stderr=False)
    added = [h for h in _stream_handlers() if h not in before]
    assert [type(h) for h in added] == [RotatingFileHandler]
    logging.getLogger("grepogram.test").info("hello file %s", redact("секрет"))
    for handler in added:
        handler.flush()
    text = paths.log_file.read_text()
    assert "hello file" in text
    assert "секрет" not in text


def test_setup_logging_twice_replaces_handlers(paths: Paths, clean_logging: None) -> None:
    root = logging.getLogger()
    previous = root.level
    setup_logging(paths)
    count = len(root.handlers)
    setup_logging(paths, "WARNING")
    assert len(root.handlers) == count
    shutdown_logging()
    assert len(root.handlers) == count - 2
    assert root.level == previous


def test_setup_logging_rejects_unknown_level(paths: Paths) -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        setup_logging(paths, "LOUD")


def test_redact_hides_content_but_stays_stable() -> None:
    text = "открыть счёт без DNI в Galicia"
    out = redact(text)
    assert "Galicia" not in out and "счёт" not in out
    assert out == redact(text)
    assert out != redact(text + "!")
    assert out.startswith(f"<{len(text)} chars #")
    assert redact("") == redact(None) == "<empty>"


# --- added by the review fixes --------------------------------------------------------------


def test_ensure_dirs_leaves_an_existing_directory_alone(tmp_path: Path) -> None:
    paths = Paths.macos_default(tmp_path / "home")
    paths.log_dir.mkdir(parents=True)
    paths.log_dir.chmod(0o755)
    paths.ensure_dirs()
    assert _mode(paths.log_dir) == 0o755
    assert _mode(paths.config_file.parent) == 0o700
    assert _mode(paths.db_file.parent) == 0o700


def test_save_creates_the_directories_on_a_fresh_machine(tmp_path: Path) -> None:
    paths = Paths.under(tmp_path / "new" / "home")
    config.save(Config(telegram=TelegramCfg(api_id=1, api_hash="h")), paths)
    assert config.load(paths).telegram.api_id == 1
    assert _mode(paths.config_file) == 0o600
    assert _mode(paths.config_file.parent) == 0o700


def test_write_private_cleans_up_when_writing_fails(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.config_file.write_text("original")

    def broken(fd: int, *args: object, **kwargs: object) -> object:
        os.close(fd)
        raise OSError("disk full")

    monkeypatch.setattr(os, "fdopen", broken)
    with pytest.raises(OSError, match="disk full"):
        config.write_private(paths.config_file, "new")
    assert paths.config_file.read_text() == "original"
    assert list(paths.config_file.parent.iterdir()) == [paths.config_file]
