import logging
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grepogram import cli, config, db, sources, tg
from grepogram.dialogs import DialogCatalog, DialogInfo
from grepogram.log import shutdown_logging
from grepogram.models import ChatRow, Config, MessageRow, Source
from grepogram.paths import Paths
from grepogram.sources import (
    AmbiguousTarget,
    DuplicateSource,
    InvalidTarget,
    SourceError,
    Target,
    UnknownSource,
    UnknownTarget,
)
from tests.fakes import (
    FakeClient,
    make_channel,
    make_chatlist,
    make_dialog,
    make_folder,
    make_group,
    make_user,
)

runner = CliRunner()

CONFIG_WITH_KEYS = '[telegram]\napi_id = 12345\napi_hash = "fakehash"\n'

ALICE = make_user(1, "Alice", "Liddell", username="alice", contact=True)
BOB = make_user(2, "Bob")
HELPER = make_user(3, "Helper", bot=True, username="helper_bot")
OLD_GROUP = make_group(10, "Old group")
ARG = make_channel(100, "Argentina chat", username="arg_chat", megagroup=True, forum=True)
GEORGIA = make_channel(101, "Грузия | Georgia chat", megagroup=True)
NEWS = make_channel(200, "News", username="news")
OUTSIDE = make_channel(300, "Outside", username="outside")

ARG_ID = -1000000000100
GEORGIA_ID = -1000000000101
NEWS_ID = -1000000000200
OUTSIDE_ID = -1000000000300
GHOST_ID = -1000000000999


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


def _client(**kwargs: object) -> FakeClient:
    dialogs = [
        make_dialog(ALICE),
        make_dialog(BOB),
        make_dialog(HELPER),
        make_dialog(OLD_GROUP),
        make_dialog(ARG),
        make_dialog(GEORGIA),
        make_dialog(NEWS),
    ]
    folders = [
        make_folder(3, "Argentina", include=[ARG, OUTSIDE, GHOST_ID], pinned=[NEWS]),
        make_folder(4, "People", contacts=True, bots=True, exclude=[HELPER]),
        make_chatlist(5, "Shared", include=[ALICE, ARG]),
    ]
    kwargs.setdefault("entities", [OUTSIDE])
    return FakeClient(dialogs=dialogs, folders=folders, **kwargs)  # type: ignore[arg-type]


def _catalog(**kwargs: object) -> DialogCatalog:
    return DialogCatalog(_client(**kwargs))


def _cfg(*entries: Source) -> Config:
    return Config(sources=list(entries))


def _chat(chat_id: int, source_id: str, **overrides: object) -> ChatRow:
    fields: dict[str, object] = {
        "id": chat_id,
        "type": "supergroup",
        "title": f"chat {chat_id}",
        "source_id": source_id,
    }
    fields.update(overrides)
    return ChatRow(**fields)  # type: ignore[arg-type]


def _store(conn: sqlite3.Connection, chat: ChatRow, messages: int = 0) -> None:
    db.upsert_chat(conn, chat)
    db.upsert_messages(
        conn,
        [
            MessageRow(chat_id=chat.id, msg_id=i, date=1_700_000_000 + i, text=f"m{i}")
            for i in range(1, messages + 1)
        ],
    )


# --- parse_target ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("-1000000000100", "id", -1000000000100),
        ("42", "id", 42),
        (" -10 ", "id", -10),
        ("@arg_chat", "username", "arg_chat"),
        ("https://t.me/arg_chat", "username", "arg_chat"),
        ("http://t.me/arg_chat/", "username", "arg_chat"),
        ("t.me/arg_chat", "username", "arg_chat"),
        ("https://t.me/arg_chat/123", "username", "arg_chat"),
        ("https://t.me/arg_chat?start=x", "username", "arg_chat"),
        ("https://www.telegram.me/Arg_Chat", "username", "Arg_Chat"),
        ("https://t.me/s/arg_chat", "username", "arg_chat"),
        ("https://t.me/c/1234567890/42", "id", -1001234567890),
        ("t.me/c/100", "id", -1000000000100),
        ("folder:Argentina", "folder", "Argentina"),
        ("Folder:  Buenos Aires ", "folder", "Buenos Aires"),
        ("chat:@arg_chat", "username", "arg_chat"),
        ("chat:-1000000000100", "id", -1000000000100),
        ("chat:https://t.me/arg_chat", "username", "arg_chat"),
        ("Argentina chat", "fuzzy", "Argentina chat"),
        ("  грузия ", "fuzzy", "грузия"),
        ("arg_chat", "fuzzy", "arg_chat"),
    ],
)
def test_parse_target(raw: str, kind: str, value: str | int) -> None:
    target = sources.parse_target(raw)
    assert (target.kind, target.value) == (kind, value)
    assert target.text == str(value)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("@", "invalid username"),
        ("@ab", "invalid username"),
        ("@bad!name", "invalid username"),
        ("@1starts_with_digit", "invalid username"),
        ("https://t.me/+AbCdEf", "invite"),
        ("https://t.me/joinchat/AbCdEf", "invite"),
        ("https://t.me/", "no chat"),
        ("https://t.me/c/", "t.me/c/<id>"),
        ("https://t.me/c/abc", "t.me/c/<id>"),
        ("folder:", "folder name missing"),
        ("folder:   ", "folder name missing"),
    ],
)
def test_parse_target_rejects_malformed_input(raw: str, message: str) -> None:
    with pytest.raises(InvalidTarget, match=message):
        sources.parse_target(raw)


def test_source_target_reads_int_and_string_values() -> None:
    assert sources.source_target(Source(chat=ARG_ID)) == Target(kind="id", value=ARG_ID)
    assert sources.source_target(Source(chat="@arg_chat")) == Target(
        kind="username", value="arg_chat"
    )
    assert sources.source_target(Source(chat="Argentina")).kind == "fuzzy"


# --- add_source ------------------------------------------------------------------------------


async def test_add_source_by_id_stores_username_when_public() -> None:
    added = await sources.add_source(_cfg(), sources.parse_target(str(ARG_ID)), _catalog())
    assert added.source == Source(chat="@arg_chat")
    assert added.title == "Argentina chat"
    assert added.folder is None
    assert [d.id for d in added.dialogs] == [ARG_ID]
    assert added.config.sources == [Source(chat="@arg_chat")]


async def test_add_source_by_id_stores_id_without_username() -> None:
    added = await sources.add_source(_cfg(), sources.parse_target(str(GEORGIA_ID)), _catalog())
    assert added.source == Source(chat=GEORGIA_ID)
    assert added.source.id == f"chat:{GEORGIA_ID}"


async def test_add_source_does_not_mutate_the_input_config() -> None:
    cfg = _cfg(Source(folder="People"))
    added = await sources.add_source(cfg, sources.parse_target("@alice"), _catalog())
    assert cfg.sources == [Source(folder="People")]
    assert added.config.sources == [Source(folder="People"), Source(chat="@alice")]
    assert added.dialogs[0].type == "user"


async def test_add_source_by_username_is_case_insensitive_and_canonical() -> None:
    added = await sources.add_source(_cfg(), sources.parse_target("@ARG_CHAT"), _catalog())
    assert added.source == Source(chat="@arg_chat")


async def test_add_source_by_link() -> None:
    added = await sources.add_source(
        _cfg(), sources.parse_target("https://t.me/news/15"), _catalog()
    )
    assert added.source == Source(chat="@news")
    assert added.dialogs[0].type == "channel"


async def test_add_source_falls_back_to_get_entity_for_unknown_dialogs() -> None:
    client = _client()
    catalog = DialogCatalog(client)
    by_id = await sources.add_source(_cfg(), sources.parse_target(str(OUTSIDE_ID)), catalog)
    assert by_id.source == Source(chat="@outside")
    assert ("get_entity", {"key": OUTSIDE_ID}) in client.calls
    by_name = await sources.add_source(_cfg(), sources.parse_target("@outside"), catalog)
    assert by_name.title == "Outside"
    assert ("get_entity", {"key": "@outside"}) in client.calls


async def test_add_source_unknown_id_and_username() -> None:
    with pytest.raises(UnknownTarget, match="no dialog with id"):
        await sources.add_source(_cfg(), sources.parse_target(str(GHOST_ID)), _catalog())
    with pytest.raises(UnknownTarget, match="@nobody_here"):
        await sources.add_source(_cfg(), sources.parse_target("@nobody_here"), _catalog())


async def test_add_source_folder_exact_and_case_insensitive() -> None:
    added = await sources.add_source(_cfg(), sources.parse_target("folder:argentina"), _catalog())
    assert added.source == Source(folder="Argentina")
    assert added.folder is not None and added.folder.id == 3
    assert sorted(d.id for d in added.dialogs) == sorted([ARG_ID, NEWS_ID, OUTSIDE_ID])
    assert added.title == "Argentina"


async def test_add_source_folder_fuzzy_unique_and_unknown() -> None:
    added = await sources.add_source(_cfg(), sources.parse_target("folder:peple"), _catalog())
    assert added.source == Source(folder="People")
    assert sorted(d.id for d in added.dialogs) == [1]
    with pytest.raises(UnknownTarget, match="no folder named 'Xyz'.*Argentina"):
        await sources.add_source(_cfg(), sources.parse_target("folder:Xyz"), _catalog())


async def test_add_source_fuzzy_unique_match() -> None:
    added = await sources.add_source(_cfg(), sources.parse_target("georgia"), _catalog())
    assert added.source == Source(chat=GEORGIA_ID)
    assert added.title == "Грузия | Georgia chat"


async def test_add_source_fuzzy_prefers_the_single_exact_match() -> None:
    added = await sources.add_source(_cfg(), sources.parse_target("news"), _catalog())
    assert added.source == Source(chat="@news")
    as_folder = await sources.add_source(_cfg(), sources.parse_target("shared"), _catalog())
    assert as_folder.source == Source(folder="Shared")
    assert as_folder.folder is not None
    over_substring = await sources.add_source(_cfg(), sources.parse_target("argentina"), _catalog())
    assert over_substring.source == Source(folder="Argentina")


async def test_add_source_fuzzy_ambiguous_lists_candidates() -> None:
    with pytest.raises(AmbiguousTarget) as excinfo:
        await sources.add_source(_cfg(), sources.parse_target("arg"), _catalog())
    err = excinfo.value
    assert err.query == "arg"
    assert "folder 'Argentina' (folder:Argentina)" in err.candidates
    assert f"supergroup 'Argentina chat' (id {ARG_ID}, @arg_chat)" in err.candidates
    assert "be more specific" in str(err)
    assert "folder:Argentina" in str(err)


async def test_add_source_fuzzy_unknown() -> None:
    with pytest.raises(UnknownTarget, match="nothing matches 'bank account'"):
        await sources.add_source(_cfg(), sources.parse_target("bank account"), _catalog())


async def test_add_source_rejects_duplicates_by_id_and_by_other_spelling() -> None:
    cfg = _cfg(Source(chat="@arg_chat"), Source(folder="Argentina"), Source(chat=GEORGIA_ID))
    with pytest.raises(DuplicateSource, match="chat:@arg_chat is already a source"):
        await sources.add_source(cfg, sources.parse_target("@arg_chat"), _catalog())
    with pytest.raises(DuplicateSource, match="chat:@arg_chat is already a source"):
        await sources.add_source(cfg, sources.parse_target(str(ARG_ID)), _catalog())
    with pytest.raises(DuplicateSource, match="folder:Argentina is already a source"):
        await sources.add_source(cfg, sources.parse_target("folder:ARGENTINA"), _catalog())
    with pytest.raises(DuplicateSource, match=f"chat:{GEORGIA_ID} is already a source"):
        await sources.add_source(cfg, sources.parse_target("georgia"), _catalog())
    spelled = _cfg(Source(chat=ARG_ID), Source(chat="@NEWS"))
    with pytest.raises(DuplicateSource, match=f"already a source as chat:{ARG_ID}"):
        await sources.add_source(spelled, sources.parse_target("@arg_chat"), _catalog())
    with pytest.raises(DuplicateSource, match=f"already a source as chat:{ARG_ID}"):
        await sources.add_source(spelled, sources.parse_target(str(ARG_ID)), _catalog())
    with pytest.raises(DuplicateSource, match="'News' is already a source as chat:@NEWS"):
        await sources.add_source(spelled, sources.parse_target(str(NEWS_ID)), _catalog())
    with pytest.raises(DuplicateSource, match="already a source as chat:@NEWS"):
        await sources.add_source(spelled, sources.parse_target("https://t.me/news"), _catalog())


async def test_add_source_since_and_comments() -> None:
    added = await sources.add_source(
        _cfg(), sources.parse_target("@news"), _catalog(), since=" 2024-01-05 ", comments=True
    )
    assert added.source == Source(chat="@news", since="2024-01-05", comments=True)
    with pytest.raises(InvalidTarget, match="ISO date"):
        await sources.add_source(_cfg(), sources.parse_target("@news"), _catalog(), since="jan")
    with pytest.raises(SourceError, match="channels only.*supergroup"):
        await sources.add_source(
            _cfg(), sources.parse_target("@arg_chat"), _catalog(), comments=True
        )
    folder = await sources.add_source(
        _cfg(), sources.parse_target("folder:Argentina"), _catalog(), comments=True
    )
    assert folder.source.comments


# --- remove_source ---------------------------------------------------------------------------


def _populate(conn: sqlite3.Connection) -> None:
    _store(conn, _chat(ARG_ID, "folder:Argentina", title="Argentina chat", username="arg_chat"), 3)
    _store(conn, _chat(NEWS_ID, "folder:Argentina", type="channel", title="News"), 2)
    _store(conn, _chat(GEORGIA_ID, f"chat:{GEORGIA_ID}", title="Грузия | Georgia chat"), 4)
    _store(conn, _chat(1, "chat:@alice", type="user", title="Alice Liddell", username="alice"), 1)
    unit_id = conn.execute(
        "INSERT INTO units(chat_id, kind, msg_id_start, msg_id_end, msg_ids, date_start, date_end,"
        " text) VALUES (?, 'window', 1, 4, '[1,2,3,4]', 1, 2, 'x') RETURNING id",
        (GEORGIA_ID,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO unit_fts(rowid, raw, stemmed, chat_id, date_start) VALUES (?, 'x', 'x', ?, 1)",
        (unit_id, GEORGIA_ID),
    )
    conn.commit()


CFG = _cfg(Source(folder="Argentina"), Source(chat=GEORGIA_ID), Source(chat="@alice"))


def test_remove_source_by_source_id_deletes_only_its_data(conn: sqlite3.Connection) -> None:
    _populate(conn)
    removed = sources.remove_source(
        conn=conn, cfg=CFG, target=sources.parse_target("chat:-1000000000101")
    )
    assert removed.source_id == f"chat:{GEORGIA_ID}"
    assert removed.source == Source(chat=GEORGIA_ID)
    assert removed.chat_ids == [GEORGIA_ID]
    assert removed.config.sources == [Source(folder="Argentina"), Source(chat="@alice")]
    assert CFG.sources[1] == Source(chat=GEORGIA_ID)
    assert db.get_chat(conn, GEORGIA_ID) is None
    assert db.message_counts(conn) == {ARG_ID: 3, NEWS_ID: 2, 1: 1}
    assert conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM unit_fts").fetchone()[0] == 0


def test_remove_source_folder_by_name_removes_all_member_chats(conn: sqlite3.Connection) -> None:
    _populate(conn)
    removed = sources.remove_source(CFG, conn, sources.parse_target("folder:argentina"))
    assert removed.source_id == "folder:Argentina"
    assert removed.source == Source(folder="Argentina")
    assert sorted(removed.chat_ids) == sorted([ARG_ID, NEWS_ID])
    assert [c.id for c in db.list_chats(conn)] == sorted([GEORGIA_ID, 1])
    with pytest.raises(UnknownSource, match="no folder source named 'Argentina'"):
        sources.remove_source(removed.config, conn, sources.parse_target("folder:Argentina"))


def test_remove_source_folder_fuzzy_name(conn: sqlite3.Connection) -> None:
    _populate(conn)
    fuzzy = sources.remove_source(CFG, conn, sources.parse_target("folder:argentin"))
    assert fuzzy.source_id == "folder:Argentina"
    assert sorted(fuzzy.chat_ids) == sorted([ARG_ID, NEWS_ID])


def test_remove_source_by_username_id_and_fuzzy_title(conn: sqlite3.Connection) -> None:
    _populate(conn)
    by_name = sources.remove_source(CFG, conn, sources.parse_target("@ALICE"))
    assert (by_name.source_id, by_name.chat_ids) == ("chat:@alice", [1])
    by_id = sources.remove_source(by_name.config, conn, sources.parse_target(str(GEORGIA_ID)))
    assert (by_id.source_id, by_id.chat_ids) == (f"chat:{GEORGIA_ID}", [GEORGIA_ID])
    assert by_id.config.sources == [Source(folder="Argentina")]


def test_remove_source_fuzzy_matches_titles_of_chat_sources(conn: sqlite3.Connection) -> None:
    _populate(conn)
    removed = sources.remove_source(CFG, conn, sources.parse_target("georgia"))
    assert removed.source_id == f"chat:{GEORGIA_ID}"
    by_handle = sources.remove_source(removed.config, conn, sources.parse_target("alice"))
    assert by_handle.source_id == "chat:@alice"
    exact = sources.remove_source(by_handle.config, conn, sources.parse_target("Argentina"))
    assert exact.source_id == "folder:Argentina"


def test_remove_source_refuses_a_chat_that_came_through_a_folder(conn: sqlite3.Connection) -> None:
    _populate(conn)
    for raw in ("@arg_chat", str(ARG_ID), "Argentina chat", "https://t.me/arg_chat"):
        with pytest.raises(SourceError, match="indexed through folder:Argentina"):
            sources.remove_source(CFG, conn, sources.parse_target(raw))
    assert len(db.list_chats(conn)) == 4


def test_remove_source_ambiguous_and_unknown(conn: sqlite3.Connection) -> None:
    _populate(conn)
    with pytest.raises(AmbiguousTarget) as excinfo:
        sources.remove_source(CFG, conn, sources.parse_target("a"))
    assert set(excinfo.value.candidates) >= {"folder:Argentina", "chat:@alice"}
    with pytest.raises(UnknownSource, match="no source matches 'zzz'"):
        sources.remove_source(CFG, conn, sources.parse_target("zzz"))
    with pytest.raises(UnknownSource, match="no folder source named 'Nope'"):
        sources.remove_source(CFG, conn, sources.parse_target("folder:Nope"))
    with pytest.raises(UnknownSource, match="id 424242 is not an indexed chat"):
        sources.remove_source(CFG, conn, sources.parse_target("424242"))
    with pytest.raises(UnknownSource, match="@nobody is not an indexed chat"):
        sources.remove_source(CFG, conn, sources.parse_target("@nobody"))
    assert len(db.list_chats(conn)) == 4


def test_remove_source_handles_data_whose_entry_left_the_config(conn: sqlite3.Connection) -> None:
    _populate(conn)
    removed = sources.remove_source(_cfg(), conn, sources.parse_target("chat:@alice"))
    assert removed.source is None
    assert removed.source_id == "chat:@alice"
    assert removed.chat_ids == [1]
    assert removed.config == _cfg()
    assert db.get_chat(conn, 1) is None
    with pytest.raises(UnknownSource, match="no folder source named 'x'.*folder:Argentina"):
        sources.remove_source(_cfg(), conn, sources.parse_target("folder:x"))


def test_remove_source_of_a_never_synced_entry(conn: sqlite3.Connection) -> None:
    removed = sources.remove_source(CFG, conn, sources.parse_target("@alice"))
    assert removed.source == Source(chat="@alice")
    assert removed.chat_ids == []
    assert len(removed.config.sources) == 2


# --- resolve_sources -------------------------------------------------------------------------


async def test_resolve_sources_folder_and_dm(conn: sqlite3.Connection) -> None:
    cfg = _cfg(Source(folder="Argentina"), Source(chat="@alice"))
    rows = await sources.resolve_sources(cfg, _client(), conn)
    assert [(r.id, r.source_id) for r in rows] == [
        (ARG_ID, "folder:Argentina"),
        (NEWS_ID, "folder:Argentina"),
        (OUTSIDE_ID, "folder:Argentina"),
        (1, "chat:@alice"),
    ]
    arg = db.get_chat(conn, ARG_ID)
    assert arg == ChatRow(
        id=ARG_ID,
        type="supergroup",
        title="Argentina chat",
        username="arg_chat",
        is_forum=True,
        source_id="folder:Argentina",
    )
    alice = db.get_chat(conn, 1)
    assert alice is not None
    assert (alice.type, alice.title, alice.username) == ("user", "Alice Liddell", "alice")
    assert db.get_chat(conn, GHOST_ID) is None
    assert db.get_chat(conn, NEWS_ID) is not None and db.get_chat(conn, NEWS_ID).type == "channel"  # type: ignore[union-attr]


async def test_resolve_sources_category_folder_and_int_and_link_chats(
    conn: sqlite3.Connection,
) -> None:
    cfg = _cfg(Source(folder="People"), Source(chat=GEORGIA_ID), Source(chat="https://t.me/news"))
    rows = await sources.resolve_sources(cfg, _client(), conn)
    assert [(r.id, r.source_id) for r in rows] == [
        (1, "folder:People"),
        (GEORGIA_ID, f"chat:{GEORGIA_ID}"),
        (NEWS_ID, "chat:https://t.me/news"),
    ]


async def test_resolve_sources_keeps_sync_state_and_first_source(
    conn: sqlite3.Connection,
) -> None:
    cfg = _cfg(Source(chat="@arg_chat"), Source(folder="Argentina"))
    first = await sources.resolve_sources(cfg, _client(), conn)
    assert [(r.id, r.source_id) for r in first][:2] == [
        (ARG_ID, "chat:@arg_chat"),
        (NEWS_ID, "folder:Argentina"),
    ]
    db.set_chat_progress(conn, ARG_ID, last_msg_id=77, last_sync_at=1_700_000_000)
    db.set_chat_unavailable(conn, NEWS_ID)
    again = await sources.resolve_sources(cfg, _client(), conn)
    assert len(again) == len(first) == 3
    arg = db.get_chat(conn, ARG_ID)
    assert arg is not None and (arg.last_msg_id, arg.last_sync_at) == (77, 1_700_000_000)
    assert arg.source_id == "chat:@arg_chat"
    news = db.get_chat(conn, NEWS_ID)
    assert news is not None and news.unavailable


async def test_resolve_sources_warns_and_skips_unresolvable_sources(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg(
        Source(folder="Xyz"),
        Source(chat="@nobody_here"),
        Source(chat="arg"),
        Source(chat="shared"),
        Source(chat="@alice"),
    )
    with caplog.at_level(logging.WARNING, logger="grepogram.sources"):
        rows = await sources.resolve_sources(cfg, _client(), conn)
    assert [r.id for r in rows] == [1]
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("skipping source folder:Xyz" in m and "no folder named" in m for m in messages)
    assert any("skipping source chat:@nobody_here" in m for m in messages)
    assert any("skipping source chat:arg:" in m and "several" in m for m in messages)
    assert any("skipping source chat:shared" in m and "folder = 'Shared'" in m for m in messages)
    assert not any("cannot resolve peer" in m for m in messages)


async def test_resolve_sources_warns_for_unresolvable_folder_peers(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="grepogram.sources"):
        rows = await sources.resolve_sources(_cfg(Source(folder="Argentina")), _client(), conn)
    assert GHOST_ID not in {r.id for r in rows}
    assert OUTSIDE_ID in {r.id for r in rows}
    assert any(
        "cannot resolve peer" in r.getMessage() and str(GHOST_ID) in r.getMessage()
        for r in caplog.records
    )


async def test_resolve_sources_with_no_sources(conn: sqlite3.Connection) -> None:
    client = _client()
    assert await sources.resolve_sources(_cfg(), client, conn) == []
    assert db.list_chats(conn) == []
    assert ("get_dialogs", {}) not in client.calls


# --- sources_status --------------------------------------------------------------------------


def test_sources_status_counts_in_config_order(conn: sqlite3.Connection) -> None:
    _populate(conn)
    db.set_chat_progress(conn, ARG_ID, last_msg_id=3, last_sync_at=1_700_000_000)
    db.set_chat_unavailable(conn, NEWS_ID)
    cfg = _cfg(Source(chat="@alice"), Source(folder="Argentina"), Source(folder="Empty"))
    statuses = sources.sources_status(cfg, conn)
    assert [s.source_id for s in statuses] == [
        "chat:@alice",
        "folder:Argentina",
        "folder:Empty",
        f"chat:{GEORGIA_ID}",
    ]
    alice, argentina, empty, orphan = statuses
    assert [(c.id, c.title, c.type, c.username, c.message_count) for c in alice.chats] == [
        (1, "Alice Liddell", "user", "alice", 1)
    ]
    assert [(c.id, c.message_count, c.last_sync_at, c.unavailable) for c in argentina.chats] == [
        (NEWS_ID, 2, None, True),
        (ARG_ID, 3, 1_700_000_000, False),
    ]
    assert empty.chats == []
    assert [(c.id, c.message_count) for c in orphan.chats] == [(GEORGIA_ID, 4)]


def test_sources_status_empty(conn: sqlite3.Connection) -> None:
    assert sources.sources_status(_cfg(), conn) == []
    db.upsert_chat(conn, _chat(5, source_id=None))  # type: ignore[arg-type]
    assert sources.sources_status(_cfg(), conn) == []


# --- CLI -------------------------------------------------------------------------------------


def _signed_in(tmp_home: Path, extra: str = "") -> Path:
    (tmp_home / "config.toml").write_text(CONFIG_WITH_KEYS + extra, encoding="utf-8")
    Paths.from_env().session_file.touch()
    return tmp_home / "config.toml"


def test_cli_sources_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["sources", "--help"])
    assert result.exit_code == 0, result.output
    for name in ("add", "ls", "rm"):
        assert name in result.output


def test_cli_sources_add_writes_config(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = _signed_in(tmp_home)
    fake = _client()
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: fake)
    result = runner.invoke(cli.app, ["sources", "add", "@arg_chat"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"added chat:@arg_chat: supergroup 'Argentina chat' (id {ARG_ID})",
        "next: grepogram sync",
    ]
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    loaded = config.load(Paths.from_env())
    assert loaded.telegram.api_id == 12345
    assert loaded.sources == [Source(chat="@arg_chat")]
    assert fake.calls[-1] == ("disconnect", {})
    folder = runner.invoke(
        cli.app, ["sources", "add", "folder:Argentina", "--since", "2024-01-01", "--comments"]
    )
    assert folder.exit_code == 0, folder.output
    assert (
        folder.stdout.splitlines()[0] == "added folder:Argentina: folder 'Argentina' with 3 chats"
    )
    assert config.load(Paths.from_env()).sources == [
        Source(chat="@arg_chat"),
        Source(folder="Argentina", since="2024-01-01", comments=True),
    ]


def test_cli_sources_add_reports_source_errors(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _signed_in(tmp_home)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: _client())
    ambiguous = runner.invoke(cli.app, ["sources", "add", "arg"])
    assert ambiguous.exit_code == 1
    assert "be more specific" in ambiguous.stderr
    assert "folder:Argentina" in ambiguous.stderr
    assert ambiguous.stdout == ""
    invalid = runner.invoke(cli.app, ["sources", "add", "https://t.me/+abc"])
    assert invalid.exit_code == 1
    assert "invite links" in invalid.stderr
    unknown = runner.invoke(cli.app, ["sources", "add", "@nobody_here"])
    assert unknown.exit_code == 1
    assert "@nobody_here" in unknown.stderr
    assert config.load(Paths.from_env()) == config.loads(config_file.read_text())
    assert config.load(Paths.from_env()).sources == []


def test_cli_sources_add_requires_keys_and_session(tmp_home: Path) -> None:
    no_keys = runner.invoke(cli.app, ["sources", "add", "@arg_chat"])
    assert no_keys.exit_code == 1
    assert "my.telegram.org" in no_keys.stderr
    (tmp_home / "config.toml").write_text(CONFIG_WITH_KEYS, encoding="utf-8")
    no_session = runner.invoke(cli.app, ["sources", "add", "@arg_chat"])
    assert no_session.exit_code == 1
    assert "run: grepogram auth" in no_session.stderr


def test_cli_sources_add_maps_network_errors(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)
    broken = _client()

    async def failing_connect() -> None:
        raise ConnectionError("offline")

    monkeypatch.setattr(broken, "connect", failing_connect)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: broken)
    result = runner.invoke(cli.app, ["sources", "add", "@arg_chat"])
    assert result.exit_code == 1
    assert "telegram error: offline" in result.stderr


def test_cli_sources_ls_prints_table_and_empty_message(tmp_home: Path) -> None:
    empty = runner.invoke(cli.app, ["sources", "ls"])
    assert empty.exit_code == 0, empty.output
    assert "no sources configured" in empty.stdout
    (tmp_home / "config.toml").write_text(
        '[[sources]]\nfolder = "Argentina"\n\n[[sources]]\nchat = "@alice"\n', encoding="utf-8"
    )
    conn = db.connect(Paths.from_env())
    db.migrate(conn)
    _store(conn, _chat(ARG_ID, "folder:Argentina", title="Argentina chat", username="arg_chat"), 3)
    db.set_chat_progress(conn, ARG_ID, last_msg_id=3, last_sync_at=1_700_000_000)
    _store(conn, _chat(NEWS_ID, "folder:Argentina", type="channel", title="News"))
    db.set_chat_unavailable(conn, NEWS_ID)
    conn.close()
    result = runner.invoke(cli.app, ["sources", "ls"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == [
        "source",
        "id",
        "type",
        "title",
        "username",
        "messages",
        "last",
        "sync",
        "status",
    ]
    assert lines[1].startswith(f"folder:Argentina  {NEWS_ID}  channel     News")
    assert lines[1].endswith("0         never             unavailable")
    assert lines[2].startswith(f"folder:Argentina  {ARG_ID}  supergroup  Argentina chat  @arg_chat")
    assert "  3  " in lines[2] and lines[2].endswith("ok")
    assert lines[3].startswith("chat:@alice")
    assert lines[3].endswith("not synced yet")
    assert not any(line.endswith(" ") for line in lines)


def test_cli_sources_rm_removes_entry_and_data(tmp_home: Path) -> None:
    config_file = tmp_home / "config.toml"
    config_file.write_text(
        '[[sources]]\nfolder = "Argentina"\n\n[[sources]]\nchat = "@alice"\n', encoding="utf-8"
    )
    conn = db.connect(Paths.from_env())
    db.migrate(conn)
    _store(conn, _chat(1, "chat:@alice", type="user", title="Alice Liddell", username="alice"), 2)
    _store(conn, _chat(ARG_ID, "folder:Argentina", title="Argentina chat"), 3)
    conn.close()
    result = runner.invoke(cli.app, ["sources", "rm", "alice"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "removed chat:@alice (1 chats deleted)"
    assert config.load(Paths.from_env()).sources == [Source(folder="Argentina")]
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    conn = db.connect(Paths.from_env())
    assert [c.id for c in db.list_chats(conn)] == [ARG_ID]
    conn.close()
    refused = runner.invoke(cli.app, ["sources", "rm", "Argentina chat"])
    assert refused.exit_code == 1
    assert "indexed through folder:Argentina" in refused.stderr
    unknown = runner.invoke(cli.app, ["sources", "rm", "zzz"])
    assert unknown.exit_code == 1
    assert "no source matches 'zzz'" in unknown.stderr
    assert unknown.stdout == ""
    folder = runner.invoke(cli.app, ["sources", "rm", "folder:Argentina"])
    assert folder.exit_code == 0, folder.output
    assert folder.stdout.strip() == "removed folder:Argentina (1 chats deleted)"
    assert config.load(Paths.from_env()).sources == []


def test_cli_sources_rm_works_offline_without_api_keys(tmp_home: Path) -> None:
    (tmp_home / "config.toml").write_text('[[sources]]\nchat = "@alice"\n', encoding="utf-8")
    result = runner.invoke(cli.app, ["sources", "rm", "@alice"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "removed chat:@alice (0 chats deleted)"


def test_cli_when_formats_timestamps() -> None:
    assert cli._when(None) == "never"
    rendered = cli._when(1_700_000_000)
    assert len(rendered) == 16 and rendered[4] == "-" and rendered[10] == " "


def test_dialog_info_type_alias_used_by_added() -> None:
    info = DialogInfo(id=1, title="x", type="user")
    assert sources.chat_value(info) == 1
    assert sources.chat_value(DialogInfo(id=1, title="x", type="user", username="u_name")) == (
        "@u_name"
    )
