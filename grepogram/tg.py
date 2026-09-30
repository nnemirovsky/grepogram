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
import sqlite3
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from telethon import TelegramClient, errors, utils
from telethon.sessions import MemorySession, SQLiteSession

from grepogram.models import DEFAULT_ACCOUNT, Config
from grepogram.paths import DIR_MODE, PRIVATE_FILE_MODE, Paths

DEVICE_MODEL = "grepogram"
AUTH_HINT = "run: grepogram auth"
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


def auth_hint(account: str = DEFAULT_ACCOUNT) -> str:
    """The command that signs ``account`` in: :data:`AUTH_HINT` itself for the default account,
    so a single-account install reads exactly what it always did, and
    ``run: grepogram auth --account <name>`` for any other."""
    if account == DEFAULT_ACCOUNT:
        return AUTH_HINT
    return f"{AUTH_HINT} --account {account}"


class AuthRequired(Exception):
    """The Telegram session of ``account`` is missing, unauthorized or was rejected by Telegram.

    ``hint`` is :func:`auth_hint` for that account.
    """

    def __init__(
        self, reason: str = "Telegram session is not authorized", account: str = DEFAULT_ACCOUNT
    ) -> None:
        self.reason = reason
        self.account = account
        self.hint = auth_hint(account)
        super().__init__(f"{reason} ({self.hint})")


class SessionMissing(AuthRequired):
    """There is no session file for ``account`` yet, so that account cannot talk to Telegram."""

    def __init__(self, path: Path, account: str = DEFAULT_ACCOUNT) -> None:
        self.path = path
        super().__init__(f"no Telegram session at {path}", account)


class SessionError(Exception):
    """The session file exists but cannot be read: another process is writing it (a
    ``grepogram auth`` in progress) or the file is damaged."""

    def __init__(self, path: Path, reason: Exception) -> None:
        self.path = path
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
        raise SessionError(path, exc) from exc
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


@dataclass(frozen=True, slots=True)
class AccountClients:
    """What :func:`make_clients` built: a client per account that has a session file, and why
    each of the others has none.

    ``unavailable`` holds a :class:`SessionMissing` for an account never signed in and a
    :class:`SessionError` for one whose file cannot be read; both carry the path, and the first
    the ``grepogram auth --account`` hint. Neither stops the other accounts.
    """

    clients: dict[str, TelegramClient] = field(default_factory=dict)
    unavailable: dict[str, SessionMissing | SessionError] = field(default_factory=dict)


def make_clients(
    cfg: Config, paths: Paths, accounts: Iterable[str] | None = None
) -> AccountClients:
    """Build a client (:func:`make_client`) for every one of ``accounts`` — every account the
    config knows (:meth:`~grepogram.models.Config.account_names`) when ``None`` — that has a
    readable session file, after :func:`ensure_session_mode` has checked it. An account without
    one is reported in :attr:`AccountClients.unavailable` rather than raised: a second account
    that was never signed in must not keep the first from syncing. Does not connect."""
    built = AccountClients()
    for account in dict.fromkeys(cfg.account_names() if accounts is None else accounts):
        try:
            ensure_session_mode(paths, account)
            built.clients[account] = make_client(cfg, paths, account)
        except (SessionMissing, SessionError) as exc:
            built.unavailable[account] = exc
    return built


def make_login_client(cfg: Config, paths: Paths, account: str = DEFAULT_ACCOUNT) -> TelegramClient:
    """Build the client ``grepogram auth`` signs ``account`` in with: the one client that writes
    that account's session file. Does not connect. Raises :class:`SessionError` when the file
    exists but SQLite cannot read it — Telethon opens it in the constructor."""
    path = paths.session_file_for(account)
    try:
        return _client(str(path), cfg)
    except sqlite3.Error as exc:
        raise SessionError(path, exc) from exc


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
        raise AuthRequired(f"Telegram rejected the session: {exc}", account) from exc


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


async def login(client: TelegramClient, *, phone: Prompt, code: Prompt, password: Prompt) -> str:
    """Run Telethon's interactive sign-in and return the account's display name.

    ``phone`` and ``code`` are asked when the session is not authorized yet; ``password`` only
    when the account has two-step verification enabled.
    """
    try:
        await client.start(phone=phone, password=password, code_callback=code)
        me = await client.get_me()
    finally:
        await client.disconnect()
    if me is None:
        raise AuthRequired("sign-in did not produce an authorized session")
    return str(utils.get_display_name(me))
