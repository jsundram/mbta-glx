# Magoun Square inbound ETAs

> **Roadmap and delivery status: [launch-plan.md](launch-plan.md)**
> **Working in this repo: [CLAUDE.md](CLAUDE.md)** — invariants that fail silently.
> **Buildout plan: [architecture.md](architecture.md)** — tiers, contracts, milestones.

The MBTA publishes ~13 predictions for the Medford/Tufts-bound platform at Magoun
Square but only reaches **8–13 minutes ahead** on the downtown-bound side — not
enough notice to leave the house. This builds longer-horizon inbound ETAs *with
error bars*, and sharpens them as trains move.

## Run it

```bash
uv run --with polars --with gtfs-realtime-bindings python src/server.py 8723
#   http://localhost:8723/board       live status board, self-updating
#   http://localhost:8723/?walk=6     ETAs with leave-by times
uv run python src/service.py 6                   # one-shot CLI, 6-minute walk
open web/index.html                              # the same board, no server at all
```

Publishing it, on the host that holds the archive:

```bash
./src/daily.sh                                   # rollup, score, suite, origin check
uv run python src/publish.py --check             # is web/ complete and current?
uv run python src/publish.py --commit            # commit what moved; never pushes
./src/refit.sh                                   # rebuild the dataset, refit, diff
```

`web/` **is** the static origin: a GitHub Actions workflow uploads it to Pages as-is,
so there is no second copy of `index.html` to fall behind, and `publish.py --check`
fails the deploy if any published artifact is stale or missing. `publish.py` moves
files; `fit.py` and `stats.py` are what write them.

**The static board** (`web/`) needs no backend: it fetches `api-v3.mbta.com`
directly and runs the same prediction in JavaScript, reading `model.json` for every
fitted number and every constant. It works from `file://`, which is why the model
is published twice — a page opened off the filesystem is not allowed to fetch a
sibling file, so `web/model.js` carries the same bytes as a script. The two
implementations are held together by `tests/test_contract.py`, which replays every
fixture through node and Python and compares the rows field for field.

**The board** leads with any service alert touching the Magoun-to-downtown
corridor — suspensions and closures elsewhere on the Green Line are filtered out so
the banner stays worth reading. A destination under a suspension is scored at 0%,
not at a slightly lower confidence.

Trains MBTA has declared will **skip Magoun** (or whose trip is cancelled) are
struck through in the upcoming list, never offered as an option, and trigger
recovery immediately if you had committed to one. Only the protobuf feed carries
these — the v3 JSON API drops a stop update that has no times, which is exactly
what a skipped stop looks like.

It carries a live GLX line map, drawn the way the T draws it — a thick
route-coloured line, white-centred station markers, the terminus capped — with Medford/Tufts at the top holding the
trains waiting to turn around, inbound running *down* the left rail toward Magoun
and outbound *up* the right rail toward the terminus. Trains are drawn as little cars — windscreen and
headlights at the leading end, so they never read as another station dot — each
labelled with its **lead car number** (`3620`, `3718`). That is the number painted
on the front of the train you board, taken from the feed's `carriages`, not an
internal id. Moving cars pulse, stale positions fade, non-revenue trains are
hatched, and trains waiting to turn around sit in the terminus pill. Because today's
inbound train is yesterday's outbound train, the right rail is a preview of the left.

The **notify me** button on the next train arms a push with no backend involved:
ntfy holds the message until the computed leave time, and it still fires after you
close the tab. First use asks for the topic and remembers it, and offers to
remember the command topic too — with that, the button also hands the train to the
notifier, which is what keeps the alert current once the page is gone.

That handoff matters because a scheduled ntfy message cannot be rescheduled at
all. `Sequence-ID` and `delete` collapse and dismiss notifications *in the app*;
they do not touch the server's queue, so every publish is a real delivery. The
page therefore arms exactly **once**, as the fallback for a notifier that is not
running, and refining it is the notifier's job — the only place it can be done.

Left open on a tablet it holds the screen awake via `navigator.wakeLock` where
supported, survives a browser-initiated reload, and refreshes immediately when the
screen wakes. If the tablet dies the alert still fires — ntfy holds it — and if the
notifier is running it is still being refined.

If the backend is reachable the footer also watches the **archiver**, and says so
when it has stopped writing — an un-captured day cannot be recovered, because the
schedules API only serves about eight days back. Unreachable is rendered as
nothing at all: a phone off the tailnet is the normal case, and a warning that
cries wolf is one you stop reading.

Below the map it scores itself: the **last 10 trains**, each showing when "leave
now" would have fired and whether you would have caught it
(`uv run python src/replay.py 390` for the same thing in the terminal).

It also answers one question at a glance: *next train expected in MM:SS*, or
*train at the station* with how long it has been there and how late it is against
the timetable. It ticks locally every 250 ms and re-polls every 10 s, so the
countdown stays smooth even between polls, and it reloads itself when the model
or the server changes under it — you never need to refresh by hand.

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
port, tunnel or static IP, and moves to a cloud host unchanged. The board's bell
uses the same topic to hand over an armed alert (`arm <eta> [vehicle]`), so an
alert armed at 08:00 on a page you then close is still tracked through a slip, a
no-show and a recovery.

It runs on this Mac for now, under `caffeinate` inside the launchd job. A lid close
still stops it; a tick that comes back late checks whether the walk still fits
before telling you to leave, so the failure is a missed nudge rather than a wrong
one.

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

## How it has been doing

The board scores itself daily and publishes the result as `stats.json`; the panel on
the board is that file. Two questions, kept apart because they are not the same one:

- **Coverage** — of the trains that actually came, how many did the app announce
  early enough to be standing on the platform for? `src/replay.py`, one rider per
  real arrival. **185 of 198** over 2026-09-24/25.
- **Cost** — once it fires, how long do you stand there, and how long from deciding
  to the doors? `src/simulate.py`, riders deciding every five minutes and following
  the real prediction through every tier. **3.7 min** on the platform, **15.6 min**
  door to train. Both from the same riders, which is the only way their difference
  means anything — platform wait alone rewards dawdling, since a strategy that keeps
  you home until it is certain scores perfectly on it while putting you on a later
  train.

`by_lead` is a third thing: raw prediction error by how much warning it gave, read
straight out of `data/pairs`. It is capped at a 20-minute lead, because past that the
rows are predictions mispaired to a vehicle's *next* visit rather than long-range
predictions — p50 error is −414 s at 20–30 min and −2188 s at 30–45 min, against
~620 s worst below that, and the feed's real reach on this platform is 8–13 min.

Each day is scored once and appended to `data/scores.jsonl`, so the window survives
the 90-day prune of the archive it was computed from.

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
| `src/simulate.py` | replay an archived day, score competing strategies |
| `src/q.sh` | ad-hoc SQL over the archive (DuckDB); replaces `zcat \| grep` |
| `src/replay.py` | score the last N trains against the leave-now advice |
| `src/stats.py` | score closed days into `data/scores.jsonl` → `stats.json` |
| `src/publish.py` | move the published set to the static origin; refuse a partial one |
| `src/refit.sh`, `src/refit.py` | a deliberate refit, and the diffs to read before accepting it |
| `src/rating.py` | watch for the schedule rating change that resets every constant |
| `.github/workflows/` | suite + weekly drift, Pages deploy, rating watch |
| `src/snapshot_schedule.py` | capture each day's schedule before the API drops it |
| `src/daily.sh`, `ops/` | launchd agents for the archiver and daily maintenance |
| `src/record_live.py` | older v3-API recorder, superseded by `record_rt.py` |
| `src/fetch_schedules.py` | one-shot schedule pull, superseded by `snapshot_schedule.py` |
| `src/backtest_berth.py` | where the berth tier's numbers came from; run by hand |
| `src/validate_live.py` | calibrate prediction error vs. lead time |
| `src/service.py`, `src/server.py`, `src/ui.html` | the live service |
| `web/index.html`, `web/app.js` | the same board with no backend; `app.js` ports `compute_rows` |
| `tests/run_cases.js`, `tests/board_smoke.py` | the JS side of the contract test; a browser check of the board |

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
