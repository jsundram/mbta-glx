"""Regression tests for bugs found in live running.

Every test here corresponds to a bug that actually shipped and cost real debugging
time. They run in under a second, which is the point: most of these were found by
waiting for trains, and none of them needed to be.
"""
import json
import re
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import os
os.environ.setdefault("MAGOUN_NTFY_TOPIC", "test")
os.environ.setdefault("MAGOUN_NTFY_CMD", "test-cmd")

import watch  # noqa: E402

HEADWAY = 528.0   # 8.8 min, the measured median


def row(eta, vehicle=None, source="schedule"):
    return {"eta": eta, "lo": eta - 159, "hi": eta + 159,
            "source": source, "vehicle": vehicle, "backed": True}


# --- bug: WIDE window swallowed the next train, sliding the plan forever ---

def test_match_never_jumps_to_the_next_train():
    """A flapping prediction must not re-point the commitment one headway later."""
    committed = {"target_eta": 1000.0}
    rows = [row(1000 + HEADWAY)]          # only the NEXT train is present
    assert watch.match_target(rows, committed, HEADWAY) is None


def test_match_follows_genuine_drift():
    committed = {"target_eta": 1000.0}
    got = watch.match_target([row(1000 + 200)], committed, HEADWAY)
    assert got is not None and got["eta"] == 1200


def test_match_prefers_vehicle_identity_over_proximity():
    """A known vehicle id is proof, even when another row sits closer in time."""
    committed = {"target_eta": 1000.0, "vehicle": "G-1"}
    rows = [row(1010, vehicle="G-2"), row(1400, vehicle="G-1")]
    got = watch.match_target(rows, committed, HEADWAY)
    assert got["vehicle"] == "G-1"


def test_match_returns_none_when_nothing_is_close():
    assert watch.match_target([row(9999)], {"target_eta": 1000.0}, HEADWAY) is None


# --- bug: arrivals keyed by (vehicle, stop) counted only the first visit ---

def _snap(t, vid, stop, status):
    return {"t": t, "vehicles": [{"id": vid, "stop": stop,
                                  "status": status, "dir": 0}]}


def test_arrivals_count_every_visit_not_just_the_first():
    """One train round-tripping through Magoun three times is three arrivals."""
    snaps = []
    t = 0.0
    for _ in range(3):
        snaps.append(_snap(t, "G-1", "70508", "STOPPED_AT")); t += 15
        snaps.append(_snap(t, "G-1", "70508", "STOPPED_AT")); t += 15   # still there
        snaps.append(_snap(t, "G-1", "70506", "IN_TRANSIT_TO")); t += 15  # left
    assert len(watch.count_arrivals(snaps, "70508")) == 3


def test_arrivals_ignore_a_train_parked_at_the_stop():
    """A stationary vehicle must register once, not once per snapshot."""
    snaps = [_snap(i * 15.0, "G-9", "70508", "STOPPED_AT") for i in range(40)]
    assert len(watch.count_arrivals(snaps, "70508")) == 1


def test_arrivals_ignore_other_stops_and_directions():
    snaps = [_snap(0.0, "G-1", "70507", "STOPPED_AT")]           # outbound platform
    snaps.append({"t": 15.0, "vehicles": [{"id": "G-2", "stop": "70508",
                                           "status": "STOPPED_AT", "dir": 1}]})
    assert watch.count_arrivals(snaps, "70508") == []


# --- bug: ?since=now returned HTTP 400 and was retried silently forever ---

def test_listen_builds_url_without_since_by_default():
    """ntfy rejects ?since=now with HTTP 400; the default must omit it entirely."""
    import inspect
    import notify
    assert inspect.signature(notify.listen).parameters["since"].default == ""

    seen = []

    def fake_urlopen(url, **kw):
        seen.append(url)
        raise OSError("boom")

    class Stop(Exception):
        pass

    def fake_sleep(_):
        raise Stop                   # abort the reconnect loop after one attempt

    o_open, o_sleep = notify.urllib.request.urlopen, notify.time.sleep
    notify.urllib.request.urlopen, notify.time.sleep = fake_urlopen, fake_sleep
    try:
        next(notify.listen("topic-x"), None)
    except Stop:
        pass
    finally:
        notify.urllib.request.urlopen, notify.time.sleep = o_open, o_sleep

    assert seen, "listen never issued a request"
    assert "since" not in seen[0], f"must not send a since param: {seen[0]}"
    assert seen[0].endswith("/topic-x/json")


def test_reply_action_posts_at_min_priority():
    """Otherwise a phone subscribed to the command topic buzzes on its own taps."""
    import notify
    a = notify.reply_action("On my way", "left")
    assert a["headers"]["Priority"] == "min"
    assert a["method"] == "POST" and a["body"] == "left"


# --- destination names ---

def test_resolve_rejects_unknown_and_accepts_substring():
    import brief
    import service
    m = service.Model()
    assert brief.resolve("park", m) == "70199"
    assert brief.resolve("70199", m) == "70199"
    try:
        brief.resolve("nowhere", m)
    except SystemExit as e:
        assert "Known:" in str(e)
    else:
        raise AssertionError("unknown destination should raise")


# --- bug: 80 s debounce fired inside a 90 s prediction flap ---

def test_debounce_outlasts_observed_prediction_flap():
    """Two live flaps lasted 90 s each; the debounce must ride them out."""
    observed_flap_s = 90
    assert watch.MISS_TICKS * watch.TICK > observed_flap_s * 1.5, (
        f"debounce {watch.MISS_TICKS * watch.TICK}s is too short for a "
        f"{observed_flap_s}s flap")


def test_vehicle_identity_survives_a_headway_sized_jump():
    """A latched vehicle must be followed however far its prediction flaps."""
    committed = {"target_eta": 1000.0, "vehicle": "G-10065"}
    rows = [row(1000 + 480, vehicle="G-10065")]     # +8 min, as observed live
    got = watch.match_target(rows, committed, HEADWAY)
    assert got is not None and got["vehicle"] == "G-10065"


# --- bug: dwell counter reset to 0 and jumped, because one missed poll
#     was treated as the train departing ---

def _veh(vid, stop, status, updated_iso, direction=0):
    return {"id": vid,
            "attributes": {"direction_id": direction, "current_status": status,
                           "updated_at": updated_iso},
            "relationships": {"stop": {"data": {"id": stop}},
                              "route": {"data": {"id": "Green-E"}}}}


def _isnap(t, vehicles):
    return {"t": t, "vehicles": vehicles}


def test_dwell_survives_a_missed_poll():
    """A vehicle absent from one snapshot must not restart the dwell counter."""
    import datetime as dt
    import service
    tr = service.ArrivalTracker()
    t0 = dt.datetime.now(dt.timezone.utc).timestamp()
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()

    here = tr.update(_isnap(t0, [_veh("G-1", "70508", "STOPPED_AT", iso(t0))]))
    assert here["G-1"] == t0
    tr.update(_isnap(t0 + 15, []))                       # dropped out of one poll
    here = tr.update(_isnap(t0 + 30,
                            [_veh("G-1", "70508", "STOPPED_AT", iso(t0 + 30))]))
    assert here["G-1"] == t0, "dwell restarted after a single missed poll"


def test_dwell_clears_once_the_train_actually_moves():
    import datetime as dt
    import service
    tr = service.ArrivalTracker()
    t0 = dt.datetime.now(dt.timezone.utc).timestamp()
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
    tr.update(_isnap(t0, [_veh("G-1", "70508", "STOPPED_AT", iso(t0))]))
    here = tr.update(_isnap(t0 + 15,
                            [_veh("G-1", "70506", "IN_TRANSIT_TO", iso(t0 + 15))]))
    assert here == {}


def test_dwell_order_is_stable_so_the_display_never_swaps():
    import datetime as dt
    import service
    tr = service.ArrivalTracker()
    t0 = dt.datetime.now(dt.timezone.utc).timestamp()
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
    tr.update(_isnap(t0, [_veh("G-2", "70508", "STOPPED_AT", iso(t0))]))
    here = tr.update(_isnap(t0 + 15, [
        _veh("G-2", "70508", "STOPPED_AT", iso(t0 + 15)),
        _veh("G-1", "70508", "STOPPED_AT", iso(t0 + 15))]))
    assert list(here) == ["G-2", "G-1"], "oldest arrival must sort first"


def test_line_map_places_moving_trains_between_stops():
    import service
    snap = _isnap(0.0, [
        _veh("G-in", "70508", "IN_TRANSIT_TO", None, direction=0),
        _veh("G-out", "70507", "IN_TRANSIT_TO", None, direction=1),
        _veh("G-at", "70512", "STOPPED_AT", None, direction=0)])
    pos = {t["id"]: t["pos"] for t in service.line_map(snap)}
    assert pos["G-at"] == 0.0                 # berthed at the terminus
    assert pos["G-in"] == 1.5                 # inbound runs DOWN toward Magoun (2)
    assert pos["G-out"] == 2.5                # outbound runs UP toward Tufts


# --- skipped / cancelled trains are stated, not inferred ---

def test_skipped_scope_is_this_stop_only():
    """A system-wide CANCELED union pulled in 67 trips against ~10 real skips."""
    import inspect
    import service
    src = inspect.getsource(service.skipped_trips)
    assert "su.stop_id == stop" in src
    # the CANCELED branch must also be scoped to this stop
    canceled = src.split("schedule_relationship == 3")[1]
    assert "su.stop_id == stop" in canceled, "cancellations must be scoped to the stop"


def test_brief_never_offers_a_skipped_train():
    import inspect
    import brief
    src = inspect.getsource(brief.options)
    assert 'not r.get("skipped")' in src


# --- archive compaction must be lossless ---

def test_delta_round_trip_is_exact():
    """Compaction is only safe if it is reversible; verify before replacing."""
    import rollup
    snaps = []
    for i in range(300):
        snaps.append({
            "t": 1000.0 + i * 15,
            "preds": [{"stop": "70508", "trip": "t1", "arr": 2000 + i,
                       "dep": 2040 + i, "rel": 0, "unc": 120, "route": "Green-E",
                       "dir": 0, "veh": "G-1", "seq": 6},
                      {"stop": "70510", "trip": "t2", "arr": 3000, "dep": None,
                       "rel": None, "unc": None, "route": "Green-E",
                       "dir": 0, "veh": "G-2", "seq": 5}],
            "vehicles": [{"id": "G-1", "stop": "70510", "status": "STOPPED_AT",
                          "seq": 5, "ts": 900 + i, "dir": 0, "route": "Green-E",
                          "trip": "t1"}],
        })
    back = list(rollup.from_delta(rollup.to_delta(snaps)))
    assert len(back) == len(snaps)
    for a, b in zip(snaps, back):
        assert a["t"] == b["t"]
        ka = {rollup._pkey(p): p["arr"] for p in a["preds"]}
        kb = {rollup._pkey(p): p["arr"] for p in b["preds"]}
        assert ka == kb
        assert {v["id"]: v["ts"] for v in a["vehicles"]} == \
               {v["id"]: v["ts"] for v in b["vehicles"]}


def test_compact_keeps_a_keyframe_every_hour():
    """A corrupt or truncated run must lose less than an hour, not the whole day."""
    import rollup
    assert rollup.KEYFRAME * 15 <= 3600


# --- delta-parquet archive: a null cannot mean "unchanged" ---

def test_archive_round_trip_handles_fields_changing_to_null():
    """veh is null until a vehicle is assigned, then set, and can clear again.

    Using null to mean "unchanged" corrupted 2,088 of 5,738 snapshots before the
    changed-field mask existed.
    """
    import archive
    snaps = []
    for i, veh in enumerate([None, None, "G-1", "G-1", None, "G-2"]):
        snaps.append({
            "t": 1000.0 + i * 15,
            "preds": [{"stop": "70508", "trip": "t1", "route": "Green-E", "dir": 0,
                       "veh": veh, "seq": 6, "arr": 2000 + i, "dep": None,
                       "unc": None, "rel": 0}],
            "vehicles": [{"id": "G-1", "route": "Green-E", "trip": "t1", "dir": 0,
                          "stop": "70510", "status": "STOPPED_AT", "seq": 5,
                          "ts": 900 + i}],
        })
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        archive.write(snaps, pathlib.Path(d) / "day=x")
        back = list(archive.read(pathlib.Path(d) / "day=x"))
    assert [p["preds"][0]["veh"] for p in back] == [s["preds"][0]["veh"] for s in snaps]


def test_archive_drops_entities_that_leave_the_feed():
    import archive
    import tempfile, pathlib
    base = {"stop": "70508", "trip": "t1", "route": "Green-E", "dir": 0,
            "veh": None, "seq": 6, "arr": 1, "dep": None, "unc": None, "rel": 0}
    veh = {"id": "G-1", "route": "Green-E", "trip": "t1", "dir": 0,
           "stop": "70510", "status": "STOPPED_AT", "seq": 5, "ts": 1}
    snaps = [{"t": 1.0, "preds": [base], "vehicles": [veh]},
             {"t": 16.0, "preds": [], "vehicles": []},
             {"t": 31.0, "preds": [base], "vehicles": [veh]}]
    with tempfile.TemporaryDirectory() as d:
        archive.write(snaps, pathlib.Path(d) / "day=x")
        back = list(archive.read(pathlib.Path(d) / "day=x"))
    assert [len(s["preds"]) for s in back] == [1, 0, 1]
    assert [len(s["vehicles"]) for s in back] == [1, 0, 1]


def test_reading_one_stop_is_the_full_read_with_the_rest_removed():
    """`pred_stop` is what keeps /history under a second, so it must be exact.

    The filter runs on encoded delta rows, before they are rebuilt. That is only
    sound because a key never changes stop -- if the key ever stopped carrying the
    stop, a filtered read would rebuild from partial deltas and quietly differ.
    """
    import archive
    import tempfile, pathlib
    def p(stop, trip, arr, veh):
        return {"stop": stop, "trip": trip, "route": "Green-E", "dir": 0,
                "veh": veh, "seq": 6, "arr": arr, "dep": None, "unc": None, "rel": 0}
    veh = {"id": "G-1", "route": "Green-E", "trip": "t1", "dir": 0,
           "stop": "70510", "status": "STOPPED_AT", "seq": 5, "ts": 1}
    snaps = [{"t": 1.0, "preds": [p("70508", "t1", 100, "G-1"),
                                  p("70510", "t1", 50, "G-1")], "vehicles": [veh]},
             {"t": 16.0, "preds": [p("70508", "t1", 110, None),
                                   p("705081", "t2", 70, "G-2")], "vehicles": []},
             {"t": 31.0, "preds": [p("70508", "t1", 120, "G-1")], "vehicles": [veh]}]
    with tempfile.TemporaryDirectory() as d:
        archive.write(snaps, pathlib.Path(d) / "day=x")
        full = list(archive.read(pathlib.Path(d) / "day=x"))
        one = list(archive.read(pathlib.Path(d) / "day=x", pred_stop="70508"))
    # "705081" shares a prefix with "70508": the separator is what keeps it out.
    assert one == [{**s, "preds": [q for q in s["preds"] if q["stop"] == "70508"]}
                   for s in full]
    assert [q["veh"] for s in one for q in s["preds"]] == ["G-1", None, "G-1"]


# --- the static property: the backend serves only what a browser cannot fetch ---
#
# architecture.md 3: the board computes its own rows from model.json and asks the
# backend for nothing else. That is an intention, not a mechanism, so these three
# checks make it a mechanism. An accident on any side trips one of them.

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT_DIR / "src" / "server.py"
WEB_DIR = ROOT_DIR / "web"
# Hosts the board is allowed to reach, each because it can only come from there.
ALLOWED_HOSTS = {
    "api-v3.mbta.com": "live predictions and vehicles; CORS-enabled, no key",
    "ntfy.sh": "the rider's own alert channel, armed by the page",
    # M4. cdn.mbta.com serves the SKIPPED/CANCELED markers as protobuf with no
    # CORS, so a browser cannot read them at all; this host parses that feed and
    # serves the ~10 trip ids for one stop. Reachable only inside the tailnet, and
    # the board degrades to an empty skip set whenever it is not.
    "mini.tail8b0808.ts.net": ("the three things a browser cannot fetch -- the skip "
                               "set, the archiver's heartbeat, and today's score; "
                               "tailnet only, and the board degrades without it"),
}


# An XML namespace is an identifier, not an address: nothing dereferences
# xmlns="http://www.w3.org/2000/svg", and the inline SVG favicon carries one. Letting
# it into ALLOWED_HOSTS would mean that list no longer answers "what does the board
# talk to", which is the only question it exists to answer.
_XMLNS = re.compile(r"""xmlns(:\w+)?=['"][^'"]*['"]""")


def _board_source() -> str:
    """Everything served from the origin. proof.html was missed and fetches MBTA."""
    return _XMLNS.sub("", "\n".join(p.read_text() for p in
                      sorted(WEB_DIR.glob("*.js")) + sorted(WEB_DIR.glob("*.html"))))


def test_the_board_reaches_no_host_it_has_no_reason_to():
    """A new absolute URL in the board is a new backend dependency.

    tests/test_publish.py checks the SIBLING assets the board fetches; absolute
    URLs were unchecked by anything, which is exactly where a server dependency
    would appear.
    """
    hosts = set(re.findall(r"https?://([a-z0-9.-]+)", _board_source()))
    extra = hosts - set(ALLOWED_HOSTS)
    assert not extra, (
        f"the board reaches {sorted(extra)}, which is not in ALLOWED_HOSTS. If this "
        "is a backend serving something the browser genuinely cannot fetch, add it "
        "with the reason; if it serves computed rows, the board is no longer static.")


def test_the_cors_header_is_sent_from_one_place_gated_by_the_allowlist():
    """Cross-origin reachability IS the CORS header, so that is what is policed.

    The previous version of this test asserted only that the string BROWSER_ROUTES
    appeared somewhere in the file, which the definition itself satisfied. Adding
    Access-Control-Allow-Origin to /api passed all of it while making the board's
    computed rows fetchable cross-origin -- verified, which is why this is now
    structural: exactly one emitting line, and the allowlist consulted right above it.
    """
    lines = SERVER.read_text().splitlines()
    cors = [i for i, l in enumerate(lines)
            if "Access-Control-Allow-Origin" in l and not l.lstrip().startswith("#")]
    assert len(cors) == 1, (
        f"the CORS header is sent from {len(cors)} place(s); it must come from exactly "
        "one, gated by BROWSER_ROUTES, or the allowlist is decoration")
    guard = "\n".join(lines[max(0, cors[0] - 6):cors[0]])
    assert "BROWSER_ROUTES" in guard, (
        "the CORS header is not guarded by a BROWSER_ROUTES membership check")


def test_the_allowlist_only_names_routes_that_exist_or_are_planned():
    import server
    routes = set(re.findall(r'u\.path == "([^"]+)"', SERVER.read_text()))
    # Nothing is merely planned any more: M4 built /skips, so every allowlisted
    # route must exist. An entry with no route behind it is the allowlist drifting
    # back into decoration.
    extra = set(server.BROWSER_ROUTES) - routes
    assert not extra, f"BROWSER_ROUTES names routes that do not exist: {sorted(extra)}"


def test_no_published_constant_smuggles_in_an_unjustified_host():
    """The board will read the M4 endpoint URL from model.json, not a literal.

    So scanning web/ for `https://` cannot see it -- architecture.md specifies the
    URL as a published constant, exactly like walk_s. Check the constants too.
    """
    c = json.loads((WEB_DIR / "model.json").read_text())["constants"]
    hosts = set(re.findall(r"https?://([a-z0-9.-]+)", json.dumps(c)))
    extra = hosts - set(ALLOWED_HOSTS)
    assert not extra, (
        f"model.json publishes hosts the board would reach: {sorted(extra)}. Add them "
        "to ALLOWED_HOSTS with the reason a browser cannot fetch them otherwise.")


def test_the_browser_route_allowlist_states_a_reason_for_each_entry():
    """An entry with no reason is how the list stops being a decision."""
    import server
    for route, why in server.BROWSER_ROUTES.items():
        assert route.startswith("/"), route
        assert len(why) > 20, f"{route} has no real justification: {why!r}"
        assert "CORS" in why or "protobuf" in why or "cannot" in why, \
            f"{route}'s reason does not say why a browser cannot fetch it: {why!r}"


def test_the_board_computes_its_own_rows():
    """The load-bearing half of 'static': compute_rows is ported, not fetched."""
    js = (WEB_DIR / "app.js").read_text()
    assert "computeRows" in js or "compute_rows" in js, \
        "app.js no longer contains the ported prediction function"
    # Path-anchored: plain "/api" also occurs inside "api-v3.mbta.com". The `}`
    # alternative catches an interpolated base, `${EXTRAS}/api`.
    assert not re.search(r"""(?:["'`]|\})/api\b""", js), \
        "the board is calling the backend's computed-rows route"


def test_rollup_keeps_predictions_that_never_resolved():
    """No-show evidence must survive into the store that is never pruned.

    rollup used to `continue` past any prediction with no matching arrival, so
    data/pairs held no record of a train that was predicted and never came -- the
    evidence the notifier's 240 s debounce would need to stop being hand-tuned.
    data/live has it and is pruned at 90 days, so dropping it here lost it for good.
    """
    src = (ROOT_DIR / "src" / "rollup.py").read_text()
    body = src[src.index("def rollup_day"):]
    assert "if a is None:\n            continue" not in body, \
        "rollup is dropping unpaired predictions again"
    assert '"actual_arr": int(a) if a is not None else None' in body, \
        "unpaired predictions must be kept with a null actual_arr"


def test_the_store_actually_contains_unresolved_predictions():
    """The guard above is about source; this is about what is on disk."""
    import glob
    import polars as pl
    files = sorted(glob.glob(str(ROOT_DIR / "data" / "pairs" / "*.parquet")))
    if not files:
        pytest.skip("no rolled-up days on this machine")
    d = pl.concat([pl.read_parquet(f) for f in files])
    m = d.filter((pl.col("stop") == "70508") & (pl.col("dir") == 0))
    unpaired = m.filter(pl.col("actual_arr").is_null()).height
    assert unpaired > 0, (
        "no unresolved predictions at Magoun inbound: either every predicted train "
        "arrived, or rollup is dropping them again. Re-roll with --force.")
    # Measured 3.7% at this stop. An order of magnitude more means the arrival
    # detection has broken, not that the trains stopped coming.
    assert unpaired / m.height < 0.25, f"{unpaired}/{m.height} unresolved is too many"
