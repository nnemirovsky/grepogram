---
description: research.db, discovery, grants, approvals and runs.
paths:
  - "grepogram/research/**"
  - "grepogram/research_db.py"
  - "tests/test_research*.py"
---

# Research

- Research state lives in `research.db` (`paths.research_db_file`, next to `index.db`) and never
  in the index: the index is derived and may be deleted and rebuilt, while approvals, exclusions
  and session history are the user's decisions and nothing rebuilds them. It has its own version
  (`research_db.SCHEMA_VERSION`) and its own append-only `research_db.MIGRATIONS`: step 2
  moved seeds, scan cursors and evidence from index row ids to `(scope, peer_id)` and walks a
  development build's version 1 up rather than refusing it — a file of decisions is migrated,
  never re-derived. Step 3 adds `grants.search_kinds` / `grants.stars_max`, the terms a
  session-wide grant was given on; a grant from before it names none and authorizes no search.
  Step 4 adds `grants.join_route` — `invite`, `username`, `id` or `folder:<candidate id>` —
  the way in a `join` / `request` grant's summary named (`research.approval._way_in`, required by
  `add_grant` for those two actions and only them). In code it is a `models.WayIn` (a
  `JoinRoute` plus the folder's candidate id), checked by `research_db.check_way_in`; that
  text form is `research_db._stored_way_in` / `_read_way_in`'s alone, so every grant any build
  wrote reads back the same. **A run takes that route and no other**
  (`research.joining._granted_way_in` → `_join_all`): one it can
  no longer take, or a grant from before step 4 that names none, fails the candidate for a new
  approval — never a swap to another way in. Step 5 rebuilds `grants` so
  `grants.via` also admits `confirm`. `add_candidate` never gives a parent to a
  candidate with a grant (ever) or any status but `proposed`, so a shared folder found after a
  decision changes neither the summary nor the join. Its `SchemaError` subclasses `db.SchemaError`, so the existing handlers
  catch it, and never advises deleting the file. Whether a candidate is *cached* is
  asked of the index every time and never stored there, and global-search results are
  candidates and evidence in `research.db`, never `messages` rows, so no sync cursor moves. Every
  research entry point refuses before opening the file while `[research] enabled` is false
  (`research.sessions.require_enabled`), and ordinary `search` never widens what it reads.
- **`research.db` never names a chat by an index row id.** Seeds (`session_seeds`), scan cursors
  (`chat_scans`) and evidence carry `models.ChatKey` — `(scope, peer_id)`, the identity
  `db.upsert_chat` finds a row by — and are resolved to the row the index holds *now*
  (`research.collect.chat_of`) at use: a rebuilt index numbers rows afresh, and a private chat's
  synthetic id goes to whichever account's row is stored second. `db._next_synthetic_id` keeps a
  high-water mark (`meta['synthetic_next']`) so a deleted scoped row's id is never handed to
  another conversation. The JSON documents show evidence as `scope` / `peer_id` plus `chat_id`,
  the row resolved at read time (`research.documents.evidence_document`, `None` when the index does not
  hold the chat), and seeds as `{scope, peer_id}`.
- Discovery reads a chat from a cursor on the **lead clock**, never a `msg_id`: every
  `upsert_messages` call stamps its rows with one tick (`messages.lead_seq`, inserts always,
  re-stores only when they read the links again) and `db.set_captured_links` does the same, so a
  comment stored below the newest `msg_id`, an edit that gained a link and a recaptured row are
  all read. A cursor counts only on the index `chat_scans.index_id` names (`db.index_id`);
  another index's reads the chat from the start, which duplicates nothing (`add_evidence` keeps
  one row per path). A channel's discussion group is read beside its channel, seed or fetched
  (`research.offline.scan_targets`). Whether a row falls back to `leads.text_leads` is `links_read`, a
  per-row fact, never an id threshold — an import stored after capture began has none either
  (`tdesktop.export_links` reads the runs an export does spell).
- Research also reads the **pinned posts** of the chats a session reads — seeds (the user's own
  sources) and chats a run fetched under a grant, never anything else — once each
  (`chat_scans.pins_read_at`), whatever their age, through `iter_messages(filter=
  InputMessagesFilterPinned)` (`research.pins.read_pins`: from `discover` for what it has not read,
  from a run for the chats it just fetched). Their leads are evidence (`via = pinned`) only:
  **never `messages` rows, and no cursor moves** — a sparse read must never pass for the history
  before it. Another account's private chat is never asked about. A chat whose links name
  `research.offline.DIRECTORY_MIN_CHATS` distinct chats is a directory (`chat_scans.directory`, sticky)
  and every lead found in it gets a `directory` path with the lead's own origin key, so it adds
  no corroboration and approving the directory still approves nothing it lists.
- **A confirmation names the exact summary that was shown.** A grant comes from exactly three
  places, each recorded in `grants.via`: `cli` — `grepogram research approve` at a terminal,
  which writes `research.approval.approval_summary` there and reads the typed-back code;
  `elicitation` — the MCP `research_approve` on a client with a dialog, granting only on an
  accepted answer whose strict-boolean `approve` is `true` (decline, cancel, an unticked box and
  any failure grant nothing, and such a client cannot use a token); and `confirm` — the CLI's
  `--confirm <token>` or the MCP `confirm` argument on a client without a dialog, valid only
  for the token of the summary rebuilt at confirm time (`grepogram.consent`). The agent is
  expected to show the user the summary and confirm on their say-so; the gate that makes that a
  human's decision is the agent harness's permission prompt, not the token. `research_db.add_grant`
  is the only grant writer and takes `via` keyword-only with no default, `grants.via` is
  `CHECK (via IN ('elicitation', 'cli', 'confirm'))` (research.db v5), and
  `research.approval.grant` rebuilds the summary and grants nothing unless it equals the text
  shown. Never add a confirmation that is not bound to the exact summary — a bare `--yes`, an
  `approve=true`. Skip and exclude only narrow and need no consent.
- A grant names one candidate (or the session, for `global_search` / `paid_search`), the
  session's account and concrete actions (`join`, `request`, `fetch`, `add_source`), and
  `research.grants.authorized(target, action)` is the one check before every outward step of a run:
  only a grant naming that very target counts, so approving a chat approves nothing discovered
  inside it, and a shared folder is approved chat by chat, never whole. **It is asked right
  before the request goes out, after every await in front of it** — a username resolved for a
  join, `chatlists.checkChatlistInvite` before a folder join, `contacts.search` and
  `checkSearchPostsFlood` before the next search (`research.joining._still_approved`,
  `research.searching._still_granted`) — so research stopped or an approval withdrawn while
  Telegram answered sends nothing more. Probes read metadata,
  never history; a `peer:` candidate is looked up only with an access hash stored for the
  session's account or by the username a sync saw it under (see `peer_cache` in `telegram-and-sync.md`) and is
  `unresolvable` otherwise, never guessed. `max_candidates` bounds one call and
  `max_session_candidates` the whole session (`research.offline.room`); what the session ceiling cuts
  moves the cursor on, since no later call could propose it. An admission request no admin
  answered within `admission_timeout_days` (`candidates.requested_at`) is `failed` with a note. Global search needs the
  `[research]` switch *and* a `global_search` grant; paying needs `paid_stars_max > 0`, a price
  within it and a `paid_search` grant, consumed before the request is sent. **A session grant
  holds the terms its summary named** (`research_db.add_grant(search_kinds=, stars_max=)`,
  written by `research.approval.grant` from the config the human read): a search switched on since is not
  covered (`research.grants.granted_kinds`), a price above the approved ceiling is refused
  (`_paid_ceiling`, `_consume_paid_grant(price)`), and `_session_entry` asks again rather than
  calling such an approval "already given" — raising a config value never widens a live grant. A run adds its
  sources in one `config.update` under `SyncLock` → `ConfigLock` with no Telegram request under
  either, syncs through `sync_all(…, recut=False, only=…)`, and discovery over what it stored
  only proposes. Sources a run added are ordinary sources and survive `stop`, which voids the
  unconsumed grants and nothing else. A grant's lifecycle: `research.running._consume_done` consumes it
  only once **every** action it names is done, so a run stopped by a budget, a flood wait or a
  busy sync leaves it live for the next run and nobody is asked twice; a refusal
  (`_refuse_candidate`) sets `failed` or `unavailable` and voids the candidate's grants — a
  `failed` candidate takes a new approval once the cause is gone, an `unavailable` one none
  (`_REFUSED`); an exclusion, a skip and a source removed from the config before its fetch void
  them too.
- Corroboration counts **origins**, not messages: `research.collect.origin_key` gives a forwarded post
  `post:<origin peer>/<origin msg>` and the same post where the index holds it in its channel
  the same key (a channel or supergroup's `msg_id` is global), a forward known only by its
  author `fwd:<author>@<date>`, and anything else `msg:<scope>:<peer>/<msg>`. Global-search
  post results and `directory` evidence reuse the key of the lead they stand for, so neither a
  forward chain nor a directory listing a chat ever counts twice.
- The approval text is the consent, so it says what the run will really do and nothing anyone
  else wrote can bend it. Every value someone else chose — the question, titles, usernames,
  folder titles — goes through `research.sessions.shown` (control, format and separator characters as
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
  `research.approval.grant` validates and writes in one `research.db` transaction; each session action is
  its own grant row, and a paid search pays only after `consume_grant` (one conditional
  `UPDATE`) succeeded, so one approval never pays twice. A global search sends the session's
  question and nothing else: `research.discovery.discover` is its only caller and picks the searches the
  switches turn on *and* the grant covers, and `research.searching.search_telegram` trusts it to.
- A candidate is a chat, not a spelling. `research_db.candidate_for` finds the session's row by
  identity, peer id, username or invite hash, `add_candidate` returns that row rather than a
  second one, and a probe that ties two rows to one chat folds the undecided one into the other
  (`research.probing._reconcile` → `research_db.merge_candidate`, never a row with a grant or a decided
  status). **A candidate's peer id is fixed once a probe learned it**: `update_candidate` raises
  on another one, `candidate_for` / `same_chat_candidates` match a username or invite only on a
  row with no peer or the same peer (`_same_chat(strict=True)`), and `add_candidate` records a
  chat whose name another row's peer holds as `peer:<id>`. A name that now leads elsewhere — a
  probe, an admission recheck, a shared folder's child or a search result carrying it — sets the
  old candidate aside (`research.probing._name_moved`: grants voided, `proposed` → `unavailable`,
  `approved` / `joined` / `pending_admission` → `failed`), so a later find can never repoint
  an approval at a chat no human saw. An exclusion names one spelling and covers every other (`research_db.excluded_by`,
  `_covered`): it moves undecided candidates to `excluded` and voids the live grants of every
  candidate of the chat, joined or waiting ones included, and `research.grants.authorized` refuses an
  excluded chat whatever its grants say; `skip` takes joined and waiting candidates too.
