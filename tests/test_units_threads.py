import dataclasses
import random
import sqlite3

import pytest

from grepogram import db, units
from grepogram.models import ChatRow, Config, MessageRow, Source, UnitRow, UnitsCfg

BASE = 1_705_314_600  # 2024-01-15 10:30:00 UTC
CHAT = -1000000000100
CHANNEL = -1000000000200
DISC = -1000000000300
CFG = UnitsCfg(window_gap_min=30, window_max_msgs=30, window_max_chars=1500, thread_max_msgs=40)
CAP3 = UnitsCfg(thread_max_msgs=3)
NEWS_SOURCE = Source(chat="@news", comments=True)
FOLDER_SOURCE = Source(folder="Argentina")


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


def _chat(chat_id: int, **overrides: object) -> ChatRow:
    fields: dict[str, object] = {
        "id": chat_id,
        "type": "supergroup",
        "title": f"chat {chat_id}",
        "source_id": FOLDER_SOURCE.id,
    }
    fields.update(overrides)
    return ChatRow(**fields)  # type: ignore[arg-type]


def _threads(messages: list[MessageRow], cfg: UnitsCfg = CFG, **kw: object) -> list[UnitRow]:
    return units.build_threads(messages, cfg, CHAT, **kw)  # type: ignore[arg-type]


def _ids(rows: list[UnitRow]) -> list[list[int]]:
    return [unit.msg_ids for unit in rows]


def _lines(messages: list[MessageRow]) -> str:
    return "\n".join(units.render_line(msg) for msg in messages)


# --- build_threads ---------------------------------------------------------------------------


def test_build_threads_empty_and_no_replies() -> None:
    assert _threads([]) == []
    assert _threads([_msg(1), _msg(2, 1), _msg(3, 2)]) == []


def test_build_threads_linear_chain() -> None:
    chain = [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 2, reply_to=2), _msg(4, 3, reply_to=3)]
    (thread,) = _threads(chain)
    assert thread == UnitRow(
        chat_id=CHAT,
        topic_id=None,
        kind="thread",
        msg_id_start=1,
        msg_id_end=4,
        msg_ids=[1, 2, 3, 4],
        date_start=BASE,
        date_end=BASE + 180,
        text=_lines(chain),
    )
    assert thread.dirty is True
    assert thread.id is None


def test_build_threads_branching_is_chronological_not_depth_first() -> None:
    messages = [
        _msg(1, 0),
        _msg(2, 1, reply_to=1),
        _msg(3, 2, reply_to=1),
        _msg(4, 3, reply_to=2),
        _msg(5, 4, reply_to=3),
        _msg(6, 5, reply_to=2),
    ]
    (thread,) = _threads(messages)
    assert thread.msg_ids == [1, 2, 3, 4, 5, 6]
    assert thread.text == _lines(messages)


def test_build_threads_reply_to_missing_message_is_a_root() -> None:
    orphan_root, reply, lone_orphan = (
        _msg(5, 0, reply_to=99),
        _msg(6, 1, reply_to=5),
        _msg(7, 2, reply_to=98),
    )
    assert _ids(_threads([orphan_root, reply, lone_orphan])) == [[5, 6]]


def test_build_threads_nested_reply_target_is_not_a_root() -> None:
    messages = [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 2, reply_to=2)]
    assert _ids(_threads(messages)) == [[1, 2, 3]]


def test_build_threads_separate_threads_in_root_date_order() -> None:
    messages = [
        _msg(2, 0),
        _msg(1, 1),
        _msg(3, 2, reply_to=1),
        _msg(4, 3, reply_to=2),
        _msg(5, 4),
    ]
    assert _ids(_threads(messages)) == [[2, 4], [1, 3]]


def test_build_threads_cap_and_continuation() -> None:
    root = _msg(1, 0)
    replies = [_msg(i, i, reply_to=1) for i in range(2, 7)]
    threads = _threads([root, *replies], CAP3)
    assert _ids(threads) == [[1, 2, 3], [1, 4, 5], [1, 6]]
    assert all(unit.kind == "thread" and unit.msg_ids[0] == 1 for unit in threads)
    assert [(u.msg_id_start, u.msg_id_end) for u in threads] == [(1, 3), (1, 5), (1, 6)]
    assert [(u.date_start, u.date_end) for u in threads] == [
        (BASE, BASE + 180),
        (BASE, BASE + 300),
        (BASE, BASE + 360),
    ]
    root_line = units.render_line(root)
    assert all(unit.text.startswith(root_line + "\n") for unit in threads)
    assert threads[1].text == _lines([root, replies[2], replies[3]])


def test_build_threads_cap_exact_fit_has_no_empty_continuation() -> None:
    messages = [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 2, reply_to=1)]
    assert _ids(_threads(messages, CAP3)) == [[1, 2, 3]]


def test_build_threads_cap_of_one_still_pairs_root_with_a_reply() -> None:
    messages = [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 2, reply_to=1)]
    assert _ids(_threads(messages, UnitsCfg(thread_max_msgs=1))) == [[1, 2], [1, 3]]


def test_build_threads_ignores_self_reply_and_cycles() -> None:
    assert _threads([_msg(1, 0, reply_to=1), _msg(2, 1)]) == []
    assert _threads([_msg(1, 0, reply_to=2), _msg(2, 1, reply_to=1)]) == []


def test_build_threads_root_stays_first_even_when_dated_later() -> None:
    (thread,) = _threads([_msg(1, 10), _msg(2, 5, reply_to=1)])
    assert thread.msg_ids == [1, 2]
    assert (thread.date_start, thread.date_end) == (BASE + 300, BASE + 600)


def test_build_threads_stamps_root_topic() -> None:
    messages = [_msg(1, 0, topic_id=5), _msg(2, 1, reply_to=1, topic_id=5), _msg(3, 2, topic_id=8)]
    (thread,) = _threads(messages)
    assert (thread.chat_id, thread.topic_id, thread.msg_ids) == (CHAT, 5, [1, 2])


def test_build_threads_roots_filter() -> None:
    messages = [_msg(1, 0), _msg(2, 1), _msg(3, 2, reply_to=1), _msg(4, 3, reply_to=2)]
    assert _ids(_threads(messages, roots={2})) == [[2, 4]]
    assert _ids(_threads(messages, roots=[1, 2])) == [[1, 3], [2, 4]]
    assert _threads(messages, roots=set()) == []
    assert _threads(messages, roots={3, 999}) == []


def test_build_threads_is_deterministic_under_shuffle() -> None:
    rng = random.Random(3)
    messages = [_msg(1, 0)]
    for msg_id in range(2, 60):
        messages.append(_msg(msg_id, msg_id, reply_to=rng.choice([None, *range(1, msg_id)])))
    expected = _threads(messages, UnitsCfg(thread_max_msgs=7))
    assert expected
    shuffled = list(messages)
    rng.shuffle(shuffled)
    assert _threads(shuffled, UnitsCfg(thread_max_msgs=7)) == expected
    for unit in expected:
        assert 2 <= len(unit.msg_ids) <= 7
        assert unit.msg_ids[1:] == sorted(unit.msg_ids[1:])


# --- build_posts -----------------------------------------------------------------------------


def _channel(source_id: str = NEWS_SOURCE.id) -> ChatRow:
    return _chat(CHANNEL, type="channel", title="News", username="news", source_id=source_id)


def _post(msg_id: int, minutes: int = 0, text: str | None = None) -> MessageRow:
    return _msg(
        msg_id,
        minutes,
        chat_id=CHANNEL,
        from_id=CHANNEL,
        from_name="News",
        text=f"post {msg_id}" if text is None else text,
    )


def _comment(
    msg_id: int, post_id: int, minutes: int, reply_to: int = 7, channel_id: int = CHANNEL
) -> MessageRow:
    return _msg(
        msg_id,
        minutes,
        reply_to=reply_to,
        chat_id=DISC,
        comment_of_chat_id=channel_id,
        comment_of_msg_id=post_id,
        from_name="Bob",
        text=f"comment {msg_id} on {post_id}",
    )


def _with_discussion(conn: sqlite3.Connection, comments: list[MessageRow]) -> None:
    db.upsert_chat(conn, _channel())
    db.upsert_chat(conn, _chat(DISC, title="News chat", discussion_of=CHANNEL))
    db.upsert_messages(conn, comments)


def test_build_posts_without_comments(conn: sqlite3.Connection) -> None:
    posts = [_post(11, 5), _post(10, 0)]
    result = units.build_posts(conn, posts, _channel(), False, CFG)
    assert result == [
        UnitRow(
            chat_id=CHANNEL,
            kind="post",
            msg_id_start=10,
            msg_id_end=10,
            msg_ids=[10],
            date_start=BASE,
            date_end=BASE,
            text="[2024-01-15 10:30] News: post 10",
        ),
        UnitRow(
            chat_id=CHANNEL,
            kind="post",
            msg_id_start=11,
            msg_id_end=11,
            msg_ids=[11],
            date_start=BASE + 300,
            date_end=BASE + 300,
            text="[2024-01-15 10:35] News: post 11",
        ),
    ]
    assert units.build_posts(conn, [], _channel(), False, CFG) == []


def test_build_posts_comments_flag_without_discussion_chat(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _channel())
    result = units.build_posts(conn, [_post(10)], _channel(), True, CFG)
    assert [(u.kind, u.msg_ids) for u in result] == [("post", [10])]


def test_build_posts_with_comments(conn: sqlite3.Connection) -> None:
    comments = [
        _comment(1, 10, 20),
        _comment(2, 10, 30, reply_to=1),
        _comment(3, 12, 40),
        _comment(4, 99, 50),
    ]
    _with_discussion(conn, comments)
    posts = [_post(10, 0), _post(11, 5), _post(12, 10)]
    result = units.build_posts(conn, posts, _channel(), True, CFG)
    assert [(u.kind, u.msg_ids) for u in result] == [
        ("post", [10]),
        ("post", [11]),
        ("post", [12]),
        ("thread", [10]),
        ("thread", [12]),
    ]
    thread = result[3]
    assert thread == UnitRow(
        chat_id=CHANNEL,
        topic_id=None,
        kind="thread",
        msg_id_start=10,
        msg_id_end=10,
        msg_ids=[10],
        date_start=BASE,
        date_end=BASE + 30 * 60,
        text=_lines([posts[0], comments[0], comments[1]]),
    )
    assert thread.text == (
        "[2024-01-15 10:30] News: post 10\n"
        "[2024-01-15 10:50] Bob: comment 1 on 10\n"
        "[2024-01-15 11:00] Bob: comment 2 on 10"
    )
    assert result[4].text == _lines([posts[2], comments[2]])
    assert {u.chat_id for u in result} == {CHANNEL}


def test_build_posts_comments_are_ordered_chronologically(conn: sqlite3.Connection) -> None:
    _with_discussion(conn, [_comment(3, 10, 40), _comment(1, 10, 20), _comment(2, 10, 30)])
    (_, thread) = units.build_posts(conn, [_post(10)], _channel(), True, CFG)
    assert thread.text.split("\n")[1:] == [
        "[2024-01-15 10:50] Bob: comment 1 on 10",
        "[2024-01-15 11:00] Bob: comment 2 on 10",
        "[2024-01-15 11:10] Bob: comment 3 on 10",
    ]


def test_build_posts_comment_threads_are_capped(conn: sqlite3.Connection) -> None:
    _with_discussion(conn, [_comment(i, 10, i) for i in range(1, 6)])
    post = _post(10)
    result = units.build_posts(conn, [post], _channel(), True, CAP3)
    threads = [u for u in result if u.kind == "thread"]
    assert [u.msg_ids for u in threads] == [[10], [10], [10]]
    assert [len(u.text.split("\n")) for u in threads] == [3, 3, 2]
    assert all(u.text.startswith(units.render_line(post) + "\n") for u in threads)
    assert [u.date_end - BASE for u in threads] == [120, 240, 300]


def test_build_posts_reads_only_the_channel_own_discussion(conn: sqlite3.Connection) -> None:
    other_channel, other_disc = CHANNEL - 1, DISC - 1
    _with_discussion(conn, [_comment(1, 10, 20)])
    db.upsert_chat(conn, _chat(other_channel, type="channel", title="Other"))
    db.upsert_chat(conn, _chat(other_disc, title="Other chat", discussion_of=other_channel))
    db.upsert_messages(
        conn,
        [
            _msg(
                5,
                1,
                chat_id=other_disc,
                comment_of_chat_id=other_channel,
                comment_of_msg_id=10,
                text="foreign",
            )
        ],
    )
    result = units.build_posts(conn, [_post(10)], _channel(), True, CFG)
    assert [(u.kind, u.msg_ids) for u in result] == [("post", [10]), ("thread", [10])]
    assert "foreign" not in result[1].text
    other = units.build_posts(
        conn, [_post(10)], db.get_chat(conn, other_channel) or _channel(), True, CFG
    )
    assert other[1].text.endswith("foreign")


def test_build_posts_ignores_a_forum_topic_numbered_like_a_post(conn: sqlite3.Connection) -> None:
    """The discussion group is a forum too. Its topic 10 and the channel's post 10 are the same
    number out of two id spaces, and only the rows that say which channel they comment on are
    the post's comments."""
    _with_discussion(conn, [_comment(1, 10, 20)])
    db.upsert_chat(conn, _chat(DISC, title="News chat", is_forum=True, discussion_of=CHANNEL))
    db.upsert_messages(
        conn,
        [
            _msg(20, 1, chat_id=DISC, topic_id=10, text="in topic ten"),
            _msg(21, 2, chat_id=DISC, topic_id=10, text="still in topic ten"),
        ],
    )
    (thread,) = [
        u for u in units.build_posts(conn, [_post(10)], _channel(), True, CFG) if u.kind == "thread"
    ]
    assert [line.split(": ", 1)[1] for line in thread.text.splitlines()[1:]] == ["comment 1 on 10"]


def test_build_posts_ignores_a_comment_left_by_a_channel_that_lost_the_group(
    conn: sqlite3.Connection,
) -> None:
    """A group can hold comments of a channel this index no longer links — one whose post rows
    are gone as well. They belong to that channel's id space, so this channel's post of the same
    number is none the wiser."""
    _with_discussion(conn, [_comment(1, 10, 20), _comment(2, 10, 25, channel_id=CHANNEL - 1)])
    (thread,) = [
        u for u in units.build_posts(conn, [_post(10)], _channel(), True, CFG) if u.kind == "thread"
    ]
    assert [line.split(": ", 1)[1] for line in thread.text.splitlines()[1:]] == ["comment 1 on 10"]


# --- units_for_chat --------------------------------------------------------------------------


def _config(*sources: Source, units_cfg: UnitsCfg = CFG) -> Config:
    return Config(units=units_cfg, sources=list(sources))


def test_units_for_chat_group_gets_windows_then_threads(conn: sqlite3.Connection) -> None:
    messages = [_msg(1, 0), _msg(2, 1, reply_to=1), _msg(3, 60)]
    result = units.units_for_chat(conn, messages, _chat(CHAT), _config(FOLDER_SOURCE))
    assert [(u.kind, u.topic_id, u.msg_ids) for u in result] == [
        ("window", None, [1, 2]),
        ("window", None, [3]),
        ("thread", None, [1, 2]),
    ]
    assert {u.chat_id for u in result} == {CHAT}


@pytest.mark.parametrize("chat_type", ["user", "bot", "group", "supergroup"])
def test_units_for_chat_non_channel_types_get_windows(
    conn: sqlite3.Connection, chat_type: str
) -> None:
    chat = _chat(CHAT, type=chat_type)
    result = units.units_for_chat(conn, [_msg(1)], chat, _config(FOLDER_SOURCE))
    assert [(u.kind, u.msg_ids) for u in result] == [("window", [1])]


def test_units_for_chat_forum_windows_per_topic_and_thread_topic(conn: sqlite3.Connection) -> None:
    messages = [
        _msg(1, 0, topic_id=5),
        _msg(2, 1, topic_id=8),
        _msg(3, 2, reply_to=1, topic_id=5),
        _msg(4, 3, topic_id=8),
    ]
    chat = _chat(CHAT, is_forum=True)
    result = units.units_for_chat(conn, messages, chat, _config(FOLDER_SOURCE))
    assert [(u.kind, u.topic_id, u.msg_ids) for u in result] == [
        ("window", 5, [1, 3]),
        ("window", 8, [2, 4]),
        ("thread", 5, [1, 3]),
    ]


def test_units_for_chat_channel_posts_only_from_folder_source(conn: sqlite3.Connection) -> None:
    _with_discussion(conn, [_comment(1, 10, 20)])
    channel = _channel(FOLDER_SOURCE.id)
    result = units.units_for_chat(conn, [_post(10)], channel, _config(FOLDER_SOURCE, NEWS_SOURCE))
    assert [(u.kind, u.msg_ids) for u in result] == [("post", [10])]


def test_units_for_chat_channel_with_comments_source(conn: sqlite3.Connection) -> None:
    _with_discussion(conn, [_comment(1, 10, 20)])
    result = units.units_for_chat(conn, [_post(10), _post(11, 5)], _channel(), _config(NEWS_SOURCE))
    assert [(u.kind, u.msg_ids) for u in result] == [
        ("post", [10]),
        ("post", [11]),
        ("thread", [10]),
    ]


def test_units_for_chat_channel_with_comments_off_in_source(conn: sqlite3.Connection) -> None:
    _with_discussion(conn, [_comment(1, 10, 20)])
    quiet = Source(chat="@news", comments=False)
    result = units.units_for_chat(conn, [_post(10)], _channel(quiet.id), _config(quiet))
    assert [(u.kind, u.msg_ids) for u in result] == [("post", [10])]


def test_units_for_chat_discussion_chat_gets_linear_windows_and_threads(
    conn: sqlite3.Connection,
) -> None:
    """The group is one conversation: comments of different posts and the group's own talk share
    its windows and its reply threads, all with no ``topic_id`` — the post a comment hangs under
    is a relation of its own and never a topic of the group."""
    messages = [
        _comment(1, 10, 0),
        _comment(2, 10, 1, reply_to=1),
        _msg(3, 2, chat_id=DISC, text="general chat"),
        _comment(4, 12, 3),
        _comment(5, 12, 60),
    ]
    _with_discussion(conn, messages)
    discussion = db.get_chat(conn, DISC)
    assert discussion is not None and discussion.discussion_of == CHANNEL
    result = units.units_for_chat(conn, messages, discussion, _config(NEWS_SOURCE))
    assert [(u.kind, u.topic_id, u.msg_ids) for u in result] == [
        ("window", None, [1, 2, 3, 4]),
        ("window", None, [5]),
        ("thread", None, [1, 2]),
    ]
    assert {u.chat_id for u in result} == {DISC}
    assert not any(u.kind == "post" for u in result)


def test_comments_enabled_matches_the_chat_source() -> None:
    cfg = _config(FOLDER_SOURCE, NEWS_SOURCE, Source(chat=42, comments=True))
    assert units.comments_enabled(cfg, _channel()) is True
    assert units.comments_enabled(cfg, _channel(FOLDER_SOURCE.id)) is False
    assert units.comments_enabled(cfg, _channel("chat:42")) is True
    assert units.comments_enabled(cfg, _channel("chat:@gone")) is False
    assert units.comments_enabled(Config(), _channel()) is False


def test_build_posts_carry_the_post_own_reaction_total(conn: sqlite3.Connection) -> None:
    """A channel's most-reacted posts are exactly what the ranking bonus is for, and a post
    thread is built by hand rather than through ``_unit``, so it would sit at zero without help.

    The thread's total is the post's and not the chunk's: its ``msg_ids`` list the post alone —
    comment ids live in the discussion group's id space, where no ``json_each`` over
    ``units.msg_ids`` reaches them — so it is the only number
    :func:`grepogram.db.refresh_unit_reactions` could ever recompute for it. The comments' own
    reactions are carried by the group's window units.
    """
    _with_discussion(conn, [_comment(1, 10, 20), _comment(2, 10, 25)])
    conn.execute("UPDATE messages SET reactions_total = 6 WHERE chat_id = ?", (DISC,))
    post = dataclasses.replace(_post(10), reactions_total=9)
    result = units.build_posts(conn, [post], _channel(), True, CFG)
    assert [(u.kind, u.msg_ids, u.reactions) for u in result] == [
        ("post", [10], 9),
        ("thread", [10], 9),
    ]
    plain = units.build_posts(conn, [_post(11)], _channel(), True, CFG)
    assert [(u.kind, u.reactions) for u in plain] == [("post", 0)]
