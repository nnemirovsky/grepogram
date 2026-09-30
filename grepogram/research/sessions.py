"""Research sessions: the errors every research call raises, opening a session, its limits,
and how its text is shown to a human.

A session is the question, the seed chats and the account a research session reads through
(:func:`start_session`); every other research call finds it active first
(:func:`active_session`) and refuses while ``[research] enabled`` is false
(:func:`require_enabled`).
"""

import dataclasses
import json
import logging
import sqlite3
import unicodedata
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from grepogram import config, db, research_db
from grepogram.filters import resolve_chats
from grepogram.models import (
    RESEARCH_LIMIT_MAX,
    Config,
    LimitOverrides,
    ResearchLimits,
    ResearchSession,
    check_research_limit,
)
from grepogram.research.collect import chat_key

log = logging.getLogger(__name__)


ENABLE_HINT = "set `enabled = true` under [research] in config.toml to allow research"


QUESTION_MAX_CHARS = 500
"""The longest question a session takes: it is shown whole in every approval summary."""


_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
"""Unicode categories an approval summary never prints as they are: control characters (the
escape that starts a terminal sequence, line breaks), format characters (bidi overrides,
zero-width marks), surrogates and line/paragraph separators."""


class ResearchError(Exception):
    """A research request that cannot be carried out; ``hint`` says what to do about it."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class ResearchDisabled(ResearchError):
    """``[research] enabled`` is false, the default: research refuses every request."""

    def __init__(self) -> None:
        super().__init__("research is disabled", ENABLE_HINT)


class UnknownSession(ResearchError):
    def __init__(self, session_id: int) -> None:
        super().__init__(
            f"no research session {session_id}", "list sessions with `grepogram research status`"
        )


class SessionStopped(ResearchError):
    def __init__(self, session_id: int) -> None:
        super().__init__(
            f"research session {session_id} is stopped",
            "start a new session to explore further; a stopped one keeps its history",
        )


class UnknownCandidate(ResearchError):
    def __init__(self, session_id: int, candidate_id: int) -> None:
        super().__init__(
            f"no candidate {candidate_id} in research session {session_id}",
            f"list them with `grepogram research candidates {session_id}`",
        )


def require_enabled(cfg: Config) -> None:
    """Raise :class:`ResearchDisabled` unless ``[research] enabled`` is true."""
    if not cfg.research.enabled:
        raise ResearchDisabled


# --- sessions -------------------------------------------------------------------------------------


def start_session(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    cfg: Config,
    question: str,
    seeds: Sequence[str],
    account: str,
    overrides: LimitOverrides | None = None,
    *,
    now: int | None = None,
) -> ResearchSession:
    """Start exploring ``question`` from the indexed chats ``seeds`` select, as ``account``.

    ``seeds`` are chat specs as ``search --chat`` takes them (:func:`grepogram.filters.
    resolve_chats`: an id, ``@name``, a link, a folder, free text, ``account:<name>``); one that
    selects nothing raises :class:`~grepogram.filters.UnknownChat`. ``account`` is the account a
    later run joins and fetches as, and must be one the config knows
    (:class:`~grepogram.config.UnknownAccount` otherwise). The limits are the
    ``[research]`` section's with ``overrides`` (:class:`~grepogram.models.LimitOverrides`, ``None``
    for "keep the config's") applied — the one place a front end's limits are checked
    (:func:`session_limits`). The question is shown in every approval summary, so it is one
    line of plain text of at most :data:`QUESTION_MAX_CHARS` characters. Nothing touches
    Telegram.
    """
    require_enabled(cfg)
    if not question.strip():
        raise ResearchError("a research session needs a question")
    if len(question) > QUESTION_MAX_CHARS:
        raise ResearchError(
            f"a research question is at most {QUESTION_MAX_CHARS} characters; this one has "
            f"{len(question)}",
            "ask it in fewer words",
        )
    if any(unicodedata.category(ch) in _HIDDEN for ch in question):
        raise ResearchError(
            "the question holds control or invisible formatting characters (a line break, a "
            "terminal escape, a direction override)",
            "write it as one line of plain text",
        )
    config.require_account(cfg, account)
    limits = session_limits(cfg, overrides)
    if not seeds:
        raise ResearchError(
            "a research session needs at least one seed chat",
            "name indexed chats to start from, as `search --chat` takes them",
        )
    chosen = [db.get_chat(conn, chat_id) for chat_id in sorted(resolve_chats(conn, cfg, seeds))]
    session = research_db.create_session(
        rdb,
        question=question,
        account=account,
        seeds=[chat_key(chat) for chat in chosen if chat is not None],
        limits=limits,
        now=now,
    )
    log.info(
        "research session %d started as %s from %d seed chat(s)", session.id, account, len(chosen)
    )
    return session


def session_limits(cfg: Config, overrides: LimitOverrides | None = None) -> ResearchLimits:
    """The ``[research]`` limits with ``overrides`` applied; each given value must be a whole
    number from 1 to its ceiling (:func:`~grepogram.models.check_research_limit`), or
    :class:`ResearchError` says which one is not."""
    given: dict[str, int] = {}
    for name, value in (overrides or {}).items():
        if value is None:
            continue
        if name not in RESEARCH_LIMIT_MAX:
            raise ResearchError(
                f"unknown research limit {name!r}", f"limits: {', '.join(RESEARCH_LIMIT_MAX)}"
            )
        try:
            given[name] = check_research_limit(name, value)
        except ValueError as exc:
            raise ResearchError(f"{name} {exc}") from None
    return dataclasses.replace(cfg.research.limits(), **given)


def active_session(rdb: sqlite3.Connection, session_id: int) -> ResearchSession:
    """The session ``session_id``, which must exist and still be active."""
    session = research_db.get_session(rdb, session_id)
    if session is None:
        raise UnknownSession(session_id)
    if session.state != "active":
        raise SessionStopped(session_id)
    return session


def known_session(rdb: sqlite3.Connection, session_id: int) -> ResearchSession:
    """The session ``session_id``, active or stopped; :class:`UnknownSession` when there is none."""
    session = research_db.get_session(rdb, session_id)
    if session is None:
        raise UnknownSession(session_id)
    return session


def horizon(session: ResearchSession) -> str:
    """The date a source a run adds for ``session`` starts from: ``since_days`` before the
    session started, so the date an approval names is the one the run uses, whenever it runs.

    Never before the first date there is: a session stored before ``since_days`` had a ceiling
    may ask for more days than the calendar holds, and must still answer rather than raise."""
    started = datetime.fromtimestamp(session.created_at, UTC).date()
    days = min(session.limits.since_days, (started - date.min).days)
    return (started - timedelta(days=days)).isoformat()


# --- what a human reads ---------------------------------------------------------------------------


def shown(text: str) -> str:
    """``text`` as an approval summary prints it: one line, with every control or invisible
    formatting character (:data:`_HIDDEN`) made visible as U+FFFD and whitespace collapsed.

    A title, a username or a question comes from someone else — a chat's owner, an agent — and
    reaches a terminal or a consent dialog; a terminal escape or a line break could otherwise
    hide the real action lines or forge new ones.
    """
    cleaned = "".join(
        (" " if ch.isspace() else "\ufffd") if unicodedata.category(ch) in _HIDDEN else ch
        for ch in text
    )
    return " ".join(cleaned.split())


def _quoted(text: str) -> str:
    """``text`` :func:`shown` and in double quotes, a quote inside it escaped, so it cannot
    close the quote early and pass what follows off as grepogram's own words."""
    return json.dumps(shown(text), ensure_ascii=False)
