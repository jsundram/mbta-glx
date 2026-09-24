"""Poll the MBTA v3 API and append GLX predictions + vehicle positions to JSONL.

One file per service day. Two requests per poll keeps us under the 20 req/min
keyless rate limit; set MBTA_API_KEY to raise it.
"""
import json
import os
import pathlib
import time
import urllib.parse
import urllib.request

DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "live"
STOPS = ["70511", "70512", "70510", "70509", "70508", "70507",
         "70506", "70505", "70514", "70513", "70502", "70501"]
PERIOD = 20.0
KEY = os.environ.get("MBTA_API_KEY")


def get(path: str, params: dict, tries: int = 4) -> dict:
    """GET with backoff; the keyless API rate-limits and occasionally truncates."""
    url = f"https://api-v3.mbta.com/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": KEY} if KEY else {})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 ** i * 1.5)
    raise RuntimeError("unreachable")


def poll() -> dict:
    preds = get("predictions", {
        "filter[stop]": ",".join(STOPS),
        "sort": "arrival_time",
    })
    veh = get("vehicles", {"filter[route]": "Green-B,Green-C,Green-D,Green-E"})
    rows = []
    for p in preds["data"]:
        a = p["attributes"]
        rows.append({
            "stop": p["relationships"]["stop"]["data"]["id"],
            "trip": (p["relationships"]["trip"]["data"] or {}).get("id"),
            "veh": (p["relationships"].get("vehicle", {}).get("data") or {}).get("id"),
            "route": (p["relationships"]["route"]["data"] or {}).get("id"),
            "dir": a["direction_id"],
            "arr": a["arrival_time"],
            "dep": a["departure_time"],
            "rel": a["schedule_relationship"],
            "status": a["status"],
        })
    vrows = []
    for v in veh["data"]:
        a = v["attributes"]
        vrows.append({
            "id": v["id"],
            "dir": a["direction_id"],
            "status": a["current_status"],
            "seq": a["current_stop_sequence"],
            "updated": a["updated_at"],
            "stop": (v["relationships"]["stop"]["data"] or {}).get("id"),
            "trip": (v["relationships"]["trip"]["data"] or {}).get("id"),
            "route": (v["relationships"]["route"]["data"] or {}).get("id"),
        })
    return {"t": time.time(), "preds": rows, "vehicles": vrows}


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    while True:
        start = time.time()
        try:
            snap = poll()
            day = time.strftime("%Y-%m-%d", time.localtime(snap["t"]))
            with (DATA / f"{day}.jsonl").open("a") as f:
                f.write(json.dumps(snap, separators=(",", ":")) + "\n")
        except Exception as e:  # noqa: BLE001 - keep the recorder alive
            with (DATA / "errors.log").open("a") as f:
                f.write(f"{time.time()} {type(e).__name__}: {e}\n")
        time.sleep(max(0.0, PERIOD - (time.time() - start)))


if __name__ == "__main__":
    main()
