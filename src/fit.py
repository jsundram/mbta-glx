"""Fit the arrival model from history and export it as JSON for the live service."""
import json
import urllib.parse
import pathlib

import numpy as np
import polars as pl

import service
from model import BASE, MAXD, TIERS

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "model.json"
# The static board fetches model.json as a sibling asset, so a refit has to
# reach web/ too or the browser keeps predicting from the previous rating.
WEB_OUT = ROOT / "web" / "model.json"
# ...and as a script, because a board opened from file:// is not allowed to
# fetch a sibling file at all (measured: Chromium refuses the scheme outright).
WEB_JS = ROOT / "web" / "model.js"
DESTINATIONS = {
    "70502": "Lechmere", "70206": "North Station", "70202": "Government Center",
    "70199": "Park Street", "70159": "Boylston", "70155": "Copley",
}
GRID = [round(q, 3) for q in np.arange(0.02, 1.0, 0.02)]


def _as_script(text: str) -> str:
    """The same bytes, reachable from file:// where fetch() is not."""
    return ("globalThis.Magoun = globalThis.Magoun || {};\n"
            "Magoun._modelText = " + json.dumps(text) + ";\n")

def _backend_url() -> str:
    """The backend's base, validated. A base, so it must carry no path.

    The obvious migration slip is pasting the old `extras_url` value -- which
    ended in /skips -- under the new key. Nothing else would catch it: the config
    and the published copy would agree with each other, and the host allowlist
    matches on host only. The board would then fetch /skips/skips and
    /skips/capture and lose both features in silence.
    """
    url = service.CONFIG.get("backend_url", "").rstrip("/")
    if url and urllib.parse.urlparse(url).path:
        raise SystemExit(
            f"data/config.json: backend_url must be a bare origin, got {url!r}. "
            "The board appends /skips and /capture itself.")
    return url


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

    # Ride times Magoun -> each plausible destination, so the notifier can answer
    # "will this train get me there by T" from the bundle alone.
    cols = ["service_date", "route_id", "direction_id", "stop_id",
            "vehicle_id", "trip_id", "stop_timestamp"]
    ev = pl.concat([
        pl.read_parquet(f, columns=cols)
        .filter((pl.col("route_id") == "Green-E") & (~pl.col("direction_id")))
        for f in sorted((OUT.parent / "raw").glob("*.parquet"))
    ]).filter(pl.col("stop_timestamp").is_not_null())
    key = ["service_date", "vehicle_id", "trip_id"]
    origin = ev.filter(pl.col("stop_id") == "70508").select(
        *key, pl.col("stop_timestamp").alias("t0"))
    model["rides"] = {}
    for stop, name in DESTINATIONS.items():
        b = ev.filter(pl.col("stop_id") == stop).select(
            *key, pl.col("stop_timestamp").alias("t1"))
        r = (origin.join(b, on=key).with_columns(r=pl.col("t1") - pl.col("t0"))
             .filter(pl.col("r").is_between(60, 5400))["r"].to_numpy().astype(float))
        if len(r) < 200:
            continue
        model["rides"][stop] = {"name": name, "n": int(len(r)),
                                "q": [float(x) for x in np.quantile(np.sort(r), GRID)]}

    # Every constant a consumer needs, so a JavaScript port reads them rather
    # than re-typing them. architecture.md: one source for the constants.
    model["constants"] = {
        "veto_window_s": 480,       # imminent scheduled train, nothing upstream
        "stale_vehicle_s": 180,     # older position = parked ghost, not service
        "dedupe_s": 240,            # a schedule row this close to a live row is it
        "min_gap_s": 120,           # keep schedule rows this far past the last row
        "horizon_s": 45 * 60,
        # A rider preference, not a fitted value -- but published so the board's
        # default and the walk the backend scores with cannot silently differ.
        # data/config.json is the one copy; the rider overrides it in the UI.
        "walk_s": service.DEFAULT_WALK,
        # The backend's base URL: the board appends /skips and /capture. A
        # deployment fact, not a fitted one -- published here so moving the host
        # is a republish rather than a code change, and so grepping web/ for a
        # host cannot miss it. A base rather than one constant per route, because
        # there are two of them now and they move together. Empty or absent means
        # no backend: the board loses the strikethrough and the capture age, and
        # is otherwise exactly the board it was before either existed.
        "backend_url": _backend_url(),
        "band_s": {"mbta": 75, "departed Ball Sq": 7,
                   "departed Medford/Tufts": 33},
        "stops": {"magoun_in": "70508", "ball_in": "70510",
                  "med_in": "70512", "med_out": "70511"},
        # The board draws the line map and filters alerts from these, so the
        # static frontend does not keep a second copy of the stop ids.
        "glx": [list(t) for t in service.GLX_STOPS],
        "alert_corridor": sorted(service.CORRIDOR),
    }

    hw = df.sort("magoun_arr").with_columns(
        g=pl.col("magoun_arr").diff().over("service_date"))["g"].drop_nulls().to_numpy()
    hw = hw[(hw > 30) & (hw < 7200)]
    model["headway_median_s"] = float(np.median(hw))
    OUT.write_text(json.dumps(model))
    WEB_OUT.write_text(OUT.read_text())
    WEB_JS.write_text(_as_script(OUT.read_text()))
    print(f"wrote {OUT} and {WEB_OUT} from {model['n_legs']} legs over {model['days']} days")
    print("  median headway", round(model["headway_median_s"] / 60, 1), "min")
    for t in TIERS:
        q = model["tiers"][t]["q"]
        lo, hi = q[GRID.index(0.1)], q[GRID.index(0.9)]
        print(f"  {t:10s} n={model['tiers'][t]['n']:5d}  q10={lo:6.0f}s q90={hi:6.0f}s  band={hi-lo:5.0f}s")
    for sid, r in model["rides"].items():
        print(f"  ride -> {r['name']:18s} n={r['n']:5d}  "
              f"p10={r['q'][GRID.index(0.1)]/60:5.1f}m p50={r['q'][GRID.index(0.5)]/60:5.1f}m "
              f"p90={r['q'][GRID.index(0.9)]/60:5.1f}m")
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
