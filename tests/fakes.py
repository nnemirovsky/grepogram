"""Client-free stand-ins for Telethon.

Tests never touch the network: ``FakeClient`` answers the handful of ``TelegramClient`` calls
grepogram makes from in-memory fixtures, and the ``make_*`` helpers build real Telethon TL
objects (``types.User``, ``types.Channel``, ``custom.Dialog``) without a client attached.
"""

import asyncio
import datetime as dt
import inspect
from collections.abc import AsyncIterator, Iterable, Mapping
from pathlib import Path
from typing import Any

from telethon import errors, utils
from telethon.tl import custom, functions, types
from telethon.tl.types import messages as tl_messages

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
    ) -> None:
        self.dialogs = list(dialogs)
        self.entities: dict[int, Any] = {}
        for entity in entities:
            self.entities[utils.get_peer_id(entity)] = entity
        for dialog in self.dialogs:
            self.entities.setdefault(dialog.id, dialog.entity)
        self.messages = {chat_id: _by_id(items) for chat_id, items in (messages or {}).items()}
        self.comments = {key: _by_id(items) for key, items in (comments or {}).items()}
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
        self.calls.append(("get_entity", {"key": key}))
        error = self.entity_errors.get(key) if isinstance(key, int | str) else None
        if error is not None:
            raise error
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
                },
            )
        )
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
                yield by_id.get(wanted)
            return
        selected = [
            m for m in pool if (not min_id or m.id > min_id) and (not max_id or m.id < max_id)
        ]
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
        """Bind the sender and chat entities the way Telethon's ``_finish_init`` does.

        The real client fills ``message.sender`` / ``message.chat`` from the ``users`` and
        ``chats`` lists Telegram returns with each history chunk; here they come from the
        entities this client knows. Forward origins stay unbound (``Forward`` needs a client).
        """
        if message.sender_id is not None:
            message._sender = self.entities.get(message.sender_id)
        message._chat = self.entities.get(message.chat_id)

    # --- raw requests ----------------------------------------------------------------------

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        for cls, response in self.responses.items():
            if isinstance(request, cls):
                if isinstance(response, BaseException):
                    raise response
                answer = response(request) if callable(response) else response
                self._learn(
                    [
                        *(getattr(answer, "chats", None) or ()),
                        *(getattr(answer, "users", None) or ()),
                    ]
                )
                return answer
        raise NotImplementedError(f"FakeClient has no response for {type(request).__name__}")

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
        at all on a real account.
        """
        if not self.strict_entities or marked_id in self.resolved:
            return
        if utils.resolve_id(marked_id)[1] is types.PeerChat:
            return
        raise ValueError(f"Could not find the input entity for {marked_id!r}")

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


def _by_id(messages: Iterable[types.Message]) -> list[types.Message]:
    return sorted(messages, key=lambda m: m.id)


async def _answer(prompt: Any) -> str:
    value = prompt() if callable(prompt) else prompt
    if inspect.isawaitable(value):
        value = await value
    return str(value)
