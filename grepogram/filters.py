"""Search filters: the chat specs and date bounds a user or Claude types, resolved against the
indexed ``chats`` table into the :class:`~grepogram.models.Filters` the search layer applies.

A chat spec is anything :func:`grepogram.sources.parse_target` understands — a marked id,
``@username``, a ``t.me`` link, ``folder:<name>`` — plus ``import:<slug>``, the source id an
export carries, or free text. The two prefixed forms read ``chats.source_id`` and are matched
alike (exact on the name, then scored); free text matches chat titles and usernames the way
``grepogram dialogs`` does: substring hits win, a ``SequenceMatcher`` ratio of at least 0.6 is
the fallback when there is none. Every spec must select at least one indexed chat, and the union
over all specs becomes ``chat_ids``. Dates are unix seconds in UTC; naive input is read as UTC.

:func:`resolve_chat` reads the same specs for the readers (``thread``, ``context``) that address
one message and therefore need exactly one chat: several is :class:`AmbiguousChat` there rather
than a wider search.
"""

import calendar
import datetime as dt
import re
import sqlite3
import time
from collections.abc import Sequence

from grepogram import db, dialogs
from grepogram.models import ChatRow, Config, Filters, Source
from grepogram.sources import (
    FOLDER_PREFIX,
    IMPORT_PREFIX,
    InvalidTarget,
    Target,
    parse_target,
    same_target,
    split_source_id,
)

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


class AmbiguousChat(FilterError):
    """A chat spec selects several indexed chats where one is needed; ``candidates`` lists them."""

    def __init__(self, spec: str, candidates: Sequence[str]) -> None:
        self.spec = spec
        self.candidates = list(candidates)
        listing = "; ".join(self.candidates)
        super().__init__(f"{spec!r} matches several indexed chats: {listing}; name one of them")


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
    the chats indexed through that folder source and ``import:<slug>`` the chat an export was
    stored as (both exact on the name first, then the same matching as free text); free text
    selects every folder and chat whose name contains it, or — when nothing does — every one
    within ``SequenceMatcher`` reach. A spec that selects nothing raises :class:`UnknownChat`
    listing the indexed folders and chats; ``cfg`` only serves that message, so a configured
    source that has never been synced is called out as such.
    """
    chats = db.list_chats(conn)
    coverage = db.chat_sources_map(conn)
    selected: set[int] = set()
    for spec in specs:
        selected |= _resolve_spec(spec, chats, coverage, cfg)
    return selected


def resolve_chat(conn: sqlite3.Connection, cfg: Config, spec: str) -> int:
    """The marked id of the one indexed chat ``spec`` selects.

    Same specs as :func:`resolve_chats`, for the readers that address a single message rather
    than a set to search: a spec that selects nothing still raises :class:`UnknownChat`, and one
    that selects several — a folder name, a title several chats share — raises
    :class:`AmbiguousChat` listing them, where a search would simply have searched them all.
    """
    found = resolve_chats(conn, cfg, [spec])
    if len(found) == 1:
        return found.pop()
    raise AmbiguousChat(spec, [_label(c) for c in _by_title(db.list_chats(conn)) if c.id in found])


def _resolve_spec(
    spec: str, chats: list[ChatRow], coverage: dict[int, list[str]], cfg: Config
) -> set[int]:
    try:
        target = parse_target(spec)
    except InvalidTarget as exc:
        raise UnknownChat(spec, _describe(chats, coverage), hint=str(exc)) from exc
    found = _select(target, chats, coverage)
    if found:
        return found
    raise UnknownChat(
        spec, _describe(chats, coverage), hint=_unsynced_hint(target, cfg, chats, coverage)
    )


def _select(target: Target, chats: list[ChatRow], coverage: dict[int, list[str]]) -> set[int]:
    """The chats ``target`` selects; empty when it names none.

    An id is Telegram's marked id or the row id a result carries (they differ only for a scoped
    chat under a synthetic id). A ``folder:`` spec selects every chat that folder source covers,
    as primary or not (``coverage``); one with an ``<account>/`` prefix only that account's
    folder, and every other spec it restricts to the chats that account's sources could reach.

    ``import:<slug>`` is read here and not by :func:`~grepogram.sources.parse_target`, which
    leaves it a fuzzy target: the score would be taken over the *whole typed string*, seven
    characters of ``import:`` in front of a slug that is often shorter than that, and no such
    spec could reach :data:`grepogram.dialogs.FUZZY_MIN_RATIO`. It is the same reason
    :func:`grepogram.sources.find_source` matches the id exactly on the other side of this
    feature, and the two resolvers of a user-typed spec against ``chats.source_id`` have to
    agree: an ``import:`` id is what ``sources ls``, the MCP ``sources`` tool and every refusal
    message print, so it must scope a search and a reader exactly as ``folder:`` does.
    """
    if target.account is not None:
        chats = [c for c in chats if c.is_shared or c.scope == target.account]
    if target.kind == "id":
        return {chat.id for chat in chats if target.value in (chat.id, chat.peer_id)}
    if target.kind == "username":
        wanted = target.text.casefold()
        return {chat.id for chat in chats if (chat.username or "").casefold() == wanted}
    folders = _tagged(chats, coverage, FOLDER_PREFIX, target.account)
    query = dialogs.normalize(target.text)
    if target.kind == "folder":
        return _by_name(query, folders)
    if target.text.casefold().startswith(IMPORT_PREFIX):
        slug = dialogs.normalize(target.text[len(IMPORT_PREFIX) :])
        return _by_name(slug, _tagged(chats, coverage, IMPORT_PREFIX))
    scored = [(dialogs.score(query, name), ids) for name, ids in folders.items()]
    for chat in chats:
        value = dialogs.score(query, chat.title or "")
        if chat.username:
            value = max(value, dialogs.score(query, chat.username))
        scored.append((value, {chat.id}))
    return _best_tier(scored)


def _by_name(query: str, tagged: dict[str, set[int]]) -> set[int]:
    """The chats a ``folder:`` or ``import:`` spec selects: the source whose name is exactly
    ``query`` when there is one, else the best tier of the scored names."""
    exact = [ids for name, ids in tagged.items() if dialogs.normalize(name) == query]
    if exact:
        return set().union(*exact)
    return _best_tier([(dialogs.score(query, name), ids) for name, ids in tagged.items()])


def _best_tier(scored: list[tuple[float, set[int]]]) -> set[int]:
    """Substring hits when there are any (they score above ``SUBSTRING_BASE``), else the fuzzy
    ones — the same substring-then-``SequenceMatcher`` order ``dialogs.match`` ranks by."""
    substring = [ids for value, ids in scored if value > dialogs.SUBSTRING_BASE]
    tier = substring or [ids for value, ids in scored if value > 0]
    return set().union(*tier)


def _tagged(
    chats: list[ChatRow],
    coverage: dict[int, list[str]],
    prefix: str,
    account: str | None = None,
) -> dict[str, set[int]]:
    """Source name (the id past ``prefix`` and past any ``<account>/``) → ids of the chats that
    source covers, over every account's sources or only ``account``'s.

    Two accounts' folders of one name land under the one name, so an unprefixed ``folder:``
    spec selects both — it is a search scope, and the wider one is what was asked for.
    """
    tagged: dict[str, set[int]] = {}
    for source_id, chat_id in _covering(chats, coverage):
        owner, bare = split_source_id(source_id)
        if bare.startswith(prefix) and (account is None or owner == account):
            tagged.setdefault(bare[len(prefix) :], set()).add(chat_id)
    return tagged


def _covering(chats: list[ChatRow], coverage: dict[int, list[str]]) -> list[tuple[str, int]]:
    """Every ``(source id, chat id)`` pair: each chat's primary source and the others covering
    it (``chat_sources``)."""
    pairs: dict[tuple[str, int], None] = {}
    for chat in chats:
        for source_id in [chat.source_id, *coverage.get(chat.id, [])]:
            if source_id:
                pairs[(source_id, chat.id)] = None
    return list(pairs)


def _describe(chats: list[ChatRow], coverage: dict[int, list[str]]) -> list[str]:
    folders: dict[str, set[int]] = {}
    for source_id, chat_id in _covering(chats, coverage):
        if split_source_id(source_id)[1].startswith(FOLDER_PREFIX):
            folders.setdefault(source_id, set()).add(chat_id)
    listing = [f"{source_id} ({len(ids)} chats)" for source_id, ids in sorted(folders.items())]
    listing += [_label(chat) for chat in _by_title(chats)]
    return listing


def _by_title(chats: list[ChatRow]) -> list[ChatRow]:
    return sorted(chats, key=lambda c: dialogs.normalize(c.title or ""))


def _label(chat: ChatRow) -> str:
    handle = f", @{chat.username}" if chat.username else ""
    return f"{chat.title!r} (id {chat.id}{handle})"


def _unsynced_hint(
    target: Target, cfg: Config, chats: list[ChatRow], coverage: dict[int, list[str]]
) -> str | None:
    """Point at a configured source the spec names when nothing has been indexed through it."""
    indexed = {source_id for source_id, _ in _covering(chats, coverage)}
    for source in cfg.sources:
        if source.id not in indexed and _names_source(target, source):
            return f"source {source.id} is configured but has no indexed chats yet, run a sync"
    return None


def _names_source(target: Target, source: Source) -> bool:
    """Whether a configured entry is the one ``target`` names.

    A chat entry is matched by the identity its ``chat =`` value resolves to
    (:func:`grepogram.sources.same_target`), so the id, the ``@username`` and both ``t.me`` link
    forms of one chat all point at it; free text still scores against the value as written. A
    target with an ``<account>/`` prefix names only that account's entries.
    """
    if target.account is not None and target.account != source.account:
        return False
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
