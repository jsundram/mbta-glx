"""Distil the raw GTFS-RT archive into compact (prediction, outcome) pairs.

The raw feed is a staging area, not the long-term store. Everything we actually
need for calibration is "at lead time L, the feed said T_pred; the train came at
T_actual". That is a few hundred KB per day instead of ~14 MB, and it is the only
form the model ever reads.

Usage:
  python src/rollup.py                 # roll up any day not yet rolled up
  python src/rollup.py --prune 14      # then delete raw archives older than 14 days
"""
import argparse
import gzip
import json
import pathlib
import re
import time

import polars as pl

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIVE = ROOT / "data" / "live"
PAIRS = ROOT / "data" / "pairs"
SCHEMA = {
    "day": pl.String, "stop": pl.String, "dir": pl.Int8, "route": pl.String,
    "veh": pl.String, "trip": pl.String, "made_at": pl.Int64, "pred_arr": pl.Int64,
    "actual_arr": pl.Int64, "lead_s": pl.Int32, "err_s": pl.Int32, "unc_s": pl.Int32,
}


def rollup_day(path: pathlib.Path) -> pl.DataFrame:
    day = re.search(r"(\d{4}-\d{2}-\d{2})", path.name).group(1)
    # Arrivals must be counted as TRANSITIONS into STOPPED_AT. Keying on
    # (vehicle, stop) for a whole day records only each vehicle's first visit,
    # and trains cycle through Magoun many times a day -- measured at a 30%
    # undercount over three hours, worse over a full day.
    actual: dict[tuple[str, str], list[float]] = {}
    prev: dict[str, tuple] = {}
    preds: list[tuple] = []
    with gzip.open(path, "rt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            snap = json.loads(line)
            t = snap["t"]
            for v in snap["vehicles"]:
                cur = (v.get("stop"), v.get("status"))
                if (v.get("status") == "STOPPED_AT" and v.get("stop")
                        and prev.get(v["id"]) != cur):
                    actual.setdefault((v["id"], v["stop"]), []).append(t)
                prev[v["id"]] = cur
            for p in snap["preds"]:
                if p.get("arr") and p.get("veh"):
                    preds.append((p["veh"], p["stop"], p.get("dir"), p.get("route"),
                                  p.get("trip"), t, p["arr"], p.get("unc")))
    rows = []
    for veh, stop, d, route, trip, made, pred, unc in preds:
        # Pair each prediction with the next arrival of that vehicle at that stop.
        times = actual.get((veh, stop))
        a = next((x for x in times if x > made), None) if times else None
        if a is None:
            continue
        rows.append({
            "day": day, "stop": stop, "dir": d, "route": route, "veh": veh,
            "trip": trip, "made_at": int(made), "pred_arr": int(pred),
            "actual_arr": int(a), "lead_s": int(a - made), "err_s": int(pred - a),
            "unc_s": int(unc) if unc is not None else -1,
        })
    return pl.DataFrame(rows, schema=SCHEMA) if rows else pl.DataFrame(schema=SCHEMA)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prune", type=int, metavar="DAYS",
                    help="delete raw archives older than DAYS, once rolled up")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    PAIRS.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")

    for raw in sorted(LIVE.glob("rt-*.jsonl.gz")):
        day = re.search(r"(\d{4}-\d{2}-\d{2})", raw.name).group(1)
        out = PAIRS / f"pairs-{day}.parquet"
        if out.exists() and not a.force:
            continue
        if day == today:
            print(f"{day} still being written, skipping")
            continue
        df = rollup_day(raw)
        df.write_parquet(out, compression="zstd")
        print(f"{day}: {df.height:6d} pairs  "
              f"{raw.stat().st_size/1e6:6.1f} MB raw -> {out.stat().st_size/1e3:6.1f} KB")

    if a.prune:
        cutoff = time.time() - a.prune * 86400
        for raw in sorted(LIVE.glob("rt-*.jsonl.gz")):
            day = re.search(r"(\d{4}-\d{2}-\d{2})", raw.name).group(1)
            if raw.stat().st_mtime < cutoff and (PAIRS / f"pairs-{day}.parquet").exists():
                raw.unlink()
                print(f"pruned {raw.name}")


if __name__ == "__main__":
    main()
