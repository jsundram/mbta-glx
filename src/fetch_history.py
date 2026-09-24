"""Download LAMP subway on-time-performance parquet files for a date range."""
import concurrent.futures as cf
import pathlib
import sys
import urllib.request
from datetime import date, timedelta

BASE = "https://performancedata.mbta.com/lamp/subway-on-time-performance-v1"
RAW = pathlib.Path(__file__).resolve().parent.parent / "data" / "raw"


def fetch(d: date) -> tuple[date, str]:
    dest = RAW / f"{d}.parquet"
    if dest.exists() and dest.stat().st_size > 0:
        return d, "cached"
    url = f"{BASE}/{d}-subway-on-time-performance-v1.parquet"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            body = r.read()
    except Exception as e:  # noqa: BLE001 - missing service dates are expected
        return d, f"skip ({e})"
    dest.write_bytes(body)
    return d, f"{len(body) / 1e6:.1f}MB"


def main(start: str, end: str) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for d, status in pool.map(fetch, days):
            print(f"{d} {status}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
