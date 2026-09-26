"""Replay an archived day and score competing strategies against it.

The archive samples every prediction, vehicle position and alert every 15 s, which
is enough to reconstruct exactly what the service would have seen at any instant.
That turns strategy questions -- which quantile, which tiers, how long a debounce --
into experiments that run in seconds instead of mornings.

Scoring simulates a rider who decides at time T, follows the strategy until it says
"leave now", walks, and boards the first train to arrive. The metric is the one
that matters: minutes spent standing on the platform.
"""
import datetime as dt
import gzip
import json
import pathlib
from collections.abc import Callable, Iterator

import service

LIVE = service.ROOT / "data" / "live"
MAGOUN_IN = "70508"
# Past this, the rider is not in service hours -- median headway is 8.8 min, so an
# hour of waiting is an overnight gap, not a bad morning. `stats.py` holds
# door-to-train to the same bound for the same reason.
MAX_WAIT_S = 3600


def snapshots(day: str) -> Iterator[dict]:
    import archive
    import rollup
    d = LIVE / f"day={day}"
    if d.is_dir():
        yield from archive.read(d)
        return
    f = LIVE / f"rt-{day}.jsonl.gz"
    if not f.exists():
        f = LIVE / f"rt-{day}.delta.jsonl.gz"
    with gzip.open(f, "rt") as fh:
        if ".delta." in f.name:
            yield from rollup.from_delta(fh)
        else:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


def to_v3(snap: dict) -> dict:
    """Adapt an archived (protobuf-shaped) snapshot to what service.etas expects.

    `revenue` is absent from the protobuf feed, so every train replays as revenue
    service; a deadhead in the archive will therefore look boardable. Live, the v3
    API supplies it. This is the one fidelity gap in the replay.
    """
    iso = lambda t: (dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
                     if t else None)
    preds = [{
        "attributes": {"direction_id": p["dir"], "arrival_time": iso(p.get("arr")),
                       "departure_time": iso(p.get("dep")),
                       "schedule_relationship": p.get("rel"), "status": None},
        "relationships": {"stop": {"data": {"id": p["stop"]}},
                          "trip": {"data": {"id": p.get("trip")}},
                          "vehicle": {"data": {"id": p["veh"]} if p.get("veh") else None},
                          "route": {"data": {"id": p.get("route")}}},
    } for p in snap["preds"]]
    vehicles = [{
        "id": v["id"],
        "attributes": {"direction_id": v["dir"], "current_status": v.get("status"),
                       "current_stop_sequence": v.get("seq"),
                       "updated_at": iso(v.get("ts") or snap["t"]),
                       "revenue": "REVENUE"},
        "relationships": {"stop": {"data": {"id": v["stop"]} if v.get("stop") else None},
                          "trip": {"data": {"id": v.get("trip")}},
                          "route": {"data": {"id": v.get("route")}}},
    } for v in snap["vehicles"]]
    return {"t": snap["t"], "preds": preds, "vehicles": vehicles}


def observed(day: str) -> list[float]:
    """Actual inbound arrivals at Magoun, as transitions into STOPPED_AT."""
    prev: dict[str, tuple] = {}
    out = []
    for snap in snapshots(day):
        for v in snap["vehicles"]:
            cur = (v.get("stop"), v.get("status"))
            if (v.get("dir") == 0 and v.get("stop") == MAGOUN_IN
                    and v.get("status") == "STOPPED_AT" and prev.get(v["id"]) != cur):
                out.append(snap["t"])
            prev[v["id"]] = cur
    return sorted(out)


# ---- strategies: snapshot -> recommended platform-arrival time, or None ----

def make_strategy(model: service.Model, q: float = 0.10, tiers=("mbta", "schedule"),
                  walk: int = 390) -> Callable[[dict, dict], float | None]:
    def strat(snapv3: dict, berths: dict) -> float | None:
        rows = service.etas(snapv3, model, walk, qs=(q, 0.5, 0.9), berths=berths)
        now = snapv3["t"]
        for r in rows:
            if r.get("skipped"):
                continue
            src = "mbta" if r["source"] not in ("schedule",) else "schedule"
            if src not in tiers:
                continue
            if r["lo"] >= now + walk:
                return r["lo"]
        return None
    return strat


_ADAPTED: dict = {}


def adapted(day: str) -> list[dict]:
    """Adapt a day once and keep it: this dominated the replay cost."""
    if day not in _ADAPTED:
        _ADAPTED[day] = [to_v3(s) for s in snapshots(day)]
    return _ADAPTED[day]


def run(day: str, strat, walk: int = 390,
        every: int = 300) -> list[tuple[float, float]]:
    """Riders decide every `every` seconds and follow the strategy until it fires.

    The recommendation is computed once per snapshot, not once per rider: a naive
    nested loop is O(riders x snapshots) and takes minutes per strategy.
    """
    snaps = adapted(day)
    arrivals = observed(day)
    if not snaps or not arrivals:
        return []
    berths: dict = {}
    rec = [strat(s, berths) for s in snaps]          # O(snapshots)
    times = [s["t"] for s in snaps]

    waits = []
    stride = max(1, every // 15)
    for i0 in range(0, len(snaps), stride):
        for j in range(i0, len(snaps)):
            R = rec[j]
            if R is None or R > times[j] + MAX_WAIT_S:
                continue
            # Fire as soon as the walk would land us at R. Testing `>=` exactly
            # never triggers: the strategy only returns trains that are STILL
            # catchable, so the row flips to the next train one step before the
            # crossing. One snapshot of tolerance closes that gap.
            if times[j] + walk >= R - 20:
                platform = times[j] + walk
                nxt = [a for a in arrivals if a >= platform]
                if nxt and (nxt[0] - platform) < MAX_WAIT_S:
                    # Platform wait alone rewards dawdling: a strategy that keeps
                    # you at home until it is certain scores perfectly on it while
                    # putting you on a later train. Elapsed time from the moment
                    # you decided is what actually costs you.
                    waits.append(((nxt[0] - platform) / 60,
                                  (nxt[0] - times[i0]) / 60))
                break
    return waits


if __name__ == "__main__":
    import sys
    import statistics as st
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-09-25"
    walk = int(sys.argv[2]) if len(sys.argv) > 2 else 390
    m = service.Model()
    # Freeze the live lookups so the replay sees only what was archived.
    service.skipped_trips = lambda *a, **k: set()
    print(f"replaying {day} at a {walk/60:.1f} min walk\n")
    print(f"{'strategy':28s} {'n':>4} {'platform wait':>14} {'>5min':>6} "
          f"{'door-to-train':>14}")
    print(f"{'':28s} {'':>4} {'mean   median':>14} {'':>6} {'mean   median':>14}")
    # NOTE: the tier filter is applied to etas() OUTPUT, which has already fused
    # and de-duplicated the tiers -- an MBTA row suppresses a nearby schedule row.
    # So "schedule only" is not a clean ablation; it is "the schedule rows that
    # survived fusion", which is why it looks so much worse than it should.
    for name, q, tiers in [
        ("mbta only, q10", 0.10, ("mbta",)),
        ("schedule only, q10*", 0.10, ("schedule",)),
        ("both, q05", 0.05, ("mbta", "schedule")),
        ("both, q10", 0.10, ("mbta", "schedule")),
        ("both, q20", 0.20, ("mbta", "schedule")),
        ("both, q35", 0.35, ("mbta", "schedule")),
    ]:
        w = run(day, make_strategy(m, q, tiers, walk), walk)
        if not w:
            print(f"{name:28s}    0")
            continue
        pw = sorted(x[0] for x in w)
        el = sorted(x[1] for x in w)
        print(f"{name:28s} {len(w):4d} {st.mean(pw):6.2f} {st.median(pw):6.2f}m "
              f"{sum(x > 5 for x in pw)/len(pw):5.0%} "
              f"{st.mean(el):7.2f} {st.median(el):6.2f}m")
