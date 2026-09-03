"""Dialog listing, Telegram folders and fuzzy matching of chat and folder titles.

Folders are Telegram's ``DialogFilter`` objects: an explicit peer list (``include_peers`` and
``pinned_peers`` minus ``exclude_peers``) plus category flags (contacts, groups, ...) narrowed by
``exclude_muted`` / ``exclude_read`` / ``exclude_archived``. :func:`folder_members` evaluates that
rule the way the official clients do; :class:`DialogCatalog` memoizes ``get_dialogs()`` and the
folder list for one process so repeated lookups do not hit Telegram again.
"""

import datetime as dt
import difflib
import logging
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from telethon import utils
from telethon.tl import functions, types

from grepogram.models import ChatType

log = logging.getLogger(__name__)

FUZZY_MIN_RATIO = 0.6
SUBSTRING_BASE = 0.8
MatchKind = Literal["dialog", "folder"]
Category = Literal["contacts", "non_contacts", "groups", "broadcasts", "bots"]


@dataclass(frozen=True, slots=True, kw_only=True)
class DialogInfo:
    """One entry of the account's dialog list, with the folders it belongs to."""

    id: int
    title: str
    type: ChatType
    username: str | None = None
    is_forum: bool = False
    folders: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class FolderInfo:
    """A Telegram folder (``DialogFilter`` / ``DialogFilterChatlist``) with marked peer ids."""

    id: int
    title: str
    include_ids: frozenset[int] = frozenset()
    pinned_ids: frozenset[int] = frozenset()
    exclude_ids: frozenset[int] = frozenset()
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False
    exclude_read: bool = False
    exclude_archived: bool = False

    @property
    def has_categories(self) -> bool:
        return any((self.contacts, self.non_contacts, self.groups, self.broadcasts, self.bots))


@dataclass(frozen=True, slots=True, kw_only=True)
class Match:
    """A dialog or folder matched by :func:`match`; ``score`` is in ``(0, 1]``."""

    kind: MatchKind
    id: int
    title: str
    score: float
    dialog: DialogInfo | None = None
    folder: FolderInfo | None = None


# --- entities --------------------------------------------------------------------------------


def chat_type(entity: Any) -> ChatType:
    """Classify a Telethon ``User`` / ``Chat`` / ``Channel`` (or their ``*Forbidden`` forms)."""
    if isinstance(entity, types.User | types.UserEmpty):
        return "bot" if getattr(entity, "bot", False) else "user"
    if isinstance(entity, types.Chat | types.ChatForbidden | types.ChatEmpty):
        return "group"
    if isinstance(entity, types.Channel | types.ChannelForbidden):
        return "supergroup" if entity.megagroup else "channel"
    raise TypeError(f"not a Telegram chat entity: {type(entity).__name__}")


def peer_id(entity: Any) -> int:
    """Telethon's marked id: ``user_id``, ``-chat_id`` or ``-100<channel_id>``."""
    return int(utils.get_peer_id(entity))


def entity_username(entity: Any) -> str | None:
    """The public ``@username`` without the ``@``, falling back to the first active alias."""
    username = getattr(entity, "username", None)
    if username:
        return str(username)
    for alias in getattr(entity, "usernames", None) or ():
        if getattr(alias, "active", False) and alias.username:
            return str(alias.username)
    return None


def dialog_info(entity: Any, folders: Iterable[str] = ()) -> DialogInfo:
    """Build a :class:`DialogInfo` from a raw entity (no client needed)."""
    return DialogInfo(
        id=peer_id(entity),
        title=str(utils.get_display_name(entity)),
        type=chat_type(entity),
        username=entity_username(entity),
        is_forum=bool(getattr(entity, "forum", False)),
        folders=list(folders),
    )


# --- folders ---------------------------------------------------------------------------------


def folder_title(tl_filter: Any) -> str:
    """``title`` is ``TextWithEntities`` on current layers and a plain string on older ones."""
    title = tl_filter.title
    return str(title.text if isinstance(title, types.TextWithEntities) else title)


def folder_from_filter(tl_filter: Any, *, self_id: int | None = None) -> FolderInfo | None:
    """Convert one ``GetDialogFilters`` entry; ``None`` for the default "All chats" entry."""
    if isinstance(tl_filter, types.DialogFilter):
        return FolderInfo(
            id=tl_filter.id,
            title=folder_title(tl_filter),
            include_ids=_peer_ids(tl_filter.include_peers, self_id),
            pinned_ids=_peer_ids(tl_filter.pinned_peers, self_id),
            exclude_ids=_peer_ids(tl_filter.exclude_peers, self_id),
            contacts=bool(tl_filter.contacts),
            non_contacts=bool(tl_filter.non_contacts),
            groups=bool(tl_filter.groups),
            broadcasts=bool(tl_filter.broadcasts),
            bots=bool(tl_filter.bots),
            exclude_muted=bool(tl_filter.exclude_muted),
            exclude_read=bool(tl_filter.exclude_read),
            exclude_archived=bool(tl_filter.exclude_archived),
        )
    if isinstance(tl_filter, types.DialogFilterChatlist):
        return FolderInfo(
            id=tl_filter.id,
            title=folder_title(tl_filter),
            include_ids=_peer_ids(tl_filter.include_peers, self_id),
            pinned_ids=_peer_ids(tl_filter.pinned_peers, self_id),
        )
    log.debug("skipping dialog filter %s", type(tl_filter).__name__)
    return None


def _peer_ids(peers: Iterable[Any], self_id: int | None) -> frozenset[int]:
    ids: set[int] = set()
    for peer in peers:
        if isinstance(peer, types.InputPeerSelf):
            if self_id is not None:
                ids.add(self_id)
            continue
        try:
            ids.add(peer_id(peer))
        except TypeError:
            log.debug("skipping folder peer %s", type(peer).__name__)
    return frozenset(ids)


def _mentions_self(tl_filter: Any) -> bool:
    peers = [
        *getattr(tl_filter, "include_peers", ()),
        *getattr(tl_filter, "pinned_peers", ()),
        *getattr(tl_filter, "exclude_peers", ()),
    ]
    return any(isinstance(peer, types.InputPeerSelf) for peer in peers)


async def fetch_folders(client: Any) -> list[FolderInfo]:
    """Read the account's folders through ``messages.getDialogFilters``.

    ``InputPeerSelf`` (Saved Messages in a folder) is resolved through ``get_me()`` only when it
    appears. The client must already be connected and authorized.
    """
    result = await client(functions.messages.GetDialogFiltersRequest())
    raw: list[Any] = list(getattr(result, "filters", result))
    self_id: int | None = None
    if any(_mentions_self(entry) for entry in raw):
        me = await client.get_me()
        self_id = int(me.id) if me is not None else None
    folders = [folder_from_filter(entry, self_id=self_id) for entry in raw]
    return [folder for folder in folders if folder is not None]


def is_muted(dialog: Any, now: dt.datetime | None = None) -> bool:
    """Whether the dialog's ``mute_until`` lies in the future."""
    mute_until = getattr(dialog.dialog.notify_settings, "mute_until", None)
    if mute_until is None:
        return False
    if mute_until.tzinfo is None:
        mute_until = mute_until.replace(tzinfo=dt.UTC)
    return bool(mute_until > (now or dt.datetime.now(dt.UTC)))


def is_unread(dialog: Any) -> bool:
    """Unread messages, mentions or reactions, or a manual unread mark."""
    return bool(
        dialog.unread_count
        or dialog.unread_mentions_count
        or dialog.unread_reactions_count
        or getattr(dialog.dialog, "unread_mark", False)
    )


def category_of(entity: Any) -> Category:
    """The folder category flag a dialog falls under."""
    kind = chat_type(entity)
    if kind == "bot":
        return "bots"
    if kind == "user":
        return "contacts" if getattr(entity, "contact", False) else "non_contacts"
    if kind == "channel":
        return "broadcasts"
    return "groups"


def folder_members(
    folder: FolderInfo, dialogs: Iterable[Any], *, now: dt.datetime | None = None
) -> set[int]:
    """Marked ids of the dialogs the folder shows.

    Explicit peers (``include_ids`` and ``pinned_ids`` minus ``exclude_ids``) are always members,
    even when absent from ``dialogs``. Category flags add every dialog of that category unless it
    is excluded explicitly or by ``exclude_muted`` / ``exclude_read`` / ``exclude_archived``.
    """
    members = set((folder.include_ids | folder.pinned_ids) - folder.exclude_ids)
    if not folder.has_categories:
        return members
    now = now or dt.datetime.now(dt.UTC)
    for dialog in dialogs:
        if dialog.id in members or dialog.id in folder.exclude_ids:
            continue
        if not getattr(folder, category_of(dialog.entity)):
            continue
        if folder.exclude_muted and is_muted(dialog, now):
            continue
        if folder.exclude_read and not is_unread(dialog):
            continue
        if folder.exclude_archived and dialog.archived:
            continue
        members.add(int(dialog.id))
    return members


# --- catalog ---------------------------------------------------------------------------------


class DialogCatalog:
    """In-process memo of the account's dialogs and folders.

    Both lists are fetched on first use through ``client`` (which must be connected inside
    ``tg.connected``) and kept until :meth:`invalidate`; nothing is cached on disk. The returned
    lists are shared, so treat them as read-only.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._dialogs: list[DialogInfo] | None = None
        self._folders: list[FolderInfo] | None = None
        self._entities: dict[int, Any] = {}

    async def list_dialogs(self) -> list[DialogInfo]:
        if self._dialogs is None:
            await self._load()
        assert self._dialogs is not None
        return self._dialogs

    async def list_folders(self) -> list[FolderInfo]:
        if self._folders is None:
            await self._load()
        assert self._folders is not None
        return self._folders

    async def entity(self, key: int | str) -> Any:
        """The raw entity behind a marked id or ``@username``.

        Dialog ids are served from the memo; anything else goes through ``client.get_entity``,
        which raises ``ValueError`` (or a Telethon error) when Telegram does not know the peer.
        """
        if self._dialogs is None:
            await self._load()
        if isinstance(key, int) and key in self._entities:
            return self._entities[key]
        return await self._client.get_entity(key)

    def invalidate(self) -> None:
        """Forget the memo so the next call re-reads dialogs and folders from Telegram."""
        self._dialogs = None
        self._folders = None
        self._entities = {}

    async def _load(self) -> None:
        raw = list(await self._client.get_dialogs(ignore_migrated=True))
        folders = await fetch_folders(self._client)
        members = {folder.id: folder_members(folder, raw) for folder in folders}
        self._entities = {int(dialog.id): dialog.entity for dialog in raw}
        self._dialogs = [
            dialog_info(
                dialog.entity,
                [folder.title for folder in folders if dialog.id in members[folder.id]],
            )
            for dialog in raw
        ]
        self._folders = folders
        log.debug("loaded %d dialogs and %d folders", len(self._dialogs), len(folders))


# --- matching --------------------------------------------------------------------------------


def normalize(text: str) -> str:
    """NFKC, casefold and collapsed whitespace — the comparison form for titles and queries."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def score(query: str, text: str) -> float:
    """Similarity of a normalized query to a title: substring hits score in ``(0.8, 1]``
    (shorter titles first, exact equality is 1.0), fuzzy hits with a ``SequenceMatcher`` ratio
    of at least ``FUZZY_MIN_RATIO`` against the whole title or one of its words score below 0.8,
    anything else 0.
    """
    target = normalize(text)
    if not query or not target:
        return 0.0
    if query in target:
        return SUBSTRING_BASE + (1 - SUBSTRING_BASE) * len(query) / len(target)
    candidates = [target, *target.split()]
    ratio = max(difflib.SequenceMatcher(None, query, c).ratio() for c in candidates)
    return SUBSTRING_BASE * ratio if ratio >= FUZZY_MIN_RATIO else 0.0


def match(
    query: str,
    dialogs: Sequence[DialogInfo],
    folders: Sequence[FolderInfo] = (),
    limit: int = 10,
) -> list[Match]:
    """Dialogs (by title or username) and folders (by name) matching ``query``, best first."""
    needle = normalize(query)
    if not needle:
        return []
    handle = needle.lstrip("@")
    found: list[Match] = []
    for dialog in dialogs:
        best = score(needle, dialog.title)
        if dialog.username and handle:
            best = max(best, score(handle, dialog.username))
        if best > 0:
            found.append(
                Match(kind="dialog", id=dialog.id, title=dialog.title, score=best, dialog=dialog)
            )
    for folder in folders:
        best = score(needle, folder.title)
        if best > 0:
            found.append(
                Match(kind="folder", id=folder.id, title=folder.title, score=best, folder=folder)
            )
    found.sort(key=lambda m: (-m.score, normalize(m.title), m.kind))
    return found[:limit]
