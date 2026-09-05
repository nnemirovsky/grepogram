import asyncio
import dataclasses
import datetime as dt
import sqlite3
import stat
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlite_vec
from telethon import errors
from telethon.tl import functions, types
from telethon.tl.types import messages as tl_messages
from typer.testing import CliRunner

from grepogram import cli, db, search, sources, sync, tg, units
from grepogram.config import ConfigError
from grepogram.models import (
    ChatRow,
    Config,
    Filters,
    MessageRow,
    PruneReport,
    Source,
    SyncCfg,
    SyncReport,
    TelegramCfg,
)
from grepogram.paths import Paths
from grepogram.sync import SyncBudget, SyncInProgress, SyncLock
from tests.fakes import FakeClient, make_channel, make_dialog, make_folder, make_group, make_user
from tests.fixtures import tl

runner = CliRunner()

ALICE = make_user(1, "Alice", "Liddell", username="alice")
BOB = make_user(2, "Bob")
ME = make_user(42, "Me", "Myself", username="me")
OLD_GROUP = make_group(10, "Old group", migrated_to=101)
ARG = make_channel(100, "Argentina chat", username="arg_chat", megagroup=True)
GEORGIA = make_channel(101, "Georgia chat", megagroup=True)
NEWS = make_channel(200, "News", username="news")
DISC = make_channel(201, "News chat", username="news_chat", megagroup=True)
DISC2 = make_channel(202, "Second news chat", username="news_chat2", megagroup=True)
OTHER = make_channel(203, "Other news", username="other_news")
FORUM_DISC = make_channel(204, "Forum news chat", username="forum_chat", megagroup=True, forum=True)

ALICE_ID = 1
OLD_ID = -10
ARG_ID = -1000000000100
GEORGIA_ID = -1000000000101
NEWS_ID = -1000000000200
DISC_ID = -1000000000201
DISC2_ID = -1000000000202
OTHER_ID = -1000000000203
FORUM_DISC_ID = -1000000000204

TELEGRAM = TelegramCfg(api_id=12345, api_hash="fakehash")
ARG_SOURCE = Source(folder="Argentina")
NEWS_SOURCE = Source(chat="@news", comments=True)
ALICE_SOURCE = Source(chat="@alice")


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths.under(tmp_path / "home")


def _client(**kwargs: object) -> FakeClient:
    dialogs = [
        make_dialog(ALICE),
        make_dialog(BOB),
        make_dialog(OLD_GROUP),
        make_dialog(ARG),
        make_dialog(GEORGIA),
        make_dialog(NEWS),
    ]
    kwargs.setdefault("folders", [make_folder(3, "Argentina", include=[ARG])])
    kwargs.setdefault("entities", [DISC])
    kwargs.setdefault("me", ME)
    return FakeClient(dialogs=dialogs, **kwargs)  # type: ignore[arg-type]


def _cfg(*entries: Source, edit_refetch: int = 200) -> Config:
    return Config(telegram=TELEGRAM, sync=SyncCfg(edit_refetch=edit_refetch), sources=list(entries))


def _full_channel(
    linked: int | None, *, chats: list[types.Channel] | None = None
) -> tl_messages.ChatFull:
    full = types.ChannelFull(
        id=200,
        about="",
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        chat_photo=types.PhotoEmpty(0),
        notify_settings=types.PeerNotifySettings(),
        bot_info=[],
        pts=0,
        linked_chat_id=linked,
    )
    if chats is None:
        chats = [NEWS, DISC] if linked else [NEWS]
    return tl_messages.ChatFull(full_chat=full, chats=chats, users=[])


def _comments(disc_id: int = DISC_ID) -> dict[tuple[int, int], list[types.Message]]:
    return {
        (NEWS_ID, 1): [
            tl.message(disc_id, 1, "comment one", sender=1, reply_to=tl.reply_header(7)),
            tl.message(disc_id, 2, "reply", sender=2, reply_to=tl.reply_header(1)),
        ],
        (NEWS_ID, 3): [tl.message(disc_id, 9, "late comment", sender=2)],
    }


def _posts(*counts: int | None) -> list[types.Message]:
    """Posts 1..n of the News channel; ``counts`` are the reply counts Telegram reports on them
    (``None`` for a post without a ``MessageReplies`` header). By default the three posts
    :func:`_comments` has threads for: two comments on post 1, none on 2, one on 3."""
    counts = counts or (2, 0, 1)
    return [
        tl.channel_post(NEWS_ID, i, f"post {i}", replies=count)
        for i, count in enumerate(counts, start=1)
    ]


def _news_client(linked: int | None = 201, **kwargs: object) -> FakeClient:
    kwargs.setdefault("messages", {NEWS_ID: _posts()})
    kwargs.setdefault("comments", _comments())
    kwargs.setdefault(
        "responses", {functions.channels.GetFullChannelRequest: _full_channel(linked)}
    )
    return _client(**kwargs)


def _discussion_client(disc: types.Channel) -> FakeClient:
    """A channel whose discussion group is a dialog of its own, in the "News" folder with it,
    and whose history holds the very messages the comment threads return."""
    disc_id = -1000000000000 - disc.id
    comments = _comments(disc_id)
    return FakeClient(
        dialogs=[make_dialog(ALICE), make_dialog(BOB), make_dialog(NEWS), make_dialog(disc)],
        me=ME,
        folders=[make_folder(3, "News", include=[NEWS, disc])],
        messages={
            NEWS_ID: _posts(),
            disc_id: [m for pool in comments.values() for m in pool],
        },
        comments=comments,
        responses={
            functions.channels.GetFullChannelRequest: _full_channel(disc.id, chats=[NEWS, disc])
        },
    )


def _forum_discussion_client() -> FakeClient:
    """A channel whose discussion group is a forum as well as its comment store.

    The group's topic 3 is numbered like the channel's post 3, and messages 3 and 4 sit in that
    topic without being comments on anything — the shape where a single ``topic_id`` could not
    say which of the two id spaces a number came from.
    """
    comments = _comments(FORUM_DISC_ID)
    history = [
        *comments[(NEWS_ID, 1)],
        tl.topic_message(FORUM_DISC_ID, 3, "topic three opens", topic_id=3, sender=1),
        tl.reply_message(
            FORUM_DISC_ID, 4, "still in topic three", reply_to=3, topic_id=3, sender=2
        ),
        *comments[(NEWS_ID, 3)],
    ]
    return FakeClient(
        dialogs=[
            make_dialog(ALICE),
            make_dialog(BOB),
            make_dialog(NEWS),
            make_dialog(FORUM_DISC),
        ],
        me=ME,
        folders=[make_folder(3, "News", include=[NEWS, FORUM_DISC])],
        messages={NEWS_ID: _posts(), FORUM_DISC_ID: history},
        comments=comments,
        responses={
            functions.channels.GetFullChannelRequest: _full_channel(
                FORUM_DISC.id, chats=[NEWS, FORUM_DISC]
            )
        },
    )


def _forum_state(conn: sqlite3.Connection) -> dict[str, object]:
    """What the forum topic of the discussion group looks like: its messages, their topic and
    the window they are filed under."""
    return {
        "topics": {
            m.msg_id: m.topic_id for m in db.get_messages(conn, FORUM_DISC_ID) if m.topic_id
        },
        "windows": sorted(
            (u.topic_id, tuple(u.msg_ids))
            for u in db.get_units(conn, FORUM_DISC_ID)
            if u.kind == "window" and u.topic_id is not None
        ),
    }


def _clock(*ticks: float) -> Callable[[], float]:
    """A monotonic clock returning ``ticks`` in order, then the last value forever."""
    values = iter(ticks)
    last = ticks[-1]

    def read() -> float:
        return next(values, last)

    return read


def _arg_chat(conn: sqlite3.Connection) -> ChatRow:
    chat = db.get_chat(conn, ARG_ID)
    assert chat is not None
    return chat


def _found(conn: sqlite3.Connection, word: str) -> set[int]:
    """The chats whose units a message search for ``word`` reaches — a message no unit holds any
    more is simply not there, so this is what "still searchable" means."""
    hits = search.lexical_messages(conn, word, Filters(), 20)
    return {u.chat_id for u in db.get_units_by_ids(conn, [m.unit_id for m in hits])}


def _texts(conn: sqlite3.Connection, chat_id: int) -> dict[int, str]:
    return {m.msg_id: m.text for m in db.get_messages(conn, chat_id)}


def _fetch_calls(client: FakeClient, chat_id: int) -> list[dict[str, object]]:
    return [kw for name, kw in client.calls if name == "iter_messages" and kw["chat_id"] == chat_id]


async def _run(
    client: FakeClient,
    conn: sqlite3.Connection,
    paths: Paths,
    cfg: Config,
    seconds: float | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    recut: bool = True,
) -> SyncReport:
    async with tg.connected(client):
        return await sync.sync_all(
            client, conn, cfg, paths, SyncBudget(seconds, clock=clock), recut=recut
        )


# --- budget ----------------------------------------------------------------------------------


def test_budget_without_seconds_never_expires() -> None:
    budget = SyncBudget()
    assert budget.seconds is None
    assert budget.deadline is None
    assert not budget.expired
    assert budget.remaining is None


def test_budget_expires_by_its_clock() -> None:
    budget = SyncBudget(5, clock=_clock(0, 3, 6, 9))
    assert budget.deadline == 5
    assert budget.remaining == 2
    assert budget.expired
    assert budget.remaining == 0


def test_budget_zero_is_expired_at_once() -> None:
    assert SyncBudget(0).expired


def test_a_cancelled_budget_is_expired_whatever_its_clock_says() -> None:
    """What a cancelled run uses to stop the worker thread it is waiting for."""
    unlimited = SyncBudget()
    unlimited.cancel()
    assert unlimited.expired
    assert unlimited.remaining == 0
    timed = SyncBudget(5, clock=_clock(0))
    assert not timed.expired
    timed.cancel()
    assert timed.expired
    assert timed.remaining == 0


# --- lock ------------------------------------------------------------------------------------


def test_lock_creates_a_private_file_and_releases(paths: Paths) -> None:
    with SyncLock(paths):
        assert paths.lock_file.exists()
        assert stat.S_IMODE(paths.lock_file.stat().st_mode) == 0o600
        with pytest.raises(SyncInProgress):
            with SyncLock(paths):
                pass
    assert paths.lock_file.exists()
    with SyncLock(paths):
        pass


def test_lock_contention_raises_sync_in_progress(paths: Paths) -> None:
    with SyncLock(paths):
        with pytest.raises(SyncInProgress, match="another sync is running") as excinfo:
            with SyncLock(paths):
                pass
        assert str(paths.lock_file) in str(excinfo.value)
    with SyncLock(paths):
        pass


def test_lock_is_released_on_error(paths: Paths) -> None:
    with pytest.raises(RuntimeError):
        with SyncLock(paths):
            raise RuntimeError("boom")
    with SyncLock(paths):
        pass


# --- since -----------------------------------------------------------------------------------


def test_since_of_parses_iso_dates() -> None:
    assert sync.since_of(None) is None
    assert sync.since_of(Source(chat="@a")) is None
    assert sync.since_of(Source(chat="@a", since="2024-03-05")) == dt.datetime(
        2024, 3, 5, tzinfo=dt.UTC
    )
    with pytest.raises(ConfigError, match="chat:@a: since must be an ISO date.*'march'"):
        sync.since_of(Source(chat="@a", since="march"))


# --- sync_chat -------------------------------------------------------------------------------


async def test_first_run_stores_everything_and_records_progress(conn: sqlite3.Connection) -> None:
    client = _client(
        messages={
            ARG_ID: [
                tl.message(ARG_ID, 1, "hello", sender=1),
                tl.message(ARG_ID, 2, "world", sender=2),
                tl.service_message(ARG_ID, 3),
            ]
        }
    )
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup", title="Argentina chat"))
    synced = await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    assert synced.new == 2
    assert synced.complete
    assert not synced.unavailable
    assert synced.discussion is None
    assert synced.migrated_to is None
    assert _texts(conn, ARG_ID) == {1: "hello", 2: "world"}
    assert synced.chat.last_msg_id == 3
    assert synced.chat.last_sync_at is not None
    stored = {m.id for m in db.get_messages(conn, ARG_ID)}
    assert set(synced.new_msg_ids) == stored
    calls = _fetch_calls(client, ARG_ID)
    assert len(calls) == 1
    assert calls[0]["min_id"] == 0
    assert calls[0]["reverse"] is True
    assert calls[0]["offset_date"] is None
    assert calls[0]["limit"] is None


async def test_names_come_from_bound_peers_and_users_are_stored(conn: sqlite3.Connection) -> None:
    client = _client(messages={ARG_ID: [tl.message(ARG_ID, 1, "hi", sender=1)]})
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup", title="Argentina chat"))
    await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    message = db.get_message(conn, ARG_ID, 1)
    assert message is not None
    assert (message.from_id, message.from_name) == (1, "Alice Liddell")
    users = {
        row["id"]: (row["display_name"], row["username"])
        for row in conn.execute("SELECT * FROM users")
    }
    assert users[1] == ("Alice Liddell", "alice")
    assert users[ARG_ID] == ("Argentina chat", "arg_chat")


async def test_outgoing_private_messages_are_attributed_to_me(conn: sqlite3.Connection) -> None:
    client = _client(
        messages={
            ALICE_ID: [
                tl.message(ALICE_ID, 1, "hi", sender=1),
                tl.message(ALICE_ID, 2, "hello", out=True),
            ]
        }
    )
    chat = db.upsert_chat(conn, ChatRow(id=ALICE_ID, type="user", title="Alice Liddell"))
    me = sync.collect_users([ME])[42]
    await sync.sync_chat(client, conn, chat, ALICE_SOURCE, SyncBudget(), me=me)
    rows = {m.msg_id: (m.from_id, m.from_name) for m in db.get_messages(conn, ALICE_ID)}
    assert rows == {1: (1, "Alice Liddell"), 2: (42, "Me Myself")}


async def test_second_run_fetches_only_newer_messages(conn: sqlite3.Connection) -> None:
    client = _client(messages={ARG_ID: [tl.message(ARG_ID, i, f"m{i}", sender=1) for i in (1, 2)]})
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    first = await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    client.messages[ARG_ID].append(tl.message(ARG_ID, 3, "m3", sender=1))
    second = await sync.sync_chat(client, conn, first.chat, ARG_SOURCE, SyncBudget())
    assert second.new == 1
    assert _texts(conn, ARG_ID) == {1: "m1", 2: "m2", 3: "m3"}
    assert second.chat.last_msg_id == 3
    incremental = [c for c in _fetch_calls(client, ARG_ID) if c["reverse"]]
    assert [c["min_id"] for c in incremental] == [0, 2]
    new_row = db.get_message(conn, ARG_ID, 3)
    assert new_row is not None
    assert second.new_msg_ids == [new_row.id]


async def test_edit_refetch_updates_text_and_keeps_row_ids(conn: sqlite3.Connection) -> None:
    client = _client(
        messages={ARG_ID: [tl.message(ARG_ID, i, f"m{i}", sender=1) for i in (1, 2, 3)]}
    )
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    first = await sync.sync_chat(
        client, conn, chat, ARG_SOURCE, SyncBudget(), cfg=Config(sync=SyncCfg(edit_refetch=3))
    )
    assert all(c["limit"] is None for c in _fetch_calls(client, ARG_ID))
    before = db.get_message(conn, ARG_ID, 2)
    assert before is not None
    client.messages[ARG_ID][1] = tl.message(
        ARG_ID, 2, "m2 edited", sender=1, edit_date=tl.at(50), reactions=tl.reactions({"👍": 3})
    )
    client.messages[ARG_ID].append(tl.message(ARG_ID, 4, "m4", sender=1))
    second = await sync.sync_chat(
        client, conn, first.chat, ARG_SOURCE, SyncBudget(), cfg=Config(sync=SyncCfg(edit_refetch=3))
    )
    after = db.get_message(conn, ARG_ID, 2)
    assert after is not None
    assert after.id == before.id
    assert after.text == "m2 edited"
    assert after.reactions_total == 3
    assert after.edit_date is not None
    added = db.get_message(conn, ARG_ID, 4)
    assert added is not None
    assert second.new == 1
    assert second.new_msg_ids == [added.id, after.id]
    refetch = [c for c in _fetch_calls(client, ARG_ID) if c["limit"] is not None]
    assert len(refetch) == 1
    assert refetch[0]["limit"] == 3
    assert refetch[0]["reverse"] is False


def test_differs_ignores_the_columns_the_upsert_never_writes() -> None:
    """``extracted_text`` and ``media_state`` are the extraction pass's, and a row Telegram maps
    carries neither. Comparing them would make every extracted message inside the
    ``edit_refetch`` window an edit on every sync, re-cut and re-embedded for ever."""
    stored = MessageRow(
        id=7,
        chat_id=ARG_ID,
        msg_id=2,
        date=100,
        text="",
        media_kind="photo",
        extracted_text="visa office notice",
        media_state=db.MEDIA_EXTRACTED,
    )
    mapped = MessageRow(chat_id=ARG_ID, msg_id=2, date=100, text="", media_kind="photo")
    assert not sync._differs(stored, mapped)
    assert sync._differs(stored, dataclasses.replace(mapped, text="caption"))


async def test_edit_refetch_keeps_extracted_media_text_across_syncs(
    conn: sqlite3.Connection,
) -> None:
    """End to end over the pass that re-reads: the photo's OCR survives, and the message does not
    come back as an edit."""
    client = _client(
        messages={
            ARG_ID: [
                tl.message(ARG_ID, 1, "m1", sender=1),
                tl.photo_message(ARG_ID, 2, "at the embassy", sender=1),
            ]
        }
    )
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    first = await sync.sync_chat(
        client, conn, chat, ARG_SOURCE, SyncBudget(), cfg=Config(sync=SyncCfg(edit_refetch=2))
    )
    row = db.get_message(conn, ARG_ID, 2)
    assert row is not None and row.media_kind == "photo"
    conn.execute(
        "UPDATE messages SET extracted_text = ?, media_state = ? WHERE id = ?",
        ("visa office notice", db.MEDIA_EXTRACTED, row.id),
    )
    again = await sync.sync_chat(
        client, conn, first.chat, ARG_SOURCE, SyncBudget(), cfg=Config(sync=SyncCfg(edit_refetch=2))
    )
    assert again.new_msg_ids == []
    after = db.get_message(conn, ARG_ID, 2)
    assert after is not None
    assert after.extracted_text == "visa office notice"
    assert after.media_state == db.MEDIA_EXTRACTED


def _reacted_history() -> list[types.Message]:
    """Three messages numbered from 101, so the chat's rowids and its Telegram ids differ.

    They coincide from 1 in a chat fetched whole into an empty index, which is exactly how a
    reaction refresh handed the wrong id space passes its tests and refreshes nothing in the
    field. ``tl.message`` dates a message at its id in minutes, so 201 opens a second window.
    """
    return [
        tl.message(ARG_ID, 101, "where do I renew a residence permit", sender=1),
        tl.message(
            ARG_ID, 102, "at the migraciones office", sender=2, reactions=tl.reactions({"👍": 2})
        ),
        tl.message(ARG_ID, 103, "thanks", sender=1),
    ]


async def test_edit_refetch_refreshes_the_reaction_total_of_a_closed_window(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The signal only works if it keeps up: a reaction lands days after the window was cut.

    Nothing about the rebuild can deliver it — ``_content_key`` does not see reactions, so
    ``_apply`` keeps the stored row, and a closed window is never re-cut at all — so the refresh
    is a direct ``UPDATE`` on the ``edit_refetch`` path, and this asserts the unit's id survives
    it: a re-cut here would mean the total is being carried by a delete and re-embed instead.
    """
    client = _client(messages={ARG_ID: _reacted_history()})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    reacted = db.get_message(conn, ARG_ID, 102)
    assert reacted is not None and reacted.id != reacted.msg_id
    assert [(u.msg_ids, u.reactions) for u in db.get_units(conn, ARG_ID)] == [([101, 102, 103], 2)]

    client.messages[ARG_ID].append(tl.message(ARG_ID, 201, "any update?", sender=1))
    await _run(client, conn, paths, cfg)
    before = {u.msg_ids[0]: u.id for u in db.get_units(conn, ARG_ID) if u.kind == "window"}
    assert sorted(before) == [101, 201]

    client.messages[ARG_ID][1] = tl.message(
        ARG_ID,
        102,
        "at the migraciones office",
        sender=2,
        reactions=tl.reactions({"👍": 2, "🔥": 5}),
    )
    await _run(client, conn, paths, cfg)
    windows = {u.msg_ids[0]: u for u in db.get_units(conn, ARG_ID) if u.kind == "window"}
    assert {start: unit.id for start, unit in windows.items()} == before
    assert windows[101].reactions == 7
    assert windows[201].reactions == 0
    assert "migraciones" in windows[101].text


async def test_edit_refetch_is_skipped_when_nothing_changed_or_disabled(
    conn: sqlite3.Connection,
) -> None:
    client = _client(messages={ARG_ID: [tl.message(ARG_ID, 1, "m1", sender=1)]})
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    first = await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    unchanged = await sync.sync_chat(client, conn, first.chat, ARG_SOURCE, SyncBudget())
    assert unchanged.new_msg_ids == []
    assert unchanged.new == 0
    assert unchanged.chat.last_sync_at is not None
    client.calls.clear()
    disabled = await sync.sync_chat(
        client, conn, first.chat, ARG_SOURCE, SyncBudget(), cfg=Config(sync=SyncCfg(edit_refetch=0))
    )
    assert disabled.new_msg_ids == []
    assert all(c["limit"] is None for c in _fetch_calls(client, ARG_ID))


# --- deletions -------------------------------------------------------------------------------


def _talk(*ids: int) -> list[types.Message]:
    """Messages numbered from 101, so a chat's rowids and its Telegram ids never coincide.

    ``tl.message`` dates a message at its id in minutes and ``window_gap_min`` is 30, so a run of
    consecutive ids is one window and a jump of a hundred opens the next.
    """
    return [tl.message(ARG_ID, i, f"m{i}", sender=1) for i in ids]


def _unit_index(conn: sqlite3.Connection, chat_id: int) -> set[int]:
    """The ``unit_fts`` rowids of a chat — what the index answers a search from."""
    stored = {unit.id for unit in db.get_units(conn, chat_id)}
    rows = conn.execute("SELECT rowid FROM unit_fts").fetchall()
    return {int(row["rowid"]) for row in rows} & stored


async def test_a_message_deleted_in_telegram_is_dropped_and_its_window_recut(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """``iter_messages`` omits a deleted message rather than yielding a hole, so the deletion is
    the stored id inside the range it covered that it did not return. The row, its ``msg_fts``
    entry and its line in the window unit all go, and the unit's index row follows."""
    client = _client(messages={ARG_ID: _talk(101, 102, 103)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    gone = db.get_message(conn, ARG_ID, 102)
    assert gone is not None and gone.id != gone.msg_id

    del client.messages[ARG_ID][1]
    await _run(client, conn, paths, cfg)

    assert _texts(conn, ARG_ID) == {101: "m101", 103: "m103"}
    assert _windows(conn, ARG_ID) == [[101, 103]]
    text = db.get_units(conn, ARG_ID)[0].text
    assert "m102" not in text and "m103" in text
    indexed = conn.execute("SELECT rowid FROM msg_fts").fetchall()
    assert gone.id not in {int(row["rowid"]) for row in indexed}
    assert _unit_index(conn, ARG_ID) == {unit.id for unit in db.get_units(conn, ARG_ID)}


async def test_a_deletion_inside_a_closed_window_is_noticed(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The case that matters: a rebuild never re-cuts a closed window (``_recut_start`` returns
    ``None``), so without :func:`units.invalidate_units_for` the deleted line would stay in the
    unit's text for good."""
    client = _client(messages={ARG_ID: _talk(101, 102, 103)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    client.messages[ARG_ID].append(tl.message(ARG_ID, 201, "later", sender=1))
    await _run(client, conn, paths, cfg)
    assert _windows(conn, ARG_ID) == [[101, 102, 103], [201]]

    del client.messages[ARG_ID][1]
    await _run(client, conn, paths, cfg)

    assert _windows(conn, ARG_ID) == [[101, 103], [201]]
    closed = next(u for u in db.get_units(conn, ARG_ID) if u.msg_ids == [101, 103])
    assert "m102" not in closed.text


async def test_a_unit_left_with_no_messages_at_all_is_dropped(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """A whole window deleted between two that survive — the middle one bounds nothing, so the
    re-cut has no messages to build it from and the unit must go rather than be rebuilt empty."""
    client = _client(messages={ARG_ID: _talk(101, 102, 201, 202, 301)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    assert _windows(conn, ARG_ID) == [[101, 102], [201, 202], [301]]

    client.messages[ARG_ID] = [m for m in client.messages[ARG_ID] if m.id not in (201, 202)]
    await _run(client, conn, paths, cfg)

    assert _texts(conn, ARG_ID) == {101: "m101", 102: "m102", 301: "m301"}
    assert _windows(conn, ARG_ID) == [[101, 102], [301]]
    assert _unit_index(conn, ARG_ID) == {unit.id for unit in db.get_units(conn, ARG_ID)}


async def test_a_stored_id_outside_the_covered_range_is_never_removed(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Only the ids the iteration reached are evidence, and it reaches ``edit_refetch`` of them
    from the newest existing message down. A stored row above where it started or below where it
    stopped was never asked about, so it stays; the full sweep is what answers for those."""
    client = _client(messages={ARG_ID: _talk(101, 102, 103)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)

    del client.messages[ARG_ID][0]
    await _run(client, conn, paths, _cfg(ARG_SOURCE, edit_refetch=2))
    assert _texts(conn, ARG_ID) == {101: "m101", 102: "m102", 103: "m103"}

    del client.messages[ARG_ID][-1]
    await _run(client, conn, paths, cfg)

    assert _texts(conn, ARG_ID) == {101: "m101", 102: "m102", 103: "m103"}
    assert _windows(conn, ARG_ID) == [[101, 102, 103]]


async def test_a_deleted_thread_root_does_not_come_back_in_a_rebuilt_thread_unit(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The primitive re-reads every message it renders. Handed the rows as they were read,
    ``_chain_tops`` would make the deleted root a thread top again and ``build_threads`` would
    render it at the head — the deletion undone in the index by the very step meant to apply it.
    """
    client = _client(
        messages={
            ARG_ID: [
                tl.message(ARG_ID, 100, "hi", sender=1),
                tl.message(ARG_ID, 101, "where do I renew a residence permit", sender=1),
                tl.message(ARG_ID, 102, "at migraciones", sender=2, reply_to=tl.reply_header(101)),
                tl.message(ARG_ID, 301, "later", sender=1),
            ]
        }
    )
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    threads = [u for u in db.get_units(conn, ARG_ID) if u.kind == "thread"]
    assert [u.msg_ids for u in threads] == [[101, 102]]

    client.messages[ARG_ID] = [m for m in client.messages[ARG_ID] if m.id != 101]
    await _run(client, conn, paths, cfg)

    assert db.get_message(conn, ARG_ID, 101) is None
    assert all("residence permit" not in unit.text for unit in db.get_units(conn, ARG_ID))
    assert [u.msg_ids for u in db.get_units(conn, ARG_ID) if u.kind == "thread"] == []
    assert _unit_index(conn, ARG_ID) == {unit.id for unit in db.get_units(conn, ARG_ID)}


async def test_a_service_message_is_not_mistaken_for_a_deletion(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """``run.map`` returns ``None`` for a service message, so it is never stored — and its id is
    still one Telegram answered with, so nothing around it can look deleted either."""
    client = _client(
        messages={
            ARG_ID: [
                tl.message(ARG_ID, 101, "m101", sender=1),
                tl.service_message(ARG_ID, 102),
                tl.message(ARG_ID, 103, "m103", sender=1),
            ]
        }
    )
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    before = _windows(conn, ARG_ID)
    assert _texts(conn, ARG_ID) == {101: "m101", 103: "m103"}

    await _run(client, conn, paths, cfg)

    assert _texts(conn, ARG_ID) == {101: "m101", 103: "m103"}
    assert _windows(conn, ARG_ID) == before


async def test_a_service_message_still_bounds_the_range_a_deletion_is_read_from(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The other half of the same rule: a service id is in ``seen`` although no row is stored.

    ``seen`` is what says how far the iteration reached, and a service message is as much an
    answer as any other. Counting only the ids that mapped to a row would pull ``max(seen)`` back
    below the deleted message, which would then read as "never asked about" and stay indexed for
    good — this pass never reaches it again once a newer message is stored above it.
    """
    client = _client(messages={ARG_ID: _talk(101, 102)})
    cfg = _cfg(ARG_SOURCE, edit_refetch=2)
    await _run(client, conn, paths, cfg)
    assert _texts(conn, ARG_ID) == {101: "m101", 102: "m102"}

    client.messages[ARG_ID] = [
        tl.message(ARG_ID, 101, "m101", sender=1),
        tl.service_message(ARG_ID, 103),
    ]
    await _run(client, conn, paths, cfg)

    assert _texts(conn, ARG_ID) == {101: "m101"}, "102 was deleted between the two runs"
    assert _windows(conn, ARG_ID) == [[101]]


async def test_a_deleted_comment_is_left_to_the_full_sweep(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """A discussion group a folder lists directly is a source chat, so this pass runs over its
    comments too — and must leave them alone. The channel's post thread carries a comment's text
    while listing only the post in ``msg_ids``, so no ``json_each`` over ``units.msg_ids`` reaches
    it and nothing here could invalidate that thread; ``prune-deleted`` follows the
    ``comment_of_*`` pair instead. The group's own messages are removed as anywhere else."""
    client = _busy_discussion_client(DISC)
    cfg = _cfg(Source(folder="News", comments=True))
    await _run(client, conn, paths, cfg)
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1", "g1", "g2"]

    # comment 2 and plain message 5 both leave the group; Telegram reports one reply on post 1
    # from now on, so nothing re-fetches the thread and re-stores what was dropped.
    client.messages[DISC_ID] = [m for m in client.messages[DISC_ID] if m.id not in (2, 5)]
    client.comments[(NEWS_ID, 1)] = [m for m in client.comments[(NEWS_ID, 1)] if m.id != 2]
    client.messages[NEWS_ID][0] = tl.channel_post(NEWS_ID, 1, "post 1", replies=1)
    await _run(client, conn, paths, cfg)

    stored = _texts(conn, DISC_ID)
    assert 2 in stored and 5 not in stored
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1", "g1", "g2"]
    thread = next(u for u in db.get_units(conn, NEWS_ID) if u.kind == "thread")
    assert "g2" in thread.text
    window = next(u for u in db.get_units(conn, DISC_ID) if 2 in u.msg_ids)
    assert "g2" in window.text and "g5" not in window.text


# --- the full sweep --------------------------------------------------------------------------


async def _prune(
    client: FakeClient,
    conn: sqlite3.Connection,
    paths: Paths,
    cfg: Config,
    budget: SyncBudget | None = None,
    *,
    chat_id: int | None = None,
) -> PruneReport:
    async with tg.connected(client):
        return await sync.prune_deleted(
            client, conn, cfg, paths, budget or SyncBudget(), chat_id=chat_id
        )


def _swept(client: FakeClient) -> list[int]:
    """The chats the sweep asked Telegram about, first asked first."""
    asked = [kw["chat_id"] for name, kw in client.calls if name == "get_messages"]
    return list(dict.fromkeys(int(chat_id) for chat_id in asked))


async def test_the_sweep_drops_exactly_the_ids_telegram_answers_nothing_for(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """``get_messages(ids=[…])`` answers one slot per id and leaves the slot of a deleted message
    empty, so here — unlike the edit-refetch pass — an empty slot *is* the deletion. The row, its
    ``msg_fts`` entry and its line in the window all go, and the cursor is cleared once the sweep
    has reached the end of the chat."""
    client = _client(messages={ARG_ID: _talk(101, 102, 103)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    gone = db.get_message(conn, ARG_ID, 102)
    assert gone is not None and gone.id != gone.msg_id

    client.messages[ARG_ID] = [m for m in client.messages[ARG_ID] if m.id != 102]
    report = await _prune(client, conn, paths, cfg)

    assert (report.removed, report.checked) == (1, 3)
    assert report.chats_done == [ARG_ID]
    assert report.chats_remaining == []
    assert _texts(conn, ARG_ID) == {101: "m101", 103: "m103"}
    assert _windows(conn, ARG_ID) == [[101, 103]]
    assert "m102" not in db.get_units(conn, ARG_ID)[0].text
    indexed = {int(row["rowid"]) for row in conn.execute("SELECT rowid FROM msg_fts")}
    assert gone.id not in indexed
    assert _unit_index(conn, ARG_ID) == {unit.id for unit in db.get_units(conn, ARG_ID)}
    assert db.prune_cursor(conn, ARG_ID) == 0


async def test_a_budget_stops_the_sweep_and_the_next_run_resumes_from_the_cursor(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cursor is a Telegram ``msg_id``: ``messages.id`` is an ``INTEGER PRIMARY KEY`` with no
    ``AUTOINCREMENT``, so the rowids this very sweep frees are handed to the next insert."""
    monkeypatch.setattr(sync, "PRUNE_BATCH", 2)
    client = _client(messages={ARG_ID: _talk(101, 102, 103, 104, 105)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    client.messages[ARG_ID] = [m for m in client.messages[ARG_ID] if m.id not in (102, 104)]
    budget = SyncBudget()
    answer = client.get_messages

    async def stop_after_one_page(*args: Any, **kwargs: Any) -> Any:
        page = await answer(*args, **kwargs)
        budget.cancel()
        return page

    monkeypatch.setattr(client, "get_messages", stop_after_one_page)
    first = await _prune(client, conn, paths, cfg, budget)

    assert (first.removed, first.checked) == (1, 2)
    assert first.chats_remaining == [ARG_ID]
    assert db.prune_cursor(conn, ARG_ID) == 102
    assert 104 in _texts(conn, ARG_ID)

    monkeypatch.setattr(client, "get_messages", answer)
    second = await _prune(client, conn, paths, cfg)

    assert (second.removed, second.checked) == (1, 3)
    assert second.chats_done == [ARG_ID]
    assert sorted(_texts(conn, ARG_ID)) == [101, 103, 105]
    assert db.prune_cursor(conn, ARG_ID) == 0


async def test_a_flood_wait_keeps_what_the_sweep_earned(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flood wait is about the account, so the run ends — with the page it committed before it
    and a cursor that starts the next run where this one stopped."""
    monkeypatch.setattr(sync, "PRUNE_BATCH", 2)
    client = _client(messages={ARG_ID: _talk(101, 102, 103, 104, 105)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    client.messages[ARG_ID] = [m for m in client.messages[ARG_ID] if m.id not in (102, 104)]
    answer = client.get_messages
    pages = 0

    async def flood_after_one_page(*args: Any, **kwargs: Any) -> Any:
        nonlocal pages
        pages += 1
        if pages > 1:
            raise errors.FloodWaitError(request=None, capture=30)
        return await answer(*args, **kwargs)

    monkeypatch.setattr(client, "get_messages", flood_after_one_page)
    report = await _prune(client, conn, paths, cfg)

    assert report.removed == 1
    assert report.chats_remaining == [ARG_ID]
    assert any("flood wait" in warning for warning in report.warnings)
    assert db.prune_cursor(conn, ARG_ID) == 102
    assert 102 not in _texts(conn, ARG_ID)
    assert 104 in _texts(conn, ARG_ID)


async def test_an_error_that_is_not_a_deletion_removes_nothing(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """A chat that went private mid-sweep says nothing about what it holds; the sweep must not
    read a refusal as a hundred deletions."""
    client = _client(messages={ARG_ID: _talk(101, 102, 103)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    before = _texts(conn, ARG_ID)

    client.failures[ARG_ID] = errors.ChannelPrivateError(request=None)
    report = await _prune(client, conn, paths, cfg)

    assert (report.removed, report.checked) == (0, 0)
    assert report.chats_remaining == [ARG_ID]
    assert any(str(ARG_ID) in warning for warning in report.warnings)
    assert _texts(conn, ARG_ID) == before
    assert db.prune_cursor(conn, ARG_ID) == 0


async def test_an_answer_that_does_not_line_up_with_the_page_removes_nothing(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short answer would make every id it left out look deleted at once, so an answer of
    another shape than the question is read as no answer at all."""
    client = _client(messages={ARG_ID: _talk(101, 102, 103)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    answer = client.get_messages

    async def truncated(*args: Any, **kwargs: Any) -> Any:
        return (await answer(*args, **kwargs))[:1]

    monkeypatch.setattr(client, "get_messages", truncated)
    report = await _prune(client, conn, paths, cfg)

    assert (report.removed, report.checked) == (0, 0), "a refused page has checked nothing"
    assert report.chats_remaining == [ARG_ID]
    assert any("did not line up" in warning for warning in report.warnings)
    assert _texts(conn, ARG_ID) == {101: "m101", 102: "m102", 103: "m103"}
    assert db.prune_cursor(conn, ARG_ID) == 0


async def test_a_deleted_comment_invalidates_the_channels_post_thread(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The case Task 12 leaves here. A post thread lists the post alone in ``msg_ids``, so no
    ``json_each`` over ``units.msg_ids`` reaches a comment id — the way from a deleted comment to
    the thread quoting it is the ``comment_of_*`` pair on the comment's own row."""
    client = _busy_discussion_client(DISC)
    cfg = _cfg(Source(folder="News", comments=True))
    await _run(client, conn, paths, cfg)
    before = next(u for u in db.get_units(conn, NEWS_ID) if u.kind == "thread" and u.msg_ids == [1])
    assert "g2" in before.text

    client.messages[DISC_ID] = [m for m in client.messages[DISC_ID] if m.id != 2]
    report = await _prune(client, conn, paths, cfg)

    assert report.removed == 1
    assert db.get_message(conn, DISC_ID, 2) is None
    thread = next(u for u in db.get_units(conn, NEWS_ID) if u.kind == "thread" and u.msg_ids == [1])
    assert "g1" in thread.text
    assert "g2" not in thread.text
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1", "g1"]
    window = next(u for u in db.get_units(conn, DISC_ID) if 1 in u.msg_ids)
    assert 2 not in window.msg_ids
    assert "g20" in window.text
    assert _unit_index(conn, NEWS_ID) == {unit.id for unit in db.get_units(conn, NEWS_ID)}


async def test_the_sweep_of_one_chat_reaches_the_discussion_group_it_links(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """``--chat <channel>`` covers the group as well, or the comments of the channel it was
    pointed at would be the one thing it could not check."""
    client = _busy_discussion_client(DISC)
    cfg = _cfg(Source(folder="News", comments=True))
    await _run(client, conn, paths, cfg)
    client.calls.clear()

    client.messages[DISC_ID] = [m for m in client.messages[DISC_ID] if m.id != 2]
    report = await _prune(client, conn, paths, cfg, chat_id=NEWS_ID)

    assert _swept(client) == [DISC_ID, NEWS_ID]
    assert report.removed == 1
    assert sorted(report.chats_done) == sorted([DISC_ID, NEWS_ID])


async def test_an_imported_or_unavailable_chat_is_never_asked_about(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Neither can answer for its own history — an import never came from Telegram at all — and
    an empty answer for such a chat would drop every message it holds."""
    client = _client(messages={ARG_ID: _talk(101, 102)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    db.upsert_chat(conn, ChatRow(id=ALICE_ID, type="user", source_id="import:alice"))
    db.upsert_messages(conn, [MessageRow(chat_id=ALICE_ID, msg_id=7, date=1)])
    db.upsert_chat(conn, ChatRow(id=GEORGIA_ID, type="supergroup", unavailable=True))
    db.upsert_messages(conn, [MessageRow(chat_id=GEORGIA_ID, msg_id=8, date=1)])
    client.calls.clear()

    report = await _prune(client, conn, paths, cfg)

    assert _swept(client) == [ARG_ID]
    assert report.chats_done == [ARG_ID]
    assert db.get_message(conn, ALICE_ID, 7) is not None
    assert db.get_message(conn, GEORGIA_ID, 8) is not None


async def test_a_comment_whose_post_is_gone_is_dropped_without_a_rebuild(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The pair names a channel and a post, and this index holds neither for ever: a channel it
    never stored, or one whose post has gone since. The comment still goes."""
    client = _client(messages={ARG_ID: _talk(101, 102, 103)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    db.upsert_chat(conn, ChatRow(id=GEORGIA_ID, type="channel"))
    with db.transaction(conn):
        conn.execute(
            "UPDATE messages SET comment_of_chat_id = ?, comment_of_msg_id = 5 "
            "WHERE chat_id = ? AND msg_id = 102",
            (GEORGIA_ID, ARG_ID),
        )
        conn.execute(
            "UPDATE messages SET comment_of_chat_id = ?, comment_of_msg_id = 5 "
            "WHERE chat_id = ? AND msg_id = 103",
            (NEWS_ID, ARG_ID),
        )

    client.messages[ARG_ID] = [m for m in client.messages[ARG_ID] if m.id == 101]
    report = await _prune(client, conn, paths, cfg)

    assert report.removed == 2
    assert _texts(conn, ARG_ID) == {101: "m101"}


async def test_a_held_sync_lock_stops_the_sweep(conn: sqlite3.Connection, paths: Paths) -> None:
    """The sweep deletes messages and writes index rows, so it holds the lock a sync holds —
    ``db.Connection``'s own lock only serialises threads within one process."""
    client = _client(messages={ARG_ID: _talk(101)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    with SyncLock(paths), pytest.raises(SyncInProgress):
        await _prune(client, conn, paths, cfg)


async def test_a_sweep_with_no_budget_left_asks_about_nothing(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(messages={ARG_ID: _talk(101, 102)})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    client.calls.clear()
    budget = SyncBudget()
    budget.cancel()

    report = await _prune(client, conn, paths, cfg, budget)

    assert _swept(client) == []
    assert (report.removed, report.checked) == (0, 0)
    assert report.chats_remaining == [ARG_ID]


async def test_since_skips_older_history_on_the_first_run_only(conn: sqlite3.Connection) -> None:
    client = _client(
        messages={
            ALICE_ID: [
                tl.message(
                    ALICE_ID, 1, "old", sender=1, date=dt.datetime(2024, 12, 31, tzinfo=dt.UTC)
                ),
                tl.message(
                    ALICE_ID, 2, "new", sender=1, date=dt.datetime(2025, 1, 2, tzinfo=dt.UTC)
                ),
            ]
        }
    )
    source = Source(chat="@alice", since="2025-01-01")
    chat = db.upsert_chat(conn, ChatRow(id=ALICE_ID, type="user"))
    first = await sync.sync_chat(client, conn, chat, source, SyncBudget())
    assert _texts(conn, ALICE_ID) == {2: "new"}
    assert _fetch_calls(client, ALICE_ID)[0]["offset_date"] == dt.datetime(
        2025, 1, 1, tzinfo=dt.UTC
    )
    client.messages[ALICE_ID].append(
        tl.message(ALICE_ID, 3, "newer", sender=1, date=dt.datetime(2025, 1, 3, tzinfo=dt.UTC))
    )
    await sync.sync_chat(client, conn, first.chat, source, SyncBudget())
    incremental = [c for c in _fetch_calls(client, ALICE_ID) if c["reverse"]]
    assert incremental[1]["offset_date"] is None
    assert incremental[1]["min_id"] == 2
    assert _texts(conn, ALICE_ID) == {2: "new", 3: "newer"}


async def test_since_keeps_a_message_stamped_at_its_very_midnight(
    conn: sqlite3.Connection,
) -> None:
    """``since`` is passed as that day's UTC midnight and the bound is inclusive there.

    Telethon 1.44 hands ``offset_date`` to ``GetHistoryRequest`` untouched and filters no message
    by date of its own (``_MessagesIter._init``, ``_message_in_range``), so a reversed chunk is
    the complement of the server's exclusive "before this date" cut. The *id* offset is the one
    ``_init`` compensates by hand to stay exclusive under ``reverse`` (``offset_id += 1``), which
    is what shows an uncompensated reversed bound keeps its boundary.
    """
    client = _client(
        messages={
            ALICE_ID: [
                tl.message(
                    ALICE_ID,
                    1,
                    "a second too early",
                    sender=1,
                    date=dt.datetime(2024, 12, 31, 23, 59, 59, tzinfo=dt.UTC),
                ),
                tl.message(
                    ALICE_ID, 2, "midnight", sender=1, date=dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
                ),
            ]
        }
    )
    source = Source(chat="@alice", since="2025-01-01")
    chat = db.upsert_chat(conn, ChatRow(id=ALICE_ID, type="user"))
    await sync.sync_chat(client, conn, chat, source, SyncBudget())
    assert _texts(conn, ALICE_ID) == {2: "midnight"}


async def test_bad_since_is_a_config_error(conn: sqlite3.Connection) -> None:
    chat = db.upsert_chat(conn, ChatRow(id=ALICE_ID, type="user"))
    with pytest.raises(ConfigError, match="since must be an ISO date"):
        await sync.sync_chat(_client(), conn, chat, Source(chat="@alice", since="x"), SyncBudget())


async def test_budget_expiry_stops_after_the_current_batch(conn: sqlite3.Connection) -> None:
    client = _client(
        messages={ARG_ID: [tl.message(ARG_ID, i, f"m{i}", sender=1) for i in range(1, 1201)]}
    )
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    partial = await sync.sync_chat(
        client, conn, chat, ARG_SOURCE, SyncBudget(10, clock=_clock(0, 100))
    )
    assert not partial.complete
    assert partial.new == sync.BATCH_SIZE
    assert len(partial.new_msg_ids) == sync.BATCH_SIZE
    assert partial.chat.last_msg_id == sync.BATCH_SIZE
    assert partial.chat.last_sync_at is None
    assert db.message_counts(conn) == {ARG_ID: sync.BATCH_SIZE}
    resumed = await sync.sync_chat(client, conn, partial.chat, ARG_SOURCE, SyncBudget())
    assert resumed.complete
    assert resumed.new == 700
    assert resumed.chat.last_msg_id == 1200
    assert resumed.chat.last_sync_at is not None
    assert db.message_counts(conn) == {ARG_ID: 1200}
    incremental = [c for c in _fetch_calls(client, ARG_ID) if c["reverse"]]
    assert [c["min_id"] for c in incremental] == [0, sync.BATCH_SIZE]


async def test_private_chat_is_marked_unavailable_and_recovers(conn: sqlite3.Connection) -> None:
    client = _client(
        messages={ARG_ID: [tl.message(ARG_ID, 1, "m1", sender=1)]},
        failures={ARG_ID: errors.ChannelPrivateError(request=None)},
    )
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    synced = await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    assert synced.unavailable
    assert synced.chat.unavailable
    assert synced.new_msg_ids == []
    assert db.message_counts(conn) == {}
    client.failures.clear()
    recovered = await sync.sync_chat(client, conn, synced.chat, ARG_SOURCE, SyncBudget())
    assert not recovered.unavailable
    assert not recovered.chat.unavailable
    assert _texts(conn, ARG_ID) == {1: "m1"}


@pytest.mark.parametrize(
    "error",
    [
        errors.ChannelPrivateError(request=None),
        errors.ChatAdminRequiredError(request=None),
        errors.ChannelInvalidError(request=None),
        errors.ChatForbiddenError(request=None),
    ],
)
async def test_every_unavailable_error_marks_the_chat(
    conn: sqlite3.Connection, error: Exception
) -> None:
    client = _client(failures={ARG_ID: error})
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    synced = await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    assert synced.unavailable
    assert _arg_chat(conn).unavailable


async def test_flood_wait_propagates_after_committing_the_batch(conn: sqlite3.Connection) -> None:
    client = _client(failures={ARG_ID: errors.FloodWaitError(request=None, capture=3600)})
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    with pytest.raises(errors.FloodWaitError):
        await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    assert not _arg_chat(conn).unavailable


@pytest.mark.parametrize(
    "error",
    [
        errors.FloodWaitError(request=None, capture=3600),
        errors.AuthKeyUnregisteredError(request=None),
    ],
    ids=["flood-wait", "auth-key-unregistered"],
)
async def test_mid_stream_failure_keeps_the_committed_batches(
    conn: sqlite3.Connection, error: Exception
) -> None:
    client = _client(
        messages={ARG_ID: [tl.message(ARG_ID, i, f"m{i}", sender=1) for i in range(1, 1201)]},
        failures={ARG_ID: (700, error)},
    )
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    with pytest.raises(type(error)):
        await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    assert db.message_counts(conn) == {ARG_ID: sync.BATCH_SIZE}
    stored = _arg_chat(conn)
    assert stored.last_msg_id == sync.BATCH_SIZE
    assert stored.last_sync_at is None
    client.failures.clear()
    resumed = await sync.sync_chat(client, conn, stored, ARG_SOURCE, SyncBudget())
    assert resumed.new == 700 and resumed.complete
    assert db.message_counts(conn) == {ARG_ID: 1200}
    incremental = [c for c in _fetch_calls(client, ARG_ID) if c["reverse"]]
    assert [c["min_id"] for c in incremental] == [0, sync.BATCH_SIZE]


async def test_service_message_alone_advances_progress_without_rows(
    conn: sqlite3.Connection,
) -> None:
    client = _client(messages={ARG_ID: [tl.message(ARG_ID, 1, "hi", sender=1)]})
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    first = await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    client.messages[ARG_ID].append(tl.service_message(ARG_ID, 2))
    second = await sync.sync_chat(client, conn, first.chat, ARG_SOURCE, SyncBudget())
    assert second.complete and second.new == 0 and second.new_msg_ids == []
    assert second.chat.last_msg_id == 2
    assert _texts(conn, ARG_ID) == {1: "hi"}


async def test_forward_origins_bound_by_the_client_are_named_and_stored(
    conn: sqlite3.Connection,
) -> None:
    forwarded = tl.forwarded_message(ARG_ID, 1, "fwd", origin=2)
    forwarded._forward = SimpleNamespace(sender=BOB, chat=None)  # what Telethon binds
    client = _client(messages={ARG_ID: [forwarded]})
    chat = db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup"))
    await sync.sync_chat(client, conn, chat, ARG_SOURCE, SyncBudget())
    row = db.get_message(conn, ARG_ID, 1)
    assert row is not None and row.fwd_from == "Bob"
    assert conn.execute("SELECT display_name FROM users WHERE id = 2").fetchone()[0] == "Bob"


# --- channel comments ------------------------------------------------------------------------


async def test_comments_are_stored_under_the_discussion_chat(conn: sqlite3.Connection) -> None:
    client = _news_client()
    news = db.upsert_chat(
        conn, ChatRow(id=NEWS_ID, type="channel", title="News", source_id=NEWS_SOURCE.id)
    )
    synced = await sync.sync_chat(client, conn, news, NEWS_SOURCE, SyncBudget())
    assert synced.new == 3
    assert synced.complete
    assert synced.chat.last_msg_id == 3
    discussion = db.get_chat(conn, DISC_ID)
    assert discussion is not None
    assert discussion.discussion_of == NEWS_ID
    assert discussion.source_id == NEWS_SOURCE.id
    assert discussion.type == "supergroup"
    assert discussion.title == "News chat"
    assert synced.discussion is not None
    assert synced.discussion.chat == discussion
    assert synced.discussion.new == 3
    comments = {m.msg_id: m for m in db.get_messages(conn, DISC_ID)}
    assert {k: v.text for k, v in comments.items()} == {
        1: "comment one",
        2: "reply",
        9: "late comment",
    }
    assert {k: v.comment_of_msg_id for k, v in comments.items()} == {1: 1, 2: 1, 9: 3}
    assert {v.comment_of_chat_id for v in comments.values()} == {NEWS_ID}
    assert {v.topic_id for v in comments.values()} == {None}
    assert comments[1].reply_to_msg_id == 7
    assert comments[2].reply_to_msg_id == 1
    assert comments[1].from_name == "Alice Liddell"
    assert set(synced.discussion.new_msg_ids) == {m.id for m in comments.values()}
    assert isinstance(client.requests[0], functions.channels.GetFullChannelRequest)
    threads = [c["reply_to"] for c in _fetch_calls(client, NEWS_ID) if c["reply_to"] is not None]
    assert threads == [1, 3]


async def test_comment_with_the_same_id_as_a_post_does_not_overwrite_it(
    conn: sqlite3.Connection,
) -> None:
    client = _news_client()
    news = db.upsert_chat(conn, ChatRow(id=NEWS_ID, type="channel", source_id=NEWS_SOURCE.id))
    await sync.sync_chat(client, conn, news, NEWS_SOURCE, SyncBudget())
    post = db.get_message(conn, NEWS_ID, 1)
    comment = db.get_message(conn, DISC_ID, 1)
    assert post is not None and comment is not None
    assert post.text == "post 1"
    assert comment.text == "comment one"
    assert post.id != comment.id
    assert (post.chat_id, post.msg_id) != (comment.chat_id, comment.msg_id)
    assert db.message_counts(conn) == {NEWS_ID: 3, DISC_ID: 3}


async def test_channel_without_discussion_group_stores_posts_only(conn: sqlite3.Connection) -> None:
    client = _news_client(linked=None)
    news = db.upsert_chat(conn, ChatRow(id=NEWS_ID, type="channel", source_id=NEWS_SOURCE.id))
    synced = await sync.sync_chat(client, conn, news, NEWS_SOURCE, SyncBudget())
    assert synced.discussion is None
    assert db.get_chat(conn, DISC_ID) is None
    assert _texts(conn, NEWS_ID) == {1: "post 1", 2: "post 2", 3: "post 3"}
    assert all(c["reply_to"] is None for c in _fetch_calls(client, NEWS_ID))


async def test_a_channel_that_lost_its_discussion_group_stops_carrying_its_comments(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """``GetFullChannelRequest`` answers a channel without a linked group with
    ``linked_chat_id = None``, and the stored link has to go with it: the group's messages stay
    what they are, they just stop being this channel's comments."""
    client = _news_client()
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    assert _thread_texts(conn, NEWS_ID) == {
        1: ["post 1", "comment one", "reply"],
        3: ["post 3", "late comment"],
    }
    client.responses[functions.channels.GetFullChannelRequest] = _full_channel(None)
    report = await _run(client, conn, paths, cfg)
    assert report.warnings == []
    unlinked = db.get_chat(conn, DISC_ID)
    assert unlinked is not None and unlinked.discussion_of is None
    assert db.get_discussion_chat(conn, NEWS_ID) is None
    assert _thread_texts(conn, NEWS_ID) == {}
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1"]
    assert _texts(conn, DISC_ID) == {1: "comment one", 2: "reply", 9: "late comment"}
    assert db.unindexed_message_ids(conn, NEWS_ID) == []


async def test_a_replaced_discussion_group_hands_the_comments_over(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """A channel given another discussion group indexes the new one and drops the old link,
    keeping every message both groups hold."""
    client = _news_client(entities=[DISC, DISC2])
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    client.responses[functions.channels.GetFullChannelRequest] = _full_channel(
        202, chats=[NEWS, DISC2]
    )
    client.messages[NEWS_ID] = _posts(1, 0, 0)
    client.comments = {(NEWS_ID, 1): [tl.message(DISC2_ID, 4, "fresh comment", sender=1)]}
    report = await _run(client, conn, paths, cfg)
    assert report.warnings == []
    linked = db.get_discussion_chat(conn, NEWS_ID)
    assert linked is not None and linked.id == DISC2_ID
    old = db.get_chat(conn, DISC_ID)
    assert old is not None and old.discussion_of is None
    assert _texts(conn, DISC_ID) == {1: "comment one", 2: "reply", 9: "late comment"}
    assert _texts(conn, DISC2_ID) == {4: "fresh comment"}
    assert _thread_texts(conn, NEWS_ID) == {1: ["post 1", "fresh comment"]}
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1", "fresh comment"]


async def test_a_group_two_channels_pointed_at_belongs_to_the_last_one(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Telegram links a group to one channel at a time, and so does the index: the channel that
    lost it has its posts rebuilt without the comments that are now another channel's."""
    client = _news_client(entities=[DISC, OTHER])
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    other = db.upsert_chat(
        conn, ChatRow(id=OTHER_ID, type="channel", title="Other", source_id="chat:@other_news")
    )
    linked = await sync.link_discussion_chat(client, conn, other)
    assert linked is not None and linked.id == DISC_ID
    assert db.get_discussion_chat(conn, OTHER_ID) == linked
    assert db.get_discussion_chat(conn, NEWS_ID) is None
    news = db.get_chat(conn, NEWS_ID)
    assert news is not None
    flagged = db.get_messages_by_ids(conn, db.unindexed_message_ids(conn, NEWS_ID))
    assert [post.msg_id for post in flagged] == [1, 3]  # the posts that held the comments
    await sync.index_pending(conn, cfg, news)
    assert _thread_texts(conn, NEWS_ID) == {}
    assert _texts(conn, DISC_ID) == {1: "comment one", 2: "reply", 9: "late comment"}


async def test_a_handed_over_group_gives_the_new_channel_none_of_the_old_comments(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Post ids start at 1 in every channel, so the channel taking a group over must inherit
    nothing that hung under the ids of the channel that lost it: its own post 1 is not the post 1
    they commented on. The comments themselves stay what they are — the group's messages, in the
    windows they were already in, searchable through them the moment the handover commits."""
    client = _news_client(entities=[DISC, OTHER])
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    assert _thread_texts(conn, NEWS_ID) == {
        1: ["post 1", "comment one", "reply"],
        3: ["post 3", "late comment"],
    }
    other_source = Source(chat="@other_news", comments=True)
    other = db.upsert_chat(
        conn,
        ChatRow(
            id=OTHER_ID,
            type="channel",
            title="Other",
            username="other_news",
            source_id=other_source.id,
        ),
    )
    db.upsert_messages(
        conn, [MessageRow(chat_id=OTHER_ID, msg_id=1, date=1_700_000_000, text="other post 1")]
    )
    assert await sync.link_discussion_chat(client, conn, other) is not None
    moved = _cfg(NEWS_SOURCE, other_source)
    await sync.index_pending(conn, moved, other)
    assert _thread_texts(conn, OTHER_ID) == {}
    assert [v.text for v in search.thread(conn, OTHER_ID, 1)] == ["other post 1"]
    assert _texts(conn, DISC_ID) == {1: "comment one", 2: "reply", 9: "late comment"}
    assert {m.comment_of_chat_id for m in db.get_messages(conn, DISC_ID)} == {None}
    assert {m.comment_of_msg_id for m in db.get_messages(conn, DISC_ID)} == {None}
    assert db.unindexed_message_ids(conn, DISC_ID) == []
    hits = search.lexical_units(conn, "comment", Filters(), 5)
    assert {u.chat_id for u in db.get_units_by_ids(conn, [m.unit_id for m in hits])} == {DISC_ID}
    assert [v.text for v in search.context(conn, DISC_ID, 2, before=1, after=1)] == [
        "comment one",
        "reply",
        "late comment",
    ]  # no longer bounded by the post it commented on: the group's own neighbours


def _fake_vectors(conn: sqlite3.Connection, chat_id: int) -> None:
    """A vector row for every unit of a chat, so a cleanup that misses them is visible."""
    db.ensure_vec_table(conn, 4)
    for unit in db.get_units(conn, chat_id):
        conn.execute(
            "INSERT INTO unit_vec(rowid, chat_id, date_start, embedding) VALUES (?, ?, 1, ?)",
            (unit.id, chat_id, sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0])),
        )


def _index_orphans(conn: sqlite3.Connection) -> list[int]:
    """``unit_fts`` and ``unit_vec`` rowids no ``units`` row backs any more."""
    tables = ["unit_fts"] + (["unit_vec"] if db.has_vec_table(conn) else [])
    return [
        int(row[0])
        for table in tables
        for row in conn.execute(
            f"SELECT rowid FROM {table} WHERE rowid NOT IN (SELECT id FROM units)"
        )
    ]


async def test_a_forum_discussion_group_stores_comments_and_topics_side_by_side(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """A discussion group can be a forum, and then ``topic_id`` and the comment relation are two
    different things about the same row: topic 3 of the group and post 3 of the channel are the
    same number out of different id spaces."""
    client = _forum_discussion_client()
    cfg = _cfg(NEWS_SOURCE, Source(chat="@forum_chat"))
    await _run(client, conn, paths, cfg)
    group = db.get_chat(conn, FORUM_DISC_ID)
    assert group is not None and group.is_forum and group.discussion_of == NEWS_ID
    stored = {m.msg_id: m for m in db.get_messages(conn, FORUM_DISC_ID)}
    assert {i: m.comment_of_msg_id for i, m in stored.items()} == {
        1: 1,
        2: 1,
        3: None,
        4: None,
        9: 3,
    }
    assert {i: m.topic_id for i, m in stored.items()} == {1: None, 2: None, 3: 3, 4: 3, 9: None}
    assert db.stored_comment_post_ids(conn, FORUM_DISC_ID, NEWS_ID) == [1, 3]
    assert _thread_texts(conn, NEWS_ID) == {
        1: ["post 1", "comment one", "reply"],
        3: ["post 3", "late comment"],
    }
    assert _forum_state(conn) == {"topics": {3: 3, 4: 3}, "windows": [(3, (3, 4))]}


async def test_an_unlink_leaves_the_forum_topics_of_the_group_alone(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The channel loses the group: its comments stop being comments, and the group's own topic
    numbered like one of the channel's posts keeps every message, its topic and its window."""
    client = _forum_discussion_client()
    cfg = _cfg(NEWS_SOURCE, Source(chat="@forum_chat"))
    await _run(client, conn, paths, cfg)
    before = _forum_state(conn)
    news = db.get_chat(conn, NEWS_ID)
    assert news is not None
    client.responses[functions.channels.GetFullChannelRequest] = _full_channel(None)
    assert await sync.link_discussion_chat(client, conn, news) is None
    assert db.get_discussion_chat(conn, NEWS_ID) is None
    assert _forum_state(conn) == before
    assert db.stored_comment_post_ids(conn, FORUM_DISC_ID, NEWS_ID) == []
    assert db.unindexed_message_ids(conn, FORUM_DISC_ID) == []
    assert _texts(conn, FORUM_DISC_ID) == {
        1: "comment one",
        2: "reply",
        3: "topic three opens",
        4: "still in topic three",
        9: "late comment",
    }
    assert [v.text for v in search.thread(conn, NEWS_ID, 3)] == ["post 3"]
    assert _found(conn, "topic") == {FORUM_DISC_ID}


async def test_a_handover_leaves_the_forum_topics_of_the_group_alone(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Another channel takes the group over. Its own post 3 must inherit neither the comments of
    the channel that lost it nor the group's forum topic 3, which was never a comment at all."""
    client = _forum_discussion_client()
    client.entities[FORUM_DISC_ID] = FORUM_DISC
    cfg = _cfg(NEWS_SOURCE, Source(chat="@forum_chat"))
    await _run(client, conn, paths, cfg)
    before = _forum_state(conn)
    other_source = Source(chat="@other_news", comments=True)
    other = db.upsert_chat(
        conn,
        ChatRow(
            id=OTHER_ID,
            type="channel",
            title="Other",
            username="other_news",
            source_id=other_source.id,
        ),
    )
    db.upsert_messages(
        conn, [MessageRow(chat_id=OTHER_ID, msg_id=3, date=1_700_000_000, text="other post 3")]
    )
    linked = await sync.link_discussion_chat(client, conn, other)
    assert linked is not None and linked.id == FORUM_DISC_ID
    assert _forum_state(conn) == before
    assert db.stored_comment_post_ids(conn, FORUM_DISC_ID, NEWS_ID) == []
    assert db.stored_comment_post_ids(conn, FORUM_DISC_ID, OTHER_ID) == []
    await sync.index_pending(conn, _cfg(NEWS_SOURCE, other_source), other)
    assert _thread_texts(conn, OTHER_ID) == {}
    assert [v.text for v in search.thread(conn, OTHER_ID, 3)] == ["other post 3"]


async def test_deleting_the_channel_leaves_the_forum_topics_of_its_group_alone(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """``sources rm`` of the channel deletes it and unlinks the group. The group's forum topic
    numbered like one of the deleted channel's posts is untouched, and its cleared comments are
    searchable right there — no sync in between."""
    client = _forum_discussion_client()
    cfg = _cfg(NEWS_SOURCE, Source(chat="@forum_chat"))
    await _run(client, conn, paths, cfg)
    before = _forum_state(conn)
    removed = sources.remove_source(cfg, conn, sources.parse_target("@news"))
    assert removed.chat_ids == [NEWS_ID]
    assert db.get_chat(conn, NEWS_ID) is None
    group = db.get_chat(conn, FORUM_DISC_ID)
    assert group is not None and group.discussion_of is None
    assert _forum_state(conn) == before
    assert db.stored_comment_post_ids(conn, FORUM_DISC_ID, NEWS_ID) == []
    assert db.unindexed_message_ids(conn, FORUM_DISC_ID) == []
    assert _found(conn, "topic") == {FORUM_DISC_ID}
    assert _found(conn, "comment") == {FORUM_DISC_ID}


async def test_a_cleared_comment_is_searchable_before_any_later_sync(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Removing the channel unmaps the comments its group held. That must not take them out of
    the index even for a moment: nothing cut from a group's rows reads the comment relation, so
    every one of them stays in the window it was in and answers a message search immediately."""
    client = _discussion_client(DISC)
    cfg = _cfg(NEWS_SOURCE, Source(chat="@news_chat"))
    await _run(client, conn, paths, cfg)
    assert _found(conn, "comment") == {DISC_ID}
    sources.remove_source(cfg, conn, sources.parse_target("@news"))
    assert db.get_chat(conn, NEWS_ID) is None
    assert {m.comment_of_chat_id for m in db.get_messages(conn, DISC_ID)} == {None}
    assert db.unindexed_message_ids(conn, DISC_ID) == []
    assert _found(conn, "comment") == {DISC_ID}
    assert _found(conn, "late") == {DISC_ID}


async def test_a_group_deleted_after_an_unlink_leaves_no_thread_quoting_it(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Nothing inside a post thread names the group whose comments it carries — the link does,
    and an unlink is the end of it. So the threads go with the link rather than with the rebuild
    the flagged posts ask for: remove the group's source in between and the comments would sit
    in ``units``, ``unit_fts`` and ``unit_vec`` with nothing left to find them by."""
    client = _news_client()
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    news = db.get_chat(conn, NEWS_ID)
    assert news is not None
    assert _thread_texts(conn, NEWS_ID) != {}
    _fake_vectors(conn, NEWS_ID)
    client.responses[functions.channels.GetFullChannelRequest] = _full_channel(None)
    assert await sync.link_discussion_chat(client, conn, news) is None
    assert _thread_texts(conn, NEWS_ID) == {}
    db.delete_chat(conn, DISC_ID)
    assert _thread_texts(conn, NEWS_ID) == {}
    assert _index_orphans(conn) == []
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1"]
    assert db.get_chat(conn, DISC_ID) is None


async def test_a_group_deleted_after_a_handover_leaves_the_old_channel_clean(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The same for a group another channel takes over: the channel that lost it drops the
    threads it fed there and then, so deleting the group later — its source now belongs to the
    channel holding it — has nothing of it left anywhere."""
    client = _news_client(entities=[DISC, OTHER])
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    other = db.upsert_chat(
        conn, ChatRow(id=OTHER_ID, type="channel", title="Other", source_id="chat:@other_news")
    )
    assert await sync.link_discussion_chat(client, conn, other) is not None
    assert _thread_texts(conn, NEWS_ID) == {}
    db.delete_chat(conn, DISC_ID)
    assert _thread_texts(conn, NEWS_ID) == {}
    assert _index_orphans(conn) == []
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1"]
    flagged = db.get_messages_by_ids(conn, db.unindexed_message_ids(conn, NEWS_ID))
    assert [post.msg_id for post in flagged] == [1, 3]


async def test_an_unresolvable_new_group_still_drops_the_link_to_the_old_one(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Telegram names a group the account cannot resolve: the comments are off for the run, and
    the group the channel held until now stops being its own. It demonstrably is not any more,
    so leaving the link would keep its comments in the channel's threads and in ``search.thread``
    for as long as the new group stays unresolvable."""
    client = _news_client()
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    assert _thread_texts(conn, NEWS_ID) == {
        1: ["post 1", "comment one", "reply"],
        3: ["post 3", "late comment"],
    }
    client.responses[functions.channels.GetFullChannelRequest] = _full_channel(202, chats=[NEWS])
    client.entity_errors[DISC2_ID] = errors.ChannelPrivateError(request=None)
    report = await _run(client, conn, paths, cfg)
    assert len(report.warnings) == 1
    assert "comments of channel" in report.warnings[0] and "unavailable" in report.warnings[0]
    assert db.get_discussion_chat(conn, NEWS_ID) is None
    old = db.get_chat(conn, DISC_ID)
    assert old is not None and old.discussion_of is None
    assert _thread_texts(conn, NEWS_ID) == {}
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1"]
    assert _texts(conn, DISC_ID) == {1: "comment one", 2: "reply", 9: "late comment"}


async def test_a_discussion_group_that_only_fails_to_resolve_keeps_its_link(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The very group the channel already holds, unreachable for one run — a permission blip —
    is not a group it lost: the link stays, the comments stay its own, and nothing is rebuilt."""
    client = _news_client()
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    client.responses[functions.channels.GetFullChannelRequest] = _full_channel(201, chats=[NEWS])
    client.entity_errors[DISC_ID] = errors.ChannelPrivateError(request=None)
    report = await _run(client, conn, paths, cfg)
    assert len(report.warnings) == 1 and "comments of channel" in report.warnings[0]
    linked = db.get_discussion_chat(conn, NEWS_ID)
    assert linked is not None and linked.id == DISC_ID
    assert _thread_texts(conn, NEWS_ID) == {
        1: ["post 1", "comment one", "reply"],
        3: ["post 3", "late comment"],
    }
    assert db.unindexed_message_ids(conn, NEWS_ID) == []


async def test_a_handed_over_group_belongs_to_the_source_that_holds_it_now(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The group's ``source_id`` follows the link (`sources.discussion_source_id`): after it
    moves from one channel to another, removing the channel it left keeps it and removing the
    one holding it now takes its comments along."""
    client = _news_client(entities=[DISC, OTHER])
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    group = db.get_chat(conn, DISC_ID)
    assert group is not None and group.source_id == NEWS_SOURCE.id
    other = db.upsert_chat(
        conn, ChatRow(id=OTHER_ID, type="channel", title="Other", source_id="chat:@other_news")
    )
    handed = await sync.link_discussion_chat(client, conn, other)
    assert handed is not None and handed.id == DISC_ID
    assert handed.source_id == "chat:@other_news"
    moved = _cfg(NEWS_SOURCE, Source(chat="@other_news", comments=True))
    by_source = {
        s.source_id: sorted(c.id for c in s.chats) for s in sources.sources_status(moved, conn)
    }
    assert by_source == {
        NEWS_SOURCE.id: [NEWS_ID],
        "chat:@other_news": sorted([DISC_ID, OTHER_ID]),
    }
    left = sources.remove_source(moved, conn, sources.parse_target("@news"))
    assert left.chat_ids == [NEWS_ID]
    assert db.get_chat(conn, DISC_ID) is not None
    assert _texts(conn, DISC_ID) == {1: "comment one", 2: "reply", 9: "late comment"}
    gone = sources.remove_source(left.config, conn, sources.parse_target("@other_news"))
    assert sorted(gone.chat_ids) == sorted([DISC_ID, OTHER_ID])
    assert db.get_chat(conn, DISC_ID) is None
    assert db.get_messages(conn, DISC_ID) == []


@pytest.mark.parametrize(
    "spelling", [str(DISC_ID), "@news_chat", "https://t.me/news_chat", "t.me/c/201"]
)
async def test_a_directly_configured_group_keeps_its_source_whatever_the_spelling(
    conn: sqlite3.Connection, spelling: str
) -> None:
    """``chat =`` takes an id, an ``@username`` and a ``t.me`` link alike, and a group covered by
    such an entry owns its rows under every one of them: the link must not overwrite its
    ``source_id`` with the channel's, which would empty the configured source and let
    ``sources rm`` of the channel delete the group."""
    client = _news_client(entities=[DISC])
    own = Source(chat=spelling)
    db.upsert_chat(
        conn,
        ChatRow(
            id=DISC_ID,
            type="supergroup",
            title="News chat",
            username="news_chat",
            source_id=own.id,
        ),
    )
    news = db.upsert_chat(
        conn, ChatRow(id=NEWS_ID, type="channel", title="News", source_id=NEWS_SOURCE.id)
    )
    linked = await sync.link_discussion_chat(client, conn, news)
    assert linked is not None and linked.source_id == own.id
    cfg = _cfg(NEWS_SOURCE, own)
    by_source = {s.source_id: [c.id for c in s.chats] for s in sources.sources_status(cfg, conn)}
    assert by_source == {NEWS_SOURCE.id: [NEWS_ID], own.id: [DISC_ID]}
    removed = sources.remove_source(cfg, conn, sources.parse_target("@news_chat"))
    assert removed.source_id == own.id and removed.chat_ids == [DISC_ID]
    assert db.get_chat(conn, NEWS_ID) is not None


async def test_removing_a_group_that_is_its_own_source_takes_its_comments_off_the_channel(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The group's messages go with it, so the channel's post threads must not go on quoting
    them — in the units, in ``unit_fts`` and in the vectors. Nothing is left for a later run to
    repair: the channel may never be resolvable again."""
    client = _discussion_client(DISC)
    cfg = _cfg(NEWS_SOURCE, Source(chat="@news_chat"))
    await _run(client, conn, paths, cfg)
    assert _thread_texts(conn, NEWS_ID) == {
        1: ["post 1", "comment one", "reply"],
        3: ["post 3", "late comment"],
    }
    removed = sources.remove_source(cfg, conn, sources.parse_target("@news_chat"))
    assert removed.source_id == "chat:@news_chat" and removed.chat_ids == [DISC_ID]
    assert db.get_chat(conn, DISC_ID) is None
    assert _thread_texts(conn, NEWS_ID) == {}
    assert search.lexical_units(conn, "comment", Filters(), 5) == []
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1"]
    assert _unit_fts_count(conn, NEWS_ID) == len(db.get_units(conn, NEWS_ID))
    posts = db.get_messages_by_msg_id(conn, NEWS_ID, [1, 3])
    assert db.unindexed_message_ids(conn, NEWS_ID) == sorted(
        post.id for post in posts.values() if post.id is not None
    )


async def test_removing_the_channel_leaves_its_discussion_group_whole(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The mirror case: the group is a source of its own and survives its channel. Its own
    windows and threads stay, the link that pointed at a chat which no longer exists goes, and
    with it the posts its messages hung under: they are posts of a channel this index no longer
    holds. Nothing of the group is re-cut for it — its windows never named those posts — so the
    comments stay searchable through the units they are already in."""
    client = _discussion_client(DISC)
    cfg = _cfg(NEWS_SOURCE, Source(chat="@news_chat"))
    await _run(client, conn, paths, cfg)
    before = [(u.kind, u.msg_ids) for u in db.get_units(conn, DISC_ID)]
    removed = sources.remove_source(cfg, conn, sources.parse_target("@news"))
    assert removed.source_id == NEWS_SOURCE.id and removed.chat_ids == [NEWS_ID]
    group = db.get_chat(conn, DISC_ID)
    assert group is not None and group.discussion_of is None
    assert _texts(conn, DISC_ID) == {1: "comment one", 2: "reply", 9: "late comment"}
    assert [(u.kind, u.msg_ids) for u in db.get_units(conn, DISC_ID)] == before
    hits = search.lexical_units(conn, "comment", Filters(), 5)
    assert {u.chat_id for u in db.get_units_by_ids(conn, [m.unit_id for m in hits])} == {DISC_ID}
    assert db.stored_comment_post_ids(conn, DISC_ID, NEWS_ID) == []
    assert db.unindexed_message_ids(conn, DISC_ID) == []
    await sync.index_pending(conn, removed.config, group)
    assert [(u.kind, u.msg_ids) for u in db.get_units(conn, DISC_ID)] == before
    assert {u.topic_id for u in db.get_units(conn, DISC_ID)} == {None}


async def test_removing_a_group_whose_channel_is_gone_leaves_nothing_to_repair(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The channel is unreachable — an account that left it, a deleted channel — so no later
    sync will ever rebuild its posts. The removal itself has to leave the index coherent."""
    client = _discussion_client(DISC)
    cfg = _cfg(NEWS_SOURCE, Source(chat="@news_chat"))
    await _run(client, conn, paths, cfg)
    db.set_chat_unavailable(conn, NEWS_ID)
    sources.remove_source(cfg, conn, sources.parse_target("@news_chat"))
    assert _thread_texts(conn, NEWS_ID) == {}
    assert search.lexical_units(conn, "comment", Filters(), 5) == []
    assert _unit_fts_count(conn, NEWS_ID) == len(db.get_units(conn, NEWS_ID))


async def test_the_unlink_and_the_flags_of_the_posts_it_invalidates_are_one_commit(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill between clearing the link and flagging the posts that held the group's comments
    would leave post threads full of a group that is not the channel's and nothing saying they
    need a rebuild. Both halves are one transaction, so a failure in the second undoes the
    first and the next run does the whole transition again."""
    client = _news_client()
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    news = db.get_chat(conn, NEWS_ID)
    assert news is not None

    def killed(*args: object, **kwargs: object) -> None:
        raise RuntimeError("killed between the unlink and the flags")

    monkeypatch.setattr(db, "mark_unindexed", killed)
    client.responses[functions.channels.GetFullChannelRequest] = _full_channel(None)
    with pytest.raises(RuntimeError):
        await sync.link_discussion_chat(client, conn, news)
    monkeypatch.undo()
    linked = db.get_discussion_chat(conn, NEWS_ID)
    assert linked is not None and linked.id == DISC_ID
    assert db.unindexed_message_ids(conn, NEWS_ID) == []
    assert not conn.in_transaction
    assert await sync.link_discussion_chat(client, conn, news) is None
    assert db.get_discussion_chat(conn, NEWS_ID) is None
    flagged = db.get_messages_by_ids(conn, db.unindexed_message_ids(conn, NEWS_ID))
    assert [post.msg_id for post in flagged] == [1, 3]


async def test_rows_stranded_in_an_unlinked_group_are_rebuilt_by_the_sweep(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Comment rows a killed run left flagged in a group the channel then loses: no source
    lists that group and no link leads to it any more, so only the sweep over
    ``messages.indexed`` at the end of the run reaches them."""
    client = _news_client()
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    stranded = [m.id for m in db.get_messages(conn, DISC_ID) if m.id is not None]
    db.mark_unindexed(conn, stranded)
    client.responses[functions.channels.GetFullChannelRequest] = _full_channel(None)
    report = await _run(client, conn, paths, cfg)
    assert report.warnings == []
    assert db.get_discussion_chat(conn, NEWS_ID) is None
    assert db.unindexed_message_ids(conn, DISC_ID) == []
    assert db.get_units(conn, DISC_ID) != []
    assert db.chats_with_unindexed(conn) == []


async def test_the_stranded_sweep_repairs_at_most_its_limit_of_chats_per_run(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """A backlog — a killed run leaves every chat it had reached flagged — heals over a few
    runs instead of turning one into a full rebuild; chats are taken in id order."""
    client = _news_client()
    cfg = _cfg(NEWS_SOURCE)
    await _run(client, conn, paths, cfg)
    for chat_id in (NEWS_ID, DISC_ID):
        db.mark_unindexed(conn, [m.id for m in db.get_messages(conn, chat_id) if m.id is not None])
    await sync.index_stranded(conn, cfg, limit=1)
    assert db.chats_with_unindexed(conn) == [NEWS_ID]
    await sync.index_stranded(conn, cfg, limit=1)
    assert db.chats_with_unindexed(conn) == []


def _arg_history() -> list[types.Message]:
    """Enough of a chat to cut into more than one unit: a reply thread and two windows."""
    first = tl.message(ARG_ID, 1, "where do I renew a residence permit", sender=1)
    return [
        first,
        tl.message(ARG_ID, 2, "at the migraciones office", sender=2, reply_to=tl.reply_header(1)),
        tl.message(ARG_ID, 3, "thanks", sender=1, reply_to=tl.reply_header(1)),
    ]


async def test_a_full_run_recuts_the_units_of_an_index_an_older_build_cut(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v0.1.1 upgrade path end to end: the re-cut is its own step after the chat loop and
    the stranded sweep, so nothing is left flagged for the next run's unbudgeted deferred pass."""
    client = _client(messages={ARG_ID: _arg_history()})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    before = [u.id for u in db.get_units(conn, ARG_ID)]
    conn.execute("DELETE FROM meta WHERE key = ?", (db.META_UNIT_RECIPE,))
    monkeypatch.setattr(units, "RECIPE_VERSION", units.RECIPE_VERSION + 1)
    report = await _run(client, conn, paths, cfg)
    assert report.warnings == []
    assert not set(before) & {u.id for u in db.get_units(conn, ARG_ID)}
    assert db.unit_recipe(conn) == units.RECIPE_VERSION
    assert db.recut_markers(conn) == {}
    assert db.chats_with_unindexed(conn) == []


async def test_a_run_that_opts_out_leaves_the_units_alone(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-sync inside a ``search`` call passes ``recut=False`` and must never empty a
    chat's units — whatever budget the user has given that refresh."""
    client = _client(messages={ARG_ID: _arg_history()})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    before = [u.id for u in db.get_units(conn, ARG_ID)]
    conn.execute("DELETE FROM meta WHERE key = ?", (db.META_UNIT_RECIPE,))
    monkeypatch.setattr(units, "RECIPE_VERSION", units.RECIPE_VERSION + 1)
    await _run(client, conn, paths, cfg, recut=False)
    assert [u.id for u in db.get_units(conn, ARG_ID)] == before
    assert db.unit_recipe(conn) is None


async def test_a_short_explicit_run_still_recuts(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: an explicit sync makes what progress its budget allows, and a short
    window is not a reason to leave an upgraded index behind."""
    client = _client(messages={ARG_ID: _arg_history()})
    cfg = _cfg(ARG_SOURCE)
    await _run(client, conn, paths, cfg)
    before = [u.id for u in db.get_units(conn, ARG_ID)]
    conn.execute("DELETE FROM meta WHERE key = ?", (db.META_UNIT_RECIPE,))
    monkeypatch.setattr(units, "RECIPE_VERSION", units.RECIPE_VERSION + 1)
    await _run(client, conn, paths, cfg, cfg.search.auto_sync_budget_s)
    assert not set(before) & {u.id for u in db.get_units(conn, ARG_ID)}
    assert db.unit_recipe(conn) == units.RECIPE_VERSION


def _v011_index() -> sqlite3.Connection:
    """A database exactly as v0.1.1 left one: the v5 schema, its rows, a unit, and no recipe.

    Every other re-cut test moves :data:`grepogram.units.RECIPE_VERSION` under the code, which
    proves the comparison and not the shipped number. This one is the upgrade an installed
    v0.1.1 actually walks, at the version this build carries. The rows are what
    :func:`grepogram.sync.map_message` writes for :func:`_arg_history`, so the edit-refetch pass
    finds nothing to re-store and the only thing that can move a unit is the re-cut itself.
    """
    connection = db.connect(":memory:")
    for statement in db.MIGRATIONS[db.BASE_VERSION]:
        connection.execute(statement)
    connection.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '5')")
    chat = ChatRow(
        id=ARG_ID,
        type="supergroup",
        title="Argentina chat",
        username="arg_chat",
        source_id="folder:Argentina",
    )
    connection.execute(
        "INSERT INTO chats(id, type, title, username, source_id, last_msg_id, last_sync_at) "
        "VALUES (?, ?, ?, ?, ?, 3, ?)",
        (chat.id, chat.type, chat.title, chat.username, chat.source_id, int(time.time()) - 60),
    )
    names = {
        user_id: user.display_name
        for user_id, user in sync.collect_users([ALICE, BOB]).items()
        if user.display_name
    }
    rows = [
        row for msg in _arg_history() if (row := sync.map_message(msg, chat, names)) is not None
    ]
    for row in rows:
        connection.execute(
            "INSERT INTO messages(chat_id, msg_id, date, from_id, from_name, reply_to_msg_id, "
            "text, indexed) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (
                row.chat_id,
                row.msg_id,
                row.date,
                row.from_id,
                row.from_name,
                row.reply_to_msg_id,
                row.text,
            ),
        )
    connection.execute(
        "INSERT INTO units(chat_id, kind, msg_id_start, msg_id_end, msg_ids, date_start, "
        "date_end, text) VALUES (?, 'window', 1, 3, '[1,2,3]', ?, ?, 'cut by the older rule')",
        (ARG_ID, rows[0].date, rows[-1].date),
    )
    return connection


async def test_an_installed_v011_index_recuts_once_at_the_shipped_recipe(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole upgrade, unpatched: migrating a v5 database records no recipe, the first sync
    re-cuts the chat once and stamps :data:`grepogram.units.RECIPE_VERSION`, and the sync after
    it re-cuts nothing — the re-index a v0.1.1 upgrader pays for happens exactly once."""
    connection = _v011_index()
    assert db.migrate(connection) == db.SCHEMA_VERSION
    assert db.unit_recipe(connection) is None
    before = [u.id for u in db.get_units(connection, ARG_ID)]
    recut: list[int] = []
    original = sync._recut_one

    def counted(conn_: sqlite3.Connection, cfg_: Config, chat: ChatRow) -> bool:
        recut.append(chat.id)
        return original(conn_, cfg_, chat)

    monkeypatch.setattr(sync, "_recut_one", counted)
    cfg = _cfg(ARG_SOURCE)
    await _run(_client(messages={ARG_ID: _arg_history()}), connection, paths, cfg)
    assert recut == [ARG_ID]
    assert db.unit_recipe(connection) == units.RECIPE_VERSION
    assert db.recut_markers(connection) == {}
    after = [u.id for u in db.get_units(connection, ARG_ID)]
    assert not set(before) & set(after)
    assert db.chats_with_unindexed(connection) == []
    recut.clear()
    await _run(_client(messages={ARG_ID: _arg_history()}), connection, paths, cfg)
    assert recut == []
    assert [u.id for u in db.get_units(connection, ARG_ID)] == after
    connection.close()


async def test_a_run_that_opts_out_never_recuts_an_installed_v011_index(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same index under the refresh a ``search`` call runs by itself: nothing starts."""
    connection = _v011_index()
    db.migrate(connection)
    before = [u.id for u in db.get_units(connection, ARG_ID)]
    recut: list[int] = []

    def counted(conn_: sqlite3.Connection, cfg_: Config, chat: ChatRow) -> bool:
        recut.append(chat.id)
        return True

    monkeypatch.setattr(sync, "_recut_one", counted)
    cfg = _cfg(ARG_SOURCE)
    client = _client(messages={ARG_ID: _arg_history()})
    await _run(client, connection, paths, cfg, recut=False)
    assert recut == []
    assert [u.id for u in db.get_units(connection, ARG_ID)] == before
    assert db.unit_recipe(connection) is None
    connection.close()


async def test_comments_are_not_fetched_without_the_flag(conn: sqlite3.Connection) -> None:
    client = _news_client()
    news = db.upsert_chat(conn, ChatRow(id=NEWS_ID, type="channel", source_id="chat:@news"))
    synced = await sync.sync_chat(client, conn, news, Source(chat="@news"), SyncBudget())
    assert synced.discussion is None
    assert client.requests == []
    assert db.get_chat(conn, DISC_ID) is None


async def test_comment_budget_expiry_resumes_from_the_last_finished_post(
    conn: sqlite3.Connection,
) -> None:
    client = _news_client()
    news = db.upsert_chat(conn, ChatRow(id=NEWS_ID, type="channel", source_id=NEWS_SOURCE.id))
    partial = await sync.sync_chat(
        client, conn, news, NEWS_SOURCE, SyncBudget(10, clock=_clock(0, 0, 100))
    )
    assert not partial.complete
    assert partial.chat.last_msg_id == 1
    assert partial.chat.last_sync_at is None
    assert partial.new == 3
    assert partial.discussion is not None and partial.discussion.new == 2
    assert {m.msg_id for m in db.get_messages(conn, DISC_ID)} == {1, 2}
    assert db.message_counts(conn)[NEWS_ID] == 3
    resumed = await sync.sync_chat(client, conn, partial.chat, NEWS_SOURCE, SyncBudget())
    assert resumed.complete
    assert resumed.chat.last_msg_id == 3
    assert resumed.new == 0
    assert resumed.discussion is not None and resumed.discussion.new == 1
    assert {m.msg_id for m in db.get_messages(conn, DISC_ID)} == {1, 2, 9}
    threads = [c["reply_to"] for c in _fetch_calls(client, NEWS_ID) if c["reply_to"] is not None]
    assert threads == [1, 3]


async def test_threads_that_grew_are_re_read_on_later_runs(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _news_client(
        messages={
            NEWS_ID: [
                tl.channel_post(NEWS_ID, 1, "post 1", replies=2),
                tl.channel_post(NEWS_ID, 2, "post 2", replies=0),
                tl.channel_post(NEWS_ID, 3, "post 3", replies=1),
            ]
        }
    )
    cfg = _cfg(NEWS_SOURCE, edit_refetch=10)
    await _run(client, conn, paths, cfg)
    unchanged = await _run(client, conn, paths, cfg)
    assert unchanged.new == 0
    threads = [c["reply_to"] for c in _fetch_calls(client, NEWS_ID) if c["reply_to"] is not None]
    assert threads == [1, 3]
    client.messages[NEWS_ID][1] = tl.channel_post(NEWS_ID, 2, "post 2", replies=1)
    client.comments[(NEWS_ID, 2)] = [tl.message(DISC_ID, 5, "new comment", sender=1)]
    client.comments[(NEWS_ID, 3)].append(tl.message(DISC_ID, 11, "another", sender=1))
    grown = await _run(client, conn, paths, cfg)
    assert grown.new == 1 and grown.chats_done == [NEWS_ID]
    threads = [c["reply_to"] for c in _fetch_calls(client, NEWS_ID) if c["reply_to"] is not None]
    assert threads == [1, 3, 2]
    comments = {m.msg_id: m.comment_of_msg_id for m in db.get_messages(conn, DISC_ID)}
    assert comments == {1: 1, 2: 1, 5: 2, 9: 3}
    (thread,) = [u for u in db.get_units(conn, NEWS_ID) if u.kind == "thread" and u.msg_ids == [2]]
    assert "new comment" in thread.text
    views = search.thread(conn, NEWS_ID, 2)
    assert [v.text for v in views] == ["post 2", "new comment"]
    client.messages[NEWS_ID][2] = tl.channel_post(NEWS_ID, 3, "post 3", replies=2)
    again = await _run(client, conn, paths, cfg)
    assert again.new == 1
    assert {m.msg_id for m in db.get_comment_messages(conn, DISC_ID, NEWS_ID, [3]).get(3, [])} == {
        9,
        11,
    }


async def test_only_posts_with_replies_cost_a_getreplies_request(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """A post Telegram reports no comments on — or without a ``MessageReplies`` header at
    all — is stored without a thread request; the posts with comments still get theirs."""
    client = _news_client(
        messages={NEWS_ID: _posts(2, 0, 1, None, 0)},
        comments={**_comments(), (NEWS_ID, 4): [tl.message(DISC_ID, 40, "unreported", sender=1)]},
    )
    report = await _run(client, conn, paths, _cfg(NEWS_SOURCE))
    assert report.chats_done == [NEWS_ID] and report.warnings == []
    assert _texts(conn, NEWS_ID) == {i: f"post {i}" for i in range(1, 6)}
    threads = [c["reply_to"] for c in _fetch_calls(client, NEWS_ID) if c["reply_to"] is not None]
    assert threads == [1, 3]
    assert {m.msg_id for m in db.get_messages(conn, DISC_ID)} == {1, 2, 9}
    news = db.get_chat(conn, NEWS_ID)
    assert news is not None and news.last_msg_id == 5


def _threads_asked(client: FakeClient, since: int = 0) -> list[int]:
    """The post ids the run made a ``GetReplies`` request for, from call ``since`` onwards."""
    return [
        kw["reply_to"]
        for name, kw in client.calls[since:]
        if name == "iter_messages" and kw["chat_id"] == NEWS_ID and kw["reply_to"] is not None
    ]


async def test_a_flood_wait_on_a_thread_keeps_what_the_batch_earned(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """A flood wait leaves the whole run, so the progress the batch made must be written first.

    Without it the posts whose threads are already stored are re-requested on every later run —
    against a channel Telegram is already rate-limiting — and the interrupted thread keeps
    nothing of what it fetched.
    """
    comments = {
        (NEWS_ID, post): [
            tl.message(DISC_ID, 100 * post + i, f"c{post}-{i}", sender=1) for i in (1, 2, 3)
        ]
        for post in range(1, 6)
    }
    client = _news_client(
        messages={NEWS_ID: _posts(3, 3, 3, 3, 3)},
        comments=comments,
        failures={(NEWS_ID, 3): (2, errors.FloodWaitError(request=None, capture=3600))},
    )
    cfg = _cfg(NEWS_SOURCE, edit_refetch=0)

    first = await _run(client, conn, paths, cfg)
    assert first.chats_remaining == [NEWS_ID] and len(first.warnings) == 1
    assert _threads_asked(client) == [1, 2, 3]
    news = db.get_chat(conn, NEWS_ID)
    assert news is not None and news.last_msg_id == 2
    stored = {m.msg_id for m in db.get_messages(conn, DISC_ID)}
    assert stored == {101, 102, 103, 201, 202, 203, 302, 303}  # post 3 keeps the prefix it read

    calls = len(client.calls)
    await _run(client, conn, paths, cfg)
    assert _threads_asked(client, calls) == [3]


async def test_a_thread_stored_in_full_is_not_requested_again(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The rule :func:`_refresh_comments` uses, applied to the incremental pass too: a post whose
    reply count is already stored costs no request when the run resumes over it."""
    client = _news_client(messages={NEWS_ID: _posts(2, 0, 1)})
    cfg = _cfg(NEWS_SOURCE, edit_refetch=0)
    await _run(client, conn, paths, cfg)
    assert _threads_asked(client) == [1, 3]
    news = db.get_chat(conn, NEWS_ID)
    assert news is not None
    db.set_chat_progress(conn, NEWS_ID, 0, news.last_sync_at)

    calls = len(client.calls)
    await _run(client, conn, paths, cfg)
    assert _threads_asked(client, calls) == []
    assert {m.msg_id for m in db.get_messages(conn, DISC_ID)} == {1, 2, 9}


async def test_private_discussion_group_disables_comments_and_keeps_the_posts(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _news_client(
        entities=[],
        responses={
            functions.channels.GetFullChannelRequest: _full_channel(201, chats=[NEWS]),
        },
        entity_errors={DISC_ID: errors.ChannelPrivateError(request=None)},
    )
    report = await _run(client, conn, paths, _cfg(NEWS_SOURCE))
    assert report.chats_done == [NEWS_ID] and report.unavailable == []
    assert report.new == 3
    assert len(report.warnings) == 1
    assert "comments of channel" in report.warnings[0] and "unavailable" in report.warnings[0]
    assert _texts(conn, NEWS_ID) == {1: "post 1", 2: "post 2", 3: "post 3"}
    assert db.get_chat(conn, DISC_ID) is None
    assert all(c["reply_to"] is None for c in _fetch_calls(client, NEWS_ID))


async def test_refused_comment_thread_switches_comments_off_for_the_run(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _news_client(
        messages={
            NEWS_ID: [
                tl.channel_post(NEWS_ID, 1, "post 1", replies=2),
                tl.channel_post(NEWS_ID, 2, "post 2", replies=0),
                tl.channel_post(NEWS_ID, 3, "post 3", replies=1),
            ]
        },
        failures={(NEWS_ID, 1): errors.ChannelPrivateError(request=None)},
    )
    cfg = _cfg(NEWS_SOURCE, edit_refetch=10)
    report = await _run(client, conn, paths, cfg)
    assert report.chats_done == [NEWS_ID] and report.unavailable == []
    assert len(report.warnings) == 1 and "comments of channel" in report.warnings[0]
    assert _texts(conn, NEWS_ID) == {1: "post 1", 2: "post 2", 3: "post 3"}
    news = db.get_chat(conn, NEWS_ID)
    assert news is not None and news.last_msg_id == 3 and not news.unavailable
    assert db.get_messages(conn, DISC_ID) == []
    threads = [c["reply_to"] for c in _fetch_calls(client, NEWS_ID) if c["reply_to"] is not None]
    assert threads == [1]
    client.failures.clear()
    recovered = await _run(client, conn, paths, cfg)
    assert recovered.warnings == [] and recovered.new == 3
    assert {m.msg_id: m.comment_of_msg_id for m in db.get_messages(conn, DISC_ID)} == {
        1: 1,
        2: 1,
        9: 3,
    }


async def test_link_discussion_chat_resolves_the_group_through_get_entity(
    conn: sqlite3.Connection,
) -> None:
    client = _news_client(
        responses={functions.channels.GetFullChannelRequest: _full_channel(201, chats=[NEWS])}
    )
    news = db.upsert_chat(conn, ChatRow(id=NEWS_ID, type="channel", source_id=NEWS_SOURCE.id))
    synced = await sync.sync_chat(client, conn, news, NEWS_SOURCE, SyncBudget())
    assert ("get_entity", {"key": DISC_ID}) in client.calls
    discussion = db.get_chat(conn, DISC_ID)
    assert discussion is not None and discussion.discussion_of == NEWS_ID
    assert synced.discussion is not None and synced.discussion.new == 3


# --- discussion group listed as a chat of its own --------------------------------------------


def _discussion_state(conn: sqlite3.Connection, disc_id: int) -> dict[str, object]:
    chat = db.get_chat(conn, disc_id)
    assert chat is not None
    return {
        "source_id": chat.source_id,
        "discussion_of": chat.discussion_of,
        "topics": {m.msg_id: m.topic_id for m in db.get_messages(conn, disc_id)},
        "comments": {m.msg_id: m.comment_of_msg_id for m in db.get_messages(conn, disc_id)},
        "window_topics": {u.topic_id for u in db.get_units(conn, disc_id) if u.kind == "window"},
    }


@pytest.mark.parametrize(
    ("disc", "config"),
    [
        pytest.param(DISC, [Source(folder="News", comments=True)], id="folder-group-first"),
        pytest.param(
            make_channel(199, "News chat", username="news_chat", megagroup=True),
            [Source(folder="News", comments=True)],
            id="folder-channel-first",
        ),
        pytest.param(
            DISC,
            [Source(chat="@news", comments=True), Source(chat=DISC_ID)],
            id="explicit-sources",
        ),
    ],
)
async def test_discussion_group_listed_by_a_source_keeps_its_history_and_the_comments(
    conn: sqlite3.Connection, paths: Paths, disc: types.Channel, config: list[Source]
) -> None:
    """Whichever of the two syncs first, the group ends up with its whole history, the
    comments carry their post ids, its windows are linear, and its row belongs to the source
    that lists it (the channel's only when none does)."""
    disc_id = -1000000000000 - disc.id
    client = _discussion_client(disc)
    cfg = _cfg(*config)
    expected = {
        "source_id": config[-1].id,
        "discussion_of": NEWS_ID,
        "topics": {1: None, 2: None, 9: None},
        "comments": {1: 1, 2: 1, 9: 3},
        "window_topics": {None},
    }
    first = await _run(client, conn, paths, cfg)
    assert sorted(first.chats_done) == sorted([disc_id, NEWS_ID])
    assert first.unavailable == [] and first.warnings == []
    assert _discussion_state(conn, disc_id) == expected
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1", "comment one", "reply"]
    assert len([c for c in _fetch_calls(client, disc_id) if c["reverse"]]) == 1
    second = await _run(client, conn, paths, cfg)
    assert sorted(second.chats_done) == sorted([disc_id, NEWS_ID]) and second.new == 0
    assert _discussion_state(conn, disc_id) == expected
    assert len([c for c in _fetch_calls(client, disc_id) if c["reverse"]]) == 2
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1", "comment one", "reply"]
    by_source = {s.source_id: [c.id for c in s.chats] for s in sources.sources_status(cfg, conn)}
    if len(config) > 1:
        assert by_source == {config[0].id: [NEWS_ID], config[1].id: [disc_id]}
    else:
        assert by_source == {config[0].id: sorted([disc_id, NEWS_ID])}


async def test_plain_history_refetch_leaves_comments_and_their_post_ids_alone(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _discussion_client(DISC)
    cfg = _cfg(Source(folder="News", comments=True), edit_refetch=10)
    await _run(client, conn, paths, cfg)
    group = db.get_chat(conn, DISC_ID)
    assert group is not None and group.last_sync_at is not None
    again = await sync.sync_chat(client, conn, group, cfg.sources[0], SyncBudget(), cfg=cfg)
    assert again.complete and again.new == 0 and again.new_msg_ids == []
    assert {m.msg_id: m.comment_of_msg_id for m in db.get_messages(conn, DISC_ID)} == {
        1: 1,
        2: 1,
        9: 3,
    }


def _busy_discussion_client(disc: types.Channel) -> FakeClient:
    """:func:`_discussion_client` with general talk around the comments: the group's history is
    1..20 a minute apart and 25..30 two hours later, where 1 and 2 (post 1) and 25 (post 3) are
    the comments."""
    disc_id = -1000000000000 - disc.id
    history = {
        i: tl.message(disc_id, i, f"g{i}", sender=1, date=tl.at(i if i <= 20 else 120 + i))
        for i in [*range(1, 21), *range(25, 31)]
    }
    return FakeClient(
        dialogs=[make_dialog(ALICE), make_dialog(BOB), make_dialog(NEWS), make_dialog(disc)],
        me=ME,
        folders=[make_folder(3, "News", include=[NEWS, disc])],
        messages={
            NEWS_ID: _posts(),
            disc_id: list(history.values()),
        },
        comments={(NEWS_ID, 1): [history[1], history[2]], (NEWS_ID, 3): [history[25]]},
        responses={
            functions.channels.GetFullChannelRequest: _full_channel(disc.id, chats=[NEWS, disc])
        },
    )


def _windows(conn: sqlite3.Connection, chat_id: int) -> list[list[int]]:
    return sorted(u.msg_ids for u in db.get_units(conn, chat_id) if u.kind == "window")


@pytest.mark.parametrize(
    "disc",
    [
        pytest.param(DISC, id="group-first"),
        pytest.param(
            make_channel(199, "News chat", username="news_chat", megagroup=True),
            id="channel-first",
        ),
    ],
)
async def test_discussion_group_history_is_windowed_whichever_side_syncs_first(
    conn: sqlite3.Connection, paths: Paths, disc: types.Channel
) -> None:
    """When the channel syncs first, its comments land in the group at ids 1, 2 and 25 and
    start the group's windows; the history 3..20 that arrives afterwards lies below the open
    window and must still end up in windows — and therefore in search."""
    disc_id = -1000000000000 - disc.id
    client = _busy_discussion_client(disc)
    report = await _run(client, conn, paths, _cfg(Source(folder="News", comments=True)))
    assert report.warnings == [] and sorted(report.chats_done) == sorted([disc_id, NEWS_ID])
    stored = {m.msg_id for m in db.get_messages(conn, disc_id)}
    assert len(stored) == 26
    windows = _windows(conn, disc_id)
    assert windows == [list(range(1, 21)), list(range(25, 31))]
    assert {i for ids in windows for i in ids} == stored
    assert db.containing_unit(conn, disc_id, 10, None) is not None
    assert [m.anchor_msg_id for m in search.lexical_messages(conn, "g10", Filters(), 10)] == [10]
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1", "g1", "g2"]


async def test_late_comment_on_an_older_post_is_windowed_in_the_group(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Run 1 stores post 1's comments (1, 2) and post 3's (25, two hours later); between the
    runs comment 22 lands on post 1 — below the group's open window, which starts at 25."""
    client = _news_client(
        messages={
            NEWS_ID: [
                tl.channel_post(NEWS_ID, 1, "post 1", replies=2),
                tl.channel_post(NEWS_ID, 2, "post 2"),
                tl.channel_post(NEWS_ID, 3, "post 3", replies=1, date=tl.at(130)),
            ]
        },
        comments={
            (NEWS_ID, 1): [
                tl.message(DISC_ID, 1, "c1", sender=1, date=tl.at(0)),
                tl.message(DISC_ID, 2, "c2", sender=2, date=tl.at(1)),
            ],
            (NEWS_ID, 3): [tl.message(DISC_ID, 25, "c25", sender=1, date=tl.at(131))],
        },
    )
    cfg = _cfg(NEWS_SOURCE, edit_refetch=10)
    await _run(client, conn, paths, cfg)
    assert _windows(conn, DISC_ID) == [[1, 2], [25]]
    client.messages[NEWS_ID][0] = tl.channel_post(NEWS_ID, 1, "post 1", replies=3)
    client.comments[(NEWS_ID, 1)].append(tl.message(DISC_ID, 22, "c22", sender=2, date=tl.at(10)))
    grown = await _run(client, conn, paths, cfg)
    assert grown.new == 1 and grown.warnings == []
    assert _windows(conn, DISC_ID) == [[1, 2, 22], [25]]
    assert db.containing_unit(conn, DISC_ID, 22, None) is not None
    assert [m.anchor_msg_id for m in search.lexical_messages(conn, "c22", Filters(), 10)] == [22]
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1", "c1", "c2", "c22"]


def _big_discussion_client(count: int) -> FakeClient:
    """:func:`_discussion_client` with a group history of ``count`` messages, three of which
    are the comments of the channel's posts."""
    history = {i: tl.message(DISC_ID, i, f"g{i}", sender=1) for i in range(1, count + 1)}
    return FakeClient(
        dialogs=[make_dialog(ALICE), make_dialog(BOB), make_dialog(NEWS), make_dialog(DISC)],
        me=ME,
        folders=[make_folder(3, "News", include=[NEWS, DISC])],
        messages={
            NEWS_ID: _posts(),
            DISC_ID: list(history.values()),
        },
        comments={(NEWS_ID, 1): [history[1], history[2]], (NEWS_ID, 3): [history[9]]},
        responses={
            functions.channels.GetFullChannelRequest: _full_channel(201, chats=[NEWS, DISC])
        },
    )


async def test_large_discussion_group_keeps_its_history_across_budgeted_runs(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The group sorts before its channel and takes three budgeted runs; the channel's first
    sync then adds the post ids to the comments without touching the rest."""
    client = _big_discussion_client(1200)
    cfg = _cfg(Source(folder="News", comments=True))
    # the budget is read when it is created, by the loop's check and by the flood-sleep cap
    # before the first chat, then after its first batch: expire right there
    reports = [
        await _run(client, conn, paths, cfg, 10, clock=_clock(0, 0, 0, 100)) for _ in range(3)
    ]
    assert [r.chats_remaining for r in reports] == [
        [DISC_ID, NEWS_ID],
        [DISC_ID, NEWS_ID],
        [NEWS_ID],
    ]
    assert reports[2].chats_done == [DISC_ID]
    assert db.message_counts(conn) == {DISC_ID: 1200}
    units_before = sorted(u.id for u in db.get_units(conn, DISC_ID) if u.id is not None)
    assert len(units_before) >= 40
    fourth = await _run(client, conn, paths, cfg)
    assert fourth.warnings == [] and fourth.chats_done == [NEWS_ID, DISC_ID]
    assert fourth.new == 3
    assert db.message_counts(conn) == {DISC_ID: 1200, NEWS_ID: 3}
    posts = {
        m.msg_id: m.comment_of_msg_id for m in db.get_messages(conn, DISC_ID) if m.comment_of_msg_id
    }
    assert posts == {1: 1, 2: 1, 9: 3}
    assert sorted(u.id for u in db.get_units(conn, DISC_ID) if u.id is not None) == units_before
    assert [v.text for v in search.thread(conn, NEWS_ID, 1)] == ["post 1", "g1", "g2"]


class _VanishingClient(FakeClient):
    """A client whose chat ``gone`` is deleted from the database as its first message arrives —
    a removal that got past the sync lock, seen from the sync's side."""

    def __init__(self, conn: sqlite3.Connection, gone: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._conn = conn
        self._gone = gone

    async def iter_messages(
        self, entity: Any, *args: Any, **kwargs: Any
    ) -> AsyncIterator[types.Message | None]:
        async for msg in super().iter_messages(entity, *args, **kwargs):
            if self._peer_id(entity) == self._gone:
                db.delete_chat(self._conn, self._gone)
            yield msg


async def test_chat_removed_during_the_sync_is_a_warning(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _VanishingClient(
        conn,
        ARG_ID,
        dialogs=[make_dialog(ARG), make_dialog(ALICE)],
        me=ME,
        folders=[make_folder(3, "Argentina", include=[ARG])],
        messages={
            ARG_ID: [tl.message(ARG_ID, 1, "m1", sender=1)],
            ALICE_ID: [tl.message(ALICE_ID, 1, "hi", sender=1)],
        },
    )
    report = await _run(client, conn, paths, _cfg(ARG_SOURCE, ALICE_SOURCE))
    assert report.warnings == [
        f"chat {ARG_ID} (Argentina chat) was removed while it was being synced"
    ]
    assert report.chats_done == [ALICE_ID] and report.chats_remaining == []
    assert report.new == 1
    assert db.get_chat(conn, ARG_ID) is None
    assert db.last_sync_run(conn) is not None


# --- migration -------------------------------------------------------------------------------


async def test_migrated_group_links_the_new_supergroup(conn: sqlite3.Connection) -> None:
    client = _client(messages={OLD_ID: [tl.message(OLD_ID, 1, "old times", sender=1)]})
    old = db.upsert_chat(
        conn, ChatRow(id=OLD_ID, type="group", title="Old group", source_id="chat:-10")
    )
    synced = await sync.sync_chat(client, conn, old, Source(chat=OLD_ID), SyncBudget())
    assert synced.migrated_to is not None
    assert synced.migrated_to.id == GEORGIA_ID
    assert synced.migrated_to.type == "supergroup"
    assert synced.migrated_to.source_id == "chat:-10"
    assert synced.chat.migrated_to == GEORGIA_ID
    assert _texts(conn, OLD_ID) == {1: "old times"}
    client.calls.clear()
    again = await sync.sync_chat(client, conn, synced.chat, Source(chat=OLD_ID), SyncBudget())
    assert again.migrated_to == synced.migrated_to
    assert again.new_msg_ids == []
    assert _fetch_calls(client, OLD_ID) == []
    assert ("get_entity", {"key": OLD_ID}) not in client.calls


async def test_group_without_migration_is_synced_normally(conn: sqlite3.Connection) -> None:
    plain = make_group(11, "Plain group")
    client = _client(entities=[plain], messages={-11: [tl.message(-11, 1, "hi", sender=1)]})
    chat = db.upsert_chat(conn, ChatRow(id=-11, type="group"))
    synced = await sync.sync_chat(client, conn, chat, Source(chat=-11), SyncBudget())
    assert synced.migrated_to is None
    assert _texts(conn, -11) == {1: "hi"}


async def test_migration_check_survives_unresolvable_entities(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    gone = make_group(13, "Gone", migrated_to=999)
    client = _client(
        entities=[gone],
        messages={
            -12: [tl.message(-12, 1, "unknown group", sender=1)],
            -13: [tl.message(-13, 1, "moved somewhere", sender=1)],
        },
    )
    unknown = db.upsert_chat(conn, ChatRow(id=-12, type="group", source_id="chat:-12"))
    moved = db.upsert_chat(conn, ChatRow(id=-13, type="group", source_id="chat:-13"))
    with caplog.at_level("WARNING", logger="grepogram.sync"):
        first = await sync.sync_chat(client, conn, unknown, Source(chat=-12), SyncBudget())
        second = await sync.sync_chat(client, conn, moved, Source(chat=-13), SyncBudget())
    assert first.migrated_to is None and _texts(conn, -12) == {1: "unknown group"}
    assert second.migrated_to is None and _texts(conn, -13) == {1: "moved somewhere"}
    stored = db.get_chat(conn, -13)
    assert stored is not None and stored.migrated_to is None
    messages = [record.getMessage() for record in caplog.records]
    assert any("cannot check for migration" in m for m in messages)
    assert any("cannot be resolved" in m for m in messages)


# --- sync_all --------------------------------------------------------------------------------


async def test_sync_all_resolves_sources_and_runs_hooks(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _news_client(
        messages={
            ARG_ID: [tl.message(ARG_ID, i, f"m{i}", sender=1) for i in (1, 2)],
            NEWS_ID: _posts(2),
            ALICE_ID: [],
        }
    )
    seen: list[tuple[int, list[int]]] = []
    monkeypatch.setattr(
        sync, "on_chat_synced", lambda c, chat, cfg, ids: seen.append((chat.id, ids))
    )
    report = await _run(client, conn, paths, _cfg(ARG_SOURCE, NEWS_SOURCE, ALICE_SOURCE))
    assert report.new == 5
    assert sorted(report.chats_done) == sorted([ARG_ID, NEWS_ID, ALICE_ID])
    assert report.chats_remaining == []
    assert report.unavailable == []
    assert report.warnings == []
    assert {chat_id for chat_id, _ in seen} == {ARG_ID, NEWS_ID, DISC_ID}
    for chat_id, ids in seen:
        assert set(ids) == {m.id for m in db.get_messages(conn, chat_id)}
    assert db.get_chat(conn, DISC_ID) is not None
    assert all(c.last_sync_at is not None for c in db.list_chats(conn) if c.id != DISC_ID)


async def test_sync_all_orders_never_synced_first_then_oldest(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup", last_sync_at=200))
    db.upsert_chat(conn, ChatRow(id=NEWS_ID, type="channel", last_sync_at=100))
    client = _news_client(linked=None, messages={})
    await _run(client, conn, paths, _cfg(ARG_SOURCE, NEWS_SOURCE, ALICE_SOURCE))
    order = [
        kw["chat_id"] for name, kw in client.calls if name == "iter_messages" and kw["reverse"]
    ]
    assert order == [ALICE_ID, NEWS_ID, ARG_ID]


async def test_sync_all_budget_leaves_chats_remaining(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(
        messages={
            ARG_ID: [tl.message(ARG_ID, i, f"m{i}", sender=1) for i in range(1, 1201)],
            ALICE_ID: [tl.message(ALICE_ID, 1, "hi", sender=1)],
        }
    )
    cfg = _cfg(ARG_SOURCE, ALICE_SOURCE)
    async with tg.connected(client):
        report = await sync.sync_all(
            client, conn, cfg, paths, SyncBudget(10, clock=_clock(0, 0, 100))
        )
    assert report.new == sync.BATCH_SIZE
    assert report.chats_done == []
    assert report.chats_remaining == [ARG_ID, ALICE_ID]
    assert _arg_chat(conn).last_msg_id == sync.BATCH_SIZE
    assert db.message_counts(conn) == {ARG_ID: sync.BATCH_SIZE}
    resumed = await _run(client, conn, paths, cfg)
    assert resumed.new == 701
    assert sorted(resumed.chats_done) == sorted([ARG_ID, ALICE_ID])
    assert resumed.chats_remaining == []
    assert db.message_counts(conn) == {ARG_ID: 1200, ALICE_ID: 1}


async def test_sync_all_reports_unavailable_and_keeps_going(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(
        messages={ALICE_ID: [tl.message(ALICE_ID, 1, "hi", sender=1)]},
        failures={ARG_ID: errors.ChannelPrivateError(request=None)},
    )
    report = await _run(client, conn, paths, _cfg(ARG_SOURCE, ALICE_SOURCE))
    assert report.unavailable == [ARG_ID]
    assert report.chats_done == [ALICE_ID]
    assert report.new == 1
    assert _arg_chat(conn).unavailable


async def test_sync_all_stops_on_flood_wait_with_a_warning(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup", last_sync_at=100))
    db.upsert_chat(conn, ChatRow(id=NEWS_ID, type="channel", last_sync_at=200))
    client = _news_client(
        linked=None,
        messages={ALICE_ID: [tl.message(ALICE_ID, 1, "hi", sender=1)]},
        failures={ARG_ID: errors.FloodWaitError(request=None, capture=3600)},
    )
    report = await _run(client, conn, paths, _cfg(ARG_SOURCE, NEWS_SOURCE, ALICE_SOURCE))
    assert report.chats_done == [ALICE_ID]
    assert report.chats_remaining == [ARG_ID, NEWS_ID]
    assert len(report.warnings) == 1
    assert "flood wait" in report.warnings[0]
    assert "3600" in report.warnings[0]
    assert _fetch_calls(client, NEWS_ID) == []


async def test_sync_all_skips_a_chat_on_other_rpc_errors(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(
        messages={ALICE_ID: [tl.message(ALICE_ID, 1, "hi", sender=1)]},
        failures={ARG_ID: errors.ChatIdInvalidError(request=None)},
    )
    report = await _run(client, conn, paths, _cfg(ARG_SOURCE, ALICE_SOURCE))
    assert report.chats_done == [ALICE_ID]
    assert report.chats_remaining == [ARG_ID]
    assert report.unavailable == []
    assert len(report.warnings) == 1 and str(ARG_ID) in report.warnings[0]
    assert not _arg_chat(conn).unavailable


async def test_sync_all_raises_auth_required(conn: sqlite3.Connection, paths: Paths) -> None:
    with pytest.raises(tg.AuthRequired):
        await _run(_client(authorized=False), conn, paths, _cfg(ARG_SOURCE))
    revoked = _client(failures={ARG_ID: errors.AuthKeyUnregisteredError(request=None)})
    with pytest.raises(tg.AuthRequired):
        await _run(revoked, conn, paths, _cfg(ARG_SOURCE))


async def test_sync_all_refuses_while_another_sync_holds_the_lock(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(messages={})
    with SyncLock(paths):
        with pytest.raises(SyncInProgress):
            await _run(client, conn, paths, _cfg(ARG_SOURCE))
    assert _fetch_calls(client, ARG_ID) == []
    report = await _run(client, conn, paths, _cfg(ARG_SOURCE))
    assert report.chats_done == [ARG_ID]


async def test_sync_all_reads_a_config_loader_once_the_lock_is_held(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """The sources come from the config as it is under the lock, so a source removed while the
    caller was still loading its model or connecting is not resolved and fetched again."""
    client = _client(messages={ALICE_ID: [tl.message(ALICE_ID, 1, "hi", sender=1)]})
    loads: list[bool] = []

    def current() -> Config:
        with pytest.raises(SyncInProgress), SyncLock(paths):
            pass
        loads.append(True)
        return _cfg(ALICE_SOURCE)

    async with tg.connected(client):
        report = await sync.sync_all(client, conn, current, paths, SyncBudget())
    assert loads == [True]
    assert report.chats_done == [ALICE_ID] and report.new == 1
    assert [c.id for c in db.list_chats(conn)] == [ALICE_ID]


async def test_sync_all_syncs_the_supergroup_a_group_migrated_to(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(
        messages={
            OLD_ID: [tl.message(OLD_ID, 1, "old", sender=1)],
            GEORGIA_ID: [tl.message(GEORGIA_ID, 1, "new", sender=1)],
        }
    )
    report = await _run(client, conn, paths, _cfg(Source(chat=OLD_ID)))
    assert report.chats_done == [OLD_ID, GEORGIA_ID]
    assert report.new == 2
    old = db.get_chat(conn, OLD_ID)
    new = db.get_chat(conn, GEORGIA_ID)
    assert old is not None and new is not None
    assert old.migrated_to == GEORGIA_ID
    assert new.source_id == "chat:-10"
    assert _texts(conn, GEORGIA_ID) == {1: "new"}
    client.calls.clear()
    again = await _run(client, conn, paths, _cfg(Source(chat=OLD_ID)))
    assert again.chats_done == [OLD_ID, GEORGIA_ID]
    assert _fetch_calls(client, OLD_ID) == []
    assert _fetch_calls(client, GEORGIA_ID) != []


async def test_sync_all_does_not_queue_a_supergroup_it_already_processed(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(
        messages={
            OLD_ID: [tl.message(OLD_ID, 1, "old", sender=1)],
            GEORGIA_ID: [tl.message(GEORGIA_ID, 1, "new", sender=1)],
        }
    )
    report = await _run(client, conn, paths, _cfg(Source(chat=OLD_ID), Source(chat=GEORGIA_ID)))
    assert sorted(report.chats_done) == sorted([OLD_ID, GEORGIA_ID])
    assert len(report.chats_done) == 2 and report.chats_remaining == []
    assert len([c for c in _fetch_calls(client, GEORGIA_ID) if c["reverse"]]) == 1


async def test_sync_all_with_no_sources_is_empty(conn: sqlite3.Connection, paths: Paths) -> None:
    before = db.last_sync_run(conn)
    report = await _run(_client(), conn, paths, _cfg())
    assert report == SyncReport()
    assert before is None and db.last_sync_run(conn) is not None


async def test_sync_all_stamps_the_run_even_when_no_chat_completes(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(failures={ARG_ID: errors.ChannelPrivateError(request=None)})
    report = await _run(client, conn, paths, _cfg(ARG_SOURCE))
    assert report.unavailable == [ARG_ID] and report.chats_done == []
    assert db.last_sync_at(conn) is None
    assert db.last_sync_run(conn) is not None


@pytest.mark.parametrize(
    ("threshold", "seconds", "expected"),
    [(120, None, 120), (120, 10, 10), (5, 10, 5)],
    ids=["unbounded", "budget-caps", "config-caps"],
)
async def test_sync_all_caps_the_flood_sleep_at_the_budget(
    conn: sqlite3.Connection,
    paths: Paths,
    threshold: int,
    seconds: float | None,
    expected: int,
) -> None:
    client = _client(messages={ALICE_ID: [tl.message(ALICE_ID, 1, "hi", sender=1)]})
    cfg = Config(
        telegram=TELEGRAM,
        sync=SyncCfg(flood_sleep_threshold=threshold),
        sources=[ALICE_SOURCE],
    )
    async with tg.connected(client):
        await sync.sync_all(client, conn, cfg, paths, SyncBudget(seconds))
    assert client.flood_sleep_threshold == expected


# --- cli -------------------------------------------------------------------------------------


CONFIG = '[telegram]\napi_id = 12345\napi_hash = "fakehash"\n\n[[sources]]\nchat = "@alice"\n'


def _setup(tmp_home: Path, config: str = CONFIG) -> Paths:
    paths = Paths.from_env()
    paths.ensure_dirs()
    paths.config_file.write_text(config)
    paths.session_file.touch()
    return paths


def test_cli_sync_prints_the_report(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _setup(tmp_home)
    fake = _client(
        messages={ALICE_ID: [tl.message(ALICE_ID, i, f"m{i}", sender=1) for i in (1, 2)]}
    )
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: fake)
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "new messages: 2" in result.stdout
    assert "chats synced: 1" in result.stdout
    assert "remaining" not in result.stdout
    assert ("disconnect", {}) in fake.calls
    conn = db.connect(paths)
    try:
        assert db.message_counts(conn) == {ALICE_ID: 2}
    finally:
        conn.close()


def test_cli_sync_smallest_budget_reports_remaining(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One second is the smallest budget the CLI takes; an expired one leaves every chat behind."""
    _setup(tmp_home)
    fake = _client(messages={ALICE_ID: [tl.message(ALICE_ID, 1, "m1", sender=1)]})
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: fake)

    def expired(seconds: float) -> SyncBudget:
        return SyncBudget(seconds, clock=_clock(0, seconds + 1))

    monkeypatch.setattr(sync, "SyncBudget", expired)
    result = runner.invoke(cli.app, ["sync", "--budget", "1"])
    assert result.exit_code == 0, result.output
    assert "new messages: 0" in result.stdout
    assert f"chats remaining: 1 ({ALICE_ID}); run sync again" in result.stdout
    assert _fetch_calls(fake, ALICE_ID) == []


def test_cli_sync_prints_warnings_and_unavailable(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_home, CONFIG + '\n[[sources]]\nfolder = "Argentina"\n')
    fake = _client(failures={ARG_ID: errors.FloodWaitError(request=None, capture=60)})
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: fake)
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "warning: flood wait" in result.stderr
    fake.failures[ARG_ID] = errors.ChannelPrivateError(request=None)
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 0, result.output
    assert f"chats unavailable: 1 ({ARG_ID})" in result.stdout


def test_cli_sync_requires_sources_keys_and_session(tmp_home: Path) -> None:
    no_sources = runner.invoke(cli.app, ["sync"])
    assert no_sources.exit_code == 1
    assert "api_id and api_hash" in no_sources.stderr
    paths = Paths.from_env()
    paths.ensure_dirs()
    paths.config_file.write_text('[telegram]\napi_id = 12345\napi_hash = "fakehash"\n')
    empty = runner.invoke(cli.app, ["sync"])
    assert empty.exit_code == 1
    assert "no sources configured" in empty.stderr
    paths.config_file.write_text(CONFIG)
    no_session = runner.invoke(cli.app, ["sync"])
    assert no_session.exit_code == 1
    assert "grepogram auth" in no_session.stderr


def test_cli_sync_reports_lock_and_auth_errors(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _setup(tmp_home)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: _client(messages={}))
    with SyncLock(paths):
        locked = runner.invoke(cli.app, ["sync"])
    assert locked.exit_code == 1
    assert "another sync is running" in locked.stderr
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: _client(authorized=False))
    unauthorized = runner.invoke(cli.app, ["sync"])
    assert unauthorized.exit_code == 1
    assert "grepogram auth" in unauthorized.stderr


@pytest.mark.parametrize("budget", ["-1", "0"], ids=["negative", "zero"])
def test_cli_sync_rejects_a_budget_that_fetches_nothing(tmp_home: Path, budget: str) -> None:
    """``--budget 0`` used to be accepted and then fetch nothing; the MCP tool always refused it."""
    result = runner.invoke(cli.app, ["sync", "--budget", budget])
    assert result.exit_code != 0
    assert "--budget" in result.output


def test_cli_sync_maps_network_errors(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(tmp_home)
    offline = _client()

    async def failing_connect() -> None:
        raise ConnectionError("offline")

    monkeypatch.setattr(offline, "connect", failing_connect)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: offline)
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 1
    assert "error: telegram error: offline" in result.stderr
    flooded = _client(
        responses={
            functions.messages.GetDialogFiltersRequest: errors.FloodWaitError(
                request=None, capture=30
            )
        }
    )
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: flooded)
    result = runner.invoke(cli.app, ["sync"])
    assert result.exit_code == 1
    assert "error: telegram error:" in result.stderr and "30" in result.stderr


# --- indexing what a run committed -----------------------------------------------------------


def _fts_count(conn: sqlite3.Connection, chat_id: int) -> int:
    row = conn.execute("SELECT count(*) FROM msg_fts WHERE chat_id = ?", (chat_id,)).fetchone()
    return int(row[0])


def _unit_fts_count(conn: sqlite3.Connection, chat_id: int) -> int:
    row = conn.execute("SELECT count(*) FROM unit_fts WHERE chat_id = ?", (chat_id,)).fetchone()
    return int(row[0])


def _windowed(conn: sqlite3.Connection, chat_id: int) -> set[int]:
    return {i for u in db.get_units(conn, chat_id) if u.kind == "window" for i in u.msg_ids}


def _threaded_history(count: int) -> list[types.Message]:
    """``count`` messages a minute apart where every third one replies to the one before it."""
    return [
        tl.message(
            ARG_ID,
            i,
            f"w{i} word",
            sender=1,
            date=tl.at(i),
            reply_to=tl.reply_header(i - 1) if i % 3 == 0 else None,
        )
        for i in range(1, count + 1)
    ]


@pytest.mark.parametrize(
    "error",
    [
        errors.FloodWaitError(request=None, capture=999),
        errors.ServerError(request=None, message="INTERNAL"),
    ],
    ids=["flood-wait", "rpc-error"],
)
async def test_batches_committed_before_a_failure_are_indexed_in_the_same_run(
    conn: sqlite3.Connection, paths: Paths, error: Exception
) -> None:
    """The 500 rows stored before the error get their units and ``msg_fts`` rows before the
    run reports, and the next run — which resumes above them — leaves nothing behind."""
    client = _client(messages={ARG_ID: _threaded_history(1200)}, failures={ARG_ID: (700, error)})
    cfg = _cfg(ARG_SOURCE)
    first = await _run(client, conn, paths, cfg)
    assert first.chats_done == [] and first.chats_remaining == [ARG_ID]
    assert len(first.warnings) == 1
    assert db.message_counts(conn) == {ARG_ID: sync.BATCH_SIZE}
    assert _fts_count(conn, ARG_ID) == sync.BATCH_SIZE
    assert db.unindexed_message_ids(conn, ARG_ID) == []
    assert _windowed(conn, ARG_ID) == set(range(1, sync.BATCH_SIZE + 1))
    assert [m.anchor_msg_id for m in search.lexical_messages(conn, "w10", Filters(), 5)] == [10]
    client.failures.clear()
    second = await _run(client, conn, paths, cfg)
    assert second.chats_done == [ARG_ID] and second.new == 700 and second.warnings == []
    assert _fts_count(conn, ARG_ID) == 1200
    assert db.unindexed_message_ids(conn, ARG_ID) == []
    assert _windowed(conn, ARG_ID) == set(range(1, 1201))
    roots = {u.msg_ids[0] for u in db.get_units(conn, ARG_ID) if u.kind == "thread"}
    assert roots == {i - 1 for i in range(1, 1201) if i % 3 == 0}
    for word in ("w10", "w499", "w501", "w900"):
        assert len(search.lexical_messages(conn, word, Filters(), 5)) == 1


async def test_batches_committed_before_an_auth_error_are_indexed_before_it_propagates(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(
        messages={ARG_ID: _threaded_history(1200)},
        failures={ARG_ID: (700, errors.AuthKeyUnregisteredError(request=None))},
    )
    with pytest.raises(tg.AuthRequired):
        await _run(client, conn, paths, _cfg(ARG_SOURCE))
    assert db.message_counts(conn) == {ARG_ID: sync.BATCH_SIZE}
    assert _fts_count(conn, ARG_ID) == sync.BATCH_SIZE
    assert db.unindexed_message_ids(conn, ARG_ID) == []


@pytest.mark.parametrize("seconds", [0, None], ids=["not-reached", "reached"])
async def test_rows_a_crash_left_unindexed_are_indexed_by_the_next_run(
    conn: sqlite3.Connection, paths: Paths, seconds: float | None
) -> None:
    """Rows committed with the progress already advanced past them — what a process killed
    between a batch commit and the rebuild leaves — are picked up whether the run reaches the
    chat or the budget keeps it from fetching anything."""
    db.upsert_chat(conn, ChatRow(id=ARG_ID, type="supergroup", source_id=ARG_SOURCE.id))
    ids = db.upsert_messages(
        conn,
        [
            MessageRow(chat_id=ARG_ID, msg_id=i, date=1_700_000_000 + i * 60, text=f"stranded w{i}")
            for i in (1, 2, 3)
        ],
    )
    db.set_chat_progress(conn, ARG_ID, 3, 1_700_000_000)
    assert db.unindexed_message_ids(conn, ARG_ID) == ids
    client = _client(messages={ARG_ID: []})
    report = await _run(client, conn, paths, _cfg(ARG_SOURCE), seconds)
    if seconds is None:
        assert report.chats_done == [ARG_ID]
    else:
        assert report.chats_remaining == [ARG_ID] and _fetch_calls(client, ARG_ID) == []
    assert db.unindexed_message_ids(conn, ARG_ID) == []
    assert _fts_count(conn, ARG_ID) == 3
    assert [u.msg_ids for u in db.get_units(conn, ARG_ID)] == [[1, 2, 3]]
    assert len(search.lexical_messages(conn, "stranded", Filters(), 5)) == 1


def _thread_texts(conn: sqlite3.Connection, chat_id: int) -> dict[int, list[str]]:
    """Post id → the texts in its post thread, post first."""
    return {
        u.msg_ids[0]: [line.split(": ", 1)[1] for line in u.text.splitlines()]
        for u in db.get_units(conn, chat_id)
        if u.kind == "thread"
    }


async def test_grown_posts_are_flagged_before_their_threads_are_re_read(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    """Threads that grew are re-read newest post first. When a flood wait stops the re-read
    halfway, the post whose thread was already re-read gets its post thread rebuilt in the
    same run — it was flagged before the reads began, and the run indexes what it committed —
    while the other one waits for the next run."""
    client = _news_client()
    cfg = _cfg(NEWS_SOURCE, edit_refetch=10)
    await _run(client, conn, paths, cfg)
    assert _thread_texts(conn, NEWS_ID) == {
        1: ["post 1", "comment one", "reply"],
        3: ["post 3", "late comment"],
    }
    client.messages[NEWS_ID][0] = tl.channel_post(NEWS_ID, 1, "post 1", replies=3)
    client.comments[(NEWS_ID, 1)].append(tl.message(DISC_ID, 22, "c22", sender=2))
    client.messages[NEWS_ID][2] = tl.channel_post(NEWS_ID, 3, "post 3", replies=2)
    client.comments[(NEWS_ID, 3)].append(tl.message(DISC_ID, 11, "another", sender=1))
    client.failures[(NEWS_ID, 1)] = errors.FloodWaitError(request=None, capture=999)
    stopped = await _run(client, conn, paths, cfg)
    assert stopped.chats_remaining == [NEWS_ID] and "flood wait" in stopped.warnings[0]
    threads = [c["reply_to"] for c in _fetch_calls(client, NEWS_ID) if c["reply_to"] is not None]
    assert threads == [1, 3, 3, 1]
    assert db.unindexed_message_ids(conn, NEWS_ID) == []
    assert db.unindexed_message_ids(conn, DISC_ID) == []
    assert _thread_texts(conn, NEWS_ID) == {
        1: ["post 1", "comment one", "reply"],
        3: ["post 3", "late comment", "another"],
    }
    assert db.containing_unit(conn, DISC_ID, 11, None) is not None
    client.failures.clear()
    resumed = await _run(client, conn, paths, cfg)
    assert resumed.chats_done == [NEWS_ID] and resumed.warnings == [] and resumed.new == 1
    assert _thread_texts(conn, NEWS_ID) == {
        1: ["post 1", "comment one", "reply", "c22"],
        3: ["post 3", "late comment", "another"],
    }
    assert db.unindexed_message_ids(conn, NEWS_ID) == []


async def test_a_cancelled_index_step_joins_its_worker_thread_first() -> None:
    """Cancelling the tool call must not leave a thread writing after the lock is released.

    ``await asyncio.to_thread(...)`` hands the job to the executor and then suspends, so a
    cancellation delivered at that suspension abandons the future rather than the job; under an
    ``anyio`` cancel scope — how the MCP server cancels a call — awaiting anything afterwards
    raises at once, so the join cannot be an ``await``.
    """
    landed: list[str] = []

    def job() -> str:
        time.sleep(0.2)
        landed.append("job")
        return "done"

    async def call() -> None:
        try:
            await sync._joined_to_thread(job)
        finally:
            landed.append("unwound")

    task = asyncio.create_task(call())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert landed == ["job", "unwound"]


async def test_a_cancelled_index_step_waits_for_its_worker_however_long_it_takes(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait has no bound, it only logs while it lasts.

    A bounded one would give the :class:`SyncLock` up over a live writer exactly in the case it
    exists for — the rebuild or the embedding backlog that outlasts the bound — and the next
    process would start writing against a database this one has not finished with.
    """
    monkeypatch.setattr(sync, "JOIN_LOG_EVERY", 0.01)
    landed: list[str] = []

    def job() -> str:
        time.sleep(0.2)
        landed.append("job")
        return "done"

    async def call() -> None:
        try:
            await sync._joined_to_thread(job)
        finally:
            landed.append("unwound")

    with caplog.at_level("WARNING", logger="grepogram.sync"):
        task = asyncio.create_task(call())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert landed == ["job", "unwound"]
    waited = [r for r in caplog.records if "still running after the run was cancelled" in r.message]
    assert len(waited) > 1


async def test_a_cancelled_job_that_can_be_aborted_is_asked_to_stop() -> None:
    """What keeps the unbounded wait short: the embedding step checks its budget between
    batches, so expiring it ends the job at the next one instead of after the backlog."""
    budget = SyncBudget()
    landed: list[str] = []

    def job() -> str:
        for _ in range(500):  # bounded, so a broken abort fails the test instead of hanging it
            if budget.expired:
                landed.append("aborted")
                return "aborted"
            time.sleep(0.01)
        landed.append("ran to the end")
        return "ran to the end"

    task = asyncio.create_task(sync._joined_to_thread(job, budget.cancel))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert landed == ["aborted"]


async def test_joined_to_thread_returns_the_result_and_propagates_failures() -> None:
    assert await sync._joined_to_thread(lambda: 7) == 7

    def explode() -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await sync._joined_to_thread(explode)
