# :mag: grepogram — Semantic Search for Your Telegram Chats

[![ci](https://github.com/nnemirovsky/grepogram/actions/workflows/ci.yml/badge.svg)](https://github.com/nnemirovsky/grepogram/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/nnemirovsky/grepogram)](https://github.com/nnemirovsky/grepogram/releases/latest)
[![pypi](https://img.shields.io/pypi/v/grepogram)](https://pypi.org/project/grepogram/)
[![python](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Ask your Telegram history a real question and get the conversation that answers it — the whole
thread, when it was said, and a link that opens the message in Telegram.

## Why grepogram

Community chats are where the answers live. Expat groups, city chats, hobby groups hold things no
web page does — how to open a bank account here without a DNI, which SIM works in the mountains,
whether the visa run still works after the June rules. Finding any of it again is the hard part.

**The problem:** Telegram matches exact words. `счёт` misses `счета`, `bank` misses `banks`, and a
paraphrase misses everything. The answers that matter are worse than a single keyword away: they
sit in reply chains, spread across several messages, switch between two languages, and go stale
after a rule change. Embedding single messages does not rescue this either — one chat message is
too short to carry meaning, and the answer usually turns on an exact token anyway: a bank name,
`ВНЖ`, `CUIT`.

**The solution:** grepogram indexes *conversations*, not messages. It syncs the chats you opt in
to through the Telegram user API, groups them into conversation-sized units — time windows, reply
threads, channel posts — and indexes every unit twice: lexically with FTS5 and Russian/English
stemming, so exact tokens still win, and densely with `bge-m3` vectors, so a paraphrase lands.
Both rankings are fused with Reciprocal Rank Fusion, re-scored by a local cross-encoder, and
returned with dates and deep links. An MCP server hands those tools to your agent along with a
playbook for using them: run several query variants, read the thread before concluding, cite a
link per claim.

Your agent supplies the reasoning; grepogram supplies the retrieval. Both models run on your
machine, and every message, vector and query stays in a SQLite file you own.

## How It Works

```mermaid
flowchart TD
    TG["Telegram — your accounts, via Telethon"]
    MSG["messages"]
    UNITS["units — windows · reply threads · channel posts"]
    FTS["msg_fts · unit_fts<br/>FTS5, raw + Snowball ru/en stems"]
    VEC["unit_vec<br/>sqlite-vec, bge-m3 1024-d, local GPU"]
    Q["query + filters (chats, folders, dates)"]
    FUSE["RRF fusion → cross-encoder rerank → dedup"]
    HITS["hits — snippet, date range, deep link"]

    TG -->|"sync: messages after last_msg_id,<br/>re-read of the newest edits"| MSG
    MSG --> UNITS
    UNITS --> FTS
    UNITS --> VEC
    Q --> FTS
    Q --> VEC
    FTS --> FUSE
    VEC --> FUSE
    FUSE --> HITS
```

A separate pass, `grepogram extract`, downloads the photos and attachments those messages carry
and reads the text out of them — OCR through macOS Vision, PDF and DOCX through pure Python — so a
screenshotted announcement is searchable as words rather than as `[photo]`. It runs after a sync,
never inside one; see [Reading Text Out of Media](#reading-text-out-of-media).

Two front ends share that index: `grepogram-mcp`, a stdio MCP server for Claude Code, Cursor,
Codex or any other MCP client, and the `grepogram` CLI for the same searches in a terminal.
The whole index lives in one SQLite file; research, once you switch it on, keeps your decisions
in a second one beside it.

## Requirements

- macOS. On Apple Silicon the models run on Metal (`mps`), otherwise on the CPU, and the default
  paths follow macOS conventions. The code runs elsewhere with `GREPOGRAM_HOME` set, but that is
  untested.
- [uv](https://docs.astral.sh/uv/). The project pins CPython 3.12 (`torch` and `sqlite-vec`
  wheels); uv installs it. The interpreter's `sqlite3` module must be able to load extensions,
  because sqlite-vec is one: uv's own managed builds and Homebrew's `python@3.12` can, while
  python.org's macOS installer build (`/usr/local/bin/python3.12`, which is also what
  `actions/setup-python` installs) and Apple's system Python cannot — they are compiled without
  `--enable-loadable-sqlite-extensions`. uv prefers an interpreter it finds over downloading its
  own, so the commands below pass `--managed-python`; installed under one of the others,
  grepogram refuses to open the index and prints this same list.
- A Telegram account — or several, signed in side by side — and an API key pair from
  https://my.telegram.org/apps; every account signs in through the same pair.
- Optional: about 4.5 GB of disk for the two models (`BAAI/bge-m3`, `BAAI/bge-reranker-v2-m3`),
  downloaded from Hugging Face on first use. Without them every search runs lexical-only. Once
  they are in the cache they load from it alone: grepogram asks huggingface.co nothing about
  files it already has, and reaches the network only when the cache holds nothing — a first
  download, logged at INFO as one. `HF_HUB_OFFLINE=1` forbids even that, and a model missing
  from the cache then degrades the search instead of downloading.

## Setup in Five Minutes

1. Create an application at https://my.telegram.org/apps and note the `api_id` and `api_hash`.

2. Install as a tool — no checkout needed:

   ```sh
   uv tool install --managed-python --python 3.12 'grepogram[dense]'
   ```

   which puts `grepogram` and `grepogram-mcp` into `$(uv tool dir --bin)`. Drop `[dense]` for a
   lexical-only install, or ask for `[dense,media]` to get OCR and document text as well (see
   [Reading Text Out of Media](#reading-text-out-of-media)).

   To run an unreleased change, install from the repository instead — any tag, branch or commit
   after the `@`, and the tip of `main` without one:

   ```sh
   uv tool install --managed-python --python 3.12 \
       'grepogram[dense] @ git+https://github.com/nnemirovsky/grepogram'
   ```

   From a checkout instead, to run it in place or to work on it:

   ```sh
   git clone https://github.com/nnemirovsky/grepogram
   cd grepogram
   uv sync --managed-python --extra dense   # or plain `uv sync --managed-python`
   uv run grepogram --help                  # prefix every command below with `uv run`
   ```

   `--managed-python --python 3.12` makes uv install and use its own CPython 3.12 instead of
   whichever `python3.12` it finds first, which is what keeps sqlite-vec loadable (see
   Requirements); `--python /opt/homebrew/bin/python3.12` does as well. Both flags matter for
   `uv tool install`: without `--python` it reuses an environment it already has, so an install
   made under the wrong interpreter stays broken until you pass it (or `uv tool uninstall
   grepogram` first).

3. Write the config and fill in the keys:

   ```sh
   grepogram config init
   grepogram config path          # prints where the config, sessions, index and log live
   ```

   Edit `~/.config/grepogram/config.toml` and set `[telegram] api_id` and `api_hash`.

4. Sign in. The session is stored as `~/.config/grepogram/session.session` with mode 0600:

   ```sh
   grepogram auth                 # phone number, login code, 2FA password if enabled
   ```

   A second account signs in next to it with `grepogram auth --account work`; see
   [Several Accounts](#several-accounts).

5. Pick what to index. Nothing is indexed unless you add it:

   ```sh
   grepogram dialogs argentina                        # find chats and folders by name
   grepogram sources add "folder:Argentina"           # a Telegram folder (all its chats)
   grepogram sources add @ru_georgia                  # a public chat by username
   grepogram sources add "Buenos Aires chat"          # a chat by (fuzzy) title
   grepogram sources add @somechannel --comments      # a channel plus its discussion threads
   grepogram sources add --since 2024-01-01 -- -1001234567890   # by id (after --), skipping older history
   grepogram sources ls
   ```

6. Sync. The first run fetches the whole history of every source (or from `--since`), builds
   the units, indexes them and, when the `dense` extra is installed, downloads `bge-m3` and embeds
   everything. `--budget` caps the run; the next run continues where it stopped:

   ```sh
   grepogram sync --budget 600
   grepogram search "открыть счёт без DNI"
   ```

7. Connect your agent. `grepogram-mcp` speaks MCP over stdio, so any client that launches a
   command works. In Claude Code, with a checkout at `<path>`:

   ```sh
   claude mcp add grepogram -s user -- uv run --project <path> grepogram-mcp
   ```

   or, after `uv tool install`:

   ```sh
   claude mcp add grepogram -s user -- "$(uv tool dir --bin)/grepogram-mcp"
   ```

   Clients that take a JSON config (Cursor, Windsurf, Codex and others) want the same command:

   ```json
   {
     "mcpServers": {
       "grepogram": { "command": "/absolute/path/to/grepogram-mcp", "args": [] }
     }
   }
   ```

   Then ask something like "what do people in the Argentina chat say about opening a bank account
   without a DNI?". The first search downloads the reranker if it is not cached yet.

Run `grepogram sync` whenever you want the index current; the MCP `search` tool also refreshes an
index older than an hour on its own (see below). A `launchd` job or a cron entry calling
`grepogram sync --budget 300` works fine next to a running MCP server: only one sync runs at a
time, and the two never contend for the session file.

## Upgrading from v0.2.0

Install the new version over the old one; there is nothing to run by hand. The first command
that opens `index.db` migrates it in place (schema steps 7 to 9: accounts, captured links and
forward origins, the discovery clock) and keeps every message, unit, vector and import. The
`session.session` you signed in with becomes the `default` account, and a config without
`[[accounts]]` means exactly what it meant before. The first `sync` afterwards re-stores, once,
the messages among each chat's newest `edit_refetch` that carry links or a forward, now with
those links; every other message stored before the upgrade offers research only the links
visible in its text until you run `grepogram recapture-links`.

**Do not go back to v0.2.0 afterwards.** It refuses the migrated index as newer and tells you
to delete `index.db` and sync again, which gives back everything Telegram still serves and loses
every `import:` history for good, since those chats cannot be fetched again. A config this
version saved also carries a `[research]` section v0.2.0 rejects as an unknown key.

## CLI Reference

Global options: `--version`, `--verbose` / `-v` (DEBUG logging). Command output goes to stdout,
diagnostics and logs to stderr and the log file.

| command | what it does |
|---|---|
| `grepogram config init` | write the annotated config template; refuses to overwrite |
| `grepogram config path` | print the resolved paths of the config, the default session, the `sessions/` directory of the other accounts, the index, `research.db`, the sync lock and the log |
| `grepogram auth [--account NAME] [--label L]` | sign in (phone, code, optional 2FA password) and store the session; `--account` signs in another account, which is added to `[[accounts]]` once the sign-in succeeds |
| `grepogram accounts ls` | every account with its label, session state (`missing`, `present`, `authorized`), the Telegram user it signed in as, its sources and the chats it reaches; offline |
| `grepogram accounts rm <name>` | remove an account: its sources, the chats only they cover, what it was recorded as reaching, its research sessions' unused approvals, and its session file; asks on the terminal first. Nothing changes on Telegram |
| `grepogram dialogs <query> [-n N] [--account NAME]` | find chats and folders of the account whose title, `@username` or folder name matches; prints kind, id, type, title, username, folders, score |
| `grepogram sources add <target> [--since YYYY-MM-DD] [--comments] [--account NAME]` | add a source and save the config; `target` is a chat id, `@username`, `t.me` link, `folder:<name>` or a fuzzy chat / folder title, and an `<account>/` prefix on it names the account as `--account` does |
| `grepogram sources ls` | every source the index holds chats under, with the account that fetches it — the configured ones first, then any others still in the database: an `import:<slug>` from `grepogram import`, and a source removed from the config whose chats are still stored — each with its chats, message counts and last sync |
| `grepogram sources rm <target>` | remove a source and delete the messages and index rows of the chats no other configured source covers — a chat another source (another account's, say) still covers stays and says so; `target` is a source id as `sources ls` prints it (`folder:<name>`, `chat:@name`, `work/chat:-100…`), a folder name, a chat id, `@username` or a fuzzy title; refuses while a sync is running. It never leaves the chat on Telegram |
| `grepogram sources prune [--dry-run]` | delete the indexed chats a folder source no longer lists — what a folder holds *now* is only knowable from Telegram, so this one needs a session; it prints what would go and asks before deleting, keeps a channel's discussion group and says so, never offers an imported chat, and prunes nothing at all if a source failed to resolve |
| `grepogram sync [--budget S]` | fetch new messages from every source of every signed-in account, rebuild units, index and embed; stops cleanly after `S` seconds (at least 1) |
| `grepogram extract [--budget S] [--retry-failed]` | read text out of the media already stored — photos through OCR, PDF and DOCX attachments — and re-cut the units holding it; a network pass, run after `sync`, resumable; `--retry-failed` queues what an earlier run could not read, and what this build had no extractor for, again. See [Reading Text Out of Media](#reading-text-out-of-media) |
| `grepogram prune-deleted [--chat X] [--budget S]` | ask Telegram about every indexed message and drop the ones it no longer has, the discussion group of a channel included; about one request per hundred stored messages, so it is run by hand, resumes where it stopped and is never started by a sync |
| `grepogram recapture-links [--chat X] [--budget S]` | re-read the indexed messages stored without their links — every message an index built before link capture holds — and store their hidden hyperlinks, URL buttons and forward origins, so research sees them; about one request per hundred such messages, run by hand, resumable, and it changes nothing else of a message and moves no sync cursor |
| `grepogram import <dir> [--chat-title T] [--account NAME]` | index a Telegram Desktop JSON export — one chat's `messages.json` or a whole account's `result.json`, every chat in it — of history this account can no longer open; offline, idempotent, and the chats it creates are marked unavailable so no sync fetches them and no prune offers them. `--chat-title` names the one chat of a single-chat export and is refused for an export holding several, which carry their own titles; `--account` names the account the export was made from, whose private chats and legacy groups it holds |
| `grepogram embed [--reembed]` | embed units the dense index does not hold yet; `--reembed` drops every vector and starts over (needed after changing `[models] embed`); refuses while a sync is running |
| `grepogram search <query> …` | search the index, see below |
| `grepogram thread <chat> <msg_id> [--json]` | print the whole reply thread a message belongs to, root first; for a channel post, the post followed by its comments from the linked discussion group |
| `grepogram context <chat> <msg_id> [--before N] [--after N] [--json]` | print the messages around one in its chat, bounded to the message's own thread or forum topic where Telegram gave it one, the message included (15 each way by default) |
| `grepogram leave <target> [--account NAME]` | leave a group or channel on Telegram as the account, after asking on the terminal; the one command that changes an account on Telegram, and it changes nothing in the config or the index |
| `grepogram research …` | discover chats the index does not hold yet, from a question and seed chats: `start`, `discover`, `candidates`, `approve`, `skip`, `exclude`, `unexclude`, `run`, `status`, `stop`; off until `[research] enabled = true`. See [Research](#research-finding-chats-you-do-not-index-yet) |
| `grepogram-mcp [-v]` | the MCP server over stdio (what an MCP client launches) |

Chat ids are negative for groups, supergroups and channels (`-100…`); when one is a positional
argument, put `--` before it: `grepogram sources add --since 2024-01-01 -- -1001234567890`.

`search` options:

| option | meaning |
|---|---|
| `-c`, `--chat <spec>` | restrict to these chats (repeatable): id, `<account>/<id>`, `@username`, `t.me` link, `folder:<name>`, `import:<slug>`, `account:<name>` or a title / folder name (substring, then fuzzy) |
| `-a`, `--account <name>` | restrict to the chats this account reaches (repeatable) — a scope, not isolation: a channel two accounts reach is in both scopes |
| `--since <when>`, `--until <when>` | date bounds on the unit's start: an ISO date (`2025-06-01`), month (`2025-06`), datetime (`2025-06-01T14:30`, optional seconds and `Z` / `+03:00`) or an age (`7d`, `3w`, `6m`, `1y`); `--until` is inclusive; naive input is UTC |
| `--mode hybrid\|lexical\|dense` | `hybrid` (default) fuses BM25 and embeddings, `lexical` is BM25 over stems only, `dense` is embeddings only; without vectors or a model every mode falls back to lexical with a warning |
| `-k`, `--limit N` | number of hits (default `[search] k`) |
| `--rerank` / `--no-rerank` | re-score the candidates with the cross-encoder (default on; skipped with a warning when the model cannot load) |
| `--full` | include each hit's whole unit text |
| `--json` | print the result document as JSON and nothing else on stdout |

Text output prints one block per hit — rank, score, unit kind, chat, UTC date range (followed by
`via <accounts>` when an account other than the default one reaches the chat), the deep link
(and a fallback link for private chats), then the snippet. The `score` ranks the hits of *this*
answer against each other and means nothing across two searches: it is normalised across the
candidate set before the reaction bonus goes on (see [How Search Works](#how-search-works)).

`thread` and `context` are what a hit leads to, the CLI half of the MCP tools of the same names:
read the conversation around a hit instead of guessing from its snippet. `<chat>` takes the same
specs as `-c`, but has to name exactly one indexed chat — a folder name, or a title several chats
share, is an error listing them to pick from. Each message prints as `chat_id/msg_id`, the UTC
timestamp and the sender, then its link and its text; `--json` prints the same
`{chat_id, msg_id, messages}` document the MCP tools return and nothing else on stdout. A channel
post's thread carries the comments from its discussion group, so every message names the chat it
is really in — that is the id to pass back, because the group numbers its messages from 1 exactly
as the channel numbers its posts. Options come before the `--` that protects a negative id:

```bash
grepogram search "открыть счёт без DNI" -k 5
grepogram thread @arg_chat 1284
grepogram context --before 5 --after 5 -- -1001234567890 1284
```

Three commands keep an index honest rather than grow it, and none of them is silent.
`sources prune` deletes the indexed chats a folder source no longer lists. What a folder holds
*now* is only knowable from Telegram, so it resolves over the network first and takes the sync
lock afterwards, for the deletion alone; it prints what would go and what it kept — a channel's
discussion group is kept, and the message names the channel keeping it — and asks before deleting
anything. A source Telegram will not answer for stops the prune outright: a folder that failed to
resolve is not a folder that lists nothing, and treating it as one would offer to delete a whole
indexed history after one transient error.

`prune-deleted` goes the other way. It asks Telegram about every message the index holds, in
batches of a hundred oldest first, and removes the ones Telegram no longer has, following a
channel to its discussion group so a deleted *comment* is caught too. That is about one request
per hundred stored messages, which is why it is yours to run and never a sync's; a run stopped by
`--budget` or a flood wait keeps every batch it finished and the next one carries on from its
cursor. Anything Telegram declines for some other reason is left where it is.

`recapture-links` fills in what an older index never kept. Messages stored before grepogram
captured links carry only their text: their hidden hyperlinks, URL buttons and forward origins
were not stored, and a sync re-reads only the newest messages of a chat. This command asks
Telegram about exactly those messages, a hundred per request, through an account that reaches the
chat, and stores their links and forward origins — nothing else of a message changes, no unit is
re-cut, no sync resumes from anywhere else, and a message Telegram no longer has is left to
`prune-deleted`. Like `prune-deleted` it is yours to run and resumes from where a budget or a
flood wait stopped it. An imported chat is never re-read.

`import` reads a Telegram Desktop export — Settings → Advanced → Export Telegram data, in the
machine-readable JSON format — of history this account can no longer open. Point it at the
directory: a single chat's export (`messages.json`) is one chat, and a whole-account export
(`result.json`) brings in **every chat it holds**, each under its own tag. `--chat-title` names
the chat of a single-chat export, which carries no title of its own, and is refused for an export
that holds several. Messages are stored, cut into units and indexed exactly as a sync's are, so
`search` answers from them immediately; each chat is tagged `import:<slug>` and marked unavailable
so no sync fetches it and no prune offers it. The units are embedded inline when the model is
available, and when it is not the command says `next: grepogram embed`. Running the same import
again updates what it stored rather than adding a second copy, `sources add` over an imported chat
is refused by name instead of quietly taking it over, and a live source that later comes to cover
one keeps its hands off it as well — including a channel with `comments = true` whose linked
discussion group turns out to be an imported chat: the posts are synced, the comments are not, and
the sync says why. Taking such a chat over is `grepogram sources rm import:<slug>`, deliberately.

## Reading Text Out of Media

A photographed embassy announcement, a rental contract as a PDF, a price list someone
screenshotted — before v0.2.0 the index held `[photo]` and `[document: contract.pdf]` and nothing
of what they said. `grepogram extract` reads that text and puts it where search can find it.

```sh
uv tool install --managed-python --python 3.12 'grepogram[dense,media]'
# from a checkout instead: uv sync --managed-python --extra dense --extra media

grepogram sync                  # first, so there is something to extract
grepogram extract --budget 600  # then, as often as you like; it resumes where it stopped
grepogram sync                  # embeds the units the extraction re-cut
```

**It is a network pass, not an offline one.** Telethon downloads from a message Telegram just
returned and never from a stored row, so every pending message is re-fetched by id before its file
is downloaded — which is also where the file's size comes from. It runs *after* a sync and never
inside one, because a 400-page PDF or a slow OCR must not eat a sync's budget. `--budget` is in
seconds like `sync`'s, a flood wait is respected the same way, each batch of 50 commits on its
own so an interrupted run keeps what it earned, and the downloaded file is deleted whatever
happens to it. Nothing is downloaded at all for media whose kind this build cannot read, or that
`[media]` switches off, or that is an attachment in a format nothing here reads: those are settled
before the first request, from the stored `media_kind` and — for documents, where `.xlsx`, `.zip`
and every other attachment share one kind — from the stored file name, which is enough to know
that only `.pdf` and `.docx` can be read.

**What the text does.** It is rendered into the unit's line for that message — next to a caption
when there is one, in place of the bare placeholder when there is not — with the `[photo]` /
`[document: …]` marker kept visible, so a reader can always tell that a machine read this off an
image rather than someone typing it. The extraction then re-cuts the units holding those messages
on the spot, closed windows included, which is the whole point: a sync re-cuts only a chat's open
window, and all but the newest handful of any chat's history sits in windows closed long ago. The
re-cut units are indexed immediately and **embedded by the next `grepogram sync`** (or
`grepogram embed`), which is what the command's closing line reminds you to run.

**How much of a file is read.** At most 4000 characters per message, and the extractors stop
reading once they are past it rather than parsing the rest. A 400-page PDF therefore contributes
its opening and nothing else: the text is rendered into the unit holding that message, and a
message longer than `units.window_max_chars` becomes a window of its own, so an uncapped contract
would be one enormous unit no embedder sees the end of.

**What OCR reads.** Russian and English only. Those are the languages grepogram asks Vision for,
narrowed to what the running macOS actually supports, so a Spanish, Georgian or Greek screenshot
comes back as whatever those two make of it — usually little, and the row is still recorded as
read rather than failed.

**What it needs.** The `media` extra, which brings `pypdf`, `python-docx` and — on macOS —
`pyobjc-framework-Vision`. PDF and DOCX are pure Python and work anywhere, and only `.pdf` and
`.docx` attachments are read at all — anything else is settled from the file name before it is
fetched. OCR is macOS Vision, so it needs a Mac, and **Russian recognition needs macOS 15**:
Vision learned Russian there, and grepogram asks it what it supports and requests only that rather
than failing the whole request over a language the system does not know. Where any of it is
missing nothing breaks — the media is parked as "no extractor here" and `--retry-failed` picks it
up once the extra is installed.

Every message with media carries a state, and `grepogram extract` reports them:

| state | what it means | what to do about it |
|---|---|---|
| pending | not looked at yet | `grepogram extract` — except in an imported or unavailable chat, where nothing can be fetched and the media stays pending for good; those rows are counted apart, as `in chats nothing can re-fetch`, so `media pending` only ever names work another run could do |
| read | the file was read; the text may still be empty, which is what a photo holding no text looks like | nothing |
| no extractor here | this build cannot read that kind: a video, sticker or poll (which nothing reads), an attachment that is neither PDF nor DOCX (a `.xlsx`, a `.zip`, an `.apk` — decided from the file name, never downloaded), a document without the `media` extra, a photo off macOS or without the extra, or a voice message or video note — those wait for whisper in v0.3.0 | install the `media` extra if it applies, then `grepogram extract --retry-failed` |
| could not be read | a corrupt file, a mislabelled one (a `.docx` holding a PDF), a download that failed | `grepogram extract --retry-failed` |
| too large to download | Telegram reported it larger than `media.max_download_mb`, so it was never fetched | raising the cap does not queue it again; nothing re-reads a skipped file |
| switched off in `[media]` | `enabled`, `ocr` or `documents` is `false` for that kind | switch it back on — the next `extract` queues it again by itself |

`[media]` is documented key by key under [Configuration](#configuration). Turning a kind off is
not destructive: text already extracted stays extracted and stays searchable, and only new work
stops.

## Several Accounts

grepogram signs in any number of Telegram accounts at once, a personal one and a work one say,
and every one of them fetches its own sources into the one index that one search spans. It is
not an account switcher: no account is "active", and every account that is signed in syncs.

```sh
grepogram auth --account work --label "work phone"   # sessions/work.session, listed under [[accounts]]
grepogram accounts ls                                 # session state, user, sources, chats reached
grepogram dialogs --account work payroll
grepogram sources add --account work @team_channel    # or the source id: work/chat:@team_channel
grepogram sync                                        # every signed-in account, one run
grepogram search "отпуск" --account work              # only what the work account reaches
```

The account `grepogram auth` signs in without `--account` is `default`. It keeps
`session.session` and needs no `[[accounts]]` entry, so an install from before accounts existed
simply is its `default` account. Every other account keeps its session in
`sessions/<name>.session`. All of them share the one `[telegram]` app, and a source belongs to
the account named by its `account` key. An account name stays one Telegram user: once a sign-in
or a sync has recorded who it is, `grepogram auth` under that name as someone else is refused,
leaves the earlier session as it was and signs the refused one out again; so is a sign-in while
the index cannot be read, since nothing could say who the name is. A session file put in place
by hand as someone else is left out of every pass that talks to Telegram — a sync, `extract`,
`prune-deleted`, `recapture-links` and `sources prune` go on without it and warn, and research
refuses to discover or run. Sign the other user in under a name of its own, or `grepogram
accounts rm <name>` first. Its id carries the name, `work/chat:@team_channel` or
`work/folder:Payroll`, so two accounts can each list `chat = 12345` and mean two different
private chats.

**Which chats are shared.** Channels and supergroups have one id and one message numbering for
everybody, so a channel two accounts reach is **one** chat in the index, fetched once per sync by
the account of its source and searched once. Private chats, bots and legacy groups are numbered
per account by Telegram, so one person's private chats with two of your accounts are **two**
chats, each with its own history. They share a Telegram id, so a reader asked for `thread 42 7`
answers with the candidates and `thread work/42 7` names one. Every hit and every message
carries `peer_id` (the chat's Telegram id) and `accounts` (the accounts that reach its chat, none
for an import). The text output adds `via <accounts>` when an account other than `default` does.

**Scopes, not isolation.** `--account work` on `search` (`accounts` on the MCP tool) and the chat
spec `account:work` narrow a query to the chats that account reaches, a channel's discussion
group included. A channel both accounts reach is in both scopes. Every account belongs to the same
local user and every source is opt-in, so a scope decides what you search, not who may read it.

**How the commands split the work.** `sync`, `extract`, `prune-deleted` and `recapture-links`
use every signed-in account. Each chat goes through the account of the source that owns it. When Telegram refuses a
shared chat to that account, or that account is not signed in, the chat is tried through another
account that reaches it, and it stays the first source's chat either way. A flood wait stops
only the account it hit, even while its sources are being read, and the others carry on.
`prune-deleted` is stricter, because an account that joined a group late can see older messages
as deleted while another still reads them: it removes a message only when every account that
reaches the chat says it is gone, and leaves the chat alone while one of them is signed out.
`extract`, `prune-deleted` and `recapture-links` report a chat that no connected account reaches
as unreachable, not as an error. `sources prune` reads each folder through the account that owns
its source, and a folder whose account cannot connect counts as a folder that did not resolve:
nothing is pruned at all until every folder answers. An account with no session, or one Telegram has signed out, is left out with a
warning (the MCP `sync` lists it under `accounts_skipped`) and the rest still sync. `dialogs`, `sources add`, `import` and `leave` act as
one account, `default` unless `--account` names another.

**Removing.** A chat that two sources cover (a channel two accounts configured, or a chat that a
folder and a `chat` entry both list) is deleted only when the last of them goes. Until then,
`sources rm` hands it to a remaining source and says which chats it kept. `accounts rm <name>`
removes that account's sources under the same rule, all of it or nothing. It also forgets which
chats the account reached and the peers its syncs cached, stops its research sessions so no approval outlives it, and deletes its
session file, after asking on the terminal. The only account left cannot be removed. `default`
can be removed while another account exists: that deletes `session.session` and the `default`
sources, and `accounts ls` still lists `default` with its session `missing` until
`grepogram auth` signs it in again. `--label` belongs to a named account; `auth --label`
without `--account` is refused, `default` having no `[[accounts]]` entry to hold it.
**Neither command leaves anything on Telegram.** `grepogram leave <target> --account <name>` is
the one command that does. It asks on the terminal first, refuses private chats, bots
and folders, and touches neither the config nor the index.

## Research: Finding Chats You Do Not Index Yet

Research starts from a question and chats you already index and looks for the chats they lead
to. It collects the links, mentions, hidden hyperlinks, URL buttons, link previews, forward
origins and shared-folder links in your chats and in their pinned posts, and notices which of
your chats are directories. After you approve specific chats and specific
actions, it joins them, requests admission, and fetches them as ordinary sources. It is **off
until you switch it on**: set `enabled = true` under `[research]` in `config.toml`, and until
then every research command and tool refuses.

A session, end to end:

```sh
grepogram research start "where do people compare bank fees?" -s "folder:Argentina" -s @arg_chat
grepogram research discover 1        # leads in the seeds, then a metadata probe of the best ones
grepogram research candidates 1      # ranked, with the evidence behind each
grepogram research approve 1 4 9:join,fetch,add_source   # asks you on this terminal; type the code back
grepogram research run 1             # carries out exactly that, within the session's budgets
grepogram research status 1
grepogram research stop 1            # explores no further; the sources it added stay
```

- **start** takes the question, the seed chats (any spec `search --chat` takes) and the account
  that will later join and fetch (`--account`, `default` otherwise). It also fixes the session's
  limits from `[research]`; `--max-depth`, `--max-candidates`, `--probe-limit`, `--since-days`,
  `--max-messages` and `--budget` override those, while `max_session_candidates` and
  `admission_timeout_days` always come from the config.
- **discover** reads the indexed messages of the seeds, and of every chat a run fetched for the
  session one hop further out — a channel's discussion group always with its channel — and
  proposes each chat they name as a candidate. A candidate is a chat: a link to a post leads to
  its channel, and a mention of a person is not a candidate. It picks up where it stopped, and a
  comment that arrives late or a message that gains a link since is read too. Messages stored
  without their hidden hyperlinks and buttons (before this version, or by an import of an export
  that kept no formatting) are read by the URLs and `@mentions` visible in their text, and the
  report says how many were read that way; `grepogram recapture-links` fetches what they hid.
  It also reads the **pinned posts** of those chats, once each and whatever their age, since a
  directory often keeps its index in a post pinned years ago: what they lead to is kept as
  evidence (`pinned`) and never stored as messages of the index. A chat whose messages name at
  least ten distinct chats is a **directory**: every chat found in it carries a `directory` path
  in its evidence, and approving the directory approves none of them. A forward from a channel
  the index does not hold is probed with what the sync learned about that channel when it
  fetched the forward — its username and the account's access hash — so it is not a dead end.
  `max_candidates` bounds one call and `max_session_candidates` the whole session. Then it
  **probes** the best `probe_limit` candidates as the session's account. A probe reads metadata
  only: title, type, size, whether the account is a member, and whether joining needs the
  admins' approval. It never reads history. A shared-folder (`addlist`) link turns into one
  candidate per chat in the folder. `--offline` skips the probing, the pinned posts and the
  global search and asks Telegram nothing; a candidate nothing has probed yet cannot be
  approved, so one found offline, or left over past `probe_limit`, waits for the next online
  `discover`. Discover is also the one step that sends a global search (see below).
- **candidates** ranks candidates by **corroboration**, which counts distinct origins: a post
  forwarded into ten chats is one piece of evidence, not ten. Ties go to how many of the
  question's words the evidence shares, and then to depth. Three facts are kept apart for each
  one: `member` (the account is in it), `cached` (the index already holds it, and through which
  accounts) and `authorized` (what you approved). `--status` (repeatable) narrows the list and
  `--evidence N` sets how many pieces of evidence each candidate prints (3 by default). A
  candidate is `proposed`, `approved`, `skipped`, `excluded`, `joined` (in, not fetched yet),
  `pending_admission`, `fetched` (a source now), `unavailable` (Telegram refused it) or
  `failed` (a run could not finish it; approving it again retries). `start`, `discover`,
  `candidates`, `run` and `status` take `--json` and print the document the matching MCP tool
  returns.
- **approve** grants named candidates named actions: `join`, `request` (an admission request),
  `fetch` and `add_source`. `fetch` always comes with `add_source`. A bare id means joining the
  chat, public or private, and fetching it as an ongoing source (`join,fetch,add_source`). It
  becomes `request` instead of `join` when the chat's admins approve who joins, and just
  `fetch,add_source` when the account is already in. To read a public chat without joining it,
  approve `ID:fetch,add_source` explicitly. The exact text of what will happen is shown first.
  It gives each chat, the account, and each action in words: how the account gets in, whether
  comments come along, and that the chat becomes an ongoing source that regular sync and search
  will include from then on. For a chat that a configured source already covers, it names that
  source, and the account, horizon and comments the fetch will really use. Titles, usernames and
  the question are printed on one line with any control or invisible formatting character shown
  as `�`, so a chat's name cannot forge or hide a line. The question itself is limited to one
  line of plain text of at most 500 characters. Some approvals are refused before anything is
  asked: a candidate not probed yet, a person or a bot, a shared folder itself (approve its
  chats), an excluded, `unavailable` or `fetched` one; `fetch` without `add_source`; `join`
  where the admins approve joins (that is `request`) and `request` anywhere else; `join` and
  `request` together; and reading a private chat the account is not in without `join` or
  `request`.
- **run** re-checks pending admission requests — one no admin answered within
  `admission_timeout_days` is given up as `failed`, and approving `request` again sends a new one
  — and joins or requests exactly the approved chats.
  It adds each as a source of the session's account, with history back to the session's horizon
  (`since_days` before the session started) and comments for a channel. It joins and adds the
  very chat the probe saw: every source it adds names the chat by its id — a public chat read
  without joining too, through the access hash the probe got — so later syncs keep reading that
  chat whatever its username does, and a freed name registered by someone else is never
  followed (a source you add yourself as `@name` keeps following the name). A chat whose
  username has since moved to another chat is never joined through it — when a later discover,
  search or admission check sees that name on another chat, the approved candidate is set aside
  (`failed`, its approval voided) and the other chat is proposed on its own. It fetches exactly those
  chats through an ordinary sync, bounded by `run_budget_s` and `max_messages_per_run`. If
  another sync holds the lock, the run adds no sources and fetches nothing — joins it already
  made stay made — and the report says so (`stopped_by: sync_busy`); the next run takes it
  from there. Then it reads the new messages and the pinned posts of what it
  fetched one hop deeper and only *proposes* what they lead to. A run stopped by a budget or a flood wait resumes on the
  next `run`, and approvals it has not carried out yet stay valid.
- **skip**, **exclude** and **unexclude** only narrow the session and need no confirmation.
  Skipping sets candidates aside and voids their approvals, including the fetch still pending for
  a chat a run already joined. An exclusion is global and permanent: that chat is never proposed
  again, in any session, until you `unexclude` it. It covers the chat under every name it is
  known by (`@name`, its id, an invite link), and it withdraws whatever is still approved for
  it. Both take candidate ids (with `--session` / `-s` naming their session), `@usernames`,
  `t.me` links or marked chat ids. `research status` lists every exclusion under the name it
  was made by, with the `--reason` it was given, and `unexclude` lifts it by that name: an
  exclusion covers every spelling of the chat, but only its own lifts it. A chat found
  under two names in one session is one candidate.
- **stop** ends the exploring and voids the approvals no run used. **Every source a run added
  stays**: it is an ordinary source now, synced and searched like the rest. Take one out with
  `grepogram sources rm`, which, like every removal, never leaves the chat on Telegram. Leaving
  is `grepogram leave`.

**Consent is a human's, and only two things can give it.** On a terminal,
`grepogram research approve` writes the summary to the controlling terminal and reads the answer
there, never from stdin, and you confirm by typing back a random code it shows. A pipe or a
blind `yes` cannot answer, without a terminal it refuses, and it has no `--yes`. That check holds
against an agent that only has the MCP tools. It does not hold against an agent that can run
shell commands: such an agent can give the command a terminal of its own and read the code off
it. Run `research approve` yourself, and do not let an agent run it for you. Through MCP,
`research_approve` shows the same summary through the client's elicitation dialog, which only
you can answer. A client without elicitation is answered with the exact
`grepogram research approve …` command for you to type yourself. No tool argument stands in for
either. An approval covers the chats it names and nothing found inside them: approving a
chat approves none of the chats its messages lead to, and a shared folder is approved chat by
chat, never as a whole. A later run reuses an approval until its work is done or the session
stops, so nothing asks twice for work you already approved.

**Telegram's own search stays off by default.** `chat_search` (Telegram's public chat search by
name) and `post_search` (its public-post search) are `false`. Even with them on, a session
searches only after you approve `global_search` for it
(`grepogram research approve 1 global_search`, and `paid_search` beside it to allow paying).
The search is sent by the next online `discover`, with the session's question as the query,
once per kind of search per session; a run never searches. That approval's text says that the
question is sent to Telegram and may bring back snippets from chats you know nothing about. The
results are stored as candidates and evidence in `research.db` and never as indexed messages.
A search sends the session's question and nothing else. Post search has a small free daily
quota, which grepogram asks about before every search. Past that quota Telegram charges Stars,
and grepogram never pays unless `paid_stars_max` is above `0` *and* you approved `paid_search`
separately. The price must also fit under `paid_stars_max`, and one such approval pays for one
search, even when two searches run at once. An approval keeps what its text named: raising
`paid_stars_max`, or switching on the other kind of search, after approving covers nothing until
you approve again; it is a grant of its own, so spending it leaves the
`global_search` approval standing.

Research keeps its sessions, candidates, evidence, approvals and exclusions in `research.db`, a
file of its own next to `index.db`. The index can be deleted and rebuilt with one sync, and your
decisions are not lost with it: `research.db` names every chat as Telegram does — its scope and
peer id, never an index row id — so a rebuilt index, or a private chat stored again under
another row, is still the same conversation to a session, which then reads it from the start.

## MCP Tools

The server is named `grepogram` and offers eighteen tools. Every tool returns one JSON object. Expected failures — no
session, another sync running, an unknown chat or message, a model that cannot load, an
ambiguous target — come back as `{"error": …, "hint": …}` (plus `candidates` when there is
something to choose from) rather than a tool error, so the agent can act on them. `warnings` are
advisory; the data next to them is valid.

| tool | arguments | returns |
|---|---|---|
| `search` | `query`, `chats: list[str] \| null`, `since`, `until`, `k=10`, `mode="hybrid"`, `rerank=true`, `full=false`, `accounts: list[str] \| null` | `{hits, warnings, index_age_min, synced}`; each hit has `score` (a within-result-set number — it orders this answer and compares across nothing else), `chat` (id, type, title, username, …), `peer_id` (the chat's Telegram id), `accounts` (the accounts that reach the chat, empty for an import), `kind` (`window` / `thread` / `post`), `date_start`, `date_end` (unix seconds, UTC), `anchor_msg_id`, `url`, `fallback_url`, `snippet`, `msg_ids`, `text` (with `full`) |
| `thread` | `chat_id`, `msg_id` | `{chat_id, msg_id, messages}`: the whole reply thread the message belongs to, root first; for a channel post, the post followed by its comments — those live in the discussion group, so the list spans two chats and each message names its own |
| `context` | `chat_id`, `msg_id`, `before=15`, `after=15` | `{chat_id, msg_id, messages}`: the surrounding messages in the same chat, bounded to the message's own thread or forum topic where Telegram gave it one |
| `sync` | `budget_s=45` | the sync report: `new`, `chats_done`, `chats_remaining`, `unavailable`, `warnings`, `accounts_skipped`, `index_age_min`; every signed-in account fetches its sources, and one whose session is missing or signed out is listed in `accounts_skipped` with its `error` and the `hint` that signs it in while the others sync |
| `sources` | — | `{sources, index_age_min}`: every source the index holds chats under, with its `account` and its chats (`id`, `title`, `type`, `username`, `message_count`, `last_sync_at`, `unavailable`, `accounts` — the accounts that reach it) — the configured sources first, then any other `source_id` still in the database, including an `import:<slug>` from `grepogram import` |
| `dialogs` | `query`, `account=null` | `{query, account, matches}`: chats and folders of the account (the default one when omitted) matching the name; each match carries `kind`, `id`, `title`, `type`, `username`, `folders`, `score` and `target`, the string to pass to `sources_add` — `<account>/chat:…` / `<account>/folder:…` for an account other than the default one |
| `sources_add` | `target`, `since=null`, `comments=false`, `account=null` | `{source, kind, title, chats, hint}` after saving the config; the source belongs to `account`, else to the account an `<account>/` prefix on `target` names, else to the default one |
| `sources_remove` | `target` | `{source_id, removed_chat_ids, kept_chat_ids, config_updated}` after deleting the data of the chats no other source covers; `target` is a source id from `sources` (`folder:<name>`, `chat:<value>`, `<account>/chat:<value>`), a folder name, a chat id, `@username` or a fuzzy title; `error` while a sync is running |
| `accounts` | — | `{accounts, hint}`: every account (offline) with `name`, `label`, `session` (`missing` / `present` / `authorized`), `user_id`, `display_name`, `sources`, `chats` and, for a missing session, the `hint` that signs it in; accounts are signed in and removed from a terminal only |
| `research_start` | `question`, `seeds: list[str]`, `account=null`, `max_depth`, `max_candidates`, `probe_limit`, `since_days`, `max_messages_per_run`, `run_budget_s` (each `null` = the `[research]` default) | the session, as `grepogram research start --json` prints it: `id`, `question`, `account`, `seeds` (each `{scope, peer_id}`, the chat as Telegram names it), `limits`, `state`, `progress`, `horizon` (the date the sources a run adds start from) |
| `research_discover` | `session_id`, `offline=false` | the discover report: `leads`, `new_candidates`, `updated_candidates`, what was left out (`beyond_depth`, `excluded`, `over_cap`, `session_full`), `directories`, `pins` (the pinned posts of the session's chats, read once each for leads and never indexed), `probe` (read-only metadata of the best candidates, as the session's account) and `searches` (the global searches the user approved); `offline` asks Telegram nothing |
| `research_candidates` | `session_id`, `status: list[str] \| null` | `{session_id, question, account, state, candidates}`, best corroborated first; each candidate keeps three facts apart — `member`, `cached` (with `cached_accounts`) and `authorized` — next to `corroboration` (distinct origins: forwards of one post count once), `overlap` and every piece of `evidence`: its `via`, the chat it was found in as `scope` and `peer_id`, and as `chat_id` — that chat's index row now, what `thread` and `context` take, `null` for a chat the index does not hold — with `msg_id`, `origin_key` and a `snippet` |
| `research_approve` | `session_id`, `items: list[str]` (`ID:join,fetch,…`, a bare `ID`, `global_search`, `paid_search`) | asks the user through MCP elicitation with the exact approval `summary`; `{approved: true, grants, …}` only when they accept and tick approve, `{approved: false, answer, …}` otherwise; a client without elicitation gets `error` and a `hint` naming the `grepogram research approve …` command for the user to type in their own terminal |
| `research_skip` | `session_id`, `candidate_ids: list[int]` | `{session_id, skipped}`; approvals they held are voided — narrowing needs no approval |
| `research_exclude` | `targets: list[str]`, `session_id=null`, `reason=null` | `{excluded, hint}`: never proposed again in any session; lifting an exclusion is `grepogram research unexclude`, in a terminal |
| `research_run` | `session_id` | the run report as `grepogram research run --json` prints it (`admitted`, `joined`, `pending_admission`, `sources_added`, `fetched`, `partial`, `unavailable`, `failed`, `messages`, `stopped_by`, `pins`, `discovery`, `warnings`) plus `accounts_skipped` |
| `research_status` | `session_id=null` | `{sessions, exclusions}` in brief (each exclusion with its `reason`), or one session in full: `session`, `candidates` by status, `pending_grants`, `pending_admission` |
| `research_stop` | `session_id` | `{session_id, stopped, grants_voided, hint}`; the sources its runs added stay |

Messages in `thread` and `context` have `chat_id`, `peer_id`, `msg_id`, `date`, `from_name`,
`text` (a `[photo]`-style placeholder for media without a caption, followed by whatever
`grepogram extract` read off it), `url`, `fallback_url`, `reply_to_msg_id` and `accounts`. A message's `chat_id` is the chat it is really in, which the top-level one
need not be: a channel post's comments come back under the discussion group's id, and comment
ids collide with the channel's post ids (both number from 1), so pass a message's own `chat_id`
back to `context` alongside its `msg_id`.

The `research_*` tools refuse with `error` and `hint` while `[research] enabled` is `false`.
Consent is the user's alone and no tool argument can stand in for it: `research_approve` shows
the user the same summary `grepogram research approve` prints on a terminal, through the MCP
client's elicitation, and grants only on an accepted answer whose `approve` box is ticked —
a decline, a cancel, an unticked box or a failed request grants nothing. A client that cannot
elicit is answered with the exact terminal command instead, for the user to type in their own
terminal; it asks on the controlling terminal and nowhere else, and an agent must never run it
for them.

Several CLI commands have **no tool here, deliberately**: `sources prune` and `prune-deleted`
delete indexed history, `extract` and `recapture-links` are long flood-exposed network passes,
`import` reads a
directory the server has no reason to be looking at, `auth` and `accounts rm` sign accounts in
and out (the `accounts` tool only lists them), `leave` is the one command that changes an account
on Telegram, and `research unexclude` lifts a decision the user made. They stay in the terminal,
and an agent that needs one should say so rather than find it. The `sync` tool is also the door to a pending unit re-cut: an
explicit `sync()` moves one along, four chats a run, while the automatic refresh inside `search`
never starts one however long its budget.

The server's `instructions` tell the agent how to use the tools: run two or three query variants
(Russian and English, the specific term and the concept, synonyms), prefer `lexical` for exact
tokens such as bank names or IDs, prefer recent hits for anything regulatory or price-related and
state the date of the evidence, call `thread` or `context` before concluding from a snippet, cite
the hit's `url` per claim, say so when nothing relevant comes back, and use `sources` / `dialogs`
/ `sources_add` / `sync` when the user names a chat that is not indexed yet. For research they lay
out the loop — start, discover, read the evidence, ask the user to approve, run, analyse with
`search` / `thread` / `context`, stop — say that forwards and copies of one post are one source
rather than independent corroboration, and name the account a claim came through when
accounts differ.

The server re-reads `config.toml` when the file changes, so a source added with the CLI while
an agent session runs is picked up by the next tool call. Every change to the file — by the server or
by `grepogram sources add` / `rm`, `auth --account`, `accounts rm` or `research run` in a
terminal — is a read-modify-write under `config.lock`, so one side's save never undoes the
other's. Models are loaded once per server process; a model that
fails to load is not retried until the server restarts.

## Configuration

`grepogram config init` writes this file to `~/.config/grepogram/config.toml` (mode 0600). Every
key is optional; the values shown are the defaults. Every command that edits the file rewrites
it without comments: `sources add` / `rm`, `auth --account` (which lists the new account),
`accounts rm`, a `research run` that adds sources, and the MCP `sources_add`, `sources_remove`
and `research_run`.

```toml
[telegram]
api_id = 0                             # create an app at https://my.telegram.org/apps
api_hash = ""

[models]
embed = "BAAI/bge-m3"                  # sentence-transformers id; change → full re-embed
rerank = "BAAI/bge-reranker-v2-m3"
device = "auto"                        # auto → mps if available else cpu
max_seq_length = 512                   # token cap for both models; change → `embed --reembed`

[search]
k = 10
rrf_k = 60
rerank_top = 40
dedup_overlap = 0.5
reaction_weight = 0.05                 # most a unit's reactions add to its reranked score
vec_fanout_max = 8                     # above this many chats → one KNN with k*4, post-filtered
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

[media]
enabled = true                         # master switch for the extraction pass
ocr = true                             # photos through macOS Vision (the `media` extra)
documents = true                       # pdf and docx
max_download_mb = 20                   # anything larger is skipped, never downloaded

[research]
enabled = false                        # research tools refuse until this is true
chat_search = false                    # contacts.search for public chats by name
post_search = false                    # channels.searchPosts (public posts)
paid_stars_max = 0                     # 0 = never pay for post search
max_depth = 2                          # hops from a seed chat a candidate may be
max_candidates = 50                    # per discover call
max_session_candidates = 500           # per session, over every discover call and run
probe_limit = 20                       # username / invite / addlist probes per discover call
since_days = 365                       # horizon given to sources a research run adds
max_messages_per_run = 5000
run_budget_s = 300
admission_timeout_days = 30            # an unanswered admission request is given up after this

# The account `grepogram auth` signs in is "default" and needs no entry. Every other account
# signed in at the same time is listed here; all of them share the [telegram] app:
#
# [[accounts]]
# name = "work"                        # a-z, 0-9, _ and -; session in sessions/work.session
# label = "work phone"                 # optional, for your own reference

# Sources are opt-in. Add them with `grepogram sources add <target>` or by hand:
#
# [[sources]]
# folder = "Argentina"
#
# [[sources]]
# chat = "@ru_georgia"                 # or "https://t.me/…" or 123456789
# account = "work"                     # optional: the account that fetches it (default: "default")
# since = "2024-01-01"                 # optional: skip older history on first sync
# comments = false                     # channels only: also index linked discussion threads
```

| key | meaning |
|---|---|
| `telegram.api_id`, `telegram.api_hash` | the application credentials from my.telegram.org; every command that talks to Telegram refuses to run while they are unset |
| `models.embed` | sentence-transformers model for the dense index; the model name and vector width are recorded in the index, and a change is refused until `grepogram embed --reembed` |
| `models.rerank` | sentence-transformers cross-encoder used when `rerank` is on |
| `models.device` | `auto`, `mps` or `cpu`; on `mps` the weights run in fp16 |
| `models.max_seq_length` | how many tokens of a unit either model reads; the rest is truncated. Raising it embeds more of a long unit and costs encode time — see [Unit length and the token cap](#how-search-works). Nothing detects a change, so it takes effect on an existing index only after `grepogram embed --reembed` |
| `search.k` | default number of hits for the CLI |
| `search.rrf_k` | the constant in `1 / (rrf_k + rank)` |
| `search.rerank_top` | how many fused candidates the cross-encoder re-scores; each retrieval list is fetched `max(k, rerank_top)` deep |
| `search.dedup_overlap` | drop a hit when at least this share of its message ids already belongs to a better hit of the same chat; a value above 1.0 disables dedup |
| `search.reaction_weight` | how much a unit's reactions can raise it after reranking. The cross-encoder's scores are min-max normalised across the candidate set and this much times `log1p(reactions) / (1 + log1p(reactions))` is added, so a well-received message wins a near-tie without a popular unit overtaking a relevant one; `0` switches it off and leaves the reranker's own scores untouched |
| `search.vec_fanout_max` | a chat filter with up to this many chats runs one KNN per chat (sqlite-vec partition key); above it one KNN over-fetches four times deeper and filters afterwards |
| `search.auto_sync_after_min` | the MCP `search` tool runs a sync first when the index is older than this many minutes; the CLI only prints a note |
| `search.auto_sync_budget_s` | the time cap of that automatic sync |
| `units.window_gap_min` | a pause longer than this closes the current window |
| `units.window_max_msgs`, `units.window_max_chars` | a window also closes at this many messages, or before the message that would take its rendered text past this many characters — a ceiling, so only a single message longer than the whole budget exceeds it |
| `units.thread_max_msgs` | a reply thread longer than this continues in further units, each repeating the root |
| `sync.edit_refetch` | how many of the newest messages of each chat are re-read to pick up edits and reaction counts — after every sync that finishes the chat's incremental pass (skipped on the chat's first sync and when the budget stops the chat); for a channel with `comments` the re-read posts whose reply count grew get their threads fetched again |
| `sync.flood_sleep_threshold` | Telethon sleeps through a `FloodWait` up to this many seconds; a longer one stops the run with a warning and the chats resume next time |
| `media.enabled` | master switch for the extraction pass: with it off, no media is downloaded and no text is read out of one |
| `media.ocr` | read text off photos with macOS Vision; needs the `media` extra and a Mac, and is simply unavailable elsewhere |
| `media.documents` | read text out of PDF and DOCX attachments; needs the `media` extra |
| `media.max_download_mb` | a file Telegram reports as larger than this is skipped without being downloaded |
| `research.enabled` | research (discovering chats you do not index yet, from a question and seed chats) is off until this is `true`; every research command and tool refuses while it is off, and ordinary search never widens what it reads |
| `research.chat_search` | let discovery ask Telegram's own chat search (`contacts.search`) for public chats by name; still needs a `global_search` approval in the session, whose text says the query leaves this machine |
| `research.post_search` | the same for Telegram's public-post search (`channels.searchPosts`), which has a small free daily quota |
| `research.paid_stars_max` | the most Telegram Stars one post search may spend once the free quota is gone; `0` never pays, and paying needs its own `paid_search` approval |
| `research.max_depth` | how many hops from a seed chat a candidate may be: leads in the seeds are depth 1, leads in a chat fetched from a depth-1 candidate depth 2 |
| `research.max_candidates` | the most candidates one discover call proposes |
| `research.max_session_candidates` | the most candidates one session holds in all, over every discover call and run; past it, discovery proposes nothing new and reports what the ceiling held back |
| `research.probe_limit` | how many usernames, invite links and shared-folder links one discover call looks up on Telegram (metadata only, no history) |
| `research.since_days` | how far back a source added by a research run fetches history |
| `research.max_messages_per_run`, `research.run_budget_s` | the message and time budget of one research run; a run stopped by either resumes next time |
| `research.admission_timeout_days` | how long a run keeps asking about an admission request no admin answered; after that the candidate is `failed` with a note, and a new approval may send the request again |
| (every research limit above) | a whole number from 1 to a ceiling far above any real session: 10 hops, 1,000 candidates or probes per discover call, 100,000 candidates per session, 36,500 days of history, 1,000,000 messages and 86,400 s per run, 3,650 days of admission wait. The overrides `research start` and `research_start` take are held to the same bounds |
| `accounts[].name` | an account signed in besides the implicit `default` one: 1 to 32 of `a-z`, `0-9`, `_` and `-`, unique, and never `default`; its session lives in `sessions/<name>.session` next to the config, while `default` keeps `session.session` |
| `accounts[].label` | optional free text describing the account, for your own reference |
| `sources[].folder` | a Telegram folder by name; its membership (included and pinned chats minus excluded ones, plus category flags) is re-resolved on every sync |
| `sources[].chat` | one chat: `@username`, `https://t.me/…` link or the id printed by `grepogram dialogs` (Telethon's marked form, `-100…` for channels and supergroups) |
| `sources[].account` | the account that fetches the source: `default` when omitted, otherwise a name listed under `[[accounts]]`. A source of another account has the id `<account>/chat:…` or `<account>/folder:…`, so two accounts can each list the same `chat` value |
| `sources[].since` | `YYYY-MM-DD`; history before this date is skipped on the first sync of the chat |
| `sources[].comments` | channels only: index the comment threads of the linked discussion group as well; on a folder source it applies to every channel in the folder. The comments are stored under the group, each naming the channel and the post it hangs under; a source that lists the group itself (the folder holding both, or a `chat` entry) indexes its whole history on top, and the two share one set of rows |

`GREPOGRAM_HOME=<dir>` puts every file (`config.toml`, `config.lock`, `session.session`,
`sessions/`, `index.db`, `research.db`, `sync.lock`, `logs/`) under one directory; the tests use it. `GREPOGRAM_FAKE_MODELS=1`
swaps both models for deterministic fakes (tests and CI only).

## How Search Works

**Units.** Single messages are too short to embed, and the answer to a question usually spans
several of them. The index therefore holds *units*: per chat (and per forum topic) the history is
cut into *windows* — chronological runs that end at a pause of more than `window_gap_min`
minutes, at `window_max_msgs` messages, or before the message that would take the window past
`window_max_chars` characters; every message that
got replies and has no parent in the chat becomes the root of a *thread* (root plus all
descendants, chronological, capped at `thread_max_msgs` with continuation units that repeat the
root); every channel message is a *post*, and with `comments = true` a thread of the post with its
comments. A discussion group indexed as a chat of its own is one linear conversation: its windows
run across comments and general talk alike, the way the group reads in Telegram, while the
channel's post threads give the per-post view. A unit's text is one line per message,
`[YYYY-MM-DD HH:MM] name: text`, with
`[photo]` / `[voice]` / `[document: name.pdf]` placeholders for media without a caption — and,
once [`grepogram extract`](#reading-text-out-of-media) has read a photo or an attachment, the
marker followed by the text found in the file, so what a screenshot said is searchable while
still reading as something a machine lifted off an image. A sync
re-cuts only the open window of each touched chat — or, when a message arrived below it that no
window holds yet (a channel storing a comment in its group ahead of the group's own history, a
late comment on an old post), the windows from the one before that message on — and rebuilds only
the threads reachable from new or edited messages; unchanged units keep their rows and their
vectors.

**Lexical.** Two FTS5 tables (`unicode61`, diacritics removed) hold a `raw` and a `stemmed` column
each — one for whole units, one for single messages so that a message packing every query term
into one line still stands out among long windows. Stemming is Snowball, chosen per token by
script: Cyrillic tokens go through the Russian stemmer (which also folds `ё` to `е`), Latin
tokens through the English one, everything else is kept as typed; nothing is dropped, so `ВНЖ`,
`DNI` and `2024` stay searchable. A query is stemmed the same way, every stem is quoted so that
`bge-m3` or `12:30` cannot break the FTS5 syntax, and the terms are joined with `AND` first, then
with `OR` when fewer rows than wanted match. Ranking is `bm25()` with the `raw` column weighted
twice the `stemmed` one. Message hits are mapped to the window or post that holds them.

**Dense.** Units are embedded with `bge-m3` (1024-d, normalised, fp16 on Metal, capped at
`models.max_seq_length` tokens) into a sqlite-vec `vec0` table partitioned by chat; the query is
embedded the same way and searched by cosine distance, with the date bounds as metadata
constraints. Vectors are keyed by the unit's rowid, so a re-cut window's old vector is deleted
with the unit and can never resurface. The model name and width are recorded in the index and
checked before every query.

**Unit length and the token cap.** `models.max_seq_length` (512 by default) is where both
models stop reading: everything past it in a unit is dropped before the text is encoded, by the
embedder and by the cross-encoder alike. The two share one key on purpose — they score the *same*
unit text, and a cap that let one of them see more of a unit than the other would have the two
stages rank different documents. (The reranker spends part of that budget on the query, so it sees
a little less of a long unit than the embedder does.)

`window_max_chars` is a **ceiling** on a unit's finished text: a window closes *before* the
message that would take it past 1500 characters, so a finished window never exceeds the cap. The
one exception is a single message longer than the whole budget, which forms a window of its own
rather than none. 1500 characters is about 470 tokens of this corpus, which is what 512 leaves
headroom for.

It was a floor until v0.2.0, tested *after* the window had already grown past the cap, so a closed
window held 1500 characters plus however much the message that carried it over brought with it.
Measured on a real 159,539-message index it ran to 3,453 characters at the top end against a
median of 1,274, and over 400 random windows tokenized with `BAAI/bge-m3`'s own tokenizer: median
384 tokens, p90 547, max 869, and **15.8% of windows longer than 512 tokens** — embedded and
reranked from their first 512 tokens only. Script is not what drove it, contrary to what you might
expect of a byte-hungry alphabet: bge-m3's SentencePiece vocabulary encodes Russian about as
compactly as English — 1500 characters of pure Cyrillic message text comes to a median 399 tokens
against 418 for pure Latin — so what reached the cap was unit *length*, in any language.

Making the cap real changes where windows are cut, so an index cut by an older build re-cuts and
re-embeds every unit once, chat by chat, over the syncs that follow — four whole chats a run, each
in one transaction, so search keeps answering throughout and `search` says so until it is through.

A truncated unit is not a lost unit. Its whole text is in the FTS tables, so lexical retrieval
matches on every word of it and the hit comes back complete — a query whose terms sit in the tail
still finds it through BM25, just not through the vector. What truncation costs is the dense
side's half of hybrid retrieval on those units, which is the half that catches a paraphrase. So if
you raise `window_max_chars` past what 512 tokens hold, raise `max_seq_length` with it.

That is paid for in time. On an Apple M1 Pro, fp16 on MPS, over 128 random real windows: embedding
runs at **12.6 units/s at 512** and **9.9 units/s at 1024** (about 21% slower), and reranking 40
`(query, unit)` pairs at **10.6 pairs/s at 512** against **6.1 at 1024** (about 42% slower — a
full `rerank_top = 40` goes from ~3.8 s to ~6.6 s, which a reader waits through on every search).
The shipped default stays 512, which the shipped `window_max_chars = 1500` now fits inside.

Nothing detects the change for you. The embedding-space guard (`index.ensure_embedding_space`)
compares two things: `meta.embed_model` against the configured model name, and the width the
`unit_vec` table declares against the model's dimension. Changing `max_seq_length` changes
neither — same model id, same 1024-d vectors — so the stored vectors stay in place and stay
short-read, and no warning is raised. **After changing `max_seq_length`, run `grepogram embed
--reembed`**, or the setting applies only to units embedded from then on, leaving the index in
two halves.

**Fusion, rerank, dedup.** The three lists — units by BM25, messages by BM25 mapped to their
units, units by cosine — are merged with Reciprocal Rank Fusion, `Σ 1 / (rrf_k + rank)`, which
needs no score calibration between tables: a unit near the top of two lists outranks one found
by a single list. The fused top `rerank_top` are re-scored by the cross-encoder on
`(query, unit text)` and re-sorted. Then near-duplicates go: a hit is dropped when at least
`dedup_overlap` of its own message ids already belong to a better hit of the same chat, so a
thread inside a window that ranks higher disappears while a window extending a better-ranked
thread survives. The top `k` survivors are returned. `mode` picks the retrieval lists; reranking
and dedup apply in every mode, including the lexical fallback.

**Reactions.** A chat usually agrees on the answer, and it says so by reacting to it. Every unit
carries the reactions its messages collected, and after the cross-encoder has scored the
candidates each one is raised by `reaction_weight * log1p(reactions) / (1 + log1p(reactions))` —
0.02 at one reaction, 0.035 at ten, never the full `reaction_weight` however many arrive. The
bonus is deliberately small: it settles a near-tie in favour of the message the chat agreed with
and cannot buy a popular unit past a relevant one. Setting `search.reaction_weight = 0`
reproduces the pre-v0.2.0 ordering exactly.

It is added on a *normalised* scale, and that is what makes one weight mean the same thing
everywhere: a cross-encoder returns raw logits spanning several units, so a fixed number added to
them would be a rounding error on one query and decisive on the next. The candidate scores are
therefore min-max normalised to `[0, 1]` across the result set before the bonus goes on. Two
consequences worth knowing: a hit's `score` — shown in the CLI and returned by the MCP tools — is
a **within-result-set number**, so it ranks the hits of one answer and says nothing across
queries; and when the reranker did not run at all (`--no-rerank`, or a model that cannot load)
neither the normalisation nor the bonus applies, because the fused RRF scores it would fall back
to top out near `1 / (rrf_k + 1)` ≈ 0.016, where this bonus would decide the whole ordering. A
result set of one, or one whose scores are all but identical, is left alone for the same reason.

**Degradation.** The dense side is used only when vectors exist and come from the configured
model. No vectors yet, the `dense` extra missing, a model that cannot load, a model change without
`--reembed` — each turns into a `dense search unavailable: …` warning and a lexical search. A
reranker that cannot load adds `reranking unavailable: …` and the fused order stands. No Metal
means CPU with a one-time warning in the log.

**Filters.** Chat specs are resolved against the indexed chats — an id, `@username`, a `t.me`
link, `folder:<name>` or `import:<slug>` (through the source that pulled the chats in, matched
exactly and then fuzzily on the name past the prefix), or free text matched against titles,
usernames and folder names (substring first, then a `SequenceMatcher` ratio of at least 0.6; all
hits of the best tier are searched). `account:<name>` selects every chat that account reaches,
and an `<account>/` prefix on any other spec keeps it to that account's private chats and the
shared channels and groups: one person's private chats with two accounts are two chats under one
Telegram id, so `thread 42 7` asks which one and `thread work/42 7` names it. An account is a
scope, not isolation — every account is the same local user's, and a channel two accounts reach
is one chat in both scopes. A spec that matches nothing is an error listing what is indexed. Date bounds apply to the unit's start time; `until` covers the whole day or month
named; relative ages (`6m`) step the calendar rather than counting 30-day months.

**Snippets and links.** Every hit has an *anchor*, the message its link opens: the matched message
for a message-level hit, otherwise the unit's best message under the query, else its first. The
snippet leads with the anchor's line and adds neighbours from within the unit up to 600
characters. A channel's post thread is anchored and linked on the post, but its snippet is cut
from the unit text and leads with the line — the post or one of its comments — that shares the
most word stems with the query, so a hit that owes its rank to a comment shows that comment.
Links follow Telegram's rules per chat type: `https://t.me/<username>/<msg>` for
public channels and supergroups, `https://t.me/c/<id>/<msg>` for private ones (with the topic
inserted for forums), `tg://openmessage?user_id=…&message_id=…` for private chats and bots with
`tg://user?id=…` as fallback, `tg://openmessage?chat_id=…&message_id=…` for legacy groups. They
are there to be shown and cited: terminals and MCP clients make a link clickable, and grepogram
never launches anything itself.

**Staying current.** `sync` re-resolves every source (folders change), then syncs chats in
`last_sync_at` order, never-synced first: new messages after the stored `last_msg_id` in batches
of 500, then a re-read of the newest `edit_refetch` messages that rewrites only rows whose content
changed (that re-read runs after every sync that finishes the chat's incremental pass, not on
its first sync or one the budget cut short). With `comments` on, only the posts Telegram reports
comments on cost a thread request; a thread that starts later is picked up by the re-read while
the post is among the newest. A budget stops the run cleanly between batches and the report
lists `chats_remaining`. Every stored message stays flagged until its units and its row in the
message index exist: a run indexes what it committed even when a flood wait, an error or a
cancelled tool call stops the fetch, and the next run picks up whatever a crash left behind, for
the chats it reaches and for the ones it does not. The rebuild, the index rows and the flag are
one transaction, and each run checks the units of the chats it indexes against the unit index, so
a run interrupted by an older version — or by a killed process — is repaired rather than marked
done, and no re-download is ever needed. A non-blocking file lock keeps two syncs off
the same index:
every command that writes the index — the CLI `sync`, `extract`, `embed`, `import`,
`sources rm`, `accounts rm`, `prune-deleted`, `recapture-links` and the deletion of
`sources prune`, the MCP `sync` and `sources_remove` — started while another sync runs fails at
once with `SyncInProgress`; a research run reports `stopped_by: sync_busy` rather than failing, and the MCP
`search` auto-sync turns it into a warning. Inside the MCP server, syncs queue instead: a `search` that finds the index stale while
another tool call is already syncing waits for it — for at most `auto_sync_budget_s` seconds —
and then searches the fresh index; a sync still running after that is reported as a warning and
the search runs on the index as it is. A sync resolves its sources from the config as it is once
it holds the lock, so a source removed while the sync was still loading its model or connecting
is not fetched again. Chats
Telegram refuses (left, kicked, private) are marked `unavailable`, retried on every sync, and
cleared when they succeed again; a legacy group that was upgraded to a supergroup is followed to
its new id. When the MCP `search` tool finds the last sync run older than `auto_sync_after_min`,
it first runs a sync capped at `auto_sync_budget_s` seconds (a flood wait is never slept through
for longer than the budget has left) and reports `synced: true`; anything that goes wrong with
that refresh — no session, a running sync, a flood wait — becomes a warning and the search runs
on the index as it is. A never-synced index is not refreshed automatically — the first `sync`
(CLI or tool) is explicit.

Only `grepogram auth` writes a session file — `session.session` for the default account,
`sessions/<name>.session` for the others. Every other client — each CLI command and each
Telegram-using MCP tool call, one per account it acts as — reads it into memory at start and
works on that copy: Telethon
writes to its session database on nearly every request and commits once a minute, so two clients
on one file would block each other for the SQLite busy timeout and then fail with `database is
locked`. A cron `grepogram sync` and the MCP server therefore never get in each other's way, and
a session created with `grepogram auth` while the server runs is picked up by its next call.

## Files and Privacy

| file | purpose | mode |
|---|---|---|
| `~/.config/grepogram/config.toml` | settings, API keys, sources | 0600 |
| `~/.config/grepogram/session.session` | Telethon session with the default account's auth key | 0600 |
| `~/.config/grepogram/sessions/<name>.session` | the session of every other account `grepogram auth --account` signed in (the directory is 0700) | 0600 |
| `~/Library/Application Support/grepogram/index.db` | messages with their links and forward origins, units, FTS and vector tables, and which accounts reach which chat (WAL) | — |
| `~/Library/Application Support/grepogram/research.db` | research sessions, candidates, evidence, approvals and exclusions — your decisions, kept apart from the rebuildable index | 0600 |
| `~/.config/grepogram/config.lock` | cross-process lock around every edit of `config.toml` | 0600 |
| `~/Library/Application Support/grepogram/sync.lock` | cross-process sync lock | 0600 |
| `~/Library/Logs/grepogram/grepogram.log` | log, rotated at 5 MB, three old files kept | — |
| `~/.cache/huggingface/hub/` | the two models, downloaded once | — |

Directories are created with mode 0700. A session file grants full access to its Telegram
account; treat it like a password and delete it (or terminate the session in Telegram's settings)
when you stop using grepogram — `grepogram accounts rm <name>` deletes an account's for you.

`grepogram extract` writes one more kind of file, and only while it runs: each photo or attachment
it reads is downloaded to a temporary file in the system scratch directory, handed to the
extractor, and deleted in a `finally` — whether the extraction succeeded, raised or the run was
interrupted. Nothing downloaded is kept; what survives the pass is the recognised *text*, in the
same `index.db` as everything else. A file Telegram reports as larger than `media.max_download_mb`
is never downloaded in the first place.

Two kinds of traffic leave the machine: MTProto requests to Telegram from your own accounts — the
same ones a client makes when you scroll a chat, plus one download per media file while `extract`
runs and, once research is enabled, its metadata probes and the joins and admission requests you
approved — and a single download per model from `huggingface.co` the first time the `dense` extra
needs one. A research question reaches Telegram only as a global search a session was approved
for, and the approval says so before it does. OCR is no exception to any of this: macOS Vision reads the image on the machine,
through a framework already installed on it, and sends nothing anywhere. Message text, extracted
text, embeddings, queries and results stay in the SQLite file and in the conversation with your
agent, both on your machine.
Embedding and reranking run locally, so a search costs a Telegram round trip at most; the language
model is whichever agent you connect, and grepogram itself needs only your Telegram credentials.
Logs keep message text below DEBUG level, where it is replaced by its length and a short digest.
grepogram starts no other process and launches no application: results carry links, and opening
one is the reader's own click.

## Local Model Throughput

Measured on an Apple M1 Pro (16 GB) with both models already in the Hugging Face cache
(`HF_HUB_OFFLINE=1`), fp16 on MPS, `max_seq_length = 512`, batch 32. The units are **real**: a
random sample of window units from a 159,539-message index, whose token lengths run to a median
of 384 and a p90 of 547. Each figure is the median of three timed rounds after a warm-up round:

| model | work | throughput |
|---|---|---|
| `BAAI/bge-m3` | embedding window units (128 real windows, batch 32) | 12.6 units/s |
| `BAAI/bge-reranker-v2-m3` | scoring `(query, unit)` pairs (`rerank_top = 40`) | 10.6 pairs/s |

Earlier versions of this table published 40.9 units/s and 37.7 pairs/s. Those were measured on
the short synthetic units of the test fixtures — six lines of about 60 characters — which run
roughly a third the token length of a real window, and they overstated both models by three to
four times. Encoder cost scales with sequence length, so measure on units the size you actually
index.

Loading takes about 8 s for the embedder and 3.5 s for the reranker, once per process. At these
rates a query has its 40 candidates reranked in about four seconds, and 10 000 units embed in
about thirteen minutes. Raising `max_seq_length` to 1024 costs about a fifth of the embedding
rate and two fifths of the reranking rate — see
[Unit length and the token cap](#how-search-works).

Loading reads the local cache and nothing else (see Requirements), which is worth about 8.7 s per
run on the same machine: a `grepogram search` that loads both models took 24.2 s while the hub was
reachable and takes 15.5 s now, matching what `HF_HUB_OFFLINE=1` already gave. Where outbound
connections are held open rather than refused — a firewall prompt nobody answers, a captive portal
— the same round trips cost minutes instead of seconds.

## Known Limitations

- Deep links into private chats and legacy groups use the `tg://openmessage` scheme, which
  Telegram's mobile apps honour; the desktop apps open the conversation through the
  `tg://user?id=` fallback but do not scroll to the message. Channel and supergroup links
  (`https://t.me/…`) work everywhere, though clicking one opens the t.me page in a browser unless
  the client hands `https://t.me` links to the Telegram app.
- The first sync of a large chat (hundreds of thousands of messages) takes a long time and may run
  into Telegram flood waits; a wait longer than `flood_sleep_threshold` stops the run and the next
  run continues, with everything the stopped run stored already searchable. Use `--since` on the
  source to cap history and `--budget` to bound a run; embedding runs at the rates above. A
  channel with `comments` adds one request per post that has a thread.
- A sync notices only the deletions among the newest `edit_refetch` messages of a chat. It re-reads
  those on every sync that finishes a chat's incremental pass, and a stored message the re-read
  covered but Telegram did not return is gone: its row goes, and the units holding it are cut
  again — a closed window included, and a unit left holding no messages is dropped rather than
  rebuilt empty. That is a set difference over the id range the re-read actually reached, so a
  stored id outside that range is never evidence of anything and never removed. Everything older
  needs `grepogram prune-deleted`, which asks Telegram about every stored message at about one
  request per hundred; it is deliberately a command you run, not something a sync does.
- Comments are the exception a sync makes to that. A deleted comment is left alone even in a
  discussion group indexed as a chat of its own, because removing it there would re-cut the
  group's own window while the channel's post thread kept the text for good — a post thread lists
  only the post in its message ids, so nothing reachable from them can invalidate it.
  `prune-deleted` follows the channel-and-post pair instead and is where a deleted comment is
  actually removed.
- Edits are still picked up only for the newest `edit_refetch` messages of a chat, and an edited
  message inside an already closed window keeps the old window text (its reply thread is rebuilt).
  Reaction totals are refreshed over the same window, directly on the units holding those
  messages, so a closed window's total moves without the window being re-cut. New comments on a
  channel post are picked up the same way — for the newest `edit_refetch` posts, when Telegram
  reports more replies than are stored; edited comments are not.
- Text is read out of media only when you run `grepogram extract`, never by a sync, and only for
  photos, PDFs and DOCX files. Voice messages and video notes are not transcribed — whisper.cpp is
  the v0.3.0 plan — and OCR needs macOS with the `media` extra, with Russian recognition needing
  macOS 15. Everything a build cannot read is parked rather than retried, and `--retry-failed`
  queues it again once that changes. A file over `media.max_download_mb` is skipped for good:
  raising the cap later does not queue it again.
- `grepogram import` reads the whole export file into memory at once — the decoded text, the
  parsed object graph, and, when the file was cut short, one more pass over that same text to
  recover it — so expect a few times the file's size in RAM. That is fine for the exports people
  actually have; a whole-account export of several hundred megabytes is one to split, exporting
  chat by chat from Telegram Desktop instead. An export that was cut short mid-write is still
  read, up to its last complete message, and the command says so.
- `since` on a source is that day's UTC midnight, and the bound is inclusive: a message stamped
  exactly at `00:00:00Z` is indexed. Telethon 1.44 hands `offset_date` to `messages.getHistory`
  untouched and filters nothing by date itself, so a `reverse=True` chunk is the complement of the
  server's exclusive "messages before this date" cut. (Telethon's docstring calls the reversed
  bound exclusive, but only the *id* offset is compensated to stay so.) This has not been confirmed
  against a real long chat; if a first sync pulls the wrong side of the date, please open an issue.
- With `comments = true` and no source listing the discussion group itself, the group holds only
  the comment threads of the channel's posts and is removed together with the channel; list the
  group (in the folder, or as a `chat` entry) to index its whole history as well. The same comments
  then appear twice in the index — in the channel's post threads and in the group's windows — so a
  question about a post may surface both.
- A channel that loses its discussion group, or is given another one, drops the old link on its
  next sync: the comments already stored stay in the index as what they are — the messages of that
  group — but they stop being shown as the channel's comments the moment the link goes. The post
  threads they fed are dropped with it, and the posts they hung under are cut again on the next
  rebuild. They stop naming a channel and a post at all: post numbers repeat across channels, so a
  group handed from one channel to another would otherwise show the old channel's comments under
  the new channel's post of the same number. The group's own units are untouched by that — a
  window is cut per forum topic and knows nothing of comments — so a comment that stops being one
  is searchable through the very window it was already in, with no sync in between. The old group
  keeps being synced only if a source of its own lists it. A
  group Telegram reports but this account cannot open (it went private, say) leaves the comments
  out of that run, and drops the stored link when it is not that same group.
- A discussion group that is also a forum keeps the two apart: a message's forum topic and the
  post it comments on are separate columns, because a topic root and a channel post are separate
  id spaces that both number from 1. Unlinking, handing the group over or deleting the channel
  therefore leaves the group's own topics exactly as they are.
- A discussion group belongs to the source that brought it in: the folder or `chat` entry listing
  it when one does, and otherwise the source of the channel that links it now — so a group handed
  from one channel to another moves to the new channel's source, and removing the channel it left
  keeps it while removing the new one takes its comments along. A group no channel links any more
  keeps the source it came in through until that source is removed. A `chat` entry counts as
  listing the group under every spelling the field takes — the id, the `@username` and a `t.me`
  link are the same chat.
- Removing the source of a discussion group takes its comments out of the channel that stored
  them: the post threads built from them go with the group, and the posts are rebuilt without
  them on the next sync. Removing the channel instead leaves the group whole — its own windows
  and threads are its own messages — and only drops the link between the two.
- A chat that came in through a folder cannot be removed on its own; remove the folder source, or
  take the chat out of the folder in Telegram and run `grepogram sources prune`, which is what
  clears the rows a folder no longer covers. Nor can a channel's discussion group be removed
  through the channel's source by naming the group; remove the channel's source.
- Messages stored before links and forward origins were captured carry neither: their hidden
  hyperlinks, URL buttons and link previews were never kept, so research reads only the URLs and
  `@mentions` visible in their text, and says how many messages it read that way. A sync re-reads
  the newest `edit_refetch` messages of each chat and stores what it finds there; for the older
  history, run `grepogram recapture-links`. A Telegram Desktop export keeps visible links,
  hidden hyperlinks and mentions but no buttons or link previews, and an imported chat cannot be
  re-read.
- A forward whose origin is a private channel the acting account holds no access to, and whose
  username Telegram did not hand over with the forward, is recorded as `unresolvable` and never
  guessed at; probing it would need the account to reach that channel first.
- Only one session at a time can be written: `grepogram auth` while another client has an
  uncommitted write open on the session file (a sync in another Telethon-based tool, say) can
  fail with `database is locked`; grepogram's own clients only read it.
- Every command and tool that edits `config.toml` — `sources add` / `rm`, `auth --account`,
  `accounts rm`, `research run`, and the MCP `sources_add`, `sources_remove` and
  `research_run` — rewrites it without its comments,
  so signing in a second account drops the annotated template; the template is the block in
  [Configuration](#configuration).
- The MCP contract targets the `mcp` 1.x SDK (`FastMCP`); 2.x renamed the API and is excluded by
  the dependency pin.

## Roadmap

- Voice and video-note transcription through whisper.cpp, joining the extraction registry that
  already carries the OCR and document extractors rather than becoming a second pipeline

## Development

```sh
uv sync --managed-python --all-extras --all-groups
uv run pytest                          # in-memory SQLite, fake models, no network
HF_HUB_OFFLINE=1 uv run pytest -m slow # real bge-m3 and reranker, once they are cached
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

[CONTRIBUTING.md](CONTRIBUTING.md) has the setup, the gates and the commit convention;
[CLAUDE.md](CLAUDE.md) has the invariants a change is reviewed against, written for coding
agents and equally the contributor guide.

## License

MIT, see [LICENSE](LICENSE).
