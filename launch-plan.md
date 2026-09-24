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

## Phase 0 — Stop losing data  ◀ urgent, irreversible

- [x] `src/snapshot_schedule.py` — daily Green-E inbound schedule, all 26 stops,
      gzipped (27 KB/day, 10 MB/yr). **The v3 API only serves ~8 days back, so every
      un-captured day is permanently unavailable for backtesting.**
- [x] `src/record_rt.py` — raw GTFS-RT archiver (no key, no rate limit, carries
      MBTA's per-prediction `uncertainty`).
- [x] `src/rollup.py` — distil to (prediction, outcome) pairs, 20× compression
      (13.8 MB/day raw → 0.68 MB/day kept), prune raw after 14 days.
- [x] `src/daily.sh` + `ops/*.plist` — launchd agents, generated.
- [ ] **Install the agents**: `./ops/install.sh` (needs your approval — installs two
      `~/Library/LaunchAgents` entries that run at login and survive reboot).
- [ ] `git init` + first commit. Nothing is version-controlled yet.

**Acceptance:** archiver survives a reboot; `data/sched_full/` gains a file every
day without intervention; `data/pairs/` gains a file for each completed day.

---

## Phase 1 — Accumulate  (passive, 2–4 weeks)

Nothing to build; the constraint is calendar time.

- [ ] ≥ 300 Magoun inbound arrivals in `data/pairs/` with predictions at 5–10 min lead.
- [ ] ≥ 21 consecutive days of `sched_full` snapshots.

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

### Triggers
- [ ] **Leave now** — fires once, at the q10 departure time for the chosen train.
- [ ] **Start jogging / ease up** — mid-walk, ~3 min in, when the train clears
      Medford/Tufts (±33 s) or Ball Sq (±7 s). Countdown, not prose.
- [ ] **Recovery** — the chosen train no-shows (~9.6% of the time). *"That one
      vanished. Next is 8:29, still 70% for your 9:00."* This is a core feature.
- [ ] **Suppression** — no notification while the do-not-leave floor holds
      (nothing berthed ⇒ ≥ 240 s clear at 99.5%).

### Open questions — need your call
1. **Notification budget.** Your sketch says "notify me about all trains from
   [time0]". At 8.8 min headway that is 3–4 pushes. Cap at 2 (leave + adjust) with a
   third only on recovery?
2. **Who chooses the train** — does it pick one, or present options and let you
   commit? Committing makes the recovery path much better.
3. **Confidence target.** 90% on-time at destination is the natural default. Higher
   means leaving meaningfully earlier.
4. **Recurring rules** ("weekdays, Park St by 9:00") or one-off each day?

---

## Phase 5 — Delivery

- [ ] Pick a channel. **ntfy.sh or Pushover ≈ 1 hour**; APNs ≈ days. Not yet decided,
      and it constrains Phase 4.
- [ ] Scheduler that arms watches from the day's brief and fires triggers.
- [ ] Health check: the archiver dying silently is the top failure mode — during
      this build two recorders ran simultaneously unnoticed.

---

## Engineering debt (Phase 3 of your plan, made concrete)

- [ ] `git init` — nothing is versioned.
- [ ] Tests for the logic that has already produced silent bugs: epoch µs-vs-s,
      stale vehicle→leg links, quantile-vs-median mixing, parked-train ghosts.
      Every one of these was caught by chance, not by a check.
- [ ] `model.json` schema version + a refit script that fails loudly on drift.
- [ ] Monitoring: alert if `data/pairs/` or `data/sched_full/` misses a day.

---

## Known traps

- **Parked trains.** An out-of-service train sat berthed 8+ hours with a frozen
  `updated_at`; it faked a train upstream and silently disabled the no-show veto.
  Positions older than 180 s are ignored — keep it that way.
- **`trip_id` is reassigned** across the terminus turnaround. Link by `vehicle_id`.
- **LAMP gaps**: terminus arrival records are 38% incomplete; berth records ~10%.
  Live feed does not share these gaps, so backtests *understate* live veto quality.
- **MBTA's own accuracy bar is asymmetric against the rider** — at 12–30 min lead it
  counts a train arriving 240 s *early* as accurate. Do not inherit their metric.
