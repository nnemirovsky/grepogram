---
description: How index.db migrations are numbered, decided and appended.
paths:
  - "grepogram/db.py"
  - "tests/conftest.py"
  - "tests/test_db.py"
  - "tests/test_upgrade.py"
---

# index.db schema and migrations

- `db.MIGRATIONS` maps a schema version to the step that brings a database to it, and `_V5` —
  keyed by `db.BASE_VERSION` — is the whole schema as the code queries it. The numbering starts
  at 5 because it is an identity, not a count: development builds walked a database up through 1,
  2, 3 and 4, and a number one of them also wrote could not say a dev index apart from a finished
  one — `schema_version = 1` on the old chain names a `messages` table with no `indexed` and no
  `comment_of_*`. `db.migrate` decides every database explicitly, never by falling through: a
  file with no schema objects gets the whole schema and the version, one at `SCHEMA_VERSION` is
  used as it is, one between `BASE_VERSION` and `SCHEMA_VERSION` that `MIGRATIONS` holds every
  step for is walked up, and everything else — tables with no recorded version, a newer version,
  anything below `BASE_VERSION` (the dev chain's 1 through 4), a version no chain of steps
  reaches, a version `db.schema_version` cannot read at all — raises `SchemaError` telling the
  user to delete `index.db` and sync again. That last one is why `schema_version` classifies what
  it reads: a `meta` table of another program's shape and a recorded version that is not a number
  are `SchemaError`, so the handlers in `cli._load` and `mcp.main` give the rebuild hint instead
  of a traceback, while a locked or unreadable file keeps raising its own `sqlite3` error — it is
  not a schema this code can classify. `MIGRATIONS` has to run from `BASE_VERSION` to
  `SCHEMA_VERSION` without a gap and `db._missing_steps` is where both paths check it: a
  mis-keyed step is a bug in grepogram, and stamping an empty database at a version every
  existing one is refused at would hide it. No step transforms rows on a guess or rewrites a value
  already stored: an index is derived from Telegram and a rebuild costs one sync. What a step
  *may* do is **fill the columns and tables it adds, deterministically from values already
  stored**, because an imported history is not derived — Telegram cannot serve it again — so
  "delete index.db and sync again" is no upgrade path a released schema may ask for. Step `7`
  fills `chats.peer_id = id`, `chats.scope` from `type`, `chat_sources` from `source_id` and
  `chat_access` for `default` (imports excepted: they reach no account); step `8` records
  `meta['links_captured_from']`, the first row id whose links are captured; step `9` turns that
  mark into the per-row `messages.links_read` (a row with links or a forward origin is read, an
  import's rows are not) and starts the lead clock. On an empty database those fills run over no
  rows. Step 9 also gives the index a random `meta['index_id']` — meta naming the file, not data
  — so a cursor kept outside the index can tell a rebuilt index from this one. **The schema is append-only now that v0.1.0 is tagged** —
  append a step above `BASE_VERSION` and leave `_V5` alone. `migrate()` applies every step from
  `BASE_VERSION` for a database with no schema objects, so a column added to `_V5` *and* to a step
  above it raises `duplicate column name` and fails `tests/conftest.py`'s shared fixture, which is
  the whole suite. v0.2.0's step `6` (`messages.extracted_text`, `messages.media_state`,
  `units.reactions`, the `messages_media_pending` partial index) is the model, and steps `7`
  (accounts: `accounts`, `chats.peer_id` / `scope` unique together, `chat_access`,
  `chat_sources`), `8` (`messages.fwd_peer_id` / `fwd_msg_id` / `fwd_date`, `message_links`)
  and `9` (`messages.links_read` / `lead_seq`, `peer_cache`) followed it; `RECIPE_VERSION` did
  not move for any of them, unit text being unchanged. The media
  index's predicate carries `media_kind IS NOT NULL` as well as `media_state = 0`, or it would
  cover every row in the table forever and the pending query would stay a scan.
