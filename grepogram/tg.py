"""Telethon client construction, session file hygiene and auth error mapping.

The session file is a Telethon SQLite database that holds the account's auth key, so it is
created with mode 0600 (:func:`prepare_session`) and checked on every use
(:func:`ensure_session_mode`). Only ``grepogram auth`` writes it (:func:`make_login_client`);
every other client gets a private in-memory copy of it (:func:`make_client`, :func:`load_session`).
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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from telethon import TelegramClient, errors, utils
from telethon.sessions import MemorySession, SQLiteSession

from grepogram.models import Config
from grepogram.paths import PRIVATE_FILE_MODE, Paths

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


class AuthRequired(Exception):
    """The Telegram session is missing, unauthorized or was rejected by Telegram."""

    hint = AUTH_HINT

    def __init__(self, reason: str = "Telegram session is not authorized") -> None:
        self.reason = reason
        super().__init__(f"{reason} ({self.hint})")


class SessionMissing(AuthRequired):
    """There is no session file yet, so nothing can talk to Telegram."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"no Telegram session at {path}")


class SessionError(Exception):
    """The session file exists but cannot be read: another process is writing it (a
    ``grepogram auth`` in progress) or the file is damaged."""

    def __init__(self, path: Path, reason: Exception) -> None:
        self.path = path
        super().__init__(f"cannot read the Telegram session at {path}: {reason}")


def load_session(paths: Paths) -> MemorySession:
    """A private in-memory copy of the session file: data centre, address, port and auth key.

    The file is read through Telethon's own ``SQLiteSession`` (so a file from an older Telethon
    layout is understood), which opens it read-write and is therefore closed at once; only the
    values above are taken out and the in-memory copy never writes anything back, and the entity
    cache starts empty — every caller re-reads the dialogs it needs.
    Raises :class:`SessionError` when SQLite cannot read the file.
    """
    try:
        stored = SQLiteSession(str(paths.session_file))
    except sqlite3.Error as exc:
        raise SessionError(paths.session_file, exc) from exc
    try:
        session = MemorySession()
        if stored.server_address:
            session.set_dc(stored.dc_id, stored.server_address, stored.port)
        session.auth_key = stored.auth_key
    finally:
        stored.close()
    return session


def make_client(cfg: Config, paths: Paths) -> TelegramClient:
    """Build a client on a private copy of the session file (:func:`load_session`); does not
    connect."""
    return _client(load_session(paths), cfg)


def make_login_client(cfg: Config, paths: Paths) -> TelegramClient:
    """Build the client ``grepogram auth`` signs in with: the one client that writes
    ``paths.session_file``. Does not connect. Raises :class:`SessionError` when the file exists
    but SQLite cannot read it — Telethon opens it in the constructor."""
    try:
        return _client(str(paths.session_file), cfg)
    except sqlite3.Error as exc:
        raise SessionError(paths.session_file, exc) from exc


def _client(session: MemorySession | str, cfg: Config) -> TelegramClient:
    return TelegramClient(
        session,
        cfg.telegram.api_id,
        cfg.telegram.api_hash,
        flood_sleep_threshold=cfg.sync.flood_sleep_threshold,
        device_model=DEVICE_MODEL,
    )


def prepare_session(paths: Paths) -> Path:
    """Create the session file with mode 0600 if needed, before Telethon creates it as 0644."""
    paths.ensure_dirs()
    fd = os.open(paths.session_file, os.O_WRONLY | os.O_CREAT, PRIVATE_FILE_MODE)
    os.close(fd)
    os.chmod(paths.session_file, PRIVATE_FILE_MODE)
    return paths.session_file


def ensure_session_mode(paths: Paths) -> Path:
    """Require an existing session file and make sure it is owner-readable only."""
    try:
        mode = stat.S_IMODE(paths.session_file.stat().st_mode)
    except FileNotFoundError:
        raise SessionMissing(paths.session_file) from None
    if mode != PRIVATE_FILE_MODE:
        paths.session_file.chmod(PRIVATE_FILE_MODE)
    return paths.session_file


@asynccontextmanager
async def wrap_auth_errors(client: TelegramClient) -> AsyncIterator[None]:
    """Raise :class:`AuthRequired` for an unauthorized client or a session Telegram rejects."""
    try:
        if not await client.is_user_authorized():
            raise AuthRequired()
        yield
    except AUTH_ERRORS as exc:
        raise AuthRequired(f"Telegram rejected the session: {exc}") from exc


@asynccontextmanager
async def connected(client: TelegramClient) -> AsyncIterator[TelegramClient]:
    """Connect without prompting, check authorization, and always disconnect.

    ``connect()`` is inside the guarded block: it opens the transport first and then talks to
    Telegram, so a failure on the way out would otherwise leave a connection (and its keepalive
    task) behind. Disconnecting a client that never connected is a no-op.
    """
    try:
        await client.connect()
        async with wrap_auth_errors(client):
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
