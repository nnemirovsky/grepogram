---
worth: yes
where: grepogram/sync.py:on_chat_synced
added: 2026-09-05
---
# An edited comment never reaches the channel's post thread

Found while auditing every unit-rebuilding path for the `comment_of_*` follow-through that
`media._recut` was missing (external review, MAJOR 3). The extraction path is fixed; this
sibling is not.

`on_chat_synced` → `units.rebuild_for_chat` rebuilds only the units of the chat whose rows
changed. A channel's post thread quotes its comments' rendered lines while listing the post
alone in `msg_ids`, so no `json_each` over `units.msg_ids` reaches a comment id and the
discussion group's own rebuild cannot touch that thread. `sync._invalidate_comment_posts` is the
only route in, and this path does not take it.

Reproduced: a discussion group synced as a source of its own, one comment on post 10 edited from
"old text" to "new text". The group's window becomes `new text`; the channel's thread keeps `old
text`, and no `indexed` flag is left that would repair it. `_refresh_comments` covers the
neighbouring case — a post whose thread *grew* is flagged `indexed = 0` before its comments are
read — so only an edit (or a reaction-free text change) inside an already-stored thread falls
through.

Not fixed with MAJOR 3 because the obvious fix is the wrong shape for this path. Calling
`_invalidate_comment_posts` from `on_chat_synced` puts a second chat's units under the hottest
rebuild in the codebase, and `units._cut_posts` deletes a channel's post threads whenever
`units.comments_enabled` is false for it — which is the case for any channel whose source is not
configured with `comments`, including one no longer in the config at all. A group synced as its
own source would then drop that channel's threads on every sync.

The shape that fits is the one `_refresh_comments` already uses: flag the channel's post rows
`indexed = 0` (`db.mark_unindexed`) and let the channel's own rebuild — where the
`comments_enabled` question belongs — do the work, with `sync.index_stranded` picking up a
channel no source and no link leads to. That needs one query from the changed rowids to the
posts they comment on, and a test that a group sync does not touch a channel whose comments are
switched off.
