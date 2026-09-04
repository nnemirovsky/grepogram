import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from grepogram import db
from grepogram.log import shutdown_logging


@pytest.fixture(autouse=True)
def fake_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test resolves models to the fakes and never opens a link — tests of the real loaders
    and of ``open`` unset these themselves — so nothing run under the test environment, a probe
    included, downloads a model or launches an application on this machine."""
    monkeypatch.setenv("GREPOGRAM_FAKE_MODELS", "1")
    monkeypatch.setenv("GREPOGRAM_NO_OPEN", "1")


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    """Close the file handler every test that configures logging leaves open.

    ``autouse`` here rather than per module: a module that declared it as a plain fixture never
    ran it, and its log file stayed open on a ``tmp_path`` that had already been removed.
    """
    yield
    shutdown_logging()


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point grepogram at a throwaway home and force fake models for the test."""
    home = tmp_path / "grepogram-home"
    home.mkdir()
    monkeypatch.setenv("GREPOGRAM_HOME", str(home))
    monkeypatch.setenv("GREPOGRAM_FAKE_MODELS", "1")
    return home


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """An empty index in memory, migrated to the current schema."""
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


def file_mode(path: Path) -> int:
    """The permission bits of ``path``, for the 0600 / 0700 assertions."""
    return stat.S_IMODE(path.stat().st_mode)
