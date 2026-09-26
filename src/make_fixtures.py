"""Freeze real snapshots as language-neutral fixtures for the contract test.

These pin `service.compute_rows` today and are what a JavaScript port will be
checked against tomorrow. Everything the function needs is inlined, so a fixture
can be replayed with no network, no clock and no file layout assumptions.
"""
import datetime as dt
import json
import pathlib
import sys

import service
import simulate

OUT = service.ROOT / "tests" / "fixtures"
WALK = service.DEFAULT_WALK
QS = (0.1, 0.5, 0.9)
HORIZON = 45 * 60


def build(day: str, count: int = 12) -> int:
    snaps = list(simulate.snapshots(day))
    if not snaps:
        print(f"no archive for {day}", file=sys.stderr)
        return 0
    slots = service.schedule_rows(dt.date.fromisoformat(day))

    # Spread across service hours, skipping the dead overnight stretch.
    picked, step = [], max(1, len(snaps) // (count + 4))
    berths: dict[str, float] = {}
    tracker = service.BerthTracker(OUT / ".unused-berth-state.json")
    tracker.seen = {}
    for i, s in enumerate(snaps):
        v3 = simulate.to_v3(s)
        berths = {k: v for k, v in _berths(tracker, v3).items()}
        if i % step == 0 and len(picked) < count:
            hour = dt.datetime.fromtimestamp(s["t"], service.TZ).hour
            if 6 <= hour <= 23:
                picked.append((v3, dict(berths)))

    model = service.Model()
    OUT.mkdir(parents=True, exist_ok=True)
    cases = []
    for v3, b in picked:
        rows = service.compute_rows(v3["t"], v3["preds"], v3["vehicles"], model,
                                    WALK, QS, HORIZON, b, slots, set())
        cases.append({
            "now": v3["t"], "walk": WALK, "qs": list(QS), "horizon": HORIZON,
            "berths": b, "skipped": [],
            "slots": [[t, tr] for t, tr in slots],
            "preds": v3["preds"], "vehicles": v3["vehicles"],
            "expected": rows,
        })
    # Synthesise one case per file that exercises the skipped tier: it is rare in
    # any given hour, and a tier with no fixture is one a JS port can silently get
    # wrong. Derived from a real case so the rest of the inputs stay realistic.
    if cases:
        base = json.loads(json.dumps(cases[0]))
        future = [tr for t, tr in slots if t > base["now"] + 600]
        if future:
            base["skipped"] = future[:2]
            base["expected"] = service.compute_rows(
                base["now"], base["preds"], base["vehicles"], model, WALK, QS,
                HORIZON, base["berths"], slots, set(base["skipped"]))
            cases.append(base)

    # Same reasoning for the two filters that only exist in the v3 feed. The
    # archive is protobuf-shaped and carries no `revenue` field at all (see
    # simulate.to_v3), so no sampled snapshot can contain a deadhead: measured, all
    # 26 cases had zero. Dropping _revenue or _live in a port therefore changed no
    # fixture's rows, which is precisely invariant 7 failing silently.
    if cases:
        cases.append(_ghost_case(cases, slots, model))

    path = OUT / f"cases-{day}.json"
    path.write_text(json.dumps(cases, separators=(",", ":"), sort_keys=True))
    print(f"{path.name}: {len(cases)} cases, {path.stat().st_size/1024:.0f} KB")
    return len(cases)


def _ghost_case(cases: list, slots: list, model) -> dict:
    """A case where a deadhead and a parked ghost are upstream of Magoun.

    Both are boardable-looking and neither is: a NON_REVENUE train runs express,
    and a stale position is a train parked hours ago. Each is placed on the vehicle
    a real MBTA prediction names, so a port that forgets either filter promotes
    that row from "mbta" (+/-75 s) to "departed Ball Sq" (+/-7 s) -- a 68 s band
    error on the number the rider acts on.
    """
    # Prefer a base whose schedule tier is already unbacked: the no-show veto is
    # the other thing a deadhead at the terminus would wrongly satisfy.
    base = next((c for c in cases
                 if any(not r["backed"] and r["source"] == "schedule"
                        for r in c["expected"])), cases[0])
    base = json.loads(json.dumps(base))
    now = base["now"]
    stamp = lambda t: (dt.datetime.fromtimestamp(t, dt.timezone.utc)
                       .replace(microsecond=0).isoformat())
    inbound = [p for p in base["preds"]
               if p["relationships"]["stop"]["data"]["id"] == service.MAGOUN_IN
               and p["attributes"]["direction_id"] == 0
               and (p["relationships"]["vehicle"]["data"] or {}).get("id")]
    vids = list(dict.fromkeys((p["relationships"]["vehicle"]["data"] or {})["id"]
                              for p in inbound))

    def place(vid, stop, status, revenue, age):
        """Put `vid` where the row's source would change if a filter were missing."""
        v = next((x for x in base["vehicles"] if x["id"] == vid), None)
        if v is None:
            v = {"id": vid, "attributes": {}, "relationships": {}}
            base["vehicles"].append(v)
        v["attributes"] = {"current_status": status, "current_stop_sequence": 1,
                           "direction_id": 0, "revenue": revenue,
                           "updated_at": stamp(now - age)}
        v["relationships"] = {"route": {"data": {"id": "Green-E"}},
                              "stop": {"data": {"id": stop}},
                              "trip": {"data": {"id": None}}}

    if vids:
        # A deadhead one stop out: live, express, and not the train you can board.
        place(vids[0], service.MAGOUN_IN, "IN_TRANSIT_TO", "NON_REVENUE", 30)
    if len(vids) > 1:
        # A revenue train whose position went stale 5 minutes ago: parked, not en
        # route. Chosen just past STALE_VEHICLE (180 s) but inside a plausible
        # wrong threshold, so widening the window is caught too, measured.
        place(vids[1], service.MAGOUN_IN, "IN_TRANSIT_TO", "REVENUE", 300)
    # A deadhead sitting on the inbound terminus platform: it must not satisfy the
    # no-show veto, and it must not produce a berth row either.
    place("G-SYNTH-DEADHEAD", service.MED_IN, "STOPPED_AT", "NON_REVENUE", 30)

    base["expected"] = service.compute_rows(
        now, base["preds"], base["vehicles"], model, WALK, QS, HORIZON,
        base["berths"], slots, set(base["skipped"]))
    return base


def _berths(tracker, v3):
    return tracker.update(v3)


if __name__ == "__main__":
    total = 0
    for day in (sys.argv[1:] or ["2026-09-25"]):
        total += build(day)
    print(f"{total} fixture cases")
