"""Read out what a refit changed, so the decision to accept it is an informed one.

A rating change resets every schedule-dependent constant, and when the quantiles
move the 28 frozen fixtures break -- a 30 s shift in the schedule offset fails all
28. That failure is the system working. The wrong response is to regenerate the
fixtures until the suite is quiet, because a regenerated fixture set that quietly
drops a tier is how the contract test stops testing anything while still passing.

So this prints two diffs and asserts nothing. `model` says how far the quantiles
moved. `fixtures` says what that did to the expected rows, and shouts if a tier
stopped appearing at all.

Usage:
  python src/refit.py model    OLD.json NEW.json
  python src/refit.py fixtures OLD_DIR  NEW_DIR
"""
import json
import pathlib
import sys

GRID_KEYS = ("tiers", "rides")        # nested: {name: {"n":, "q": [...]}}
FLAT_KEYS = ("sched", "berth")        # one {"n":, "q": [...]} each
SCALARS = ("n_legs", "days", "headway_median_s")
# The quantiles the rider actually acts on. lo is quoted, not the median.
MARKS = ((0.10, "q10"), (0.50, "q50"), (0.90, "q90"))


def _at(model: dict, q: list[float], p: float) -> float:
    grid = model["grid"]
    i = min(range(len(grid)), key=lambda j: abs(grid[j] - p))
    return q[i]


def _band(model: dict, before: dict, after: dict, name: str) -> list[str]:
    out = []
    dn = after["n"] - before["n"]
    moves = [(lab, _at(model, after["q"], p) - _at(model, before["q"], p))
             for p, lab in MARKS]
    worst = max(abs(x - y) for x, y in zip(before["q"], after["q"])) \
        if len(before["q"]) == len(after["q"]) else float("inf")
    if any(abs(d) >= 0.5 for _, d in moves) or worst >= 0.5 or dn:
        out.append(f"  {name:22s} n {before['n']:6d} -> {after['n']:<6d} ({dn:+d})  "
                   + "  ".join(f"{lab} {d:+7.1f}s" for lab, d in moves)
                   + f"   worst {worst:+.1f}s")
    return out


def model_diff(before: dict, after: dict) -> list[str]:
    """How far every published quantile moved. Empty means a genuine no-op."""
    lines: list[str] = []
    if before.get("grid") != after.get("grid"):
        lines.append("  GRID CHANGED -- every consumer's lookup shifts")
    for k in SCALARS:
        if before.get(k) != after.get(k):
            lines.append(f"  {k:22s} {before.get(k)} -> {after.get(k)}")
    for k in FLAT_KEYS:
        if k in before and k in after:
            lines += _band(after, before[k], after[k], k)
        elif (k in before) != (k in after):
            lines.append(f"  {k:22s} {'REMOVED' if k in before else 'ADDED'}")
    for group in GRID_KEYS:
        b, a = before.get(group, {}), after.get(group, {})
        for name in sorted(set(b) | set(a)):
            if name not in b or name not in a:
                lines.append(f"  {group}.{name:16s} "
                             f"{'REMOVED -- a tier the board still selects' if name in b else 'ADDED'}")
            else:
                lines += _band(after, b[name], a[name], f"{group}.{name}")
    cb = before.get("constants", {})
    ca = after.get("constants", {})
    for name in sorted(set(cb) | set(ca)):
        if cb.get(name) != ca.get(name):
            lines.append(f"  constants.{name:12s} {cb.get(name)} -> {ca.get(name)}")
    return lines


def _cases(path: pathlib.Path) -> dict[str, list]:
    return {f.name: json.loads(f.read_text()) for f in sorted(path.glob("cases-*.json"))}


def _sources(cases: dict[str, list]) -> set[str]:
    return {r["source"] for file in cases.values() for c in file for r in c["expected"]}


def fixture_diff(before: dict[str, list], after: dict[str, list]) -> list[str]:
    """What the refit did to the expected rows, and whether a tier vanished.

    A tier that stops appearing is the failure mode worth shouting about: the
    contract test still passes, with one fewer thing being tested.
    """
    lines: list[str] = []
    gone = _sources(before) - _sources(after)
    added = _sources(after) - _sources(before)
    if gone:
        lines.append(f"  A TIER STOPPED APPEARING: {sorted(gone)}")
        lines.append("  Do not accept this. A fixture set that covers one fewer "
                     "source still passes.")
    if added:
        lines.append(f"  new source in the expected rows: {sorted(added)}")

    if set(before) != set(after):
        lines.append(f"  files {sorted(before)} -> {sorted(after)}")
    nb = sum(len(v) for v in before.values())
    na = sum(len(v) for v in after.values())
    if nb != na:
        lines.append(f"  CASE COUNT {nb} -> {na}"
                     + ("  (fewer cases is less coverage)" if na < nb else ""))

    changed_cases = changed_rows = 0
    field_worst: dict[str, float] = {}
    flips: list[str] = []
    for name in sorted(set(before) & set(after)):
        for i, (cb, ca) in enumerate(zip(before[name], after[name])):
            rb, ra = cb["expected"], ca["expected"]
            if len(rb) != len(ra):
                lines.append(f"  {name}#{i}: {len(rb)} rows -> {len(ra)}")
                changed_cases += 1
                continue
            touched = False
            for j, (x, y) in enumerate(zip(rb, ra)):
                for f in sorted(set(x) | set(y)):
                    if x.get(f) == y.get(f):
                        continue
                    touched = True
                    if isinstance(x.get(f), (int, float)) and \
                            isinstance(y.get(f), (int, float)) and \
                            not isinstance(x.get(f), bool):
                        d = abs(y[f] - x[f])
                        field_worst[f] = max(field_worst.get(f, 0.0), d)
                    else:
                        flips.append(f"  {name}#{i} row {j}: {f} "
                                     f"{x.get(f)!r} -> {y.get(f)!r}")
                if set(x) != set(y):
                    flips.append(f"  {name}#{i} row {j}: fields "
                                 f"{sorted(set(x) ^ set(y))} appeared or vanished")
            changed_rows += sum(1 for x, y in zip(rb, ra) if x != y)
            changed_cases += touched
    if changed_cases:
        lines.append(f"  {changed_cases} of {min(nb, na)} cases changed, "
                     f"{changed_rows} rows")
    for f, d in sorted(field_worst.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {f:10s} worst move {d:+.1f}")
    # Source and skipped changes are per-row decisions, not arithmetic. Show them
    # all: this is the half of the diff a human has to actually read.
    lines += flips[:40]
    if len(flips) > 40:
        lines.append(f"  ... and {len(flips) - 40} more field changes")
    return lines


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in ("model", "fixtures"):
        sys.exit(__doc__)
    what, a, b = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    if what == "model":
        lines = model_diff(json.loads(a.read_text()), json.loads(b.read_text()))
        head = "the model"
    else:
        lines = fixture_diff(_cases(a), _cases(b))
        head = "the expected rows"
    if not lines:
        print(f"{head}: no change")
        return
    print(f"{head} changed:")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
