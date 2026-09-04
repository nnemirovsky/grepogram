# grepogram

grepogram is a local search engine over the Telegram chats you opt in to, exposed to
Claude Code through MCP (plus a thin CLI). It syncs messages through the Telegram user API
(Telethon), stores them in a single SQLite file, groups them into conversation-level units
(time windows, reply threads, channel posts), indexes those units lexically (FTS5 with
Russian/English stemming) and densely (sqlite-vec with `bge-m3` computed on the local GPU),
fuses both rankings with Reciprocal Rank Fusion, reranks with a local cross-encoder, and
returns hits with deep links that open the original message in Telegram. Nothing leaves the
machine: there is no API token, no hosted service, and embeddings are computed locally.

## Local model throughput

Measured with `uv run pytest -m slow` on an Apple M1 Pro (16 GB) with both models already in
the Hugging Face cache (`HF_HUB_OFFLINE=1`), fp16 on MPS, `max_seq_length = 512`:

| model | work | throughput |
|---|---|---|
| `BAAI/bge-m3` | embedding window-sized units (64 units of six lines, batch 32) | 40.9 units/s |
| `BAAI/bge-reranker-v2-m3` | scoring `(query, unit)` pairs (`rerank_top = 40`) | 37.7 pairs/s |

Loading takes about 8 s for the embedder and 3.5 s for the reranker, once per process. At these
rates a query has its 40 candidates reranked in about a second, and 10 000 units embed in about
four minutes.

Work in progress. Licensed under MIT.
