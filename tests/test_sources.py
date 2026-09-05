import dataclasses
import fcntl
import logging
import os
import sqlite3
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from telethon import TelegramClient, errors
from typer.testing import CliRunner

from grepogram import cli, config, db, sources, sync, tg
from grepogram.dialogs import DialogCatalog, DialogInfo
from grepogram.models import ChatRow, Config, MessageRow, Source
from grepogram.paths import Paths
from grepogram.sources import (
    AmbiguousTarget,
    DuplicateSource,
    InvalidTarget,
    SourceError,
    UnknownSource,
    UnknownTarget,
)
from grepogram.tdesktop import ImportedChat
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


DISC_ID = -1000000000201


def _with_news_and_its_group(conn: sqlite3.Connection, group_source: str) -> None:
    _store(conn, _chat(NEWS_ID, "chat:@news", type="channel", title="News", username="news"), 2)
    _store(
        conn,
        _chat(
            DISC_ID, group_source, title="News chat", username="news_chat", discussion_of=NEWS_ID
        ),
        3,
    )


def test_remove_source_refuses_a_discussion_group_indexed_through_its_channel(
    conn: sqlite3.Connection,
) -> None:
    _with_news_and_its_group(conn, "chat:@news")
    cfg = _cfg(Source(chat="@news", comments=True))
    for raw in (str(DISC_ID), "@news_chat", "News chat", "https://t.me/news_chat", "news chat"):
        with pytest.raises(SourceError, match=f"discussion group of channel {NEWS_ID}") as excinfo:
            sources.remove_source(cfg, conn, sources.parse_target(raw))
        assert "indexed through chat:@news; remove that source instead" in str(excinfo.value)
    assert db.message_counts(conn) == {NEWS_ID: 2, DISC_ID: 3}
    removed = sources.remove_source(cfg, conn, sources.parse_target("@news"))
    assert removed.source_id == "chat:@news" and sorted(removed.chat_ids) == [DISC_ID, NEWS_ID]
    assert removed.config.sources == [] and db.list_chats(conn) == []


def test_remove_source_refuses_a_group_a_channel_was_unlinked_from(
    conn: sqlite3.Connection,
) -> None:
    """The link is cleared when Telegram unlinks the group, but the channel's source still holds
    its rows — naming the group must not silently remove the channel with it."""
    _with_news_and_its_group(conn, "chat:@news")
    db.set_discussion_chat(conn, NEWS_ID, None)
    cfg = _cfg(Source(chat="@news", comments=True))
    with pytest.raises(SourceError) as excinfo:
        sources.remove_source(cfg, conn, sources.parse_target("@news_chat"))
    assert "indexed through chat:@news; remove that source instead" in str(excinfo.value)
    assert "discussion group" not in str(excinfo.value)
    assert db.message_counts(conn) == {NEWS_ID: 2, DISC_ID: 3}


def test_remove_source_of_a_discussion_group_that_is_a_source_of_its_own(
    conn: sqlite3.Connection,
) -> None:
    _with_news_and_its_group(conn, f"chat:{DISC_ID}")
    cfg = _cfg(Source(chat="@news", comments=True), Source(chat=DISC_ID))
    removed = sources.remove_source(cfg, conn, sources.parse_target("News chat"))
    assert removed.source_id == f"chat:{DISC_ID}" and removed.chat_ids == [DISC_ID]
    assert removed.config.sources == [Source(chat="@news", comments=True)]
    assert db.message_counts(conn) == {NEWS_ID: 2}
    _store(
        conn,
        _chat(
            DISC_ID,
            "chat:@News_Chat",
            title="News chat",
            username="news_chat",
            discussion_of=NEWS_ID,
        ),
        1,
    )
    by_handle = sources.remove_source(
        _cfg(Source(chat="@News_Chat")), conn, sources.parse_target(str(DISC_ID))
    )
    assert by_handle.source_id == "chat:@News_Chat" and by_handle.chat_ids == [DISC_ID]


CHAT_SPELLINGS = [str(DISC_ID), "@news_chat", "https://t.me/news_chat", "t.me/c/201"]
"""Every documented spelling of ``chat =`` for the same discussion group."""


@pytest.mark.parametrize("spelling", CHAT_SPELLINGS)
def test_discussion_source_id_keeps_a_group_its_own_source_covers(spelling: str) -> None:
    """A ``chat:`` entry naming the group owns it whichever spelling it is written in; only the
    text of ``Source.chat`` differs, and identity is what decides."""
    group = _chat(
        DISC_ID,
        f"chat:{spelling}",
        title="News chat",
        username="news_chat",
        discussion_of=NEWS_ID,
    )
    channel = _chat(NEWS_ID, "chat:@news", type="channel", title="News", username="news")
    assert sources.discussion_source_id(group, channel) == f"chat:{spelling}"


def test_discussion_source_id_keeps_a_group_held_as_an_import() -> None:
    """An ``import:`` tag is an ownership claim like a folder's or a ``chat:`` entry's, and the
    one every writer of ``source_id`` has to honour: the channel's source must never replace it.
    `sync.link_discussion_chat` refuses such a link outright before this is asked, so this is
    the rule the refusal rests on rather than the guard itself."""
    group = _chat(DISC_ID, "import:news-chat", title="News chat", unavailable=True)
    channel = _chat(NEWS_ID, "folder:News", type="channel", title="News", username="news")
    assert sources.discussion_source_id(group, channel) == "import:news-chat"


@pytest.mark.parametrize("spelling", CHAT_SPELLINGS)
def test_remove_source_of_a_group_configured_under_any_spelling(
    conn: sqlite3.Connection, spelling: str
) -> None:
    _with_news_and_its_group(conn, f"chat:{spelling}")
    cfg = _cfg(Source(chat="@news", comments=True), Source(chat=spelling))
    for raw in (str(DISC_ID), "@news_chat", "https://t.me/news_chat", "News chat"):
        found = sources.find_source(cfg, conn, sources.parse_target(raw))
        assert found == f"chat:{spelling}"
    removed = sources.remove_source(cfg, conn, sources.parse_target("@news_chat"))
    assert removed.source_id == f"chat:{spelling}" and removed.chat_ids == [DISC_ID]
    assert removed.config.sources == [Source(chat="@news", comments=True)]
    assert db.message_counts(conn) == {NEWS_ID: 2}


@pytest.mark.parametrize(
    ("spelling", "targets"),
    [
        (str(DISC_ID), [str(DISC_ID), "t.me/c/201", f"chat:{DISC_ID}"]),
        ("t.me/c/201", [str(DISC_ID), "https://t.me/c/201/7"]),
        ("@news_chat", ["@news_chat", "@NEWS_CHAT", "https://t.me/news_chat"]),
        ("https://t.me/news_chat", ["@news_chat", "t.me/news_chat"]),
    ],
)
def test_remove_source_of_a_configured_chat_that_was_never_synced(
    conn: sqlite3.Connection, spelling: str, targets: list[str]
) -> None:
    """No ``chats`` row to resolve the target through, so the configured entries are all there
    is to match against — and a target matches the one whose identity it shares, not the one
    spelled the same way. Nothing maps an ``@username`` to an id without a stored chat, so only
    targets of the entry's own kind can find it."""
    cfg = _cfg(Source(chat=spelling))
    for raw in targets:
        removed = sources.remove_source(cfg, conn, sources.parse_target(raw))
        assert removed.source_id == f"chat:{spelling}"
        assert removed.chat_ids == [] and removed.config.sources == []


def test_with_source_appends_and_rejects_duplicates() -> None:
    cfg = _cfg(Source(chat="@alice"))
    added = sources.with_source(cfg, Source(chat="@news", comments=True), None)
    assert added.sources == [Source(chat="@alice"), Source(chat="@news", comments=True)]
    assert cfg.sources == [Source(chat="@alice")]
    with pytest.raises(DuplicateSource, match="chat:@alice is already a source"):
        sources.with_source(added, Source(chat="@alice"), None)
    alice = DialogInfo(id=1, type="user", title="Alice Liddell", username="alice")
    with pytest.raises(DuplicateSource, match="already a source as chat:@alice"):
        sources.with_source(added, Source(chat=1), alice)


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
    for name in ("add", "ls", "rm", "prune"):
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


def _config_lock_held(paths: Paths) -> bool:
    """Whether another descriptor — another process, as far as ``flock`` is concerned — is
    refused the config lock right now."""
    fd = os.open(paths.config_lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def test_cli_sources_add_keeps_a_change_saved_while_the_target_resolved(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP server removes ``@alice`` while ``sources add`` is talking to Telegram; the add
    is applied to the file as it is by then, under the config lock, so the removal survives."""
    _signed_in(tmp_home, '[[sources]]\nchat = "@alice"\n')
    paths = Paths.from_env()
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: _client())
    real_add = cli._add_source

    async def add_after_the_removal(
        client: Any, cfg: Config, target: sources.Target, since: str | None, comments: bool
    ) -> sources.Added:
        added = await real_add(client, cfg, target, since, comments)
        config.update(paths, lambda current: dataclasses.replace(current, sources=[]))
        return added

    monkeypatch.setattr(cli, "_add_source", add_after_the_removal)
    result = runner.invoke(cli.app, ["sources", "add", "@arg_chat"])
    assert result.exit_code == 0, result.output
    assert config.load(paths).sources == [Source(chat="@arg_chat")]
    assert stat.S_IMODE(paths.config_lock_file.stat().st_mode) == 0o600
    assert not _config_lock_held(paths)


def test_cli_sources_add_refuses_a_source_another_process_added_meanwhile(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)
    paths = Paths.from_env()
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: _client())
    real_add = cli._add_source

    async def add_after_the_other_add(
        client: Any, cfg: Config, target: sources.Target, since: str | None, comments: bool
    ) -> sources.Added:
        added = await real_add(client, cfg, target, since, comments)
        config.update(paths, lambda current: sources.with_source(current, added.source, None))
        return added

    monkeypatch.setattr(cli, "_add_source", add_after_the_other_add)
    result = runner.invoke(cli.app, ["sources", "add", "@arg_chat"])
    assert result.exit_code == 1
    assert "already" in result.stderr and result.stdout == ""
    assert config.load(paths).sources == [Source(chat="@arg_chat")]


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


def test_cli_sources_rm_refuses_while_a_sync_runs(tmp_home: Path) -> None:
    (tmp_home / "config.toml").write_text('[[sources]]\nchat = "@alice"\n', encoding="utf-8")
    with sync.SyncLock(Paths.from_env()):
        result = runner.invoke(cli.app, ["sources", "rm", "@alice"])
    assert result.exit_code == 1
    assert "another sync is running" in result.stderr and result.stdout == ""
    assert config.load(Paths.from_env()).sources == [Source(chat="@alice")]
    freed = runner.invoke(cli.app, ["sources", "rm", "@alice"])
    assert freed.exit_code == 0, freed.output


def test_cli_sources_rm_saves_the_config_under_the_sync_lock(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync that takes the lock the moment ``rm`` releases it must already read a config
    without the source, or it would re-create the chats ``rm`` just deleted."""
    (tmp_home / "config.toml").write_text('[[sources]]\nchat = "@alice"\n', encoding="utf-8")
    real_save = config.save
    held: list[bool] = []

    def save(cfg: Config, paths: Paths) -> None:
        with pytest.raises(sync.SyncInProgress), sync.SyncLock(paths):
            pass
        held.append(True)
        real_save(cfg, paths)

    monkeypatch.setattr(config, "save", save)
    result = runner.invoke(cli.app, ["sources", "rm", "@alice"])
    assert result.exit_code == 0, result.output
    assert held == [True] and config.load(Paths.from_env()).sources == []


def test_cli_sources_rm_applies_to_the_config_as_stored_under_the_config_lock(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An MCP ``sources_add`` saves the Argentina folder after ``rm`` read the file and before
    it took the locks; the removal is applied to the file with the folder in it, not to the
    earlier snapshot, and runs with the config lock held."""
    (tmp_home / "config.toml").write_text('[[sources]]\nchat = "@alice"\n', encoding="utf-8")
    paths = Paths.from_env()
    real_load = cli._load
    real_remove = sources.remove_source

    def load_then_lose_the_race() -> tuple[Paths, Config, sqlite3.Connection]:
        loaded = real_load()
        config.update(
            paths,
            lambda current: dataclasses.replace(
                current, sources=[*current.sources, Source(folder="Argentina")]
            ),
        )
        return loaded

    def remove_under_the_locks(
        cfg: Config, conn: sqlite3.Connection, target: sources.Target
    ) -> sources.Removed:
        assert [s.id for s in cfg.sources] == ["chat:@alice", "folder:Argentina"]
        assert _config_lock_held(paths)
        with pytest.raises(sync.SyncInProgress), sync.SyncLock(paths):
            pass
        return real_remove(cfg, conn, target)

    monkeypatch.setattr(cli, "_load", load_then_lose_the_race)
    monkeypatch.setattr(sources, "remove_source", remove_under_the_locks)
    result = runner.invoke(cli.app, ["sources", "rm", "@alice"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "removed chat:@alice (0 chats deleted)"
    assert config.load(paths).sources == [Source(folder="Argentina")]
    assert not _config_lock_held(paths)


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


# --- added by the review fixes --------------------------------------------------------------


async def test_entity_errors_only_unknown_peers_become_unknown_targets() -> None:
    private = _client(entity_errors={GHOST_ID: errors.ChannelPrivateError(request=None)})
    with pytest.raises(UnknownTarget, match="no dialog with id"):
        await sources.add_source(
            _cfg(), sources.parse_target(str(GHOST_ID)), DialogCatalog(private)
        )
    flooded = _client(entity_errors={GHOST_ID: errors.FloodWaitError(request=None, capture=30)})
    with pytest.raises(errors.FloodWaitError):
        await sources.add_source(
            _cfg(), sources.parse_target(str(GHOST_ID)), DialogCatalog(flooded)
        )


async def test_resolve_sources_skips_private_folder_peers_but_not_flood_waits(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    private = _client(entity_errors={GHOST_ID: errors.ChannelPrivateError(request=None)})
    with caplog.at_level(logging.WARNING, logger="grepogram.sources"):
        rows = await sources.resolve_sources(_cfg(Source(folder="Argentina")), private, conn)
    assert {r.id for r in rows} == {ARG_ID, NEWS_ID, OUTSIDE_ID}
    assert any("cannot resolve peer" in r.getMessage() for r in caplog.records)
    flooded = _client(entity_errors={GHOST_ID: errors.FloodWaitError(request=None, capture=30)})
    with pytest.raises(errors.FloodWaitError):
        await sources.resolve_sources(_cfg(Source(folder="Argentina")), flooded, conn)


async def test_username_targets_are_served_from_the_dialog_memo() -> None:
    client = _client()
    added = await sources.add_source(
        _cfg(), sources.parse_target("@arg_chat"), DialogCatalog(client)
    )
    assert added.source == Source(chat="@arg_chat")
    assert all(name != "get_entity" for name, _ in client.calls)


async def test_duplicate_check_ignores_fuzzy_and_malformed_chat_values() -> None:
    cfg = _cfg(Source(chat="georgia"), Source(chat="@ab"))
    added = await sources.add_source(cfg, sources.parse_target("@news"), _catalog())
    assert added.source == Source(chat="@news")
    assert len(added.config.sources) == 3


async def test_ambiguous_folder_names_are_refused_for_add_and_remove(
    conn: sqlite3.Connection,
) -> None:
    folders = [
        make_folder(3, "Argentina", include=[ARG]),
        make_folder(6, "Argentine", include=[NEWS]),
    ]
    client = FakeClient(dialogs=[make_dialog(ARG), make_dialog(NEWS)], folders=folders)
    with pytest.raises(AmbiguousTarget) as added:
        await sources.add_source(
            _cfg(), sources.parse_target("folder:argentin"), DialogCatalog(client)
        )
    assert added.value.candidates == [
        "folder 'Argentina' (folder:Argentina)",
        "folder 'Argentine' (folder:Argentine)",
    ]
    cfg = _cfg(Source(folder="Argentina"), Source(folder="Argentine"))
    with pytest.raises(AmbiguousTarget) as removed:
        sources.remove_source(cfg, conn, sources.parse_target("folder:argentin"))
    assert removed.value.candidates == ["folder:Argentina", "folder:Argentine"]


def test_remove_source_by_id_or_username_finds_the_other_spelling(
    conn: sqlite3.Connection,
) -> None:
    _populate(conn)
    _store(conn, _chat(555, "chat:555", title="X chat", username="xchat"), 1)
    cfg = _cfg(*CFG.sources, Source(chat=555))
    by_id = sources.remove_source(cfg, conn, sources.parse_target("1"))
    assert (by_id.source_id, by_id.chat_ids) == ("chat:@alice", [1])
    assert Source(chat="@alice") not in by_id.config.sources
    by_username = sources.remove_source(by_id.config, conn, sources.parse_target("@xchat"))
    assert (by_username.source_id, by_username.chat_ids) == ("chat:555", [555])
    assert Source(chat=555) not in by_username.config.sources


# --- prune -----------------------------------------------------------------------------------


LEFT_ID = -1000000000555


def _left(source_id: str = "folder:Argentina", **overrides: object) -> ChatRow:
    """A chat indexed through a folder that no longer lists it."""
    return _chat(LEFT_ID, source_id, title="Left chat", **overrides)


def _membership(
    listed: dict[str, set[int]] | None = None, failed: dict[str, str] | None = None
) -> sources.FolderMembership:
    return sources.FolderMembership(listed=listed or {}, failed=failed or {})


async def test_folder_membership_lists_every_peer_a_folder_names() -> None:
    cfg = _cfg(Source(folder="Argentina"), Source(chat="@alice"))
    membership = await sources.folder_membership(cfg, _catalog())
    # GHOST_ID has no entity to resolve, and is still listed by the folder: `folder_dialogs`
    # drops such a peer, and reading that as "it left" is what would delete a live chat
    assert membership.listed == {"folder:Argentina": {ARG_ID, NEWS_ID, OUTSIDE_ID, GHOST_ID}}
    assert membership.failed == {}


async def test_folder_membership_drops_a_peer_the_folder_also_excludes() -> None:
    """A folder can name a peer in ``include``/``pinned`` and in ``exclude`` at once, and
    ``exclude`` wins in Telegram. Reading the peer as listed would make ``sources prune`` keep a
    chat the folder has actually dropped — and the explicit-peer union is exactly the half that
    would keep it, because ``folder_dialogs`` already leaves it out."""
    folder = make_folder(3, "Argentina", include=[ARG, GHOST_ID], pinned=[NEWS], exclude=[NEWS])
    client = FakeClient(dialogs=[make_dialog(ARG), make_dialog(NEWS)], folders=[folder])
    membership = await sources.folder_membership(
        _cfg(Source(folder="Argentina")), DialogCatalog(client)
    )
    assert membership.listed == {"folder:Argentina": {ARG_ID, GHOST_ID}}


async def test_folder_membership_records_an_unresolvable_source_instead_of_skipping_it() -> None:
    cfg = _cfg(Source(folder="Xyz"), Source(folder="Argentina"))
    membership = await sources.folder_membership(cfg, _catalog())
    assert set(membership.listed) == {"folder:Argentina"}
    assert "no folder named" in membership.failed["folder:Xyz"]


async def test_folder_membership_records_a_transient_rpc_error() -> None:
    flooded = _catalog(entity_errors={GHOST_ID: errors.FloodWaitError(request=None, capture=30)})
    membership = await sources.folder_membership(_cfg(Source(folder="Argentina")), flooded)
    assert membership.listed == {}
    assert "wait" in membership.failed["folder:Argentina"].casefold()


def test_prunable_offers_a_chat_the_folder_no_longer_lists(conn: sqlite3.Connection) -> None:
    _populate(conn)
    _store(conn, _left(), 4)
    scan = sources.prunable(CFG, conn, _membership({"folder:Argentina": {ARG_ID, NEWS_ID}}))
    assert [(c.chat.id, c.messages) for c in scan.prunable] == [(LEFT_ID, 4)]
    assert scan.prunable[0].reason == "folder:Argentina no longer lists it"
    assert scan.kept == [] and scan.unresolved == []


def test_prunable_keeps_the_chats_the_folder_still_lists(conn: sqlite3.Connection) -> None:
    _populate(conn)
    scan = sources.prunable(CFG, conn, _membership({"folder:Argentina": {ARG_ID, NEWS_ID}}))
    assert scan.prunable == [] and scan.kept == []


def test_prunable_prunes_nothing_when_the_source_fails_to_resolve(
    conn: sqlite3.Connection,
) -> None:
    """The hazard this command exists around: `resolve_sources` logs and skips a source it
    cannot resolve, so deriving "the folder no longer lists them" from a failed resolution
    would offer a whole indexed history for deletion after one transient RPCError."""
    _populate(conn)
    _store(conn, _left(), 4)
    scan = sources.prunable(
        CFG, conn, _membership(failed={"folder:Argentina": "A wait of 30 seconds is required"})
    )
    assert scan.prunable == []
    assert scan.unresolved == ["folder:Argentina: A wait of 30 seconds is required"]
    # every chat of the source is held back, not only the one that looked gone
    assert {c.chat.id for c in scan.kept} == {LEFT_ID, ARG_ID, NEWS_ID}
    assert {c.reason for c in scan.kept} == {"folder:Argentina could not be checked"}


def test_prunable_prunes_nothing_while_another_source_is_unchecked(
    conn: sqlite3.Connection,
) -> None:
    """Coverage is a union over every source, so one folder that did not answer means no chat
    can be proved uncovered — not even a chat of a folder that did answer."""
    _populate(conn)
    _store(conn, _left(), 4)
    cfg = _cfg(*CFG.sources, Source(folder="Other"))
    scan = sources.prunable(
        cfg,
        conn,
        _membership({"folder:Argentina": {ARG_ID, NEWS_ID}}, {"folder:Other": "no folder named"}),
    )
    assert scan.prunable == [] and scan.unresolved == ["folder:Other: no folder named"]


def test_prunable_keeps_a_chat_covered_by_another_source(conn: sqlite3.Connection) -> None:
    """A chat two sources cover keeps the first source's id, so the stored tag says nothing
    about who covers it now."""
    _populate(conn)
    _store(conn, _left(), 4)
    by_folder = _cfg(*CFG.sources, Source(folder="Other"))
    scan = sources.prunable(
        by_folder,
        conn,
        _membership({"folder:Argentina": {ARG_ID, NEWS_ID}, "folder:Other": {LEFT_ID}}),
    )
    assert scan.prunable == [] and scan.kept == []


def test_prunable_keeps_a_chat_a_chat_entry_names(conn: sqlite3.Connection) -> None:
    _populate(conn)
    _store(conn, _left(username="left_chat"), 4)
    by_username = _cfg(*CFG.sources, Source(chat="https://t.me/left_chat"))
    listed = _membership({"folder:Argentina": {ARG_ID, NEWS_ID}})
    assert sources.prunable(by_username, conn, listed).prunable == []
    by_id = _cfg(*CFG.sources, Source(chat=LEFT_ID))
    assert sources.prunable(by_id, conn, listed).prunable == []


def test_prunable_keeps_a_discussion_group_a_channel_still_links(
    conn: sqlite3.Connection,
) -> None:
    _populate(conn)
    _store(conn, _left(type="supergroup"), 4)
    db.set_discussion_chat(conn, NEWS_ID, LEFT_ID)
    scan = sources.prunable(CFG, conn, _membership({"folder:Argentina": {ARG_ID, NEWS_ID}}))
    assert scan.prunable == []
    assert [(c.chat.id, c.reason) for c in scan.kept] == [
        (LEFT_ID, f"still the discussion group of channel {NEWS_ID}")
    ]


def test_prunable_offers_a_discussion_group_whose_channel_is_gone(
    conn: sqlite3.Connection,
) -> None:
    _populate(conn)
    _store(conn, _left(discussion_of=-1000000000777), 4)
    scan = sources.prunable(CFG, conn, _membership({"folder:Argentina": {ARG_ID, NEWS_ID}}))
    assert [c.chat.id for c in scan.prunable] == [LEFT_ID]


def test_prunable_never_offers_an_imported_chat(conn: sqlite3.Connection) -> None:
    _populate(conn)
    _store(conn, _chat(LEFT_ID, "import:left-chat", title="Left chat"), 4)
    scan = sources.prunable(CFG, conn, _membership({"folder:Argentina": {ARG_ID, NEWS_ID}}))
    assert scan.prunable == [] and scan.kept == []


def test_prunable_keeps_a_chat_whose_source_is_no_longer_configured(
    conn: sqlite3.Connection,
) -> None:
    _populate(conn)
    _store(conn, _left("folder:Gone"), 4)
    scan = sources.prunable(CFG, conn, _membership({"folder:Argentina": {ARG_ID, NEWS_ID}}))
    assert scan.prunable == []
    assert [(c.chat.id, c.reason) for c in scan.kept] == [
        (LEFT_ID, "folder:Gone is not a configured source; use sources rm")
    ]


def _offered(chat: ChatRow) -> sources.PruneCandidate:
    """The candidate ``prunable`` would build for a chat it offers."""
    return sources.PruneCandidate(
        chat=chat, reason=f"{chat.source_id} no longer lists it", messages=0
    )


def test_prune_chats_deletes_the_rows_and_skips_ids_that_are_gone(
    conn: sqlite3.Connection,
) -> None:
    _populate(conn)
    news = _offered(_chat(NEWS_ID, "folder:Argentina", type="channel", title="News"))
    ghost = _offered(_chat(GHOST_ID, "folder:Argentina"))
    removed = sources.prune_chats(conn, [news, ghost, news])
    assert removed == [NEWS_ID]
    assert [c.id for c in db.list_chats(conn)] == sorted([ARG_ID, GEORGIA_ID, 1])
    assert db.message_counts(conn) == {ARG_ID: 3, GEORGIA_ID: 4, 1: 1}


def test_prune_chats_keeps_a_chat_imported_since_the_scan(conn: sqlite3.Connection) -> None:
    """The race the scan cannot close: the offer predates the sync lock by a confirmation
    prompt, and `sources rm` plus an import of the same chat fit inside one. An imported
    history has no dialog behind it, so deleting it on a stale verdict loses it for good."""
    _populate(conn)
    offer = _offered(_chat(NEWS_ID, "folder:Argentina", type="channel", title="News"))
    db.upsert_chat(conn, _chat(NEWS_ID, "import:news", type="channel", title="News"))
    assert sources.prune_chats(conn, [offer]) == []
    assert db.message_counts(conn)[NEWS_ID] == 2


def test_prune_chats_keeps_a_chat_another_source_took_over(conn: sqlite3.Connection) -> None:
    """A sync that resolved the chat under a different source between the scan and the lock:
    the verdict was about the folder that no longer lists it, and it no longer describes
    this row."""
    _populate(conn)
    offer = _offered(_chat(NEWS_ID, "folder:Argentina", type="channel", title="News"))
    db.upsert_chat(conn, _chat(NEWS_ID, "folder:Other", type="channel", title="News"))
    assert sources.prune_chats(conn, [offer]) == []
    assert db.message_counts(conn)[NEWS_ID] == 2


def test_prune_chats_keeps_a_chat_linked_as_a_discussion_group_since_the_scan(
    conn: sqlite3.Connection,
) -> None:
    """`prunable` keeps a channel's discussion group; a sync can have made it one since."""
    _populate(conn)
    group = _chat(NEWS_ID, "folder:Argentina", type="supergroup", title="News chat")
    _store(conn, group, 2)
    db.set_discussion_chat(conn, ARG_ID, NEWS_ID)
    assert sources.prune_chats(conn, [_offered(group)]) == []
    assert db.get_chat(conn, NEWS_ID) is not None


# --- prune, through the CLI ------------------------------------------------------------------


def _prune_home(tmp_home: Path, monkeypatch: pytest.MonkeyPatch, extra: str = "") -> Paths:
    """A signed-in home with the Argentina folder as its only source, holding one chat the
    folder still lists and one it does not."""
    _signed_in(tmp_home, '[[sources]]\nfolder = "Argentina"\n' + extra)
    paths = Paths.from_env()
    conn = db.connect(paths)
    db.migrate(conn)
    _store(conn, _chat(ARG_ID, "folder:Argentina", title="Argentina chat", username="arg_chat"), 3)
    _store(conn, _left(), 4)
    conn.close()
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: _client())
    return paths


def _indexed(paths: Paths) -> list[int]:
    conn = db.connect(paths)
    try:
        return [chat.id for chat in db.list_chats(conn)]
    finally:
        conn.close()


def test_cli_sources_prune_deletes_after_a_confirmation(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prune_home(tmp_home, monkeypatch)
    result = runner.invoke(cli.app, ["sources", "prune"], input="y\n")
    assert result.exit_code == 0, result.output
    assert f"{LEFT_ID}" in result.stdout and "folder:Argentina no longer lists it" in result.stdout
    assert "removed 1 chats" in result.stdout
    assert _indexed(paths) == [ARG_ID]
    again = runner.invoke(cli.app, ["sources", "prune"])
    assert again.exit_code == 0, again.output
    assert again.stdout.strip() == "nothing to prune"


def test_cli_sources_prune_dry_run_changes_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prune_home(tmp_home, monkeypatch)
    result = runner.invoke(cli.app, ["sources", "prune", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "1 chats would go" in result.stdout
    assert _indexed(paths) == sorted([ARG_ID, LEFT_ID])
    assert config.load(paths).sources == [Source(folder="Argentina")]


def test_cli_sources_prune_declined_removes_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prune_home(tmp_home, monkeypatch)
    result = runner.invoke(cli.app, ["sources", "prune"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "nothing removed" in result.stdout
    assert _indexed(paths) == sorted([ARG_ID, LEFT_ID])


def test_cli_sources_prune_refuses_when_a_source_cannot_be_resolved(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prune_home(tmp_home, monkeypatch, extra='\n[[sources]]\nfolder = "Xyz"\n')
    result = runner.invoke(cli.app, ["sources", "prune"], input="y\n")
    assert result.exit_code == 1
    assert "nothing was pruned" in result.stderr and "folder:Xyz" in result.stderr
    assert "does not resolve is not a source that lists nothing" in result.stderr
    assert _indexed(paths) == sorted([ARG_ID, LEFT_ID])


def test_cli_sources_prune_keeps_a_linked_discussion_group_and_says_why(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prune_home(tmp_home, monkeypatch)
    conn = db.connect(paths)
    _store(conn, _chat(NEWS_ID, "folder:Argentina", type="channel", title="News"))
    db.set_discussion_chat(conn, NEWS_ID, LEFT_ID)
    conn.close()
    result = runner.invoke(cli.app, ["sources", "prune"], input="y\n")
    assert result.exit_code == 0, result.output
    assert f"kept Left chat (id {LEFT_ID}): still the discussion group" in result.stdout
    assert "nothing to prune" in result.stdout
    assert _indexed(paths) == sorted([ARG_ID, NEWS_ID, LEFT_ID])


def test_cli_sources_prune_refuses_while_a_sync_runs(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _prune_home(tmp_home, monkeypatch)
    with sync.SyncLock(paths):
        result = runner.invoke(cli.app, ["sources", "prune"], input="y\n")
    assert result.exit_code == 1
    assert "another sync is running" in result.stderr
    assert _indexed(paths) == sorted([ARG_ID, LEFT_ID])
    freed = runner.invoke(cli.app, ["sources", "prune"], input="y\n")
    assert freed.exit_code == 0, freed.output
    assert _indexed(paths) == [ARG_ID]


def test_cli_sources_prune_resolves_before_it_takes_the_sync_lock(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram is read first and the lock is taken for the deletion alone: a round trip held
    across the sync lock would block every sync for as long as Telegram takes to answer."""
    paths = _prune_home(tmp_home, monkeypatch)
    real_membership = cli._folder_membership
    real_prune = sources.prune_chats
    order: list[str] = []

    async def membership_without_the_lock(
        client: TelegramClient, cfg: Config
    ) -> sources.FolderMembership:
        with sync.SyncLock(paths):  # free while the network is being read
            order.append("resolved")
        return await real_membership(client, cfg)

    def prune_under_the_lock(
        conn: sqlite3.Connection, candidates: Sequence[sources.PruneCandidate]
    ) -> list[int]:
        with pytest.raises(sync.SyncInProgress), sync.SyncLock(paths):
            pass
        order.append("deleted")
        return real_prune(conn, candidates)

    monkeypatch.setattr(cli, "_folder_membership", membership_without_the_lock)
    monkeypatch.setattr(sources, "prune_chats", prune_under_the_lock)
    result = runner.invoke(cli.app, ["sources", "prune"], input="y\n")
    assert result.exit_code == 0, result.output
    assert order == ["resolved", "deleted"]
    assert _indexed(paths) == [ARG_ID]


def test_cli_sources_prune_needs_keys_a_session_and_sources(tmp_home: Path) -> None:
    no_keys = runner.invoke(cli.app, ["sources", "prune"])
    assert no_keys.exit_code == 1 and "my.telegram.org" in no_keys.stderr
    (tmp_home / "config.toml").write_text(CONFIG_WITH_KEYS, encoding="utf-8")
    no_sources = runner.invoke(cli.app, ["sources", "prune"])
    assert no_sources.exit_code == 1 and "no sources configured" in no_sources.stderr
    (tmp_home / "config.toml").write_text(
        CONFIG_WITH_KEYS + '[[sources]]\nfolder = "Argentina"\n', encoding="utf-8"
    )
    no_session = runner.invoke(cli.app, ["sources", "prune"])
    assert no_session.exit_code == 1 and "run: grepogram auth" in no_session.stderr


def test_cli_sources_prune_maps_network_errors(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prune_home(tmp_home, monkeypatch)
    broken = _client()

    async def failing_connect() -> None:
        raise ConnectionError("offline")

    monkeypatch.setattr(broken, "connect", failing_connect)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: broken)
    result = runner.invoke(cli.app, ["sources", "prune"])
    assert result.exit_code == 1
    assert "telegram error: offline" in result.stderr


# --- import ----------------------------------------------------------------------------------


def _export(*chats: ChatRow, messages: int = 1) -> list[ImportedChat]:
    """A parsed export: what :func:`grepogram.tdesktop.read_export` hands the importer."""
    return [
        ImportedChat(
            chat=chat,
            messages=[
                MessageRow(chat_id=chat.id, msg_id=i, date=1_700_000_000 + i, text=f"import {i}")
                for i in range(1, messages + 1)
            ],
        )
        for chat in chats
    ]


def _imported(chat_id: int, title: str | None) -> ChatRow:
    """A chat as :func:`grepogram.tdesktop.read_export` builds it: no source, never fetched."""
    return ChatRow(id=chat_id, type="supergroup", title=title, unavailable=True)


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Valencia Expats", "valencia-expats"),
        ("Грузия | Georgia chat", "грузия-georgia-chat"),
        ("  ...Trip 2019!  ", "trip-2019"),
        ("a" * 60, "a" * 40),
        ("🙂", ""),
        (None, ""),
    ],
    ids=["ascii", "cyrillic", "punctuation", "truncated", "emoji-only", "unnamed"],
)
def test_import_slug_reads_a_title_as_the_readable_half_of_a_source_id(
    title: str | None, slug: str
) -> None:
    assert sources.import_slug(title) == slug


def test_import_source_ids_are_the_slugged_titles(conn: sqlite3.Connection) -> None:
    chats = [_imported(LEFT_ID, "Left chat"), _imported(ARG_ID, "Argentina chat")]
    assert sources.import_source_ids(conn, chats) == {
        LEFT_ID: "import:left-chat",
        ARG_ID: "import:argentina-chat",
    }


def test_import_source_ids_disambiguate_a_shared_title(conn: sqlite3.Connection) -> None:
    """Two contacts of one name in a single export must not share a source id: ``sources rm``
    on either would then delete both."""
    chats = [_imported(LEFT_ID, "Anna"), _imported(ARG_ID, "Anna")]
    assert sources.import_source_ids(conn, chats) == {
        LEFT_ID: f"import:anna-{abs(LEFT_ID)}",
        ARG_ID: f"import:anna-{abs(ARG_ID)}",
    }


def test_import_source_ids_avoid_a_slug_an_earlier_import_claimed(
    conn: sqlite3.Connection,
) -> None:
    _store(conn, _chat(ARG_ID, "import:anna", title="Anna"))
    assert sources.import_source_ids(conn, [_imported(LEFT_ID, "Anna")]) == {
        LEFT_ID: f"import:anna-{abs(LEFT_ID)}"
    }
    # the chat that holds the plain slug keeps it, so re-importing it stays idempotent
    assert sources.import_source_ids(conn, [_imported(ARG_ID, "Anna")]) == {ARG_ID: "import:anna"}


def test_import_source_ids_disambiguate_again_when_the_first_fallback_collides(
    conn: sqlite3.Connection,
) -> None:
    """The fallback is ``<slug>-<abs(chat_id)>``, which is itself a title someone can have.

    Sharing an id would make ``sources rm`` on either delete both histories, so the chat id is
    appended again until nothing else holds the result.
    """
    chats = [_imported(LEFT_ID, "Anna"), _imported(ARG_ID, "Anna"), _imported(NEWS_ID, "Anna")]
    ids = sources.import_source_ids(conn, [*chats, _imported(GEORGIA_ID, f"Anna {abs(ARG_ID)}")])
    assert len(set(ids.values())) == 4, "no two chats of one export share a source id"
    assert {ids[ARG_ID], ids[GEORGIA_ID]} == {
        f"import:anna-{abs(ARG_ID)}",
        f"import:anna-{abs(ARG_ID)}-{abs(ARG_ID)}",
    }


def test_import_source_ids_do_not_depend_on_the_order_of_the_export(
    conn: sqlite3.Connection,
) -> None:
    chats = [_imported(LEFT_ID, "Anna"), _imported(ARG_ID, "Anna"), _imported(NEWS_ID, "Anna")]
    assert sources.import_source_ids(conn, chats) == sources.import_source_ids(
        conn, list(reversed(chats))
    )


def test_import_source_ids_fall_back_to_the_chat_id_for_a_nameless_chat(
    conn: sqlite3.Connection,
) -> None:
    assert sources.import_source_ids(conn, [_imported(LEFT_ID, None)]) == {
        LEFT_ID: f"import:chat-{abs(LEFT_ID)}"
    }


def test_import_chats_stores_the_rows_under_an_import_tag(conn: sqlite3.Connection) -> None:
    stored = sources.import_chats(conn, _export(_imported(LEFT_ID, "Left chat"), messages=3))
    assert [(item.source_id, item.messages) for item in stored] == [("import:left-chat", 3)]
    chat = db.get_chat(conn, LEFT_ID)
    assert chat is not None
    assert chat.source_id == "import:left-chat"
    # nothing was fetched from Telegram, so no sync resumes from this chat
    assert chat.unavailable and chat.last_msg_id == 0
    assert db.message_counts(conn) == {LEFT_ID: 3}


def test_import_chats_is_idempotent(conn: sqlite3.Connection) -> None:
    entries = _export(_imported(LEFT_ID, "Left chat"), messages=3)
    sources.import_chats(conn, entries)
    again = sources.import_chats(conn, entries)
    assert [item.source_id for item in again] == ["import:left-chat"]
    assert db.message_counts(conn) == {LEFT_ID: 3}


def test_import_chats_refuses_a_chat_already_synced_from_telegram(
    conn: sqlite3.Connection,
) -> None:
    """`db.upsert_chat` overwrites `source_id`, so an import over a live chat would retag it and
    hide it from the source that fetches it."""
    _populate(conn)
    with pytest.raises(sources.ImportConflict) as excinfo:
        sources.import_chats(conn, _export(_imported(ARG_ID, "Argentina chat")))
    assert "already indexed from Telegram through folder:Argentina" in str(excinfo.value)
    assert "grepogram sources rm" in str(excinfo.value)


def test_import_chats_refuses_a_stored_chat_that_carries_no_source(
    conn: sqlite3.Connection,
) -> None:
    """An untagged row is still a row this index got from Telegram; refusing is the safe way to
    be wrong about it."""
    _store(conn, ChatRow(id=LEFT_ID, type="supergroup", title="Left chat"), 2)
    with pytest.raises(sources.ImportConflict, match="with no source"):
        sources.import_chats(conn, _export(_imported(LEFT_ID, "Left chat")))


def test_import_chats_writes_nothing_when_one_chat_of_the_export_is_refused(
    conn: sqlite3.Connection,
) -> None:
    """The refusal is checked for the whole export before the first row goes in, so a partial
    import never leaves half a history under a tag the other half does not carry."""
    _populate(conn)
    entries = _export(_imported(LEFT_ID, "Left chat"), _imported(ARG_ID, "Argentina chat"))
    with pytest.raises(sources.ImportConflict):
        sources.import_chats(conn, entries)
    assert db.get_chat(conn, LEFT_ID) is None
    assert db.message_counts(conn) == {ARG_ID: 3, NEWS_ID: 2, GEORGIA_ID: 4, 1: 1}


def test_refuse_imported_refuses_a_live_source_over_an_imported_chat(
    conn: sqlite3.Connection,
) -> None:
    sources.import_chats(conn, _export(_imported(ARG_ID, "Argentina chat")))
    covered = [DialogInfo(id=ARG_ID, type="supergroup", title="Argentina chat")]
    with pytest.raises(sources.ImportConflict) as excinfo:
        sources.refuse_imported(conn, covered)
    assert "already in the index as import:argentina-chat" in str(excinfo.value)
    assert "grepogram sources rm import:argentina-chat" in str(excinfo.value)


def test_refuse_imported_passes_a_chat_that_is_not_an_import(conn: sqlite3.Connection) -> None:
    _populate(conn)
    sources.refuse_imported(
        conn,
        [
            DialogInfo(id=ARG_ID, type="supergroup", title="Argentina chat"),
            DialogInfo(id=GHOST_ID, type="channel", title="Never indexed"),
        ],
    )


def test_refuse_imported_refuses_a_folder_holding_one_imported_chat(
    conn: sqlite3.Connection,
) -> None:
    """Adding the folder would cover that chat on every sync from now on, so the whole folder is
    refused rather than the one member silently taken over."""
    sources.import_chats(conn, _export(_imported(NEWS_ID, "News")))
    covered = [
        DialogInfo(id=ARG_ID, type="supergroup", title="Argentina chat"),
        DialogInfo(id=NEWS_ID, type="channel", title="News"),
    ]
    with pytest.raises(sources.ImportConflict, match="import:news"):
        sources.refuse_imported(conn, covered)


async def test_resolve_sources_leaves_an_imported_chat_under_its_import_tag(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """The crossing `refuse_imported` does not cover: `sources add` is guarded, but the folder
    gaining the chat on Telegram afterwards is not, and this runs on every sync.

    `db.upsert_chat` writes `source_id` unconditionally, so a resolve would replace
    `import:argentina-chat` with `folder:Argentina` — and `sources prune`, which keys on the
    prefix, would then offer the whole imported history for deletion.
    """
    sources.import_chats(conn, _export(_imported(ARG_ID, "Argentina chat"), messages=3))
    with caplog.at_level(logging.INFO, logger="grepogram.sources"):
        rows = await sources.resolve_sources(_cfg(Source(folder="Argentina")), _client(), conn)
    assert ARG_ID not in [row.id for row in rows], "an import is not a chat to sync"
    chat = db.get_chat(conn, ARG_ID)
    assert chat is not None and chat.source_id == "import:argentina-chat"
    assert chat.unavailable and chat.last_msg_id == 0
    assert "import:argentina-chat" in caplog.text and "sources rm" in caplog.text
    held = [record for record in caplog.records if "import:argentina-chat" in record.getMessage()]
    assert [record.levelname for record in held] == ["INFO"], (
        "a standing state of the index, logged once per held chat on every sync — including "
        "every automatic one inside an MCP search — is not a warning"
    )
    scan = sources.prunable(
        _cfg(Source(folder="Argentina")), conn, _membership({"folder:Argentina": {NEWS_ID}})
    )
    assert ARG_ID not in [c.chat.id for c in scan.prunable], "never offered for pruning"
    assert db.message_counts(conn)[ARG_ID] == 3


def _import_home(tmp_home: Path, monkeypatch: pytest.MonkeyPatch, chat_id: int) -> Paths:
    """A signed-in home whose index already holds ``chat_id`` as a Telegram Desktop import."""
    _signed_in(tmp_home)
    paths = Paths.from_env()
    conn = db.connect(paths)
    db.migrate(conn)
    sources.import_chats(conn, _export(_imported(chat_id, "Argentina chat"), messages=2))
    conn.close()
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: _client())
    return paths


def test_cli_sources_add_refuses_an_imported_chat_and_leaves_its_tag(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this guard `db.upsert_chat` would replace `import:argentina-chat` with
    `chat:@arg_chat` on the next sync, and `sources prune` — which keys on the prefix — would
    then offer the imported history for deletion."""
    paths = _import_home(tmp_home, monkeypatch, ARG_ID)
    result = runner.invoke(cli.app, ["sources", "add", "@arg_chat"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "already in the index as import:argentina-chat" in result.stderr
    assert config.load(paths).sources == []
    conn = db.connect(paths)
    try:
        chat = db.get_chat(conn, ARG_ID)
        assert chat is not None and chat.source_id == "import:argentina-chat"
        assert db.message_counts(conn) == {ARG_ID: 2}
    finally:
        conn.close()


def test_cli_sources_add_refuses_a_folder_that_holds_an_imported_chat(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _import_home(tmp_home, monkeypatch, ARG_ID)
    result = runner.invoke(cli.app, ["sources", "add", "folder:Argentina"])
    assert result.exit_code == 1
    assert "already in the index as import:argentina-chat" in result.stderr
    assert config.load(paths).sources == []


def test_cli_sources_rm_removes_an_imported_chat_by_its_source_id(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal above tells the user to do exactly this, so it has to work."""
    paths = _import_home(tmp_home, monkeypatch, ARG_ID)
    result = runner.invoke(cli.app, ["sources", "rm", "import:argentina-chat"])
    assert result.exit_code == 0, result.output
    assert "removed import:argentina-chat (1 chats deleted)" in result.stdout
    conn = db.connect(paths)
    try:
        assert db.list_chats(conn) == []
    finally:
        conn.close()
