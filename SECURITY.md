# Security policy

## Reporting a vulnerability

Report privately through GitHub, not in a public issue:

**[Open a private security advisory](https://github.com/nnemirovsky/grepogram/security/advisories/new)**

The report is visible only to the maintainer ([@nnemirovsky](https://github.com/nnemirovsky)) until an advisory is published, so nothing sensitive is exposed while a fix is prepared. Expect an acknowledgement within a week.

If the form is unavailable, open a normal issue that says only "I would like a private channel for a security report", with no details, and a private advisory will be opened for you.

Never attach real credentials, tokens, personal data or private files to a report. Describe the shape of the problem instead.

## Supported versions

Only the latest release gets security fixes.

## Scope

grepogram holds your Telegram session and a plain-text copy of the chats you opted into, and it can act on your Telegram account in research mode. A vulnerability is a way to break the promises in [PRIVACY.md](PRIVACY.md), for example:

- your `api_id`, `api_hash`, a session file, a login code or a 2FA password reaching a tool result, the log, the plugin's setup command or any file other than the ones PRIVACY.md lists
- `config.toml`, a session file or `research.db` created readable by other users of the machine
- a sync, search or MCP tool reading the history of a chat that is not one of your sources
- `research approve`, `accounts rm` or `leave` acting without a matching `--confirm` token or terminal confirmation, or a research run taking a route other than the one its approval summary named
- grepogram sending a message, joining a chat or spending stars without the approval PRIVACY.md describes
- message text or invite links written to the log above DEBUG level
- grepogram writing outside its own directories, or sending data anywhere other than Telegram and the model download from huggingface.co

Out of scope:

- content from your own index reaching the Claude conversation when a search or reader returns it. That is what the tools are for.
- the plugin's permission hook being bypassed by unusual command text. It is a second layer, and the CLI's own confirmation is the guarantee.
- what Telegram, Telethon or Hugging Face do with the requests grepogram makes
- anything that needs an attacker who can already read your files or run code as your user
