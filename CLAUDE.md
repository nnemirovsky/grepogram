# CLAUDE.md

grepogram: local hybrid search over opt-in Telegram chats, served to Claude Code over MCP.
Python 3.12 pinned, `uv` only (no pip, no global installs), developed on macOS.

## Commands

- `uv sync --all-extras --all-groups` — full environment (the `dense` extra brings torch and
  sentence-transformers)
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
  existing one is refused at would hide it. Nothing has shipped, so no step transforms rows: an
  index is derived from Telegram and a rebuild costs one sync. Change the schema by editing `_V5`
  until the first release; after it, append a step above `BASE_VERSION` and leave `_V5` alone.
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
- `messages.topic_id` is a forum topic and nothing else; the post a message comments on is
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
- Never let a probe, smoke test or validation step call `links.open_link`, `links.run_command` or
  the `open_message` tool with the real runner: inject a recording runner. `GREPOGRAM_NO_OPEN=1`
  (set by the autouse fixture) makes `open_link` return the url without running anything and
  `open_message` report `opened: false`, so nothing under the test environment launches Telegram
  or a browser.
- `links.message_url` returns the `https://t.me` form in `Link.url` (what hits and message views
  show) and the `tg://` form in `Link.app_url` (`resolve` / `privatepost` / `openmessage`);
  `open_link` tries `app_url`, then `url`, then `fallback_url`, because `open https://t.me/…` on
  macOS lands in Safari, not in the Telegram app.
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
  autouse fixture sets it for every test; tests of the real loaders unset it themselves.
- `GREPOGRAM_NO_OPEN=1` — `links.open_link` returns the url without running `open` and the
  `open_message` tool answers `opened: false` with an `error` saying so. The same autouse fixture
  sets it for every test; the `open_link` / `open_message` tests unset it and inject a runner.
  Both flags are read through `paths.env_flag` (`1`, `true`, `yes`, `on`).

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
`units` (windows, threads, posts, incremental rebuild), `stem` (tokenizer, Snowball, FTS query),
`index` (FTS and vec maintenance, KNN), `embed` and `rerank` (protocols, fakes, bge models),
`links` (deep links, `open`), `filters` (chat specs, dates), `search` (retrieval, fusion, dedup,
readers), `cli` (typer app), `mcp` (FastMCP server with nine tools).

Plans live in `docs/plans/`, finished ones in `docs/plans/completed/`.
