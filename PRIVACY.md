# Privacy

grepogram is a local tool. It indexes the Telegram chats you choose on your own machine and
answers searches over them for an agent such as Claude Code, through its CLI or its MCP server.
There is no grepogram server, account or telemetry: the project operates no service that
receives your data.

This document covers the `grepogram` CLI, the `grepogram-mcp` server and the Claude Code plugin
in `plugin/`.

## What it reads

- **Your opt-in sources only.** grepogram reads the chats, folders and channels you add with
  `grepogram sources add` (or import from a Telegram Desktop export with `grepogram import`),
  through your own Telegram session, using the Telegram app credentials (`api_id`, `api_hash`)
  you created at my.telegram.org. It does not read the message history of other chats.
- **Your chat list, to find a source.** `grepogram dialogs` reads the account's dialog list and
  folders from Telegram (chat names, usernames and types, not history) to match what you ask
  for, and `grepogram sources add` resolves the target you name.
- **Attachments, when media extraction is on.** `grepogram extract` downloads PDF, DOCX and
  photo attachments of stored messages to a scratch file, extracts the text (OCR runs locally on
  macOS Vision), stores the text and deletes the scratch file. It is a separate command, never
  part of a sync, and `[media]` in the config can switch it off or cap the download size.
- **Research mode is off by default** (`[research] enabled = false`). When you turn it on it can:
  - read links found in messages you already hold, with no network access;
  - probe a candidate chat's metadata (title, type, member count and similar) by username,
    invite link or shared-folder link, without reading its history;
  - join a chat, send an admission request, fetch its history or add it as a source, only for the
    candidate and the action you approved. An approval is a summary you are shown and confirm;
    a run takes exactly the route that summary named;
  - send a search phrase to Telegram's global search (public chats by name, public posts), only
    when `chat_search` / `post_search` is enabled in the config and you approved the search for
    that session. The phrase is the research session's question. A paid post search needs its own
    approval and a star ceiling in the config, and the default ceiling is zero.
- Nothing is sent to Telegram on your behalf as a message. Apart from the joins and admission
  requests a research run makes for what you approved, and the login session `grepogram auth`
  creates, the one command that changes your account on Telegram is `grepogram leave`, and it
  needs an explicit confirmation.

## What it writes

Everything lives in grepogram's own directories: three by default, one with `GREPOGRAM_HOME`.
By default the config and sessions are in `~/.config/grepogram`, the index and the sync lock in
`~/Library/Application Support/grepogram` and the logs in `~/Library/Logs/grepogram`. With
`GREPOGRAM_HOME=<dir>` all of it goes under `<dir>`. `grepogram config path` prints the actual
locations.

| File | Contents |
| --- | --- |
| `config.toml` | settings, your Telegram `api_id` / `api_hash`, the source list; mode 0600 |
| `session.session`, `sessions/<name>.session` | Telegram login sessions; mode 0600 |
| `index.db` | stored messages (text, sender names, chat titles and usernames, links, extracted attachment text), the full-text index and the embedding vectors |
| `research.db` | research sessions, candidate chats, evidence and your approvals and exclusions; mode 0600 |
| `config.lock`, `sync.lock` | lock files, empty; mode 0600 |
| `grepogram.log` (in `~/Library/Logs/grepogram`, or `logs/` under `GREPOGRAM_HOME`) | operational log, rotated at 5 MB with three backups |

Directories grepogram creates get mode 0700. A directory that already exists, such as your own
`GREPOGRAM_HOME`, keeps its mode. `index.db` holds your chat content in plain text on disk, so
protect the account and disk it lives on accordingly.

Message text does not appear in the log above DEBUG level, and a research invite link found in a
message is never logged above DEBUG. At DEBUG the log can hold more, so share a debug log with
care.

With the `dense` extra, the embedding and rerank models (`BAAI/bge-m3` and
`BAAI/bge-reranker-v2-m3` by default) are cached by Hugging Face in its own cache directory,
outside grepogram's tree.

The plugin itself stores nothing. Its only active component is a hook that asks for a permission
prompt before a consent confirmation command runs; it makes no network request and writes no file.
`/grepogram:setup` runs other tools' commands only after you agree to each at its permission
prompt: `uv tool install` writes the tool into uv's tool directory, `uv tool update-shell` edits
your shell profile, and `claude mcp add` (offered once, default no) edits Claude Code's user
configuration.

## Network destinations

1. **Telegram**, over MTProto through the Telethon library, with your own session and app
   credentials: syncing your sources, resolving the targets you add, research probes, joins and
   searches you approved, attachment downloads, and `grepogram leave`.
2. **huggingface.co**, only to download the embedding and rerank models the first time they are
   needed and are not in the local cache. `huggingface_hub` sends its own request headers, such
   as a user agent, which grepogram does not control; set `HF_HUB_OFFLINE=1` to forbid the
   network entirely, and search then degrades instead of downloading. After the first download
   everything runs locally.
3. **PyPI and uv's Python downloads**, only when you install or upgrade the tool: that is uv's
   own traffic (`uv tool install --managed-python` may also download a Python build), and
   `/grepogram:setup` runs it only under a permission prompt.

grepogram itself sends no telemetry, analytics or crash reports. Embedding, ranking and OCR run
on your machine.

## What reaches Claude

When Claude Code calls a grepogram tool or runs a grepogram command, the result enters your
Claude conversation. A search or reader returns content from your index: message text and
snippets, sender names, chat titles and usernames, message links and dates. Other commands return
more than the index holds:

- `grepogram dialogs` lists matching chats and folders from the account's whole chat list
  (names, usernames and types), indexed or not;
- `grepogram accounts ls` prints each signed-in account's display name and Telegram user id;
- `grepogram research candidates` and `status` return the titles and member counts of chats you
  do not index, and evidence snippets, including global-search results from chats you know
  nothing about;
- `grepogram config path` prints local file paths.

This is personal data of the people in your chats, not only yours. It leaves your machine only as
part of that conversation, under your agreement with Anthropic, and only for what a command
returned. grepogram does not push the index anywhere. The tools search and read; they do not send
messages.

The plugin's skills pre-allow only the commands that change nothing on your account or in your
config: `search`, `thread`, `context`, `sync`, `dialogs`, `sources ls`, `accounts ls`,
`config path` and the research steps that find, list or set aside candidates (`start`, `discover`,
`candidates`, `status`, `skip`, `stop`). `sync`, `dialogs` and an online `discover` reach
Telegram. Anything that changes the config, the install, an account or an approval keeps the
normal permission prompt, and `research approve`, `run` and `exclude` are never pre-allowed. The
hook adds a prompt on the confirming `--confirm` calls; it reads the command text, so it is
defence in depth, not a sandbox.

Your Telegram `api_id`, `api_hash`, login code and 2FA password never go to Claude:

- `/grepogram:setup` does not read `config.toml` into the conversation. It only counts matching
  key lines with `grep -c`, which prints a number, and it never prints a key value.
- `grepogram auth` is run by you in your own terminal, so the code and password are typed there
  and never into the chat.

If you do not want chat content in a conversation, do not run searches from it.

## Retention and deletion

grepogram keeps what it has indexed until you delete it; there is no automatic expiry.

- `grepogram sources rm <source>` removes a source and deletes the messages and index data of
  the chats no other source covers.
- `grepogram sources prune` and `grepogram prune-deleted` delete indexed chats a folder no longer
  lists and messages deleted on Telegram.
- `grepogram accounts rm <name>` removes an account, its sources, the chats only they cover, its
  session file and stops its research sessions. Nothing changes on Telegram. To revoke the login
  itself, end that session in Telegram under Settings, Devices.
- To remove everything, delete the directories holding the files `grepogram config path` prints,
  or your `GREPOGRAM_HOME` directory. Uninstall the tool with `uv tool uninstall grepogram`. The
  Hugging Face model cache is separate; delete it from that cache's directory if you want the
  space back.

## Contact

Questions and reports: https://github.com/nnemirovsky/grepogram/issues
