"""Pull scheduled inbound arrivals at Magoun (70508) for each service date."""
import json
import os
import pathlib
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "sched"
KEY = os.environ.get("MBTA_API_KEY")


def fetch(d: date) -> int:
    dest = OUT / f"{d}.json"
    if dest.exists():
        return -1
    q = urllib.parse.urlencode({
        "filter[stop]": "70508", "filter[date]": str(d),
        "filter[direction_id]": "0", "page[limit]": "500",
    })
    req = urllib.request.Request(f"https://api-v3.mbta.com/schedules?{q}",
                                 headers={"x-api-key": KEY} if KEY else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read())
    rows = [{"trip": s["relationships"]["trip"]["data"]["id"],
             "arr": s["attributes"]["arrival_time"],
             "dep": s["attributes"]["departure_time"]} for s in body["data"]]
    dest.write_text(json.dumps(rows))
    return len(rows)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    s = date(2026, 8, 18)
    for i in range(37):
        d = s + timedelta(days=i)
        try:
            n = fetch(d)
            print(d, "cached" if n < 0 else n, flush=True)
        except Exception as e:  # noqa: BLE001
            print(d, "ERR", e, flush=True)
        time.sleep(3.5 if not KEY else 0.2)
