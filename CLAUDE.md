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
uv run --with polars --with numpy python src/stats.py    # score closed days -> data/stats.json
uv run python src/make_icon.py                   # redraw web/icon-180.png
uv run python src/publish.py --check             # is web/ complete and current?
uv run python src/publish.py --commit            # commit what moved; never pushes
./src/refit.sh                                   # rebuild dataset, refit, diff, suite
./src/refit.sh --fixtures                        # then, deliberately; read the row diff
uv run python src/rating.py --check              # has the schedule rating moved?
```

`ops/install.sh` is idempotent and must not be run with `sudo`.

## What is running, and how to tell

Deployed on this Mac since 2026-09-26. Four launchd agents, and a Tailscale proxy
that is the only way the rider's phone reaches the backend.

```bash
./ops/status.sh        # agents, capture freshness, endpoints, keys -- one answer
```

| agent | what it does | dies quietly? |
|---|---|---|
| `com.magoun.archiver` | `record_rt.py`, appends every 15 s | **yes** — an un-captured day is gone; `/capture` and the board's footer exist for this |
| `com.magoun.server` | `serve.sh` → `server.py` on 127.0.0.1:8723 | yes — the board degrades to an empty skip set |
| `com.magoun.watch` | `watch.sh` → the notifier | yes — no push, no error |
| `com.magoun.daily` | `daily.sh`, scheduled | no — it leaves a log |

`tailscale serve` maps **one path at a time**, so each allowlisted route needs its
own line and the proxy ends up enforcing the same line `BROWSER_ROUTES` does. Set
once, survives reboot, lost if Tailscale is reinstalled or
`tailscale serve --https=443 off` is run:

```bash
tailscale serve --bg --https 443 --set-path /skips   http://127.0.0.1:8723/skips
tailscale serve --bg --https 443 --set-path /capture http://127.0.0.1:8723/capture
tailscale serve --bg --https 443 --set-path /today   http://127.0.0.1:8723/today
```

`/skips` and `/capture` are verified live: 200 with `access-control-allow-origin: *`
through the proxy, while `/api`, `/status`, `/board` and `/history` answer **404**
there — but note *why*: `/` on this hostname is mounted to **port 8770, which is a
different application** (Deck), so an unmapped path is not refused, it is answered
by that app. A route with no `--set-path` line therefore 404s from somewhere else
entirely, which looks identical to a backend that is down. `/today` is verified on
**loopback** — 200, `access-control-allow-origin: *`,
48 trains scored, 19 ms for a cached hit — but its proxy path is **not set yet**, so
it is unreachable from the phone until that third line is run and
`com.magoun.server` is restarted onto the code that serves it. Until then the board
simply hides the panel, which is what it does off the tailnet anyway. `./ops/status.sh` checks every allowlisted path and every private one, and
reads both lists out of `server.py` rather than keeping its own copy.

Secrets live in `ops/secrets.env` (gitignored; `ops/ntfy.env` is the older name and
is still read). `ops/secrets.env.example` documents all three values. The board
cannot read that file, and nothing per-device is in the page, the repo or
`model.json` — it is served from a public origin. Four keys in `localStorage`, all
of them set from the **settings panel** under the footer: `magoun.walk` (seconds),
`magoun.ntfy`, `magoun.cmd`, `magoun.mbtakey`. The panel is a link, not a dialog on
load: the board reads fine with none of it set. The bell opens it when there is
nowhere to send an alert, and once — the `magoun.cmd.asked` flag — to offer the
command topic to an install that predates the handoff. The MBTA key also has its own
box, shown only while v3 is actually refusing this address.

The board is served at **https://jsundram.github.io/mbta-glx/** by `pages.yml`.

## Checks the suite cannot do

Neither is collected by pytest, both need the real world, and both exist because
the things they catch are invisible to reading. Run them when you touch what they
cover — a green suite is not evidence about either.

```bash
# the board in a real browser: file:// and over HTTP, ~90 s
PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \
  uv run --with playwright==1.61.0 python tests/board_smoke.py
# the notifier against the live feed and a real ntfy round trip, ~25 min.
# gtfs-realtime-bindings or the skip path is invisible and the run says so as
# "no skips" -- see the launcher test in tests/test_server.py.
uv run --with numpy --with gtfs-realtime-bindings python tests/live_notifier.py
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

The backend serves the board three things it cannot fetch, all allowlisted in
`server.BROWSER_ROUTES` and all reached at `constants.backend_url`: `/skips` (the
protobuf-only skip set), `/capture` (when the archive was last appended to, so a
dead archiver is visible rather than silent) and `/today` (how trains have actually
run today — aggregates only, never rows, because scoring a day needs the whole
day's prediction stream and arrivals). Reachable over Tailscale; unreachable is a
normal state and must not be rendered as failure — each of the three degrades to
one missing feature, never to a broken board.

`/today` never scores on the request path: this server is single-threaded and the
board fetches `/skips` in the same tick behind a 2.5 s timeout. It answers from the
last summary and recomputes behind that, at most once a minute and only when the
archive has actually grown — so a dead archiver costs nothing, which on a storm
weekend is the case that matters.

`web/` **is** the static origin — Pages uploads it as-is. `publish.py` moves files
and never derives them; `fit.py` and `stats.py` are what write `data/`. `stats.json`
is **not** published any more: the board's self-score panel asks `/today`, and a file
in `web/` that nothing fetches goes stale there with every test still green.
`data/stats.json` remains the project's own record.

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
   older code, and the uncompacted `rt-*.jsonl.gz` is pruned at 90 days. The
   *compacted* `data/live/day=*` is never pruned by anything, despite what
   architecture.md used to say — so a reader of any age may turn up.
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
11. **`err_s` in `data/pairs` is `predicted - actual`, so a NEGATIVE err is a
    train that came LATE.** Every consumer has to say which it means: the board
    printed `p50 -22s` for a train 22 s late for a month, which reads as early.
    `fit.py` flips it once, into a column called `late`, and everything downstream
    reads that.
12. **Predictions in `data/pairs` past ~20 min are mispairs, not long-range
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
- **Nothing quoted is symmetric, because nothing measured is.** `lo`/`hi` are
  fitted q10/q90 offsets that sit well off centre, and `(hi - lo) / 2` printed as a
  single `±` invents an early side. The MBTA tier's band was three eyeballed
  half-widths (±75 s / ±33 s / ±7 s) until 2026-09-26; it is now `model.json`'s
  `pred` table — quantiles of *actual minus predicted*, binned by how far ahead the
  prediction was made, interpolated between bin centres so the quoted ETA does not
  jump as a train's lead crosses an edge. The early half of the old band was 75 s of
  platform wait per trip on a train that measurably does not arrive early; the
  catch rate it bought back (2%) is now a quantile choice you can see.
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
  refined from a page at all: `web/index.html` and `src/status.html` arm **once**,
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
- **The v3 API allows 20 requests/minute unauthenticated, counted PER CLIENT IP.**
  `service.snapshot()` is *two* requests, so a notifier tick at 20 s is ~6/min and
  **one open board is ~12.5/min** — a board plus anything else on the same public
  IP is already over, and a home network puts every device behind one. (The
  archiver is innocent: `record_rt.py` reads `cdn.mbta.com`, not v3.) Measured
  live: `x-ratelimit-remaining: 0` with one board open and a live test running.
  A 429 is swallowed by `watch.main` as a bad tick, and in the board it aborts
  `tick()` at `snapshot()` — *before* the skip and capture fetches, so those look
  unanswered when the real fault is upstream.
  Two *places*, not necessarily two keys — MBTA approves key requests rather than
  handing them out, so one key in both is normal and fine (the limit is per key
  and usage is nowhere near it). The board is the heavy user at ~12.5/min per open
  tab; `watch.py` only fetches while a plan is armed and the archiver reads
  `cdn.mbta.com`, so with a single key the *board* is where it buys the most.
  The backend reads `MBTA_API_KEY` from `ops/secrets.env` (`service.KEY` sends it
  as `x-api-key`); the board keeps one in `localStorage["magoun.mbtakey"]`, sent
  as an `api_key` query param —
  a custom header would force a CORS preflight and double the request count.
  **Never publish a key into `model.json`**: that file is served from a public
  origin. `tests/test_server.py` fails on a committed key.
- The v3 `/schedules` endpoint only serves ~8 days back. Daily snapshots are the
  only way to keep them; an un-captured day is gone.

## Scope

One rider, one platform, one direction. Prefer measuring to arguing — nearly every
belief in this repo that went unmeasured turned out wrong, including several of
mine. When a claim matters, check it against `data/` before writing it down.
