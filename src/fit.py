"""Fit the arrival model from history and export it as JSON for the live service."""
import json
import pathlib

import numpy as np
import polars as pl

from model import BASE, MAXD, TIERS

OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "model.json"
GRID = [round(q, 3) for q in np.arange(0.02, 1.0, 0.02)]


def main() -> None:
    df = pl.read_parquet("data/magoun.parquet").filter(pl.col("magoun_arr").is_not_null())
    model = {"tiers": {}, "grid": GRID, "n_legs": df.height,
             "days": df["service_date"].n_unique()}
    for t in TIERS:
        d = (df["magoun_arr"] - df[BASE[t]]).drop_nulls().to_numpy().astype(float)
        d = np.sort(d[(d >= 0) & (d <= MAXD[t])])
        # Store a quantile sketch of D; the service reconstructs conditional residuals.
        model["tiers"][t] = {"n": int(len(d)),
                             "q": [float(x) for x in np.quantile(d, GRID)]}
    s = df["sched_dev"].drop_nulls().to_numpy().astype(float)
    s = np.sort(s[np.abs(s) <= 1800])
    model["sched"] = {"n": int(len(s)), "q": [float(x) for x in np.quantile(s, GRID)]}

    # Berth rule: once a train is on the inbound platform its departure is bounded
    # below by the physical turnaround, which the timetable cannot express.
    base = pl.max_horizontal(
        pl.col("med_platform_arr") + 357,      # 165 s turnaround + 192 s run
        pl.col("sched_epoch") + 60,            # departures run ~1 min late
    ).fill_null(pl.col("med_platform_arr") + 357)
    delta = (df.filter(pl.col("med_platform_arr").is_not_null())
             .with_columns(b=base)
             .with_columns(d=pl.col("magoun_arr") - pl.col("b"))["d"]
             .drop_nulls().to_numpy().astype(float))
    delta = np.sort(delta[(delta > -900) & (delta < 1800)])
    model["berth"] = {"n": int(len(delta)),
                      "q": [float(x) for x in np.quantile(delta, GRID)],
                      "turn_plus_run": 357, "sched_bias": 60}

    hw = df.sort("magoun_arr").with_columns(
        g=pl.col("magoun_arr").diff().over("service_date"))["g"].drop_nulls().to_numpy()
    hw = hw[(hw > 30) & (hw < 7200)]
    model["headway_median_s"] = float(np.median(hw))
    OUT.write_text(json.dumps(model))
    print(f"wrote {OUT} from {model['n_legs']} legs over {model['days']} days")
    print("  median headway", round(model["headway_median_s"] / 60, 1), "min")
    for t in TIERS:
        q = model["tiers"][t]["q"]
        lo, hi = q[GRID.index(0.1)], q[GRID.index(0.9)]
        print(f"  {t:10s} n={model['tiers'][t]['n']:5d}  q10={lo:6.0f}s q90={hi:6.0f}s  band={hi-lo:5.0f}s")
    q = model["berth"]["q"]
    print(f"  {'berth':10s} n={model['berth']['n']:5d}  "
          f"q10={q[GRID.index(0.1)]:6.0f}s q90={q[GRID.index(0.9)]:6.0f}s  "
          f"band={q[GRID.index(0.9)]-q[GRID.index(0.1)]:5.0f}s")
    q = model["sched"]["q"]
    print(f"  {'schedule':10s} n={model['sched']['n']:5d}  "
          f"q10={q[GRID.index(0.1)]:6.0f}s q90={q[GRID.index(0.9)]:6.0f}s  "
          f"band={q[GRID.index(0.9)]-q[GRID.index(0.1)]:5.0f}s")


if __name__ == "__main__":
    main()
