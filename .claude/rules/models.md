---
description: How the embedding and reranking models load.
paths:
  - "grepogram/embed.py"
  - "grepogram/rerank.py"
  - "tests/test_embed*.py"
  - "tests/test_rerank*.py"
---

# Model loading

- A model loads from the Hugging Face cache and nothing else. Both `BgeM3Embedder` and
  `BgeReranker` go through `embed.load_cached_first(load, what)`, which calls the
  sentence-transformers constructor with `local_files_only=True` and retries with the network
  only when `embed.not_cached` recognises the failure — transformers re-raises huggingface_hub's
  `LocalEntryNotFoundError` as a plain `OSError` about the connection, so the match walks
  `__cause__` / `__context__` and compares class *names*: huggingface_hub belongs to the `dense`
  extra and nothing outside it may be imported here. That retry is the first download and is
  logged at INFO; `HF_HUB_OFFLINE` set skips it, and every failure still reaches the caller as
  `ModelUnavailable`. Never call `SentenceTransformer` / `CrossEncoder` directly: the round trip
  they make for an already-cached model costs about 8.7 s per `grepogram search` on a reachable
  network and minutes behind a firewall that holds connections open.
