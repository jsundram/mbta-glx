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
