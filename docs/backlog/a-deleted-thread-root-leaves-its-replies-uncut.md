---
worth: yes
where: grepogram/units.py:_invalidate_threads
added: 2026-09-05
---
# A deleted thread root leaves its surviving replies in no thread

Raised in the phase-4 review (below the bar) and recorded rather than fixed.

`_invalidate_threads` finds a thread through the units that quote it, takes the root from
`msg_ids[0]`, and rebuilds from that root down. When the root itself is the message that was
deleted, `stored.get(root_id)` is `None` and the loop `continue`s: the stale unit is returned and
dropped, and nothing replaces it. The replies are still there and one of them is now the head of
a thread of its own — `units.build_threads` would cut it on a full rebuild — but no incremental
path reaches them, so they stay searchable only through their windows until the chat is re-cut
for some other reason (a `RECIPE_VERSION` bump).

Reachable from both deletion paths, `sync._drop_deleted` and `sync.prune_deleted`, whenever the
message that goes is the one that started a reply chain.

The fix is to re-derive the root instead of giving up on it: with the old root gone, the
surviving replies of that thread form one or more new chains, and cutting them is what
`build_threads` already does given the right roots. The shape to work out is which of the
survivors are roots now — a reply whose own `reply_to_msg_id` no longer resolves to a stored row
— and to feed those to `build_threads` in place of the deleted one. A test that deletes the head
of a three-message chain and asserts the two survivors come back as a thread pins it.
