---
worth: no
where: grepogram/units.py:_apply
added: 2026-09-05
---
# _apply would keep one of two stale units sharing a content key

Raised in the phase-4 quality review and declined there. `_apply` matches an existing unit by its
content key and keeps it; if two stale rows ever shared one key, only one would be replaced and the
other would linger.

No current builder can produce two units with the same content key in the same chat and topic —
windows partition the id space and threads are keyed by their root — so the state is unreachable
rather than merely unlikely. Kept so the next review of this function does not rediscover and
re-argue it; delete this file if a builder is ever added that could produce a collision.
