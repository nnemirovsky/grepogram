import datetime as dt
import sqlite3
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from telethon import errors
from telethon.tl import functions, types
from telethon.tl.types import messages as tl_messages
from typer.testing import CliRunner

from grepogram import cli, db, sync, tg
from grepogram.config import ConfigError
from grepogram.log import shutdown_logging
from grepogram.models import ChatRow, Config, Source, SyncCfg, TelegramCfg
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
DISC = make_channel(201, "News chat", megagroup=True)

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


def _full_channel(linked: int | None) -> tl_messages.ChatFull:
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
    return tl_messages.ChatFull(full_chat=full, chats=[NEWS, DISC] if linked else [NEWS], users=[])


def _news_client(linked: int | None = 201, **kwargs: object) -> FakeClient:
    kwargs.setdefault(
        "messages", {NEWS_ID: [tl.channel_post(NEWS_ID, i, f"post {i}") for i in (1, 2, 3)]}
    )
    kwargs.setdefault(
        "comments",
        {
            (NEWS_ID, 1): [
                tl.message(DISC_ID, 1, "comment one", sender=1, reply_to=tl.reply_header(7)),
                tl.message(DISC_ID, 2, "reply", sender=2, reply_to=tl.reply_header(1)),
            ],
            (NEWS_ID, 3): [tl.message(DISC_ID, 9, "late comment", sender=2)],
        },
    )
    kwargs.setdefault(
        "responses", {functions.channels.GetFullChannelRequest: _full_channel(linked)}
    )
    return _client(**kwargs)


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
) -> sync.SyncReport:
    async with tg.connected(client):
        return await sync.sync_all(client, conn, cfg, paths, SyncBudget(seconds))


# --- budget ----------------------------------------------------------------------------------


def test_budget_without_seconds_never_expires() -> None:
    budget = SyncBudget()
    assert budget.seconds is None
    assert budget.deadline is None
    assert not budget.expired
    assert budget.remaining is None
    assert repr(budget) == "SyncBudget(seconds=None)"


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
    with SyncLock(paths) as lock:
        assert lock.held
        assert paths.lock_file.exists()
        assert stat.S_IMODE(paths.lock_file.stat().st_mode) == 0o600
    assert not lock.held
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
    assert threads == [1, 2, 3]


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
    assert {m.msg_id for m in db.get_messages(conn, DISC_ID)} == {1, 2}
    assert db.message_counts(conn)[NEWS_ID] == 3
    resumed = await sync.sync_chat(client, conn, partial.chat, NEWS_SOURCE, SyncBudget())
    assert resumed.complete
    assert resumed.chat.last_msg_id == 3
    assert {m.msg_id for m in db.get_messages(conn, DISC_ID)} == {1, 2, 9}
    threads = [c["reply_to"] for c in _fetch_calls(client, NEWS_ID) if c["reply_to"] is not None]
    assert threads == [1, 2, 3]


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


# --- sync_all --------------------------------------------------------------------------------


async def test_sync_all_resolves_sources_and_runs_hooks(
    conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _news_client(
        messages={
            ARG_ID: [tl.message(ARG_ID, i, f"m{i}", sender=1) for i in (1, 2)],
            NEWS_ID: [tl.channel_post(NEWS_ID, 1, "post 1")],
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


async def test_sync_all_with_no_sources_is_empty(conn: sqlite3.Connection, paths: Paths) -> None:
    report = await _run(_client(), conn, paths, _cfg())
    assert report == sync.SyncReport()


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
