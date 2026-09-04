"""Deep links that open a stored message in Telegram, and the ``open`` call behind them.

Telegram addresses a message differently per chat type. Public channels and supergroups have a
web link, ``https://t.me/<username>/<msg>``; private ones use ``https://t.me/c/<id>/<msg>``,
where ``<id>`` is Telegram's bare channel id — Telethon's marked ``-100<id>`` form the ``chats``
table stores is a client-side convention no Telegram URL understands, so
:func:`strip_channel_prefix` undoes it. Forum supergroups insert the topic: ``…/<topic>/<msg>``.
Private chats and legacy groups have no web form at all; the ``tg://openmessage`` scheme reaches
the message on mobile, and the desktop clients at least open the conversation through
``tg://user?id=`` — which is why :class:`~grepogram.models.Link` carries a ``fallback_url`` and
every hit and message view passes both on.

The web link is what results show and cite, but it is not what opens the app: ``open
https://t.me/…`` on macOS lands on the t.me page in the browser. Every link therefore also
carries an ``app_url`` in the ``tg://`` scheme the desktop app is registered for —
``tg://resolve?domain=<username>&post=<msg>`` for public chats,
``tg://privatepost?channel=<id>&post=<msg>`` for private ones, both with ``&thread=<topic>`` in a
forum topic, and the ``tg://openmessage`` forms as they are — and :func:`open_link` tries that
first, the web link second and the fallback last.

:func:`open_link` is the macOS side: ``open <url>`` hands the link to whatever owns the scheme.
The command runner is injectable so tests never launch anything, and ``GREPOGRAM_NO_OPEN=1``
(:func:`opening_disabled`) makes :func:`open_link` return the url without running anything at
all — the test environment sets it, so a probe or a smoke test that reaches the real runner
still opens nothing on the machine.
"""

import logging
import re
import subprocess
import sys
from collections.abc import Sequence
from typing import Protocol

from grepogram.models import ChatRow, Link
from grepogram.paths import env_flag

log = logging.getLogger(__name__)

OPEN_TIMEOUT_S = 15
NO_OPEN_ENV = "GREPOGRAM_NO_OPEN"
_CHANNEL_MARK = re.compile(r"^-100([1-9]\d*)$")  # Telethon's utils.resolve_id rule


class OpenFailed(Exception):
    """``open`` exited non-zero for the link and its fallback."""


class Runner(Protocol):
    def __call__(self, args: Sequence[str], /) -> subprocess.CompletedProcess[bytes]: ...


def strip_channel_prefix(chat_id: int) -> int:
    """Telegram's bare channel id from Telethon's marked one: ``-1001234`` → ``1234``.

    Only a channel or supergroup mark is accepted; a user id (positive) or a legacy group id
    (``-1234``) raises ``ValueError`` rather than producing a link to the wrong place.
    """
    match = _CHANNEL_MARK.match(str(chat_id))
    if match is None:
        raise ValueError(f"not a marked channel id: {chat_id}")
    return int(match.group(1))


def message_url(chat: ChatRow, msg_id: int, topic_id: int | None = None) -> Link:
    """The links that open ``msg_id`` in ``chat``: the web form to show, the ``tg://`` form the
    app takes, and a fallback where the link is mobile-only.

    ``topic_id`` is inserted only for forum supergroups, where it is a topic root and nothing
    else: a comment's channel post id lives in ``comment_of_msg_id`` and never reaches here.
    """
    if chat.type in ("channel", "supergroup"):
        topic = topic_id if chat.is_forum and topic_id is not None else None
        thread = "" if topic is None else f"&thread={topic}"
        if chat.username:
            base = f"https://t.me/{chat.username}"
            app = f"tg://resolve?domain={chat.username}&post={msg_id}{thread}"
        else:
            bare = strip_channel_prefix(chat.id)
            base = f"https://t.me/c/{bare}"
            app = f"tg://privatepost?channel={bare}&post={msg_id}{thread}"
        web = f"{base}/{msg_id}" if topic is None else f"{base}/{topic}/{msg_id}"
        return Link(web, app_url=app)
    if chat.type in ("user", "bot"):
        app = f"tg://openmessage?user_id={chat.id}&message_id={msg_id}"
        return Link(app, f"tg://user?id={chat.id}", app_url=app)
    if chat.type == "group":
        app = f"tg://openmessage?chat_id={abs(chat.id)}&message_id={msg_id}"
        return Link(app, app_url=app)
    raise ValueError(f"unknown chat type: {chat.type!r}")


def run_command(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """The default :class:`Runner`: run ``args`` capturing output, never raising on exit code."""
    return subprocess.run(list(args), capture_output=True, check=False, timeout=OPEN_TIMEOUT_S)


def opening_disabled() -> bool:
    """True when ``GREPOGRAM_NO_OPEN`` is set (``1``, ``true``, ``yes`` or ``on``): links are
    returned, never opened."""
    return env_flag(NO_OPEN_ENV)


def open_link(link: Link, *, runner: Runner = run_command, platform: str = sys.platform) -> str:
    """Open ``link`` with macOS ``open`` and return the url that worked.

    The ``app_url`` goes first — it is what reaches the Telegram app — then the web ``url``,
    then the fallback, each tried when ``open`` rejects the one before (no application
    registered for its scheme) or hangs past :data:`OPEN_TIMEOUT_S`; a url the link repeats is
    tried once. :class:`OpenFailed` carries ``open``'s stderr (or the timeout) when all fail.
    Other platforms get ``NotImplementedError`` and the link is left to the caller to display.
    With ``GREPOGRAM_NO_OPEN`` set nothing runs and the web url comes back as it is.
    """
    if opening_disabled():
        log.info("%s is set; not opening %s", NO_OPEN_ENV, link.url)
        return link.url
    if platform != "darwin":
        raise NotImplementedError(f"opening links needs macOS 'open' ({platform}): {link.url}")
    errors: list[str] = []
    tried: list[str] = []
    for url in (link.app_url, link.url, link.fallback_url):
        if url is None or url in tried:
            continue
        tried.append(url)
        try:
            result = runner(["open", url])
        except subprocess.TimeoutExpired:
            errors.append(f"{url}: open did not finish within {OPEN_TIMEOUT_S}s")
            continue
        if result.returncode == 0:
            return url
        detail = (
            result.stderr.decode(errors="replace").strip() or f"exit status {result.returncode}"
        )
        errors.append(f"{url}: {detail}")
    raise OpenFailed("open failed for " + "; ".join(errors))
