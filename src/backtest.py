"""Backtest Magoun inbound arrival prediction on held-out days.

Decision being scored: the rider asks at time T, walks WALK seconds, and paces to
reach the platform at the recommended time R. Score = platform wait, the time they
stand there. Promising a train that already left shows up as a full extra headway
of wait, so the metric needs no separate "miss" term.

Strategies compared:
  blind  -- walk out the door with no information (R = T + walk)
  sched  -- published schedule plus a fitted bias/quantile correction
  live   -- physical upstream evidence only (terminus / berthed / departed / Ball Sq)
  both   -- schedule as the clock, live evidence to sharpen it and to veto no-shows
"""
import datetime
import json
import pathlib

import numpy as np
import polars as pl

from model import BASE, TIERS, Model

WALK = 360
TAU = 600          # an imminent scheduled slot with no train upstream is a no-show
HEADWAY = 8.8 * 60

def load():
    df = pl.read_parquet("data/magoun.parquet").filter(pl.col("magoun_arr").is_not_null())
    sched = {}
    for f in sorted(pathlib.Path("data/sched").glob("*.json")):
        rows = json.loads(f.read_text())
        sched[int(f.stem.replace("-", ""))] = np.array(
            sorted(datetime.datetime.fromisoformat(r["arr"]).timestamp()
                   for r in rows if r["arr"]))
    return df, sched


def day_state(day_df: pl.DataFrame):
    """Per-train evidence: list of (known_at, base_time, tier) sorted sharpest-first."""
    trains = []
    for r in day_df.iter_rows(named=True):
        ev = [(r[BASE[t]], t) for t in TIERS if r[BASE[t]] is not None]
        trains.append({"arr": r["magoun_arr"], "ev": ev})
    return trains


def predict(T, trains, sched_slots, m, q, strategy, walk):
    cands = []
    backed = []   # predicted arrivals of trains we can actually see
    if strategy in ("live", "both"):
        for tr in trains:
            p = m.predict_train(tr["ev"], T, q)
            if p is not None:
                backed.append(p)
        cands.extend(backed)
    if strategy in ("sched", "both"):
        for s in sched_slots:
            p = s + m.sched_offset(q)
            if p <= T:
                continue
            if strategy == "both":
                # trust an imminent slot only if some visible train could be it
                if s - T < TAU and not any(abs(b - p) < 300 for b in backed):
                    continue
            cands.append(p)
    floor = T + walk
    ok = [c for c in cands if c >= floor]
    if ok:
        return min(ok)
    return min(cands) if cands else floor


def run(walk=WALK, qs=(0.05, 0.1, 0.2, 0.35, 0.5)):
    df, sched = load()
    test_days = sorted(sched)
    train_df = df.filter(~pl.col("service_date").is_in(test_days))
    m = Model(train_df)

    print(f"fit on {train_df['service_date'].n_unique()} days; "
          f"evaluate on {len(test_days)} days with full schedules; walk={walk}s\n")
    print("residual seconds to Magoun, by tier and time since that event (q=0.10):")
    print("  " + "tier".ljust(12) + "".join(f"+{e}s".rjust(8) for e in (0, 120, 300, 600, 900)))
    for t in TIERS:
        cells = [m.residual(t, e, 0.10) for e in (0, 120, 300, 600, 900)]
        print("  " + t.ljust(12) + "".join(
            ("   --  " if c is None else f"{c:7.0f}").rjust(8) for c in cells))

    results = {}
    for day in test_days:
        d = df.filter(pl.col("service_date") == day)
        actual = np.array(sorted(d["magoun_arr"].to_list()), dtype=float)
        if len(actual) < 10:
            continue
        trains = day_state(d)
        slots = sched[day]
        for T in np.arange(actual.min() - 1800, actual.max() - 60, 120):
            up = [t for t in trains if t["arr"] > T]
            fut = slots[slots > T]
            for strat in ["blind", "sched", "live", "both"]:
                for q in ([0.5] if strat == "blind" else qs):
                    R = (T + walk) if strat == "blind" else predict(
                        T, up, fut, m, q, strat, walk)
                    R = max(R, T + walk)
                    nxt = actual[actual >= R]
                    if not len(nxt):
                        continue
                    w = (nxt[0] - R) / 60
                    if w > 60:
                        continue
                    results.setdefault((strat, q), []).append(w)

    print(f"\nplatform wait, minutes ({len(results[('blind',0.5)])} query times)\n")
    print(f"{'strategy':10s} {'q':>5} {'mean':>7} {'median':>7} {'p90':>7} {'p95':>7} {'%>5min':>8}")
    for (strat, q), v in sorted(results.items()):
        a = np.array(v)
        print(f"{strat:10s} {q:5.2f} {a.mean():7.2f} {np.median(a):7.2f} "
              f"{np.quantile(a,.9):7.2f} {np.quantile(a,.95):7.2f} {(a>5).mean():7.1%}")


if __name__ == "__main__":
    run()
