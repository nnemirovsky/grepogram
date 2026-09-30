"""Core data types shared by every grepogram module.

The ``*Cfg`` dataclasses mirror ``config.toml`` and carry its defaults; the ``*Row`` dataclasses
mirror the SQLite tables; the remaining types are the shapes returned by search, sync and the MCP
tools. Everything is an immutable, slotted dataclass so it serialises with ``dataclasses.asdict``.
"""

import re
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
LinkKind = Literal["link", "text_url", "mention", "button", "webpage"]
"""How a message names a Telegram destination (``message_links.kind``): a visible URL, a hidden
``text_url`` hyperlink, an ``@mention`` (or a mention by id), a URL button, a link preview."""

ResearchState = Literal["active", "stopped"]
"""A research session explores while ``active``; ``stopped`` is final and voids its grants."""
CandidateKind = Literal["username", "peer", "invite", "addlist"]
"""What a candidate's identity names: a public chat by ``@name``, a chat by its marked id, an
invite link's hash, a shared folder's slug — the chat-level forms of :mod:`grepogram.leads`."""
CandidateStatus = Literal[
    "proposed",
    "approved",
    "skipped",
    "excluded",
    "joined",
    "pending_admission",
    "fetched",
    "unavailable",
    "failed",
]
EvidenceVia = Literal[
    "link",
    "mention",
    "text_url",
    "button",
    "webpage",
    "pinned",
    "forward",
    "directory",
    "shared_folder",
    "chat_search",
    "post_search",
]
"""The path that led research to a candidate (``evidence.via``)."""
CandidateAction = Literal["fetch", "join", "request", "add_source"]
"""What a grant may allow for one candidate."""
SessionAction = Literal["global_search", "paid_search"]
"""What a grant may allow for a whole session rather than one candidate."""
GrantAction = CandidateAction | SessionAction
GrantChannel = Literal["elicitation", "cli"]
"""Where a human approved a grant: an MCP elicitation answered in the host, or the CLI reading
the controlling terminal. Nothing else can produce one — there is no third channel."""
SearchKind = Literal["chat_search", "post_search"]
"""A Telegram-side search research ran (``searches.kind``)."""


DEFAULT_ACCOUNT = "default"
"""The implicit account: always present, signed in by ``grepogram auth``, session in
``session.session``. A config without ``[[accounts]]`` names this one alone."""
ACCOUNT_NAME = re.compile(r"[a-z0-9_-]{1,32}")
"""What an account name may be: it becomes a file name (``sessions/<name>.session``) and a
prefix of source ids, so it is kept to a safe, lowercase alphabet."""


def is_account_name(name: str) -> bool:
    """Whether ``name`` is a valid account name (:data:`ACCOUNT_NAME`, matched in full)."""
    return ACCOUNT_NAME.fullmatch(name) is not None


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
    """How a search retrieves, fuses, reranks and orders its hits.

    ``reaction_weight`` is the most a unit can gain for the reactions it collected, on the
    normalised scale :func:`grepogram.search.search` puts the cross-encoder's scores on: the
    bonus is ``reaction_weight * log1p(reactions) / (1 + log1p(reactions))``, so it rises fast
    over the first few reactions and never reaches the weight itself. ``0`` switches it off and
    leaves the reranker's own scores exactly as they were.
    """

    k: int = 10
    rrf_k: int = 60
    rerank_top: int = 40
    dedup_overlap: float = 0.5
    reaction_weight: float = 0.05
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


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    """The bounds a research session works within, fixed when it starts (``sessions.limits``).

    ``max_depth`` is how many hops from a seed a candidate may be; ``max_candidates`` and
    ``probe_limit`` cap one discover call; ``since_days`` is the history horizon a source added
    by a run gets; ``max_messages_per_run`` and ``run_budget_s`` bound one run.
    """

    max_depth: int = 2
    max_candidates: int = 50
    probe_limit: int = 20
    since_days: int = 365
    max_messages_per_run: int = 5000
    run_budget_s: int = 300


@dataclass(frozen=True, slots=True)
class ResearchCfg:
    """``[research]``: whether research runs at all, what it may ask Telegram, and its limits.

    ``enabled`` is off by default and every research entry point refuses while it is. The two
    search switches let discovery reach Telegram's own chat search (``contacts.search``) and
    public-post search (``channels.searchPosts``), each still behind a grant; ``paid_stars_max``
    at 0 means post search never pays. The remaining keys are the defaults of
    :class:`ResearchLimits` a new session copies.
    """

    enabled: bool = False
    chat_search: bool = False
    post_search: bool = False
    paid_stars_max: int = 0
    max_depth: int = 2
    max_candidates: int = 50
    probe_limit: int = 20
    since_days: int = 365
    max_messages_per_run: int = 5000
    run_budget_s: int = 300

    def limits(self) -> ResearchLimits:
        """The limits a session started under this config gets unless it overrides them."""
        return ResearchLimits(
            max_depth=self.max_depth,
            max_candidates=self.max_candidates,
            probe_limit=self.probe_limit,
            since_days=self.since_days,
            max_messages_per_run=self.max_messages_per_run,
            run_budget_s=self.run_budget_s,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountCfg:
    """One ``[[accounts]]`` entry: a Telegram account signed in besides :data:`DEFAULT_ACCOUNT`.

    Every account shares ``[telegram]``'s API app; what sets one apart is its session file
    (:meth:`grepogram.paths.Paths.session_file_for`) and the sources that name it.
    """

    name: str
    label: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """One ``[[sources]]`` entry: a Telegram folder or a single chat, fetched by ``account``."""

    folder: str | None = None
    chat: str | int | None = None
    since: str | None = None
    comments: bool = False
    account: str = DEFAULT_ACCOUNT

    def __post_init__(self) -> None:
        has_folder = bool(self.folder)
        has_chat = self.chat is not None and self.chat != ""
        if has_folder == has_chat:
            raise ValueError("a source needs exactly one of 'folder' or 'chat'")

    @property
    def id(self) -> str:
        """Stable identifier stored in ``chats.source_id``.

        ``folder:<title>`` / ``chat:<value>`` for :data:`DEFAULT_ACCOUNT`, so every id stored
        before accounts existed still names its source; ``<account>/folder:<title>`` /
        ``<account>/chat:<value>`` for any other, so two accounts can each hold ``chat = 12345``
        — two different private chats.
        """
        target = f"folder:{self.folder}" if self.folder is not None else f"chat:{self.chat}"
        if self.account == DEFAULT_ACCOUNT:
            return target
        return f"{self.account}/{target}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    telegram: TelegramCfg = field(default_factory=TelegramCfg)
    models: ModelsCfg = field(default_factory=ModelsCfg)
    search: SearchCfg = field(default_factory=SearchCfg)
    units: UnitsCfg = field(default_factory=UnitsCfg)
    sync: SyncCfg = field(default_factory=SyncCfg)
    media: MediaCfg = field(default_factory=MediaCfg)
    research: ResearchCfg = field(default_factory=ResearchCfg)
    accounts: list[AccountCfg] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)

    def account_names(self) -> tuple[str, ...]:
        """Every account this config knows: :data:`DEFAULT_ACCOUNT` first, then ``accounts``."""
        return (DEFAULT_ACCOUNT, *(account.name for account in self.accounts))


SHARED_CHAT_TYPES: frozenset[ChatType] = frozenset({"channel", "supergroup"})
"""Chat types whose ids and message ids are global: every account that reaches such a chat sees
the same peer id and the same ``msg_id`` for each message, so the index holds one row for all of
them. Users, bots and legacy groups number their messages per account and get a row per account.
"""


def chat_scope(chat_type: ChatType, account: str) -> str:
    """The ``chats.scope`` a chat of ``chat_type`` reached through ``account`` is stored under.

    ``''`` for a channel or supergroup (:data:`SHARED_CHAT_TYPES`), one row whichever account
    reaches it; the account name for a user, bot or legacy group, whose history is that
    account's own. The one rule — the schema step, :func:`grepogram.db.upsert_chat` and every
    lookup derive the scope here.
    """
    return "" if chat_type in SHARED_CHAT_TYPES else account


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatRow:
    """One stored chat.

    ``id`` is the row's key, the one every other table and every internal reference uses;
    ``peer_id`` is Telegram's marked id, the one every call to Telegram and every link uses.
    They are equal for every channel and supergroup — a shared row's ``id`` always *is* its peer
    id, so ``discussion_of``, ``comment_of_chat_id``, ``migrated_to`` and ``t.me/c/`` links stay
    in channel space — and for most private chats; a scoped row (a user, bot or legacy group seen
    by one account) takes a synthetic id (:data:`grepogram.db.SYNTHETIC_BASE` and up) only when
    another account's row already holds that peer id.

    ``peer_id = 0`` (the default) means "the same as ``id``" and is resolved on construction, so
    a caller that knows only the Telegram id builds a row as it always did. ``scope`` is
    :func:`chat_scope`'s answer; left empty it resolves to :data:`DEFAULT_ACCOUNT`'s scope.
    """

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
    peer_id: int = 0
    scope: str = ""

    def __post_init__(self) -> None:
        if not self.peer_id:
            object.__setattr__(self, "peer_id", self.id)
        if not self.scope:
            object.__setattr__(self, "scope", chat_scope(self.type, DEFAULT_ACCOUNT))

    @property
    def is_shared(self) -> bool:
        """Whether this is a channel or supergroup row, one for every account that reaches it."""
        return self.type in SHARED_CHAT_TYPES

    @property
    def is_broadcast(self) -> bool:
        """A channel in its own right — not the discussion group of one.

        Broadcast channels are cut into ``post`` units instead of windows, and their post
        threads carry the comments of the linked group.
        """
        return self.type == "channel" and self.discussion_of is None


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountRow:
    """One ``accounts`` row: who an account turned out to be when it last signed in."""

    name: str
    user_id: int | None = None
    display_name: str | None = None
    added_at: int | None = None


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

    ``fwd_peer_id`` / ``fwd_msg_id`` / ``fwd_date`` are a forward's structured origin — the peer
    and message it was forwarded from and when that was sent (:func:`grepogram.sync.forward_origin`)
    — beside ``fwd_from``, the name shown for it. ``links`` is every Telegram destination the
    message names as ``(kind, target)`` pairs, sorted, targets normalized by
    :func:`grepogram.leads.normalize`; ``None`` means *not read*, not *none*: a row read back from
    the index does not carry them (:func:`grepogram.db.message_links` does), and
    :func:`grepogram.db.upsert_messages` replaces a message's stored links only with a tuple.
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
    fwd_peer_id: int | None = None
    fwd_msg_id: int | None = None
    fwd_date: int | None = None
    text: str = ""
    media_kind: MediaKind | None = None
    media_filename: str | None = None
    reactions_total: int = 0
    extracted_text: str | None = None
    media_state: int = 0
    links: tuple[tuple[LinkKind, str], ...] | None = None


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
    """One search result: a unit of ``chat`` and the message its link opens.

    ``peer_id`` is the chat's Telegram id (``chat.peer_id``), which differs from ``chat.id`` only
    for a private chat stored under a synthetic id; ``chat.id`` is what the readers take back.
    ``accounts`` are the signed-in accounts that reach the chat (:func:`grepogram.db.chat_reach`)
    — where the hit came from, empty for a Telegram Desktop import."""

    score: float
    chat: ChatRow
    peer_id: int
    kind: UnitKind
    date_start: int
    date_end: int
    anchor_msg_id: int
    url: str
    fallback_url: str | None = None
    snippet: str
    msg_ids: list[int]
    text: str | None = None
    accounts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageView:
    """One message as a reader sees it. ``chat_id`` is the chat the message is *in*, which is
    not always the chat that was asked about: a channel post's thread carries the comments of
    the linked discussion group, and their ``msg_id`` lives in that group's id space, where post
    ids and comment ids both number from 1 and collide by construction.

    ``chat_id`` is the stored row's id, the one to pass back to a reader; ``peer_id`` is that
    chat's Telegram id, and ``accounts`` the accounts that reach it, as on a
    :class:`Hit`."""

    chat_id: int
    peer_id: int
    msg_id: int
    date: int
    from_name: str | None
    text: str
    url: str
    fallback_url: str | None = None
    reply_to_msg_id: int | None = None
    accounts: list[str] = field(default_factory=list)


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
    a budget or a flood wait left in the queue for the next run, and only the next run's own
    work: media in a chat nothing may re-fetch is counted in ``unreachable`` instead.
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
    """Pending media a further run could still read — what "run extract again" is offered for."""
    unreachable: int = 0
    """Pending media in a chat no run may re-fetch: an imported or an unavailable one, or one no
    connected account reaches."""
    chats_unreachable: list[int] = field(default_factory=list)
    """The chats holding pending media that no connected account reaches, left alone."""
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class PruneReport:
    """What one deletion sweep did — :func:`grepogram.sync.prune_deleted`'s answer.

    ``checked`` counts the stored ids the sweep asked Telegram about, ``removed`` the messages
    that came back empty and were dropped. A chat is in ``chats_done`` once the sweep reached the
    end of its history and in ``chats_remaining`` when a budget, a flood wait or an error stopped
    it partway — its cursor stays where it got to, so the next run carries on from there. A
    chat no connected account reaches is in ``chats_unreachable`` and was not asked about.
    """

    removed: int = 0
    checked: int = 0
    chats_done: list[int] = field(default_factory=list)
    chats_remaining: list[int] = field(default_factory=list)
    chats_unreachable: list[int] = field(default_factory=list)
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
    accounts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceStatus:
    """One source and the chats it covers; ``account`` is the account the source belongs to,
    ``None`` for an ``import:`` that came from an export rather than through any account."""

    source_id: str
    account: str | None = DEFAULT_ACCOUNT
    chats: list[ChatStatus]


# --- research.db rows ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ResearchSession:
    """One ``sessions`` row of ``research.db``: a question explored from seed chats by one
    account. ``seeds`` are ``chats.id`` values of the index; ``progress`` is what the research
    loop records about its runs."""

    id: int
    question: str
    account: str
    seeds: tuple[int, ...]
    limits: ResearchLimits
    state: ResearchState
    created_at: int
    stopped_at: int | None = None
    progress: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """One ``candidates`` row: a chat research found and has not indexed, unique per session by
    ``identity`` (a :mod:`grepogram.leads` target string).

    Three facts stay apart: ``member`` is whether the acting account is in the chat (``None``
    until a probe says), whether the index already holds it is asked of ``index.db`` when needed
    and never stored, and whether a human authorised anything is a live row of ``grants``.
    ``access_hash`` is the acting account's. ``parent_id`` names the candidate it was found
    inside (a shared folder's peers), which grants nothing for it.
    """

    id: int
    session_id: int
    identity: str
    kind: CandidateKind
    depth: int
    status: CandidateStatus = "proposed"
    peer_id: int | None = None
    username: str | None = None
    invite_hash: str | None = None
    addlist_slug: str | None = None
    title: str | None = None
    type: ChatType | None = None
    participants: int | None = None
    member: bool | None = None
    access_hash: int | None = None
    request_needed: bool | None = None
    parent_id: int | None = None
    source_id: str | None = None
    probed_at: int | None = None
    created_at: int = 0
    note: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Evidence:
    """One path that led to a candidate. ``chat_id`` / ``msg_id`` name the message it was found
    in (index ids for an indexed chat, the peer and post for a search result); ``origin_key``
    is what corroboration counts — every forward of one post shares it."""

    id: int
    candidate_id: int
    via: EvidenceVia
    origin_key: str
    chat_id: int | None = None
    msg_id: int | None = None
    snippet: str | None = None
    found_at: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Grant:
    """One human approval: ``actions`` on one candidate (or the session, when ``candidate_id``
    is ``None``) by ``account``, given through ``via``. Live while neither consumed nor voided."""

    id: int
    session_id: int
    candidate_id: int | None
    account: str
    actions: tuple[GrantAction, ...]
    via: GrantChannel
    summary: str
    granted_at: int
    consumed_at: int | None = None
    voided_at: int | None = None

    @property
    def live(self) -> bool:
        return self.consumed_at is None and self.voided_at is None


@dataclass(frozen=True, slots=True, kw_only=True)
class Exclusion:
    """A target research never proposes again, in any session."""

    identity: str
    reason: str | None = None
    created_at: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRecord:
    """One Telegram-side search a session ran; its results are evidence, never messages."""

    id: int
    session_id: int
    kind: SearchKind
    query: str
    ran_at: int
    results: int = 0
    note: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanCursor:
    """How far a session's discovery has read one indexed chat: the Telegram ``msg_id`` of the
    newest message scanned and the depth the chat's leads are found at."""

    session_id: int
    chat_id: int
    depth: int
    msg_id: int
    scanned_at: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateView:
    """A candidate as research presents it: its evidence, how many independent origins that
    evidence has (``corroboration``), how many of the question's terms its snippets share
    (``overlap``), and whether the index already holds the chat — ``cached_chats`` are those
    rows, ``cached_accounts`` the accounts they came through. Membership stays
    ``candidate.member``; neither of the two says anything about the other."""

    candidate: Candidate
    corroboration: int = 0
    overlap: int = 0
    cached_chats: tuple[int, ...] = ()
    cached_accounts: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()

    @property
    def cached(self) -> bool:
        return bool(self.cached_chats)


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoverReport:
    """What one offline discovery pass did — :func:`grepogram.research.discover_offline`'s answer.

    ``leads`` counts the paths to a chat outside the session that were read; ``in_session`` those
    to a chat the session already reads, ``people`` those naming a person, and neither counts as
    a lead. ``text_fallback`` is how many messages, stored before links were captured, were read
    by their visible text alone — their hidden hyperlinks and buttons were never seen.
    ``beyond_depth``, ``excluded`` and ``over_cap`` count the new identities left out, and
    ``truncated`` says the cap held some back: their chats keep their cursor, so the next call
    reads them again.
    """

    session_id: int
    chats_scanned: int = 0
    messages_scanned: int = 0
    text_fallback: int = 0
    leads: int = 0
    in_session: int = 0
    people: int = 0
    new_candidates: list[int] = field(default_factory=list)
    updated_candidates: list[int] = field(default_factory=list)
    beyond_depth: int = 0
    excluded: int = 0
    over_cap: int = 0
    truncated: bool = False
