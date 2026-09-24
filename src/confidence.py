"""What does "90% confident" actually buy you?

Confidence here means P(at the destination by the deadline) -- a yes/no event. It
says nothing about HOW late you are when it goes wrong, and those are different
numbers a rider needs separately:

  * raising confidence costs earliness (boredom), and
  * the lateness tail is set by the headway, because missing a train costs a whole
    one -- so it barely moves as confidence rises.

Monte Carlo over the measured distributions: schedule deviation at Magoun, the
9.6% no-show rate, headway, and the Magoun -> destination ride.
"""
import pathlib

import numpy as np
import polars as pl

RAW = pathlib.Path(__file__).resolve().parent.parent / "data" / "raw"
NO_SHOW = 0.096
DEST = {"70502": "Lechmere", "70206": "North Station",
        "70202": "Govt Center", "70199": "Park St", "70155": "Copley"}


def ride_samples(stop: str) -> np.ndarray:
    cols = ["service_date", "route_id", "direction_id", "stop_id",
            "vehicle_id", "trip_id", "stop_timestamp"]
    ev = pl.concat([
        pl.read_parquet(f, columns=cols)
        .filter((pl.col("route_id") == "Green-E") & (~pl.col("direction_id")))
        for f in sorted(RAW.glob("*.parquet"))
    ]).filter(pl.col("stop_timestamp").is_not_null())
    key = ["service_date", "vehicle_id", "trip_id"]
    a = ev.filter(pl.col("stop_id") == "70508").select(*key, pl.col("stop_timestamp").alias("t0"))
    b = ev.filter(pl.col("stop_id") == stop).select(*key, pl.col("stop_timestamp").alias("t1"))
    d = a.join(b, on=key).with_columns(r=pl.col("t1") - pl.col("t0"))
    return d.filter(pl.col("r").is_between(60, 5400))["r"].to_numpy().astype(float)


def simulate(ride: np.ndarray, dev: np.ndarray, headway: np.ndarray,
             slack: float, n: int = 200_000, rng=None) -> tuple[float, np.ndarray]:
    """slack = deadline - (scheduled Magoun arrival + median ride). Returns
    P(on time) and the lateness samples (seconds) for the trips that ran late."""
    rng = rng or np.random.default_rng(0)
    arr = rng.choice(dev, n)                       # actual vs scheduled at Magoun
    missed = rng.random(n) < NO_SHOW               # train never ran -> wait one headway
    arr = arr + missed * rng.choice(headway, n)
    dest = arr + rng.choice(ride, n)               # ride to destination
    budget = slack + np.median(ride)
    late = dest - budget
    return float(np.mean(late <= 0)), late[late > 0]


def main(stop: str = "70199") -> None:
    ride = ride_samples(stop)
    df = pl.read_parquet("data/magoun.parquet")
    dev = df["sched_dev"].drop_nulls().to_numpy().astype(float)
    dev = dev[np.abs(dev) <= 1800]
    hw = (df.sort("magoun_arr")
          .with_columns(g=pl.col("magoun_arr").diff().over("service_date"))["g"]
          .drop_nulls().to_numpy().astype(float))
    hw = hw[(hw > 30) & (hw < 3600)]

    print(f"Destination: {DEST[stop]}   (ride n={len(ride)}, "
          f"median {np.median(ride)/60:.1f} min, p90 {np.quantile(ride,.9)/60:.1f} min)")
    print(f"no-show {NO_SHOW:.1%} · median headway {np.median(hw)/60:.1f} min\n")
    print(f"{'target':>7} {'leave this early':>17} {'actually on time':>17} "
          f"{'if late: median':>16} {'if late: p90':>13}")
    rng = np.random.default_rng(7)
    for target in (0.50, 0.75, 0.90, 0.95, 0.99):
        lo, hi = -600.0, 3600.0
        for _ in range(40):                        # bisect on slack for this target
            mid = (lo + hi) / 2
            p, _l = simulate(ride, dev, hw, mid, 60_000, rng)
            lo, hi = (lo, mid) if p >= target else (mid, hi)
        slack = (lo + hi) / 2
        p, late = simulate(ride, dev, hw, slack, 200_000, rng)
        ml = np.median(late) / 60 if len(late) else 0.0
        p9 = np.quantile(late, .9) / 60 if len(late) else 0.0
        print(f"{target:6.0%} {slack/60:14.1f} min {p:16.1%} "
              f"{ml:13.1f} min {p9:10.1f} min")
    print("\n'leave this early' = buffer beyond the scheduled arrival at your destination.")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "70199")
