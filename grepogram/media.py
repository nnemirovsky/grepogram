"""The extraction pass: text out of the media stored messages carry.

``grepogram extract`` runs this. It is a **network** pass, not the offline analogue of
``grepogram embed``: Telethon downloads from a ``Message`` object, never from a stored row, so
every pending message is re-fetched by id (``client.get_messages(chat_id, ids=[…])``) before its
file is downloaded — which is also where the size comes from, there being no size column. It
runs after a sync rather than inside one because a 400-page PDF or a slow OCR must never eat a
sync's budget, and it is resumable by construction: ``messages.media_state`` is the whole of its
memory.

The offline half comes first and touches no network at all. Which kinds have an extractor here
and which are switched off in ``[media]`` follows from the stored ``media_kind`` alone, and which
attachments the document extractor could read follows from the stored ``media_filename``, so bulk
``UPDATE``s park them — and none of them flags ``indexed``. Most kinds have no extractor
(``video``, ``sticker``, ``audio``, ``webpage``, ``poll``, ``contact``, ``location``, ``other``,
plus ``voice`` and ``video_note`` until whisper lands in v0.3.0), so flagging would mark tens of
thousands of rows across every chat and hand the unbudgeted deferred ``index_pending`` loop
:func:`grepogram.sync._sync_chats` ends with a whole-index backlog — which the next 20-second
auto-sync inside a ``search`` would then rebuild and re-embed in full. That is why
:func:`grepogram.db.set_media_text` and :func:`grepogram.db.set_media_state` are two writers.

Then, over what is left, chat by chat and oldest first: re-fetch a batch, check the reported size
against ``[media] max_download_mb``, download into a scratch directory, extract, and commit the
whole batch at once so a flood wait keeps what the run earned. The temp file goes in a
``finally``, whatever happened.

A batch that read something ends by cutting the units holding those messages again
(:func:`grepogram.units.invalidate_units_for`, once per batch) and indexing the delta. That is
not optional bookkeeping: the ``indexed = 0`` flag ``set_media_text`` raises is cleared by the
next sync whether or not anything was rebuilt, and a sync never re-cuts a closed window — which
is where all but the newest handful of a chat's messages live — so without this step the text
this pass reads would be written to a column nothing ever renders.
"""

import functools
import logging
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from telethon import errors

from grepogram import db, extract, index, sync, units
from grepogram.extract import ExtractError, Extractor
from grepogram.models import ChatRow, Config, MediaKind, MediaReport, MessageRow

log = logging.getLogger(__name__)

BATCH = 50
"""How many messages one re-fetch and one commit cover.

Telegram takes at most 100 ids in a ``messages.getMessages``; half of that leaves room while
keeping the commit — and therefore what a flood wait costs — down to one batch of downloads.
"""

ALL_KINDS: tuple[MediaKind, ...] = get_args(MediaKind)
"""Every ``media_kind`` a stored row can carry, which is what the offline pass reasons over."""

_SUFFIX_MAX = 16


@dataclass(frozen=True, slots=True)
class _Outcome:
    """One message's result, held until its batch is committed."""

    row_id: int
    state: int
    text: str | None = None


def disabled_kinds(cfg: Config) -> set[MediaKind]:
    """The kinds ``[media]`` switches off — parked at ``MEDIA_DISABLED`` rather than re-read.

    ``ocr`` owns ``photo`` and ``documents`` owns ``document``; ``enabled`` is the master switch
    and never reaches here, because a pass that is off does nothing at all rather than parking
    every kind it knows.
    """
    off: set[MediaKind] = set()
    if not cfg.media.ocr:
        off.add("photo")
    if not cfg.media.documents:
        off.add("document")
    return off


def resolve_offline_states(
    conn: sqlite3.Connection,
    cfg: Config,
    extractors: dict[MediaKind, Extractor],
    *,
    retry_failed: bool = False,
) -> tuple[int, int, int]:
    """Park what the stored ``media_kind`` already decides; ``(unsupported, disabled, requeued)``.

    Four bulk updates, before a single Telegram request: a kind with no extractor here is
    ``MEDIA_UNSUPPORTED``, a kind switched off in the config is ``MEDIA_DISABLED``, and a kind
    that was switched back on comes off ``MEDIA_DISABLED`` into the queue so it drains once
    instead of being re-read on every pass. A kind that is both switched off and unreadable here
    is unsupported: no extractor is the stronger fact, and switching it back on then leaves it
    where it is until this build can read it.

    The fourth is inside the ``document`` kind rather than across kinds
    (:func:`grepogram.db.park_unreadable_documents`): ``document`` is ``sync.document_kind``'s
    fallback, so a spreadsheet, an archive and an installer all wear it, and only the stored
    filename says which of them :func:`grepogram.extract.extract_document` could ever read.
    Without this they were re-fetched, downloaded in full and *then* refused — the one case the
    offline half exists to prevent, and the most common document a chat posts.

    ``retry_failed`` additionally re-queues what an earlier run could not read — and what it
    parked as unsupported, since installing the ``media`` extra is exactly what makes those
    readable and there is no other way back out of that state.

    None of these updates touches ``indexed``: see :func:`grepogram.db.move_media_state`.
    """
    off = disabled_kinds(cfg)
    unsupported = [kind for kind in ALL_KINDS if kind not in extractors]
    disabled = [kind for kind in ALL_KINDS if kind in extractors and kind in off]
    live = [kind for kind in ALL_KINDS if kind in extractors and kind not in off]
    parked = db.move_media_state(conn, unsupported, frm=db.MEDIA_PENDING, to=db.MEDIA_UNSUPPORTED)
    switched = db.move_media_state(conn, disabled, frm=db.MEDIA_PENDING, to=db.MEDIA_DISABLED)
    requeued = db.move_media_state(conn, live, frm=db.MEDIA_DISABLED, to=db.MEDIA_PENDING)
    if retry_failed:
        requeued += db.move_media_state(conn, live, frm=db.MEDIA_FAILED, to=db.MEDIA_PENDING)
        requeued += db.move_media_state(conn, live, frm=db.MEDIA_UNSUPPORTED, to=db.MEDIA_PENDING)
    if "document" in live:
        parked += db.park_unreadable_documents(conn, extract.DOCUMENT_SUFFIXES)
    return parked, switched, requeued


async def run(
    conn: sqlite3.Connection,
    client: Any,
    cfg: Config,
    budget: sync.SyncBudget,
    *,
    retry_failed: bool = False,
) -> MediaReport:
    """Extract what the queue holds within ``budget``; the client must be connected.

    The caller holds the :class:`grepogram.sync.SyncLock` — this writes ``media_state``,
    ``extracted_text`` and ``indexed``, and ``db.Connection``'s lock only serialises threads
    within one process.

    A flood wait ends the run with what it earned; any other Telegram error costs one chat its
    turn and is reported. Every message the pass looks at leaves with a state written, so a run
    can never spin on a batch it cannot resolve. The client's ``flood_sleep_threshold`` is capped
    against the time left exactly as a sync caps it (:func:`grepogram.sync._cap_flood_sleep`), so
    a bounded run never sleeps through a wait longer than it has.
    """
    warnings: list[str] = []
    if not cfg.media.enabled:
        log.info("[media] enabled is false; the extraction pass did nothing")
        remaining, unreachable = _queue_left(conn)
        return MediaReport(
            remaining=remaining,
            unreachable=unreachable,
            warnings=["[media] enabled is false in the config; nothing was extracted"],
        )
    extractors = extract.registry()
    unsupported, disabled, requeued = resolve_offline_states(
        conn, cfg, extractors, retry_failed=retry_failed
    )
    tally = dict.fromkeys(
        (db.MEDIA_EXTRACTED, db.MEDIA_FAILED, db.MEDIA_SKIPPED, db.MEDIA_UNSUPPORTED), 0
    )
    with _scratch() as scratch:
        for chat_id in _fetchable_chats(conn):
            if budget.expired:
                break
            sync._cap_flood_sleep(client, cfg.sync, budget)
            try:
                await _extract_chat(conn, client, chat_id, extractors, cfg, scratch, budget, tally)
            except errors.FloodWaitError as exc:
                log.warning("flood wait of %ss on chat %s; stopping this run", exc.seconds, chat_id)
                warnings.append(
                    f"flood wait: Telegram asks to wait {exc.seconds}s before more media "
                    "requests; run `grepogram extract` again later"
                )
                break
            except (errors.RPCError, ValueError) as exc:
                log.warning("chat %s: %s; its media was skipped this run", chat_id, exc)
                warnings.append(f"chat {chat_id}: {exc}")
    remaining, unreachable = _queue_left(conn)
    return MediaReport(
        extracted=tally[db.MEDIA_EXTRACTED],
        failed=tally[db.MEDIA_FAILED],
        skipped=tally[db.MEDIA_SKIPPED],
        unsupported=unsupported + tally[db.MEDIA_UNSUPPORTED],
        disabled=disabled,
        requeued=requeued,
        remaining=remaining,
        unreachable=unreachable,
        warnings=warnings,
    )


def _queue_left(conn: sqlite3.Connection) -> tuple[int, int]:
    """What the queue still holds, split into what a next run could read and what none can.

    ``remaining`` is the pass's only completion signal — ``grepogram extract`` prints "run
    extract again" for it and a script may loop on it — so it counts the chats this pass would
    walk, not the whole index (:func:`_fetchable_chats`, :func:`grepogram.db.count_pending_media`).
    An imported chat's media carries a ``media_kind`` like any other and sits at
    ``MEDIA_PENDING`` for good, since nothing may ever re-fetch the message it hangs on: counted
    with the rest it would make every run after any import report work that can never be done.
    It is reported as ``unreachable`` instead, which says what it is and asks for nothing.
    """
    fetchable = db.count_pending_media(conn, _fetchable_chats(conn))
    return fetchable, db.count_pending_media(conn) - fetchable


def _fetchable_chats(conn: sqlite3.Connection) -> list[int]:
    """The chats holding pending media this pass can actually re-fetch, in id order.

    Every download starts from a ``Message`` Telegram just returned, so the queue is only worth
    walking for a chat Telegram will answer about at all — :func:`grepogram.sync.refetchable`'s
    rule, the same one the deletion sweep uses. An imported chat is the case that bites: its rows
    sit at ``MEDIA_PENDING`` for good and every run would ask for a peer the account cannot
    resolve, which Telethon answers with a plain ``ValueError`` — not an ``RPCError``, so it
    would leave ``grepogram extract`` as a traceback rather than a warning.
    """
    stored = {chat.id: chat for chat in db.list_chats(conn)}
    return [
        chat_id
        for chat_id in db.chats_with_pending_media(conn)
        if (chat := stored.get(chat_id)) is not None and sync.refetchable(chat)
    ]


@contextmanager
def _scratch() -> Iterator[Path]:
    """The directory downloads land in, removed with anything left in it when the pass ends.

    Its own function so a test can put it somewhere it can look at afterwards; the pass deletes
    each file in a ``finally`` of its own, and this is the net under that.
    """
    with tempfile.TemporaryDirectory(prefix="grepogram-media-") as directory:
        yield Path(directory)


async def _extract_chat(
    conn: sqlite3.Connection,
    client: Any,
    chat_id: int,
    extractors: dict[MediaKind, Extractor],
    cfg: Config,
    scratch: Path,
    budget: sync.SyncBudget,
    tally: dict[int, int],
) -> None:
    """One chat's queue, batch by batch, each batch committed on its own."""
    chat = db.get_chat(conn, chat_id)
    while not budget.expired:
        rows = db.messages_pending_media(conn, BATCH, chat_id)
        if not rows:
            return
        outcomes = await _extract_batch(client, chat_id, rows, extractors, cfg, scratch, budget)
        await sync._joined_to_thread(
            functools.partial(_store, conn, chat, cfg, outcomes), budget.cancel
        )
        for outcome in outcomes:
            tally[outcome.state] += 1
        if len(outcomes) < len(rows):
            return  # the budget ran out inside the batch; the rest stays queued for the next run


async def _extract_batch(
    client: Any,
    chat_id: int,
    rows: Sequence[MessageRow],
    extractors: dict[MediaKind, Extractor],
    cfg: Config,
    scratch: Path,
    budget: sync.SyncBudget,
) -> list[_Outcome]:
    """Re-fetch one batch and read every file it is still worth reading.

    The re-fetch is the whole reason this pass needs a client: a download needs the ``Message``
    Telegram just returned, and so does the size — no column carries it. The answer is matched
    back by id rather than by position, so a short or reordered reply cannot shift a row's
    outcome onto its neighbour.
    """
    fetched = await client.get_messages(chat_id, ids=[row.msg_id for row in rows])
    by_id = {int(msg.id): msg for msg in fetched or () if msg is not None}
    cap = cfg.media.max_download_mb * 1024 * 1024
    outcomes: list[_Outcome] = []
    for row in rows:
        if budget.expired:
            break
        outcomes.append(
            await _extract_one(client, row, by_id.get(row.msg_id), extractors, cap, scratch, budget)
        )
    return outcomes


async def _extract_one(
    client: Any,
    row: MessageRow,
    msg: Any,
    extractors: dict[MediaKind, Extractor],
    cap: int,
    scratch: Path,
    budget: sync.SyncBudget,
) -> _Outcome:
    """One message: size, download, extract — and the temp file gone whatever happened."""
    row_id = int(row.id or 0)
    if msg is None:
        log.debug(
            "message %s/%s is no longer there; its media is left for a retry",
            row.chat_id,
            row.msg_id,
        )
        return _Outcome(row_id, db.MEDIA_FAILED)
    extractor = extractors.get(row.media_kind) if row.media_kind else None
    if extractor is None:
        return _Outcome(row_id, db.MEDIA_UNSUPPORTED)
    size = media_size(getattr(msg, "media", None))
    if size is not None and size > cap:
        log.debug(
            "message %s/%s: %d bytes is over the %d-byte cap; not downloaded",
            row.chat_id,
            row.msg_id,
            size,
            cap,
        )
        return _Outcome(row_id, db.MEDIA_SKIPPED)
    target = scratch / _temp_name(row)
    written: Path | None = None
    try:
        answer = await client.download_media(msg, file=str(target))
        if answer is None:
            log.debug("message %s/%s: nothing downloaded", row.chat_id, row.msg_id)
            return _Outcome(row_id, db.MEDIA_FAILED)
        written = Path(answer)
        text = await sync._joined_to_thread(
            functools.partial(_read, extractor, written), budget.cancel
        )
    except (ExtractError, OSError) as exc:
        log.debug("message %s/%s: %s", row.chat_id, row.msg_id, exc)
        return _Outcome(row_id, db.MEDIA_FAILED)
    finally:
        _remove(target)
        _remove(written)
    return _Outcome(row_id, db.MEDIA_EXTRACTED, text)


def _read(extractor: Extractor, path: Path) -> str:
    """The blocking part, on a worker thread: parsing a PDF or running Vision over a photo."""
    return extractor(path)


def _remove(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # a directory, a vanished mount — never worth failing the pass
        log.debug("could not remove the temporary file %s: %s", path, exc)


def _store(
    conn: sqlite3.Connection,
    chat: ChatRow | None,
    cfg: Config,
    outcomes: Sequence[_Outcome],
) -> None:
    """Write one batch's outcomes in a single transaction, through both writers, and re-cut.

    Extracted text goes through :func:`grepogram.db.set_media_text`, which flags the row for a
    rebuild because its rendered line changed; every other state goes through
    :func:`grepogram.db.set_media_state`, which leaves ``indexed`` exactly as it found it. The
    flag lives only until :func:`_recut`, at the end of this same transaction, has done the
    rebuild it asks for.
    """
    parked: dict[int, list[int]] = {}
    extracted: list[int] = []
    with db.transaction(conn):
        for outcome in outcomes:
            if outcome.state == db.MEDIA_EXTRACTED:
                db.set_media_text(conn, outcome.row_id, outcome.text or "")
                extracted.append(outcome.row_id)
            else:
                parked.setdefault(outcome.state, []).append(outcome.row_id)
        for state, ids in parked.items():
            db.set_media_state(conn, ids, state)
        _recut(conn, chat, cfg, extracted)


def _recut(
    conn: sqlite3.Connection, chat: ChatRow | None, cfg: Config, row_ids: Sequence[int]
) -> None:
    """Re-cut the units holding this batch's extracted messages and index what changed.

    The step that makes the whole feature do anything at all. ``db.set_media_text`` flags the row
    ``indexed = 0``, but a flag on its own is thrown away: a sync's rebuild never re-cuts a closed
    window (:func:`grepogram.units._recut_start` returns ``None``) and ``on_chat_synced`` clears
    the flag regardless, so the text would be dropped for all but the newest handful of messages
    in every chat. :func:`grepogram.units.invalidate_units_for` reaches a closed window, and the
    :data:`grepogram.units.RECIPE_VERSION` bump is no substitute: that one-time re-cut runs on the
    first sync after the upgrade, long before this pass has worked through the backlog.

    Once per batch, never per message: ``db.windows_from`` replaces every window from the earliest
    touched one to the end of the chat, so fifty photos in one chat would otherwise re-cut and
    re-embed that tail fifty times over. The indexing is here rather than in ``units`` because
    :mod:`grepogram.index` imports :class:`~grepogram.units.UnitDelta` from there.

    An extracted **comment** is followed to the channel it was left under
    (:func:`grepogram.sync._invalidate_comment_posts`), the same way a deleted one is. A post
    thread quotes its comments' rendered lines while listing the post alone in ``msg_ids``, so no
    ``json_each`` over ``units.msg_ids`` reaches a comment id and the discussion group's own
    invalidation cannot touch that thread; ``comment_of_chat_id`` / ``comment_of_msg_id`` on the
    comment's row is the only route to it. Without this the group's window carried the text a
    photo was read for and the channel's thread kept the bare ``[photo]`` for good — no flag is
    left behind that would repair it.

    The message rows are re-indexed and un-flagged in this same transaction. ``msg_fts`` holds
    the extracted text too (:func:`grepogram.index.message_index_text`) — it is what anchors a
    hit on the right message — so the rebuild ``db.set_media_text``'s ``indexed = 0`` asks for
    happens here, where it is bounded by the batch, and the flag is cleared with it. Leaving it
    raised would hand the next sync's *unbudgeted* rebuild loops (``_sync_chats``' per-chat and
    deferred ``index_pending``) every extracted row in every chat at once — a 20-second auto-sync
    inside an MCP ``search`` included. What stays flagged is exactly what this cannot cover, and
    :func:`grepogram.sync.index_stranded` reaches all of it later: a chat that is gone by the
    time the batch is stored, and a row in a ``(chat, topic)`` that holds no window at all
    (:func:`grepogram.units.uncut_rows`). The second is the state a first sync interrupted
    between storing its rows and cutting their units leaves behind — the one
    ``messages.indexed = 0`` exists to repair — and an invalidation reaches none of it, so
    clearing the flag there would strand those rows in no unit for good. Both branches leave the
    flag raised for the same reason and it is bounded either way: a chat, not the index.
    """
    if chat is None or not row_ids:
        return
    rows = db.get_messages_by_ids(conn, row_ids)
    stranded = {msg.id for msg in units.uncut_rows(conn, chat, rows)}
    index.index_units(conn, units.invalidate_units_for(conn, chat, cfg, rows))
    sync._invalidate_comment_posts(conn, cfg, rows)
    index.index_messages(conn, row_ids)
    db.mark_indexed(conn, [row_id for row_id in row_ids if row_id not in stranded])


def media_size(media: Any) -> int | None:
    """Bytes Telegram reports for this media, ``None`` when it reports none.

    Read off the raw TL attributes of the re-fetched message, never ``Message.file``: the
    client-bound helpers are off limits here as they are in ``sync.map_message``, and an unknown
    size means "download it and see" rather than "skip it".
    """
    document = getattr(media, "document", None)
    if document is not None:
        size = getattr(document, "size", None)
        return int(size) if size else None
    photo = getattr(media, "photo", None)
    if photo is not None:
        return _photo_size(photo)
    return None


def _photo_size(photo: Any) -> int | None:
    """The largest of a photo's stored sizes, in the several shapes Telegram uses for them.

    ``PhotoSize`` carries ``size``, ``PhotoSizeProgressive`` a list of them, and the stripped and
    cached forms carry the bytes themselves; ``PhotoSizeEmpty`` carries nothing.
    """
    largest = 0
    for size in getattr(photo, "sizes", None) or ():
        progressive = getattr(size, "sizes", None)
        if progressive:
            largest = max(largest, max(int(one) for one in progressive))
            continue
        one_size = getattr(size, "size", None)
        if one_size is not None:
            largest = max(largest, int(one_size))
            continue
        raw = getattr(size, "bytes", None)
        if raw is not None:
            largest = max(largest, len(raw))
    return largest or None


def _temp_name(row: MessageRow) -> str:
    """A scratch file name unique in the pass and carrying the extension the dispatcher reads.

    Only the extension of the stored filename survives — a document's format is what
    :func:`grepogram.extract.extract_document` chooses on, and the name itself is attacker-shaped
    text that has no business becoming a path.
    """
    suffix = Path(Path(row.media_filename or "").name).suffix.lower()
    kept = "".join(char for char in suffix if char.isalnum() or char == ".")[:_SUFFIX_MAX]
    return f"{row.chat_id}_{row.msg_id}{kept}"
