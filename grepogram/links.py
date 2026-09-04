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

:func:`open_link` is the macOS side: ``open <url>`` hands the link to whatever owns the scheme
(the Telegram app, or a browser that redirects to it). The command runner is injectable so tests
never launch anything.
"""

import re
import subprocess
import sys
from collections.abc import Sequence
from typing import Protocol

from grepogram.models import ChatRow, Link

OPEN_TIMEOUT_S = 15
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
    """The link that opens ``msg_id`` in ``chat`` (plus a fallback where the link is mobile-only).

    ``topic_id`` is inserted only for forum supergroups; a discussion chat stores each comment's
    channel post id in the same column, and that must not end up in the URL.
    """
    if chat.type in ("channel", "supergroup"):
        if chat.username:
            base = f"https://t.me/{chat.username}"
        else:
            base = f"https://t.me/c/{strip_channel_prefix(chat.id)}"
        if chat.is_forum and topic_id is not None:
            return Link(f"{base}/{topic_id}/{msg_id}")
        return Link(f"{base}/{msg_id}")
    if chat.type in ("user", "bot"):
        return Link(
            f"tg://openmessage?user_id={chat.id}&message_id={msg_id}",
            f"tg://user?id={chat.id}",
        )
    if chat.type == "group":
        return Link(f"tg://openmessage?chat_id={abs(chat.id)}&message_id={msg_id}")
    raise ValueError(f"unknown chat type: {chat.type!r}")


def run_command(args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """The default :class:`Runner`: run ``args`` capturing output, never raising on exit code."""
    return subprocess.run(list(args), capture_output=True, check=False, timeout=OPEN_TIMEOUT_S)


def open_link(link: Link, *, runner: Runner = run_command, platform: str = sys.platform) -> str:
    """Open ``link`` with macOS ``open`` and return the url that worked.

    The fallback url is tried when ``open`` rejects the primary one (no application registered
    for its scheme) or hangs past :data:`OPEN_TIMEOUT_S`; :class:`OpenFailed` carries ``open``'s
    stderr (or the timeout) when both fail. Other platforms get ``NotImplementedError`` and the
    link is left to the caller to display.
    """
    if platform != "darwin":
        raise NotImplementedError(f"opening links needs macOS 'open' ({platform}): {link.url}")
    errors: list[str] = []
    for url in (link.url, link.fallback_url):
        if url is None:
            continue
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
