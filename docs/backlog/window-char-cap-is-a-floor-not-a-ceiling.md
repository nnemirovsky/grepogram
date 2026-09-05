---
worth: yes
where: grepogram/units.py:cut_windows
added: 2026-09-05
---
# a window closes after exceeding window_max_chars, not before

`window_max_chars` is tested before the next message is appended, so a closed window holds at least
the cap plus whatever message carried it over. On a real 160k-message index the median window text is
1,274 characters against a 1,500 cap, but the tail reaches 3,453 — and 15.8% of a 400-window sample
exceed the embedder's 512-token cap, so their vectors represent only the first part of the unit while
FTS holds all of it.

Mitigated for now by `models.max_seq_length` (v1 ships 512; this machine runs 1024), which costs
~40% of the cross-encoder's throughput on every query. The cheaper fix is making the cap a real
ceiling — close the window *before* appending a message that would exceed it — which keeps units
inside 512 tokens and costs nothing at query time. It changes unit boundaries, so it needs a full
re-cut of every unit, not just a re-embed; that is why it was not done during v1.

Note the original diagnosis blamed Cyrillic tokenisation. Measured, that is wrong: 1500 characters of
Russian is ~399 tokens against ~418 for Latin. The cause is unit length, not script.
