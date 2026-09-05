# Contributing

Issues and pull requests are welcome. This file is what a change has to satisfy before it can be
merged; the invariants a reviewer will actually check it against live in [CLAUDE.md](CLAUDE.md),
which is written for coding agents and doubles as the contributor guide.

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/) only — no `pip`, no global installs.

```sh
git clone https://github.com/nnemirovsky/grepogram
cd grepogram
uv sync --managed-python --all-extras --all-groups
```

`--managed-python` is not decoration. sqlite-vec is a loadable SQLite extension, and uv would
otherwise build the environment on whichever `python3.12` it finds first — python.org's macOS
build and Apple's system Python are compiled without `--enable-loadable-sqlite-extensions`, so
`db.connect` refuses them with `ExtensionsUnsupported`. CI sets `UV_MANAGED_PYTHON: "1"` for the
same reason.

`--all-extras` brings `dense` (torch and sentence-transformers, plus about 4.5 GB of models on
first use) and `media` (pypdf, python-docx, and pyobjc's Vision bindings on macOS).
`--all-groups` brings the `dev` group the suite needs. CI installs neither extra — only
`uv sync --locked --group dev` — which is why every test runs against fakes and why anything the
tests import has to be in `dev` as well as in its extra.

`uv.lock` is committed and CI installs with `--locked`. After touching dependencies, run
`uv lock` and commit the lockfile in the same commit.

## The four gates

All four pass before every commit. CI runs them on macOS and repeats the last three on Ubuntu:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

`mypy` runs `strict` over both `grepogram/` and `tests/` — test code is held to the same typing
as the package. `ruff` is configured at 100 columns with the `E`, `F`, `I`, `UP` and `B` rule
sets.

The slow suite is separate and deselected by default. It loads the real `bge-m3` embedder and the
real `bge-reranker-v2-m3` cross-encoder, so both must already be in the Hugging Face cache:

```sh
HF_HUB_OFFLINE=1 uv run pytest -m slow
```

Run it after any change to embedding, reranking or the index. If a model is missing from the
cache, fetch it first — `HF_HUB_OFFLINE=1` is what keeps the run from reaching the network on its
own.

## Tests never reach the network

No test may talk to Telegram, to Hugging Face, or to the filesystem outside its own `tmp_path`.
This is not a style preference: it is what lets the suite run on a fresh checkout with no Telegram
account, no API keys and no models downloaded.

- `tests/fakes.py` holds `FakeClient`, driven by in-memory fixtures, and the builders for the
  Telethon entities it yields. A feature that talks to Telegram grows `FakeClient` rather than a
  mock of its own call site.
- `tests/fixtures/tl.py` builds real `telethon.types.Message` objects with no client attached, so
  message mapping is exercised against the same shapes Telethon produces.
- An autouse fixture sets `GREPOGRAM_FAKE_MODELS=1`, so `embed.load_embedder` and
  `rerank.load_reranker` return the fakes. Tests of the real loaders unset it themselves and stub
  `torch` / `sentence_transformers` through `sys.modules`.
- The `tmp_home` fixture points `GREPOGRAM_HOME` at a `tmp_path` subdirectory, so no test touches
  `~/.config/grepogram`.
- CI sets `HF_HUB_OFFLINE=1` for the whole run.

Every code change carries tests: new functions and modified ones, the success path and the failure
path. A behaviour change updates the cases that pinned the old behaviour rather than adding a
second set beside them.

## Commits

Scoped Conventional Commits with a **lowercase** description:

```
feat(sync): fetch messages incrementally
fix(units): cut a window before it exceeds the char cap
docs(readme): write setup and usage
test(mcp): assert stdout stays empty
```

The scope is required. One logical change per commit. No `Co-Authored-By` or other trailers.

## Things that will fail review

- `print`. stdout is the MCP protocol. Log through `logging`; `typer.echo` belongs to `cli.py`
  alone, with `err=True` for anything diagnostic.
- Message text in the log above DEBUG. Pass it through `log.redact()`.
- A version string anywhere but `grepogram/__init__.py`, which hatch and `grepogram --version`
  both read.
- A schema change that edits `_V5`. The base schema is frozen now that v0.1.0 is tagged; append a
  migration step above `db.BASE_VERSION` instead.
- `async with client` on a Telethon client — it calls `start()` and prompts on stdin. Use
  `tg.connected(client)`.
- A writer in `db.py` outside `db.transaction(conn)`.
- A file that ends with a trailing blank line, or without a newline.

## Reporting a bug

Open an issue with the bug report template and fill in the version, the macOS version and which
optional extras are installed — most reports turn on one of those three. `grepogram config path`
prints where the config, session, index and log live; the log is the useful attachment.
