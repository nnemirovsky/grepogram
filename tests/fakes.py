"""Client-free stand-ins for Telethon.

Tests never touch the network: ``FakeClient`` answers the handful of ``TelegramClient`` calls
grepogram makes from in-memory fixtures, and the ``make_*`` helpers build real Telethon TL
objects (``types.User``, ``types.Channel``, ``custom.Dialog``) without a client attached.
"""

import asyncio
import copy
import datetime as dt
import inspect
import zlib
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telethon import errors, utils
from telethon.tl import custom, functions, types
from telethon.tl.types import chatlists as tl_chatlists
from telethon.tl.types import contacts as tl_contacts
from telethon.tl.types import messages as tl_messages

from grepogram.models import DEFAULT_ACCOUNT
from tests.fixtures import tl

FAR_FUTURE = dt.datetime(2100, 1, 1, tzinfo=dt.UTC)


def make_user(
    user_id: int,
    first_name: str,
    last_name: str | None = None,
    *,
    username: str | None = None,
    bot: bool = False,
    contact: bool = False,
) -> types.User:
    return types.User(
        id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        bot=bot,
        contact=contact,
        access_hash=user_id,
    )


def make_channel(
    channel_id: int,
    title: str,
    *,
    username: str | None = None,
    megagroup: bool = False,
    forum: bool = False,
) -> types.Channel:
    """A channel (``megagroup=False``) or supergroup (``megagroup=True``)."""
    return types.Channel(
        id=channel_id,
        title=title,
        username=username,
        megagroup=megagroup,
        broadcast=not megagroup,
        forum=forum,
        access_hash=channel_id,
        photo=types.ChatPhotoEmpty(),
        date=None,
    )


def no_discussion(request: Any) -> Any:
    """``channels.getFullChannel`` of a channel with no discussion group, for ``responses=``."""
    return tl_messages.ChatFull(
        full_chat=types.ChannelFull(
            id=0,
            about="",
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=0,
            chat_photo=types.PhotoEmpty(id=0),
            notify_settings=types.PeerNotifySettings(),
            bot_info=[],
            pts=0,
        ),
        chats=[],
        users=[],
    )


def make_group(chat_id: int, title: str, *, migrated_to: int | None = None) -> types.Chat:
    """A legacy (small) group; ``migrated_to`` marks it as upgraded to that supergroup."""
    return types.Chat(
        id=chat_id,
        title=title,
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=None,
        version=1,
        migrated_to=(
            types.InputChannel(migrated_to, migrated_to) if migrated_to is not None else None
        ),
    )


def make_dialog(
    entity: Any,
    *,
    pinned: bool = False,
    archived: bool = False,
    unread_count: int = 0,
    muted: bool = False,
) -> custom.Dialog:
    """A ``custom.Dialog`` as ``get_dialogs()`` returns it, built without a client."""
    dialog = types.Dialog(
        peer=utils.get_peer(entity),
        top_message=0,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=unread_count,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        notify_settings=types.PeerNotifySettings(mute_until=FAR_FUTURE if muted else None),
        pinned=pinned,
        folder_id=1 if archived else None,
    )
    return custom.Dialog(None, dialog, {utils.get_peer_id(entity): entity}, None)


def make_folder(
    folder_id: int,
    title: str,
    *,
    include: Iterable[Any] = (),
    pinned: Iterable[Any] = (),
    exclude: Iterable[Any] = (),
    contacts: bool = False,
    non_contacts: bool = False,
    groups: bool = False,
    broadcasts: bool = False,
    bots: bool = False,
    exclude_muted: bool = False,
    exclude_read: bool = False,
    exclude_archived: bool = False,
) -> types.DialogFilter:
    """A regular folder as returned by ``GetDialogFiltersRequest``.

    Peers may be entities, marked ids or ``InputPeer`` objects (``types.InputPeerSelf()`` for
    Saved Messages).
    """
    return types.DialogFilter(
        id=folder_id,
        title=types.TextWithEntities(title, []),
        pinned_peers=[_input_peer(p) for p in pinned],
        include_peers=[_input_peer(p) for p in include],
        exclude_peers=[_input_peer(p) for p in exclude],
        contacts=contacts or None,
        non_contacts=non_contacts or None,
        groups=groups or None,
        broadcasts=broadcasts or None,
        bots=bots or None,
        exclude_muted=exclude_muted or None,
        exclude_read=exclude_read or None,
        exclude_archived=exclude_archived or None,
    )


def make_chatlist(
    folder_id: int,
    title: str,
    *,
    include: Iterable[Any] = (),
    pinned: Iterable[Any] = (),
) -> types.DialogFilterChatlist:
    """A shared folder (chatlist): explicit peers only, no category flags."""
    return types.DialogFilterChatlist(
        id=folder_id,
        title=types.TextWithEntities(title, []),
        pinned_peers=[_input_peer(p) for p in pinned],
        include_peers=[_input_peer(p) for p in include],
    )


def _input_peer(peer: Any) -> Any:
    if isinstance(peer, int):
        peer = utils.get_peer(peer)
        if isinstance(peer, types.PeerUser):
            return types.InputPeerUser(peer.user_id, peer.user_id)
        if isinstance(peer, types.PeerChat):
            return types.InputPeerChat(peer.chat_id)
        return types.InputPeerChannel(peer.channel_id, peer.channel_id)
    return utils.get_input_peer(peer)


def make_message(
    chat_id: int,
    msg_id: int,
    text: str = "",
    *,
    date: dt.datetime | None = None,
) -> types.Message:
    """A minimal text message; ``tests/fixtures/tl.py`` has the richer builders."""
    return tl.message(chat_id, msg_id, text, date=date)


@dataclass(frozen=True, slots=True)
class FakeInvite:
    """An invite link of a :class:`FakeWorld`: the chat it opens, whether joining it sends an
    admission request, whether a non-member may preview the chat (``ChatInvitePeek``), and the
    member count the preview shows (the entity's own when ``None``)."""

    entity: Any
    request_needed: bool = False
    peek: bool = False
    participants: int | None = None


@dataclass(frozen=True, slots=True)
class FakeChatlist:
    """A shared folder (``t.me/addlist/<slug>``) of a :class:`FakeWorld`: its title and chats."""

    title: str
    entities: list[Any] = field(default_factory=list)


FREE_SEARCH = types.SearchPostsFlood(total_daily=10, remains=10, stars_amount=0)
"""The ``channels.checkSearchPostsFlood`` answer of an account with free post searches left."""


class FakeWorld:
    """Telegram as several accounts see it: one set of chats, a view of it per account.

    ``entities`` are the peers Telegram knows; ``messages`` and ``comments`` are the histories of
    the **shared** chats (channels and supergroups), which carry one global set of message ids
    whatever account reads them. A private chat's history is the account's own — the same
    conversation has other message ids for the other side — so it is not held here but handed
    to :meth:`client` as that account's ``messages``.

    ``invites`` maps an invite hash to the :class:`FakeInvite` it opens (or to the exception
    ``messages.checkChatInvite`` raises for it) and ``chatlists`` a folder slug to its
    :class:`FakeChatlist` (or an exception); a hash or a slug the world does not hold is
    refused the way Telegram refuses it.

    :meth:`client` builds the account's :class:`FakeClient`: ``members`` are the chats it has a
    dialog with, every entity it sees carries the access hash *that account* addresses it by
    (:meth:`access_hash`, different per account as in Telegram), and a private channel it is no
    member of refuses its history with ``ChannelPrivateError``. A public chat — one with a
    username — is readable without joining.
    """

    def __init__(
        self,
        *,
        entities: Iterable[Any] = (),
        messages: Mapping[int, Iterable[types.Message]] | None = None,
        comments: Mapping[tuple[int, int], Iterable[types.Message]] | None = None,
        invites: Mapping[str, FakeInvite | BaseException] | None = None,
        chatlists: Mapping[str, FakeChatlist | BaseException] | None = None,
    ) -> None:
        self.entities: dict[int, Any] = {utils.get_peer_id(e): e for e in entities}
        self.messages = {chat_id: list(items) for chat_id, items in (messages or {}).items()}
        self.comments = {key: list(items) for key, items in (comments or {}).items()}
        self.invites = dict(invites or {})
        self.chatlists = dict(chatlists or {})

    @staticmethod
    def access_hash(account: str, marked_id: int) -> int:
        """The access hash ``account`` addresses ``marked_id`` by — stable, and its own."""
        return zlib.crc32(f"{account}:{marked_id}".encode())

    def seen_by(self, account: str, entity: Any, *, member: bool = True) -> Any:
        """``entity`` as ``account`` receives it: a copy carrying that account's access hash,
        and for a channel or group it is not in (``member=False``) the ``left`` flag Telegram
        sets on it.

        A legacy group has no access hash at all and is handed over as it is.
        """
        left = not member and isinstance(entity, types.Channel | types.Chat)
        if getattr(entity, "access_hash", None) is None and not left:
            return entity
        seen = copy.copy(entity)
        if getattr(entity, "access_hash", None) is not None:
            seen.access_hash = self.access_hash(account, utils.get_peer_id(entity))
        if left:
            seen.left = True
        return seen

    def client(
        self,
        account: str = DEFAULT_ACCOUNT,
        *,
        members: Iterable[Any] = (),
        **kwargs: Any,
    ) -> "FakeClient":
        """The client of ``account``, with a dialog for each of ``members`` and no folders."""
        dialogs = [make_dialog(self.seen_by(account, entity)) for entity in members]
        kwargs.setdefault("folders", [])
        return FakeClient(account=account, world=self, dialogs=dialogs, **kwargs)


class FakeClient:
    """In-memory replacement for ``TelegramClient``.

    ``messages`` maps a marked peer id (``-100…`` for channels and supergroups) to that chat's
    messages; ``comments`` maps ``(channel_id, post_id)`` to the discussion-side messages that
    ``iter_messages(channel, reply_to=post_id)`` returns; ``responses`` maps a raw request class
    to a result, an exception to raise, or a callable taking the request; ``failures`` maps a
    peer id — or ``(channel_id, post_id)`` for one comment thread — to an exception
    ``iter_messages`` raises for it, either at once or, as ``(after, exception)``, once ``after``
    messages were yielded; ``entity_errors`` maps a ``get_entity`` key to the exception it
    raises; ``downloads`` maps ``(chat_id, msg_id)`` to the bytes ``download_media`` writes for
    that message, or to an exception it raises; ``folders`` registers a
    ``GetDialogFiltersRequest`` response (the default "All chats" entry first, like Telegram).
    Every method call is recorded in ``calls`` as ``(name, kwargs)``.

    ``entities`` is the *world*, not the client's cache: a peer this client could learn about,
    the way Telegram knows one whether or not the session has its access hash. What the session
    holds is :attr:`resolved`, which starts empty exactly as ``tg.load_session``'s does, and
    ``strict_entities`` (the default) makes addressing an unlearned peer by bare id fail with
    the plain ``ValueError`` Telethon raises for it — see :meth:`_require_resolved`. Pass
    ``strict_entities=False`` only for a test whose subject is not peer resolution and that has
    no realistic route to warm the cache.

    ``account`` and ``world`` make it one account's view of a :class:`FakeWorld`: the world's
    entities (with this account's access hashes) and shared histories join its own, and a private
    channel it has no dialog with refuses its history. ``session`` stands in for Telethon's
    in-memory session: ``session.process_entities`` with ``InputPeer*`` objects seeds the cache
    with stored access hashes, which address the peer only when the hash is this account's.

    Besides ``responses``, the raw requests research sends are answered from the world itself
    (:meth:`_research_answer`): ``messages.checkChatInvite`` and ``chatlists.checkChatlistInvite``
    from its invites and folders (``chatlists_joined`` names the folders this account imported),
    ``channels.getChannels`` only with *this account's* access hash and ``messages.getChats``
    only for a group it is in, ``contacts.search`` over public chats and its own, and
    ``channels.searchPosts`` over public channels' posts, metered by ``search_flood``. Their
    ``chats`` and ``users`` — and a ``chat`` field — teach the session the peers they carry.

    The joins a research run sends change this account's view of the world (:meth:`join`):
    ``channels.joinChannel`` for a public chat (one listed in ``join_requests`` answers with an
    admission request instead), ``messages.importChatInvite`` from the world's invites, and
    ``chatlists.joinChatlistInvite`` / ``joinChatlistUpdates`` for exactly the peers named,
    each a chat of that folder, and for ``joinChatlistUpdates`` a folder this account imported,
    named by its :meth:`filter_id` (each call's ids recorded in ``chatlist_joins``). An
    admission request sent is kept in ``requested`` until a test admits the account with
    :meth:`join`.
    """

    def __init__(
        self,
        *,
        dialogs: Iterable[custom.Dialog] = (),
        entities: Iterable[Any] = (),
        messages: Mapping[int, Iterable[types.Message]] | None = None,
        comments: Mapping[tuple[int, int], Iterable[types.Message]] | None = None,
        responses: Mapping[type, Any] | None = None,
        failures: Mapping[Any, Any] | None = None,
        entity_errors: Mapping[Any, BaseException] | None = None,
        downloads: Mapping[tuple[int, int], bytes | BaseException] | None = None,
        folders: Iterable[Any] | None = None,
        authorized: bool = True,
        me: types.User | None = None,
        two_factor: bool = False,
        strict_entities: bool = True,
        account: str = DEFAULT_ACCOUNT,
        world: FakeWorld | None = None,
        chatlists_joined: Iterable[str] = (),
        search_flood: types.SearchPostsFlood = FREE_SEARCH,
        join_requests: Iterable[Any] = (),
    ) -> None:
        self.account = account
        self.chatlists_joined = set(chatlists_joined)
        self.join_requests = {int(utils.get_peer_id(e)) for e in join_requests}
        self.requested: set[int] = set()
        self.chatlist_joins: list[list[int]] = []
        self.search_flood = search_flood
        self.world = world
        self.dialogs = list(dialogs)
        self.members = {int(dialog.id) for dialog in self.dialogs}
        self.entities: dict[int, Any] = {}
        for entity in entities:
            self.entities[utils.get_peer_id(entity)] = entity
        for dialog in self.dialogs:
            self.entities.setdefault(dialog.id, dialog.entity)
        histories: dict[int, Iterable[types.Message]] = {}
        threads: dict[tuple[int, int], Iterable[types.Message]] = {}
        if world is not None:
            for marked, entity in world.entities.items():
                seen = world.seen_by(account, entity, member=marked in self.members)
                self.entities.setdefault(marked, seen)
            histories.update(world.messages)
            threads.update(world.comments)
        histories.update(messages or {})
        threads.update(comments or {})
        self.messages = {chat_id: _by_id(items) for chat_id, items in histories.items()}
        self.comments = {key: _by_id(items) for key, items in threads.items()}
        self.responses = dict(responses or {})
        if folders is not None:
            self.responses.setdefault(
                functions.messages.GetDialogFiltersRequest,
                tl_messages.DialogFilters(filters=[types.DialogFilterDefault(), *folders]),
            )
        self.failures: dict[Any, Any] = dict(failures or {})
        self.entity_errors: dict[Any, BaseException] = dict(entity_errors or {})
        self.downloads: dict[tuple[int, int], bytes | BaseException] = dict(downloads or {})
        self.authorized = authorized
        self.me = me
        self.two_factor = two_factor
        self.strict_entities = strict_entities
        self.resolved: set[int] = set()
        self.seeded: dict[int, int] = {}
        self.session = FakeSession(self)
        self.connected = False
        self.flood_sleep_threshold = 120
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[Any] = []
        self.start_inputs: dict[str, str] = {}

    # --- connection and auth ---------------------------------------------------------------

    async def connect(self) -> None:
        self.calls.append(("connect", {}))
        self.connected = True

    async def disconnect(self) -> None:
        self.calls.append(("disconnect", {}))
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def is_user_authorized(self) -> bool:
        self.calls.append(("is_user_authorized", {}))
        return self.authorized

    async def get_me(self) -> types.User | None:
        self.calls.append(("get_me", {}))
        return self.me if self.authorized else None

    async def log_out(self) -> bool:
        """``auth.logOut``: the authorization ends and the client disconnects, as Telethon's
        does; ``False`` for a session that holds none."""
        self.calls.append(("log_out", {}))
        was = self.authorized
        self.authorized = False
        self.connected = False
        return was

    async def start(
        self,
        phone: Any = None,
        password: Any = None,
        *,
        code_callback: Any = None,
        **_: Any,
    ) -> "FakeClient":
        self.calls.append(("start", {}))
        self.connected = True
        if self.authorized:
            return self
        self.start_inputs["phone"] = await _answer(phone)
        self.start_inputs["code"] = await _answer(code_callback)
        if self.two_factor:
            self.start_inputs["password"] = await _answer(password)
        self.authorized = True
        return self

    # --- dialogs and entities --------------------------------------------------------------

    async def get_dialogs(
        self, *_: Any, ignore_migrated: bool = False, **__: Any
    ) -> list[custom.Dialog]:
        """The dialogs; with ``ignore_migrated`` a legacy group upgraded to a supergroup is
        left out, as Telethon does. Yields to the event loop once, as a request would, so
        concurrent callers interleave the way they do against Telegram."""
        self.calls.append(("get_dialogs", {}))
        await asyncio.sleep(0)
        self._learn(dialog.entity for dialog in self.dialogs)
        return [
            dialog
            for dialog in self.dialogs
            if not (
                ignore_migrated
                and isinstance(dialog.entity, types.Chat)
                and dialog.entity.migrated_to is not None
            )
        ]

    async def get_entity(self, key: Any) -> Any:
        """The entity behind a username, a marked id or a peer. A bare id is held to what
        Telethon can do with it (:meth:`_require_resolved`): only a peer the session already
        knows — from a dialog, an answer, or an access hash seeded into it — resolves; a
        username is looked up the way ``contacts.resolveUsername`` would."""
        self.calls.append(("get_entity", {"key": key}))
        error = self.entity_errors.get(key) if isinstance(key, int | str) else None
        if error is not None:
            raise error
        if isinstance(key, int):
            self._require_resolved(key)
        entity = self._find_entity(key)
        if entity is None:
            raise ValueError(f"Could not find the input entity for {key!r}")
        self._learn([entity])
        return entity

    # --- messages --------------------------------------------------------------------------

    async def iter_messages(
        self,
        entity: Any,
        limit: float | None = None,
        *,
        offset_date: dt.datetime | None = None,
        offset_id: int = 0,
        max_id: int = 0,
        min_id: int = 0,
        ids: int | list[int] | None = None,
        reverse: bool = False,
        reply_to: int | None = None,
        filter: Any = None,
        **_: Any,
    ) -> AsyncIterator[types.Message | None]:
        chat_id = self._peer_id(entity)
        self.calls.append(
            (
                "iter_messages",
                {
                    "chat_id": chat_id,
                    "limit": limit,
                    "offset_date": offset_date,
                    "offset_id": offset_id,
                    "max_id": max_id,
                    "min_id": min_id,
                    "ids": ids,
                    "reverse": reverse,
                    "reply_to": reply_to,
                    "filter": filter,
                },
            )
        )
        if not self._may_read(chat_id):
            raise errors.ChannelPrivateError(request=None)
        failure = self.failures.get(chat_id)
        if reply_to is not None and (chat_id, reply_to) in self.failures:
            failure = self.failures[(chat_id, reply_to)]
        fail_after, error = failure if isinstance(failure, tuple) else (0, failure)
        if error is not None and fail_after == 0:
            raise error
        if reply_to is not None:
            try:
                pool = self.comments[(chat_id, reply_to)]
            except KeyError:
                raise errors.MsgIdInvalidError(request=None) from None
        else:
            pool = self.messages.get(chat_id, [])
        if ids is not None:
            by_id = {m.id: m for m in pool}
            for wanted in [ids] if isinstance(ids, int) else ids:
                found = by_id.get(wanted)
                if found is not None:
                    self._attach_peers(found)
                yield found
            return
        selected = [
            m for m in pool if (not min_id or m.id > min_id) and (not max_id or m.id < max_id)
        ]
        if filter is not None:
            # messages.search with a filter; the only one grepogram sends is the pinned one
            kind = filter if isinstance(filter, type) else type(filter)
            if kind is not types.InputMessagesFilterPinned:
                raise NotImplementedError(f"FakeClient has no search filter {kind.__name__}")
            selected = [m for m in selected if getattr(m, "pinned", False)]
        # offset_id (and min_id, which Telethon turns into one) takes priority over offset_date.
        # Reversed, the date bound is inclusive: Telethon 1.44 passes offset_date to
        # GetHistoryRequest untouched and filters nothing by date, so the chunk is the complement
        # of the server's exclusive "before this date" cut — while the id offset it does
        # compensate by hand (`offset_id += 1` in _MessagesIter._init) to stay exclusive.
        by_date = offset_date is not None and not offset_id and not min_id
        if reverse:
            selected.sort(key=lambda m: m.id)
            if offset_id:
                selected = [m for m in selected if m.id > offset_id]
            if by_date:
                selected = [m for m in selected if m.date >= offset_date]
        else:
            selected.sort(key=lambda m: m.id, reverse=True)
            if offset_id:
                selected = [m for m in selected if m.id < offset_id]
            if by_date:
                selected = [m for m in selected if m.date < offset_date]
        if limit is not None:
            selected = selected[: int(limit)]
        for yielded, message in enumerate(selected):
            if error is not None and yielded == fail_after:
                raise error
            self._attach_peers(message)
            yield message

    async def get_messages(
        self,
        entity: Any,
        limit: int | None = None,
        *,
        ids: int | list[int] | None = None,
        **kwargs: Any,
    ) -> Any:
        """``iter_messages`` collected, with Telethon's return shapes.

        A list of ``ids`` answers a list holding ``None`` where a message is deleted or was
        never stored — the signal the extraction pass and ``prune-deleted`` read — and a single
        int ``ids`` answers that one message or ``None``. Without ``ids`` Telethon defaults the
        limit to one message, and so does this.
        """
        self.calls.append(
            ("get_messages", {"chat_id": self._peer_id(entity), "limit": limit, "ids": ids})
        )
        if ids is None and limit is None:
            limit = 1
        got = [m async for m in self.iter_messages(entity, limit=limit, ids=ids, **kwargs)]
        if isinstance(ids, int):
            return got[0] if got else None
        return got

    async def download_media(self, message: Any, file: Any = None, **_: Any) -> str | None:
        """Write the bytes registered for ``message`` to ``file`` and answer the path it took.

        ``file`` is a path, never a directory: the extraction pass names its own temp file after
        the media so the extractor can dispatch on the extension. A message with nothing
        registered downloads as ``None``, which is what Telethon answers for media it cannot
        write out.
        """
        key = (int(message.chat_id), int(message.id))
        self.calls.append(("download_media", {"chat_id": key[0], "msg_id": key[1], "file": file}))
        payload = self.downloads.get(key)
        if isinstance(payload, BaseException):
            raise payload
        if payload is None:
            return None
        path = Path(file)
        path.write_bytes(payload)
        return str(path)

    def _attach_peers(self, message: types.Message) -> None:
        """Bind the sender, chat and forward-origin entities the way Telethon's
        ``_finish_init`` does.

        The real client fills ``message.sender`` / ``message.chat`` from the ``users`` and
        ``chats`` lists Telegram returns with each history chunk, and ``message.forward`` — a
        ``custom.Forward`` — with the origin from the same lists; here they come from the
        entities this client knows, which carry this account's access hashes. Those lists teach
        the session the peers they hold, so an origin bound here is learned too.
        """
        if message.sender_id is not None:
            message._sender = self.entities.get(message.sender_id)
        message._chat = self.entities.get(message.chat_id)
        fwd = message.fwd_from
        if fwd is not None and fwd.from_id is not None:
            origin = self.entities.get(int(utils.get_peer_id(fwd.from_id)))
            known = {} if origin is None else {int(utils.get_peer_id(origin)): origin}
            message._forward = custom.Forward(_ENTITY_CACHE, fwd, known)
            if origin is not None:
                self._learn([origin])

    # --- raw requests ----------------------------------------------------------------------

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        for cls, response in self.responses.items():
            if isinstance(request, cls):
                if isinstance(response, BaseException):
                    raise response
                answer = response(request) if callable(response) else response
                self._learn_answer(answer)
                return answer
        answer = self._research_answer(request)
        if answer is None:
            raise NotImplementedError(f"FakeClient has no response for {type(request).__name__}")
        self._learn_answer(answer)
        return answer

    def _learn_answer(self, answer: Any) -> None:
        """Learn what Telethon's ``_entities_to_rows`` reads off an answer: ``user``, ``chat``,
        ``chats`` and ``users``."""
        single = [getattr(answer, name, None) for name in ("user", "chat")]
        self._learn(
            [
                *(entity for entity in single if entity is not None),
                *(getattr(answer, "chats", None) or ()),
                *(getattr(answer, "users", None) or ()),
            ]
        )

    # --- raw requests research sends --------------------------------------------------------

    def _research_answer(self, request: Any) -> Any:
        """The world's answer to a request research sends, or ``None`` for any other."""
        if isinstance(request, functions.messages.CheckChatInviteRequest):
            return self._check_invite(request)
        if isinstance(request, functions.chatlists.CheckChatlistInviteRequest):
            return self._check_chatlist(request)
        if isinstance(request, functions.channels.GetChannelsRequest):
            return self._get_channels(request)
        if isinstance(request, functions.messages.GetChatsRequest):
            return self._get_chats(request)
        if isinstance(request, functions.contacts.SearchRequest):
            return self._search_chats(request)
        if isinstance(request, functions.channels.CheckSearchPostsFloodRequest):
            return self.search_flood
        if isinstance(request, functions.channels.SearchPostsRequest):
            return self._search_posts(request)
        if isinstance(request, functions.channels.JoinChannelRequest):
            return self._join_channel(request)
        if isinstance(request, functions.messages.ImportChatInviteRequest):
            return self._import_invite(request)
        if isinstance(request, functions.chatlists.JoinChatlistInviteRequest):
            return self._join_chatlist(request, request.slug, request.peers)
        if isinstance(request, functions.chatlists.JoinChatlistUpdatesRequest):
            return self._join_chatlist(request, self._imported(request), request.peers)
        return None

    # --- joining ---------------------------------------------------------------------------

    def join(self, entity: Any) -> Any:
        """Make this account a member of ``entity``: a dialog, the member's view of the entity
        (``left`` cleared) and its history readable — what a join, or an admin admitting an
        admission request, does. Answers the entity as the account now sees it."""
        marked = int(utils.get_peer_id(entity))
        if self.world is not None:
            seen = self.world.seen_by(self.account, self.world.entities.get(marked, entity))
        else:
            seen = entity
        self.entities[marked] = seen
        if marked not in self.members:
            self.members.add(marked)
            self.dialogs.append(make_dialog(seen))
        self.requested.discard(marked)
        self._learn([seen])
        return seen

    def _joined(self, *entities: Any) -> types.Updates:
        joined = [self.join(entity) for entity in entities]
        return types.Updates(updates=[], users=[], chats=joined, date=None, seq=0)

    def _join_channel(self, request: Any) -> Any:
        """``channels.joinChannel``: a public chat anyone joins, one whose admins approve joins
        (``join_requests``, or the channel's own ``join_request`` flag, as Telegram sets it)
        answers with the request sent, and a private one refuses — a join
        by id reaches only what is public. The access hash must be this account's."""
        wanted = request.channel
        marked = int(utils.get_peer_id(types.PeerChannel(wanted.channel_id)))
        entity = self.entities.get(marked)
        if entity is None or getattr(entity, "access_hash", None) != wanted.access_hash:
            raise errors.ChannelInvalidError(request=request)
        if marked in self.members:
            return self._joined(entity)
        if not getattr(entity, "username", None):
            raise errors.ChannelPrivateError(request=request)
        if marked in self.join_requests or getattr(entity, "join_request", False):
            self.requested.add(marked)
            raise errors.InviteRequestSentError(request=request)
        return self._joined(entity)

    def _import_invite(self, request: Any) -> Any:
        """``messages.importChatInvite``: joins the invite's chat, or sends an admission request
        when the invite asks for one; a member already in is ``USER_ALREADY_PARTICIPANT``."""
        invites = self.world.invites if self.world is not None else {}
        found = invites.get(request.hash)
        if found is None:
            raise errors.InviteHashInvalidError(request=request)
        if isinstance(found, BaseException):
            raise found
        marked = int(utils.get_peer_id(found.entity))
        if marked in self.members:
            raise errors.UserAlreadyParticipantError(request=request)
        if found.request_needed:
            self.requested.add(marked)
            raise errors.InviteRequestSentError(request=request)
        return self._joined(found.entity)

    @staticmethod
    def filter_id(slug: str) -> int:
        """The dialog filter an imported folder became for this account — stable per slug, and
        above 1, the ids Telegram keeps for its own filters."""
        return 2 + zlib.crc32(slug.encode()) % 1000

    def _imported(self, request: Any) -> str:
        """The folder ``chatlists.joinChatlistUpdates`` names by its filter id: one this account
        imported, or Telegram refuses the request."""
        wanted = request.chatlist.filter_id
        for slug in self.chatlists_joined:
            if self.filter_id(slug) == wanted:
                return slug
        raise errors.BadRequestError(request, "FILTER_ID_INVALID", 400)

    def _join_chatlist(self, request: Any, slug: str, peers: Iterable[Any]) -> Any:
        """``chatlists.joinChatlistInvite`` (a folder not imported yet, by slug) or
        ``chatlists.joinChatlistUpdates`` (its missing chats): joins exactly ``peers``, which
        must be chats *that folder* lists and carry this account's access hashes, and imports
        the folder."""
        folders = self.world.chatlists if self.world is not None else {}
        folder = folders.get(slug)
        if folder is None or isinstance(folder, BaseException):
            raise errors.BadRequestError(request, "INVITE_SLUG_EXPIRED", 400)
        listed = {int(utils.get_peer_id(e)) for e in folder.entities}
        wanted = [int(utils.get_peer_id(peer)) for peer in peers]
        for peer, marked in zip(peers, wanted, strict=True):
            entity = self.entities.get(marked)
            known = getattr(entity, "access_hash", None)
            if marked not in listed:
                raise errors.BadRequestError(request, "PEER_ID_INVALID", 400)
            if entity is None or getattr(peer, "access_hash", known) != known:
                raise errors.ChannelInvalidError(request=request)
        self.chatlists_joined.add(slug)
        self.chatlist_joins.append(wanted)
        return self._joined(*(self.entities[marked] for marked in wanted))

    def _check_invite(self, request: Any) -> Any:
        invites = self.world.invites if self.world is not None else {}
        found = invites.get(request.hash)
        if found is None:
            raise errors.InviteHashInvalidError(request=request)
        if isinstance(found, BaseException):
            raise found
        marked = int(utils.get_peer_id(found.entity))
        entity = self.entities.get(marked, found.entity)
        if marked in self.members:
            return types.ChatInviteAlready(chat=entity)
        if found.peek:
            return types.ChatInvitePeek(chat=entity, expires=FAR_FUTURE)
        channel = isinstance(entity, types.Channel)
        count = found.participants
        if count is None:
            count = getattr(entity, "participants_count", None) or 0
        return types.ChatInvite(
            title=entity.title,
            photo=types.PhotoEmpty(id=0),
            participants_count=count,
            color=0,
            channel=channel or None,
            broadcast=(channel and not entity.megagroup) or None,
            megagroup=(channel and bool(entity.megagroup)) or None,
            public=bool(getattr(entity, "username", None)) or None,
            request_needed=found.request_needed or None,
        )

    def _check_chatlist(self, request: Any) -> Any:
        folders = self.world.chatlists if self.world is not None else {}
        found = folders.get(request.slug)
        if found is None:
            raise errors.BadRequestError(request, "INVITE_SLUG_EXPIRED", 400)
        if isinstance(found, BaseException):
            raise found
        entities = [self.entities.get(utils.get_peer_id(e), e) for e in found.entities]
        chats = [e for e in entities if not isinstance(e, types.User)]
        users = [e for e in entities if isinstance(e, types.User)]
        if request.slug in self.chatlists_joined:
            return tl_chatlists.ChatlistInviteAlready(
                filter_id=self.filter_id(request.slug),
                missing_peers=[
                    utils.get_peer(e) for e in entities if utils.get_peer_id(e) not in self.members
                ],
                already_peers=[
                    utils.get_peer(e) for e in entities if utils.get_peer_id(e) in self.members
                ],
                chats=chats,
                users=users,
            )
        return tl_chatlists.ChatlistInvite(
            title=types.TextWithEntities(found.title, []),
            peers=[utils.get_peer(e) for e in entities],
            chats=chats,
            users=users,
        )

    def _get_channels(self, request: Any) -> Any:
        chats = []
        for wanted in request.id:
            marked = int(utils.get_peer_id(types.PeerChannel(wanted.channel_id)))
            entity = self.entities.get(marked)
            if entity is None or getattr(entity, "access_hash", None) != wanted.access_hash:
                raise errors.ChannelInvalidError(request=request)
            chats.append(entity)
        return tl_messages.Chats(chats=chats)

    def _get_chats(self, request: Any) -> Any:
        chats = []
        for bare in request.id:
            marked = int(utils.get_peer_id(types.PeerChat(bare)))
            if marked not in self.members or marked not in self.entities:
                raise errors.ChatIdInvalidError(request=request)
            chats.append(self.entities[marked])
        return tl_messages.Chats(chats=chats)

    def _search_chats(self, request: Any) -> Any:
        """``contacts.search``: chats and users whose name or username holds ``q`` — the
        account's own (``my_results``) and public ones (``results``)."""
        needle = request.q.lower()
        mine: list[Any] = []
        public: list[Any] = []
        for marked, entity in self.entities.items():
            name = utils.get_display_name(entity).lower()
            username = (getattr(entity, "username", None) or "").lower()
            if needle not in name and needle not in username:
                continue
            if marked in self.members:
                mine.append(entity)
            elif username:
                public.append(entity)
        found = [*mine, *public][: request.limit]
        return tl_contacts.Found(
            my_results=[utils.get_peer(e) for e in found if e in mine],
            results=[utils.get_peer(e) for e in found if e not in mine],
            chats=[e for e in found if not isinstance(e, types.User)],
            users=[e for e in found if isinstance(e, types.User)],
        )

    def _search_posts(self, request: Any) -> Any:
        """``channels.searchPosts``: posts of public broadcast channels holding the query,
        newest first. A free search spends one of ``search_flood.remains``; with none left the
        request must carry ``allow_paid_stars``, which this refuses otherwise (the error name is
        this fake's stand-in, grepogram never sends such a request)."""
        flood = self.search_flood
        if not (flood.query_is_free or flood.remains > 0):
            if not request.allow_paid_stars:
                raise errors.BadRequestError(request, "ALLOW_PAYMENT_REQUIRED", 400)
        elif not flood.query_is_free:
            self.search_flood = types.SearchPostsFlood(
                total_daily=flood.total_daily,
                remains=flood.remains - 1,
                stars_amount=flood.stars_amount,
                wait_till=flood.wait_till,
            )
        needle = (request.query or "").lower()
        posts: list[types.Message] = []
        chats: dict[int, Any] = {}
        for chat_id, history in self.messages.items():
            entity = self.entities.get(chat_id)
            if not isinstance(entity, types.Channel) or entity.megagroup or not entity.username:
                continue
            for message in history:
                if needle and needle in (message.message or "").lower():
                    posts.append(message)
                    chats[chat_id] = entity
        posts.sort(key=lambda m: m.date, reverse=True)
        posts = posts[: request.limit]
        return tl_messages.MessagesSlice(
            count=len(posts),
            messages=posts,
            topics=[],
            chats=[chats[int(utils.get_peer_id(m.peer_id))] for m in posts],
            users=[],
            search_flood=self.search_flood,
        )

    # --- the session's entity cache ---------------------------------------------------------

    def forget_entities(self) -> None:
        """Start over with an empty entity cache, as every freshly built client does.

        ``tg.make_client`` hands out a private in-memory copy of the session file holding the
        data centre and the auth key alone, so a CLI command that runs after a sync — ``extract``
        and ``prune-deleted`` are the two — begins knowing no peer at all. A test that fills an
        index through one pass and then exercises another must call this in between, or it
        measures a cache the second pass would never have.
        """
        self.resolved.clear()
        self.seeded.clear()

    def _learn(self, entities: Iterable[Any]) -> None:
        """Cache the peers of an answer, as Telethon's ``session.process_entities`` does.

        Every RPC result passes through it in the real client, which is why one ``get_dialogs()``
        is enough to make every dialog addressable by bare id afterwards, and why a
        ``GetFullChannelRequest`` makes the discussion group in its ``chats`` addressable even
        when the account never joined it. A legacy group brings the supergroup it migrated to,
        the way Telegram returns both in one ``chats`` list.
        """
        for entity in entities:
            try:
                self.resolved.add(int(utils.get_peer_id(entity)))
            except (TypeError, AttributeError):
                continue
            target = getattr(entity, "migrated_to", None)
            if target is not None:
                self.resolved.add(utils.get_peer_id(types.PeerChannel(target.channel_id)))

    def _require_resolved(self, marked_id: int) -> None:
        """Refuse a peer this client never learned, the way Telethon 1.44 refuses one.

        ``tg.load_session`` copies the data centre and the auth key out of the session file and
        nothing else, so the entity cache of every client grepogram builds starts empty. With
        it empty, ``get_input_entity`` finds no access hash and its network fallback
        (``channels.getChannels`` / ``users.getUsers`` with ``access_hash = 0``) answers only
        for a bot's private chats or a contact — for a user session on a private supergroup it
        ends in ``ValueError: Could not find the input entity``. A legacy group is the one id
        that needs no hash: Telethon turns a ``PeerChat`` straight into an ``InputPeerChat``.

        This is what ``FakeClient`` used to be more permissive than the real client about, and
        it is why nine review rounds passed over a ``grepogram extract`` that resolved no chat
        at all on a real account — and, while ``get_entity(<id>)`` still answered for any peer
        of the world, why a sync whose primary account could not resolve a group looked as if it
        fell back to another account when it silently moved the group's primary source instead.
        """
        if not self.strict_entities or marked_id in self.resolved:
            return
        kind = utils.resolve_id(marked_id)[1]
        if kind is types.PeerChat:
            return
        if marked_id in self.seeded:
            known = getattr(self.entities.get(marked_id), "access_hash", None)
            if known is not None and known == self.seeded[marked_id]:
                self.resolved.add(marked_id)
                return
            # a hash that is not this account's: Telegram refuses the request it went out in
            if kind is types.PeerChannel:
                raise errors.ChannelInvalidError(request=None)
            raise errors.PeerIdInvalidError(request=None)
        raise ValueError(f"Could not find the input entity for {marked_id!r}")

    def _may_read(self, marked_id: int) -> bool:
        """Whether this account may read a world chat's history: always, but for a private
        channel or supergroup (no username) it has no dialog with."""
        if self.world is None:
            return True
        entity = self.world.entities.get(marked_id)
        if not isinstance(entity, types.Channel) or entity.username:
            return True
        return marked_id in self.members

    # --- helpers ---------------------------------------------------------------------------

    def _peer_id(self, entity: Any) -> int:
        if isinstance(entity, int):
            self._require_resolved(entity)
            return entity
        if isinstance(entity, str):
            found = self._find_entity(entity)
            if found is None:
                raise ValueError(f"Could not find the input entity for {entity!r}")
            return int(utils.get_peer_id(found))
        return int(utils.get_peer_id(entity))

    def _find_entity(self, key: Any) -> Any:
        if isinstance(key, int):
            return self.entities.get(key)
        if isinstance(key, str):
            if key.lstrip("-").isdigit():
                return self.entities.get(int(key))
            name, _ = utils.parse_username(key)
            if not name:
                return None
            for entity in self.entities.values():
                username = getattr(entity, "username", None)
                if username and username.lower() == name.lower():
                    return entity
            return None
        return self.entities.get(utils.get_peer_id(key))


class FakeSession:
    """The part of Telethon's ``MemorySession`` grepogram writes to: ``process_entities``.

    Telethon turns each ``InputPeerUser`` / ``InputPeerChannel`` it is given into a cache row, so
    a later request naming that peer by bare id goes out with the stored access hash; this
    records the hash in :attr:`FakeClient.seeded`, where :meth:`FakeClient._require_resolved`
    checks it against the account's own.
    """

    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def process_entities(self, tlo: Any) -> None:
        entities = tlo if isinstance(tlo, list | tuple) else [tlo]
        for entity in entities:
            if isinstance(entity, types.InputPeerUser | types.InputPeerChannel):
                self._client.seeded[int(utils.get_peer_id(entity))] = int(entity.access_hash)


class _NoCache:
    """What ``custom.Forward`` asks a client for: an entity cache that knows nothing, so the
    origin comes from the answer's own lists, as it does for a fresh client."""

    _mb_entity_cache: dict[int, Any] = {}


_ENTITY_CACHE = _NoCache()


def _by_id(messages: Iterable[types.Message]) -> list[types.Message]:
    return sorted(messages, key=lambda m: m.id)


async def _answer(prompt: Any) -> str:
    value = prompt() if callable(prompt) else prompt
    if inspect.isawaitable(value):
        value = await value
    return str(value)
