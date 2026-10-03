---
description: Shared fixtures, fakes and test conventions.
paths:
  - "tests/**"
---

# Tests

- `tests/conftest.py` — the fixtures every module shares: `fake_models`, `plain_cli_output`
  (`TERM=dumb`, so rich prints option names whole), `no_terminal` and `clean_logging` (all
  autouse), `tmp_home`, `conn` (an in-memory index, migrated), `v6_conn` (an empty index at
  schema 6, as v0.2.0 left it, for the upgrade tests) and the `file_mode` helper.
  A module that needs more overrides `conn` by requesting it (`tests/test_filters.py`).
- `tests/fakes.py` — `FakeClient` (async `get_dialogs`, `iter_messages` with Telethon's offset
  semantics, `get_messages(entity, ids=…)`, `get_entity`, `download_media(message, file)`, raw
  requests such as `GetDialogFiltersRequest`) driven by in-memory fixtures, plus `make_*` builders
  for TL entities and dialogs. Two of those answers are signals the product reads, not
  conveniences: a list `ids` answers one slot per id and `None` where a message is gone, which is
  what `sync.prune_deleted` and `media.run` both key on, and the `downloads=` mapping keyed by
  `(chat_id, msg_id)` is what a download writes (bytes, or an exception to raise). Messages it
  yields carry `msg.forward` bound to the origin entity this account sees (its access hash, and
  the origin learned, as Telethon learns an answer's `chats`), and `iter_messages(filter=
  InputMessagesFilterPinned)` answers only the `pinned` ones — any other filter is refused.
  **`FakeClient` refuses a peer it has not learned**, like the real one: `entities=` is the world
  and `resolved` is the session cache, which starts empty and is filled by `get_dialogs()`, by a
  successful `get_entity`, and by the `chats` / `users` of any raw answer — Telethon's
  `session.process_entities`. Addressing an unlearned id raises `ValueError: Could not find the
  input entity` (a legacy `PeerChat` id needs no access hash and is allowed, as in Telethon),
  and that includes `get_entity(<marked id>)`: no path of the fake resolves a bare id the real
  client could not. `session.process_entities` with `InputPeer*` objects seeds a hash, which
  addresses the peer only when it is the account's own.
  `forget_entities()` models the fresh client `extract` and `prune-deleted` each build after a
  sync, and `strict_entities=False` is for a test with no realistic route to warm up. A fake more
  permissive than production is a fake that hides bugs: this one hid a `grepogram extract` that
  resolved no chat at all on a real account through nine review rounds. Several accounts are
  several `FakeClient`s over one `FakeWorld` (`FakeWorld.client(account, members=…)`), with
  per-account membership, private-chat histories and access hashes: a hash that is not the
  account's own is refused, a private channel refuses a non-member, and a channel an account is
  not in comes back `left`. The world also carries invites, shared folders and the global-search
  answers research probes.
- `tests/fixtures/two_accounts.py` — two accounts' chats in one index: a channel both reach and
  its link-only discussion group, and each account's private chat with the same person (the
  work one on a synthetic row id, message ids colliding).
  `tests/test_upgrade.py` builds a v0.2.0 home (schema 6, one `session.session`, imports) from
  what the CLI writes and checks it migrates, syncs and searches unchanged.
- `tests/fixtures/tl.py` — real Telethon `types.Message` objects built without a client (text,
  caption with photo, voice, document with filename, reply, forum topic, forward, service message,
  reactions, channel post, hyperlink, mention, URL button, channel-post forward).
- `tests/fixtures/sample.pdf`, `tests/fixtures/sample.docx` — tiny hand-built documents the
  extraction tests round-trip; `tests/fixtures/tdesktop_export.json` — a Telegram Desktop export
  the import tests parse.
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
