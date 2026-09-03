"""Filesystem locations used by grepogram.

``GREPOGRAM_HOME=<dir>`` redirects everything under one directory (``config.toml``,
``session.session``, ``index.db``, ``sync.lock``, ``logs/``); tests rely on this. Without it the
macOS conventions apply: config and session under ``~/.config/grepogram``, index and lock under
``~/Library/Application Support/grepogram``, logs under ``~/Library/Logs/grepogram``.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

ENV_HOME = "GREPOGRAM_HOME"
SESSION_SUFFIX = ".session"
LOG_FILE_NAME = "grepogram.log"
DIR_MODE = 0o700


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
        """Create every directory with mode 0700; safe to call repeatedly."""
        for directory in self.directories:
            directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            directory.chmod(DIR_MODE)
