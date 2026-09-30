"""Telethon client construction, session file hygiene and auth error mapping.

Every account has its own session file (:meth:`grepogram.paths.Paths.session_file_for`:
``session.session`` for :data:`~grepogram.models.DEFAULT_ACCOUNT`, ``sessions/<name>.session``
for any other), and every function here that touches one takes the ``account`` it belongs to,
``default`` when omitted. A session file is a Telethon SQLite database that holds the account's
auth key, so it is created with mode 0600 (:func:`prepare_session`) and checked on every use
(:func:`ensure_session_mode`). Only ``grepogram auth`` writes it (:func:`make_login_client`);
every other client gets a private in-memory copy of it (:func:`make_client`, :func:`load_session`;
:func:`make_clients` builds one per signed-in account).
Telethon writes to its session database on every request that returns peers and commits once a
minute, so two clients on one file — a cron ``grepogram sync`` next to the MCP server, or two
tool calls in one process — block each other for the SQLite busy timeout and then fail with
``database is locked``; an in-memory copy per client has nothing to contend for. Never use
``async with client``: Telethon's ``__aenter__`` calls ``start()``, which prompts for a phone
number on stdin; :func:`connected` connects without prompting and turns dead-session errors into
:class:`AuthRequired`.
"""

import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from telethon import TelegramClient, errors, utils
from telethon.sessions import MemorySession, SQLiteSession

from grepogram.models import DEFAULT_ACCOUNT, Config
from grepogram.paths import DIR_MODE, PRIVATE_FILE_MODE, Paths

DEVICE_MODEL = "grepogram"
AUTH_ERRORS: tuple[type[Exception], ...] = (
    errors.AuthKeyUnregisteredError,
    errors.SessionRevokedError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
    errors.SessionExpiredError,
    errors.AuthKeyInvalidError,
    errors.AuthKeyPermEmptyError,
    errors.ActiveUserRequiredError,
)
"""Every ``UnauthorizedError`` that means the stored session is dead and ``grepogram auth`` is
the way out — all of Telethon's subclasses but ``SessionPasswordNeededError``, which belongs to
the sign-in flow."""

Prompt = Callable[[], str | Awaitable[str]]


def auth_command(account: str = DEFAULT_ACCOUNT) -> str:
    """The command that signs ``account`` in: ``grepogram auth``, with ``--account <name>`` for
    any account but the default one. Every sign-in hint, the CLI's and the MCP server's, is
    worded around it."""
    if account == DEFAULT_ACCOUNT:
        return "grepogram auth"
    return f"grepogram auth --account {account}"


def auth_hint(account: str = DEFAULT_ACCOUNT) -> str:
    """The CLI's sign-in hint for ``account``: ``run:`` and its :func:`auth_command` —
    ``run: grepogram auth`` for the default account, ``run: grepogram auth --account <name>``
    for any other."""
    return f"run: {auth_command(account)}"


class AuthRequired(Exception):
    """The Telegram session of ``account`` is missing, unauthorized or was rejected by Telegram.

    ``hint`` is what to do about it: :func:`auth_hint` for that account unless a subclass
    knows better.
    """

    def __init__(
        self,
        reason: str = "Telegram session is not authorized",
        account: str = DEFAULT_ACCOUNT,
        hint: str | None = None,
    ) -> None:
        self.reason = reason
        self.account = account
        self.hint = auth_hint(account) if hint is None else hint
        super().__init__(f"{reason} ({self.hint})")


class OtherUser(AuthRequired):
    """The session of ``account`` is Telegram user ``user_id``, not ``recorded``, the user the
    index recorded under that name.

    Everything tied to an account name — its private chats, the access hashes it stored, its
    research approvals — belongs to the recorded user, so a pass leaves such an account out
    rather than act as someone else (:func:`grepogram.sync.check_account`). The way out is not a
    sign-in under the same name, which ``grepogram auth`` refuses, but removing the account
    first; ``hint`` says so."""

    def __init__(self, account: str, user_id: int, recorded: int) -> None:
        super().__init__(
            f"account {account} is signed in as Telegram user {user_id}, not user {recorded} "
            "this index recorded for it; nothing was done as it",
            account,
            f"run `grepogram accounts rm {account}` and sign it in again if the change is meant",
        )
        self.user_id = user_id
        self.recorded = recorded


class SessionMissing(AuthRequired):
    """There is no session file for ``account`` yet, so that account cannot talk to Telegram."""

    def __init__(self, path: Path, account: str = DEFAULT_ACCOUNT) -> None:
        self.path = path
        super().__init__(f"no Telegram session at {path}", account)


class SessionError(Exception):
    """The session file of ``account`` exists but cannot be read: another process is writing it
    (a ``grepogram auth`` in progress) or the file is damaged."""

    def __init__(self, path: Path, reason: Exception, account: str = DEFAULT_ACCOUNT) -> None:
        self.path = path
        self.account = account
        super().__init__(f"cannot read the Telegram session at {path}: {reason}")


def load_session(paths: Paths, account: str = DEFAULT_ACCOUNT) -> MemorySession:
    """A private in-memory copy of ``account``'s session file: data centre, address, port and
    auth key.

    The file is read through Telethon's own ``SQLiteSession`` (so a file from an older Telethon
    layout is understood), which opens it read-write and is therefore closed at once; only the
    values above are taken out and the in-memory copy never writes anything back, and the entity
    cache starts empty — every caller re-reads the dialogs it needs.
    Raises :class:`SessionError` when SQLite cannot read the file.
    """
    path = paths.session_file_for(account)
    try:
        stored = SQLiteSession(str(path))
    except sqlite3.Error as exc:
        raise SessionError(path, exc, account) from exc
    try:
        session = MemorySession()
        if stored.server_address:
            session.set_dc(stored.dc_id, stored.server_address, stored.port)
        session.auth_key = stored.auth_key
    finally:
        stored.close()
    return session


def make_client(cfg: Config, paths: Paths, account: str = DEFAULT_ACCOUNT) -> TelegramClient:
    """Build a client on a private copy of ``account``'s session file (:func:`load_session`);
    does not connect."""
    return _client(load_session(paths, account), cfg)


ClientFactory = Callable[[Config, Paths, str], Any]
"""Builds the (unconnected) client of an account: ``(config, paths, account)``."""


@dataclass(frozen=True, slots=True)
class Accounts:
    """The accounts a multi-account pass works with: a client per account that takes part, and
    why each account that owns a configured source was left out.

    :func:`make_clients` fills ``skipped`` with a :class:`SessionMissing` for an account never
    signed in and a :class:`SessionError` for one whose file cannot be read;
    :func:`connected_all` adds the :class:`AuthRequired` of each session Telegram refuses. Every
    one of them names its account, and none stops the other accounts.
    """

    clients: dict[str, Any] = field(default_factory=dict)
    skipped: dict[str, Exception] = field(default_factory=dict)


def make_clients(
    cfg: Config,
    paths: Paths,
    accounts: Iterable[str] | None = None,
    *,
    factory: ClientFactory | None = None,
) -> Accounts:
    """Build a client (``factory``, :func:`make_client` by default — looked up when called) for
    every one of
    ``accounts`` — every account the config knows (:meth:`~grepogram.models.Config.account_names`)
    when ``None`` — that has a readable session file, after :func:`ensure_session_mode` has
    checked it. Does not connect.

    An account without one is left out rather than raised — a second account that was never
    signed in must not keep the first from syncing — and reported in :attr:`Accounts.skipped`
    when it owns a configured source; an install that signs in only named accounts never had a
    default session to miss. With no client at all, the reason of an account that owns sources
    (else the first one's) is raised, so a single-account install reads exactly the "run:
    grepogram auth" it always did.
    """
    build = make_client if factory is None else factory
    clients: dict[str, Any] = {}
    unavailable: dict[str, Exception] = {}
    for account in dict.fromkeys(cfg.account_names() if accounts is None else accounts):
        try:
            ensure_session_mode(paths, account)
            clients[account] = build(cfg, paths, account)
        except (SessionMissing, SessionError) as exc:
            unavailable[account] = exc
    owners = {source.account for source in cfg.sources}
    if not clients and unavailable:
        first = next(iter(unavailable.values()))
        raise next((exc for name, exc in unavailable.items() if name in owners), first)
    skipped = {name: exc for name, exc in unavailable.items() if name in owners}
    return Accounts(clients, skipped)


def make_login_client(
    cfg: Config, paths: Paths, account: str = DEFAULT_ACCOUNT, *, path: Path | None = None
) -> TelegramClient:
    """Build the client ``grepogram auth`` signs ``account`` in with: the one client that writes
    a session file — ``path``, the staged copy :func:`stage_login` made, or else the account's
    own. Does not connect. Raises :class:`SessionError` naming the account's file when SQLite
    cannot read it — Telethon opens it in the constructor."""
    own = paths.session_file_for(account)
    try:
        return _client(str(own if path is None else path), cfg)
    except sqlite3.Error as exc:
        raise SessionError(own, exc, account) from exc


def stage_login(paths: Paths, account: str = DEFAULT_ACCOUNT) -> Path:
    """A private (0600) copy of ``account``'s session file, next to it, for a sign-in to write
    into — an empty one when the account has none yet.

    A sign-in may turn out to be another Telegram user than the one the index recorded under the
    account's name, and Telethon writes the file it signs in with as it goes: signing in on the
    copy leaves the account's own session untouched until :func:`commit_login` puts the copy in
    its place, and a refused sign-in only deletes the copy. A session still signed in is copied
    whole, so signing in again asks nothing."""
    paths.ensure_dirs()
    own = paths.session_file_for(account)
    own.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{own.stem}-login-", suffix=".session", dir=own.parent)
    os.close(fd)
    staged = Path(name)
    try:
        os.chmod(staged, PRIVATE_FILE_MODE)
        if own.exists():
            shutil.copyfile(own, staged)  # into the 0600 file mkstemp made: the mode stays
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    return staged


def commit_login(paths: Paths, account: str, staged: Path) -> Path:
    """Put the session :func:`stage_login` staged and a sign-in wrote in the place of
    ``account``'s own, atomically and 0600; returns the account's session file."""
    own = paths.session_file_for(account)
    os.chmod(staged, PRIVATE_FILE_MODE)
    os.replace(staged, own)
    return own


def _client(session: MemorySession | str, cfg: Config) -> TelegramClient:
    return TelegramClient(
        session,
        cfg.telegram.api_id,
        cfg.telegram.api_hash,
        flood_sleep_threshold=cfg.sync.flood_sleep_threshold,
        device_model=DEVICE_MODEL,
    )


def prepare_session(paths: Paths, account: str = DEFAULT_ACCOUNT) -> Path:
    """Create ``account``'s session file with mode 0600 if needed, before Telethon creates it as
    0644. Every directory grepogram writes into — ``sessions/`` among them — is created with
    :data:`~grepogram.paths.DIR_MODE` first; the file's own directory is made sure of again in
    case it lies outside them."""
    paths.ensure_dirs()
    path = paths.session_file_for(account)
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, PRIVATE_FILE_MODE)
    os.close(fd)
    os.chmod(path, PRIVATE_FILE_MODE)
    return path


def ensure_session_mode(paths: Paths, account: str = DEFAULT_ACCOUNT) -> Path:
    """Require ``account``'s session file to exist and make sure it is owner-readable only."""
    path = paths.session_file_for(account)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        raise SessionMissing(path, account) from None
    if mode != PRIVATE_FILE_MODE:
        path.chmod(PRIVATE_FILE_MODE)
    return path


def reraise_unauthorized(exc: Exception, account: str) -> NoReturn:
    """Raise what a rejected session inside a multi-account pass becomes: :class:`AuthRequired`
    naming ``account`` for a dead session, and the error itself for any other
    ``UnauthorizedError``.

    Several clients run inside one :func:`connected` block each, and an error that unwinds
    through all of them is claimed by whichever block it leaves first — the last account
    entered, not the one Telegram rejected. Only the code that made the request knows whose
    session it was, so it names it here.
    """
    if isinstance(exc, AUTH_ERRORS):
        raise AuthRequired(f"Telegram rejected the session: {exc}", account) from exc
    raise exc


@asynccontextmanager
async def wrap_auth_errors(
    client: TelegramClient, account: str = DEFAULT_ACCOUNT
) -> AsyncIterator[None]:
    """Raise :class:`AuthRequired` for ``account`` when its client is unauthorized or Telegram
    rejects its session."""
    try:
        if not await client.is_user_authorized():
            raise AuthRequired(account=account)
        yield
    except AUTH_ERRORS as exc:
        reraise_unauthorized(exc, account)


@asynccontextmanager
async def connected(
    client: TelegramClient, account: str = DEFAULT_ACCOUNT
) -> AsyncIterator[TelegramClient]:
    """Connect without prompting, check authorization, and always disconnect. An auth failure
    names ``account``.

    ``connect()`` is inside the guarded block: it opens the transport first and then talks to
    Telegram, so a failure on the way out would otherwise leave a connection (and its keepalive
    task) behind. Disconnecting a client that never connected is a no-op.
    """
    try:
        await client.connect()
        async with wrap_auth_errors(client, account):
            yield client
    finally:
        await client.disconnect()


@asynccontextmanager
async def connected_all(
    clients: Mapping[str, Any], skipped: Mapping[str, Exception] | None = None
) -> AsyncIterator[Accounts]:
    """:func:`connected` for every account of ``clients`` at once; all of them are disconnected
    on the way out. ``skipped`` are the accounts already left out (:func:`make_clients`).

    An account whose session is not authorized is left out and added to
    :attr:`Accounts.skipped` rather than raised — one signed-out account must not keep the
    others from syncing — unless it leaves no account connected at all, when its
    :class:`AuthRequired` is raised as :func:`connected` would. Any other failure (the network)
    is raised.
    """
    live = Accounts(skipped=dict(skipped or {}))
    refused: list[AuthRequired] = []
    async with AsyncExitStack() as stack:
        for account, client in clients.items():
            try:
                await stack.enter_async_context(connected(client, account))
            except AuthRequired as exc:
                live.skipped[account] = exc
                refused.append(exc)
                continue
            live.clients[account] = client
        if not live.clients and refused:
            raise refused[0]
        yield live


@dataclass(frozen=True, slots=True)
class SignedIn:
    """Who :func:`login` signed in: the display name and Telegram's user id, and whether this
    sign-in made a new authorization (``fresh``) rather than finding the session signed in."""

    name: str
    user_id: int
    fresh: bool = True


async def login(
    client: TelegramClient, *, phone: Prompt, code: Prompt, password: Prompt
) -> SignedIn:
    """Run Telethon's interactive sign-in and return who the account is.

    ``phone`` and ``code`` are asked when the session is not authorized yet; ``password`` only
    when the account has two-step verification enabled.
    """
    try:
        await client.connect()
        fresh = not await client.is_user_authorized()
        await client.start(phone=phone, password=password, code_callback=code)
        me = await client.get_me()
    finally:
        await client.disconnect()
    if me is None:
        raise AuthRequired("sign-in did not produce an authorized session")
    return SignedIn(name=str(utils.get_display_name(me)), user_id=int(me.id), fresh=fresh)


async def log_out(client: TelegramClient) -> bool:
    """End the authorization ``client``'s session holds on Telegram's side
    (``auth.logOut``); ``True`` when Telegram confirmed it.

    For a sign-in that is not kept (``grepogram auth`` refusing another Telegram user under a
    recorded name): deleting the staged file alone would leave a live authorization behind whose
    only key is gone, listed under the user's devices until someone ends it by hand. Best
    effort — a failure to reach Telegram is ``False`` and never raised, since the sign-in is
    refused either way."""
    try:
        await client.connect()
        return bool(await client.log_out())
    except (errors.RPCError, ConnectionError, OSError, sqlite3.Error):
        return False
    finally:
        await client.disconnect()
