import datetime as dt
import sqlite3
import time
from collections.abc import Iterator

import pytest

from grepogram import db, filters
from grepogram.filters import FilterError, InvalidDate, UnknownChat
from grepogram.models import ChatRow, Config, Filters, Source

ARG_CHAT = -1000000000100
ARG_NEWS = -1000000000101
ARG_DISCUSSION = -1000000000102
GEORGIA = -1000000000200
BANK = -1000000000300
ALICANTE = -1000000000400
ALICE = 777

CHATS = [
    ChatRow(
        id=ARG_CHAT,
        type="supergroup",
        title="Argentina chat",
        username="arg_chat",
        is_forum=True,
        source_id="folder:Argentina",
    ),
    ChatRow(
        id=ARG_NEWS,
        type="channel",
        title="Argentina News",
        username="argnews",
        source_id="folder:Argentina",
    ),
    ChatRow(
        id=ARG_DISCUSSION,
        type="supergroup",
        title="Argentina News Chat",
        source_id="folder:Argentina",
        discussion_of=ARG_NEWS,
    ),
    ChatRow(
        id=GEORGIA,
        type="supergroup",
        title="Грузия | Georgia chat",
        username="ru_georgia",
        source_id="chat:@ru_georgia",
    ),
    ChatRow(id=BANK, type="supergroup", title="Банки и финансы", source_id=f"chat:{BANK}"),
    ChatRow(id=ALICANTE, type="supergroup", title="Alicante expats", source_id="folder:Spain"),
    ChatRow(id=ALICE, type="user", title="Alice Liddell", username="alice", source_id="chat:777"),
]
ARGENTINA = {ARG_CHAT, ARG_NEWS, ARG_DISCUSSION}

CFG = Config(
    sources=[
        Source(folder="Argentina"),
        Source(chat="@ru_georgia"),
        Source(chat=BANK),
        Source(folder="Spain"),
        Source(chat=777),
        Source(folder="Empty"),
        Source(chat="@ghost_channel"),
        Source(chat=-1000000000999),
    ]
)


def ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    return int(dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.UTC).timestamp())


NOW = ts(2025, 3, 31, 12, 0, 0)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    db.migrate(connection)
    for chat in CHATS:
        db.upsert_chat(connection, chat)
    yield connection
    connection.close()


@pytest.fixture
def empty_conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


# --- parse_when ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "end", "expected"),
    [
        pytest.param("2025-06-01", False, ts(2025, 6, 1), id="date"),
        pytest.param("2025-06-01", True, ts(2025, 6, 1, 23, 59, 59), id="date-end"),
        pytest.param(" 2025-06-01 ", False, ts(2025, 6, 1), id="date-padded"),
        pytest.param("2024-02-29", True, ts(2024, 2, 29, 23, 59, 59), id="leap-day-end"),
        pytest.param("2025-06", False, ts(2025, 6, 1), id="month"),
        pytest.param("2025-06", True, ts(2025, 6, 30, 23, 59, 59), id="month-end"),
        pytest.param("2025-12", True, ts(2025, 12, 31, 23, 59, 59), id="december-end"),
        pytest.param("2024-02", True, ts(2024, 2, 29, 23, 59, 59), id="leap-month-end"),
        pytest.param("2025-06-01T14:30", False, ts(2025, 6, 1, 14, 30), id="datetime"),
        pytest.param("2025-06-01T14:30", True, ts(2025, 6, 1, 14, 30), id="datetime-end-is-point"),
        pytest.param("2025-06-01 14:30:15", False, ts(2025, 6, 1, 14, 30, 15), id="datetime-space"),
        pytest.param("2025-06-01T14:30Z", False, ts(2025, 6, 1, 14, 30), id="datetime-zulu"),
        pytest.param("2025-06-01t14:30z", False, ts(2025, 6, 1, 14, 30), id="datetime-lowercase"),
        pytest.param("2025-06-01T14:30+03:00", False, ts(2025, 6, 1, 11, 30), id="datetime-offset"),
        pytest.param(
            "2025-06-01T14:30-0500", False, ts(2025, 6, 1, 19, 30), id="datetime-offset-compact"
        ),
        pytest.param("0d", False, NOW, id="zero-days"),
        pytest.param("7d", False, NOW - 7 * 86400, id="days"),
        pytest.param("7d", True, NOW - 7 * 86400, id="days-end-is-point"),
        pytest.param("7D", False, NOW - 7 * 86400, id="days-uppercase"),
        pytest.param("7 d", False, NOW - 7 * 86400, id="days-spaced"),
        pytest.param("2w", False, NOW - 14 * 86400, id="weeks"),
        pytest.param("1m", False, ts(2025, 2, 28, 12), id="month-back-clamped"),
        pytest.param("3m", False, ts(2024, 12, 31, 12), id="months-back-across-year"),
        pytest.param("13m", False, ts(2024, 2, 29, 12), id="months-back-to-leap-day"),
        pytest.param("1y", False, ts(2024, 3, 31, 12), id="year"),
        pytest.param("12m", False, ts(2024, 3, 31, 12), id="twelve-months-is-a-year"),
    ],
)
def test_parse_when(text: str, end: bool, expected: int) -> None:
    assert filters.parse_when(text, NOW, end=end) == expected


def test_parse_when_year_back_from_leap_day_clamps() -> None:
    assert filters.parse_when("1y", ts(2024, 2, 29, 8)) == ts(2023, 2, 28, 8)


def test_parse_when_defaults_to_current_time() -> None:
    before = int(time.time())
    value = filters.parse_when("0d")
    assert before <= value <= int(time.time())


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "yesterday",
        "2025",
        "2025-13",
        "2025-00",
        "2025-02-30",
        "2025-6-1",
        "20250601",
        "2025/06/01",
        "01.06.2025",
        "2025-06-01T25:00",
        "2025-06-01T14",
        "2025-W10",
        "7x",
        "-7d",
        "d7",
        "7 days",
        "1.5d",
    ],
)
def test_parse_when_rejects_bad_input(text: str) -> None:
    with pytest.raises(InvalidDate) as info:
        filters.parse_when(text, NOW)
    message = str(info.value)
    assert repr(text) in message
    assert "2025-06-01" in message and "7d" in message and "1y" in message


def test_parse_when_rejects_huge_relative_values() -> None:
    with pytest.raises(InvalidDate):
        filters.parse_when("9999y", NOW)


def test_invalid_date_is_a_value_error_and_a_filter_error() -> None:
    with pytest.raises(ValueError):
        filters.parse_when("nope", NOW)
    with pytest.raises(FilterError):
        filters.parse_when("nope", NOW)


# --- resolve_chats ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param(str(GEORGIA), {GEORGIA}, id="marked-id"),
        pytest.param("777", {ALICE}, id="user-id"),
        pytest.param("chat:777", {ALICE}, id="source-id-numeric"),
        pytest.param("@ru_georgia", {GEORGIA}, id="username"),
        pytest.param("@RU_Georgia", {GEORGIA}, id="username-case-insensitive"),
        pytest.param("chat:@ru_georgia", {GEORGIA}, id="source-id-username"),
        pytest.param("https://t.me/ru_georgia", {GEORGIA}, id="public-link"),
        pytest.param("https://t.me/ru_georgia/1234", {GEORGIA}, id="public-message-link"),
        pytest.param("t.me/c/300/15", {BANK}, id="private-link"),
        pytest.param("folder:Argentina", ARGENTINA, id="folder"),
        pytest.param("folder:argentina", ARGENTINA, id="folder-case-insensitive"),
        pytest.param("folder:Argentin", ARGENTINA, id="folder-substring"),
        pytest.param("folder:Argentnia", ARGENTINA, id="folder-fuzzy"),
        pytest.param("Argentina", ARGENTINA, id="folder-name-as-text"),
        pytest.param("argentina news", {ARG_NEWS, ARG_DISCUSSION}, id="title-substring"),
        pytest.param("Georgia", {GEORGIA}, id="title-substring-latin-part"),
        pytest.param("грузия", {GEORGIA}, id="title-substring-cyrillic"),
        pytest.param("ГРУЗИЯ", {GEORGIA}, id="title-substring-cyrillic-case"),
        pytest.param("Georgai", {GEORGIA}, id="title-fuzzy-typo"),
        pytest.param("банк", {BANK}, id="title-substring-cyrillic-word"),
        pytest.param("arg_chat", {ARG_CHAT}, id="username-as-text"),
        pytest.param("argnews", {ARG_NEWS}, id="username-as-text-2"),
        pytest.param("alice", {ALICE}, id="substring-beats-fuzzy"),
        pytest.param("alic", {ALICE, ALICANTE}, id="substring-in-both"),
        pytest.param("Alicante  Expats", {ALICANTE}, id="whitespace-collapsed"),
    ],
)
def test_resolve_chats_single_spec(conn: sqlite3.Connection, spec: str, expected: set[int]) -> None:
    assert filters.resolve_chats(conn, CFG, [spec]) == expected


def test_resolve_chats_unions_specs(conn: sqlite3.Connection) -> None:
    got = filters.resolve_chats(conn, CFG, ["@alice", "folder:Argentina", str(BANK)])
    assert got == ARGENTINA | {ALICE, BANK}


def test_resolve_chats_no_specs(conn: sqlite3.Connection) -> None:
    assert filters.resolve_chats(conn, CFG, []) == set()


def test_resolve_chats_fuzzy_falls_back_only_without_substring(conn: sqlite3.Connection) -> None:
    # "alicant" is a substring of one title and within SequenceMatcher reach of "Alice": the
    # substring tier wins alone; a pure typo takes every fuzzy hit
    assert filters.resolve_chats(conn, CFG, ["Alicant"]) == {ALICANTE}
    assert filters.resolve_chats(conn, CFG, ["Alicnate expats"]) == {ALICANTE}
    assert filters.resolve_chats(conn, CFG, ["Alicanto"]) == {ALICANTE, ALICE}


@pytest.mark.parametrize(
    "spec",
    [
        "-1000000000999",
        "@nobody",
        "https://t.me/nobody",
        "folder:Nowhere",
        "kubernetes",
        "xyz",
    ],
)
def test_resolve_chats_unknown(conn: sqlite3.Connection, spec: str) -> None:
    with pytest.raises(UnknownChat) as info:
        filters.resolve_chats(conn, CFG, [spec])
    err = info.value
    assert err.spec == spec
    assert isinstance(err, FilterError)
    message = str(err)
    assert repr(spec) in message
    assert "'Argentina chat' (id -1000000000100, @arg_chat)" in message
    assert "folder:Argentina (3 chats)" in message
    assert "folder:Spain (1 chats)" in message
    assert "'Банки и финансы' (id -1000000000300)" in message
    assert any("Alice Liddell" in candidate for candidate in err.candidates)


def test_resolve_chats_unknown_stops_at_first_bad_spec(conn: sqlite3.Connection) -> None:
    with pytest.raises(UnknownChat, match="'nope'"):
        filters.resolve_chats(conn, CFG, ["@alice", "nope", "@nobody"])


@pytest.mark.parametrize(
    ("spec", "source_id"),
    [
        pytest.param("folder:Empty", "folder:Empty", id="folder-spec"),
        pytest.param("folder:empty", "folder:Empty", id="folder-spec-case"),
        pytest.param("Empty", "folder:Empty", id="folder-text"),
        pytest.param("@ghost_channel", "chat:@ghost_channel", id="username"),
        pytest.param("ghost_channel", "chat:@ghost_channel", id="username-text"),
        pytest.param("-1000000000999", "chat:-1000000000999", id="id"),
    ],
)
def test_resolve_chats_names_unsynced_source(
    conn: sqlite3.Connection, spec: str, source_id: str
) -> None:
    with pytest.raises(UnknownChat) as info:
        filters.resolve_chats(conn, CFG, [spec])
    assert (
        info.value.hint
        == f"source {source_id} is configured but has no indexed chats yet, run a sync"
    )
    assert "run a sync" in str(info.value)


def test_resolve_chats_unknown_without_hint(conn: sqlite3.Connection) -> None:
    with pytest.raises(UnknownChat) as info:
        filters.resolve_chats(conn, CFG, ["@nobody"])
    assert info.value.hint is None
    assert "(" not in str(info.value).split(";")[0]


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        pytest.param("https://t.me/+AbCdEf", "invite links", id="invite-link"),
        pytest.param("@x", "invalid username", id="short-username"),
        pytest.param("   ", "empty target", id="blank"),
        pytest.param("folder:", "folder name missing", id="folder-without-name"),
    ],
)
def test_resolve_chats_invalid_target(conn: sqlite3.Connection, spec: str, fragment: str) -> None:
    with pytest.raises(UnknownChat) as info:
        filters.resolve_chats(conn, CFG, [spec])
    assert info.value.hint is not None and fragment in info.value.hint
    assert fragment in str(info.value)


def test_resolve_chats_empty_index(empty_conn: sqlite3.Connection) -> None:
    with pytest.raises(UnknownChat) as info:
        filters.resolve_chats(empty_conn, CFG, ["Argentina"])
    assert info.value.candidates == []
    assert "nothing is indexed yet" in str(info.value)
    assert (
        info.value.hint
        == "source folder:Argentina is configured but has no indexed chats yet, run a sync"
    )


def test_resolve_chats_ignores_config_for_matching(empty_conn: sqlite3.Connection) -> None:
    db.upsert_chat(empty_conn, ChatRow(id=1, type="user", title="Solo", source_id="chat:1"))
    assert filters.resolve_chats(empty_conn, Config(), ["solo"]) == {1}
    with pytest.raises(UnknownChat) as info:
        filters.resolve_chats(empty_conn, Config(), ["Argentina"])
    assert info.value.hint is None


def test_resolve_chats_untitled_chat_is_listed_by_id(empty_conn: sqlite3.Connection) -> None:
    db.upsert_chat(empty_conn, ChatRow(id=5, type="user", source_id="chat:5"))
    with pytest.raises(UnknownChat) as info:
        filters.resolve_chats(empty_conn, Config(), ["anything"])
    assert info.value.candidates == ["None (id 5)"]
    assert filters.resolve_chats(empty_conn, Config(), ["5"]) == {5}


# --- resolve_filters -------------------------------------------------------------------------


def test_resolve_filters_empty(conn: sqlite3.Connection) -> None:
    assert filters.resolve_filters(conn, CFG, None, None, None, NOW) == Filters(None, None, None)
    assert filters.resolve_filters(conn, CFG, [], "", "  ", NOW) == Filters(None, None, None)


def test_resolve_filters_composes(conn: sqlite3.Connection) -> None:
    got = filters.resolve_filters(conn, CFG, ["@alice", "Argentina"], "2025-06", "2025-06", NOW)
    assert got == Filters(ARGENTINA | {ALICE}, ts(2025, 6, 1), ts(2025, 6, 30, 23, 59, 59))


def test_resolve_filters_until_is_inclusive_end_of_day(conn: sqlite3.Connection) -> None:
    got = filters.resolve_filters(conn, CFG, None, "2025-06-01", "2025-06-01", NOW)
    assert got == Filters(None, ts(2025, 6, 1), ts(2025, 6, 1, 23, 59, 59))


def test_resolve_filters_relative_bounds_share_now(conn: sqlite3.Connection) -> None:
    got = filters.resolve_filters(conn, CFG, None, "2w", "1w", NOW)
    assert got == Filters(None, NOW - 14 * 86400, NOW - 7 * 86400)


def test_resolve_filters_default_now(conn: sqlite3.Connection) -> None:
    before = int(time.time())
    got = filters.resolve_filters(conn, CFG, None, "1d", None)
    assert got.since is not None
    assert before - 86400 <= got.since <= int(time.time()) - 86400
    assert got.until is None


def test_resolve_filters_rejects_inverted_range(conn: sqlite3.Connection) -> None:
    with pytest.raises(InvalidDate, match="'2025-07' lies after until '2025-06'"):
        filters.resolve_filters(conn, CFG, None, "2025-07", "2025-06", NOW)
    with pytest.raises(InvalidDate):
        filters.resolve_filters(conn, CFG, None, "1d", "2d", NOW)


def test_resolve_filters_same_instant_is_allowed(conn: sqlite3.Connection) -> None:
    got = filters.resolve_filters(conn, CFG, None, "2025-06-01T10:00", "2025-06-01T10:00", NOW)
    assert got.since == got.until == ts(2025, 6, 1, 10)


def test_resolve_filters_propagates_errors(conn: sqlite3.Connection) -> None:
    with pytest.raises(UnknownChat):
        filters.resolve_filters(conn, CFG, ["@nobody"], None, None, NOW)
    with pytest.raises(InvalidDate):
        filters.resolve_filters(conn, CFG, ["@alice"], "soon", None, NOW)
