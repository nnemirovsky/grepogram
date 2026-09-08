---
worth: yes
where: grepogram/media.py:_extract_batch
added: 2026-09-08
---
# a message deleted in Telegram is parked as retryable, not as gone

`media.run` re-fetches every pending row by id before downloading it. When Telegram answers with
nothing for an id — the message has been deleted since it was indexed — the row is classified
`MEDIA_FAILED`, which means *retryable*. It never becomes retryable: a deleted message cannot be
fetched again, so the row sits in the queue for good and every `extract --retry-failed` re-attempts
it.

Found on a live index: 37 photos in one chat failed on three consecutive runs, and a direct probe
showed `client.get_messages(chat_id, ids=[…])` returning nothing for each — they had been deleted.
`prune-deleted` removes them correctly, because there an empty slot for a requested id *is* the
deletion signal. `media.run` reads the same answer and draws a weaker conclusion from it.

The fix is small, and the interesting part is which of two shapes it should take. Either the pass
learns the same rule as `sync.prune_deleted` (an empty answer for a requested id means gone) and
deletes the row through `db.delete_messages` + `units.invalidate_units_for`, which makes `extract`
a second deletion detector and needs the `comment_of_*` follow-through `_invalidate_comment_posts`
gives; or it parks the row in a terminal state that says "the message is gone, `prune-deleted` owns
it" and leaves the removal to the pass that already does it properly. The second is smaller and
keeps one owner for deletions, which is the reason to prefer it — but it needs a new `media_state`
value, so it is a schema-adjacent decision rather than a one-line change.
