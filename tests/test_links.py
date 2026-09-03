import subprocess
import sys
from collections.abc import Sequence

import pytest

from grepogram import links
from grepogram.models import ChatRow, ChatType, Link

SUPERGROUP = -1001234567890
CHANNEL = -1009876543210
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
        (-1001234, 1234),
        (SUPERGROUP, 1234567890),
        (CHANNEL, 9876543210),
        (-1001000000000100, 1000000000100),
        (-1001, 1),
        (-10012, 12),
    ],
)
def test_strip_channel_prefix(chat_id: int, expected: int) -> None:
    assert links.strip_channel_prefix(chat_id) == expected


@pytest.mark.parametrize("chat_id", [USER, 0, -1234, GROUP, -100, -1000123])
def test_strip_channel_prefix_rejects_non_channel_ids(chat_id: int) -> None:
    with pytest.raises(ValueError, match=str(chat_id)):
        links.strip_channel_prefix(chat_id)


# --- message_url -----------------------------------------------------------------------------


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
            Link("tg://openmessage?user_id=777000&message_id=42", "tg://user?id=777000"),
            id="user",
        ),
        pytest.param(
            _chat(USER, "user", "alice"),
            None,
            Link("tg://openmessage?user_id=777000&message_id=42", "tg://user?id=777000"),
            id="user-username-ignored",
        ),
        pytest.param(
            _chat(USER, "user"),
            TOPIC,
            Link("tg://openmessage?user_id=777000&message_id=42", "tg://user?id=777000"),
            id="user-ignores-topic",
        ),
        pytest.param(
            _chat(USER, "bot"),
            None,
            Link("tg://openmessage?user_id=777000&message_id=42", "tg://user?id=777000"),
            id="bot",
        ),
        pytest.param(
            _chat(USER, "bot", "some_bot"),
            None,
            Link("tg://openmessage?user_id=777000&message_id=42", "tg://user?id=777000"),
            id="bot-username-ignored",
        ),
        pytest.param(
            _chat(GROUP, "group"),
            None,
            Link("tg://openmessage?chat_id=4567&message_id=42"),
            id="group",
        ),
        pytest.param(
            _chat(GROUP, "group", "legacy"),
            TOPIC,
            Link("tg://openmessage?chat_id=4567&message_id=42"),
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


def test_message_url_web_links_have_no_fallback() -> None:
    for chat in (_chat(SUPERGROUP, "supergroup", "x"), _chat(CHANNEL, "channel")):
        assert links.message_url(chat, MSG).fallback_url is None


def test_message_url_private_supergroup_with_bad_id_raises() -> None:
    chat = ChatRow(id=-1234, type="supergroup", title="t")
    with pytest.raises(ValueError, match="-1234"):
        links.message_url(chat, MSG)


def test_message_url_unknown_type_raises() -> None:
    chat = ChatRow(id=1, type="secret", title="t")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="secret"):
        links.message_url(chat, MSG)


# --- open_link -------------------------------------------------------------------------------


class Recorder:
    """A :class:`links.Runner` that records every command and answers from a script."""

    def __init__(self, *codes: int, stderr: bytes = b"") -> None:
        self.codes = list(codes)
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], /) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(args))
        code = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(list(args), code, b"", self.stderr if code else b"")


WEB = Link("https://t.me/ru_georgia/42")
PRIVATE = Link("tg://openmessage?user_id=777000&message_id=42", "tg://user?id=777000")


def test_open_link_runs_open_with_the_url() -> None:
    runner = Recorder()
    assert links.open_link(WEB, runner=runner, platform="darwin") == WEB.url
    assert runner.calls == [["open", WEB.url]]


def test_open_link_stops_after_the_primary_url_succeeds() -> None:
    runner = Recorder()
    assert links.open_link(PRIVATE, runner=runner, platform="darwin") == PRIVATE.url
    assert runner.calls == [["open", PRIVATE.url]]


def test_open_link_falls_back_when_open_rejects_the_primary_url() -> None:
    runner = Recorder(1, 0, stderr=b"No application knows how to open URL")
    assert links.open_link(PRIVATE, runner=runner, platform="darwin") == PRIVATE.fallback_url
    assert runner.calls == [["open", PRIVATE.url], ["open", PRIVATE.fallback_url]]


def test_open_link_raises_with_stderr_when_everything_fails() -> None:
    runner = Recorder(1, 1, stderr=b"No application knows how to open URL\n")
    with pytest.raises(links.OpenFailed) as info:
        links.open_link(PRIVATE, runner=runner, platform="darwin")
    message = str(info.value)
    assert PRIVATE.url in message
    assert str(PRIVATE.fallback_url) in message
    assert "No application knows how to open URL" in message
    assert len(runner.calls) == 2


def test_open_link_reports_exit_status_without_stderr() -> None:
    runner = Recorder(3)
    with pytest.raises(links.OpenFailed, match="exit status 3"):
        links.open_link(WEB, runner=runner, platform="darwin")


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_open_link_refuses_other_platforms_without_running_anything(platform: str) -> None:
    runner = Recorder()
    with pytest.raises(NotImplementedError, match=platform):
        links.open_link(WEB, runner=runner, platform=platform)
    assert runner.calls == []


def test_open_link_defaults_to_the_current_platform() -> None:
    runner = Recorder()
    if sys.platform == "darwin":
        assert links.open_link(WEB, runner=runner) == WEB.url
        assert runner.calls == [["open", WEB.url]]
    else:
        with pytest.raises(NotImplementedError):
            links.open_link(WEB, runner=runner)


def test_run_command_captures_output_and_never_raises() -> None:
    script = "import sys; sys.stderr.write('nope'); sys.exit(3)"
    result = links.run_command([sys.executable, "-c", script])
    assert result.returncode == 3
    assert result.stderr == b"nope"
    ok = links.run_command([sys.executable, "-c", "pass"])
    assert ok.returncode == 0
