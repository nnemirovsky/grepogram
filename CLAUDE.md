# CLAUDE.md

grepogram: local hybrid search over opt-in Telegram chats, served to Claude Code over MCP.
Python 3.12 pinned, `uv` only (no pip, no global installs), developed on macOS.

## Commands

- `uv sync --all-extras --all-groups` — full environment (the `dense` extra brings torch and
  sentence-transformers)
- `uv run pytest` — the suite: in-memory SQLite, fake models, no network; `slow` tests are
  deselected by `addopts`
- `HF_HUB_OFFLINE=1 uv run pytest -m slow` — real `bge-m3` and reranker; both must already be in
  the Hugging Face cache
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` — lint, formatting and
  strict typing over `grepogram/` and `tests/` (`[tool.mypy] files`)
- `uv run grepogram --help`, `uv run grepogram-mcp` — the CLI and the MCP server
- all checks pass before every commit; CI (`.github/workflows/ci.yml`) runs them with
  `--group dev` only, `GREPOGRAM_FAKE_MODELS=1` and `HF_HUB_OFFLINE=1`

## Commit convention

Scoped Conventional Commits with a lowercase description: `feat(sync): fetch messages
incrementally`, `docs(readme): write setup and usage`, `test(mcp): assert stdout stays empty`.
The scope is required. One logical change per commit. No `Co-Authored-By` or other trailers, and
never change the git identity.

## Rules

- stdout is the MCP protocol. Never `print`. Log through `logging` (stderr plus a rotating file,
  `grepogram/log.py`); `typer.echo` only in `cli.py`, with `err=True` for diagnostics. `mcp.main()`
  and every tool body redirect stdout to stderr, and `tests/test_mcp.py` asserts stdout stays
  empty.
- Message text never reaches the log above DEBUG; pass it through `log.redact()`.
- Message mapping (`sync.map_message`) reads raw TL attributes only — `msg.message`, `msg.media`,
  `msg.reply_to`, `msg.fwd_from`, `msg.reactions`, `msg.from_id`, `msg.post`, `msg.date`,
  `msg.edit_date` — never the client-bound helpers (`msg.text`, `msg.file`, `msg.sender`,
  `msg.chat`), so a message built without a client maps exactly like one Telethon yields.
- Never `async with client` on a Telethon client (it calls `start()` and prompts on stdin); use
  `tg.connected(client)`.
- Every writer in `db.py` runs inside `db.transaction(conn)`. FTS and vec rows are keyed by the
  parent rowid (`messages.id`, `units.id`) and deleted by rowid, never by an UNINDEXED column.
- `config.toml`, the session file and the lock file are written with mode 0600
  (`config.write_private`, `tg.prepare_session`), directories with 0700.
- Files end with a single newline; no trailing blank lines.

## Environment variables

- `GREPOGRAM_HOME=<dir>` — every file (`config.toml`, `session.session`, `index.db`, `sync.lock`,
  `logs/`) under one directory. The `tmp_home` fixture in `tests/conftest.py` points it at a
  `tmp_path` subdirectory; tests must never touch the real `~/.config/grepogram`.
- `GREPOGRAM_FAKE_MODELS=1` — `embed.load_embedder` and `rerank.load_reranker` return
  `FakeEmbedder` (hashed bag of stems with a small RU/EN lexicon, 256-d) and `FakeReranker`. An
  autouse fixture sets it for every test; tests of the real loaders unset it themselves.

## Tests

- `tests/fakes.py` — `FakeClient` (async `get_dialogs`, `iter_messages` with Telethon's offset
  semantics, `get_entity`, raw requests such as `GetDialogFiltersRequest`) driven by in-memory
  fixtures, plus `make_*` builders for TL entities and dialogs.
- `tests/fixtures/tl.py` — real Telethon `types.Message` objects built without a client (text,
  caption with photo, voice, document with filename, reply, forum topic, forward, service message,
  reactions, channel post).
- `tests/fixtures/chat_ru.py` — a 62-message bilingual corpus over two chats, loaded through the
  real pipeline (`upsert_messages` → `rebuild_for_chat` → `index_chat`); `PARAPHRASE` names the
  pair only the dense side can connect.
- Model-layer tests (`tests/test_embed.py`, `tests/test_rerank.py`) inject stub modules with
  `monkeypatch.setitem(sys.modules, "torch", …)` and `"sentence_transformers"` (`None` simulates
  an `ImportError`), so they pass without the `dense` extra.
- `@pytest.mark.slow` tests load the real models and are excluded by default.
- Little Snitch on this Mac holds outbound connections from Python until a human answers, so an
  in-process Hugging Face download hangs unattended. Fetch the model files with `curl` into the
  `hf_hub_download` cache layout (`~/.cache/huggingface/hub/models--<org>--<name>/` with
  `blobs/<etag>`, `snapshots/<commit>/<file>` as relative symlinks into `blobs/`, `refs/main`) and
  run the slow tests with `HF_HUB_OFFLINE=1`.

## Layout

`grepogram/`: `paths` (file locations), `config` (TOML and `TEMPLATE`), `models` (dataclasses),
`db` (schema, migrations, accessors), `tg` (client, session, auth errors), `dialogs` (folders,
fuzzy matching), `sources` (targets, resolution, status), `sync` (fetch, mapping, lock, budget),
`units` (windows, threads, posts, incremental rebuild), `stem` (tokenizer, Snowball, FTS query),
`index` (FTS and vec maintenance, KNN), `embed` and `rerank` (protocols, fakes, bge models),
`links` (deep links, `open`), `filters` (chat specs, dates), `search` (retrieval, fusion, dedup,
readers), `cli` (typer app), `mcp` (FastMCP server with nine tools).

Plans live in `docs/plans/`, finished ones in `docs/plans/completed/`.
