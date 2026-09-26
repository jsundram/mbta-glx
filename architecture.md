# Architecture — buildout plan

The prototype works end to end on one Mac. This is the plan to split it into the
three tiers it wants to be, in an order where each step is useful on its own.

Background and evidence live in **[launch-plan.md](launch-plan.md)**; the traps
that fail silently are in **[CLAUDE.md](CLAUDE.md)**. This document is only the
shape of the system and the order of work.

---

## 1. Shape

```
┌─ STATIC (GitHub Pages / Cloudflare Pages) ──────────── £0 ─┐
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
│  publish.py     push artifacts to the static origin    │   │
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

### M3 — Publish pipeline
- `publish.py` writes `model.json` + `stats.json` to the Pages repo.
- GitHub Actions: daily rollup, weekly drift check, refit on rating change.

**Done when:** the static board is serving artifacts nobody copied by hand.

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
| always-on host | iPad kiosk · Raspberry Pi · fly.io / VPS | iPad covers refinement only; capture still needs a real host. Only M5 is blocked. |
| archive retention | prune 90d · keep forever | compaction makes a full year ~0.46 GB; keeping everything is now affordable |
| `data/live` location | inside Dropbox · outside | appended every 15 s; moving it out removes constant sync churn |

## 6. Explicit non-goals

Other stops, other directions, other riders. Beating MBTA's prediction inside their
own horizon — we relay it. A general transit app.

## 7. Sequencing note

M1–M4 are unblocked. Phase 1 calibration is gated on the works ending **2026-10-05**
and a rating change on **2026-12-12** resets every schedule-dependent constant, so
prefer shipping the structure now and re-fitting into it later.
