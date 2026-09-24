"""Shared GLX constants and event extraction from LAMP parquet files."""
import pathlib

import polars as pl

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
RAW = DATA / "raw"

# Platform stop_ids. Inbound = toward Heath St / downtown (direction_id False).
INBOUND = {
    "70512": ("Medford/Tufts", 0),
    "70510": ("Ball Square", 1),
    "70508": ("Magoun Square", 2),
    "70506": ("Gilman Square", 3),
    "70514": ("East Somerville", 4),
    "70502": ("Lechmere", 5),
}
OUTBOUND_TERMINUS = "70511"  # Medford/Tufts arrival platform
TARGET = "70508"  # Magoun Square, inbound
ORIGIN = "70512"  # Medford/Tufts, inbound departure
PRIOR = "70510"  # Ball Square, inbound

COLS = [
    "service_date", "route_id", "direction_id", "stop_id", "stop_sequence",
    "vehicle_id", "trip_id", "stop_timestamp", "move_timestamp",
    "dwell_time_seconds", "travel_time_seconds", "scheduled_arrival_time",
    "start_time", "direction_destination",
]


def load_events(files: list[pathlib.Path] | None = None) -> pl.DataFrame:
    """All Green Line stop events at GLX platforms, with arrival/departure epochs."""
    files = files or sorted(RAW.glob("*.parquet"))
    keep = set(INBOUND) | {OUTBOUND_TERMINUS}
    frames = []
    for f in files:
        df = (
            pl.read_parquet(f, columns=COLS)
            .filter(pl.col("route_id").str.starts_with("Green-"))
            .filter(pl.col("stop_id").is_in(keep))
        )
        frames.append(df)
    ev = pl.concat(frames)
    return ev.with_columns(
        arr=pl.col("stop_timestamp"),
        dep=pl.col("stop_timestamp") + pl.col("dwell_time_seconds").fill_null(0),
    )
