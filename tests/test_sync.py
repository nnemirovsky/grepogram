import datetime as dt
import sqlite3
import stat
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors
from telethon.tl import functions, types
from telethon.tl.types import messages as tl_messages
from typer.testing import CliRunner

from grepogram import cli, db, search, sources, sync, tg
from grepogram.config import ConfigError
from grepogram.log import shutdown_logging
from grepogram.models import (
    ChatRow,
    Config,
    Filters,
    MessageRow,
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

ALICE_ID = 1
OLD_ID = -10
ARG_ID = -1000000000100
GEORGIA_ID = -1000000000101
NEWS_ID = -1000000000200
DISC_ID = -1000000000201

TELEGRAM = TelegramCfg(api_id=12345, api_hash="fakehash")
ARG_SOURCE = Source(folder="Argentina")
NEWS_SOURCE = Source(chat="@news", comments=True)
ALICE_SOURCE = Source(chat="@alice")


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
) -> SyncReport:
    async with tg.connected(client):
        return await sync.sync_all(client, conn, cfg, paths, SyncBudget(seconds, clock=clock))


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
        client, conn, chat, ARG_SOURCE, SyncBudget(), sync_cfg=SyncCfg(edit_refetch=3)
    )
    assert all(c["limit"] is None for c in _fetch_calls(client, ARG_ID))
    before = db.get_message(conn, ARG_ID, 2)
    assert before is not None
    client.messages[ARG_ID][1] = tl.message(
        ARG_ID, 2, "m2 edited", sender=1, edit_date=tl.at(50), reactions=tl.reactions({"👍": 3})
    )
    client.messages[ARG_ID].append(tl.message(ARG_ID, 4, "m4", sender=1))
    second = await sync.sync_chat(
        client, conn, first.chat, ARG_SOURCE, SyncBudget(), sync_cfg=SyncCfg(edit_refetch=3)
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
        client, conn, first.chat, ARG_SOURCE, SyncBudget(), sync_cfg=SyncCfg(edit_refetch=0)
    )
    assert disabled.new_msg_ids == []
    assert all(c["limit"] is None for c in _fetch_calls(client, ARG_ID))


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
    assert {k: v.topic_id for k, v in comments.items()} == {1: 1, 2: 1, 9: 3}
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
    comments = {m.msg_id: m.topic_id for m in db.get_messages(conn, DISC_ID)}
    assert comments == {1: 1, 2: 1, 5: 2, 9: 3}
    (thread,) = [u for u in db.get_units(conn, NEWS_ID) if u.kind == "thread" and u.msg_ids == [2]]
    assert "new comment" in thread.text
    views = search.thread(conn, NEWS_ID, 2)
    assert [v.text for v in views] == ["post 2", "new comment"]
    client.messages[NEWS_ID][2] = tl.channel_post(NEWS_ID, 3, "post 3", replies=2)
    again = await _run(client, conn, paths, cfg)
    assert again.new == 1
    assert {m.msg_id for m in db.get_messages_in_topic(conn, DISC_ID, 3)} == {9, 11}


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
    assert {m.msg_id: m.topic_id for m in db.get_messages(conn, DISC_ID)} == {1: 1, 2: 1, 9: 3}


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
        "topics": {1: 1, 2: 1, 9: 3},
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
    again = await sync.sync_chat(
        client, conn, group, cfg.sources[0], SyncBudget(), sync_cfg=cfg.sync
    )
    assert again.complete and again.new == 0 and again.new_msg_ids == []
    assert {m.msg_id: m.topic_id for m in db.get_messages(conn, DISC_ID)} == {1: 1, 2: 1, 9: 3}


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
    topics = {m.msg_id: m.topic_id for m in db.get_messages(conn, DISC_ID) if m.topic_id}
    assert topics == {1: 1, 2: 1, 9: 3}
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


def test_cli_sync_budget_zero_reports_remaining(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_home)
    fake = _client(messages={ALICE_ID: [tl.message(ALICE_ID, 1, "m1", sender=1)]})
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: fake)
    result = runner.invoke(cli.app, ["sync", "--budget", "0"])
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


def test_cli_sync_rejects_negative_budget(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["sync", "--budget", "-1"])
    assert result.exit_code != 0


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
