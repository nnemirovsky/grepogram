# grepogram as a Claude Code plugin

> Where this plan describes existing code it cites the file it was read from on `main` at
> `089e93f`. Read the code, not this file, when the two disagree, and update this file when the
> implementation has to deviate.

## Overview

Ship grepogram as a Claude Code plugin, installable from the repository as a marketplace and
later from Anthropic's plugin directory. The plugin is **CLI-first**: its skills and its setup
command drive the `grepogram` CLI through Bash and parse its `--json` output. The MCP server stays
**optional and off by default** — the plugin carries no `.mcp.json`; setup offers to register
`grepogram-mcp` with `claude mcp add` and only does so on a yes.

What the plugin adds over today's manual `claude mcp add`:

1. **One-step install** — `/plugin marketplace add nnemirovsky/grepogram`, then
   `/plugin install grepogram@grepogram`.
2. **`/grepogram:setup`** — an idempotent walk from no CLI to a first sync.
3. **Two skills** — `search` (search craft) and `research` (the research workflow), the MCP
   server's `INSTRUCTIONS` playbook translated to CLI calls, with read-only commands pre-allowed.
4. **A consent hook** — the "ask" gate README tells users to add by hand, shipped as a
   PreToolUse hook on the confirming calls.

The plugin ships with **0.3.0**, the first release carrying the accounts and research features it
drives; the version bump is part of this branch and the tag follows the merge.

Decisions taken with the user before planning (brainstorm, 2026-10-03):

- **Option A**: one plugin, CLI by default, MCP registered only on request; no bundled `.mcp.json`.
- **Plugin lives in `plugin/`**, the repository root carries the marketplace. The repo is
  mostly Python, tests and CI; a subfolder keeps those out of what users install and keeps the
  directory's download-and-run scanner from flagging dev files.
- **Gap accepted**: `sync`, `sources ls`, `dialogs` and `accounts ls` have no `--json`; Claude
  reads their plain output. The CLI is not widened for the plugin.
- **Playbook duplicated, drift tested**: the skills restate the MCP `INSTRUCTIONS` in CLI terms
  rather than generating one from the other; a test fails when a skill names a command or flag
  the CLI does not have.
- **Regular testing**: each artifact, then its tests in the same task.

Revised after the automated plan review (2026-10-03): version floor decoupled from
`__version__`; `--` before positional chat ids; the hook's MCP branch and its jq path dropped;
research-approve prompt expectations made consistent; setup never reads `config.toml`.

## Context (from discovery)

- `grepogram/mcp.py:102` — `INSTRUCTIONS`, the server playbook the skills translate: query
  variants, `mode="lexical"`, `since` and evidence dates, `thread` / `context` with each
  message's own `chat_id`, citing `url`, corroboration by distinct origin, multi-account scopes,
  the research flow.
- `grepogram/cli.py` — typer app. `search`, `thread`, `context` take `--json`; `search --json`
  prints `asdict(SearchResult)` including `index_age_min` (cli.py:1011), and the plain output
  only prints a staleness note past `cfg.search.auto_sync_after_min` (cli.py:1018). `research
  approve`, `accounts rm` and `leave` take `--json` and `--confirm TOKEN`; without a terminal
  (or with `--json`) they print `{"summary", "confirm", "command"}` (plus `error` for a refused
  token, `cli._needs_confirmation`, cli.py:2318) and exit `CONFIRM_EXIT` (3). `research start`
  requires `--seed/-s` (cli.py:1708-1714).
- **Negative chat ids.** A channel or supergroup row has `id == peer_id`, Telethon's marked id
  (`-100…`). Click reads a negative positional as an option: `grepogram thread -1001234567 5`
  fails with "No such option: -1"; `grepogram thread --json -- -1001234567 5` parses (verified).
  Option values (`search --chat -100…`) are fine. `leave` already puts its target after `--`
  (`cli._with_confirm`).
- **Index age.** CLI `index_age_min` comes from `db.last_sync_at` (the last completed chat sync);
  the MCP `_stale` (mcp.py:699-707) prefers `last_sync_run` and treats never-synced as empty, not
  stale. A null age with no sources configured comes with a warning.
- **MCP approve under Claude Code.** Claude Code declares the elicitation capability, so
  `research_approve` grants only through the dialog and rejects a `confirm` argument
  (`mcp._can_elicit`, mcp.py:1083-1098; `ELICIT_INSTEAD`, mcp.py:1246-1248); a failed dialog
  hands back the shell `command`, which goes through Bash. The hook therefore needs no MCP rule.
- `README.md:170-194` — step 7 "Connect your agent" (`claude mcp add …`); `README.md:614-632` —
  the hand-written Claude Code "ask" rules for `research approve`, `accounts rm`, `leave`.
- `pyproject.toml` — hatch wheel `packages = ["grepogram"]`, so `plugin/` never reaches the
  wheel. The sdist already ships `.claude/`, `docs/`, `tests/` and `uv.lock` (checked with
  `uv build --sdist` at 0.2.0), so `plugin/`, `.claude-plugin/` and `PRIVACY.md` will be in it
  too — harmless, no test.
- `grepogram/__init__.py` — `__version__ = "0.2.0"`. PyPI 0.2.0 has no `accounts`, `research`,
  `leave` or `search --account` (its sub-apps are `config` and `sources` only). The repository
  variable `PYPI_PUBLISH` is `true`.
- `.github/workflows/release.yml:24-31` — the shell step that refuses a tag not matching
  `__version__`. `ci.yml` runs pytest on `macos-latest` only; `ubuntu-latest` runs lint and mypy.
- `tests/test_mcp.py:1675` — the existing precedent for `subprocess.run` in tests;
  `tests/test_cli.py:18` — `CliRunner`; `typer.main.get_command(app)` gives the click tree.
- Network call sites for PRIVACY.md: Telethon (`grepogram/tg.py`, `sync.py`, `research/`),
  Hugging Face downloads in `grepogram/embed.py:236` and `rerank.py`; nothing else imports an
  HTTP client (`leads.py` only parses URLs).

## Development Approach

- **testing approach**: Regular (code first, then tests in the same task)
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional - they are a required part of the checklist
  - write unit tests for new functions/methods
  - write unit tests for modified functions/methods
  - add new test cases for new code paths
  - update existing test cases if behavior changes
  - tests cover both success and error scenarios
- **CRITICAL: all tests must pass before starting next task** - no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- run tests after each change: `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`
- maintain backward compatibility: the MCP server, its tools and the CLI are untouched
- scoped lowercase Conventional Commits, one logical change each (`feat(plugin): …`,
  `test(plugin): …`, `docs(readme): …`, `ci(release): …`, `chore(release): …`)
- iterate on the plugin with `claude --plugin-dir ./plugin`: a marketplace install is cached by
  `version` and does not pick up edits

## Testing Strategy

- **unit tests**: one new module, `tests/test_plugin.py`, holding the manifest/version lock, the
  floor check, the CLI drift check and the hook matrix. Pure file reads plus a `subprocess.run`
  of the hook script; no network, no Claude Code. They run on the macOS CI job.
- **e2e tests**: the project has none. Installing the plugin into Claude Code and running setup,
  search and an approve round is manual (Post-Completion).

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview

```
.claude-plugin/marketplace.json     # repo root: the marketplace, source "./plugin"
plugin/
  .claude-plugin/plugin.json        # manifest; version == grepogram.__version__
  .claude-plugin/<icon>             # square PNG the user picks (never named in any text file)
  commands/setup.md                 # /grepogram:setup
  skills/search/SKILL.md
  skills/research/SKILL.md
  hooks/hooks.json                  # one PreToolUse(Bash) entry
  scripts/consent-gate.sh           # the single, self-contained hook script
PRIVACY.md                          # repo root; manifest privacyPolicyUrl points here
tests/test_plugin.py
```

- **Version lock.** `plugin.json` `version` equals `__version__`: a test asserts it and
  `release.yml` refuses a tag whose `plugin.json` disagrees.
- **CLI floor, decoupled.** The setup command and both skills state the minimum CLI version they
  need as one literal phrase, `grepogram >= X.Y.Z`, exactly once per file and the same in all
  three. A test asserts they agree and that the floor is `<= __version__`. The floor is raised
  on purpose, when a skill starts using newer CLI surface — not on every release, so a plugin
  update never hard-stops a user whose CLI still does everything the skills ask. It starts at
  `0.3.0`. Below the floor, the preflight names the fix (`uv tool upgrade grepogram`).
- **Pre-allowed commands.** Each skill/command lists its CLI calls that change nothing in
  `allowed-tools` (`Bash(grepogram search:*)` …) — some read Telegram (`sync`, `dialogs`,
  `research discover`). Anything that changes the config, the install, the MCP registration or
  an account on Telegram keeps the normal permission prompt, and `research approve` is never
  pre-allowed: a hook that times out or fails is non-blocking, and an allow rule behind it would
  let the confirming call through unprompted.
- **Positional chat ids after `--`.** Every skill command that takes a chat id as a positional
  puts its options first and `--` before the positionals (`grepogram context --json -- <chat_id>
  <msg_id>`); the drift check enforces it.
- **Consent hook.** The CLI's token handshake guarantees *what* is confirmed; the human gate is
  the harness prompt. With `approve` un-allowed, the summary-only call gets the normal prompt and
  the `--confirm` call gets a hook-forced `ask` that holds even under auto mode or an allow rule
  the user added.
- **Directory constraints** (from the session that submitted resume-watchdog, ticktock and
  iwdp-mcp): hook commands point at one self-contained script by a quoted literal path under
  `${CLAUDE_PLUGIN_ROOT}`; the script sources and runs no other plugin file, uses no heredocs or
  here-strings, builds its output with a fixed `printf`; no `npx`, `uvx`, `pip`, `npm`, `brew`
  or `curl | sh` in any command or text Claude is told to run; no top-level `bin/`; no text file
  names the icon; explicit manifest URLs so the listing does not scrape the README's badges.

## Technical Details

### `plugin/.claude-plugin/plugin.json`

```json
{
  "name": "grepogram",
  "displayName": "grepogram",
  "version": "0.3.0",
  "description": "Search your opt-in Telegram chats locally with hybrid BM25 + embedding retrieval, from Claude Code through the grepogram CLI",
  "author": {"name": "Nikita Nemirovsky", "url": "https://github.com/nnemirovsky"},
  "homepage": "https://github.com/nnemirovsky/grepogram",
  "repository": "https://github.com/nnemirovsky/grepogram",
  "documentationUrl": "https://github.com/nnemirovsky/grepogram#readme",
  "supportUrl": "https://github.com/nnemirovsky/grepogram/issues",
  "privacyPolicyUrl": "https://github.com/nnemirovsky/grepogram/blob/main/PRIVACY.md",
  "license": "MIT",
  "keywords": ["telegram", "search", "local", "rag", "embeddings", "chats"]
}
```

`documentationUrl`, `supportUrl` and `privacyPolicyUrl` were expected to draw UNKNOWN_KEY
warnings; the validator raised none (Task 8). Components are auto-discovered; no paths in the
manifest.

### `.claude-plugin/marketplace.json` (repo root)

```json
{
  "name": "grepogram",
  "description": "Claude Code plugins for searching your own Telegram chats locally",
  "owner": {"name": "Nikita Nemirovsky", "url": "https://github.com/nnemirovsky"},
  "plugins": [
    {"name": "grepogram", "source": "./plugin", "description": "…same as plugin.json…"}
  ]
}
```

Check the exact marketplace schema with `claude plugin validate .` before settling field names.
The marketplace serves `./plugin` from the default branch: merging to `main` publishes.

### `plugin/hooks/hooks.json`

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/scripts/consent-gate.sh\"", "timeout": 10}]
      }
    ]
  }
}
```

### `plugin/scripts/consent-gate.sh`

- `#!/bin/bash`, within bash 3.2 (macOS `/bin/bash`): `[[ =~ ]]` with the pattern in a variable,
  no associative arrays, no `${var,,}`, no `mapfile`.
- Reads the hook payload from stdin once (`payload=$(cat)`). No `jq`: the raw payload is matched
  directly, which over-asks slightly (a `cwd` or description naming grepogram can satisfy part of
  a match) — acceptable; missing a confirmation is not.
- Ask when the payload matches `grepogram.*--confirm` under `shopt -s nocasematch`
  (`--confirm TOKEN` or `--confirm=TOKEN`). `--confirm` exists only on `research approve`,
  `accounts rm` and `leave`, so the words between are not matched: a line continuation or a tab
  (both JSON-escaped in the payload), a quoted word or another case cannot slip past. That covers
  a bare `grepogram`, an absolute path, `uv run grepogram` and chained commands. (Changed after
  review: the first pattern required the subcommand words to be adjacent and missed those
  forms.) Deliberate obfuscation (`grepogra""m`) still gets past a regex; the hook is defence in
  depth and the docs say so.
- **Ask output** on stdout, exit 0 (not stderr — the plugin-dev examples get this wrong):
  `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"grepogram: this confirms a consent summary (research approval, account removal or leaving a chat); check it matches what you agreed to"}}`
- **Otherwise:** no output, exit 0 — the normal permission rules decide.
- No sourcing, no heredocs, no here-strings, one file.

### Drift check (`tests/test_plugin.py`)

- Collect every command line from `plugin/commands/*.md` and `plugin/skills/*/SKILL.md`: inline
  code spans and fenced-block lines that start with `grepogram `, plus `allowed-tools` entries of
  the form `Bash(grepogram <words>:*)` — frontmatter as a YAML list or a comma string.
- `shlex.split` each (a `ValueError` fails with file and line); walk `typer.main.get_command(app)`
  through subcommand names, root options (`--version`, `-v`) allowed before the first one.
- Every token starting with `-` before a `--` must be one of the resolved command's
  `param.opts + param.secondary_opts` (covers `--no-rerank`, `-k`, `-c`, `-a`, `-s`, `-n`);
  `--x=y` is split on `=`. A token that is a negative number before `--` fails (the CLI would read
  it as an option). Tokens after `--`, placeholders (`<query>`, `…`) and positional values are
  skipped.
- A line naming a subcommand the app lacks, or a flag the command lacks, fails with the file and
  the line.
- Added after review: an option's value is skipped (so `--chat -100…` passes), `=value` on a flag
  fails, square brackets are dropped, `~~~` fences count, an option named on its own in an inline
  span must exist on some command, and every command with a string positional (a query, a
  question, a chat) needs `--` before it, `research approve` excepted. A separate test holds every
  `allowed-tools` entry to a fixed set of commands that change nothing, and another pins the CLI
  constants and JSON field names the skills quote.

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): plugin files, tests, CI check, the version bump,
  docs.
- **Post-Completion** (no checkboxes): tagging 0.3.0, installing the plugin into Claude Code and
  exercising it, the directory submission.

## Implementation Steps

### Task 1: Manifest, marketplace, version lock and the 0.3.0 bump

**Files:**
- Modify: `grepogram/__init__.py`
- Create: `plugin/.claude-plugin/plugin.json`
- Create: `.claude-plugin/marketplace.json`
- Create: `tests/test_plugin.py`
- Modify: `.github/workflows/release.yml`

- [x] bump `__version__` to `0.3.0` (its own `chore(release): …` commit); check nothing else
      pins the old number (tests, README)
- [x] create `plugin/.claude-plugin/plugin.json` as in Technical Details, `version` `0.3.0`
- [x] create the root `.claude-plugin/marketplace.json` pointing at `./plugin`
- [x] run `claude plugin validate .` and `claude plugin validate plugin/.claude-plugin/plugin.json`; (result: both pass; marketplace description warning fixed by adding a top-level `description`; no warnings left)
      fix what they report (record any warning left on purpose in this plan)
- [x] extend the tag check in `release.yml` to also refuse a tag whose
      `plugin/.claude-plugin/plugin.json` `version` differs (one `jq -r .version` line beside the
      existing grep)
- [x] write tests: `plugin.json` parses, has the required keys, `version == grepogram.__version__`;
      `marketplace.json` lists exactly one plugin whose `source` directory holds that manifest
- [x] run the full checks — must pass before Task 2
- ⚠️ missed here and fixed after review: README and code comments still promised whisper.cpp
  transcription in 0.3.0, which this release does not ship; they now say voice messages and
  video notes are not transcribed yet and whisper.cpp is on the roadmap

### Task 2: Consent gate hook

**Files:**
- Create: `plugin/hooks/hooks.json`
- Create: `plugin/scripts/consent-gate.sh` (executable)
- Modify: `tests/test_plugin.py`

- [x] create `hooks.json` with the one `Bash` PreToolUse entry, the command quoted as
      `"\"${CLAUDE_PLUGIN_ROOT}/scripts/consent-gate.sh\""`
- [x] write `consent-gate.sh` per Technical Details (`#!/bin/bash`, bash 3.2, cheap filter, one
      regex, fixed ask JSON on stdout, silent exit 0 otherwise); `chmod +x` and commit the mode
- [x] write the hook matrix test, executing the script directly so the shebang (`/bin/bash`, 3.2
      on macOS) is honoured, payload on stdin: asks for `grepogram research approve 3 1:join
      --json --confirm abc`, `--confirm=abc`, `/Users/x/.local/bin/grepogram accounts rm work
      --confirm abc`, `uv run grepogram leave --confirm abc -- @chat`, `accounts  rm` with two
      spaces, a chained `cd x && grepogram leave … --confirm y`; the ask output parses as JSON
      with `permissionDecision == "ask"` and the exit code is 0
- [x] write the silent cases: the same commands without `--confirm`, `grepogram search …`,
      `ls`, a payload with no `grepogram` at all, an empty payload; record (and test) the chosen
      behaviour for a search query whose text contains "leave --confirm" — over-asking is fine (result: it asks, tested; the pattern needs no trailing space after `--confirm`)
- [x] write a test that every `command` in `hooks.json` is the quoted `${CLAUDE_PLUGIN_ROOT}`
      path of a file that exists and is executable, and a lint-style test that the script has
      no `source` / `. ` of another file, no `<<`, and no `uvx|npx|pip |npm |brew `
- [x] run the full checks — must pass before Task 3

### Task 3: Search skill, the drift check and the floor check

**Files:**
- Create: `plugin/skills/search/SKILL.md`
- Modify: `tests/test_plugin.py`

- [x] write the frontmatter: `name: search`, a trigger-rich `description` (what people say in the
      user's Telegram chats, "find in Telegram", a named chat or folder, RU and EN phrasing),
      `allowed-tools` for `grepogram --version`, `search`, `thread`, `context`, `sync`,
      `sources ls`, `dialogs`, `accounts ls`
- [x] write the preflight: `grepogram >= 0.3.0` stated once; `grepogram --version` missing → point
      to `/grepogram:setup`; below the floor → `uv tool upgrade grepogram` (or setup)
- [x] write the freshness rule: on the first `search --json` of a conversation, an
      `index_age_min` above 60 (the default of the configurable `[search]
      auto_sync_after_min`) → one `grepogram sync --budget 60`, then search again; a null age →
      read `warnings`: no sources configured → point to `/grepogram:setup`, nothing synced yet →
      one sync or setup; sync at most once per conversation; say when a sync was skipped because
      another one runs
- [x] write the playbook from `mcp.py` `INSTRUCTIONS` in CLI terms: 2–3 RU/EN variants,
      `--mode lexical`, `--since`, evidence dates (unix seconds → dates), `thread` / `context`
      with each message's own `chat_id`, **always options first and `--` before the positional
      ids** (`grepogram thread --json -- <chat_id> <msg_id>`), cite `url`, forwards are one
      origin, multi-account (`--account`, `account:<name>` chat specs), say "nothing found"
      plainly, the unindexed-chat path `sources ls` → `dialogs` → `sources add` → `sync`; errors
      and `hint`s
- [x] implement the drift check in `tests/test_plugin.py` per Technical Details, run over every
      md file under `plugin/`
- [x] write drift tests on synthetic markdown: an unknown subcommand fails, an unknown flag
      fails, a negative number before `--` fails, the same after `--` passes, `--flag=value`,
      `secondary_opts` (`--no-rerank`), short forms and root options pass, placeholders pass, an
      `allowed-tools` entry is checked in both list and comma-string form, a `shlex` error names
      the file and line
- [x] write the floor check: exactly one `grepogram >= X.Y.Z` per md file under `plugin/`, all
      equal, and `<= __version__`
- [x] run the full checks — must pass before Task 4

### Task 4: Research skill

**Files:**
- Create: `plugin/skills/research/SKILL.md`
- Modify: `tests/test_plugin.py` (only if the drift check needs a new case)

- [x] write the frontmatter: `name: research`, `description` for finding chats the user does not
      index yet / an explicit research request; `allowed-tools` for `research start`,
      `discover`, `candidates`, `status`, `skip`, `stop` (not `approve`, `run`, `exclude`)
- [x] write the body: the same floor and preflight; `[research] enabled` false → say so and stop
      (the CLI's own error names the key); `research start` needs `--seed` (one or more indexed
      chats) and an `--account`; the flow start → discover → candidates (evidence; `member`,
      `cached`, `authorized` kept apart; only probed candidates can be approved) → report and ask
- [x] write the approval step: always `--json` on both calls (it never reads a terminal); the
      first call exits 3 — the expected "needs confirmation" result, not a failure to retry — and
      prints `summary`, `confirm` (the token) and `command`; show `summary` verbatim; only after
      the user agrees, run the same command with `--confirm <token>`; expect a permission prompt
      on both calls; a refused token comes back with `error` and a fresh summary — show it again,
      never confirm on own judgement
- [x] write run → analyse with `search` / `thread` / `context` (with `--`) → `stop` (sources
      stay); `skip` and `exclude` only narrow, `exclude` takes marked ids after `--`; global and
      paid search need their own approvals
- [x] confirm the drift and floor checks cover this file (they glob); add a case only if the file
      uses a form the parser did not meet before
- [x] run the full checks — must pass before Task 5

### Task 5: `/grepogram:setup`

**Files:**
- Create: `plugin/commands/setup.md`

- [x] write the frontmatter: `description`, `disable-model-invocation: true`, `allowed-tools` for
      `grepogram --version`, `config path`, `accounts ls`, `dialogs`, `sources ls`; the floor
      phrase once
- [x] step 1: no `uv` → tell the user to install it from https://docs.astral.sh/uv/ themselves
      (Claude runs no installer script); CLI missing or below the floor → ask which extras
      (`dense` ≈ 4.5 GB of models, `media` for OCR/PDF/DOCX) → show `uv tool install
      --managed-python --python 3.12 'grepogram[…]'` (or `uv tool upgrade grepogram`) and run it
      only under the normal prompt; `grepogram` not on PATH afterwards → `uv tool update-shell`
      and a restart (the pre-allowed rules match the bare command only)
- [x] steps 2–3: `config init` if absent and `config path`; the user pastes `api_id` / `api_hash`
      into the file themselves; **never Read `config.toml`** — check the keys are set without
      printing values (`grep -cE '^api_hash = "[^"]+"'` and the `api_id` equivalent, or the
      missing-keys error of `grepogram dialogs <x>`); `grepogram auth` (and `auth --account
      <name>`) in a separate terminal, verified with `accounts ls`
- [x] steps 4–6: ask what to index, `dialogs <query>`, `sources add` per pick; first
      `grepogram sync --budget 600` in the background, progress reported, resumable; mention
      research exists and is off, do not enable it
- [x] step 6: offer MCP (default no): `claude mcp get grepogram` first (README users may already
      have it) → `claude mcp add grepogram -s user -- "$(uv tool dir --bin)/grepogram-mcp"`; say
      a restart or `/mcp` reconnect picks it up; name the undo `claude mcp remove grepogram -s
      user`; every step re-checks its state first so re-running setup is safe
- [x] confirm the drift and floor checks cover this file; run the full checks — must pass before
      Task 6

### Task 6: Privacy policy

**Files:**
- Create: `PRIVACY.md`

- [x] verify each claim against the code before writing it: what is read (opt-in sources through
      the user's own sessions; research probes metadata of candidates, joins only what was
      approved, global search sends the session question only after its approval); what is
      written (`config.toml`, sessions, `index.db`, `research.db`, locks, `logs/` under
      `~/.config/grepogram` or `GREPOGRAM_HOME`, 0600 / 0700; the Hugging Face cache)
- [x] network: Telegram (MTProto via Telethon) and the first-use model downloads from
      huggingface.co (huggingface_hub sends its own user-agent headers); grepogram itself sends
      no telemetry; the plugin's hook makes no request
- [x] what reaches Claude's context: hit snippets and message text, sender names, chat titles and
      usernames, links — personal data of chat members; it leaves the machine only as part of the
      user's Claude conversation; the API keys never do (setup never reads the config)
- [x] write a test that `PRIVACY.md` exists and that the manifest's `privacyPolicyUrl` names it
- [x] run the full checks — must pass before Task 7

### Task 7: Icon

**Files:**
- Create: `plugin/.claude-plugin/` icon PNG (the user's pick)

- [x] ⚠️ needs the user: render 3–4 square PNG options (512–2048 px, < 2 MB) in the scratchpad (user picked option 3, the terminal tile)
- [x] send the user a contact sheet and wait for the choice (user picked option 3, the terminal tile)
- [x] copy the chosen PNG into `plugin/.claude-plugin/`; grep every text file in the repo to
      confirm none names the file
- [x] write a test: exactly one PNG in `plugin/.claude-plugin/`, square, 512–2048 px, < 2 MB
      (read the IHDR chunk with `struct`, no new dependency), and no text file in the tree
      (the top-level files plus `plugin/`, `docs/`, `grepogram/`, `tests/`, `.github/`,
      `.claude-plugin/` and `.claude/rules/`, binary files skipped — not `git ls-files`, which
      breaks in an sdist, and not a whole-tree walk, which read gitignored local archives)
      contains its basename
- [x] run the full checks — must pass before Task 8

### Task 8: Verify acceptance criteria

- [x] verify all requirements from Overview are implemented (result: one-step install via root marketplace with source `./plugin`; `/grepogram:setup` (`disable-model-invocation`); `search` and `research` skills with read-only `allowed-tools`, `research approve` not pre-allowed; consent hook as a PreToolUse(Bash) entry; version 0.3.0 in `__init__` and `plugin.json`; no `.mcp.json`)
- [x] verify no `.mcp.json`, no top-level `bin/`, no `uvx|npx|pip install|brew install|curl … | sh`
      under `plugin/`
- [x] run `claude plugin validate .` and `claude plugin validate plugin/.claude-plugin/plugin.json`
      once more; record any warning left on purpose (result: both print "Validation passed" with no warnings, so none is left; the UNKNOWN_KEY warnings Technical Details expected for `documentationUrl`, `supportUrl` and `privacyPolicyUrl` did not appear)
- [x] run full test suite: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy` (result: 2526 passed, 11 deselected; ruff, format and mypy clean)
- [x] verify each rule in the hook script has a matrix row (pytest-cov does not measure bash) (result: filter without `grepogram`, each of the three alternatives, both `--confirm` forms, absolute path, `uv run`, chaining, double space, no-confirm silence and empty payload all have rows; no gap)

### Task 9: [Final] Update documentation

- [x] README: a "Claude Code plugin" install path first in Setup (marketplace add, install,
      `/grepogram:setup`); keep `claude mcp add` for other clients and for users who want MCP;
      the hand-written "ask" rules block becomes "the plugin installs this gate; without it, add
      these rules"; claim no more about bypass/auto mode than the manual check recorded
- [x] CLAUDE.md: the plugin layout, the version lock (a release bumps `__version__` and
      `plugin.json`), the floor rule (raised on purpose when a skill needs newer CLI surface),
      `--` before positional chat ids, the drift check, the hook script constraints; never name
      the icon file
- [x] CONTRIBUTING.md: the same release, floor and drift rules where it covers releasing
- [x] move this plan to `docs/plans/completed/` (done by the orchestrator at completion)

## Post-Completion

*Items requiring manual intervention or external systems - no checkboxes, informational only*

**Release (right after the merge — merging is publishing):**
- tag `v0.3.0` and push it; `release.yml` checks `__version__` and `plugin.json`, cuts the GitHub
  release and, with `PYPI_PUBLISH == 'true'`, uploads to PyPI; confirm `uv tool install
  grepogram` then resolves 0.3.0

**Manual verification:**
- iterate with `claude --plugin-dir ./plugin`; then `/plugin marketplace add
  ~/Developer/grepogram`, `/plugin install grepogram@grepogram`, restart; `/grepogram:setup` with
  the CLI present and with it absent
- ask a real question; check the skill triggers, pre-allowed calls do not prompt, `sources add`
  does, the stale-index sync runs once, a negative chat id reaches `thread` / `context`
- a research approve round: both calls prompt, the `--confirm` one through the hook; check the
  hook still prompts under auto mode and under `bypassPermissions` (a local Claude Code source
  copy suggests a hook "ask" is forced through both and becomes a denial under `-p`; record what
  actually happens)
- with MCP registered, `research_approve` shows the elicitation dialog

**Directory submission** (rules from the resume-watchdog / ticktock / iwdp-mcp submissions):
- check that "grepogram" is not within a character or two of an existing listing
  (NAME_CONFUSABLE)
- the icon must be on `main` before the first portal save; surfaces: Claude Code only
- portal answers: Under 18: No; auto-publish: off; push webhook: yes — create it with
  `gh api repos/nnemirovsky/grepogram/hooks -X POST --input -` (events `["push"]`, content type
  json) and confirm the ping delivery got 200 "pong"
- personal data question: "Reads only"
- a residual COMMAND_SCRIPT_NOT_FOLLOWED hold without a line number may be left for the reviewer;
  for any unclear check, get its expanded detail and rule code before changing anything