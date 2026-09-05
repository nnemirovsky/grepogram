# CLAUDE.md

grepogram: local hybrid search over opt-in Telegram chats, served to Claude Code over MCP.
Python 3.12 pinned, `uv` only (no pip, no global installs), developed on macOS.

## Commands

- `uv sync --managed-python --all-extras --all-groups` — full environment (the `dense` extra
  brings torch and sentence-transformers). `--managed-python` is not decoration: sqlite-vec is a
  loadable extension, and uv would otherwise build the venv on whichever `python3.12` it finds
  first — python.org's macOS build and Apple's system Python are compiled without
  `--enable-loadable-sqlite-extensions`, so `db.connect` refuses them (`ExtensionsUnsupported`).
  CI sets `UV_MANAGED_PYTHON: "1"` for the same reason: the macOS runner's
  `/usr/local/bin/python3.12` is the python.org build and uv prefers it over a download.
- `uv run pytest` — the suite: in-memory SQLite, fake models, no network; `slow` tests are
  deselected by `addopts`
- `HF_HUB_OFFLINE=1 uv run pytest -m slow` — real `bge-m3` and reranker; both must already be in
  the Hugging Face cache
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` — lint, formatting and
  strict typing over `grepogram/` and `tests/` (`[tool.mypy] files`)
- `uv run grepogram --help`, `uv run grepogram-mcp` — the CLI and the MCP server
- all checks pass before every commit; CI (`.github/workflows/ci.yml`) runs them with
  `--group dev` only, `GREPOGRAM_FAKE_MODELS=1` and `HF_HUB_OFFLINE=1`
- `uv.lock` is committed and CI installs with `--locked`: after touching dependencies run
  `uv lock` and commit the lockfile
- the version lives in `grepogram/__init__.py` only (`__version__`, read by hatch and
  `grepogram --version`)

## Commit convention

Scoped Conventional Commits with a lowercase description: `feat(sync): fetch messages
incrementally`, `docs(readme): write setup and usage`, `test(mcp): assert stdout stays empty`.
The scope is required. One logical change per commit. No `Co-Authored-By` or other trailers, and
never change the git identity.

## Rules

- stdout is the MCP protocol. Never `print`. Log through `logging` (stderr plus a rotating file,
  `grepogram/log.py`); `typer.echo` only in `cli.py`, with `err=True` for diagnostics. `mcp.main()`
  and every tool body redirect stdout to stderr, and `tests/test_mcp.py` asserts stdout stays
  empty.
- Message text never reaches the log above DEBUG; pass it through `log.redact()`.
- Message mapping (`sync.map_message`) reads raw TL attributes only — `msg.message`, `msg.media`,
  `msg.reply_to`, `msg.fwd_from`, `msg.reactions`, `msg.from_id`, `msg.post`, `msg.date`,
  `msg.edit_date` — never the client-bound helpers (`msg.text`, `msg.file`, `msg.sender`,
  `msg.chat`), so a message built without a client maps exactly like one Telethon yields.
- Never `async with client` on a Telethon client (it calls `start()` and prompts on stdin); use
  `tg.connected(client)`. Only `grepogram auth` opens the session file for writing
  (`tg.make_login_client`); every other client works on an in-memory copy (`tg.make_client` →
  `tg.load_session`), because two Telethon clients on one session database block each other and
  fail with `database is locked`. The MCP server builds a fresh client per Telegram-using tool
  call (`AppState.telegram()`): Telethon caches the authorization check per instance and
  concurrent calls must never share a connection one of them will close. Syncs in the server go
  through `AppState.sync_lock`, config writes through `AppState.editing_config()`, and
  `sources_remove` / `sources rm` take the `SyncLock` like a sync does and save the config under
  it. `sync_all` resolves its sources from the config as it is once it holds the `SyncLock`:
  callers pass a loader (`state.config`, `functools.partial(config.load, paths)`), not the
  snapshot they started with, so a source removed while the model loaded is not fetched again.
- Every edit of `config.toml` is a read-modify-write under `config.ConfigLock` (a blocking flock
  on `config.lock` next to the file, held for milliseconds): the CLI goes through
  `config.update(paths, change)` or takes the lock explicitly inside its `SyncLock`, and
  `AppState.editing_config()` takes it inside the process-wide lock. Never save a config derived
  from a snapshot read before a network round trip; re-read under the lock and apply the delta
  (`sources.with_source`, drop by id). Lock order is `SyncLock` → `ConfigLock` → thread lock.
- A model loads from the Hugging Face cache and nothing else. Both `BgeM3Embedder` and
  `BgeReranker` go through `embed.load_cached_first(load, what)`, which calls the
  sentence-transformers constructor with `local_files_only=True` and retries with the network
  only when `embed.not_cached` recognises the failure — transformers re-raises huggingface_hub's
  `LocalEntryNotFoundError` as a plain `OSError` about the connection, so the match walks
  `__cause__` / `__context__` and compares class *names*: huggingface_hub belongs to the `dense`
  extra and nothing outside it may be imported here. That retry is the first download and is
  logged at INFO; `HF_HUB_OFFLINE` set skips it, and every failure still reaches the caller as
  `ModelUnavailable`. Never call `SentenceTransformer` / `CrossEncoder` directly: the round trip
  they make for an already-cached model costs about 8.7 s per `grepogram search` on a reachable
  network and minutes behind a firewall that holds connections open.
- `db.MIGRATIONS` maps a schema version to the step that brings a database to it, and `_V5` —
  keyed by `db.BASE_VERSION` — is the whole schema as the code queries it. The numbering starts
  at 5 because it is an identity, not a count: development builds walked a database up through 1,
  2, 3 and 4, and a number one of them also wrote could not say a dev index apart from a finished
  one — `schema_version = 1` on the old chain names a `messages` table with no `indexed` and no
  `comment_of_*`. `db.migrate` decides every database explicitly, never by falling through: a
  file with no schema objects gets the whole schema and the version, one at `SCHEMA_VERSION` is
  used as it is, one between `BASE_VERSION` and `SCHEMA_VERSION` that `MIGRATIONS` holds every
  step for is walked up, and everything else — tables with no recorded version, a newer version,
  anything below `BASE_VERSION` (the dev chain's 1 through 4), a version no chain of steps
  reaches, a version `db.schema_version` cannot read at all — raises `SchemaError` telling the
  user to delete `index.db` and sync again. That last one is why `schema_version` classifies what
  it reads: a `meta` table of another program's shape and a recorded version that is not a number
  are `SchemaError`, so the handlers in `cli._load` and `mcp.main` give the rebuild hint instead
  of a traceback, while a locked or unreadable file keeps raising its own `sqlite3` error — it is
  not a schema this code can classify. `MIGRATIONS` has to run from `BASE_VERSION` to
  `SCHEMA_VERSION` without a gap and `db._missing_steps` is where both paths check it: a
  mis-keyed step is a bug in grepogram, and stamping an empty database at a version every
  existing one is refused at would hide it. No step transforms rows: an index is derived from
  Telegram and a rebuild costs one sync. **The schema is append-only now that v0.1.0 is tagged** —
  append a step above `BASE_VERSION` and leave `_V5` alone. `migrate()` applies every step from
  `BASE_VERSION` for a database with no schema objects, so a column added to `_V5` *and* to a step
  above it raises `duplicate column name` and fails `tests/conftest.py`'s shared fixture, which is
  the whole suite. v0.2.0's step `6` (`messages.extracted_text`, `messages.media_state`,
  `units.reactions`, the `messages_media_pending` partial index) is the model. That index's
  predicate carries `media_kind IS NOT NULL` as well as `media_state = 0`, or it would cover every
  row in the table forever and the pending query would stay a scan.
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
  `_sync_chats`'s unbudgeted deferred `index_pending` loop to drain. **A short-budget run never
  starts one**: below `RECUT_MIN_BUDGET_S` the pass logs and returns (an unlimited budget,
  `remaining is None`, always qualifies), writes no flag, and `search` re-derives the condition
  into `search.RECUT_PENDING` so a user who has only ever searched is told to sync. The floor
  sits between two numbers in other modules and `tests/test_mcp.py` pins both: below it
  `search.auto_sync_budget_s = 20`, so no `search` ever starts a re-cut, and above it the MCP
  `sync` tool's own `budget_s = 120` default, so an explicit `sync()` — as deliberate as
  `grepogram sync`, and bounded and resumable either way — does. **The re-cut touches no
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
- Work a sync hands to a worker thread goes through `sync._joined_to_thread`, never bare
  `asyncio.to_thread`: an `anyio` cancel scope (how the MCP server cancels a tool call) abandons
  the future rather than the job, and the `SyncLock` must not be released while a detached thread
  still writes. The join is a `threading.Event` — a cancelled scope raises out of every `await`,
  so it cannot be one — and it is **not** bounded: a bound would give the lock up over a live
  writer in exactly the case it exists for (a big rebuild, an embedding backlog), and the next
  process would start writing against a database this one has not finished with. What keeps the
  wait short is the `abort` callback: the embedding step is paced by `SyncBudget`, so a
  cancellation calls `budget.cancel()` and `index.embed_dirty_units` stops at the next batch;
  the indexing step is one transaction and ends on its own. Only a killed process detaches a
  writer, and `messages.indexed` plus `index.repair_unit_index` are what the next run repairs it
  with.
- A channel has at most one discussion group, and the partial unique index on
  `chats.discussion_of` is what says so — `db.get_discussion_chat` is a lookup, not a
  pick between rows. `db.set_discussion_chat` is the only way the link moves or clears
  (`upsert_chat` COALESCEs the column so re-resolving the group as a source chat never drops it);
  `sync.link_discussion_chat` calls it on every sync with what `GetFullChannelRequest` reports,
  so an unlinked or replaced group loses the link. Its messages stay — they are a real group's —
  while the post threads they fed are dropped right there with their index rows
  (`sync._drop_comment_units` → `db.drop_comment_units`) and the channel's posts that held them
  are flagged `indexed = 0` so the next rebuild cuts them again, with the new group's comments or
  with none. Leaving the threads to that rebuild would not do: nothing inside a thread names the
  group it quotes, only the link does, so a group deleted in between would leave them
  unreachable. The mapping goes in the same call: `db.drop_comment_units` clears
  `comment_of_chat_id` / `comment_of_msg_id` on every row of the group naming that channel
  (`db._clear_comment_mapping`). Left behind, the mapping would hand the old channel's comments
  to the next channel's post of the same number — post ids start at 1 in every channel. It is
  cleared by channel, not by which of its posts are still stored, so a comment on a post the
  channel has since dropped goes too. The whole transition is one `db.transaction` in
  `sync._relink_discussion` — the link that moves, the group's `source_id` and the flags of the
  posts it invalidates — so a killed process leaves all of it or none of it; never split those
  halves again. A group Telegram names but will not resolve still clears a link pointing at a
  *different* group (that one is demonstrably not the channel's any more), while a link to the
  very group that failed to resolve is left alone and retried next run.
- Who owns a discussion group's `source_id` is `sources.discussion_source_id`, and
  `sources_status`, `sources rm` and `sources._refuse_indirect` read the same rule: a group a
  source covers directly (a folder holding it, a `chat:` entry naming it) keeps that source; a
  group known only through a channel's link belongs to the source of the channel that links it
  *now*, so it moves along when another channel takes it over — removing the old channel then
  leaves it and removing the new one takes its comments along; a group a channel was unlinked
  from keeps the source it came in through until that source is removed.
- Never decide what a `chat:` source covers by comparing `source_id` strings. `chat =` takes an
  id, an `@username`, `https://t.me/<name>` and `t.me/c/<id>`, and all four are one chat:
  `sources.parse_target` folds them into a `Target`, and `sources._names_chat` / `_same_target`
  compare that against the `chats` row's `id` and `username` (case-insensitively). `_own_source`,
  `_same_chat` and `find_source`'s `_named_source` all go through them; a spelling-based
  comparison silently treats a directly configured group as indirect, which hands its rows to the
  channel's source. A fuzzy `chat =` value names no identity offline and matches nothing.
- `db.delete_chat` is a no-op for a chat this index does not hold — deleting an unknown id must
  not clear the `discussion_of` of a live group that names it — and for a stored one it removes
  the units of *other* chats that quote it: a channel's post threads carry the comments of its
  discussion group, so deleting a group drops those threads with their `unit_fts` and `unit_vec`
  rows and flags the posts `indexed = 0`. The flag alone is not enough — the channel may never
  resolve again, and the index must not answer with rows that are gone. `db.drop_comment_units`
  is that cleanup, and the single answer to "which units quote this group": `delete_chat` and
  `sync._drop_comment_units` both call it, so the threads never outlive the link that is the only
  thing tying them to the group (a thread lists the post in `msg_ids`, never a comment id — no
  `json_each` over `units.msg_ids` can find them). Deleting a channel clears the link of a group
  that outlives it (through `db.set_discussion_chat`, still the only way `discussion_of` is
  cleared) and runs the same cleanup for every group it unlinks; the group keeps every message it
  holds, its own windows, threads and forum topics among them, and only stops holding *comments*.
- `messages.topic_id` is Telegram's thread/topic id, and it is *only meaningful inside a forum*:
  there it is the topic root `units.window_topic` cuts windows by. Outside a forum Telegram still
  sets `reply_to.reply_to_top_id` for a legacy message thread, so the column is populated on such
  rows too (a real non-forum supergroup here: 124 of 13,227 messages) — `units.window_topic`
  returns `None` unless `chat.is_forum`, so those messages are windowed linearly and
  `db.containing_unit` finds their window through `lookup_topic=None`. Never treat a set
  `topic_id` as proof of a forum. `search.context` does scope by the column
  (`db.get_context_messages` filters on `topic_id IS`), so the context of such a message is
  bounded to its legacy thread rather than to the whole chat. The post a message comments on is
  `comment_of_chat_id` / `comment_of_msg_id`, NULL on every row that is not a comment.
  They cannot share a column: a discussion group can be a forum, and a topic root and a channel
  post are separate id spaces that both number from 1, so a group that is both would answer a
  comment read with a topic message and lose real topics to an unlink. Every comment read and
  every cleanup is keyed by the pair — `units.build_posts`, `search.thread`,
  `db.get_comment_messages`, `db.count_comment_messages`, `db.stored_comment_post_ids`,
  `db.drop_comment_units` — and `sync._fetch_comments` is the only writer of it. Nothing derived
  from a group's rows reads the pair (windows are cut per forum topic in `units.window_topic`,
  threads follow `reply_to_msg_id`), so clearing it needs no rebuild and drops no unit: a cleared
  comment is searchable through the window it was already in, in the same commit. Keep it that
  way — a cleanup that has to drop units to stay correct can strand a message until a later sync.
  `db.upsert_messages` COALESCEs both columns like `topic_id`, because the group's own history
  sync re-reads a comment with no comment relation on it.
- A partial batch keeps what it earned: `sync._store_batch` writes `set_chat_progress` in a
  `finally` and `_fetch_comments` stores its rows as it reads them, because a flood wait on one
  comment thread leaves the whole run. A thread is only requested while Telegram reports more
  replies than are stored (`db.count_comment_messages`).
- grepogram launches nothing. It has no `open`, no `subprocess` outside the tests, and no
  platform dependency on macOS beyond its default paths: a result carries links and a human or an
  agent clicks one. The `open_message` tool, `links.open_link` and the `tg://resolve` /
  `tg://privatepost` forms that only fed it were removed for that reason — a search answers with
  ten hits, so "open this one" was never the workflow, and a review probe that reached the real
  runner once launched Telegram and Safari with fixture links. Do not bring any of it back.
- `links.message_url` returns the form to show and cite in `Link.url` — `https://t.me/…` where
  Telegram has one, `tg://openmessage?…` for the private chats and legacy groups that have none —
  and `Link.fallback_url` carries `tg://user?id=` for a DM, which is what a desktop client
  actually opens. Both are display links; nothing consumes them but the reader. The bare channel
  id a `t.me/c/` link needs comes from `telethon.utils.resolve_id`, never from string surgery on
  the `-100` prefix: the mark is arithmetic (`-(1000000000000 + id)`), so a channel id below ten
  digits leaves zeros right behind that prefix and any lexical rule either swallows them or
  refuses the id — which took `search`, `thread` and `context` down for the whole chat.
  `sources.parse_target` builds the same mark arithmetically for `t.me/c/<id>`, so such ids reach
  the index by the front door.
- A `MessageView` names the chat it is in (`chat_id`), because a list of them can span two:
  `search.thread` follows a channel post with its discussion group's comments, and post ids and
  comment ids both number from 1, so `msg_id` alone names two different messages. The top-level
  `chat_id` of `mcp._messages_result` is the argument, not where every message lives; the tool
  docs, the server `INSTRUCTIONS` and README say to pass a message's own `chat_id` back.
- Windows are cut in `msg_id` order but rows do not always arrive that way (a channel stores
  comments in its discussion group before the group's own history gets there). `units._recut_windows`
  starts at the open window unless a changed message no window holds lies below it; then it
  re-cuts from the window before that message. Membership in `msg_ids`, not the id range, decides
  whether a window holds a message.
- `mcp` stays `<2`: `grepogram/mcp.py` targets the 1.x `FastMCP` API (2.x renamed it).
- Never instantiate `FastMCP` at module level; `mcp.build_server()` runs after `setup_logging()`
  because `FastMCP.__init__` calls `logging.basicConfig`, and `main()` lowers the `mcp` logger to
  WARNING (the lowlevel server logs every request at INFO).
- Every writer in `db.py` runs inside `db.transaction(conn)`. FTS and vec rows are keyed by the
  parent rowid (`messages.id`, `units.id`) and deleted by rowid, never by an UNINDEXED column.
  The one connection is shared across threads: `db.Connection` runs every statement to completion
  under a re-entrant lock and `transaction()` holds it from `BEGIN` to `COMMIT`; the connection is
  in autocommit mode, so a bare statement never leaves an implicit transaction open.
- `config.toml`, the session file and the lock files (`sync.lock`, `config.lock`) are written
  with `paths.PRIVATE_FILE_MODE` (0600) — `config.write_private`, `tg.prepare_session`,
  `paths.FileLock`; the directories grepogram creates get `paths.DIR_MODE` (0700) and an existing
  one (a user's own `GREPOGRAM_HOME`) is left as it is. Both cross-process locks derive from
  `paths.FileLock`: `SyncLock` sets `blocking = False` and its own `busy()`, `ConfigLock` blocks.
- Files end with a single newline; no trailing blank lines.

## Environment variables

- `GREPOGRAM_HOME=<dir>` — every file (`config.toml`, `config.lock`, `session.session`,
  `index.db`, `sync.lock`, `logs/`) under one directory. The `tmp_home` fixture in
  `tests/conftest.py` points it at a `tmp_path` subdirectory; tests must never touch the real
  `~/.config/grepogram`.
- `GREPOGRAM_FAKE_MODELS=1` — `embed.load_embedder` and `rerank.load_reranker` return
  `FakeEmbedder` (hashed bag of stems with a small RU/EN lexicon, 256-d) and `FakeReranker`. An
  autouse fixture sets it for every test; tests of the real loaders unset it themselves. It is
  read through `paths.env_flag` (`1`, `true`, `yes`, `on`), the one helper for such switches.
- `HF_HUB_OFFLINE=1` — huggingface_hub's own flag, honoured rather than owned:
  `embed.load_cached_first` never retries with the network while it is set, so a model missing
  from the cache degrades the search instead of downloading. CI sets it for the whole suite,
  which is why the `stubs` fixtures of `tests/test_embed.py` and `tests/test_rerank.py` unset it
  and each test decides it.

## Tests

- `tests/conftest.py` — the fixtures every module shares: `fake_models` and `clean_logging`
  (both autouse), `tmp_home`, `conn` (an in-memory index, migrated) and the `file_mode` helper.
  A module that needs more overrides `conn` by requesting it (`tests/test_filters.py`).
- `tests/fakes.py` — `FakeClient` (async `get_dialogs`, `iter_messages` with Telethon's offset
  semantics, `get_entity`, raw requests such as `GetDialogFiltersRequest`) driven by in-memory
  fixtures, plus `make_*` builders for TL entities and dialogs.
- `tests/fixtures/tl.py` — real Telethon `types.Message` objects built without a client (text,
  caption with photo, voice, document with filename, reply, forum topic, forward, service message,
  reactions, channel post).
- `tests/fixtures/chat_ru.py` — a 62-message bilingual corpus over two chats, loaded through the
  real pipeline (`upsert_messages` → `rebuild_for_chat` → `index_chat`); `PARAPHRASE` names the
  pair only the dense side can connect.
- Model-layer tests (`tests/test_embed.py`, `tests/test_rerank.py`) inject stub modules with
  `monkeypatch.setitem(sys.modules, "torch", …)` and `"sentence_transformers"` (`None` simulates
  an `ImportError`), so they pass without the `dense` extra.
- `@pytest.mark.slow` tests load the real models and are excluded by default.
- Little Snitch on this Mac holds outbound connections from Python until a human answers, so an
  in-process Hugging Face download hangs unattended. Fetch the model files with `curl` into the
  `hf_hub_download` cache layout (`~/.cache/huggingface/hub/models--<org>--<name>/` with
  `blobs/<etag>`, `snapshots/<commit>/<file>` as relative symlinks into `blobs/`, `refs/main`) and
  run the slow tests with `HF_HUB_OFFLINE=1`.

## Layout

`grepogram/`: `paths` (file locations, `FileLock`), `config` (TOML and `TEMPLATE`), `models`
(dataclasses and the shared `Literal`s: `ChatType`, `UnitKind`, `MediaKind`, `SearchMode`),
`db` (schema, migrations, accessors), `tg` (client, session, auth errors), `dialogs` (folders,
fuzzy matching), `sources` (targets, resolution, status), `sync` (fetch, mapping, lock, budget),
`units` (windows, threads, posts, incremental rebuild, `RECIPE_VERSION`), `stem` (tokenizer,
Snowball, FTS query), `index` (FTS and vec maintenance, KNN), `embed` and `rerank` (protocols,
fakes, bge models), `extract` (the extractor registry: PDF, DOCX, macOS Vision OCR), `media` (the
bounded extraction pass), `tdesktop` (Telegram Desktop export parsing),
`links` (deep links), `filters` (chat specs, dates, `resolve_chat` for the one-chat
readers), `search` (retrieval, fusion, dedup, readers), `cli` (typer app: `search` and the
`thread` / `context` readers beside `sources`, `sync`, `extract`, `embed`, `import`,
`prune-deleted`, `config`), `mcp` (FastMCP server with eight tools). The CLI and the MCP server
offer the same readers, and `--json` prints the document the matching tool returns. Four commands
are **CLI-only by design** and have no MCP tool: `sources prune` and `prune-deleted` delete
indexed history, `extract` is a long flood-exposed network pass, and `import` reads a directory
the server cannot see.

Plans live in `docs/plans/`, finished ones in `docs/plans/completed/`.
