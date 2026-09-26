"""Score the board against real outcomes and publish it as stats.json.

The panel answers two different questions, so it reads two different sources.

**Coverage** -- of the trains that actually came, how many did the app announce
early enough to be on the platform for? That is `replay.score`, one rider per real
arrival, and it needs the full archive: the vehicle stream to find arrivals, the
prediction stream to find when "leave now" would have fired.

**Cost** -- once it fires, how long do you stand there, and how long from deciding
to the doors? That is `simulate.run`, riders deciding on a five-minute grid and
following the real `compute_rows` through every tier. Both means come from the same
riders, so the gap between them means something. Platform wait alone rewards
dawdling -- a strategy that keeps you home until it is certain scores perfectly on
it while putting you on a later train -- which is why door-to-train sits beside it.

**Prediction error** -- `by_lead` is a different question again, and `data/pairs`
already holds exactly it: (made_at, pred_arr, actual_arr) per arrival. Pairs is
never pruned, so this is the half of the panel with a long memory.

The archive behind coverage and cost *is* pruned at 90 days, so each day's score is
appended to `data/scores.jsonl` the first time it is computed. The window is then
aggregated from that record, and the panel keeps its history after the raw days it
was computed from are gone.

Usage:
  python src/stats.py                  # score any closed day not yet scored
  python src/stats.py --days 14        # publish a 14-day window instead of 7
  python src/stats.py --force          # rescore days already in the scoreboard
"""
import argparse
import json
import pathlib
import re
import time

import polars as pl

import replay
import service
import simulate

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIVE = ROOT / "data" / "live"
PAIRS = ROOT / "data" / "pairs"
SCORES = ROOT / "data" / "scores.jsonl"
OUT = ROOT / "data" / "stats.json"
# The static board fetches stats.json as a sibling asset. Same reasoning as
# model.json in fit.py: an artifact that stops at data/ never reaches the rider.
WEB_OUT = ROOT / "web" / "stats.json"

MAGOUN_IN = "70508"
# Predictions past 20 min are not predictions, they are mispairs. rollup pairs each
# prediction with that vehicle's NEXT arrival at that stop, so a missed STOPPED_AT
# transition attributes a prediction to the following visit. Measured on
# 2026-09-24, Magoun inbound: |err| tops out at ~620 s through the 15-20 min bin
# and then explodes -- p50 -414 s at 20-30 min, -2188 s at 30-45 min. The feed's
# real reach on this platform is 8-13 min (CLAUDE.md), so nothing true is lost.
MAX_LEAD_S = 1200
LEAD_BINS = [(0, 300, "0-5min"), (300, 600, "5-10min"),
             (600, 900, "10-15min"), (900, 1200, "15-20min")]


def archives() -> dict[str, pathlib.Path]:
    """Every archived day, in whichever form it is stored, newest form winning."""
    out: dict[str, pathlib.Path] = {}
    for p in sorted(list(LIVE.glob("rt-*.jsonl*.gz")) + list(LIVE.glob("day=*"))):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
        if m:
            out[m.group(1)] = p
    return out


def score_day(day: str, path: pathlib.Path, walk: int) -> dict:
    """Coverage and cost for one closed day.

    `day` is passed to the replay deliberately: its schedule slots default to the
    three most recent snapshots, which silently drops the timetable tier for an
    older day. Measured on 2026-09-24 -- pinned to its own day, 22 of 74 arrivals
    were warned by the timetable alone and 69 were caught; pinned to the wrong
    day, the schedule tier disappears and caught falls to 60.
    """
    rows = replay.score(walk=walk, n=10**9, paths=[path], day=day)
    told = [r for r in rows if r["told"] is not None]

    # The rider grid replays compute_rows, which reaches for live skip markers.
    # Freeze that: a score of a past day must not depend on today's network.
    service.skipped_trips = lambda *a, **k: set()
    model = service.Model()
    waits = simulate.run(day, simulate.make_strategy(model, 0.10,
                                                     ("mbta", "schedule"), walk), walk)
    simulate._ADAPTED.clear()          # one adapted day is ~5,700 snapshots
    # A rider who decides at 02:15 and boards at 05:10 is not a rider. simulate.run
    # already drops anyone facing over an hour on the platform; hold door-to-train
    # to the same hour rather than inventing a service window. Measured on
    # 2026-09-25, a full day: 47 of 286 riders were overnight, with door-to-train
    # reaching 292 min and dragging the mean from 16.1 to 42.6. The partial
    # 2026-09-24 archive starts at 13:33 and hid this entirely.
    waits = [w for w in waits if w[1] * 60 <= simulate.MAX_WAIT_S]

    return {"day": day, "arrivals": len(rows), "told": len(told),
            "caught": sum(1 for r in told if r["caught"]),
            "riders": len(waits),
            # Minutes out of simulate.run, seconds in the contract.
            "platform_wait_sum_s": round(sum(w[0] for w in waits) * 60),
            "door_to_train_sum_s": round(sum(w[1] for w in waits) * 60),
            "walk_s": walk, "scored_at": int(time.time())}


def read_scores() -> dict[str, dict]:
    if not SCORES.exists():
        return {}
    return {r["day"]: r for r in
            (json.loads(l) for l in SCORES.read_text().splitlines() if l.strip())}


def write_scores(scores: dict[str, dict]) -> None:
    SCORES.write_text("".join(
        json.dumps(scores[d], separators=(",", ":")) + "\n" for d in sorted(scores)))


def by_lead(days: list[str]) -> list[dict]:
    """Prediction error at Magoun inbound, binned by how much warning it gave."""
    files = [PAIRS / f"pairs-{d}.parquet" for d in days]
    files = [f for f in files if f.exists()]
    if not files:
        return []
    df = (pl.concat([pl.read_parquet(f) for f in files])
          .filter((pl.col("stop") == MAGOUN_IN) & (pl.col("dir") == 0)
                  & (pl.col("lead_s") <= MAX_LEAD_S)))
    out = []
    for lo, hi, name in LEAD_BINS:
        b = df.filter(pl.col("lead_s").is_between(lo, hi, closed="left"))
        if not b.height:
            continue
        e = b["err_s"]
        out.append({"bin": name, "n": b.height,
                    "p10": round(e.quantile(0.10)), "p50": round(e.quantile(0.50)),
                    "p90": round(e.quantile(0.90))})
    return out


def build(scores: dict[str, dict], days: int) -> dict | None:
    """Aggregate the last `days` scored days into the published contract."""
    window = sorted(scores)[-days:]
    if not window:
        return None
    rs = [scores[d] for d in window]
    riders = sum(r["riders"] for r in rs)
    of = sum(r["told"] for r in rs)
    return {
        "as_of": window[-1],
        "window_days": len(window),
        "caught": sum(r["caught"] for r in rs),
        "of": of,
        # A window with no riders would divide by zero, and a null here renders as
        # "NaN min" on the board rather than hiding the panel. Report -1 instead:
        # absent data, not a zero-minute wait.
        "mean_platform_wait_s": round(sum(r["platform_wait_sum_s"] for r in rs)
                                      / riders) if riders else -1,
        "mean_door_to_train_s": round(sum(r["door_to_train_sum_s"] for r in rs)
                                      / riders) if riders else -1,
        "by_lead": by_lead(window),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="window to publish")
    ap.add_argument("--walk", type=int, default=service.DEFAULT_WALK)
    ap.add_argument("--force", action="store_true",
                    help="rescore days already in the scoreboard")
    a = ap.parse_args()

    today = time.strftime("%Y-%m-%d")
    scores = read_scores()
    for day, path in sorted(archives().items()):
        if day == today:
            print(f"{day} still being written, skipping")
            continue
        if day in scores and not a.force:
            continue
        t0 = time.time()
        scores[day] = score_day(day, path, a.walk)
        r = scores[day]
        print(f"{day}: caught {r['caught']}/{r['told']} of {r['arrivals']} arrivals, "
              f"{r['riders']} riders, {time.time() - t0:.1f}s")
    write_scores(scores)

    stats = build(scores, a.days)
    if stats is None:
        print("no closed day has been scored yet; not writing stats.json")
        return
    OUT.write_text(json.dumps(stats))
    WEB_OUT.write_text(OUT.read_text())
    print(f"wrote {OUT} and {WEB_OUT}")
    print(f"  {stats['window_days']}d to {stats['as_of']}: "
          f"caught {stats['caught']}/{stats['of']}")
    print(f"  platform wait  {stats['mean_platform_wait_s'] / 60:5.1f} min")
    print(f"  door-to-train  {stats['mean_door_to_train_s'] / 60:5.1f} min")
    for b in stats["by_lead"]:
        print(f"  {b['bin']:9s} n={b['n']:6d}  p10={b['p10']:6d} p50={b['p50']:6d} "
              f"p90={b['p90']:6d}")


if __name__ == "__main__":
    main()
