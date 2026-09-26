# CLAUDE.md

Inbound Green Line ETAs for Magoun Square, with error bars, and a push that says
when to leave. The MBTA feed only reaches 8–13 min ahead on the downtown-bound
platform; that gap is the whole project.

- **[README.md](README.md)** — how to run it, what the history says.
- **[launch-plan.md](launch-plan.md)** — phases, decisions, measured constants,
  known traps. Read before re-deriving anything; most questions are answered there
  with a number.

## Commands

```bash
uv run --with polars python src/server.py 8723   # /board, /status, /history, /api
./src/watch.sh plan park 09:00                   # arm the notifier
./src/q.sh "SELECT ... FROM pairs"               # ad-hoc SQL over the archive
uv run --with pytest --with numpy --with polars python -m pytest tests/ -q
./ops/install.sh                                 # (re)load the launchd agents
```

`ops/install.sh` is idempotent and must not be run with `sudo`.

## Invariants — breaking these fails silently

1. **`trip_id` is reassigned** across the terminus turnaround and mid-run. Link
   trains by `vehicle_id`, or by walking a vehicle's day in time order. Analyses
   keyed on `trip_id` miss real behaviour (they missed express running entirely).
2. **Count arrivals as transitions into `STOPPED_AT`.** Keying on
   `(vehicle, stop)` per day records only each vehicle's first visit — a measured
   30% undercount.
3. **A null never means "unchanged".** `veh`, `dep`, `unc` are legitimately null,
   so any delta encoding needs an explicit changed-field mask. This corrupted
   2,088 of 5,738 snapshots before it was caught.
4. **Verify round-trips on the whole object.** A narrow check over four fields
   passed while losing 1,595 rows. `archive.py` / `rollup.compact_parquet` only
   delete the original after a full-snapshot comparison.
5. **Schema changes must be additive.** `rollup.py` reads archives written by
   older code, and raw is pruned at 90 days.
6. **The archiver is the one process that must not lose data.** It writes plain
   appendable JSONL; compaction happens later on closed days. Do not make it
   clever.
7. **Filter non-revenue and stale vehicles.** Deadheads run express and parked
   trains sit for hours with a frozen `updated_at`; both otherwise satisfy the
   no-show veto. `revenue` exists in the v3 API and **not** in the protobuf feed.
8. **Timezone must be `ZoneInfo`**, never a fixed offset. EDT→EST flips
   2026-11-01.

## Conventions

- **polars** for anything that ships or is tested; **DuckDB** (`src/q.sh`) for
  ad-hoc questions only — it must stay out of the deploy path.
- **Model as data.** `data/model.json` holds every fitted quantile so consumers do
  lookup and arithmetic, not modelling. This is what keeps a future JS frontend
  from becoming a second implementation.
- **The rider's metric is platform wait, not prediction error** — and platform wait
  alone rewards dawdling, so report door-to-train alongside it.
- Quote a **low quantile**, not the median: median headway is 8.8 min, so a minute
  optimistic costs nine.

## Gotchas that cost real time

- `launchctl bootout` is **asynchronous**; bootstrapping too soon fails with
  `5: Input/output error` and leaves the agent stopped.
- ntfy rejects `?since=now` with **HTTP 400**; subscribe with no `since`. Its
  reconnect loop must log, or a dead channel looks exactly like silence.
- MBTA predictions **flap ~8 min for ~90 s**. Debounce longer than the flap, and
  never widen a re-match far enough to reach the next train (headway 528 s).
- `cdn.mbta.com/*.pb` has **no CORS** and is the only source of `SKIPPED`
  markers. `api-v3.mbta.com` has `access-control-allow-origin: *`.
- The v3 `/schedules` endpoint only serves ~8 days back. Daily snapshots are the
  only way to keep them; an un-captured day is gone.

## Scope

One rider, one platform, one direction. Prefer measuring to arguing — nearly every
belief in this repo that went unmeasured turned out wrong, including several of
mine. When a claim matters, check it against `data/` before writing it down.
