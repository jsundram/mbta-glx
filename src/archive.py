"""Compact a finished archive day into delta-encoded parquet.

Measured against the alternatives on a real day (preds + vehicles):

    JSONL + gzip  (capture format)              9.89 MB
    delta JSONL + gzip                          2.02 MB   4.9x
    delta parquet + zstd, times as offsets      1.25 MB   7.9x

Two things do the work. Only rows that changed are stored -- 37% of rows repeat
verbatim and most of the rest move an ETA by a second or two. And timestamps are
stored as offsets from the snapshot time, turning ten-digit epochs into small
integers that zstd flattens.

The other half of the argument is that DuckDB reads this directly. The JSONL form
has to be reconstructed in Python before any question can be asked of it.
"""
import gzip
import json
import pathlib
from collections.abc import Iterator

import polars as pl

PF = ("stop", "trip", "route", "dir", "veh", "seq", "arr", "dep", "unc", "rel")
VF = ("id", "route", "trip", "dir", "stop", "status", "seq", "ts")
OFFSET_P = ("arr", "dep")
OFFSET_V = ("ts",)


def _pkey(p: dict) -> str:
    return f"{p['stop']}|{p.get('trip')}"


def _encode(snaps, key_fn, fields, coll, offsets) -> pl.DataFrame:
    """Rows carry a bitmask of which fields changed.

    A null cannot mean "unchanged": `veh` and `dep` are legitimately null, so a
    field CHANGING to null is indistinguishable from one that did not move. That
    ambiguity silently corrupted 2,088 of 5,738 snapshots before the mask existed.
    """
    rows, prev = [], {}
    for s in snaps:
        t = int(s["t"])
        cur = {key_fn(e): e for e in s[coll]}
        for k, e in cur.items():
            old = prev.get(k)
            if old is None:
                d = {c: e.get(c) for c in fields}
                mask = (1 << len(fields)) - 1
            else:
                mask = 0
                d = {}
                for i, c in enumerate(fields):
                    if e.get(c) != old.get(c):
                        mask |= 1 << i
                        d[c] = e.get(c)
                    else:
                        d[c] = None
                if mask == 0:
                    continue
            rows.append({"t": t, "key": k, "gone": False, "new": old is None,
                         "mask": mask, **d})
        for k in prev:
            if k not in cur:
                rows.append({"t": t, "key": k, "gone": True, "new": False,
                             "mask": 0, **{c: None for c in fields}})
        prev = cur
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame(
        schema={"t": pl.Int64, "key": pl.String, "gone": pl.Boolean,
                "new": pl.Boolean, "mask": pl.Int64,
                **{c: pl.String for c in fields}})
    for c in offsets:
        if c in df.columns:
            df = df.with_columns(
                (pl.col(c).cast(pl.Int64, strict=False) - pl.col("t"))
                .cast(pl.Int32).alias(c))
    return df.sort(["key", "t"])


def _decode(df: pl.DataFrame, fields, offsets, id_field: str | None) -> dict:
    """{t: {key: entity}} deltas, times restored to absolute."""
    for c in offsets:
        if c in df.columns:
            df = df.with_columns(
                (pl.col(c).cast(pl.Int64, strict=False) + pl.col("t")).alias(c))
    out: dict[int, list] = {}
    for r in df.sort("t").iter_rows(named=True):
        out.setdefault(r["t"], []).append(r)
    return out


def write(snaps: list[dict], dest: pathlib.Path) -> pathlib.Path:
    dest.mkdir(parents=True, exist_ok=True)
    _encode(snaps, _pkey, PF, "preds", OFFSET_P).write_parquet(
        dest / "preds.parquet", compression="zstd")
    _encode(snaps, lambda v: v["id"], VF, "vehicles", OFFSET_V).write_parquet(
        dest / "vehicles.parquet", compression="zstd")
    meta = [{"t": s["t"], "alerts": json.dumps(s["alerts"])}
            for s in snaps if "alerts" in s]
    (dest / "meta.json").write_text(json.dumps(
        {"n": len(snaps), "times": [s["t"] for s in snaps], "alerts": meta}))
    return dest


def read(src: pathlib.Path, pred_stop: str | None = None) -> Iterator[dict]:
    """Rebuild full snapshots. Inverse of write().

    `pred_stop` keeps only that stop's predictions; vehicles are always whole. It
    is exact, not a sample: the key is `stop|trip`, so a key never changes stop and
    each key's deltas rebuild without reference to any other key. It exists
    because one stop is 0.3% of a day's prediction rows (3,952 of 1,244,816 on
    2026-10-06), and decoding the rest into dicts was ~4 of /history's 10 s.
    """
    meta = json.loads((src / "meta.json").read_text())
    preds = pl.read_parquet(src / "preds.parquet")
    if pred_stop is not None:
        preds = preds.filter(pl.col("key").str.starts_with(f"{pred_stop}|"))
    dp = _decode(preds, PF, OFFSET_P, None)
    dv = _decode(pl.read_parquet(src / "vehicles.parquet"), VF, OFFSET_V, "id")
    alerts = {a["t"]: json.loads(a["alerts"]) for a in meta["alerts"]}
    cp: dict[str, dict] = {}
    cv: dict[str, dict] = {}
    for t in meta["times"]:
        ti = int(t)
        for r in dp.get(ti, []):
            k = r["key"]
            if r["gone"]:
                cp.pop(k, None)
            elif r["new"] or k not in cp:
                cp[k] = {c: r[c] for c in PF}
            else:
                m = r["mask"]
                cp[k] = {**cp[k],
                         **{c: r[c] for i, c in enumerate(PF) if m >> i & 1}}
        for r in dv.get(ti, []):
            k = r["key"]
            if r["gone"]:
                cv.pop(k, None)
            elif r["new"] or k not in cv:
                cv[k] = {c: r[c] for c in VF}
            else:
                m = r["mask"]
                cv[k] = {**cv[k],
                         **{c: r[c] for i, c in enumerate(VF) if m >> i & 1}}
        snap = {"t": t, "preds": list(cp.values()), "vehicles": list(cv.values())}
        if t in alerts:
            snap["alerts"] = alerts[t]
        yield snap
