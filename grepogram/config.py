"""Reading and writing ``config.toml``.

``load`` parses the file with :mod:`tomllib` and validates every key against the dataclasses in
:mod:`grepogram.models`; ``save`` serialises a :class:`Config` with :mod:`tomli_w` and writes it
with mode 0600. Saving is comment-lossy by design: TOML libraries do not round-trip comments, so
``TEMPLATE`` (the annotated file written by ``grepogram config init``) is where comments live.

The file is edited by more than one process — the CLI in a terminal, the MCP server under Claude
Code — and every edit is a read-modify-write, so :class:`ConfigLock` (an ``flock`` on
``config.lock`` next to the file) serialises them across processes and :func:`update` is the
one way to apply a change: it re-reads the file under the lock and saves the result of a pure
function of what is stored, never of a snapshot taken earlier.
"""

import dataclasses
import datetime as dt
import os
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, get_type_hints

import tomli_w

from grepogram.models import Config, ModelsCfg, SearchCfg, Source, SyncCfg, TelegramCfg, UnitsCfg
from grepogram.paths import PRIVATE_FILE_MODE, FileLock, Paths

TEMPLATE = """\
[telegram]
api_id = 0                             # create an app at https://my.telegram.org/apps
api_hash = ""

[models]
embed = "BAAI/bge-m3"                  # sentence-transformers id; change → full re-embed
rerank = "BAAI/bge-reranker-v2-m3"
device = "auto"                        # auto → mps if available else cpu

[search]
k = 10
rrf_k = 60
rerank_top = 40
dedup_overlap = 0.5
vec_fanout_max = 8                     # above this many chats → one KNN with k*4, post-filtered
auto_sync_after_min = 60
auto_sync_budget_s = 20

[units]
window_gap_min = 30
window_max_msgs = 30
window_max_chars = 1500
thread_max_msgs = 40

[sync]
edit_refetch = 200
flood_sleep_threshold = 120

# Sources are opt-in. Add them with `grepogram sources add <target>` or by hand:
#
# [[sources]]
# folder = "Argentina"
#
# [[sources]]
# chat = "@ru_georgia"                 # or "https://t.me/…" or 123456789
# since = "2024-01-01"                 # optional: skip older history on first sync
# comments = false                     # channels only: also index linked discussion threads
"""

_SECTIONS = ("telegram", "models", "search", "units", "sync")
_SOURCE_KEYS = ("folder", "chat", "since", "comments")


class ConfigError(Exception):
    """The config file is malformed or contains keys grepogram does not know."""


def load(paths: Paths) -> Config:
    """Read ``paths.config_file``; a missing file means all defaults."""
    try:
        text = paths.config_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    try:
        return loads(text)
    except ConfigError as exc:
        raise ConfigError(f"{paths.config_file}: {exc}") from exc


def loads(text: str) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    return from_dict(raw)


def from_dict(raw: dict[str, Any]) -> Config:
    for key in raw:
        if key not in _SECTIONS and key != "sources":
            raise ConfigError(f"unknown key: {key}")
    return Config(
        telegram=_section(TelegramCfg, raw, "telegram"),
        models=_section(ModelsCfg, raw, "models"),
        search=_section(SearchCfg, raw, "search"),
        units=_section(UnitsCfg, raw, "units"),
        sync=_section(SyncCfg, raw, "sync"),
        sources=_sources(raw.get("sources", [])),
    )


def save(cfg: Config, paths: Paths) -> None:
    """Write ``cfg`` to ``paths.config_file`` with mode 0600 (comments are not preserved).

    Callers that derived ``cfg`` from an earlier :func:`load` go through :func:`update` instead,
    which holds :class:`ConfigLock` from the read to the write.
    """
    paths.ensure_dirs()
    write_private(paths.config_file, dumps(cfg))


class ConfigLock(FileLock):
    """Exclusive ``flock`` on ``paths.config_lock_file`` around a read-modify-write of the config.

    Blocking, unlike the sync lock: a holder keeps it for the milliseconds a load and a save
    take, and a process that dies releases it. Callers nest it inside the sync lock when they
    need both.
    """

    def __init__(self, paths: Paths) -> None:
        super().__init__(paths.config_lock_file)


def update(paths: Paths, change: Callable[[Config], Config]) -> Config:
    """Apply ``change`` to the config as stored and save the result; returns what was saved.

    The file is read inside :class:`ConfigLock`, so a change another process saved since the
    caller last looked — a source the MCP server removed while ``sources add`` was resolving its
    target — is carried over instead of being overwritten. ``change`` is a pure function of the
    stored config (:func:`grepogram.sources.with_source`, say) and must not touch the network:
    the lock is held while it runs. Raises what ``change`` or :func:`load` raise.
    """
    with ConfigLock(paths):
        updated = change(load(paths))
        save(updated, paths)
    return updated


def dumps(cfg: Config) -> str:
    return tomli_w.dumps(to_dict(cfg))


def to_dict(cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {name: dataclasses.asdict(getattr(cfg, name)) for name in _SECTIONS}
    if cfg.sources:
        out["sources"] = [_source_dict(source) for source in cfg.sources]
    return out


def write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, owner-readable only."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, PRIVATE_FILE_MODE)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _section[SectionT: (TelegramCfg, ModelsCfg, SearchCfg, UnitsCfg, SyncCfg)](
    cls: type[SectionT], raw: dict[str, Any], name: str
) -> SectionT:
    data = raw.get(name, {})
    if not isinstance(data, dict):
        raise ConfigError(f"invalid value for {name}: expected a table")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in hints:
            raise ConfigError(f"unknown key: {name}.{key}")
        kwargs[key] = _checked(value, hints[key], f"{name}.{key}")
    return cls(**kwargs)


def _checked(value: object, expected: type, key: str) -> object:
    if expected is bool:
        ok = isinstance(value, bool)
    elif expected is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif expected is float:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        if ok:
            value = float(value)  # type: ignore[arg-type]
    elif expected is str:
        ok = isinstance(value, str)
    else:
        raise TypeError(f"unsupported config field type {expected!r} for {key}")
    if not ok:
        raise ConfigError(
            f"invalid value for {key}: expected {expected.__name__}, got {type(value).__name__}"
        )
    return value


def _sources(raw: object) -> list[Source]:
    if not isinstance(raw, list):
        raise ConfigError("invalid value for sources: expected an array of tables")
    sources: list[Source] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"sources[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"invalid value for {where}: expected a table")
        source = _source(entry, where)
        if source.id in seen:
            raise ConfigError(f"{where}: duplicate source {source.id!r}")
        seen.add(source.id)
        sources.append(source)
    return sources


def _source(entry: dict[str, Any], where: str) -> Source:
    kwargs: dict[str, Any] = {}
    for key, value in entry.items():
        if key not in _SOURCE_KEYS:
            raise ConfigError(f"unknown key: {where}.{key}")
        if key == "chat":
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise ConfigError(
                    f"invalid value for {where}.chat: expected str or int, "
                    f"got {type(value).__name__}"
                )
            kwargs[key] = value
        elif key == "since":
            if isinstance(value, dt.date):
                value = value.isoformat()
            kwargs[key] = _checked(value, str, f"{where}.since")
        elif key == "comments":
            kwargs[key] = _checked(value, bool, f"{where}.comments")
        else:
            kwargs[key] = _checked(value, str, f"{where}.{key}")
    try:
        return Source(**kwargs)
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _source_dict(source: Source) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if source.folder is not None:
        out["folder"] = source.folder
    else:
        out["chat"] = source.chat
    if source.since is not None:
        out["since"] = source.since
    if source.comments:
        out["comments"] = True
    return out
