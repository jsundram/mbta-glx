# Architecture — buildout plan

The prototype works end to end on one Mac. This is the plan to split it into the
three tiers it wants to be, in an order where each step is useful on its own.

Background and evidence live in **[launch-plan.md](launch-plan.md)**; the traps
that fail silently are in **[CLAUDE.md](CLAUDE.md)**. This document is only the
shape of the system and the order of work.

---

## 1. Shape

```
┌─ STATIC (GitHub Pages: web/ uploaded as-is) ────────── £0 ─┐
│  board.html + app.js                                       │
│    ├─ fetch  api-v3.mbta.com          live, CORS, no key   │
│    ├─ fetch  model.json               fitted quantiles     │
│    ├─ <script> model.js               same bytes, file://  │
│    ├─ fetch  stats.json               how it has been doing│
│    ├─ fetch  live-extras.json         skips (no CORS path) │
│    └─ POST   ntfy.sh                  arm its own alerts   │
└────────────────────────────────────────────────────────────┘
        ▲ published artifacts              ▲ scheduled push
┌─ BACKEND (Mac now, Pi/VPS later) ─────────────────────┐   │
│  record_rt.py   continuous capture  → data/live/       │   │
│  rollup.py      distil + compact    → data/pairs/      │   │
│  fit.py         refit per rating    → model.json       │   │
│  stats.py       score closed days   → stats.json       │   │
│  publish.py     move artifacts to the static origin    │   │
│  skips.py       live-extras.json every ~30 s           │   │
│  watch.py       refine alerts, brief, recovery  ───────────┘
└────────────────────────────────────────────────────────┘
```

Three things can only run server-side, and nothing else needs to:

1. **The protobuf feeds.** No CORS, and the only source of `SKIPPED` / `CANCELED`.
2. **The archive.** A browser cannot accumulate history.
3. **Proactive triggers** with no page open (morning brief, post-close refinement).

An always-on tablet running the board covers (3) for the common case — it re-arms
the pending alert every minute. It cannot cover (1) or (2).

---

## 2. Data contracts

The seams. Everything else is implementation.

### `model.json` — fitted quantiles, ~6 KB, published per rating

```jsonc
{
  "grid":  [0.02, 0.04, ... 0.98],        // 49 quantile levels
  "tiers": { "ball_dep": {"n": 4332, "q": [...49 seconds...]},
             "med_dep":  {...}, "berthed": {...}, "term": {...} },
  "berth": {"n": 3520, "q": [...], "turn_plus_run": 357, "sched_bias": 60},
  "sched": {"n": 3749, "q": [...]},
  "rides": {"70199": {"name": "Park Street", "n": 3852, "q": [...]}, ...},
  "headway_median_s": 528.0,
  "n_legs": 4418, "days": 35
}
```

**Model as data.** Consumers do lookup and arithmetic, never modelling. This is the
single rule that keeps a JS frontend from becoming a second implementation.

### The prediction row — the frontend's own output

```jsonc
{ "eta": 1790281459, "lo": 1790281300, "hi": 1790281755,
  "source": "mbta" | "berthed at Medford/Tufts" | "departed Medford/Tufts"
          | "departed Ball Sq" | "schedule" | "not stopping here",
  "backed": true,          // something upstream corroborates it
  "vehicle": "G-10065" | null,
  "skipped": false }
```

`lo` is the quantile the rider acts on. `eta` is the median. Never quote `eta` as
the leave time.

### `live-extras.json` — what the browser cannot fetch, every ~30 s

```jsonc
{ "t": 1790281459, "skipped_trips": ["77745376", ...], "ttl_s": 60 }
```

### `stats.json` — self-scoring, published daily

```jsonc
{ "as_of": "2026-09-26", "window_days": 7,
  "caught": 61, "of": 70, "mean_platform_wait_s": 168,
  "mean_door_to_train_s": 964,
  "by_lead": [{"bin": "5-10min", "n": 104, "p10": -61, "p50": -39, "p90": 78}] }
```

`web/board.html` reads these field names directly and hides its panel when the file
is absent or unparseable — so a missing `stats.json` degrades quietly, but a
*renamed* field shows an empty panel instead of failing. Keep the names.
`tests/test_stats.py` pins them from both sides, reading the names out of
board.html itself rather than a list typed twice.

Where each field comes from, since they are not the same question (M3):

| field | source | what it answers |
|---|---|---|
| `caught`, `of` | `replay.score` over `data/live` | of the trains that came, how many were announced in time |
| `mean_platform_wait_s`, `mean_door_to_train_s` | `simulate.run` over `data/live` | what it costs once it fires — same riders for both, so their gap means something |
| `by_lead` | `data/pairs`, never pruned | prediction error by how much warning it gave |

The two means are `-1` when the window has no riders, not `null` and not `0`: the
board would render `NaN min` for one and a free ride for the other. Each day's score
is appended to `data/scores.jsonl`, so the window outlives the 90-day prune of the
archive it was computed from.

### `data/pairs/*.parquet` — the long-term store

`(day, stop, dir, route, veh, trip, made_at, pred_arr, actual_arr, lead_s, err_s,
unc_s)`. ~0.68 MB/day. Never pruned.

### `data/live/day=YYYY-MM-DD/` — compacted archive

`preds.parquet`, `vehicles.parquet`, `meta.json`. Delta-encoded, times as offsets,
~1.25 MB/day, directly queryable by DuckDB. Pruned at 90 days.

---

## 3. The one real risk

Going static puts the tier-selection logic in the browser, so it exists twice:
Python for fitting, backtesting and `simulate.py`; JavaScript for the live board.
**Only the Python side has tests.**

Mitigations, in order of importance:

1. **Keep the JS thin.** It reads `model.json` and does arithmetic. Anything that
   fits, simulates, or scores stays in Python and never ships.
2. **A contract test.** Done: `tests/fixtures/cases-*.json` run through both
   implementations and `tests/test_contract.py` asserts identical rows, field for
   field, for every case. `tests/run_cases.js` is the node side; the comparison
   lives only in Python so "identical" has one definition. Sensitivity is in M2.
3. **One source for the constants.** Done: `model.json` now carries a `constants`
   block — `veto_window_s`, `stale_vehicle_s`, `dedupe_s`, `min_gap_s`,
   `horizon_s`, the per-source `band_s`, and the stop ids. `service.py` reads them
   from there, so a JS port reads the same file rather than re-typing numbers.

If the JS starts growing past arithmetic, that is the signal to fall back to a thin
backend serving `/status` — less work, gives up the static property.

---

## 4. Build order

Each milestone is independently useful; nothing is a big-bang cutover.

### M1 — Extract the contract *(no behaviour change)* — **done**
- `service.compute_rows(now, preds, vehicles, model, walk, qs, horizon, berths,
  slots, skipped) → rows` is pure: no clock, no network, no file reads. `etas()`
  is now a thin wrapper that gathers the I/O.
- `tests/fixtures/cases-*.json` — 26 real archived snapshots with everything
  inlined (schedule, skip set, berth state, walk, clock), plus the expected rows.
  Regenerate with `src/make_fixtures.py`, deliberately, never to silence a failure.
- `tests/test_contract.py` — 53 assertions over them.

**Sensitivity, measured by injecting drift:**

| injected change | caught by |
|---|---|
| schedule offset +30 s | 24/24 cases |
| berth offset +30 s | 2/24 cases |
| veto window 480→900 s | 1/24 cases |

Thin but real. The danger is not a weak test, it is a *regenerated* fixture set
that quietly drops a tier — so `test_every_tier_is_exercised_by_some_fixture`
asserts all six sources appear, and the generator synthesises a skipped-tier case
because skips are too rare to catch by sampling.

### M2 — Static board *(the tablet dashboard becomes real)* — **done**

`web/board.html` + `web/app.js` run with no backend: `src/status.html` with its
data source swapped, a 43-line diff. `app.js` is the port of `compute_rows`,
`upstream_state`, `_live`, `_revenue`, `BerthTracker` and `ArrivalTracker`, plus
the I/O that gathers what the pure function needs. Every constant is read from
`model.json`; a missing one throws rather than falling back to a literal, because
a silent default is how the two implementations would drift while both looked fine.

**Contract test.** `tests/test_contract.py` runs all 28 fixture cases through node
as well as Python and compares every field of every row. Sensitivity, measured by
injecting drift into the JS:

| injected change | caught by |
|---|---|
| schedule offset +30 s | 28/28 |
| mbta band +1 s | 23/28 |
| stale filter removed | 4/28 |
| berth offset +30 s | 2/28 |
| revenue filter removed | 2/28 |
| veto window 480→900 s | 1/28 |

The fixture set grew from 26 cases to 28 for the last row. Measured first: not one
of the 26 real snapshots contained a `NON_REVENUE` vehicle, because GTFS-realtime
has no revenue field at all, so the archive cannot carry one (`simulate.to_v3` says
as much). Dropping the deadhead filter therefore changed nothing anywhere —
invariant 7 failing silently in the implementation with no backtest behind it.
`make_fixtures` now synthesises one case per day with a deadhead and a
five-minute-stale ghost on the inbound approach, on the vehicles real predictions
name, so forgetting either filter moves a row from ±75 s to ±7 s and fails. The 26
pre-existing cases are byte-identical.

**Two things were wrong until measured in a browser:**

- A board opened as `file://` cannot fetch a sibling file at all — Chromium: `URL
  scheme "file" is not supported`, before CORS is even reached. `fit.py` therefore
  publishes `web/model.js` as well, the same bytes as a script, and `app.js` falls
  back to it. Without it the static board never got a model.
- Live predictions and vehicles *do* fetch cross-origin from a `file://` page, so
  only the sibling assets needed the fallback. `stats.json` and `live-extras.json`
  degrade to a hidden panel and an empty skip set.

`tests/board_smoke.py` drives the real page (playwright, MBTA and ntfy stubbed,
~90 s, not collected by pytest) and checks 18 properties of it, including the
re-arm — measured at 60 s apart, which is the only reason to leave a tablet open.

**Done when:** the board runs from `file://` with the Mac server stopped, an iPad
left open keeps re-arming an alert, and both implementations agree on every
fixture. All three verified.

### M3 — Publish pipeline — **done**

**The static origin is `web/` in this repo.** `pages.yml` uploads that directory to
Pages as-is, so board.html and app.js have no second copy to fall behind and there
is no `docs/`. `publish.py --check` runs before the upload and fails the deploy on
an incomplete origin.

`publish.py` moves the published set and never derives it — a missing
`data/model.json` is an error telling you to run `fit.py`. The set is an explicit
manifest, each derived entry naming the file under `data/` it must equal:

| published | must equal | why |
|---|---|---|
| `board.html`, `app.js` | — | authored in `web/`, which *is* the origin |
| `model.json` | `data/model.json` | fitted quantiles, fetched |
| `model.js` | `data/model.json`, script-wrapped | a `file://` board cannot fetch a sibling file at all |
| `stats.json` | `data/stats.json` | the self-score |

**A refit publishes three artifacts, not one.** A publisher carrying only
`data/model.json` leaves the board predicting from the previous rating with nothing
on screen to say so. `tests/test_publish.py` fails if the manifest loses one, and
separately if the board starts fetching an asset the manifest does not carry.

Write-only by default; `--commit` makes a local commit, scoped to its own paths;
nothing ever pushes. `--to DIR` stages the set elsewhere, so Cloudflare or a second
Pages repo stays available without rework.

**`stats.json` reads two sources, because it answers two questions.**

- **Coverage** — of the trains that came, how many were announced early enough to be
  on the platform for — is `replay.score`, one rider per real arrival.
- **Cost** — platform wait and door-to-train — is `simulate.run`, riders on a
  five-minute grid through the real `compute_rows`. Both means come from the *same*
  riders, so the gap between them means something. Platform wait alone rewards
  dawdling; door-to-train is what stops it.
- **`by_lead`** is prediction error, and `data/pairs` already holds exactly it. Pairs
  is never pruned, so this is the half of the panel with a long memory.

Measured over 2026-09-24/25: caught 185/198, platform wait 3.7 min, door-to-train
15.6 min. Verified in a browser over HTTP, which is the only shape where the panel
can appear at all.

Three things the data settled:

- **The window survives pruning.** Coverage and cost need the archive, which is
  pruned at 90 days, so each day's score is appended to `data/scores.jsonl` the
  first time it is computed and the window is aggregated from that. The archive
  goes, the score stays.
- **`by_lead` is capped at a 20 min lead.** `rollup` pairs each prediction with that
  vehicle's *next* arrival, so a missed `STOPPED_AT` transition attributes it to the
  following visit. |err| tops out near 620 s through the 15–20 min bin, then p50 is
  −414 s at 20–30 min and −2188 s at 30–45 min. One such row inverts a bin, and the
  feed's real reach here is 8–13 min anyway.
- **Riders who decide overnight are not riders.** 47 of 286 on 2026-09-25 had
  door-to-train up to 292 min, dragging the mean from 16.1 to 42.6. `simulate.run`
  already drops anyone facing over an hour on the platform; door-to-train is held to
  the same bound rather than inventing a service window.

**Refit on rating change** is two halves, because they cannot run in the same place.
`rating.yml` detects: MBTA's feed index republishes constantly — 1016 rows, 1016
distinct `feed_start_date`s — so the identity watched is `(season, version,
feed_end_date)`, which held across all 14 republishes of the current rating and
changed at the Summer→Fall boundary. `feed_end_date` is also where **2026-12-12**
comes from; it is not a date anybody typed in. `src/refit.sh` then runs on the
capture host, because `fit.py` reads `data/magoun.parquet`, which only
`build_dataset.py` writes and nothing schedules — so a refit is the dataset rebuild
too. It halts before regenerating fixtures: a +30 s schedule shift fails all 28
cases, and that failure is the contract test working. `--fixtures` is a second,
deliberate run that prints the row diff.

**The daily rollup is not a workflow, and cannot be.** `data/live`, `data/pairs` and
`data/raw` are gitignored — large, churning daily — so the archive lives on the
capture host and `src/daily.sh` does the rollup, the scoring and the origin check
there. A scheduled job that rolled up an empty checkout would succeed every night
and produce nothing, which is worse than not having it. A test asserts no workflow
runs `rollup.py`, `stats.py`, `fit.py`, `build_dataset.py` or `make_fixtures.py`,
and another asserts the premise it rests on.

So CI does what needs no archive: the suite on every push, the same suite weekly as
a drift check (a node release or a polars upgrade moving under a repo that did not
change), and the rating watch. The node parity test used to *skip* when node was
missing, which is indistinguishable from passing in a green log — it now fails when
`CI` is set.

The `revenue` sidecar in launch-plan is **later, not M3**. It changes what the
archive records and what fixtures can sample, and touches nothing the publisher
does.

**Done when:** the static board is serving artifacts nobody copied by hand. Done —
one open item: the repo has no git remote yet, so the workflows are committed but
have never run. Add the remote, push, and set Pages → Build and deployment → Source
→ GitHub Actions.

### M4 — `live-extras.json`
- Small loop publishing skipped trips every ~30 s.
- Board degrades cleanly when it is stale or missing (`ttl_s`).

**Done when:** a skipped train is struck through on the static board.

### M5 — Backend notifier
- `watch.py` refines an armed alert after the page closes; brief and recovery.
- Needs the always-on host decision (below).

**Done when:** an alert armed at 08:00 and then abandoned still tracks a train
that slips.

---

## 5. Open decisions

| decision | options | notes |
|---|---|---|
| always-on host | iPad kiosk · Raspberry Pi · fly.io / VPS | iPad covers refinement only; capture still needs a real host. **Only M5 is blocked** — M4 no longer needs it, see below. |
| archive retention | prune 90d · keep forever | compaction makes a full year ~0.46 GB; keeping everything is now affordable |
| `data/live` location | inside Dropbox · outside | appended every 15 s; moving it out removes constant sync churn |
| git remote | none yet | M3's workflows are committed but have never run. Needs a remote, a push, and Pages → Source → GitHub Actions. |

### Decided: M4 serves its endpoint off the Mac over Tailscale

`live-extras.json` stops being a *published artifact* and becomes a **live endpoint**
on the backend — a route on `server.py`, reachable from the rider's devices over
Tailscale. `tailscale serve` supplies a real cert for `machine.tailnet.ts.net`, which
matters because the board is served over HTTPS and a plain `http://` fetch would be
blocked as mixed content.

This is closer to §1 as drawn than a published file was, and it removes hosting from
M4's critical path entirely. It needs three things: `Access-Control-Allow-Origin: *`
(**not** an allowlist of the Pages origin — the board is also opened from `file://`,
where `Origin` is `null`), a bind beyond `127.0.0.1`, and the endpoint URL published
in `model.json`'s constants like `walk_s`, so switching origins is a republish rather
than a code change.

**Why this is not a one-way door.** The route is the same Python in a fly.io
container; the URL is a published constant; CORS is identical either way. The real
migration cost was never the endpoint — it is where `data/live` and `plan.json` live,
and that is owed whenever M5 moves, Tailscale or not.

**What it does not fix: sleep.** For M4 that is survivable — an asleep Mac means the
fetch fails and the board degrades to an empty skip set, which is the designed
behaviour. For M5 it is fatal, and Tailscale changes nothing about it. `caffeinate`
is the interim mitigation; it blocks idle sleep, not a lid close, and does not
survive a reboot unless it is in the launchd plist.

**The line that keeps the door open.** The endpoint serves *only* what a browser
physically cannot fetch. The moment it serves computed rows, the static property is
gone and fly.io stops being optional. That is an intention, so it is enforced rather
than hoped for: `server.BROWSER_ROUTES` is an allowlist with a stated reason per
entry, and `tests/test_regressions.py` fails if a CORS header appears outside it, if
the board reaches a host that is not justified, or if the board starts calling a
computed-rows route. Verified by injecting all four.

## 6. Explicit non-goals

Other stops, other directions, other riders. Beating MBTA's prediction inside their
own horizon — we relay it. A general transit app.

## 7. Sequencing note

M1–M3 are done; M4 is unblocked. Phase 1 calibration is gated on the works ending **2026-10-05**
and a rating change on **2026-12-12** resets every schedule-dependent constant, so
prefer shipping the structure now and re-fitting into it later.
