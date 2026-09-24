# Magoun Square inbound ETAs

> **Roadmap and delivery status: [launch-plan.md](launch-plan.md)**

The MBTA publishes ~13 predictions for the Medford/Tufts-bound platform at Magoun
Square but only reaches **8–13 minutes ahead** on the downtown-bound side — not
enough notice to leave the house. This builds longer-horizon inbound ETAs *with
error bars*, and sharpens them as trains move.

## Run it

```bash
uv run --with polars python src/server.py 8723   # http://localhost:8723/?walk=6
uv run python src/service.py 6                   # one-shot CLI, 6-minute walk
```

Set `MBTA_API_KEY` (free, from api-v3.mbta.com) to lift the keyless rate limit.

## Phone notifications (Phase 5a)

**One-time setup:**

1. Install the **ntfy** app (iOS/Android, free) and subscribe to
   **`MAGOUN_NTFY_TOPIC` only** (from `ops/ntfy.env`). Do *not* subscribe to
   `MAGOUN_NTFY_CMD` — that topic carries button taps in the other direction, and
   subscribing to it just echoes your own commands back at you. Both strings are
   unguessable and act as passwords, so the file is gitignored.
   Allow ntfy through your Focus settings, or the leave-now alert stays silent.
2. Test the channel: `./src/watch.sh` is the runtime; for a one-off check run
   `set -a && . ops/ntfy.env && set +a && uv run python src/notify.py`.
3. Install the agent: `./ops/install.sh` (adds `com.magoun.watch`).

**Daily use** — tell it where you need to be, then answer the notifications:

```bash
./src/watch.sh plan park 09:00              # Park Street by 9:00
./src/watch.sh plan "north station" 08:45 0.95   # ...at 95% confidence
```

Destinations take a name or a GTFS stop id — any unambiguous substring works
(`park`, `copley`, `lechmere`, `boylston`, `government`, `north`):

| name | stop id | typical ride from Magoun |
|---|---|---|
| Lechmere | 70502 | 7.7 min |
| North Station | 70206 | 13.6 min |
| Government Center | 70202 | 18.4 min |
| Park Street | 70199 | 21.2 min |
| Boylston | 70159 | 23.7 min |
| Copley | 70155 | 28.2 min |

You get a brief with up to three trains, each a button showing its on-time chance.
Tap one to commit. Then **Leave now** fires at the right moment (priority 5), with
*On my way* / *Next one* / *Cancel*. Tapping *On my way* arms the **adjust** push,
which lands once the train is actually moving and says whether to ease up or hurry.
If the committed train never runs — 9.6% of them don't — a **recovery** push offers
the next one. Four notifications normally, six in the worst case.

Button taps travel back over a second ntfy topic, so the notifier needs no inbound
port, tunnel or static IP, and moves to a cloud host unchanged.

## What the history says

35 days of MBTA LAMP stop events (2026-08-18 → 09-23), 4,418 real inbound arrivals
at Magoun. Estimator sharpness, as the 10th–90th percentile error band:

| evidence | band | lead time |
|---|---|---|
| departed Ball Square | **14 s** | ~51 s |
| departed Medford/Tufts | **65 s** | ~190 s |
| published schedule + bias | **318 s** | unlimited |
| berthed at Medford/Tufts | 829 s | ~13 min |
| arrived at outbound terminus | 1058 s | ~14 min |

The terminus signals are **too loose to time a train**. The layover is not random
delay, it is *waiting*: plotted against how early a train berths relative to its
scheduled departure, it is flat at ~165 s (the minimum turnaround) then climbs
one-for-one with the clock. The rule is
`departure ≈ max(berth + 165 s, scheduled departure + 60 s)`; corr(layover, slack)
= 0.68, and conditioning on it collapses the spread from 795 s to 319 s — the same
width as reading the timetable directly. Terminus state is therefore used only as a
presence check, not as a clock.

**When a train runs behind, the turnaround IS information the schedule lacks.**
The timetable assumes the train left on time; if it berths late the binding
constraint becomes physics, not the clock. Splitting by regime:

| predictor | late berth (slack ≤ 0, 5.6%) | early berth (94%) |
|---|---|---|
| schedule only | median **−282 s**, band 818 s | median −2 s, band 263 s |
| berth + 165 s turnaround | median **−13 s**, band 426 s | median −471 s, band 702 s |
| `max(...)` of both | median −13 s, band 426 s | median −2 s, band 263 s |

Schedule-only is biased **4.7 minutes early** exactly when the train is late. Taking
the max fixes that, narrowing the overall band 315 s → 271 s, and as a fitted tier
it beats every other estimator at this horizon (band **299 s** vs 318 s for the
schedule and 829 s for raw berth time). At a 6.5-min walk it lifts the sub-minute
wait rate from 17.8% to 21.2% at matched risk, or 30.4% if you accept more long
waits.

**A hard floor falls out of the same fact.** If no train is berthed at
Medford/Tufts, the next one cannot reach Magoun for at least:

| floor | rule holds |
|---|---|
| 240 s | 99.5% |
| 300 s | 98.2% |
| 360 s | 94.6% |

Useful as a "don't leave yet" suppressor.

**Beware parked trains.** An out-of-service train can sit on the inbound platform
for hours with a frozen `updated_at`; one was observed berthed for 8+ hours. Any
position older than 180 s is ignored (`_live()` in `service.py`), otherwise it fakes
a train upstream and silently disables the no-show veto.

**Time of day does carry usable signal**, in the left tail (leaving early is the
only direction that strands you):

| period | leaves >60 s early | p10 | median | p90 |
|---|---|---|---|---|
| Midday 10–15 | 10.5% | −71 s | +64 s | +277 s |
| AM rush 6–9 | 9.1% | −41 s | +49 s | +205 s |
| PM rush 16–17 | 6.5% | 0 s | +67 s | +219 s |
| Evening 18–23 | 5.0% | +3 s | +71 s | +303 s |

A leave-now alert should carry a larger margin midday than in the evening; the
model currently uses one margin all day.

Held-out backtest (8 days, scored on minutes spent standing on the platform):

| strategy | mean wait | median | P(wait > 5 min) |
|---|---|---|---|
| walk out blind | 5.71 min | 4.77 | 47.2% |
| schedule, 10th pct | 4.17 min | 2.18 | 26.5% |
| + live fusion (2-min walk) | 3.91 min | 2.00 | 24.6% |

Recommendations target the **early edge** of the arrival window, not the median:
median headway is 8.8 min, so arriving a minute late costs ~9 minutes.

**~9.6% of scheduled inbound trains never appear at Magoun.** That is the main
residual risk and what the "unconfirmed" flag is for.

## Layout

| file | role |
|---|---|
| `src/fetch_history.py` | download LAMP daily parquet |
| `src/glx.py` | platform IDs, event extraction |
| `src/build_dataset.py` | link Magoun arrivals back to upstream evidence |
| `src/model.py` / `src/fit.py` | conditional residual distributions → `data/model.json` |
| `src/backtest.py`, `src/backtest_fuse.py` | held-out evaluation |
| `src/record_rt.py` | archive the raw GTFS-RT feeds (run continuously) |
| `src/rollup.py` | distil raw archive to compact pairs; prune raw |
| `src/snapshot_schedule.py` | capture each day's schedule before the API drops it |
| `src/daily.sh`, `ops/` | launchd agents for the archiver and daily maintenance |
| `src/record_live.py` | older v3-API recorder, superseded by `record_rt.py` |
| `src/validate_live.py` | calibrate prediction error vs. lead time |
| `src/service.py`, `src/server.py`, `src/ui.html` | the live service |

## Historical feed archive: what exists

There is **no public archive of historical MBTA predictions**. Checked and ruled out:

| source | result |
|---|---|
| `mbta-gtfs-s3`, `mbta-busloc-s3` buckets | AccessDenied |
| LAMP `subway-on-time-performance-v1` | actuals only, no predicted times |
| LAMP `LAMP_ALL_RT_fields` (4.5 GB) | also actuals; derived from GTFS-RT, not predictions |
| `cdn.mbta.com/archive/` | 403 |
| transitfeeds.com / OpenMobilityData | dead (403) |
| Wayback Machine, `TripUpdates.pb` | ~17 scattered snapshots over 5 years — unusable |

Two things do exist, and both are now wired in:

**1. MBTA's own prediction-accuracy data** (`data/ref/pred_accuracy.csv`, weekly by
route and lead bin). Their "accurate" thresholds are asymmetric *against* the rider:

| lead bin | counts as accurate if the train arrives |
|---|---|
| 0–3 min | 60 s early → 60 s late |
| 3–6 min | 90 s early → 120 s late |
| 6–12 min | 150 s early → 210 s late |
| 12–30 min | **240 s early** → 360 s late |

"240 s early" is the train leaving four minutes before the predicted time — exactly
the case that strands you. Green-E clears even that lenient bar only **68%** of the
time in the 12–30 min bin (74–78% in the shorter bins).

**2. The raw GTFS-RT feed**, archived by `src/record_rt.py` from
`cdn.mbta.com/realtime/*.pb`. No API key, no rate limit, full system coverage, and
it carries the per-prediction **`uncertainty`** field that the v3 JSON API drops —
MBTA's own confidence estimate, in seconds. ~22 KB/snapshot at 15 s, gzipped daily.
This supersedes `record_live.py` (kept only to read its older files).

`src/validate_live.py` reads both formats and reports signed error against lead
time, plus realized error against MBTA's stated uncertainty.

## Storage and retention

The raw archive is a **staging area, not the store**. `src/rollup.py` distils each
day into (prediction, outcome) pairs — the only form the model ever reads — at 20x
compression:

| | per day | per year |
|---|---|---|
| raw `rt-*.jsonl.gz` | 13.8 MB | **5.05 GB** |
| rolled-up `pairs-*.parquet` (zstd) | 0.68 MB | **248 MB** |

```bash
uv run --with polars python src/rollup.py              # distil completed days
uv run --with polars python src/rollup.py --prune 14   # drop raw older than 14 days
```

Keep ~2 weeks of raw so a changed extraction can be re-run, then let it go.

**How long the data stays useful** differs sharply by what it measures:

| quantity | shelf life | why |
|---|---|---|
| run times (Ball→Magoun, Medford→Magoun) | indefinite | physical; track geometry and speed limits |
| minimum turnaround (~165 s) | indefinite | physical |
| schedule deviation, layover-vs-slack | one rating | tied to the active timetable |
| MBTA prediction error | trailing 30–60 days | their algorithm changes without notice |

MBTA republished GTFS **94 times in 2026**, but those are amendments; what matters
is the *rating* (Fall 2026 runs Sep 2 – Dec 12, so roughly quarterly). **Refit the
schedule-dependent numbers each rating.** The current 35-day window straddles the
Summer/Fall boundary: the two ratings are close (band 364 s vs 295 s, headway 9.0 vs
8.7 min), so pooling is not badly wrong, but Fall-only is tighter than the pooled
318 s quoted above.

## Caveats

- The ±75 s band on MBTA-sourced predictions is **provisional**. `validate_live.py`
  calibrates it from recorded data and needs a few days of `record_rt.py` first.
  At 9 arrivals the median signed error is negative at every lead bin (−16 s to
  −39 s, i.e. predicts slightly early — the safe direction), but the p90 at 5–10 min
  lead is +78 s, so the dangerous tail is real. Not yet enough data to set the band.
- The no-show veto is weakly validated: LAMP's terminus records are 38% incomplete,
  so "no train upstream" is often missing data rather than a missing train. The live
  vehicle feed does not have that gap, so the veto should do better than the
  backtest credits it for.
- Schedules from the v3 API only cover the active GTFS window (~8 days back).
