"""Logging setup: stderr plus a rotating file, never stdout.

stdout carries the MCP protocol, so no handler may write there. Message text must not reach the
log above DEBUG; pass it through :func:`redact` first.
"""

import hashlib
import logging
import sys
from logging.handlers import RotatingFileHandler

from grepogram.paths import Paths

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
NOISY_LOGGERS = ("telethon", "httpx", "httpcore", "sentence_transformers", "urllib3")

_installed: list[logging.Handler] = []
_previous_level: int | None = None


def setup_logging(paths: Paths, level: int | str = logging.INFO, stderr: bool = True) -> None:
    """Install a rotating file handler (and a stderr handler) on the root logger.

    Calling it again replaces the handlers installed by the previous call.
    """
    global _previous_level
    resolved = _level(level)
    paths.ensure_dirs()
    shutdown_logging()
    root = logging.getLogger()
    _previous_level = root.level
    root.setLevel(resolved)
    formatter = logging.Formatter(LOG_FORMAT)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            paths.log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    ]
    if stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
        _installed.append(handler)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(resolved, logging.INFO))


def shutdown_logging() -> None:
    """Remove and close the handlers installed by :func:`setup_logging`."""
    global _previous_level
    root = logging.getLogger()
    while _installed:
        handler = _installed.pop()
        root.removeHandler(handler)
        handler.close()
    if _previous_level is not None:
        root.setLevel(_previous_level)
        _previous_level = None


def redact(text: str | None) -> str:
    """Replace message text with its length and a short digest for log lines."""
    if not text:
        return "<empty>"
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=4).hexdigest()
    return f"<{len(text)} chars #{digest}>"


def _level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    try:
        return logging.getLevelNamesMapping()[level.upper()]
    except KeyError:
        raise ValueError(f"unknown log level: {level!r}") from None
