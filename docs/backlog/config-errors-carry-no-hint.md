---
worth: yes
where: grepogram/config.py
added: 2026-09-05
---
# a config error reaches the agent without a hint

Every other expected failure returns `{error, hint}`; `ConfigError` returns `hint: null`. Seen live
when a running MCP server met a config written by a newer build:

    {"error": "…/config.toml: unknown key: models.max_seq_length", "hint": null}

Accurate, but the reader is left to work out that the server is stale and a restart fixes it. An
unknown key in a section the build knows should hint at exactly that; a malformed value should point
at the template. Cheap and self-contained.
