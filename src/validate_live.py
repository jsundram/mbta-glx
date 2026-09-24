"""Calibrate prediction error from the recorded live stream.

Ground truth comes from the stream itself: a train has arrived at Magoun inbound
the first time its vehicle reports STOPPED_AT 70508 in direction 0. Every earlier
prediction for that vehicle is then scored by how far ahead it was made, which is
exactly the error-vs-lead-time curve the ETA error bars need.
"""
import collections
import datetime as dt
import gzip
import json
import pathlib
import sys

import numpy as np

MAGOUN_IN = "70508"


def load(paths):
    """Read both recorder formats: v3-JSON .jsonl and GTFS-RT .jsonl.gz."""
    for p in paths:
        op = gzip.open if str(p).endswith(".gz") else open
        with op(p, "rt") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)


def _epoch(v):
    """Prediction times are epoch ints in the RT feed, ISO strings in the v3 feed."""
    if v is None:
        return None
    return float(v) if isinstance(v, (int, float)) else dt.datetime.fromisoformat(v).timestamp()


def main(paths):
    actual: dict[str, list[float]] = {}     # vehicle -> every arrival, not just the first
    prev: dict[str, tuple] = {}
    preds = collections.defaultdict(list)   # vehicle -> [(made_at, predicted_arr)]
    for snap in load(paths):
        t = snap["t"]
        for v in snap["vehicles"]:
            cur = (v.get("stop"), v.get("status"))
            if (v.get("dir") == 0 and v.get("stop") == MAGOUN_IN
                    and v.get("status") == "STOPPED_AT"
                    and prev.get(v["id"]) != cur):
                actual.setdefault(v["id"], []).append(t)
            prev[v["id"]] = cur
        for p in snap["preds"]:
            if p["stop"] != MAGOUN_IN or p["dir"] != 0 or not p["arr"]:
                continue
            vid = p["veh"]
            if not vid:
                continue
            preds[vid].append((t, _epoch(p["arr"]), p.get("unc")))

    rows = []
    for vid, times in actual.items():
        for made, pred, unc in preds.get(vid, []):
            arr = next((x for x in times if x > made), None)
            if arr is not None and pred is not None:
                rows.append((arr - made, pred - arr, unc))  # lead, signed error, stated unc
    if not rows:
        print("no completed arrivals with prior predictions yet; let the recorder run")
        return
    lead = np.array([r[0] for r in rows])
    err = np.array([r[1] for r in rows])
    n_arr = sum(len(v) for v in actual.values())
    print(f"{len(rows)} (prediction, outcome) pairs across {n_arr} arrivals\n")
    print(f"{'lead time':>14} {'n':>5} {'p10 err':>8} {'median':>8} {'p90 err':>8} {'band':>7}")
    for lo, hi in [(0, 120), (120, 300), (300, 600), (600, 900), (900, 1800)]:
        m = (lead >= lo) & (lead < hi)
        if m.sum() < 5:
            continue
        e = err[m]
        print(f"{lo//60:4d}-{hi//60:3d} min {m.sum():8d} {np.quantile(e,.1):7.0f}s "
              f"{np.median(e):7.0f}s {np.quantile(e,.9):7.0f}s "
              f"{np.quantile(e,.9)-np.quantile(e,.1):6.0f}s")
    print("\n(positive error = MBTA predicted LATER than the train actually arrived,")
    print(" i.e. the direction that makes you miss it)")

    unc = np.array([r[2] if r[2] is not None else -1 for r in rows], dtype=float)
    have = unc >= 0
    if have.sum() >= 20:
        print("\nMBTA's own stated uncertainty vs. realized error:")
        print(f"{'stated unc':>12} {'n':>6} {'|err| p50':>10} {'|err| p90':>10} {'covered':>9}")
        for u in sorted(set(unc[have])):
            m = have & (unc == u)
            if m.sum() < 10:
                continue
            e = np.abs(err[m])
            print(f"{u:11.0f}s {m.sum():6d} {np.median(e):9.0f}s "
                  f"{np.quantile(e,.9):9.0f}s {np.mean(e <= u):8.0%}")


if __name__ == "__main__":
    live = pathlib.Path(__file__).resolve().parent.parent / "data" / "live"
    ps = sys.argv[1:] or (sorted(live.glob("*.jsonl")) + sorted(live.glob("*.jsonl.gz")))
    main(ps)
