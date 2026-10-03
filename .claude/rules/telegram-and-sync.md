---
description: Telegram clients and sessions, the sync, sources and coverage, discussion groups, imports and comment mapping.
paths:
  - "grepogram/sync.py"
  - "grepogram/sources.py"
  - "grepogram/accounts.py"
  - "grepogram/tg.py"
  - "grepogram/dialogs.py"
  - "grepogram/leads.py"
  - "grepogram/filters.py"
  - "grepogram/tdesktop.py"
  - "grepogram/db.py"
  - "grepogram/cli.py"
  - "grepogram/mcp.py"
  - "grepogram/media.py"
  - "grepogram/research_db.py"
  - "grepogram/research/probing.py"
  - "grepogram/research/approval.py"
  - "grepogram/research/collect.py"
  - "tests/fakes.py"
  - "tests/fixtures/two_accounts.py"
  - "tests/test_sync*.py"
  - "tests/test_sources.py"
  - "tests/test_tg.py"
  - "tests/test_dialogs.py"
  - "tests/test_leads.py"
  - "tests/test_filters.py"
  - "tests/test_tdesktop.py"
  - "tests/test_cli.py"
---

# Telegram, sync and sources

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
  `research.approval.target_identities` / `parse_approval` and `research_db._spelled` read ids through the
  same helpers, `research_db.get_candidate` / `get_session` answer `None` for an id past 64 bits,
  and `research.collect.message_leads` skips one link that still fails to read. `MessageRow.links` is `None` for a row nobody read links for — one read back
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
  `accounts.signed_in_user` (`get_me` against `db.conflicting_account`, raising `tg.OtherUser`, an
  `AuthRequired` whose hint is `accounts rm`), put by `accounts.check_account` or — for a pass that
  goes on without the account, with the one wording of why — `accounts.ask_account`: a sync
  (`always=True`, then `sync._record_account` records a first sign-in), `StoredPass.start`
  (`prune-deleted`, `extract`, `recapture-links`) and the folder read of `sources prune`
  (both through `accounts.checked_accounts`, which leaves the account out with a warning), and `research.running.run` / `discover` (its pins, global searches and probes) and `grepogram leave`
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
  the two that re-fetch by id, both driven by `accounts.StoredPass` — must call
  `accounts.warm_peer_cache(client, chats, conn, account)` first, or every
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
- `chat_sources` holds every source that covers a chat and `chat_access` every account that
  reaches it, with the access hash that account addresses it by; `chats.source_id` stays the
  *primary* owner the import and discussion-ownership rules read. `db.upsert_chat` writes
  neither table. `sources.resolve_sources(cfg, clients, conn)` does, each account's sources
  through that account's `DialogCatalog`, and replaces a source's coverage
  (`db.set_source_chats`) only when that source resolved this run — a failed source, or one whose
  account has no client, keeps what it had. A legacy group's supergroup inherits the group's
  coverage and reach when the migration is found (`sync._inherit_coverage`), and a resolve that
  lists the group keeps the supergroup covered (`sources._with_migrations`); a `migrated_to`
  naming a row deleted since makes the next sync check the migration again rather than follow
  a dangling id. `sources.remove_source` deletes a chat only when no
  source left in the config covers it — recorded in `chat_sources`, or named outright by a
  `chat:` entry through its id or stored username (`sources._configured_for`), which covers it
  before its first sync; a source that has recorded nothing yet and could still list the chat
  keeps it undecided under it (`sources._undecided_cover`, `Removed.undecided_chat_ids`)
  instead of deleting on a guess — a folder, or a fuzzy `chat =` value the chat's stored title
  matches under `dialogs.match`, and either only of an account with a session file
  (`has_session`, `sources.with_session`; the CLI and MCP pass it, the default is "none"). A
  fuzzy value that does not match, an invite link, or a source of an account with no session
  decides nothing: a chat parked under a source that can never tell is a chat the user removed
  that stays searchable for good. The undecided chat's way out is `sources prune`: a folder
  that syncs without it offers it, and so does a `chat:` source that resolved to another chat
  and neither records nor names it (`sources._stray_under`) — short of a channel entry with
  `comments`, whose unlinked group looks the same and keeps that source.
  `sources rm <such a chat>` deletes it alone (`Removed.stray`) instead of its unrelated
  source. A chat some remaining source does cover moves its primary to the first remaining
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
  the fetch. **One order, one retry rule**: `accounts.reaching_accounts` (over `recorded_reach`)
  orders the accounts for a sync, `extract` and `prune-deleted` alike, and
  `accounts.through_accounts` is the one walk down it — flood wait stops that account and moves on,
  a shared chat's refusal or unaddressable peer moves on, anything else ends the chat's turn.
  A chat goes to the first account of its route in the run and not stopped, so a chat whose own
  account is absent is fetched through another that reaches it; one nobody in the run reaches
  is counted in one warning per account (`_SyncPass.unfetched`), not listed as remaining. A
  fallback fetch's own warnings carry the fetching account's label. Every fetch — a lane's own
  and a fallback another lane makes — holds that account's `_Lane.turn` lock, warm-up included,
  so one client never runs two fetches at once, and a fallback that finds its account
  flood-stopped once its turn comes passes over it (`accounts.AccountStopped`) without sending
  anything. A flood wait stops only that account's queue; a scoped chat never falls back. `prune-deleted` removes a message only
  when **every** account `recorded_reach` names answers it empty (`sync._confirmed_gone`): an
  account that joined late may see history as empty that another still reads, so any of them
  that cannot answer is "cannot tell" and the chat keeps everything this pass: absent (no
  session, or left out as another Telegram user), flood-stopped, unable to address the peer
  (`ValueError`), or **refused the chat outright** (`UNAVAILABLE_ERRORS`). Never pass a refused
  account over: it may be the one that fetched the history it has since been banned from or
  left, and the account still answering may be a late joiner that sees that history as empty —
  the deletion would be unrecoverable. The accepted price is that a shared chat one recorded
  account can no longer reach is never pruned again until `accounts rm` forgets that account's
  reach (`db.forget_account`; removing a source alone leaves `chat_access` as it is). Because no
  rerun changes that, the sweep still asks the other witnesses and, when one answers, reports the
  chat in `PruneReport.chats_held` with a warning naming the refused account and that remedy —
  never in `chats_remaining`, whose CLI line says to run again; a chat no witness answers at all
  stays in `chats_remaining` with Telegram's refusals. `only=` narrows the fetch to the chats
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
- A forward names its origin by id alone, so `sync.forward_peers` records what the fetching
  account was handed with the message — `msg.forward.chat`'s username and non-`min` access hash
  — in `peer_cache` (index.db, per account), and `research.probing._probe_peer` looks a `peer:`
  candidate up with the access hash of the index's chat row, else `peer_cache`'s, else resolves
  the cached username and takes the answer only when it is that very peer
  (`_probe_named_peer`). The cached username is a probe hint and never a candidate's identity:
  a stale one would fold two chats into one candidate.
- `grepogram recapture-links` (`sync.recapture_links`) is the backfill for rows whose links were
  never read: by id through `StoredPass`, a hundred per request, under the `SyncLock`, resumable
  on a `meta['links_recapture:<chat>']` cursor. It writes `message_links`, `fwd_*`, `links_read`
  and a lead-clock tick and nothing else — no text, no `indexed`, no unit, no sync cursor — and
  leaves a message Telegram no longer has to `prune-deleted`; imports are never re-read.
