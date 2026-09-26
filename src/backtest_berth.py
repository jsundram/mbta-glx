"""Does berth-aware prediction beat the schedule at a 6-7 minute walk?

Adds the regime the schedule cannot see: once a train berths at Medford/Tufts, its
departure is bounded below by the physical turnaround, so
    departure = max(scheduled departure + 60 s, berth + 165 s)
which matters only when the train is running behind -- but that is exactly when the
schedule is worst (biased 4.7 min early, band 818 s).
"""
import numpy as np
import polars as pl

import service

from backtest import load
from model import Model

RUN = 192          # Medford/Tufts departure -> Magoun arrival, median
TURN = 165         # minimum turnaround once berthed
SCHED_BIAS = 60    # departures run ~1 min late vs timetable


def legs_for(d: pl.DataFrame):
    sd = (pl.col("service_date").cast(pl.String).str.to_datetime("%Y%m%d")
          .dt.replace_time_zone("America/New_York").dt.epoch("s"))
    d = d.with_columns(med_sched=sd + pl.col("scheduled_arrival_time_med"))
    return [
        {"arr": r["magoun_arr"], "berth": r["med_platform_arr"],
         "dep": r["med_dep"], "ball": r["ball_dep"], "sched": r["med_sched"]}
        for r in d.iter_rows(named=True)
    ]


def fit_berth(df: pl.DataFrame, qs) -> dict:
    """Quantiles of (actual Magoun arrival - berth-rule base), fitted like any tier."""
    sd = (pl.col("service_date").cast(pl.String).str.to_datetime("%Y%m%d")
          .dt.replace_time_zone("America/New_York").dt.epoch("s"))
    d = (df.with_columns(med_sched=sd + pl.col("scheduled_arrival_time_med"))
         .filter(pl.col("med_platform_arr").is_not_null()
                 & pl.col("magoun_arr").is_not_null()))
    base = pl.max_horizontal(
        pl.col("med_platform_arr") + TURN,
        pl.col("med_sched") + SCHED_BIAS).fill_null(pl.col("med_platform_arr") + TURN)
    delta = (d.with_columns(b=base)
             .with_columns(delta=pl.col("magoun_arr") - pl.col("b"))["delta"]
             .drop_nulls().to_numpy().astype(float))
    delta = delta[(delta > -900) & (delta < 1800)]
    return {q: float(np.quantile(delta, q)) for q in qs}


def predict(T, trains, slots, m, q, walk, use_berth, boff=None):
    cands, seen = [], []
    for tr in trains:
        p = None
        if tr["ball"] is not None and tr["ball"] <= T:
            r = m.residual("ball_dep", T - tr["ball"], q)
            p = T + max(r, 0) if r is not None else None
        elif tr["dep"] is not None and tr["dep"] <= T:
            r = m.residual("med_dep", T - tr["dep"], q)
            p = T + max(r, 0) if r is not None else None
        elif use_berth and tr["berth"] is not None and tr["berth"] <= T:
            base = tr["berth"] + TURN
            if tr["sched"] is not None:
                base = max(base, tr["sched"] + SCHED_BIAS)
            p = base + boff[q]
        if p is not None and p > T:
            cands.append(p)
            seen.append(p)
    off = m.sched_offset(q)
    for s in slots:
        p = s + off
        if p > T and not any(abs(b - p) < 240 for b in seen):
            cands.append(p)
    ok = [c for c in cands if c >= T + walk]
    return min(ok) if ok else (min(cands) if cands else T + walk)


def run(walk=None):
    walk = walk if walk is not None else service.DEFAULT_WALK
    df, sched = load()
    test = sorted(sched)
    train = df.filter(~pl.col("service_date").is_in(test))
    m = Model(train)
    qs = (0.05, 0.10, 0.20, 0.35, 0.50)
    boff = fit_berth(train, qs)
    print("berth-rule offset quantiles (s):",
          {k: round(v) for k, v in boff.items()})
    res = {}
    for day in test:
        d = df.filter(pl.col("service_date") == day)
        if d.height < 10:
            continue
        actual = np.array(sorted(d["magoun_arr"].to_list()), dtype=float)
        trains, slots = legs_for(d), sched[day]
        for T in np.arange(actual.min() - 1800, actual.max() - 60, 120):
            up = [t for t in trains if t["arr"] > T]
            fut = slots[slots > T]
            cases = [("blind", None, 0.5), ("schedule+live", False, 0.10)]
            cases += [(f"+ berth q{int(qq*100):02d}", True, qq) for qq in qs]
            for name, ub, q in cases:
                R = (T + walk) if name == "blind" else predict(
                    T, up, fut, m, q, walk, ub, boff)
                R = max(R, T + walk)
                nxt = actual[actual >= R]
                if len(nxt) and (nxt[0] - R) / 60 <= 60:
                    res.setdefault(name, []).append((nxt[0] - R) / 60)
    print(f"walk = {walk}s ({walk/60:.1f} min), {len(test)} held-out days\n")
    print(f"{'strategy':16s} {'mean wait':>10} {'median':>8} {'p90':>7} "
          f"{'wait<=1min':>11} {'wait>5min':>10}")
    for name in res:
        a = np.array(res[name])
        print(f"{name:16s} {a.mean():10.2f} {np.median(a):8.2f} {np.quantile(a,.9):7.2f} "
              f"{(a<=1).mean():10.1%} {(a>5).mean():9.1%}")


if __name__ == "__main__":
    run()
