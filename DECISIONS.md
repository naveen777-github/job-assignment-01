# Engineering decisions

## Invariants identified

- Logical event identity is `(deviceId, bootId, sequence)` — sequence is only unique *within a boot*, not per device.
- Current-state ordering is lexicographic on `(generation, sequence)`; `deviceTime` is diagnostic metadata only and must never influence ordering decisions.
- `current_state` must never move backward relative to `(generation, sequence)`, regardless of delivery order or delay.
- A realtime `device.state.changed` message must be published if and only if a database commit actually changed `current_state` — never before the commit, never for a no-op.
- The raw audit table (`telemetry_events`) retains exactly one row per logical event, and rows are never mutated or deleted.
- WebSocket clients are independent of one another: the health, speed, or failure of one client must not affect delivery to any other, and per-client memory must be bounded.
- `/api/devices` is the sole source of truth after any WebSocket gap (first connect, reconnect, or a message that was never delivered) — the socket itself guarantees nothing.

## Incidents fixed

1. **Event identity across restarts** ([PR #1](https://github.com/naveen777-github/job-assignment-01/pull/1)) — `telemetry_events` had `UNIQUE(device_id, sequence)`, missing `boot_id`. Since sequence restarts at 1 on every boot, a new boot's early events silently collided with an older boot's rows and were dropped as "duplicates" by `INSERT OR IGNORE`. Fixed with a migration rebuilding the table under `UNIQUE(device_id, boot_id, sequence)`.
2. **Current-state ordering** ([PR #2](https://github.com/naveen777-github/job-assignment-01/pull/2)) — the `current_state` upsert compared `excluded.device_time > current_state.device_time`, directly contradicting "do not use deviceTime to decide current state." A delayed event with a later device clock could move state backward; a device with a skewed clock could have valid newer readings silently dropped. Fixed the `WHERE` clause to compare `(generation, sequence)`.
3. **Publish-before-commit** ([PR #3](https://github.com/naveen777-github/job-assignment-01/pull/3)) — `TelemetryService.ingest` computed a "preview" state and published it to WebSocket clients **before** calling the repository method that ran the actual DB transaction. Duplicates, stale/out-of-order events, and even a failing transaction all produced a successful-looking realtime update. Reordered to publish only after a successful, state-changing commit, using the committed state.
4. **Unbounded / blocking WebSocket fan-out** ([PR #4](https://github.com/naveen777-github/job-assignment-01/pull/4)) — `RealtimeHub.publish` awaited every client's `send_json` sequentially inside the same coroutine the HTTP ingest request awaited, so one slow or stuck client stalled delivery to all clients (and stalled ingestion itself), with no bound on how much could back up. Replaced with a bounded per-client queue and a dedicated sender task per client; a client whose queue overflows is dropped instead of blocking anyone else.
5. **No reconnect recovery** ([PR #5](https://github.com/naveen777-github/job-assignment-01/pull/5)) — the dashboard fetched `/api/devices` exactly once, at page load. A WebSocket that dropped and reconnected (network blip, server restart, laptop sleep) left the dashboard on stale state indefinitely, since WebSocket delivery isn't guaranteed. Fixed by refetching the snapshot on every successful reconnection.

## Design choices and trade-offs

- Fixed event identity at the schema level (a real `UNIQUE` constraint rebuild) rather than an application-level duplicate check, because the audit table's constraint is the actual enforcement point — an app-level check would race under concurrent duplicate delivery, which is exactly the scenario the protocol calls out ("the transport is at-least-once").
- SQLite can't `ALTER` a `UNIQUE` constraint in place, so migration_002 rebuilds `telemetry_events`. Rather than trying to preserve exact `AUTOINCREMENT` bookkeeping across the rebuild (fragile, SQLite-internals-dependent), the surrogate `id` column is renumbered in original insertion order. Audit content and relative order are fully preserved; only the internal `id` values change. See "Schema or API compatibility concerns" below.
- For WebSocket backpressure, chose one bounded `asyncio.Queue` + one sender task per client over a single shared broadcast queue (would still couple clients together) or unbounded buffering (violates the explicit memory-bound requirement in `docs/runtime-contract.md`). The limit defaults to 32 messages and is configurable via `WS_CLIENT_QUEUE_LIMIT`, following the existing `HOST`/`PORT`/`DATA_FILE` env-var convention already used in `__main__.py`.
- Removed `TelemetryRepository.preview_state` entirely instead of leaving it unused — it was the actual mechanism that caused the publish-before-commit bug, and an unused method that reintroduces the same shape of bug is a liability, not a convenience.
- Left `list_events` ordering (`ORDER BY id DESC`) untouched; since `id` is reassigned in original insertion order during the migration, "newest received first" semantics are preserved across the rebuild.

## Schema or API compatibility concerns

- `migration_002` rebuilds `telemetry_events` and reassigns `id` values. Nothing in this repo depends on specific `id` values surviving a migration — `current_state` keys on `(device_id, metric)` and references boots via `(device_id, boot_id)`, never via `telemetry_events.id`. An external consumer that cached raw `id` values across a migration would see them change; this is called out here since it's the one schema change with a (theoretical) compatibility edge.
- All HTTP/WebSocket request and response shapes are unchanged: `/api/boots`, `/api/telemetry`, `/api/devices`, `/api/events`, and the `device.state.changed` WebSocket message keep every documented field, with the same meaning.
- Added one new environment variable, `WS_CLIENT_QUEUE_LIMIT` (default `32`), and one new optional `create_app(..., websocket_client_queue_limit=...)` parameter. Both are additive with safe defaults — no existing call site breaks.
- Behavior change (intentional, required by `docs/runtime-contract.md`): a client slow enough to overflow its send queue is now disconnected by the server, where previously it would have been kept indefinitely at unbounded memory cost. A legitimate dashboard already retries its connection every second (`app.js`), so the visible effect is a brief "Realtime disconnected" flash followed by an automatic reconnect and snapshot refetch, not silent memory growth on the server.

## Remaining risks or incomplete work

- No automated test exercises the dashboard's reconnect *trigger* itself (the `hasConnectedBefore` branch in `app.js`) — this repo has no JS test harness (no `package.json`, no bundler, no test runner), and introducing one felt disproportionate to a single-behavior frontend fix in an otherwise Python-backend-focused assignment. Verified instead via `node --check` (syntax), code review, and a live run of the server plus `simulator.py --chaos`; the server-side half of the contract (the snapshot endpoint staying authoritative across a WebSocket gap) is covered by an automated API test. See `AI_USAGE.md` for the verification detail.
- `WS_CLIENT_QUEUE_LIMIT=32` is a reasonable local-dev default but untuned; a real deployment would size it from expected client count and message rate rather than a flat constant.
- The migration path is forward-only (no down-migration), consistent with the pattern already established by `migrations.py` in the starter repo. Acceptable for a local single-user tool; would need revisiting before any shared/multi-environment use.
- No rate limiting or authentication was added to any endpoint — out of scope per the assignment's engineering constraints (local-only, no cloud, don't replace the framework).
- All database access is serialized through a single `RLock` plus SQLite's own `BEGIN IMMEDIATE`, which is correct but means write throughput is bounded by single-writer SQLite semantics. Not observed as a problem at the scale exercised here (4 simulated devices, chaos mode); would need revisiting at a much larger device count or write rate.
