"""Link each actual inbound Magoun arrival back to its upstream signals.

Event-structure notes (verified against raw LAMP timelines):
  * stop_timestamp is arrival; move_timestamp is departure from the PREVIOUS stop.
  * The Medford/Tufts inbound row (seq 4) carries no dwell, so departure from the
    terminus must be read off Ball Square's move_timestamp, not arr+dwell.
  * trip_id is reassigned across the turnaround, so the outbound terminus arrival
    is linked by vehicle_id instead.
"""
import sys

import polars as pl

from glx import OUTBOUND_TERMINUS, load_events

SEQ = {4: "med", 5: "ball", 6: "magoun"}
MAX_LAYOVER = 3600


def build() -> pl.DataFrame:
    ev = load_events().filter(
        pl.col("vehicle_id").is_not_null() & pl.col("route_id").str.starts_with("Green-")
    )
    ib = ev.filter(~pl.col("direction_id") & pl.col("stop_sequence").is_in(list(SEQ)))

    legs = (
        ib.select(
            "service_date", "vehicle_id", "trip_id", "route_id", "stop_sequence",
            "arr", "move_timestamp", "scheduled_arrival_time", "start_time",
        )
        .unique(subset=["service_date", "vehicle_id", "trip_id", "stop_sequence"], keep="last")
        .with_columns(tag=pl.col("stop_sequence").replace_strict(SEQ, return_dtype=pl.String))
        .pivot(
            on="tag",
            index=["service_date", "vehicle_id", "trip_id", "route_id", "start_time"],
            values=["arr", "move_timestamp", "scheduled_arrival_time"],
            aggregate_function="first",
        )
    )

    legs = legs.rename({
        "arr_med": "med_platform_arr", "arr_ball": "ball_arr", "arr_magoun": "magoun_arr",
        "move_timestamp_ball": "med_dep", "move_timestamp_magoun": "ball_dep",
        "scheduled_arrival_time_magoun": "magoun_sched",
    }).filter(pl.col("magoun_arr").is_not_null())

    # Outbound terminus arrival for the same vehicle, most recent before it berths inbound.
    term = (
        ev.filter((pl.col("stop_id") == OUTBOUND_TERMINUS) & pl.col("direction_id"))
        .filter(pl.col("arr").is_not_null())
        .select("vehicle_id", pl.col("arr").alias("term_arr"))
        .unique().sort("term_arr")
    )
    anchor = pl.coalesce(pl.col("med_platform_arr"), pl.col("med_dep"), pl.col("magoun_arr"))
    legs = (
        legs.with_columns(_anchor=anchor).sort("_anchor")
        .join_asof(term, left_on="_anchor", right_on="term_arr",
                   by="vehicle_id", strategy="backward")
    )

    # Enforce causality on the BASE timestamps before deriving anything: a vehicle
    # asof-match can otherwise reach back to a previous round trip hours earlier.
    legs = legs.with_columns(
        ball_dep=pl.when((pl.col("magoun_arr") - pl.col("ball_dep")).is_between(20, 600))
        .then(pl.col("ball_dep")).otherwise(None),
    ).with_columns(
        med_dep=pl.when(
            (pl.col("ball_arr") - pl.col("med_dep")).is_between(30, 900)
            & (pl.col("magoun_arr") - pl.col("med_dep")).is_between(60, 1800)
        ).then(pl.col("med_dep")).otherwise(None),
    ).with_columns(
        med_platform_arr=pl.when(
            (pl.col("magoun_arr") - pl.col("med_platform_arr")).is_between(120, 3600)
        ).then(pl.col("med_platform_arr")).otherwise(None),
    ).with_columns(
        term_arr=pl.when(
            (pl.col("magoun_arr") - pl.col("term_arr")).is_between(120, 3600)
        ).then(pl.col("term_arr")).otherwise(None),
    )

    df = legs.with_columns(
        run_ball=pl.col("magoun_arr") - pl.col("ball_dep"),
        run_med=pl.col("magoun_arr") - pl.col("med_dep"),
        layover_platform=pl.col("med_dep") - pl.col("med_platform_arr"),
        layover_total=pl.col("med_dep") - pl.col("term_arr"),
        term_to_magoun=pl.col("magoun_arr") - pl.col("term_arr"),
        med_platform_to_magoun=pl.col("magoun_arr") - pl.col("med_platform_arr"),
    )
    # Drop links that reach back past a prior round trip or violate causality.
    for c, lo, hi in [("run_ball", 20, 600), ("run_med", 60, 1800),
                      ("layover_platform", 0, MAX_LAYOVER), ("layover_total", 0, MAX_LAYOVER),
                      ("term_to_magoun", 60, MAX_LAYOVER), ("med_platform_to_magoun", 60, MAX_LAYOVER)]:
        df = df.with_columns(
            pl.when(pl.col(c).is_between(lo, hi)).then(pl.col(c)).otherwise(None).alias(c))

    local = pl.from_epoch("magoun_arr", time_unit="s").dt.replace_time_zone(
        "UTC").dt.convert_time_zone("America/New_York")
    # Service date starts at 03:00 local; scheduled seconds-after-midnight can exceed 86400.
    sd_midnight = (
        pl.col("service_date").cast(pl.String).str.to_datetime("%Y%m%d")
        .dt.replace_time_zone("America/New_York").dt.epoch("s")
    )
    return df.with_columns(
        hour=local.dt.hour(), dow=local.dt.weekday(), local_time=local,
        sched_epoch=sd_midnight + pl.col("magoun_sched"),
    ).with_columns(
        sched_dev=pl.col("magoun_arr") - pl.col("sched_epoch")
    ).with_columns(
        # Fold DST / rollover mismatches back into a sane window.
        sched_dev=pl.when(pl.col("sched_dev").abs() > 43200)
        .then(None).otherwise(pl.col("sched_dev"))
    ).sort("magoun_arr")


if __name__ == "__main__":
    out = build()
    out.write_parquet(sys.argv[1] if len(sys.argv) > 1 else "data/magoun.parquet")
    print("legs with a real Magoun arrival:", out.height)
    print()
    print(f"{'signal':24s} {'n':>5} {'p10':>6} {'p50':>6} {'p90':>6} {'p99':>6}")
    for c in ["run_ball", "run_med", "layover_platform", "layover_total",
              "med_platform_to_magoun", "term_to_magoun"]:
        s = out[c].drop_nulls()
        print(f"{c:24s} {len(s):5d} {s.quantile(.1):6.0f} {s.quantile(.5):6.0f} "
              f"{s.quantile(.9):6.0f} {s.quantile(.99):6.0f}")
