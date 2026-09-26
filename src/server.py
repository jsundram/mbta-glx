"""Local HTTP service: JSON ETAs at /api, web UI at /.

Run:  uv run --with polars python src/server.py [port]
Then: http://localhost:8723/?walk=6
"""
import json
import pathlib
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
}

# The archiver appends every 15 s, and the largest ordinary gap measured across a
# 15-hour day was 18 s. Ten minutes is therefore not jitter -- it is asleep, dead
# or throttled, and every minute of it is data that cannot be re-fetched.
CAPTURE_STALE_S = 600

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
                    __import__("datetime").datetime.fromtimestamp(now, service.TZ).date())
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


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8723
    print(f"Magoun inbound ETA service on http://localhost:{port}/?walk=6")
    # Still localhost only. architecture.md 5 called for "a bind beyond 127.0.0.1"
    # because it assumed the rider's devices would reach this directly; they reach
    # it through `tailscale serve`, which terminates TLS on the tailnet and proxies
    # to loopback. So the wider bind buys nothing and costs the LAN an open port.
    #
    #   tailscale serve --bg --https 443 --set-path /skips http://127.0.0.1:8723/skips
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
