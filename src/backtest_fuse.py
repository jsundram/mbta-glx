"""Final backtest: fuse the schedule with live evidence, sharpest estimator wins.

Sharpness (q10-q90 band width) decides which source times a given train:
  departed Ball Sq   14 s   but only ~51 s of lead
  departed Medford   65 s   ~190 s of lead
  schedule          332 s   unlimited lead
  berthed / terminus 830-1050 s -- too loose to time a train, used only as a
                                   presence check that the scheduled train exists
"""
import numpy as np
import polars as pl

from backtest import day_state, load
from model import Model

HEADWAY = 8.8 * 60


def predict_all(T, trains, slots, m, q, use_live, veto, walk):
    """Candidate arrival times for trains not yet past Magoun."""
    live = []
    for tr in trains:
        for base, tier in tr["ev"]:
            if base is None or base > T or tier not in ("ball_dep", "med_dep"):
                continue
            r = m.residual(tier, T - base, q)
            if r is not None:
                live.append(T + max(r, 0.0))
            break
    present = 0
    for tr in trains:
        for base, tier in tr["ev"]:
            if base is not None and base <= T and tier in ("berthed", "term"):
                present += 1
                break

    cands = list(live) if use_live else []
    off = m.sched_offset(q)
    for s in slots:
        p = s + off
        if p <= T:
            continue
        if use_live and any(abs(b - p) < 240 for b in live):
            continue                      # live already times this train, sharper
        if veto and s - T < 480 and present == 0:
            continue                      # nothing upstream: scheduled train is a no-show
        cands.append(p)
    ok = [c for c in cands if c >= T + walk]
    return min(ok) if ok else (min(cands) if cands else T + walk)


def run():
    df, sched = load()
    test = sorted(sched)
    m = Model(df.filter(~pl.col("service_date").is_in(test)))
    print(f"{'walk':>5} {'strategy':12s} {'q':>5} {'mean wait':>10} {'median':>8} "
          f"{'p90':>7} {'%>5min':>8} {'%>10min':>8}")
    for walk in (120, 240, 360, 600):
        res = {}
        for day in test:
            d = df.filter(pl.col("service_date") == day)
            if d.height < 10:
                continue
            actual = np.array(sorted(d["magoun_arr"].to_list()), dtype=float)
            trains, slots = day_state(d), sched[day]
            for T in np.arange(actual.min() - 1800, actual.max() - 60, 120):
                up = [t for t in trains if t["arr"] > T]
                fut = slots[slots > T]
                for name, ul, vt, q in [("blind", None, None, 0.5),
                                        ("sched", False, False, 0.10),
                                        ("fuse", True, False, 0.10),
                                        ("fuse+veto", True, True, 0.10)]:
                    R = (T + walk) if name == "blind" else predict_all(
                        T, up, fut, m, q, ul, vt, walk)
                    R = max(R, T + walk)
                    nxt = actual[actual >= R]
                    if not len(nxt):
                        continue
                    w = (nxt[0] - R) / 60
                    if w <= 60:
                        res.setdefault((name, q), []).append(w)
        for (name, q), v in res.items():
            a = np.array(v)
            print(f"{walk:5d} {name:12s} {q:5.2f} {a.mean():10.2f} {np.median(a):8.2f} "
                  f"{np.quantile(a,.9):7.2f} {(a>5).mean():7.1%} {(a>10).mean():7.1%}")
        print()


if __name__ == "__main__":
    run()
