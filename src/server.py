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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/api":
            walk = int(parse_qs(u.query).get("walk", ["6"])[0]) * 60
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
                    "following": [{"eta": r["eta"], "source": r["source"]}
                                  for r in rows[1:4]],
                    "upstream": service.upstream_state(snap),
                    "line": line,
                    "stops": [n for n, _, _ in service.GLX_STOPS],
                    "recent": _arrivals.recent[-5:],
                    "headway_median_s": _model.headway,
                }).encode()
                self._send(200, "application/json", body)
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
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8723
    print(f"Magoun inbound ETA service on http://localhost:{port}/?walk=6")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
