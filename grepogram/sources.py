"""Sources: the ``[[sources]]`` entries that decide which Telegram chats get indexed.

A *target* is what a user or Claude types: a marked id as printed by ``grepogram dialogs``, an
``@username``, a ``t.me`` link, ``folder:<name>``, or free text matched fuzzily against dialog
titles and folder names. :func:`add_source` resolves a target through a
:class:`~grepogram.dialogs.DialogCatalog` into a :class:`Source`; :func:`resolve_sources` turns
every configured source into ``chats`` rows tagged with ``source_id`` and runs before each sync
because folder membership changes; :func:`remove_source` drops an entry together with its
chats' data; :func:`sources_status` reports what is indexed per source.

A chat can also arrive with no source to fetch it from: :func:`import_chats` stores a Telegram
Desktop export and tags each of its chats ``import:<slug>``, a source id that names no
``[[sources]]`` entry because there is nothing to sync. That prefix is what every protection of
an imported history keys on — :func:`prunable` never offers such a chat and
:func:`grepogram.sync.prune_deleted` never sweeps one — while
:func:`grepogram.db.upsert_chat` overwrites ``source_id`` unconditionally, so both directions
are refused by name: :func:`import_chats` will not import over a chat synced from Telegram, and
:func:`refuse_imported` will not let a live source cover an imported chat.
"""

import collections
import dataclasses
import datetime as dt
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from telethon import errors, utils
from telethon.tl import types

from grepogram import db, dialogs, tg
from grepogram.dialogs import DialogCatalog, DialogInfo, FolderInfo, Match
from grepogram.models import (
    ACCOUNT_NAME,
    DEFAULT_ACCOUNT,
    ChatRow,
    ChatStatus,
    Config,
    Source,
    SourceStatus,
    chat_scope,
)
from grepogram.tdesktop import ImportedChat

log = logging.getLogger(__name__)

TargetKind = Literal["id", "username", "folder", "fuzzy"]
FOLDER_PREFIX = "folder:"
CHAT_PREFIX = "chat:"
IMPORT_PREFIX = "import:"
IMPORT_SLUG_MAX = 40
"""How much of a chat title an ``import:`` source id carries; the rest is readability, not
identity — :func:`import_source_ids` disambiguates a collision with the chat's own id."""
ENTITY_ERRORS: tuple[type[Exception], ...] = (
    ValueError,
    TypeError,
    errors.UsernameInvalidError,
    errors.UsernameNotOccupiedError,
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.PeerIdInvalidError,
)

_INT_RE = re.compile(r"^-?\d+$")
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?P<path>[^?#]*)",
    re.IGNORECASE,
)
_ACCOUNT_PREFIX_RE = re.compile(
    rf"^(?P<account>{ACCOUNT_NAME.pattern})/(?=(?:{CHAT_PREFIX}|{FOLDER_PREFIX}))", re.IGNORECASE
)
"""The ``<account>/`` in front of a source id of an account other than the default one
(:attr:`grepogram.models.Source.id`). Only a ``chat:`` or ``folder:`` id carries one, so free
text with a slash in it stays free text."""


class SourceError(Exception):
    """A source target cannot be parsed, resolved or applied."""


class InvalidTarget(SourceError):
    """The target has a recognised shape but is malformed (bad username, invite link, ...)."""


class UnknownTarget(SourceError):
    """The target parsed fine but names no dialog or folder of this account."""


class AmbiguousTarget(SourceError):
    """A fuzzy target matches several dialogs, folders or sources."""

    def __init__(self, query: str, candidates: Sequence[str]) -> None:
        self.query = query
        self.candidates = list(candidates)
        listing = "; ".join(self.candidates)
        super().__init__(f"{query!r} matches several entries, be more specific: {listing}")


class DuplicateSource(SourceError):
    """The resolved chat or folder is already a source."""


class UnknownSource(SourceError):
    """No configured or indexed source matches the target."""


class ImportConflict(SourceError):
    """A Telegram Desktop import and a live source would claim the same chat.

    Raised in both directions, because :func:`grepogram.db.upsert_chat` overwrites ``source_id``
    unconditionally and whichever writes last would silently take the chat over: importing an
    export of a chat this index already syncs, and adding a live source for a chat this index
    holds as an import.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class Target:
    """A parsed target; ``value`` is the marked id for ``kind="id"`` and text otherwise.

    ``account`` is the account an ``<account>/chat:…`` or ``<account>/folder:…`` source id
    names, and ``None`` for everything typed without that prefix — which is not "the default
    account" but "any account, the default one first" to the readers that match it
    (:func:`find_source`).
    """

    kind: TargetKind
    value: str | int
    account: str | None = None

    @property
    def text(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class Added:
    """Result of :func:`add_source`: the new config and what the entry resolved to.

    ``dialogs`` holds the single chat, or every member of the folder at the time of adding.
    """

    config: Config
    source: Source
    title: str
    dialogs: list[DialogInfo]
    folder: FolderInfo | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Removed:
    """Result of :func:`remove_source`; ``source`` is ``None`` when only stale data was removed."""

    config: Config
    source_id: str
    source: Source | None
    chat_ids: list[int]
    """The chats deleted with the source: nothing left in the config covers them."""
    kept_chat_ids: list[int] = dataclasses.field(default_factory=list)
    """The chats it covered that another configured source still covers; they stay, with the
    first of those as their new primary source (:func:`remove_source`)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Imported:
    """One chat :func:`import_chats` stored: the row as it now stands and what went into it.

    ``chat`` is read back from the database, so it carries the sync state an earlier import or
    an earlier life left on the row; ``source_id`` is the ``import:<slug>`` tag this import
    wrote, and ``messages`` how many rows of the export it stored under it.
    """

    chat: ChatRow
    source_id: str
    messages: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FolderMembership:
    """What every folder source lists right now, read from Telegram by :func:`folder_membership`.

    ``listed`` maps a folder source's id to the chat ids it covers *at this moment*; ``failed``
    maps the id of a source that could not be resolved to the reason. A source is in exactly one
    of the two, and one in neither was never looked at. The failures are data rather than a log
    line on purpose: :func:`prunable` derives "the folder no longer lists this chat" from this
    object, and a folder that answered with an error must never read as an empty one.
    """

    listed: dict[str, set[int]] = dataclasses.field(default_factory=dict)
    failed: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class PruneCandidate:
    """One indexed chat the prune scan reached, with ``reason`` saying why it is offered or kept
    and ``messages`` how many stored messages would go with it."""

    chat: ChatRow
    reason: str
    messages: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PruneScan:
    """What ``sources prune`` would delete (:attr:`prunable`), what it deliberately keeps
    (:attr:`kept`) and which configured sources it could not check (:attr:`unresolved`)."""

    prunable: list[PruneCandidate]
    kept: list[PruneCandidate]
    unresolved: list[str]


# --- targets ---------------------------------------------------------------------------------


def parse_target(raw: str) -> Target:
    """Classify a target string.

    ``-1000000000100`` / ``42`` → marked id (as printed by ``grepogram dialogs``);
    ``@name``, ``https://t.me/name`` and ``t.me/s/name`` → username; ``t.me/c/<id>`` → the
    private channel's marked id; ``folder:<name>`` → folder; ``chat:<value>`` → the value parsed
    again (so source ids from ``sources ls`` work); anything else → fuzzy text. Invite links
    and malformed usernames raise :class:`InvalidTarget`. A source id of another account,
    ``<account>/chat:<value>`` or ``<account>/folder:<name>``, parses as the id past the prefix
    with :attr:`Target.account` set.
    """
    text = raw.strip()
    if not text:
        raise InvalidTarget("empty target")
    prefixed = _ACCOUNT_PREFIX_RE.match(text)
    if prefixed is not None:
        inner = parse_target(text[prefixed.end() :])
        return dataclasses.replace(inner, account=prefixed.group("account").casefold())
    lowered = text.casefold()
    if lowered.startswith(CHAT_PREFIX):
        return parse_target(text[len(CHAT_PREFIX) :])
    if lowered.startswith(FOLDER_PREFIX):
        name = text[len(FOLDER_PREFIX) :].strip()
        if not name:
            raise InvalidTarget(f"folder name missing in {raw!r}")
        return Target(kind="folder", value=name)
    if _INT_RE.match(text):
        return Target(kind="id", value=int(text))
    if text.startswith("@"):
        return Target(kind="username", value=_username(text[1:], raw))
    link = _LINK_RE.match(text)
    if link is not None:
        return _parse_link(link.group("path"), raw)
    return Target(kind="fuzzy", value=text)


def _parse_link(path: str, raw: str) -> Target:
    parts = [part for part in path.split("/") if part]
    if not parts:
        raise InvalidTarget(f"no chat in link {raw!r}")
    head = parts[0]
    if head == "c":
        if len(parts) < 2 or not parts[1].isdigit():
            raise InvalidTarget(f"expected t.me/c/<id> in {raw!r}")
        return Target(kind="id", value=dialogs.peer_id(types.PeerChannel(int(parts[1]))))
    if head == "s" and len(parts) > 1:
        head = parts[1]
    if head.startswith("+") or head == "joinchat":
        raise InvalidTarget(
            f"invite links cannot be indexed ({raw!r}): join the chat in Telegram, then add it "
            "by title, @username or id"
        )
    return Target(kind="username", value=_username(head, raw))


def _username(name: str, raw: str) -> str:
    if not _USERNAME_RE.match(name):
        raise InvalidTarget(f"invalid username in {raw!r}")
    return name


def split_source_id(source_id: str) -> tuple[str, str]:
    """``(account, id past the account prefix)`` of a source id.

    ``work/chat:@x`` → ``("work", "chat:@x")``; an id with no prefix — every default-account
    source, and ``import:<slug>``, which names no account at all — is the default account's.
    """
    prefixed = _ACCOUNT_PREFIX_RE.match(source_id)
    if prefixed is None:
        return DEFAULT_ACCOUNT, source_id
    return prefixed.group("account").casefold(), source_id[prefixed.end() :]


def source_account(source_id: str) -> str:
    """The account a source id belongs to (:func:`split_source_id`)."""
    return split_source_id(source_id)[0]


def _in_account[T](
    items: Iterable[T], account_of: Callable[[T], str], account: str | None
) -> list[T]:
    """The ``items`` a target of ``account`` can mean: that account's alone when it names one,
    else the default account's when there are any, else all of them.

    A target typed without an ``<account>/`` prefix is how every command has always named the
    default account's sources, so it keeps doing that when the default account has a match, and
    reaches another account's only when that is the one match there is.
    """
    listed = list(items)
    if account is not None:
        return [item for item in listed if account_of(item) == account]
    default = [item for item in listed if account_of(item) == DEFAULT_ACCOUNT]
    return default or listed


def describe_match(found: Match) -> str:
    """One-line description of a candidate, used when a target is ambiguous."""
    if found.folder is not None:
        return f"folder {found.title!r} ({FOLDER_PREFIX}{found.title})"
    dialog = found.dialog
    assert dialog is not None
    handle = f", @{dialog.username}" if dialog.username else ""
    return f"{dialog.type} {dialog.title!r} (id {dialog.id}{handle})"


# --- resolution through the dialog catalog ---------------------------------------------------


async def resolve_target(target: Target, catalog: DialogCatalog) -> DialogInfo | FolderInfo:
    """Turn a target into one dialog or one folder of the account."""
    if target.kind == "folder":
        return await find_folder(target.text, catalog)
    if target.kind == "id":
        return await _dialog_by_id(int(target.value), catalog)
    if target.kind == "username":
        return await _dialog_by_username(target.text, catalog)
    return await _fuzzy(target.text, catalog)


async def find_folder(name: str, catalog: DialogCatalog) -> FolderInfo:
    """The folder called ``name`` (case-insensitively), or a unique fuzzy match."""
    folders = await catalog.list_folders()
    wanted = dialogs.normalize(name)
    for folder in folders:
        if dialogs.normalize(folder.title) == wanted:
            return folder
    found = [m for m in dialogs.match(name, [], folders) if m.folder is not None]
    if len(found) == 1:
        assert found[0].folder is not None
        return found[0].folder
    if found:
        raise AmbiguousTarget(name, [describe_match(m) for m in found])
    known = ", ".join(repr(folder.title) for folder in folders) or "none"
    raise UnknownTarget(f"no folder named {name!r} (folders: {known})")


async def folder_dialogs(folder: FolderInfo, catalog: DialogCatalog) -> list[DialogInfo]:
    """Every chat the folder shows, including explicit peers missing from the dialog list."""
    members = [info for info in await catalog.list_dialogs() if folder.title in info.folders]
    listed = {info.id for info in members}
    explicit = (folder.include_ids | folder.pinned_ids) - folder.exclude_ids - listed
    for extra in sorted(explicit):
        try:
            entity = await catalog.entity(extra)
        except ENTITY_ERRORS as exc:
            log.warning("folder %r: cannot resolve peer %s: %s", folder.title, extra, exc)
            continue
        members.append(dialogs.dialog_info(entity, [folder.title]))
    return members


async def _dialog_by_id(marked_id: int, catalog: DialogCatalog) -> DialogInfo:
    for info in await catalog.list_dialogs():
        if info.id == marked_id:
            return info
    try:
        entity = await catalog.entity(marked_id)
    except ENTITY_ERRORS as exc:
        raise UnknownTarget(f"no dialog with id {marked_id}: {exc}") from exc
    return dialogs.dialog_info(entity)


async def _dialog_by_username(name: str, catalog: DialogCatalog) -> DialogInfo:
    wanted = name.casefold()
    for info in await catalog.list_dialogs():
        if info.username and info.username.casefold() == wanted:
            return info
    try:
        entity = await catalog.entity(f"@{name}")
    except ENTITY_ERRORS as exc:
        raise UnknownTarget(f"no chat with username @{name}: {exc}") from exc
    return dialogs.dialog_info(entity)


async def _fuzzy(text: str, catalog: DialogCatalog) -> DialogInfo | FolderInfo:
    found = dialogs.match(text, await catalog.list_dialogs(), await catalog.list_folders())
    if not found:
        raise UnknownTarget(
            f'nothing matches {text!r}; try `grepogram dialogs "{text}"` or an @username / id'
        )
    return _pick_unique(text, found, lambda m: m.score, describe_match).entry


def _pick_unique[T](
    text: str,
    ranked: Sequence[T],
    score_of: Callable[[T], float],
    describe: Callable[[T], str],
) -> T:
    """The one candidate ``text`` names, best first: an exact score wins over every fuzzy rival,
    and anything else with rivals is an :class:`AmbiguousTarget`."""
    exact = [item for item in ranked if score_of(item) >= dialogs.EXACT_SCORE]
    if len(ranked) > 1 and len(exact) != 1:
        raise AmbiguousTarget(text, [describe(item) for item in ranked])
    return exact[0] if exact else ranked[0]


# --- add / remove ----------------------------------------------------------------------------


async def add_source(
    cfg: Config,
    target: Target,
    catalog: DialogCatalog,
    *,
    since: str | None = None,
    comments: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> Added:
    """Resolve ``target`` and append it to ``cfg.sources``; the caller saves the config.

    ``catalog`` reads ``account``'s dialogs and the source is that account's. A target that
    carries an ``<account>/`` prefix naming another account is refused rather than resolved
    through the wrong session.

    Chats are stored as ``@username`` when they have one (readable in ``config.toml``) and as
    the marked id otherwise. Folders are stored by their exact title. An entry that is already
    present for the same account — by id or by another spelling of the same chat — raises
    :class:`DuplicateSource`; another account's source for the same chat is not a duplicate.
    """
    if target.account is not None and target.account != account:
        raise InvalidTarget(
            f"{target.text!r} is named as a source of account {target.account}, but it is being "
            f"added for account {account}"
        )
    resolved = await resolve_target(target, catalog)
    normalized_since = _since(since)
    if isinstance(resolved, FolderInfo):
        source = Source(
            folder=resolved.title, since=normalized_since, comments=comments, account=account
        )
        members = await folder_dialogs(resolved, catalog)
        dialog = None
    else:
        dialog = resolved
        if comments and dialog.type != "channel":
            raise SourceError(
                f"comments applies to channels only; {dialog.title!r} is a {dialog.type}"
            )
        source = Source(
            chat=chat_value(dialog), since=normalized_since, comments=comments, account=account
        )
        members = [dialog]
    updated = with_source(cfg, source, dialog)
    log.info("adding source %s (%s)", source.id, resolved.title)
    return Added(
        config=updated,
        source=source,
        title=resolved.title,
        dialogs=members,
        folder=resolved if isinstance(resolved, FolderInfo) else None,
    )


def with_source(cfg: Config, source: Source, dialog: DialogInfo | None) -> Config:
    """``cfg`` with ``source`` appended; :class:`DuplicateSource` when its account already has
    it — by id or, given the ``dialog`` a chat source resolved to, under another spelling of that
    chat. The same chat under another account is a second source, not a duplicate: each account
    fetches what it reaches, and a shared chat is stored once whichever does.

    Pure, so a caller that resolved the source over the network can re-read the config right
    before saving and apply the source to that, not to the snapshot it started from.
    """
    _reject_duplicate(cfg, source, dialog)
    return dataclasses.replace(cfg, sources=[*cfg.sources, source])


def chat_value(dialog: DialogInfo) -> str | int:
    """The ``chat =`` value stored for a dialog: ``@username`` when public, else the marked id."""
    return f"@{dialog.username}" if dialog.username else dialog.id


def parse_since(value: str | None) -> dt.date | None:
    """A source's ``since`` as a date, ``None`` when it is not set.

    Raises :class:`ValueError` for anything else; each caller wraps it in the error its layer
    reports — :class:`InvalidTarget` while a target is being added, ``ConfigError`` in
    :func:`grepogram.sync.since_of` when a stored config turns out to hold a bad one.
    """
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"since must be an ISO date (YYYY-MM-DD), got {value!r}") from None


def _since(value: str | None) -> str | None:
    try:
        day = parse_since(value)
    except ValueError as exc:
        raise InvalidTarget(str(exc)) from None
    return None if day is None else day.isoformat()


def _reject_duplicate(cfg: Config, source: Source, dialog: DialogInfo | None) -> None:
    for existing in cfg.sources:
        if existing.id == source.id:
            raise DuplicateSource(f"{source.id} is already a source")
        if existing.account != source.account:
            continue
        if dialog is not None and existing.chat is not None and _same_chat(existing, dialog):
            raise DuplicateSource(f"{dialog.title!r} is already a source as {existing.id}")


def _same_chat(existing: Source, dialog: DialogInfo) -> bool:
    target = _target_of(str(existing.chat))
    return target is not None and _names_chat(target, dialog.id, dialog.username)


def _target_of(value: str) -> Target | None:
    """``value`` parsed as a target; ``None`` when it is not one (an invite link, a bad handle)."""
    try:
        return parse_target(value)
    except InvalidTarget:
        return None


def _names_chat(target: Target, peer_id: int, username: str | None) -> bool:
    """Whether ``target`` is this very chat: its marked id (``peer_id``, Telegram's — never a
    row id, which a scoped chat may hold a synthetic one of), or its ``@username`` in any case.

    Identity, never spelling. ``chat =`` takes an id, an ``@username``, ``https://t.me/<name>``
    and ``t.me/c/<id>`` alike, and :func:`parse_target` has already folded all four into those
    two shapes, so every documented form answers the same. A fuzzy value — a title typed into
    ``config.toml`` by hand — names no identity without the dialog catalog and matches nothing.
    """
    if target.kind == "id":
        return target.value == peer_id
    if target.kind == "username":
        return bool(username) and target.text.casefold() == str(username).casefold()
    return False


def same_target(one: Target, other: Target) -> bool:
    """Whether two parsed targets name the same chat, whatever they were spelled as.

    Used wherever a configured ``chat =`` value has to be recognised in a target the user typed
    (:func:`_named_source`, :func:`grepogram.filters._names_source`); folder and fuzzy targets
    name no identity and match nothing here.
    """
    if one.kind != other.kind:
        return False
    if one.kind == "id":
        return one.value == other.value
    if one.kind == "username":
        return one.text.casefold() == other.text.casefold()
    return False


def remove_source(cfg: Config, conn: sqlite3.Connection, target: Target) -> Removed:
    """Drop the source ``target`` names and delete every chat nothing else covers any more.

    The target may be a source id (``folder:Argentina``, ``chat:@arg_chat``,
    ``work/chat:@arg_chat``), a folder name, a chat id / ``@username`` / link, or a fuzzy name
    matched against source entries and the titles of their indexed chats. A chat entry answers
    to every spelling of the same chat, its own included: what is compared is the identity a
    target resolves to, never the text ``config.toml`` happens to hold. Naming a chat that came
    in through a folder, or a channel's discussion group indexed through the channel's source,
    is refused: that would silently remove the whole folder or the channel
    (:func:`_refuse_indirect`). The caller holds the sync lock, so no sync writes to the chats
    being deleted.

    **A chat is deleted only when no source left in the config covers it.** Several can — a
    channel two accounts each configured, a chat both a folder and a ``chat:`` entry list —
    and ``chat_sources`` records which (:func:`resolve_sources`). A chat another configured
    source still covers keeps everything indexed from it and only changes its primary owner
    (``chats.source_id``): to the first remaining covering source in config order, or — for a
    channel's discussion group known only through the link — to its channel's source
    (:func:`discussion_source_id`), which is why discussion groups are decided after the
    channels. A new owner is always a configured source, never an ``import:`` tag
    (:func:`imported_tag`): an imported chat has no other source and goes with its own. The
    removed source leaves every chat's coverage, the kept ones included.

    Deleting a discussion group takes the comments it fed to a channel's post threads with it
    (:func:`grepogram.db.delete_chat`), so the index never quotes rows this removed.
    """
    return remove_source_id(cfg, conn, find_source(cfg, conn, target))


def remove_source_id(cfg: Config, conn: sqlite3.Connection, source_id: str) -> Removed:
    """:func:`remove_source` for a source id already known exactly — a ``[[sources]]`` entry's
    own :attr:`~grepogram.models.Source.id`, as ``accounts rm`` walks an account's sources —
    with no target to parse or match. The same rules decide what is deleted and what stays."""
    source = next((s for s in cfg.sources if s.id == source_id), None)
    remaining = [s for s in cfg.sources if s.id != source_id]
    rank = {s.id: position for position, s in enumerate(remaining)}
    deleted: list[int] = []
    kept: list[int] = []
    with db.transaction(conn):
        owned = db.list_chats(conn, source_id=source_id)
        for chat in sorted(owned, key=lambda c: (c.discussion_of is not None, c.id)):
            successor = _successor(conn, chat, source_id, rank)
            if successor is None:
                db.delete_chat(conn, chat.id)
                deleted.append(chat.id)
            else:
                db.set_primary_source(conn, chat.id, successor)
                kept.append(chat.id)
        db.set_source_chats(conn, source_id, [])
    log.info(
        "removed source %s with %d chats; %d stay under another source",
        source_id,
        len(deleted),
        len(kept),
    )
    return Removed(
        config=dataclasses.replace(cfg, sources=remaining),
        source_id=source_id,
        source=source,
        chat_ids=sorted(deleted),
        kept_chat_ids=sorted(kept),
    )


def _successor(
    conn: sqlite3.Connection, chat: ChatRow, removed: str, rank: Mapping[str, int]
) -> str | None:
    """The configured source that owns ``chat`` once ``removed`` is gone, ``None`` when none
    covers it and it goes too. ``rank`` orders the sources left in the config."""
    if (chat.source_id or "").startswith(IMPORT_PREFIX):
        return None
    if chat.discussion_of is not None:
        channel = db.get_chat(conn, chat.discussion_of)
        linked = None if channel is None else discussion_source_id(chat, channel)
        if linked is not None and linked != removed and linked in rank:
            return linked
    covering = [s for s in db.chat_source_ids(conn, chat.id) if s in rank]
    return min(covering, key=rank.__getitem__) if covering else None


def find_source(cfg: Config, conn: sqlite3.Connection, target: Target) -> str:
    """The source id ``target`` refers to, among configured entries, ``chats.source_id`` and
    the coverage ``chat_sources`` records.

    Sources of every account are candidates. A target with an ``<account>/`` prefix
    (:attr:`Target.account`) means that account's alone; one without means the default
    account's when it has a match and any account's otherwise, and two accounts' matches with
    none of the default's are an :class:`AmbiguousTarget` listing both ids (:func:`_in_account`).

    An ``import:<slug>`` is matched exactly, like a ``folder:`` name and unlike everything else
    here: it names no ``[[sources]]`` entry and there is nothing to resolve it through, and the
    fuzzy fallback scores the *whole typed string* against the bare slug — with a seven-character
    prefix in front of it, no slug of five characters or fewer could ever reach
    :data:`grepogram.dialogs.FUZZY_MIN_RATIO`. An imported chat titled "Mama" or "Дом" could
    therefore not be removed by any spelling, which is the one command every guard this feature
    added tells the user to run (:func:`refuse_imported`, :func:`_refuse_live`,
    :func:`grepogram.sync.link_discussion_chat` and :func:`resolve_sources`' log line).
    """
    chats = db.list_chats(conn)
    known = [s.id for s in cfg.sources]
    stored = {c.source_id for c in chats if c.source_id}
    stored |= {s for ids in db.chat_sources_map(conn).values() for s in ids}
    known += sorted(stored - set(known))
    if target.kind == "folder":
        return _folder_source(target, known)
    if target.kind in ("id", "username"):
        named = _named_source(target, known)
        if named is not None:
            return named
        return _indexed_source(chats, target)
    if target.text.casefold().startswith(IMPORT_PREFIX):
        return _import_source(target.text, known)
    return _fuzzy_source(target.text, known, chats)


def _named_source(target: Target, known: Sequence[str]) -> str | None:
    """The ``chat:`` entry that names the chat ``target`` names, under either one's spelling.

    This is what finds a source that was never synced: with no ``chats`` row to resolve the
    target through, the configured entries are all there is to match it against. Only entries of
    the target's own kind can answer — nothing maps an ``@username`` to a marked id offline — so
    a chat that *is* indexed still falls through to :func:`_indexed_source`, which knows both.
    """
    found: list[str] = []
    for source_id in known:
        bare = split_source_id(source_id)[1]
        if not bare.casefold().startswith(CHAT_PREFIX):
            continue
        other = _target_of(bare)
        if other is not None and same_target(other, target):
            found.append(source_id)
    found = _in_account(found, source_account, target.account)
    if len(found) > 1:
        raise AmbiguousTarget(target.text, found)
    return found[0] if found else None


def _indexed_source(chats: Sequence[ChatRow], target: Target) -> str:
    """The source of the indexed chat ``target`` names; refused when that source covers more.

    An id is Telegram's marked id or the row id a search result carries, which differ only for a
    scoped chat under a synthetic id; a private chat two accounts both hold is two rows of one
    peer, told apart by the account (:func:`_in_account`).
    """
    if target.kind == "id":
        label = f"id {target.value}"
        matched = [c for c in chats if target.value in (c.peer_id, c.id)]
    else:
        label = f"@{target.text}"
        wanted = target.text.casefold()
        matched = [c for c in chats if (c.username or "").casefold() == wanted]
    matched = _in_account(matched, _chat_account, target.account)
    if len(matched) > 1:
        raise AmbiguousTarget(
            label, [f"{c.title!r} (id {c.id}) through {c.source_id or '-'}" for c in matched]
        )
    return _chat_source(matched[0] if matched else None, label)


def _chat_account(chat: ChatRow) -> str:
    """The account a stored chat belongs to for a lookup: a scoped chat's own, a shared chat's
    primary source's."""
    return chat.scope or source_account(chat.source_id or "")


def _folder_source(target: Target, known: list[str]) -> str:
    name = target.text
    wanted = dialogs.normalize(name)
    folders = [
        (s, bare[len(FOLDER_PREFIX) :])
        for s, bare in ((s, split_source_id(s)[1]) for s in known)
        if bare.startswith(FOLDER_PREFIX)
    ]
    exact = _in_account(
        [s for s, title in folders if dialogs.normalize(title) == wanted],
        source_account,
        target.account,
    )
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise AmbiguousTarget(name, exact)
    hits = _in_account(
        [s for s, title in folders if dialogs.score(wanted, title) > 0],
        source_account,
        target.account,
    )
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise AmbiguousTarget(name, hits)
    raise UnknownSource(f"no folder source named {name!r} (sources: {', '.join(known) or 'none'})")


def _import_source(text: str, known: list[str]) -> str:
    """The ``import:`` id ``text`` names, matched the way :func:`_folder_source` matches a folder.

    Exactly first, on the normalized slug, so ``import:x-123`` reaches the chat of that name and
    not the ``import:x-123-123`` that :func:`import_source_ids` derived beside it; only then a
    score over the other import ids, which is what answers a half-remembered slug with a single
    candidate or with the list to choose from.
    """
    wanted = dialogs.normalize(text[len(IMPORT_PREFIX) :])
    imports = [s for s in known if s.casefold().startswith(IMPORT_PREFIX)]
    for source_id in imports:
        if dialogs.normalize(source_id[len(IMPORT_PREFIX) :]) == wanted:
            return source_id
    hits = [s for s in imports if dialogs.score(wanted, s[len(IMPORT_PREFIX) :]) > 0]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise AmbiguousTarget(text, hits)
    raise UnknownSource(
        f"no imported source named {text!r} (sources: {', '.join(known) or 'none'})"
    )


def _chat_source(chat: ChatRow | None, label: str) -> str:
    if chat is None or not chat.source_id:
        raise UnknownSource(f"{label} is not an indexed chat")
    _refuse_indirect(chat, label)
    return chat.source_id


def _refuse_indirect(chat: ChatRow, label: str) -> None:
    """Refuse a target that names a chat whose source covers more than that chat: a member of a
    folder source, or a channel's discussion group indexed through the channel's source — the
    group a channel was unlinked from included, which keeps that source until it is removed
    (:func:`discussion_source_id` owns that rule).

    An ``import:<slug>`` covers nothing but this chat and is therefore not indirect:
    :func:`import_source_ids` gives every colliding chat a tag of its own, so the tag and the
    chat are one to one and removing it removes exactly what was named. Refusing it sent the
    user to ``sources rm import:<slug>`` for a chat they had just named a perfectly good way.
    """
    if not chat.source_id:
        return
    if split_source_id(chat.source_id)[1].startswith(FOLDER_PREFIX):
        raise SourceError(
            f"{label} is indexed through {chat.source_id}; remove that folder source instead "
            "or take the chat out of the folder in Telegram"
        )
    if chat.source_id.startswith(IMPORT_PREFIX) or _own_source(chat):
        return
    owner = (
        ""
        if chat.discussion_of is None
        else f"the discussion group of channel {chat.discussion_of}, "
    )
    raise SourceError(
        f"{label} is {owner}indexed through {chat.source_id}; remove that source instead"
    )


def _own_source(chat: ChatRow) -> bool:
    """Whether ``chat.source_id`` is a ``chat:`` entry naming this very chat.

    Read by identity (:func:`_names_chat`), so the id, the ``@username`` and the two ``t.me``
    link forms of one chat all count as covering it directly — a group configured as
    ``chat = "https://t.me/..."`` owns its rows exactly as one configured by id does.
    """
    source_id = split_source_id(chat.source_id or "")[1]
    if not source_id.casefold().startswith(CHAT_PREFIX):
        return False
    target = _target_of(source_id)
    return target is not None and _names_chat(target, chat.peer_id, chat.username)


def _fuzzy_source(text: str, known: list[str], chats: list[ChatRow]) -> str:
    query = dialogs.normalize(text)
    best: dict[str, tuple[float, bool, ChatRow | None]] = {}

    def consider(source_id: str, name: str | None, chat: ChatRow | None) -> None:
        """Record the best match per source; a hit on the source's own name beats one on a
        chat it indexes (``chat`` is ``None`` for the former)."""
        value = dialogs.score(query, name) if name else 0.0
        if value <= 0:
            return
        current = best.get(source_id)
        if current is None or (value, chat is None) > current[:2]:
            best[source_id] = (value, chat is None, chat)

    for source_id in known:
        consider(source_id, source_id.split(":", 1)[1], None)
    for chat in chats:
        if chat.source_id:
            consider(chat.source_id, chat.title, chat)
            consider(chat.source_id, f"@{chat.username}" if chat.username else None, chat)
    if not best:
        raise UnknownSource(f"no source matches {text!r} (sources: {', '.join(known) or 'none'})")
    ranked = sorted(best, key=lambda s: (-best[s][0], s))
    winner = _pick_unique(text, ranked, lambda s: best[s][0], str)
    matched = best[winner][2]
    if matched is not None:
        _refuse_indirect(matched, repr(text))
    return winner


# --- resolution on sync ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Resolution:
    """What :func:`resolve_sources` made of the configured sources.

    ``chats`` are the rows a sync fetches: every chat a source resolved to, and every chat whose
    primary source did not resolve this run (it keeps that source, and a sync still reaches it
    through whichever account can). ``flooded`` maps an account whose resolve Telegram stopped
    with a flood wait to the seconds asked, and ``failed`` one another Telegram error stopped to
    the error: that account's sources were all skipped, keeping what they covered.
    """

    chats: list[ChatRow] = dataclasses.field(default_factory=list)
    flooded: dict[str, int] = dataclasses.field(default_factory=dict)
    failed: dict[str, str] = dataclasses.field(default_factory=dict)


async def resolve_sources(
    cfg: Config, clients: Mapping[str, Any], conn: sqlite3.Connection
) -> Resolution:
    """Upsert a ``chats`` row for every chat the configured sources currently cover.

    ``clients`` maps an account to its connected client, and each source is resolved through
    its own account's (one :class:`~grepogram.dialogs.DialogCatalog` per account): folder
    membership and entities are re-read from Telegram each time. Before an account's first
    source, its client's session is handed every access hash the index stores for it
    (:func:`seed_peers`), so a chat outside its dialog list — a ``chat:<id>`` of a group it no
    longer shows, a public chat it reads without joining — is addressed by that hash rather than
    not at all, and a ``chat = "@name"`` source whose chat the index already holds is re-read by
    its id instead of by a ``contacts.resolveUsername`` on every sync (:func:`source_dialogs`).
    Sync state on existing rows is preserved, and so is a stored ``discussion_of``
    (:func:`grepogram.db.upsert_chat`): a channel's discussion group listed by a source is
    synced as a chat of its own and keeps holding the channel's comments.

    Every row is stored through the account whose source reached it — a private chat or legacy
    group as that account's own row, a channel or supergroup as the one shared row
    (:func:`grepogram.models.chat_scope`) — never under the default account by omission. A chat
    covered by several sources keeps the first one (in config order) as its primary
    ``source_id``; each covering source is recorded in ``chat_sources`` and each account that
    reached it in ``chat_access``, with the access hash its client addresses the chat by. The
    coverage of a source that resolved is replaced by what it lists now, the chats a folder
    names but whose entity would not resolve included (they are still listed).

    **A source that did not resolve keeps what it had** — an error says nothing about what it
    covers — and that includes being the *primary* of the chats it owns: they are returned as
    they are stored, so a later source covering the same chat never takes it over for one run
    and hands it back the next. That holds for every way a source can fail to resolve: its
    account has no client in this run (not signed in, or not part of it), the chat or folder no
    longer resolves (:class:`SourceError`), or Telegram stopped its account's resolve — a flood
    wait (``flooded``) or another Telegram error (``failed``), after which every other source
    of that account is skipped the same way rather than asked again. A rejected session is
    re-raised as :class:`~grepogram.tg.AuthRequired` naming its account
    (:func:`~grepogram.tg.reraise_unauthorized`).

    **A chat held as an ``import:`` is left alone**, logged and not returned. ``upsert_chat``
    writes ``source_id`` unconditionally, so without this a resolve would quietly replace
    ``import:<slug>`` with the live source's id — and every protection keyed on that prefix would
    go with it, :func:`prunable` offering the whole imported history for deletion the moment the
    folder stopped listing the chat. :func:`refuse_imported` guards the two commands that *add* a
    source, but nothing guards the folder gaining that chat on Telegram afterwards, and this runs
    on every sync. Taking the chat over is a deliberate act: ``sources rm import:<slug>`` first.

    That last one is logged at INFO rather than WARNING: it is a standing state of the index, not
    an event, and it would otherwise be printed once per held chat on every sync — including
    every automatic one inside an MCP ``search``.
    """
    catalogs: dict[str, DialogCatalog] = {}
    rows: list[ChatRow] = []
    stored: dict[tuple[str, int], ChatRow] = {}
    held_back: set[tuple[str, int]] = set()
    coverage: dict[str, set[int]] = {}
    flooded: dict[str, int] = {}
    failed: dict[str, str] = {}
    now = int(time.time())

    def keep(source: Source) -> None:
        _keep_primary(conn, source, stored, rows)

    for source in cfg.sources:
        client = clients.get(source.account)
        if client is None:
            log.warning(
                "skipping source %s: account %s has no signed-in client in this run",
                source.id,
                source.account,
            )
            keep(source)
            continue
        if source.account in flooded or source.account in failed:
            log.warning(
                "skipping source %s: account %s stopped resolving", source.id, source.account
            )
            keep(source)
            continue
        catalog = catalogs.get(source.account)
        if catalog is None:
            seed_peers(client, db.stored_peers(conn, source.account))
            catalog = catalogs[source.account] = DialogCatalog(client)
        try:
            infos, named = await _source_listing(source, catalog, conn)
        except SourceError as exc:
            log.warning("skipping source %s: %s", source.id, exc)
            keep(source)
            continue
        except errors.UnauthorizedError as exc:
            tg.reraise_unauthorized(exc, source.account)
        except errors.FloodWaitError as exc:
            log.warning(
                "flood wait of %ss resolving source %s; skipping every source of account %s",
                exc.seconds,
                source.id,
                source.account,
            )
            flooded[source.account] = int(exc.seconds)
            keep(source)
            continue
        except errors.RPCError as exc:
            log.warning(
                "resolving source %s failed: %s; skipping every source of account %s",
                source.id,
                exc,
                source.account,
            )
            failed[source.account] = str(exc)
            keep(source)
            continue
        covered = coverage.setdefault(source.id, set())
        covered |= _still_named(conn, source, named)
        for info in infos:
            key = (chat_scope(info.type, source.account), info.id)
            if key in held_back:
                continue
            chat = stored.get(key)
            if chat is None:
                held = imported_tag(conn, info.id, scope=key[0])
                if held is not None:
                    held_back.add(key)
                    log.info(
                        "chat %s (%s) is held as %s, a Telegram Desktop import; source %s does "
                        "not take it over — run `grepogram sources rm %s` first to sync it from "
                        "Telegram",
                        info.id,
                        info.title,
                        held,
                        source.id,
                        held,
                    )
                    continue
                chat = db.upsert_chat(conn, _source_chat(info, source), source.account)
                stored[key] = chat
                rows.append(chat)
            else:
                log.debug(
                    "chat %s is also covered by %s; %s stays its primary source",
                    info.id,
                    source.id,
                    chat.source_id,
                )
            covered.add(chat.id)
            db.set_chat_access(
                conn,
                chat.id,
                source.account,
                access_hash=catalog.access_hash(info.id),
                via="source",
                checked_at=now,
            )
    for source_id, chat_ids in coverage.items():
        db.set_source_chats(conn, source_id, chat_ids)
    log.info("resolved %d chats from %d sources", len(rows), len(cfg.sources))
    return Resolution(chats=rows, flooded=flooded, failed=failed)


def _keep_primary(
    conn: sqlite3.Connection,
    source: Source,
    stored: dict[tuple[str, int], ChatRow],
    rows: list[ChatRow],
) -> None:
    """Hold on to the chats ``source`` — one that did not resolve this run — is the primary
    source of: they join ``rows`` as they are stored and are claimed in ``stored``, so no later
    source in config order becomes their primary for this run alone."""
    for chat_id in db.source_chat_ids(conn, source.id):
        chat = db.get_chat(conn, chat_id)
        if chat is None or chat.source_id != source.id:
            continue
        key = (chat.scope, chat.peer_id)
        if key in stored:
            continue
        stored[key] = chat
        rows.append(chat)


def seed_peers(client: Any, peers: Iterable[tuple[int, int | None]]) -> set[int]:
    """Hand ``client``'s session the access hashes of ``peers`` — ``(marked id, hash)`` pairs the
    index stored for this client's account — and return the marked ids now addressable.

    ``session.process_entities`` is the call Telethon feeds every answer through, so a peer
    seeded here is addressed by its bare id with no request at all. A legacy group counts
    without a hash (Telethon turns a ``PeerChat`` straight into an ``InputPeerChat``); any other
    peer without one is left out. The hash has to be this account's own: another account's would
    address the peer as a different user, and Telegram refuses it.
    """
    ready: set[int] = set()
    inputs: list[Any] = []
    for peer, access_hash in peers:
        if peer in ready:
            continue
        bare, kind = utils.resolve_id(peer)
        if kind is types.PeerChat:
            ready.add(peer)
            continue
        if access_hash is None:
            continue
        if kind is types.PeerChannel:
            inputs.append(types.InputPeerChannel(bare, access_hash))
        else:
            inputs.append(types.InputPeerUser(bare, access_hash))
        ready.add(peer)
    if inputs:
        client.session.process_entities(inputs)
        log.debug("seeded %d stored access hashes", len(inputs))
    return ready


def _source_chat(info: DialogInfo, source: Source) -> ChatRow:
    """The row ``source`` proposes for a chat it lists, filed under the source's account."""
    return ChatRow(
        id=info.id,
        peer_id=info.id,
        scope=chat_scope(info.type, source.account),
        type=info.type,
        title=info.title,
        username=info.username,
        is_forum=info.is_forum,
        source_id=source.id,
    )


async def _source_listing(
    source: Source, catalog: DialogCatalog, conn: sqlite3.Connection
) -> tuple[list[DialogInfo], frozenset[int]]:
    """What ``source`` covers now, plus the peers a folder names outright (resolvable or not)."""
    if source.folder is None:
        return await source_dialogs(source, catalog, conn), frozenset()
    folder = await find_folder(source.folder, catalog)
    named = (folder.include_ids | folder.pinned_ids) - folder.exclude_ids
    return await folder_dialogs(folder, catalog), frozenset(named)


def _still_named(conn: sqlite3.Connection, source: Source, named: frozenset[int]) -> set[int]:
    """The chats ``source`` covered so far whose peer its folder still names outright.

    :func:`folder_dialogs` drops a named peer whose entity Telegram will not hand over — a
    channel gone private, say — and such a peer is still very much listed by the folder, so its
    coverage stays; dropping it would let ``sources rm`` of another source delete a chat this
    folder still holds.
    """
    if not named:
        return set()
    kept: set[int] = set()
    for chat_id in db.source_chat_ids(conn, source.id):
        chat = db.get_chat(conn, chat_id)
        if chat is not None and chat.peer_id in named:
            kept.add(chat_id)
    return kept


def imported_tag(conn: sqlite3.Connection, chat_id: int, *, scope: str | None = None) -> str | None:
    """The ``import:<slug>`` this chat is held under, ``None`` when it is not an import.

    The one question every writer of ``chats.source_id`` has to ask before it writes, because
    :func:`grepogram.db.upsert_chat` overwrites the column: :func:`resolve_sources` on every
    sync, :func:`refuse_imported` for the two commands that add a source, and
    :func:`grepogram.sync.link_discussion_chat` for a channel whose discussion group turns out
    to be one.

    ``chat_id`` is a row id; with ``scope`` it is a Telegram peer id instead, looked up under
    that scope (:func:`grepogram.db.get_chat_by_peer`), which is how a caller holding only what
    Telegram answered asks about the row that answer would be stored in.
    """
    stored = (
        db.get_chat(conn, chat_id) if scope is None else db.get_chat_by_peer(conn, chat_id, scope)
    )
    source_id = "" if stored is None else (stored.source_id or "")
    return source_id if source_id.startswith(IMPORT_PREFIX) else None


async def source_dialogs(
    source: Source, catalog: DialogCatalog, conn: sqlite3.Connection | None = None
) -> list[DialogInfo]:
    """The chats one source covers right now.

    With ``conn``, a ``chat = "@name"`` source whose chat the index already holds is re-read by
    its stored id (:func:`_stored_handle`) before anything asks Telegram to resolve the name.
    """
    if source.folder is not None:
        return await folder_dialogs(await find_folder(source.folder, catalog), catalog)
    target = parse_target(str(source.chat))
    if conn is not None and target.kind == "username":
        known = await _stored_handle(target.text, source, catalog, conn)
        if known is not None:
            return [known]
    resolved = await resolve_target(target, catalog)
    if isinstance(resolved, FolderInfo):
        raise UnknownTarget(
            f"chat {source.chat!r} names the folder {resolved.title!r}; "
            f"use folder = {resolved.title!r} instead"
        )
    return [resolved]


async def _stored_handle(
    name: str, source: Source, catalog: DialogCatalog, conn: sqlite3.Connection
) -> DialogInfo | None:
    """The chat ``@name`` is, read without ``contacts.resolveUsername`` when that can be done.

    A dialog of the account answers first, as it always did. Otherwise a chat this source
    already covers under the same handle, with an access hash stored for the account (and so
    seeded into its session by :func:`resolve_sources`), is read by that id — a public channel
    the account follows without joining has no dialog, and resolving its name on every sync, the
    automatic one inside a search included, is the request Telegram rate-limits hardest. The
    answer counts only while the chat still holds the handle; ``None`` sends the caller to
    resolve the name after all, which is also what a handle that moved to another chat needs.
    """
    wanted = name.casefold()
    for info in await catalog.list_dialogs():
        if info.username and info.username.casefold() == wanted:
            return info
    for chat_id in db.source_chat_ids(conn, source.id):
        chat = db.get_chat(conn, chat_id)
        if chat is None or (chat.username or "").casefold() != wanted:
            continue
        if db.access_hash(conn, chat.id, source.account) is None:
            continue
        try:
            info = dialogs.dialog_info(await catalog.entity(chat.peer_id))
        except ENTITY_ERRORS as exc:
            log.debug("chat %s: its stored access hash did not resolve: %s", chat.id, exc)
            return None
        return info if (info.username or "").casefold() == wanted else None
    return None


def discussion_source_id(group: ChatRow | None, channel: ChatRow) -> str | None:
    """The ``source_id`` a channel's discussion group carries once the link is applied.

    One rule decides who owns a group's rows — and therefore what a ``sources rm`` of that source
    deletes:

    * a group a source covers directly keeps that source, whether it is a folder holding it or a
      ``chat:`` entry naming it — by identity, so the id, the ``@username`` and both ``t.me``
      link forms of one group all count (:func:`_own_source`); :func:`resolve_sources` writes the
      same id on every run, and the link never overwrites it;
    * a group known only through a channel's link belongs to the source of the channel that links
      it *now*, so a group handed from channel X to channel Y moves to Y's source together with
      the link — removing X then leaves the group alone and removing Y takes it along, which is
      where its comments came from;
    * a group a channel was unlinked from keeps the source it came in through until that source
      is removed: the comments stored under it were indexed through that source and go with it;
    * a group held as an ``import:`` keeps that tag, like every other writer of ``source_id``
      (:func:`imported_tag`). :func:`grepogram.sync.link_discussion_chat` refuses the link
      before it ever gets here, so this is the invariant rather than the guard — but the rule
      belongs with the rule it is part of, and a future caller inherits it.

    A channel with no source of its own (never resolved, only stored) changes nothing.
    :func:`_refuse_indirect` reads the same rule from the other end — only a group that is its
    own source can be removed by naming it — and :func:`grepogram.db.delete_chat` completes it:
    whichever source owns the group, removing it drops the comments the group fed to the
    channel's post threads along with the rows they quote.
    """
    if group is None or not group.source_id:
        return channel.source_id
    bare = split_source_id(group.source_id)[1]
    if bare.startswith((FOLDER_PREFIX, IMPORT_PREFIX)) or _own_source(group):
        return group.source_id
    return channel.source_id or group.source_id


# --- prune -----------------------------------------------------------------------------------


async def folder_membership(cfg: Config, catalogs: Mapping[str, DialogCatalog]) -> FolderMembership:
    """Read what every folder source lists right now, each through the catalog of its own
    account (``catalogs``, whose clients must be connected). A folder source whose account has
    no catalog — not signed in — is recorded as failed: its folders are that account's, and no
    other account's folder of the same name says anything about them.

    The network half of ``sources prune``, and it differs from :func:`resolve_sources` in the
    one way that matters here: a source that does not resolve is *recorded* as failed instead of
    being logged and skipped. :func:`prunable` reads a chat's absence from its folder as "it
    left", so a transient ``RPCError`` swallowed in silence would offer that source's whole
    indexed history for deletion.

    A folder's membership is the chats it shows plus the explicit peers it names, resolvable or
    not: :func:`folder_dialogs` drops a peer whose entity Telegram will not hand over (a channel
    gone private, say), and such a peer is still very much listed by the folder.

    An ``UnauthorizedError`` is re-raised rather than recorded, the way
    :func:`grepogram.sync._sync_chats` re-raises it: every ``UnauthorizedError`` is an
    ``RPCError``, and a session revoked mid-scan is not a source Telegram would not answer for
    but a session that answers for none. A dead session is raised as
    :class:`~grepogram.tg.AuthRequired` naming the folder's account
    (:func:`~grepogram.tg.reraise_unauthorized`) — the clients of every account are connected at
    once, and left to unwind it would be claimed by whichever was connected last — so the user
    is told to sign *that* account in again instead of to try again once Telegram comes back.
    """
    listed: dict[str, set[int]] = {}
    failed: dict[str, str] = {}
    for source in cfg.sources:
        if source.folder is None:
            continue
        catalog = catalogs.get(source.account)
        if catalog is None:
            failed[source.id] = f"account {source.account} is not signed in"
            continue
        try:
            folder = await find_folder(source.folder, catalog)
            members = await folder_dialogs(folder, catalog)
        except errors.UnauthorizedError as exc:
            tg.reraise_unauthorized(exc, source.account)
        except (SourceError, errors.RPCError) as exc:
            log.warning("cannot check source %s: %s", source.id, exc)
            failed[source.id] = str(exc)
            continue
        named = (folder.include_ids | folder.pinned_ids) - folder.exclude_ids
        listed[source.id] = {info.id for info in members} | set(named)
    return FolderMembership(listed=listed, failed=failed)


def prunable(cfg: Config, conn: sqlite3.Connection, folders: FolderMembership) -> PruneScan:
    """The indexed chats whose folder source no longer lists them, with a reason for each.

    ``folders`` is what :func:`folder_membership` just read from Telegram, and it is the only
    evidence: a chat is offered when a folder source that *resolved* brought it in and no
    resolved source covers it any more.

    Four kinds of chat are kept instead, each with its reason:

    * one an ``import:`` source holds — an import has no dialog by definition, so no folder
      could ever list it;
    * one a ``chat:`` entry names, whatever its stored ``source_id`` says: a chat two sources
      cover keeps the first one's id (:func:`resolve_sources`), so the stored tag is no proof of
      who covers it now;
    * a channel's discussion group the channel still links — it is indexed through that link,
      not through a folder listing (:func:`discussion_source_id`);
    * one whose source is gone from the config; that is ``sources rm``'s business, and nothing
      here can resolve a source the config does not hold.

    A configured source that failed to resolve makes the whole scan inconclusive, and
    :attr:`PruneScan.prunable` comes back empty while :attr:`PruneScan.unresolved` is not:
    coverage is a union over every source, so one unchecked source means no chat can be *proved*
    uncovered. The caller reports that instead of deleting anything.
    """
    named = [
        (source.account, target)
        for source, target in ((s, _target_of(str(s.chat))) for s in cfg.sources if s.chat)
        if target is not None
    ]
    counts = db.message_counts(conn)
    unresolved = [f"{source_id}: {why}" for source_id, why in sorted(folders.failed.items())]
    offered: list[PruneCandidate] = []
    kept: list[PruneCandidate] = []

    def record(into: list[PruneCandidate], chat: ChatRow, reason: str) -> None:
        into.append(PruneCandidate(chat=chat, reason=reason, messages=counts.get(chat.id, 0)))

    for chat in db.list_chats(conn):
        source_id = chat.source_id or ""
        if not split_source_id(source_id)[1].startswith(FOLDER_PREFIX):
            continue  # an import and a chat entry name themselves; neither can leave a folder
        if any(
            chat.peer_id in ids and _reaches(chat, source_account(listed))
            for listed, ids in folders.listed.items()
        ) or any(
            _reaches(chat, account) and _names_chat(t, chat.peer_id, chat.username)
            for account, t in named
        ):
            continue
        if source_id in folders.failed:
            record(kept, chat, f"{source_id} could not be checked")
        elif source_id not in folders.listed:
            record(kept, chat, f"{source_id} is not a configured source; use sources rm")
        elif chat.discussion_of is not None and db.get_chat(conn, chat.discussion_of) is not None:
            record(kept, chat, f"still the discussion group of channel {chat.discussion_of}")
        else:
            record(offered, chat, f"{source_id} no longer lists it")
    if unresolved:
        log.warning("prune scan is inconclusive: %s", "; ".join(unresolved))
    return PruneScan(prunable=[] if unresolved else offered, kept=kept, unresolved=unresolved)


def _reaches(chat: ChatRow, account: str) -> bool:
    """Whether a source of ``account`` listing ``chat``'s peer means this very row: always for a
    shared chat, and for a private chat or legacy group only when it is that account's own."""
    return chat.is_shared or chat.scope == account


def prune_chats(conn: sqlite3.Connection, candidates: Sequence[PruneCandidate]) -> list[int]:
    """Delete the chats :func:`prunable` offered, with their messages, units and index rows.

    One transaction for the lot, and every candidate is put to :func:`_still_prunable` again
    inside it. The re-check is not belt and braces: the scan and the confirmation both happen
    *before* the sync lock is taken, because a network round trip must never be held across it,
    so the verdict this acts on can be seconds or minutes old and another process is free to
    have changed the chat in between — to have taken it over, linked it as a discussion group,
    or removed the live source and imported the same chat, which is the one that cannot be
    undone. What the offer rested on is re-read here instead of assumed. Returns the ids
    actually removed, which is how the caller notices that a candidate was dropped; each drop is
    logged with the reason.

    ``candidates`` and not ids for that reason: :attr:`PruneCandidate.chat` carries the
    ``source_id`` the offer was made under, which is what "unchanged" is measured against.
    """
    removed: list[int] = []
    with db.transaction(conn):
        for candidate in candidates:
            stored = db.get_chat(conn, candidate.chat.id)
            if stored is None or not _still_prunable(conn, stored, candidate):
                continue
            db.delete_chat(conn, stored.id)
            removed.append(stored.id)
    log.info("pruned %d chats", len(removed))
    return removed


def _still_prunable(conn: sqlite3.Connection, stored: ChatRow, candidate: PruneCandidate) -> bool:
    """Whether ``stored`` is still the chat :func:`prunable` offered, re-read under the lock.

    Three questions, in the order of what they cost if missed:

    * is it an import now? :func:`imported_tag` is the question every path that touches
      ``chats.source_id`` has to ask, and this one deletes rows rather than writing the column,
      which makes it the worst place to skip: an imported history has no dialog behind it and
      Telegram cannot hand it back. ``sources rm folder:X`` followed by an import of the same
      chat is all it takes, and neither command takes longer than a confirmation prompt;
    * does it still carry the source id it was offered under? The whole verdict was "*this*
      folder source brought it in and no resolved source covers it any more", so any other value
      in the column — another source's id after a sync resolved it, a folder entry rewritten —
      says the scan is describing a chat that no longer exists in that form;
    * is it a channel's discussion group now? :func:`prunable` keeps one for the reason that it
      is indexed through the link and not through a folder listing, and
      :func:`grepogram.sync.link_discussion_chat` can have made it one since.

    Everything here is a database read: this runs under the :class:`~grepogram.sync.SyncLock`,
    where a Telegram round trip has no business. Re-resolving the folders is what the next
    ``sources prune`` is for.
    """
    chat_id = stored.id
    held = imported_tag(conn, chat_id)
    if held is not None:
        log.warning(
            "chat %s (%s) is held as %s since the prune was scanned; it was not deleted — "
            "an imported history cannot be fetched again",
            chat_id,
            stored.title,
            held,
        )
        return False
    if (stored.source_id or "") != (candidate.chat.source_id or ""):
        log.warning(
            "chat %s (%s) was offered under source %s and now carries %s; it was not deleted",
            chat_id,
            stored.title,
            candidate.chat.source_id or "-",
            stored.source_id or "-",
        )
        return False
    if stored.discussion_of is not None and db.get_chat(conn, stored.discussion_of) is not None:
        log.warning(
            "chat %s (%s) is the discussion group of channel %s since the prune was scanned; "
            "it was not deleted",
            chat_id,
            stored.title,
            stored.discussion_of,
        )
        return False
    return True


# --- import ----------------------------------------------------------------------------------


def import_chats(
    conn: sqlite3.Connection, entries: Sequence[ImportedChat], account: str = DEFAULT_ACCOUNT
) -> list[Imported]:
    """Store a parsed Telegram Desktop export of ``account``, tagging each of its chats
    ``import:<slug>``.

    ``account`` is whose export it is, and it matters for the chats whose history is one
    account's own: a private chat, a bot or a legacy group is stored under that account's scope
    (:func:`grepogram.models.chat_scope`), beside — never over — another account's chat with
    the same peer, which may then take a synthetic row id; the messages are stored under the id
    the row got. A channel or supergroup is one row whoever exported it.

    The rows go in through :func:`grepogram.db.upsert_messages` like any other, so the caller
    rebuilds units, indexes and embeds them exactly as a sync does; nothing here derives
    anything. What is special is only the tag: an imported chat carries ``unavailable = 1`` and
    ``last_msg_id = 0`` (:class:`grepogram.tdesktop.ImportedChat` sets both), so no sync ever
    resumes from it, and its ``import:`` source id is what :func:`prunable` and
    :func:`grepogram.sync.prune_deleted` recognise to leave the history alone — neither a folder
    nor Telegram can be asked about a chat the account can no longer open.

    Refused as a whole, before a single row is written, when any chat of the export is already
    indexed from Telegram (:class:`ImportConflict`): :func:`grepogram.db.upsert_chat` overwrites
    ``source_id``, so the import would retag a live chat and hide it from the source that fetches
    it. A chat already stored as an import is not a conflict — re-running an import is how a
    partial one is finished, and it is idempotent because the ids come from the export.
    """
    rows = [
        dataclasses.replace(entry.chat, scope=chat_scope(entry.chat.type, account))
        for entry in entries
    ]
    _refuse_live(conn, rows)
    source_ids = import_source_ids(conn, rows)
    stored: list[Imported] = []
    with db.transaction(conn):
        for entry, row in zip(entries, rows, strict=True):
            source_id = source_ids[row.id]
            chat = db.upsert_chat(conn, dataclasses.replace(row, source_id=source_id), account)
            messages = entry.messages
            if chat.id != row.id:
                messages = [dataclasses.replace(m, chat_id=chat.id) for m in messages]
            message_ids = db.upsert_messages(conn, messages)
            stored.append(Imported(chat=chat, source_id=source_id, messages=len(message_ids)))
    log.info(
        "imported %d chats and %d messages", len(stored), sum(item.messages for item in stored)
    )
    return stored


def refuse_imported(
    conn: sqlite3.Connection, covered: Sequence[DialogInfo], account: str = DEFAULT_ACCOUNT
) -> None:
    """Refuse a live source of ``account`` that would cover a chat this index holds as an import.

    :func:`grepogram.db.upsert_chat` writes ``source_id`` unconditionally, so the very next sync
    would replace ``import:<slug>`` with the new source's id — and every protection keyed on that
    prefix would go with it: :func:`prunable` would offer the imported history for deletion the
    moment a folder stopped listing the chat, and :func:`grepogram.sync.prune_deleted` would ask
    Telegram about ids it never had and delete the lot. Nothing below this point can tell an
    imported chat from a fetched one afterwards, so the two commands that add a source — ``sources
    add`` and the MCP tool of the same name — refuse by name here, before the config is saved.

    ``covered`` is what the source resolved to: the one chat, or every member of a folder. One
    imported chat in a folder refuses the whole folder, because adding it would cover that chat
    on every sync from now on.
    """
    for dialog in covered:
        source_id = imported_tag(conn, dialog.id, scope=chat_scope(dialog.type, account))
        if source_id is None:
            continue
        raise ImportConflict(
            f"{dialog.title!r} (id {dialog.id}) is already in the index as {source_id}, a "
            "Telegram Desktop import; a live source would take it over on the next sync and the "
            f"imported history would become prunable — remove it with `grepogram sources rm "
            f"{source_id}` first if you want this chat synced from Telegram instead"
        )


def import_source_ids(conn: sqlite3.Connection, chats: Sequence[ChatRow]) -> dict[int, str]:
    """The ``import:<slug>`` source id of every chat of one export, keyed by chat id.

    The slug is the title, so ``sources ls`` reads as something a human recognises and a second
    import of the same export writes the same id — which is what makes re-running one idempotent
    rather than a second copy under a fresh tag. A title that slugifies to nothing (an emoji-only
    name, a chat the export left unnamed) falls back to the chat's own id.

    Two chats can want the same slug — two contacts of one name in a single export, or a title
    an earlier import already claimed — and sharing a source id would make ``sources rm`` on
    either delete both, so every colliding chat carries its own id instead: the chat's own id is
    appended, and appended again while the result is *still* taken. That last loop is not
    theoretical — a chat titled ``x`` with id 123 and a chat titled ``x-123`` both land on
    ``import:x-123`` at the first attempt.

    The chats are walked in id order rather than the export's, so the answer depends only on the
    export and the index and is stable across runs.
    """
    slugs = {chat.id: import_slug(chat.title) or f"chat-{abs(chat.id)}" for chat in chats}
    shared = {slug for slug, count in collections.Counter(slugs.values()).items() if count > 1}
    identity = {chat.id: (chat.scope, chat.peer_id) for chat in chats}
    held = {
        str(chat.source_id): (chat.scope, chat.peer_id)
        for chat in db.list_chats(conn)
        if (chat.source_id or "").startswith(IMPORT_PREFIX)
    }
    ids: dict[int, str] = {}
    taken: set[str] = set()

    def claimed(source_id: str, chat_id: int) -> bool:
        """Whether ``source_id`` belongs to some other chat — in this export or in the index.

        A stored chat is the same chat when its Telegram identity (scope and peer) is, not its
        row id: an account's private chat may be stored under a synthetic one."""
        mine = identity[chat_id]
        return source_id in taken or held.get(source_id, mine) != mine

    for chat_id in sorted(slugs):
        slug = slugs[chat_id]
        source_id = f"{IMPORT_PREFIX}{slug}"
        if slug in shared or claimed(source_id, chat_id):
            source_id = f"{IMPORT_PREFIX}{slug}-{abs(chat_id)}"
        while claimed(source_id, chat_id):
            source_id = f"{source_id}-{abs(chat_id)}"
        taken.add(source_id)
        ids[chat_id] = source_id
    return ids


def import_slug(title: str | None) -> str:
    """``title`` as the readable half of an ``import:`` source id, ``""`` when it yields none.

    Letters and digits are kept as they are — a Cyrillic title stays Cyrillic, since the id is
    read by people and never typed into a URL — everything else separates words, and the result
    is one lowercase dash-joined run of at most :data:`IMPORT_SLUG_MAX` characters.
    """
    words: list[str] = []
    word = ""
    for char in (title or "").casefold():
        if char.isalnum():
            word += char
        elif word:
            words.append(word)
            word = ""
    if word:
        words.append(word)
    return "-".join(words)[:IMPORT_SLUG_MAX].strip("-")


def _refuse_live(conn: sqlite3.Connection, chats: Sequence[ChatRow]) -> None:
    """Refuse an import over a chat this index already syncs from Telegram, naming it.

    Anything stored under a source that is not an ``import:`` one counts, an untagged row
    included: the export is a snapshot of a history Telegram still serves, and importing it
    would both retag the chat and leave two writers over the same ``(chat_id, msg_id)`` rows.
    """
    for chat in chats:
        stored = db.get_chat_by_peer(conn, chat.peer_id, chat.scope)
        if stored is None or (stored.source_id or "").startswith(IMPORT_PREFIX):
            continue
        through = f"through {stored.source_id}" if stored.source_id else "with no source"
        raise ImportConflict(
            f"{stored.title or stored.type} (id {stored.id}) is already indexed from Telegram "
            f"{through}; remove that source with `grepogram sources rm` before importing an "
            "export of the same chat, or the import would take the chat over"
        )


# --- status ----------------------------------------------------------------------------------


def sources_status(cfg: Config, conn: sqlite3.Connection) -> list[SourceStatus]:
    """Per-source view of the indexed chats: configured sources first, in config order, then
    any source id still present in the database but gone from the config.

    A chat is listed under every source that covers it — its primary ``source_id`` and each one
    ``chat_sources`` records — so a channel two accounts configured shows under both. Each entry
    names the account its source belongs to; an ``import:`` names none. Each chat lists the
    accounts ``chat_access`` records as reaching it — none for an import.
    """
    counts = db.message_counts(conn)
    coverage = db.chat_sources_map(conn)
    reaching = db.chat_accounts_map(conn)
    by_source: dict[str, list[ChatRow]] = {}
    for chat in db.list_chats(conn):
        owners = dict.fromkeys([chat.source_id] if chat.source_id else [])
        owners.update(dict.fromkeys(coverage.get(chat.id, [])))
        for source_id in owners:
            by_source.setdefault(source_id, []).append(chat)
    configured = [s.id for s in cfg.sources]
    order = configured + sorted(set(by_source) - set(configured))
    return [
        SourceStatus(
            source_id=source_id,
            account=None if source_id.startswith(IMPORT_PREFIX) else source_account(source_id),
            chats=[
                ChatStatus(
                    id=chat.id,
                    title=chat.title,
                    type=chat.type,
                    username=chat.username,
                    message_count=counts.get(chat.id, 0),
                    last_sync_at=chat.last_sync_at,
                    unavailable=chat.unavailable,
                    accounts=reaching.get(chat.id, []),
                )
                for chat in by_source.get(source_id, [])
            ],
        )
        for source_id in order
    ]
