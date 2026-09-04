import random
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from telethon.tl import functions, types
from telethon.tl.types import messages as tl_messages

from grepogram import db, sync, tg, units
from grepogram.models import ChatRow, Config, MessageRow, Source, SyncReport, UnitRow, UnitsCfg
from grepogram.paths import Paths
from grepogram.sync import SyncBudget
from grepogram.units import UnitDelta
from tests.fakes import FakeClient, make_channel, make_dialog, make_folder, make_user
from tests.fixtures import tl

BASE = 1_705_314_600  # 2024-01-15 10:30:00 UTC
CHAT = -1000000000100
CHANNEL = -1000000000200
DISC = -1000000000300
UNITS = UnitsCfg(window_gap_min=30, window_max_msgs=5, window_max_chars=400, thread_max_msgs=4)
FOLDER = Source(folder="Argentina")
NEWS = Source(chat="@news", comments=True)
CFG = Config(units=UNITS, sources=[FOLDER, NEWS])

Shape = tuple[object, ...]


def _msg(
    msg_id: int,
    minutes: int = 0,
    reply_to: int | None = None,
    text: str | None = None,
    chat_id: int = CHAT,
    **overrides: object,
) -> MessageRow:
    fields: dict[str, object] = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "date": BASE + minutes * 60,
        "from_id": 1,
        "from_name": "Alice",
        "reply_to_msg_id": reply_to,
        "text": f"message {msg_id}" if text is None else text,
    }
    fields.update(overrides)
    return MessageRow(**fields)  # type: ignore[arg-type]


def _chat(chat_id: int = CHAT, **overrides: object) -> ChatRow:
    fields: dict[str, object] = {
        "id": chat_id,
        "type": "supergroup",
        "title": f"chat {chat_id}",
        "source_id": FOLDER.id,
    }
    fields.update(overrides)
    return ChatRow(**fields)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def chat(conn: sqlite3.Connection) -> ChatRow:
    return db.upsert_chat(conn, _chat())


def _sync(
    conn: sqlite3.Connection, chat: ChatRow, rows: Iterable[MessageRow], cfg: Config = CFG
) -> UnitDelta:
    """Store ``rows`` (new or edited) and rebuild, as one sync of ``chat`` would."""
    return units.rebuild_for_chat(conn, chat, cfg, db.upsert_messages(conn, rows))


def _shape_of(unit: UnitRow) -> Shape:
    return (
        unit.kind,
        unit.topic_id,
        tuple(unit.msg_ids),
        unit.date_start,
        unit.date_end,
        unit.text,
    )


def _shape(rows: Iterable[UnitRow]) -> list[Shape]:
    return sorted(
        (_shape_of(unit) for unit in rows),
        key=lambda s: (str(s[0]), s[1] is not None, s[1] or 0, s[2]),
    )


def _stored(conn: sqlite3.Connection, chat_id: int = CHAT) -> list[Shape]:
    return _shape(db.get_units(conn, chat_id))


def _expected(conn: sqlite3.Connection, chat: ChatRow, cfg: Config = CFG) -> list[Shape]:
    return _shape(units.units_for_chat(conn, db.get_messages(conn, chat.id), chat, cfg))


def _ids_by_shape(conn: sqlite3.Connection, chat_id: int = CHAT) -> dict[Shape, int]:
    return {_shape_of(unit): unit.id for unit in db.get_units(conn, chat_id) if unit.id}


def _by_msg_ids(
    conn: sqlite3.Connection, kind: str, chat_id: int = CHAT
) -> dict[tuple[int, ...], UnitRow]:
    return {tuple(u.msg_ids): u for u in db.get_units(conn, chat_id) if u.kind == kind}


# --- basics ----------------------------------------------------------------------------------


def test_rebuild_with_no_ids_or_foreign_ids_is_a_noop(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    assert units.rebuild_for_chat(conn, chat, CFG, []) == UnitDelta()
    other = db.upsert_chat(conn, _chat(-1000000000101))
    ids = db.upsert_messages(conn, [_msg(1, chat_id=other.id), _msg(2, 1, chat_id=other.id)])
    assert units.rebuild_for_chat(conn, chat, CFG, ids) == UnitDelta()
    assert db.get_units(conn, chat.id) == []
    assert units.rebuild_for_chat(conn, chat, CFG, [10_000]) == UnitDelta()
    assert not conn.in_transaction


def test_first_rebuild_inserts_windows_and_threads(conn: sqlite3.Connection, chat: ChatRow) -> None:
    delta = _sync(conn, chat, [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 60)])
    stored = db.get_units(conn, chat.id)
    assert sorted(delta.inserted_ids) == sorted(u.id for u in stored if u.id)
    assert delta.deleted_ids == []
    assert [(u.kind, u.msg_ids) for u in stored] == [
        ("window", [1, 2]),
        ("window", [3]),
        ("thread", [1, 2]),
    ]
    assert all(u.dirty and u.embedded_model is None for u in stored)
    assert _stored(conn) == _expected(conn, chat)


# --- windows ---------------------------------------------------------------------------------


def test_new_message_extends_the_open_window(conn: sqlite3.Connection, chat: ChatRow) -> None:
    first = _sync(conn, chat, [_msg(1, 0), _msg(2, 1)])
    (old_id,) = first.inserted_ids
    second = _sync(conn, chat, [_msg(3, 3)])
    assert second.deleted_ids == [old_id]
    assert len(second.inserted_ids) == 1
    (window,) = db.get_units(conn, chat.id)
    assert window.id == second.inserted_ids[0]
    assert window.msg_ids == [1, 2, 3]
    assert window.text == "\n".join(units.render_line(m) for m in db.get_messages(conn, chat.id))
    assert _stored(conn) == _expected(conn, chat)


def test_message_after_a_gap_keeps_the_open_window_row(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    first = _sync(conn, chat, [_msg(1, 0), _msg(2, 1)])
    second = _sync(conn, chat, [_msg(3, 45)])
    assert second.deleted_ids == []
    assert len(second.inserted_ids) == 1
    windows = _by_msg_ids(conn, "window")
    assert windows[(1, 2)].id == first.inserted_ids[0]
    assert windows[(3,)].id == second.inserted_ids[0]
    assert _stored(conn) == _expected(conn, chat)


def test_window_limits_apply_across_syncs(conn: sqlite3.Connection, chat: ChatRow) -> None:
    _sync(conn, chat, [_msg(i, i) for i in range(1, 5)])
    _sync(conn, chat, [_msg(i, i) for i in range(5, 8)])
    assert [u.msg_ids for u in db.get_units(conn, chat.id)] == [[1, 2, 3, 4, 5], [6, 7]]
    assert _stored(conn) == _expected(conn, chat)


def test_edit_inside_the_open_window_updates_its_text(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    first = _sync(conn, chat, [_msg(1, 0), _msg(2, 1)])
    delta = _sync(conn, chat, [_msg(2, 1, text="edited", edit_date=BASE + 600)])
    assert delta.deleted_ids == first.inserted_ids
    (window,) = db.get_units(conn, chat.id)
    assert window.msg_ids == [1, 2]
    assert "Alice: edited" in window.text
    assert "message 2" not in window.text
    assert window.dirty


def test_edit_inside_a_closed_window_is_not_recut_but_its_thread_is(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    _sync(conn, chat, [_msg(1, 0), _msg(2, 1, reply_to=1)])
    _sync(conn, chat, [_msg(3, 60)])
    before = _by_msg_ids(conn, "window")
    old_thread = _by_msg_ids(conn, "thread")[(1, 2)]
    delta = _sync(conn, chat, [_msg(1, 0, text="edited root", edit_date=BASE + 900)])
    after = _by_msg_ids(conn, "window")
    assert after[(1, 2)] == before[(1, 2)]
    assert "message 1" in after[(1, 2)].text
    assert after[(3,)] == before[(3,)]
    assert delta.deleted_ids == [old_thread.id]
    (new_thread,) = delta.inserted_ids
    thread = _by_msg_ids(conn, "thread")[(1, 2)]
    assert thread.id == new_thread
    assert thread.dirty
    assert thread.text.startswith("[2024-01-15 10:30] Alice: edited root")


def test_edit_that_leaves_unit_text_unchanged_keeps_every_row(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    first = _sync(conn, chat, [_msg(1, 0), _msg(2, 1, reply_to=1)])
    ids = _ids_by_shape(conn)
    delta = _sync(conn, chat, [_msg(2, 1, reply_to=1, reactions_total=5)])
    assert delta == UnitDelta()
    assert _ids_by_shape(conn) == ids
    assert sorted(ids.values()) == sorted(first.inserted_ids)


def test_forum_topics_are_recut_independently(conn: sqlite3.Connection) -> None:
    forum = db.upsert_chat(conn, _chat(is_forum=True))
    _sync(conn, forum, [_msg(1, 0, topic_id=5), _msg(2, 1, topic_id=8), _msg(3, 2)])
    ids = _ids_by_shape(conn)
    delta = _sync(conn, forum, [_msg(4, 3, topic_id=8)])
    windows = _by_msg_ids(conn, "window")
    assert {k: u.topic_id for k, u in windows.items()} == {(1,): 5, (2, 4): 8, (3,): None}
    assert windows[(1,)].id == ids[_shape_of(windows[(1,)])]
    assert windows[(3,)].id == ids[_shape_of(windows[(3,)])]
    assert len(delta.deleted_ids) == 1 and len(delta.inserted_ids) == 1
    assert _stored(conn) == _expected(conn, forum)


def test_general_topic_messages_never_join_a_numbered_topic(conn: sqlite3.Connection) -> None:
    forum = db.upsert_chat(conn, _chat(is_forum=True))
    _sync(conn, forum, [_msg(1, 0, topic_id=5), _msg(2, 1)])
    _sync(conn, forum, [_msg(3, 2), _msg(4, 3, topic_id=5)])
    windows = _by_msg_ids(conn, "window")
    assert {k: u.topic_id for k, u in windows.items()} == {(1, 4): 5, (2, 3): None}


def test_messages_without_a_window_are_covered_on_the_next_rebuild(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    db.upsert_messages(conn, [_msg(1, 0), _msg(2, 1)])
    _sync(conn, chat, [_msg(3, 2)])
    assert [u.msg_ids for u in db.get_units(conn, chat.id)] == [[1, 2, 3]]


# --- threads ---------------------------------------------------------------------------------


def test_reply_to_a_deep_descendant_rebuilds_the_whole_thread(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    _sync(conn, chat, [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 2, reply_to=2)])
    old = _by_msg_ids(conn, "thread")[(1, 2, 3)]
    delta = _sync(conn, chat, [_msg(4, 3, reply_to=3)])
    assert old.id in delta.deleted_ids
    threads = _by_msg_ids(conn, "thread")
    assert list(threads) == [(1, 2, 3, 4)]
    assert threads[(1, 2, 3, 4)].id in delta.inserted_ids
    assert _stored(conn) == _expected(conn, chat)


def test_first_reply_turns_a_message_into_a_root(conn: sqlite3.Connection, chat: ChatRow) -> None:
    _sync(conn, chat, [_msg(1, 0), _msg(2, 1)])
    assert _by_msg_ids(conn, "thread") == {}
    _sync(conn, chat, [_msg(3, 2, reply_to=2)])
    assert list(_by_msg_ids(conn, "thread")) == [(2, 3)]


def test_reply_to_a_missing_parent_heads_a_thread_once_replied_to(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    _sync(conn, chat, [_msg(5, 0, reply_to=99)])
    assert _by_msg_ids(conn, "thread") == {}
    _sync(conn, chat, [_msg(6, 1, reply_to=5)])
    assert list(_by_msg_ids(conn, "thread")) == [(5, 6)]


def test_thread_continuation_pieces_keep_unchanged_ones(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    _sync(conn, chat, [_msg(1, 0), *(_msg(i, i, reply_to=1) for i in range(2, 7))])
    pieces = _by_msg_ids(conn, "thread")
    assert list(pieces) == [(1, 2, 3, 4), (1, 5, 6)]
    first_id = pieces[(1, 2, 3, 4)].id
    delta = _sync(conn, chat, [_msg(7, 7, reply_to=6)])
    pieces = _by_msg_ids(conn, "thread")
    assert list(pieces) == [(1, 2, 3, 4), (1, 5, 6, 7)]
    assert pieces[(1, 2, 3, 4)].id == first_id
    assert first_id not in delta.deleted_ids
    assert _stored(conn) == _expected(conn, chat)


def test_reply_cycles_and_self_replies_form_no_thread(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    delta = _sync(
        conn, chat, [_msg(1, 0, reply_to=2), _msg(2, 1, reply_to=1), _msg(3, 2, reply_to=3)]
    )
    assert len(delta.inserted_ids) == 1
    assert [u.kind for u in db.get_units(conn, chat.id)] == ["window"]
    _sync(conn, chat, [_msg(4, 3, reply_to=2)])
    assert [u.kind for u in db.get_units(conn, chat.id)] == ["window"]


def test_changed_messages_of_one_thread_rebuild_it_once(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    _sync(conn, chat, [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 2, reply_to=1)])
    _sync(conn, chat, [_msg(4, 3, reply_to=2), _msg(5, 4, reply_to=3), _msg(6, 5, reply_to=1)])
    threads = [u for u in db.get_units(conn, chat.id) if u.kind == "thread"]
    assert [u.msg_ids for u in threads] == [[1, 2, 3, 4], [1, 5, 6]]


def test_rebuild_touches_only_threads_of_changed_messages(
    conn: sqlite3.Connection, chat: ChatRow
) -> None:
    _sync(conn, chat, [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 2), _msg(4, 3, reply_to=3)])
    untouched = _by_msg_ids(conn, "thread")[(1, 2)]
    delta = _sync(conn, chat, [_msg(5, 4, reply_to=4)])
    assert untouched.id not in delta.deleted_ids
    assert _by_msg_ids(conn, "thread")[(1, 2)] == untouched


# --- property: chunked syncs equal one pass --------------------------------------------------


def _history(n: int, seed: int, topics: Sequence[int | None] = (None,)) -> list[MessageRow]:
    """Pseudo-random chat: bursts and pauses, reply chains, a few replies to unstored messages."""
    rng = random.Random(seed)
    minute = 0
    earlier: dict[int | None, list[int]] = {}
    rows: list[MessageRow] = []
    for msg_id in range(1, n + 1):
        minute += rng.choice([1, 1, 1, 2, 4, 40, 90])
        topic = rng.choice(list(topics))
        seen = earlier.setdefault(topic, [])
        roll = rng.random()
        reply_to: int | None = None
        if seen and roll < 0.45:
            reply_to = rng.choice(seen[-8:])
        elif roll < 0.5:
            reply_to = 900 + msg_id
        text = f"m{msg_id} " + "word " * rng.randint(1, 40)
        rows.append(_msg(msg_id, minute, reply_to=reply_to, text=text, topic_id=topic))
        seen.append(msg_id)
    return rows


def _sync_in_chunks(
    conn: sqlite3.Connection, chat: ChatRow, rows: Sequence[MessageRow], sizes: Iterable[int]
) -> None:
    start = 0
    for size in sizes:
        if start >= len(rows):
            break
        _sync(conn, chat, rows[start : start + size])
        start += size
    if start < len(rows):
        _sync(conn, chat, rows[start:])


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize(
    "sizes",
    [[60], [60, 60], [40, 40, 40], [1] * 120, [3, 17, 1, 50, 9, 2], [119, 1], [1, 119]],
    ids=lambda s: "+".join(map(str, s)) if len(s) < 8 else f"{len(s)}x{s[0]}",
)
def test_chunked_syncs_match_one_pass(
    conn: sqlite3.Connection, chat: ChatRow, seed: int, sizes: list[int]
) -> None:
    rows = _history(120, seed)
    _sync_in_chunks(conn, chat, rows, sizes)
    assert _stored(conn) == _expected(conn, chat)
    assert len(db.get_units(conn, chat.id)) == len(_expected(conn, chat))


@pytest.mark.parametrize("sizes", [[80], [40, 40], [1] * 80])
def test_chunked_syncs_match_one_pass_in_a_forum(
    conn: sqlite3.Connection, sizes: list[int]
) -> None:
    forum = db.upsert_chat(conn, _chat(is_forum=True))
    rows = _history(80, 7, topics=(None, 5, 8))
    _sync_in_chunks(conn, forum, rows, sizes)
    assert _stored(conn) == _expected(conn, forum)


def test_chunked_syncs_match_a_one_pass_rebuild(conn: sqlite3.Connection, chat: ChatRow) -> None:
    rows = _history(90, 11)
    _sync_in_chunks(conn, chat, rows, [30, 30, 30])
    fresh = db.connect(":memory:")
    db.migrate(fresh)
    _sync(fresh, db.upsert_chat(fresh, _chat()), rows)
    assert _stored(conn) == _stored(fresh)
    fresh.close()


# --- channels --------------------------------------------------------------------------------


def _post(msg_id: int, minutes: int = 0, text: str | None = None, **overrides: Any) -> MessageRow:
    return _msg(
        msg_id,
        minutes,
        chat_id=CHANNEL,
        from_id=CHANNEL,
        from_name="News",
        text=f"post {msg_id}" if text is None else text,
        **overrides,
    )


def _comment(msg_id: int, post_id: int, minutes: int, reply_to: int = 7) -> MessageRow:
    return _msg(
        msg_id,
        minutes,
        reply_to=reply_to,
        chat_id=DISC,
        topic_id=post_id,
        from_name="Bob",
        text=f"comment {msg_id} on {post_id}",
    )


def _channel(conn: sqlite3.Connection, source_id: str = NEWS.id) -> ChatRow:
    return db.upsert_chat(
        conn, _chat(CHANNEL, type="channel", title="News", username="news", source_id=source_id)
    )


def _discussion(conn: sqlite3.Connection) -> ChatRow:
    return db.upsert_chat(conn, _chat(DISC, title="News chat", discussion_of=CHANNEL))


def test_channel_posts_are_added_per_new_message(conn: sqlite3.Connection) -> None:
    channel = _channel(conn, FOLDER.id)
    first = _sync(conn, channel, [_post(1), _post(2, 1)])
    assert [(u.kind, u.msg_ids) for u in db.get_units(conn, CHANNEL)] == [
        ("post", [1]),
        ("post", [2]),
    ]
    second = _sync(conn, channel, [_post(3, 2)])
    assert second.deleted_ids == []
    posts = _by_msg_ids(conn, "post", CHANNEL)
    assert [posts[(1,)].id, posts[(2,)].id] == first.inserted_ids
    assert posts[(3,)].id == second.inserted_ids[0]
    assert _stored(conn, CHANNEL) == _expected(conn, channel)


def test_channel_with_comments_builds_post_threads_and_discussion_windows(
    conn: sqlite3.Connection,
) -> None:
    channel = _channel(conn)
    discussion = _discussion(conn)
    comment_ids = db.upsert_messages(
        conn, [_comment(1, 10, 5), _comment(2, 10, 6, reply_to=1), _comment(3, 12, 8)]
    )
    _sync(conn, channel, [_post(10), _post(11, 1), _post(12, 2)])
    assert [(u.kind, u.msg_ids) for u in db.get_units(conn, CHANNEL)] == [
        ("post", [10]),
        ("post", [11]),
        ("post", [12]),
        ("thread", [10]),
        ("thread", [12]),
    ]
    thread = _by_msg_ids(conn, "thread", CHANNEL)[(10,)]
    assert thread.text.splitlines()[1:] == [
        units.render_line(m) for m in db.get_messages_in_topic(conn, DISC, 10)
    ]
    assert _stored(conn, CHANNEL) == _expected(conn, channel)
    units.rebuild_for_chat(conn, discussion, CFG, comment_ids)
    assert [(u.kind, u.topic_id, u.msg_ids) for u in db.get_units(conn, DISC)] == [
        ("window", None, [1, 2, 3]),
        ("thread", 10, [1, 2]),
    ]
    assert _stored(conn, DISC) == _expected(conn, discussion)


def test_edited_post_rebuilds_its_post_and_thread_only(conn: sqlite3.Connection) -> None:
    channel = _channel(conn)
    _discussion(conn)
    db.upsert_messages(conn, [_comment(1, 10, 5), _comment(2, 11, 6)])
    _sync(conn, channel, [_post(10), _post(11, 1)])
    before = _ids_by_shape(conn, CHANNEL)
    delta = _sync(conn, channel, [_post(10, text="post 10 (edited)", edit_date=BASE + 60)])
    after = _ids_by_shape(conn, CHANNEL)
    assert len(delta.deleted_ids) == 2 and len(delta.inserted_ids) == 2
    kept = {shape: unit_id for shape, unit_id in before.items() if shape[2] == (11,)}
    assert kept and all(after[shape] == unit_id for shape, unit_id in kept.items())
    assert all("post 10 (edited)" in str(shape[5]) for shape in after if shape[2] == (10,))
    assert _stored(conn, CHANNEL) == _expected(conn, channel)


# --- atomicity -------------------------------------------------------------------------------


def test_rebuild_rolls_back_as_a_whole(
    conn: sqlite3.Connection, chat: ChatRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sync(conn, chat, [_msg(1, 0), _msg(2, 1)])
    before = db.get_units(conn, chat.id)
    ids = db.upsert_messages(conn, [_msg(3, 2)])

    def explode(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(db, "delete_units", explode)
    with pytest.raises(RuntimeError, match="boom"):
        units.rebuild_for_chat(conn, chat, CFG, ids)
    assert not conn.in_transaction
    assert db.get_units(conn, chat.id) == before


# --- end to end through sync_all -------------------------------------------------------------


ALICE = make_user(1, "Alice", "Liddell", username="alice")
BOB = make_user(2, "Bob")
ARG = make_channel(100, "Argentina chat", username="arg_chat", megagroup=True)
NEWS_TG = make_channel(200, "News", username="news")
DISC_TG = make_channel(201, "News chat", megagroup=True)
ARG_ID = -1000000000100
NEWS_ID = -1000000000200
DISC_ID = -1000000000201


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths.under(tmp_path / "home")


def _client(**kwargs: object) -> FakeClient:
    dialogs = [make_dialog(ALICE), make_dialog(BOB), make_dialog(ARG), make_dialog(NEWS_TG)]
    kwargs.setdefault("folders", [make_folder(3, "Argentina", include=[ARG])])
    kwargs.setdefault("entities", [DISC_TG])
    kwargs.setdefault("me", make_user(42, "Me"))
    return FakeClient(dialogs=dialogs, **kwargs)  # type: ignore[arg-type]


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
    return tl_messages.ChatFull(full_chat=full, chats=[NEWS_TG, DISC_TG], users=[])


async def _run(
    client: FakeClient, conn: sqlite3.Connection, paths: Paths, cfg: Config
) -> SyncReport:
    async with tg.connected(client):
        return await sync.sync_all(client, conn, cfg, paths, SyncBudget())


def test_on_chat_synced_rebuilds_units(conn: sqlite3.Connection, chat: ChatRow) -> None:
    ids = db.upsert_messages(conn, [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 60)])
    sync.on_chat_synced(conn, chat, CFG, ids)
    assert [(u.kind, u.msg_ids) for u in db.get_units(conn, chat.id)] == [
        ("window", [1, 2]),
        ("window", [3]),
        ("thread", [1, 2]),
    ]
    assert _stored(conn, chat.id) == _expected(conn, chat)


async def test_sync_all_produces_window_and_thread_units(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(
        messages={
            ARG_ID: [
                tl.message(ARG_ID, 1, "where do I open an account?", sender=1),
                tl.message(ARG_ID, 2, "try Galicia", sender=2, reply_to=tl.reply_header(1)),
                tl.message(ARG_ID, 3, "thanks!", sender=1, reply_to=tl.reply_header(2)),
                tl.message(ARG_ID, 4, "anyone around?", sender=2, date=tl.at(200)),
            ]
        }
    )
    cfg = Config(units=UNITS, sources=[FOLDER])
    report = await _run(client, conn, paths, cfg)
    assert report.new == 4 and report.chats_done == [ARG_ID]
    stored = db.get_units(conn, ARG_ID)
    assert [(u.kind, u.msg_ids) for u in stored] == [
        ("window", [1, 2, 3]),
        ("window", [4]),
        ("thread", [1, 2, 3]),
    ]
    assert stored[0].text.splitlines() == [
        "[2025-01-01 00:01] Alice Liddell: where do I open an account?",
        "[2025-01-01 00:02] Bob: try Galicia",
        "[2025-01-01 00:03] Alice Liddell: thanks!",
    ]
    chat = db.get_chat(conn, ARG_ID)
    assert chat is not None
    assert _stored(conn, ARG_ID) == _expected(conn, chat, cfg)
    thread_id = stored[2].id
    client.messages[ARG_ID].append(
        tl.message(ARG_ID, 5, "yes, still here", sender=1, date=tl.at(202))
    )
    report = await _run(client, conn, paths, cfg)
    assert report.new == 1
    stored = db.get_units(conn, ARG_ID)
    assert [(u.kind, u.msg_ids) for u in stored] == [
        ("window", [1, 2, 3]),
        ("thread", [1, 2, 3]),
        ("window", [4, 5]),
    ]
    assert stored[1].id == thread_id
    assert _stored(conn, ARG_ID) == _expected(conn, chat, cfg)


async def test_sync_all_builds_channel_posts_threads_and_discussion_units(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _client(
        messages={NEWS_ID: [tl.channel_post(NEWS_ID, i, f"post {i}") for i in (1, 2)]},
        comments={
            (NEWS_ID, 1): [
                tl.message(DISC_ID, 1, "comment one", sender=1, reply_to=tl.reply_header(7)),
                tl.message(DISC_ID, 2, "reply", sender=2, reply_to=tl.reply_header(1)),
            ]
        },
        responses={functions.channels.GetFullChannelRequest: _full_channel(201)},
    )
    cfg = Config(units=UNITS, sources=[NEWS])
    report = await _run(client, conn, paths, cfg)
    assert report.new == 4
    assert [(u.kind, u.msg_ids) for u in db.get_units(conn, NEWS_ID)] == [
        ("post", [1]),
        ("post", [2]),
        ("thread", [1]),
    ]
    thread = _by_msg_ids(conn, "thread", NEWS_ID)[(1,)]
    assert thread.text.splitlines() == [
        "[2025-01-01 00:01] News: post 1",
        "[2025-01-01 00:01] Alice Liddell: comment one",
        "[2025-01-01 00:02] Bob: reply",
    ]
    assert [(u.kind, u.topic_id, u.msg_ids) for u in db.get_units(conn, DISC_ID)] == [
        ("window", None, [1, 2]),
        ("thread", 1, [1, 2]),
    ]
    for chat_id in (NEWS_ID, DISC_ID):
        chat = db.get_chat(conn, chat_id)
        assert chat is not None
        assert _stored(conn, chat_id) == _expected(conn, chat, cfg)
