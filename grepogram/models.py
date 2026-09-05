"""Core data types shared by every grepogram module.

The ``*Cfg`` dataclasses mirror ``config.toml`` and carry its defaults; the ``*Row`` dataclasses
mirror the SQLite tables; the remaining types are the shapes returned by search, sync and the MCP
tools. Everything is an immutable, slotted dataclass so it serialises with ``dataclasses.asdict``.
"""

from dataclasses import dataclass, field
from typing import Literal

ChatType = Literal["user", "bot", "group", "supergroup", "channel"]
UnitKind = Literal["window", "thread", "post"]
SearchMode = Literal["hybrid", "lexical", "dense"]
"""How a search retrieves: fused, BM25 only, embeddings only (:func:`grepogram.search.search`)."""
MediaKind = Literal[
    "photo",
    "video",
    "voice",
    "video_note",
    "document",
    "sticker",
    "audio",
    "poll",
    "contact",
    "location",
    "webpage",
    "other",
]


@dataclass(frozen=True, slots=True)
class TelegramCfg:
    api_id: int = 0
    api_hash: str = ""


@dataclass(frozen=True, slots=True)
class ModelsCfg:
    embed: str = "BAAI/bge-m3"
    rerank: str = "BAAI/bge-reranker-v2-m3"
    device: str = "auto"
    max_seq_length: int = 512
    """Token cap both models truncate at: a longer unit is embedded and reranked only up to it.
    Changing it re-embeds nothing by itself — ``grepogram embed --reembed`` does."""


@dataclass(frozen=True, slots=True)
class SearchCfg:
    k: int = 10
    rrf_k: int = 60
    rerank_top: int = 40
    dedup_overlap: float = 0.5
    vec_fanout_max: int = 8
    auto_sync_after_min: int = 60
    auto_sync_budget_s: int = 20


@dataclass(frozen=True, slots=True)
class UnitsCfg:
    window_gap_min: int = 30
    window_max_msgs: int = 30
    window_max_chars: int = 1500
    thread_max_msgs: int = 40


@dataclass(frozen=True, slots=True)
class SyncCfg:
    edit_refetch: int = 200
    flood_sleep_threshold: int = 120


@dataclass(frozen=True, slots=True)
class MediaCfg:
    """What the extraction pass reads out of media, and how much of it it will download.

    ``enabled`` switches the whole pass off; ``ocr`` and ``documents`` switch one kind of
    extractor off, which parks that media at ``db.MEDIA_DISABLED`` instead of re-reading it on
    every pass. ``max_download_mb`` is checked against the size Telegram reports before anything
    is fetched, so an oversized file costs no traffic at all.
    """

    enabled: bool = True
    ocr: bool = True
    documents: bool = True
    max_download_mb: int = 20


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """One ``[[sources]]`` entry: a Telegram folder or a single chat."""

    folder: str | None = None
    chat: str | int | None = None
    since: str | None = None
    comments: bool = False

    def __post_init__(self) -> None:
        has_folder = bool(self.folder)
        has_chat = self.chat is not None and self.chat != ""
        if has_folder == has_chat:
            raise ValueError("a source needs exactly one of 'folder' or 'chat'")

    @property
    def id(self) -> str:
        """Stable identifier stored in ``chats.source_id``."""
        if self.folder is not None:
            return f"folder:{self.folder}"
        return f"chat:{self.chat}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    telegram: TelegramCfg = field(default_factory=TelegramCfg)
    models: ModelsCfg = field(default_factory=ModelsCfg)
    search: SearchCfg = field(default_factory=SearchCfg)
    units: UnitsCfg = field(default_factory=UnitsCfg)
    sync: SyncCfg = field(default_factory=SyncCfg)
    media: MediaCfg = field(default_factory=MediaCfg)
    sources: list[Source] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatRow:
    id: int
    type: ChatType
    title: str | None = None
    username: str | None = None
    is_forum: bool = False
    source_id: str | None = None
    discussion_of: int | None = None
    last_msg_id: int = 0
    last_sync_at: int | None = None
    unavailable: bool = False
    migrated_to: int | None = None

    @property
    def is_broadcast(self) -> bool:
        """A channel in its own right — not the discussion group of one.

        Broadcast channels are cut into ``post`` units instead of windows, and their post
        threads carry the comments of the linked group.
        """
        return self.type == "channel" and self.discussion_of is None


@dataclass(frozen=True, slots=True, kw_only=True)
class UserRow:
    id: int
    display_name: str | None = None
    username: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageRow:
    """One stored message.

    ``topic_id`` means one thing only: the forum topic the message sits in, ``None`` everywhere
    else. ``comment_of_chat_id`` / ``comment_of_msg_id`` are the other, separate relation — the
    channel and the post this message is a comment on, set only on the comments a channel's
    discussion group holds and ``None`` on every other row, a forum topic message included. The
    two are independent because their ids are: a forum topic root and a channel post both start
    at 1 and a discussion group can be a forum, so one column could never carry both.

    ``extracted_text`` is what an extractor read out of the attached media — OCR of a photo, the
    text of a PDF or a DOCX — and ``media_state`` how far the extraction pass got with this row
    (:data:`grepogram.db.MEDIA_PENDING` and the states beside it). Both are written by that pass
    alone: :func:`grepogram.db.upsert_messages` never touches them, so a re-store of a message
    Telegram re-read does not throw away what was extracted from its media.
    """

    id: int | None = None
    chat_id: int
    msg_id: int
    date: int
    edit_date: int | None = None
    from_id: int | None = None
    from_name: str | None = None
    reply_to_msg_id: int | None = None
    topic_id: int | None = None
    comment_of_chat_id: int | None = None
    comment_of_msg_id: int | None = None
    fwd_from: str | None = None
    text: str = ""
    media_kind: MediaKind | None = None
    media_filename: str | None = None
    reactions_total: int = 0
    extracted_text: str | None = None
    media_state: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class UnitRow:
    id: int | None = None
    chat_id: int
    topic_id: int | None = None
    kind: UnitKind
    msg_id_start: int
    msg_id_end: int
    msg_ids: list[int]
    date_start: int
    date_end: int
    text: str
    reactions: int = 0
    """Reactions on the messages this unit holds, summed when it is cut."""
    dirty: bool = True
    embedded_model: str | None = None


@dataclass(frozen=True, slots=True)
class Filters:
    chat_ids: set[int] | None = None
    since: int | None = None
    until: int | None = None


@dataclass(frozen=True, slots=True)
class Link:
    """Where a message lives: ``url`` is the form to show and cite (``https://t.me/…`` where
    Telegram has one, a ``tg://`` form where it has none) and ``fallback_url`` a second link for
    the chats whose ``url`` only mobile honours."""

    url: str
    fallback_url: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Hit:
    score: float
    chat: ChatRow
    kind: UnitKind
    date_start: int
    date_end: int
    anchor_msg_id: int
    url: str
    fallback_url: str | None = None
    snippet: str
    msg_ids: list[int]
    text: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageView:
    """One message as a reader sees it. ``chat_id`` is the chat the message is *in*, which is
    not always the chat that was asked about: a channel post's thread carries the comments of
    the linked discussion group, and their ``msg_id`` lives in that group's id space, where post
    ids and comment ids both number from 1 and collide by construction."""

    chat_id: int
    msg_id: int
    date: int
    from_name: str | None
    text: str
    url: str
    fallback_url: str | None = None
    reply_to_msg_id: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResult:
    hits: list[Hit]
    warnings: list[str] = field(default_factory=list)
    index_age_min: int | None = None
    synced: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncReport:
    new: int = 0
    chats_done: list[int] = field(default_factory=list)
    chats_remaining: list[int] = field(default_factory=list)
    unavailable: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaReport:
    """What one run of the extraction pass did — :func:`grepogram.media.run`'s answer.

    The offline counters (``unsupported``, ``disabled``, ``requeued``) come from the bulk
    updates that park media on what the stored ``media_kind`` alone says, before a single
    Telegram request; the rest are messages the pass actually re-fetched. ``remaining`` is what
    a budget or a flood wait left in the queue for the next run.
    """

    extracted: int = 0
    """Media that was read; ``extracted_text`` may still be empty — a photo holding no text."""
    failed: int = 0
    skipped: int = 0
    """Larger than ``[media] max_download_mb``, so never downloaded."""
    unsupported: int = 0
    """Parked because this build has no extractor for the kind, offline or in the loop."""
    disabled: int = 0
    """Parked offline: the kind is switched off in ``[media]``."""
    requeued: int = 0
    """Put back in the queue: a kind switched back on, or ``--retry-failed``."""
    remaining: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatStatus:
    id: int
    title: str | None
    type: ChatType
    username: str | None = None
    message_count: int
    last_sync_at: int | None
    unavailable: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceStatus:
    source_id: str
    chats: list[ChatStatus]
