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

from collections.abc import Iterator

import polars as pl

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIVE = ROOT / "data" / "live"
PAIRS = ROOT / "data" / "pairs"
SCHEMA = {
    "day": pl.String, "stop": pl.String, "dir": pl.Int8, "route": pl.String,
    "veh": pl.String, "trip": pl.String, "made_at": pl.Int64, "pred_arr": pl.Int64,
    "actual_arr": pl.Int64, "lead_s": pl.Int32, "err_s": pl.Int32, "unc_s": pl.Int32,
}


KEYFRAME = 240          # a full snapshot every hour, so a corrupt run loses <1 h
# Diff every field rather than a hand-picked set. An earlier version tracked only
# arr/dep/rel/unc and silently lost `veh`, which is null on a prediction until a
# vehicle is assigned to the trip and then changes.
_SENTINEL = "\u0000del"


def _pkey(p: dict) -> str:
    return f"{p['stop']}|{p.get('trip')}"


def to_delta(snaps: list[dict]) -> list[str]:
    """Re-encode a finished day as keyframes plus what actually changed.

    37% of rows repeat verbatim and most of the rest move an ETA by one or two
    seconds, so the full-state format spends ~150 bytes to record a 1-second
    change. Measured 4.6x smaller, which is what makes keeping raw days forever
    affordable instead of pruning them.
    """
    out, prev_p, prev_v = [], {}, {}
    for i, s in enumerate(snaps):
        cp = {_pkey(p): p for p in s["preds"]}
        cv = {v["id"]: v for v in s["vehicles"]}
        if i % KEYFRAME == 0:
            rec = {"t": s["t"], "k": 1, "p": list(cp.values()),
                   "v": list(cv.values())}
        else:
            dp = {}
            for k, pr in cp.items():
                old = prev_p.get(k)
                if old is None:
                    dp[k] = {"__full": pr}
                else:
                    d = {f: pr[f] for f in pr if pr[f] != old.get(f)}
                    d.update({f: _SENTINEL for f in old if f not in pr})
                    if d:
                        dp[k] = d
            dv = {}
            for k, v in cv.items():
                old = prev_v.get(k)
                if old is None:
                    dv[k] = {"__full": v}
                elif v != old:
                    d = {f: v[f] for f in v if v[f] != old.get(f)}
                    d.update({f: _SENTINEL for f in old if f not in v})
                    dv[k] = d
            rec = {"t": s["t"], "p": dp, "v": dv}
            gone = [k for k in prev_p if k not in cp]
            if gone:
                rec["x"] = gone
            # Vehicles leave the feed too. Omitting this silently kept departed
            # trains alive forever in the reconstruction, which changed arrival
            # detection and quietly dropped 1,595 pairs from a single day.
            gonev = [k for k in prev_v if k not in cv]
            if gonev:
                rec["xv"] = gonev
        if "alerts" in s:
            rec["alerts"] = s["alerts"]
        out.append(json.dumps(rec, separators=(",", ":")))
        prev_p, prev_v = cp, cv
    return out


def _apply(old: dict | None, d: dict) -> dict:
    if "__full" in d:
        return d["__full"]
    out = dict(old or {})
    for f, v in d.items():
        if v == _SENTINEL:
            out.pop(f, None)
        else:
            out[f] = v
    return out


def from_delta(lines) -> Iterator[dict]:
    """Rebuild full snapshots from the delta form. Inverse of to_delta."""
    prev_p, prev_v = {}, {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("k"):
            cp = {_pkey(p): p for p in rec["p"]}
            cv = {v["id"]: v for v in rec["v"]}
        else:
            cp = {k: dict(v) for k, v in prev_p.items()}
            for k in rec.get("x", []):
                cp.pop(k, None)
            for k, d in rec["p"].items():
                cp[k] = _apply(cp.get(k), d)
            cv = {k: dict(v) for k, v in prev_v.items()}
            for k in rec.get("xv", []):
                cv.pop(k, None)
            for k, d in rec["v"].items():
                cv[k] = _apply(cv.get(k), d)
        snap = {"t": rec["t"], "preds": list(cp.values()),
                "vehicles": list(cv.values())}
        if "alerts" in rec:
            snap["alerts"] = rec["alerts"]
        prev_p, prev_v = cp, cv
        yield snap


def compact_parquet(path: pathlib.Path) -> tuple[int, int] | None:
    """Rewrite a finished raw day as delta-encoded parquet, if it round-trips.

    Chosen over delta-JSONL after measuring like-for-like: 1.25 vs 2.02 MB/day,
    and DuckDB queries it directly instead of needing reconstruction in Python.
    """
    import archive
    with gzip.open(path, "rt") as f:
        snaps = [json.loads(l) for l in f if l.strip()]
    if not snaps:
        return None
    day = re.search(r"(\d{4}-\d{2}-\d{2})", path.name).group(1)
    dest = LIVE / f"day={day}"
    archive.write(snaps, dest)
    back = list(archive.read(dest))
    if len(back) != len(snaps):
        return None

    def canon(s):
        return (round(s["t"], 3),
                sorted(json.dumps({k: p.get(k) for k in archive.PF}, sort_keys=True)
                       for p in s["preds"]),
                sorted(json.dumps({k: v.get(k) for k in archive.VF}, sort_keys=True)
                       for v in s["vehicles"]),
                json.dumps(s.get("alerts"), sort_keys=True))
    if any(canon(a) != canon(b) for a, b in zip(snaps, back)):
        return None
    before = path.stat().st_size
    after = sum(f.stat().st_size for f in dest.iterdir())
    path.unlink()
    return before, after


def compact(path: pathlib.Path) -> tuple[int, int] | None:
    """Rewrite a finished raw day in delta form, only if it round-trips exactly.

    The archiver is deliberately left alone: it is the one process that must never
    lose data, so it keeps writing the simple full-state format and compaction
    happens here, on days that are already closed.
    """
    with gzip.open(path, "rt") as f:
        snaps = [json.loads(l) for l in f if l.strip()]
    if not snaps:
        return None
    lines = to_delta(snaps)
    back = list(from_delta(lines))
    if len(back) != len(snaps):
        return None
    # Compare the ENTIRE snapshot. An earlier version checked only four
    # prediction fields, passed, and still lost rows.
    def canon(s):
        return (round(s["t"], 3),
                sorted((json.dumps(p, sort_keys=True) for p in s["preds"])),
                sorted((json.dumps(v, sort_keys=True) for v in s["vehicles"])),
                json.dumps(s.get("alerts"), sort_keys=True))
    for a, b in zip(snaps, back):
        if canon(a) != canon(b):
            return None
    before = path.stat().st_size
    tmp = path.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt", compresslevel=9) as f:
        f.write("\n".join(lines) + "\n")
    out = path.with_name(path.name.replace(".jsonl.gz", ".delta.jsonl.gz"))
    tmp.replace(out)
    path.unlink()
    return before, out.stat().st_size


def rollup_day(path: pathlib.Path) -> pl.DataFrame:
    """Distil one archived day, in whichever form it is stored."""
    day = re.search(r"(\d{4}-\d{2}-\d{2})", path.name).group(1)
    # Arrivals must be counted as TRANSITIONS into STOPPED_AT. Keying on
    # (vehicle, stop) for a whole day records only each vehicle's first visit,
    # and trains cycle through Magoun many times a day -- measured at a 30%
    # undercount over three hours, worse over a full day.
    actual: dict[tuple[str, str], list[float]] = {}
    prev: dict[str, tuple] = {}
    preds: list[tuple] = []

    for snap in _snapshots(path):
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


def _snapshots(path: pathlib.Path):
    """Yield snapshots from any archive form: raw JSONL, delta JSONL, or parquet."""
    if path.is_dir():
        import archive
        yield from archive.read(path)
        return
    with gzip.open(path, "rt") as f:
        if ".delta." in path.name:
            yield from from_delta(f)
        else:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prune", type=int, metavar="DAYS",
                    help="delete raw archives older than DAYS, once rolled up")
    ap.add_argument("--compact", action="store_true",
                    help="re-encode finished raw days in delta form (~4x smaller)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    PAIRS.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")

    for raw in sorted(list(LIVE.glob("rt-*.jsonl*.gz")) + list(LIVE.glob("day=*"))):
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

    if a.compact:
        for raw in sorted(LIVE.glob("rt-*.jsonl.gz")):
            if ".delta." in raw.name or raw.name.endswith(f"{today}.jsonl.gz"):
                continue
            r = compact_parquet(raw)
            print(f"compact {raw.name}: "
                  + (f"{r[0]/1e6:.2f} -> {r[1]/1e6:.2f} MB ({r[0]/r[1]:.1f}x)"
                     if r else "SKIPPED (round-trip check failed)"))

    if a.prune:
        cutoff = time.time() - a.prune * 86400
        for raw in sorted(LIVE.glob("rt-*.jsonl.gz")):
            day = re.search(r"(\d{4}-\d{2}-\d{2})", raw.name).group(1)
            if raw.stat().st_mtime < cutoff and (PAIRS / f"pairs-{day}.parquet").exists():
                raw.unlink()
                print(f"pruned {raw.name}")


if __name__ == "__main__":
    main()
