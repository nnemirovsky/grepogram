import pytest

from grepogram import links
from grepogram.models import ChatRow, ChatType, Link

SUPERGROUP = -1001234567890
CHANNEL = -1009876543210
SHORT = -1000123456789  # a channel whose bare id is shorter than ten digits: 123456789
GROUP = -4567
USER = 777000
MSG = 42
TOPIC = 7


def _chat(
    chat_id: int, type_: ChatType, username: str | None = None, forum: bool = False
) -> ChatRow:
    return ChatRow(id=chat_id, type=type_, title="t", username=username, is_forum=forum)


# --- strip_channel_prefix --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chat_id", "expected"),
    [
        (SUPERGROUP, 1234567890),
        (CHANNEL, 9876543210),
        pytest.param(-1009999999999, 9999999999, id="bare-id-at-telethon-max"),
        pytest.param(SHORT, 123456789, id="bare-id-of-nine-digits"),
        pytest.param(-1000000001234, 1234, id="bare-id-of-four-digits"),
        pytest.param(-1000000000001, 1, id="bare-id-of-one-digit"),
    ],
)
def test_strip_channel_prefix(chat_id: int, expected: int) -> None:
    """The mark is arithmetic, so every zero between the ``-100`` and the bare id belongs to the
    ``1000000000000`` that was added — a bare id shorter than ten digits is not a malformed one.
    """
    assert links.strip_channel_prefix(chat_id) == expected


@pytest.mark.parametrize(
    "chat_id",
    [
        USER,
        0,
        -1234,
        GROUP,
        -100,
        -1000123,
        pytest.param(-1001234, id="legacy-group-that-looks-marked"),
        pytest.param(-1000000000000, id="the-mark-itself"),
    ],
)
def test_strip_channel_prefix_rejects_non_channel_ids(chat_id: int) -> None:
    """Telethon reads anything down to ``-1000000000000`` as a legacy group; only a marked
    channel gets a ``t.me/c`` link."""
    with pytest.raises(ValueError, match=str(chat_id)):
        links.strip_channel_prefix(chat_id)


# --- message_url -----------------------------------------------------------------------------


USER_URL = "tg://openmessage?user_id=777000&message_id=42"
USER_FALLBACK = "tg://user?id=777000"
GROUP_URL = "tg://openmessage?chat_id=4567&message_id=42"


@pytest.mark.parametrize(
    ("chat", "topic_id", "expected"),
    [
        pytest.param(
            _chat(SUPERGROUP, "supergroup", "ru_georgia"),
            None,
            Link("https://t.me/ru_georgia/42"),
            id="supergroup-public",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", "ru_georgia", forum=True),
            TOPIC,
            Link("https://t.me/ru_georgia/7/42"),
            id="supergroup-public-forum-topic",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", "ru_georgia", forum=True),
            None,
            Link("https://t.me/ru_georgia/42"),
            id="supergroup-public-forum-general",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", "ru_georgia"),
            TOPIC,
            Link("https://t.me/ru_georgia/42"),
            id="supergroup-public-not-forum-ignores-topic",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup"),
            None,
            Link("https://t.me/c/1234567890/42"),
            id="supergroup-private",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", forum=True),
            TOPIC,
            Link("https://t.me/c/1234567890/7/42"),
            id="supergroup-private-forum-topic",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", forum=True),
            None,
            Link("https://t.me/c/1234567890/42"),
            id="supergroup-private-forum-general",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup"),
            TOPIC,
            Link("https://t.me/c/1234567890/42"),
            id="supergroup-private-not-forum-ignores-topic",
        ),
        pytest.param(
            _chat(SHORT, "supergroup"),
            None,
            Link("https://t.me/c/123456789/42"),
            id="supergroup-private-short-bare-id",
        ),
        pytest.param(
            _chat(CHANNEL, "channel", "durov"),
            None,
            Link("https://t.me/durov/42"),
            id="channel-public",
        ),
        pytest.param(
            _chat(CHANNEL, "channel"),
            None,
            Link("https://t.me/c/9876543210/42"),
            id="channel-private",
        ),
        pytest.param(
            _chat(CHANNEL, "channel", "durov"),
            TOPIC,
            Link("https://t.me/durov/42"),
            id="channel-public-ignores-topic",
        ),
        pytest.param(
            _chat(USER, "user"),
            None,
            Link(USER_URL, USER_FALLBACK),
            id="user",
        ),
        pytest.param(
            _chat(USER, "user", "alice"),
            None,
            Link(USER_URL, USER_FALLBACK),
            id="user-username-ignored",
        ),
        pytest.param(
            _chat(USER, "user"),
            TOPIC,
            Link(USER_URL, USER_FALLBACK),
            id="user-ignores-topic",
        ),
        pytest.param(
            _chat(USER, "bot"),
            None,
            Link(USER_URL, USER_FALLBACK),
            id="bot",
        ),
        pytest.param(
            _chat(USER, "bot", "some_bot"),
            None,
            Link(USER_URL, USER_FALLBACK),
            id="bot-username-ignored",
        ),
        pytest.param(
            _chat(GROUP, "group"),
            None,
            Link(GROUP_URL),
            id="group",
        ),
        pytest.param(
            _chat(GROUP, "group", "legacy"),
            TOPIC,
            Link(GROUP_URL),
            id="group-username-and-topic-ignored",
        ),
    ],
)
def test_message_url(chat: ChatRow, topic_id: int | None, expected: Link) -> None:
    assert links.message_url(chat, MSG, topic_id) == expected


def test_message_url_topic_is_keyword_optional() -> None:
    chat = _chat(SUPERGROUP, "supergroup", "ru_georgia", forum=True)
    assert links.message_url(chat, MSG) == Link("https://t.me/ru_georgia/42")
    assert links.message_url(chat, MSG, topic_id=TOPIC) == Link("https://t.me/ru_georgia/7/42")


def test_message_url_web_links_have_no_fallback_and_a_dm_link_does() -> None:
    """A channel or supergroup link is a ``https://t.me`` url that needs no second form. A
    private chat has no web form at all, so its ``url`` is the ``tg://openmessage`` one the
    mobile apps honour and ``fallback_url`` carries ``tg://user?id=`` for a desktop reader to
    click; a legacy group has no fallback either, only the ``tg://openmessage`` form."""
    for chat in (_chat(SUPERGROUP, "supergroup", "x"), _chat(CHANNEL, "channel")):
        link = links.message_url(chat, MSG)
        assert link.fallback_url is None
        assert link.url.startswith("https://t.me/")
    for chat in (_chat(USER, "user"), _chat(USER, "bot")):
        link = links.message_url(chat, MSG)
        assert link == Link(USER_URL, USER_FALLBACK)
    assert links.message_url(_chat(GROUP, "group"), MSG) == Link(GROUP_URL)


def test_message_url_private_supergroup_with_bad_id_raises() -> None:
    chat = ChatRow(id=-1234, type="supergroup", title="t")
    with pytest.raises(ValueError, match="-1234"):
        links.message_url(chat, MSG)


def test_message_url_unknown_type_raises() -> None:
    chat = ChatRow(id=1, type="secret", title="t")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="secret"):
        links.message_url(chat, MSG)
