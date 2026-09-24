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


if __name__ == "__main__":
    main()
