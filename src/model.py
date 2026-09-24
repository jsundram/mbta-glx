"""Predictive model for inbound Magoun arrivals.

Each tier of evidence (terminus arrival, berthed at Medford/Tufts, departed
Medford/Tufts, departed Ball Sq) carries an empirical distribution D of "time from
that event until the train reaches Magoun". The live quantity we need is the
RESIDUAL: given the event happened `elapsed` seconds ago and the train has not yet
arrived, how much longer? That is quantile(D - elapsed | D > elapsed), which both
sharpens the estimate as a train sits and stops long-dwelling trains from being
predicted into the past.
"""
import numpy as np
import polars as pl

TIERS = ["ball_dep", "med_dep", "berthed", "term"]   # sharpest first
BASE = {"ball_dep": "ball_dep", "med_dep": "med_dep",
        "berthed": "med_platform_arr", "term": "term_arr"}
MAXD = {"ball_dep": 600, "med_dep": 1800, "berthed": 3600, "term": 3600}


class Model:
    def __init__(self, df: pl.DataFrame):
        self.D = {}
        for t in TIERS:
            d = (df["magoun_arr"] - df[BASE[t]]).drop_nulls().to_numpy().astype(float)
            self.D[t] = np.sort(d[(d >= 0) & (d <= MAXD[t])])
        s = df["sched_dev"].drop_nulls().to_numpy().astype(float)
        self.sched_dev = np.sort(s[np.abs(s) <= 1800])

    def residual(self, tier: str, elapsed: float, q: float) -> float | None:
        """Quantile q of remaining seconds to Magoun, given `elapsed` already passed."""
        d = self.D[tier]
        i = np.searchsorted(d, elapsed, side="right")
        surv = d[i:]
        if len(surv) < 25:
            # Too far into the tail to estimate; fall back to the last reliable slice.
            surv = d[-25:]
            if elapsed >= surv[-1]:
                return None
        return float(np.quantile(surv, q) - elapsed)

    def sched_offset(self, q: float) -> float:
        return float(np.quantile(self.sched_dev, q))

    def predict_train(self, ev: list[tuple], T: float, q: float) -> float | None:
        """ev = [(base_time, tier)] sharpest-first. Returns predicted arrival epoch."""
        for base, tier in ev:
            if base is None or base > T:
                continue
            r = self.residual(tier, T - base, q)
            if r is None:
                continue
            return T + max(r, 0.0)
        return None
