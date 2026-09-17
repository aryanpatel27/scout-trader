# Postmortems

## 1 · Retry storm after a crashed order response (2026-09-10)
**What happened.** The 3:55 close-out of a real position filled at the broker, but the
execution harness died before printing its JSON response. The engine read "no output"
as "rejected", kept the local position, and retried the sell every 2 seconds all
night — ~2.6 MB of identical log lines against a position that no longer existed.
**Why.** The subprocess wrapper treated empty stdout as failure without surfacing
stderr; no backoff; the local mirror had no reconciliation against broker truth.
**Fixes.** stderr captured and reported; per-ticker 120s backoff after any failed live
order; equity exits skip outside regular hours; a reconciler deletes local positions the
broker no longer holds (>3 min old, never practice-wallet rows).

## 2 · Overnight hold after a stalled quote feed (2026-09-14)
**What happened.** Two afternoon entries were never flattened at 3:55 — zero engine
events from 3:40 to 4:15 — and were sold at the next open; one had gapped down (−4.3R).
**Why.** The quote poller used per-ticker calls with no timeout; a hung socket froze the
thread, quotes went stale, and `manage_positions` silently `continue`d on "no fresh
quote" — even for a time-based exit that needs no price.
**Fixes.** Flatten proceeds on the last known price when the quote is stale (with a
warning); any open position without a fresh quote raises an error event; an after-hours
extended-hours LIMIT exit sells anything still held after the close; the poller became a
single batched call with a 10s timeout plus a heartbeat; a supervisor relaunches it.
**Lesson.** Silence is not success — every "skip" path on a real position must be loud.

## 3 · Half-dead process from file-descriptor exhaustion (2026-09-17)
**What happened.** The dashboard stopped answering ("connection reset by peer"), DNS lookups
failed, and the decision journal could not be written — while the engine loop kept ticking.
**Why.** launchd starts services with a 256-descriptor soft limit. Two leaks filled it in about
two days: threaded `yfinance.download(threads=True)` opened one never-closed handle to yfinance's
sqlite timezone cache per worker thread (79 handles), and the on-chain JSON-RPC client read
`urlopen(...)` responses without closing them, stranding sockets in `CLOSE_WAIT` (74).
**Fixes.** Responses are closed with a context manager; batch downloads run unthreaded; the
process raises its own soft limit to 4096 at startup; a watchdog samples `/dev/fd` every minute
and, above 80% of the limit, logs an error and exits so launchd relaunches a clean process (all
state lives in SQLite). Practice positions whose price feed dies are closed after 30 minutes
instead of holding their slot forever.
**Lesson.** A process that is "up" is not necessarily working — health needs to be measured at
the edges users touch (HTTP, DNS, disk), and self-restart beats limping.
