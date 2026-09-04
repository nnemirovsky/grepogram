"""Filesystem locations used by grepogram, the flags that steer them and the lock over a file.

``GREPOGRAM_HOME=<dir>`` redirects everything under one directory (``config.toml``,
``config.lock``, ``session.session``, ``index.db``, ``sync.lock``, ``logs/``); tests rely on this.
Without it the macOS conventions apply: config, its lock and the session under
``~/.config/grepogram``, index and sync lock under ``~/Library/Application Support/grepogram``,
logs under ``~/Library/Logs/grepogram``. :func:`env_flag` reads a boolean switch such as
``GREPOGRAM_FAKE_MODELS`` the same way everywhere.

:class:`FileLock` is the ``flock`` both cross-process locks are built on —
:class:`grepogram.sync.SyncLock` over ``sync.lock`` and :class:`grepogram.config.ConfigLock` over
``config.lock`` — so the mode bits, the directory and the close-on-error handling are written once.
"""

import fcntl
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

ENV_HOME = "GREPOGRAM_HOME"
SESSION_SUFFIX = ".session"
CONFIG_LOCK_NAME = "config.lock"
LOG_FILE_NAME = "grepogram.log"
DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
"""What every file grepogram writes with secrets in it gets: the config, the session, the locks."""
_TRUE = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether the environment variable ``name`` is set to ``1``, ``true``, ``yes`` or ``on``."""
    env = os.environ if env is None else env
    return env.get(name, "").strip().lower() in _TRUE


@dataclass(frozen=True, slots=True)
class Paths:
    config_file: Path
    session_file: Path
    db_file: Path
    lock_file: Path
    log_dir: Path

    def __post_init__(self) -> None:
        if self.session_file.suffix != SESSION_SUFFIX:
            raise ValueError(
                f"session_file must end with {SESSION_SUFFIX!r} because Telethon appends it "
                f"otherwise: {self.session_file}"
            )

    @property
    def log_file(self) -> Path:
        return self.log_dir / LOG_FILE_NAME

    @property
    def config_lock_file(self) -> Path:
        """The flock taken around every read-modify-write of ``config_file``, next to it."""
        return self.config_file.with_name(CONFIG_LOCK_NAME)

    @property
    def directories(self) -> tuple[Path, ...]:
        """Every directory grepogram writes into, deduplicated, in a stable order."""
        ordered: dict[Path, None] = {}
        for directory in (
            self.config_file.parent,
            self.session_file.parent,
            self.db_file.parent,
            self.lock_file.parent,
            self.log_dir,
        ):
            ordered.setdefault(directory, None)
        return tuple(ordered)

    @classmethod
    def under(cls, root: Path) -> Self:
        """The single-directory layout used with ``GREPOGRAM_HOME``."""
        return cls(
            config_file=root / "config.toml",
            session_file=root / "session.session",
            db_file=root / "index.db",
            lock_file=root / "sync.lock",
            log_dir=root / "logs",
        )

    @classmethod
    def macos_default(cls, home: Path) -> Self:
        config_dir = home / ".config" / "grepogram"
        data_dir = home / "Library" / "Application Support" / "grepogram"
        return cls(
            config_file=config_dir / "config.toml",
            session_file=config_dir / "session.session",
            db_file=data_dir / "index.db",
            lock_file=data_dir / "sync.lock",
            log_dir=home / "Library" / "Logs" / "grepogram",
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Resolve paths from ``GREPOGRAM_HOME`` (``os.environ`` by default) or the defaults."""
        env = os.environ if env is None else env
        override = env.get(ENV_HOME, "").strip()
        if override:
            return cls.under(Path(override).expanduser())
        return cls.macos_default(Path.home())

    def ensure_dirs(self) -> None:
        """Create the directories that do not exist yet with mode 0700; safe to call repeatedly.

        A directory that already exists — a ``GREPOGRAM_HOME`` the user made, say — keeps its
        mode: grepogram protects what it creates and leaves other people's directories alone.
        """
        for directory in self.directories:
            try:
                directory.mkdir(mode=DIR_MODE, parents=True)
            except FileExistsError:
                continue
            directory.chmod(DIR_MODE)


class FileLock:
    """An exclusive ``flock`` on a lock file, held for the ``with`` block.

    The file is created with :data:`PRIVATE_FILE_MODE` under a :data:`DIR_MODE` directory and is
    never deleted: unlinking a file another process is about to lock would let both proceed.
    ``blocking`` decides what contention costs — waiting for the holder, which is right for a
    lock held for milliseconds, or :meth:`busy` at once, which is right for one held for the
    length of a sync. Re-entering from the same descriptor is not supported.
    """

    blocking: bool = True

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def busy(self) -> Exception:
        """What a non-blocking lock raises when another process holds it."""
        return BlockingIOError(f"another process holds the lock on {self.path}")

    def __enter__(self) -> Self:
        self.path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise self.busy() from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
