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


def username(name: str) -> LeadTarget | None:
    if not _is_username(name):
        return None
    name = name.lower()
    return LeadTarget(kind="username", target=f"@{name}", username=name)


def post(name: str, msg_id: int) -> LeadTarget | None:
    if not _is_username(name) or msg_id <= 0:
        return None
    name = name.lower()
    return LeadTarget(kind="post", target=f"@{name}/{msg_id}", username=name, msg_id=msg_id)


def private_post(channel_id: int, msg_id: int) -> LeadTarget | None:
    """A post of the channel whose bare id ``t.me/c/<id>`` names; the mark is arithmetic
    (:func:`telethon.utils.get_peer_id`), never a prefix glued onto the digits."""
    if channel_id <= 0 or msg_id <= 0:
        return None
    marked = int(utils.get_peer_id(types.PeerChannel(channel_id)))
    return LeadTarget(
        kind="private_post", target=f"c/{channel_id}/{msg_id}", peer_id=marked, msg_id=msg_id
    )


def invite(invite_hash: str) -> LeadTarget | None:
    # an all-digit `t.me/+…` is a phone number link, not an invite
    if not _TOKEN.fullmatch(invite_hash) or _DIGITS.fullmatch(invite_hash):
        return None
    return LeadTarget(kind="invite", target=f"+{invite_hash}", invite_hash=invite_hash)


def addlist(slug: str) -> LeadTarget | None:
    if not _TOKEN.fullmatch(slug):
        return None
    return LeadTarget(kind="addlist", target=f"addlist/{slug}", slug=slug)


def peer(marked_id: int) -> LeadTarget | None:
    if marked_id == 0:
        return None
    return LeadTarget(kind="peer", target=f"peer:{marked_id}", peer_id=marked_id)


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
        return (
            post(name, int(rest)) if _DIGITS.fullmatch(rest) else (None if rest else username(name))
        )
    if text.startswith("peer:"):
        number = text.removeprefix("peer:")
        return peer(int(number)) if re.fullmatch(r"-?[0-9]+", number) else None
    if text.startswith("+"):
        return invite(text[1:])
    if text.startswith("c/"):
        parts = text.split("/")
        if len(parts) == 3 and all(_DIGITS.fullmatch(part) for part in parts[1:]):
            return private_post(int(parts[1]), int(parts[2]))
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
        number = query.get("post", "")
        return post(domain, int(number)) if _DIGITS.fullmatch(number) else username(domain)
    if action == "join":
        return invite(query.get("invite", ""))
    if action == "addlist":
        return addlist(query.get("slug", ""))
    if action == "privatepost":
        channel, number = query.get("channel", ""), query.get("post", "")
        if _DIGITS.fullmatch(channel) and _DIGITS.fullmatch(number):
            return private_post(int(channel), int(number))
        return None
    if action == "user":
        user_id = query.get("id", "")
        return peer(int(user_id)) if _DIGITS.fullmatch(user_id) else None
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
    if not rest or not all(_DIGITS.fullmatch(segment) for segment in rest[:3]):
        return None
    channel_id = int(rest[0])
    if len(rest) == 1:
        return peer(int(utils.get_peer_id(types.PeerChannel(channel_id)))) if channel_id else None
    return private_post(channel_id, int(rest[min(len(rest), 3) - 1]))


def _public_path(name: str, rest: list[str]) -> LeadTarget | None:
    """``t.me/<name>`` names the chat; ``/<post>`` or ``/<topic>/<post>`` a post of it."""
    numbers = [int(segment) for segment in rest[:2] if _DIGITS.fullmatch(segment)]
    if rest and numbers and len(numbers) == min(len(rest), 2):
        return post(name, numbers[-1])
    return username(name)
