"""Morning brief: which trains get you to a destination by a deadline.

Confidence here is P(at the destination by the deadline) -- see launch-plan.md for
what that does and does not buy. It accounts for three things: uncertainty in when
the train reaches Magoun, the 9.6% chance the scheduled train never runs (in which
case you fall through to the next one), and the ride itself, which is the largest
of the three (Magoun->Park St spans 18.3-25.4 min).
"""
import datetime as dt
import os

import numpy as np

import service

NO_SHOW = 0.096
N = 20_000


def _qsample(lo: float, mid: float, hi: float, n: int, rng) -> np.ndarray:
    """Sample an arrival time from its q10/q50/q90, piecewise-linear in between."""
    xs = [0.0, 0.10, 0.50, 0.90, 1.0]
    ys = [lo - (mid - lo), lo, mid, hi, hi + (hi - mid)]
    return np.interp(rng.random(n), xs, ys)


def _ride(model: service.Model, stop: str, n: int, rng) -> np.ndarray:
    r = model.m.get("rides", {}).get(stop)
    if not r:
        raise KeyError(f"no ride distribution for stop {stop}; re-run src/fit.py")
    return rng.choice(np.asarray(r["q"], dtype=float), n)


def options(dest: str, deadline: float, walk: int, model: service.Model,
            snap: dict, berths: dict, limit: int = 4) -> list[dict]:
    """Rank the upcoming trains by P(at `dest` by `deadline`)."""
    rng = np.random.default_rng(11)
    rows = service.etas(snap, model, walk, berths=berths)
    rows = [r for r in rows if r["lo"] > snap["t"]][:8]
    ride = _ride(model, dest, N, rng)
    out = []
    for i, r in enumerate(rows):
        arr = _qsample(r["lo"], r["eta"], r["hi"], N, rng)
        # If this train never runs, you fall through to the next one.
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt:
            alt = _qsample(nxt["lo"], nxt["eta"], nxt["hi"], N, rng)
            arr = np.where(rng.random(N) < NO_SHOW, alt, arr)
        elif r["source"] == "schedule":
            arr = np.where(rng.random(N) < NO_SHOW, arr + model.headway, arr)
        # Can you physically be on the platform before it arrives, leaving now?
        p_catch = float(np.mean(arr >= snap["t"] + walk))
        at_dest = arr + ride
        out.append({
            "p_catch": p_catch,
            "eta": r["eta"], "lo": r["lo"], "source": r["source"],
            "backed": r["backed"], "vehicle": r.get("vehicle"),
            "leave_by": r["lo"] - walk,
            "p_ontime": float(np.mean(at_dest <= deadline)),
            "dest_p50": float(np.median(at_dest)),
            "dest_p90": float(np.quantile(at_dest, 0.9)),
            "catchable": p_catch >= 0.90,
        })
    return out[:limit]


def health(model: service.Model, window_h: float = 3.0) -> dict:
    """How are trains running today, versus the 35-day baseline?"""
    import gzip
    import json
    import pathlib
    live = service.ROOT / "data" / "live"
    files = sorted(live.glob("rt-*.jsonl.gz"))
    if not files:
        return {"state": "unknown", "note": "no archive yet"}
    seen: dict[str, float] = {}
    cutoff = dt.datetime.now().timestamp() - window_h * 3600
    try:
        with gzip.open(files[-1], "rt") as f:
            for line in f:
                if not line.strip():
                    continue
                snap = json.loads(line)
                if snap["t"] < cutoff:
                    continue
                for v in snap.get("vehicles", []):
                    if (v.get("dir") == 0 and v.get("stop") == service.MAGOUN_IN
                            and v.get("status") == "STOPPED_AT"):
                        seen.setdefault(v["id"], snap["t"])
    except OSError:
        return {"state": "unknown", "note": "archive unreadable"}
    ts = sorted(seen.values())
    if len(ts) < 3:
        return {"state": "unknown", "note": f"only {len(ts)} arrivals seen"}
    gaps = np.diff(ts)
    gaps = gaps[(gaps > 30) & (gaps < 3600)]
    if not len(gaps):
        return {"state": "unknown", "note": "no usable gaps"}
    med = float(np.median(gaps))
    base = model.headway
    ratio = med / base
    state = "normal" if ratio < 1.15 else ("slow" if ratio < 1.4 else "disrupted")
    worst = float(gaps.max())
    return {"state": state, "median_headway_s": med, "baseline_s": base,
            "worst_gap_s": worst, "n": len(ts) }


def render(opts: list[dict], hl: dict, dest_name: str, deadline: float,
           target: float) -> tuple[str, str]:
    tz = service.TZ
    f = lambda t: dt.datetime.fromtimestamp(t, tz).strftime("%-I:%M")
    title = f"{dest_name} by {f(deadline)}"
    good = [o for o in opts if o["p_ontime"] >= target and o["catchable"]]
    pick = good[0] if good else next((o for o in opts if o["catchable"]), None)
    now = service.time.time()
    lines = []
    for o in opts:
        mark = "*" if pick and o is pick else " "
        spare = (deadline - o["dest_p50"]) / 60
        if o["p_catch"] < 0.5:
            when = "too late"
        elif not o["catchable"]:
            when = "tight — go now"
        elif o["leave_by"] <= now + 60:
            when = "leave now"
        else:
            when = f"leave {f(o['leave_by'])}"
        lines.append(
            f"{mark} {f(o['eta'])} -> {f(o['dest_p50'])}  "
            f"{o['p_ontime']:.0%} on time  {spare:+.0f}m spare  ({when})")
    if hl["state"] == "unknown":
        lines.append(f"\nService today: unknown ({hl.get('note','')})")
    else:
        lines.append(
            f"\nService today: {hl['state']} "
            f"({hl['median_headway_s']/60:.1f} min headway vs "
            f"{hl['baseline_s']/60:.1f} baseline)")
    return title, "\n".join(lines)


if __name__ == "__main__":
    import sys
    dest = sys.argv[1] if len(sys.argv) > 1 else "70199"
    hhmm = sys.argv[2] if len(sys.argv) > 2 else "17:30"
    walk = int(os.environ.get("MAGOUN_WALK_S", "390"))
    m = service.Model()
    now = dt.datetime.now(service.TZ)
    h, mi = (int(x) for x in hhmm.split(":"))
    dl = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if dl < now:
        dl += dt.timedelta(days=1)
    snap = service.snapshot()
    berths = service.BerthTracker().update(snap)
    opts = options(dest, dl.timestamp(), walk, m, snap, berths)
    t, b = render(opts, health(m), m.m["rides"][dest]["name"], dl.timestamp(), 0.90)
    print(t); print(b)
