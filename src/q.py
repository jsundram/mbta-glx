"""Ad-hoc SQL over the archive. The replacement for zcat | grep.

    ./src/q.sh "SELECT * FROM pairs WHERE stop='70508' LIMIT 5"
    ./src/q.sh            # interactive

Views are pre-registered so a question is one line instead of a script:

    pairs     (prediction, outcome) pairs           data/pairs/*.parquet
    lamp      LAMP stop events                      data/raw/*.parquet
    legs      linked Magoun arrivals                data/magoun.parquet
    snaps     raw archive snapshots (nested)        data/live/rt-*.jsonl.gz
    preds     archive snapshots, unnested to rows   (snaps UNNEST preds)
    vehicles  archive vehicle rows                  (snaps UNNEST vehicles)

Deliberately analysis-only: nothing the live service or the notifier depends on
imports this, so it adds nothing to the deploy footprint.
"""
import pathlib
import sys

import duckdb

ROOT = pathlib.Path(__file__).resolve().parent.parent
VIEWS = {
    "pairs": "read_parquet('{r}/data/pairs/*.parquet')",
    "lamp": "read_parquet('{r}/data/raw/*.parquet')",
    "legs": "read_parquet('{r}/data/magoun.parquet')",
    "snaps": "read_json_auto('{r}/data/live/rt-*.jsonl.gz')",
}


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for name, src in VIEWS.items():
        try:
            con.execute(f"CREATE VIEW {name} AS SELECT * FROM {src.format(r=ROOT)}")
        except Exception:      # noqa: BLE001 - a missing dataset just skips its view
            pass
    # The archive is nested; flatten the two arrays people actually query.
    for name, col in (("preds", "preds"), ("vehicles", "vehicles")):
        try:
            con.execute(f"""CREATE VIEW {name} AS
                SELECT s.t AS t, u.* FROM snaps s, UNNEST(s.{col}) AS x(u)""")
        except Exception:      # noqa: BLE001
            pass
    return con


def main() -> None:
    con = connect()
    if len(sys.argv) > 1:
        con.sql(" ".join(sys.argv[1:])).show(max_rows=60)
        return
    print(__doc__)
    have = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    print("available:", ", ".join(have), "\n")
    while True:
        try:
            line = input("q> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if line in ("exit", "quit", ""):
            return
        try:
            con.sql(line).show(max_rows=60)
        except Exception as e:  # noqa: BLE001
            print("error:", e)


if __name__ == "__main__":
    main()
