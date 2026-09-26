"""Contract test: the prediction logic must produce exactly these rows.

Fixtures in tests/fixtures/ are real archived snapshots with everything the
function needs inlined -- schedule, skip set, berth state, walk, clock. They pin
`service.compute_rows` today, and when a JavaScript frontend exists it must
replay the same files and produce identical rows. That is the only thing standing
between a static frontend and two implementations quietly disagreeing.

Regenerate deliberately, never to make a failure go away:
    uv run python src/make_fixtures.py 2026-09-25
"""
import json
import pathlib
import sys

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
             "horizon_s", "band_s", "stops", "glx", "alert_corridor"}


def test_web_model_is_the_published_copy():
    """web/model.json is what the static board reads; a stale copy is a silent fork."""
    assert json.loads((WEB / "model.json").read_text()) == \
        json.loads((service.ROOT / "data" / "model.json").read_text()), \
        "web/model.json is out of date: re-copy data/model.json after a refit"


def test_model_carries_every_constant_the_port_needs():
    """Anything the JS would otherwise re-type has to be in model.json."""
    got = set(json.loads((WEB / "model.json").read_text())["constants"])
    assert CONSTANTS <= got, f"model.json is missing: {sorted(CONSTANTS - got)}"


def test_glx_stops_match_the_python_definition():
    c = json.loads((WEB / "model.json").read_text())["constants"]
    assert [tuple(t) for t in c["glx"]] == service.GLX_STOPS
    assert set(c["alert_corridor"]) == service.CORRIDOR
    assert c["stops"] == {"magoun_in": service.MAGOUN_IN, "ball_in": service.BALL_IN,
                          "med_in": service.MED_IN, "med_out": service.MED_OUT}
