"""Accounts: who each signed-in account is, and which one a pass asks about a chat through.

Every Telegram-facing pass — a sync, the passes over stored chats (``prune-deleted``,
``extract``, ``recapture-links``), the folder read of ``sources prune``, ``grepogram leave`` and
every research pass — works through one client per configured account, and this is the layer
they share for that. :func:`check_account` and :func:`ask_account` make sure a client is the
Telegram user the index recorded under its name before anything acts through it
(:func:`signed_in_user` is the one question both put). :func:`reaching_accounts` names the
accounts that may be asked about a chat, in order, and :func:`through_accounts` asks them until
one answers — a flood wait stops an account for the rest of the pass, a refusal of a shared chat
moves on to the next. :class:`StoredPass` is that routing for a pass that walks ``chats`` rows
rather than a source list, and :func:`warm_peer_cache` teaches a client the peers it is about to
address by bare id. :func:`labels_accounts`, :func:`account_warning` and :func:`flood_warning`
are how every report words an account and a flood wait.

Nothing here fetches, stores or indexes a message: that is :mod:`grepogram.sync`, which imports
this module and never the other way round.
"""

import logging
import math
import sqlite3
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from telethon import errors, utils
from telethon.tl import functions

from grepogram import db, tg
from grepogram.config import ConfigError
from grepogram.models import DEFAULT_ACCOUNT, ChatRow, SyncCfg
from grepogram.sources import IMPORT_PREFIX, seed_peers, source_account

log = logging.getLogger(__name__)


class Budget(Protocol):
    """What routing needs from a :class:`~grepogram.sync.SyncBudget`: whether it ran out, and
    how many seconds it has left (``None`` for an unlimited one)."""

    @property
    def expired(self) -> bool: ...

    @property
    def remaining(self) -> float | None: ...


UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    errors.ChannelPrivateError,
    errors.ChatAdminRequiredError,
    errors.ChannelInvalidError,
    errors.ChatForbiddenError,
)
"""What Telegram answers when it refuses a chat to an account: :func:`~grepogram.sync.sync_chat`
marks the chat ``unavailable`` and reports it, and :data:`REROUTE_ERRORS` tries a shared one
through the next account that reaches it."""

REROUTE_ERRORS: tuple[type[Exception], ...] = (ValueError, *UNAVAILABLE_ERRORS)
"""What sends :func:`through_accounts` on to the next account that reaches a shared chat: a
refusal, or a peer this account's client cannot address at all."""


# --- who an account is ----------------------------------------------------------------------


def other_user(conn: sqlite3.Connection, account: str, user_id: int) -> tg.OtherUser | None:
    """:class:`~grepogram.tg.OtherUser` when ``user_id`` is not the Telegram user the index
    recorded under ``account``; ``None`` when it is, or when no user is recorded yet (a
    ``default`` from before accounts existed, a name never synced), which takes whoever signs
    in."""
    recorded = db.conflicting_account(conn, account, user_id)
    if recorded is None:
        return None
    assert recorded.user_id is not None  # a row with no user recorded takes anyone
    return tg.OtherUser(account, user_id, recorded.user_id)


def _other_user_text(refused: tg.OtherUser) -> str:
    """How a report words an account left out for being another Telegram user: the refusal's
    own reason and hint (:class:`~grepogram.tg.OtherUser`), not a second phrasing of them."""
    return f"{refused.reason} — {refused.hint}"


async def signed_in_user(conn: sqlite3.Connection, account: str, client: Any) -> Any | None:
    """Ask Telegram who ``client`` — connected, signed in as ``account`` — is, and return that
    user, or ``None`` when Telegram names none; raises :class:`~grepogram.tg.OtherUser` when the
    index recorded someone else under that name, and :class:`~grepogram.tg.AuthRequired` naming
    ``account`` for a rejected session. The question every identity check puts
    (:func:`check_account`, :func:`ask_account`); any other Telegram error propagates."""
    try:
        me = await client.get_me()
    except errors.UnauthorizedError as exc:
        tg.reraise_unauthorized(exc, account)
    if me is not None:
        refused = other_user(conn, account, int(me.id))
        if refused is not None:
            raise refused
    return me


async def check_account(conn: sqlite3.Connection, account: str, client: Any) -> None:
    """Make sure ``client`` — connected, signed in as ``account`` — is the Telegram user the
    index recorded under that name before anything acts through it; raises
    :class:`~grepogram.tg.OtherUser` when it is someone else.

    The one identity check every Telegram-facing pass goes through: a sync
    (:func:`ask_account` with ``always``, which then records a first sign-in), the passes over
    stored chats (:meth:`StoredPass.start`: ``prune-deleted``, ``extract``,
    ``recapture-links``), the folder read of ``sources prune`` (both through
    :func:`checked_accounts`), ``grepogram leave``, and every research pass that talks to
    Telegram — a run, a discover, a global search. A session file copied into place by hand, or
    one ``grepogram auth`` committed before this index recorded anyone, would otherwise delete,
    join, leave or ask as a user no one chose. Nothing is sent when no user is recorded yet; a
    rejected session is :class:`~grepogram.tg.AuthRequired` naming ``account``, and any other
    Telegram error propagates."""
    if db.get_account(conn, account) is not None:
        await _recorded_user(conn, account, client)


async def _recorded_user(conn: sqlite3.Connection, account: str, client: Any) -> Any:
    """:func:`signed_in_user` for an account the index recorded: a session that names no user
    cannot be checked, and is :class:`~grepogram.tg.AuthRequired`."""
    me = await signed_in_user(conn, account, client)
    if me is None:
        raise tg.AuthRequired(account=account)
    return me


@dataclass(frozen=True, slots=True)
class AccountAnswer:
    """What asking who an account is came to (:func:`ask_account`): ``me``, the Telegram user
    it is, when it may act and was asked; otherwise ``left_out``, why it sits the pass out, with
    ``other_user`` set when that is because it is another Telegram user than the index recorded
    — refused for good — rather than a Telegram error on the question itself."""

    me: Any | None = None
    left_out: str | None = None
    other_user: bool = False


async def ask_account(
    conn: sqlite3.Connection, account: str, client: Any, *, always: bool = False
) -> AccountAnswer:
    """:func:`check_account` for a pass that goes on without the account it refuses: the
    refusal — a session of another Telegram user, or a Telegram error (a flood wait included) on
    the question itself, since an account that cannot say who it is is not trusted to act — is
    logged and worded for the report instead of raised. ``always`` asks even when no user is
    recorded under ``account`` yet, for the sync that records the first sign-in — which takes a
    session naming no user as one with nothing to record. A rejected session is still raised as
    :class:`~grepogram.tg.AuthRequired`."""
    try:
        if always:
            return AccountAnswer(me=await signed_in_user(conn, account, client))
        if db.get_account(conn, account) is not None:
            return AccountAnswer(me=await _recorded_user(conn, account, client))
    except tg.OtherUser as exc:
        log.warning("%s; it sits this pass out", exc.reason)
        return AccountAnswer(left_out=_other_user_text(exc), other_user=True)
    except errors.FloodWaitError as exc:
        log.warning("flood wait of %ss asking who account %s is", exc.seconds, account)
        return AccountAnswer(
            left_out=flood_warning(
                flood_seconds(exc), "asking who the account is", "it sat this pass out"
            )
        )
    except errors.RPCError as exc:
        log.warning("account %s could not say who it is: %s", account, exc)
        return AccountAnswer(left_out=f"it could not say who it is ({exc}); it sat this pass out")
    return AccountAnswer()


async def checked_accounts(
    conn: sqlite3.Connection, clients: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """``clients`` without the accounts :func:`ask_account` leaves out, and why each one was.
    A rejected session is raised as :class:`~grepogram.tg.AuthRequired`."""
    kept: dict[str, Any] = {}
    left_out: dict[str, str] = {}
    for account, client in clients.items():
        answer = await ask_account(conn, account, client)
        if answer.left_out is not None:
            left_out[account] = answer.left_out
        else:
            kept[account] = client
    return kept, left_out


# --- how a report names an account -----------------------------------------------------------


def labels_accounts(accounts: Collection[str]) -> bool:
    """Whether a pass over ``accounts`` names the account in each warning: whenever it holds any
    account but the default one, so a report of several accounts says whose request a warning is
    about, while a single-account install reads exactly what it always did."""
    return any(account != DEFAULT_ACCOUNT for account in accounts)


def account_warning(labelled: bool, account: str, warning: str) -> str:
    """``warning`` as a report prints it: ``account <name>:`` in front when ``labelled``."""
    return f"account {account}: {warning}" if labelled else warning


def flood_warning(seconds: int | None, before: str, then: str) -> str:
    """The one wording of a flood wait in a report: how long Telegram asks to wait — "a while"
    when it named no time —, before what, and what that means for the caller."""
    wait = "a while" if seconds is None else f"{seconds}s"
    return f"flood wait: Telegram asks to wait {wait} before {before}; {then}"


def flood_seconds(exc: errors.FloodError) -> int | None:
    """How long a flood error asks to wait, or ``None`` when it names no time: a
    ``FloodWaitError`` carries ``seconds``, while the wider ``FloodError`` family a research
    pass catches (a plain 420, ``FloodTestPhoneWaitError``…) need not. The one reader of it, so
    :func:`flood_warning` words both alike."""
    seconds = getattr(exc, "seconds", None)
    return int(seconds) if isinstance(seconds, int) else None


# --- which account asks about a chat ---------------------------------------------------------


def recorded_reach(conn: sqlite3.Connection, chat: ChatRow) -> list[str]:
    """The accounts the index records as reaching ``chat``, in the order to ask them.

    A private chat, a bot or a legacy group is its scope account's history and nobody else's —
    another account's chat with the same person carries other message ids. A channel or
    supergroup is one shared row: the account of its primary source goes first (the one a sync
    fetches it through), then every account ``chat_access`` records as reaching it, then — for a
    discussion group, which a sync reaches through its channel's link rather than a resolve of
    its own and so may have no access row — the accounts that reach the channel it holds the
    comments of, and the channel's primary source's. Empty for a shared row nothing ties to any
    account at all (one built by hand, or by a link to a channel the index no longer holds).
    """
    if not chat.is_shared:
        return [chat.scope]
    order: list[str] = []
    if chat.source_id and not chat.source_id.startswith(IMPORT_PREFIX):
        order.append(source_account(chat.source_id))
    order += db.chat_accounts(conn, chat.id)
    if chat.discussion_of is not None:
        order += db.chat_accounts(conn, chat.discussion_of)
        channel = db.get_chat(conn, chat.discussion_of)
        if channel is not None and channel.source_id:
            order.append(source_account(channel.source_id))
    return list(dict.fromkeys(order))


def reaching_accounts(
    conn: sqlite3.Connection, chat: ChatRow, connected: Collection[str]
) -> list[str]:
    """The accounts of ``connected`` a pass may ask about ``chat`` through, in the order to try.

    :func:`recorded_reach`, narrowed to ``connected`` — so a private chat is asked through its own
    account or not at all, never through a defaulted one. A shared row nothing ties to any account
    is anyone's to ask about: every connected account, the default one first. The one order a sync
    (:func:`~grepogram.sync._fetch_chat`), the extraction pass and the deletion sweep all follow.
    """
    order = recorded_reach(conn, chat)
    if not order:
        order = sorted(connected, key=lambda account: (account != DEFAULT_ACCOUNT, account))
    return [account for account in order if account in connected]


@dataclass(frozen=True, slots=True)
class Refusal:
    """One account a shared chat was refused to — why, and the error when it was one."""

    account: str
    reason: str
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class Flooded:
    """An account a flood wait stopped, and the seconds Telegram asked to wait."""

    account: str
    seconds: int


@dataclass(frozen=True, slots=True)
class Failed:
    """The account whose error ended a chat's turn outright, and that error."""

    account: str
    error: Exception


@dataclass(frozen=True, slots=True)
class RefusedAnswer[T]:
    """An account that answered, with a result the caller reads as a refusal."""

    account: str
    result: T


@dataclass(slots=True)
class Attempt[T]:
    """How :func:`through_accounts` went for one chat.

    ``account`` and ``result`` are who answered and what, when someone did. ``refusals`` are the
    accounts it was refused to on the way, in order, and ``refused`` the first refusal that came as
    an answer rather than an error (a :class:`~grepogram.sync.SyncedChat` marked ``unavailable``).
    ``flooded`` holds each account a flood wait stopped, with the seconds asked, and ``failure`` the
    error that ended the chat's turn outright.
    """

    account: str | None = None
    result: T | None = None
    refusals: list[Refusal] = field(default_factory=list)
    refused: RefusedAnswer[T] | None = None
    flooded: list[Flooded] = field(default_factory=list)
    failure: Failed | None = None


def _never(_: object) -> bool:
    return False


def cap_flood_sleep(client: Any, sync_cfg: SyncCfg, budget: Budget) -> None:
    """Never let Telethon sleep through a flood wait longer than the budget has left.

    ``flood_sleep_threshold`` is what the client sleeps through on its own; a wait above it raises
    :class:`~telethon.errors.FloodWaitError`, which :func:`~grepogram.sync._sync_chats` turns into a
    warning. Inside a bounded run the threshold shrinks with the time left, so a 20-second auto-sync
    never blocks a search for the two minutes the config allows an unattended sync.
    """
    remaining = budget.remaining
    threshold = sync_cfg.flood_sleep_threshold
    if remaining is not None:
        threshold = min(threshold, math.ceil(remaining))
    client.flood_sleep_threshold = threshold


async def through_accounts[T](
    conn: sqlite3.Connection,
    clients: Mapping[str, Any],
    chat: ChatRow,
    route: Sequence[str],
    sync_cfg: SyncCfg,
    budget: Budget,
    stopped: set[str],
    act: Callable[[str, Any], Awaitable[T]],
    *,
    refused: Callable[[T], bool] = _never,
    halted: Callable[[], bool] | None = None,
    warmed: Collection[str] = (),
) -> Attempt[T]:
    """``act(account, client)`` through the first account of ``route`` that answers about
    ``chat`` — the one rule every pass that may take a chat through several accounts follows.

    Accounts in ``stopped`` are passed over. Before each attempt the client's flood-sleep
    threshold is capped against the budget (:func:`cap_flood_sleep`) and, unless the caller
    warmed it already (``warmed``), the client is taught the chat's peer
    (:func:`warm_peer_cache`). Then:

    * a flood wait stops that account — added to ``stopped`` for the rest of the pass — and the
      next account is tried;
    * a rejected session is raised naming its account (:func:`~grepogram.tg.reraise_unauthorized`);
    * a shared chat refused to the account (:data:`REROUTE_ERRORS`), or answered with a result
      ``refused`` says is a refusal, is tried through the next account;
    * any other Telegram error, or any refusal of a private chat, ends the chat's turn.

    A :class:`~grepogram.config.ConfigError` is the caller's to raise, and ``halted`` (the
    clock alone by default) ends the walk before the next account — the first is the caller's to
    decide on, which every caller does just before it asks.
    """
    outcome: Attempt[T] = Attempt()
    stop = halted if halted is not None else (lambda: budget.expired)
    tried = False
    for account in route:
        if account in stopped:
            continue
        if tried and stop():
            break
        tried = True
        client = clients[account]
        cap_flood_sleep(client, sync_cfg, budget)
        try:
            if account not in warmed:
                await warm_peer_cache(client, [chat], conn, account)
            result = await act(account, client)
        except ConfigError:
            raise
        except errors.FloodWaitError as exc:
            log.warning(
                "flood wait of %ss on chat %s through account %s; stopping it for this run",
                exc.seconds,
                chat.id,
                account,
            )
            stopped.add(account)
            outcome.flooded.append(Flooded(account, int(exc.seconds)))
            continue
        except errors.UnauthorizedError as exc:
            tg.reraise_unauthorized(exc, account)
        except (errors.RPCError, ValueError) as exc:
            log.warning("chat %s (%s) through account %s: %s", chat.id, chat.title, account, exc)
            if chat.is_shared and isinstance(exc, REROUTE_ERRORS):
                outcome.refusals.append(Refusal(account, str(exc), exc))
                continue
            outcome.failure = Failed(account, exc)
            return outcome
        if chat.is_shared and refused(result):
            outcome.refusals.append(Refusal(account, "Telegram refused it"))
            if outcome.refused is None:
                outcome.refused = RefusedAnswer(account, result)
            continue
        outcome.account, outcome.result = account, result
        return outcome
    return outcome


@dataclass(slots=True, eq=False)
class StoredPass:
    """Which account a pass over stored chats asks about each chat through.

    :func:`~grepogram.sync.prune_deleted` and :func:`grepogram.media.run` walk ``chats`` rows rather
    than a source list and re-fetch by id, so each chat needs an account that reaches it and a
    client that can address it. :meth:`start` routes every chat (:func:`reaching_accounts`) and
    warms each client with the chats it is asked about first — or, with ``every``, with every chat
    it may be asked about at all (:func:`warm_peer_cache`, seeded from the stored access hashes
    before any request); a chat no connected account reaches is put in ``unreachable`` — reported,
    never an error, and never asked through an account it is not.

    :meth:`visit` asks through the first account left (:func:`through_accounts`): a flood wait
    stops that account for the rest of the pass (its chats move on to the next account that
    reaches them, or wait for the next run), a shared chat its account is refused — or cannot
    address at all — is tried through the next one, warmed for that chat first, and any other
    error costs the chat its turn with a warning. Warnings name their account whenever the pass
    was *started* with one but the default (``labelled``, decided over every client
    :meth:`start` was handed, before :func:`checked_accounts` left any out — so leaving out the
    only other account does not strip the prefix from the warnings that follow).
    """

    conn: sqlite3.Connection
    clients: Mapping[str, Any]
    sync_cfg: SyncCfg
    budget: Budget
    flood_warning: Callable[[int], str]
    labelled: bool = False
    routes: dict[int, list[str]] = field(default_factory=dict)
    unreachable: list[ChatRow] = field(default_factory=list)
    stopped: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    async def start(
        cls,
        conn: sqlite3.Connection,
        clients: Mapping[str, Any],
        chats: Sequence[ChatRow],
        sync_cfg: SyncCfg,
        budget: Budget,
        flood_warning: Callable[[int], str],
        *,
        every: bool = False,
    ) -> "StoredPass":
        """Route ``chats`` and, unless the budget is already spent, warm every client for the
        chats it goes first for — or, with ``every``, for each chat it is on the route of. The
        flood-sleep cap goes on **before** each warm-up, which makes requests of its own.

        Every account is put to :func:`check_account` first: one whose session is another
        Telegram user than the index recorded, or that cannot say who it is, is left out with a
        warning — its chats go through another account that reaches them, or are
        ``unreachable`` — because a deletion sweep run as someone else reads that user's
        answers as the recorded user's history being gone."""
        for client in clients.values():
            cap_flood_sleep(client, sync_cfg, budget)
        checked, left_out = await checked_accounts(conn, clients)
        state = cls(conn, checked, sync_cfg, budget, flood_warning, labels_accounts(clients))
        for account, reason in left_out.items():
            state.warn(account, reason)
        clients = checked
        asked: dict[str, list[ChatRow]] = {}
        for chat in chats:
            route = reaching_accounts(conn, chat, clients)
            if not route:
                state.unreachable.append(chat)
                log.info(
                    "chat %s (%s): no connected account reaches it; left alone",
                    chat.id,
                    chat.title,
                )
                continue
            state.routes[chat.id] = route
            for account in route if every else route[:1]:
                asked.setdefault(account, []).append(chat)
        if budget.expired:
            return state
        for account, routed in asked.items():
            client = clients[account]
            cap_flood_sleep(client, sync_cfg, budget)
            try:
                await warm_peer_cache(client, routed, conn, account)
            except errors.UnauthorizedError as exc:
                tg.reraise_unauthorized(exc, account)
        return state

    def warn(self, account: str, warning: str) -> None:
        self.warnings.append(account_warning(self.labelled, account, warning))

    def stop(self, account: str, seconds: int) -> None:
        """A flood wait of ``seconds`` on ``account``: no more requests through it this pass."""
        self.stopped.add(account)
        self.warn(account, self.flood_warning(seconds))

    async def visit[T](self, chat: ChatRow, act: Callable[[Any], Awaitable[T]]) -> T | None:
        """``act(client)`` for the first account that answers about ``chat``; ``None`` when none
        did — a flood wait, a refusal, an error or the budget ended its turn."""

        async def through(account: str, client: Any) -> T:
            return await act(client)

        return await self.visit_as(chat, through)

    async def visit_as[T](self, chat: ChatRow, act: Callable[[str, Any], Awaitable[T]]) -> T | None:
        """:meth:`visit` for a pass that needs to know which account answered:
        ``act(account, client)``."""
        route = self.routes.get(chat.id, [])
        outcome = await through_accounts(
            self.conn,
            self.clients,
            chat,
            route,
            self.sync_cfg,
            self.budget,
            self.stopped,
            act,
            warmed=route[:1],
        )
        for flood in outcome.flooded:
            self.warn(flood.account, self.flood_warning(flood.seconds))
        if outcome.account is not None:
            return outcome.result
        if outcome.failure is not None:
            failed = outcome.failure
            self.warn(failed.account, f"chat {chat.id} ({chat.title}): {failed.error}")
        elif outcome.refusals:
            last = outcome.refusals[-1]
            self.warn(last.account, f"chat {chat.id} ({chat.title}): {last.reason}")
        return None


# --- teaching a client its peers -------------------------------------------------------------


async def warm_peer_cache(
    client: Any, chats: Sequence[ChatRow], conn: sqlite3.Connection, account: str
) -> None:
    """Teach ``client``, ``account``'s, the peers of ``chats`` before anything addresses them by
    bare id.

    :func:`grepogram.tg.load_session` copies the data centre and the auth key out of the session
    file and nothing else, so **the entity cache of every client grepogram builds starts empty**
    — the docstring there states the rule and every Telegram-facing pass has to honour it. A
    request that names a chat by its stored id and nothing else, which is what
    ``client.get_messages(chat.peer_id, ids=[…])`` is, has no access hash to build an ``InputPeer``
    from: Telethon 1.44 asks the session, gets nothing, and its network fallback
    (``channels.getChannels`` / ``users.getUsers`` with ``access_hash = 0``) is documented to
    answer only for a bot's private chats or a contact. For a user session on a private
    supergroup it ends in a plain ``ValueError: Could not find the input entity``, which is not
    an ``RPCError`` and reaches a caller as a skipped chat or a traceback.

    One ``get_dialogs()`` is the whole fix: Telethon writes the peers of every answer into the
    session (``session.process_entities``), so the dialog list makes every chat the account has a
    dialog with addressable for the rest of the client's life. It is the same warm-up a sync gets
    for free from :func:`~grepogram.sources.resolve_sources` and the reason no path that goes
    through a :class:`~grepogram.dialogs.DialogCatalog` ever had to think about this; the two passes
    that walk stored rows instead of a source list — :func:`~grepogram.sync.prune_deleted` and
    :func:`grepogram.media.run` — are the ones that must ask for it by hand. The client's own call
    rather than that catalog, because neither pass has any use for the folder list the catalog reads
    beside it.

    **What the index already stores comes first, and costs no request at all.** Every resolve
    records the access hash each account addresses a chat by (``chat_access``,
    :func:`grepogram.db.access_hash`), and that hash is all an ``InputPeer`` needs: it is handed
    to the client's session (``session.process_entities``, the call Telethon itself feeds every
    answer through), and a chat seeded that way — or a legacy group, which needs no hash at all
    — is left out of every route below. Only the rest cost the dialog list, and none left means
    no request. A discussion group's channel is seeded the same way, so the third route below can
    name it. The hash is ``account``'s own: another account's would address the peer as a
    different user, and Telegram refuses it.

    The dialog list is not the whole account, though, and the two chats it misses are exactly
    the ones a source list reaches by a **stored handle** rather than by id:

    * A public channel or group the account follows without joining has no dialog at all. A sync
      never notices, because its source is a ``chat = "@name"`` and
      :func:`~grepogram.sources.resolve_sources` resolves the handle — and that handle is stored
      on the row as ``chats.username``, so this can walk the same route with
      ``client.get_entity(chat.username)``. Skipping it left ``extract`` and ``prune-deleted``
      failing every by-id request for precisely the chats the warm-up was added for.
    * A channel's discussion group is indexed through the channel's link
      (:func:`~grepogram.sync.link_discussion_chat`) and may have neither a dialog nor a username of
      its own. ``GetFullChannelRequest`` on the channel answers with the group among ``full.chats``,
      which caches it exactly as the dialog list caches a dialog.

    Both in that order, and the order matters: the *channel* a link-only group hangs off may
    itself be outside the dialog list, and naming it by bare id in ``GetFullChannelRequest``
    would fail for the same reason everything else here does. The username pass runs over the
    whole list first, so a channel with a handle is resolved before its group is asked for.

    ``resolved`` is what the session is known to hold, keyed by what actually came back
    (``utils.get_peer_id``) rather than by what was asked for: a handle that has moved to another
    peer resolves *that* one, and the chat it was stored on still needs its second route.

    Nothing here is worth failing a pass for: a chat no route resolves is left to the caller's
    per-chat handler, which costs that chat its turn and reports it, and a Telegram error during the
    warm-up (a flood wait included) resurfaces on the very next request the pass makes, where it is
    handled properly. An ``UnauthorizedError`` is the exception every handler makes, for the reason
    :func:`~grepogram.sync._sync_chats` makes it: a session revoked mid-run is not a chat that would
    not resolve, and only re-raising lets :func:`grepogram.tg.wrap_auth_errors` turn it into an
    :class:`~grepogram.tg.AuthRequired` with the ``grepogram auth`` hint instead of a wall of
    per-chat warnings and an exit code of zero.
    """
    seeded = _seed_stored_peers(client, chats, conn, account)
    chats = [chat for chat in chats if chat.peer_id not in seeded]
    if not chats:
        return
    try:
        listed = {int(dialog.id) for dialog in await client.get_dialogs(ignore_migrated=True)}
    except errors.UnauthorizedError:
        raise
    except (errors.RPCError, ValueError) as exc:
        log.warning("could not read the dialog list to resolve %d chats: %s", len(chats), exc)
        return
    log.debug("warmed the entity cache with %d dialogs", len(listed))
    resolved = listed | seeded
    for chat in chats:
        if chat.peer_id in resolved or not chat.username:
            continue
        try:
            entity = await client.get_entity(chat.username)
        except errors.UnauthorizedError:
            raise
        except (errors.RPCError, ValueError) as exc:
            log.warning(
                "chat %s (%s): @%s, the handle it is stored under, could not be resolved, "
                "so the chat may not resolve: %s",
                chat.id,
                chat.title,
                chat.username,
                exc,
            )
            continue
        resolved.add(int(utils.get_peer_id(entity)))
    for chat in chats:
        if chat.peer_id in resolved or chat.discussion_of is None:
            continue
        try:
            await client(functions.channels.GetFullChannelRequest(chat.discussion_of))
        except errors.UnauthorizedError:
            raise
        except (errors.RPCError, ValueError) as exc:
            log.warning(
                "chat %s (%s): channel %s, which it holds the comments of, could not be read, "
                "so the group may not resolve: %s",
                chat.id,
                chat.title,
                chat.discussion_of,
                exc,
            )


def _seed_stored_peers(
    client: Any, chats: Sequence[ChatRow], conn: sqlite3.Connection, account: str
) -> set[int]:
    """Hand ``client``'s session the access hashes ``account`` has stored for ``chats`` and for
    the channels their discussion groups hang off (:func:`~grepogram.sources.seed_peers`);
    returns the peer ids now addressable."""
    wanted = [(chat.id, chat.peer_id) for chat in chats]
    wanted += [(chat.discussion_of, chat.discussion_of) for chat in chats if chat.discussion_of]
    return seed_peers(
        client, [(peer, db.access_hash(conn, row_id, account)) for row_id, peer in wanted]
    )
