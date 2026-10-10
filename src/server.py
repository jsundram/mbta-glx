"""Local HTTP service: JSON ETAs at /api, web UI at /.

Run:  uv run --with polars python src/server.py [port]
Then: http://localhost:8723/?walk=6
"""
import datetime as dt
import gzip
import json
import pathlib
import re
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import replay
import service

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "src" / "ui.html"
CACHE_TTL = 10.0

_model = service.Model()
_berths = service.BerthTracker()
_arrivals = service.ArrivalTracker()
VERSION = str(int(time.time()))     # changes on restart so open pages self-reload
_lock = threading.Lock()
_cache: dict = {"t": 0.0, "snap": None, "berths": {}, "here": {}}


def current_snapshot() -> tuple[dict, dict]:
    """Snapshot plus berth times; polling here is what lets berths be observed."""
    with _lock:
        if time.time() - _cache["t"] > CACHE_TTL or _cache["snap"] is None:
            snap = service.snapshot()
            _cache["snap"] = snap
            _cache["berths"] = _berths.update(snap)
            _cache["here"] = _arrivals.update(snap)
            _cache["t"] = time.time()
        return _cache["snap"], _cache["berths"]


def _nearest_scheduled(t: float, slots: list[float]) -> float | None:
    return min(slots, key=lambda s: abs(s - t)) if slots else None


# Routes the BROWSER is allowed to reach cross-origin, and nothing else.
#
# The static board is the architecture: it computes its own rows from model.json and
# asks the backend only for what a browser physically cannot fetch -- today that is
# the SKIPPED/CANCELED markers, which live in a protobuf feed with no CORS. The
# moment this backend starts serving computed rows to the board, the static property
# is gone and the board has a server dependency again (architecture.md 3).
#
# Cross-origin reachability IS the CORS header, so that is what is policed: a route
# may only send Access-Control-Allow-Origin if it is listed here, with a reason.
# tests/test_regressions.py enforces it. Adding a route here should feel deliberate.
BROWSER_ROUTES = {
    # {"as_of", "trips", "ttl_s"} and nothing more. Named for the one thing it
    # serves, not "extras": a bag invites a second thing in it, and the whole
    # point of this allowlist is that there is never a second thing.
    "/skips": "protobuf-only SKIPPED/CANCELED markers; cdn.mbta.com has no CORS",
    # A second entry, and it earns the same test as the first: a browser cannot
    # know when a file on this Mac was last written, and an un-captured day is
    # gone for good -- the v3 /schedules endpoint only serves ~8 days back. This
    # is liveness, not computed rows, so it does not touch the static property:
    # with the backend unreachable the board says "unknown" and works exactly as
    # before.
    "/capture": ("a browser cannot know when a file on this Mac was last written, "
                 "and an un-captured day cannot be re-fetched"),
    # A third, and it is the same test and the same argument. Scoring today needs
    # today's whole prediction stream and today's arrivals, which are in data/live
    # on this Mac; the board has been open for ten minutes and cannot know what the
    # 07:14 said. Aggregates only -- counts and a median, never rows -- so the
    # static property holds: the board still computes every ETA it displays, and
    # with this unreachable it hides one panel and is otherwise unchanged.
    "/today": ("a browser cannot score today -- it needs the whole day's prediction "
               "stream and arrivals, which is the archive on this Mac, and the board "
               "has been open ten minutes; aggregates only, never computed rows"),
    # A fourth, for web/figures.html. It was a published file, which meant a 250 KB
    # commit and a push from this Mac every night for a page that only ever drew
    # the archive -- and the archive is on this Mac. src/figures.py still writes it
    # nightly (daily.sh); this hands the file over and computes nothing, so it costs
    # the single-threaded server one read, never a replay.
    "/figures": ("a browser cannot read the archive, and the figures are derived "
                 "from it; a nightly file, served as written, never computed here"),
}

# What src/figures.py writes and /figures hands over. A module constant so a test
# can point it somewhere without moving ROOT under /capture and /today.
FIGURES = ROOT / "data" / "figures.json"

# The archiver appends every 15 s, and the largest ordinary gap measured across a
# 15-hour day was 18 s. Ten minutes is therefore not jitter -- it is asleep, dead
# or throttled, and every minute of it is data that cannot be re-fetched.
CAPTURE_STALE_S = 600

# --- today's score, the panel the board shows -------------------------------
# Recomputed OFF the request path, deliberately. This server is single-threaded and
# shares it with /skips, and a full day costs ~0.6 s (measured 2026-09-26: 112
# arrivals out of 150,817 prediction rows, and it grows through the day because the
# gzip has to be read from the start every time -- nothing about this stream is
# seekable or incremental). Serving that synchronously would stall the skip fetch
# the board makes in the same tick, behind a 2.5 s timeout. So a request answers
# instantly from the last computed summary and kicks a recompute behind it.
#
# Two things gate that recompute, because a blind timer is the wrong shape here:
#
#  * A floor of 60 s between passes. A train arrives every 8.8 min at the median,
#    so 60 s is already nine times oversampled for "a new train showed up", and the
#    board polls on its own minute on top of that.
#  * The archive must have GROWN since the last pass. The archiver appends every
#    15 s, so this is nearly always true while it is alive -- and exactly false when
#    it is not, which is what stops a dead archiver costing a full-day rescore every
#    minute until midnight.
#
# `as_of` 0 means "nothing computed yet", which the board treats as it treats an
# unreachable backend: it hides the panel rather than showing an empty one.
TODAY_MIN_INTERVAL_S = 60
# "Within two minutes of the time it quoted." Published in the payload so the board
# states the threshold the numbers were actually measured against.
TODAY_CLOSE_S = 120
# Below this a percentage is a rounding artifact wearing a measurement's clothes: at
# 06:05 two trains have run and one of them is 50%. The board hides the panel.
TODAY_MIN_TRAINS = 3
# One entry per walk anyone has asked with. Keyed by walk because the rider's own
# walk is the one the score has to be about -- it lives in their browser, not here,
# and a coverage number computed against somebody else's front door is not about
# them. A cap, not a single slot: with one slot, two devices on different walks
# would each invalidate the other's entry on every request and neither would ever
# see a body, which is a panel that silently never appears.
TODAY_MAX_KEYS = 4

_today_lock = threading.Lock()
_today: dict[tuple[str, int], dict] = {}
_today_running: set[tuple[str, int]] = set()


# The ladder the board's "last N trains" slider steps along. Aggregates, not rows:
# each rung is the same summary over the tail of the day, so the slider needs no
# further request and /today still never sends a train.
TAIL_STEPS = (5, 10, 20, 40, 80)


def _counts(told: list[dict], close_s: int) -> dict:
    """The tallies, over whichever trains are handed in.

    The three buckets partition: `err < -close`, `|err| <= close`, `err > close`,
    summing to `trains`. The median wait is over the trains that were CAUGHT; a
    missed train has a negative wait, and mixing those in makes a bad morning
    produce a small reassuring median.

    `missed_close` exists because the headline and the bars measure different
    failures and the gap between them reads as a contradiction. The bars are about
    the QUOTE -- was the time on screen right. `caught` is about the WALK -- would
    you have been standing there. A train quoted from the timetable puts you on the
    platform 22 s before the scheduled minute (the fitted q10), so one that turns
    up half a minute early is missed while still sitting inside the +/-2 min "close"
    bar. Measured on 2026-09-26: 47 missed, 26 of them inside that bar. Without
    this number the panel says "caught 57%" beside "18% more than 2 min early" and
    leaves the reader to reconcile them.
    """
    err = [r["arrival"] - r["predicted"] for r in told]
    waits = [r["wait_s"] for r in told if r["caught"]]
    missed = [r for r in told if not r["caught"]]
    return {
        "trains": len(told),
        "early": sum(1 for e in err if e < -close_s),
        "close": sum(1 for e in err if abs(e) <= close_s),
        "late": sum(1 for e in err if e > close_s),
        "caught": sum(1 for r in told if r["caught"]),
        "missed_close": sum(1 for r in missed
                            if abs(r["arrival"] - r["predicted"]) <= close_s),
        "median_wait_s": round(statistics.median(waits)) if waits else None,
    }


def today_summary(rows: list[dict], day: str, walk: int, as_of: float,
                  close_s: int = TODAY_CLOSE_S,
                  gaps: tuple[int, int] = (0, 0)) -> dict:
    """What the board says about today, from replay.score's rows. Pure.

    Scored at the moment the rider ACTS, which is the whole point: `predicted` is
    the ETA that was on screen when leave-now fired, not MBTA's last word thirty
    seconds before the train, which is always accurate and never useful. So "early"
    means the train beat the time you were quoted -- the failure that leaves you
    watching it go -- and "late" means you stood there longer than you were told.
    """
    told = [r for r in rows if r["told"] is not None]
    return {
        "day": day, "as_of": as_of, "ttl_s": TODAY_MIN_INTERVAL_S, "walk_s": walk,
        "close_s": close_s, "min_trains": TODAY_MIN_TRAINS,
        # Not a footnote: on a day the power went out these numbers are about the
        # part of it that was recorded, and the board says so rather than implying
        # it watched the whole day.
        "gap_s": gaps[0], "max_gap_s": gaps[1],
        **_counts(told, close_s),
        # Oldest rung first, and only rungs with fewer trains than the day has --
        # a "last 40" that is the whole day is a slider position that does nothing.
        "tail": [{"n": n, **_counts(told[-n:], close_s)}
                 for n in TAIL_STEPS if n < len(told)],
    }


# A hole in today's capture is not a quiet matter for this panel. Arrivals are
# counted as transitions into STOPPED_AT (invariant 2), and a dwell at Magoun is
# 20-30 s, so a minute of missing snapshots loses whole arrivals -- and each lost
# one also mispairs the predictions that were aimed at it. The score then comes out
# worse than the day really was, with nothing to say so. The archiver appends every
# 15 s and the largest ordinary gap measured across a 15-hour day was 18 s, so 60 s
# is comfortably "this is not jitter".
GAP_FLOOR_S = 60
_T = re.compile(rb'^\{"t":\s*([0-9.]+)')


def _capture_gaps(day: str) -> tuple[int, int]:
    """(seconds unrecorded, longest single hole) in today's archive so far.

    One extra pass over the gzip, reading only the leading "t" of each line rather
    than parsing 25 MB of JSON: the decompression is the cost and it is ~0.2 s. Runs
    in the same background thread as the score, once a minute at most.

    The slow path is not optional. If a line does not start the way the archiver
    writes them today, this falls back to parsing it -- because the failure mode of
    a regex that silently matches nothing is "no gaps at all", which is the
    reassuring answer, and it would arrive on the day the writer changed.
    """
    path = ROOT / "data" / "live" / f"rt-{day}.jsonl.gz"
    total = longest = 0.0
    prev = None
    try:
        with gzip.open(path, "rb") as fh:
            for line in fh:
                m = _T.match(line)
                if m:
                    t = float(m.group(1))
                else:
                    try:
                        t = float(json.loads(line)["t"])
                    except Exception:  # noqa: BLE001 - a blank or truncated tail line
                        continue
                if prev is not None and t - prev > GAP_FLOOR_S:
                    total += t - prev
                    longest = max(longest, t - prev)
                prev = t
    except OSError:
        return 0, 0
    return round(total), round(longest)


def _archive_stamp(day: str) -> tuple[float, int] | None:
    """(mtime, size) of today's archive, or None before its first append."""
    try:
        st = (ROOT / "data" / "live" / f"rt-{day}.jsonl.gz").stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _score_today(day: str, walk: int) -> dict | None:
    """Today so far, or None if it cannot be scored honestly."""
    if _archive_stamp(day) is None:
        # Before the archiver's first append of the day. Not an error: an empty
        # summary, which reads as "no trains yet" rather than as a dead backend.
        return today_summary([], day, walk, time.time())
    path = ROOT / "data" / "live" / f"rt-{day}.jsonl.gz"
    try:
        # day= is not optional: without it replay reads the three most recent
        # schedule snapshots and the timetable tier silently vanishes, which is
        # invariant 10 and costs ~12% of the coverage number.
        rows = replay.score(walk=walk, n=10**9, paths=[path], day=day)
    except replay.NoScheduleSnapshot as e:
        # Loud, and no answer at all. A day with no captured timetable scores
        # without the tier that carries the horizon past ~13 min, and publishing
        # that as "how trains have run today" would be a quietly wrong number.
        print(f"/today: {e}", file=sys.stderr, flush=True)
        return None
    except Exception as e:  # noqa: BLE001 - one bad day must not kill the server
        print(f"/today failed for {day}: {e!r}", file=sys.stderr, flush=True)
        return None
    return today_summary(rows, day, walk, time.time(),
                         gaps=_capture_gaps(day))


def _refresh_today(key: tuple[str, int]) -> None:
    day, walk = key
    # Stamped BEFORE the scan, not after: the archiver appends while this runs, and
    # recording the later stamp would mark those snapshots as already scored.
    stamp = _archive_stamp(day)
    try:
        summary = _score_today(day, walk)
    finally:
        with _today_lock:
            _today_running.discard(key)
    if summary is None:
        return
    with _today_lock:
        _today[key] = {"body": json.dumps(summary).encode(),
                       "at": time.time(), "stamp": stamp}
        while len(_today) > TODAY_MAX_KEYS:
            _today.pop(min(_today, key=lambda k: _today[k]["at"]))


def today_body(day: str, walk: int) -> bytes:
    """The last computed summary, plus a recompute if one is due. Never waits."""
    key = (day, walk)
    with _today_lock:
        ent = _today.get(key)
        due = ent is None or (
            time.time() - ent["at"] >= TODAY_MIN_INTERVAL_S
            and _archive_stamp(day) != ent["stamp"])
        if due and key not in _today_running:
            _today_running.add(key)
            threading.Thread(target=_refresh_today, args=(key,),
                             daemon=True).start()
        if ent:
            return ent["body"]
    return json.dumps(today_summary([], day, walk, 0.0)).encode()


def warm_today() -> None:
    """Score today before the first board asks, so a restart costs no empty panel."""
    key = (dt.datetime.now(service.TZ).date().isoformat(), service.DEFAULT_WALK)
    with _today_lock:
        if key in _today_running:
            return
        _today_running.add(key)
    threading.Thread(target=_refresh_today, args=(key,), daemon=True).start()


# The board's whole origin, mounted at the root of its own port rather than as
# paths on a shared one. Everything the browser may reach is here and nothing else
# is, so BROWSER_ROUTES is enforced in one place that a test can reach -- not by the
# absence of a line in a proxy config on another machine's terms.
PUBLIC_PORT = 8724

# How long the board may trust a skip set. Two of service.skipped_trips' own 30 s
# cache cycles: long enough that an ordinary miss does not blank the strikethrough,
# short enough that a dead upstream stops being quoted as fact.
SKIP_TTL_S = 60


def _walk(query: str) -> int:
    """The walk in seconds: ?walk=<minutes> if given, else the configured default.

    This used to default to 6 minutes -- a third number, disagreeing with the walk in
    data/config.json that the board and the notifier both read. The number itself is
    deliberately not repeated here; prose goes stale too.
    """
    q = parse_qs(query).get("walk")
    return int(float(q[0]) * 60) if q else service.DEFAULT_WALK


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/api":
            walk = _walk(u.query)
            try:
                snap, berths = current_snapshot()
                rows = service.etas(snap, _model, walk, berths=berths)
                body = json.dumps({
                    "now": snap["t"], "walk": walk,
                    "upstream": service.upstream_state(snap),
                    "berthed": len(berths),
                    "headway_median_s": _model.headway,
                    "trains": rows[:8],
                }).encode()
                self._send(200, "application/json", body)
            except Exception as e:  # noqa: BLE001
                self._send(503, "application/json",
                           json.dumps({"error": str(e)}).encode())
        elif u.path == "/status":
            try:
                snap, berths = current_snapshot()
                rows = service.etas(snap, _model, 0, berths=berths)
                now = snap["t"]
                slots = service.schedule_today(
                    dt.datetime.fromtimestamp(now, service.TZ).date())
                line = service.line_map(snap)
                # A train is only "at the station" if the same snapshot also places
                # it stopped at Magoun. Otherwise the hero and the map can disagree,
                # which is worse than either being briefly wrong on its own.
                at_magoun = {t["id"] for t in line
                             if t["dir"] == 0 and t["stopped"] and t["pos"] == 2.0}
                here = []
                for vid, since in _cache.get("here", {}).items():
                    if vid not in at_magoun:
                        continue
                    sched = _nearest_scheduled(since, slots)
                    here.append({"vehicle": vid, "since": since,
                                 "dwell_s": now - since,
                                 "late_s": (since - sched) if sched else None})
                nxt = next((r for r in rows if r["eta"] > now), None)
                body = json.dumps({
                    "now": now, "version": VERSION,
                    "at_station": here,
                    "next": {"eta": nxt["eta"], "lo": nxt["lo"], "hi": nxt["hi"],
                             "source": nxt["source"], "backed": nxt["backed"]}
                    if nxt else None,
                    "following": [{"eta": r["eta"], "source": r["source"],
                                   "skipped": r.get("skipped", False)}
                                  for r in rows[1:5]],
                    "upstream": service.upstream_state(snap),
                    "line": line,
                    "stops": [n for n, _, _ in service.GLX_STOPS],
                    "recent": _arrivals.recent[-5:],
                    "alerts": [a for a in service.relevant()
                               if (a["severity"] or 0) >= 5][:3],
                    "headway_median_s": _model.headway,
                }).encode()
                self._send(200, "application/json", body)
            except Exception as e:  # noqa: BLE001
                self._send(503, "application/json",
                           json.dumps({"error": str(e)}).encode())
        elif u.path == "/history":
            try:
                walk = _walk(u.query)
                rows = replay.score(walk=walk, n=10)
                self._send(200, "application/json",
                           json.dumps({"rows": rows, "walk": walk}).encode())
            except Exception as e:  # noqa: BLE001
                self._send(503, "application/json",
                           json.dumps({"error": str(e)}).encode())
        elif u.path == "/skips":
            # The one thing a browser physically cannot get: cdn.mbta.com serves
            # the protobuf with no CORS. Parsed here rather than relayed -- the
            # feed is ~1 MB and the answer is ~10 trip ids for one stop.
            #
            # 200 with a stale set and an honest `as_of`, never 503: the board can
            # tell "no skips" from "cannot see skips" only if it is given the
            # timestamp, and a 503 collapses both into an empty set.
            try:
                trips, as_of = service.skip_set()
                self._send(200, "application/json", json.dumps({
                    "as_of": as_of, "trips": sorted(trips),
                    "ttl_s": SKIP_TTL_S}).encode())
            except Exception as e:  # noqa: BLE001
                self._send(503, "application/json",
                           json.dumps({"error": str(e)}).encode())
        elif u.path == "/today":
            # Invariant 8: the agency's day, never the host's. On a UTC-clocked host
            # the date rolls over at 20:00 ET, which would score the evening commute
            # against tomorrow's empty archive.
            day = dt.datetime.now(service.TZ).date().isoformat()
            self._send(200, "application/json", today_body(day, _walk(u.query)))
        elif u.path == "/capture":
            # The newest archive file's mtime, not today's by name: the archiver
            # opens, appends and closes once per snapshot, so mtime is the
            # heartbeat -- and taking the newest avoids reading 0 for the few
            # seconds after midnight before the new day's file exists.
            try:
                live = ROOT / "data" / "live"
                as_of = max((f.stat().st_mtime
                             for f in live.glob("rt-*.jsonl.gz")), default=0.0)
                self._send(200, "application/json", json.dumps({
                    "as_of": as_of,
                    "stale_after_s": CAPTURE_STALE_S}).encode())
            except Exception as e:  # noqa: BLE001
                self._send(503, "application/json",
                           json.dumps({"error": str(e)}).encode())
        elif u.path == "/figures":
            # 503 when the file is missing, not an empty 200: a page with nothing to
            # draw has to be able to say "no figures yet" rather than draw nothing.
            try:
                self._send(200, "application/json", FIGURES.read_bytes())
            except OSError as e:
                self._send(503, "application/json",
                           json.dumps({"error": f"no figures: {e}"}).encode())
        elif u.path == "/board":
            self._send(200, "text/html; charset=utf-8",
                       (ROOT / "src" / "status.html").read_bytes())
        elif u.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.read_bytes())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The ONE place this header is emitted, and only for an allowlisted route.
        # An allowlist nothing consults is decoration: until this existed, adding
        # Access-Control-Allow-Origin to /api made the board's rows fetchable
        # cross-origin with every test still green.
        if urlparse(self.path).path in BROWSER_ROUTES:
            # `*`, not the Pages origin: the board is also opened from file://,
            # which sends a null Origin.
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


class PublicHandler(Handler):
    """The same routes, on a second port that serves ONLY the allowlisted ones.

    The allowlist was enforced twice: here, as the gate on the CORS header, and by
    `tailscale serve` having one `--set-path` line per route and no others. That
    second half was load-bearing and invisible. /api, /status, /board and /history
    are unreachable from the tailnet only because nobody mounted them -- and every
    new route needed a proxy change, in a place where a missing line does not look
    like a missing line: an unmapped path falls through to whatever owns `/` on that
    hostname, which on this host is a different application, so it 404s from
    somewhere else and reads exactly like a backend that is down.

    So the split moves into the process, where a test can see it. This port is the
    board's whole origin and gets mounted at the root of its own; the private port
    keeps the computed-rows routes that would end the static property if a browser
    could reach them. Adding a route to BROWSER_ROUTES now changes what is public,
    with no second place to remember and nothing to re-run on the proxy.
    """

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path not in BROWSER_ROUTES:
            # Deliberately indistinguishable from an unknown route: this origin's
            # story is that it has the allowlist on it and nothing else exists.
            return self._send(404, "text/plain", b"not found")
        super().do_GET()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8723
    public = int(sys.argv[2]) if len(sys.argv) > 2 else PUBLIC_PORT
    print(f"Magoun inbound ETA service on http://localhost:{port}/?walk=6")
    # Both localhost only. architecture.md 5 called for "a bind beyond 127.0.0.1"
    # because it assumed the rider's devices would reach this directly; they reach
    # it through `tailscale serve`, which terminates TLS on the tailnet and proxies
    # to loopback. So the wider bind buys nothing and costs the LAN an open port.
    #
    # One mount, once, and never again as routes are added:
    #
    #   tailscale serve --bg --https 8443 http://127.0.0.1:8724
    #
    # The public port is a whole origin, so it is mounted at a root rather than a
    # path. Paths on one hostname are how several apps end up sharing a namespace
    # and answering for each other; a port each keeps them apart.
    print(f"  public routes on http://localhost:{public} "
          f"({', '.join(sorted(BROWSER_ROUTES))})")
    warm_today()
    threading.Thread(
        target=HTTPServer(("127.0.0.1", public), PublicHandler).serve_forever,
        daemon=True).start()
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
