"""Watch for the schedule rating change that resets every fitted constant.

A rating change is the one event that invalidates the whole model at once -- the
schedule offset, the berth rule's `sched_bias`, the veto window, all of it. The next
one is 2026-12-12, which is not a date anybody typed in: it is `feed_end_date` on
the feed MBTA is serving right now.

The signal has to survive republishing. `cdn.mbta.com/archive/archived_feeds.txt`
lists every published feed newest first, and `feed_start_date` moves on every
republish -- 1016 rows, 1016 distinct start dates, so watching that would fire
weekly and mean nothing. What is stable is the rating label inside `feed_version`:
"Fall 2026, 2026-09-24T17:51:42+00:00, version D" held across all 14 republishes of
the current rating and changed at the Summer->Fall boundary. So the identity watched
here is (season, version, feed_end_date), and a move in any of them means refit.

This only detects. The refit itself reads data/raw and data/magoun.parquet, which
live on the capture host and not in git, so `./src/refit.sh` runs there.

Usage:
  python src/rating.py              # show the current rating and what is recorded
  python src/rating.py --check      # exit 1 if it moved (for CI)
  python src/rating.py --record     # accept the current rating as the baseline
"""
import argparse
import csv
import io
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
RECORD = ROOT / "data" / "rating.json"
FEEDS = "https://cdn.mbta.com/archive/archived_feeds.txt"
KEYS = ("season", "version", "feed_end_date")


def parse(text: str) -> dict:
    """The rating MBTA is serving, from the first (newest) row of the feed index."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("archived_feeds.txt has no rows")
    r = rows[0]
    # "Fall 2026, 2026-09-24T17:51:42+00:00, version D" -- the middle field is the
    # build timestamp and changes on every republish, so it is deliberately not
    # part of the identity.
    parts = [p.strip() for p in r["feed_version"].split(",")]
    return {"season": parts[0],
            "version": parts[-1] if len(parts) >= 3 else "",
            "feed_start_date": r["feed_start_date"],
            "feed_end_date": r["feed_end_date"],
            "feed_version": r["feed_version"]}


def fetch(url: str = FEEDS) -> dict:
    with urllib.request.urlopen(url, timeout=30) as f:
        return parse(f.read().decode())


def recorded() -> dict | None:
    return json.loads(RECORD.read_text()) if RECORD.exists() else None


def changed(before: dict | None, after: dict) -> list[str]:
    """Which parts of the rating identity moved. Empty means the same rating."""
    if before is None:
        return ["nothing recorded yet"]
    return [f"{k}: {before.get(k)!r} -> {after[k]!r}"
            for k in KEYS if before.get(k) != after[k]]


def describe(r: dict) -> str:
    return (f"{r['season']} {r['version']}, serving "
            f"{r['feed_start_date']}..{r['feed_end_date']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if the rating moved")
    ap.add_argument("--record", action="store_true",
                    help="accept the current rating as the baseline")
    ap.add_argument("--url", default=FEEDS)
    a = ap.parse_args()

    cur = fetch(a.url)
    old = recorded()
    print(f"serving:  {describe(cur)}")
    print(f"recorded: {describe(old) if old else '(nothing recorded)'}")
    moved = changed(old, cur)

    if a.record:
        RECORD.write_text(json.dumps(cur, indent=1) + "\n")
        print(f"recorded {RECORD}")
        return

    if not moved:
        print(f"\nsame rating; it ends {cur['feed_end_date']}")
        return

    print("\nthe rating moved:")
    for line in moved:
        print(f"  {line}")
    print("\nEvery schedule-dependent constant is now fitted to the wrong timetable.")
    print("On the capture host, where data/raw lives:")
    print("  ./src/refit.sh                 # rebuild the dataset, refit, diff")
    print("  ./src/refit.sh --fixtures      # then, deliberately, and read the diff")
    print("  uv run python src/rating.py --record")
    if a.check:
        sys.exit(1)


if __name__ == "__main__":
    main()
