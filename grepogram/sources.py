"""Sources: the ``[[sources]]`` entries that decide which Telegram chats get indexed.

A *target* is what a user or Claude types: a marked id as printed by ``grepogram dialogs``, an
``@username``, a ``t.me`` link, ``folder:<name>``, or free text matched fuzzily against dialog
titles and folder names. :func:`add_source` resolves a target through a
:class:`~grepogram.dialogs.DialogCatalog` into a :class:`Source`; :func:`resolve_sources` turns
every configured source into ``chats`` rows tagged with ``source_id`` and runs before each sync
because folder membership changes; :func:`remove_source` drops an entry together with its
chats' data; :func:`sources_status` reports what is indexed per source.
"""

import dataclasses
import datetime as dt
import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from telethon import errors
from telethon.tl import types

from grepogram import db, dialogs
from grepogram.dialogs import DialogCatalog, DialogInfo, FolderInfo, Match
from grepogram.models import ChatRow, ChatStatus, Config, Source, SourceStatus

log = logging.getLogger(__name__)

TargetKind = Literal["id", "username", "folder", "fuzzy"]
FOLDER_PREFIX = "folder:"
CHAT_PREFIX = "chat:"
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


@dataclass(frozen=True, slots=True, kw_only=True)
class Target:
    """A parsed target; ``value`` is the marked id for ``kind="id"`` and text otherwise."""

    kind: TargetKind
    value: str | int

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


# --- targets ---------------------------------------------------------------------------------


def parse_target(raw: str) -> Target:
    """Classify a target string.

    ``-1000000000100`` / ``42`` → marked id (as printed by ``grepogram dialogs``);
    ``@name``, ``https://t.me/name`` and ``t.me/s/name`` → username; ``t.me/c/<id>`` → the
    private channel's marked id; ``folder:<name>`` → folder; ``chat:<value>`` → the value parsed
    again (so source ids from ``sources ls`` work); anything else → fuzzy text. Invite links
    and malformed usernames raise :class:`InvalidTarget`.
    """
    text = raw.strip()
    if not text:
        raise InvalidTarget("empty target")
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
) -> Added:
    """Resolve ``target`` and append it to ``cfg.sources``; the caller saves the config.

    Chats are stored as ``@username`` when they have one (readable in ``config.toml``) and as
    the marked id otherwise. Folders are stored by their exact title. An entry that is already
    present — by id or by another spelling of the same chat — raises :class:`DuplicateSource`.
    """
    resolved = await resolve_target(target, catalog)
    normalized_since = _since(since)
    if isinstance(resolved, FolderInfo):
        source = Source(folder=resolved.title, since=normalized_since, comments=comments)
        members = await folder_dialogs(resolved, catalog)
        dialog = None
    else:
        dialog = resolved
        if comments and dialog.type != "channel":
            raise SourceError(
                f"comments applies to channels only; {dialog.title!r} is a {dialog.type}"
            )
        source = Source(chat=chat_value(dialog), since=normalized_since, comments=comments)
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
    """``cfg`` with ``source`` appended; :class:`DuplicateSource` when it is already there — by
    id or, given the ``dialog`` a chat source resolved to, under another spelling of that chat.

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
        if dialog is not None and existing.chat is not None and _same_chat(existing, dialog):
            raise DuplicateSource(f"{dialog.title!r} is already a source as {existing.id}")


def _same_chat(existing: Source, dialog: DialogInfo) -> bool:
    try:
        target = parse_target(str(existing.chat))
    except InvalidTarget:
        return False
    if target.kind == "id":
        return target.value == dialog.id
    if target.kind == "username":
        return bool(dialog.username) and target.text.casefold() == str(dialog.username).casefold()
    return False


def remove_source(cfg: Config, conn: sqlite3.Connection, target: Target) -> Removed:
    """Drop the source ``target`` names and delete every chat indexed through it.

    The target may be a source id (``folder:Argentina``, ``chat:@arg_chat``), a folder name, a
    chat id / ``@username`` / link, or a fuzzy name matched against source entries and the
    titles of their indexed chats. Naming a chat that came in through a folder, or a channel's
    discussion group indexed through the channel's source, is refused: that would silently
    remove the whole folder or the channel (:func:`_refuse_indirect`). The caller holds the sync
    lock, so no sync writes to the chats being deleted.
    """
    source_id = find_source(cfg, conn, target)
    source = next((s for s in cfg.sources if s.id == source_id), None)
    chats = db.list_chats(conn, source_id=source_id)
    with db.transaction(conn):
        for chat in chats:
            db.delete_chat(conn, chat.id)
    log.info("removed source %s with %d chats", source_id, len(chats))
    return Removed(
        config=dataclasses.replace(cfg, sources=[s for s in cfg.sources if s.id != source_id]),
        source_id=source_id,
        source=source,
        chat_ids=[chat.id for chat in chats],
    )


def find_source(cfg: Config, conn: sqlite3.Connection, target: Target) -> str:
    """The source id ``target`` refers to, among configured entries and ``chats.source_id``."""
    chats = db.list_chats(conn)
    known = [s.id for s in cfg.sources]
    known += sorted({c.source_id for c in chats if c.source_id and c.source_id not in known})
    if target.kind == "folder":
        return _folder_source(target.text, known)
    if target.kind == "id":
        candidate = f"{CHAT_PREFIX}{target.value}"
        if candidate in known:
            return candidate
        return _chat_source(db.get_chat(conn, int(target.value)), f"id {target.value}")
    if target.kind == "username":
        wanted = target.text.casefold()
        for source_id in known:
            if source_id.casefold() == f"{CHAT_PREFIX}@{wanted}":
                return source_id
        owner = next((c for c in chats if (c.username or "").casefold() == wanted), None)
        return _chat_source(owner, f"@{target.text}")
    return _fuzzy_source(target.text, known, chats)


def _folder_source(name: str, known: list[str]) -> str:
    wanted = dialogs.normalize(name)
    folders = [s for s in known if s.startswith(FOLDER_PREFIX)]
    for source_id in folders:
        if dialogs.normalize(source_id[len(FOLDER_PREFIX) :]) == wanted:
            return source_id
    scored = [(s, dialogs.score(wanted, s[len(FOLDER_PREFIX) :])) for s in folders]
    hits = [s for s, value in scored if value > 0]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise AmbiguousTarget(name, hits)
    raise UnknownSource(f"no folder source named {name!r} (sources: {', '.join(known) or 'none'})")


def _chat_source(chat: ChatRow | None, label: str) -> str:
    if chat is None or not chat.source_id:
        raise UnknownSource(f"{label} is not an indexed chat")
    _refuse_indirect(chat, label)
    return chat.source_id


def _refuse_indirect(chat: ChatRow, label: str) -> None:
    """Refuse a target that names a chat whose source covers more than that chat: a member of a
    folder source, or a channel's discussion group indexed through the channel's source — the
    group a channel was unlinked from included, which keeps that source until it is removed
    (:func:`discussion_source_id` owns that rule)."""
    if not chat.source_id:
        return
    if chat.source_id.startswith(FOLDER_PREFIX):
        raise SourceError(
            f"{label} is indexed through {chat.source_id}; remove that folder source instead "
            "or take the chat out of the folder in Telegram"
        )
    if _own_source(chat):
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
    """Whether ``chat.source_id`` is a ``chat:`` entry naming this very chat."""
    own = {f"{CHAT_PREFIX}{chat.id}"}
    if chat.username:
        own.add(f"{CHAT_PREFIX}@{chat.username}".casefold())
    return (chat.source_id or "").casefold() in own


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


async def resolve_sources(cfg: Config, client: Any, conn: sqlite3.Connection) -> list[ChatRow]:
    """Upsert a ``chats`` row for every chat the configured sources currently cover.

    Folder membership and entities are re-read from Telegram each time (``client`` must be
    connected). A chat covered by two sources keeps the first source's id; a source that no
    longer resolves is logged at WARNING and skipped. Sync state on existing rows is preserved,
    and so is a stored ``discussion_of`` (:func:`grepogram.db.upsert_chat`): a channel's
    discussion group listed by a source is synced as a chat of its own and keeps holding the
    channel's comments.
    """
    catalog = DialogCatalog(client)
    rows: list[ChatRow] = []
    seen: set[int] = set()
    for source in cfg.sources:
        try:
            infos = await source_dialogs(source, catalog)
        except SourceError as exc:
            log.warning("skipping source %s: %s", source.id, exc)
            continue
        for info in infos:
            if info.id in seen:
                log.debug("chat %s already covered by another source, keeping the first", info.id)
                continue
            seen.add(info.id)
            rows.append(
                db.upsert_chat(
                    conn,
                    ChatRow(
                        id=info.id,
                        type=info.type,
                        title=info.title,
                        username=info.username,
                        is_forum=info.is_forum,
                        source_id=source.id,
                    ),
                )
            )
    log.info("resolved %d chats from %d sources", len(rows), len(cfg.sources))
    return rows


async def source_dialogs(source: Source, catalog: DialogCatalog) -> list[DialogInfo]:
    """The chats one source covers right now."""
    if source.folder is not None:
        return await folder_dialogs(await find_folder(source.folder, catalog), catalog)
    resolved = await resolve_target(parse_target(str(source.chat)), catalog)
    if isinstance(resolved, FolderInfo):
        raise UnknownTarget(
            f"chat {source.chat!r} names the folder {resolved.title!r}; "
            f"use folder = {resolved.title!r} instead"
        )
    return [resolved]


def discussion_source_id(group: ChatRow | None, channel: ChatRow) -> str | None:
    """The ``source_id`` a channel's discussion group carries once the link is applied.

    One rule decides who owns a group's rows — and therefore what a ``sources rm`` of that source
    deletes:

    * a group a source covers directly keeps that source, whether it is a folder holding it or a
      ``chat:`` entry naming it; :func:`resolve_sources` writes the same id on every run, and the
      link never overwrites it;
    * a group known only through a channel's link belongs to the source of the channel that links
      it *now*, so a group handed from channel X to channel Y moves to Y's source together with
      the link — removing X then leaves the group alone and removing Y takes it along, which is
      where its comments came from;
    * a group a channel was unlinked from keeps the source it came in through until that source
      is removed: the comments stored under it were indexed through that source and go with it.

    A channel with no source of its own (never resolved, only stored) changes nothing.
    :func:`_refuse_indirect` reads the same rule from the other end — only a group that is its
    own source can be removed by naming it.
    """
    if group is None or not group.source_id:
        return channel.source_id
    if group.source_id.startswith(FOLDER_PREFIX) or _own_source(group):
        return group.source_id
    return channel.source_id or group.source_id


# --- status ----------------------------------------------------------------------------------


def sources_status(cfg: Config, conn: sqlite3.Connection) -> list[SourceStatus]:
    """Per-source view of the indexed chats: configured sources first, in config order, then
    any ``source_id`` still present in the database but gone from the config."""
    counts = db.message_counts(conn)
    by_source: dict[str, list[ChatRow]] = {}
    for chat in db.list_chats(conn):
        if chat.source_id:
            by_source.setdefault(chat.source_id, []).append(chat)
    configured = [s.id for s in cfg.sources]
    order = configured + sorted(set(by_source) - set(configured))
    return [
        SourceStatus(
            source_id=source_id,
            chats=[
                ChatStatus(
                    id=chat.id,
                    title=chat.title,
                    type=chat.type,
                    username=chat.username,
                    message_count=counts.get(chat.id, 0),
                    last_sync_at=chat.last_sync_at,
                    unavailable=chat.unavailable,
                )
                for chat in by_source.get(source_id, [])
            ],
        )
        for source_id in order
    ]
