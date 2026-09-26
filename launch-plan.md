# Launch plan — Magoun inbound departure notifier

**Goal.** A push notification that tells me when to leave the house so I reach a
named destination by a named time, without missing the train and without standing
on the platform.

**Non-goals.** A general transit app. Beating the MBTA at prediction (we use their
predictions wherever they are sharper). Serving anyone but one rider at one origin.

Status key: `[ ]` not started · `[~]` in progress · `[x]` delivered

---

## Established measurements — do not re-derive

From 35 service days (2026-08-18 → 09-23), 4,418 observed inbound arrivals at
Magoun, MBTA LAMP. Re-fit per GTFS rating; **Fall 2026 runs Sep 2 – Dec 12**.

| quantity | value | note |
|---|---|---|
| median headway | 8.8 min | flat 6am–midnight; p95 15.7 min |
| Ball Sq departure → Magoun | ±14 s band | ~51 s of lead |
| Medford departure → Magoun | ±65 s band | ~192 s of lead |
| berth rule `max(sched+60, berth+165)` | ±299 s band | best estimator at walk distance |
| schedule alone | ±318 s band | median +80 s late |
| berthed at terminus (raw) | ±829 s band | unusable as a clock |
| minimum turnaround | 165 s median (p10 97 s) | physical floor |
| no train berthed ⇒ no arrival within | 240 s (99.5%) / 300 s (98.2%) | "don't leave yet" |
| scheduled trains that never run | **9.6%** | the main residual risk |
| Magoun → Park St ride | 18.3 / 21.2 / 25.4 min (p10/50/90) | **spread 7.2 min** |
| Magoun → Govt Center | spread 6.5 min | |
| Magoun → North Station | spread 4.8 min | |

**Two facts that shape the whole design.**

1. At a 6–7 min walk the train has *not* left Medford/Tufts, so we are predicting a
   dispatcher decision, not a moving object. Irreducible floor ≈ ±32 s even with
   perfect knowledge; realistic best ≈ ±150 s.
2. **The ride downtown is more uncertain than the boarding** (±7.2 min to Park St
   vs ±4.5 min to get on a train). Destination-time advice must be quoted as
   quantiles, never as "gets you there N minutes early".

---

## Decisions made (2026-09-24)

1. **Notification budget: 4 normal, 6 worst case.** Two per train (leave / adjust),
   covering the target train plus the one before it as the safety option; the extra
   pair is the recovery train if the target is missed.
2. **The rider picks the train.** The system proposes, you commit. The commit is
   also what arms the adjust-notification — see the open question below.
3. **Confidence is adjustable in the app**, with the real trade shown (below).
4. **One-offs, no recurring rules.** Destination and deadline change day to day.

---

## What "90% confident" actually buys  (measured, not asserted)

Monte Carlo over the measured schedule deviation, the 9.6% no-show rate, headway,
and the real Magoun→destination ride (`src/confidence.py`). Destination Park St:

| target | leave this early | if late: median | if late: p90 |
|---|---|---|---|
| 50% | 2.1 min | 3.3 min | 12.1 min |
| 75% | 5.4 min | 3.8 min | 12.5 min |
| **90%** | **10.4 min** | 3.7 min | 12.6 min |
| 95% | 14.1 min | 3.9 min | 12.6 min |
| 99% | 23.0 min | 3.7 min | 12.9 min |

**Raising confidence does not make you less late — it makes you late less often.**
The lateness column is flat at ~4 min median / ~12.6 min p90 across every target,
and holds for North Station and Copley too. That is because lateness is dominated by
the discrete event of missing a train, which costs a whole headway (8.8 min) no
matter how much buffer you left.

So the app's confidence control means exactly one thing: **how many minutes of
boredom you buy to reduce how often you are late.** Concretely —

- 90% → 95% costs **3.7 min** of extra daily buffer to halve the late rate.
- 95% → 99% costs **8.9 min** more to go from 1-in-20 to 1-in-100.
- When you *are* late, expect ~4 min; the bad day is ~13 min. That never improves.

The UI should say this in the rider's terms, e.g. *"late about one morning a
fortnight, usually by about 4 minutes"* — a frequency and a magnitude, not a
percentage.

---

## Phase 0 — Stop losing data  ◀ urgent, irreversible

- [x] `src/snapshot_schedule.py` — daily Green-E inbound schedule, all 26 stops,
      gzipped (27 KB/day, 10 MB/yr). **The v3 API only serves ~8 days back, so every
      un-captured day is permanently unavailable for backtesting.**
- [x] `src/record_rt.py` — raw GTFS-RT archiver (no key, no rate limit, carries
      MBTA's per-prediction `uncertainty`).
- [x] `src/rollup.py` — distil to (prediction, outcome) pairs, 20× compression
      (13.8 MB/day raw → 0.68 MB/day kept), prune raw after 14 days.
- [x] `src/daily.sh` + `ops/*.plist` — launchd agents, generated.
- [x] **Agents installed** — `com.magoun.archiver` (KeepAlive) and
      `com.magoun.daily` (11:30 / 21:30) are loaded and running.
- [x] `git init` + first commit.

**Acceptance:** archiver survives a reboot; `data/sched_full/` gains a file every
day without intervention; `data/pairs/` gains a file for each completed day.

### Runbook — changing what the agents capture

The agents will need updating whenever we capture more fields, more stops, or store
things differently. Expect this at least at Phase 3 (journey model needs downstream
stops) and whenever a GTFS rating turns over.

```bash
# 1. change src/record_rt.py (or snapshot_schedule.py / rollup.py)
# 2. reload -- edits are NOT picked up automatically, the archiver is long-running
./ops/install.sh          # idempotent: bootout then bootstrap, safe to re-run
launchctl list | grep magoun
tail -f data/live/archiver.err
```

**`launchctl bootout` is asynchronous.** Bootstrapping before the unload finishes
fails with `Bootstrap failed: 5: Input/output error` *and leaves the agent stopped* —
a reinstall that silently takes the archiver down. `ops/install.sh` now polls
`launchctl print` until the agent is really gone before bootstrapping. Never run it
with `sudo`: LaunchAgents load into your GUI domain (`gui/501`), root has none, and
launchctl reports only `125: Domain does not support specified action`.

**Schema changes must be additive.** `rollup.py` reads every archived day, including
ones written by older code, so never rename or repurpose a field — add a new one and
leave old records missing it. `data/pairs/*.parquet` is the long-term store; if its
schema must change, re-derive from raw within the 14-day prune window or the old
shape is permanent.

**Storage note:** the project lives in Dropbox, so data durability is handled, but
the raw archive churns **13.8 MB/day** through sync. The 14-day prune holds it at
~190 MB steady state. If Dropbox sync becomes a problem, move `data/raw/` and
`data/live/` outside the Dropbox tree — nothing in git depends on their location.

---

## The Sep 26 – Oct 5 works: what it does to the data

Nine days of Green Line suspension south of North Station, starting 03:00 on
2026-09-26. **Magoun and Medford/Tufts keep running**, so the local prediction
problem is unaffected in kind — but several fitted quantities are not safe to pool
across it.

| quantity | effect of the works |
|---|---|
| Magoun→downtown **ride times** | **absent**. No Green Line service to Park St, Govt Center, North Station, Haymarket, Boylston or Copley. Phase 3 gets nine dead days. |
| schedule deviation, layover, berth rule | **suspect**. Trains will short-turn on an amended timetable; turnaround behaviour at the terminus is exactly what changes. |
| headway, no-show rate | **suspect**, likely both worse than baseline. |
| MBTA prediction error | **suspect**. Disruption is a different regime for their model too. |
| run times Ball→Magoun, Medford→Magoun | **safe**. Physical, and that track is still in service. |

**Consequence for Phase 1: its clock effectively restarts 2026-10-05.** Of the next
two weeks, nine days are contaminated for everything except the physical run times.
Pooling them would bias the very calibration Phase 1 exists to produce.

- [x] **Archive alert state alongside the trains** (`record_rt.fetch_alerts`), so
      disrupted periods are self-identifying later instead of being nine
      unexplained days of odd numbers. Written only when the alert set changes.
      LAMP does publish `LAMP_RT_ALERTS.parquet` (130 MB) as a historical fallback,
      so unlike schedules this is recoverable — but not conveniently.
- [ ] **Exclude 2026-09-26 → 2026-10-05 when fitting** anything but run times.

### Querying: DuckDB for exploration, polars for the pipeline

Measured, not assumed:

| task | DuckDB | polars |
|---|---|---|
| 35 LAMP parquet files, grouped aggregate | 0.02 s | 0.04 s |
| unnest the gzipped archive, count skips at Magoun | **1.4 s** | needs a bespoke script |

**Not a scale argument.** The data is tiny — pairs are ~250 MB/year and polars
already lazy-scans parquet globs perfectly well. Both engines answer the same
question in hundredths of a second.

**It is an ergonomics argument, and a real one.** Nearly every analysis in this
project began as a throwaway script wrapping
`pl.concat([pl.read_parquet(f) for f in glob(...)])`. `src/q.sh` replaces that with
one line of SQL, and it reads the nested `.jsonl.gz` archive directly — so it is
also the honest replacement for `zcat | grep`.

The split worth keeping:

| | engine | why |
|---|---|---|
| pipeline (`fit`, `rollup`, `simulate`, service) | **polars** | typed, tested, same language as the rest; ships |
| ad-hoc exploration | **DuckDB** (`src/q.sh`) | SQL over a glob beats boilerplate |

Two conditions make this land better later than now, which is what "once we have
stabilized" gets right: SQL rewards a schema that has stopped moving (ours changed
three times in one day), and if the archive becomes parquet then DuckDB queries it
with no reconstruction at all.

`src/q.py` is deliberately analysis-only — nothing the live service or notifier
imports touches it, so it adds nothing to the deploy footprint.

### Storage format: why JSONL, and where it is wrong

Measured on a real day (320,622 prediction rows, 243,368 vehicle rows):

| format | MB/day | vs current |
|---|---|---|
| JSONL + gzip (current capture) | 9.63 | 1.0× |
| parquet + snappy | 3.18 | 3.0× |
| parquet + zstd | 2.18 | 4.4× |
| parquet + zstd, sorted by key | 2.09 | 4.6× |
| delta JSONL + gzip | 1.95 | 4.9× |
| **delta parquet + zstd, times as offsets** | **1.25** | **7.9×** |

**Raw protobuf is not an option.** The unfiltered feed is ~890 KB per poll, so
storing it verbatim is ~5 GB/day. Filtering to the corridor is what makes this
9.6 MB in the first place; the format is not where the volume comes from.

**JSONL is right for the capture tier**, for reasons unrelated to size:
- Append-only and crash-safe — every line is independent, so a kill loses at most
  one line. Parquet is columnar and batch-oriented: writing hourly means holding an
  hour in memory and losing it on a crash.
- Schema moved three times in one day (`unc`, `rel`, `alerts` all added mid-stream).
  Parquet wants a fixed schema per file.
- `zcat | grep` answered real questions repeatedly during debugging.

**JSONL is wrong for the archive tier.** An earlier comparison here was
apples-to-oranges — delta-JSONL against *full-state* parquet. Done fairly, delta
parquet is **1.62× smaller** as well as directly queryable. Two things do the work:
only changed rows are stored, and timestamps are kept as offsets from the snapshot
time, turning ten-digit epochs into small integers.

`src/archive.py` is now the compacted form (`data/live/day=YYYY-MM-DD/`), and
`q.sh` exposes it as the `arc_preds` / `arc_vehicles` views — DuckDB reads it with
no reconstruction at all.

### Compaction

`rollup.py --compact` re-encodes finished days as delta parquet, **6–7×** smaller
on real days, and only deletes the original if every snapshot round-trips exactly. Two bugs were caught by that check before anything was overwritten:
vehicles leaving the feed were never removed on reconstruction (silently dropping
1,595 pairs from one day), and a hand-picked field list missed `veh`. A third
appeared in the parquet version: **a null cannot mean "unchanged"**, because `veh`
and `dep` are legitimately null, so a field *changing to* null read as unmoved —
that corrupted 2,088 of 5,738 snapshots until an explicit changed-field bitmask
replaced it. In all three cases the verifier caught it before anything was
overwritten, and in the first case only after being widened to compare entire
snapshots rather than a chosen subset.

With compaction, pruning can move from 30 days to 90 and a full year of raw costs
~0.75 GB.

### Storage volume

Not a problem, and deliberately made bigger.

| | per day | 30-day steady state |
|---|---|---|
| before | 13.8 MB | 0.41 GB |
| now, with downtown platforms | **24.2 MB** | **0.73 GB** |

The increase is a choice, not drift: 201 of the 245 predictions per snapshot are
the downtown platforms the journey model needs, and those cannot be backfilled.
Prune extended from 14 to 30 days so the works period survives long enough to be
re-examined once we know what actually happened.

## Phase 1 — Accumulate  (passive, 2–4 weeks)

Calendar time is the constraint for *volume*, but accumulating and testing are
independent — the archiver runs regardless, and live testing does not consume it.
**Keep exercising the notifier daily while data accumulates.** Waiting is not
neutral: a silent capture bug (see the 30% arrival undercount) banks corrupt data,
and once `--prune 14` removes the raw archive the corruption is permanent.

- [ ] ≥ 300 Magoun inbound arrivals in `data/pairs/` with predictions at 5–10 min lead.
- [ ] ≥ 21 consecutive days of `sched_full` snapshots.
- [ ] **Measure prediction volatility, not just error.** A live run on 2026-09-24
      showed a Magoun inbound prediction move 16:23 → 16:31 → 16:23 within 90
      seconds. So some of the measured "error at 5–10 min lead" is *flap* rather
      than genuine uncertainty, and a prediction sampled once does not represent
      what the rider would have seen a minute earlier. Per (train, stop), compute
      the per-minute drift of its ETA and the size of the largest jump; report
      alongside the error quantiles. A stable ±90 s and a flapping ±90 s call for
      very different leave-now logic — the first can be trusted as quoted, the
      second needs smoothing or a wider quoted band.
      `data/pairs/*.parquet` already stores every (made_at, pred_arr) pair, so this
      is computable from the archive with no new capture.

**Acceptance:** the MBTA prediction-error table in `validate_live.py` is stable
across two consecutive weeks (quantiles move < 20 s week over week).

**Why this gates everything:** the current prediction-error numbers rest on **10
arrivals**. Every stated `±` on an MBTA-sourced ETA is a placeholder until this closes.

---

## Phase 2 — Finish the model, then stop  (bounded — resist scope creep)

We are near the physics floor; remaining headroom is small and the **evaluation set
(9 days) is now a weaker link than the model**. Two known-good improvements, then
switch from sharpness to calibration.

- [ ] Re-fit on Fall-2026 rating only (pooled band 318 s vs Fall-only 295 s — the
      current model is slightly pessimistic).
- [ ] Period-dependent margins. Early departures are the only direction that
      strands you, and they are not uniform:

      | period | leaves >60 s early | p10 |
      |---|---|---|
      | Midday 10–15 | 10.5% | −71 s |
      | AM rush 6–9 | 9.1% | −41 s |
      | PM rush 16–17 | 6.5% | 0 s |
      | Evening 18–23 | 5.0% | +3 s |

- [ ] **Calibration proof, not sharpness.** Quoted q10 must be the empirical q10.

**Acceptance:** over ≥ 21 held-out days, the realised miss rate at each quoted
quantile is within 3 points of nominal (q10 → 7–13% actual). Sharpness is explicitly
*not* an acceptance criterion.

**Out of scope:** new features aimed at narrowing the band. If band < 250 s, stop.

---

## Measured: do trains skip Magoun?

Essentially never, for revenue service. Given a trip that stopped on *both* sides:

| direction | trips | no stop at Magoun |
|---|---|---|
| inbound | 4,262 | 14 (**0.33%**) |
| outbound | 4,752 | 7 (**0.15%**) |

And most of those are feed gaps rather than skips: the Ball→Gilman elapsed time for
the 15 inbound cases has a **median of 217 s against a normal 210 s**, i.e. the
train stopped and the event was simply not recorded. Only the fast tail (p10 97 s)
looks like a genuine skip, putting real skips near **0.1%**.

A neighbouring stop shows *higher* miss rates (Gilman 0.77% inbound), which
confirms the measurement is dominated by capture gaps, not operations.

**Caveat — and a correction.** Attributing an observed express to a non-revenue
train was wrong. A rider reported twice riding an outbound evening train that
stopped at Lechmere or East Somerville, offloaded everyone not bound for
Medford/Tufts, and ran express. That is scheduled-service short-turning, not a
deadhead.

Re-run keyed by `vehicle_id` instead of `trip_id` (the trip is re-labelled
mid-run, which is why the first pass was blind to it), 254 outbound hops skip two
or more stops. But they do not survive a physics check:

- **174 are physically impossible** — e.g. Lechmere→Medford/Tufts in 2.1 min
  against a 12.2 min all-stops run. Feed gaps or vehicle-id reuse.
- **80 are plausible in duration, but take the all-stops time anyway**
  (338 s observed vs 324 s expected; 735 s vs 703 s). A real express saves the
  dwell at each skipped stop, roughly 40 s apiece. These did not.

So **LAMP shows no clear signature of express running**, while the rider has
observed it directly. The likely explanation is that such runs are re-labelled or
dropped from the performance dataset, not that they do not happen. Treat the 0.1%
skip figure as "skips that LAMP records", not as ground truth.

- [ ] **Detect express runs from the live archive instead.** `record_rt.py` samples
      vehicle positions every 15 s, so a train passing Magoun outbound without ever
      reporting `STOPPED_AT` there is directly observable, independent of how the
      trip is labelled. This is the only way to get a real rate.

## Alerts: the gap that mattered most

Found by asking what other feeds exist, and immediately non-hypothetical. On
2026-09-25 the alerts feed carried a **nine-day suspension** of Green Line service
south of North Station (Sep 26 03:00 – Oct 5 03:00), informing Park Street,
Government Center, North Station, Haymarket, Boylston and Copley. Magoun and
Medford/Tufts are *not* informed, so trains keep running and every local signal
looks healthy — while the journey model would have gone on quoting
"Park St by 8:47, 90% confident" for nine days with no train going there.

Now integrated: `service.alerts()` / `relevant()` / `blocking()`. A destination
under a suspension gets `p_ontime = 0`, and the board carries a banner.

Alerts are filtered to the **Magoun-to-downtown corridor**. The unfiltered feed
surfaced a Dean Road closure on the C branch and a Brigham Circle suspension on
the far end of the E — neither reachable from Magoun. A banner that fires on
irrelevant alerts trains the eye to ignore it.

## Other feeds worth taking, in value order

- [x] **Stop-level `SKIPPED` markers — integrated, with a caveat.** `record_rt.py` already archives
      `stop_time_update.schedule_relationship`, and it is *already populated*: 8
      distinct trips marked as skipping Magoun in the first day and a half, and the
      counts are identical across all inbound GLX platforms, i.e. whole trips
      declared as not serving the extension. **This is the no-show signal,
      announced rather than inferred**, and it is sitting unused in the archive.
      It may also be what the rider observed as "going express".

      **Measured lead time is poor.** The marker lands about **two minutes before**
      the scheduled arrival and lingers for an hour afterwards; on three of nine
      cases it appeared *after* the scheduled time. So it is a confirmation, not a
      warning, and it cannot save a 6.5-minute walk. What it is, is **definitive** —
      which the inferred no-show veto never was. Concretely it means:

      - a scheduled train MBTA says is skipping never appears as an option
      - a committed train that gets marked fires recovery **immediately**, bypassing
        the 240 s debounce built for a noisy feed
      - the archive can now label no-shows as ground truth instead of inferring them

      Coverage is partial: ~10 marked skips a day against a ~9.6% no-show rate on
      ~145 trains, so roughly a third of no-shows are announced and the rest stay
      silent. The veto still earns its place for the other two-thirds.
- [ ] **Trip-level `CANCELED`.** Seen live on the Green Line. Same purpose,
      stronger statement.
- [ ] **Occupancy.** `occupancy_status` per vehicle and per carriage — "the front
      car is jammed" is real information for a rider about to run for a train.
- [ ] **Bus fallback.** When the corridor is suspended, the useful answer is not a
      lower probability but a different mode. Magoun is served by bus routes; the
      same v3 API predicts them.

## Phase 3 — Journey model  (new; falls out of the ride-time finding)

Predicting arrival at Magoun is the smaller half of the problem.

- [ ] Extend the dataset from "arrival at Magoun" to "arrival at destination stop"
      for the downtown stops above.
- [ ] Ride-time distributions conditioned on time of day and boarded train.
- [ ] Use MBTA downstream predictions once aboard (they are good at this) to
      tighten the destination ETA mid-journey.

**Acceptance:** for "be at X by T", the quoted confidence is calibrated end-to-end,
not just to Magoun.

**Design consequence already known:** one extra train buys 8.8 min of margin, which
slightly exceeds the Park St ride spread (7.2 min). So "how many trains early?" is
usually *exactly one*, and the notifier should say which.

---

## Phase 4 — Notification engine

### Input
> "I need to be at **Park Street** by **9:00**." (one-off; recurring rules later)

### Output, morning brief
> Take the **8:21** — reaches Park by **8:52**, 8 min spare, **90%** confident.
> Fallback **8:12** — 17 min spare, 97% confident.
> The **8:29** only makes it **55%** of the time.
> Service today: **normal** (headway 8.6 min vs 8.8 baseline, 1 no-show so far).
> I'll start watching at **8:04**.

Note the shape change from the original sketch: **confidence, not "minutes early"** —
a 6-minute cushion at Park St has a ±3.6 min tail on it.

### Revisions

An ETA that moves must **say so**, repeatedly, rather than the system quietly
re-deciding. Any move of 90 s or more is announced (no more often than every 90 s,
so revisions cannot spam), and the sequence a rider should see is:

> Your train may be running late — now expected 5:19
> Updated arrival time — now expected 5:11
> Updated arrival time — now expected 5:13

Critically, a commitment is **no longer dropped** when the match is lost. After the
240 s debounce the best candidate is adopted *and announced*, because a flap of one
headway is indistinguishable from a no-show, and following the wrong train loudly
beats abandoning the right one silently.

### Triggers
- [ ] **Leave now** — fires once, at the q10 departure time for the chosen train.
- [ ] **Start jogging / ease up** — mid-walk, ~3 min in, when the train clears
      Medford/Tufts (±33 s) or Ball Sq (±7 s). Countdown, not prose.
- [ ] **Recovery** — the chosen train no-shows (~9.6% of the time). *"That one
      vanished. Next is 8:29, still 70% for your 9:00."* This is a core feature.
- [ ] **Suppression** — no notification while the do-not-leave floor holds
      (nothing berthed ⇒ ≥ 240 s clear at 99.5%).

### Resolved: how it knows you left

ntfy's `http` action button posts back to a second topic, so the **On my way** button
on the leave-now push tells the service directly. No inbound port, no tunnel, no
static IP, and the notifier stays host-portable. If it is never tapped, the adjust
push simply does not fire — a missed nudge rather than a wrong one.

**Known limitations, all found by testing:**

- ntfy.sh does not replay cached messages for anonymous topics, so a tap made while
  the watcher is down is lost. Acceptable — tap again.
- `since=now` is **not** valid on the subscribe stream (HTTP 400); only durations,
  timestamps, message ids and `all` are. Subscribe with no `since` at all.
- Priority 5 does **not** pierce an iOS Focus mode. ntfy must be allowed through
  Focus manually, or the leave-now alert is silent — which is the whole product
  failing. This is the strongest argument for Pushover (5c) on another device.
- Subscribe on the phone to the outbound topic **only**. Subscribing to the command
  topic echoes your own taps back and looks like the notifier spamming you.

### Superseded — how does it know you left?

The adjust ("start jogging") notification needs a reference point. Three options:

1. **Assume you left at the leave-now time.** Zero friction, wrong whenever you
   dawdle — and dawdling is exactly when you need the nudge.
2. **Tap to confirm** on the leave-now push. One tap, and it doubles as the commit
   from decision 2. Probably the right default.
3. **Location.** Accurate, no friction, but needs the native app from 5c.

Start with (2), since committing to a train is already the interaction.

---

## Replay harness: strategies as experiments

`src/simulate.py` replays an archived day and scores competing strategies against
it. The archive samples everything every 15 s, so any instant can be reconstructed
exactly; a whole day against six strategies runs in **seconds**, not mornings.

First run, 2026-09-25, 6.5-minute walk, riders deciding every 5 minutes:

| strategy | platform wait (mean/median) | >5 min | door-to-train (mean/median) |
|---|---|---|---|
| MBTA only, q10 | 3.79 / 3.03 min | 29% | 44.4 / 16.6 min |
| schedule only, q10\* | 7.42 / 7.29 min | 75% | 102.4 / 74.5 min |
| both, q05 | 4.31 / 3.54 min | 36% | 43.8 / 16.1 min |
| **both, q10** | 4.14 / 3.04 min | 33% | 43.9 / 16.1 min |
| both, q20 | 4.07 / 3.04 min | 33% | 43.7 / 16.1 min |
| both, q35 | 4.14 / 3.04 min | 34% | 44.0 / 16.1 min |

**The quantile barely matters.** Everything from q05 to q35 lands within 0.3 min of
everything else. We have spent a lot of this project reasoning about where on the
arrival distribution to aim; on this evidence that is not the lever. Adding the
schedule tier buys a slightly earlier train (door-to-train 16.06 vs 16.57 median)
at the cost of slightly more platform time — the trade we designed for, now
measured rather than argued.

**The harness also exposed a flaw in the metric used all along.** Platform wait
alone *rewards dawdling*: a strategy that keeps you at home until it is certain
scores beautifully on it while putting you on a later train. `door-to-train`
(decision → boarding) is the honest measure and is now reported alongside.

\* Not a clean ablation: the tier filter runs on `etas()` output, which has already
fused and de-duplicated tiers, so an MBTA row suppresses a nearby schedule row.
"Schedule only" here means "schedule rows that survived fusion", which is why it
looks worse than a true schedule-only strategy would.

**Caveats:** one day, one walk time, and mean door-to-train is inflated by riders
who decide at 3am — the medians are the meaningful figures.

- [ ] Run across many days once the works period ends and Phase 1 has clean data.
- [ ] Build a true tier ablation that bypasses fusion.

## Self-scoring: the last 10 trains

`src/replay.py` replays the recorded prediction stream against observed arrivals and
asks the only question that matters: **when would "leave now" have fired, and would
you have made it?** Shown on the board, refreshed every minute.

Both sources the service quotes are replayed — MBTA's own prediction where one
exists, and the timetable before that — because scoring MBTA predictions alone
understates the app, the schedule being what carries the horizon past ~13 minutes.

First run, 12 evening arrivals at a 6.5-min walk: **caught 9/12, mean wait 3.2 min**.
The three failures were all the *same* failure — the first usable prediction arrived
less than a walk-time before the train, so no advice was possible. That is precisely
the gap this project exists to close, and it is now measured continuously rather
than argued about.

Two replay traps, both hit while building it:
- A vehicle passes Magoun many times a day, so its prediction stream must be
  windowed to the current visit; otherwise predictions from hours earlier look like
  hours of warning.
- Windowing by the previous arrival is not enough — a *missed* intervening arrival
  lets predictions aimed at the previous visit through. The prediction must also be
  about this arrival (within 15 min of it).

## Status board

`http://localhost:8723/board` — one screen, no interaction: next train in MM:SS, or
"train at the station" with dwell time and lateness against the timetable. Ticks
locally at 250 ms between 5 s polls so the countdown is smooth, keeps painting from
the last good reading when the server is unreachable (and marks itself stale), and
self-reloads when the server restarts with new code.

Trains on the map carry their **lead car number** from the feed's `carriages`
field (`label` is the coupled pair, e.g. `3620-3819`). That is the number on the
front of the train, so what the board says matches what the rider sees pull in —
worth far more than the internal `G-10222` vehicle id.

A live GLX line map sits under the countdown: terminus at top, inbound running down
the left rail, outbound running up the right. The right rail is the long-horizon
signal made visible — those trains become the inbound ones after the turnaround.

Built as a debugging surface as much as a product: the failure modes in this
project are all *temporal*, and watching predictions move in real time makes them
obvious in a way that reading logs after the fact does not.

## How long does daily updating stay worth it?

Measured by refitting on random subsets and comparing to the 35-day estimate
(median absolute error, seconds):

| quantity | 35-day | 1d | 3d | 8d | 21d | quoted band |
|---|---|---|---|---|---|---|
| run_ball q10 | 46 s | 1 | 0 | 0 | 0 | ±14 s |
| run_med q10 | 169 s | 3 | 2 | 1 | 0 | ±65 s |
| layover q50 | 568 s | 50 | 40 | 24 | 10 | — |
| sched_dev q10 | −22 s | 38 | 25 | 14 | 5 | ±318 s |
| sched_dev q90 | 296 s | 64 | 69 | 18 | 12 | ±318 s |
| headway q50 | 528 s | 33 | 22 | 21 | 8 | — |

**The physical quantities are finished.** Run times converge in *one or two days*
and then never move — they are track geometry and speed limits. Collecting more
data to refit them is pure waste.

**The schedule-dependent ones converge in one to two weeks**, and then the residual
error is small against the band we actually quote: after ~8 days `sched_dev q90` is
within 18 s of its final value on a 318 s band. Past about two weeks per rating,
extra days buy essentially nothing.

### So the cadence should change with time

| | early (first ~2 weeks of a rating) | later |
|---|---|---|
| capture | **essential** | **keep** — but for other reasons (below) |
| refit | **daily, it is still moving** | **per rating (~quarterly), not daily** |

Daily *refitting* has a short shelf life. Daily *capture* stays worth it, for
reasons that have nothing to do with converging an estimate:

1. **Rating boundaries.** Every new timetable resets the schedule-dependent half.
   Fall 2026 ends Dec 12; the numbers must be re-earned then, from scratch.
2. **MBTA's prediction model drifts** and they do not announce changes. This is
   the one quantity genuinely wanting a trailing window rather than a converged
   estimate — and we have nowhere near enough history to say how fast it moves.
3. **Change detection.** A converged estimate is only useful if you notice when it
   stops being true. Cheap capture is what makes a drift alarm possible.
4. **The board's self-scoring and the replay harness** both need recent days.
5. **Forensics.** The Sep 26 works are the example: understanding an odd fortnight
   later requires having captured it at the time.

- [ ] Replace the daily refit with: refit on rating change, plus a weekly drift
      check that alarms when a fitted quantile moves more than its convergence
      noise (roughly: >20 s for `sched_dev`, >2 s for run times).

## Deploy architecture

Verified 2026-09-25, not assumed:

| endpoint | CORS | usable from a browser? |
|---|---|---|
| `api-v3.mbta.com` (predictions, vehicles, alerts, schedules) | `access-control-allow-origin: *` | **yes** |
| `cdn.mbta.com/realtime/*.pb` (TripUpdates, Alerts) | none | **no** — server only |

A throwaway static page (`web/proof.html`) fetched live predictions cross-origin
*and* loaded `model.json` as a sibling asset, with no server involved. So the
split the rider asked for is real:

```
  static site  ──fetch──>  api-v3.mbta.com        (live, CORS, no key needed)
   (Pages/CDN) ──fetch──>  model.json, stats.json (published by the backend)
                ──fetch──>  live-extras.json      (the one thing browsers cannot get)

  backend (small, always-on)
     record_rt.py    continuous capture  ──>  data/live, data/pairs
     daily.sh        rollup + fit        ──>  publishes model.json / stats.json
     skips publisher every ~30 s         ──>  live-extras.json
     watch.py        the notifier (needs always-on scheduling)
```

### What has to stay server-side, and why

1. **The protobuf feeds.** No CORS, and they carry the `SKIPPED` / `CANCELED`
   markers that the v3 JSON API drops. The backend publishes a small
   `live-extras.json` (a list of skipped trip ids) every ~30 s.
2. **The archive.** History is the point; a browser cannot accumulate it.
3. **The notifier trigger.** Must fire at a wall-clock instant whether or not any
   page is open. macOS sleeping is exactly why the Mac cannot be the final host.

### Hosting

| piece | where | cost |
|---|---|---|
| static board | GitHub Pages / Cloudflare Pages | £0 |
| model.json / stats.json | committed artifacts, published by CI | £0 |
| daily rollup + refit | GitHub Actions cron (daily granularity is fine) | £0 |
| archiver + skips + notifier | Raspberry Pi, or fly.io / small VPS | £0–5/mo |

GitHub Actions is fine for the *daily* work and useless for the trigger: 5-minute
granularity, routinely 5–15 minutes late.

### The fork worth deciding before building

Going fully static means **the tier logic runs in the browser**, so it exists twice:
Python for fitting, backtesting and `simulate.py`; JavaScript for the live page.
That is a genuine divergence risk — the two could disagree and only the Python one
is tested.

Mitigation, and the reason it is still the right call: keep the **model as data**.
`model.json` already holds every fitted quantile, so the JavaScript does lookup and
arithmetic (~80 lines), not modelling. Anything that fits, simulates or scores stays
in Python and never ships to the browser.

The alternative — a thin backend serving `/status`, frontend stays dumb — is less
work today and gives up the static property the rider asked for. Not recommended,
but it is the fallback if the JS port starts growing.

- [x] **Port the tier selection to JS.** `web/app.js` + `web/board.html`: lookup and
      arithmetic only, every constant read from `model.json`, and a missing constant
      throws instead of falling back to a literal. Verified in a browser from
      `file://` with the Mac server stopped — which turned up that a page opened off
      the filesystem cannot fetch a sibling file at all (Chromium: `URL scheme
      "file" is not supported`), so `fit.py` publishes `web/model.js` too.
- [x] **A contract test that the JS and Python tier logic agree.** 28 fixtures
      through both implementations, every field of every row compared; drift
      injection table in architecture.md §4.
- [ ] Backend publisher for `live-extras.json` (skips) every ~30 s
- [ ] GitHub Actions: daily rollup, refit, commit `model.json` + `stats.json`
- [ ] Pick the always-on host (Pi vs fly.io) — still the open Phase 5 decision
- [ ] **A v3 sidecar for `revenue`.** Live serving already merges both feeds — v3
      for predictions and positions, protobuf for `SKIPPED`/`CANCELED`. The archive
      does not: GTFS-realtime's `VehiclePosition` has no revenue field, so no
      archived snapshot can carry one and no sampled fixture can contain a deadhead
      (measured: 0 of 26). Cheapest fix is a sidecar, not a second full capture —
      poll v3 `/vehicles` every ~60 s for `(t, vehicle_id, revenue)` only, ~1,440
      calls/day against the keyless 20 req/min limit, and join it in at rollup
      rather than in the archiver (invariant 6: the archiver must stay dumb). Then
      fixtures can sample real deadheads instead of synthesising them.

## Phase 5 — Delivery

### Start on this Mac, move to cloud later — yes, if we hold one line

The notifier must fire at ~08:04 with ~30 s precision. macOS sleeps, and `launchd`
runs a missed job *on wake*, far too late. So the Mac is fine to **start** on (develop
against it, prove the loop) but is not the end state.

Migration stays cheap if we hold this architectural rule from the first line of code:

> **The notifier depends only on (a) the MBTA API and (b) a small bundle —
> `model.json`, today's schedule snapshot, and a few KB of state. Never on the raw
> archive.**

Everything needed to fire a notification is < 100 KB. The archiver and its 190 GB-class
history can stay on the Mac (or stop entirely) without the notifier caring.

- [x] Config via env (`MAGOUN_ROOT`, `MAGOUN_WALK_S`) — no absolute paths in code.
- [x] Real timezone (`ZoneInfo("America/New_York")`). **Was hardcoded to EDT**, which
      would have silently broken every schedule lookup on 2026-11-01.
- [ ] `--once` tick mode so a cloud cron can drive it without a long-running process.
- [ ] No `launchd` assumptions inside application code; scheduling stays in `ops/`.

Migration then = copy the bundle, set env vars, deploy. Hosts, when that day comes:
Raspberry Pi (~$50 once), VPS/fly.io ($0–5/mo), or Lambda + EventBridge (1-min
granularity). GitHub Actions cron is out — 5-min granularity and routinely 5–15 min late.

### Channel: three real options, in increasing cost

**(a) Off-the-shelf app — nothing to build.**

| app | cost | pierces Do Not Disturb | action buttons |
|---|---|---|---|
| **ntfy** | free | no | **yes — incl. `http`** |
| **Pushover** | $5 once | yes (priority 2 = emergency, retries until acked) | limited |

**ntfy's `http` action button solves the open question above for free.** A notification
can carry a "Leaving now" button that POSTs straight back to the service — that single
tap both commits the train and arms the countdown, with zero app development. ntfy also
supports up to 3 buttons, priority 1–5, and scheduled/updatable messages via
`sequence_id`. Pushover's edge is emergency priority, which pierces quiet hours — likely
worth $5 once a silenced 08:04 alert costs a real missed train.

**(b) PWA — yes, and probably the end state.** iOS has supported Web Push for
home-screen web apps since 16.4, so a PWA gets both the morning-brief UI (see options,
tap to commit) *and* real push, with **no App Store and no $99/yr**. Constraints: must
be added to the Home Screen (not just a Safari tab), HTTPS only, permission from a user
gesture, subscription dies if it is removed from the Home Screen, no Live Activities,
and no critical-alert/DND bypass. For a brief you read at 07:50 and a trigger at 08:04,
none of those bite.

**(c) Native — only for the ticking countdown.** A live-updating lock-screen countdown
is an iOS Live Activity, which genuinely requires a native app. On distribution: the
App Store concern is real (guideline 4.2, minimum functionality) but **irrelevant, because
it never needs to ship publicly**:

- Free Apple ID sideload — profile expires every **7 days**. Impractical.
- **$99/yr developer account + TestFlight** — 90-day builds, internal testers, no public
  review. This is the normal way to run a personal app.
- React Native / Expo speeds up the ordinary app, but Live Activities still need a native
  module, so RN does not avoid either the $99 or the native work.

**Before paying for (c), test whether the countdown is needed at all.** Two or three
timed pushes ("train in 2 min" / "1 min" / "now") approximate a ticking countdown over a
3–4 minute walk segment, and they fit inside the 4–6 notification budget already agreed.

### Recommended ladder

- [x] **5a — ntfy, on this Mac.** Built and verified: `notify.py` (transport),
      `brief.py` (options + P(on time)), `watch.py` (triggers + commands),
      `ops/com.magoun.watch.plist`. Live delivery confirmed against ntfy.sh with
      priority 5 and `http` action buttons intact.
      **Remaining:** install the ntfy app, subscribe to both topics, run
      `./ops/install.sh`, then a real morning to shake it out.
- [ ] **5b — PWA** for the morning brief and train picking. Free, no store, likely final.
- [ ] **5c — Pushover** if leave-now must pierce Do Not Disturb.
- [ ] **5d — native app** only if discrete pushes prove insufficient for the countdown.

### Also in Phase 5

- [ ] Scheduler that arms watches from the day's brief and fires triggers.
- [ ] Health check: the archiver dying silently is the top failure mode — during
      this build two recorders ran simultaneously unnoticed for 20 minutes.

---

## Engineering debt (Phase 3 of your plan, made concrete)

- [x] `git init` — done; data excluded via `.gitignore`, Dropbox covers durability.
- [x] `tests/test_regressions.py` — one test per bug that actually shipped. Runs in
      0.12 s and now runs as part of `src/daily.sh`. Covers: the re-match jumping to
      the next train, arrivals counted per-visit not per-vehicle, `?since=now`
      returning HTTP 400, command posts at min priority, destination name resolution.
- [ ] Still untested by anything automated: epoch µs-vs-s, stale vehicle→leg links,
      quantile-vs-median mixing, parked-train ghosts.
- [ ] `model.json` schema version + a refit script that fails loudly on drift.
- [ ] Monitoring: alert if `data/pairs/` or `data/sched_full/` misses a day.

---

## Known traps

- **Never widen a re-match far enough to reach the next train.** The first fix for
  prediction flap used a ±900 s window; with an 8.8 min headway that silently
  re-pointed the commitment at the *following* train, twice in a row (+6.9 min each
  time), so leave-now never fired while real trains came and went. Match on vehicle
  identity first, and cap positional drift at 0.45 × headway.
- **Count arrivals as transitions into STOPPED_AT.** Keying on `(vehicle, stop)` for
  a whole day records only each vehicle's first visit; trains cycle through Magoun
  repeatedly, measured at a **30% undercount over three hours**. This corrupted the
  prediction-error calibration until fixed.
- **Non-revenue trains are in the live feed and run express.** They appear in
  GTFS-RT VehiclePositions like any other train, so without filtering, a deadhead
  parked at Medford/Tufts satisfies the no-show veto and one passing Magoun reads
  as "train at the station". The v3 JSON API exposes `revenue`
  (`REVENUE`/`NON_REVENUE`); **the protobuf feed does not** — its TripDescriptor
  carries only the standard fields. They are excluded from the veto, the berth
  tracker and at-station detection, and drawn dashed on the map.
- **A vehicle missing from one poll has not left the platform.** Treating absence as
  departure reset the dwell counter to zero and then made it jump. Grace period of
  75 s for vehicles missing from the feed; a vehicle *seen elsewhere* clears at once.
  Display order is by arrival time so two trains at the platform never swap places.
- **MBTA predictions flap by ~8 minutes for ~90 seconds.** Measured twice on
  2026-09-24: `16:23 → 16:31 → 16:23` and `17:19 → 17:11`, both about 90 s long,
  both the same vehicle throughout. Consequences, each learned the hard way:
  - A single bad tick must not trigger anything (first false recovery).
  - The debounce must be **longer than the flap**. An 80 s debounce fired 25 s
    before the feed corrected itself. Now 12 ticks ≈ 240 s.
  - The re-match window must never reach the next train (headway 528 s), or the
    commitment silently slides forward and leave-now never fires.
  - **A flap of one headway is indistinguishable from a no-show by timing alone.**
    Only the vehicle id separates them. When the commitment came from a schedule
    row there is no vehicle yet, so this case cannot be resolved — the notifier
    therefore *asks* ("your train may be running late") rather than asserting.

- **Parked trains.** An out-of-service train sat berthed 8+ hours with a frozen
  `updated_at`; it faked a train upstream and silently disabled the no-show veto.
  Positions older than 180 s are ignored — keep it that way.
- **`trip_id` is reassigned** across the terminus turnaround. Link by `vehicle_id`.
- **LAMP gaps**: terminus arrival records are 38% incomplete; berth records ~10%.
  Live feed does not share these gaps, so backtests *understate* live veto quality.
- **MBTA's own accuracy bar is asymmetric against the rider** — at 12–30 min lead it
  counts a train arriving 240 s *early* as accurate. Do not inherit their metric.
