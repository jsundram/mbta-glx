"""Two pictures of the archive: a Marey diagram of the corridor, and a heatmap of
lateness at Magoun. Writes data/figures.json; the backend serves it at /figures.

Neither figure is live. The board answers "when do I leave"; these answer "how has
this line been running", which is a question about days that are already over. So
this is a nightly derived artifact -- `src/daily.sh` rebuilds it, the backend's
/figures hands it over as written, and web/figures.html draws it with no library.
It is not published: it changes every night and derives from an archive that only
exists on the capture host, so a published copy meant a commit and push a day.

**The Marey** is the live archive: arrivals counted as transitions into STOPPED_AT
(invariant 2) and linked by vehicle_id (invariant 1), which is what makes a train's
whole run one line instead of two trips with a gap at the terminus. The y axis is
measured, not assumed -- a station sits at the median observed running time from
Medford/Tufts, so a straight line is a train running at the corridor's usual speed
and a slump is congestion.

**The heatmap** is LAMP (data/raw): 35 days and counting of stop events with the
schedule attached, which is the only source deep enough for a day-by-day picture.
The live archive starts 2026-09-24.

Two things measured here that are not obvious, and both would otherwise be drawn
as if they were signal:

1. **`ADDED-*` trips have no usable scheduled time.** 18.3% of inbound Magoun
   arrivals over 2026-08-18..09-23 are unscheduled extras, and LAMP still fills in
   `scheduled_arrival_time` for them: 88% of those land more than 30 minutes from
   the arrival, against 0.14% of real trips. Left in, they are the whole tail --
   p1 goes from -830 s to -65913 s -- and a 15-minute cell holds about two trains,
   so one of them owns the cell. They are excluded from the median and counted
   separately, because "a train ran that was not in the timetable" is real and
   deleting it silently is how the figure would lie in the rider's favour.
2. **Schedule snapshots cannot be joined to the archive by trip_id.** For
   2026-09-26 the overlap between the 111 trips in data/sched_full and the 127
   observed inbound E trips is exactly zero: GTFS republishes between the capture
   and the service day and renumbers every trip. So the scheduled lines are drawn
   as their own timetable, never matched to a train, and nothing here flags a run
   as unscheduled -- an unjoinable id is not evidence of a deadhead.

Usage:
  uv run --with polars python src/figures.py            # rebuild data/figures.json
  uv run --with polars python src/figures.py --days 3   # fewer Marey days
"""
import argparse
import gzip
import math
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import glx  # noqa: E402
import rollup  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LIVE = DATA / "live"
SCHED = DATA / "sched_full"
STOPS_REF = DATA / "ref" / "stops.json"   # platform coordinates, from api-v3 /stops
OUT = DATA / "figures.json"

# Invariant 8: a ZoneInfo, never a fixed offset. EDT->EST flips 2026-11-01, and
# every time in this file is seconds after local midnight so the page does no
# timezone arithmetic at all.
TZ = ZoneInfo("America/New_York")

MAREY_DAYS = 3          # days of Marey served; ~45 KB each
HEAT_DAYS = 60          # days of heatmap served; LAMP has 35 so far
BIN_S = 900             # heatmap column width: 15 min, ~2 inbound trains
RUN_GAP_S = 1800        # a longer pause is a layover, so a new run starts
# Only the rider's own line. D shares the trunk from Lechmere south and B and C
# only touch the last five stations, so on a corridor diagram they are 400 short
# lines around 175 real ones -- the "broken segments" are mostly them. --routes
# puts them back for a question that needs the whole trunk.
ROUTES = ("Green-E",)
MAX_DEV_S = 1800        # a |deviation| past this is a mispairing, not a late train

# The corridor: Medford/Tufts to Copley, the stops a Magoun rider rides through.
# (name, inbound platform, outbound platform). Ordered north to south; the order
# is checked against the archive by tests/test_figures.py rather than trusted.
CORRIDOR = [
    ("Medford/Tufts", "70512", "70511"),
    ("Ball Square", "70510", "70509"),
    ("Magoun Square", "70508", "70507"),
    ("Gilman Square", "70506", "70505"),
    ("East Somerville", "70514", "70513"),
    ("Lechmere", "70502", "70501"),
    ("Science Park", "70208", "70207"),
    ("North Station", "70206", "70205"),
    ("Haymarket", "70204", "70203"),
    ("Government Center", "70202", "70201"),
    ("Park Street", "70199", "70200"),
    ("Boylston", "70159", "70158"),
    ("Arlington", "70157", "70156"),
    ("Copley", "70155", "70154"),
]
# stop id -> (station index, 0 inbound / 1 outbound)
WHERE = {}
for _i, (_n, _in, _out) in enumerate(CORRIDOR):
    WHERE[_in], WHERE[_out] = (_i, 0), (_i, 1)
MAGOUN = next(i for i, (n, _, _) in enumerate(CORRIDOR) if n == "Magoun Square")
MAGOUN_IN = CORRIDOR[MAGOUN][1]


def midnight(day: str) -> int:
    """Epoch seconds at local midnight opening `day`."""
    return int(datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp())


def archives() -> dict[str, pathlib.Path]:
    """Every archived day, in whichever form it is stored, newest last."""
    out = {}
    for p in sorted(LIVE.glob("day=*")) + sorted(LIVE.glob("rt-*.jsonl*gz")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
        if m:
            out[m.group(1)] = p
    return dict(sorted(out.items()))


def arrivals(path: pathlib.Path, routes: tuple[str, ...] = ROUTES) -> list[list]:
    """Corridor arrivals from one archived day: [t, vehicle, route, station, dir, dwell].

    Invariant 2: an arrival is a TRANSITION into STOPPED_AT. Keying on (vehicle,
    stop) instead records only each vehicle's first visit of the day, and a train
    passes Magoun a dozen times -- a measured 30% undercount.

    `dwell` is how long the train was still there, and it is what separates a
    station stop from the terminus layover: without it the median Medford/Tufts to
    Ball Square leg measures 617 s, which is the berth, not the run. It is a lower
    bound -- the last snapshot that still said STOPPED_AT, so the real departure is
    inside the following 15 s.
    """
    out, prev, open_at = [], {}, {}
    for snap in rollup._snapshots(path):
        t = int(snap["t"])
        for v in snap["vehicles"]:
            vid, stop, status = v["id"], v.get("stop"), v.get("status")
            cur = (stop, status)
            here = WHERE.get(stop or "")
            if routes and v.get("route") not in routes:
                here = None
            if status == "STOPPED_AT" and here:
                if prev.get(vid) != cur:
                    out.append([t, vid, v.get("route"), here[0], here[1], 0])
                    open_at[vid] = len(out) - 1
                elif vid in open_at:
                    row = out[open_at[vid]]
                    row[5] = t - row[0]
            prev[vid] = cur
    out.sort(key=lambda r: (r[0], r[1]))
    return out


def runs(arr: list[list], day: str) -> list[dict]:
    """Split each vehicle's day into runs along the corridor.

    Invariant 1: trip_id is reassigned at the turnaround and mid-run, so a run is
    walked out of one vehicle's arrivals in time order. It ends where the train
    turns around, doubles back, or sits longer than a layover.
    """
    zero = midnight(day)
    by_veh: dict[str, list[list]] = {}
    for rec in arr:
        by_veh.setdefault(rec[1], []).append(rec)

    out = []
    for veh, rows in by_veh.items():
        mine, cur = [], None
        for t, _v, route, station, direction, dwell in rows:
            if cur is not None:
                last_station, last_t = cur["p"][-3], cur["p"][-2] + zero
                # The SAME station again is a hold, not a doubling back: a train
                # held at Government Center reported STOPPED_AT, dropped out of
                # that state, and reported it again five minutes later. Splitting
                # there is where most of the broken segments came from.
                if (station == last_station and direction == cur["d"]
                        and t - last_t <= RUN_GAP_S):
                    cur["p"][-1] = max(cur["p"][-1], t + dwell - last_t)
                    continue
                turned = direction != cur["d"]
                back = (station < last_station if direction == 0
                        else station > last_station)
                if turned or back or t - last_t > RUN_GAP_S:
                    mine.append(cur)
                    cur = None
            if cur is None:
                cur = {"v": veh, "r": (route or "").replace("Green-", ""),
                       "d": direction, "p": []}
            cur["p"] += [station, t - zero, dwell]
        if cur is not None:
            mine.append(cur)
        join_turnarounds(mine)
        out += mine
    # A single stop is a dot, not a trajectory, and half of them are a vehicle
    # appearing mid-corridor at the edge of the archive.
    return [r for r in out if len(r["p"]) >= 6]


def join_turnarounds(vruns: list[dict]) -> None:
    """Make a train's two directions meet at the station where it turned.

    Measured, and the reason outbound trains never reached Medford/Tufts on the
    diagram: over 2026-09-24 and 09-26 the outbound terminus platform (70511) is
    reported STOPPED_AT three times in total. A train arriving at Medford/Tufts is
    already on 70512 with the next inbound trip on it, so the outbound run ends at
    Ball Square and the inbound run starts at the terminus. Same shape at a
    short-turn: on 2026-09-26, 101 of 117 inbound runs ended at North Station and
    62 outbound runs then "began" at Science Park, because the train turned while
    berthed on the inbound platform.

    So the join reuses a timestamp that was actually observed -- the next run's
    first arrival, or this one's last departure -- and never invents one.
    """
    for a, b in zip(vruns, vruns[1:]):
        if a["d"] == b["d"]:
            continue
        sa, ta, da = a["p"][-3], a["p"][-2], a["p"][-1]
        sb, tb = b["p"][0], b["p"][1]
        if sa == sb or not (ta <= tb <= ta + RUN_GAP_S):
            continue
        if (sb < sa) if a["d"] == 1 else (sb > sa):
            a["p"] += [sb, tb, 0]           # it ran on, and turned there
        elif ta + da <= tb:
            b["p"] = [sa, ta + da, 0] + b["p"]   # it turned where the last run ended


def metres(a: dict, b: dict) -> float:
    """Great-circle metres between two platforms. Straight-line, and it has to be:
    the track's real alignment is not in any feed this project reads."""
    r = 6371000.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def distances() -> list[int] | None:
    """Cumulative metres along the corridor, or None with no cached coordinates.

    The second y axis the diagram can use. Run time is the default because it
    makes a normal train a straight line and an abnormal one visibly bent; metres
    is the classic Marey axis, where the slope IS the speed -- and the two disagree
    a lot, because the downtown hops are short and slow while the GLX ones are long
    and quick. Which one you want depends on the question, so both are published.
    """
    if not STOPS_REF.exists():
        return None
    ref = json.loads(STOPS_REF.read_text())
    out, total = [0], 0.0
    for (_, a, _), (_, b, _) in zip(CORRIDOR, CORRIDOR[1:]):
        if a not in ref or b not in ref:
            return None
        total += metres(ref[a], ref[b])
        out.append(round(total))
    return out


def spacing(all_runs: list[dict]) -> list[int]:
    """Where each station sits on the y axis: median running time from the top.

    Measured rather than assumed. Uniform spacing draws the 7-minute Lechmere to
    North Station hop the same height as the 60-second one from Ball Square to
    Magoun, which bends every line in a way the trains did not.
    """
    legs: dict[int, list[int]] = {}
    for r in all_runs:
        pts = list(zip(r["p"][::3], r["p"][1::3], r["p"][2::3]))
        for (a, ta, da), (b, tb, _db) in zip(pts, pts[1:]):
            run_s = abs(tb - ta) - da          # net of the dwell at the near end
            if abs(b - a) == 1 and 0 < run_s < RUN_GAP_S:
                legs.setdefault(min(a, b), []).append(run_s)
    known = [v for vs in legs.values() for v in vs]
    fallback = sorted(known)[len(known) // 2] if known else 120
    y, total = [0], 0
    for i in range(len(CORRIDOR) - 1):
        vs = sorted(legs.get(i, []))
        total += vs[len(vs) // 2] if vs else fallback
        y.append(total)
    return y


def scheduled(day: str) -> tuple[list[dict], list[int]]:
    """The timetable's own lines, and which stations it covers.

    Never joined to a train: the ids do not survive a GTFS republish (see the
    module docstring). Green-E inbound only, which is what snapshot_schedule.py
    captures -- so the scheduled lines stop where its coverage does, and the page
    says so rather than drawing a shorter line as a shorter trip.
    """
    path = SCHED / f"{day}.json.gz"
    if not path.exists():
        return [], []
    zero = midnight(day)
    trips: dict[str, list[tuple[int, int]]] = {}
    seen = set()
    with gzip.open(path, "rt") as f:
        for row in json.load(f):
            here = WHERE.get(row["stop"])
            when = row.get("arr") or row.get("dep")
            if not here or not when:
                continue
            t = int(datetime.fromisoformat(when).timestamp()) - zero
            trips.setdefault(row["trip"], []).append((here[0], t))
            seen.add(here[0])
    out = []
    for pts in trips.values():
        pts.sort(key=lambda p: p[1])
        if len(pts) >= 2:
            out.append({"p": [x for p in pts for x in p]})
    out.sort(key=lambda r: r["p"][1])
    return out, sorted(seen)


def marey(days: int, routes: tuple[str, ...] = ROUTES) -> dict:
    """The Marey diagram, one entry per archived day, newest last."""
    have = archives()
    today = datetime.now(TZ).date().isoformat()
    closed = [d for d in have if d < today][-days:]
    out = []
    for day in closed:
        rs = runs(arrivals(have[day], routes), day)
        sched, sched_stops = scheduled(day)
        out.append({"day": day, "runs": rs, "sched": sched,
                    "sched_stops": sched_stops})
    return {"days": out, "skipped_today": today in have}


def heat(days: int) -> dict:
    """Lateness at Magoun inbound, by day and 15-minute bin, from LAMP.

    The cell is a MEDIAN of `actual - scheduled`, so positive is LATE. (Invariant
    11 is about `err_s` in data/pairs, which runs the other way; this column is
    called `late` for the same reason fit.py renames that one -- a figure whose
    sign you have to remember is a figure that gets read backwards.)

    Every day LAMP has is a row, including the ones where nothing can be scored:
    on 2026-08-22, 08-23, 09-19 and 09-20 every single Magoun inbound train is an
    `ADDED-*` trip, so the median has nothing to stand on. Dropping those rows
    would draw four weekend days as if they had not happened. A row with no cells
    and `added` equal to `n` is the honest shape, and the page prints it.
    """
    empty = {"bin_s": BIN_S, "days": [], "stop": MAGOUN_IN,
             "max_dev_s": MAX_DEV_S, "missing": []}
    try:
        ev = glx.load_events()
    except ValueError:
        # load_events concatenates whatever LAMP days are in data/raw, and with
        # none -- a fresh checkout, which is what CI is -- polars refuses to
        # concat an empty list. Only that case is an empty heatmap; anything
        # else raising here is a real fault.
        if any(glx.RAW.glob("*.parquet")):
            raise
        return empty
    ev = ev.filter(
        (pl.col("stop_id") == MAGOUN_IN) & (~pl.col("direction_id"))
        & pl.col("arr").is_not_null())
    if ev.is_empty():
        return empty
    # Service dates start at 03:00 local and scheduled seconds-after-midnight run
    # past 86400 for the after-midnight trains, so the day's own midnight is the
    # only sane origin. Same conversion as build_dataset.py, same ZoneInfo rule.
    sd_midnight = (pl.col("service_date").cast(pl.String).str.to_datetime("%Y%m%d")
                   .dt.replace_time_zone("America/New_York").dt.epoch("s"))
    ev = ev.with_columns(
        day=(pl.col("service_date").cast(pl.String)
             .str.to_datetime("%Y%m%d").dt.date().cast(pl.String)),
        sched_epoch=sd_midnight + pl.col("scheduled_arrival_time"),
        added=pl.col("trip_id").str.starts_with("ADDED-"),
    ).with_columns(
        late=pl.col("arr") - pl.col("sched_epoch"),
        bin=(pl.col("scheduled_arrival_time") // BIN_S).cast(pl.Int32),
    ).with_columns(
        # An ADDED-* trip is a real train with an unusable scheduled time, and a
        # |deviation| past half an hour at a stop with an 8.8 min headway is a
        # mispairing. Both are counted; neither is binned.
        usable=~pl.col("added") & pl.col("late").is_not_null()
        & (pl.col("late").abs() <= MAX_DEV_S),
    )
    cells = (ev.filter("usable").group_by("day", "bin")
             .agg(p50=pl.col("late").median().round().cast(pl.Int32), n=pl.len())
             .sort("day", "bin"))
    grouped: dict[str, list] = {}
    for c in cells.to_dicts():
        grouped.setdefault(c["day"], []).append([c["bin"], c["p50"], c["n"]])

    per_day = (ev.group_by("day").agg(
        n=pl.len(), added=pl.col("added").sum(),
        # A scheduled trip whose deviation is impossible, or that LAMP gave no
        # scheduled time at all. Counted apart from the extras: the two are
        # different kinds of "cannot be scored".
        dropped=(~pl.col("added") & ~pl.col("usable")).sum(),
        p50=pl.col("late").filter("usable").median().round().cast(pl.Int32),
    ).sort("day"))

    out = []
    for d in per_day.to_dicts()[-days:]:
        out.append({"day": d["day"], "cells": grouped.get(d["day"], []),
                    "n": d["n"], "added": d["added"], "dropped": d["dropped"],
                    "p50": d["p50"]})
    # Days inside the span with no LAMP file at all are a third state: not a quiet
    # day, not a day with no schedule -- a day nobody downloaded.
    span = {r["day"] for r in out}
    missing = []
    if out:
        first = datetime.fromisoformat(out[0]["day"]).date()
        last = datetime.fromisoformat(out[-1]["day"]).date()
        d = first
        while d <= last:
            if d.isoformat() not in span:
                missing.append(d.isoformat())
            d += timedelta(days=1)
    return {"bin_s": BIN_S, "days": out, "stop": MAGOUN_IN,
            "max_dev_s": MAX_DEV_S, "missing": missing}


def build(marey_days: int = MAREY_DAYS, heat_days: int = HEAT_DAYS,
          routes: tuple[str, ...] = ROUTES) -> dict:
    m = marey(marey_days, routes)
    y = spacing([r for d in m["days"] for r in d["runs"]])
    km = distances()
    return {
        "as_of": int(time.time()),
        "tz": "America/New_York",
        "stops": [{"name": n, "in": i, "out": o, "y": y[k],
                   **({"m": km[k]} if km else {})}
                  for k, (n, i, o) in enumerate(CORRIDOR)],
        "magoun": MAGOUN,
        "routes": [r.replace("Green-", "") for r in routes],
        "marey": m,
        "heat": heat(heat_days),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=MAREY_DAYS,
                    help="how many archived days of Marey to serve")
    ap.add_argument("--heat-days", type=int, default=HEAT_DAYS)
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    ap.add_argument("--routes", default=",".join(ROUTES),
                    help="comma-separated route ids, or 'all' for every Green line")
    a = ap.parse_args()

    routes = () if a.routes == "all" else tuple(a.routes.split(","))
    fig = build(a.days, a.heat_days, routes)
    a.out.write_text(json.dumps(fig, separators=(",", ":")) + "\n")
    days = fig["marey"]["days"]
    runs_n = sum(len(d["runs"]) for d in days)
    cells = sum(len(d["cells"]) for d in fig["heat"]["days"])
    added = sum(d["added"] for d in fig["heat"]["days"])
    print(f"{a.out.relative_to(ROOT)}  {a.out.stat().st_size / 1024:.0f} KB")
    print(f"  marey: {len(days)} days, {runs_n} runs"
          + (f", {days[-1]['day']} newest" if days else ""))
    print(f"  heat:  {len(fig['heat']['days'])} days, {cells} cells, "
          f"{added} ADDED-* trains counted but not binned")


if __name__ == "__main__":
    main()
