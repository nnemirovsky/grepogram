---
worth: no
where: grepogram/sync.py:_joined_to_thread
added: 2026-09-05
---
# _joined_to_thread calls abort() for a plain job failure, not just cancellation

Raised in the phase-4 quality review and declined there. The `except BaseException` that exists to
propagate a cancel scope also runs `abort()` when the worker simply raises.

Harmless as written: the only abort in use is `SyncBudget.cancel()`, and it runs at the end of a run
whose remaining work is being discarded anyway, so cancelling a budget nothing will consult changes
nothing. Kept so the next reader of this handler does not re-argue it; it becomes real only if an
abort with side effects beyond the current run is ever added.
