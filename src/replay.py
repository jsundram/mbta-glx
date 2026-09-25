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

MAGOUN_IN = "70508"
ROOT = pathlib.Path(__file__).resolve().parent.parent
LIVE = ROOT / "data" / "live"
SCHED = ROOT / "data" / "sched_full"


def _schedule_slots() -> list[float]:
    """Scheduled inbound arrivals at Magoun, from the daily snapshots."""
    out: list[float] = []
    for f in sorted(SCHED.glob("*.json.gz"))[-3:]:
        with gzip.open(f, "rt") as fh:
            for r in json.load(fh):
                if r.get("stop") == MAGOUN_IN and (r.get("arr") or r.get("dep")):
                    out.append(dt.datetime.fromisoformat(
                        r["arr"] or r["dep"]).timestamp())
    return sorted(out)


def _snapshots(paths):
    for p in paths:
        op = gzip.open if str(p).endswith(".gz") else open
        with op(p, "rt") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)


def score(walk: int = 390, band: int = 75, n: int = 10, paths=None,
          sched_band: int = 22) -> list[dict]:
    """Replay the last `n` arrivals as the service would have handled them.

    Both sources the service quotes are replayed: MBTA's own prediction where one
    exists, and the timetable (minus its fitted q10 offset) before that. Scoring
    against MBTA predictions alone would understate the app, because the schedule
    is what carries the horizon beyond ~13 minutes.
    """
    paths = paths or sorted(LIVE.glob("rt-*.jsonl.gz"))[-2:]
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

    slots = _schedule_slots()
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
            if t >= (a - band) - walk:
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
    walk = int(sys.argv[1]) if len(sys.argv) > 1 else 390
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
