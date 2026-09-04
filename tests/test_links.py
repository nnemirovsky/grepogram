import subprocess
import sys
from collections.abc import Sequence

import pytest

from grepogram import links
from grepogram.models import ChatRow, ChatType, Link


@pytest.fixture(autouse=True)
def opening_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test environment forbids opening links (``conftest.py``); the ``open_link`` tests
    inject a recording runner and want the call to reach it. The switch itself is tested with
    the variable set again."""
    monkeypatch.delenv(links.NO_OPEN_ENV, raising=False)


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


PUBLIC_APP = "tg://resolve?domain=ru_georgia&post=42"
PRIVATE_APP = "tg://privatepost?channel=1234567890&post=42"
USER_APP = "tg://openmessage?user_id=777000&message_id=42"
GROUP_APP = "tg://openmessage?chat_id=4567&message_id=42"


@pytest.mark.parametrize(
    ("chat", "topic_id", "expected"),
    [
        pytest.param(
            _chat(SUPERGROUP, "supergroup", "ru_georgia"),
            None,
            Link("https://t.me/ru_georgia/42", app_url=PUBLIC_APP),
            id="supergroup-public",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", "ru_georgia", forum=True),
            TOPIC,
            Link("https://t.me/ru_georgia/7/42", app_url=f"{PUBLIC_APP}&thread=7"),
            id="supergroup-public-forum-topic",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", "ru_georgia", forum=True),
            None,
            Link("https://t.me/ru_georgia/42", app_url=PUBLIC_APP),
            id="supergroup-public-forum-general",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", "ru_georgia"),
            TOPIC,
            Link("https://t.me/ru_georgia/42", app_url=PUBLIC_APP),
            id="supergroup-public-not-forum-ignores-topic",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup"),
            None,
            Link("https://t.me/c/1234567890/42", app_url=PRIVATE_APP),
            id="supergroup-private",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", forum=True),
            TOPIC,
            Link("https://t.me/c/1234567890/7/42", app_url=f"{PRIVATE_APP}&thread=7"),
            id="supergroup-private-forum-topic",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup", forum=True),
            None,
            Link("https://t.me/c/1234567890/42", app_url=PRIVATE_APP),
            id="supergroup-private-forum-general",
        ),
        pytest.param(
            _chat(SUPERGROUP, "supergroup"),
            TOPIC,
            Link("https://t.me/c/1234567890/42", app_url=PRIVATE_APP),
            id="supergroup-private-not-forum-ignores-topic",
        ),
        pytest.param(
            _chat(SHORT, "supergroup"),
            None,
            Link(
                "https://t.me/c/123456789/42",
                app_url="tg://privatepost?channel=123456789&post=42",
            ),
            id="supergroup-private-short-bare-id",
        ),
        pytest.param(
            _chat(CHANNEL, "channel", "durov"),
            None,
            Link("https://t.me/durov/42", app_url="tg://resolve?domain=durov&post=42"),
            id="channel-public",
        ),
        pytest.param(
            _chat(CHANNEL, "channel"),
            None,
            Link(
                "https://t.me/c/9876543210/42",
                app_url="tg://privatepost?channel=9876543210&post=42",
            ),
            id="channel-private",
        ),
        pytest.param(
            _chat(CHANNEL, "channel", "durov"),
            TOPIC,
            Link("https://t.me/durov/42", app_url="tg://resolve?domain=durov&post=42"),
            id="channel-public-ignores-topic",
        ),
        pytest.param(
            _chat(USER, "user"),
            None,
            Link(USER_APP, "tg://user?id=777000", app_url=USER_APP),
            id="user",
        ),
        pytest.param(
            _chat(USER, "user", "alice"),
            None,
            Link(USER_APP, "tg://user?id=777000", app_url=USER_APP),
            id="user-username-ignored",
        ),
        pytest.param(
            _chat(USER, "user"),
            TOPIC,
            Link(USER_APP, "tg://user?id=777000", app_url=USER_APP),
            id="user-ignores-topic",
        ),
        pytest.param(
            _chat(USER, "bot"),
            None,
            Link(USER_APP, "tg://user?id=777000", app_url=USER_APP),
            id="bot",
        ),
        pytest.param(
            _chat(USER, "bot", "some_bot"),
            None,
            Link(USER_APP, "tg://user?id=777000", app_url=USER_APP),
            id="bot-username-ignored",
        ),
        pytest.param(
            _chat(GROUP, "group"),
            None,
            Link(GROUP_APP, app_url=GROUP_APP),
            id="group",
        ),
        pytest.param(
            _chat(GROUP, "group", "legacy"),
            TOPIC,
            Link(GROUP_APP, app_url=GROUP_APP),
            id="group-username-and-topic-ignored",
        ),
    ],
)
def test_message_url(chat: ChatRow, topic_id: int | None, expected: Link) -> None:
    assert links.message_url(chat, MSG, topic_id) == expected


def test_message_url_topic_is_keyword_optional() -> None:
    chat = _chat(SUPERGROUP, "supergroup", "ru_georgia", forum=True)
    assert links.message_url(chat, MSG) == Link("https://t.me/ru_georgia/42", app_url=PUBLIC_APP)
    assert links.message_url(chat, MSG, topic_id=TOPIC) == Link(
        "https://t.me/ru_georgia/7/42", app_url=f"{PUBLIC_APP}&thread=7"
    )


def test_message_url_web_links_have_no_fallback_and_every_link_has_an_app_form() -> None:
    for chat in (_chat(SUPERGROUP, "supergroup", "x"), _chat(CHANNEL, "channel")):
        link = links.message_url(chat, MSG)
        assert link.fallback_url is None
        assert link.url.startswith("https://t.me/")
    for chat in (
        _chat(SUPERGROUP, "supergroup", "x"),
        _chat(CHANNEL, "channel"),
        _chat(USER, "user"),
        _chat(GROUP, "group"),
    ):
        app_url = links.message_url(chat, MSG).app_url
        assert app_url is not None and app_url.startswith("tg://")


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
PUBLIC = Link("https://t.me/ru_georgia/42", app_url=PUBLIC_APP)
USER_LINK = Link(USER_APP, "tg://user?id=777000", app_url=USER_APP)


def test_open_link_launches_the_app_url_before_the_web_url() -> None:
    runner = Recorder()
    assert links.open_link(PUBLIC, runner=runner, platform="darwin") == PUBLIC_APP
    assert runner.calls == [["open", PUBLIC_APP]]


def test_open_link_falls_back_to_the_web_url_when_the_app_url_is_rejected() -> None:
    runner = Recorder(1, stderr=b"no application knows how to open URL")
    assert links.open_link(PUBLIC, runner=runner, platform="darwin") == PUBLIC.url
    assert runner.calls == [["open", PUBLIC_APP], ["open", PUBLIC.url]]


def test_open_link_tries_a_repeated_url_once() -> None:
    runner = Recorder(1, 0)
    assert links.open_link(USER_LINK, runner=runner, platform="darwin") == "tg://user?id=777000"
    assert runner.calls == [["open", USER_APP], ["open", "tg://user?id=777000"]]
    failing = Recorder(1, 1)
    with pytest.raises(links.OpenFailed) as excinfo:
        links.open_link(USER_LINK, runner=failing, platform="darwin")
    assert str(excinfo.value).count(USER_APP) == 1 and len(failing.calls) == 2


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


@pytest.mark.parametrize("value", ["1", "true", " Yes ", "on"])
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_open_link_returns_the_url_without_running_anything_when_disabled(
    monkeypatch: pytest.MonkeyPatch, value: str, platform: str
) -> None:
    monkeypatch.setenv(links.NO_OPEN_ENV, value)
    runner = Recorder()
    assert links.opening_disabled()
    assert links.open_link(PRIVATE, runner=runner, platform=platform) == PRIVATE.url
    assert links.open_link(WEB, runner=runner) == WEB.url
    assert runner.calls == []


@pytest.mark.parametrize("value", [None, "", "0", "no", "off"])
def test_opening_is_disabled_only_by_a_true_value(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(links.NO_OPEN_ENV, raising=False)
    else:
        monkeypatch.setenv(links.NO_OPEN_ENV, value)
    assert not links.opening_disabled()
    runner = Recorder()
    assert links.open_link(WEB, runner=runner, platform="darwin") == WEB.url
    assert runner.calls == [["open", WEB.url]]


def test_run_command_captures_output_and_never_raises() -> None:
    script = "import sys; sys.stderr.write('nope'); sys.exit(3)"
    result = links.run_command([sys.executable, "-c", script])
    assert result.returncode == 3
    assert result.stderr == b"nope"
    ok = links.run_command([sys.executable, "-c", "pass"])
    assert ok.returncode == 0


# --- added by the review fixes --------------------------------------------------------------


class Scripted:
    """A :class:`links.Runner` whose answers are exit codes or exceptions to raise, in order."""

    def __init__(self, *answers: int | BaseException) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], /) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(args))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(list(args), answer, b"", b"")


def _hang(args: Sequence[str] = ("open",)) -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired(list(args), links.OPEN_TIMEOUT_S)


def test_open_link_treats_a_hung_open_as_a_failure() -> None:
    runner = Scripted(_hang(), _hang())
    with pytest.raises(links.OpenFailed) as info:
        links.open_link(PRIVATE, runner=runner, platform="darwin")
    message = str(info.value)
    assert PRIVATE.url in message and str(PRIVATE.fallback_url) in message
    assert f"did not finish within {links.OPEN_TIMEOUT_S}s" in message
    assert len(runner.calls) == 2


def test_open_link_falls_back_when_the_primary_open_hangs() -> None:
    runner = Scripted(_hang(), 0)
    assert links.open_link(PRIVATE, runner=runner, platform="darwin") == PRIVATE.fallback_url
    assert runner.calls == [["open", PRIVATE.url], ["open", PRIVATE.fallback_url]]
