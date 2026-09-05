"""The unit recipe: what says the stored units were cut by an older rule, and the bounded
chat-by-chat re-cut that brings them up to date."""

import logging
import sqlite3
from collections.abc import Iterable

import pytest

from grepogram import db, index, search, sync, units
from grepogram.models import ChatRow, Config, MessageRow, Source, SyncCfg, UnitsCfg
from grepogram.sync import SyncBudget

BASE = 1_705_314_600  # 2024-01-15 10:30:00 UTC
GROUP = -1000000000100
CHANNEL = -1000000000200
DISC = -1000000000300

FOLDER = Source(folder="Argentina")
NEWS = Source(chat="@news", comments=True)
UNITS = UnitsCfg(window_gap_min=30, window_max_msgs=5, window_max_chars=4000, thread_max_msgs=4)
CFG = Config(units=UNITS, sync=SyncCfg(), sources=[FOLDER, NEWS])
"""The recipe pass reads ``units`` and the sources; nothing else of the config reaches it."""

NARROW = Config(
    units=UnitsCfg(window_gap_min=30, window_max_msgs=2, window_max_chars=4000, thread_max_msgs=4),
    sources=[FOLDER, NEWS],
)
"""``CFG`` with a tighter window cap — how a test stands in for a rule change between two cuts."""


# --- fixtures --------------------------------------------------------------------------------


def _msg(
    msg_id: int,
    chat_id: int = GROUP,
    minutes: int = 0,
    reply_to: int | None = None,
    **overrides: object,
) -> MessageRow:
    fields: dict[str, object] = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "date": BASE + minutes * 60,
        "from_id": 1,
        "from_name": "Alice",
        "reply_to_msg_id": reply_to,
        "text": f"message {msg_id}",
    }
    fields.update(overrides)
    return MessageRow(**fields)  # type: ignore[arg-type]


def _chat(chat_id: int, **overrides: object) -> ChatRow:
    fields: dict[str, object] = {
        "id": chat_id,
        "type": "supergroup",
        "title": f"chat {chat_id}",
        "source_id": FOLDER.id,
    }
    fields.update(overrides)
    return ChatRow(**fields)  # type: ignore[arg-type]


def _store(conn: sqlite3.Connection, chat: ChatRow, rows: Iterable[MessageRow]) -> ChatRow:
    """Store a chat and its messages and cut its units the way a first sync does."""
    stored = db.upsert_chat(conn, chat)
    ids = db.upsert_messages(conn, rows)
    sync.on_chat_synced(conn, stored, CFG, ids)
    return stored


def _group(conn: sqlite3.Connection, chat_id: int = GROUP, count: int = 6) -> ChatRow:
    """A group whose messages make both windows and a reply thread."""
    rows = [_msg(1, chat_id), *(_msg(n, chat_id, reply_to=1) for n in range(2, count + 1))]
    return _store(conn, _chat(chat_id), rows)


def _kinds(conn: sqlite3.Connection, chat_id: int) -> set[str]:
    return {unit.kind for unit in db.get_units(conn, chat_id)}


def _unit_ids(conn: sqlite3.Connection, chat_id: int) -> list[int]:
    return [unit.id for unit in db.get_units(conn, chat_id) if unit.id is not None]


def _texts(conn: sqlite3.Connection, chat_id: int) -> list[str]:
    return sorted(unit.text for unit in db.get_units(conn, chat_id))


def _fts_ids(conn: sqlite3.Connection, chat_id: int) -> list[int]:
    rows = conn.execute("SELECT rowid FROM unit_fts WHERE chat_id = ? ORDER BY rowid", (chat_id,))
    return [int(row["rowid"]) for row in rows]


def _bump(monkeypatch: pytest.MonkeyPatch, version: int) -> None:
    monkeypatch.setattr(units, "RECIPE_VERSION", version)


# --- the recorded recipe ---------------------------------------------------------------------


def test_a_fresh_database_records_the_current_recipe(conn: sqlite3.Connection) -> None:
    """Nothing was cut by an older rule, and a sync-time check could never tell: by then
    ``index_pending`` has already cut units for every chat the run fetched."""
    assert db.unit_recipe(conn) == units.RECIPE_VERSION


def test_a_database_with_no_recorded_recipe_reads_as_none(conn: sqlite3.Connection) -> None:
    """What a v0.1.1 index looks like — a mismatch, not a fresh database."""
    conn.execute("DELETE FROM meta WHERE key = ?", (db.META_UNIT_RECIPE,))
    assert db.unit_recipe(conn) is None


def test_a_recipe_that_is_not_a_number_reads_as_none(conn: sqlite3.Connection) -> None:
    db.set_meta(conn, db.META_UNIT_RECIPE, "later")
    assert db.unit_recipe(conn) is None


def test_the_recipe_round_trips(conn: sqlite3.Connection) -> None:
    db.set_unit_recipe(conn, 7)
    assert db.unit_recipe(conn) == 7


def test_recut_markers_carry_a_version_and_clear_together(conn: sqlite3.Connection) -> None:
    db.set_recut_marker(conn, GROUP, 3)
    db.set_recut_marker(conn, CHANNEL, 4)
    assert db.recut_markers(conn) == {GROUP: "3", CHANNEL: "4"}
    db.clear_recut_markers(conn)
    assert db.recut_markers(conn) == {}


def test_a_marker_key_that_names_no_chat_is_skipped(conn: sqlite3.Connection) -> None:
    db.set_meta(conn, f"{db.META_RECUT_PREFIX}everything", "3")
    db.set_recut_marker(conn, GROUP, 3)
    assert db.recut_markers(conn) == {GROUP: "3"}


def test_marker_listing_does_not_match_the_underscores_as_wildcards(
    conn: sqlite3.Connection,
) -> None:
    """``_`` is a single-character wildcard in ``LIKE`` and the prefix holds two of them."""
    db.set_meta(conn, "unitXrecutY:1", "3")
    db.set_recut_marker(conn, GROUP, 3)
    assert db.recut_markers(conn) == {GROUP: "3"}
    db.clear_recut_markers(conn)
    assert db.get_meta(conn, "unitXrecutY:1") == "3"


# --- recut_chat ------------------------------------------------------------------------------


def test_recut_chat_replaces_every_unit_and_reports_the_delta(conn: sqlite3.Connection) -> None:
    chat = _group(conn)
    before = _unit_ids(conn, chat.id)
    delta = units.recut_chat(conn, chat, CFG)
    after = _unit_ids(conn, chat.id)
    assert delta.deleted_ids == before
    assert delta.inserted_ids == after
    assert not set(before) & set(after)


def test_recut_chat_keeps_every_unit_kind_a_conversation_had(conn: sqlite3.Connection) -> None:
    chat = _group(conn)
    assert _kinds(conn, chat.id) == {"window", "thread"}
    units.recut_chat(conn, chat, CFG)
    assert _kinds(conn, chat.id) == {"window", "thread"}


def test_recut_chat_keeps_a_channels_posts_and_post_threads(conn: sqlite3.Connection) -> None:
    channel = _store(
        conn, _chat(CHANNEL, type="channel", source_id=NEWS.id), [_msg(1, CHANNEL, minutes=1)]
    )
    db.upsert_chat(conn, _chat(DISC, source_id=NEWS.id, discussion_of=CHANNEL))
    db.upsert_messages(
        conn, [_msg(1, DISC, minutes=2, comment_of_chat_id=CHANNEL, comment_of_msg_id=1)]
    )
    units.recut_chat(conn, channel, CFG)
    assert _kinds(conn, channel.id) == {"post", "thread"}


def test_recut_chat_applies_the_rule_in_force_now(conn: sqlite3.Connection) -> None:
    """The point of the whole pass: a cut the stored units could not notice on their own."""
    chat = _store(conn, _chat(GROUP), [_msg(n, minutes=n) for n in range(1, 7)])
    assert len(db.get_units(conn, chat.id)) == 2
    units.recut_chat(conn, chat, NARROW)
    assert len(db.get_units(conn, chat.id)) == 3


def test_recut_chat_writes_no_message_row(conn: sqlite3.Connection) -> None:
    """Unit boundaries change, message text does not — so no ``msg_fts`` rewrite and no
    ``indexed = 0`` backlog for the next run's unbudgeted deferred pass to drain."""
    chat = _group(conn)
    before = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
    units.recut_chat(conn, chat, NARROW)
    after = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert db.unindexed_message_ids(conn, chat.id) == []


# --- recut_pending_chats ---------------------------------------------------------------------


async def test_a_freshly_migrated_database_recuts_nothing(conn: sqlite3.Connection) -> None:
    chat = _group(conn)
    before = _unit_ids(conn, chat.id)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == 0
    assert _unit_ids(conn, chat.id) == before
    assert db.recut_markers(conn) == {}


async def test_an_equal_recipe_does_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _group(conn)
    _bump(monkeypatch, 9)
    db.set_unit_recipe(conn, 9)
    before = _unit_ids(conn, chat.id)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == 0
    assert _unit_ids(conn, chat.id) == before


async def test_a_bump_recuts_every_chat_and_records_the_recipe(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _group(conn, GROUP)
    second = _group(conn, GROUP - 1)
    before = _unit_ids(conn, first.id) + _unit_ids(conn, second.id)
    _bump(monkeypatch, 2)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == 2
    after = _unit_ids(conn, first.id) + _unit_ids(conn, second.id)
    assert not set(before) & set(after)
    assert db.unit_recipe(conn) == 2
    assert db.recut_markers(conn) == {}


async def test_a_recut_indexes_the_units_it_cut(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the indexing the chat's ``unit_fts`` stays torn until the next sync."""
    chat = _group(conn)
    _bump(monkeypatch, 2)
    await sync.recut_pending_chats(conn, CFG, SyncBudget())
    assert _fts_ids(conn, chat.id) == _unit_ids(conn, chat.id)
    assert not index.unit_index_gaps(conn, chat.id)


async def test_a_bump_applies_the_rule_in_force_now(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _store(conn, _chat(GROUP), [_msg(n, minutes=n) for n in range(1, 7)])
    _bump(monkeypatch, 2)
    await sync.recut_pending_chats(conn, NARROW, SyncBudget())
    assert len(db.get_units(conn, chat.id)) == 3


async def test_an_index_with_rows_and_no_recipe_is_recut(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v0.1.1 upgrade path: "no recipe recorded" is a mismatch, not a fresh database."""
    chat = _store(conn, _chat(GROUP), [_msg(n, minutes=n) for n in range(1, 7)])
    conn.execute("DELETE FROM meta WHERE key = ?", (db.META_UNIT_RECIPE,))
    _bump(monkeypatch, 2)
    assert await sync.recut_pending_chats(conn, NARROW, SyncBudget()) == 1
    assert len(db.get_units(conn, chat.id)) == 3
    assert db.unit_recipe(conn) == 2


async def test_an_index_with_no_chats_records_the_recipe_without_recutting(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (db.META_UNIT_RECIPE,))
    _bump(monkeypatch, 2)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == 0
    assert db.unit_recipe(conn) == 2


async def test_a_recut_keeps_every_unit_kind_the_chat_had(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = _group(conn)
    channel = _store(
        conn, _chat(CHANNEL, type="channel", source_id=NEWS.id), [_msg(1, CHANNEL, minutes=1)]
    )
    db.upsert_chat(conn, _chat(DISC, source_id=NEWS.id, discussion_of=CHANNEL))
    db.upsert_messages(
        conn, [_msg(1, DISC, minutes=2, comment_of_chat_id=CHANNEL, comment_of_msg_id=1)]
    )
    _bump(monkeypatch, 2)
    await sync.recut_pending_chats(conn, CFG, SyncBudget())
    assert _kinds(conn, group.id) == {"window", "thread"}
    assert _kinds(conn, channel.id) == {"post", "thread"}


async def test_a_link_only_discussion_group_is_recut(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is absent from ``resolve_sources``' output, and its windows hold every comment in the
    index — so the candidates are every stored chat, not the run's own queue."""
    db.upsert_chat(conn, _chat(CHANNEL, type="channel", source_id=NEWS.id))
    group = _store(conn, _chat(DISC, source_id=None, discussion_of=CHANNEL), [_msg(1, DISC)])
    before = _unit_ids(conn, group.id)
    _bump(monkeypatch, 2)
    await sync.recut_pending_chats(conn, CFG, SyncBudget())
    assert not set(before) & set(_unit_ids(conn, group.id))
    assert db.recut_markers(conn) == {}


async def test_at_most_recut_chats_per_run_move(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [GROUP - n for n in range(sync.RECUT_CHATS_PER_RUN + 2)]
    for chat_id in ids:
        _group(conn, chat_id, count=2)
    _bump(monkeypatch, 2)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == sync.RECUT_CHATS_PER_RUN
    assert len(db.recut_markers(conn)) == sync.RECUT_CHATS_PER_RUN
    assert db.unit_recipe(conn) != 2
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == 2
    assert db.unit_recipe(conn) == 2
    assert db.recut_markers(conn) == {}


async def test_a_run_cut_short_resumes_where_it_stopped(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    for chat_id in (GROUP, GROUP - 1, GROUP - 2):
        _group(conn, chat_id, count=2)
    _bump(monkeypatch, 2)
    budget = SyncBudget()

    real = units.recut_chat

    def once(conn_: sqlite3.Connection, chat: ChatRow, cfg: Config) -> units.UnitDelta:
        budget.cancel()
        return real(conn_, chat, cfg)

    monkeypatch.setattr(units, "recut_chat", once)
    assert await sync.recut_pending_chats(conn, CFG, budget) == 1
    monkeypatch.setattr(units, "recut_chat", real)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == 2
    assert db.unit_recipe(conn) == 2


async def test_a_stale_marker_from_an_earlier_recipe_does_not_skip_a_chat(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the last chat but before the cleanup leaves markers behind; a presence flag
    would make the next bump skip exactly the chats already done."""
    chat = _group(conn)
    db.set_recut_marker(conn, chat.id, 2)
    before = _unit_ids(conn, chat.id)
    _bump(monkeypatch, 3)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == 1
    assert not set(before) & set(_unit_ids(conn, chat.id))
    assert db.unit_recipe(conn) == 3


async def test_a_chat_removed_before_its_recut_is_skipped(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise ``insert_units`` raises ``IntegrityError`` outside the chat loop's guard and
    escapes ``sync_all`` as a traceback."""
    _group(conn, GROUP)
    survivor = _group(conn, GROUP - 1)
    _bump(monkeypatch, 2)
    real = units.recut_chat
    removed = False

    def drop_first(conn_: sqlite3.Connection, chat: ChatRow, cfg: Config) -> units.UnitDelta:
        nonlocal removed
        if not removed:
            removed = True
            db.delete_chat(conn_, GROUP)
        return real(conn_, chat, cfg)

    monkeypatch.setattr(units, "recut_chat", drop_first)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == 1
    assert db.recut_markers(conn) == {}
    assert db.unit_recipe(conn) == 2
    assert db.get_units(conn, survivor.id)


async def test_the_pass_writes_no_message_row(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _group(conn, GROUP)
    _group(conn, GROUP - 1)
    before = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
    _bump(monkeypatch, 2)
    await sync.recut_pending_chats(conn, NARROW, SyncBudget())
    after = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert db.chats_with_unindexed(conn) == []


# --- when a re-cut is allowed to start -------------------------------------------------------


async def test_a_budget_below_the_floor_does_not_start_one(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    chat = _group(conn)
    before = _unit_ids(conn, chat.id)
    _bump(monkeypatch, 2)
    with caplog.at_level(logging.INFO, logger="grepogram.sync"):
        assert await sync.recut_pending_chats(conn, CFG, SyncBudget(1)) == 0
    assert _unit_ids(conn, chat.id) == before
    assert db.unit_recipe(conn) != 2
    assert db.recut_markers(conn) == {}
    assert "re-cut is pending" in caplog.text


async def test_the_auto_sync_budget_never_starts_one(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``search.auto_sync_budget_s`` defaults to 20 s, and a search must not empty an index."""
    chat = _group(conn)
    before = _unit_ids(conn, chat.id)
    _bump(monkeypatch, 2)
    seconds = float(Config().search.auto_sync_budget_s)
    assert seconds < sync.RECUT_MIN_BUDGET_S
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget(seconds)) == 0
    assert _unit_ids(conn, chat.id) == before


async def test_an_unlimited_budget_starts_one(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = _group(conn)
    before = _unit_ids(conn, chat.id)
    _bump(monkeypatch, 2)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget()) == 1
    assert not set(before) & set(_unit_ids(conn, chat.id))


async def test_a_generous_budget_starts_one(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _group(conn)
    _bump(monkeypatch, 2)
    assert await sync.recut_pending_chats(conn, CFG, SyncBudget(sync.RECUT_MIN_BUDGET_S * 2)) == 1


async def test_a_recut_without_an_embedder_warns_that_the_vectors_went(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``cli._optional_embedder`` hands ``sync_all`` ``None`` when the model cannot load, and the
    re-cut chats would silently end up unembedded."""
    _group(conn)
    _bump(monkeypatch, 2)
    with caplog.at_level(logging.WARNING, logger="grepogram.sync"):
        await sync.recut_pending_chats(conn, CFG, SyncBudget())
    assert "grepogram embed" in caplog.text


async def test_no_such_warning_with_an_embedder(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from grepogram.embed import FakeEmbedder

    _group(conn)
    _bump(monkeypatch, 2)
    with caplog.at_level(logging.WARNING, logger="grepogram.sync"):
        await sync.recut_pending_chats(conn, CFG, SyncBudget(), FakeEmbedder())
    assert "grepogram embed" not in caplog.text


# --- the warning a search re-derives ---------------------------------------------------------


def test_search_warns_while_a_recut_is_pending(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short-budget path writes no flag, and an MCP-only user — whose syncs are the
    20-second ones inside a ``search`` call — is exactly who never sees the log line."""
    _group(conn)
    _bump(monkeypatch, 2)
    result = search.search(conn, CFG, "message")
    assert search.RECUT_PENDING in result.warnings


def test_search_does_not_warn_once_the_recipe_matches(conn: sqlite3.Connection) -> None:
    _group(conn)
    result = search.search(conn, CFG, "message")
    assert search.RECUT_PENDING not in result.warnings


def test_an_empty_index_says_it_is_empty_rather_than_pending(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bump(monkeypatch, 2)
    result = search.search(conn, CFG, "message")
    assert result.warnings == [search.NOTHING_INDEXED]
