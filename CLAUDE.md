# CLAUDE.md

grepogram: local hybrid search over opt-in Telegram chats of one or more signed-in accounts,
served to agents over MCP and the CLI (the Claude Code plugin in `plugin/` drives the CLI), with
an opt-in research mode that finds chats beyond them.
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
- the version lives in `grepogram/__init__.py` (`__version__`, read by hatch and
  `grepogram --version`) and is mirrored in `plugin/.claude-plugin/plugin.json` `version`; a test
  and `release.yml` refuse a mismatch
- `claude plugin validate .` and `claude plugin validate plugin/.claude-plugin/plugin.json` check
  the marketplace and the manifest; `claude --plugin-dir ./plugin` runs the working tree, since a
  marketplace install is cached by `version` and does not pick up edits

## Releasing

`.github/workflows/release.yml` runs on a `v*` tag. In order:

1. bump `__version__` in `grepogram/__init__.py` **and** `version` in
   `plugin/.claude-plugin/plugin.json` together, commit, and push to `main` (`release.yml` and a
   test in `tests/test_plugin.py` both refuse a mismatch);
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

The plugin's CLI floor is the phrase `grepogram >= X.Y.Z` in each skill and command markdown file
(one per file, all equal, never above `__version__`; a test enforces it). It is raised on purpose,
only when a skill starts needing CLI surface the older release lacks, and not on every release.

The marketplace serves `./plugin` from `main`, so merging publishes the plugin: the tag and the
PyPI upload of a version the plugin needs follow right away. Installed users get a plugin-only
change only with the next version bump, because Claude Code caches the plugin by `version`.

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
  refusal note is logged at DEBUG (`research.joining._refuse_candidate`), and `mcp.tool_failure` logs a
  `ResearchError` or `SourceError` by its type above DEBUG, its text at DEBUG.
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
  `accounts.reaching_accounts`); an id naming two scoped rows is ambiguous and answered with
  candidates, and `<account>/<peer>` names one (`filters.resolve_chat`). `ChatRow.peer_id = 0` /
  `scope = ""` resolve on construction to `id` and the default account's scope.
- Every confirmation of consent or of a change a config edit cannot undo — `accounts rm`,
  `leave`, `research approve` — happens in one of two ways, and never through stdin or a bare
  `--yes`. At a controlling terminal (`cli._terminal` over `/dev/tty`) the yes is a random code
  the question shows, typed back (`cli._ask`), never `y`. Without one — an agent running the
  command — the command prints the exact summary, a token bound to it (`grepogram.consent`) and
  the exact confirming command, changes nothing and exits `cli.CONFIRM_EXIT` (3); the same
  command with `--confirm <token>` acts, and a token whose rebuilt summary differs (state
  changed, other items, another chat) is refused with a fresh summary and token, also exit 3.
  The token is a short SHA-256 over the command, the normalized request and the summary — for
  `research approve` the request includes each named candidate's status, since a skip leaves the
  summary unchanged. It is not a secret and not proof of a human: for an agent the human gate is
  the harness's own permission prompt (Claude Code "ask" rules on these commands), and the token
  only guarantees that what is confirmed is exactly what was shown. Never claim more for it.
  `--json` never prompts. `sources prune` is the one question still asked through
  `typer.confirm` on stdin: it deletes indexed rows only, after printing them, and changes
  nothing on Telegram or in the config. `cli._open_terminal` opens the tty unbuffered in binary
  and wraps it for text, because a text-mode `r+` open wants a seekable file and fails on every
  real terminal. The autouse `no_terminal` fixture points `cli.TERMINAL` at a path nothing opens,
  so the real opener runs and the token path is taken; a test that answers installs its own
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
- grepogram launches nothing. It has no `open`, no `subprocess` outside the tests, and no
  platform dependency on macOS beyond its default paths: a result carries links and a human or an
  agent clicks one. The `open_message` tool, `links.open_link` and the `tg://resolve` /
  `tg://privatepost` forms that only fed it were removed for that reason — a search answers with
  ten hits, so "open this one" was never the workflow, and a review probe that reached the real
  runner once launched Telegram and Safari with fixture links. Do not bring any of it back.
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
- Files end with a single newline; no trailing blank lines.

Module rules live in `.claude/rules/` and load when Claude reads a file they cover:

- `telegram-and-sync.md`: Telegram clients and sessions, message mapping, sources and coverage, the
  per-account sync, discussion groups, imports, comment mapping, forward peers and link recapture
- `schema-migrations.md`: how `db.MIGRATIONS` is numbered, decided and appended
- `units-and-indexing.md`: the `indexed` flag, `RECIPE_VERSION` and re-cuts, media extraction,
  reactions, `joined_to_thread`, window cutting
- `models.md`: loading the embedder and reranker from the Hugging Face cache
- `readers-links-mcp.md`: display links, `MessageView`, the `mcp` version pin and `FastMCP` setup
- `research.md`: `research.db`, discovery, pins, grants, approvals, runs and candidates
- `tests.md`: shared fixtures, `FakeClient` and the test corpora

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

## Layout

`grepogram/`: `paths` (file locations, `FileLock`), `config` (TOML and `TEMPLATE`), `models`
(dataclasses and the shared `Literal`s: `ChatType`, `UnitKind`, `MediaKind`, `SearchMode`,
`LinkKind` and the research ones; the research row types too), `db` (schema, migrations,
accessors), `tg` (per-account clients and sessions, auth errors, sign-in), `dialogs` (folders,
fuzzy matching), `sources` (targets, resolution, coverage, status), `accounts` (who each
signed-in account is — `signed_in_user`, `check_account`, `ask_account` — how a report names an
account and a flood wait, and which account asks about a chat: `reaching_accounts`,
`through_accounts`, `StoredPass`, `warm_peer_cache`; it imports nothing of `sync`, which imports
it, and takes a budget as its own `Budget` protocol for that reason), `sync` (fetch, mapping,
lock, budget, the per-account queues, the deletion sweep and the link recapture pass,
`joined_to_thread`),
`units` (windows, threads, posts, incremental rebuild, `RECIPE_VERSION`), `stem` (tokenizer,
Snowball, FTS query), `index` (FTS and vec maintenance, KNN), `embed` and `rerank` (protocols,
fakes, bge models), `extract` (the extractor registry: PDF, DOCX, macOS Vision OCR), `media` (the
bounded extraction pass), `tdesktop` (Telegram Desktop export parsing),
`links` (deep links), `leads` (normalizing a Telegram link or mention to a target, and the
text fallback for rows whose links were never read), `filters` (chat specs, dates,
`account:` scopes, `resolve_chat` for the one-chat readers), `search` (retrieval, fusion,
dedup, readers), `research_db` (`research.db`: schema and accessors), `research/` (a package;
its `__init__` re-exports what the CLI, the MCP server and the tests call, and each module
imports only the ones before it: `collect` (leads, what the index caches, ranking), `sessions`
(errors, sessions, `shown`), `grants` (`authorized` and which searches a session may run),
`offline` (offline discovery), `pins`, `probing` (and `_record_entity`, shared with the global
search), `searching` (global search), `discovery` (the whole discover call), `approval`
(summaries, grants, skip / exclude / stop, the approval grammar), `joining`, `running` (the run)
and `documents` (the JSON documents the CLI and the tools share); a test patches a function in
the module that looks it up, e.g. `research.discovery.discover_offline`), `cli` (typer app: `search` and the `thread` / `context` readers beside
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

`plugin/` is the Claude Code plugin, listed by the root `.claude-plugin/marketplace.json`:
`.claude-plugin/plugin.json` (manifest, whose version tracks `__version__`, and the icon PNG),
`skills/search` and `skills/research` (`SKILL.md`), `commands/setup.md`, `hooks/hooks.json` and
`scripts/consent-gate.sh`. It bundles no MCP server; the CLI is what it drives. Rules for it,
checked by `tests/test_plugin.py`:

- skills and commands put options first and `--` before a positional value that takes free text
  or a chat (a query, a question, a chat id); a drift check parses every command and flag they and
  their `allowed-tools` name against the real Typer app, so a renamed command or flag fails the
  suite;
- `allowed-tools` pre-allow only commands that change nothing (`READ_ONLY` in the test): never
  `research approve`, `run` or `exclude`, `accounts rm`, `leave`, `sources add`, a config edit,
  an install or the MCP registration. The hook is a second layer, not the gate: a hook that fails
  or times out does not block;
- the hook is one self-contained `/bin/bash` 3.2 script: no sourcing, no heredocs, no installers,
  nothing on stdout but the hook's JSON decision. It asks for a permission prompt on any payload
  naming `grepogram` and `--confirm`, in any case, and never under-asks on an honest call; a
  regex cannot stop deliberate obfuscation, so it is defence in depth. `PRIVACY.md` covers what
  the plugin reads;
- the drift check catches names, not meaning: the skills restate `mcp.INSTRUCTIONS` and depend on
  CLI JSON shapes (`index_age_min`, the hit and candidate fields, `summary` / `confirm` /
  `command` with exit 3), and a change to any of them is mirrored in `plugin/skills/*` (a test
  pins the field names and constants the skills quote);
- never write the icon's file name in any text file (a test scans the top-level files and the
  project's own directories for it).

Plans live in `docs/plans/`, finished ones in `docs/plans/completed/`.
