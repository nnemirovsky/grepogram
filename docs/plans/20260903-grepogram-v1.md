# grepogram v1

## Overview

grepogram is a local search engine over opt-in Telegram chats, exposed to Claude Code through MCP (plus a thin CLI). It syncs messages through the Telegram user API (Telethon), stores them in one SQLite file, builds conversation-level search units (time windows, reply threads, channel posts), indexes them lexically (FTS5 with RU/EN stemming) and densely (sqlite-vec with `bge-m3` computed locally on the Mac GPU), fuses both with Reciprocal Rank Fusion, reranks with a local cross-encoder, and returns hits with deep links that open the original message in Telegram.

Problem it solves: Telegram's own search is exact-word, morphology-blind and useless for "what do people in the Argentina chat say about opening a bank account without a DNI". Pure vector search over single messages (tried before) fails because answers live in reply chains and hinge on exact tokens (bank names, `ВНЖ`, `CUIT`). The LLM layer is Claude Code itself — no API token, no hosted service; embeddings never leave the machine.

Key benefits: hybrid retrieval over thread-shaped units, hard date filters (chat knowledge is time-sensitive), sources chosen from Claude via fuzzy dialog matching, index auto-refreshes on stale searches, everything in one `uv` project intended to be open-sourced (MIT).

## Context (from discovery)

- Brand-new repo at `/Users/nemirovsky/Developer/grepogram`, `main`, empty. No existing code or conventions to match.
- Verified on this Mac (2026-09-03): Homebrew CPython 3.12.12 with SQLite 3.52 loads `sqlite-vec` v0.1.9 (`vec0` partition key + metadata filter KNN works), and FTS5 with `unicode61 remove_diacritics 2` + `bm25(fts, 2.0, 1.0)` works. Python 3.14 is the default `python3` but lacks torch/sqlite-vec wheels → pin 3.12.
- Verified FTS5 behaviour (plan review): `DELETE … WHERE <unindexed col>=?` is a full scan, `DELETE … WHERE rowid=?` is a direct lookup; `INSERT OR REPLACE` reallocates rowids while `ON CONFLICT … DO UPDATE` preserves them; a bare `MATCH '"tok"'` searches all indexed columns; `bm25()` is negative (better = more negative); filtering on UNINDEXED columns must be a plain `AND col = ?`, not inside `MATCH`.
- Telegram facts: `messages.getDialogFilters` returns `DialogFilter` / `DialogFilterChatlist` / `DialogFilterDefault`; `title` is `TextWithEntities` (use `.text`); membership = `include_peers ∪ pinned_peers − exclude_peers` plus category flags (`contacts`, `non_contacts`, `groups`, `broadcasts`, `bots`) and `exclude_muted/read/archived`. `iter_messages(channel, reply_to=post_id)` returns messages that live in the **linked discussion group** (their own `chat_id`/`msg_id` space). Telethon appends `.session` to any session path lacking it. With `reverse=True`, `offset_date`/`offset_id` semantics invert (messages *after* the offset). Deep links: `https://t.me/<username>/<msg>`, `https://t.me/c/<id>/<msg>` (topics insert `/<topic>`), `tg://openmessage?user_id=&message_id=` is mobile-only.
- Related local tooling: Claude Code with `claude mcp add`; `uv` 0.10; no Ollama, no `tdl`.
- Design was brainstormed and validated section by section on 2026-09-03, then reviewed by the plan-review agent; its must/should-fix findings are incorporated below.

## Development Approach

- **testing approach**: Regular (code first, then tests) — each task implements, then adds tests, runs them, commits
- complete each task fully before moving to the next
- make small, focused changes; one conventional commit per logical change: `type(scope): lowercase description` (`feat(sync): fetch messages incrementally`)
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional — they are a required part of the checklist
  - unit tests for new and modified functions; success and error paths
  - all tests run against in-memory SQLite and fake Telegram/embedder objects — no network, no model downloads (except the `@pytest.mark.slow` tests); model-layer tests inject stub modules into `sys.modules` so they pass without the `dense` extra installed
- **CRITICAL: all tests must pass before starting next task** — `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy grepogram`
- **CRITICAL: update this plan file when scope changes during implementation**
- MCP server speaks stdio: nothing may print to stdout except the protocol; logs go to stderr and the log file; `main()` redirects stdout to stderr around startup and tool bodies, and a test asserts stdout stays empty
- message text is never logged above DEBUG; `config.toml` and the session file are written with mode 0600

## Testing Strategy

- **unit tests**: required for every task (see Development Approach above); pure functions (stemmer, window cutting, thread assembly, RRF, dedup, links, filter parsing) are table-driven
- **integration tests**: in-memory SQLite end-to-end (`sync fixtures → units → index → search`) with `FakeEmbedder` and `FakeReranker` (`GREPOGRAM_FAKE_MODELS=1`)
- **Telegram**: never called in tests; Telethon TL objects (`types.Message`, `types.DialogFilter`, …) are constructed directly in fixtures **without a client**, and the client is a small fake exposing async generators
- **slow**: `@pytest.mark.slow` tests load real `bge-m3` / reranker (RU/EN paraphrase closer than an unrelated sentence, fp16 sanity) — excluded by default via `addopts`
- **e2e tests**: none (no UI)
- **static**: `ruff` (lint + format) and `mypy --strict` on `grepogram/`; `pytest-cov` report per module

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview

```
Telethon ──sync──▶ messages ──units──▶ units ──index──▶ msg_fts / unit_fts (FTS5, rowid = messages.id / units.id)
                    (SQLite)                              unit_vec (sqlite-vec, rowid = units.id, bge-m3)
                                                                │
                            search: filters → lexical ∪ dense → RRF → rerank → dedup → hits(url)
                                                                │
                                              FastMCP (stdio) ◀─┴─▶ typer CLI
```

Key decisions and rationale:

- **Units, not messages, are the dense unit.** Windows (time-gapped), threads (reply chains) and channel posts carry enough context to embed; single messages do not and would 10× the vector store. Messages still get BM25 so exact "find that thing" queries work; a message hit maps to its containing window.
- **Hybrid + rerank.** BM25 with per-script Snowball stemming catches exact tokens and Russian morphology; dense catches paraphrase; RRF needs no tuning; cross-encoder rerank fixes the top of the list.
- **Local models only** (`BAAI/bge-m3`, `BAAI/bge-reranker-v2-m3` via sentence-transformers on MPS). Missing models degrade to lexical-only with a warning — never a crash.
- **Opt-in sources**, resolved on every sync (folders can change). Any peer type is a valid source. Channel comments live in the linked discussion chat, stored as its own `chats` row.
- **MCP carries the agent playbook** in its `instructions`: query variants, recency preference, cite `url`, read `thread`/`context` before concluding.
- **One SQLite file, WAL**, `busy_timeout=5s`, `check_same_thread=False`, FK cascades from `chats`, a `sync.lock` for cross-process sync exclusion. FTS and vec rows are keyed by the parent rowid so deletes are direct lookups.

## Technical Details

### Paths (all overridable with `GREPOGRAM_HOME=<dir>` → `<dir>/{config.toml,session.session,index.db,sync.lock,logs/}` — used by tests)

- `~/.config/grepogram/config.toml` (0600), `~/.config/grepogram/session.session` (Telethon SQLite session, 0600; the `.session` suffix is mandatory because Telethon appends it otherwise)
- `~/Library/Application Support/grepogram/index.db`, `~/Library/Application Support/grepogram/sync.lock`
- `~/Library/Logs/grepogram/grepogram.log` (RotatingFileHandler 5 MB × 3) + stderr

### config.toml (read with stdlib `tomllib`, written with `tomli_w`; `save` is comment-lossy by design — `grepogram config init` writes this annotated template with the `[[sources]]` examples commented out, so a fresh config has no live sources and the template parses to the defaults)

```toml
[telegram]
api_id = 0
api_hash = ""

[models]
embed = "BAAI/bge-m3"                 # sentence-transformers id; change → full re-embed
rerank = "BAAI/bge-reranker-v2-m3"
device = "auto"                        # auto → mps if available else cpu

[search]
k = 10
rrf_k = 60
rerank_top = 40
dedup_overlap = 0.5
vec_fanout_max = 8                     # > this many chats in a filter → one KNN with k*4, post-filtered
auto_sync_after_min = 60
auto_sync_budget_s = 20

[units]
window_gap_min = 30
window_max_msgs = 30
window_max_chars = 1500
thread_max_msgs = 40

[sync]
edit_refetch = 200
flood_sleep_threshold = 120

[[sources]]
folder = "Argentina"

[[sources]]
chat = "@ru_georgia"          # or "https://t.me/…" or 123456789
since = "2024-01-01"          # optional: skip older history on first sync
comments = false              # channels only: also index linked discussion threads
```

### SQLite schema (`db.py`, `schema_version` in `meta`)

```sql
meta(key TEXT PRIMARY KEY, value TEXT);            -- schema_version, embed_model, embed_dim
chats(id INTEGER PRIMARY KEY, type TEXT NOT NULL,   -- user|bot|group|supergroup|channel
      title TEXT, username TEXT, is_forum INTEGER DEFAULT 0,
      source_id TEXT,                               -- which [[sources]] entry pulled it in
      discussion_of INTEGER,                        -- set on a linked discussion chat: the channel id
      last_msg_id INTEGER DEFAULT 0, last_sync_at INTEGER,
      unavailable INTEGER DEFAULT 0, migrated_to INTEGER);
users(id INTEGER PRIMARY KEY, display_name TEXT, username TEXT);
messages(id INTEGER PRIMARY KEY,                    -- surrogate; msg_fts.rowid = messages.id
         chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
         msg_id INTEGER NOT NULL, date INTEGER NOT NULL, edit_date INTEGER,
         from_id INTEGER, from_name TEXT, reply_to_msg_id INTEGER, topic_id INTEGER,
         fwd_from TEXT, text TEXT NOT NULL DEFAULT '', media_kind TEXT, media_filename TEXT,
         reactions_total INTEGER DEFAULT 0,
         UNIQUE (chat_id, msg_id));                  -- upserts use ON CONFLICT DO UPDATE (rowid stays)
  CREATE INDEX messages_chat_date ON messages(chat_id, date);
  CREATE INDEX messages_reply ON messages(chat_id, reply_to_msg_id);
units(id INTEGER PRIMARY KEY AUTOINCREMENT,         -- unit_fts.rowid = unit_vec.rowid = units.id
      chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE, topic_id INTEGER,
      kind TEXT NOT NULL,                            -- window|thread|post
      msg_id_start INTEGER, msg_id_end INTEGER, msg_ids TEXT NOT NULL, -- JSON array
      date_start INTEGER, date_end INTEGER, text TEXT NOT NULL,
      dirty INTEGER DEFAULT 1, embedded_model TEXT);
  CREATE INDEX units_chat_kind_range ON units(chat_id, kind, msg_id_start, msg_id_end);
msg_fts  USING fts5(raw, stemmed, chat_id UNINDEXED, date UNINDEXED,
                    tokenize='unicode61 remove_diacritics 2');           -- rowid = messages.id
unit_fts USING fts5(raw, stemmed, chat_id UNINDEXED, date_start UNINDEXED,
                    tokenize='unicode61 remove_diacritics 2');           -- rowid = units.id
unit_vec USING vec0(chat_id INTEGER PARTITION KEY, date_start INTEGER,
                    embedding FLOAT[<dim>] distance_metric=cosine);      -- rowid = units.id, created once dim is known
```

FTS and vec virtual tables cannot carry FK constraints, so `db.delete_chat(conn, chat_id)` deletes their rows by rowid before deleting the `chats` row (which cascades to `messages`/`units`).

### Core types (`grepogram/models.py`)

- `ChatRow`, `MessageRow`, `UnitRow` — `dataclass(slots=True, frozen=True)` mirrors of the tables
- `Source(folder | chat, since, comments)`, `Config(telegram, models, search, units, sync, sources)`
- `Filters(chat_ids: set[int] | None, since: int | None, until: int | None)`
- `Link(url, fallback_url: str | None)`
- `Hit(score, chat: ChatRow, kind, date_start, date_end, anchor_msg_id, url, fallback_url, snippet, msg_ids, text | None)`
- `MessageView(msg_id, date, from_name, text, url, fallback_url, reply_to_msg_id)`
- `SearchResult(hits, warnings: list[str], index_age_min: int | None, synced: bool)`
- `SyncReport(new: int, chats_done: list[int], chats_remaining: list[int], unavailable: list[int], warnings: list[str])`
- `SourceStatus(source_id, chats: list[ChatStatus(title, type, message_count, last_sync_at, unavailable)])`

### Processing rules

- **Message mapping** reads **raw TL attributes only** (`msg.message`, `msg.media`, `msg.reply_to`, `msg.fwd_from`, `msg.reactions`, `msg.from_id`, `msg.post`) — never client-bound helpers (`msg.text`, `msg.file`, `msg.sender`, `msg.chat`), so fixtures work without a client. Service messages → skipped. Captions are text. `reply_to_msg_id` from `reply_to.reply_to_msg_id`, ignored when it is the forum topic root (`reply_to.forum_topic and not reply_to.reply_to_top_id`); `topic_id = reply_to.reply_to_top_id or reply_to.reply_to_msg_id` for forum messages. `media_kind ∈ {photo, video, voice, video_note, document, sticker, audio, poll, contact, location, webpage, other}` from the `MessageMedia*` type and document attributes; `media_filename` from `DocumentAttributeFilename`. `reactions_total = Σ reactions.results[].count`. `from_name` = display name resolved from the peers Telethon returns alongside messages; `users` upserted.
- **Fetch**: `iter_messages(entity, min_id=last_msg_id, reverse=True, offset_date=since_on_first_run)` (with `reverse=True` the offset means *after*); upsert in batches of 500 with `ON CONFLICT(chat_id, msg_id) DO UPDATE`; `last_msg_id = max(msg_id)`. Then `iter_messages(entity, limit=edit_refetch)` upsert (edits, reactions). Channels with `comments=true`: resolve the linked discussion chat via `GetFullChannelRequest(...).full_chat.linked_chat_id`, upsert it as its own `chats` row (`discussion_of = channel id`, same `source_id`), then for each new post `iter_messages(channel, reply_to=post.id)` and store the returned messages under the **discussion chat's** id with their own `msg_id`s (their `reply_to_msg_id`/thread root is the discussion-side forwarded post; `topic_id` = the channel post id ties each comment to its post). `MsgIdInvalidError` (post without a thread) → skip that post, DEBUG log. `ChannelPrivateError` / `ChatAdminRequiredError` / `ChannelInvalidError` → `unavailable=1`. Entity `migrated_to` → new `chats` row, old row `migrated_to` set, old messages kept.
- **Budget**: `SyncBudget(deadline)`; chats processed in `last_sync_at ASC` order; on expiry stop cleanly after the current batch and report `chats_remaining`. `sync_all(..., embedder=None)` ends with `rebuild_for_chat` + `index_chat` per synced chat and `embed_dirty_units` when an embedder is given — every caller (CLI `sync`, MCP `sync`, auto-sync in `search`) goes through this one function.
- **Windows**: per `(chat_id, topic_id)`, chronological; start new window when `gap > window_gap_min` OR `len >= window_max_msgs` OR `chars >= window_max_chars`. Text lines: `[YYYY-MM-DD HH:MM] {from_name}: {text}` (media-only → `[photo]`/`[voice]` placeholder). Only the last window per `(chat, topic)` is "open": on new messages it is deleted and re-cut from its `msg_id_start`.
- **Threads**: roots = messages with ≥1 reply and no parent inside the chat; thread = root + descendants (BFS over `reply_to_msg_id`), chronological, capped at `thread_max_msgs`; overflow continues in another unit (`kind=thread`, same root id in `msg_ids[0]`). On sync, rebuild threads whose root is reachable from any new message.
- **Posts**: `chats.type == channel` → each post is a `post` unit; with `comments=true`, the post text + its comments (read from the discussion chat via `discussion_of`) form a `thread` unit owned by the channel chat.
- **Stemming** (`stem.py`): tokens = `\w+` over NFKC-normalized lowercase text; Cyrillic token → `snowballstemmer.stemmer("russian")`, Latin → `"english"`, else unchanged; digits and short tokens kept. `fts_query(text, op)` quotes every token (`"tok"`) and joins with ` AND ` / ` OR `; returns `None` when no tokens survive (pure emoji/punctuation) — lexical mode then returns an empty result with a warning, hybrid mode runs dense-only.
- **Lexical search**: `SELECT rowid, bm25(unit_fts, 2.0, 1.0) AS s … WHERE unit_fts MATCH ? AND chat_id IN (…) AND date_start BETWEEN ? AND ? ORDER BY s ASC` (bm25 is negative; score = `-s`); AND first, OR fallback when `< k` hits; both `unit_fts` and `msg_fts` queried; message hit → containing window via `units WHERE kind='window' AND chat_id=? AND ? BETWEEN msg_id_start AND msg_id_end`; anchor = matched `msg_id` (for unit hits: the unit's best message via `msg_fts`, else first message).
- **Dense search**: sqlite-vec partition key supports only `=`. With a chat filter of ≤ `vec_fanout_max` chats, KNN runs once per `chat_id` (k each) and merges; above that, one unfiltered KNN with `k*4` post-filtered by `chat_id`; with no chat filter, one unfiltered KNN. `date_start >= since` / `<= until` as metadata constraints. `knn` returns `[]` when `unit_vec` does not exist or is empty.
- **Fusion**: RRF `score = Σ 1/(rrf_k + rank)` over unit ids from `[lexical_units, lexical_msgs→window, dense]`; top `rerank_top` → cross-encoder `(query, unit.text)` → sort; dedup: drop a hit whose `msg_ids` overlap ≥ `dedup_overlap` with a higher-scored hit; return top `k`.
- **Snippet**: anchor message text ± neighbours from `messages` within the unit until 600 chars, anchor first.
- **Links** (`links.py`): `channel|supergroup` with `username` → `https://t.me/{username}/{msg}` (`is_forum` → `/{topic}/{msg}`); without username → `https://t.me/c/{id}/{msg}` where `id` strips the `-100` prefix; `user|bot` → `url = tg://openmessage?user_id={id}&message_id={msg}`, `fallback_url = tg://user?id={id}`; `group` (legacy) → `tg://openmessage?chat_id={id}&message_id={msg}`. `Hit` and `MessageView` carry both `url` and `fallback_url`.
- **Staleness**: `index_age_min = (now − max(chats.last_sync_at)) / 60`; when `> auto_sync_after_min` and the caller is the MCP `search` tool, run `sync_all(budget=auto_sync_budget_s)` first and set `synced=true`. Auto-sync errors (`AuthRequired`, `SyncInProgress`, `FloodWaitError`, budget overrun) are appended to `warnings` and the search proceeds on the existing index; only the explicit `sync` tool returns them as `error`.
- **Degradation**: `ModelUnavailable` or missing `unit_vec` → `mode=lexical`, `warnings += ["dense search unavailable: <reason>"]`; no MPS → CPU with one-time WARN log.

### MCP contract (`mcp.py`, `from mcp.server.fastmcp import FastMCP`, server name `grepogram`)

| tool | signature | returns |
|---|---|---|
| `search` | `(query, chats: list[str] \| None, since: str \| None, until: str \| None, k=10, mode="hybrid", rerank=True, full=False)` | `SearchResult` as JSON |
| `thread` | `(chat_id, msg_id)` | `MessageView` list for the reply thread containing `msg_id` |
| `context` | `(chat_id, msg_id, before=15, after=15)` | surrounding `MessageView` list |
| `sync` | `(budget_s=45)` | `SyncReport` |
| `sources` | `()` | `SourceStatus` list (from `sources.sources_status`) |
| `dialogs` | `(query)` | fuzzy matches over dialog titles and folder names: `{id, title, type, username, folders}` |
| `sources_add` | `(target)` | target = id / `@username` / t.me link / `folder:<name>` / fuzzy title → writes config, returns resolved entry |
| `sources_remove` | `(target)` | removes matching entry and deletes its chats' data (`db.delete_chat`) |
| `open_message` | `(chat_id, msg_id)` | looks up the message (for `topic_id`), runs `open <url>`; returns the url used |

`instructions` (server-level): run 2–3 query variants (Russian and English, the specific term and the concept, synonyms); prefer recent hits for anything regulatory or price-related and state the date; call `thread`/`context` before drawing a conclusion from a snippet; cite `url` per claim; if nothing relevant comes back, say so rather than guess; call `sources`/`dialogs` when the user names a chat that is not indexed yet.

### CLI (`cli.py`, typer)

`grepogram auth` · `grepogram config path|init` · `grepogram dialogs "<q>"` · `grepogram sources add|ls|rm <target>` · `grepogram sync [--budget S]` · `grepogram search "<q>" [--chat X]… [--since 2025-06] [--until …] [--mode hybrid|lexical|dense] [--no-rerank] [-k N] [--full] [--json]` · `grepogram embed [--reembed]`. Output: plain aligned text with `url` on its own line per hit.

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): everything in this repo — code, tests, docs
- **Post-Completion** (no checkboxes): my.telegram.org app creation, real-account auth, MCP registration in Claude Code, quality tuning on real chats

## Implementation Steps

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `LICENSE`, `README.md`
- Create: `grepogram/__init__.py`, `grepogram/py.typed`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/test_smoke.py`

- [x] `pyproject.toml`: project `grepogram`, `requires-python = ">=3.12,<3.13"`, deps `telethon`, `mcp>=1.2,<2` (mcp 2.x renamed `FastMCP` to `MCPServer`; the MCP contract targets the 1.x API), `sqlite-vec`, `snowballstemmer`, `typer`, `tomli-w`; optional extra `dense = ["sentence-transformers", "torch"]`; dependency group `dev` = `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`; scripts `grepogram = "grepogram.cli:app"`, `grepogram-mcp = "grepogram.mcp:main"`; ruff (line-length 100, rules E,F,I,UP,B) and mypy `strict = true`; pytest `markers = ["slow"]`, `addopts = "-m 'not slow'"`, `asyncio_mode = "auto"`
- [x] `.python-version` = `3.12`; `uv sync --all-extras --all-groups` succeeds on this Mac (CI later uses `--group dev` only)
- [x] `.gitignore` (`.venv/`, `__pycache__/`, `*.egg-info`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `.coverage`), MIT `LICENSE`, README stub with one-paragraph description
- [x] `tests/conftest.py`: `tmp_home` fixture setting `GREPOGRAM_HOME` to a `tmp_path` subdir and `GREPOGRAM_FAKE_MODELS=1`
- [x] write `tests/test_smoke.py`: `import grepogram`; `sqlite3` can `enable_load_extension` + load `sqlite_vec` (fails fast on an unsupported interpreter)
- [x] run `uv run pytest`, `uv run ruff check .`, `uv run mypy grepogram` — must pass before task 2

### Task 2: Paths, config and logging

**Files:**
- Create: `grepogram/paths.py`, `grepogram/config.py`, `grepogram/log.py`, `grepogram/models.py`
- Create: `tests/test_config.py`

- [x] `paths.py`: `Paths` dataclass (`config_file`, `session_file` ending in `.session`, `db_file`, `lock_file`, `log_dir`) built from `GREPOGRAM_HOME` or the macOS defaults; `ensure_dirs()` creates directories 0700
- [x] `models.py`: `Source`, `Config` (+ nested `TelegramCfg`, `ModelsCfg`, `SearchCfg`, `UnitsCfg`, `SyncCfg`) with the defaults from Technical Details; `ChatRow`, `MessageRow`, `UnitRow`, `Filters`, `Link`, `Hit`, `MessageView`, `SearchResult`, `SyncReport`, `SourceStatus`/`ChatStatus`
- [x] `config.py`: `load(paths) -> Config` via `tomllib` (missing file → defaults; unknown keys → `ConfigError` naming the key), `save(cfg, paths)` via `tomli_w` with mode 0600 (comment-lossy, documented), `TEMPLATE` = the annotated config from Technical Details, `Source.id` = stable string (`folder:Name` / `chat:<value>`)
- [x] `log.py`: `setup_logging(paths, level, stderr=True)` — stderr handler + `RotatingFileHandler`; never stdout; helper `redact(text)` used by any log line that could carry message text
- [x] write tests: defaults load with no file; round-trip save/load; 0600 mode; unknown key error; `GREPOGRAM_HOME` override; `session_file` ends with `.session`; `TEMPLATE` parses to the defaults; logging never attaches a stdout handler
- [x] run tests — must pass before task 3

### Task 3: Database layer and schema

**Files:**
- Create: `grepogram/db.py`
- Create: `tests/test_db.py`

- [x] `db.py`: `connect(paths | ":memory:") -> sqlite3.Connection` with `check_same_thread=False`, `PRAGMA journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `row_factory=sqlite3.Row`, `sqlite_vec.load`
- [x] `migrate(conn)`: versioned migrations list (`meta.schema_version`); v1 creates `meta`, `chats`, `users`, `messages` (surrogate `id`, `UNIQUE(chat_id, msg_id)`, FK cascade, indexes), `units` (FK cascade, index), `msg_fts`, `unit_fts` exactly as in Technical Details; `ensure_vec_table(conn, dim, drop=False)` creates `unit_vec` later and records `embed_dim` in `meta`
- [x] typed accessors used by later tasks: `upsert_chat`, `get_chat`, `list_chats`, `delete_chat` (fts + vec rows by rowid, then `chats` row → cascade), `upsert_users`, `upsert_messages(batch)` using `ON CONFLICT(chat_id, msg_id) DO UPDATE SET …` (never `INSERT OR REPLACE`) and returning affected `messages.id`s, `get_messages(chat_id, since_msg_id=None, topic_id=None)`, `get_message`, `set_meta/get_meta`
- [x] write tests: fresh migrate creates all tables/indexes; migrate twice is a no-op; `ensure_vec_table` creates `vec0` with the given dim and refuses a different dim unless `drop=True`; upsert updates in place and **preserves `messages.id` across an edit**; `delete_chat` cascades to messages/units and removes fts/vec rows; connection usable from a second thread
- [x] run tests — must pass before task 4

### Task 4: CLI skeleton

**Files:**
- Create: `grepogram/cli.py`
- Create: `tests/test_cli.py`

- [x] `cli.py`: typer `app` with `--version`, `config path` (prints resolved paths), `config init` (writes `TEMPLATE` unless the file exists, 0600), `--verbose` flag wiring `setup_logging`; sub-apps `sources`/`config` registered now, further commands added in later tasks
- [x] `_open_db()` helper: `connect` + `migrate`, and `_load()` returning `(paths, cfg, conn)` for commands
- [x] write tests with `typer.testing.CliRunner`: `--version`; `config path` respects `GREPOGRAM_HOME`; `config init` writes 0600 and refuses to overwrite; unknown command exits non-zero
- [x] run tests — must pass before task 5

### Task 5: Telegram client wrapper and `auth`

**Files:**
- Create: `grepogram/tg.py`
- Modify: `grepogram/cli.py`
- Create: `tests/test_tg.py`, `tests/fakes.py`

- [x] `tg.py`: `make_client(cfg, paths) -> TelegramClient` (session at `paths.session_file`, `flood_sleep_threshold` from config, `device_model="grepogram"`); `ensure_session_mode(paths)` asserts the session file exists (clear `SessionMissing` error otherwise) then chmods 0600
- [x] `AuthRequired(Exception)` with hint text `run: grepogram auth`; `wrap_auth_errors()` async context manager mapping `AuthKeyUnregisteredError`, `SessionRevokedError`, `UserDeactivatedError`, and `client.is_user_authorized() is False` to `AuthRequired`
- [x] `auth` CLI command: phone → code → optional 2FA password via `client.start(...)` callbacks; prints account name on success; refuses when `api_id == 0` with instructions pointing to my.telegram.org and `grepogram config init`
- [x] `tests/fakes.py`: `FakeClient` (async `get_dialogs`, `iter_messages`, `get_entity`, `__call__` for raw requests such as `GetDialogFiltersRequest`/`GetFullChannelRequest`) driven by dict fixtures — reused by tasks 6–9
- [x] write tests: error mapping table-driven; `auth` refuses without `api_id`; `ensure_session_mode` sets 0600 on an existing file and raises `SessionMissing` (not `FileNotFoundError`) when absent
- [x] run tests — must pass before task 6

### Task 6: Dialog listing, folders and fuzzy matching

**Files:**
- Create: `grepogram/dialogs.py`
- Modify: `grepogram/cli.py`
- Create: `tests/test_dialogs.py`

- [x] `dialogs.py`: `DialogInfo(id, title, type, username, is_forum, folders: list[str])`; `chat_type(entity)` → `user|bot|group|supergroup|channel`; `peer_id(entity)` as Telethon's marked id (`-100…` for channels/supergroups)
- [x] `fetch_folders(client) -> list[FolderInfo]` via `GetDialogFiltersRequest`: handle `DialogFilter` and `DialogFilterChatlist` (skip `DialogFilterDefault`), `title.text`, peers + flags
- [x] `DialogCatalog` (in-process memo of `get_dialogs()` + folders, `invalidate()` called by `sources_add`; no on-disk cache): `list_dialogs()`; folder membership via `folder_members(folder, dialogs)` (include ∪ pinned − exclude; flags → filter dialogs by type/contact/muted/unread/archived)
- [x] `match(query, dialogs, folders, limit=10)`: case-insensitive substring first, then `difflib.SequenceMatcher` ratio ≥ 0.6; returns dialogs and folders tagged with `kind`
- [x] `dialogs "<q>"` CLI command printing matches as a table
- [x] write tests: `folder_members` for explicit peers, each category flag, each exclude flag; `match` ordering; memo returns the same list until `invalidate()`; `title.text` handling
- [x] run tests — must pass before task 7

### Task 7: Sources management and resolution

**Files:**
- Create: `grepogram/sources.py`
- Modify: `grepogram/cli.py`
- Create: `tests/test_sources.py`

- [x] `parse_target(str) -> Target`: numeric id, `@username`, `https://t.me/<name>` / `t.me/c/<id>`, `folder:<name>`, otherwise `fuzzy:<text>`
- [x] `add_source(cfg, target, catalog)`: fuzzy/`folder:` resolved through `dialogs.match` (ambiguous → `AmbiguousTarget` listing candidates); duplicates rejected; `remove_source(cfg, conn, target)` removes the entry and calls `db.delete_chat` for every chat with that `source_id`
- [x] `resolve_sources(cfg, client, conn) -> list[ChatRow]`: for each source produce chats (folder → members, chat → entity) and `upsert_chat` with `source_id`, `type`, `title`, `username`, `is_forum`; returns resolved rows; unresolvable source → WARN and skip
- [x] `sources_status(cfg, conn) -> list[SourceStatus]` (source id, resolved chats with title/type/message count/`last_sync_at`/`unavailable`) — shared by `sources ls` and the MCP `sources` tool
- [x] `sources add|ls|rm` CLI commands
- [x] write tests: `parse_target` table; add/remove/duplicate/ambiguous; `remove_source` deletes chat data; `resolve_sources` with `FakeClient` for a folder source and a DM source; `sources_status` counts
- [x] run tests — must pass before task 8

### Task 8: Message mapping

**Files:**
- Create: `grepogram/sync.py`
- Create: `tests/test_sync_map.py`, `tests/fixtures/tl.py`

- [x] `tests/fixtures/tl.py`: builders creating real Telethon `types.Message` objects **with no client attached** (text, caption+photo, voice, document with filename, reply, forum-topic message, forwarded, service message, reactions, channel post)
- [x] `sync.py`: `map_message(msg, chat, names: dict[int, str]) -> MessageRow | None` implementing the mapping rules using raw TL attributes only (service → None; `msg.message`; reply/topic rules; `media_kind`; `media_filename` via `DocumentAttributeFilename`; `fwd_from`; `reactions_total`)
- [x] `sender_of(msg, names) -> tuple[int | None, str]` and `collect_users(entities)` building the names map from the peers returned with messages, for the `users` upsert
- [x] write tests: one case per fixture kind; forum topic root not treated as a reply; empty-text media message keeps `text=''`; `map_message` works on a message built without a client
- [x] run tests — must pass before task 9

### Task 9: Incremental sync with budget and lock

**Files:**
- Modify: `grepogram/sync.py`, `grepogram/cli.py`
- Create: `tests/test_sync.py`

- [x] `SyncBudget(seconds | None)` with `expired` property; `SyncLock(paths)` context manager using `fcntl.flock` → `SyncInProgress` when held elsewhere
- [x] `sync_chat(client, conn, chat, source, budget) -> SyncedChat(new_msg_ids)`: incremental `iter_messages(min_id, reverse=True, offset_date=since on first run)`, batches of 500 via `upsert_messages`, `last_msg_id` + `last_sync_at` update, then edit re-fetch of `edit_refetch` newest messages
- [x] channel comments (`comments=true`): resolve `linked_chat_id` via `GetFullChannelRequest`, upsert the discussion chat (`discussion_of`, same `source_id`), fetch `iter_messages(channel, reply_to=post.id)` for new posts and store under the discussion chat id; `MsgIdInvalidError` → skip post
- [x] error handling: `ChannelPrivateError` / `ChatAdminRequiredError` / `ChannelInvalidError` → `unavailable=1` and continue; `migrated_to` → new chat row linked; `FloodWaitError` beyond threshold → stop this chat, report; `AuthRequired` propagates
- [x] `sync_all(client, conn, cfg, paths, budget, embedder=None) -> SyncReport`: `resolve_sources` → chats ordered by `last_sync_at ASC` (never-synced first) → `sync_chat` until budget expires → per synced chat call `on_chat_synced(conn, chat, cfg, new_msg_ids)` (unit rebuild + indexing are wired in tasks 12/14; `embedder` used in task 19) — a module-level function this task's tests monkeypatch so they do not depend on later tasks
- [x] `sync [--budget S]` CLI command printing the report
- [x] write tests with `FakeClient`: first run stores all messages; second run fetches only `> last_msg_id`; edit re-fetch updates text and keeps `messages.id`; budget expiry leaves `chats_remaining`; private chat marked unavailable; comment with the same numeric id as a channel post does not overwrite it (different `chat_id`, different urls); lock contention raises `SyncInProgress`
- [x] run tests — must pass before task 10

### Task 10: Window builder

**Files:**
- Create: `grepogram/units.py`
- Create: `tests/test_units_windows.py`

- [x] `render_line(msg) -> str` (`[YYYY-MM-DD HH:MM] name: text`, media placeholder when text empty)
- [x] `cut_windows(messages, cfg.units, chat_id, topic_id) -> list[UnitRow]`: chronological, cut on gap/count/chars rules; `msg_ids` JSON, `msg_id_start/end`, `date_start/end`, `text`
- [x] `group_by_topic(messages)` for forum chats (`topic_id` None for non-forum)
- [x] write tests: gap cut, count cut, char cut, single message, empty input, topic grouping, placeholder rendering, deterministic output
- [x] run tests — must pass before task 11

### Task 11: Thread and post builders

**Files:**
- Modify: `grepogram/units.py`
- Create: `tests/test_units_threads.py`

- [x] `build_threads(messages, cfg.units, chat_id) -> list[UnitRow]`: reply graph, roots, BFS descendants in chronological order, cap with continuation units
- [x] `build_posts(conn, messages, chat, comments: bool, cfg.units) -> list[UnitRow]`: one `post` per channel message; with comments → `thread` = post text + comments read from the discussion chat (`chats.discussion_of = chat.id`), owned by the channel with `msg_ids = [post id]`, capped at `thread_max_msgs` like reply threads
- [x] `units_for_chat(conn, messages, chat, cfg) -> list[UnitRow]` choosing windows+threads for chats/groups, posts(+threads) for channels; discussion chats themselves (`discussion_of` set) get windows+threads like any group
- [x] write tests: linear chain, branching replies, reply to missing message (treated as root), cap + continuation, channel posts with and without comments (comments in a separate chat row)
- [x] run tests — must pass before task 12

### Task 12: Incremental unit maintenance wired into sync

**Files:**
- Modify: `grepogram/units.py`, `grepogram/db.py`, `grepogram/sync.py`
- Create: `tests/test_units_incremental.py`

- [x] `db.py`: `get_units`, `insert_units`, `delete_units(ids)`, `open_window(chat_id, topic_id)`, `threads_touching(chat_id, msg_ids)`, `mark_dirty`
- [x] `rebuild_for_chat(conn, chat, cfg, new_msg_ids) -> UnitDelta(inserted_ids, deleted_ids)`: delete open window(s) and re-cut from their `msg_id_start` with new messages; find thread roots reachable from new messages, delete those threads and rebuild; channels: add posts for new messages; all new/changed units `dirty=1`
- [x] `sync.on_chat_synced(conn, chat, cfg, new_msg_ids)` calls `rebuild_for_chat` (replacing the stub) so a sync produces units (the `cfg` argument was added: the rebuild needs `cfg.units` and the sources' `comments` flags)
- [x] write tests: property — syncing messages in two halves yields the same unit set as one pass; edit to a message inside a closed window does not re-cut (documented v1 limitation) but marks its thread dirty if any; a `FakeClient` sync end-to-end produces window units
- [x] run tests — must pass before task 13

### Task 13: Stemmer, tokenizer and FTS query builder

**Files:**
- Create: `grepogram/stem.py`
- Create: `tests/test_stem.py`

- [x] `tokenize(text) -> list[str]`: NFKC, lowercase, `\w+` (keeps digits and `_`), no stop-word removal
- [x] `stem_token(tok)`: Cyrillic → russian Snowball, Latin → english, else identity; `stem_text(text) -> str`
- [x] `fts_query(text, op: Literal["AND","OR"]) -> str | None`: stem + quote each token; `None` for no tokens
- [x] write tests: Russian inflections map to one stem (`счёт/счета/счетов`), English plurals, mixed-script sentence, punctuation and emoji stripped, digits kept, `fts_query` quoting of tokens containing `-`/`:`, `None` for emoji-only input; generated query is accepted by an in-memory FTS5 table
- [x] run tests — must pass before task 14

### Task 14: Lexical indexing

**Files:**
- Create: `grepogram/index.py`
- Modify: `grepogram/sync.py`
- Create: `tests/test_index_lexical.py`

- [x] `index_messages(conn, message_ids)`: `DELETE FROM msg_fts WHERE rowid=?` then insert with explicit `rowid = messages.id` (`raw`, `stemmed`, `chat_id`, `date`), skipping empty text
- [x] `index_units(conn, delta: UnitDelta)`: `DELETE FROM unit_fts WHERE rowid=?` for deleted ids, insert with `rowid = units.id` (`chat_id`, `date_start`); also calls `delete_unit_vectors(conn, deleted_ids)` (a no-op while `unit_vec` does not exist, a rowid delete once it does — already the real implementation, task 19 only adds tests with vectors present)
- [x] `index_chat(conn, chat, message_ids, delta)` called by `sync.on_chat_synced` right after `rebuild_for_chat` with the `UnitDelta` it returns — the per-chat step is one ordered function, not a hook list, because the indexer needs the rebuild's return value; task 19's `embed_dirty_units` runs once at the end of `sync_all`, not per chat
- [x] write tests: stemmed match finds inflected form; `chat_id` filter through UNINDEXED column as plain `AND`; deleted units disappear from `unit_fts`; re-index is idempotent; `EXPLAIN QUERY PLAN` for the delete shows a rowid lookup, not a scan
- [x] run tests — must pass before task 15

### Task 15: Deep links

**Files:**
- Create: `grepogram/links.py`
- Create: `tests/test_links.py`

- [x] `message_url(chat: ChatRow, msg_id, topic_id=None) -> Link(url, fallback_url | None)` per the link rules (public/private supergroup+channel, forum topics, user/bot with fallback, legacy group)
- [x] `strip_channel_prefix(chat_id)` (`-1001234 → 1234`)
- [x] `open_link(link)` → `subprocess.run(["open", url])` (macOS), returns url; non-darwin → `NotImplementedError` with message
- [x] write tests: table over all chat types × with/without username × forum; `open_link` uses a monkeypatched runner
- [x] run tests — must pass before task 16

### Task 16: Filter resolution and date parsing

**Files:**
- Create: `grepogram/filters.py`
- Create: `tests/test_filters.py`

- [x] `parse_when(s: str, now) -> int`: ISO date (`2025-06-01`), ISO month (`2025-06`), ISO datetime, relative `7d`/`3w`/`6m`/`1y`; `ValueError` with the accepted grammar otherwise
- [x] `resolve_chats(conn, cfg, specs: list[str]) -> set[int]`: numeric ids, `@username`, `folder:<name>`/folder names (via `chats.source_id`), fuzzy `chats.title` (substring, then `SequenceMatcher` ≥ 0.6); unknown → `UnknownChat` listing indexed titles
- [x] `resolve_filters(conn, cfg, chats, since, until, now) -> Filters`
- [x] write tests: `parse_when` table incl. errors; `resolve_chats` for each spec kind and the unknown path; `resolve_filters` composes correctly
- [x] run tests — must pass before task 17

### Task 17: Lexical search end-to-end and `search` CLI (first usable milestone)

**Files:**
- Create: `grepogram/search.py`
- Modify: `grepogram/cli.py`
- Create: `tests/test_search_lexical.py`, `tests/fixtures/chat_ru.py`

- [x] `lexical_units(conn, q, filters, limit)` and `lexical_messages(conn, q, filters, limit)`: `bm25()` ordered ASC and negated into a score, AND→OR fallback, `chat_id`/date as plain `AND` predicates; message hits mapped to containing window with `anchor_msg_id`; `fts_query() is None` → empty list; the two lists are fused with `rrf` (pulled forward from task 21: a unit the message list also finds ranks first, and the unit list alone would make the message list redundant)
- [x] `build_hit(conn, unit, anchor_msg_id, score, full)` — snippet (anchor ± neighbours ≤ 600 chars), `url`/`fallback_url` via `links.message_url`, `chat` row
- [x] `search(conn, cfg, query, filters, k, mode="lexical", full=False) -> SearchResult` (hybrid/dense modes raise `NotImplementedError` until task 21); `index_age_min` computed; empty `fts_query` → empty result with a warning
- [x] `search` CLI command with `--chat/--since/--until/--mode/-k/--full/--json` and readable text output (score, chat, date range, url, snippet)
- [x] `tests/fixtures/chat_ru.py`: ~60 synthetic Russian/English messages across two chats with reply chains about banks, SIM cards and visas, plus a synonym pair the FakeEmbedder can bridge later
- [x] write tests: full pipeline in memory (`upsert → rebuild_for_chat → index_chat → search`): inflected query hits; chat filter excludes other chat; date filter; AND→OR fallback observed; message hit maps to window with correct anchor and url; emoji-only query → warning; CLI JSON output shape
- [x] run tests — must pass before task 18

### Task 18: Embedder protocol with fake and bge-m3 implementations

**Files:**
- Create: `grepogram/embed.py`
- Create: `tests/test_embed.py`, `tests/test_embed_slow.py`

- [ ] `Embedder` Protocol (`name: str`, `dim: int`, `embed(texts: list[str]) -> list[list[float]]`, `embed_query(text) -> list[float]`); `ModelUnavailable(Exception)`
- [ ] `FakeEmbedder(dim=8)`: deterministic token-hash bag-of-words vectors, L2-normalized — similar texts land close
- [ ] `BgeM3Embedder(model_id, device)`: lazy `sentence_transformers` import; `device="auto"` → `mps` if `torch.backends.mps.is_available()` else `cpu` (one-time WARN); fp16 on mps; `model.max_seq_length = 512`; `batch_size=32`, `normalize_embeddings=True`; import/download failure → `ModelUnavailable(reason)`
- [ ] `load_embedder(cfg) -> Embedder` (`GREPOGRAM_FAKE_MODELS=1` → fake, for tests and CI)
- [ ] write tests: fake determinism and normalization; nearer texts have higher cosine; `load_embedder` raises `ModelUnavailable` when the import fails; device selection logic — both via stub modules injected into `sys.modules['torch']` / `sys.modules['sentence_transformers']` so the tests pass without the `dense` extra
- [ ] `tests/test_embed_slow.py` (`slow`): real bge-m3 embeds a RU and an EN paraphrase closer than an unrelated sentence, cosine above a threshold (catches fp16 collapse on MPS)
- [ ] run tests — must pass before task 19

### Task 19: Dense indexing and KNN

**Files:**
- Modify: `grepogram/index.py`, `grepogram/db.py`, `grepogram/sync.py`, `grepogram/cli.py`
- Create: `tests/test_index_dense.py`

- [ ] `ensure_embedding_space(conn, embedder, reembed: bool)`: compares `meta.embed_model/embed_dim`; mismatch → `EmbeddingSpaceMismatch` unless `reembed`, which calls `ensure_vec_table(drop=True)`, clears `embedded_model`, sets `dirty=1`
- [ ] `embed_dirty_units(conn, embedder, batch=256, budget=None) -> int`: select `dirty=1`, embed in batches, upsert into `unit_vec` (rowid = unit id, `chat_id`, `date_start`), set `dirty=0`, `embedded_model`
- [ ] `delete_unit_vectors(conn, ids)` (already real since task 14: no-op when `unit_vec` is absent, rowid delete otherwise — verify with vectors present) and `db.delete_chat` extended to remove the chat's vectors
- [ ] `knn(conn, qvec, filters, k, fanout_max) -> list[tuple[int, float]]`: per-`chat_id` KNN with partition constraint when `len(chat_ids) <= fanout_max`, else one KNN with `k*4` post-filtered; `date_start` metadata constraints; `[]` when `unit_vec` is absent or empty; merged and sorted by distance
- [ ] `sync_all(..., embedder)` calls `embed_dirty_units` after indexing when an embedder is given; `embed [--reembed]` CLI command; CLI `sync` loads the embedder (warning and skip on `ModelUnavailable`)
- [ ] write tests (FakeEmbedder): dirty units embedded once; re-run is a no-op; re-cutting the open window removes the old unit's vector and `knn` never returns a rowid absent from `units`; KNN respects chat and date filters in both fan-out branches; `knn` on a DB without `unit_vec` returns `[]`; mismatch raises; `--reembed` rebuilds with new dim
- [ ] run tests — must pass before task 20

### Task 20: Reranker protocol with fake and bge implementations

**Files:**
- Create: `grepogram/rerank.py`
- Create: `tests/test_rerank.py`, `tests/test_rerank_slow.py`

- [ ] `Reranker` Protocol (`score(query, texts) -> list[float]`); `FakeReranker` (token-overlap score)
- [ ] `BgeReranker(model_id, device)`: lazy `sentence_transformers.CrossEncoder`, same device logic as the embedder, `max_length=512`, `ModelUnavailable` on import/download failure
- [ ] `load_reranker(cfg)` honouring `GREPOGRAM_FAKE_MODELS=1`
- [ ] write tests: fake ordering; loader failure via `sys.modules` stubs; device selection
- [ ] `tests/test_rerank_slow.py` (`slow`): real reranker ranks the relevant RU unit above a distractor
- [ ] run tests — must pass before task 21

### Task 21: RRF fusion, dedup and hybrid search

**Files:**
- Modify: `grepogram/search.py`, `grepogram/cli.py`
- Create: `tests/test_search_hybrid.py`

- [ ] `rrf(rankings: list[list[int]], k) -> dict[int, float]` (exists in `search.py` since task 17, where lexical mode already fuses the unit and message lists; extend its test with the three-list case); `dedup(hits, overlap) -> list[Hit]` by `msg_ids` overlap against higher-scored hits
- [ ] `search(...)` for `mode="hybrid"|"dense"`: lexical lists + dense list → `rrf` → top `rerank_top` → rerank (when `rerank=True` and a reranker loads) → dedup → top `k`; `ModelUnavailable` or empty/missing `unit_vec` → fall back to lexical with `warnings`; empty `fts_query` in hybrid → dense-only
- [ ] CLI: `--mode hybrid|dense`, `--no-rerank`; hybrid becomes the default
- [ ] write tests: `rrf` math on a hand-computed example; `dedup` keeps higher score; hybrid finds the fixture's synonym paraphrase that lexical misses; dense unavailable → lexical result + warning; hybrid on a never-embedded DB → lexical hits + warning, no exception; `rerank=False` skips reranker
- [ ] run tests — must pass before task 22

### Task 22: Thread and context readers

**Files:**
- Modify: `grepogram/search.py`, `grepogram/db.py`
- Create: `tests/test_readers.py`

- [ ] `db.py`: `get_thread_messages(chat_id, msg_id)` — walk up `reply_to_msg_id` to the root, then all descendants chronological; `get_context_messages(chat_id, msg_id, before, after)` — by `msg_id` order within the same `topic_id`
- [ ] `search.py`: `thread(conn, chat_id, msg_id)` and `context(conn, chat_id, msg_id, before, after)` returning `MessageView` lists with `url`/`fallback_url`; unknown message → `UnknownMessage`
- [ ] write tests: thread from a leaf reaches root and siblings; context respects before/after and topic boundary; error path
- [ ] run tests — must pass before task 23

### Task 23: MCP server

**Files:**
- Create: `grepogram/mcp.py`
- Create: `tests/test_mcp.py`

- [ ] `mcp.py`: `FastMCP("grepogram", instructions=INSTRUCTIONS)`; `AppState` holding `paths/cfg/conn`, `DialogCatalog`, lazily-created Telethon client, embedder, reranker; `main()` sets up logging to stderr+file only, wraps startup and every tool body in `contextlib.redirect_stdout(sys.stderr)`, and runs stdio transport
- [ ] tools `search`, `thread`, `context`, `sync`, `sources`, `dialogs`, `sources_add`, `sources_remove`, `open_message` as thin wrappers (`sources` → `sources_status`; `open_message` fetches the message first for `topic_id`) returning JSON-serializable dicts; docstrings become tool descriptions (include filter syntax and the `mode` values)
- [ ] staleness in `search`: `index_age_min > auto_sync_after_min` → `sync_all(budget=auto_sync_budget_s, embedder)` first, `synced=true`; auto-sync failures (`AuthRequired`, `SyncInProgress`, `FloodWaitError`, budget overrun) become `warnings`, search proceeds
- [ ] `AuthRequired` and `ModelUnavailable` from explicit tools surface as results with `error`/`hint` fields, never as crashes; `open_message` returns the url even when `open` is unavailable
- [ ] write tests: tool functions called directly against in-memory state with the fixture chat: `search` returns hits with `url`; stale index triggers the sync callable exactly once and a failing auto-sync yields warnings not errors; `sources_add` with a fuzzy title writes config and invalidates the catalog; `dialogs` returns matches; `AuthRequired` surfaces as `error`; tool called from a worker thread works; `capfd` around `search` + `sync` + `sources_add` asserts `captured.out == ""`; one in-process MCP client session lists the nine tools
- [ ] run tests — must pass before task 24

### Task 24: Static checks, CI and slow-test gate

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `pyproject.toml` (if config gaps surface)

- [ ] `ruff check . && ruff format --check .` clean; `mypy grepogram` strict clean (typed shims or targeted `# type: ignore[...]` with reason for Telethon/sentence-transformers)
- [ ] CI: macOS job — `uv sync --group dev` (no `dense` extra; `GREPOGRAM_FAKE_MODELS=1`, torch is never downloaded), ruff, mypy, `pytest --cov=grepogram --cov-report=term-missing`; ubuntu job — ruff + mypy only
- [ ] `uv run pytest -m slow` run once locally on this Mac with real models; record embed throughput (units/s) in README
- [ ] write tests: none new — this task verifies the suite; add regression tests for anything mypy/ruff/coverage surfaces (modules below 80% line coverage get tests before moving on)
- [ ] run full suite — must pass before task 25

### Task 25: Verify acceptance criteria
- [ ] verify all requirements from Overview are implemented: opt-in sources (folder + any chat type), incremental sync with budget, channel comments in their discussion chat, windows/threads/posts, stemmed FTS5 keyed by rowid, bge-m3 dense over units with vector cleanup, RRF + rerank + dedup, date/chat filters, deep links with fallback, MCP tools + instructions, auto-sync on stale with warnings, degradation without models, stdout hygiene, CLI parity
- [ ] verify edge cases: forum topics, channel with `comments=true`, private chat becoming unavailable, empty config (`search` reports no sources), model mismatch, concurrent sync lock, never-embedded DB in hybrid mode
- [ ] run full test suite: `uv run pytest --cov=grepogram && uv run ruff check . && uv run ruff format --check . && uv run mypy grepogram`
- [ ] run `uv run pytest -m slow` locally
- [ ] review each module's test file against its public functions; coverage report shows no module below 80%

### Task 26: [Final] Update documentation
- [ ] README: what/why, 5-minute setup (my.telegram.org → `uv tool install` or `uv run` → `grepogram config init` → `grepogram auth` → `grepogram sources add "folder:Argentina"` → `grepogram sync` → `claude mcp add grepogram -s user -- uv run --project <path> grepogram-mcp`), MCP tool list, config reference, how search works (one diagram), privacy notes, roadmap (OCR, voice, documents, export import, prune)
- [ ] create `CLAUDE.md` for the repo: commands (`uv run pytest`, ruff, mypy), commit convention, "stdout is the MCP protocol", `GREPOGRAM_FAKE_MODELS`, where fixtures live, raw-TL-attributes rule
- [ ] move this plan to `docs/plans/completed/`

## Post-Completion

*Items requiring manual intervention or external systems — no checkboxes, informational only*

**Manual verification:**
- Create an application at my.telegram.org (api_id/api_hash), `grepogram config init`, fill in the keys, run `grepogram auth`
- Add one real country chat, run `grepogram sync`, then `grepogram search` with 5–10 real questions in Russian and English; compare lexical vs hybrid vs hybrid+rerank; tune `window_gap_min`, `window_max_chars`, `rerank_top` in config
- Confirm on a real chat that `since` on a first sync skips *older* history (Telethon inverts `offset_date` under `reverse=True`); if it fetches the wrong side, switch to `offset_id`-based paging
- Register the MCP server in Claude Code (`claude mcp add … -s user`), ask Claude the same questions, check that it follows the `instructions` (query variants, citations, `thread` before concluding) and that `open_message` lands on the right message in Telegram Desktop and the native macOS app
- Watch first-sync time and FloodWait behaviour on a large (>100k messages) chat; if it is painful, `since` on the source is the knob; check MPS memory with `max_seq_length = 512` and batch 32

**Follow-ups (not v1):**
- `sources prune` (chats that left a folder) — `sources rm` covers deliberate removal in v1
- OCR of photos via macOS Vision (attachments table + `extracted_text` feeding `unit` text)
- Voice/video-note transcription (whisper.cpp) and PDF/DOCX extraction
- `grepogram import <tdesktop export dir>` for chats no longer accessible
- Deletions handling and `reactions_total` as a ranking signal
- Publishing to PyPI under `grepogram`
