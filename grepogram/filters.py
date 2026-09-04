"""Search filters: the chat specs and date bounds a user or Claude types, resolved against the
indexed ``chats`` table into the :class:`~grepogram.models.Filters` the search layer applies.

A chat spec is anything :func:`grepogram.sources.parse_target` understands — a marked id,
``@username``, a ``t.me`` link, ``folder:<name>`` — or free text. Free text names a folder
(through ``chats.source_id``) or matches chat titles and usernames the way ``grepogram dialogs``
does: substring hits win, a ``SequenceMatcher`` ratio of at least 0.6 is the fallback when there
is none. Every spec must select at least one indexed chat, and the union over all specs becomes
``chat_ids``. Dates are unix seconds in UTC; naive input is read as UTC.
"""

import calendar
import datetime as dt
import re
import sqlite3
import time
from collections.abc import Sequence

from grepogram import db, dialogs
from grepogram.models import ChatRow, Config, Filters, Source
from grepogram.sources import FOLDER_PREFIX, InvalidTarget, Target, parse_target, same_target

WHEN_GRAMMAR = (
    "an ISO date (2025-06-01), month (2025-06) or datetime (2025-06-01T14:30[:00][Z|+03:00]), "
    "or an age like 7d / 3w / 6m / 1y"
)
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?$", re.IGNORECASE
)
_RELATIVE_RE = re.compile(r"^(\d+)\s*([dwmy])$")


class FilterError(Exception):
    """A search filter cannot be applied to the index."""


class InvalidDate(FilterError, ValueError):
    """A date bound is not in the accepted grammar, or ``since`` lies after ``until``."""


class UnknownChat(FilterError):
    """A chat spec selects no indexed chat; ``candidates`` lists what is indexed."""

    def __init__(self, spec: str, candidates: Sequence[str], hint: str | None = None) -> None:
        self.spec = spec
        self.candidates = list(candidates)
        self.hint = hint
        message = f"no indexed chat matches {spec!r}"
        if hint:
            message += f" ({hint})"
        listing = "; ".join(self.candidates) or "nothing is indexed yet"
        super().__init__(f"{message}; indexed: {listing}")


# --- dates -----------------------------------------------------------------------------------


def parse_when(s: str, now: int | None = None, *, end: bool = False) -> int:
    """Unix seconds (UTC) for one date bound.

    ``2025-06-01`` and ``2025-06`` are the start of that day or month — or, with ``end=True``,
    its last second, which makes an inclusive ``until``. ``2025-06-01T14:30[:00]`` is that
    instant, UTC unless it carries ``Z`` or an offset. ``7d`` / ``3w`` / ``6m`` / ``1y`` is that
    long before ``now`` (unix seconds, default the current time); months and years step the
    calendar and clamp the day. Anything else raises :class:`InvalidDate`, a ``ValueError``
    whose message states the grammar.
    """
    text = s.strip()
    try:
        moment = _parse(text, now, end)
    except (ValueError, OverflowError) as exc:
        raise InvalidDate(f"cannot read date {s!r}: expected {WHEN_GRAMMAR}") from exc
    return int(moment.timestamp())


def _parse(text: str, now: int | None, end: bool) -> dt.datetime:
    month = _MONTH_RE.match(text)
    if month is not None:
        start = dt.datetime(int(month.group(1)), int(month.group(2)), 1, tzinfo=dt.UTC)
        return _shift_months(start, 1) - dt.timedelta(seconds=1) if end else start
    if _DATE_RE.match(text):
        day = dt.date.fromisoformat(text)
        start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)
        return start + dt.timedelta(days=1, seconds=-1) if end else start
    if _DATETIME_RE.match(text):
        moment = dt.datetime.fromisoformat(text.upper())
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)
    relative = _RELATIVE_RE.match(text.casefold())
    if relative is not None:
        amount, unit = int(relative.group(1)), relative.group(2)
        base = dt.datetime.fromtimestamp(time.time() if now is None else now, dt.UTC)
        if unit == "d":
            return base - dt.timedelta(days=amount)
        if unit == "w":
            return base - dt.timedelta(weeks=amount)
        return _shift_months(base, -amount if unit == "m" else -12 * amount)
    raise ValueError(text)


def _shift_months(moment: dt.datetime, months: int) -> dt.datetime:
    """``moment`` moved by ``months`` calendar months, the day clamped to the target month."""
    year, month0 = divmod(moment.year * 12 + moment.month - 1 + months, 12)
    month = month0 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


# --- chats -----------------------------------------------------------------------------------


def resolve_chats(conn: sqlite3.Connection, cfg: Config, specs: Sequence[str]) -> set[int]:
    """Marked ids of the indexed chats the specs select, as one union.

    A marked id, ``@username`` or ``t.me`` link selects that chat; ``folder:<name>`` selects
    the chats indexed through that folder source (exact name first, then the same matching as
    free text); free text selects every folder and chat whose name contains it, or — when
    nothing does — every one within ``SequenceMatcher`` reach. A spec that selects nothing
    raises :class:`UnknownChat` listing the indexed folders and chats; ``cfg`` only serves that
    message, so a configured source that has never been synced is called out as such.
    """
    chats = db.list_chats(conn)
    selected: set[int] = set()
    for spec in specs:
        selected |= _resolve_spec(spec, chats, cfg)
    return selected


def _resolve_spec(spec: str, chats: list[ChatRow], cfg: Config) -> set[int]:
    try:
        target = parse_target(spec)
    except InvalidTarget as exc:
        raise UnknownChat(spec, _describe(chats), hint=str(exc)) from exc
    found = _select(target, chats)
    if found:
        return found
    raise UnknownChat(spec, _describe(chats), hint=_unsynced_hint(target, cfg, chats))


def _select(target: Target, chats: list[ChatRow]) -> set[int]:
    if target.kind == "id":
        return {chat.id for chat in chats if chat.id == target.value}
    if target.kind == "username":
        wanted = target.text.casefold()
        return {chat.id for chat in chats if (chat.username or "").casefold() == wanted}
    folders = _folders(chats)
    query = dialogs.normalize(target.text)
    if target.kind == "folder":
        exact = [ids for name, ids in folders.items() if dialogs.normalize(name) == query]
        if exact:
            return set().union(*exact)
        return _best_tier([(dialogs.score(query, name), ids) for name, ids in folders.items()])
    scored = [(dialogs.score(query, name), ids) for name, ids in folders.items()]
    for chat in chats:
        value = dialogs.score(query, chat.title or "")
        if chat.username:
            value = max(value, dialogs.score(query, chat.username))
        scored.append((value, {chat.id}))
    return _best_tier(scored)


def _best_tier(scored: list[tuple[float, set[int]]]) -> set[int]:
    """Substring hits when there are any (they score above ``SUBSTRING_BASE``), else the fuzzy
    ones — the same substring-then-``SequenceMatcher`` order ``dialogs.match`` ranks by."""
    substring = [ids for value, ids in scored if value > dialogs.SUBSTRING_BASE]
    tier = substring or [ids for value, ids in scored if value > 0]
    return set().union(*tier)


def _folders(chats: list[ChatRow]) -> dict[str, set[int]]:
    """Folder name → ids of the chats indexed through that folder source."""
    folders: dict[str, set[int]] = {}
    for chat in chats:
        if chat.source_id and chat.source_id.startswith(FOLDER_PREFIX):
            folders.setdefault(chat.source_id[len(FOLDER_PREFIX) :], set()).add(chat.id)
    return folders


def _describe(chats: list[ChatRow]) -> list[str]:
    folders = _folders(chats)
    listing = [f"{FOLDER_PREFIX}{name} ({len(ids)} chats)" for name, ids in sorted(folders.items())]
    for chat in sorted(chats, key=lambda c: dialogs.normalize(c.title or "")):
        handle = f", @{chat.username}" if chat.username else ""
        listing.append(f"{chat.title!r} (id {chat.id}{handle})")
    return listing


def _unsynced_hint(target: Target, cfg: Config, chats: list[ChatRow]) -> str | None:
    """Point at a configured source the spec names when nothing has been indexed through it."""
    indexed = {chat.source_id for chat in chats if chat.source_id}
    for source in cfg.sources:
        if source.id not in indexed and _names_source(target, source):
            return f"source {source.id} is configured but has no indexed chats yet, run a sync"
    return None


def _names_source(target: Target, source: Source) -> bool:
    """Whether a configured entry is the one ``target`` names.

    A chat entry is matched by the identity its ``chat =`` value resolves to
    (:func:`grepogram.sources.same_target`), so the id, the ``@username`` and both ``t.me`` link
    forms of one chat all point at it; free text still scores against the value as written.
    """
    if source.folder is not None:
        if target.kind == "folder":
            return dialogs.normalize(target.text) == dialogs.normalize(source.folder)
        return (
            target.kind == "fuzzy"
            and dialogs.score(dialogs.normalize(target.text), source.folder) > 0
        )
    if target.kind == "fuzzy":
        return dialogs.score(dialogs.normalize(target.text), str(source.chat)) > 0
    try:
        spelled = parse_target(str(source.chat))
    except InvalidTarget:
        return False
    return same_target(spelled, target)


# --- composition -----------------------------------------------------------------------------


def resolve_filters(
    conn: sqlite3.Connection,
    cfg: Config,
    chats: Sequence[str] | None,
    since: str | None,
    until: str | None,
    now: int | None = None,
) -> Filters:
    """The :class:`Filters` for a search: ``chats`` through :func:`resolve_chats` (``None`` or
    empty → no chat filter), ``since`` as a start bound and ``until`` as an inclusive end bound
    through :func:`parse_when` (blank → unbounded), both relative to the same ``now``.
    """
    now = int(time.time()) if now is None else now
    chat_ids = resolve_chats(conn, cfg, chats) if chats else None
    since_ts = _bound(since, now, end=False)
    until_ts = _bound(until, now, end=True)
    if since_ts is not None and until_ts is not None and since_ts > until_ts:
        raise InvalidDate(f"since {since!r} lies after until {until!r}")
    return Filters(chat_ids, since_ts, until_ts)


def _bound(value: str | None, now: int, *, end: bool) -> int | None:
    if value is None or not value.strip():
        return None
    return parse_when(value, now, end=end)
