import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from grepogram import db
from grepogram.log import shutdown_logging


@pytest.fixture(autouse=True)
def fake_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test resolves models to the fakes — tests of the real loaders unset this themselves
    — so nothing run under the test environment, a probe included, downloads a model."""
    monkeypatch.setenv("GREPOGRAM_FAKE_MODELS", "1")


@pytest.fixture(autouse=True)
def plain_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every assertion on CLI text reads the same words wherever the suite runs.

    Typer prints usage errors and help through rich, and as soon as rich takes the environment
    for a terminal it styles an option name in pieces — ``--budget`` comes out as ``-`` and
    ``-budget`` with escape codes between them, and a substring assertion misses it. A local
    pytest run is not a terminal, GitHub Actions is one to rich, so such a test would fail only
    there. ``TERM=dumb`` is what rich reads as "no terminal".
    """
    monkeypatch.setenv("TERM", "dumb")


@pytest.fixture(autouse=True)
def no_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test ever asks the developer's real terminal.

    ``accounts rm`` and ``leave`` confirm on the controlling terminal (``cli._open_terminal``),
    which a local ``pytest`` run has; the suite runs as if there were none, and a test that
    answers a confirmation installs its own terminal over this one.
    """

    def refuse() -> None:
        raise OSError("no terminal in the test suite")

    monkeypatch.setattr("grepogram.cli._open_terminal", refuse)


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


@pytest.fixture
def v6_conn() -> Iterator[sqlite3.Connection]:
    """An empty index in memory at schema 6 — what v0.2.0 left in the field — built from the
    released steps and not migrated further, for the upgrade tests to populate and walk up."""
    connection = db.connect(":memory:")
    for version in (db.BASE_VERSION, 6):
        for statement in db.MIGRATIONS[version]:
            connection.execute(statement)
    db.set_meta(connection, db.META_SCHEMA_VERSION, "6")
    yield connection
    connection.close()


def file_mode(path: Path) -> int:
    """The permission bits of ``path``, for the 0600 / 0700 assertions."""
    return stat.S_IMODE(path.stat().st_mode)
