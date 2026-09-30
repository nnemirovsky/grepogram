"""Telegram destinations named by a link or a mention, normalized to one identity each.

Pure: nothing here talks to Telegram or to the index. :func:`normalize` turns one URL or mention
into a :class:`LeadTarget` whose ``target`` string is the identity research deduplicates on and
``message_links.target`` stores; anything that does not name a Telegram destination — an ordinary
web page, a phone number link, a sticker set — is ``None`` and is never stored. The forms:

========================  ===========================================================
``@name``                 a public chat, channel, user or bot by its username
``@name/123``             post 123 of that public chat
``c/<id>/<post>``         a post of a private channel or supergroup (``t.me/c/…``)
``+<hash>``               an invite link (``t.me/+…``, ``t.me/joinchat/…``, ``tg://join``)
``addlist/<slug>``        a shared folder (``t.me/addlist/…``, ``tg://addlist``)
``peer:<marked id>``      a peer named by its id alone (``tg://user``, ``t.me/c/<id>``)
========================  ===========================================================

Usernames are case-insensitive in Telegram and are lowercased; invite hashes and folder slugs
are not, and keep their case. :func:`normalize` accepts its own output, so a stored target reads
back into the same :class:`LeadTarget`.

:func:`text_leads` is the fallback for rows stored before links were captured
(``meta['links_captured_from']``): it finds only what the text shows — visible URLs and
``@mentions`` — never a hidden ``text_url`` hyperlink or a button, which only the message's
entities and markup carried.
"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from telethon import utils
from telethon.tl import types

from grepogram.models import LinkKind

LeadKind = Literal["username", "post", "private_post", "invite", "addlist", "peer"]

TELEGRAM_HOSTS = frozenset({"t.me", "telegram.me", "telegram.dog"})

_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
"""A username as a link or a mention spells it: 4 to 32 characters (collectible usernames can be
four long), a letter first."""
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
"""An invite hash or a folder slug: base64url."""
_DIGITS = re.compile(r"[0-9]+")

INT64_MAX = 2**63 - 1
"""The largest id Telegram's ``long`` — and an SQLite ``INTEGER`` — holds: a peer id, marked or
bare, is one of them."""
MSG_ID_MAX = 2**31 - 1
"""The largest message id: Telegram's ``int``."""
_ID_DIGITS = 19
"""The most digits a number read out of a link may have; anything longer names no Telegram id
(and past about 4300 digits ``int`` itself refuses to read it)."""

_RESERVED = frozenset(
    {
        "addemoji",
        "addlist",
        "addstickers",
        "addtheme",
        "boost",
        "confirmphone",
        "contact",
        "giftcode",
        "invoice",
        "joinchat",
        "login",
        "proxy",
        "setlanguage",
        "share",
        "socks",
    }
)
"""First ``t.me`` path segments that are Telegram's own routes, never a username."""

_LINK_IN_TEXT = re.compile(
    r"(?<![\w.@/-])(?:https?://)?(?:www\.)?(?:[a-z0-9_]{4,32}\.)?"
    r"(?:t\.me|telegram\.me|telegram\.dog)(?![\w.-])[^\s<>\"'`]*"
    r"|(?<![\w/])tg://[^\s<>\"'`]+",
    re.IGNORECASE,
)
_MENTION_IN_TEXT = re.compile(r"(?<![\w@./-])@[A-Za-z][A-Za-z0-9_]{3,31}(?![\w@])")
_TRAILING = ".,;:!?)]}>»\"'"
"""Punctuation a sentence puts right after a URL, which the URL never ends with."""


@dataclass(frozen=True, slots=True)
class LeadTarget:
    """One Telegram destination: ``target`` is its identity, the other fields what it names."""

    kind: LeadKind
    target: str
    username: str | None = None
    peer_id: int | None = None
    """The marked id: of the peer for ``peer``, of the channel for ``private_post``."""
    msg_id: int | None = None
    invite_hash: str | None = None
    slug: str | None = None


def username_identity(name: str) -> str:
    """How an identity spells the chat ``@name`` names: lowercased, as Telegram matches it. The
    one spelling of it — :func:`username` and every reader of a stored name build it here."""
    return f"@{name.lower()}"


def peer_identity(marked_id: int) -> str:
    """How an identity spells a peer named by its marked id alone (:func:`peer`)."""
    return f"peer:{marked_id}"


def invite_identity(invite_hash: str) -> str:
    """How an identity spells an invite link by its hash (:func:`invite`)."""
    return f"+{invite_hash}"


def username(name: str) -> LeadTarget | None:
    if not _is_username(name):
        return None
    name = name.lower()
    return LeadTarget(kind="username", target=username_identity(name), username=name)


def number(text: str) -> int | None:
    """``text`` as a non-negative number when it is ASCII digits of an id's length, else
    ``None`` — the one reader of an id a link spells."""
    if len(text) > _ID_DIGITS or not _DIGITS.fullmatch(text):
        return None
    return int(text)


def valid_peer(marked_id: int) -> bool:
    """Whether ``marked_id`` can be a Telegram peer: not zero, a signed 64-bit value, and for a
    group or channel a mark (:func:`telethon.utils.resolve_id`) over a positive bare id."""
    if marked_id == 0 or not -INT64_MAX - 1 <= marked_id <= INT64_MAX:
        return False
    return int(utils.resolve_id(marked_id)[0]) > 0


def _msg_id(msg_id: int) -> bool:
    return 0 < msg_id <= MSG_ID_MAX


def _channel_mark(channel_id: int) -> int | None:
    """The marked id of the channel ``t.me/c/<channel_id>`` names, or ``None`` when no channel
    has that bare id; the mark is arithmetic (:func:`telethon.utils.get_peer_id`), never a
    prefix glued onto the digits."""
    if not 0 < channel_id <= INT64_MAX:
        return None
    marked = int(utils.get_peer_id(types.PeerChannel(channel_id)))
    return marked if valid_peer(marked) else None


def post(name: str, msg_id: int) -> LeadTarget | None:
    if not _is_username(name) or not _msg_id(msg_id):
        return None
    name = name.lower()
    return LeadTarget(kind="post", target=f"@{name}/{msg_id}", username=name, msg_id=msg_id)


def private_post(channel_id: int, msg_id: int) -> LeadTarget | None:
    """A post of the channel whose bare id ``t.me/c/<id>`` names (:func:`_channel_mark`); an id
    out of Telegram's range names nothing."""
    marked = _channel_mark(channel_id)
    if marked is None or not _msg_id(msg_id):
        return None
    return LeadTarget(
        kind="private_post", target=f"c/{channel_id}/{msg_id}", peer_id=marked, msg_id=msg_id
    )


def invite(invite_hash: str) -> LeadTarget | None:
    # an all-digit `t.me/+…` is a phone number link, not an invite
    if not _TOKEN.fullmatch(invite_hash) or _DIGITS.fullmatch(invite_hash):
        return None
    return LeadTarget(kind="invite", target=invite_identity(invite_hash), invite_hash=invite_hash)


def addlist(slug: str) -> LeadTarget | None:
    if not _TOKEN.fullmatch(slug):
        return None
    return LeadTarget(kind="addlist", target=f"addlist/{slug}", slug=slug)


def peer(marked_id: int) -> LeadTarget | None:
    if not valid_peer(marked_id):
        return None
    return LeadTarget(kind="peer", target=peer_identity(marked_id), peer_id=marked_id)


def normalize(value: str) -> LeadTarget | None:
    """The Telegram destination ``value`` names — a URL (``https://t.me/…``, ``telegram.me``,
    ``telegram.dog``, ``<name>.t.me``, a bare ``t.me/…``, ``tg://resolve|join|addlist|
    privatepost|user``), a mention (``@name``) or a target this module produced — else ``None``.
    """
    text = value.strip().rstrip(_TRAILING)
    if not text:
        return None
    if text.startswith("@"):
        name, _, rest = text[1:].partition("/")
        if not rest:
            return username(name)
        msg_id = number(rest)
        return None if msg_id is None else post(name, msg_id)
    if text.startswith("peer:"):
        spelled = text.removeprefix("peer:")
        bare = number(spelled.removeprefix("-"))
        if bare is None:
            return None
        return peer(-bare if spelled.startswith("-") else bare)
    if text.startswith("+"):
        return invite(text[1:])
    if text.startswith("c/"):
        parts = text.split("/")
        numbers = [number(part) for part in parts[1:]]
        if len(parts) == 3 and numbers[0] is not None and numbers[1] is not None:
            return private_post(numbers[0], numbers[1])
        return None
    if text.startswith("addlist/"):
        return addlist(text.removeprefix("addlist/"))
    if text.lower().startswith("tg:"):
        return _tg_url(text)
    return _web_url(text)


def text_leads(text: str) -> tuple[tuple[LinkKind, str], ...]:
    """``(kind, target)`` for every Telegram destination ``text`` shows — URLs as ``link``,
    ``@mentions`` as ``mention`` — deduplicated and sorted like ``MessageRow.links``."""
    found: set[tuple[LinkKind, str]] = set()
    for match in _LINK_IN_TEXT.finditer(text):
        lead = normalize(match.group(0))
        if lead is not None:
            found.add(("link", lead.target))
    for match in _MENTION_IN_TEXT.finditer(text):
        lead = normalize(match.group(0))
        if lead is not None:
            found.add(("mention", lead.target))
    return tuple(sorted(found))


def _is_username(name: str) -> bool:
    return _USERNAME.fullmatch(name) is not None and name.lower() not in _RESERVED


def _tg_url(text: str) -> LeadTarget | None:
    parts = urlsplit(text)
    action = (parts.netloc or parts.path.strip("/")).lower()
    query = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
    if action == "resolve":
        domain = query.get("domain", "")
        spelled = query.get("post", "")
        if not _DIGITS.fullmatch(spelled):
            return username(domain)
        msg_id = number(spelled)
        return None if msg_id is None else post(domain, msg_id)
    if action == "join":
        return invite(query.get("invite", ""))
    if action == "addlist":
        return addlist(query.get("slug", ""))
    if action == "privatepost":
        channel, msg_id = number(query.get("channel", "")), number(query.get("post", ""))
        if channel is not None and msg_id is not None:
            return private_post(channel, msg_id)
        return None
    if action == "user":
        user_id = number(query.get("id", ""))
        return None if user_id is None else peer(user_id)
    return None


def _web_url(text: str) -> LeadTarget | None:
    if "://" not in text:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or "@" in parts.netloc:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    segments = [segment for segment in parts.path.split("/") if segment]
    if host not in TELEGRAM_HOSTS:
        # a username's own subdomain: https://<name>.t.me
        name, _, parent = host.partition(".")
        return username(name) if parent == "t.me" and not segments else None
    if not segments:
        return None
    head, rest = segments[0], segments[1:]
    route = head.lower()  # Telegram's own routes open whatever their case: t.me/JoinChat/…
    if head.startswith("+"):
        return invite(head[1:])
    if route == "joinchat":
        return invite(rest[0]) if rest else None
    if route == "addlist":
        return addlist(rest[0]) if rest else None
    if route == "c":
        return _private_path(rest)
    if route == "s":  # the web preview of a public channel: t.me/s/<name>[/<post>]
        head, rest = (rest[0], rest[1:]) if rest else ("", [])
    return _public_path(head, rest)


def _private_path(rest: list[str]) -> LeadTarget | None:
    """``t.me/c/<id>`` names the chat; ``/<post>`` or ``/<topic>/<post>`` a post of it."""
    numbers = [number(segment) for segment in rest[:3]]
    if not rest or any(found is None for found in numbers):
        return None
    channel_id = numbers[0]
    assert channel_id is not None
    if len(rest) == 1:
        marked = _channel_mark(channel_id)
        return None if marked is None else peer(marked)
    msg_id = numbers[min(len(rest), 3) - 1]
    assert msg_id is not None
    return private_post(channel_id, msg_id)


def _public_path(name: str, rest: list[str]) -> LeadTarget | None:
    """``t.me/<name>`` names the chat; ``/<post>`` or ``/<topic>/<post>`` a post of it."""
    digits = [segment for segment in rest[:2] if _DIGITS.fullmatch(segment)]
    if rest and digits and len(digits) == min(len(rest), 2):
        msg_id = number(digits[-1])
        return None if msg_id is None else post(name, msg_id)
    return username(name)
