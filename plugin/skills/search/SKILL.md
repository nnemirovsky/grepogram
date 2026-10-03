---
name: search
description: This skill should be used when the user asks to find, recall or check something that was said in their Telegram chats, channels or folders, for example "find in Telegram", "search my chats", "what did they say in the chat about ...", "who recommended ...", "найди в телеграме", "поищи в чатах", "что писали в чате про ...", or names a chat or folder to look through. Searches the local grepogram index (hybrid BM25 plus embeddings, Russian and English) through the grepogram CLI and reads threads and context around the hits.
allowed-tools: Bash(grepogram --version:*), Bash(grepogram search:*), Bash(grepogram thread:*), Bash(grepogram context:*), Bash(grepogram sync:*), Bash(grepogram sources ls:*), Bash(grepogram dialogs:*), Bash(grepogram accounts ls:*)
---

# Searching the user's Telegram chats with grepogram

Search runs offline over the chats the user opted in to. Drive it through the `grepogram` CLI with Bash and read the `--json` output.

## Preflight

This skill needs grepogram >= 0.3.0. On the first search of a conversation run `grepogram --version`.

- Command not found: tell the user to run `/grepogram:setup`, then stop.
- Version below the floor: tell the user to run `uv tool upgrade grepogram` (or `/grepogram:setup`), then stop.

## Search

Run `grepogram search --json "<query>"`. Useful options, all before the query:

- `--mode lexical` for exact tokens: bank names, ids, prices. The default is `hybrid`.
- `--since 6m` (also `2025-06`, `2025-06-01`, `7d`, `3w`, `1y`) and `--until` to bound the dates.
- `--chat <spec>` to limit chats (repeatable): an id, `@username`, `folder:<name>`, `account:<name>` or a title.
- `--account <name>` to limit to what one signed-in account reaches (repeatable). It is a scope, not isolation.
- `-k 20` for more hits, `--full` for each hit's whole unit text, `--no-rerank` to skip the cross-encoder.

Example: `grepogram search --json --since 1y --mode lexical "Wise"`.

Run 2-3 variants per question: Russian and English, the specific term and the concept, synonyms (for example "ВНЖ", "residence permit", "residencia"). Compare the hits across variants before answering.

## Freshness

The first `search --json` of a conversation returns `index_age_min`, minutes since the last sync.

- Above 60 (the default of the configurable `[search] auto_sync_after_min`): run `grepogram sync --budget 60` once, then search again.
- `null`: read `warnings`. No sources configured: point the user to `/grepogram:setup`. Nothing synced yet: run one `grepogram sync --budget 60` or point to setup.
- Sync at most once per conversation. If the sync errors because another one is running, say so and search the index as it is.
- `warnings` are advisory; the hits next to them are valid.

## Reading the hits

Each hit carries `url`, `snippet`, `chat` (with `id` and `title`), `anchor_msg_id`, `date_start`, `date_end` (unix seconds, UTC) and `accounts`.

- Chat knowledge is time-sensitive. Prefer recent hits for anything regulatory, procedural or about prices, filter with `--since` when it matters, and state the date of the evidence. Convert `date_start` and `date_end` to calendar dates, never print raw seconds.
- Read a hit before drawing a conclusion from its snippet; the answer usually sits in the replies. Run `grepogram thread --json -- <chat_id> <msg_id>` for the reply thread, or `grepogram context --json --before 15 --after 15 -- <chat_id> <msg_id>` for the neighbouring messages.
- Pass each message's own `chat_id` back with its `msg_id`: a channel post's comments come from the discussion group, and both chats number their messages from 1. Take `chat.id` and `anchor_msg_id` from a hit, `chat_id` and `msg_id` from a message in a thread.
- Always put the options first and `--` before the positional ids: a channel id is negative (`-100...`) and the CLI would read it as an option.
- Cite the hit's `url` for every claim; it opens the message in Telegram.
- Corroboration counts distinct origins. Forwards and copies of one post are one source however many chats repeat them. Say so when a claim rests on one forwarded post and prefer independent chats.
- Several accounts may be signed in. Name the account a claim came through when the accounts differ (`accounts` on the hit).
- If nothing relevant comes back, say so plainly after trying other variants, filters and chats. Never guess.

## A chat that is not indexed yet

When the user names a chat that search does not reach:

1. `grepogram sources ls` shows what is indexed.
2. `grepogram dialogs "<name>"` finds the chat or folder (add `--account <name>` to act as another account; `grepogram accounts ls` lists them).
3. Ask the user to confirm, then `grepogram sources add "<target>"` (this changes the config, so it keeps the normal permission prompt).
4. `grepogram sync --budget 600`, then search again.

## Errors

A failed command prints `error:` and often `hint:` on stderr. Relay the hint and do what it says when it is safe; otherwise ask the user. `sync`, `sources ls`, `dialogs` and `accounts ls` have no `--json`; read their plain output.
