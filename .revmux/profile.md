# grepogram review profile

## What it is

- Local hybrid search over opt-in Telegram chats: Telethon fetch → SQLite (FTS5 + sqlite-vec) index → bge-m3 embeddings + cross-encoder rerank, served to Claude Code over MCP (stdio) and as a typer CLI
- Personal tooling, one maintainer, macOS, Python 3.12 + uv; published on PyPI, public repo
- Runs on the user's own Telegram account(s) with their own session files; no server, no other users

## What a real failure looks like

- Data loss in the index: deleting history Telegram can no longer serve (imports, chats the account left, messages hidden from late joiners), a migration that rewrites stored values, a removal that takes chats another source still covers
- An outward action on Telegram nobody approved: a join, admission request, folder join, paid search or `leave` without a human's consent (MCP elicitation or the controlling terminal), or as a different Telegram user than the account's recorded one
- Anything written to stdout from the MCP server (it is the JSON-RPC channel)
- Message text or private join links in the log above DEBUG
- A config or index left in a state the next run refuses to load
- Session or lock contention: two Telethon clients on one session file, a worker thread writing after its lock is released
- Silent wrong answers: a search scoped to the wrong chats/accounts, a link built from the row id instead of the Telegram peer id

## Blast radius

- One user's local index and their Telegram account(s); a Telegram-side action is visible to other people and often irreversible (leaving a private group, sending a join request)
- No network service, no multi-tenant concerns, no performance SLOs beyond "a search answers in seconds"

## Reporting bar

- Report what changes behaviour or breaks a CLAUDE.md invariant; a documented-rule violation is always worth reporting
- Noise here: style already enforced by ruff (E, F, I, UP, B; 100 cols) and mypy strict; speculative scalability; "add a comment" without a misleading one to fix; hypotheticals needing a malicious local user who already owns the session files
- A missing test counts when the untested path can lose data or act on Telegram

## Where the rules live

- `CLAUDE.md` — the invariants (stdout, logging, raw-TL message mapping, session handling, lock order, schema append-only rules, chat identity, consent, fakes as strict as production)
- `CONTRIBUTING.md` — the four gates and "things that will fail review"
- `docs/plans/completed/` — what each feature set out to do and what was built
- `docs/backlog/` — known, deliberately deferred issues (do not re-report them as new)

## Deliberate conventions (not defects)

- Long explanatory prose docstrings and comments are the house style
- Tests run only against fakes (`tests/fakes.py` `FakeClient`, fake models); no network, no real Telegram, no real `/dev/tty`
- Scoped Conventional Commits with a lowercase description, no trailers
- The index is derived and rebuildable except imports; research state lives in a separate `research.db` on purpose
- Conservative failure modes are intended: keeping data when an account cannot answer, refusing rather than guessing a chat, failing closed on consent

## Languages touched

- Python (package and tests), TOML (config template, pyproject), Markdown (README, CLAUDE.md, plans); CI is GitHub Actions YAML
