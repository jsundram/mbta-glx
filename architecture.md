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
│  index.html + app.js                                       │
│    ├─ fetch  api-v3.mbta.com          live, CORS, no key   │
│    ├─ fetch  model.json               fitted quantiles     │
│    ├─ <script> model.js               same bytes, file://  │
│    ├─ fetch  stats.json               how it has been doing│
│    ├─ fetch  <backend_url>/skips      skips (no CORS path) │
│    ├─ fetch  <backend_url>/capture    is the archiver alive? │
│    └─ POST   ntfy.sh                  arm its own alerts   │
└────────────────────────────────────────────────────────────┘
        ▲ published artifacts              ▲ scheduled push
┌─ BACKEND (Mac now, Pi/VPS later) ─────────────────────┐   │
│  record_rt.py   continuous capture  → data/live/       │   │
│  rollup.py      distil + compact    → data/pairs/      │   │
│  fit.py         refit per rating    → model.json       │   │
│  stats.py       score closed days   → stats.json       │   │
│  publish.py     move artifacts to the static origin    │   │
│  server.py /skips  the skip set, parsed from protobuf  │   │
│  watch.py       refine alerts, brief, recovery  ───────────┘
└────────────────────────────────────────────────────────┘
```

Three things can only run server-side, and nothing else needs to:

1. **The protobuf feeds.** No CORS, and the only source of `SKIPPED` / `CANCELED`.
2. **The archive.** A browser cannot accumulate history.
3. **Proactive triggers** with no page open (morning brief, post-close refinement).

An always-on tablet does **not** cover (3), which was the M2 assumption: a
scheduled ntfy message cannot be re-pointed, so the page can only add deliveries,
never refine one. The board arms once as a fallback and hands the train to
`watch.py` (M5). A tablet cannot cover (1) or (2) either.

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

### `GET /capture` — the archiver's heartbeat

```jsonc
{ "as_of": 1790450599, "stale_after_s": 600 }
```

The mtime of the newest `rt-*.jsonl.gz`, which is exact: the archiver opens,
appends and closes once per snapshot. The newest file rather than today's by
name, or the few seconds after midnight would read as a dead archiver.

The second entry in `BROWSER_ROUTES`, and it passes the same test as the first —
a browser cannot know when a file on this Mac was last written. It is liveness,
not computed rows, so the static property is untouched: with the backend
unreachable the board says nothing and works exactly as before. That third state
matters. **Unreachable must not look like dead**, or the warning is worse than
useless — a phone off the tailnet is the normal case.

`600 s` against a 15 s write cadence, and the largest ordinary gap measured
across a 15-hour day was 18 s.

### `GET /skips` — what the browser cannot fetch

```jsonc
{ "as_of": 1790281459, "trips": ["77745376", ...], "ttl_s": 60 }
```

A **live endpoint**, not a published file — see §5. That is why the fields are
named as they are. `as_of` is when the set was last *successfully* derived, which
is not when it was served: `service.skipped_trips` hands back its previous set
when cdn.mbta.com fails, so a healthy 200 can carry a stale answer and only this
field separates "no skips" from "I cannot see skips". `0` means never fetched.
The endpoint returns 200 with a stale set rather than 503 for the same reason — a
503 collapses both cases into an empty set at the board.

It is **not a proxy.** The feed is ~1 MB of protobuf and the answer is ~10 trip
ids for one stop, so the parse happens here. And it is named for the one thing it
serves rather than "extras": a bag invites a second thing in it, and the whole
point of `BROWSER_ROUTES` is that there is never a second thing.

### `stats.json` — self-scoring, published daily

```jsonc
{ "as_of": "2026-09-26", "window_days": 7,
  "caught": 61, "of": 70, "mean_platform_wait_s": 168,
  "mean_door_to_train_s": 964,
  "by_lead": [{"bin": "5-10min", "n": 104, "p10": -61, "p50": -39, "p90": 78}] }
```

`web/index.html` reads these field names directly and hides its panel when the file
is absent or unparseable — so a missing `stats.json` degrades quietly, but a
*renamed* field shows an empty panel instead of failing. Keep the names.
`tests/test_stats.py` pins them from both sides, reading the names out of
index.html itself rather than a list typed twice.

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
~1.25 MB/day (measured), directly queryable by DuckDB. **Never pruned** — this
said "pruned at 90 days" and that was wrong. `rollup.py --prune 90` deletes the
*uncompacted* `rt-*.jsonl.gz` only, and only once `data/pairs` holds that day, so
nothing has ever deleted a compacted day. See §5.

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

`web/index.html` + `web/app.js` run with no backend: `src/status.html` with its
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
  only the sibling assets needed the fallback. `stats.json` degrades to a hidden
  panel, and the skip set to an empty one.

`tests/board_smoke.py` drives the real page (playwright, MBTA and ntfy stubbed,
~90 s, not collected by pytest) and checks 18 properties of it, including the
re-arm — measured at 60 s apart, which is the only reason to leave a tablet open.

**Done when:** the board runs from `file://` with the Mac server stopped, an iPad
left open keeps re-arming an alert, and both implementations agree on every
fixture. All three verified.

### M3 — Publish pipeline — **done**

**The static origin is `web/` in this repo.** `pages.yml` uploads that directory to
Pages as-is, so index.html and app.js have no second copy to fall behind and there
is no `docs/`. `publish.py --check` runs before the upload and fails the deploy on
an incomplete origin.

`publish.py` moves the published set and never derives it — a missing
`data/model.json` is an error telling you to run `fit.py`. The set is an explicit
manifest, each derived entry naming the file under `data/` it must equal:

| published | must equal | why |
|---|---|---|
| `index.html`, `app.js` | — | authored in `web/`, which *is* the origin |
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

### M4 — the skip set — **transport done; the feature waits on a skip**

Two halves, and only one of them can be finished on demand.

**The transport, done.** `GET /skips` on `server.py` returns
`{as_of, trips, ttl_s}` and nothing else, parsed from the protobuf feed rather
than relayed. `data/config.json` holds `backend_url` — a bare origin, which
`fit.py` rejects a path on — published into `model.json`'s constants beside
`walk_s`, and `app.js` appends `/skips` and `/capture` to it. So moving the host
is a republish, not a code change. Adding the constant was a
one-field refit: 1 of 52 fields changed, the fit untouched.

It stays bound to `127.0.0.1`. §5 called for "a bind beyond 127.0.0.1" on the
assumption the rider's devices would reach it directly; they reach it through
`tailscale serve`, which terminates TLS on the tailnet and proxies to loopback, so
a wider bind buys nothing and costs the LAN an open port.

`tests/test_server.py` boots the real handler on a real port: the shape, and the
CORS gating checked against the allowlist for every route rather than the three
anyone thought to name. Injecting CORS everywhere fails three of its tests; adding
a field to the response fails another.

**The feature, demonstrated on a synthesised skip.** `board_smoke.py` replays the
fixture that carries one, stubs the endpoint with its trip ids, and checks the
whole path: the board asks `<backend_url>/skips`, the ids come back, the matching
schedule slots become "not stopping here" rows, and `getComputedStyle` reports
`line-through`. The other half of the same scenario aborts the endpoint and
checks the board still paints and simply shows no skipped train. Nothing used to
cover any of that — the contract test stops at the rows, so a deleted CSS rule or
a renamed field would have gone unnoticed.

**Deployed 2026-09-26 and verified end to end.** `com.magoun.server` runs
`serve.sh`, and `tailscale serve` publishes exactly two paths — so the proxy
enforces the same line `BROWSER_ROUTES` does, from the other side. Measured
through it: `/skips` and `/capture` answer 200 with `access-control-allow-origin: *`;
`/api`, `/status`, `/board` and `/history` answer 404. `./ops/status.sh` checks all
of it in one command. The exact `serve` invocations are in CLAUDE.md.

**Still unobserved: a real one.** Skips are ~10/day system-wide, rare at Magoun,
and the marker lands ~2 min before the scheduled arrival. Note the notifier could
not have seen one before 2026-09-26 either: `watch.sh` launched without
`gtfs-realtime-bindings`, so every skip lookup raised and was swallowed as though
the feed were down.

**Done when:** a skipped train is struck through on the static board. Done for a
synthesised skip, end to end in a browser; a live one is a matter of waiting.

### M5 — Backend notifier — **done**

Mostly not new logic: `watch.py` already had the tick, the debounce, the revision
rule and the command thread. What it did not have was a connection to the thing
that arms an alert, a test that ran the tick at all, and agreement between what the
plan documents said and what the code did.

**The handoff.** The bell on the static board schedules an ntfy push with `At` and
re-points it every minute while the page is open. It now also posts
`arm <eta> [vehicle]` to the command topic `watch.py` already subscribes to — the
notification-button reply path, in the other direction. No endpoint, no widened
CORS, nothing for the backend to serve. `arm` is idempotent, because an open page
sends it every minute and a fresh plan each time would forget that leave-now had
fired. A plan armed this way has no destination and no deadline — the bell knows a
train, not an errand — so it gets leave-now, revisions and recovery, and no
probability of arriving anywhere by any time.

**Three things were wrong, and only running them showed it:**

| | was | is |
|---|---|---|
| a train that slips | `UnboundLocalError` on every revision tick, swallowed as a bad tick — taking the leave-now below it too | announced, and still tracked |
| the 240 s debounce | `_recover`, which drops the commitment | `_uncertain`, which adopts and says so — the behaviour launch-plan.md already specified and nothing called |
| a tap mid-tick | overwritten by the tick's own save | the handler holds the lock |

**And ntfy does not do what the board assumed.** Measured against ntfy.sh: three
publishes with one `Sequence-ID` and one `At` deliver three times, and
`/{topic}/{seq}/delete` publishes a `message_delete` event while the scheduled send
still arrives. Both are the ntfy *app* collapsing or dismissing a notification, not
the server rescheduling — indistinguishable from "delivers exactly once" when you
are watching a phone. So a re-arm is a real max-priority delivery, and a board left
open past its leave time queued one a minute until the train arrived. The board now
arms **once** — the fallback for a notifier that is not running — and hands the
refining to the notifier, which is the only place it can actually be done. The
handoff names the train that was *armed*, read back from storage: handing over
`data.next` walks the commitment onto the following train the moment this one
arrives, which resets the fact that leave-now fired.

`tests/test_watch.py` drives real ticks through the real plan file with three seams
stubbed — where a snapshot comes from, what `etas` makes of it, where a push goes.
Each fix was reverted separately and fails its own test and no other.

**Done when:** an alert armed at 08:00 and then abandoned still tracks a train
that slips. **Done, and demonstrated live** — `tests/live_notifier.py`, 2026-09-26:

```
13:24:15  ARMING 13:39:19 · schedule · veh=None · leave 13:31:08
13:24:17  command: 'arm 1790444359 -'         <- real ntfy round trip
13:31:11  fired LEAVE NOW for 1:39 via schedule
13:37:23  no match for 1:39 (1/12)            <- the train stopped being predicted
13:41:07  adopted 1:50 after 12 misses
13:41:08  announced revision -> 1:50 (+11.0 min), veh=G-10089
```

Armed on a **schedule row 15 minutes out with no vehicle id** — past MBTA's 8–13
min horizon, which is the gap this project exists for. Leave-now fired three
seconds after the computed `lo - walk`. Then the 13:39 never came: twelve
consecutive misses, ~240 s, the debounce rode them out in silence, and
`_uncertain` adopted the 13:50 and said so — latching vehicle `G-10089`, which it
never had when armed. That is the no-show path, on a real train, and it is the
path that was dead code this morning.

Both pushes were captured off the topic, text and buttons intact:

```
13:31:11  [Leave now]
          1:39 train · 8 min out · via schedule (+/-159s)
          [On my way -> left] [Next one -> bump] [Cancel -> cancel]
13:41:08  [Your train may be running late]
          Now expected 1:50 (+11 min vs your pick) · +/-150s · leave 1:42
          [Show options -> brief]
```

Two details in there are the M5 work, confirmed live rather than by test. The
leave-now carries **no** catch/on-time/95%-there line, because a board-armed plan
has no destination and `_detail` returns None rather than inventing one. And the
no-show push **asks rather than asserts** — "may be running late", not "that train
vanished" — because at one headway the feed cannot tell those apart and the
adopted train's id proves nothing about the one that went missing.

The ~16 silent ticks after 13:41 matter too: with `announced_eta` set, every one
of them ran the revision branch, which is the line that raised
`UnboundLocalError` before it was fixed. None did.

One genuine fault surfaced, and it was not in the notifier: **HTTP 429**. The v3
API allows 20 requests/minute unauthenticated, `service.snapshot()` is two of
them, and the archiver was already polling — a second notifier tipped it over
within six minutes. `watch.main` swallows that as a bad tick. Set `MBTA_API_KEY`
before running anything extra; see CLAUDE.md.

Still unexercised live: a **skip** (protobuf-only, ~10/day system-wide) and a
**slip** large enough to trip `DRIFT_ALERT` without the train vanishing first.

---

## 5. Open decisions

| decision | options | notes |
|---|---|---|
| always-on host | iPad kiosk · Raspberry Pi · fly.io / VPS | **Deferred, not closed** — M5 ships on this Mac, see below. |
| ~~archive retention~~ | **already keep forever** | Measured, not estimated. Nothing prunes the compacted archive; `--prune 90` only drops the uncompacted JSONL, and only once pairs exists for that day. Growth is 1.25 MB/day compacted + 1.41 MB/day pairs = **~1 GB/year**, plus ~330 MB of rolling uncompacted. The doc's old 0.46 GB/year counted the compacted half only. At that size the question is not disk, it is that `data/live` sits in Dropbox and churns — the row below. |
| `data/live` location | inside Dropbox · outside | appended every 15 s; moving it out removes constant sync churn |
| ~~git remote~~ | **done** | github.com/jsundram/mbta-glx, public. Pages deploys from Actions; suite, pages and rating have all run green. |

### Deferred: M5 ships on this Mac, and the gap is made visible instead

The notifier has to fire at a wall-clock instant. This Mac sleeps, launchd runs the
missed tick on wake, and no amount of care inside `watch.py` changes that. The
decision is to ship M5 here anyway and keep the host question open, because the
alternative is holding a finished milestone hostage to a deployment.

What that buys and costs, stated rather than hoped:

- `caffeinate -i -s` now wraps the job inside `src/watch.sh`, so it comes back with
  the launchd agent after a reboot instead of lasting until somebody forgets to
  re-run it. It blocks idle sleep. **It does not survive a lid close.**
- A tick that runs late no longer fires a leave-now regardless. The test is the
  walk itself, so there is no constant to tune: if `eta - now` still leaves room to
  get there, go; otherwise say the train cannot be made and offer the next one.
  Before this, a board-armed plan would send the rider out for a train up to 30
  minutes gone.
- So the failure mode is a **missed nudge, not a wrong one** — and it is now the
  only thing the host question still buys.

**What moving would cost, when it is worth paying.** `watch.py` needs the MBTA API,
`model.json`, and a few KB of state — under 100 KB, and Phase 5 held that line from
the first line of code. The one exception is `brief.health`, which reads
`data/live/rt-*.jsonl.gz` to say whether service is normal today; off this host that
degrades to `"unknown"` unless the notifier keeps its own rolling arrival window,
which `count_arrivals` already knows how to build. fly.io runs the rest unchanged;
Lambda would mean giving up the tick loop and the ntfy subscription thread.

### Decided: M4 serves its endpoint off the Mac over Tailscale

The skip set stops being a *published artifact* and becomes a **live endpoint**
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

**Live since 2026-09-26.** Two `--set-path` routes rather than serving `/`, so
`/api` and the rest stay off the tailnet entirely — the static property is now
enforced twice, by the CORS allowlist and by the proxy. One consequence found in
use: the board is a *heavy* v3 client (~12.5 requests/min per open tab against an
anonymous cap of 20 per client IP), and a home network puts every device behind
one address. The backend is nearly idle by comparison. See CLAUDE.md's gotcha.

**What it does not fix: sleep.** For M4 that is survivable — an asleep Mac means the
fetch fails and the board degrades to an empty skip set, which is the designed
behaviour. For M5 it is fatal, and Tailscale changes nothing about it. `caffeinate`
is the interim mitigation; it blocks idle sleep, not a lid close, and does not
survive a reboot unless it is in the launchd plist.

**The line that keeps the door open.** The endpoint serves *only* what a browser
physically cannot fetch. The moment it serves computed rows, the static property is
gone and fly.io stops being optional. That is an intention, so it is enforced rather
than hoped for: `server._send` emits the CORS header from exactly one
place, gated on `BROWSER_ROUTES` — an allowlist with a stated reason per entry — and
`tests/test_regressions.py` asserts that structure rather than the mere presence of
the name. It also fails if any `web/` file *or* `model.json` constant names an
unjustified host, and if the board calls a computed-rows route.

Verified live, and now automatically: `tests/test_server.py` boots the real
handler on a real port and reads the headers off the wire — `/skips` carries the
header, `/api` and `/nope` do not, and every route is checked against the
allowlist rather than the three anyone thought to name. Injecting CORS everywhere
fails three of its tests. The first version of the test was
decoration — it checked only that the string `BROWSER_ROUTES` appeared somewhere,
which the definition satisfied, so CORS on `/api` passed the whole suite.

## 6. Explicit non-goals

Other stops, other directions, other riders. Beating MBTA's prediction inside their
own horizon — we relay it. A general transit app.

## 7. Sequencing note

M1–M3 and M5 are done, and M4's transport with them; what is left of M4 is a real
skipped train to look at. `BROWSER_ROUTES` no longer has a "planned" entry —
every allowlisted route exists, and the test says so rather than carrying an
exception. There is no `skips.py`: the endpoint answers from
`service.skipped_trips`' own 30 s cache, so nothing needs to publish on a loop.
Phase 1 calibration is gated on the works ending **2026-10-05**
and a rating change on **2026-12-12** resets every schedule-dependent constant, so
prefer shipping the structure now and re-fitting into it later.
