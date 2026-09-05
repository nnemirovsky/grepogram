"""The extraction pass: its offline half, its network half and the two writers under both.

Nothing here reaches Telegram — ``FakeClient`` answers ``get_messages`` and ``download_media``
from the fixtures registered on it, and the committed ``sample.pdf`` is what a download writes.
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from telethon import errors
from telethon.tl import types

from grepogram import db, extract, index, media, sync, units
from grepogram.extract import ExtractError
from grepogram.models import ChatRow, Config, MediaCfg, MessageRow, TelegramCfg, UnitsCfg
from grepogram.sync import SyncBudget
from tests.fakes import FakeClient
from tests.fixtures import tl

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_PDF = FIXTURES / "sample.pdf"
CHAT_ID = -1000000000100
OTHER_ID = -1000000000101


def _chat(chat_id: int = CHAT_ID) -> ChatRow:
    return ChatRow(id=chat_id, type="supergroup", title=f"chat {chat_id}", source_id="chat:@x")


def _cfg(**media_kw: Any) -> Config:
    return Config(telegram=TelegramCfg(api_id=1, api_hash="h"), media=MediaCfg(**media_kw))


def _message(chat_id: int, msg_id: int, **overrides: Any) -> MessageRow:
    fields: dict[str, Any] = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "date": 1_700_000_000 + msg_id,
        "text": "",
    }
    fields.update(overrides)
    return MessageRow(**fields)


def _pdf_row(chat_id: int, msg_id: int, name: str = "note.pdf") -> MessageRow:
    return _message(chat_id, msg_id, media_kind="document", media_filename=name)


def _states(conn: sqlite3.Connection) -> dict[int, int]:
    """``msg_id`` to ``media_state`` for every stored row."""
    rows = conn.execute("SELECT msg_id, media_state FROM messages ORDER BY msg_id")
    return {int(row["msg_id"]): int(row["media_state"]) for row in rows}


def _indexed(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute("SELECT msg_id, indexed FROM messages ORDER BY msg_id")
    return {int(row["msg_id"]): int(row["indexed"]) for row in rows}


def _text(conn: sqlite3.Connection, msg_id: int) -> str | None:
    row = conn.execute("SELECT extracted_text FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
    value = row["extracted_text"]
    return None if value is None else str(value)


def _fetches(client: FakeClient) -> list[dict[str, Any]]:
    """What the pass asked Telegram to re-fetch, one entry per ``get_messages`` batch."""
    return [args for name, args in client.calls if name == "get_messages"]


def _pdf_client(*msg_ids: int, chat_id: int = CHAT_ID, name: str = "note.pdf") -> FakeClient:
    """A client holding one PDF message per id, each downloading the committed fixture."""
    payload = SAMPLE_PDF.read_bytes()
    return FakeClient(
        messages={chat_id: [tl.document_message(chat_id, i, name) for i in msg_ids]},
        downloads={(chat_id, i): payload for i in msg_ids},
    )


def _stub(text: str = "stub text") -> extract.Extractor:
    """An extractor that reads nothing and answers ``text`` — a registry entry with no library."""
    return lambda _: text


@pytest.fixture(autouse=True)
def one_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin what this build can read, so no assertion here depends on the machine it runs on.

    ``document`` is what CI can run (pypdf and python-docx are in the dev group); ``photo`` needs
    a Mac carrying the ``media`` extra, so it is deliberately absent and the tests that want it
    register their own.
    """
    monkeypatch.setattr(extract, "registry", lambda: {"document": extract.extract_document})


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Downloads land in a directory the test can look inside after the pass has ended."""
    directory = tmp_path / "scratch"
    directory.mkdir()

    def visible() -> Any:
        class _Kept:
            def __enter__(self) -> Path:
                return directory

            def __exit__(self, *_: Any) -> None:
                return None

        return _Kept()

    monkeypatch.setattr(media, "_scratch", visible)
    return directory


# --- the writers -------------------------------------------------------------------------


def test_set_media_text_stores_the_text_and_flags_the_row(conn: sqlite3.Connection) -> None:
    """The one transition that changes a rendered line is the one that flags a rebuild."""
    db.upsert_chat(conn, _chat())
    [row_id] = db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    db.mark_indexed(conn, [row_id])
    db.set_media_text(conn, row_id, "recognised text")
    assert _text(conn, 1) == "recognised text"
    assert _states(conn) == {1: db.MEDIA_EXTRACTED}
    assert _indexed(conn) == {1: 0}
    assert not conn.in_transaction


def test_set_media_state_writes_the_state_and_leaves_indexed_alone(
    conn: sqlite3.Connection,
) -> None:
    db.upsert_chat(conn, _chat())
    ids = db.upsert_messages(conn, [_pdf_row(CHAT_ID, i) for i in (1, 2)])
    db.mark_indexed(conn, ids)
    db.set_media_state(conn, ids, db.MEDIA_SKIPPED)
    assert _states(conn) == {1: db.MEDIA_SKIPPED, 2: db.MEDIA_SKIPPED}
    assert _indexed(conn) == {1: 1, 2: 1}
    assert _text(conn, 1) is None
    assert not conn.in_transaction


def test_set_media_state_handles_more_ids_than_one_in_list(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat())
    ids = db.upsert_messages(conn, [_pdf_row(CHAT_ID, i) for i in range(1, db.IN_BATCH + 3)])
    db.set_media_state(conn, ids, db.MEDIA_UNSUPPORTED)
    assert set(_states(conn).values()) == {db.MEDIA_UNSUPPORTED}


def test_move_media_state_moves_only_the_named_kinds(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_messages(
        conn,
        [
            _message(CHAT_ID, 1, media_kind="video"),
            _pdf_row(CHAT_ID, 2),
            _message(CHAT_ID, 3),
        ],
    )
    moved = db.move_media_state(conn, ["video"], frm=db.MEDIA_PENDING, to=db.MEDIA_UNSUPPORTED)
    assert moved == 1
    assert _states(conn) == {1: db.MEDIA_UNSUPPORTED, 2: db.MEDIA_PENDING, 3: db.MEDIA_PENDING}
    assert db.move_media_state(conn, [], frm=db.MEDIA_PENDING, to=db.MEDIA_DISABLED) == 0
    assert not conn.in_transaction


def test_pending_queries_see_only_media_at_pending(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_chat(conn, _chat(OTHER_ID))
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1), _message(CHAT_ID, 2), _pdf_row(OTHER_ID, 1)])
    assert db.count_pending_media(conn) == 2
    # marked channel ids are negative, so ascending order puts the later one first
    assert db.chats_with_pending_media(conn) == [OTHER_ID, CHAT_ID]
    assert [row.msg_id for row in db.messages_pending_media(conn, 10)] == [1, 1]
    assert [row.chat_id for row in db.messages_pending_media(conn, 10, CHAT_ID)] == [CHAT_ID]
    assert len(db.messages_pending_media(conn, 1)) == 1
    db.set_media_state(conn, [1], db.MEDIA_UNSUPPORTED)
    assert db.count_pending_media(conn) == 1


# --- the offline half --------------------------------------------------------------------


def _mixed(conn: sqlite3.Connection) -> list[int]:
    """One row of every media kind plus a text row, all freshly stored and pending."""
    db.upsert_chat(conn, _chat())
    rows = [_message(CHAT_ID, i, media_kind=kind) for i, kind in enumerate(media.ALL_KINDS, 1)]
    rows.append(_message(CHAT_ID, len(rows) + 1))
    return db.upsert_messages(conn, rows)


def _kind_states(conn: sqlite3.Connection) -> dict[str | None, int]:
    rows = conn.execute("SELECT media_kind, media_state FROM messages")
    return {row["media_kind"]: int(row["media_state"]) for row in rows}


def test_the_offline_pass_parks_every_kind_with_no_extractor(conn: sqlite3.Connection) -> None:
    _mixed(conn)
    extractors = {"document": extract.extract_document}
    parked, disabled, requeued = media.resolve_offline_states(conn, _cfg(), extractors)  # type: ignore[arg-type]
    assert (parked, disabled, requeued) == (len(media.ALL_KINDS) - 1, 0, 0)
    states = _kind_states(conn)
    assert states["document"] == db.MEDIA_PENDING
    assert states["photo"] == db.MEDIA_UNSUPPORTED
    assert states["voice"] == db.MEDIA_UNSUPPORTED
    assert states["video_note"] == db.MEDIA_UNSUPPORTED
    assert states[None] == db.MEDIA_PENDING, "a row with no media is not the pass's business"


def test_the_offline_pass_changes_no_indexed_value(conn: sqlite3.Connection) -> None:
    """The hazard this whole design is built around.

    Most kinds have no extractor, so flagging here would mark tens of thousands of rows across
    every chat — and ``sync._sync_chats``' deferred ``index_pending`` loop has no budget check,
    so the next 20-second auto-sync inside a ``search`` would rebuild and re-embed all of them.
    """
    ids = _mixed(conn)
    db.mark_indexed(conn, ids)
    before = _indexed(conn)
    media.resolve_offline_states(conn, _cfg(ocr=False), {"document": extract.extract_document})
    assert _indexed(conn) == before
    assert set(before.values()) == {1}


def test_a_disabled_kind_is_parked_and_requeued_when_it_comes_back(
    conn: sqlite3.Connection,
) -> None:
    _mixed(conn)
    extractors: Any = {"document": extract.extract_document, "photo": extract.ocr_image}
    _, disabled, _ = media.resolve_offline_states(conn, _cfg(documents=False), extractors)
    assert disabled == 1
    assert _kind_states(conn)["document"] == db.MEDIA_DISABLED
    assert _kind_states(conn)["photo"] == db.MEDIA_PENDING
    _, _, requeued = media.resolve_offline_states(conn, _cfg(), extractors)
    assert requeued == 1
    assert _kind_states(conn)["document"] == db.MEDIA_PENDING


def test_a_kind_that_is_both_switched_off_and_unreadable_here_is_unsupported(
    conn: sqlite3.Connection,
) -> None:
    """No extractor is the stronger fact: switching it back on must not queue the unreadable."""
    _mixed(conn)
    media.resolve_offline_states(conn, _cfg(ocr=False), {"document": extract.extract_document})
    assert _kind_states(conn)["photo"] == db.MEDIA_UNSUPPORTED
    media.resolve_offline_states(conn, _cfg(), {"document": extract.extract_document})
    assert _kind_states(conn)["photo"] == db.MEDIA_UNSUPPORTED


def test_disabled_kinds_follow_the_two_switches() -> None:
    assert media.disabled_kinds(_cfg()) == set()
    assert media.disabled_kinds(_cfg(ocr=False)) == {"photo"}
    assert media.disabled_kinds(_cfg(documents=False)) == {"document"}
    assert media.disabled_kinds(_cfg(ocr=False, documents=False)) == {"photo", "document"}


async def test_the_offline_states_are_resolved_with_no_client_call_at_all(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not one of the tens of thousands of parked rows costs a Telegram request."""
    ids = _mixed(conn)
    db.mark_indexed(conn, ids)
    monkeypatch.setattr(
        extract, "registry", lambda: {"document": extract.extract_document, "photo": _stub()}
    )
    client = FakeClient()
    report = await media.run(conn, client, _cfg(ocr=False, documents=False), SyncBudget())
    assert client.calls == []
    assert report.disabled == 2
    assert report.unsupported == len(media.ALL_KINDS) - 2
    assert report.extracted == 0
    assert set(_indexed(conn).values()) == {1}


# --- the network half --------------------------------------------------------------------


async def test_a_pdf_is_downloaded_extracted_and_stored(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    ids = db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    db.mark_indexed(conn, ids)
    client = _pdf_client(1)
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.extracted == 1
    assert report.remaining == 0
    stored = _text(conn, 1)
    assert stored and "sample pdf" in stored.lower()
    assert _states(conn) == {1: db.MEDIA_EXTRACTED}
    assert _indexed(conn) == {1: 0}, "the rendered line changed, so its units are behind"
    assert _fetches(client) == [{"chat_id": CHAT_ID, "limit": None, "ids": [1]}]
    assert [name for name, _ in client.calls if name == "download_media"]


async def test_the_temp_file_is_removed_after_a_success(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    await media.run(conn, _pdf_client(1), _cfg(), SyncBudget())
    assert list(scratch.iterdir()) == []


async def test_the_temp_file_is_removed_after_a_failed_extraction(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    client = FakeClient(
        messages={CHAT_ID: [tl.document_message(CHAT_ID, 1, "note.pdf")]},
        downloads={(CHAT_ID, 1): b"not a pdf at all"},
    )
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.failed == 1
    assert _states(conn) == {1: db.MEDIA_FAILED}
    assert list(scratch.iterdir()) == []


async def test_the_temp_file_carries_the_extension_the_dispatcher_reads(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    """A name is only ever a suffix here — the rest of it has no business becoming a path."""
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1, name="../../../etc/pass wd.PDF")])
    client = _pdf_client(1, name="../../../etc/pass wd.PDF")
    await media.run(conn, client, _cfg(), SyncBudget())
    [call] = [args for name, args in client.calls if name == "download_media"]
    assert Path(str(call["file"])).name == f"{CHAT_ID}_1.pdf"
    assert Path(str(call["file"])).parent == scratch
    assert _states(conn) == {1: db.MEDIA_EXTRACTED}


async def test_a_file_over_the_cap_is_skipped_before_it_is_downloaded(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    ids = db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    db.mark_indexed(conn, ids)
    message = tl.document_message(CHAT_ID, 1, "huge.pdf")
    message.media.document.size = 21 * 1024 * 1024
    client = FakeClient(messages={CHAT_ID: [message]}, downloads={(CHAT_ID, 1): b"never read"})
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.skipped == 1
    assert _states(conn) == {1: db.MEDIA_SKIPPED}
    assert _indexed(conn) == {1: 1}, "nothing was read, so nothing needs a rebuild"
    assert not [name for name, _ in client.calls if name == "download_media"]


async def test_a_file_at_the_cap_is_still_downloaded(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    message = tl.document_message(CHAT_ID, 1, "note.pdf")
    message.media.document.size = 20 * 1024 * 1024
    client = FakeClient(
        messages={CHAT_ID: [message]}, downloads={(CHAT_ID, 1): SAMPLE_PDF.read_bytes()}
    )
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.extracted == 1


async def test_a_message_telegram_no_longer_returns_is_failed_not_lost(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    report = await media.run(conn, FakeClient(messages={CHAT_ID: []}), _cfg(), SyncBudget())
    assert report.failed == 1
    assert _states(conn) == {1: db.MEDIA_FAILED}


async def test_media_that_will_not_download_is_failed(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    client = FakeClient(messages={CHAT_ID: [tl.document_message(CHAT_ID, 1, "note.pdf")]})
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.failed == 1
    assert _states(conn) == {1: db.MEDIA_FAILED}


async def test_a_failed_extraction_is_retried_only_with_retry_failed(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    broken = FakeClient(
        messages={CHAT_ID: [tl.document_message(CHAT_ID, 1, "note.pdf")]},
        downloads={(CHAT_ID, 1): b"not a pdf at all"},
    )
    assert (await media.run(conn, broken, _cfg(), SyncBudget())).failed == 1
    good = _pdf_client(1)
    again = await media.run(conn, good, _cfg(), SyncBudget())
    assert (again.extracted, again.requeued) == (0, 0)
    assert good.calls == [], "a failed row stays out of the queue until it is asked for"
    retried = await media.run(conn, good, _cfg(), SyncBudget(), retry_failed=True)
    assert (retried.extracted, retried.requeued) == (1, 1)
    assert _states(conn) == {1: db.MEDIA_EXTRACTED}


async def test_retry_failed_requeues_what_an_earlier_build_could_not_read(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing the ``media`` extra is the only way out of ``MEDIA_UNSUPPORTED``."""
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_message(CHAT_ID, 1, media_kind="photo")])
    client = FakeClient(
        messages={CHAT_ID: [tl.photo_message(CHAT_ID, 1)]},
        downloads={(CHAT_ID, 1): b"jpeg bytes"},
    )
    monkeypatch.setattr(extract, "registry", dict)
    assert (await media.run(conn, client, _cfg(), SyncBudget())).unsupported >= 1
    assert _states(conn) == {1: db.MEDIA_UNSUPPORTED}
    assert (await media.run(conn, client, _cfg(), SyncBudget(), retry_failed=True)).requeued == 0
    assert _states(conn) == {1: db.MEDIA_UNSUPPORTED}, "still no extractor, still nothing to queue"
    monkeypatch.setattr(extract, "registry", lambda: {"photo": _stub("now readable")})
    report = await media.run(conn, client, _cfg(), SyncBudget(), retry_failed=True)
    assert (report.requeued, report.extracted) == (1, 1)
    assert _text(conn, 1) == "now readable"


async def test_a_photo_is_read_by_the_registered_extractor(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point, with the one call CI cannot make replaced: OCR text lands on the row."""
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_message(CHAT_ID, 1, media_kind="photo")])
    monkeypatch.setattr(extract, "registry", lambda: {"photo": lambda path: f"read {path.name}"})
    client = FakeClient(
        messages={CHAT_ID: [tl.photo_message(CHAT_ID, 1)]},
        downloads={(CHAT_ID, 1): b"jpeg bytes"},
    )
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.extracted == 1
    assert _text(conn, 1) == f"read {CHAT_ID}_1"


async def test_an_image_holding_no_text_is_extracted_not_failed(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_message(CHAT_ID, 1, media_kind="photo")])
    monkeypatch.setattr(extract, "registry", lambda: {"photo": _stub("")})
    client = FakeClient(
        messages={CHAT_ID: [tl.photo_message(CHAT_ID, 1)]},
        downloads={(CHAT_ID, 1): b"jpeg bytes"},
    )
    assert (await media.run(conn, client, _cfg(), SyncBudget())).extracted == 1
    assert _states(conn) == {1: db.MEDIA_EXTRACTED}
    assert _text(conn, 1) == ""


async def test_a_kind_with_no_extractor_that_reaches_the_loop_is_parked_not_downloaded(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The net under the offline pass: nothing is fetched for a kind nothing here can read."""
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_message(CHAT_ID, 1, media_kind="video")])
    monkeypatch.setattr(media, "resolve_offline_states", lambda *_, **__: (0, 0, 0))
    client = FakeClient(
        messages={CHAT_ID: [tl.document_message(CHAT_ID, 1, "clip.mp4")]},
        downloads={(CHAT_ID, 1): b"video bytes"},
    )
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.unsupported == 1
    assert _states(conn) == {1: db.MEDIA_UNSUPPORTED}
    assert not [name for name, _ in client.calls if name == "download_media"]


async def test_the_pass_is_off_when_media_enabled_is_false(conn: sqlite3.Connection) -> None:
    db.upsert_chat(conn, _chat())
    ids = db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    db.mark_indexed(conn, ids)
    client = _pdf_client(1)
    report = await media.run(conn, client, _cfg(enabled=False), SyncBudget())
    assert client.calls == []
    assert report.extracted == 0
    assert report.remaining == 1
    assert report.warnings and "enabled is false" in report.warnings[0]
    assert _states(conn) == {1: db.MEDIA_PENDING}
    assert _indexed(conn) == {1: 1}


# --- the budget and what a run keeps -------------------------------------------------------


async def test_the_budget_stops_the_pass_and_the_next_run_resumes(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch commits what it read; the rest stays queued and the next run picks it up."""
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, i) for i in (1, 2, 3)])
    client = _pdf_client(1, 2, 3)
    budget = SyncBudget()
    real_read = media._read

    def read_then_run_out(extractor: Any, path: Path) -> str:
        budget.cancel()  # the last thing this run has time for
        return str(real_read(extractor, path))

    monkeypatch.setattr(media, "_read", read_then_run_out)
    report = await media.run(conn, client, _cfg(), budget)
    assert report.extracted == 1
    assert report.remaining == 2
    assert _states(conn) == {1: db.MEDIA_EXTRACTED, 2: db.MEDIA_PENDING, 3: db.MEDIA_PENDING}
    monkeypatch.setattr(media, "_read", real_read)
    rest = await media.run(conn, client, _cfg(), SyncBudget())
    assert rest.extracted == 2
    assert _states(conn) == dict.fromkeys((1, 2, 3), db.MEDIA_EXTRACTED)


async def test_an_expired_budget_reads_nothing_and_still_resolves_the_offline_states(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    _mixed(conn)
    client = FakeClient()
    budget = SyncBudget()
    budget.cancel()
    report = await media.run(conn, client, _cfg(), budget)
    assert client.calls == []
    assert report.unsupported >= 1
    assert report.extracted == 0


async def test_the_flood_sleep_threshold_shrinks_with_the_time_left(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    """A bounded run never lets Telethon sleep through a wait longer than the run has."""
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    client = _pdf_client(1)
    client.flood_sleep_threshold = 120
    await media.run(conn, client, _cfg(), SyncBudget(5.0))
    assert client.flood_sleep_threshold == 5
    client.flood_sleep_threshold = 120
    db.set_media_state(conn, [1], db.MEDIA_PENDING)
    await media.run(conn, client, _cfg(), SyncBudget())
    assert client.flood_sleep_threshold == 120, "an unlimited run leaves the configured threshold"


async def test_a_flood_wait_keeps_what_the_run_earned_and_stops(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_chat(conn, _chat(OTHER_ID))
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 2), _pdf_row(OTHER_ID, 1)])
    # OTHER_ID sorts first (marked ids are negative), so it is read before the flood wait lands
    client = _pdf_client(1, chat_id=OTHER_ID)
    client.failures[CHAT_ID] = errors.FloodWaitError(request=None, capture=30)
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.extracted == 1
    assert report.remaining == 1
    assert report.warnings and "flood wait" in report.warnings[0]
    assert _states(conn) == {1: db.MEDIA_EXTRACTED, 2: db.MEDIA_PENDING}


async def test_an_rpc_error_costs_one_chat_its_turn_and_the_rest_runs(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    db.upsert_chat(conn, _chat())
    db.upsert_chat(conn, _chat(OTHER_ID))
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1), _pdf_row(OTHER_ID, 2)])
    payload = SAMPLE_PDF.read_bytes()
    client = FakeClient(
        messages={
            CHAT_ID: [tl.document_message(CHAT_ID, 1, "note.pdf")],
            OTHER_ID: [tl.document_message(OTHER_ID, 2, "note.pdf")],
        },
        downloads={(CHAT_ID, 1): payload, (OTHER_ID, 2): payload},
        failures={CHAT_ID: errors.ChannelPrivateError(request=None)},
    )
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.extracted == 1
    assert report.warnings and str(CHAT_ID) in report.warnings[0]
    assert _states(conn) == {1: db.MEDIA_PENDING, 2: db.MEDIA_EXTRACTED}


async def test_a_chat_that_fails_is_not_retried_in_the_same_run(
    conn: sqlite3.Connection, scratch: Path
) -> None:
    """The loop must never spin on a batch it cannot resolve."""
    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, i) for i in (1, 2)])
    client = FakeClient(failures={CHAT_ID: errors.ChannelPrivateError(request=None)})
    report = await media.run(conn, client, _cfg(), SyncBudget())
    assert report.remaining == 2
    assert len([name for name, _ in client.calls if name == "get_messages"]) == 1


# --- reading the size off the re-fetched media ---------------------------------------------


def test_media_size_reads_a_document() -> None:
    message = tl.document_message(CHAT_ID, 1, "note.pdf")
    assert media.media_size(message.media) == 1024


def test_media_size_reads_the_largest_photo_size() -> None:
    photo = tl.photo(1)
    photo.sizes = [
        types.PhotoSizeEmpty("a"),
        types.PhotoSize("m", 320, 240, 1000),
        types.PhotoStrippedSize("i", b"abc"),
        types.PhotoSizeProgressive("y", 800, 600, [500, 9000]),
    ]
    assert media.media_size(types.MessageMediaPhoto(photo=photo)) == 9000


def test_media_size_is_none_when_telegram_reports_none() -> None:
    photo = tl.photo(1)
    photo.sizes = []
    assert media.media_size(types.MessageMediaPhoto(photo=photo)) is None
    assert media.media_size(None) is None
    assert media.media_size(types.MessageMediaPoll(poll=None, results=None)) is None
    document = tl.document(mime_type="application/pdf")
    document.size = 0
    assert media.media_size(types.MessageMediaDocument(document=document)) is None


# --- the seams the pass is built on --------------------------------------------------------


def test_the_scratch_directory_is_removed_with_whatever_is_left_in_it() -> None:
    with media._scratch() as directory:
        assert directory.is_dir()
        (directory / "leftover").write_bytes(b"x")
    assert not directory.exists()


def test_removing_a_file_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    media._remove(tmp_path / "never written")
    media._remove(None)
    media._remove(tmp_path)  # a directory: logged, never raised


def test_read_hands_the_path_to_the_extractor(tmp_path: Path) -> None:
    def extractor(path: Path) -> str:
        return f"read {path.name}"

    assert media._read(extractor, tmp_path / "a.pdf") == "read a.pdf"


async def test_an_extract_error_out_of_the_registry_becomes_a_failed_row(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: Path) -> str:
        raise ExtractError("no")

    db.upsert_chat(conn, _chat())
    db.upsert_messages(conn, [_pdf_row(CHAT_ID, 1)])
    monkeypatch.setattr(extract, "registry", lambda: {"document": boom})
    report = await media.run(conn, _pdf_client(1), _cfg(), SyncBudget())
    assert report.failed == 1


# --- the re-cut that makes the text searchable ---------------------------------------------


def _units(conn: sqlite3.Connection, chat_id: int = CHAT_ID) -> dict[tuple[int, ...], str]:
    """Every unit of a chat as ``msg_ids`` to text."""
    return {tuple(unit.msg_ids): unit.text for unit in db.get_units(conn, chat_id)}


def _fts(conn: sqlite3.Connection, chat_id: int = CHAT_ID) -> list[str]:
    rows = conn.execute("SELECT raw FROM unit_fts WHERE chat_id = ? ORDER BY rowid", (chat_id,))
    return [str(row["raw"]) for row in rows]


def _synced(conn: sqlite3.Connection, rows: list[MessageRow], cfg: Config) -> ChatRow:
    """A chat with its messages stored and its units cut, exactly as a first sync leaves it."""
    chat = db.upsert_chat(conn, _chat())
    sync.on_chat_synced(conn, chat, cfg, db.upsert_messages(conn, rows))
    return chat


def _text_row(msg_id: int) -> MessageRow:
    return _message(CHAT_ID, msg_id, text=f"message {msg_id}")


async def test_ocr_text_reaches_the_closed_window_the_photo_sits_in(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the whole feature comes down to: without the re-cut the text is written to a column
    nothing renders, because a sync never re-cuts a closed window and clears the flag anyway."""
    cfg = _cfg()
    windows = UnitsCfg(window_gap_min=30, window_max_msgs=3, window_max_chars=4000)
    cfg = Config(telegram=cfg.telegram, media=cfg.media, units=windows)
    photo = _message(CHAT_ID, 2, media_kind="photo")
    _synced(conn, [_text_row(1), photo, *(_text_row(i) for i in (3, 4, 5))], cfg)
    assert "[photo]" in _units(conn)[(1, 2, 3)]

    monkeypatch.setattr(extract, "registry", lambda: {"photo": _stub("ОТКРЫТО с 9:00")})
    client = FakeClient(
        messages={CHAT_ID: [tl.photo_message(CHAT_ID, 2)]},
        downloads={(CHAT_ID, 2): b"jpeg bytes"},
    )
    assert (await media.run(conn, client, cfg, SyncBudget())).extracted == 1
    assert "[photo] ОТКРЫТО с 9:00" in _units(conn)[(1, 2, 3)]
    assert any("ОТКРЫТО с 9:00" in raw for raw in _fts(conn)), "and it is searchable"
    assert not index.unit_index_gaps(conn, CHAT_ID)


async def test_one_batch_recuts_the_chat_once_not_once_per_message(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fifty photos in one chat would otherwise re-cut and re-embed its tail fifty times."""
    ids = list(range(1, media.BATCH + 1))
    cfg = _cfg()
    _synced(conn, [_message(CHAT_ID, i, media_kind="photo") for i in ids], cfg)
    calls: list[int] = []
    real = units.invalidate_units_for

    def counted(c: sqlite3.Connection, chat: ChatRow, config: Config, rows: Any) -> units.UnitDelta:
        calls.append(len(rows))
        return real(c, chat, config, rows)

    monkeypatch.setattr(units, "invalidate_units_for", counted)
    monkeypatch.setattr(extract, "registry", lambda: {"photo": _stub("read")})
    client = FakeClient(
        messages={CHAT_ID: [tl.photo_message(CHAT_ID, i) for i in ids]},
        downloads={(CHAT_ID, i): b"jpeg bytes" for i in ids},
    )
    assert (await media.run(conn, client, cfg, SyncBudget())).extracted == len(ids)
    assert calls == [len(ids)]
    assert all("[photo] read" in text for text in _units(conn).values())


async def test_a_batch_that_read_nothing_recuts_nothing(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg()
    _synced(conn, [_message(CHAT_ID, 1, media_kind="photo")], cfg)
    before = _units(conn)
    calls: list[int] = []

    def never(*_: Any) -> units.UnitDelta:
        calls.append(1)
        return units.UnitDelta()

    monkeypatch.setattr(units, "invalidate_units_for", never)
    monkeypatch.setattr(extract, "registry", lambda: {"photo": _stub("never reached")})
    client = FakeClient(messages={CHAT_ID: [tl.photo_message(CHAT_ID, 1)]})
    assert (await media.run(conn, client, cfg, SyncBudget())).failed == 1
    assert calls == []
    assert _units(conn) == before


async def test_a_chat_whose_row_is_gone_extracts_without_recutting(
    conn: sqlite3.Connection, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The text is still stored; there is no chat left to cut units for.

    The foreign key takes a chat's messages with it, so this is the guard rather than a path a
    sync can walk — and it is the difference between a stopped pass and a ``None`` attribute
    error inside the worker thread if the two ever come apart.
    """
    cfg = _cfg()
    _synced(conn, [_message(CHAT_ID, 1, media_kind="photo")], cfg)
    monkeypatch.setattr(db, "get_chat", lambda *_: None)
    monkeypatch.setattr(extract, "registry", lambda: {"photo": _stub("read anyway")})
    client = FakeClient(
        messages={CHAT_ID: [tl.photo_message(CHAT_ID, 1)]},
        downloads={(CHAT_ID, 1): b"jpeg bytes"},
    )
    assert (await media.run(conn, client, cfg, SyncBudget())).extracted == 1
    assert _text(conn, 1) == "read anyway"
    assert "[photo] read anyway" not in " ".join(_units(conn).values())
