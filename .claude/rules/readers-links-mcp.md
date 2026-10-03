---
description: Display links, message views and the MCP server.
paths:
  - "grepogram/links.py"
  - "grepogram/search.py"
  - "grepogram/mcp.py"
  - "tests/test_links.py"
  - "tests/test_readers.py"
  - "tests/test_mcp.py"
---

# Links, readers and the MCP server

- `links.message_url` returns the form to show and cite in `Link.url` — `https://t.me/…` where
  Telegram has one, `tg://openmessage?…` for the private chats and legacy groups that have none —
  and `Link.fallback_url` carries `tg://user?id=` for a DM, which is what a desktop client
  actually opens. Both are display links; nothing consumes them but the reader. The bare channel
  id a `t.me/c/` link needs comes from `telethon.utils.resolve_id`, never from string surgery on
  the `-100` prefix: the mark is arithmetic (`-(1000000000000 + id)`), so a channel id below ten
  digits leaves zeros right behind that prefix and any lexical rule either swallows them or
  refuses the id — which took `search`, `thread` and `context` down for the whole chat.
  `sources.parse_target` builds the same mark arithmetically for `t.me/c/<id>`, so such ids reach
  the index by the front door. Every form is built from `chat.peer_id`, never the row id, which
  for a scoped chat may be synthetic.
- A `MessageView` names the chat it is in (`chat_id`, with `peer_id` and the `accounts` that
  reach it beside it, as on a `Hit`), because a list of them can span two:
  `search.thread` follows a channel post with its discussion group's comments, and post ids and
  comment ids both number from 1, so `msg_id` alone names two different messages. The top-level
  `chat_id` of `mcp._messages_result` is the argument, not where every message lives; the tool
  docs, the server `INSTRUCTIONS`, README and the plugin skills (`plugin/skills/*/SKILL.md`) say
  to pass a message's own `chat_id` back.
- `mcp` stays `<2`: `grepogram/mcp.py` targets the 1.x `FastMCP` API (2.x renamed it).
- Never instantiate `FastMCP` at module level; `mcp.build_server()` runs after `setup_logging()`
  because `FastMCP.__init__` calls `logging.basicConfig`, and `main()` lowers the `mcp` logger to
  WARNING (the lowlevel server logs every request at INFO).
