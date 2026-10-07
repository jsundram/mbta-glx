"""Score past trains against what the app would have told you.

For each observed arrival at Magoun we replay the recorded prediction stream and
ask: when would "leave now" have fired, and if you had left then, would you have
caught it and how long would you have waited? This is the app grading itself on
real outcomes rather than on its own error bars.
"""
import datetime as dt
import gzip
import json
import pathlib

import service

MAGOUN_IN = "70508"
ROOT = pathlib.Path(__file__).resolve().parent.parent
LIVE = ROOT / "data" / "live"
SCHED = ROOT / "data" / "sched_full"


class NoScheduleSnapshot(FileNotFoundError):
    """A pinned day has no captured schedule, so it cannot be scored honestly."""


def _schedule_slots(day: str | None = None) -> list[float]:
    """Scheduled inbound arrivals at Magoun, from the daily snapshots.

    `day` pins the snapshot to the day being scored. Without it this reads the
    three most recent snapshots, which is fine for "how did this morning go" but
    silently drops the schedule tier for any older day -- and the schedule is what
    carries the horizon past ~13 min, so 22 of 74 arrivals on 2026-09-24 were
    warned by it alone. A scorer that walks a window must pass the day.
    """
    out: list[float] = []
    if day:
        # A pinned day with no snapshot must not quietly become "no timetable".
        # Silently returning zero slots is the same failure as pinning the wrong
        # day: the schedule tier vanishes and coverage undercounts, and stats.py
        # would then write that score down as final.
        files = [SCHED / f"{day}.json.gz"]
        if not files[0].exists():
            raise NoScheduleSnapshot(
                f"no schedule snapshot for {day}: {files[0]}. The v3 /schedules "
                "endpoint only serves ~8 days back, so an un-captured day is gone "
                "and cannot be scored against its timetable.")
    else:
        files = sorted(SCHED.glob("*.json.gz"))[-3:]
    for f in files:
        with gzip.open(f, "rt") as fh:
            for r in json.load(fh):
                if r.get("stop") == MAGOUN_IN and (r.get("arr") or r.get("dep")):
                    out.append(dt.datetime.fromisoformat(
                        r["arr"] or r["dep"]).timestamp())
    return sorted(out)


def _snapshots(paths):
    for p in paths:
        import archive
        import rollup
        if pathlib.Path(p).is_dir():
            # score() reads no other stop's predictions, so it need not decode them.
            yield from archive.read(pathlib.Path(p), pred_stop=MAGOUN_IN)
            continue
        op = gzip.open if str(p).endswith(".gz") else open
        with op(p, "rt") as f:
            if ".delta." in str(p):
                yield from rollup.from_delta(f)
                continue
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)


_MODEL: service.Model | None = None


def _model() -> service.Model:
    """The fitted model, loaded once. This scorer reads it rather than restating it."""
    global _MODEL
    if _MODEL is None:
        _MODEL = service.Model()
    return _MODEL


def score(walk: int | None = None, band: int | None = None, n: int = 10, paths=None,
          sched_band: int | None = None, day: str | None = None,
          model: service.Model | None = None) -> list[dict]:
    """Replay the last `n` arrivals as the service would have handled them.

    Both sources the service quotes are replayed: MBTA's own prediction where one
    exists, and the timetable (minus its fitted q10 offset) before that. Scoring
    against MBTA predictions alone would understate the app, because the schedule
    is what carries the horizon beyond ~13 minutes.

    Both thresholds are read from the model, not restated. They were hardcoded 75
    and 22 -- and 22 was a hand-copied snapshot of model.sched_offset(0.10) ==
    -22.0, which moves at every refit while the literal would not have. This
    function feeds stats.json's published coverage number, so the staleness would
    have been invisible and wrong at exactly the moment a rating changed.

    `band` is an experiment override, and it is the shape this used to have: one
    symmetric half-width for every lead. Left None -- which is how stats.py calls
    it -- the MBTA threshold is the fitted q10 of that prediction's own error at
    its own lead, which is what compute_rows now quotes.
    """
    walk = walk if walk is not None else service.DEFAULT_WALK
    m = model or _model()
    # Where the q10 arrival is, for a prediction of `a` made at `t`.
    lo_at = ((lambda a, t: a - band) if band is not None
             else (lambda a, t: a + m.pred_offset(a - t, 0.10)))
    if sched_band is None:
        # The service quotes slot + sched_offset(q10); this is the same number
        # with the sign the arithmetic below wants.
        sched_band = -m.sched_offset(0.10)
    paths = paths or (sorted(LIVE.glob("rt-*.jsonl*.gz"))
                      + sorted(LIVE.glob("day=*")))[-2:]
    arrivals: list[tuple[float, str]] = []
    prev: dict[str, tuple] = {}
    preds: dict[str, list[tuple[float, float]]] = {}
    for snap in _snapshots(paths):
        t = snap["t"]
        for v in snap["vehicles"]:
            cur = (v.get("stop"), v.get("status"))
            if (v.get("dir") == 0 and v.get("stop") == MAGOUN_IN
                    and v.get("status") == "STOPPED_AT" and prev.get(v["id"]) != cur):
                arrivals.append((t, v["id"]))
            prev[v["id"]] = cur
        for p in snap["preds"]:
            if (p.get("stop") == MAGOUN_IN and p.get("dir") == 0
                    and p.get("arr") and p.get("veh")):
                preds.setdefault(p["veh"], []).append((t, float(p["arr"])))

    # A vehicle passes Magoun many times a day, so its prediction stream must be
    # windowed to THIS visit -- between its previous arrival and this one --
    # otherwise predictions from hours earlier look like hours of warning.
    prev_arr: dict[str, float] = {}
    windows: list[tuple[float, str, float]] = []
    for tA, vid in arrivals:
        windows.append((tA, vid, prev_arr.get(vid, 0.0)))
        prev_arr[vid] = tA

    slots = _schedule_slots(day)
    out = []
    for tA, vid, since in windows[-n:]:
        # Two guards, both needed. Window to this visit, and require the
        # prediction to actually be ABOUT this arrival: a missed intervening
        # arrival otherwise lets predictions aimed at the previous visit through,
        # which shows up as an implausible hour of "warning".
        stream = [(t, a) for t, a in preds.get(vid, [])
                  if since < t < tA and abs(a - tA) <= 900 and tA - t <= 2700]
        # What the timetable alone would have said about this train.
        slot = min((s for s in slots if abs(s - tA) <= 900),
                   key=lambda s: abs(s - tA), default=None)
        fired, src = None, None
        if slot is not None:
            fired, src = (slot - sched_band - walk, slot), "schedule"
        for t, a in stream:
            # The service tells you to leave when the q10 arrival minus the walk
            # has been reached. Replay that rule against what it knew at the time.
            if t >= lo_at(a, t) - walk:
                if fired is None or t < fired[0]:
                    fired, src = (t, a), "mbta"
                break
        row = {"arrival": tA, "vehicle": vid, "source": src,
               "first_pred_lead_s": (tA - stream[0][0]) if stream else None}
        if fired is None:
            row.update(told=None, wait_s=None, caught=None,
                       note="no prediction and no scheduled slot")
        else:
            t_fire, a_fire = fired
            platform = t_fire + walk
            row["too_late"] = bool(tA - t_fire < walk)
            row.update(told=t_fire, predicted=a_fire,
                       lead_s=tA - t_fire,
                       wait_s=tA - platform,
                       caught=bool(tA >= platform))
        out.append(row)
    return out


if __name__ == "__main__":
    import datetime
    import sys
    walk = int(sys.argv[1]) if len(sys.argv) > 1 else service.DEFAULT_WALK
    rows = score(walk=walk, n=12)
    f = lambda t: datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")
    print(f"replaying the last {len(rows)} arrivals at a {walk/60:.1f} min walk\n")
    print(f"{'arrived':>10} {'told to leave':>14} {'warning':>8} "
          f"{'outcome':>24} {'via':>9}")
    ok = wait = 0
    for r in rows:
        if r["told"] is None:
            print(f"{f(r['arrival']):>10} {'—':>14} {'—':>8} {r['note']:>22}")
            continue
        w = r["wait_s"]
        if r["caught"]:
            verdict = f"waited {int(w)//60}:{int(w)%60:02d}"
        elif r.get("too_late"):
            verdict = f"no useful warning ({r['lead_s']/60:.1f}m)"
        else:
            verdict = f"MISSED by {int(-w)}s"
        ok += r["caught"]
        wait += max(w, 0)
        print(f"{f(r['arrival']):>10} {f(r['told']):>14} "
              f"{r['lead_s']/60:7.1f}m {verdict:>24} {r['source']:>9}")
    n = sum(1 for r in rows if r["told"] is not None)
    if n:
        print(f"\ncaught {ok}/{n} · mean wait {wait/max(ok,1)/60:.1f} min")
