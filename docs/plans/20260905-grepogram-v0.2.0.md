# grepogram v0.2.0

> Revised after a plan review that found eleven critical and thirteen important defects, all of
> them verified against the source before revision. Where this plan describes existing code it now
> cites the file and line it was read from; the places the first draft described from memory are
> exactly the places that were wrong. Read the code, not this file, when the two disagree.

## Overview

v0.2.0 makes grepogram see what a chat actually posted, rank by what the chat agreed with, and
keep its index honest as messages come and go — then opens the project to the world.

Eight items, decided with the user before planning:

1. **Photo OCR through macOS Vision**, feeding recognised text into the message and its unit.
2. **PDF and DOCX text extraction**, through the same subsystem.
3. **The window-cap fix** — `window_max_chars` is a floor today, not a ceiling.
4. **`sources prune`** for chats that have left an indexed folder.
5. **Reactions as a ranking signal.**
6. **Deleted-message handling.**
7. **PyPI publishing**, with the repository going public.
8. **`grepogram import <tdesktop export dir>`** for chats the account can no longer open.

The problem each solves, in the user's own terms: these are noisy expat and country-community
chats, searched for answers Google does not have. Today a photographed embassy announcement, a
rental contract in a PDF and a price list someone screenshotted are all invisible — the index
holds only `[photo]` and `[document: contract.pdf]`. The answer the chat converged on is not
ranked any higher than the three wrong guesses above it. Messages people deleted still surface.
And 15.8% of windows silently overflow the embedder, so part of their text has no vector at all.

Voice and video-note transcription (whisper.cpp) is **deferred to v0.3.0** and is not planned
here. The extraction subsystem is nonetheless built as a registry of extractors, so whisper joins
it later as one more entry rather than as a second pipeline.

## Context (from discovery)

Every claim below was read from the file named. Line numbers are as of `main` at v0.1.1.

- **`grepogram/units.py:119-126`** — `_OpenWindow.must_cut_before` tests
  `self.chars >= cfg.window_max_chars` *before* `add` (units.py:128-134) appends, so a closed
  window holds the cap plus whatever message carried it over. Measured on the live 160k-message
  index: median window 1,274 chars against a 1,500 cap, tail to 3,453, and 15.8% of a 400-window
  sample over the embedder's 512-token cap. `add` calls `render_line` a second time, so a fix
  that also needs the prospective length must avoid rendering three times.
- **`grepogram/units.py:44-53`** (`render_line`) and **`units.py:64-69`** (`media_placeholder`) —
  render `[photo]` and `[document: name.pdf]`. This is the seam extracted text plugs into.
- **`grepogram/units.py:559-567`** — `_content_key` is
  `(kind, topic_id, msg_ids, date_start, date_end, text)`. **Reactions are not in it**, and
  `_apply` keeps the stored row when the key matches; its docstring names this very case ("a
  reaction count changing on a message inside it, say"). A reaction total on a unit therefore
  cannot be refreshed through the rebuild — see Task 10.
- **`grepogram/units.py:456-457`** — `_recut_start` returns `None` for a message inside an already
  closed window, which README's Known Limitations states as shipped behaviour. Nothing re-cuts a
  closed window; only deleting its unit does.
- **`grepogram/db.py:53, 536-547`** — `meta(key, value)` with `get_meta` / `set_meta`, already
  transaction-safe.
- **`grepogram/db.py:80-98`** — `messages` carries `media_kind`, `media_filename` **and
  `reactions_total`**, but **no media size**. `units` carries none of them.
- **`grepogram/db.py:144, 455-470`** — `BASE_VERSION = 5`, `MIGRATIONS = {BASE_VERSION: _V5}`,
  `SCHEMA_VERSION = max(MIGRATIONS)`. For a database with no schema objects `migrate()` applies
  **every** step from `BASE_VERSION` through `SCHEMA_VERSION`. Adding a column to `_V5` *and* in a
  step 6 therefore double-applies and raises `duplicate column name`.
- **`grepogram/db.py:1171-1175`** — `delete_units` deliberately leaves `unit_fts` and `unit_vec`
  alone: "their FTS and vector rows are the indexer's to drop by the same ids". Only
  `index.index_units` / `index.repair_unit_index` clean them, per chat, during that chat's indexing.
- **`grepogram/sync.py:198, 315`** — `reactions_total` is populated on every store and refreshed by
  the `edit_refetch` tail pass, because `_differs` (sync.py:788-799) compares the whole
  `MessageRow` and exempts only `topic_id` / `comment_of_*` as "kept" fields. **`reactions_total`
  is read nowhere in `search.py`, `index.py` or `mcp.py`.**
- **`grepogram/sync.py:752-772`** — `_refetch_edits` iterates with `client.iter_messages`, which
  **omits** deleted messages rather than yielding an empty slot. Detecting a deletion here is a set
  difference over the id range the iteration covered, not an empty-slot check.
- **`grepogram/sync.py:75, 1031, 1119`** — `on_chat_synced` is one transaction **per chat**;
  `index_stranded` sweeps at most `STRANDED_CHATS = 4` chats per run. There is no single
  transaction in which a whole-index rebuild completes.
- **`grepogram/sync.py:1326-1327`** — `_sync_chats` ends with
  `for chat in [*deferred, *queue]: await index_pending(...)`, **with no budget check**, and
  `index_pending` (sync.py:1080-1099) rebuilds every chat holding unindexed rows. Anything that
  leaves a whole-index backlog of `indexed = 0` rows will therefore be drained in full by the very
  next run, however short its budget — including a 20-second `_auto_sync` inside a `search` call.
- **`grepogram/units.py:358-383`** — `rebuild_for_chat` opens with
  `changed = [m for m in db.get_messages_by_ids(conn, new_msg_ids) ...]` and rebuilds **only what
  those ids reach**: `_rebuild_posts` touches the changed posts, `_rebuild_conversation` only the
  topics in `changed`, `_rebuild_threads` only reachable threads. Dropping a chat's units and then
  calling it with a partial id list destroys every unit the list does not reach, permanently.
- **`grepogram/sync.py:365-372`** — `SyncBudget.remaining` is `None` for an unlimited budget, so
  any comparison against it must handle `None` before comparing.
- **`grepogram/sync.py:232, 250-275`** — `media_of` classifies media; `document_kind` maps **both**
  PDF and DOCX to the single `MediaKind` member `"document"` (models.py:15-28). A registry keyed on
  `MediaKind` alone cannot tell them apart.
- **`grepogram/sync.py:284`** — a function named `media_text` already exists and is used by
  `map_message`. The new column is therefore called **`extracted_text`**, not `media_text`.
- **`grepogram/search.py:489, 583-585, 621-625`** — `search()` reranks in every mode unless
  `rerank=False`; a unit that is indexed but not stored is logged and skipped, so a missing unit
  is silent; and when the reranker cannot load, `_rerank` returns the **fused RRF scores**
  untouched, which top out near `1/(rrf_k+1) ≈ 0.016`.
- **`grepogram/rerank.py:156-161`** — `as_scores` passes `predict`'s output through unchanged;
  `BgeReranker` applies no sigmoid, so production scores are raw logits spanning several units,
  while `FakeReranker.score` (rerank.py:70-74) returns a fraction in `[0,1]`. A fixed additive
  bonus tuned on the fake is a no-op in production and dominant in the no-reranker fallback.
- **`grepogram/mcp.py:507-518`** — `_auto_sync` runs `sync_all` with
  `search.auto_sync_budget_s = 20` on any `search` call once the index is older than 60 minutes.
  Anything that empties the index must never be startable from here.
- **`grepogram/cli.py:260-289`** — `embed_cmd` builds **no Telegram client**; `sync_cmd`
  (cli.py:191-216) is the model for anything that talks to Telegram (`_require_api_keys`,
  `tg.make_client`, `tg.connected`, the `AuthRequired` / `RPCError` handling).
- **`grepogram/config.py:71`** — `_POSITIVE_KEYS` holds **dotted** keys.
- **`tests/conftest.py:56-58`** — the shared `conn` fixture runs `db.migrate` on an empty
  in-memory database, so a broken migration path fails the whole suite, not one test.
- **`tests/fakes.py:181-436`** — `FakeClient` has **neither `get_messages` nor `download_media`**.
- **`.github/workflows/ci.yml:36, 50`** — both jobs run `uv sync --locked --group dev`. **No
  extras are installed in CI**, and `mypy` runs strict over `grepogram` and `tests`.
- **`README.md:385-410`** ("Unit length and the token cap") documents the current floor behaviour
  at length and recommends `max_seq_length` as the mitigation; **`README.md:571-575`** documents
  deleted messages and edits. Tasks 2, 12 and 13 falsify these passages.
- Tooling present on this machine: `gitleaks` (`/opt/homebrew/bin/gitleaks`) and `actionlint`
  (`~/go/bin/actionlint`).

## Development Approach

- **testing approach**: Regular (code first, then tests) — what v1 used; the suite is built for it.
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - write unit tests for new functions/methods and for modified ones
  - add cases for new code paths; update existing cases when behaviour changes
  - cover both success and error scenarios
- **CRITICAL: all tests must pass before starting the next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- run tests after each change
- maintain backward compatibility

### Non-negotiable project rules (from CLAUDE.md)

- **stdout is the MCP protocol.** Never `print`. Log through `logging`; `typer.echo` only in
  `cli.py`, with `err=True` for diagnostics.
- **The schema is append-only now that v0.1.0 is tagged.** Append a step above `BASE_VERSION`;
  **leave `_V5` alone.** Editing the base DDL is only for before the first release.
- Scoped Conventional Commits, lowercase description. One logical change per commit. No trailers,
  never change the git identity.
- `uv.lock` is committed and CI installs with `--locked` — after touching dependencies run
  `uv lock` and commit the lockfile.
- The version lives in `grepogram/__init__.py` only.
- Every gate passes before a task is done:
  `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy`

## Testing Strategy

- **unit tests**: required for every task (see Development Approach above)
- **no e2e/UI tests**: this project has none — a CLI and an MCP server. The equivalent is
  `tests/test_cli.py` (typer `CliRunner`) and `tests/test_mcp.py` (in-process tool calls), and
  both count with the same rigour.
- **slow tests** (`-m slow`) exercise the real `bge-m3` and reranker and run with
  `HF_HUB_OFFLINE=1`. Anything touching embedding or reranking must keep them passing.
- **fakes over network**: `tests/fakes.py` and `GREPOGRAM_FAKE_MODELS=1` mean no test may reach
  Telegram, HuggingFace or the filesystem outside `tmp_path`. `FakeClient` must therefore grow
  `get_messages` and `download_media` before Tasks 7 and 13 can be tested (Task 5 does it); Task 12
  needs neither: it detects deletions through `iter_messages(limit=…)` and a set difference over
  the ids that came back. (`FakeClient.iter_messages` also implements `ids=` yielding `None` for a
  missing id at `tests/fakes.py:352-356` — that is what **Task 13** builds on, not Task 12.)
- **CI installs no extras** (ci.yml:36,50). `pypdf` and `python-docx` must therefore join the `dev`
  dependency group as well as the `media` extra, and `pypdf`, `docx`, `Vision` and `Quartz` need
  `ignore_missing_imports` overrides or strict mypy fails in both jobs.
- **Three tests that would pass while the feature is broken** — named here because they are the
  traps the review found, and each task's checkboxes are written to avoid them:
  1. a `units.reactions` test that only checks sum/zero/round-trip passes against a column frozen
     at zero in production (Task 10 tests the *refresh*, not the sum);
  2. a reaction-bonus reordering test on `FakeReranker`'s `[0,1]` scale proves nothing about raw
     logits (Task 11 pins the bonus to a normalised scale and tests both scales);
  3. an `EXPLAIN QUERY PLAN` test passes on a partial index that covers the whole table (Task 3's
     predicate excludes rows with no media at all);
  4. any test using `messages.id` where `units.msg_ids` holds Telegram `msg_id` passes in a fixture
     chat where the two coincide from 1 and fails silently in production (Task 10 requires a chat
     where they differ);
  5. a test that OCR text reaches a unit passes on a channel post or an open window while the
     closed-window case — the overwhelming majority of real history — is broken (Task 8 requires
     the closed-window case explicitly).

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update the plan if implementation deviates from the original scope
- keep the plan in sync with the work actually done

## Solution Overview

**One re-index, not four — and never a stranded one.** Three items change what a unit's text is,
and each would on its own invalidate every stored unit. They share a single `unit_recipe` integer
in `meta`. The dangerous part is *how* the re-cut runs, and the first draft got it wrong in four
separate ways, so the mechanism is spelled out here and is binding:

- **The re-cut is a bounded pass over whole chats, not a global flag.** It runs as its own step,
  `sync.recut_pending_chats`, after the chat loop — **not** hooked into `on_chat_synced`. It takes
  at most `RECUT_CHATS_PER_RUN` chats and stops when the budget runs out. This is what keeps it
  away from `sync.py:1326`'s unbudgeted deferred `index_pending` loop: nothing is ever left flagged
  outside the transaction that rebuilds it, so no later run — least of all a 20-second auto-sync —
  inherits a whole-index backlog to drain.
- **A chat is re-cut whole or not at all, through `units_for_chat` — not `rebuild_for_chat`.**
  After a delete-all, every stale lookup inside `rebuild_for_chat` is empty by construction
  (`open_window` → `None` at units.py:426, `post_units` / `threads_touching` → `[]`), so it is
  `units_for_chat` (units.py:327) reached the long way, at the cost of loading the chat twice and
  one `get_descendants` per reply chain. Use `units_for_chat` directly.
- **The re-cut touches no message row.** Message *text* does not change when units are re-cut, so
  `msg_fts` needs no rewrite and `messages.indexed` needs no flagging. Flagging inside a
  transaction that also rebuilds recovers nothing (it rolls back with everything else) while
  costing a full write of `messages` and churn of the `messages_unindexed` partial index — and
  flagging *outside* one is the whole-index backlog this design exists to prevent. Do neither.
- **Progress is recorded per chat, so an interrupted re-cut continues.** `meta['unit_recut:<id>']`
  marks a chat done. A run that is cut short leaves the finished chats marked, and the next run
  picks up only the unmarked ones — it must never re-flag work already done.
- **A re-cut is started deliberately, not incidentally.** A run whose budget is below
  `RECUT_MIN_BUDGET_S` does not start one (an unlimited budget — `remaining is None` — always
  qualifies); it logs that a re-cut is pending, and `search` re-derives the condition and returns
  it in the existing `warnings` field so an MCP-only user is told to run `grepogram sync`.
- **"Fresh" means the database holds no units**, not "no recipe recorded" — a v0.1.1 index has no
  recipe recorded either, and treating it as fresh is exactly how the whole release silently does
  nothing.
- **Completion means no chat is left unmarked.** Only then is `unit_recipe` recorded and the
  per-chat markers cleared.

**Extraction is a network pass, run after sync.** Telethon cannot download from a stored row — it
needs the `Message` object — so the pass re-fetches each pending message by id before downloading.
That makes it a Telegram-facing, flood-exposed pass modelled on `sync_cmd`, not the offline
`embed_cmd` the first draft claimed. It still runs *after* sync rather than inside it, because a
400-page PDF or a slow OCR must not consume a sync's budget. State lives in a column, so it is
resumable by construction.

**Extractors dispatch in two stages.** `MediaKind` is the outer key, but it has one `document`
member for both PDF and DOCX, so the document entry is itself a dispatcher on filename extension
with a magic-byte confirmation. `pdf`/`docx` are pure Python and run everywhere; `photo` maps to
macOS Vision through an optional extra and is simply absent when unavailable, so its media is
marked unsupported rather than failing the pass. `voice` and `video_note` stay unmapped — that is
where whisper lands in v0.3.0.

**Reactions are refreshed directly, never through the rebuild.** `_content_key` does not include
reactions and a closed window is never re-cut, so a rebuild-driven refresh is inert by
construction. Instead a small `UPDATE units SET reactions = ...` pass recomputes the totals for
units holding messages the `edit_refetch` pass just re-read. After the cross-encoder has ordered
the candidates, scores are min-max normalised across the result set and a bounded bonus is added —
normalisation is what makes a single weight meaningful against raw logits — and the bonus is
skipped entirely when reranking did not actually run, because the RRF fallback scale would let it
dominate.

**Deletions where they are free, sweeps where they are not.** `iter_messages` omits deleted
messages, so the tail pass detects them as a set difference over the id range it covered — no
extra requests. A full sweep is ~1,600 requests, so it is an explicit
`grepogram prune-deleted --budget N`, resumable through a `meta` cursor, never automatic.

**Going public is the last thing, and it is manual.** The workflow job and the readiness work are
tasks; flipping the repository and configuring the PyPI trusted publisher need the user's own
account and are Post-Completion. A clean full-history secret scan gates both.

## Technical Details

### Schema (v6) — a migration step only, `_V5` untouched

```sql
ALTER TABLE messages ADD COLUMN extracted_text TEXT;
ALTER TABLE messages ADD COLUMN media_state INTEGER NOT NULL DEFAULT 0;
ALTER TABLE units    ADD COLUMN reactions INTEGER NOT NULL DEFAULT 0;
CREATE INDEX messages_media_pending
    ON messages(chat_id, id) WHERE media_state = 0 AND media_kind IS NOT NULL;
```

`MIGRATIONS` gains key `6`; `SCHEMA_VERSION = max(MIGRATIONS)` follows automatically (db.py:144).
`_V5` is **not** edited — a fresh database applies steps 5 and 6 in order.

The index predicate excludes `media_kind IS NULL`, or it would cover every row in the table
forever and the pending query would remain a scan.

`media_state`: `0` not looked at, `1` extracted (`extracted_text` may still be empty when an image
held no text), `2` unsupported (no extractor for this kind), `3` failed (retryable — a timeout, a
corrupt file), `4` skipped (over `max_download_mb`), `5` disabled (the kind is switched off in
config). `0` is the work queue; `3` is retried by `--retry-failed`; `5` is re-queued to `0` when
the kind is switched back on, so a disabled kind drains instead of being re-read on every pass.

### New config section

```toml
[media]
enabled = true          # master switch for the extraction pass
ocr = true              # photos through macOS Vision
documents = true        # pdf and docx
max_download_mb = 20    # anything larger is skipped, never downloaded
```

`MediaCfg` joins `models.py`, `_SECTIONS` in `config.py` and `TEMPLATE`. `_POSITIVE_KEYS` gains
the **dotted** key `"media.max_download_mb"` (config.py:71).

`SearchCfg` gains `reaction_weight: float = 0.05`, applied to normalised scores.

### The unit recipe

`units.RECIPE_VERSION: int`, stored as `meta['unit_recipe']`. `meta['unit_recut:<chat_id>']` holds
**the version a chat was last re-cut at** — a value, not a presence flag, because a crash between
the last chat and the final cleanup would otherwise leave stale markers that make the *next* bump
skip exactly the chats already done. This release bumps three times (2 → 3 → 4), so that matters.

**A database built from empty records `RECIPE_VERSION` in `db.migrate` itself**, on the branch that
creates the schema (db.py:455-462). It cannot be decided later: by the time any sync-time pass runs,
`index_pending` (sync.py:1313, 1326) has already cut units for every chat fetched, so "the database
holds no units" is never true and a brand-new index would re-cut everything it just cut correctly.

`sync.recut_pending_chats(conn, cfg, budget)` runs as its own step, **after `index_stranded`**
(sync.py:1328) so that sweep cannot rebuild a chat the re-cut just finished:

1. read `meta['unit_recipe']`; if it equals `RECIPE_VERSION`, do nothing;
2. if `budget.remaining` is not `None` and below `RECUT_MIN_BUDGET_S`, log that a re-cut is pending
   and return — do **not** start one (an unlimited budget always qualifies);
3. otherwise walk **all** chats (`db.list_chats`, not the run's queue — a channel's discussion group
   known only through the link is absent from `resolve_sources`' output, sources.py:575-612, and its
   windows hold every comment in the index), taking those whose marker is not `RECIPE_VERSION`, at
   most `RECUT_CHATS_PER_RUN`, while the budget allows;
4. for each, **through `_joined_to_thread`** (CLAUDE.md invariant; a per-chat transaction holds
   `db.Connection.lock` and the MCP server shares that connection), in **one** transaction: re-read
   the chat row and skip it if it is gone, `units.recut_chat`, `index.index_units` for the returned
   delta, `index.repair_unit_index`, then write the marker;
5. when every chat's marker is `RECIPE_VERSION`, record `unit_recipe = RECIPE_VERSION` and delete
   the markers — **in one transaction**, so the cleanup is tidy-up rather than correctness.

The indexing lives in `sync.py`, **not** in `units.recut_chat`: `index.py:48` does
`from grepogram.units import UnitDelta`, so `units.py` importing `index` is a cycle. Pass **no
message ids** to the indexer — `index.index_messages` (index.py:75-93) rewrites an `msg_fts` row per
id, and a unit re-cut changes no message text; this is the single largest wasted cost available here.

Bumped by: the window fix (Task 2 → 2), extracted text reaching `render_line` (Task 8 → 3), and
reactions landing on units (Task 10 → 4). A v0.1.1 index sees one mismatch against the final value
and pays one re-cut.

### Processing flow, extraction pass

```
first, offline, with no network at all — these depend only on the stored media_kind:
    bulk UPDATE media_state = 2 where the kind has no registered extractor
    bulk UPDATE media_state = 5 where the kind is disabled in config
    bulk UPDATE media_state = 0 where a state-5 kind has been re-enabled

then, over the rows that remain at media_state = 0, oldest first, chat by chat:
    re-fetch the batch by id:  client.get_messages(chat_id, ids=[...])
    for each message, within the budget (seconds, like SyncBudget):
        size from the re-fetched media > cap         -> media_state = 4, never downloaded
        download_media to a temp file in the scratch dir
        text = extractor(path)                       -> ExtractError: media_state = 3
        extracted_text = text, media_state = 1, message flagged indexed = 0
        delete the temp file (in a finally)
    commit per batch, so a flood wait keeps what it earned
```

### tdesktop import

Telegram Desktop's `result.json` holds real ids. The importer stores through `db.upsert_messages`,
marks the chat `unavailable = 1` and `last_msg_id = 0` (nothing was fetched from Telegram), and
gives it source id `import:<slug>`. `db.upsert_chat` (db.py:553-574) overwrites `source_id`
unconditionally, so adding a live source over an imported chat must be **refused by name** in
Task 15's `sources add` guard, or the import protection silently evaporates.

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): everything achievable in this repository.
- **Post-Completion** (no checkboxes): flipping the repository to public, configuring the PyPI
  trusted publisher, and the manual verification runs against real Telegram data.

## Implementation Steps

### Task 1: Record a unit-recipe version and re-cut safely, chat by chat

**Files:**
- Modify: `grepogram/units.py`
- Modify: `grepogram/sync.py`
- Modify: `grepogram/db.py`
- Modify: `grepogram/search.py`
- Modify: `tests/test_sync.py`
- Create: `tests/test_units_recipe.py`

- [x] add `units.RECIPE_VERSION: int = 1`, `sync.RECUT_MIN_BUDGET_S` and `sync.RECUT_CHATS_PER_RUN`
      with docstrings naming what a bump costs and why a short-budget run must not start one
- [x] add `db.unit_recipe` / `db.set_unit_recipe`, the **version-valued** `unit_recut:<chat_id>`
      markers, and the `meta` helpers this needs — there is no prefix listing and no `delete_meta`
      in `db.py` today (db.py:536-549 is the whole meta surface)
- [x] **stamp `unit_recipe = RECIPE_VERSION` in `db.migrate`** on the branch that builds a schema
      from empty (db.py:455-462). `units.py:34` already imports `db`, so `db.py` must not import
      `units` at module level — put `RECIPE_VERSION` in `db.py`, or import it inside `migrate()`. A sync-time "no units yet" test can never fire: `index_pending`
      (sync.py:1313, 1326) has already cut units for every chat fetched by then, so a brand-new
      index would re-cut everything it just cut correctly
- [x] add `units.recut_chat(conn, chat, cfg)`: collect the chat's unit ids, `db.delete_units`, then
      `db.insert_units(units_for_chat(conn, db.get_messages(conn, chat.id), chat, cfg))`, returning
      `UnitDelta(inserted_ids=…, deleted_ids=<the ids collected first>)`
- [x] use **`units_for_chat` (units.py:327), not `rebuild_for_chat`**: after a delete-all every
      stale lookup in `rebuild_for_chat` is empty by construction, so it is the same result reached
      the long way — loading the chat twice plus one `get_descendants` per reply chain — and its
      `_apply` reports `deleted_ids = []`, so the delta would not even be honest
- [x] the re-cut must **not** touch `messages`: no `indexed = 0` flagging, no `msg_fts` rewrite.
      Unit boundaries change, message text does not
- [x] add `sync.recut_pending_chats(conn, cfg, budget)` implementing the five-step procedure in
      "The unit recipe" **exactly** as written — its own step after `index_stranded`, running each
      chat through `_joined_to_thread` with `budget.cancel` as `abort`, and doing the indexing
      itself (`index.index_units` + `repair_unit_index`, **no message ids**), because `units.py`
      cannot import `index` (index.py:48 imports `UnitDelta` from units — a cycle)
- [x] re-read the chat row each iteration and skip a chat that is gone, as `index_pending` does
      (sync.py:1093); otherwise `insert_units` raises `IntegrityError` outside the chat loop's guard
      (sync.py:1316) and escapes `sync_all` as a traceback
- [x] compare the marker **by value** (`!= str(RECIPE_VERSION)` means "needs a re-cut"), and make
      step 5 one transaction; handle `budget.remaining is None` (unlimited) before comparing
      (sync.py:365-372)
- [x] have `search` re-derive a pending re-cut (`db.unit_recipe(conn) != units.RECIPE_VERSION`) and
      return it in the existing `warnings` field — the short-budget path writes no flag, and the
      MCP-only user is exactly who needs the warning
- [x] log a warning when a re-cut drops vectors and `cfg` yields no embedder
      (`cli._optional_embedder`, cli.py:221-227), or the chats silently end up unembedded
- [x] write tests: a bump re-cuts chat by chat; an equal version does nothing; a database with rows
      and no recipe **does** re-cut; **a freshly migrated database records the recipe and re-cuts
      nothing**; a budget below the floor does not start one and emits the warning; an unlimited
      budget does start one; a re-cut chat keeps every unit kind it had (window, thread, post);
      **a crash after the last chat but before cleanup does not make the next bump skip that chat**;
      **a link-only discussion group is re-cut**; a chat deleted mid-pass is skipped, not raised;
      at most `RECUT_CHATS_PER_RUN` chats move per run; **no `messages` row is written**
- [x] run tests — must pass before task 2

### Task 2: Make `window_max_chars` a ceiling instead of a floor

**Files:**
- Modify: `grepogram/units.py`
- Modify: `grepogram/embed.py` (➕ its `BgeM3Embedder` docstring also described the floor)
- Modify: `README.md`
- Modify: `tests/test_units_windows.py`
- Modify: `tests/test_units_recipe.py` (➕ its `_bump` targets were absolute; now relative to
  `RECIPE_VERSION`, so a bump does not rewrite the file)
- Delete: `docs/backlog/window-char-cap-is-a-floor-not-a-ceiling.md`

- [x] change `_OpenWindow.must_cut_before` (units.py:119-126) to cut when appending `msg` *would*
      exceed `window_max_chars`, not when the window already has
- [x] keep a single oversized message forming a window of its own (the empty-window guard)
- [x] render each line once — `must_cut_before` and `add` (units.py:129) both need its length
- [x] bump `units.RECIPE_VERSION` to 2
- [x] update the `cut_windows` docstring, which describes the old behaviour
- [x] rewrite README's "Unit length and the token cap" (README.md:385-410), which currently
      explains the floor behaviour and recommends `max_seq_length` as the mitigation
- [x] write tests: a window never exceeds the cap; an oversized single message still stands alone;
      a message that exactly hits the cap is kept; the measured 15.8% overflow case now passes
- [x] `git rm` the backlog file in this task's commit
- [x] run tests — must pass before task 3

### Task 3: Add schema step 6 for extracted text and unit reactions

**Files:**
- Modify: `grepogram/db.py`
- Modify: `grepogram/models.py`
- Modify: `grepogram/sync.py`
- Modify: `tests/test_db.py`

- [x] add **migration step 6 only** — `MIGRATIONS[6]` with the four statements from Technical
      Details. **Do not touch `_V5`**: `migrate()` applies every step from `BASE_VERSION` for a
      fresh database (db.py:455-462), so a column in both places raises `duplicate column name` and
      fails `tests/conftest.py`'s shared fixture, i.e. the entire suite
- [x] give `messages_media_pending` the predicate `media_state = 0 AND media_kind IS NOT NULL`
- [x] keep a re-store from clobbering `extracted_text` / `media_state` by **omitting both from the
      upsert's SET clause entirely** (db.py:150-181). The COALESCE idiom used for `topic_id` cannot
      work here: it relies on `None` meaning "not supplied", and `media_state` is
      `NOT NULL DEFAULT 0`, so a freshly mapped row carries `0` and would reset every extracted
      message to pending on every sync
- [x] add `extracted_text` / `media_state` to `MessageRow` and `reactions` to `UnitRow`, and wire
      **all three** through the row mappings and `_UNIT_INSERT` (db.py:184-188) in this task, so no
      later task inherits a half-wired column
- [x] **exclude** `extracted_text` and `media_state` from `_differs`'s comparison by normalising
      both sides (`dataclasses.replace(row, extracted_text=None, media_state=0)`) before comparing.
      The existing "kept" idiom (sync.py:793-797) tests `is None`, so it would exempt
      `extracted_text` correctly but **not** `media_state`, whose fresh value is `0`. Without this,
      every extracted message inside the `edit_refetch` window counts as an edit on every sync and
      is re-cut and re-embedded forever
- [x] write tests: a v5 database migrates to v6 keeping its rows; a fresh database migrates once
      with no duplicate-column error; a re-store preserves extracted text and state; `_differs`
      ignores both columns; a unit round-trips a non-zero `reactions` value; `EXPLAIN QUERY PLAN`
      shows the partial index used **and** a test asserting it does not cover media-less rows
- [x] run tests — must pass before task 4

### Task 4: Add the `[media]` config section

**Files:**
- Modify: `grepogram/models.py`
- Modify: `grepogram/config.py`
- Modify: `README.md`
- Modify: `tests/test_config.py`

- [x] add `MediaCfg` with `enabled`, `ocr`, `documents`, `max_download_mb`
- [x] register it in `_SECTIONS`, in `Config`, and in `TEMPLATE` with comments matching the style
- [x] add the dotted key `"media.max_download_mb"` to `_POSITIVE_KEYS` (config.py:71)
- [x] update README's Configuration block (README.md:273-320) by hand — **no test pins README
      against `TEMPLATE` today**; `tests/test_cli.py:142,156` only pin the *written file*
- [x] add that missing pin: a test asserting README's config block matches `config.TEMPLATE`
      verbatim, so this drift cannot ship again
- [x] write tests: defaults, each key's type rejection, a zero `max_download_mb` refused
- [x] run tests — must pass before task 5

### Task 5: Build the extractor registry with PDF and DOCX, and teach `FakeClient` to fetch

**Files:**
- Create: `grepogram/extract.py`
- Create: `tests/test_extract.py`
- Create: `tests/fixtures/sample.pdf`, `tests/fixtures/sample.docx`
- Modify: `pyproject.toml`
- Modify: `tests/fakes.py`

- [ ] add a `media` optional extra (`pypdf`, `python-docx`,
      `pyobjc-framework-Vision; sys_platform == 'darwin'`) **and** add `pypdf` + `python-docx` to
      `[dependency-groups] dev` — CI installs no extras (ci.yml:36,50), so without this the tests
      cannot import them; then `uv lock` and commit the lockfile
- [ ] add `ignore_missing_imports` overrides for `pypdf`, `docx`, `Vision`, `Quartz`, or strict
      mypy fails in both CI jobs
- [ ] define `Extractor = Callable[[Path], str]` and build the registry through a
      `_build_registry() -> dict[MediaKind, Extractor]` the tests can re-invoke under `monkeypatch`
      — an import-time constant cannot be re-derived, and Task 6 must prove the registry gains and
      loses `photo` with availability
- [ ] make the `document` entry a **dispatcher**: `MediaKind` has one `document` member for both
      formats (models.py:15-28, sync.py:250-275), so choose by `media_filename` extension and
      confirm with magic bytes; an unrecognised document is `ExtractError`
- [ ] cap extracted text at `EXTRACT_MAX_CHARS = 4000` — after Task 2 an oversized single message
      forms a window of its own, so this bounds that window at a few times `window_max_chars`
      (1500) rather than letting a 400-page PDF become one enormous unit
- [ ] leave `voice` and `video_note` unmapped, with a comment naming v0.3.0
- [ ] give `FakeClient` `get_messages(chat_id, ids=[...])` and `download_media(...)`, with a
      deleted id coming back as `None` — Tasks 7 and 13 depend on this. `iter_messages` already
      implements `ids=` this way (tests/fakes.py:352-356), so `get_messages` is a thin wrapper
- [ ] write tests: the PDF and DOCX fixtures round-trip; extension/magic dispatch picks the right
      extractor and rejects a mismatch; a corrupt file raises `ExtractError`; an unmapped kind is
      absent; the length cap truncates; `FakeClient`'s two new methods behave as documented
- [ ] run tests — must pass before task 6

### Task 6: Add the macOS Vision OCR extractor behind a seam

**Files:**
- Modify: `grepogram/extract.py`
- Modify: `tests/test_extract.py`

- [ ] implement `ocr_image(path)` through `pyobjc-framework-Vision` (`VNRecognizeTextRequest`,
      accurate level)
- [ ] query `supportedRecognitionLanguages` and request only what is supported — **Vision gained
      Russian only in macOS 15**, and requesting an unsupported language fails the whole request;
      fall back to the supported subset rather than failing
- [ ] register it for `photo` only when the import succeeds **and** the platform is darwin;
      otherwise leave `photo` unmapped so the pass marks it unsupported instead of failing
- [ ] put the Vision call behind **one** module-level indirection a test replaces, so the suite
      never needs a Mac with the extra installed — and keep the un-runnable surface to that single
      call, since the repo has no coverage config or `# pragma: no cover` and Task 18 pins coverage
      at the 99% baseline
- [ ] log once, at debug, when OCR is unavailable and why — never on stdout
- [ ] write tests: the registry gains `photo` when the seam reports available and lacks it when
      not; the fake seam's text reaches the caller; a Vision failure becomes `ExtractError`; an
      unsupported language is dropped rather than raising
- [ ] run tests — must pass before task 7

### Task 7: Add the bounded extraction pass and its CLI command

**Files:**
- Create: `grepogram/media.py`
- Modify: `grepogram/cli.py`
- Modify: `grepogram/db.py`
- Create: `tests/test_media.py`
- Modify: `tests/test_cli.py`

- [ ] add `db.messages_pending_media(conn, limit)` and two distinct writers: `db.set_media_text`,
      which stores the text **and** flags `indexed = 0`, and `db.set_media_state`, which writes only
      the state byte and **leaves `indexed` alone**
- [ ] implement `media.run(conn, client, cfg, budget)` following the flow in Technical Details —
      it **re-fetches each pending message by id** (`client.get_messages`) because Telethon cannot
      download from a stored row, and reads the size from the re-fetched media, since no size
      column exists
- [ ] `--budget` is **seconds**, like `SyncBudget`; respect the flood-wait threshold as `sync` does
- [ ] resolve the offline states **before** touching the network, as three bulk `UPDATE`s (no
      extractor → `2`, disabled → `5`, re-enabled → back to `0`): both depend only on the stored
      `media_kind`, and leaving them in the loop would re-fetch tens of thousands of messages over
      the network purely to write a state byte — most kinds (`video`, `sticker`, `audio`,
      `webpage`, `poll`, `contact`, `location`, `other`, plus the deferred `voice`/`video_note`)
      have no extractor at all. These updates change no rendered text, so they must **not** flag
      `indexed = 0`: doing so would flag tens of thousands of rows across every chat and hand
      `sync.py:1326`'s unbudgeted loop the exact backlog this plan forbids
- [ ] take the `SyncLock` for the whole pass and report `SyncInProgress` as a clean error — this
      writes `media_state`, `extracted_text` and `indexed`, and `db.Connection`'s lock only
      serialises within one process (CLAUDE.md invariant; `embed_cmd` takes it at cli.py:280)
- [ ] add `grepogram extract [--budget N] [--retry-failed]` modelled on **`sync_cmd`**, not
      `embed_cmd`: this pass needs a Telegram client, so it needs `_require_api_keys`,
      `tg.make_client`, `tg.connected` and the `AuthRequired` / `RPCError` handling
- [ ] delete the temp file in a `finally`; commit per batch so a flood wait keeps what it earned
- [ ] write tests: each state transition; the size cap skips before downloading; a disabled kind
      lands on `5` and is re-queued when re-enabled; **the offline states are resolved with no
      client call at all and change no `indexed` value**; a failed extraction is retried only with `--retry-failed`; the temp file
      is always removed; the budget stops the pass mid-chat and the next run resumes; a held
      `SyncLock` is a clean error; no API keys is a clean error, not a traceback
- [ ] run tests — must pass before task 8

### Task 8: Feed extracted text into the rendered unit, including closed windows

**Files:**
- Modify: `grepogram/units.py`
- Modify: `grepogram/media.py`
- Modify: `tests/test_units_windows.py`
- Modify: `tests/test_media.py`

- [ ] use `extracted_text` in `render_line` (units.py:44-53): a caption keeps its text and gains
      the extracted text; a caption-less photo renders it in place of the bare `[photo]`
- [ ] keep the placeholder when `extracted_text` is empty or absent, so nothing regresses
- [ ] keep the marker visible — a reader must be able to tell a machine read this off an image
- [ ] add `units.invalidate_units_for(conn, chat, cfg, rows)` taking **`Sequence[MessageRow]`**,
      not ids: it needs each row's topic, and Task 12 calls it after deleting the rows, so the
      caller must carry what it read
- [ ] take the topic from **`units.window_topic(chat, msg)`**, never `msg.topic_id` — non-forum
      windows carry `topic_id = NULL` while Telegram populates `messages.topic_id` outside forums
      for legacy threads (CLAUDE.md measures 124 of 13,227 rows in a real supergroup), and
      `_WINDOW_SCOPE` is `topic_id IS ?` (db.py:45), so the raw value finds no window and the
      invalidation silently does nothing. Every fixture chat has `topic_id = None`, so this passes
      in tests and fails on real data
- [ ] `db.containing_unit` (db.py:1238-1260) returns a **window or a post, never a thread**. Also
      take `db.threads_touching(conn, chat.id, msg_ids)` as stale, and a channel's post thread
      (`units._post_thread`, units.py:306-316) alongside its post unit, or a message's text survives
      in every thread unit quoting it
- [ ] **`rows` supply only `msg_id`, `chat_id` and `window_topic` — never rendered text.** Every
      message the primitive renders must be **re-read from the database**. `_chain_tops`
      (units.py:504-525) appends the *passed* row as a thread top when its parent is unstored and
      `build_threads` renders it at the head (units.py:495-503); `_rebuild_posts` (units.py:404)
      renders `changed` directly. Handing it the rows as-read would make Task 12 **resurrect a
      deleted message into a fresh thread or post unit**, undoing the deletion in the index, and
      would make Task 8 re-render a thread-rooted photo with its stale `[photo]` placeholder
- [ ] a stale thread whose root is no longer stored is **dropped, not rebuilt** — which is also
      what makes Task 12's "a unit left with no messages is dropped" true for threads
- [ ] call it **once per chat per batch**, on the minimum start across the rows —
      `db.windows_from` (db.py:1226-1235) returns every window from there to the end of the chat, so
      a per-message call would re-cut and re-embed the chat's tail once per extracted photo
- [ ] hand the returned `UnitDelta` to `index.index_units` from the **caller** (units.py cannot
      import index — index.py:48 is a cycle), or the chat's `unit_fts` stays torn until the next sync
- [ ] call it from the extraction pass for every message whose `extracted_text` changed. **Without
      this the feature is inert for existing history**: `_recut_start` (units.py:436-460) returns
      `None` when every changed message sits in a closed window — its own docstring says closed
      windows are never re-cut — and `on_chat_synced` then calls `mark_indexed` anyway, so the flag
      clears and the extracted text is silently discarded. Flagging `indexed = 0` is not enough,
      and the `RECIPE_VERSION` bump does not save it either: the one-time re-cut fires on the first
      sync after upgrade, long before `grepogram extract` has worked through the backlog
- [ ] Task 12 reuses this primitive for deletions — build it here, once
- [ ] bump `units.RECIPE_VERSION` to 3 so units cut before this render change pick the text up
- [ ] write tests: rendering with and without `extracted_text`, with and without a caption; **a
      photo inside an already closed window gets its OCR text into that window's unit text** (the
      test that matters); a group chat, not just a channel post; **a non-forum chat carrying a
      legacy `topic_id`**; a thread unit quoting the message is rebuilt too; a batch of 50 extracted
      messages in one chat re-cuts its tail once, not 50 times; **a photo that heads a reply thread
      gets its text into the thread unit, not the stale placeholder**; the recipe bump re-cuts
- [ ] run tests — must pass before task 9

### Task 9: Add `sources prune`

**Files:**
- Modify: `grepogram/sources.py`
- Modify: `grepogram/cli.py`
- Modify: `tests/test_sources.py`
- Modify: `tests/test_cli.py`

- [ ] add `sources.prunable(cfg, conn, folders)` returning indexed chats whose folder source no
      longer lists them, with a reason for each. `folders` is the resolved folder membership from
      `sources.source_dialogs` (sources.py:616-627) — state its type in the signature
- [ ] this pass **needs Telegram**: knowing what a folder holds *now* requires a connected client,
      so the command needs `_require_api_keys`, `tg.make_client` and `tg.connected` like
      `sync_cmd`, not the offline shape of `sources rm`
- [ ] resolve over the network **first, then** take the `SyncLock` for the deletion — the ordering
      `sources_rm` uses (`with sync.SyncLock(paths), config.ConfigLock(paths):`, cli.py:575-589).
      Note `sources_add` takes no `SyncLock` at all, so it is not the precedent to copy. There is no
      config to save: a chat that left a folder changes no `[[sources]]` entry
- [ ] a source that **fails to resolve** must abort the prune for that source with a clear message.
      `resolve_sources` logs and skips an unresolvable source (sources.py:588-591), so deriving
      "the folder no longer lists them" from a failed resolution would offer to delete a user's
      whole indexed history after one transient `RPCError`
- [ ] add `grepogram sources prune [--dry-run]`, defaulting to showing what would go and requiring
      confirmation — deleting indexed history is not a silent operation
- [ ] leave a discussion group a channel still links, and say why it was kept
- [ ] never prune a chat whose source id starts `import:`, which by definition has no dialog
- [ ] **no MCP tool** — pruning deletes indexed history and stays a deliberate CLI action
- [ ] write tests: a chat removed from a folder is prunable; one still in it is not; a linked
      discussion group is kept; `--dry-run` changes nothing; an imported chat is never listed; **a
      source that fails to resolve prunes nothing**; a held lock is a clean error
- [ ] run tests — must pass before task 10

### Task 10: Put reactions on units and keep them fresh

**Files:**
- Modify: `grepogram/units.py`
- Modify: `grepogram/sync.py`
- Modify: `grepogram/db.py`
- Modify: `tests/test_units_windows.py`
- Modify: `tests/test_sync.py`

- [ ] fill `UnitRow.reactions` in `units._unit` by summing `reactions_total` over its messages —
      **and in `units._post_thread` (units.py:306-316), which constructs `UnitRow` directly and
      would otherwise take the default 0**, so a channel's most-reacted post threads get no bonus
- [ ] add `db.refresh_unit_reactions(conn, chat_id, msg_ids)` recomputing totals with a direct
      `UPDATE` over `json_each(units.msg_ids)`. **`msg_ids` here are Telegram `msg_id`s**, the
      space `units.msg_ids` stores (units.py:96) — everything on the `edit_refetch` path carries
      `messages.id` rowids instead, so the caller must convert. In a fixture chat rowid and
      `msg_id` both start at 1 and coincide, which is exactly how this ships broken
- [ ] call it from the `edit_refetch` path, **independent of the unit rebuild** — `_content_key`
      (units.py:559-567) excludes reactions so `_apply` keeps the stored row, and a closed window
      is never re-cut (units.py:456-457); a rebuild-driven refresh is inert by construction
- [ ] **do not add `reactions` to `_content_key`.** It looks like the fix and is the opposite of
      one: every reaction change would invalidate, delete, re-insert and re-embed the unit, with
      `edit_refetch = 200` messages per chat per sync, forever. `_apply` keeping the stored row is
      what preserves the refreshed total
- [ ] scope the refresh to the unit's own chat and note in the docstring that a post thread
      reflects the post's messages, its comments' reactions being carried by the discussion group's
      own window units
- [ ] bump `units.RECIPE_VERSION` to 4
- [ ] write tests, **in a chat where rowid and `msg_id` differ** so the id-space confusion cannot
      hide: a unit's total is the sum of its messages; **a reaction count that changes after the
      unit was cut is picked up by the refresh** (the test that matters); a closed window's total
      updates without the unit being re-cut; **a post thread carries a non-zero total**; zero when
      none react; a round-trip
- [ ] run tests — must pass before task 11

### Task 11: Apply a bounded reaction bonus on a defined score scale

**Files:**
- Modify: `grepogram/search.py`
- Modify: `grepogram/models.py`
- Modify: `grepogram/config.py`
- Modify: `README.md`
- Modify: `tests/test_search_hybrid.py`

- [ ] add `SearchCfg.reaction_weight: float = 0.05` plus its `TEMPLATE` and README entries
- [ ] **min-max normalise the rerank scores across the candidate set for ordering only**, then add
      `reaction_weight * log1p(reactions) / (1 + log1p(reactions))`. `as_scores` (rerank.py:156-161)
      returns raw logits spanning several units in production while `FakeReranker` returns `[0,1]`,
      so an un-normalised bonus is a no-op in production and a tuned-on-the-fake test proves nothing
- [ ] the bonus must land **in `Hit.score` itself**. "Ordering only" is a provable no-op:
      `search.py:491` is `dedup(_hits(...), ...)[:k]`, and `dedup` (search.py:265-279) opens with
      `sorted(hits, key=lambda h: -h.score)` — whatever order `_rerank` returns is discarded and
      rebuilt from `Hit.score`, which also decides which of two overlapping hits `dedup` keeps and
      where `[:k]` cuts
- [ ] say in README that `score` is therefore a within-result-set number, not comparable across
      queries — it is shown in the CLI output and the MCP result (README:215, 244)
- [ ] when the range is degenerate — a single candidate, or `hi - lo` below a small epsilon —
      **skip the bonus**: `(s - lo) / (hi - lo)` would divide by zero, and an exact-equality test
      would make a `1e-9` spread reorder the whole set by reactions alone
- [ ] apply it only when reranking **actually ran**: `_rerank` (search.py:609-635) returns the fused
      RRF scores untouched when the reranker cannot load, and those top out near `0.016` where this
      bonus would dominate outright. `_rerank` returns the same shape either way, so **give it a way
      to report that it scored** and key on that — **not** on `mode`, since `search()` reranks in
      every mode unless `rerank=False` (search.py:489)
- [ ] `reaction_weight = 0` must reproduce the previous ordering exactly
- [ ] document the bonus in README's How Search Works section
- [ ] write tests **through `search()`, not `_rerank`**, so `dedup` and `[:k]` are exercised: two
      near-tied units reorder by reactions in the returned hits; a far-behind unit does not overtake
      a relevant one at the default weight; **the same assertions hold on a raw-logit scale as on
      `[0,1]`**; a single-candidate set does not raise; a near-degenerate spread does not reorder by
      reactions alone; no bonus when the reranker did not run; zero weight reproduces the previous
      hits exactly
- [ ] run tests — must pass before task 12

### Task 12: Notice deletions during the edit-refetch pass

**Files:**
- Modify: `grepogram/sync.py`
- Modify: `grepogram/units.py`
- Modify: `grepogram/db.py`
- Modify: `tests/test_sync.py`

- [ ] detect deletions as a **set difference**: the stored ids inside the id range
      `_refetch_edits`'s iteration actually covered, minus the ids it returned.
      `client.iter_messages` (sync.py:766) omits deleted messages rather than yielding an empty
      slot, so there is nothing to test for emptiness here
- [ ] bound it to the covered range only — a stored id outside what the iteration reached is not
      evidence of anything
- [ ] add `db.delete_messages(conn, chat_id, msg_ids)` removing the rows and their `msg_fts`
      entries in one transaction
- [ ] reuse `units.invalidate_units_for` from Task 8 — the same primitive, since `_recut_start`
      (units.py:436-460) returns `None` for a closed window and `rebuild_for_chat` (units.py:377)
      can no longer reach a deleted row by id
- [ ] the order is **read the rows → `delete_messages` → `invalidate_units_for(conn, chat, cfg,
      rows)`**, all in one transaction: the primitive takes `MessageRow`s because the topic it needs
      lives on rows that no longer exist by then — and it must re-read everything it renders, or a
      deleted message that heads a thread is rendered straight back into a new unit from the row
      passed in (units.py:504-525, 495-503)
- [ ] **skip rows with `comment_of_chat_id IS NOT NULL` and leave them to Task 13.** A *link-only*
      discussion group is never in `resolve_sources`' output (sources.py:575-612) so this pass never
      sees it — but a group listed directly by a folder or a `chat:` entry **is** a source chat, and
      `_refetch_edits` does run for it. Deleting its comment here would re-cut its window while the
      channel's post thread keeps the text forever, since no `json_each` over `units.msg_ids` can
      reach a comment (CLAUDE.md). The explicit skip is what makes the carve-out true
- [ ] a unit left with no messages is dropped rather than rebuilt empty
- [ ] write tests: a deleted message disappears and its containing window is re-cut; a deletion
      inside a closed window is handled; a unit that loses every message is dropped; an id outside
      the covered range is never removed; **a deleted message that heads a reply thread does not
      come back in a rebuilt thread unit**; a service message (which `run.map` never stores) is not
      mistaken for a deletion
- [ ] run tests — must pass before task 13

### Task 13: Add `grepogram prune-deleted` for a full sweep

**Files:**
- Modify: `grepogram/sync.py`
- Modify: `grepogram/db.py`
- Modify: `grepogram/cli.py`
- Modify: `tests/test_sync.py`
- Modify: `tests/test_cli.py`

- [ ] implement a resumable sweep: stored ids in batches of 100 through
      `client.get_messages(chat_id, ids=[...])`, oldest first
- [ ] here an id that comes back `None` / `MessageEmpty` **is** the deletion signal — this is the
      pass the empty-slot check belongs to, unlike Task 12
- [ ] guard the false positive: anything Telegram declines for another reason must not be removed
- [ ] store the cursor as a `meta` key `prune_sweep:<chat_id>` holding a **Telegram `msg_id`**, not
      a `messages.id`: `messages.id` is `INTEGER PRIMARY KEY` without `AUTOINCREMENT` (db.py:81), so
      rowids freed by a sweep are reused by the next insert and a rowid cursor is unstable across
      exactly the operation that writes it. No new schema — a v7 migration here was never planned
- [ ] respect `--budget` (seconds) and the flood-wait threshold exactly as `sync` does
- [ ] take the `SyncLock` for the whole sweep; `SyncInProgress` is a clean error
- [ ] reach a channel's discussion group too, so a deleted **comment** is caught here — post threads
      carry no comment ids in `msg_ids`, so invalidate the post thread through
      `comment_of_chat_id` / `comment_of_msg_id` (CLAUDE.md is explicit that nothing else finds it)
- [ ] add `grepogram prune-deleted [--chat X] [--budget N]`, reporting removals and how far it got.
      **No MCP tool** — like `sources prune`, deleting indexed history stays a deliberate CLI action
- [ ] write tests: a sweep removes exactly the deleted ids; a budget stops it and the next run
      resumes from the cursor; a flood wait keeps what the run earned; a non-deletion error removes
      nothing; a deleted comment invalidates the channel's post thread; a held lock is a clean error
- [ ] run tests — must pass before task 14

### Task 14: Parse a Telegram Desktop export

**Files:**
- Create: `grepogram/tdesktop.py`
- Create: `tests/test_tdesktop.py`
- Create: `tests/fixtures/tdesktop_export.json`

- [ ] parse `result.json` (and a single-chat `messages.json`) into `MessageRow`s: id, date, sender,
      reply, text runs (the `text` field is a list of strings and entity dicts), media kind
- [ ] map the export's chat types onto grepogram's, and its ids onto the **marked** ids the rest of
      the code uses — an export writes bare ids for channels
- [ ] tolerate a truncated or partial export: report what was skipped, never raise mid-file
- [ ] write tests over a small hand-written fixture: text runs flatten correctly, replies survive,
      a service message is skipped, media becomes the right `media_kind`, a bare channel id becomes
      the marked form, a malformed entry is reported and skipped
- [ ] run tests — must pass before task 15

### Task 15: Add `grepogram import`

**Files:**
- Modify: `grepogram/cli.py`
- Modify: `grepogram/sources.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_sources.py`

- [ ] add `grepogram import <dir> [--chat-title X]`, storing through `db.upsert_messages` so units,
      FTS and vectors follow the normal path
- [ ] mark the chat `unavailable = 1`, `last_msg_id = 0` and source id `import:<slug>`
- [ ] refuse to import over a chat already synced from Telegram, naming it
- [ ] refuse `sources add` for a chat that is already imported — `db.upsert_chat` (db.py:553-574)
      overwrites `source_id` unconditionally, so without this guard `import:<slug>` silently
      becomes `chat:@x` and Task 9's prune protection evaporates. **`sources_add` (cli.py:470-511)
      opens no database connection at all** — it uses `Paths.from_env()` and `_load_config`, never
      `_load()` — so this task must add one, and check the MCP `sources_add` tool the same way
- [ ] index what was imported at the end of the command, so it is searchable immediately
- [ ] write tests: an import creates a searchable chat; a second import of the same export is
      idempotent; importing over a live chat is refused; `sources add` over an imported chat is
      refused and the source id survives
- [ ] run tests — must pass before task 16

### Task 16: Prepare the repository to be public

**Files:**
- Create: `CONTRIBUTING.md`
- Create: `.github/ISSUE_TEMPLATE/bug_report.md`
- Modify: `README.md`

- [ ] run `gitleaks detect --log-opts="--all"` over the full history and **report the verbatim
      result in the task output** — this gates every Post-Completion step
- [ ] grep the history for the user's `api_id` / `api_hash` shape and for absolute home paths, and
      report anything found rather than rewriting history unasked
- [ ] write `CONTRIBUTING.md`: the uv setup, the four gates, the commit convention, and that tests
      never touch the network
- [ ] add a bug-report issue template asking for the grepogram version, the macOS version and
      whether the extras are installed
- [ ] update README's install instructions for a public repository, and its Roadmap (whisper
      transcription is what remains)
- [ ] no tests apply — verification is the scan output and the rendered files
- [ ] run the full gate — must pass before task 17

### Task 17: Add the PyPI publish job to the release workflow

**Files:**
- Modify: `.github/workflows/release.yml`

- [ ] `release.yml` today is a **single job** that builds and releases. Either add the publish step
      to that job (simplest, keeps `dist/` in place) or split into build + publish jobs wired with
      `upload-artifact` / `download-artifact` — decide and say which in the commit body; do not
      assume a second job can see `dist/`
- [ ] use PyPI trusted publishing (`pypa/gh-action-pypi-publish`, `id-token: write`, environment
      `pypi`)
- [ ] gate it on a repository variable so a tag never publishes until the trusted publisher exists —
      an unconfigured run must **skip**, not fail
- [ ] keep the GitHub release step unchanged and independent of whether PyPI publishing ran
- [ ] verify with `actionlint .github/workflows/release.yml` (installed at `~/go/bin/actionlint`);
      `gh workflow view` reads the remote and cannot check an unpushed edit
- [ ] no unit tests apply; record the actionlint output in the commit body
- [ ] run the full gate — must pass before task 18

### Task 18: Verify acceptance criteria

- [ ] verify all eight Overview items are implemented and reachable from the CLI, and from MCP
      where they belong (`sources prune` and `prune-deleted` deliberately are not)
- [ ] verify the re-index happens exactly once for a v0.1.1 upgrader: a database with rows and no
      recorded recipe re-cuts, ends at `RECIPE_VERSION = 4`, and a second sync re-cuts nothing
- [ ] verify a short-budget run never starts a re-cut and never empties a chat's units outside its
      own transaction
- [ ] verify graceful degradation with the `media` extra absent and on a non-darwin platform
- [ ] verify `reaction_weight = 0` reproduces v0.1.1 ordering exactly
- [ ] run the full suite: `uv run pytest`
- [ ] run the slow suite: `HF_HUB_OFFLINE=1 uv run pytest -m slow`
- [ ] run `uv run ruff check . && uv run ruff format --check . && uv run mypy`
- [ ] verify coverage has not dropped below the **v0.1.1 baseline of 99%** (3,954 statements, 29
      missed), measured with `uv run pytest --cov=grepogram`

### Task 19: [Final] Update documentation and release

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `grepogram/__init__.py`

- [ ] document the extraction pass, `[media]`, `sources prune`, `prune-deleted`, `import` and the
      reaction bonus in README, including the one-time re-index on upgrade
- [ ] rewrite the Known Limitations bullet on deleted messages and edits (README.md:571-575), which
      Tasks 12 and 13 falsify
- [ ] update README's Files and Privacy section — extraction writes temporary files and OCR reads
      image content, all locally
- [ ] add the new invariants to CLAUDE.md: the recipe-version contract **including that units are
      never dropped globally and a short-budget run never starts a re-cut**; that extraction is a
      network pass that never blocks a sync; that an extractor degrades rather than fails; and that
      unit reactions are refreshed directly because `_content_key` cannot see them
- [ ] bump `__version__` to `0.2.0`
- [ ] run the full gate — this task modifies source
- [ ] move this plan to `docs/plans/completed/`

## Post-Completion

*Items requiring manual intervention or external systems — no checkboxes, informational only*

**Gated on Task 16's secret scan coming back clean.**

**Manual verification against real data** (nothing below can be tested in CI):
- run `grepogram extract` over the live index and check OCR quality on real Russian and English
  photos — recognition-language support is the thing most likely to be wrong, and Russian needs
  macOS 15
- confirm the one-time re-cut and re-embed completes in the expected ~1 hour on 47k units, that
  search keeps working chat by chat while it runs, and that quality does not regress afterwards
- run `prune-deleted` over one chat and confirm nothing live is removed
- import a real Telegram Desktop export and search it

**External system updates:**
- flip the GitHub repository to public (the user's own action; the secret scan must be clean first)
- create the PyPI project and configure trusted publishing for `nnemirovsky/grepogram`, environment
  `pypi`, then set the repository variable that ungates the publish job
- tag `v0.2.0` and confirm the release workflow publishes both the GitHub release and PyPI
- enable branch protection on `main` once the repository is public — rulesets are unavailable on a
  private repository under the current plan, which is why v0.1.x relied on convention
