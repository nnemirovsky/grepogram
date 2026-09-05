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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from telethon import errors
from telethon.tl import types

from grepogram import db, dialogs
from grepogram.dialogs import DialogCatalog, DialogInfo, FolderInfo, Match
from grepogram.models import ChatRow, ChatStatus, Config, Source, SourceStatus
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
    target = _target_of(str(existing.chat))
    return target is not None and _names_chat(target, dialog.id, dialog.username)


def _target_of(value: str) -> Target | None:
    """``value`` parsed as a target; ``None`` when it is not one (an invite link, a bad handle)."""
    try:
        return parse_target(value)
    except InvalidTarget:
        return None


def _names_chat(target: Target, chat_id: int, username: str | None) -> bool:
    """Whether ``target`` is this very chat: its marked id, or its ``@username`` in any case.

    Identity, never spelling. ``chat =`` takes an id, an ``@username``, ``https://t.me/<name>``
    and ``t.me/c/<id>`` alike, and :func:`parse_target` has already folded all four into those
    two shapes, so every documented form answers the same. A fuzzy value — a title typed into
    ``config.toml`` by hand — names no identity without the dialog catalog and matches nothing.
    """
    if target.kind == "id":
        return target.value == chat_id
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
    """Drop the source ``target`` names and delete every chat indexed through it.

    The target may be a source id (``folder:Argentina``, ``chat:@arg_chat``), a folder name, a
    chat id / ``@username`` / link, or a fuzzy name matched against source entries and the
    titles of their indexed chats. A chat entry answers to every spelling of the same chat, its
    own included: what is compared is the identity a target resolves to, never the text
    ``config.toml`` happens to hold. Naming a chat that came in through a folder, or a channel's
    discussion group indexed through the channel's source, is refused: that would silently
    remove the whole folder or the channel (:func:`_refuse_indirect`). The caller holds the sync
    lock, so no sync writes to the chats being deleted.

    Deleting a discussion group takes the comments it fed to a channel's post threads with it
    (:func:`grepogram.db.delete_chat`), so the index never quotes rows this removed.
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
    if target.kind in ("id", "username"):
        named = _named_source(target, known)
        if named is not None:
            return named
        return _indexed_source(conn, chats, target)
    return _fuzzy_source(target.text, known, chats)


def _named_source(target: Target, known: Sequence[str]) -> str | None:
    """The ``chat:`` entry that names the chat ``target`` names, under either one's spelling.

    This is what finds a source that was never synced: with no ``chats`` row to resolve the
    target through, the configured entries are all there is to match it against. Only entries of
    the target's own kind can answer — nothing maps an ``@username`` to a marked id offline — so
    a chat that *is* indexed still falls through to :func:`_indexed_source`, which knows both.
    """
    for source_id in known:
        if not source_id.casefold().startswith(CHAT_PREFIX):
            continue
        other = _target_of(source_id)
        if other is not None and same_target(other, target):
            return source_id
    return None


def _indexed_source(conn: sqlite3.Connection, chats: Sequence[ChatRow], target: Target) -> str:
    """The source of the indexed chat ``target`` names; refused when that source covers more."""
    if target.kind == "id":
        chat_id = int(target.value)
        return _chat_source(db.get_chat(conn, chat_id), f"id {chat_id}")
    wanted = target.text.casefold()
    owner = next((c for c in chats if (c.username or "").casefold() == wanted), None)
    return _chat_source(owner, f"@{target.text}")


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
    """Whether ``chat.source_id`` is a ``chat:`` entry naming this very chat.

    Read by identity (:func:`_names_chat`), so the id, the ``@username`` and the two ``t.me``
    link forms of one chat all count as covering it directly — a group configured as
    ``chat = "https://t.me/..."`` owns its rows exactly as one configured by id does.
    """
    source_id = chat.source_id or ""
    if not source_id.casefold().startswith(CHAT_PREFIX):
        return False
    target = _target_of(source_id)
    return target is not None and _names_chat(target, chat.id, chat.username)


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

    **A chat held as an ``import:`` is left alone**, logged and not returned. ``upsert_chat``
    writes ``source_id`` unconditionally, so without this a resolve would quietly replace
    ``import:<slug>`` with the live source's id — and every protection keyed on that prefix would
    go with it, :func:`prunable` offering the whole imported history for deletion the moment the
    folder stopped listing the chat. :func:`refuse_imported` guards the two commands that *add* a
    source, but nothing guards the folder gaining that chat on Telegram afterwards, and this runs
    on every sync. Taking the chat over is a deliberate act: ``sources rm import:<slug>`` first.
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
            held = _imported_tag(conn, info.id)
            if held is not None:
                log.warning(
                    "chat %s (%s) is held as %s, a Telegram Desktop import; source %s does not "
                    "take it over — run `grepogram sources rm %s` first to sync it from Telegram",
                    info.id,
                    info.title,
                    held,
                    source.id,
                    held,
                )
                continue
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


def _imported_tag(conn: sqlite3.Connection, chat_id: int) -> str | None:
    """The ``import:<slug>`` this chat is held under, ``None`` when it is not an import."""
    stored = db.get_chat(conn, chat_id)
    source_id = "" if stored is None else (stored.source_id or "")
    return source_id if source_id.startswith(IMPORT_PREFIX) else None


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
      ``chat:`` entry naming it — by identity, so the id, the ``@username`` and both ``t.me``
      link forms of one group all count (:func:`_own_source`); :func:`resolve_sources` writes the
      same id on every run, and the link never overwrites it;
    * a group known only through a channel's link belongs to the source of the channel that links
      it *now*, so a group handed from channel X to channel Y moves to Y's source together with
      the link — removing X then leaves the group alone and removing Y takes it along, which is
      where its comments came from;
    * a group a channel was unlinked from keeps the source it came in through until that source
      is removed: the comments stored under it were indexed through that source and go with it.

    A channel with no source of its own (never resolved, only stored) changes nothing.
    :func:`_refuse_indirect` reads the same rule from the other end — only a group that is its
    own source can be removed by naming it — and :func:`grepogram.db.delete_chat` completes it:
    whichever source owns the group, removing it drops the comments the group fed to the
    channel's post threads along with the rows they quote.
    """
    if group is None or not group.source_id:
        return channel.source_id
    if group.source_id.startswith(FOLDER_PREFIX) or _own_source(group):
        return group.source_id
    return channel.source_id or group.source_id


# --- prune -----------------------------------------------------------------------------------


async def folder_membership(cfg: Config, catalog: DialogCatalog) -> FolderMembership:
    """Read what every folder source lists right now; ``catalog``'s client must be connected.

    The network half of ``sources prune``, and it differs from :func:`resolve_sources` in the
    one way that matters here: a source that does not resolve is *recorded* as failed instead of
    being logged and skipped. :func:`prunable` reads a chat's absence from its folder as "it
    left", so a transient ``RPCError`` swallowed in silence would offer that source's whole
    indexed history for deletion.

    A folder's membership is the chats it shows plus the explicit peers it names, resolvable or
    not: :func:`folder_dialogs` drops a peer whose entity Telegram will not hand over (a channel
    gone private, say), and such a peer is still very much listed by the folder.
    """
    listed: dict[str, set[int]] = {}
    failed: dict[str, str] = {}
    for source in cfg.sources:
        if source.folder is None:
            continue
        try:
            folder = await find_folder(source.folder, catalog)
            members = await folder_dialogs(folder, catalog)
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
    covered: set[int] = set()
    for ids in folders.listed.values():
        covered |= ids
    named = [
        target
        for target in (_target_of(str(s.chat)) for s in cfg.sources if s.chat is not None)
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
        if source_id.startswith(IMPORT_PREFIX) or not source_id.startswith(FOLDER_PREFIX):
            continue  # an import and a chat entry name themselves; neither can leave a folder
        if chat.id in covered or any(_names_chat(t, chat.id, chat.username) for t in named):
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


def prune_chats(conn: sqlite3.Connection, chat_ids: Sequence[int]) -> list[int]:
    """Delete the chats :func:`prunable` offered, with their messages, units and index rows.

    One transaction for the lot, and a chat that is no longer stored is skipped rather than
    raised: the scan runs before the sync lock is taken — a network round trip must never be
    held across it — so a sync may have removed a chat in between. Returns the ids removed.
    """
    removed: list[int] = []
    with db.transaction(conn):
        for chat_id in chat_ids:
            if db.get_chat(conn, chat_id) is None:
                continue
            db.delete_chat(conn, chat_id)
            removed.append(chat_id)
    log.info("pruned %d chats", len(removed))
    return removed


# --- import ----------------------------------------------------------------------------------


def import_chats(conn: sqlite3.Connection, entries: Sequence[ImportedChat]) -> list[Imported]:
    """Store a parsed Telegram Desktop export, tagging each of its chats ``import:<slug>``.

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
    rows = [entry.chat for entry in entries]
    _refuse_live(conn, rows)
    source_ids = import_source_ids(conn, rows)
    stored: list[Imported] = []
    with db.transaction(conn):
        for entry in entries:
            source_id = source_ids[entry.chat.id]
            chat = db.upsert_chat(conn, dataclasses.replace(entry.chat, source_id=source_id))
            message_ids = db.upsert_messages(conn, entry.messages)
            stored.append(Imported(chat=chat, source_id=source_id, messages=len(message_ids)))
    log.info(
        "imported %d chats and %d messages", len(stored), sum(item.messages for item in stored)
    )
    return stored


def refuse_imported(conn: sqlite3.Connection, covered: Sequence[DialogInfo]) -> None:
    """Refuse a live source that would cover a chat this index holds as an import.

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
        source_id = _imported_tag(conn, dialog.id)
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
    held = {
        str(chat.source_id): chat.id
        for chat in db.list_chats(conn)
        if (chat.source_id or "").startswith(IMPORT_PREFIX)
    }
    ids: dict[int, str] = {}
    taken: set[str] = set()

    def claimed(source_id: str, chat_id: int) -> bool:
        """Whether ``source_id`` belongs to some other chat — in this export or in the index."""
        return source_id in taken or held.get(source_id, chat_id) != chat_id

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
        stored = db.get_chat(conn, chat.id)
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
