"""Reading a Telegram Desktop export: ids, text runs, media, and what a broken file costs.

``tests/fixtures/tdesktop_export.json`` is hand-written and committed — nothing here downloads
an export — and holds both generations of the format on purpose: the supergroup carries
``date_unixtime``, ``text_entities`` and ``from_id: "user…"``, the legacy group only ``date``, a
bare ``text`` and an integer ``from_id``. The malformed and truncated cases are built inline,
because a fixture that is broken on disk is a fixture nobody can read.
"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from grepogram import tdesktop
from grepogram.links import strip_channel_prefix
from grepogram.tdesktop import Export, ExportError, ImportedChat, read_export

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXPORT = FIXTURES / "tdesktop_export.json"

EXPATS = -1001234567890
"""The marked id of the fixture's ``public_supergroup``, whose export id is ``1234567890``."""
BOAT = -987654
"""The marked id of the fixture's ``private_group``, whose export id is ``987654``."""


@pytest.fixture
def export() -> Export:
    return read_export(EXPORT)


def _chat(export: Export, chat_id: int) -> ImportedChat:
    return next(entry for entry in export.chats if entry.chat.id == chat_id)


def _write(tmp_path: Path, data: Any, name: str = "result.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _account(chats: list[dict[str, Any]]) -> dict[str, Any]:
    return {"about": "your data", "chats": {"about": "chats", "list": chats}}


def _chat_entry(messages: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    entry = {"name": "Chat", "type": "public_channel", "id": 4242, "messages": messages}
    entry.update(over)
    return entry


def _message(**over: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": 1,
        "type": "message",
        "date": "2024-03-01T09:00:00",
        "date_unixtime": "1709283600",
        "from": "Nina",
        "from_id": "user777000",
        "text": "",
        "text_entities": [],
    }
    message.update(over)
    return message


def _one(tmp_path: Path, **over: Any) -> Any:
    """The single message of a one-chat export built from ``over``, parsed."""
    path = _write(tmp_path, _account([_chat_entry([_message(**over)])]))
    return read_export(path).chats[0].messages[0]


# --- the fixture -----------------------------------------------------------------------------


def test_export_holds_both_chats(export: Export) -> None:
    assert [entry.chat.id for entry in export.chats] == [EXPATS, BOAT]
    assert [entry.chat.type for entry in export.chats] == ["supergroup", "group"]
    assert [entry.chat.title for entry in export.chats] == ["Valencia Expats", "Двое в лодке"]
    assert export.messages == 6


def test_a_bare_channel_id_becomes_the_marked_form(export: Export) -> None:
    """The export writes ``1234567890``; the index works in ``-100…`` and must round-trip."""
    chat = _chat(export, EXPATS).chat
    assert chat.id == -1001234567890
    assert strip_channel_prefix(chat.id) == 1234567890
    assert all(row.chat_id == chat.id for row in _chat(export, EXPATS).messages)


def test_a_short_channel_id_keeps_its_leading_zeros() -> None:
    """The case string surgery on the ``-100`` prefix gets wrong: nine digits, not ten."""
    marked = tdesktop.marked_chat_id(123456789, "channel")
    assert marked == -1000123456789
    assert strip_channel_prefix(marked) == 123456789


def test_a_legacy_group_id_is_negated_not_prefixed(export: Export) -> None:
    assert _chat(export, BOAT).chat.id == -987654


def test_an_already_marked_id_passes_through() -> None:
    assert tdesktop.marked_chat_id(-1001234567890, "channel") == -1001234567890


def test_an_imported_chat_is_marked_unavailable(export: Export) -> None:
    """Nothing was fetched from Telegram, so no sync may resume from it."""
    for entry in export.chats:
        assert entry.chat.unavailable is True
        assert entry.chat.last_msg_id == 0
        assert entry.chat.source_id is None


def test_text_runs_flatten_to_the_plain_text(export: Export) -> None:
    row = _chat(export, EXPATS).messages[0]
    assert row.text == "Записаться на ВНЖ можно тут https://sede.example.es до пятницы"


def test_a_service_message_is_skipped_and_counted(export: Export) -> None:
    """``run.map`` skips a ``MessageService``; so does this, and it is not a warning."""
    assert [row.msg_id for row in _chat(export, EXPATS).messages] == [2, 3, 4]
    assert export.service == 1


def test_a_malformed_entry_is_reported_and_skipped(export: Export) -> None:
    assert export.skipped == 1
    assert any("no usable id" in warning for warning in export.warnings)
    assert export.messages == 6


def test_a_reply_survives(export: Export) -> None:
    row = next(row for row in _chat(export, EXPATS).messages if row.msg_id == 3)
    assert row.reply_to_msg_id == 2


def test_reactions_are_summed(export: Export) -> None:
    rows = {row.msg_id: row for row in _chat(export, EXPATS).messages}
    assert rows[2].reactions_total == 5
    assert rows[3].reactions_total == 0


def test_dates_and_edits(export: Export) -> None:
    rows = {row.msg_id: row for row in _chat(export, EXPATS).messages}
    assert rows[3].date == 1709284020
    assert rows[3].edit_date == 1709284140
    assert rows[2].edit_date is None


def test_an_old_export_without_unixtime_reads_its_iso_date_as_utc(export: Export) -> None:
    row = _chat(export, BOAT).messages[0]
    assert row.date == int(dt.datetime(2022, 6, 4, 18, 0, tzinfo=dt.UTC).timestamp())
    assert row.text == "старый экспорт без date_unixtime"


def test_senders_of_both_generations(export: Export) -> None:
    """``"user777000"`` in the newer export, a bare ``777000`` in the older one."""
    assert _chat(export, EXPATS).messages[0].from_id == 777000
    assert _chat(export, EXPATS).messages[0].from_name == "Nina"
    assert _chat(export, BOAT).messages[0].from_id == 777000


def test_a_forward_keeps_its_origin(export: Export) -> None:
    row = next(row for row in _chat(export, BOAT).messages if row.msg_id == 2)
    assert row.fwd_from == "Valencia Expats"


def test_media_kinds_from_the_fixture(export: Export) -> None:
    expats = {row.msg_id: row for row in _chat(export, EXPATS).messages}
    boat = {row.msg_id: row for row in _chat(export, BOAT).messages}
    assert (expats[3].media_kind, expats[3].media_filename) == ("photo", None)
    assert expats[3].text == "объявление у входа"
    assert (expats[4].media_kind, expats[4].media_filename) == ("document", "tasa_790.pdf")
    assert (boat[2].media_kind, boat[2].media_filename) == ("video", "clip.mp4")
    assert boat[3].media_kind == "poll"


def test_a_poll_contributes_its_question_and_answers(export: Export) -> None:
    row = next(row for row in _chat(export, BOAT).messages if row.msg_id == 3)
    assert row.text == "Идём в четверг?\nда\nнет"


def test_nothing_extracted_is_claimed_for_an_import(export: Export) -> None:
    """The extraction pass owns both columns; an import must leave its queue untouched."""
    for entry in export.chats:
        assert all(row.extracted_text is None and row.media_state == 0 for row in entry.messages)


# --- finding the file ------------------------------------------------------------------------


def test_a_directory_is_searched_for_result_json(tmp_path: Path) -> None:
    _write(tmp_path, _account([_chat_entry([_message()])]))
    assert read_export(tmp_path).chats[0].chat.id == -1000000004242


def test_a_directory_falls_back_to_messages_json(tmp_path: Path) -> None:
    """A single-chat export is the chat object itself, with no ``chats`` wrapper."""
    _write(tmp_path, _chat_entry([_message(text="привет")]), name="messages.json")
    export = read_export(tmp_path)
    assert [row.text for row in export.chats[0].messages] == ["привет"]


def test_result_json_wins_over_messages_json(tmp_path: Path) -> None:
    _write(tmp_path, _account([_chat_entry([_message(text="account")])]))
    _write(tmp_path, _chat_entry([_message(text="single")]), name="messages.json")
    assert read_export(tmp_path).chats[0].messages[0].text == "account"


def test_a_file_may_be_named_directly(tmp_path: Path) -> None:
    path = _write(tmp_path, _chat_entry([_message()]), name="whatever.json")
    assert read_export(path).chats[0].chat.title == "Chat"


def test_a_missing_path_is_an_export_error(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="no Telegram Desktop export"):
        read_export(tmp_path / "nope")


def test_a_directory_without_an_export_is_an_export_error(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="holds no result.json"):
        read_export(tmp_path)


def test_a_json_document_that_is_not_an_export_is_an_export_error(tmp_path: Path) -> None:
    path = _write(tmp_path, {"hello": "world"})
    with pytest.raises(ExportError, match="not a Telegram Desktop export"):
        read_export(path)


def test_a_json_array_is_an_export_error(tmp_path: Path) -> None:
    path = _write(tmp_path, [1, 2, 3])
    with pytest.raises(ExportError, match="does not hold a JSON object"):
        read_export(path)


def test_a_file_that_is_not_json_at_all_is_an_export_error(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text("not json, not even close", encoding="utf-8")
    with pytest.raises(ExportError, match="not readable JSON"):
        read_export(path)


def test_an_undecodable_file_is_an_export_error(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(ExportError, match="cannot read"):
        read_export(path)


# --- a partial export ------------------------------------------------------------------------


def test_a_truncated_export_keeps_everything_before_the_cut(tmp_path: Path) -> None:
    """Someone cancelled the export: the file ends mid-message and must still be read."""
    whole = EXPORT.read_text(encoding="utf-8")
    cut = whole.index('"объявление у входа"')
    path = tmp_path / "result.json"
    path.write_text(whole[:cut], encoding="utf-8")
    export = read_export(path)
    assert [row.msg_id for row in export.chats[0].messages] == [2]
    assert any("truncated" in warning for warning in export.warnings)
    assert export.skipped == 0


def test_a_truncation_inside_a_string_is_recovered_too(tmp_path: Path) -> None:
    whole = json.dumps(_account([_chat_entry([_message(id=1, text="kept"), _message(id=2)])]))
    path = tmp_path / "result.json"
    path.write_text(whole[: whole.index('"id": 2') + 6] + '"unterminat', encoding="utf-8")
    export = read_export(path)
    assert [row.msg_id for row in export.chats[0].messages] == [1]


def test_a_truncation_before_anything_whole_is_an_export_error(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text('{"about": "your data", "chats": {"list": [{"name": "Ch', encoding="utf-8")
    with pytest.raises(ExportError, match="not readable JSON"):
        read_export(path)


def test_an_export_whose_chats_hold_no_list_reports_it(tmp_path: Path) -> None:
    """A recovery that stopped inside ``chats`` leaves the wrapper without its list."""
    path = _write(tmp_path, {"about": "your data", "chats": {"about": "chats"}})
    export = read_export(path)
    assert export.chats == []
    assert any("no chat list" in warning for warning in export.warnings)
    assert export.skipped == 0


def test_a_recovered_prefix_with_mismatched_brackets_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text('{"chats": {"list": [}]}}', encoding="utf-8")
    with pytest.raises(ExportError, match="not readable JSON"):
        read_export(path)


def test_warnings_are_bounded(tmp_path: Path) -> None:
    """A file broken from end to end must not answer with a warning per entry."""
    broken = [_message(id=None) for _ in range(tdesktop.MAX_WARNINGS + 5)]
    path = _write(tmp_path, _account([_chat_entry(broken)]))
    export = read_export(path)
    assert export.skipped == tdesktop.MAX_WARNINGS + 5
    assert len(export.warnings) == tdesktop.MAX_WARNINGS + 1
    assert export.warnings[-1] == "and 5 more problems in this export"


# --- chats -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exported", "mapped"),
    [
        ("personal_chat", "user"),
        ("saved_messages", "user"),
        ("bot_chat", "bot"),
        ("private_group", "group"),
        ("private_supergroup", "supergroup"),
        ("public_supergroup", "supergroup"),
        ("private_channel", "channel"),
        ("public_channel", "channel"),
    ],
)
def test_every_export_chat_type_maps(tmp_path: Path, exported: str, mapped: str) -> None:
    path = _write(tmp_path, _account([_chat_entry([_message()], type=exported)]))
    assert read_export(path).chats[0].chat.type == mapped


def test_an_unknown_chat_type_is_skipped_rather_than_guessed(tmp_path: Path) -> None:
    """The mark applied to the id depends on the type, so a guess files it under another chat."""
    path = _write(tmp_path, _account([_chat_entry([_message()], type="future_kind")]))
    export = read_export(path)
    assert export.chats == []
    assert any("unknown export type" in warning for warning in export.warnings)
    assert export.skipped == 1


def test_a_chat_without_an_id_is_skipped(tmp_path: Path) -> None:
    path = _write(tmp_path, _account([_chat_entry([_message()], id=None)]))
    export = read_export(path)
    assert export.chats == []
    assert any("no usable id" in warning for warning in export.warnings)


def test_a_chat_entry_that_is_not_an_object_is_skipped(tmp_path: Path) -> None:
    path = _write(tmp_path, _account(["oops"]))  # type: ignore[list-item]
    export = read_export(path)
    assert export.chats == []
    assert export.warnings == ["a chat entry is not an object"]


def test_a_nameless_chat_keeps_a_row(tmp_path: Path) -> None:
    path = _write(tmp_path, _account([_chat_entry([_message()], name="  ")]))
    assert read_export(path).chats[0].chat.title is None


def test_a_chat_without_messages_is_still_a_chat(tmp_path: Path) -> None:
    entry = _chat_entry([])
    del entry["messages"]
    path = _write(tmp_path, _account([entry]))
    assert read_export(path).chats[0].messages == []


def test_a_message_list_that_is_not_a_list_is_reported(tmp_path: Path) -> None:
    entry = _chat_entry([])
    entry["messages"] = "soon"
    path = _write(tmp_path, _account([entry]))
    export = read_export(path)
    assert export.chats[0].messages == []
    assert any("not a list" in warning for warning in export.warnings)


# --- messages --------------------------------------------------------------------------------


def test_a_message_entry_that_is_not_an_object_is_skipped(tmp_path: Path) -> None:
    path = _write(tmp_path, _account([_chat_entry(["oops"])]))  # type: ignore[list-item]
    export = read_export(path)
    assert export.chats[0].messages == []
    assert export.skipped == 1


def test_a_message_of_an_unknown_type_is_skipped(tmp_path: Path) -> None:
    path = _write(tmp_path, _account([_chat_entry([_message(type="something_new")])]))
    export = read_export(path)
    assert export.chats[0].messages == []
    assert any("unknown type" in warning for warning in export.warnings)


def test_a_message_with_no_readable_date_is_skipped(tmp_path: Path) -> None:
    over = {"date": "not a date", "date_unixtime": None}
    path = _write(tmp_path, _account([_chat_entry([_message(**over)])]))
    export = read_export(path)
    assert export.chats[0].messages == []
    assert any("no readable date" in warning for warning in export.warnings)


def test_an_integer_unixtime_is_accepted(tmp_path: Path) -> None:
    assert _one(tmp_path, date_unixtime=1700000000).date == 1700000000


def test_a_bare_text_string_flattens(tmp_path: Path) -> None:
    message = _message(text="просто текст")
    del message["text_entities"]
    path = _write(tmp_path, _account([_chat_entry([message])]))
    assert read_export(path).chats[0].messages[0].text == "просто текст"


def test_text_entities_win_over_text(tmp_path: Path) -> None:
    """Both fields carry the same content in a newer export; the entity list is the fuller one."""
    over = {
        "text": ["see ", {"type": "link", "text": "https://x.example"}],
        "text_entities": [
            {"type": "plain", "text": "see "},
            {"type": "link", "text": "https://x.example"},
        ],
    }
    assert _one(tmp_path, **over).text == "see https://x.example"


def test_an_empty_entity_list_falls_back_to_text(tmp_path: Path) -> None:
    """A truncated entity list must not throw away the text beside it."""
    assert _one(tmp_path, text="кое-что", text_entities=[]).text == "кое-что"


def test_an_entity_without_text_contributes_nothing(tmp_path: Path) -> None:
    over = {"text_entities": [{"type": "plain", "text": "a"}, {"type": "spoiler"}, 7]}
    assert _one(tmp_path, **over).text == "a"


def test_a_reply_to_another_chat_is_not_an_in_chat_reply(tmp_path: Path) -> None:
    over = {"reply_to_message_id": 9, "reply_to_peer_id": "channel999"}
    assert _one(tmp_path, **over).reply_to_msg_id is None


def test_a_reply_naming_this_chat_survives(tmp_path: Path) -> None:
    over = {"reply_to_message_id": 9, "reply_to_peer_id": "channel4242"}
    assert _one(tmp_path, **over).reply_to_msg_id == 9


def test_a_hidden_forward_origin_reads_as_unknown(tmp_path: Path) -> None:
    assert _one(tmp_path, forwarded_from=None).fwd_from == "unknown"


def test_a_message_that_was_not_forwarded_has_no_origin(tmp_path: Path) -> None:
    assert _one(tmp_path).fwd_from is None


def test_an_anonymous_sender_has_no_name(tmp_path: Path) -> None:
    row = _one(tmp_path, **{"from": None, "from_id": "channel4242"})
    assert row.from_name is None
    assert row.from_id == -1000000004242


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("user777000", 777000),
        ("channel1234567890", -1001234567890),
        ("chat987654", -987654),
        (777000, 777000),
        (-1001234567890, -1001234567890),
        ("777000", 777000),
        ("peer777000", None),
        ("userNaN", None),
        (None, None),
        (True, None),
    ],
)
def test_peer_id_forms(raw: Any, expected: int | None) -> None:
    assert tdesktop.peer_id(raw) == expected


# --- media -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "kind", "filename"),
    [
        ({"photo": "photos/p.jpg"}, "photo", None),
        ({"file": "stickers/s.webp", "media_type": "sticker"}, "sticker", "s.webp"),
        ({"file": "voice/v.ogg", "media_type": "voice_message"}, "voice", "v.ogg"),
        ({"file": "round/r.mp4", "media_type": "video_message"}, "video_note", "r.mp4"),
        ({"file": "video/v.mp4", "media_type": "video_file"}, "video", "v.mp4"),
        ({"file": "gif/g.mp4", "media_type": "animation"}, "video", "g.mp4"),
        ({"file": "audio/a.mp3", "media_type": "audio_file"}, "audio", "a.mp3"),
        ({"file": "files/d.pdf", "file_name": "contract.pdf"}, "document", "contract.pdf"),
        ({"mime_type": "application/zip"}, "document", None),
        ({"poll": {"question": "q", "answers": []}}, "poll", None),
        ({"contact_information": {"first_name": "Ana"}}, "contact", None),
        ({"contact_vcard": "vcards/c.vcard"}, "contact", None),
        ({"location_information": {"latitude": 1.0}}, "location", None),
        ({"place_name": "Mercado"}, "location", None),
        ({}, None, None),
    ],
)
def test_media_of(over: dict[str, Any], kind: str | None, filename: str | None) -> None:
    assert tdesktop.media_of(_message(**over)) == (kind, filename)


def test_a_file_that_was_not_exported_yields_no_filename(tmp_path: Path) -> None:
    over = {"file": "(File not included. Change data exporting settings to download.)"}
    row = _one(tmp_path, **over)
    assert (row.media_kind, row.media_filename) == ("document", None)


def test_a_venue_contributes_its_name_and_address(tmp_path: Path) -> None:
    over = {"place_name": "Mercado Central", "address": "Plaça de la Ciutat"}
    assert _one(tmp_path, **over).text == "Mercado Central\nPlaça de la Ciutat"


def test_a_contact_contributes_its_name(tmp_path: Path) -> None:
    over = {"contact_information": {"first_name": "Ana", "last_name": "Ruiz"}}
    assert _one(tmp_path, **over).text == "Ana Ruiz"


def test_a_caption_wins_over_the_media_text(tmp_path: Path) -> None:
    caption = [{"type": "plain", "text": "тут"}]
    over = {"place_name": "Mercado", "text": "тут", "text_entities": caption}
    assert _one(tmp_path, **over).text == "тут"


@pytest.mark.parametrize(
    ("reactions", "total"),
    [
        ([{"type": "emoji", "count": 3}, {"type": "emoji", "count": 2}], 5),
        ([{"type": "emoji"}], 0),
        (["nope"], 0),
        ([], 0),
        (None, 0),
        ("many", 0),
    ],
)
def test_reactions_total(reactions: Any, total: int) -> None:
    assert tdesktop.reactions_total(reactions) == total


def test_a_truncation_after_an_escaped_quote_is_recovered(tmp_path: Path) -> None:
    """The bracket scan has to read JSON's escapes, or a quoted ``{`` opens a container."""
    quoted = _message(id=1, text='она сказала \\" { \\" и ушла')
    whole = json.dumps(_account([_chat_entry([quoted, _message(id=2)])]), ensure_ascii=False)
    path = tmp_path / "result.json"
    path.write_text(whole[: whole.index('"id": 2') + 7], encoding="utf-8")
    export = read_export(path)
    assert [row.msg_id for row in export.chats[0].messages] == [1]
    assert export.chats[0].messages[0].text == 'она сказала \\" { \\" и ушла'


def test_a_prefix_that_still_does_not_parse_is_refused(tmp_path: Path) -> None:
    """The cut point is a complete container, not a promise that what precedes it is valid."""
    path = tmp_path / "result.json"
    path.write_text('{"chats" [1]}', encoding="utf-8")
    with pytest.raises(ExportError, match="not readable JSON"):
        read_export(path)
