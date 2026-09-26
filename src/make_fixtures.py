"""Freeze real snapshots as language-neutral fixtures for the contract test.

These pin `service.compute_rows` today and are what a JavaScript port will be
checked against tomorrow. Everything the function needs is inlined, so a fixture
can be replayed with no network, no clock and no file layout assumptions.
"""
import json
import pathlib
import sys

import service
import simulate

OUT = service.ROOT / "tests" / "fixtures"
WALK = 390
QS = (0.1, 0.5, 0.9)
HORIZON = 45 * 60


def build(day: str, count: int = 12) -> int:
    snaps = list(simulate.snapshots(day))
    if not snaps:
        print(f"no archive for {day}", file=sys.stderr)
        return 0
    import datetime as dt
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

    path = OUT / f"cases-{day}.json"
    path.write_text(json.dumps(cases, separators=(",", ":"), sort_keys=True))
    print(f"{path.name}: {len(cases)} cases, {path.stat().st_size/1024:.0f} KB")
    return len(cases)


def _berths(tracker, v3):
    return tracker.update(v3)


if __name__ == "__main__":
    total = 0
    for day in (sys.argv[1:] or ["2026-09-25"]):
        total += build(day)
    print(f"{total} fixture cases")
