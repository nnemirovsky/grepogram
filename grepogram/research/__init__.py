"""Research: find chats the index does not hold yet, starting from a question and seed chats.

This module is the one both the CLI and the MCP server drive; what it decides is kept in
``research.db`` (:mod:`grepogram.research_db`), what it reads is ``index.db``. Every entry point
refuses while ``[research] enabled`` is false (:func:`require_enabled`).

The package is split along those passes, each module importing only the ones listed before it:
:mod:`~grepogram.research.collect` (leads, what the index caches, ranking),
:mod:`~grepogram.research.sessions` (errors, sessions, what a human reads),
:mod:`~grepogram.research.grants` (authorization), :mod:`~grepogram.research.offline` (offline
discovery), :mod:`~grepogram.research.pins`, :mod:`~grepogram.research.probing`,
:mod:`~grepogram.research.searching` (global search), :mod:`~grepogram.research.discovery` (the
whole discover call), :mod:`~grepogram.research.approval` (summaries, grants, the approval
grammar), :mod:`~grepogram.research.joining`, :mod:`~grepogram.research.running` (the run) and
:mod:`~grepogram.research.documents` (what the CLI and the MCP server answer with). This module
re-exports what the CLI, the MCP server and the tests call.

**Offline discovery** (:func:`discover_offline`) talks to no one. It reads the messages the
index already holds of a session's seed chats — and of any chat a later run fetched for it, at
the depth that chat was found at, a channel's discussion group always beside its channel — and
turns every Telegram destination they name into a *candidate* one hop further out:

- the links a message was stored with (``message_links``: visible URLs, hidden ``text_url``
  hyperlinks, ``@mentions``, URL buttons, link previews);
- a forward's structured origin (``messages.fwd_peer_id``);
- for a row whose links were never read (``messages.links_read``: stored before link capture,
  or by an import whose export spelled no entities), whatever
  :func:`grepogram.leads.text_leads` finds in its text — visible URLs and mentions only, which
  the report counts as ``text_fallback`` so a caller can say that hidden links were not seen
  (``grepogram recapture-links`` reads such rows again, :func:`grepogram.sync.recapture_links`).

Where it has read to is a cursor on the index's lead clock (:func:`grepogram.db.lead_clock`),
not a message id: a discussion group stores comments out of ``msg_id`` order, and an edit or a
recapture gives an old row links it did not have — both are rows whose leads changed since the
cursor. Every chat is named in ``research.db`` as Telegram names it, ``(scope, peer_id)``
(:class:`~grepogram.models.ChatKey`), and found in the index as it is now, so a rebuilt index
or a private chat stored again under another row id is still the same conversation; a cursor
taken on another index (:func:`grepogram.db.index_id`) reads the chat from the start.

A chat whose messages name at least :data:`DIRECTORY_MIN_CHATS` distinct chats is a
**directory**: every lead found in it also carries a ``directory`` path, which shares the
lead's origin key and so adds no corroboration. Approving a directory approves nothing it lists.

**Pinned posts** (:func:`read_pins`) of the chats a session reads — its seeds, the user's own
indexed sources, and the chats runs fetched under a grant — are read once each, whatever their
age: a directory often keeps its index in a post pinned long before any ``since``. Their leads
are evidence (``via = pinned``) in ``research.db`` only; the posts are never stored as messages
and no sync cursor moves.

A candidate is a chat, not a post: ``@name/123`` and ``t.me/c/<id>/<post>`` lead to ``@name``
and ``peer:<marked id>``, the post staying in the evidence. A user id (a mention by id, a
forward from a person) names a person rather than a chat to index and is not a candidate; a
chat the session already reads (a seed, or one it fetched) is not one either.

Three facts about a candidate are never conflated: whether the acting account is a **member** (a
probe's answer, ``candidates.member``), whether the chat is **cached** — already in ``index.db``,
and through which accounts (:func:`~grepogram.research.collect.cached_in`, asked of the index every
time and never stored, since the index can be rebuilt under ``research.db``) — and whether a human
**authorized** anything for it (a live grant).

**Corroboration** counts distinct *origin keys*, not messages: every copy of one post — the
post itself where it is indexed and each forward of it, wherever it landed — shares the key
``post:<peer>/<msg>``, so a post forwarded into ten chats is one piece of evidence rather than
ten. Candidates rank by corroboration, then by how many of the question's terms their evidence
snippets share, then by depth. ``max_candidates`` caps how many *new* candidates one call adds;
a chat whose leads that cap cut keeps its scan cursor, so the next call reads them again.
``max_session_candidates`` caps the whole session; what it cuts no later call proposes either.

**Probing** (:func:`probe`, :func:`probe_candidates`) asks Telegram what a candidate *is* —
title, type, size, whether the acting account is a member, whether joining needs an admission
request — and never reads its history: pinned posts and messages need a grant. At most
``probe_limit`` candidates per call, best ranked first; a flood wait stops the pass with a
warning and leaves the rest for the next call. A shared folder's chats become candidates of
their own (``via = shared_folder``, ``parent_id`` the folder), and approving the folder grants
nothing for them.

**Global search** (:func:`search_telegram`, from :func:`discover`) runs only while
``[research]`` switches it on *and* the session holds a live ``global_search`` grant. Its
results are candidates and evidence in ``research.db`` — never ``messages`` rows, so no sync
cursor moves. A post search pays only when ``paid_stars_max`` allows the price *and* a separate
``paid_search`` grant exists, which the paid search then consumes.

**A run** (:func:`run`) carries out what a human approved and nothing else: each join,
admission request, shared-folder join, source added and fetch is preceded by :func:`authorized`
for that very candidate and action, the chats it fetches are synced through the ordinary
:func:`grepogram.sync.sync_all` narrowed with ``only``, and the chats it stored into are read by
discovery one hop deeper, which only ever proposes. A shared folder the account already imported
takes its missing chats through ``chatlists.joinChatlistUpdates`` (core.telegram.org, "Shared
folders": ``missing_peers`` of ``chatlistInviteAlready`` are passed to that method), one not
imported yet through ``chatlists.joinChatlistInvite``, each naming exactly the approved peers.

What this relies on of Telegram's API (core.telegram.org, reverified 2026-09-30, and the TL
classes of Telethon 1.44 / layer 227 for the exact fields):

- ``messages.checkChatInvite(hash)`` answers ``chatInviteAlready`` (``chat``: the account is a
  member), ``chatInvitePeek`` (``chat`` and ``expires``: previewable without joining), or
  ``chatInvite`` (``title``, ``participants_count``, flags ``channel`` / ``broadcast`` /
  ``megagroup`` / ``public`` / ``request_needed``, and no peer at all); errors
  ``INVITE_HASH_EXPIRED``, ``INVITE_HASH_INVALID``, ``INVITE_HASH_EMPTY``, ``CHANNEL_PRIVATE``.
- ``chatlists.checkChatlistInvite(slug)`` answers ``chatlists.chatlistInvite`` (``title`` as
  ``TextWithEntities``, ``peers``, ``chats``, ``users``) or, once the folder is imported,
  ``chatlists.chatlistInviteAlready`` (``filter_id``, ``missing_peers`` not joined yet,
  ``already_peers``); a dead slug is an RPC error Telethon has no class for (``INVITE_SLUG_*``),
  hence the plain ``RPCError`` catch and the match on its message
  (:func:`grepogram.research.joining._folder_refused`).
- ``messages.search`` with ``inputMessagesFilterPinned`` (what ``iter_messages(filter=…)``
  sends) answers a chat's pinned messages, newest first, whatever their date.
- ``messageFwdHeader``: ``from_id`` + ``channel_post`` address a channel post; ``from_name``
  without ``from_id`` is an account hiding itself, which names no peer; ``saved_from_peer`` /
  ``saved_from_msg_id`` are set only for Saved Messages. :func:`grepogram.sync.forward_origin`
  stores exactly that, and the origin chat Telegram hands along with the message (in the
  answer's ``chats``; ``min`` when the account may only see it, whose access hash addresses
  nothing) leaves its username and this account's access hash in ``peer_cache``
  (:func:`grepogram.sync.forward_peers`). A forward origin a probe cannot address even so — no
  access hash for this account and no username, the usual case for a private channel — is
  recorded ``unresolvable``, never guessed.
- ``channels.checkSearchPostsFlood(query)`` → ``searchPostsFlood``: ``total_daily``,
  ``remains``, ``wait_till``, ``query_is_free``, ``stars_amount``; the page on search says to
  ask it before ``channels.searchPosts`` (``query``, ``offset_rate``, ``offset_peer``,
  ``offset_id``, ``limit``, ``allow_paid_stars``), which searches every public channel and
  answers ``messages.Messages`` — a post search is free while ``remains`` or ``query_is_free``
  says so and costs ``stars_amount`` otherwise, paid only through ``allow_paid_stars``.
- ``contacts.search(q, limit)`` → ``contacts.found`` with ``my_results`` and ``results`` as
  peers and the ``chats`` / ``users`` behind them; the account's own contacts are excluded.
- ``contacts.resolveUsername`` (through ``client.get_entity``) answers a ``Channel`` whose
  ``left`` flag says the account is not a member; ``channels.getChannels`` needs the account's
  own access hash, so a peer id alone reaches only what the index stored a hash for.

Message text never reaches the log above DEBUG; counts do.
"""

from grepogram.research.approval import (
    APPROVAL_GRAMMAR,
    DESCENDANTS_NOTE,
    Approval,
    approval_args,
    approval_summary,
    approve_command,
    confirm_token,
    exclude,
    grant,
    parse_approval,
    prepare_approval,
    skip,
    stop,
    target_identities,
    unexclude,
    with_default_actions,
)
from grepogram.research.collect import (
    SNIPPET_CHARS,
    candidate_views,
    chat_key,
    snippet,
)
from grepogram.research.discovery import (
    discover,
)
from grepogram.research.documents import (
    candidates_document,
    grant_documents,
    report_document,
    session_document,
    status_document,
)
from grepogram.research.grants import (
    authorized,
    authorized_actions,
    granted_kinds,
    search_granted,
    search_kinds,
)
from grepogram.research.joining import (
    PENDING_NOTE,
)
from grepogram.research.offline import (
    DIRECTORY_MIN_CHATS,
    discover_offline,
)
from grepogram.research.pins import (
    read_pins,
)
from grepogram.research.probing import (
    UNRESOLVABLE_NOTE,
    probe,
    probe_candidates,
)
from grepogram.research.running import (
    PARTIAL_NOTE,
    run,
)
from grepogram.research.searching import (
    search_telegram,
)
from grepogram.research.sessions import (
    ENABLE_HINT,
    QUESTION_MAX_CHARS,
    ResearchDisabled,
    ResearchError,
    SessionStopped,
    UnknownCandidate,
    UnknownSession,
    active_session,
    horizon,
    require_enabled,
    start_session,
)

__all__ = [
    "APPROVAL_GRAMMAR",
    "Approval",
    "DESCENDANTS_NOTE",
    "DIRECTORY_MIN_CHATS",
    "ENABLE_HINT",
    "PARTIAL_NOTE",
    "PENDING_NOTE",
    "QUESTION_MAX_CHARS",
    "ResearchDisabled",
    "ResearchError",
    "SNIPPET_CHARS",
    "SessionStopped",
    "UNRESOLVABLE_NOTE",
    "UnknownCandidate",
    "UnknownSession",
    "active_session",
    "approval_args",
    "approval_summary",
    "approve_command",
    "authorized",
    "authorized_actions",
    "candidate_views",
    "candidates_document",
    "chat_key",
    "confirm_token",
    "discover",
    "discover_offline",
    "exclude",
    "grant",
    "grant_documents",
    "granted_kinds",
    "horizon",
    "parse_approval",
    "prepare_approval",
    "probe",
    "probe_candidates",
    "read_pins",
    "report_document",
    "require_enabled",
    "run",
    "search_granted",
    "search_kinds",
    "search_telegram",
    "session_document",
    "skip",
    "snippet",
    "start_session",
    "status_document",
    "stop",
    "target_identities",
    "unexclude",
    "with_default_actions",
]
