# Multiple accounts and Telegram research

> Where this plan describes existing code it cites the file it was read from on `main` at
> `791c6e0`. Read the code, not this file, when the two disagree, and update this file when the
> implementation has to deviate.

## Overview

Two features, planned together because account identity runs through everything research does
(who discovers, who joins, who is approved to fetch, who owns the resulting source):

1. **Simultaneous multiple accounts.** Several Telegram accounts are signed in at once; every
   one of them fetches its sources, all of their material lands in one index, one search spans
   them, and every result says which account(s) it came through. Switching a single active
   account is not the goal and is not built.
2. **Opt-in research.** Starting from a question and some indexed seed chats, grepogram discovers
   chats the user does not index yet — through links, mentions, hyperlinks, buttons and pinned
   posts, forward origins, directory channels, shared-folder (`addlist`) links, and optionally
   Telegram's own chat and public-post search — presents them with the evidence that led there,
   and after a *human* approves specific targets and actions, joins / requests / fetches them and
   follows further leads within depth, time and message budgets. Approved discoveries become
   ordinary ongoing sources; stopping a research session removes nothing.

Decisions taken with the user before planning:

- **Unified index** (not one index per account, not composite keys everywhere). Channels and
  supergroups — global ids, global message ids — stay one shared `chats` row fetched once; DMs,
  bots and legacy groups — per-account message ids — get an account-scoped row. A `chat_access`
  table records which accounts reach a chat, `chat_sources` which sources cover it.
- **Approval = MCP elicitation, else CLI.** `research_approve` asks the human through
  `ctx.elicit()`; a host without elicitation gets a refusal naming the exact
  `grepogram research approve …` command, which only confirms on an interactive TTY. No tool
  parameter can stand in for consent.
- **Regular testing** (code, then tests in the same task), **one branch** `feat/accounts-research`,
  multi-account first, research second, `/planning:exec` → `/revmux:revmux` → one PR.

## Context (from discovery)

- `grepogram/db.py:58-129` — `_V5`: `chats.id INTEGER PRIMARY KEY` is the Telegram marked peer id;
  `messages` is `UNIQUE (chat_id, msg_id)`; `msg_fts`/`unit_fts` carry `chat_id UNINDEXED`;
  `users(id)` global. `_V6` (db.py:131) is the last step, `MIGRATIONS = {5: _V5, 6: _V6}`
  (db.py:171). CLAUDE.md: schema is append-only, no step transforms rows.
- `grepogram/tg.py` — one session: `load_session(paths)` copies DC + auth key into a
  `MemorySession`; `make_client(cfg, paths)`, `make_login_client`, `ensure_session_mode(paths)`,
  `AUTH_HINT = "run: grepogram auth"`.
- `grepogram/paths.py` — `Paths(config_file, session_file, db_file, lock_file, log_dir)`,
  `Paths.under(root)` / `macos_default(home)`; `FileLock` base of `SyncLock` and `ConfigLock`.
- `grepogram/config.py` — sections `telegram, models, search, units, sync, media` + `[[sources]]`
  with keys `folder, chat, since, comments` (config.py:84); `update(paths, change)` under
  `ConfigLock`; `TEMPLATE`.
- `grepogram/models.py` — `Source.id` is `folder:<title>` / `chat:<value>`; `ChatRow` has no
  account or peer field; `Hit`, `MessageView` carry `chat_id` only.
- Telegram call sites keyed on `chat.id` (all must address the peer, not the row):
  `sync.py:743, 835, 881, 1064, 1075, 1125, 1144, 1940-1970, 2010`, `media.py:310, 357`,
  `links.message_url` (links.py:40), `map_message` compares `from_id == chat.id` and
  `reply_of(msg.reply_to, chat.id)` (sync.py:186-221).
- `sources.resolve_sources` (sources.py:690) builds one `DialogCatalog(client)`; a chat covered
  by two sources keeps the first; `remove_source` (sources.py:484) deletes every chat whose
  `source_id` matches. `parse_target` rejects invite links (sources.py:236).
- `sync.sync_all` (sync.py:1490) takes one client, one `SyncLock`, one `SyncBudget`;
  `_sync_chats` (sync.py:1638) breaks the whole loop on a flood wait (`_record_failure`).
  `warm_peer_cache` (sync.py:1881) warms one client.
- `sync.forward_of` (sync.py:326) flattens `msg.fwd_from` into a display string: the origin peer
  and post id are lost. Nothing stores `msg.entities` (hidden `text_url` hyperlinks, mentions)
  or `msg.reply_markup` (URL buttons).
- `mcp.AppState.telegram()` (mcp.py:290) builds one client from `client_factory(cfg, paths)`;
  eight tools; `INSTRUCTIONS` (mcp.py:75) is the agent playbook.
- `cli.py` — typer app with `config` and `sources` sub-apps; `auth`, `dialogs`, `sync`,
  `extract`, `prune-deleted`, `import`, `embed`, `search`, `thread`, `context`.
- `tests/fakes.py` — `FakeClient` refuses unlearned peers (`strict_entities`), answers raw
  requests through `__call__`; one client = one account.
- Verified in the locked environment: Telethon 1.44.0 (layer 227) has
  `channels.SearchPostsRequest(…, allow_paid_stars=…)`, `channels.CheckSearchPostsFloodRequest`,
  `messages.CheckChatInviteRequest` / `ImportChatInviteRequest`,
  `chatlists.CheckChatlistInviteRequest` / `JoinChatlistInviteRequest`, `contacts.SearchRequest`,
  `messages.SearchGlobalRequest`, `channels.JoinChannelRequest`; `ChatInvite`,
  `ChatInviteAlready`, `ChatInvitePeek` types. mcp 1.29.1 `Context.elicit` exists.

## Development Approach

- **testing approach**: Regular (code first, then tests in the same task)
- complete each task fully before moving to the next; small, focused, granular commits
  (`type(scope): lowercase description`, scope required, no trailers)
- **CRITICAL: every task MUST include new/updated tests** for the code it changes — success and
  error paths; `FakeClient` stays as strict as production (a peer it has not learned is refused)
- **CRITICAL: all checks pass before the next task**: `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`
- **CRITICAL: update this plan file when scope changes during implementation**
- backward compatibility: an existing single-account install (`session.session`, config without
  `[[accounts]]`, index at schema 6) keeps working with no user action beyond upgrading
- CLAUDE.md rules stay binding: stdout is the protocol, `map_message` reads raw TL attributes
  only, lock order `SyncLock` → `ConfigLock` → thread lock, every config edit through
  `config.update` / `editing_config`, every `chats.source_id` writer asks `imported_tag`, every
  `db` writer inside `db.transaction`, units never import `index`

## Testing Strategy

- **unit tests**: required in every task; in-memory SQLite, fake models, no network
- **multi-account fakes**: tests build one `FakeClient` per account from one shared "world" of
  entities and messages, with per-account membership, per-account DM histories (different
  `msg_id`s for the same conversation) and per-account access hashes, so shared-vs-scoped
  identity is exercised the way Telegram behaves
- **migration tests**: a schema-6 database built with the v0.2.0 steps (and one holding an
  `import:` chat) migrates to the new version with every row, unit, FTS and vec row intact
- no UI e2e tests exist in this project; the MCP tests (`tests/test_mcp.py`) are the protocol-level
  tests and keep asserting stdout stays empty

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix; document blockers with ⚠️ prefix
- keep this plan in sync with the work actually done

## Solution Overview

### Accounts

- **Config.** A new `[[accounts]]` array: `name` (`[a-z0-9_-]{1,32}`) and optional `label`. The
  implicit account `default` always exists and uses the existing `session.session`, so a config
  without `[[accounts]]` is exactly today's install. Other accounts keep their session at
  `<config dir>/sessions/<name>.session` (0600, dir 0700). `[telegram] api_id/api_hash` stay
  shared: one API app signs in many accounts.
- **Sources name an account.** `[[sources]]` gains `account` (omitted = `default`). `Source.id`
  stays `chat:<v>` / `folder:<t>` for `default` and becomes `<account>/chat:<v>` /
  `<account>/folder:<t>` otherwise, so two accounts can each hold `chat = 12345` (two different
  DMs) and every existing `chats.source_id` stays valid.
- **Chat identity (schema step 7).** `chats` gains `peer_id` (Telegram marked id) and `scope`
  (`''` for channel/supergroup, the account name for user/bot/group), unique on
  `(scope, peer_id)`. **Invariant: a shared row's `id` equals its `peer_id`**, so
  `discussion_of`, `comment_of_chat_id`, `migrated_to` and `t.me/c/` links — all channel-space —
  are untouched. A scoped row takes `id = peer_id` when that id is free and a synthetic id
  (`>= 1 << 62`, far above any Telegram id) otherwise. Everything that talks to Telegram or
  builds a link uses `peer_id`; everything internal keeps using `id`.
- **Access and coverage.** `chat_access(chat_id, account, access_hash, via, checked_at)` —
  which accounts can reach a chat, and the account-specific access hash so a fresh client can
  address a stored chat without a dialog walk or a rate-limited username resolve.
  `chat_sources(chat_id, source_id)` — every source that currently covers a chat;
  `chats.source_id` remains the *primary* owner that the existing import / discussion-ownership
  rules read. Removing a source deletes a chat only when no remaining configured source covers
  it; otherwise the primary moves to a remaining one (never over an `import:` tag).
- **Step 7 fills columns in place** (`peer_id = id`, `scope` from `type`, `chat_sources` from
  `source_id`, `chat_access` for `default`). This is the first step that writes rows. It is
  justified: imported history cannot be rebuilt from Telegram, so "delete index.db and sync
  again" is not an acceptable upgrade path. CLAUDE.md is amended to say so: steps may *fill new
  columns deterministically from existing ones*, never rewrite existing values.
- **Sync across accounts.** `sync_all` takes an account→client mapping. Each resolved chat is
  fetched once, by the account of its primary source, falling back to another account in
  `chat_access` when that one is refused. Account queues run concurrently (`asyncio.gather`) under
  the one `SyncLock` and one `SyncBudget` (the DB connection already serialises statements); a
  flood wait stops only that account's queue. `index_pending`, `index_stranded`, re-cut and
  embedding stay once per run. Each account signs its own "me".
- **Queries.** One index, one search. `account:<name>` chat specs and an `accounts` search
  parameter scope a query to chats that account reaches; this is a **scope, not isolation** —
  every account belongs to the same local user, and opt-in sources already bound what is indexed.
  `Hit` and `MessageView` gain `peer_id` and `accounts`; a user-supplied chat id that names two
  scoped rows is ambiguous and answered with candidates (`<account>/<peer>` disambiguates).

### Research

- **Link and provenance capture (schema step 8).** `messages` gains `fwd_peer_id`,
  `fwd_msg_id`, `fwd_date` (structured origin, the display `fwd_from` stays). A
  `message_links(message_id, kind, target)` table holds every Telegram destination a message
  names: plain URLs, `text_url` hyperlinks, `@mentions` / `mention_name`, URL buttons, webpage
  preview URLs, invite and `addlist` links. `map_message` fills both from raw TL attributes
  (`msg.entities`, `msg.reply_markup`, `msg.fwd_from`, `msg.media.webpage`) — added to the
  CLAUDE.md list. Rows stored before step 8 have none; discovery falls back to a regex over their
  stored text (visible URLs and mentions only) and says so.
- **Research state lives in `research.db`**, a separate 0600 SQLite file next to `index.db`
  with its own version. The index is derived and may be deleted and rebuilt; approvals,
  exclusions and session history are the user's decisions and must survive that.
- **Sessions**: question, seed chats, acting account, limits (depth, candidates, `since`
  horizon, messages per run, run seconds), state (`active` / `stopped`), progress.
- **Candidates**: one per `(session, target identity)`: username, peer, invite hash, `addlist`
  slug or post; depth; status (`proposed`, `approved`, `skipped`, `excluded`, `joined`,
  `pending_admission`, `fetched`, `unavailable`, `failed`); and three separate facts that are
  never conflated: **membership** (the acting account is in the chat), **cached** (the chat is
  already in `index.db`, and through which accounts), **authorization** (a live grant exists).
- **Evidence**: every path that led to a candidate — `via` (`link`, `mention`, `text_url`,
  `button`, `pinned`, `forward`, `directory`, `shared_folder`, `chat_search`, `post_search`),
  the source chat/message, a snippet, and an **origin key**: forwards of one post share the
  origin `(fwd_peer_id, fwd_msg_id)` and count once, so a post forwarded into ten chats is one
  piece of corroboration, not ten.
- **Probing** (read-only metadata, no history): resolve username, `messages.checkChatInvite`,
  `chatlists.checkChatlistInvite`, bounded per discover call and flood-aware. Probing tells the
  approval what it is approving (title, type, size, public/private, member or not, request
  needed). Fetching any history — pinned posts included — needs a grant.
- **Global discovery** (`contacts.search`, `channels.searchPosts`) is off unless `[research]`
  enables it *and* the session holds a `global_search` grant whose approval text disclosed that
  queries reach Telegram and may return snippets from unknown channels. `searchPosts` is preceded
  by `checkSearchPostsFlood`; `allow_paid_stars` is never sent unless `paid_stars_max > 0` and a
  separate `paid_search` grant exists. Search results are stored only as evidence in
  `research.db`, never as `messages` rows, so no sync cursor moves.
- **Grants** name a session, one candidate (or the session for search actions), the acting
  account, the concrete actions (`fetch`, `join`, `request`, `add_source`), the channel that
  produced them (`elicitation` | `cli`) and the time. Approving a directory or a chat grants
  nothing for what is discovered inside it. A grant is reused by later runs until consumed or
  the session stops; nothing asks twice for approved work. Skips and exclusions need no consent
  (they only narrow); exclusions are global and persistent.
- **Run**: for each granted candidate, in order — join (`channels.joinChannel`,
  `messages.importChatInvite`, `chatlists.joinChatlistInvite` with exactly the approved peers) or
  record `pending_admission` on `InviteRequestSentError`; add the ongoing source through
  `config.update` (account, `since` = horizon, `comments` for channels); sync exactly those
  chats through `sync_all(only=…)` under the `SyncLock`, the research time budget and a message
  cap; then discover over the newly stored messages at depth + 1 (proposed, never auto-approved).
  Pending admissions are re-checked at the start of every run. The run is resumable from
  `research.db` alone.
- **Stop** marks the session stopped, voids its unconsumed grants and leaves every source it
  added. **Removing a source never leaves a chat**; leaving is its own CLI-only command.

### Interfaces

CLI and MCP share `grepogram/research.py` and `research.db`.

| CLI | MCP | does |
|---|---|---|
| `research start` | `research_start` | question, seeds, account, limits |
| `research discover` | `research_discover` | offline leads + bounded probing (+ global search if granted) |
| `research candidates` | `research_candidates` | candidates with evidence and the three access facts |
| `research approve` (TTY only) | `research_approve` (elicitation) | grant named targets × concrete actions |
| `research skip` / `exclude` | `research_skip` / `research_exclude` | narrow, no consent needed |
| `research run` | `research_run` | execute granted work within budgets |
| `research status` | `research_status` | progress, pending grants, pending admissions |
| `research stop` | `research_stop` | stop exploring; sources stay |
| `accounts ls` / `accounts rm`, `auth --account`, `leave` | `accounts` (read-only) | account management |

Research tools refuse with a hint while `[research] enabled = false` (the default); ordinary
`search` never expands scope.

## Technical Details

### Config additions

```toml
[[accounts]]
name = "work"                  # sessions/work.session; "default" is implicit (session.session)
label = "work phone"

[[sources]]
chat = "@some_channel"
account = "work"               # omitted → default

[research]
enabled = false                # research tools refuse until this is true
chat_search = false            # contacts.search for public chats by name
post_search = false            # channels.searchPosts (public posts)
paid_stars_max = 0             # 0 = never pay for post search
max_depth = 2
max_candidates = 50            # per discover call
probe_limit = 20               # username / invite / addlist probes per discover call
since_days = 365               # horizon given to sources a research run adds
max_messages_per_run = 5000
run_budget_s = 300
```

### Schema step 7 (accounts)

```sql
CREATE TABLE accounts(name TEXT PRIMARY KEY, user_id INTEGER, display_name TEXT, added_at INTEGER);
ALTER TABLE chats ADD COLUMN peer_id INTEGER;
ALTER TABLE chats ADD COLUMN scope TEXT NOT NULL DEFAULT '';
UPDATE chats SET peer_id = id;
UPDATE chats SET scope = 'default' WHERE type IN ('user', 'bot', 'group');
CREATE UNIQUE INDEX chats_scope_peer ON chats(scope, peer_id);
CREATE TABLE chat_access(chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    account TEXT NOT NULL, access_hash INTEGER, via TEXT, checked_at INTEGER,
    PRIMARY KEY (chat_id, account));
CREATE TABLE chat_sources(chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL, PRIMARY KEY (chat_id, source_id));
INSERT INTO chat_sources SELECT id, source_id FROM chats WHERE source_id IS NOT NULL;
INSERT INTO chat_access(chat_id, account, via) SELECT id, 'default', 'migrated' FROM chats
    WHERE source_id IS NULL OR source_id NOT LIKE 'import:%';
```

An empty database gets `_V5`, `_V6` and step 7 in order (`migrate` already does this), so the
updates run on no rows. `db.upsert_chat` looks rows up by `(scope, peer_id)`; `db.SYNTHETIC_BASE
= 1 << 62`; `ChatRow` gains `peer_id: int` and `scope: str = ""`; `chat_scope(type, account)`
is the one rule.

### Schema step 8 (research capture)

```sql
ALTER TABLE messages ADD COLUMN fwd_peer_id INTEGER;
ALTER TABLE messages ADD COLUMN fwd_msg_id INTEGER;
ALTER TABLE messages ADD COLUMN fwd_date INTEGER;
CREATE INDEX messages_fwd_origin ON messages(fwd_peer_id, fwd_msg_id) WHERE fwd_peer_id IS NOT NULL;
CREATE TABLE message_links(message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, target TEXT NOT NULL, PRIMARY KEY (message_id, kind, target));
CREATE INDEX message_links_target ON message_links(target);
```

`target` is normalized by `grepogram/leads.py` (`@name`, `@name/123`, `c/<id>/<post>`,
`+<hash>`, `addlist/<slug>`, `peer:<marked id>`); non-Telegram URLs are not stored.
`upsert_messages` replaces a message's links with what the fresh row carries. `RECIPE_VERSION`
does not move: unit text is unchanged.

### research.db (own schema, `research_db.SCHEMA_VERSION = 1`)

`sessions(id, question, account, seeds JSON, limits JSON, state, created_at, stopped_at)`,
`candidates(id, session_id, identity UNIQUE per session, kind, peer_id, username, invite_hash,
addlist_slug, title, type, depth, status, member, access_hash, request_needed, probed_at, note)`,
`evidence(id, candidate_id, via, chat_id, msg_id, origin_key, snippet, found_at)`,
`grants(id, session_id, candidate_id NULL, account, actions JSON, via, granted_at, consumed_at,
voided_at)`, `exclusions(identity PRIMARY KEY, reason, created_at)`,
`searches(id, session_id, kind, query, ran_at, results)`. Opened with `paths.PRIVATE_FILE_MODE`.

### Consent flow

1. `research_approve(session_id, items=[{candidate_id, actions}])` builds a plain-text summary:
   per target the title, account, membership, and each action in words ("join
   @x as work", "send an admission request to …", "fetch history since 2025-09-30", "**add as an
   ongoing source** — regular sync and search will include it"); for `global_search` the
   disclosure.
2. If `ctx.session.client_params.capabilities.elicitation` is present →
   `ctx.elicit(message=summary, schema=Confirm)` where `Confirm` has one boolean `approve`.
   `accept` + `approve=True` → grants written with `via='elicitation'`; decline / cancel → nothing.
3. Otherwise → error result with `hint`: `run in a terminal: grepogram research approve <sid>
   <ids…>` (the exact items). The CLI prints the same summary and reads the confirmation from
   `/dev/tty`; with no TTY it refuses. There is no `--yes`.

## What Goes Where

- **Implementation Steps**: code, tests, docs in this repository
- **Post-Completion**: signing in a second real account, live checks against Telegram

## Implementation Steps

### Task 1: Accounts in config, models and paths

**Files:**
- Modify: `grepogram/models.py`, `grepogram/config.py`, `grepogram/paths.py`
- Modify: `tests/test_config.py` (the paths tests live there, not in `tests/test_smoke.py`),
  `tests/test_mcp.py` (`sources_add` returns the source's `account`)
- Modify: `README.md` — `test_readme_config_block_is_the_template_verbatim` pins the `TEMPLATE`
  mirror, so the new `[[accounts]]` / `account` lines and their key rows land here, not in task 20

- [x] `models.AccountCfg(name, label)`, `DEFAULT_ACCOUNT = "default"`, `Config.accounts`,
  `Config.account_names()` (default first); `Source.account: str = DEFAULT_ACCOUNT` and the
  `Source.id` scheme (`<account>/` prefix for non-default)
- [x] `config.py`: parse/validate `[[accounts]]` (name regex, duplicates, `default` reserved),
  `account` on sources (must name a known account), `_source_dict` omits a default account;
  `TEMPLATE` documents both. The name rule is `models.ACCOUNT_NAME` / `is_account_name` (so
  `paths` can refuse a bad name without importing `config`) and `config.check_account_name` is
  the validator task 8's `auth --account` reuses
- [x] `paths.Paths.session_file_for(account)` (`default` → `session_file`, else
  `<config dir>/sessions/<name>.session`), `sessions` dir in `directories`
- [x] tests: round trip, unknown account on a source, invalid/duplicate names, ids of default vs
  named-account sources, session paths under `GREPOGRAM_HOME` and macOS defaults
- [x] run checks — must pass before task 2

### Task 2: Per-account Telegram clients and sessions

**Files:**
- Modify: `grepogram/tg.py`, `tests/test_tg.py`

- [x] `load_session`, `make_client`, `make_login_client`, `prepare_session`,
  `ensure_session_mode` take an `account` (default `DEFAULT_ACCOUNT`) and use
  `paths.session_file_for`; `prepare_session` creates `sessions/` with `DIR_MODE`.
  `wrap_auth_errors` / `connected` take the `account` too, so an auth failure raised while
  talking to Telegram names the account whose session died
- [x] `AuthRequired` / `SessionMissing` carry the account and a hint
  `run: grepogram auth --account <name>` (unchanged text for `default`); `tg.auth_hint(account)`
  is the one rule
- [x] `make_clients(cfg, paths, accounts=None)` → `AccountClients(clients={account: client},
  unavailable={account: SessionMissing | SessionError})` for the accounts that have a session
  file (all of `cfg.account_names()` when `accounts` is `None`); accounts without one — or with
  an unreadable one — are reported in `unavailable`, not fatal
- [x] tests: two accounts get distinct in-memory copies of distinct files, modes 0600/0700,
  hints per account, missing session for one account only
- [x] run checks — must pass before task 3

### Task 3: Schema step 7 — chat identity, access and coverage

**Files:**
- Modify: `grepogram/db.py`, `grepogram/models.py`, `tests/test_db.py`, `tests/conftest.py`

- [x] add `_V7` (see Technical Details) and `MIGRATIONS[7]`; amend the `migrate` / `MIGRATIONS`
  docstrings with the fill-new-columns rule
- [x] `ChatRow.peer_id`, `ChatRow.scope`; `_chat_row` reads them; `chat_scope(type, account)`
- [x] `upsert_chat(conn, chat, account)` looks up by `(scope, peer_id)`, allocates
  `id = peer_id` when free else the next synthetic id `>= SYNTHETIC_BASE`, keeps COALESCE rules
  (`discussion_of`); asserts shared rows keep `id == peer_id` (a `ValueError` when another row
  holds a shared chat's peer id). `account` defaults to `None` = the account `chat.scope` names,
  so every existing caller keeps storing under `default`; `upsert_chat` writes neither
  `chat_access` nor `chat_sources` — task 5 owns what covers and reaches a chat.
  `ChatRow.peer_id = 0` / `scope = ""` resolve on construction to `id` / the default account's
  scope, so the ~350 existing constructions and calls stay valid
- [x] accessors: `get_chat_by_peer(conn, peer_id, scope)`, `chats_for_peer(conn, peer_id)`,
  `set_chat_access` / `chat_accounts(conn, chat_id)` / `access_hash(conn, chat_id, account)`,
  `set_chat_sources` (replaces the set) / `chat_source_ids(conn, chat_id)`, `upsert_account` /
  `list_accounts` (`models.AccountRow`)
- [x] tests: v6 → v7 migration on a populated index (units, FTS, vec, an `import:` chat) keeps
  everything and fills the new columns; empty db builds all steps; two accounts' DMs with the same
  user get distinct rows; a channel reached by two accounts is one row with two access entries;
  `delete_chat` cascades the new tables
- [x] run checks — must pass before task 4

### Task 4: Address Telegram and links by peer id

**Files:**
- Modify: `grepogram/sync.py`, `grepogram/media.py`, `grepogram/links.py`
- Modify: `tests/test_sync.py`, `tests/test_sync_map.py`, `tests/test_media.py`, `tests/test_links.py`
- `grepogram/search.py` needed no change: it hands the stored `ChatRow` to `links.message_url`,
  which is where the peer id is read

- [x] every call in Context's list uses `chat.peer_id`; `map_message` compares `from_id` with
  `chat.peer_id` and passes `chat.peer_id` to `reply_of`, while the row keeps `chat_id=chat.id`.
  `media._extract_chat` / `_extract_batch` take the `ChatRow` (not an id) so the re-fetch can
  name the peer; the re-cut still reads the row as stored now
- [x] `links.message_url` builds `tg://openmessage?user_id=` / `chat_id=` from `peer_id`
- [x] `_chat_row_from_entity`, `_check_migration`, `link_discussion_chat` go through the
  scope-aware `upsert_chat`. The account is explicit, never defaulted: `sync_chat` passes
  `source.account` to both, `_chat_row_from_entity(entity, source_id, account)` spells the scope
  out with `chat_scope`, and `upsert_chat` gets the same account. `sync.foreign_scope(chat,
  source)` refuses a scoped chat to another account's source — `sync_chat` raises `ValueError`,
  and `_sync_chats` skips the chat with a warning so one misfiled source cannot end the run
  (until tasks 5/6 resolve and fetch per account, a non-default source naming a DM hits this)
- [x] tests: a DM row with a synthetic id syncs, extracts media, prunes and links by its peer id;
  a legacy group migrating to a supergroup still links
- [x] run checks — must pass before task 5

### Task 5: Sources per account, shared coverage and removal

**Files:**
- Modify: `grepogram/sources.py`, `grepogram/filters.py`, `tests/test_sources.py`, `tests/test_filters.py`
- Modify: `grepogram/dialogs.py` (`DialogCatalog.access_hash`, entities `entity()` resolved join
  the memo), `grepogram/db.py` (`set_source_chats`, `source_chat_ids`, `chat_sources_map`,
  `set_primary_source`), `grepogram/models.py` (`SourceStatus.account`)
- Modify: `grepogram/sync.py` — `_sync_chats` resolves with `{DEFAULT_ACCOUNT: client}` until
  task 6 hands it every account's client, so another account's source is skipped (logged)
  instead of being read through the default session; `tests/test_sync.py` covers that and keeps
  the `foreign_scope` guard tested by feeding the run a misfiled chat
- Modify: `grepogram/cli.py`, `grepogram/mcp.py`, `tests/test_mcp.py` — `sources rm` reports the
  chats kept under another source, `sources_remove` returns `kept_chat_ids`

- [x] `parse_target` / `find_source` accept the `<account>/` prefix on source ids; `_names_chat`
  and `_same_target` compare against `peer_id`. `Target.account` is set only by an explicit
  prefix (`sources.split_source_id` / `source_account` read it off an id); an unprefixed target
  means the default account's source when it has a match and any account's otherwise, with two
  other accounts' matches an `AmbiguousTarget` (`sources._in_account`). In `filters`, an
  unprefixed `folder:` spec selects every account's folder of that name (a search scope), a
  prefixed one that account's only, and an id matches `peer_id` or the row id
- [x] `resolve_sources(cfg, clients, conn)` resolves each account's sources with that account's
  `DialogCatalog`; writes `chat_sources`, `chat_access` (with the entity's `access_hash`); a chat
  covered by several sources keeps the first as primary (`imported_tag` still guards, looked up
  by `(scope, peer_id)`). A source whose account has no client is skipped with a warning and
  keeps its coverage; a resolved source's coverage is replaced by what it lists now
  (`db.set_source_chats`), keeping the peers a folder names but could not resolve
- [x] `add_source(..., account)` and `with_source` reject a duplicate per account
- [x] `remove_source` deletes a chat only when no other configured source covers it
  (`chat_sources` minus the removed one ∩ configured ids); otherwise moves the primary
  `source_id` to a remaining source (through `discussion_source_id` for discussion groups) and
  drops the `chat_sources` row; `sources_status` lists chats under every covering source with
  the account. `Removed.kept_chat_ids` names the chats that stayed; channels are decided before
  discussion groups so a linked-only group follows its channel's new primary
- [x] tests: same channel via two accounts' sources → removing one keeps rows and re-points the
  primary; removing the last deletes; scoped DMs of two accounts are independent; imports untouched
- [x] run checks — must pass before task 6

### Task 6: Multi-account sync

**Files:**
- Modify: `grepogram/sync.py`, `tests/fakes.py`, `tests/test_sync.py`
- Modify: `grepogram/cli.py`, `grepogram/mcp.py` — pass `{DEFAULT_ACCOUNT: client}` to
  `sync_all` until tasks 8 and 9 hand it every signed-in account; `grepogram/media.py` — the
  new `warm_peer_cache` signature with the default account until task 7 routes per account
- Modify: `tests/test_mcp.py`, `tests/test_media.py`, `tests/test_index_dense.py`,
  `tests/test_index_lexical.py`, `tests/test_units_incremental.py` — callers of the new signatures

- [x] `tests/fakes.py`: a `FakeWorld` shared by several `FakeClient(account=…)` with
  per-account membership, DM histories and access hashes (`FakeWorld.client(account, members=…)`
  builds one; a private channel refuses a non-member; `FakeClient.session.process_entities`
  seeds stored hashes, and a hash that is not the account's own is refused like Telegram does)
- [x] `sync_all(clients: Mapping[str, client], …, only: Collection[str] | None = None)`:
  resolve once, assign each chat to its fetching account (primary source's account, then other
  `chat_access` accounts on `ChannelPrivateError` / `ChatForbiddenError` / missing peer), run one
  queue per account concurrently; `_record_failure` stops only that account's queue on a flood
  wait; `me` per account; `index_pending` / `index_stranded` / re-cut / embed once. The queues
  run in an `asyncio.TaskGroup`; a fallback is only for shared chats, warms the other client
  first and a flood wait there stops *that* account's queue; `only` narrows the fetch to the
  chats the named sources cover (every source is still resolved, so the primary never moves);
  each account's `me` is recorded in `accounts`; a rejected session raises `AuthRequired`
  naming its account; an unaddressable peer (`ValueError`) costs a chat its turn instead of
  ending the run
- [x] `warm_peer_cache(client, chats, conn, account)` seeds Telethon's session with stored
  `chat_access.access_hash` before falling back to dialogs / username / discussion routes (a
  legacy group counts as addressable, a discussion group's channel is seeded too; nothing left
  unseeded means no request at all)
- [x] `SyncReport` gains per-account `warnings` context (account name in each warning): every
  warning reads `account <name>: …` whenever the run holds an account other than `default`, so a
  single-account install's report is unchanged
- [x] tests: two accounts sync concurrently into one index; a shared channel is fetched once;
  fallback to the second account when the first is refused; a flood wait on one account leaves
  the other finishing; `only=` limits the run to named sources
- [x] run checks — must pass before task 7

### Task 7: Per-account extraction and deletion sweeps

**Files:**
- Modify: `grepogram/media.py`, `grepogram/sync.py` (`prune_deleted`), `tests/test_media.py`, `tests/test_sync.py`
- Modify: `grepogram/models.py` (`MediaReport.chats_unreachable`, `PruneReport.chats_unreachable`),
  `grepogram/cli.py` (passes `{DEFAULT_ACCOUNT: client}` until task 8, prints the unreachable
  chats), `tests/test_cli.py`

- [x] `media.run` and `prune_deleted` take the client mapping and route each chat to an account
  in `chat_access` (primary source's account first), warming that client first
  — both through one driver, `sync.StoredPass` (`sync.reaching_accounts` orders the accounts:
  a scoped chat only its own scope account; a shared one its primary source's account, then
  `chat_access`, then for a discussion group the channel's accesses and primary; a shared row
  nothing ties to an account is tried through every connected account, default first). A flood
  wait stops that account for the pass and its chats move on to the next account that reaches
  them; a shared chat refused (or unaddressable) is retried through the next account, warmed
  for it first
- [x] a chat no connected account reaches is reported `unreachable`, never an error
  (`MediaReport.chats_unreachable` with its media in `unreachable`,
  `PruneReport.chats_unreachable`)
- [x] tests: media of a second account's DM is extracted through that account; a chat reachable
  by none is counted; `forget_entities` after sync still resolves via stored access hashes
- [x] run checks — must pass before task 8

### Task 8: CLI accounts, per-account commands and `leave`

**Files:**
- Modify: `grepogram/cli.py`, `tests/test_cli.py`
- Modify: `grepogram/tg.py` (`connected_all` → `Live(clients, refused)`, `login` → `SignedIn(name,
  user_id)`), `grepogram/db.py` (`account_chat_counts`, `forget_account`), `grepogram/sources.py`
  (`remove_source_id`, `folder_membership(cfg, catalogs)` per account, `import_chats(…, account)`)
- Modify: `tests/conftest.py` (autouse `no_terminal`: no test reads the real terminal),
  `tests/test_tg.py`, `tests/test_db.py`, `tests/test_sources.py`, `tests/test_sync.py`,
  `tests/test_dialogs.py`, `tests/test_index_dense.py` — `make_client` / `make_login_client` fakes
  now take the account

- [x] `grepogram auth --account NAME` (new names are appended to `[[accounts]]` through
  `config.update` after a successful login; the `accounts` table records the user). The name is
  checked with `config.check_account_name` before any sign-in; `--label` sets the entry's label
  (refused for `default`, which has no entry); an index that cannot be opened costs a warning,
  not the sign-in
- [x] `accounts ls` (name, label, session present/authorized-at-last-use, user, sources, chats)
  and `accounts rm NAME` (TTY confirmation; under `SyncLock` → `ConfigLock`: remove the account's
  sources through the Task 5 rules, drop its `chat_access` rows, delete its session file;
  `default` cannot be removed while it is the only account). The confirmation is read from the
  controlling terminal (`cli._terminal` over `/dev/tty`, `cli.NoTerminal` without one), never
  stdin, and asked before either lock is taken; the sources are then re-read under the locks and
  removed one by one with `sources.remove_source_id`; `db.forget_account` drops the access rows
  and the `accounts` row
- [x] `--account` on `dialogs`, `sources add`, `import`; `sync`, `extract`, `prune-deleted` use
  every signed-in account; `sources ls` shows the account. An `<account>/` prefix on a `sources
  add` / `leave` target names the account too; an unknown account is refused by name with the
  `auth --account` hint. The multi-account commands build clients with `tg.make_clients` and
  connect them with `tg.connected_all`: an account without a session (warned when it owns a
  source) or signed out is left out with a warning, and only no account at all is an error.
  ➕ `sources prune` reads each folder source through its own account's catalog
  (`folder_membership(cfg, catalogs)`); a folder whose account is not connected is unchecked, so
  the prune stops rather than reading another account's folder of the same name.
  ➕ `import --account` stores an export's private chats and legacy groups under that account's
  scope (a synthetic row id when the default account holds the peer, messages remapped to it);
  `_refuse_live` and `import_source_ids` compare `(scope, peer_id)`, not row ids
- [x] `grepogram leave TARGET --account NAME`: TTY-confirmed `channels.leaveChannel` /
  `messages.deleteChatUser(self)`; never touches sources or the index (the terminal is opened
  before any network call; private chats, bots and folders are refused)
- [x] tests: second account auth flow with fake prompts, `accounts rm` keeps shared chats,
  `leave` refuses without TTY and never edits config
- [x] run checks — must pass before task 9

### Task 9: MCP multi-account

**Files:**
- Modify: `grepogram/mcp.py`, `tests/test_mcp.py`
- Modify: `grepogram/models.py` (`ChatStatus.accounts`), `grepogram/sources.py`
  (`sources_status` fills it), `grepogram/db.py` (`chat_accounts_map`), `README.md` (MCP tool
  table)

- [x] `AppState.telegram(account)` and `AppState.telegrams()` (all signed-in accounts, each its
  own fresh client); `ClientFactory` takes the account. `telegrams()` follows the CLI's rules:
  a missing / unreadable session is reported only for an account that owns a source, a
  refused one always (`tg.connected_all`), and only no account at all raises (a source
  owner's reason first); it yields `Accounts(clients, skipped)`. An unknown account is
  `UnknownAccount` (a `ConfigError` whose hint is the `auth --account` sign-in)
- [x] `sync` and `_auto_sync` pass every account; `dialogs(query, account)`,
  `sources_add(target, since, comments, account)`, `sources_remove` accept prefixed ids;
  `sources` result carries accounts; new read-only `accounts` tool.
  ➕ `sync` returns `accounts_skipped` (`account`, `error`, `hint`) plus one warning per skipped
  account; a `dialogs` match's `target` carries the `<account>/` prefix for a non-default
  account; every chat in `sources` lists the accounts that reach it (`ChatStatus.accounts`);
  an auth failure of a non-default account names it in `error` and its sign-in in `hint`
- [x] tests: two fake accounts through the tools, auth error of one account reported with its
  hint while the other syncs, stdout stays empty
- [x] run checks — must pass before task 10

### Task 10: Account scopes and provenance in queries

**Files:**
- Modify: `grepogram/filters.py`, `grepogram/search.py`, `grepogram/models.py`, `grepogram/mcp.py`, `grepogram/cli.py`
- Modify: `tests/test_filters.py`, `tests/test_readers.py`, `tests/test_search_lexical.py`, `tests/test_mcp.py`

- [ ] chat spec `account:<name>` → chats whose `chat_access` includes it; `search(…, accounts=)`
  in the library, `--account` in the CLI, `accounts` in the MCP tool
- [ ] `resolve_chat` accepts `<account>/<peer>` and answers an ambiguous bare peer id with
  candidates
- [ ] `Hit` and `MessageView` gain `peer_id` and `accounts`; JSON outputs carry them
- [ ] tests: account-scoped search excludes the other account's DMs but keeps shared channels
  both reach; thread/context on a synthetic-id chat; ambiguity error
- [ ] run checks — must pass before task 11

### Task 11: Schema step 8 and link/forward capture in `map_message`

**Files:**
- Create: `grepogram/leads.py`, `tests/test_leads.py`
- Modify: `grepogram/db.py`, `grepogram/models.py`, `grepogram/sync.py`, `tests/fixtures/tl.py`, `tests/test_sync_map.py`, `tests/test_db.py`

- [ ] `leads.py` (pure): `normalize(url_or_mention) -> LeadTarget | None` for `t.me` /
  `telegram.me` / `tg://resolve` / `tg://join` / `tg://addlist` forms, usernames, `c/<id>/<post>`,
  invites, `addlist`, and `text_leads(text)` regex fallback
- [ ] `_V8` + `MIGRATIONS[8]`; `MessageRow.fwd_peer_id/fwd_msg_id/fwd_date`,
  `MessageRow.links: tuple[tuple[str, str], ...]`; `upsert_messages` writes/replaces links
- [ ] `map_message` reads `msg.entities` (`MessageEntityUrl`, `TextUrl`, `Mention`,
  `MentionName`), `msg.reply_markup` URL buttons, `msg.media.webpage.url`, and `msg.fwd_from`
  (`from_id`, `channel_post`, `date`, `saved_from_peer` / `saved_from_msg_id`)
- [ ] extend `tests/fixtures/tl.py` with a hyperlink, a mention, a URL button and a channel-post
  forward; tests for mapping, normalization table, upsert replacement, v7 → v8 migration
- [ ] run checks — must pass before task 12

### Task 12: `[research]` config and `research.db`

**Files:**
- Create: `grepogram/research_db.py`, `tests/test_research_db.py`
- Modify: `grepogram/models.py`, `grepogram/config.py`, `grepogram/paths.py`, `tests/test_config.py`

- [ ] `ResearchCfg` with the defaults in Technical Details; `[research]` section and `TEMPLATE`
- [ ] `paths.research_db_file` next to `index.db`
- [ ] `research_db`: connect (0600), own versioned schema, `SchemaError` on an unknown version,
  accessors for sessions, candidates, evidence, grants, exclusions, searches; every writer in a
  transaction
- [ ] tests: create/reopen, mode, CRUD, candidate identity uniqueness per session, exclusions
  global
- [ ] run checks — must pass before task 13

### Task 13: Offline discovery

**Files:**
- Create: `grepogram/research.py`, `tests/test_research.py`

- [ ] `start_session(rdb, conn, cfg, question, seeds, account, limits)` (seeds resolved through
  `filters.resolve_chats`, refusing while `research.enabled` is false)
- [ ] `collect_leads(conn, chat_ids, since_msg_ids)` from `message_links`, `fwd_peer_id`, and
  `leads.text_leads` for rows stored before step 8; evidence with origin keys
- [ ] candidate building: exclusions skipped, depth tracked, `cached` from `index.db`
  (`chats_for_peer`), dedupe by identity, corroboration = distinct origin keys, ranking by
  corroboration and question-term overlap of evidence snippets; `max_candidates` cap
- [ ] tests: a link, a hidden hyperlink, a button, a forward chain and ten forwards of one post
  (one corroboration), an excluded target, an already-indexed chat marked cached, depth cap
- [ ] run checks — must pass before task 14

### Task 14: Probing and global discovery

**Files:**
- Modify: `grepogram/research.py`, `tests/fakes.py`, `tests/test_research.py`

- [ ] reverify `messages.checkChatInvite`, `messageFwdHeader`, `chatlists`, `channels.searchPosts`
  / `checkSearchPostsFlood` and `contacts.search` semantics against core.telegram.org and record
  what the code relies on in the module docstring
- [ ] `probe(client, rdb, candidate)`: username → entity (title, type, participants, member,
  access hash stored per account); invite → `ChatInvite` (title, `request_needed`, member count) /
  `ChatInviteAlready` (member) / `ChatInvitePeek`; `addlist` → peers become child candidates
  (`via=shared_folder`); `probe_limit` per call; `FloodWaitError` stops probing with a warning
- [ ] `global_search(client, rdb, session, query)`: only with the config switch and a live
  `global_search` grant; `checkSearchPostsFlood` first, no `allow_paid_stars` unless
  `paid_stars_max > 0` and a `paid_search` grant; results → candidates + evidence only
- [ ] tests: every invite shape, private/hidden forward origins recorded honestly as
  unresolvable, quota exhausted, paid path refused by default, no `messages` row written
- [ ] run checks — must pass before task 15

### Task 15: Grants and approval enforcement

**Files:**
- Modify: `grepogram/research.py`, `tests/test_research.py`

- [ ] `approval_summary(rdb, conn, session, items)` — the exact text a human sees (target,
  account, membership, each action in words, "adds an ongoing source", search disclosure)
- [ ] `grant(rdb, session, items, via)` where `via` is a `Literal["elicitation", "cli"]`
  produced only by the two entry points; validates actions against candidate state (no `join`
  for a member, `request` only when `request_needed`, `fetch` implies `add_source` must be
  explicit); `skip`, `exclude`, `unexclude`
- [ ] `authorized(rdb, candidate, action)` — the single check the run uses; stop voids
  unconsumed grants
- [ ] tests: descendants of an approved directory stay unauthorized; a grant is reused across
  runs; invalid action combinations refused; stopped session has no live grants
- [ ] run checks — must pass before task 16

### Task 16: Research run

**Files:**
- Modify: `grepogram/research.py`, `grepogram/sync.py` (message cap on `SyncBudget`), `tests/test_research.py`

- [ ] `run(rdb, conn, cfg, paths, clients, session, budget)`: re-check pending admissions; join /
  request / chatlist-join exactly the granted targets (`UserAlreadyParticipantError` = member,
  `InviteRequestSentError` = `pending_admission`, `ChannelsTooMuchError` / `ChannelPrivateError`
  = recorded honestly); add sources via `config.update` with account, `since`, `comments`;
  `sync_all(…, only=added)`; discovery over the new messages at depth + 1
- [ ] `SyncBudget` gains an optional message allowance checked in `_fetch_new` next to
  `budget.expired`
- [ ] progress and status recorded in `research.db`; a stopped session refuses to run
- [ ] tests: full loop with fakes (discover → approve → run → new candidates proposed, not
  fetched), pending admission later accepted, budgets stop and resume, sources survive stop
- [ ] run checks — must pass before task 17

### Task 17: CLI `research`

**Files:**
- Modify: `grepogram/cli.py`, `tests/test_cli.py`

- [ ] `research` sub-app: `start`, `discover`, `candidates` (evidence, member / cached /
  authorized columns), `approve SESSION ID[:actions]…` (prints the summary, reads the answer
  from `/dev/tty`, refuses with no TTY, no `--yes`), `skip`, `exclude`, `unexclude`, `run`,
  `status`, `stop`; `--json` where the readers have it
- [ ] tests: approve refuses without a TTY and grants with a simulated TTY answer, candidates
  rendering, stop keeps sources
- [ ] run checks — must pass before task 18

### Task 18: MCP research tools and consent through elicitation

**Files:**
- Modify: `grepogram/mcp.py`, `tests/test_mcp.py`

- [ ] tools `research_start`, `research_discover`, `research_candidates`, `research_approve`,
  `research_skip`, `research_exclude`, `research_run`, `research_status`, `research_stop`;
  all refuse with a hint while research is disabled
- [ ] `research_approve` elicits with the Task 15 summary when the client declares elicitation,
  grants `via='elicitation'` only on accept + true; otherwise returns the CLI command as `hint`;
  no parameter accepts an approval flag
- [ ] `INSTRUCTIONS`: research playbook (seed → discover → review evidence → ask the user to
  approve → run → analyse with `search` / `thread` / `context`), forward origins are not
  independent corroboration, account provenance
- [ ] tests: accept / decline / cancel, no capability → hint, stdout empty, disabled refusal
- [ ] run checks — must pass before task 19

### Task 19: Verify acceptance criteria

- [ ] every Overview requirement (both features, every permission requirement in the handoff)
  traced to code and a test
- [ ] upgrade path: a v0.2.0 home (schema 6, single session, imports) works unchanged
- [ ] run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`
- [ ] `HF_HUB_OFFLINE=1 uv run pytest -m slow` if the models are cached

### Task 20: [Final] Update documentation

- [ ] README: accounts (auth, sources per account, filters, provenance), research (enabling,
  workflow, consent, global search disclosure, costs), new commands and tools, files table
  (`sessions/`, `research.db`)
- [ ] CLAUDE.md: identity invariants (`id == peer_id` for shared rows, peer for Telegram),
  migration fill rule, `map_message` raw attributes, research consent rule, tool count, layout
- [ ] CONTRIBUTING.md if gates changed; move this plan to `docs/plans/completed/`

## Post-Completion

**Manual verification**
- sign in a second real account; sync a DM both accounts have with one person; confirm two
  independent histories and one shared channel row
- run a research session in Claude Code and confirm the elicitation dialog appears, and that a
  host without elicitation gets the CLI hint
- a real invite with admission requests; a real `addlist` link; one post search to observe the
  free quota answer

**Release**
- version bump and tag per CLAUDE.md "Releasing" once the PR merges
