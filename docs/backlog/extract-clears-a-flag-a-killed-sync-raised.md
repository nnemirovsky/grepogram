---
worth: yes
where: grepogram/media.py:_recut
added: 2026-09-05
---
# extract clears an `indexed` flag a killed sync raised for a different reason

Raised in the phase-4 review (below the bar) and recorded rather than fixed.

`media._recut` ends with `db.mark_indexed(conn, [row_id for row_id in row_ids if row_id not in
stranded])`, and `stranded` is only what `units.uncut_rows` names — a `(chat, topic)` that holds
no window at all. Every other row of the batch has its flag cleared, whether this pass raised it
(`db.set_media_text`) or a sync that was killed before its rebuild did.

The two are not the same work. `units.invalidate_units_for` re-cuts the windows a message sits
in and, through `_invalidate_threads`, the thread units that *already quote* it — it does not
build a thread that was never cut, which is exactly what the killed sync's
`units.rebuild_for_chat` was going to do. So for a row flagged before the extraction, the
windows come out right and a pending reply-thread build is silently dropped: the flag that was
the only record of it is gone, and `sync.index_stranded` never sees the chat again.

Needs a crash inside a first sync's rebuild followed by `grepogram extract` before the next
`sync`, on a chat holding a reply thread — narrow, which is why it is here and not in the
release.

The fix is to keep the flag raised for the rows that arrived with it already down, not only for
`uncut_rows`: read `messages.indexed` for the batch before `set_media_text` writes anything, and
subtract those rowids from the `mark_indexed` argument the way `stranded` is subtracted now.
That leaves them for `index_stranded`, whose `on_chat_synced` does the full rebuild the killed
sync owed. One extra read per batch, and a test that a row flagged before the pass is still
flagged after it while the extracted text is in place.
