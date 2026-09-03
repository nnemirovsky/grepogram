import sqlite3
import threading
from collections.abc import Iterator

import pytest

from grepogram import stem
from grepogram.db import FTS_TOKENIZE

DOCS = {
    1: "Открыл счёт в банке Galicia без DNI, только с паспортом",
    2: "Visas and bank accounts: opening one takes weeks",
    3: "bge-m3 runs at 12:30, don't forget foo_bar",
    4: "AND OR NOT are ordinary words here",
}


@pytest.fixture
def fts() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE t USING fts5("
        f"raw, stemmed, chat_id UNINDEXED, tokenize='{FTS_TOKENIZE}')"
    )
    conn.executemany(
        "INSERT INTO t(rowid, raw, stemmed, chat_id) VALUES (?, ?, ?, 1)",
        [(rowid, text, stem.stem_text(text)) for rowid, text in DOCS.items()],
    )
    try:
        yield conn
    finally:
        conn.close()


def _match(conn: sqlite3.Connection, query: str | None) -> list[int]:
    assert query is not None
    rows = conn.execute("SELECT rowid FROM t WHERE t MATCH ? ORDER BY rowid", (query,))
    return [rowid for (rowid,) in rows]


# --- tokenize --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Открыл счёт", ["открыл", "счёт"]),
        ("Hello, World!", ["hello", "world"]),
        ("ＤＮＩ ﬁne", ["dni", "fine"]),
        ("e-mail 12:30 don't", ["e", "mail", "12", "30", "don", "t"]),
        ("foo_bar 2024 m3", ["foo_bar", "2024", "m3"]),
        ("🙂🙂 !!! ... —", []),
        ("", []),
        ("   \n\t", []),
    ],
)
def test_tokenize(text: str, expected: list[str]) -> None:
    assert stem.tokenize(text) == expected


def test_tokenize_keeps_short_tokens_and_stop_words() -> None:
    assert stem.tokenize("a в the и") == ["a", "в", "the", "и"]


# --- language_of / stem_token ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("счёт", "russian"),
        ("внж", "russian"),
        ("iphoneом", "russian"),
        ("bank", "english"),
        ("café", "english"),
        ("m3", "english"),
        ("2024", None),
        ("שלום", None),
        ("你好", None),
        ("_", None),
    ],
)
def test_language_of(token: str, expected: str | None) -> None:
    assert stem.language_of(token) == expected


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["счёт", "счета", "счетов"], "счет"),
        (["банк", "банки", "банков", "банке"], "банк"),
        (["виза", "визы", "визу"], "виз"),
        (["аргентина", "аргентине", "аргентину"], "аргентин"),
    ],
)
def test_russian_inflections_share_one_stem(tokens: list[str], expected: str) -> None:
    assert {stem.stem_token(token) for token in tokens} == {expected}


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("banks", "bank"),
        ("accounts", "account"),
        ("visas", "visa"),
        ("opening", "open"),
        ("opened", "open"),
    ],
)
def test_english_stems(token: str, expected: str) -> None:
    assert stem.stem_token(token) == expected


@pytest.mark.parametrize("token", ["2024", "m3", "t", "внж", "dni", "שלום", "你好", "foo_bar"])
def test_digits_short_tokens_and_other_scripts_pass_through(token: str) -> None:
    assert stem.stem_token(token) == token


def test_stem_token_is_thread_safe() -> None:
    words = ["счета", "банков", "accounts", "visas", "открыл", "opening"] * 50
    expected = ["счет", "банк", "account", "visa", "откр", "open"] * 50
    results: dict[int, list[str]] = {}

    def run(index: int) -> None:
        stem.stem_token.cache_clear()
        results[index] = [stem.stem_token(word) for word in words]

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(result == expected for result in results.values())


# --- stem_text -------------------------------------------------------------------------------


def test_stem_text_mixed_script_sentence() -> None:
    text = "Открыл счета в банках без DNI — documents needed!"
    assert stem.stem_text(text) == "откр счет в банк без dni document need"


def test_stem_text_empty() -> None:
    assert stem.stem_text("🙂 ...") == ""


# --- fts_query -------------------------------------------------------------------------------


def test_fts_query_and_form() -> None:
    assert stem.fts_query("Открыть счета в банках") == '"откр" AND "счет" AND "в" AND "банк"'


def test_fts_query_or_form() -> None:
    assert stem.fts_query("visas accounts", "OR") == '"visa" OR "account"'


def test_fts_query_splits_on_dash_colon_and_apostrophe() -> None:
    query = stem.fts_query("bge-m3 12:30 don't")
    assert query == '"bge" AND "m3" AND "12" AND "30" AND "don" AND "t"'


def test_fts_query_dedups_repeated_stems_in_first_seen_order() -> None:
    assert stem.fts_query("счёт банка счета банки") == '"счет" AND "банк"'


@pytest.mark.parametrize("text", ["", "   ", "🙂🙂", "!!! ... — ?", "🇦🇷 🏦"])
def test_fts_query_none_without_tokens(text: str) -> None:
    assert stem.fts_query(text) is None
    assert stem.fts_query(text, "OR") is None


# --- accepted by FTS5 ------------------------------------------------------------------------


def test_fts5_and_query_matches_inflected_forms(fts: sqlite3.Connection) -> None:
    assert _match(fts, stem.fts_query("счета банков")) == [1]
    assert _match(fts, stem.fts_query("visa account")) == [2]


def test_fts5_and_requires_every_token_but_or_does_not(fts: sqlite3.Connection) -> None:
    assert _match(fts, stem.fts_query("счета visas")) == []
    assert _match(fts, stem.fts_query("счета visas", "OR")) == [1, 2]


def test_fts5_accepts_tokens_split_from_dash_colon_apostrophe(fts: sqlite3.Connection) -> None:
    assert _match(fts, stem.fts_query("bge-m3 12:30 don't")) == [3]
    assert _match(fts, stem.fts_query("bge-m3", "OR")) == [3]


def test_fts5_underscore_token_matches_as_phrase(fts: sqlite3.Connection) -> None:
    assert _match(fts, stem.fts_query("foo_bar")) == [3]


def test_fts5_operator_words_are_matched_literally(fts: sqlite3.Connection) -> None:
    assert _match(fts, stem.fts_query("AND OR NOT")) == [4]


def test_fts5_raw_column_keeps_exact_spelling(fts: sqlite3.Connection) -> None:
    assert _match(fts, '"galicia"') == [1]
    assert _match(fts, '"счёт"') == [1]


def test_fts5_rejects_the_unquoted_form(fts: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.OperationalError):
        _match(fts, "bge-m3 12:30")
