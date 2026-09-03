# grepogram

grepogram is a local search engine over the Telegram chats you opt in to, exposed to
Claude Code through MCP (plus a thin CLI). It syncs messages through the Telegram user API
(Telethon), stores them in a single SQLite file, groups them into conversation-level units
(time windows, reply threads, channel posts), indexes those units lexically (FTS5 with
Russian/English stemming) and densely (sqlite-vec with `bge-m3` computed on the local GPU),
fuses both rankings with Reciprocal Rank Fusion, reranks with a local cross-encoder, and
returns hits with deep links that open the original message in Telegram. Nothing leaves the
machine: there is no API token, no hosted service, and embeddings are computed locally.

Work in progress. Licensed under MIT.
