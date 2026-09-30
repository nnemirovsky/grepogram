# CLAUDE.md

grepogram: local hybrid search over opt-in Telegram chats of one or more signed-in accounts,
served to Claude Code over MCP, with an opt-in research mode that finds chats beyond them.
Python 3.12 pinned, `uv` only (no pip, no global installs), developed on macOS.

## Commands

- `uv sync --managed-python --all-extras --all-groups` — full environment. The `dense` extra
  brings torch and sentence-transformers; the `media` extra brings pypdf, python-docx and
  pyobjc-framework-Vision. `--managed-python` is not decoration: sqlite-vec is a
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
- **CI installs no extras**, so the pure-Python half of an extra that the tests import must be
  mirrored into the `dev` group — pypdf and python-docx are there for that reason (CONTRIBUTING.md
  states the rule). What cannot be mirrored (torch, pyobjc) is what the tests must monkeypatch
  around: a module-level `import docx` in a test file makes the whole module uncollectable on CI
- `uv.lock` is committed and CI installs with `--locked`: after touching dependencies run
  `uv lock` and commit the lockfile
- the version lives in `grepogram/__init__.py` only (`__version__`, read by hatch and
  `grepogram --version`)

## Releasing

`.github/workflows/release.yml` runs on a `v*` tag. In order:

1. bump `__version__` in `grepogram/__init__.py`, commit, and push to `main`;
2. tag it `vX.Y.Z` and push the tag — the workflow refuses a tag whose name does not match
   `__version__`, so the two can never drift;
3. it runs `uv build`, uploads `dist/` as an artifact (before the release step, so a failed
   release still leaves the build to look at), and then publishes. **Either order of tag and
   release works**: the step asks `gh release view` first, and creates the release
   (`gh release create … --generate-notes --verify-tag`, so the tag must already be on the
   remote) when there is none, or attaches the build to the release a workstation already
   published (`gh release upload … --clobber`). `gh release create` has no update mode and
   answers 422 `already_exists` for a tag that already has a release, so running both would
   fail the job and, through `needs: release`, skip the PyPI upload entirely;
4. the `pypi` job is gated on the repository variable `PYPI_PUBLISH == 'true'` and `needs:
   release`, downloads that artifact and uploads through trusted publishing (the `pypi`
   environment, `id-token: write`, no API token). Until the variable is set the job skips, so a
   tag pushed before the PyPI publisher exists still cuts a GitHub release instead of failing the
   workflow.

`ci.yml` is the per-push suite and is separate from this.

`CONTRIBUTING.md` covers the same gates for an outside contributor, plus the PR flow; when the
two disagree, this file is what the repository's own work follows and the other should be
corrected to match.

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
- Message text never reaches the log above DEBUG; pass it through `log.redact()`. Neither does
  an invite or shared-folder link read out of someone's message (a private way in): a research
  refusal note is logged at DEBUG (`research._refuse_candidate`), and `mcp.tool_failure` logs a
  `ResearchError` or `SourceError` by its type above DEBUG, its text at DEBUG.
- Message mapping (`sync.map_message`) reads raw TL attributes only — `msg.message`, `msg.media`
  (its `webpage.url` included), `msg.reply_to`, `msg.fwd_from` (`from_id`, `channel_post`,
  `date`, `saved_from_peer`, `saved_from_msg_id`), `msg.entities`, `msg.reply_markup`,
  `msg.reactions`, `msg.from_id`, `msg.post`, `msg.date`, `msg.edit_date` — never the
  client-bound helpers (`msg.text`, `msg.file`, `msg.sender`, `msg.chat`), so a message built
  without a client maps exactly like one Telethon yields. It compares `from_id` with, and hands
  `reply_of`, `chat.peer_id`, never the row id. The structured forward origin is
  `sync.forward_origin` (channel post first, then the saved-from pair, else the author alone,
  nothing for a hidden account); the Telegram destinations a message names are `sync.links_of`
  (entity offsets are UTF-16 code units) normalized through `leads.normalize`, which drops every
  non-Telegram URL. **Every id a link, a message or a user spells is range-checked before it can
  be bound**: `leads.number` reads at most 19 ASCII digits, `leads.valid_peer` takes a signed
  64-bit marked id over a positive bare one, message ids stop at Telegram's `int` — so
  `t.me/c/99999999999999999999/5` names nothing instead of raising `OverflowError` at the first
  SQL bind and halting discovery for every session reading that chat. `sources.parse_target`,
  `research.target_identities` / `parse_approval` and `research_db._spelled` read ids through the
  same helpers, `research_db.get_candidate` / `get_session` answer `None` for an id past 64 bits,
  and `research.message_leads` skips one link that still fails to read. `MessageRow.links` is `None` for a row nobody read links for — one read back
  from the index, an import — and `upsert_messages` then leaves the stored `message_links` alone;
  a tuple, even an empty one, replaces them. `sync._differs` compares the links and the `fwd_*`
  columns in full (`sync._with_links` gives a stored row its links), so changed links alone
  re-store a row.
- Never `async with client` on a Telethon client (it calls `start()` and prompts on stdin); use
  `tg.connected(client, account)`, or `tg.connected_all(clients)` for several, which leaves a
  signed-out account out (in `tg.Accounts.skipped`) and raises only when none connects. Every account has
  a session file of its own (`paths.session_file_for`: `default` keeps `session.session`, any
  other account `sessions/<name>.session`) and every `tg` function takes the account, so an
  auth failure names whose session died (`tg.auth_hint(account)`). Only `grepogram auth` opens a
  session file for writing (`tg.make_login_client`), and it signs in on a 0600 copy
  (`tg.stage_login`) that replaces the account's file (`tg.commit_login`, an `os.replace`) only
  once `db.conflicting_account` says the index recorded no *other* Telegram user under that name: an
  account name is one Telegram user, since its scoped private chats, `chat_access` /
  `peer_cache` hashes and research grants are that user's. A name with no user recorded yet (a
  v0.2.0 `default`) takes whoever signs in; an index `auth` cannot read refuses the sign-in too
  (fail closed); a refused sign-in leaves the old session, logs the staged one out on Telegram's
  side (`tg.log_out`, only when this sign-in made the authorization: `SignedIn.fresh`) and
  deletes the copy. **Every Telegram-facing pass holds the same line through one check**,
  `sync.signed_in_user` (`get_me` against `db.conflicting_account`, raising `tg.OtherUser`, an
  `AuthRequired` whose hint is `accounts rm`), put by `sync.check_account` or — for a pass that
  goes on without the account, with the one wording of why — `sync.ask_account`: a sync
  (`always=True`, then `sync._record_account` records a first sign-in), `StoredPass.start`
  (`prune-deleted`, `extract`, `recapture-links`) and the folder read of `sources prune`
  (both through `sync.checked_accounts`, which leaves the account out with a warning), and `research.run` / `discover` (its pins, global searches and probes) and `grepogram leave`
  (before it resolves or asks; its question names the Telegram user), which refuse. A session
  swapped by hand never deletes, joins or asks as a user nobody chose. Every other client works on an in-memory
  copy (`tg.make_client` → `tg.load_session`; `tg.make_clients` for every account, reporting a
  missing or unreadable session of an account that owns a source in `Accounts.skipped`
  instead of raising), because two Telethon clients on one session database block each other and fail
  with `database is locked`. **That copy carries the data centre and the auth key and
  nothing else, so its entity cache starts empty and no chat is addressable by its stored id
  until something warms it.** A pass that walks a source list gets that for free
  (`sources.resolve_sources` → `DialogCatalog` → `get_dialogs`, whose peers Telethon writes into
  the session); a pass that walks `chats` rows instead — `sync.prune_deleted` and `media.run`,
  the two that re-fetch by id, both driven by `sync.StoredPass` — must call
  `sync.warm_peer_cache(client, chats, conn, account)` first, or every
  `client.get_messages(chat.peer_id, ids=…)` raises a plain `ValueError` that is not an
  `RPCError`. The warm-up seeds the account's own stored `chat_access.access_hash` first (a
  legacy group needs none) and reaches for the routes below only for what that left unseeded.
  The dialog list is not the whole account, and `warm_peer_cache` walks the two stored handles a
  `chats` row can carry for what it misses, **in this order**: `chats.username` through
  `client.get_entity`, for a public chat the account follows without joining (a sync only ever
  reaches such a chat through the `@name` its source names, and that handle is on the row —
  skipping it left `extract` and `prune-deleted` failing by-id requests for exactly the chats the
  warm-up was added for), and then `GetFullChannelRequest(chat.discussion_of)` for a link-only
  discussion group. The order is load-bearing: that request *names* the channel, so a channel
  outside the dialog list has to be resolved by its own handle before its group can be asked for.
  Those two are the whole of it — `source_id` is a source id and not a peer, `title` is fuzzy
  text matched against the dialog list this already read, and a legacy `PeerChat` id needs no
  access hash at all. What no route resolves costs that chat its turn with a warning, and only an
  `UnauthorizedError` is re-raised, ahead of every handler. The MCP server builds a fresh client per account per Telegram-using
  tool call (`AppState.telegram(account)`, `AppState.telegrams()` for every signed-in account,
  which skips an unusable one the way the CLI does and reports it): Telethon caches the authorization check per instance and
  concurrent calls must never share a connection one of them will close. Syncs in the server go
  through `AppState.sync_lock`, config writes through `AppState.editing_config()`, and
  `sources_remove` / `sources rm` take the `SyncLock` like a sync does and save the config under
  it. `sync_all` resolves its sources from the config as it is once it holds the `SyncLock`:
  callers pass a loader (`state.config`, `functools.partial(config.load, paths)`), not the
  snapshot they started with, so a source removed while the model loaded is not fetched again.
- **A chat's Telegram identity is `(scope, peer_id)`, and `chats.id` is only its row.**
  `peer_id` is Telethon's marked id. `scope` is `''` for a channel or supergroup — global ids and
  global message ids, so one row whichever account reaches it, fetched once — and the account
  name for a user, bot or legacy group, whose message ids are that account's own
  (`models.chat_scope(type, account)` is the one rule). **A shared row's `id` equals its
  `peer_id`**, and `db.upsert_chat` raises `ValueError` rather than break that, because
  `discussion_of`, `comment_of_chat_id`, `migrated_to` and `t.me/c/` links all live in channel id
  space. A scoped row takes `id = peer_id` when that id is free and the next synthetic id
  `>= db.SYNTHETIC_BASE` (`1 << 62`) otherwise, so two accounts' private chats with one person
  are two rows. Everything that talks to Telegram or builds a link reads `chat.peer_id`;
  everything inside the index keeps `chat.id`. A fixture where the two coincide hides every
  mix-up, which is what the synthetic-id tests and `tests/fixtures/two_accounts.py` exist for. A
  scoped chat is only ever read through its own account (`sync.foreign_scope`,
  `sync.reaching_accounts`); an id naming two scoped rows is ambiguous and answered with
  candidates, and `<account>/<peer>` names one (`filters.resolve_chat`). `ChatRow.peer_id = 0` /
  `scope = ""` resolve on construction to `id` and the default account's scope.
- `chat_sources` holds every source that covers a chat and `chat_access` every account that
  reaches it, with the access hash that account addresses it by; `chats.source_id` stays the
  *primary* owner the import and discussion-ownership rules read. `db.upsert_chat` writes
  neither table. `sources.resolve_sources(cfg, clients, conn)` does, each account's sources
  through that account's `DialogCatalog`, and replaces a source's coverage
  (`db.set_source_chats`) only when that source resolved this run — a failed source, or one whose
  account has no client, keeps what it had. `sources.remove_source` deletes a chat only when no
  source left in the config covers it; otherwise the primary moves to the first remaining
  covering source in config order — for a link-only discussion group to its channel's
  (`discussion_source_id`), which is why channels are decided before groups — and never onto an
  `import:` tag, and `Removed.kept_chat_ids` names what stayed. `accounts rm` removes an
  account (`sources.remove_account`, under the `SyncLock` and `ConfigLock` the CLI takes after
  its confirmation) — its sources through the same rule (`sources.remove_source_id`), `db.forget_account`
  drops its `chat_access`, `peer_cache` and `accounts` rows and the config is saved, all inside one `db.transaction`, so a
  failure deletes nothing; it first stops the account's active research sessions, voiding their
  unused grants. **Removing a source or an account never
  leaves a chat on Telegram**; `grepogram leave` is the one command that does, CLI-only. An
  account's reach (`db.chat_reach`, `Hit.accounts`, the `account:` spec and the `accounts` search
  scope) is its `chat_access` rows plus the discussion groups of the channels it reaches, and an
  import reaches no account. A scope narrows a query; it is not isolation.
- `sync.sync_all` takes an account → client mapping, resolves every source once, and fetches
  each chat through the account of its primary source: one queue per account in an
  `asyncio.TaskGroup` under the one `SyncLock` and `SyncBudget`. `get_me` and the resolve are
  guarded per account with the flood-sleep cap applied first: a flood wait or a Telegram error
  on one account's resolve stops that account with a warning (`sources.Resolution.flooded` /
  `failed`), and a dead session is raised as `AuthRequired` naming the account
  (`tg.reraise_unauthorized`, needed because several `connected` blocks would otherwise let the
  last one entered claim it). `resolve_sources` first seeds each client with every access hash
  `chat_access` stores for its account (`sources.seed_peers`), re-reads a `chat = "@name"`
  source's stored chat by id rather than resolving the name each sync, and — the rule that
  stops primaries flipping — a source that does not resolve (no client, `SourceError`, its
  account stopped) keeps being the primary of the chats it owns, which are still returned for
  the fetch. **One order, one retry rule**: `sync.reaching_accounts` (over `recorded_reach`)
  orders the accounts for a sync, `extract` and `prune-deleted` alike, and
  `sync.through_accounts` is the one walk down it — flood wait stops that account and moves on,
  a shared chat's refusal or unaddressable peer moves on, anything else ends the chat's turn.
  A chat goes to the first account of its route in the run and not stopped, so a chat whose own
  account is absent is fetched through another that reaches it; one nobody in the run reaches
  is counted in one warning per account (`_SyncPass.unfetched`), not listed as remaining. A
  fallback fetch's own warnings carry the fetching account's label. A flood wait stops only
  that account's queue; a scoped chat never falls back. `prune-deleted` removes a message only
  when **every** account `recorded_reach` names answers it empty (`sync._confirmed_gone`): an
  account that joined late may see history as empty that another still reads, so one of them
  absent or flood-stopped leaves the chat untouched, and only an account refused the chat
  outright is passed over. `only=` narrows the fetch to the chats
  the named sources cover while every source is still resolved, so a narrowed run never moves a
  primary. `index_pending`, `index_stranded`, the re-cut and embedding stay once per run, and a
  report's warnings read `account <name>: …` only when an account other than `default` is in
  the run, so a single-account report is what it always was. `SyncBudget(seconds, messages=…)`
  carries a research run's message allowance: the fetch loops check `halted` (clock or cap),
  while `expired` stays the clock alone so the cap never cuts indexing or embedding. Every
  batch of new rows — comments included — is stored through `_Run.store_new`, which
  `SyncBudget.hold`s its share of the allowance first, so concurrent queues cannot each store a
  batch past the cap; a comment thread stops at the cap and its post is fetched again with it. The CLI's
  multi-account commands and `AppState.telegrams()` leave out an account with no session (warned
  about only when it owns a source) or one Telegram signed out, and fail only when no account is
  left; the MCP `sync` reports the skipped ones in `accounts_skipped`.
- Every confirmation of consent or of a change a config edit cannot undo — `accounts rm`,
  `leave`, `research approve` — is read from the controlling terminal (`cli._terminal` over
  `/dev/tty`), never stdin, and refused without one (`cli.NoTerminal`); there is no `--yes`.
  `sources prune` is the one question still asked through `typer.confirm` on stdin: it deletes
  indexed rows only, after printing them, and changes nothing on Telegram or in the config. The yes is a random code the question shows, typed
  back (`cli._ask`), never `y`: a pipe or a blind `yes` cannot guess it. `cli._open_terminal`
  opens the tty unbuffered in binary and wraps it for text, because a text-mode `r+` open wants
  a seekable file and fails on every real terminal. None of this stops an agent with a shell,
  which can give the command a pty of its own and read the code: the terminal check holds
  against an MCP-only agent, and every text that names the command says the user types it
  themselves — never claim more. The autouse `no_terminal` fixture points `cli.TERMINAL` at a
  path nothing opens, so the real opener runs and refuses; a test that answers installs its own
  terminal, and `tests/test_cli.py` drives the real opener on an `os.openpty()` pair.
- Every edit of `config.toml` is a read-modify-write under `config.ConfigLock` (a blocking flock
  on `config.lock` next to the file, held for milliseconds): the CLI goes through
  `config.update(paths, change)` or takes the lock explicitly inside its `SyncLock`, and
  `AppState.editing_config()` takes it inside the process-wide lock. Never save a config derived
  from a snapshot read before a network round trip; re-read under the lock and apply the delta
  (`sources.with_source`, drop by id). Lock order is `SyncLock` → `ConfigLock` → thread lock.
  The delta is checked against the config it lands on: `with_source` refuses a source whose
  account that config no longer lists (`sources.AccountRemoved` — `accounts rm` saved while
  the target resolved), and `config.save` parses its own text back before writing, so no
  writer can leave a file `config.load` refuses and lock every command and the server out.
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
  existing one is refused at would hide it. No step transforms rows on a guess or rewrites a value
  already stored: an index is derived from Telegram and a rebuild costs one sync. What a step
  *may* do is **fill the columns and tables it adds, deterministically from values already
  stored**, because an imported history is not derived — Telegram cannot serve it again — so
  "delete index.db and sync again" is no upgrade path a released schema may ask for. Step `7`
  fills `chats.peer_id = id`, `chats.scope` from `type`, `chat_sources` from `source_id` and
  `chat_access` for `default` (imports excepted: they reach no account); step `8` records
  `meta['links_captured_from']`, the first row id whose links are captured; step `9` turns that
  mark into the per-row `messages.links_read` (a row with links or a forward origin is read, an
  import's rows are not) and starts the lead clock. On an empty database those fills run over no
  rows. Step 9 also gives the index a random `meta['index_id']` — meta naming the file, not data
  — so a cursor kept outside the index can tell a rebuilt index from this one. **The schema is append-only now that v0.1.0 is tagged** —
  append a step above `BASE_VERSION` and leave `_V5` alone. `migrate()` applies every step from
  `BASE_VERSION` for a database with no schema objects, so a column added to `_V5` *and* to a step
  above it raises `duplicate column name` and fails `tests/conftest.py`'s shared fixture, which is
  the whole suite. v0.2.0's step `6` (`messages.extracted_text`, `messages.media_state`,
  `units.reactions`, the `messages_media_pending` partial index) is the model, and steps `7`
  (accounts: `accounts`, `chats.peer_id` / `scope` unique together, `chat_access`,
  `chat_sources`), `8` (`messages.fwd_peer_id` / `fwd_msg_id` / `fwd_date`, `message_links`)
  and `9` (`messages.links_read` / `lead_seq`, `peer_cache`) followed it; `RECIPE_VERSION` did
  not move for any of them, unit text being unchanged. The media
  index's predicate carries `media_kind IS NOT NULL` as well as `media_state = 0`, or it would
  cover every row in the table forever and the pending query would stay a scan.
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
  caller, not the budget**: `sync_all` takes `recut: bool = True` and `mcp._auto_sync` — the
  refresh inside a `search` — is the one caller passing `False`. A budget floor was tried and
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
- Work a sync — or a research pass (`media.run`, `research.discover` and `research.run`'s
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
- **Every writer of `chats.source_id` asks `sources.imported_tag` first**, because
  `db.upsert_chat` overwrites the column and an `import:<slug>` is the whole of what protects a
  history Telegram cannot serve again: `resolve_sources` skips such a chat on every sync,
  `refuse_imported` refuses the two commands that add a source, and
  `sync.link_discussion_chat` refuses the link outright when a channel's discussion group turns
  out to be one — `DiscussionUnavailable`, so the posts sync without comments and the run says
  why. Refusing the link rather than only keeping the tag is deliberate: pointing `discussion_of`
  at that group would store live comments into a chat marked `unavailable` whose rows came from
  an export. `_check_migration` is the fourth writer and needs no guard — it copies the chat's
  own `source_id` onto the supergroup it migrated to, and only when that supergroup is not
  already stored. `sources.remove_source_id` → `db.set_primary_source` is the fifth, and its
  guard is `_successor`: the new primary is always a configured source covering the chat,
  never an `import:` tag. Lose the tag and `sources rm` of the live source deletes the import,
  `prunable` offers it, and the `import:` handle every refusal tells the user to remove is gone.
  The tag is looked up by identity, `imported_tag(conn, id, scope=…)` finding the row by
  `(scope, peer_id)`, and `import --account` files an export's private chats and legacy groups
  under that account's scope (a synthetic row id when `default` holds the peer).
  `sources.prune_chats` asks the same question a fourth time, on the *delete* side: the scan and
  the confirmation both predate the `SyncLock` — a Telegram round trip must never be held across
  it — so every candidate is put to `_still_prunable` again inside the deletion transaction, and
  one that gained an `import:` tag, changed `source_id`, or became a channel's discussion group
  in between is dropped. It takes `PruneCandidate`s and not ids for exactly that: the offer's
  `source_id` is what "unchanged" is measured against. Re-ordering the resolve and the lock is
  not the fix and never will be.
- Never decide what a `chat:` source covers by comparing `source_id` strings. `chat =` takes an
  id, an `@username`, `https://t.me/<name>` and `t.me/c/<id>`, and all four are one chat:
  `sources.parse_target` folds them into a `Target`, and `sources._names_chat` / `_same_target`
  compare that against the `chats` row's `peer_id` and `username` (case-insensitively).
  `_own_source`, `_same_chat` and `find_source`'s `_named_source` all go through them; a
  spelling-based comparison silently treats a directly configured group as indirect, which hands
  its rows to the channel's source. A fuzzy `chat =` value names no identity offline and matches
  nothing. A source of an account other than `default` has the id `<account>/chat:<value>` or
  `<account>/folder:<name>`; `parse_target` takes that prefix only in front of `chat:` /
  `folder:` and sets `Target.account`, `sources.split_source_id` reads it off an id, and an
  unprefixed target means the default account's match when there is one and any account's
  otherwise (`_in_account`, `AmbiguousTarget` for two). `filters` honours an `<account>/`
  prefix on any spec, but only for an account it knows, so a title holding a slash stays fuzzy.
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
  the index by the front door. Every form is built from `chat.peer_id`, never the row id, which
  for a scoped chat may be synthetic.
- A `MessageView` names the chat it is in (`chat_id`, with `peer_id` and the `accounts` that
  reach it beside it, as on a `Hit`), because a list of them can span two:
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
- `config.toml`, every session file, `research.db` and the lock files (`sync.lock`,
  `config.lock`) are written with `paths.PRIVATE_FILE_MODE` (0600) — `config.write_private`,
  `tg.prepare_session`, `tg.stage_login`, `research_db.connect`, `paths.FileLock`; the directories grepogram
  creates, `sessions/` among them, get `paths.DIR_MODE` (0700) and an existing
  one (a user's own `GREPOGRAM_HOME`) is left as it is. Both cross-process locks derive from
  `paths.FileLock`: `SyncLock` sets `blocking = False` and its own `busy()`, `ConfigLock` blocks.
- Research state lives in `research.db` (`paths.research_db_file`, next to `index.db`) and never
  in the index: the index is derived and may be deleted and rebuilt, while approvals, exclusions
  and session history are the user's decisions and nothing rebuilds them. It has its own version
  (`research_db.SCHEMA_VERSION`, now 4) and its own append-only `research_db.MIGRATIONS`: step 2
  moved seeds, scan cursors and evidence from index row ids to `(scope, peer_id)` and walks a
  development build's version 1 up rather than refusing it — a file of decisions is migrated,
  never re-derived. Step 3 adds `grants.search_kinds` / `grants.stars_max`, the terms a
  session-wide grant was given on; a grant from before it names none and authorizes no search.
  Step 4 adds `grants.join_route` — `invite`, `username`, `id` or `folder:<candidate id>` —
  the way in a `join` / `request` grant's summary named (`research._way_in`, required by
  `add_grant` for those two actions and only them). In code it is a `models.WayIn` (a
  `JoinRoute` plus the folder's candidate id), checked by `research_db.check_way_in`; that
  text form is `research_db._stored_way_in` / `_read_way_in`'s alone, so every grant any build
  wrote reads back the same. **A run takes that route and no other**
  (`research._granted_way_in` → `_join_all`): one it can
  no longer take, or a grant from before step 4 that names none, fails the candidate for a new
  approval — never a swap to another way in. `add_candidate` never gives a parent to a
  candidate with a grant (ever) or any status but `proposed`, so a shared folder found after a
  decision changes neither the summary nor the join. Its `SchemaError` subclasses `db.SchemaError`, so the existing handlers
  catch it, and never advises deleting the file. Whether a candidate is *cached* is
  asked of the index every time and never stored there, and global-search results are
  candidates and evidence in `research.db`, never `messages` rows, so no sync cursor moves. Every
  research entry point refuses before opening the file while `[research] enabled` is false
  (`research.require_enabled`), and ordinary `search` never widens what it reads.
- **`research.db` never names a chat by an index row id.** Seeds (`session_seeds`), scan cursors
  (`chat_scans`) and evidence carry `models.ChatKey` — `(scope, peer_id)`, the identity
  `db.upsert_chat` finds a row by — and are resolved to the row the index holds *now*
  (`research.chat_of`) at use: a rebuilt index numbers rows afresh, and a private chat's
  synthetic id goes to whichever account's row is stored second. `db._next_synthetic_id` keeps a
  high-water mark (`meta['synthetic_next']`) so a deleted scoped row's id is never handed to
  another conversation. The JSON documents show evidence as `scope` / `peer_id` plus `chat_id`,
  the row resolved at read time (`research.evidence_document`, `None` when the index does not
  hold the chat), and seeds as `{scope, peer_id}`.
- Discovery reads a chat from a cursor on the **lead clock**, never a `msg_id`: every
  `upsert_messages` call stamps its rows with one tick (`messages.lead_seq`, inserts always,
  re-stores only when they read the links again) and `db.set_captured_links` does the same, so a
  comment stored below the newest `msg_id`, an edit that gained a link and a recaptured row are
  all read. A cursor counts only on the index `chat_scans.index_id` names (`db.index_id`);
  another index's reads the chat from the start, which duplicates nothing (`add_evidence` keeps
  one row per path). A channel's discussion group is read beside its channel, seed or fetched
  (`research.scan_targets`). Whether a row falls back to `leads.text_leads` is `links_read`, a
  per-row fact, never an id threshold — an import stored after capture began has none either
  (`tdesktop.export_links` reads the runs an export does spell).
- Research also reads the **pinned posts** of the chats a session reads — seeds (the user's own
  sources) and chats a run fetched under a grant, never anything else — once each
  (`chat_scans.pins_read_at`), whatever their age, through `iter_messages(filter=
  InputMessagesFilterPinned)` (`research.read_pins`: from `discover` for what it has not read,
  from a run for the chats it just fetched). Their leads are evidence (`via = pinned`) only:
  **never `messages` rows, and no cursor moves** — a sparse read must never pass for the history
  before it. Another account's private chat is never asked about. A chat whose links name
  `research.DIRECTORY_MIN_CHATS` distinct chats is a directory (`chat_scans.directory`, sticky)
  and every lead found in it gets a `directory` path with the lead's own origin key, so it adds
  no corroboration and approving the directory still approves nothing it lists.
- A forward names its origin by id alone, so `sync.forward_peers` records what the fetching
  account was handed with the message — `msg.forward.chat`'s username and non-`min` access hash
  — in `peer_cache` (index.db, per account), and `research._probe_peer` looks a `peer:`
  candidate up with the access hash of the index's chat row, else `peer_cache`'s, else resolves
  the cached username and takes the answer only when it is that very peer
  (`_probe_named_peer`). The cached username is a probe hint and never a candidate's identity:
  a stale one would fold two chats into one candidate.
- `grepogram recapture-links` (`sync.recapture_links`) is the backfill for rows whose links were
  never read: by id through `StoredPass`, a hundred per request, under the `SyncLock`, resumable
  on a `meta['links_recapture:<chat>']` cursor. It writes `message_links`, `fwd_*`, `links_read`
  and a lead-clock tick and nothing else — no text, no `indexed`, no unit, no sync cursor — and
  leaves a message Telegram no longer has to `prune-deleted`; imports are never re-read.
- **No parameter stands in for consent.** A grant comes from exactly two places: `grepogram
  research approve`, which writes `research.approval_summary` to the controlling terminal and
  reads the typed-back code there, and the MCP `research_approve`, which shows the same summary
  through `ctx.elicit` and grants only on an accepted answer whose strict-boolean `approve` is
  `true`. Decline, cancel, an unticked box, a client without form elicitation and any failure of
  the request grant nothing, and the answer's hint is the terminal command
  (`research.approve_command`), worded as one the user types in their own terminal themselves. `research_db.add_grant` is the only grant writer and takes `via`
  keyword-only with no default, `grants.via` is `CHECK (via IN ('elicitation', 'cli'))`, and
  `research.grant` rebuilds the summary and grants nothing unless it equals the text the human
  saw. Never add an `approve` / `confirm` / `yes` argument to a tool or a `--yes` to the CLI;
  `tests/test_mcp.py` asserts no research tool takes a consent-shaped parameter. Skip and
  exclude only narrow and need no consent.
- A grant names one candidate (or the session, for `global_search` / `paid_search`), the
  session's account and concrete actions (`join`, `request`, `fetch`, `add_source`), and
  `research.authorized(target, action)` is the one check before every outward step of a run:
  only a grant naming that very target counts, so approving a chat approves nothing discovered
  inside it, and a shared folder is approved chat by chat, never whole. Probes read metadata,
  never history; a `peer:` candidate is looked up only with an access hash stored for the
  session's account or by the username a sync saw it under (see `peer_cache` above) and is
  `unresolvable` otherwise, never guessed. `max_candidates` bounds one call and
  `max_session_candidates` the whole session (`research.room`); what the session ceiling cuts
  moves the cursor on, since no later call could propose it. An admission request no admin
  answered within `admission_timeout_days` (`candidates.requested_at`) is `failed` with a note. Global search needs the
  `[research]` switch *and* a `global_search` grant; paying needs `paid_stars_max > 0`, a price
  within it and a `paid_search` grant, consumed before the request is sent. **A session grant
  holds the terms its summary named** (`research_db.add_grant(search_kinds=, stars_max=)`,
  written by `research.grant` from the config the human read): a search switched on since is not
  covered (`research.granted_kinds`), a price above the approved ceiling is refused
  (`_paid_ceiling`, `_consume_paid_grant(price)`), and `_session_entry` asks again rather than
  calling such an approval "already given" — raising a config value never widens a live grant. A run adds its
  sources in one `config.update` under `SyncLock` → `ConfigLock` with no Telegram request under
  either, syncs through `sync_all(…, recut=False, only=…)`, and discovery over what it stored
  only proposes. Sources a run added are ordinary sources and survive `stop`, which voids the
  unconsumed grants and nothing else. A grant's lifecycle: `research._consume_done` consumes it
  only once **every** action it names is done, so a run stopped by a budget, a flood wait or a
  busy sync leaves it live for the next run and nobody is asked twice; a refusal
  (`_refuse_candidate`) sets `failed` or `unavailable` and voids the candidate's grants — a
  `failed` candidate takes a new approval once the cause is gone, an `unavailable` one none
  (`_REFUSED`); an exclusion, a skip and a source removed from the config before its fetch void
  them too.
- Corroboration counts **origins**, not messages: `research.origin_key` gives a forwarded post
  `post:<origin peer>/<origin msg>` and the same post where the index holds it in its channel
  the same key (a channel or supergroup's `msg_id` is global), a forward known only by its
  author `fwd:<author>@<date>`, and anything else `msg:<scope>:<peer>/<msg>`. Global-search
  post results and `directory` evidence reuse the key of the lead they stand for, so neither a
  forward chain nor a directory listing a chat ever counts twice.
- The approval text is the consent, so it says what the run will really do and nothing anyone
  else wrote can bend it. Every value someone else chose — the question, titles, usernames,
  folder titles — goes through `research.shown` (control, format and separator characters as
  U+FFFD, one line) and `_quoted`, and `start_session` refuses a question over
  `QUESTION_MAX_CHARS` or holding such characters. A fetch through a source that already covers
  the chat names that source, its account, `since` and comments (`_covering_source`); an
  `add_source` a configured source already satisfies says it is reused; comments are disclosed
  on both lines, and only a `type == "channel"` gets them. A bare candidate id is
  `join,fetch,add_source` (`request` where `request_needed`, which a probe also reads from
  `Channel.join_request`), public chats included; reading one without joining is an explicit
  `ID:fetch,add_source`. A run acts on the peer the probe saw: a join goes by the stored
  peer id and access hash, a username is resolved only without them and must still name that
  peer (`_OtherChat`), `_mark_joined` never overwrites `peer_id` from a join answer and fails the
  candidate when Telegram answered with another chat, an invite no probe tied to a peer takes
  only the one chat of the answer that is the probed type (and title, between two alike) and
  fails otherwise (`_joined_entity` → `None`), never the answer's first chat blindly. **Every
  source a run adds names the chat by its peer id** (`_planned_source`), a public chat read
  without joining included, so no later ordinary sync follows a freed `@name` to whoever
  registers it (a user's own `chat = "@name"` source keeps following its handle: the user named
  the handle, research approved a chat); such a chat has no dialog, so `_address_public` seeds
  the probe's access hash into the run's client (`sources.seed_peers`) — or, probed without
  one, resolves the username, which must still name the probed peer — and `_add_sources` keeps
  it in `peer_cache` for the session's account before the config write — under `SyncLock` and
  `ConfigLock`, after the approval and the account are checked again on the config as it is
  then, and only for the sources actually added, so a session `accounts rm` ended while the run
  planned adds nothing and leaves no hash for the forgotten account
  (`_remember_read_without_joining`), because a run that adds the source and fetches nothing
  (`ID:add_source` alone, or a fetch deferred and the session stopped) leaves no `chat_access`
  row behind; `sources.resolve_sources` seeds both (`db.stored_peers`, `db.cached_peers`), and a
  source that still does not resolve is a warning in the sync report, never only a log line
  (a run limited by `only` — research's — reports the sources it names alone). A
  candidate with no peer id gets no source.
  `research.grant` validates and writes in one `research.db` transaction; each session action is
  its own grant row, and a paid search pays only after `consume_grant` (one conditional
  `UPDATE`) succeeded, so one approval never pays twice. A global search sends the session's
  question and nothing else: `research.discover` is its only caller and picks the searches the
  switches turn on *and* the grant covers, and `research.search_telegram` trusts it to.
- A candidate is a chat, not a spelling. `research_db.candidate_for` finds the session's row by
  identity, peer id, username or invite hash, `add_candidate` returns that row rather than a
  second one, and a probe that ties two rows to one chat folds the undecided one into the other
  (`research._reconcile` → `research_db.merge_candidate`, never a row with a grant or a decided
  status). **A candidate's peer id is fixed once a probe learned it**: `update_candidate` raises
  on another one, `candidate_for` / `same_chat_candidates` match a username or invite only on a
  row with no peer or the same peer (`_same_chat(strict=True)`), and `add_candidate` records a
  chat whose name another row's peer holds as `peer:<id>`. A name that now leads elsewhere — a
  probe, an admission recheck, a shared folder's child or a search result carrying it — sets the
  old candidate aside (`research._name_moved`: grants voided, `proposed` → `unavailable`,
  `approved` / `joined` / `pending_admission` → `failed`), so a later find can never repoint
  an approval at a chat no human saw. An exclusion names one spelling and covers every other (`research_db.excluded_by`,
  `_covered`): it moves undecided candidates to `excluded` and voids the live grants of every
  candidate of the chat, joined or waiting ones included, and `research.authorized` refuses an
  excluded chat whatever its grants say; `skip` takes joined and waiting candidates too.
- Files end with a single newline; no trailing blank lines.

## Environment variables

- `GREPOGRAM_HOME=<dir>` — every file (`config.toml`, `config.lock`, `session.session`,
  `sessions/`, `index.db`, `research.db`, `sync.lock`, `logs/`) under one directory. The `tmp_home` fixture in
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

- `tests/conftest.py` — the fixtures every module shares: `fake_models`, `plain_cli_output`
  (`TERM=dumb`, so rich prints option names whole), `no_terminal` and `clean_logging` (all
  autouse), `tmp_home`, `conn` (an in-memory index, migrated), `v6_conn` (an empty index at
  schema 6, as v0.2.0 left it, for the upgrade tests) and the `file_mode` helper.
  A module that needs more overrides `conn` by requesting it (`tests/test_filters.py`).
- `tests/fakes.py` — `FakeClient` (async `get_dialogs`, `iter_messages` with Telethon's offset
  semantics, `get_messages(entity, ids=…)`, `get_entity`, `download_media(message, file)`, raw
  requests such as `GetDialogFiltersRequest`) driven by in-memory fixtures, plus `make_*` builders
  for TL entities and dialogs. Two of those answers are signals the product reads, not
  conveniences: a list `ids` answers one slot per id and `None` where a message is gone, which is
  what `sync.prune_deleted` and `media.run` both key on, and the `downloads=` mapping keyed by
  `(chat_id, msg_id)` is what a download writes (bytes, or an exception to raise). Messages it
  yields carry `msg.forward` bound to the origin entity this account sees (its access hash, and
  the origin learned, as Telethon learns an answer's `chats`), and `iter_messages(filter=
  InputMessagesFilterPinned)` answers only the `pinned` ones — any other filter is refused.
  **`FakeClient` refuses a peer it has not learned**, like the real one: `entities=` is the world
  and `resolved` is the session cache, which starts empty and is filled by `get_dialogs()`, by a
  successful `get_entity`, and by the `chats` / `users` of any raw answer — Telethon's
  `session.process_entities`. Addressing an unlearned id raises `ValueError: Could not find the
  input entity` (a legacy `PeerChat` id needs no access hash and is allowed, as in Telethon),
  and that includes `get_entity(<marked id>)`: no path of the fake resolves a bare id the real
  client could not. `session.process_entities` with `InputPeer*` objects seeds a hash, which
  addresses the peer only when it is the account's own.
  `forget_entities()` models the fresh client `extract` and `prune-deleted` each build after a
  sync, and `strict_entities=False` is for a test with no realistic route to warm up. A fake more
  permissive than production is a fake that hides bugs: this one hid a `grepogram extract` that
  resolved no chat at all on a real account through nine review rounds. Several accounts are
  several `FakeClient`s over one `FakeWorld` (`FakeWorld.client(account, members=…)`), with
  per-account membership, private-chat histories and access hashes: a hash that is not the
  account's own is refused, a private channel refuses a non-member, and a channel an account is
  not in comes back `left`. The world also carries invites, shared folders and the global-search
  answers research probes.
- `tests/fixtures/two_accounts.py` — two accounts' chats in one index: a channel both reach and
  its link-only discussion group, and each account's private chat with the same person (the
  work one on a synthetic row id, message ids colliding).
  `tests/test_upgrade.py` builds a v0.2.0 home (schema 6, one `session.session`, imports) from
  what the CLI writes and checks it migrates, syncs and searches unchanged.
- `tests/fixtures/tl.py` — real Telethon `types.Message` objects built without a client (text,
  caption with photo, voice, document with filename, reply, forum topic, forward, service message,
  reactions, channel post, hyperlink, mention, URL button, channel-post forward).
- `tests/fixtures/sample.pdf`, `tests/fixtures/sample.docx` — tiny hand-built documents the
  extraction tests round-trip; `tests/fixtures/tdesktop_export.json` — a Telegram Desktop export
  the import tests parse.
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
(dataclasses and the shared `Literal`s: `ChatType`, `UnitKind`, `MediaKind`, `SearchMode`,
`LinkKind` and the research ones; the research row types too), `db` (schema, migrations,
accessors), `tg` (per-account clients and sessions, auth errors, sign-in), `dialogs` (folders,
fuzzy matching), `sources` (targets, resolution, coverage, status), `sync` (fetch, mapping,
lock, budget, the per-account queues and `StoredPass`),
`units` (windows, threads, posts, incremental rebuild, `RECIPE_VERSION`), `stem` (tokenizer,
Snowball, FTS query), `index` (FTS and vec maintenance, KNN), `embed` and `rerank` (protocols,
fakes, bge models), `extract` (the extractor registry: PDF, DOCX, macOS Vision OCR), `media` (the
bounded extraction pass), `tdesktop` (Telegram Desktop export parsing),
`links` (deep links), `leads` (normalizing a Telegram link or mention to a target, and the
text fallback for rows whose links were never read), `filters` (chat specs, dates,
`account:` scopes, `resolve_chat` for the one-chat readers), `search` (retrieval, fusion,
dedup, readers), `research_db` (`research.db`: schema and accessors), `research` (discovery,
probing, global search, approval grammar and summaries, grants, runs, the JSON documents the
CLI and the tools share), `cli` (typer app: `search` and the `thread` / `context` readers beside
`sources`, `accounts`, `auth`, `sync`, `extract`, `embed`, `import`, `prune-deleted`,
`recapture-links`, `leave`,
`research`, `config`), `mcp` (FastMCP server with eighteen tools: the readers, `sync`, the
source tools, a read-only `accounts` and nine `research_*`). The CLI and the MCP server offer
the same readers, and `--json` prints the document the matching tool returns. Some commands are
**CLI-only by design** and have no MCP tool: `sources prune` and `prune-deleted` delete indexed
history, `extract` and `recapture-links` are long flood-exposed network passes, `import` reads a
directory the server
cannot see, `auth` and `accounts rm` sign accounts in and out, `leave` is the one command that
changes an account on Telegram, and `research unexclude` lifts the user's own decision.

Plans live in `docs/plans/`, finished ones in `docs/plans/completed/`.
