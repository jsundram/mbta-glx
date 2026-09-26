"""Contract test: the prediction logic must produce exactly these rows.

Fixtures in tests/fixtures/ are real archived snapshots with everything the
function needs inlined -- schedule, skip set, berth state, walk, clock. They pin
`service.compute_rows` today, and when a JavaScript frontend exists it must
replay the same files and produce identical rows. That is the only thing standing
between a static frontend and two implementations quietly disagreeing.

Regenerate deliberately, never to make a failure go away:
    uv run python src/make_fixtures.py 2026-09-25
"""
import functools
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import os
os.environ.setdefault("MAGOUN_NTFY_TOPIC", "test")
os.environ.setdefault("MAGOUN_NTFY_CMD", "test-cmd")

import service  # noqa: E402

FIXTURES = sorted((pathlib.Path(__file__).parent / "fixtures").glob("cases-*.json"))
ROW_KEYS = {"eta", "lo", "hi", "source", "backed", "vehicle", "skipped",
            "catchable", "leave_in"}


def _cases():
    for f in FIXTURES:
        for i, c in enumerate(json.loads(f.read_text())):
            yield pytest.param(c, id=f"{f.stem}#{i}")


def _run(c):
    return service.compute_rows(
        c["now"], c["preds"], c["vehicles"], service.Model(),
        c["walk"], tuple(c["qs"]), c["horizon"], c["berths"],
        [(t, tr) for t, tr in c["slots"]], set(c["skipped"]))


@pytest.mark.skipif(not FIXTURES, reason="no fixtures generated")
@pytest.mark.parametrize("case", list(_cases()))
def test_rows_match_the_frozen_expectation(case):
    got = _run(case)
    assert len(got) == len(case["expected"])
    for a, b in zip(got, case["expected"]):
        for k in ROW_KEYS:
            av, bv = a.get(k), b.get(k)
            if isinstance(av, float) and isinstance(bv, float):
                assert abs(av - bv) < 1e-6, f"{k}: {av} != {bv}"
            else:
                assert av == bv, f"{k}: {av!r} != {bv!r}"


@pytest.mark.skipif(not FIXTURES, reason="no fixtures generated")
def test_compute_rows_is_pure():
    """No clock, no network, no file reads: same inputs, same output, twice."""
    case = json.loads(FIXTURES[0].read_text())[0]
    assert _run(case) == _run(case)


@pytest.mark.skipif(not FIXTURES, reason="no fixtures generated")
def test_every_row_carries_the_full_contract():
    """A JS port has to emit these exact keys; a missing one must fail here."""
    for f in FIXTURES:
        for c in json.loads(f.read_text()):
            for r in c["expected"]:
                assert ROW_KEYS <= set(r), ROW_KEYS - set(r)


@pytest.mark.skipif(not FIXTURES, reason="no fixtures generated")
def test_lo_is_never_quoted_as_the_leave_time_after_eta():
    """lo <= eta <= hi. The rider acts on lo; inverting these strands people."""
    for f in FIXTURES:
        for c in json.loads(f.read_text()):
            for r in c["expected"]:
                assert r["lo"] <= r["eta"] <= r["hi"], r


# --- the fixtures themselves must stay worth trusting ---

TIERS = {"mbta", "berthed at Medford/Tufts", "departed Medford/Tufts",
         "departed Ball Sq", "schedule", "not stopping here"}


@pytest.mark.skipif(not FIXTURES, reason="no fixtures generated")
def test_every_tier_is_exercised_by_some_fixture():
    """A tier with no fixture is a tier a JS port can get wrong undetected.

    Measured: a 30 s drift in the berth offset is caught by only 2 of 24 cases and
    a veto-window change by 1. Thin coverage still catches, but regenerating
    fixtures must never drop a tier entirely.
    """
    seen = {r["source"] for f in FIXTURES
            for c in json.loads(f.read_text()) for r in c["expected"]}
    assert TIERS <= seen, f"no fixture exercises: {sorted(TIERS - seen)}"


@pytest.mark.skipif(not FIXTURES, reason="no fixtures generated")
def test_fixtures_are_self_contained():
    """No fixture may depend on the clock, the network or the repo layout."""
    for f in FIXTURES:
        for c in json.loads(f.read_text()):
            assert {"now", "walk", "qs", "horizon", "berths", "skipped",
                    "slots", "preds", "vehicles", "expected"} <= set(c)
            assert c["slots"], "a fixture with no schedule cannot test the schedule tier"


# --- the static bundle: what the browser is allowed to read ---

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
CONSTANTS = {"veto_window_s", "stale_vehicle_s", "dedupe_s", "min_gap_s",
             "horizon_s", "band_s", "stops", "glx", "alert_corridor", "walk_s"}


def test_web_model_is_the_published_copy():
    """web/model.json is what the static board reads; a stale copy is a silent fork."""
    assert json.loads((WEB / "model.json").read_text()) == \
        json.loads((service.ROOT / "data" / "model.json").read_text()), \
        "web/model.json is out of date: re-copy data/model.json after a refit"


def test_web_model_script_is_the_same_bytes():
    """web/model.js exists because a file:// board cannot fetch model.json at all.

    Measured 2026-09-26 in Chromium: `URL scheme "file" is not supported`. It is a
    third copy of the same bytes, so it has to be checked, not trusted.
    """
    js = (WEB / "model.js").read_text()
    m = re.search(r"Magoun\._modelText = (\".*\");", js, re.S)
    assert m, "web/model.js is not in the shape app.js expects"
    assert json.loads(json.loads(m.group(1))) == \
        json.loads((WEB / "model.json").read_text()), \
        "web/model.js is stale: re-run src/fit.py"


def test_model_carries_every_constant_the_port_needs():
    """Anything the JS would otherwise re-type has to be in model.json."""
    got = set(json.loads((WEB / "model.json").read_text())["constants"])
    assert CONSTANTS <= got, f"model.json is missing: {sorted(CONSTANTS - got)}"


def test_the_walk_has_exactly_one_source():
    """It was the literal 390 in eight files. data/config.json is the copy now.

    A rider preference rather than a fitted value, so it lives in config rather than
    being fitted -- but it is published into model.json, because a board defaulting
    to one walk while stats.json scores another describes a different rider than the
    one reading the panel.
    """
    cfg = json.loads((service.ROOT / "data" / "config.json").read_text())
    assert service.DEFAULT_WALK == cfg["walk_s"]
    assert json.loads((WEB / "model.json").read_text())["constants"]["walk_s"] == \
        cfg["walk_s"], "model.json publishes a different walk than config.json"


def test_no_module_retypes_the_walk():
    """Every consumer reads service.DEFAULT_WALK; only the config names the number."""
    literal = str(json.loads((service.ROOT / "data" / "config.json").read_text())["walk_s"])
    offenders = []
    # .html too: src/status.html held a second copy that only .py scanning missed.
    files = (sorted((service.ROOT / "src").glob("*.py"))
             + sorted((service.ROOT / "src").glob("*.html")))
    for f in files:
        # service.py holds the one fallback, for a missing or mangled config file.
        if f.name == "service.py":
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            # Word-anchored: a bare substring match fires on 3900, a stop id or an
            # epoch, and the old "1790" escape hatch was already dead.
            if re.search(rf"\b{literal}\b", line):
                offenders.append(f"{f.name}:{i}")
    assert not offenders, f"the walk is re-typed at {offenders}"


def test_the_board_does_not_retype_the_walk():
    walk = json.loads((WEB / "model.json").read_text())["constants"]["walk_s"]
    for name in ("app.js", "board.html"):
        src = (WEB / name).read_text()
        assert not re.search(rf"\b{walk}\b", src), \
            f"web/{name} hardcodes a default walk"
    assert "walk_s" in (WEB / "app.js").read_text(), \
        "the board does not read the published walk at all"


def test_glx_stops_match_the_python_definition():
    c = json.loads((WEB / "model.json").read_text())["constants"]
    assert [tuple(t) for t in c["glx"]] == service.GLX_STOPS
    assert set(c["alert_corridor"]) == service.CORRIDOR
    assert c["stops"] == {"magoun_in": service.MAGOUN_IN, "ball_in": service.BALL_IN,
                          "med_in": service.MED_IN, "med_out": service.MED_OUT}


# --- the JavaScript port has to agree, case for case ---
#
# This is the mitigation architecture.md §3 calls for: going static puts the tier
# selection in the browser, so the logic exists twice and only Python is fitted and
# backtested. The fixtures are the seam. If this test goes quiet -- node missing in
# CI, say -- the static board is no longer covered by anything.

NODE_RUNNER = pathlib.Path(__file__).parent / "run_cases.js"


def _node() -> str | None:
    return shutil.which("node")


@functools.lru_cache(maxsize=1)
def _js_rows() -> dict:
    """{(file, case index): rows} as computed by web/app.js under node."""
    out = subprocess.run(
        [_node(), str(NODE_RUNNER), str(service.ROOT / "data" / "model.json"),
         *[str(f) for f in FIXTURES]],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    rows = {}
    for line in out.stdout.splitlines():
        d = json.loads(line)
        rows[(d["file"], d["case"])] = d["rows"]
    return rows


needs_node = pytest.mark.skipif(_node() is None or not FIXTURES,
                                reason="node or fixtures unavailable")


def test_the_javascript_side_actually_runs_here():
    """A skipped parity test and a passing one look identical in a green log.

    Locally node is optional. In CI it is the whole point: without it the suite goes
    green having compared nothing, which is exactly the "test goes quiet" failure
    described above. So CI is required to have node, and to have fixtures to run.
    """
    if not os.environ.get("CI"):
        pytest.skip("local run; node is optional here")
    assert _node() is not None, \
        "node is missing in CI: the JavaScript port is not being compared to anything"
    assert FIXTURES, "no fixtures in CI: the contract test is pinning nothing"


@needs_node
@pytest.mark.parametrize("case", list(_cases()))
def test_javascript_port_matches_python(case, request):
    """Same fixture, both implementations, row for row."""
    fname, idx = request.node.callspec.id.split("#")
    got = _js_rows()[(f"{fname}.json", int(idx))]
    py = _run(case)
    assert len(got) == len(py), f"{len(got)} JS rows vs {len(py)} Python rows"
    for a, b in zip(py, got):
        for k in ROW_KEYS:
            av, bv = a.get(k), b.get(k)
            if isinstance(av, (int, float)) and isinstance(bv, (int, float)) \
                    and not isinstance(av, bool):
                assert abs(av - bv) < 1e-6, f"{k}: python {av} != js {bv}"
            else:
                assert av == bv, f"{k}: python {av!r} != js {bv!r}"


@needs_node
def test_javascript_port_covers_every_case_and_tier():
    """26 cases and all six sources, or the port has an untested branch."""
    rows = _js_rows()
    expected = {(f.name, i) for f in FIXTURES
                for i, _ in enumerate(json.loads(f.read_text()))}
    assert set(rows) == expected
    assert len(rows) >= 26, f"only {len(rows)} cases ran through node"
    seen = {r["source"] for rs in rows.values() for r in rs}
    assert TIERS <= seen, f"the JS port never produced: {sorted(TIERS - seen)}"


@needs_node
def test_javascript_reads_its_constants_from_the_model():
    """A model.json without constants must fail loudly, not fall back to literals.

    Silent defaults are how two implementations drift while both look healthy.
    """
    stripped = json.loads((service.ROOT / "data" / "model.json").read_text())
    del stripped["constants"]["veto_window_s"]
    tmp = pathlib.Path(tempfile.mkdtemp()) / "model.json"
    tmp.write_text(json.dumps(stripped))
    out = subprocess.run([_node(), str(NODE_RUNNER), str(tmp), str(FIXTURES[0])],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode != 0
    assert "veto_window_s" in out.stderr


@pytest.mark.skipif(not FIXTURES, reason="no fixtures generated")
def test_the_v3_only_filters_are_exercised_by_some_fixture():
    """Some fixture must contain a deadhead, and a ghost, that visibly gets vetoed.

    `revenue` exists in the v3 API and not in the protobuf archive, so no sampled
    snapshot can carry a deadhead: measured, all 26 real cases had zero, and
    dropping `_revenue` from the JS port changed no row anywhere. make_fixtures
    synthesises one. The property checked here is not "a deadhead is present" but
    "a deadhead sits on the inbound approach and the row for its vehicle is still
    the wide `mbta` band" -- i.e. the filter actually bit.
    """
    caught = {"deadhead": False, "ghost": False}
    for f in FIXTURES:
        for c in json.loads(f.read_text()):
            wide = {r["vehicle"] for r in c["expected"] if r["source"] == "mbta"}
            for v in c["vehicles"]:
                a = v["attributes"]
                approaching = (
                    a["direction_id"] == 0
                    and a["current_status"] in ("IN_TRANSIT_TO", "INCOMING_AT")
                    and (v["relationships"]["stop"]["data"] or {}).get("id")
                    == service.MAGOUN_IN
                    and v["id"] in wide)
                if not approaching:
                    continue
                if not service._revenue(a) and service._live(a, c["now"]):
                    caught["deadhead"] = True
                if service._revenue(a) and not service._live(a, c["now"]):
                    caught["ghost"] = True
    assert caught["deadhead"], "no fixture proves a deadhead is kept out of the sharp tier"
    assert caught["ghost"], "no fixture proves a stale position is kept out of it"
