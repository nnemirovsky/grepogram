import os
import sqlite3
from pathlib import Path

import sqlite_vec

import grepogram


def test_package_imports_with_version() -> None:
    assert grepogram.__version__


def test_sqlite_loads_sqlite_vec_extension() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        (version,) = conn.execute("SELECT vec_version()").fetchone()
        assert version.startswith("v")
        conn.execute("CREATE VIRTUAL TABLE v USING vec0(embedding FLOAT[4])")
    finally:
        conn.close()


def test_sqlite_has_fts5() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE f USING fts5(body, tokenize='unicode61 remove_diacritics 2')"
        )
        conn.execute("INSERT INTO f(body) VALUES ('привет мир')")
        rows = conn.execute("SELECT rowid FROM f WHERE f MATCH '\"привет\"'").fetchall()
        assert rows == [(1,)]
    finally:
        conn.close()


def test_tmp_home_fixture_isolates_environment(tmp_home: Path) -> None:
    assert tmp_home.is_dir()
    assert os.environ["GREPOGRAM_HOME"] == str(tmp_home)
    assert os.environ["GREPOGRAM_FAKE_MODELS"] == "1"
