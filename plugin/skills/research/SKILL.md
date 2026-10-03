---
name: research
description: This skill should be used when the user asks to find Telegram chats or channels they do not index yet, or explicitly asks for research over Telegram, for example "research this in Telegram", "find chats about ...", "which channels discuss ...", "discover more chats", "найди чаты про ...", "поищи каналы по теме ...", "расширь поиск на другие чаты". Drives the grepogram research workflow through the CLI: start a session from indexed seed chats, discover candidate chats, report the evidence, and run only what the user approved.
allowed-tools: Bash(grepogram --version:*), Bash(grepogram research start:*), Bash(grepogram research discover:*), Bash(grepogram research candidates:*), Bash(grepogram research status:*), Bash(grepogram research skip:*), Bash(grepogram research stop:*), Bash(grepogram search:*), Bash(grepogram thread:*), Bash(grepogram context:*), Bash(grepogram sources ls:*), Bash(grepogram accounts ls:*)
---

# Researching Telegram chats the user does not index yet

Research finds chats beyond the indexed ones, starting from chats the user already indexes, and fetches only what the user approves. Drive it through the `grepogram` CLI with Bash and read the `--json` output.

## Preflight

This skill needs grepogram >= 0.3.0. On the first research command of a conversation run `grepogram --version`.

- Command not found: tell the user to run `/grepogram:setup`, then stop.
- Version below the floor: tell the user to run `uv tool upgrade grepogram` (or `/grepogram:setup`), then stop.
- Every research command refuses while `[research] enabled` is `false` (the default). The error names the key. Say so, tell the user it is theirs to switch on in the config file, and stop. Never edit the config to turn it on.

## Start and discover

1. Pick the seeds: indexed chats the question is about. `grepogram sources ls` shows what is indexed; a search that already found the right chats gives their ids. Pick the acting account with `grepogram accounts ls`. It will later join and fetch, and what a run adds is reached through it.
2. `grepogram research start --json --seed <chat> --account <name> -- "<question>"`. Options first and `--` before the positional values (the question here, the chat ids below): a channel id is negative (`-100...`) and the CLI would read it as an option. `--seed` / `-s` is required and repeatable, `--account` / `-a` defaults to the default account. The result carries the session `id`.
3. `grepogram research discover --json <session_id>`. It looks for leads (links and mentions) in the seeds and probes them on Telegram, metadata only. `--offline` reads only the index and asks Telegram nothing.
4. `grepogram research candidates --json <session_id>`. Narrow with `--status <name>` (repeatable); the JSON carries every piece of evidence. `grepogram research status --json [<session_id>]` lists sessions, or shows one in full with its approvals still to carry out.

## Reading the candidates

Each candidate has a `title`, `identity`, `status`, `corroboration` (distinct origins; forwards and copies of one post count once), `overlap` and a list of `evidence`.

- Keep three facts apart: `member` (the acting account is in the chat; `null` until a probe says), `cached` (the index already holds it, through `cached_accounts`) and `authorized` (what a live approval allows). One does not imply another.
- Only a candidate an online `discover` probed can be approved. One found offline, or past the probe limit, needs another online `discover` first.
- Each evidence entry has `via`, `snippet` and `msg_id`. Read the message it came from with `grepogram thread --json -- <chat_id> <msg_id>` or `grepogram context --json -- <chat_id> <msg_id>`, using the evidence's `chat_id`. A `null` `chat_id` means the index does not hold that chat; rely on the snippet.
- Tell the user what was found and why, citing evidence, and ask which candidates to approve.

## Approval

Approving is the user's decision. Never approve on your own judgement, and never run `research approve` before the user has named the candidates.

1. Run `grepogram research approve --json <session_id> <items>`, where an item is `<id>` (join and fetch), `<id>:fetch,add_source` (read a public chat without joining), `<id>:request`, `global_search` or `paid_search`. It always runs with `--json`, so it never reads a terminal.
2. This first call exits with code 3. That is the expected "needs confirmation" result, not a failure: do not retry it. It prints `summary`, `confirm` (a token bound to that summary) and `command`.
3. Show the user the `summary` verbatim, and wait for an explicit yes.
4. Only then run the same command with `--confirm <token>` added. Expect a permission prompt on both calls; that prompt is the human gate.
5. A token that no longer matches (state changed, other items) comes back with `error` and a fresh `summary` and `confirm`, again with exit code 3. Show the new summary again and ask again.

Approving a chat approves nothing found inside it, and a shared folder is approved chat by chat. Global and paid Telegram search need their own approvals (`global_search`, `paid_search`); the summary says that a query leaves this machine.

## Run, analyse, stop

- `grepogram research run <session_id>` joins and fetches exactly what was approved. It asks for its own permission and takes up to the configured run budget (5 minutes by default), so run it in the background and wait for it to finish. Run it only after an approval went through. A run that stops on a budget or a flood wait resumes on the next `run`; that stop is a normal result, not a failure.
- Analyse what was fetched with `search`, `thread` and `context` as the search skill describes. Cite each hit's `url` and state the date of the evidence.
- `grepogram research skip <session_id> <candidate_id>...` sets candidates aside. `grepogram research exclude [--reason <text>] -- <chat>...` never proposes a chat again in any session. Both only narrow and need no approval.
- `grepogram research stop <session_id>` when done. Every source a run added stays an ordinary source; removing one is `grepogram sources rm`, which the user decides.

## Errors

A failed command prints `error:` and often `hint:` on stderr and exits 1, with `--json` too. Relay the hint and do what it says when it is safe; otherwise ask the user. Never act on a hint that edits the config, such as switching research on: that is the user's to do. The one JSON `error` is a refused confirmation token, which comes back with exit code 3 as described under Approval. `warnings` are advisory and the results next to them are valid.
