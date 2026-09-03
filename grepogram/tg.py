"""Telethon client construction, session file hygiene and auth error mapping.

The session file is a Telethon SQLite database that holds the account's auth key, so it is
created with mode 0600 (:func:`prepare_session`) and checked on every use
(:func:`ensure_session_mode`). Never use ``async with client``: Telethon's ``__aenter__`` calls
``start()``, which prompts for a phone number on stdin; :func:`connected` connects without
prompting and turns dead-session errors into :class:`AuthRequired`.
"""

import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from telethon import TelegramClient, errors, utils

from grepogram.models import Config
from grepogram.paths import Paths

DEVICE_MODEL = "grepogram"
SESSION_MODE = 0o600
AUTH_HINT = "run: grepogram auth"
AUTH_ERRORS: tuple[type[Exception], ...] = (
    errors.AuthKeyUnregisteredError,
    errors.SessionRevokedError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
    errors.SessionExpiredError,
    errors.AuthKeyInvalidError,
)

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


def make_client(cfg: Config, paths: Paths) -> TelegramClient:
    """Build the client for ``paths.session_file``; does not connect."""
    return TelegramClient(
        str(paths.session_file),
        cfg.telegram.api_id,
        cfg.telegram.api_hash,
        flood_sleep_threshold=cfg.sync.flood_sleep_threshold,
        device_model=DEVICE_MODEL,
    )


def prepare_session(paths: Paths) -> Path:
    """Create the session file with mode 0600 if needed, before Telethon creates it as 0644."""
    paths.ensure_dirs()
    fd = os.open(paths.session_file, os.O_WRONLY | os.O_CREAT, SESSION_MODE)
    os.close(fd)
    os.chmod(paths.session_file, SESSION_MODE)
    return paths.session_file


def ensure_session_mode(paths: Paths) -> Path:
    """Require an existing session file and make sure it is owner-readable only."""
    try:
        mode = stat.S_IMODE(paths.session_file.stat().st_mode)
    except FileNotFoundError:
        raise SessionMissing(paths.session_file) from None
    if mode != SESSION_MODE:
        paths.session_file.chmod(SESSION_MODE)
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
    """Connect without prompting, check authorization, and always disconnect."""
    await client.connect()
    try:
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
