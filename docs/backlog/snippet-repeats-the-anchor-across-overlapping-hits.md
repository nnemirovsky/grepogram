---
worth: later
where: grepogram/search.py:snippet
added: 2026-09-05
---
# a window and a thread over the same conversation show the same snippet

Both units lead with the same anchor line, so two hits look like duplicates even when their bodies
differ. Measured over five real queries: an overlapping pair sharing 2 of 6 message ids — the thread
carried four messages the window did not — read as a copy of the hit above it and cost a slot in the
top k.

Not a dedup problem: `dedup_overlap = 0.5` is correct, and lowering it to 0.3 would delete the unique
content. The fix is presentational — when a hit's anchor already appeared in a better hit, lead its
snippet with the highest-scoring line the reader has not seen. What is unresolved is whether that is
worth the complexity or whether the second hit should simply be dropped and its extra messages folded
into the first; deciding needs a few weeks of real use to say which reads better.
