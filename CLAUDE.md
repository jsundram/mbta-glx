# CLAUDE.md

Inbound Green Line ETAs for Magoun Square, with error bars, and a push that says
when to leave. The MBTA feed only reaches 8–13 min ahead on the downtown-bound
platform; that gap is the whole project.

- **[README.md](README.md)** — how to run it, what the history says.
- **[launch-plan.md](launch-plan.md)** — phases, decisions, measured constants,
  known traps. Read before re-deriving anything; most questions are answered there
  with a number.
- **[architecture.md](architecture.md)** — the buildout: tiers, data contracts,
  milestones M1–M5. Start here for "what do I build next".

## Commands

```bash
# /board, /status, /history, /api, /skips. gtfs-realtime-bindings is not optional:
# without it /skips answers {"as_of": 0, "trips": []} forever, which at the board
# is indistinguishable from a day with no skipped trains. Stop com.magoun.server
# first, or this loses the port race and the agent crash-loops on 8723.
uv run --with polars --with gtfs-realtime-bindings python src/server.py 8723
./src/watch.sh plan park 09:00                   # arm the notifier
./src/q.sh "SELECT ... FROM pairs"               # ad-hoc SQL over the archive
uv run --with pytest --with numpy --with polars python -m pytest tests/ -q
node tests/run_cases.js data/model.json tests/fixtures/cases-*.json   # the JS side
./ops/install.sh                                 # (re)load the launchd agents
```

The publish pipeline (M3). The archive is gitignored and lives here, not in the
repo, so everything that reads it runs on this host; CI only runs the suite.

```bash
./src/daily.sh                                   # rollup, score, suite, origin check
uv run --with polars --with numpy python src/stats.py    # score closed days -> stats.json
uv run python src/publish.py --check             # is web/ complete and current?
uv run python src/publish.py --commit            # commit what moved; never pushes
./src/refit.sh                                   # rebuild dataset, refit, diff, suite
./src/refit.sh --fixtures                        # then, deliberately; read the row diff
uv run python src/rating.py --check              # has the schedule rating moved?
```

`ops/install.sh` is idempotent and must not be run with `sudo`.

## Checks the suite cannot do

Neither is collected by pytest, both need the real world, and both exist because
the things they catch are invisible to reading. Run them when you touch what they
cover — a green suite is not evidence about either.

```bash
# the board in a real browser: file:// and over HTTP, ~90 s
PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
  uv run --with playwright==1.61.0 python tests/board_smoke.py
# the notifier against the live feed and a real ntfy round trip, ~25 min
uv run --with numpy python tests/live_notifier.py
```

- **`board_smoke.py`** after any change to `web/` — a `file://` board cannot fetch
  a sibling file at all, which no unit test can see.
- **`live_notifier.py`** after any change to `watch.py`, `notify.py`, `brief.py` or
  the board's handoff, and before claiming M5 works. It arms a train past MBTA's
  horizon, drops the page, and reports which triggers actually fired. It mints
  throwaway ntfy topics and a scratch plan file, so it never touches
  `ops/ntfy.env`, `data/plan.json` or the running notifier; `--real-topics` opts
  in to the phone deliberately. A revision needs a real slip and a recovery needs
  a real no-show, so it names what it could *not* exercise rather than reporting a
  quiet window as success.

`web/` **is** the static origin — Pages uploads it as-is. `publish.py` moves files
and never derives them; `fit.py` and `stats.py` are what write `data/`.

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
   no-show veto. `revenue` exists in the v3 API and **not** in the protobuf feed —
   so the archive cannot carry it, no replayed or sampled fixture can contain a
   deadhead (measured: 0 of 26), and dropping this filter used to break nothing in
   any test. `make_fixtures` synthesises one deadhead-and-ghost case per day for
   exactly this reason; do not let a regeneration drop it.
8. **Timezone must be `ZoneInfo`**, never a fixed offset. EDT→EST flips
   2026-11-01.
9. **A skipped test is not a passing test.** The node parity test skips when node is
   missing, which looks identical to green in a CI log while comparing nothing. It
   fails instead when `CI` is set. Same shape as invariant 7: a check with nothing
   behind it.
10. **Score a day against *that day's* schedule.** `replay.score` defaulted to the
    three most recent schedule snapshots, which silently drops the timetable tier for
    an older day — and the timetable carries the horizon past ~13 min. Measured on
    2026-09-24: pinned to its own day, 69/74 with 22 arrivals warned by the timetable
    alone; pinned to the wrong day, 60/74 and the tier gone. Pass `day=`.
11. **Predictions in `data/pairs` past ~20 min are mispairs, not long-range
    predictions.** `rollup` pairs each prediction with that vehicle's *next* arrival,
    so a missed `STOPPED_AT` transition attributes it to the following visit: p50 err
    is −414 s at a 20–30 min lead and −2188 s at 30–45 min, against ~620 s worst
    below that. Cap the lead before binning.

## The contract

`service.compute_rows` is pure and is the function `web/app.js` reproduces exactly.
`tests/fixtures/cases-*.json` pins it: real snapshots with every input inlined, plus
expected rows. `tests/test_contract.py` runs both implementations against these
files and compares every field; `tests/run_cases.js` is the node side. Two of the 28
cases are synthesised, not sampled — see invariant 7 below.

Regenerate fixtures deliberately (`src/make_fixtures.py`), never to make a failure
go away — a regenerated fixture that drops a tier is how this test stops working
without anyone noticing.

## Conventions

- **polars** for anything that ships or is tested; **DuckDB** (`src/q.sh`) for
  ad-hoc questions only — it must stay out of the deploy path.
- **The backend serves only what a browser cannot fetch.** The board computes its
  own rows; the one thing it asks for is the protobuf-only skip set. Cross-origin
  reachability *is* the CORS header, so `server._send` emits it from exactly one
  place, gated on `BROWSER_ROUTES`, and the test asserts that structure — one
  emitting line with the allowlist checked above it. An earlier version of that test
  only asserted the string `BROWSER_ROUTES` appeared in the file, which the
  definition itself satisfied: CORS on `/api` passed the whole suite. An allowlist
  nothing consults is decoration. The board side is checked too: no unjustified host
  in any `web/` file *or* in `model.json`'s constants (the M4 URL will be published
  there, so grepping `web/` for `https://` cannot see it), and no call to a
  computed-rows route.
- **Keep unresolved predictions.** `rollup.py` stores a prediction that never
  matched an arrival with a null `actual_arr`. It used to drop them, so `data/pairs`
  — the store that is never pruned — held no evidence of a train that was predicted
  and never came, which is exactly what the 240 s no-show debounce needs to stop
  being hand-tuned off two observed flaps. Null means "no later arrival in THIS
  day's archive", so it includes end-of-day truncation; measured 3.7% at Magoun
  inbound, spread through the day rather than bunched at the end.
- **One copy of the walk.** `data/config.json` holds `walk_s`; everything reads
  `service.DEFAULT_WALK` and `fit.py` publishes it into `model.json` so the board's
  default and the walk `stats.py` scores with cannot disagree. `MAGOUN_WALK_S` still
  overrides for one-off experiments. It was the literal `390` in eight files.
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
- **`Sequence-ID` does not replace a pending scheduled message, and `delete` does
  not cancel one.** Measured: three publishes with one `Sequence-ID` and one `At`
  deliver three times; `GET /{topic}/{seq}/delete` publishes a `message_delete`
  *event* and the scheduled send still arrives. Both are instructions to the ntfy
  **app** to collapse or dismiss a notification in the tray, not server-side
  scheduling — which is why from a phone it looks exactly like "reschedule delivers
  exactly once", and was written down that way. So a scheduled alert cannot be
  refined from a page at all: `web/board.html` and `src/status.html` arm **once**,
  as the fallback for a notifier that is not running, and refinement belongs to
  `watch.py` — which is what the `arm` handoff on the command topic is for.
- MBTA predictions **flap ~8 min for ~90 s**. Debounce longer than the flap, and
  never widen a re-match far enough to reach the next train (headway 528 s).
- `cdn.mbta.com/*.pb` has **no CORS** and is the only source of `SKIPPED`
  markers. `api-v3.mbta.com` has `access-control-allow-origin: *`.
- A page opened as `file://` **cannot fetch a sibling file** (Chromium: `URL scheme
  "file" is not supported`) — CORS never enters into it. Hence `web/model.js`, the
  same bytes as a script; `fit.py` writes it and a test compares them. Cross-origin
  fetches to `api-v3.mbta.com` do work from `file://`.
- **The v3 API is 20 requests/minute unauthenticated, and this repo has several
  pollers.** `service.snapshot()` is *two* requests, so the archiver at 15 s is
  ~8/min and a notifier tick at 20 s is another ~6/min. Add a second notifier — a
  live test alongside the launchd one — and it tips over into **HTTP 429**, which
  `watch.main` swallows as a bad tick and `brief.health` never sees at all. Set
  `MBTA_API_KEY` (`service.KEY` already reads it) before running anything extra,
  or stop the other pollers first. Measured: two notifiers plus the archiver
  throttled within six minutes.
- The v3 `/schedules` endpoint only serves ~8 days back. Daily snapshots are the
  only way to keep them; an un-captured day is gone.

## Scope

One rider, one platform, one direction. Prefer measuring to arguing — nearly every
belief in this repo that went unmeasured turned out wrong, including several of
mine. When a claim matters, check it against `data/` before writing it down.
