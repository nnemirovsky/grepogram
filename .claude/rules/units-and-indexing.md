---
description: Units, the indexed flag, the recipe version, media extraction, reactions and worker-thread joins.
paths:
  - "grepogram/units.py"
  - "grepogram/index.py"
  - "grepogram/media.py"
  - "grepogram/extract.py"
  - "grepogram/search.py"
  - "grepogram/sync.py"
  - "grepogram/db.py"
  - "grepogram/mcp.py"
  - "grepogram/research/discovery.py"
  - "grepogram/research/running.py"
  - "grepogram/research/offline.py"
  - "tests/test_units*.py"
  - "tests/test_index*.py"
  - "tests/test_media.py"
  - "tests/test_extract.py"
  - "tests/test_search*.py"
---

# Units, indexing and extraction

- `messages.indexed` is 0 for a row whose units and `msg_fts` entry are behind: every
  `upsert_messages` sets it, `db.mark_unindexed` raises it for a post whose thread grew, and
  `sync.on_chat_synced` clears it after the rebuild. `sync._sync_chats` runs `index_pending` for
  a chat after its fetch whether it returned or raised, and for the chats the run never reached,
  so a batch committed before a flood wait or a crash is never left without units. It then ends
  with `sync.index_stranded`, a sweep of at most `sync.STRANDED_CHATS` chats that still hold
  flagged rows while no source and no link leads to them — a discussion group a channel was
  unlinked from is reachable through `db.chats_with_unindexed` and through nothing else. Never make
  `on_chat_synced` depend on what a run remembers in memory; the flag is the source of truth.
  The rebuild, the indexing and `mark_indexed` are one transaction, so the flag is a two-phase
  marker rather than three commits with gaps in between; keep it that way. A rerun over the same
  rows only repairs because `index.index_chat` ends with `index.repair_unit_index`, which compares
  `units` with `unit_fts` in both directions — a rebuild that re-cuts identical units reports an
  empty delta and would index nothing.
- `units.RECIPE_VERSION` names how this build cuts and renders a unit, and it is the whole of the
  contract between a stored index and the code reading it. Bump it in the same commit as any
  change to what a unit's text or boundaries are — v0.2.0 bumped three times (the window cap
  becoming a ceiling, extracted text reaching `render_line`, reactions landing on `UnitRow`) and
  an upgrader pays for the highest number once. `db.migrate` stamps `meta['unit_recipe']` on the
  branch that builds a schema from empty, because by the time any sync-time pass could ask, a
  brand-new index has already cut units for every chat it fetched — "fresh" is *the database
  holds no units*, never "no recipe recorded", which is exactly what a v0.1.1 index also looks
  like. `sync.recut_pending_chats` is the only thing that acts on a mismatch, and three
  properties of it are load-bearing. **Units are never dropped globally**: a re-cut takes at most
  `RECUT_CHATS_PER_RUN` whole chats and each one's delete-all, `units.recut_chat`,
  `index.index_units`, `repair_unit_index` and its `meta['unit_recut:<chat_id>']` marker are one
  transaction, so no chat is ever left flagged outside the transaction that rebuilds it and no
  later run — least of all a 20-second auto-sync — inherits a whole-index backlog for
  `_sync_chats`'s unbudgeted deferred `index_pending` loop to drain. **Who may start one is the
  caller, not the budget**: `sync_all` takes `recut: bool = True`, and exactly two callers pass
  `False` — `mcp._auto_sync`, the refresh inside a `search`, and a research run
  (`research.running.run`), whose fetch of the chats it adds is not the place for a whole-index
  re-cut either. A budget floor was tried and
  removed: `search.auto_sync_budget_s` is user-editable, so a floor made a search's own refresh
  start whole-index re-cutting the moment the number was raised, while an explicit sync whose
  fetch had eaten the budget never got to start one. An explicit sync makes whatever progress its
  budget allows, bounded at `RECUT_CHATS_PER_RUN` and resumable through the markers — **and never
  fewer than one chat**: `recut_pending_chats` runs after `_sync_chats` on the *same* budget, and
  the `edit_refetch` tail of a large index can spend all of it with nothing left to fetch, so a
  gate on `budget.expired` alone left the MCP `sync` tool at its default budget re-cutting nothing,
  ever. The first chat is taken whatever the clock says, the rest wait for the next run, and a
  *cancelled* budget (`SyncBudget.cancelled`) keeps nothing back — the caller and its lock are
  going away. Nothing
  flags a pending re-cut, so `search` re-derives the condition into `search.RECUT_PENDING` and a
  user who has only ever searched is told to sync. **The re-cut touches no
  `messages` row**: unit boundaries change, message text does not, so no `indexed = 0` flagging
  and no `msg_fts` rewrite — flagging inside the transaction recovers nothing and flagging outside
  one is the backlog this design exists to prevent. The marker holds the version a chat was last
  re-cut at, never a bare presence flag, so a crash between the last chat and the cleanup cannot
  make the *next* bump skip the chats already done. Go through `units.units_for_chat`, not
  `rebuild_for_chat`: after a delete-all every stale lookup inside the latter is empty by
  construction and its `_apply` would report `deleted_ids = []`, so the delta would not even be
  honest.
- `units.py` must never import `grepogram.index` — `index.py` imports `UnitDelta` from `units`,
  so the reverse is a cycle. `units.recut_chat` and `units.invalidate_units_for` therefore return
  a `UnitDelta` and the **caller** hands it to `index.index_units`: `sync.recut_pending_chats`,
  `sync._drop_deleted`, `sync.prune_deleted` and `media.run` all do. A primitive that indexed its
  own delta would be the thing that could not be written here.
- Extraction is a **network pass that never blocks a sync**. Telethon downloads from a `Message`
  object and never from a stored row, so `media.run` re-fetches every pending message by id
  before downloading it — that is also where the size comes from, there being no size column. It
  runs from `grepogram extract` after a sync, never inside one, because a 400-page PDF or a slow
  OCR must not eat a sync's budget; `messages.media_state` is the whole of its memory, so it is
  resumable by construction. The offline half comes first and touches no network at all: which
  kinds have an extractor and which are switched off in `[media]` follows from the stored
  `media_kind` alone, so three bulk `UPDATE`s park them — and **none of them flags `indexed`**,
  which is why `db.set_media_text` (text plus `indexed = 0`) and `db.set_media_state` (the state
  byte alone) are two writers. Most kinds have no extractor at all, so flagging there would mark
  tens of thousands of rows across every chat and hand `_sync_chats`'s deferred loop the very
  backlog the recipe contract forbids. A batch that read something ends by calling
  `units.invalidate_units_for` once per chat per batch and indexing the delta: `_recut_start`
  returns `None` for a message in a closed window, where all but the newest handful of a chat's
  history lives, and `on_chat_synced` clears `indexed` whether or not anything was rebuilt — so
  without that step the text this pass reads would be written to a column nothing ever renders.
  That re-cut is a chat's own units and no more, so `media._recut` owes the same two follow-ups
  every such pass owes: `sync._invalidate_comment_posts` for an extracted **comment**, whose
  channel post thread quotes it and is reachable only through `comment_of_*`, and keeping
  `indexed = 0` raised for the rows `units.uncut_rows` names — a `(chat, topic)` with no window
  at all, where `_invalidation_start` answers `None` and clearing the flag would hide the rows
  from `index_stranded`, the one pass that would ever cut their first windows. An *empty delta*
  is not that case and must still clear: an OCR that read nothing leaves every unit identical,
  and keeping those flagged is the whole-index backlog again.
- **A message re-stored with a different attachment loses its extraction.**
  `db._MESSAGE_UPSERT` leaves `extracted_text` and `media_state` alone so a routine re-store
  cannot clobber the pass's work, *except* when `media_kind` or `media_filename` moved
  (`db._ATTACHMENT_REPLACED`) — then the text is dropped and the state goes back to
  `MEDIA_PENDING`, because `MEDIA_EXTRACTED` is terminal and the row would otherwise carry the
  previous file's text for good, unreachable even to `extract --retry-failed`. Those two columns
  and nothing else: a caption edit moves neither, and keying this on `edit_date` or `text` would
  re-download every extracted photo in the index for a typo fix, `edit_refetch` messages per
  chat per sync. `sync._differs` compares both columns in full, so a replaced attachment always
  reaches the upsert as an edit. **Clearing the column is only half of the reset**: the units and
  `unit_fts` rows cut from that text still hold it, and `indexed = 0` reaches none of it — a
  rebuild does not re-cut a closed window and `on_chat_synced` clears the flag anyway, so with no
  row left flagged and no media left pending nothing would ever lead back to it. Invalidating is
  the caller's job, `db` knowing nothing of `units`: `sync._Run.store` asks
  `db.attachment_replaced` over the row as it is stored *before* the upsert, and for the rows
  that actually carried text runs `units.invalidate_units_for` + `index.index_units` and
  `sync._invalidate_comment_posts` in the upsert's own transaction — the same pairing
  `media._recut` runs in the other direction, off the event loop (`joined_to_thread`) because a
  re-cut runs to the end of the chat. Rows that carried no text are deliberately left alone: a
  changed rendered line inside a closed window is the documented v1 limitation
  (`units.rebuild_for_chat`), and widening this to every attachment change would re-cut a chat's
  tail for edits the design accepts.
- An extractor **degrades rather than fails**. `extract.registry()` is re-derived on every call,
  never frozen at import, because what a kind maps to is a property of the environment: `pypdf`
  and `python-docx` come with the `media` extra, and OCR needs macOS and
  `pyobjc-framework-Vision` (Vision learned Russian only in macOS 15, and a
  `VNRecognizeTextRequest` given a language the build does not know fails outright, so
  `_requested_languages` narrows `OCR_LANGUAGES` to `supportedRecognitionLanguages` instead). An
  installation without them registers less and the pass parks that media at
  `db.MEDIA_UNSUPPORTED` — this build cannot read it, rather than a failure to retry — and
  nothing raises, nothing is lost, and `--retry-failed` queues it again once the extra is there.
  Every module must keep importing with no extra installed; the Vision call sits behind one
  module-level indirection so the suite never needs a Mac.
- Unit reactions are refreshed by a **direct `UPDATE`**, never through the rebuild.
  `units._content_key` is `(kind, topic_id, msg_ids, date_start, date_end, text)` and reactions
  are deliberately not in it — adding them would invalidate, delete, re-insert and re-embed a
  unit on every reaction change, `edit_refetch = 200` messages per chat per sync, forever — so
  `units._apply` keeps the stored row when it re-cuts an identical unit, and that is what
  preserves a refreshed total. A rebuild-driven refresh is inert twice over: `_apply` keeps the
  row, and the closed window nearly every re-fetched message sits in is never re-cut at all. The
  refresh is `db.refresh_unit_reactions`, called from the `edit_refetch` path independently of
  the rebuild, recomputing totals over `json_each(units.msg_ids)`. Those are Telegram `msg_id`s,
  the space `units.msg_ids` stores, while everything on the `edit_refetch` path carries
  `messages.id` rowids — the caller converts. In a fixture chat the two coincide from 1, which is
  exactly how this ships broken.
- Work a sync — or a research pass (`media.run`, `research.discovery.discover` and `research.running.run`'s
  `discover_offline`, the MCP `research_discover` offline branch) — hands to a worker thread that
  writes goes through `sync.joined_to_thread`, never bare `asyncio.to_thread`: an `anyio` cancel scope (how the MCP server cancels a tool call) abandons
  the future rather than the job, and the `SyncLock` must not be released while a detached thread
  still writes. The join is a `threading.Event` — a cancelled scope raises out of every `await`,
  so it cannot be one — and it is **not** bounded: a bound would give the lock up over a live
  writer in exactly the case it exists for (a big rebuild, an embedding backlog), and the next
  process would start writing against a database this one has not finished with. What keeps the
  wait short is the `abort` callback: the embedding step is paced by `SyncBudget`, so a
  cancellation calls `budget.cancel()` and `index.embed_dirty_units` stops at the next batch;
  the indexing step is one transaction and ends on its own. `abort` runs on a cancellation
  only, never for a job that raised: the budget it cancels is every account's queue's. Only a
  killed process detaches a
  writer, and `messages.indexed` plus `index.repair_unit_index` are what the next run repairs it
  with.
- Windows are cut in `msg_id` order but rows do not always arrive that way (a channel stores
  comments in its discussion group before the group's own history gets there). `units._recut_windows`
  starts at the open window unless a changed message no window holds lies below it; then it
  re-cuts from the window before that message. Membership in `msg_ids`, not the id range, decides
  whether a window holds a message.
