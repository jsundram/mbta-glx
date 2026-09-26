"""Archive the MBTA GTFS-RT feeds, filtered to the Green Line Extension.

There is no public archive of historical MBTA predictions, so the only way to
compare "what the feed said" against "what happened" is to keep the feed ourselves.

This reads the protobuf feeds off the CDN rather than the v3 JSON API: no API key,
no rate limit, complete system coverage, and it exposes the per-prediction
`uncertainty` field that the JSON API drops. Output is one gzipped JSONL file per
day; each line is a full snapshot.
"""
import gzip
import json
import pathlib
import time
import urllib.request

from google.transit import gtfs_realtime_pb2 as pb

OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "live"
CDN = "https://cdn.mbta.com/realtime"
PERIOD = 15.0
ALERT_EVERY = 20        # alerts change slowly; ~5 minutes is plenty
GLX = {"70511", "70512", "70510", "70509", "70508", "70507",
       "70506", "70505", "70514", "70513", "70502", "70501"}
# Downtown platforms a Magoun rider passes through. Captured deliberately: the
# journey model (Magoun -> a downtown destination) needs these predictions and
# they cannot be backfilled. Costs roughly 10 MB/day gzipped.
DOWNTOWN = {"70208", "70207", "70206", "70205", "70204", "70203", "70202",
            "70201", "70200", "70199", "70198", "70197", "70196",
            "70159", "70158", "70155", "70154"}
CAPTURE = GLX | DOWNTOWN
STATUS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}


def fetch(name: str, tries: int = 3) -> pb.FeedMessage:
    for i in range(tries):
        try:
            with urllib.request.urlopen(f"{CDN}/{name}.pb", timeout=25) as r:
                msg = pb.FeedMessage()
                msg.ParseFromString(r.read())
                return msg
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * 2 ** i)
    raise RuntimeError("unreachable")


_alerts: dict = {"n": 0, "data": [], "sig": None}


def fetch_alerts() -> list[dict]:
    """Archive the alert state alongside the trains.

    LAMP does publish a historical alerts archive, so this is not strictly
    irrecoverable the way schedules are -- but 130 MB of parquet is a poor way to
    answer "was service disrupted at 08:14 on the 27th", and without it every
    metric fitted over a works period is silently contaminated.
    """
    try:
        with urllib.request.urlopen(f"{CDN}/Alerts.pb", timeout=25) as r:
            msg = pb.FeedMessage()
            msg.ParseFromString(r.read())
    except Exception:  # noqa: BLE001
        return _alerts["data"]
    out = []
    for e in msg.entity:
        a = e.alert
        stops = sorted({ie.stop_id for ie in a.informed_entity if ie.stop_id})
        routes = sorted({ie.route_id for ie in a.informed_entity if ie.route_id})
        # Green Line routes, or any stop on the corridor. Without the route test
        # this quietly archived every Commuter Rail elevator notice.
        if not (any(r.startswith("Green") for r in routes)
                or (set(stops) & CAPTURE)):
            continue
        out.append({
            "id": e.id, "effect": a.effect, "cause": a.cause,
            "routes": routes, "stops": stops,
            "periods": [(p.start or None, p.end or None) for p in a.active_period],
            "header": (a.header_text.translation[0].text
                       if a.header_text.translation else None),
        })
    return out


def poll() -> dict:
    tu, vp = fetch("TripUpdates"), fetch("VehiclePositions")
    preds = []
    for e in tu.entity:
        t = e.trip_update
        for s in t.stop_time_update:
            if s.stop_id not in CAPTURE:
                continue
            preds.append({
                "stop": s.stop_id, "trip": t.trip.trip_id, "route": t.trip.route_id,
                "dir": t.trip.direction_id, "veh": t.vehicle.id or None,
                "seq": s.stop_sequence or None,
                "arr": s.arrival.time if s.HasField("arrival") else None,
                "dep": s.departure.time if s.HasField("departure") else None,
                "unc": (s.arrival.uncertainty if s.HasField("arrival")
                        else s.departure.uncertainty if s.HasField("departure") else None),
                "rel": s.schedule_relationship,
            })
    vehicles = []
    for e in vp.entity:
        v = e.vehicle
        if not v.trip.route_id.startswith("Green"):
            continue
        vehicles.append({
            "id": v.vehicle.id, "route": v.trip.route_id, "trip": v.trip.trip_id,
            "dir": v.trip.direction_id, "stop": v.stop_id or None,
            "status": STATUS.get(v.current_status), "seq": v.current_stop_sequence,
            "ts": v.timestamp,
        })
    snap = {"t": time.time(), "feed_ts": tu.header.timestamp,
            "preds": preds, "vehicles": vehicles}
    # Alerts change slowly and are bulky, so write them only when they change.
    # Every later snapshot inherits the most recent "alerts" line before it.
    if _alerts["n"] % ALERT_EVERY == 0:
        data = fetch_alerts()
        sig = json.dumps([a["id"] for a in data], sort_keys=True)
        if sig != _alerts["sig"]:
            _alerts["data"], _alerts["sig"] = data, sig
            snap["alerts"] = data
    _alerts["n"] += 1
    return snap


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    while True:
        start = time.time()
        try:
            snap = poll()
            day = time.strftime("%Y-%m-%d", time.localtime(snap["t"]))
            with gzip.open(OUT / f"rt-{day}.jsonl.gz", "at") as f:
                f.write(json.dumps(snap, separators=(",", ":")) + "\n")
        except Exception as e:  # noqa: BLE001 - keep the archiver alive
            with (OUT / "errors.log").open("a") as f:
                f.write(f"{time.time()} rt {type(e).__name__}: {e}\n")
        time.sleep(max(0.0, PERIOD - (time.time() - start)))


if __name__ == "__main__":
    main()
