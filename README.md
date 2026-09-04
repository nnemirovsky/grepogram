# grepogram

grepogram is a local search engine over the Telegram chats you opt in to, exposed to Claude Code
as an MCP server (plus a CLI). It syncs messages through the Telegram user API (Telethon), stores
them in one SQLite file, groups them into conversation-sized units (time windows, reply threads,
channel posts), indexes those units lexically (FTS5 with Russian and English stemming) and densely
(sqlite-vec with `bge-m3` vectors computed on the local GPU), fuses both rankings with Reciprocal
Rank Fusion, reranks with a local cross-encoder, and returns hits with deep links that open the
original message in Telegram.

Claude Code is the language model. grepogram has no API token, no hosted service and no telemetry;
the embedding and reranking models run on your machine.

## The problem

Telegram's built-in search matches exact words: `счёт` does not find `счета`, `bank` does not
find `banks`, and a paraphrase finds nothing. Community chats — expat groups, city chats, hobby
groups — hold answers that no web page has ("how do I open a bank account here without a DNI",
"which SIM works in the mountains", "is the visa run still possible after the June change"), but
those answers sit in reply chains, spread over several messages, in two languages, and they go
stale. Vector search over single messages does not fix this: a single message is too short to
embed, and the answer usually hinges on an exact token (a bank name, `ВНЖ`, `CUIT`).

grepogram indexes conversations rather than messages, combines exact-token search with semantic
search, filters by date, and hands Claude a playbook for using the tools: run several query
variants, read the thread before concluding, cite a link per claim.

## How it works

```
 Telegram (your account, via Telethon)
     │  sync: new messages after last_msg_id, re-read of the newest edits
     ▼
 messages ──▶ units: windows · threads · posts
 (SQLite)         │
                  ├──▶ msg_fts, unit_fts   FTS5, raw + stemmed text (Snowball ru/en)
                  └──▶ unit_vec            sqlite-vec, bge-m3 vectors (local GPU)
                                │
 query ─▶ filters ─▶ BM25 lists + KNN list ─▶ RRF ─▶ cross-encoder rerank ─▶ dedup ─▶ hits + links
                                │
      Claude Code ◀── MCP over stdio ── grepogram-mcp         grepogram CLI
```

## Requirements

- macOS. On Apple Silicon the models run on Metal (`mps`), otherwise on the CPU; `open_message`
  uses `open`, and the default paths follow macOS conventions. The code runs elsewhere with
  `GREPOGRAM_HOME` set, but that is untested.
- [uv](https://docs.astral.sh/uv/). The project pins CPython 3.12 (`torch` and `sqlite-vec`
  wheels); uv installs it. The interpreter's `sqlite3` module must be able to load extensions —
  uv-managed and Homebrew builds can, Apple's system Python cannot.
- A Telegram account and an API key pair from https://my.telegram.org/apps.
- Optional: about 4.5 GB of disk for the two models (`BAAI/bge-m3`, `BAAI/bge-reranker-v2-m3`),
  downloaded from Hugging Face on first use. Without them every search runs lexical-only.

## Setup in five minutes

1. Create an application at https://my.telegram.org/apps and note the `api_id` and `api_hash`.

2. Install. Either as a tool, from a checkout:

   ```sh
   git clone <this repository> grepogram
   cd grepogram
   uv tool install '.[dense]'     # or `uv tool install .` for lexical-only search
   ```

   which puts `grepogram` and `grepogram-mcp` into `$(uv tool dir --bin)`, or from the checkout
   without installing:

   ```sh
   uv sync --extra dense          # or plain `uv sync`
   uv run grepogram --help        # prefix every command below with `uv run`
   ```

3. Write the config and fill in the keys:

   ```sh
   grepogram config init
   grepogram config path          # prints where config, session, index and log live
   ```

   Edit `~/.config/grepogram/config.toml` and set `[telegram] api_id` and `api_hash`.

4. Sign in. The session is stored as `~/.config/grepogram/session.session` with mode 0600:

   ```sh
   grepogram auth                 # phone number, login code, 2FA password if enabled
   ```

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

7. Register the MCP server in Claude Code. With a checkout at `<path>`:

   ```sh
   claude mcp add grepogram -s user -- uv run --project <path> grepogram-mcp
   ```

   or, after `uv tool install`:

   ```sh
   claude mcp add grepogram -s user -- "$(uv tool dir --bin)/grepogram-mcp"
   ```

   Start `claude`, check `/mcp` shows `grepogram` connected, and ask something like "what do
   people in the Argentina chat say about opening a bank account without a DNI?". The first
   search downloads the reranker if it is not cached yet.

Run `grepogram sync` whenever you want the index current; the MCP `search` tool also refreshes an
index older than an hour on its own (see below). A `launchd` job or a cron entry calling
`grepogram sync --budget 300` works fine; only one sync runs at a time.

## CLI reference

Global options: `--version`, `--verbose` / `-v` (DEBUG logging). Command output goes to stdout,
diagnostics and logs to stderr and the log file.

| command | what it does |
|---|---|
| `grepogram config init` | write the annotated config template; refuses to overwrite |
| `grepogram config path` | print the resolved config, session, index, lock and log paths |
| `grepogram auth` | sign in (phone, code, optional 2FA password) and store the session |
| `grepogram dialogs <query> [-n N]` | find chats and folders of the account whose title, `@username` or folder name matches; prints kind, id, type, title, username, folders, score |
| `grepogram sources add <target> [--since YYYY-MM-DD] [--comments]` | add a source and save the config; `target` is a chat id, `@username`, `t.me` link, `folder:<name>` or a fuzzy chat / folder title |
| `grepogram sources ls` | configured sources with their chats, message counts and last sync |
| `grepogram sources rm <target>` | remove a source and delete its chats' messages and index rows |
| `grepogram sync [--budget S]` | fetch new messages from every source, rebuild units, index and embed; stops cleanly after `S` seconds |
| `grepogram embed [--reembed]` | embed units the dense index does not hold yet; `--reembed` drops every vector and starts over (needed after changing `[models] embed`) |
| `grepogram search <query> …` | search the index, see below |
| `grepogram-mcp [-v]` | the MCP server over stdio (what Claude Code launches) |

Chat ids are negative for groups, supergroups and channels (`-100…`); when one is a positional
argument, put `--` before it: `grepogram sources add --since 2024-01-01 -- -1001234567890`.

`search` options:

| option | meaning |
|---|---|
| `-c`, `--chat <spec>` | restrict to these chats (repeatable): id, `@username`, `t.me` link, `folder:<name>` or a title / folder name (substring, then fuzzy) |
| `--since <when>`, `--until <when>` | date bounds on the unit's start: an ISO date (`2025-06-01`), month (`2025-06`), datetime (`2025-06-01T14:30`, optional seconds and `Z` / `+03:00`) or an age (`7d`, `3w`, `6m`, `1y`); `--until` is inclusive; naive input is UTC |
| `--mode hybrid\|lexical\|dense` | `hybrid` (default) fuses BM25 and embeddings, `lexical` is BM25 over stems only, `dense` is embeddings only; without vectors or a model every mode falls back to lexical with a warning |
| `-k`, `--limit N` | number of hits (default `[search] k`) |
| `--rerank` / `--no-rerank` | re-score the candidates with the cross-encoder (default on; skipped with a warning when the model cannot load) |
| `--full` | include each hit's whole unit text |
| `--json` | print the result document as JSON and nothing else on stdout |

Text output prints one block per hit — rank, score, unit kind, chat, UTC date range, the deep link
(and a fallback link for private chats), then the snippet.

## MCP tools

The server is named `grepogram`. Every tool returns one JSON object. Expected failures — no
session, another sync running, an unknown chat or message, a model that cannot load, an
ambiguous target — come back as `{"error": …, "hint": …}` (plus `candidates` when there is
something to choose from) rather than a tool error, so Claude can act on them. `warnings` are
advisory; the data next to them is valid.

| tool | arguments | returns |
|---|---|---|
| `search` | `query`, `chats: list[str] \| null`, `since`, `until`, `k=10`, `mode="hybrid"`, `rerank=true`, `full=false` | `{hits, warnings, index_age_min, synced}`; each hit has `score`, `chat` (id, type, title, username, …), `kind` (`window` / `thread` / `post`), `date_start`, `date_end` (unix seconds, UTC), `anchor_msg_id`, `url`, `fallback_url`, `snippet`, `msg_ids`, `text` (with `full`) |
| `thread` | `chat_id`, `msg_id` | `{chat_id, msg_id, messages}`: the whole reply thread the message belongs to, root first; for a channel post, the post followed by its comments |
| `context` | `chat_id`, `msg_id`, `before=15`, `after=15` | `{chat_id, msg_id, messages}`: the surrounding messages in the same chat or forum topic |
| `sync` | `budget_s=45` | the sync report: `new`, `chats_done`, `chats_remaining`, `unavailable`, `warnings`, `index_age_min` |
| `sources` | — | `{sources, index_age_min}`: every configured source with its chats (`id`, `title`, `type`, `username`, `message_count`, `last_sync_at`, `unavailable`) |
| `dialogs` | `query` | `{query, matches}`: chats and folders of the account matching the name; each match carries `kind`, `id`, `title`, `type`, `username`, `folders`, `score` and `target`, the string to pass to `sources_add` |
| `sources_add` | `target`, `since=null`, `comments=false` | `{source, kind, title, chats, hint}` after saving the config |
| `sources_remove` | `target` | `{source_id, removed_chat_ids, config_updated}` after deleting the chats' data |
| `open_message` | `chat_id`, `msg_id` | `{chat_id, msg_id, url, fallback_url, opened}`; opens the message in the Telegram app through `open`; when that fails the result still carries the urls plus `error` and `hint` |

Messages in `thread` and `context` have `msg_id`, `date`, `from_name`, `text` (a `[photo]`-style
placeholder for media without a caption), `url`, `fallback_url` and `reply_to_msg_id`.

The server's `instructions` tell Claude how to use the tools: run two or three query variants
(Russian and English, the specific term and the concept, synonyms), prefer `lexical` for exact
tokens such as bank names or IDs, prefer recent hits for anything regulatory or price-related and
state the date of the evidence, call `thread` or `context` before concluding from a snippet, cite
the hit's `url` per claim, say so when nothing relevant comes back, and use `sources` / `dialogs`
/ `sources_add` / `sync` when the user names a chat that is not indexed yet.

The server re-reads `config.toml` when the file changes, so a source added with the CLI while
Claude Code runs is picked up by the next tool call. Models are loaded once per server process; a
model that fails to load is not retried until the server restarts.

## Configuration

`grepogram config init` writes this file to `~/.config/grepogram/config.toml` (mode 0600). Every
key is optional; the values shown are the defaults. `grepogram sources add` / `rm` rewrite the
file without comments.

```toml
[telegram]
api_id = 0                             # create an app at https://my.telegram.org/apps
api_hash = ""

[models]
embed = "BAAI/bge-m3"                  # sentence-transformers id; change → full re-embed
rerank = "BAAI/bge-reranker-v2-m3"
device = "auto"                        # auto → mps if available else cpu

[search]
k = 10
rrf_k = 60
rerank_top = 40
dedup_overlap = 0.5
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

# Sources are opt-in. Add them with `grepogram sources add <target>` or by hand:
#
# [[sources]]
# folder = "Argentina"
#
# [[sources]]
# chat = "@ru_georgia"                 # or "https://t.me/…" or 123456789
# since = "2024-01-01"                 # optional: skip older history on first sync
# comments = false                     # channels only: also index linked discussion threads
```

| key | meaning |
|---|---|
| `telegram.api_id`, `telegram.api_hash` | the application credentials from my.telegram.org; every command that talks to Telegram refuses to run while they are unset |
| `models.embed` | sentence-transformers model for the dense index; the model name and vector width are recorded in the index, and a change is refused until `grepogram embed --reembed` |
| `models.rerank` | sentence-transformers cross-encoder used when `rerank` is on |
| `models.device` | `auto`, `mps` or `cpu`; on `mps` the weights run in fp16 |
| `search.k` | default number of hits for the CLI |
| `search.rrf_k` | the constant in `1 / (rrf_k + rank)` |
| `search.rerank_top` | how many fused candidates the cross-encoder re-scores; each retrieval list is fetched `max(k, rerank_top)` deep |
| `search.dedup_overlap` | drop a hit when at least this share of its message ids already belongs to a better hit of the same chat; a value above 1.0 disables dedup |
| `search.vec_fanout_max` | a chat filter with up to this many chats runs one KNN per chat (sqlite-vec partition key); above it one KNN over-fetches four times deeper and filters afterwards |
| `search.auto_sync_after_min` | the MCP `search` tool runs a sync first when the index is older than this many minutes; the CLI only prints a note |
| `search.auto_sync_budget_s` | the time cap of that automatic sync |
| `units.window_gap_min` | a pause longer than this closes the current window |
| `units.window_max_msgs`, `units.window_max_chars` | a window also closes at this many messages or this many characters of rendered text |
| `units.thread_max_msgs` | a reply thread longer than this continues in further units, each repeating the root |
| `sync.edit_refetch` | how many of the newest messages of each chat are re-read on every sync to pick up edits and reaction counts |
| `sync.flood_sleep_threshold` | Telethon sleeps through a `FloodWait` up to this many seconds; a longer one stops the run with a warning and the chats resume next time |
| `sources[].folder` | a Telegram folder by name; its membership (included and pinned chats minus excluded ones, plus category flags) is re-resolved on every sync |
| `sources[].chat` | one chat: `@username`, `https://t.me/…` link or the id printed by `grepogram dialogs` (Telethon's marked form, `-100…` for channels and supergroups) |
| `sources[].since` | `YYYY-MM-DD`; history before this date is skipped on the first sync of the chat |
| `sources[].comments` | channels only: index the comment threads of the linked discussion group as well; only honoured on `chat` sources, not on channels that arrive through a folder |

`GREPOGRAM_HOME=<dir>` puts every file (`config.toml`, `session.session`, `index.db`,
`sync.lock`, `logs/`) under one directory; the tests use it. `GREPOGRAM_FAKE_MODELS=1` swaps both
models for deterministic fakes (tests and CI only).

## How search works

**Units.** Single messages are too short to embed, and the answer to a question usually spans
several of them. The index therefore holds *units*: per chat (and per forum topic) the history is
cut into *windows* — chronological runs that end at a pause of more than `window_gap_min`
minutes, at `window_max_msgs` messages or at `window_max_chars` characters; every message that
got replies and has no parent in the chat becomes the root of a *thread* (root plus all
descendants, chronological, capped at `thread_max_msgs` with continuation units that repeat the
root); every channel message is a *post*, and with `comments = true` a thread of the post with its
comments. A unit's text is one line per message, `[YYYY-MM-DD HH:MM] name: text`, with
`[photo]` / `[voice]` / `[document: name.pdf]` placeholders for media without a caption. A sync
re-cuts only the open window of each touched chat and rebuilds only the threads reachable from
new or edited messages; unchanged units keep their rows and their vectors.

**Lexical.** Two FTS5 tables (`unicode61`, diacritics removed) hold a `raw` and a `stemmed` column
each — one for whole units, one for single messages so that a message packing every query term
into one line still stands out among long windows. Stemming is Snowball, chosen per token by
script: Cyrillic tokens go through the Russian stemmer (which also folds `ё` to `е`), Latin
tokens through the English one, everything else is kept as typed; nothing is dropped, so `ВНЖ`,
`DNI` and `2024` stay searchable. A query is stemmed the same way, every stem is quoted so that
`bge-m3` or `12:30` cannot break the FTS5 syntax, and the terms are joined with `AND` first, then
with `OR` when fewer rows than wanted match. Ranking is `bm25()` with the `raw` column weighted
twice the `stemmed` one. Message hits are mapped to the window or post that holds them.

**Dense.** Units are embedded with `bge-m3` (1024-d, normalised, 512-token cap, fp16 on Metal)
into a sqlite-vec `vec0` table partitioned by chat; the query is embedded the same way and
searched by cosine distance, with the date bounds as metadata constraints. Vectors are keyed by
the unit's rowid, so a re-cut window's old vector is deleted with the unit and can never resurface.
The model name and width are recorded in the index and checked before every query.

**Fusion, rerank, dedup.** The three lists — units by BM25, messages by BM25 mapped to their
units, units by cosine — are merged with Reciprocal Rank Fusion, `Σ 1 / (rrf_k + rank)`, which
needs no score calibration between tables: a unit near the top of two lists outranks one found
by a single list. The fused top `rerank_top` are re-scored by the cross-encoder on
`(query, unit text)` and re-sorted. Then near-duplicates go: a hit is dropped when at least
`dedup_overlap` of its own message ids already belong to a better hit of the same chat, so a
thread inside a window that ranks higher disappears while a window extending a better-ranked
thread survives. The top `k` survivors are returned. `mode` picks the retrieval lists; reranking
and dedup apply in every mode, including the lexical fallback.

**Degradation.** The dense side is used only when vectors exist and come from the configured
model. No vectors yet, the `dense` extra missing, a model that cannot load, a model change without
`--reembed` — each turns into a `dense search unavailable: …` warning and a lexical search. A
reranker that cannot load adds `reranking unavailable: …` and the fused order stands. No Metal
means CPU with a one-time warning in the log.

**Filters.** Chat specs are resolved against the indexed chats — an id, `@username`, a `t.me`
link, `folder:<name>` (through the source that pulled the chats in), or free text matched against
titles, usernames and folder names (substring first, then a `SequenceMatcher` ratio of at least
0.6; all hits of the best tier are searched). A spec that matches nothing is an error listing what
is indexed. Date bounds apply to the unit's start time; `until` covers the whole day or month
named; relative ages (`6m`) step the calendar rather than counting 30-day months.

**Snippets and links.** Every hit has an *anchor*, the message its link opens: the matched message
for a message-level hit, otherwise the unit's best message under the query, else its first. The
snippet leads with the anchor's line and adds neighbours from within the unit up to 600
characters. Links follow Telegram's rules per chat type: `https://t.me/<username>/<msg>` for
public channels and supergroups, `https://t.me/c/<id>/<msg>` for private ones (with the topic
inserted for forums), `tg://openmessage?user_id=…&message_id=…` for private chats and bots with
`tg://user?id=…` as fallback, `tg://openmessage?chat_id=…&message_id=…` for legacy groups.

**Staying current.** `sync` re-resolves every source (folders change), then syncs chats in
`last_sync_at` order, never-synced first: new messages after the stored `last_msg_id` in batches
of 500, then a re-read of the newest `edit_refetch` messages that rewrites only rows whose content
changed. A budget stops the run cleanly between batches and the report lists `chats_remaining`.
A file lock makes concurrent syncs from the CLI and the MCP server wait for each other
(`SyncInProgress` when one is running). Chats Telegram refuses (left, kicked, private) are marked
`unavailable`, retried on every sync, and cleared when they succeed again; a legacy group that
was upgraded to a supergroup is followed to its new id. When the MCP `search` tool finds the
index older than `auto_sync_after_min`, it first runs a sync capped at `auto_sync_budget_s`
seconds and reports `synced: true`; anything that goes wrong with that refresh — no session, a
running sync, a flood wait — becomes a warning and the search runs on the index as it is.

## Files and privacy

| file | purpose | mode |
|---|---|---|
| `~/.config/grepogram/config.toml` | settings, API keys, sources | 0600 |
| `~/.config/grepogram/session.session` | Telethon session with the account's auth key | 0600 |
| `~/Library/Application Support/grepogram/index.db` | messages, units, FTS and vector tables (WAL) | — |
| `~/Library/Application Support/grepogram/sync.lock` | cross-process sync lock | 0600 |
| `~/Library/Logs/grepogram/grepogram.log` | log, rotated at 5 MB, three old files kept | — |
| `~/.cache/huggingface/hub/` | the two models, downloaded once | — |

Directories are created with mode 0700. The session file grants full access to the Telegram
account; treat it like a password and delete it (or terminate the session in Telegram's settings)
when you stop using grepogram.

What leaves the machine: MTProto traffic to Telegram from your own account (the same requests a
client makes when you scroll a chat), and one download per model from `huggingface.co` when the
`dense` extra is installed. Nothing else. Message text, embeddings, queries and results stay in
the SQLite file and in the conversation with Claude Code on your machine; the log never contains
message text above DEBUG level (text is replaced with its length and a short digest). There is no
API key for any language model in the project: the model is whatever runs Claude Code.

## Local model throughput

Measured with `uv run pytest -m slow` on an Apple M1 Pro (16 GB) with both models already in
the Hugging Face cache (`HF_HUB_OFFLINE=1`), fp16 on MPS, `max_seq_length = 512`:

| model | work | throughput |
|---|---|---|
| `BAAI/bge-m3` | embedding window-sized units (64 units of six lines, batch 32) | 40.9 units/s |
| `BAAI/bge-reranker-v2-m3` | scoring `(query, unit)` pairs (`rerank_top = 40`) | 37.7 pairs/s |

Loading takes about 8 s for the embedder and 3.5 s for the reranker, once per process. At these
rates a query has its 40 candidates reranked in about a second, and 10 000 units embed in about
four minutes.

## Known limitations

- Deep links into private chats and legacy groups use the `tg://openmessage` scheme, which
  Telegram's mobile apps honour; the desktop apps open the conversation through the
  `tg://user?id=` fallback but do not scroll to the message. Channel and supergroup links
  (`https://t.me/…`) work everywhere.
- The first sync of a large chat (hundreds of thousands of messages) takes a long time and may run
  into Telegram flood waits; a wait longer than `flood_sleep_threshold` stops the run and the next
  run continues. Use `--since` on the source to cap history and `--budget` to bound a run; embedding
  runs at the rates above.
- Deleted messages are not removed from the index; they disappear when their source is removed.
  Edits are picked up only for the newest `edit_refetch` messages of a chat, and an edited message
  inside an already closed window keeps the old window text (its reply thread is rebuilt).
- `since` on a source relies on Telethon's `offset_date` under `reverse=True` meaning "after this
  date". This is what Telethon documents and what the test double implements; it has not yet been
  confirmed against a real long chat. If a first sync pulls the wrong side of the date, please open
  an issue.
- `comments = true` is honoured for `chat` sources only; channels that arrive through a folder get
  their posts indexed without the discussion threads.
- A chat that came in through a folder cannot be removed on its own; remove the folder source or
  take the chat out of the folder in Telegram.
- `sources add` / `rm` and the MCP tools rewrite `config.toml` without its comments.
- The MCP contract targets the `mcp` 1.x SDK (`FastMCP`); 2.x renamed the API and is excluded by
  the dependency pin.

## Roadmap

- `sources prune` for chats that left a folder
- OCR of photos through macOS Vision, feeding the unit text
- Voice and video-note transcription (whisper.cpp)
- Text extraction from PDF and DOCX attachments
- `grepogram import <tdesktop export dir>` for chats no longer accessible from the account
- Handling of deleted messages; reaction counts as a ranking signal
- Publishing to PyPI

## Development

```sh
uv sync --all-extras --all-groups
uv run pytest                          # in-memory SQLite, fake models, no network
HF_HUB_OFFLINE=1 uv run pytest -m slow # real bge-m3 and reranker, once they are cached
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Conventions for contributors and coding agents are in [CLAUDE.md](CLAUDE.md).

## License

MIT, see [LICENSE](LICENSE).
