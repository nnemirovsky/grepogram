import dataclasses
import json
import sqlite3
from collections.abc import Iterator

import pytest

from grepogram import db, search
from grepogram.models import ChatRow, MessageRow, MessageView
from grepogram.search import UnknownMessage
from tests.fixtures import chat_ru

ARG = chat_ru.ARG_ID
GEO = chat_ru.GEO_ID
CHANNEL = -1001000000300
DISC = -1001000000400
FORUM = -1001000000500
PLAIN = -1001000000600
BASE = 1_705_314_600  # 2024-01-15 10:30:00 UTC


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    db.migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def loaded(conn: sqlite3.Connection) -> chat_ru.Loaded:
    return chat_ru.load(conn)


def _chat(chat_id: int, **overrides: object) -> ChatRow:
    fields: dict[str, object] = {
        "id": chat_id,
        "type": "supergroup",
        "title": f"chat {chat_id}",
        "source_id": "folder:Test",
    }
    fields.update(overrides)
    return ChatRow(**fields)  # type: ignore[arg-type]


def _msg(
    chat_id: int,
    msg_id: int,
    minutes: int = 0,
    reply_to: int | None = None,
    topic_id: int | None = None,
    **overrides: object,
) -> MessageRow:
    fields: dict[str, object] = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "date": BASE + minutes * 60,
        "from_id": 1,
        "from_name": "Alice",
        "reply_to_msg_id": reply_to,
        "topic_id": topic_id,
        "text": f"message {msg_id}",
    }
    fields.update(overrides)
    return MessageRow(**fields)  # type: ignore[arg-type]


def _store(conn: sqlite3.Connection, chat: ChatRow, messages: list[MessageRow]) -> None:
    db.upsert_chat(conn, chat)
    db.upsert_messages(conn, messages)


def _ids(views: list[MessageView]) -> list[int]:
    return [view.msg_id for view in views]


@pytest.fixture
def channel(conn: sqlite3.Connection) -> None:
    """A public channel with two posts and a private discussion chat holding their comments.

    Comment 10 replies to the discussion-side copy of post 1 (id 5, never stored), 11 replies
    to 10; comment 12 belongs to post 2. Post 3 has no comments.
    """
    _store(
        conn,
        _chat(CHANNEL, type="channel", username="news"),
        [_msg(CHANNEL, 1, 0), _msg(CHANNEL, 2, 60), _msg(CHANNEL, 3, 120)],
    )
    _store(
        conn,
        _chat(DISC, discussion_of=CHANNEL),
        [
            _msg(DISC, 10, 5, reply_to=5, topic_id=1),
            _msg(DISC, 11, 7, reply_to=10, topic_id=1),
            _msg(DISC, 12, 65, reply_to=6, topic_id=2),
        ],
    )


@pytest.fixture
def forum(conn: sqlite3.Connection) -> None:
    """A public forum whose two topics interleave by ``msg_id``, plus two General messages."""
    _store(
        conn,
        _chat(FORUM, username="forum_chat", is_forum=True),
        [
            _msg(FORUM, 101, 1, topic_id=100),
            _msg(FORUM, 102, 2, topic_id=200),
            _msg(FORUM, 103, 3, topic_id=100),
            _msg(FORUM, 104, 4, topic_id=200),
            _msg(FORUM, 105, 5, topic_id=100),
            _msg(FORUM, 106, 6, topic_id=200),
            _msg(FORUM, 107, 7, topic_id=100),
            _msg(FORUM, 108, 8, topic_id=200),
            _msg(FORUM, 109, 9),
            _msg(FORUM, 110, 10),
        ],
    )


# --- thread ----------------------------------------------------------------------------------


def test_thread_from_a_leaf_reaches_the_root_and_its_siblings(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    views = search.thread(conn, ARG, 5)  # 5 → 4 → 3 → 1; 2, 6 and 7, 10 hang off the same root
    assert _ids(views) == [1, 2, 3, 4, 5, 6, 7, 10]
    assert views[0].reply_to_msg_id is None
    assert [v.reply_to_msg_id for v in views[1:]] == [1, 1, 3, 4, 1, 6, 3]
    assert views[0].text.startswith("Всем привет!")
    assert views[0].from_name == "Ольга"


def test_thread_is_the_same_from_every_member(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    from_root = search.thread(conn, ARG, 26)
    assert _ids(from_root) == [26, 27, 28, 29]
    assert search.thread(conn, ARG, 28) == from_root
    assert search.thread(conn, ARG, 29) == from_root


def test_thread_of_a_lone_message_is_the_message(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    (view,) = search.thread(conn, ARG, 12)
    assert view.msg_id == 12 and view.reply_to_msg_id is None
    assert view.text == "Вот скрин тарифов Galicia за обслуживание счёта."


def test_thread_views_carry_deep_links(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    public = search.thread(conn, ARG, 13)
    assert [v.url for v in public] == [f"https://t.me/arg_chat/{i}" for i in (13, 14, 15, 19, 20)]
    private = search.thread(conn, GEO, 8)  # 8 → 2 → 1
    assert _ids(private) == [1, 2, 3, 4, 5, 8]
    assert [v.url for v in private] == [f"https://t.me/c/1000000200/{i}" for i in _ids(private)]
    assert all(v.fallback_url is None for v in public + private)


def test_thread_is_chronological_not_id_ordered(conn: sqlite3.Connection) -> None:
    _store(
        conn,
        _chat(PLAIN),
        [_msg(PLAIN, 1, 0), _msg(PLAIN, 2, 10, reply_to=1), _msg(PLAIN, 3, 5, reply_to=1)],
    )
    assert _ids(search.thread(conn, PLAIN, 2)) == [1, 3, 2]


def test_thread_dates_never_decrease(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    for chat_id, msg_id in ((ARG, 5), (ARG, 33), (GEO, 20)):
        dates = [v.date for v in search.thread(conn, chat_id, msg_id)]
        assert dates == sorted(dates)


def test_thread_with_an_unstored_parent_starts_at_the_stored_top(
    conn: sqlite3.Connection,
) -> None:
    _store(
        conn,
        _chat(PLAIN),
        [
            _msg(PLAIN, 2, 1, reply_to=1),
            _msg(PLAIN, 3, 2, reply_to=2),
            _msg(PLAIN, 4, 3, reply_to=1),
        ],
    )
    assert _ids(search.thread(conn, PLAIN, 3)) == [2, 3]
    assert _ids(search.thread(conn, PLAIN, 4)) == [4]


def test_thread_survives_reply_cycles_and_self_replies(conn: sqlite3.Connection) -> None:
    _store(
        conn,
        _chat(PLAIN),
        [
            _msg(PLAIN, 1, 0, reply_to=2),
            _msg(PLAIN, 2, 1, reply_to=1),
            _msg(PLAIN, 3, 2, reply_to=2),
            _msg(PLAIN, 5, 3, reply_to=5),
        ],
    )
    assert _ids(search.thread(conn, PLAIN, 3)) == [1, 2, 3]
    assert set(_ids(search.thread(conn, PLAIN, 1))) == {1, 2, 3}
    assert _ids(search.thread(conn, PLAIN, 5)) == [5]


def test_thread_of_a_channel_post_appends_its_comments(
    conn: sqlite3.Connection, channel: None
) -> None:
    views = search.thread(conn, CHANNEL, 1)
    assert _ids(views) == [1, 10, 11]
    assert views[0].url == "https://t.me/news/1"
    assert [v.url for v in views[1:]] == [
        "https://t.me/c/1000000400/10",
        "https://t.me/c/1000000400/11",
    ]
    assert [v.reply_to_msg_id for v in views] == [None, 5, 10]
    assert _ids(search.thread(conn, CHANNEL, 2)) == [2, 12]
    assert _ids(search.thread(conn, CHANNEL, 3)) == [3]


def test_thread_of_a_channel_without_a_discussion_chat_is_the_post(
    conn: sqlite3.Connection,
) -> None:
    _store(conn, _chat(CHANNEL, type="channel", username="news"), [_msg(CHANNEL, 1, 0)])
    (view,) = search.thread(conn, CHANNEL, 1)
    assert view.msg_id == 1 and view.url == "https://t.me/news/1"


def test_thread_from_a_comment_stays_in_the_discussion_chat(
    conn: sqlite3.Connection, channel: None
) -> None:
    views = search.thread(conn, DISC, 11)
    assert _ids(views) == [10, 11]
    assert all(v.url.startswith("https://t.me/c/1000000400/") for v in views)


def test_thread_in_a_forum_links_through_the_topic(conn: sqlite3.Connection, forum: None) -> None:
    (view,) = search.thread(conn, FORUM, 105)
    assert view.url == "https://t.me/forum_chat/100/105"
    (general,) = search.thread(conn, FORUM, 109)
    assert general.url == "https://t.me/forum_chat/109"


# --- message views ---------------------------------------------------------------------------


def test_view_of_a_media_message_shows_a_placeholder(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    (photo,) = search.thread(conn, ARG, 11)
    assert photo.text == "[photo]" and photo.from_name == "Дима"
    (captioned,) = search.thread(conn, ARG, 12)
    assert captioned.text.startswith("Вот скрин")


def test_view_keeps_the_stored_sender_even_when_unknown(conn: sqlite3.Connection) -> None:
    _store(conn, _chat(PLAIN), [_msg(PLAIN, 1, 0, from_id=None, from_name=None, text="  hi  ")])
    (view,) = search.thread(conn, PLAIN, 1)
    assert view.from_name is None and view.text == "hi"


def test_views_for_a_private_chat_carry_the_fallback(conn: sqlite3.Connection) -> None:
    _store(conn, _chat(42, type="user", title="Bob"), [_msg(42, 7, 0)])
    (view,) = search.context(conn, 42, 7)
    assert view.url == "tg://openmessage?user_id=42&message_id=7"
    assert view.fallback_url == "tg://user?id=42"


def test_views_serialise_to_json(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    document = json.dumps([dataclasses.asdict(v) for v in search.thread(conn, ARG, 5)])
    parsed = json.loads(document)
    assert len(parsed) == 8
    assert set(parsed[0]) == {
        "msg_id",
        "date",
        "from_name",
        "text",
        "url",
        "fallback_url",
        "reply_to_msg_id",
    }


# --- context ---------------------------------------------------------------------------------


def test_context_respects_before_and_after(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    assert _ids(search.context(conn, ARG, 5, before=2, after=2)) == [3, 4, 5, 6, 7]
    assert _ids(search.context(conn, ARG, 5, before=0, after=0)) == [5]
    assert _ids(search.context(conn, ARG, 5, before=1, after=3)) == [4, 5, 6, 7, 8]
    assert _ids(search.context(conn, ARG, 5, before=4, after=0)) == [1, 2, 3, 4, 5]


def test_context_defaults_to_fifteen_each_side(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    assert _ids(search.context(conn, ARG, 20)) == list(range(5, 36))


def test_context_stops_at_the_ends_of_the_chat(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    assert _ids(search.context(conn, ARG, 1, before=3, after=1)) == [1, 2]
    assert _ids(search.context(conn, ARG, 42, before=1, after=3)) == [41, 42]
    assert _ids(search.context(conn, GEO, 1, before=3, after=1)) == [1, 2]


def test_context_ignores_reply_structure(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    views = search.context(conn, ARG, 26, before=1, after=1)
    assert _ids(views) == [25, 26, 27]
    assert [v.reply_to_msg_id for v in views] == [24, None, 26]


def test_context_views_carry_deep_links(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    views = search.context(conn, GEO, 12, before=1, after=1)
    assert [v.url for v in views] == [f"https://t.me/c/1000000200/{i}" for i in (11, 12, 13)]
    assert [v.from_name for v in views] == ["Maria", "Alice", "Дима"]


def test_context_stays_within_the_topic(conn: sqlite3.Connection, forum: None) -> None:
    topic = search.context(conn, FORUM, 105, before=5, after=5)
    assert _ids(topic) == [101, 103, 105, 107]
    assert [v.url for v in topic] == [f"https://t.me/forum_chat/100/{i}" for i in _ids(topic)]
    general = search.context(conn, FORUM, 110, before=5, after=5)
    assert _ids(general) == [109, 110]
    assert [v.url for v in general] == [
        "https://t.me/forum_chat/109",
        "https://t.me/forum_chat/110",
    ]


def test_context_of_a_comment_stays_with_its_post(conn: sqlite3.Connection, channel: None) -> None:
    assert _ids(search.context(conn, DISC, 11, before=5, after=5)) == [10, 11]
    assert _ids(search.context(conn, DISC, 12, before=5, after=5)) == [12]
    assert _ids(search.context(conn, CHANNEL, 2, before=5, after=5)) == [1, 2, 3]


def test_context_rejects_negative_counts(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    with pytest.raises(ValueError, match="negative"):
        search.context(conn, ARG, 5, before=-1)
    with pytest.raises(ValueError, match="negative"):
        db.get_context_messages(conn, ARG, 5, 1, -1)


# --- errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize("chat_id, msg_id", [(ARG, 999), (GEO, 0), (12345, 1)])
def test_unknown_message_raises(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded, chat_id: int, msg_id: int
) -> None:
    with pytest.raises(UnknownMessage) as thread_error:
        search.thread(conn, chat_id, msg_id)
    with pytest.raises(UnknownMessage) as context_error:
        search.context(conn, chat_id, msg_id)
    for error in (thread_error.value, context_error.value):
        assert (error.chat_id, error.msg_id) == (chat_id, msg_id)
        assert str(error) == f"message {msg_id} of chat {chat_id} is not indexed"
        assert isinstance(error, LookupError)


def test_db_readers_return_nothing_for_an_unknown_message(
    conn: sqlite3.Connection, loaded: chat_ru.Loaded
) -> None:
    assert db.get_thread_messages(conn, ARG, 999) == []
    assert db.get_context_messages(conn, ARG, 999, 5, 5) == []
    assert db.get_thread_messages(conn, 12345, 1) == []


def test_db_descendants_exclude_the_root(conn: sqlite3.Connection, loaded: chat_ru.Loaded) -> None:
    assert [m.msg_id for m in db.get_descendants(conn, ARG, 1)] == [2, 3, 6, 4, 7, 10, 5]
    assert db.get_descendants(conn, ARG, 12) == []
    assert db.get_descendants(conn, ARG, 999) == []
