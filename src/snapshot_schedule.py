"""Snapshot the Green-E inbound schedule for a service date.

The v3 API only serves schedules inside the active GTFS window (~8 days back), so
any day we fail to capture is permanently unavailable for backtesting. This is the
cheapest possible insurance: one small file per day, captured before it expires.

Captures every inbound stop (not just Magoun) so the journey model -- Magoun to a
downtown destination -- can be backtested too.
"""
import gzip
import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "sched_full"
BLOCKS = OUT.parent / "blocks"
KEY = os.environ.get("MBTA_API_KEY")


def _get(url: str, tries: int = 4) -> dict:
    req = urllib.request.Request(url, headers={"x-api-key": KEY} if KEY else {})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 ** i * 2)
    raise RuntimeError("unreachable")


def snapshot(d: date, force: bool = False) -> int:
    dest = OUT / f"{d}.json.gz"
    if dest.exists() and not force:
        return -1
    q = urllib.parse.urlencode({
        "filter[route]": "Green-E", "filter[direction_id]": "0",
        "filter[date]": str(d), "page[limit]": "3000",
        "fields[schedule]": "arrival_time,departure_time,stop_sequence",
    })
    url = f"https://api-v3.mbta.com/schedules?{q}"
    rows = []
    while url:
        body = _get(url)
        for s in body["data"]:
            a = s["attributes"]
            rows.append({
                "trip": s["relationships"]["trip"]["data"]["id"],
                "stop": s["relationships"]["stop"]["data"]["id"],
                "seq": a.get("stop_sequence"),
                "arr": a.get("arrival_time"),
                "dep": a.get("departure_time"),
            })
        url = (body.get("links") or {}).get("next")
    if not rows:
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    with gzip.open(dest, "wt") as f:
        json.dump(rows, f, separators=(",", ":"))
    return len(rows)


def snapshot_blocks(d: date, force: bool = False) -> int:
    """trip -> block for every Green-E trip on `d`, both directions.

    A block is the run one train is planned to work all day, so it is the only
    thing in the timetable that could say WHICH train a scheduled slot is. Whether
    trains actually keep to their blocks is unmeasured; this keeps the evidence.

    Both directions, because a block crosses the turnaround. Taken on the service
    day because these are only useful joined to observed trip ids, and the day is
    the latest the ids can be read. Whether that matters is not settled: the
    snapshot taken the day before shared no trip ids with what ran on 2026-09-26
    (invariant 15), but 10-05 through 10-08 carried identical ids. /trips serves
    the current GTFS rating only -- on 10-07, 10-05 onward answered and 10-04 and
    earlier answered empty -- so an uncaptured day is gone.

    `captured` is when the API was asked, which is what tells a same-day capture
    from a backfill: a past day is answered from today's GTFS, so it is right only
    if nothing was republished in between.
    """
    dest = BLOCKS / f"{d}.json.gz"
    if dest.exists() and not force:
        return -1
    q = urllib.parse.urlencode({
        "filter[route]": "Green-E", "filter[date]": str(d),
        "fields[trip]": "block_id,direction_id",
    })
    body = _get(f"https://api-v3.mbta.com/trips?{q}")
    rows = [{"trip": t["id"], "block": t["attributes"].get("block_id"),
             "dir": t["attributes"].get("direction_id")} for t in body["data"]]
    if not rows:
        return 0
    BLOCKS.mkdir(parents=True, exist_ok=True)
    with gzip.open(dest, "wt") as f:
        json.dump({"day": str(d), "captured": time.time(), "trips": rows}, f,
                  separators=(",", ":"))
    return len(rows)


def main() -> None:
    force = "--force" in sys.argv
    today = date.today()
    # Today and tomorrow: tomorrow's schedule is already published and captures
    # amendments before they are overwritten by the next GTFS republish.
    for d in (today, today + timedelta(days=1)):
        try:
            n = snapshot(d, force)
            print(f"{d}: {'cached' if n < 0 else f'{n} rows'}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"{d}: ERROR {e}", flush=True)
    # Blocks for today only, for the reason in snapshot_blocks.
    try:
        n = snapshot_blocks(today, force)
        print(f"{today} blocks: {'cached' if n < 0 else f'{n} trips'}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"{today} blocks: ERROR {e}", flush=True)


if __name__ == "__main__":
    main()
