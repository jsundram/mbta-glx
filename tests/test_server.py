"""The backend's HTTP surface, driven over a real socket.

M4's route is the first thing the board actually fetches from this process, so the
property that keeps the board static -- "serve only what a browser cannot fetch" --
stops being an intention here and becomes traffic. Until now it was checked two
ways, both indirect: the structure of `_send` (one emitting line, guarded by
BROWSER_ROUTES) and a human running curl once. The structural test exists because
an earlier version asserted only that the string BROWSER_ROUTES appeared in the
file, which the definition itself satisfied.

This boots the real handler on a real port and reads the headers off the wire.
Nothing here reaches the network: the two things that would (the skip set and the
snapshot) are stubbed, and a route that fails still goes through `_send`, which is
the code under test.
"""
import http.client
import json
import os
import pathlib
import sys
import subprocess
import threading
from http.server import HTTPServer

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("MAGOUN_NTFY_TOPIC", "test")
os.environ.setdefault("MAGOUN_NTFY_CMD", "test-cmd")

import server  # noqa: E402
import service  # noqa: E402

TRIPS = {"77745376", "77745399"}
AS_OF = 1790281459.0


@pytest.fixture
def live(monkeypatch):
    """The handler on a real port, with every network call stubbed out."""
    monkeypatch.setattr(service, "skip_set", lambda *a, **k: (set(TRIPS), AS_OF))
    monkeypatch.setattr(server, "current_snapshot",
                        lambda: (_ for _ in ()).throw(RuntimeError("no network")))
    # HTTPServer, not ThreadingHTTPServer: production is single-threaded, and a
    # threaded fixture would hide exactly the serialisation this has to preserve.
    httpd = HTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()          # or every fixture use leaks a listening socket


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    out = (r.status, dict(r.getheaders()), body)
    c.close()
    return out


# --- the route the board cannot do without a backend ---

def test_skips_serves_the_set_and_when_it_was_derived(live):
    status, headers, body = get(live, "/skips")
    assert status == 200
    d = json.loads(body)
    assert set(d) == {"as_of", "trips", "ttl_s"}, \
        f"the skip route grew a field: {sorted(d)}"
    assert sorted(d["trips"]) == sorted(TRIPS)
    assert d["as_of"] == AS_OF
    assert d["ttl_s"] == server.SKIP_TTL_S


def test_skips_is_reachable_cross_origin(live):
    """The board is opened from file:// too, where Origin is null -- so `*`, not
    an allowlist of the Pages origin."""
    _, headers, _ = get(live, "/skips")
    assert headers.get("Access-Control-Allow-Origin") == "*"


def test_the_computed_rows_route_is_not_reachable_cross_origin(live):
    """The load-bearing half. The moment /api answers a browser, the board has a
    server dependency again and the static property is gone.

    It answers 503 here because the snapshot is stubbed to fail -- which is the
    point: an error still goes through _send, so the header decision is still
    being made, and it must still be no.
    """
    status, headers, _ = get(live, "/api")
    assert status == 503
    assert "Access-Control-Allow-Origin" not in headers


def test_an_unknown_route_is_not_reachable_cross_origin(live):
    status, headers, _ = get(live, "/nope")
    assert status == 404
    assert "Access-Control-Allow-Origin" not in headers


def test_every_route_that_sends_cors_is_on_the_allowlist(live):
    """Walk the real surface rather than trusting the one grep above it."""
    for path in ("/skips", "/api", "/status", "/history", "/board", "/", "/nope"):
        _, headers, _ = get(live, path)
        sent = "Access-Control-Allow-Origin" in headers
        assert sent == (path in server.BROWSER_ROUTES), \
            f"{path}: CORS sent={sent}, allowlisted={path in server.BROWSER_ROUTES}"


# --- the timestamp is the whole reason the board can trust the set ---

@pytest.fixture
def upstream_down(monkeypatch):
    """Make the protobuf fetch fail, and make sure the fetch is what fails.

    `skipped_trips` imports google.transit INSIDE its try, before urlopen. That
    package is not in the suite's environment, so patching urlopen alone left
    these tests passing through the ImportError arm -- the right answer for the
    wrong reason, and no coverage of the mechanism they name. Stubbing the module
    puts the failure back where the docstring says it is.
    """
    import sys
    import types
    pkg = types.ModuleType("google")
    transit = types.ModuleType("google.transit")
    pb = types.ModuleType("google.transit.gtfs_realtime_pb2")
    pb.FeedMessage = lambda: (_ for _ in ()).throw(
        AssertionError("parsed something; the fetch should have failed first"))
    transit.gtfs_realtime_pb2 = pb
    pkg.transit = transit
    monkeypatch.setitem(sys.modules, "google", pkg)
    monkeypatch.setitem(sys.modules, "google.transit", transit)
    monkeypatch.setitem(sys.modules, "google.transit.gtfs_realtime_pb2", pb)
    calls = []

    def boom(*a, **k):
        calls.append(a[0] if a else None)
        raise OSError("cdn.mbta.com is down")

    monkeypatch.setattr(service.urllib.request, "urlopen", boom)
    return calls


def test_a_failed_upstream_fetch_does_not_advance_as_of(monkeypatch, upstream_down):
    """skipped_trips hands back its previous set when cdn.mbta.com fails, so the
    server can be healthy and the answer stale. Only as_of separates those."""
    monkeypatch.setattr(service, "_SKIP_CACHE",
                        {"t": 0.0, "trips": set(), "fail_t": 0.0})
    trips, as_of = service.skip_set()
    assert upstream_down, "the test never reached the fetch it claims to break"
    assert trips == set()
    assert as_of == 0.0, "never successfully fetched must not look fresh"


def test_as_of_is_the_last_success_not_the_last_request(monkeypatch, upstream_down):
    monkeypatch.setattr(service, "_SKIP_CACHE",
                        {"t": 1790000000.0, "trips": {"x"}, "fail_t": 0.0})
    # ttl=0 forces a re-fetch attempt, which fails; the old set and time stand.
    trips, as_of = service.skip_set(ttl=0)
    assert upstream_down
    assert trips == {"x"} and as_of == 1790000000.0


def test_a_dead_upstream_is_asked_once_per_backoff_not_once_per_request(
        monkeypatch, upstream_down):
    """The ttl shortcut is keyed on the SUCCESS time, which stops moving exactly
    when fetches start failing -- so without a separate failure stamp every
    request pays a fresh 20 s timeout. server.py is single-threaded and the board
    polls every 10 s, so that stalls /board and /status too, not just /skips."""
    monkeypatch.setattr(service, "_SKIP_CACHE",
                        {"t": 0.0, "trips": set(), "fail_t": 0.0})
    for _ in range(10):
        service.skip_set(ttl=0)
    assert len(upstream_down) == 1, (
        f"{len(upstream_down)} upstream fetches for 10 requests; a dead feed must "
        "be asked once per backoff window")


def test_the_backoff_lets_go_once_it_expires(monkeypatch, upstream_down):
    import time as _t
    monkeypatch.setattr(service, "_SKIP_CACHE",
                        {"t": 0.0, "trips": set(),
                         "fail_t": _t.time() - service.SKIP_FAIL_BACKOFF - 1})
    service.skip_set(ttl=0)
    assert len(upstream_down) == 1, "an expired backoff must try again"


# --- the ops scripts, which drifted apart once already ---

def test_install_and_uninstall_agree_on_the_agent_list():
    """com.magoun.server was added to install.sh and not to uninstall.sh, so
    `uninstall` left the backend running with its plist in place to come back."""
    ops = pathlib.Path(__file__).resolve().parent.parent / "ops"
    plists = sorted(p.stem for p in ops.glob("com.magoun.*.plist"))
    assert plists, "no agents found; this test would pass vacuously"
    for script in ("install.sh", "uninstall.sh"):
        out = subprocess.run([str(ops / script), "--list"], capture_output=True,
                             text=True, timeout=30)
        got = sorted(out.stdout.split())
        assert got == plists, f"{script} acts on {got}, ops/ has {plists}"
