"""Reading a Telegram Desktop export into the rows the index stores.

Telegram Desktop's *Export chat history* writes JSON: ``result.json`` for a whole account, one
``messages.json`` per chat when a single chat is exported. :func:`read_export` turns either into
:class:`ImportedChat` values — a :class:`~grepogram.models.ChatRow` and the
:class:`~grepogram.models.MessageRow` list under it — which ``grepogram import`` stores through
``db.upsert_messages`` so units, FTS and vectors follow the ordinary path. Nothing here opens a
database or touches Telegram: this module only reads a file.

Two things the format does differently from Telethon, and both are why this is a module rather
than a dict comprehension:

*Ids are bare.* An export writes a channel as ``1234567890``, while every id in this index is
Telethon's marked form — ``-(1000000000000 + id)`` for a channel or supergroup, ``-id`` for a
legacy group. :func:`marked_chat_id` applies the mark through :func:`telethon.utils.get_peer_id`,
which is the same arithmetic convention :func:`grepogram.links.strip_channel_prefix` undoes; a
lexical rule over the ``-100`` prefix loses the leading zeros of a short id, which is a bug this
project has already paid for once.

*Text is a list of runs.* ``text`` mixes bare strings with entity objects
(``{"type": "link", "text": "…"}``), and newer exports carry the same content again as
``text_entities``, where even the plain runs are objects. :func:`flatten_text` prefers
``text_entities`` and falls back to ``text``, so both export generations flatten to the same
plain string.

A half-written export is the normal case — someone cancels the export, or the disk fills — so
nothing here raises part-way through a file. A truncated JSON document is recovered up to its
last complete object (:func:`_recover`), an entry that cannot be read is counted and described in
:attr:`Export.warnings`, and a service message is skipped the way
:func:`grepogram.sync.map_message` skips one. :class:`ExportError` is raised only when there is no
export to read at all.
"""

import datetime as dt
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeGuard

from telethon import utils
from telethon.tl import types

from grepogram.models import ChatRow, ChatType, MediaKind, MessageRow
from grepogram.units import UNKNOWN_SENDER

log = logging.getLogger(__name__)

EXPORT_FILES = ("result.json", "messages.json")
"""What an export directory is searched for, in order: an account export, then a single chat."""

MAX_WARNINGS = 20
"""Problems described one by one before :class:`Export` starts counting them instead.

An export truncated at the front is broken from end to end, and a warning per entry would be a
million-line answer to a one-line question."""

CHAT_TYPES: dict[str, ChatType] = {
    "personal_chat": "user",
    "saved_messages": "user",
    "bot_chat": "bot",
    "private_group": "group",
    "private_supergroup": "supergroup",
    "public_supergroup": "supergroup",
    "private_channel": "channel",
    "public_channel": "channel",
}
"""The export's own chat types mapped onto :data:`~grepogram.models.ChatType`.

A type outside this map is skipped rather than guessed at: the mark applied to a chat's id
depends on which of these it is, so a wrong guess files a whole history under an id that names
another chat entirely."""

_MEDIA_TYPES: dict[str, MediaKind] = {
    "sticker": "sticker",
    "animation": "video",
    "video_file": "video",
    "video_message": "video_note",
    "voice_message": "voice",
    "audio_file": "audio",
}
"""``media_type`` values that name a kind; a file without one is a plain ``document``.

``animation`` is a GIF, which :func:`grepogram.sync.document_kind` also calls ``video``."""

_PEER_PREFIXES: tuple[tuple[str, Any], ...] = (
    ("user", types.PeerUser),
    ("channel", types.PeerChannel),
    ("chat", types.PeerChat),
)
"""How an export spells a sender: ``user123``, ``channel123``, ``chat123``."""


class ExportError(Exception):
    """There is no Telegram Desktop export here to read.

    Raised before parsing starts — a missing directory, no ``result.json`` in it, a file holding
    something else entirely, a truncation so early that nothing survives it. Anything wrong
    *inside* a readable export is a warning on :class:`Export`, never this.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportedChat:
    """One chat an export holds: the row to store and the messages under it.

    ``chat`` carries what the export states — the marked id, the type, the title — plus
    ``unavailable = True``, which is unconditionally true of an imported chat: nothing was
    fetched from Telegram and ``last_msg_id`` stays 0, so no sync resumes from it. ``source_id``
    is left unset because it is the importing command's to choose (``import:<slug>``).
    """

    chat: ChatRow
    messages: list[MessageRow]


@dataclass(frozen=True, slots=True, kw_only=True)
class Export:
    """What one export file parsed into, and what could not be read out of it.

    ``service`` counts the service messages (joins, pins, title changes) that were skipped by
    design; ``skipped`` counts the entries that could not be read at all. ``warnings`` describes
    the first :data:`MAX_WARNINGS` of the latter, plus the truncation itself when the file was
    cut short.
    """

    path: Path
    chats: list[ImportedChat]
    service: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def messages(self) -> int:
        """How many messages were read, over every chat in the export."""
        return sum(len(entry.messages) for entry in self.chats)


class _Notes:
    """Warnings and counters collected while an export is read, bounded by :data:`MAX_WARNINGS`."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.service = 0
        self.skipped = 0
        self.suppressed = 0

    def note(self, message: str) -> None:
        if len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(message)
        else:
            self.suppressed += 1

    def drop(self, message: str) -> None:
        """Count an entry that could not be read, and describe it while there is room."""
        self.skipped += 1
        self.note(message)

    def collect(self) -> list[str]:
        if self.suppressed == 0:
            return list(self.warnings)
        return [*self.warnings, f"and {self.suppressed} more problems in this export"]


# --- reading ---------------------------------------------------------------------------------


def read_export(path: Path) -> Export:
    """Parse the export at ``path`` — a directory holding one, or the JSON file itself."""
    source = find_export(path)
    notes = _Notes()
    data = _load(source, notes)
    chats: list[ImportedChat] = []
    for entry in _chat_entries(data, source, notes):
        parsed = _parse_chat(entry, notes)
        if parsed is not None:
            chats.append(parsed)
    export = Export(
        path=source,
        chats=chats,
        service=notes.service,
        skipped=notes.skipped,
        warnings=notes.collect(),
    )
    log.info(
        "read %s: %d chats, %d messages, %d service messages and %d entries skipped",
        source,
        len(export.chats),
        export.messages,
        export.service,
        export.skipped,
    )
    return export


def find_export(path: Path) -> Path:
    """The JSON file to read: ``path`` when it names one, else the export file inside it."""
    if path.is_file():
        return path
    if path.is_dir():
        for name in EXPORT_FILES:
            candidate = path / name
            if candidate.is_file():
                return candidate
        raise ExportError(f"{path} holds no {' or '.join(EXPORT_FILES)}")
    raise ExportError(f"no Telegram Desktop export at {path}")


def _load(path: Path, notes: _Notes) -> Any:
    """The export's JSON, recovered up to its last complete object when the file was cut short.

    **The whole file is held in memory, twice over.** ``read_text`` decodes it into a ``str``,
    ``json.loads`` builds the object graph beside it, and the recovery scan below walks that same
    string character by character — so a whole-account export runs at roughly three times the
    file's size in RAM plus the parsed graph. That is deliberate: ``import`` is a one-off,
    offline, interactive command, and a streaming parser would buy nothing for the exports people
    actually have while making the truncation recovery impossible. An export large enough to be a
    problem is one to split per chat (Telegram Desktop exports one chat at a time as well);
    README's Known Limitations says so.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        recovered = _recover(text)
        if recovered is None:
            raise ExportError(f"{path} is not readable JSON: {exc}") from exc
        notes.note(
            f"{path.name} is truncated ({exc.msg}, line {exc.lineno}); "
            "everything before the cut was read"
        )
        return recovered


def _recover(text: str) -> Any | None:
    """The longest prefix of a truncated export that parses, its open containers closed.

    A cancelled export leaves a JSON document with no end: the containers it opened are never
    closed and the last entry is half-written. Scanning once for the last ``}`` or ``]`` that
    still leaves something open gives the cut point — everything before it is complete — and the
    open containers are closed in reverse to make a document out of it. ``None`` when the file
    holds nothing whole enough to recover, which :func:`_load` turns into :class:`ExportError`.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    cut = -1
    cut_stack: list[str] = []
    for position, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or stack[-1] != char:
                return None
            stack.pop()
            if stack:
                cut, cut_stack = position, list(stack)
    if cut < 0:
        return None
    try:
        return json.loads(text[: cut + 1] + "".join(reversed(cut_stack)))
    except json.JSONDecodeError:
        return None


def _chat_entries(data: Any, path: Path, notes: _Notes) -> list[Any]:
    """The chat objects in either export shape: ``chats.list`` of an account, or the file itself."""
    if not isinstance(data, dict):
        raise ExportError(f"{path} does not hold a JSON object")
    chats = data.get("chats")
    if isinstance(chats, dict):
        listed = chats.get("list")
        if isinstance(listed, list):
            return listed
        notes.note(f"{path.name} holds no chat list; the export was cut short before it")
        return []
    if isinstance(data.get("messages"), list) or isinstance(data.get("type"), str):
        return [data]
    raise ExportError(
        f"{path} is not a Telegram Desktop export: it names neither chats nor messages"
    )


# --- chats -----------------------------------------------------------------------------------


def _parse_chat(entry: Any, notes: _Notes) -> ImportedChat | None:
    if not isinstance(entry, dict):
        notes.drop("a chat entry is not an object")
        return None
    name = entry.get("name")
    title = str(name).strip() if isinstance(name, str) and name.strip() else None
    chat_type = CHAT_TYPES.get(str(entry.get("type")))
    if chat_type is None:
        notes.drop(f"chat {title or '<untitled>'}: unknown export type {entry.get('type')!r}")
        return None
    raw_id = entry.get("id")
    if not _is_int(raw_id):
        notes.drop(f"chat {title or '<untitled>'}: no usable id ({raw_id!r})")
        return None
    chat = ChatRow(
        id=marked_chat_id(int(raw_id), chat_type),
        type=chat_type,
        title=title,
        unavailable=True,
    )
    listed = entry.get("messages")
    if listed is not None and not isinstance(listed, list):
        notes.drop(f"chat {title or chat.id}: its message list is not a list")
        listed = None
    messages = [
        row for row in (_parse_message(raw, chat, notes) for raw in listed or ()) if row is not None
    ]
    return ImportedChat(chat=chat, messages=messages)


def marked_chat_id(raw: int, chat_type: ChatType) -> int:
    """An export's bare chat id in the marked form the rest of grepogram uses.

    The mark is arithmetic — ``-(1000000000000 + id)`` for a channel or supergroup, ``-id`` for a
    legacy group, a user's id unchanged — so :func:`telethon.utils.get_peer_id` applies it, the
    same convention :func:`grepogram.links.strip_channel_prefix` undoes. String surgery on the
    ``-100`` prefix cannot: a channel id shorter than ten digits leaves zeros right behind the
    prefix and any lexical rule either swallows them or refuses the id.

    A negative id is already marked and passes through, so an export generation that writes the
    marked form is read the same way as one that writes bare ids.
    """
    if raw < 0:
        return raw
    if chat_type in ("channel", "supergroup"):
        return int(utils.get_peer_id(types.PeerChannel(raw)))
    if chat_type == "group":
        return int(utils.get_peer_id(types.PeerChat(raw)))
    return int(utils.get_peer_id(types.PeerUser(raw)))


# --- messages --------------------------------------------------------------------------------


def _parse_message(raw: Any, chat: ChatRow, notes: _Notes) -> MessageRow | None:
    """One export message as a :class:`MessageRow`; ``None`` for one that is not stored.

    Service messages (``"type": "service"`` — joins, pins, topic edits) are skipped silently,
    exactly as :func:`grepogram.sync.map_message` skips a ``MessageService``. Anything else that
    cannot be read is counted and described.
    """
    if not isinstance(raw, dict):
        notes.drop(f"chat {chat.id}: a message entry is not an object")
        return None
    kind = raw.get("type")
    if kind == "service":
        notes.service += 1
        return None
    if kind != "message":
        notes.drop(f"chat {chat.id}: message entry of unknown type {kind!r}")
        return None
    msg_id = raw.get("id")
    if not _is_int(msg_id):
        notes.drop(f"chat {chat.id}: a message has no usable id ({msg_id!r})")
        return None
    date = _epoch(raw.get("date_unixtime"), raw.get("date"))
    if date is None:
        notes.drop(f"chat {chat.id}: message {msg_id} has no readable date")
        return None
    media_kind, media_filename = media_of(raw)
    return MessageRow(
        chat_id=chat.id,
        msg_id=int(msg_id),
        date=date,
        edit_date=_epoch(raw.get("edited_unixtime"), raw.get("edited")),
        from_id=peer_id(raw.get("from_id")),
        from_name=_display_name(raw.get("from")),
        reply_to_msg_id=_reply_to(raw, chat),
        fwd_from=_forwarded_from(raw),
        text=flatten_text(raw) or media_text(raw),
        media_kind=media_kind,
        media_filename=media_filename,
        reactions_total=reactions_total(raw.get("reactions")),
    )


def flatten_text(message: Mapping[str, Any]) -> str:
    """The plain text of a message, out of whichever run list the export carries.

    ``text`` is the older field and mixes bare strings with entity objects
    (``["see ", {"type": "link", "text": "https://…"}]``); ``text_entities`` is the newer one and
    spells every run, plain ones included, as an object. ``text_entities`` wins where it yields
    anything and ``text`` covers the rest, so an old export, a new one and a truncated entity
    list all flatten to the same concatenated string.
    """
    return _flatten(message.get("text_entities")) or _flatten(message.get("text"))


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten(part) for part in value)
    if isinstance(value, Mapping):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return ""


def media_of(message: Mapping[str, Any]) -> tuple[MediaKind | None, str | None]:
    """``(media_kind, media_filename)`` for what a message attached, ``(None, None)`` for none.

    The export names media by the field it lands in — ``photo``, ``poll``,
    ``contact_information``, ``location_information`` — and everything else by ``media_type``
    beside a ``file``, which is where a plain document falls through to.
    """
    if "photo" in message:
        return "photo", None
    if "poll" in message:
        return "poll", None
    if "contact_information" in message or "contact_vcard" in message:
        return "contact", None
    if "location_information" in message or "place_name" in message:
        return "location", None
    if any(key in message for key in ("file", "media_type", "mime_type")):
        return _MEDIA_TYPES.get(str(message.get("media_type")), "document"), _filename(message)
    return None, None


def _filename(message: Mapping[str, Any]) -> str | None:
    """``file_name`` where the export writes one, else the name of the file it wrote out.

    ``file`` is a path relative to the export directory, or a parenthesised note when the files
    themselves were not exported — a note is no filename, so only a path contributes one.
    """
    name = message.get("file_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    path = message.get("file")
    if isinstance(path, str) and "/" in path:
        return Path(path).name or None
    return None


def media_text(message: Mapping[str, Any]) -> str:
    """Text a poll, venue or contact carries in place of a message body; ``""`` otherwise.

    The same rule :func:`grepogram.sync.media_text` applies to a live message, so an imported
    poll is searchable by its question exactly as a synced one is.
    """
    poll = message.get("poll")
    if isinstance(poll, Mapping):
        parts = [str(poll.get("question") or "")]
        answers = poll.get("answers")
        if isinstance(answers, list):
            parts += [str(a.get("text") or "") for a in answers if isinstance(a, Mapping)]
        return "\n".join(part for part in parts if part)
    place = [str(message.get(key)) for key in ("place_name", "address") if message.get(key)]
    if place:
        return "\n".join(place)
    contact = message.get("contact_information")
    if isinstance(contact, Mapping):
        names = [str(contact.get(key)) for key in ("first_name", "last_name") if contact.get(key)]
        return " ".join(names)
    return ""


def peer_id(raw: Any) -> int | None:
    """The marked id of an export's peer reference, ``None`` when it names nobody readable.

    Newer exports spell a sender ``user123``, ``channel123`` or ``chat123``; older ones wrote the
    bare user id as a number. Either way the answer is marked through
    :func:`telethon.utils.get_peer_id`, so it matches the ids ``messages.from_id`` already holds.
    """
    if _is_int(raw):
        return int(raw) if int(raw) < 0 else int(utils.get_peer_id(types.PeerUser(int(raw))))
    if not isinstance(raw, str):
        return None
    for prefix, peer in _PEER_PREFIXES:
        if raw.startswith(prefix):
            digits = raw[len(prefix) :]
            return int(utils.get_peer_id(peer(int(digits)))) if digits.isdigit() else None
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


def reactions_total(reactions: Any) -> int:
    """How many reactions a message collected, over every emoji on it."""
    if not isinstance(reactions, list):
        return 0
    total = 0
    for entry in reactions:
        count = entry.get("count") if isinstance(entry, Mapping) else None
        if _is_int(count):
            total += int(count)
    return total


def _reply_to(message: Mapping[str, Any], chat: ChatRow) -> int | None:
    """The message this one replies to, inside this chat only.

    A reply quoting a message from somewhere else carries ``reply_to_peer_id``; that is not an
    in-chat reply and no thread is built from it, the same call
    :func:`grepogram.sync.reply_of` makes.
    """
    parent = message.get("reply_to_message_id")
    if not _is_int(parent):
        return None
    other = message.get("reply_to_peer_id")
    if other is not None and peer_id(other) != chat.id:
        return None
    return int(parent)


def _forwarded_from(message: Mapping[str, Any]) -> str | None:
    """Who a forwarded message came from; ``None`` when it was not forwarded.

    The key is present on every forward, holding ``null`` for an origin that hides itself, which
    is the ``unknown`` :func:`grepogram.sync.forward_of` records for the same case.
    """
    if "forwarded_from" not in message:
        return None
    return _display_name(message.get("forwarded_from")) or UNKNOWN_SENDER


def _display_name(raw: Any) -> str | None:
    """A name the export wrote, stripped; ``None`` for a missing, empty or non-string one."""
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _epoch(unixtime: Any, iso: Any) -> int | None:
    """Unix seconds from an export's date pair, ``None`` when neither can be read.

    ``date_unixtime`` is what newer exports write and is unambiguous. The ``date`` beside it is
    the exporting machine's *local* wall clock with no offset on it, so an old export that
    carries only that is read as UTC — the only reading available, and off by the exporter's
    offset at worst.

    An integer this build cannot turn back into a date is not a date (:func:`_renderable`).
    """
    if isinstance(unixtime, str) and unixtime.lstrip("-").isdigit():
        return _renderable(int(unixtime))
    if _is_int(unixtime):
        return _renderable(int(unixtime))
    if not isinstance(iso, str) or not iso:
        return None
    try:
        when = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return _renderable(int(when.timestamp()))


def _renderable(seconds: int) -> int | None:
    """``seconds`` if a stored row carrying it can be rendered, ``None`` when it cannot.

    ``date_unixtime`` is whatever the file says and this module's contract is that an unreadable
    field costs the entry, not the import — but the range of a plain integer is not the range of
    a date, and nothing else here checks it. Stored, such a value reaches
    ``datetime.fromtimestamp`` in :func:`grepogram.units.render_line`, which raises
    ``ValueError: year … is out of range`` — after the rows are committed, in the pass that cuts
    their units. From then on every sync reaches the chat again through
    :func:`grepogram.sync.index_stranded` and raises the same error, the MCP ``sync`` tool
    answers ``error``, and a ``search`` old enough to auto-sync answers ``error`` instead of
    hits: one unreadable field bricking the whole index. So the check is the render itself.
    """
    try:
        dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return seconds


def _is_int(value: Any) -> TypeGuard[int]:
    """A JSON number that is an integer — ``True``/``False`` are ints in Python and are not.

    A :class:`~typing.TypeGuard` rather than a bare predicate so the callers that go on to do
    arithmetic with the value read as narrowed rather than as ``Any``.
    """
    return isinstance(value, int) and not isinstance(value, bool)
