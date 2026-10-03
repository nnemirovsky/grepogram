---
description: Set up grepogram from nothing to a first sync, one safe step at a time
allowed-tools: Bash(grepogram --version:*), Bash(grepogram config path:*), Bash(grepogram accounts ls:*), Bash(grepogram dialogs:*), Bash(grepogram sources ls:*)
disable-model-invocation: true
---

# Set up grepogram

Walk the user from no CLI to a first sync. This needs grepogram >= 0.3.0. Check the state before every step and skip a step that is already done, so running this command again is safe. Report each step in a line or two and wait for the user where the step says so.

Rules that hold throughout:

- Never read `config.toml` with Read, cat or any other tool: it holds the Telegram `api_hash`. Never print an `api_id` or `api_hash` value.
- Never run an installer script and never use `npx`, `uvx`, `pip` or `brew`. The only installs are `uv tool install` and `uv tool upgrade`, shown first and run only under the normal permission prompt.
- Anything that changes the config, the install, the MCP registration or an account keeps the normal permission prompt. Do not add allow rules for it.

## 1. The CLI

1. Run `uv --version`. Without `uv`, tell the user to install it from https://docs.astral.sh/uv/ themselves, then stop. Run no installer for it.
2. Run `grepogram --version`.
   - Found and at or above the floor: go to step 2.
   - Not found, or below the floor: ask which extras they want. `dense` adds the embedding and rerank models (about 4.5 GB) for semantic search; without it search is lexical only. `media` adds OCR and PDF/DOCX text extraction. Default to `dense`.
3. Show the exact command, then run it under the normal prompt: `uv tool install --managed-python --python 3.12 'grepogram[dense]'` (use `[dense,media]` or no extras to match the answer). For an existing install below the floor run `uv tool upgrade grepogram` instead, and if that leaves it below the floor, reinstall with the command above.
4. Run `grepogram --version` again. If the command is still not found, `uv tool update-shell` puts uv's tool directory on PATH; run it under the normal prompt, then tell the user to restart the shell and Claude Code and run `/grepogram:setup` again, then stop. The pre-allowed rules match the bare `grepogram` command only.

## 2. The config and the Telegram keys

1. Run `grepogram config path`. The `config` line is the config file's location.
2. If that file does not exist, run `grepogram config init` under the normal prompt. It refuses to overwrite an existing file.
3. Check that the keys are set without showing them, with `grep -cE '^api_id = [1-9][0-9]*' <config file>` and `grep -cE '^api_hash = "[^"]+"' <config file>`, replacing `<config file>` with the path from step 1. Each prints only a count; `1` means set, `0` means still the template default. Never run grep without `-c`.
4. If either is `0`, tell the user to create an app at https://my.telegram.org/apps and paste its `api_id` and `api_hash` into the `[telegram]` section of that file themselves, then wait for them to say they are done and run the two checks again.

## 3. Sign in

1. Run `grepogram accounts ls`. An account with session `authorized` is signed in; go to step 4.
2. Otherwise tell the user to run `grepogram auth` in a separate terminal, because it asks for the phone number, the login code and the 2FA password and they must type those themselves. A second account is `grepogram auth --account <name>`. Never ask for a code or a password in the chat.
3. When the user says they finished, run `grepogram accounts ls` to confirm.

## 4. Choose what to index

Nothing is indexed unless the user adds it.

1. Ask what to index: a Telegram folder, particular chats, a public channel.
2. For each answer run `grepogram dialogs "<query>"` (add `--account <name>` for another account) and show the matches.
3. After the user picks, run `grepogram sources add "<target>"` under the normal prompt: `folder:<name>`, `@username`, a chat title, or a chat id after `--` (`grepogram sources add --since 2024-01-01 -- -1001234567890`). `--since YYYY-MM-DD` skips older history on the first sync; `--comments` also takes a channel's discussion threads.
4. `grepogram sources ls` shows what is indexed.

## 5. First sync

1. Run `grepogram sync --budget 600` in the background. It may take a long time on the first run, and with `dense` it downloads the embedding model first.
2. Report progress as it prints. The run stops at the budget and the next `grepogram sync --budget 600` resumes where it stopped; say so rather than treating a budget stop as a failure.
3. When it is done, try one search with the search skill so the user sees a result.
4. Mention that a research mode exists and is off by default. Do not enable it; switching it on is the user's edit in the config file.

## 6. Optional: the MCP server

The plugin works through the CLI and does not need the MCP server. Offer it once and default to no.

1. Run `claude mcp get grepogram`. If it is already registered, say so and skip the rest.
2. On a yes, run `claude mcp add grepogram -s user -- "$(uv tool dir --bin)/grepogram-mcp"` under the normal prompt. Say a restart of Claude Code or a `/mcp` reconnect picks it up, and that the undo is `claude mcp remove grepogram -s user`.

Finish with a short list of what is now set up and what the user still has to do.
