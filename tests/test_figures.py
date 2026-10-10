"""The two figures, and the four ways they would lie quietly.

Every check here is over synthesised snapshots rather than the archive, because
the archive is gitignored and the failures below have nothing to do with which day
you run them on. The one test that does need real data says so and skips.
"""
import gzip
import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import figures  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

MED, BALL, MAGOUN = "70512", "70510", "70508"
MED_OUT = "70511"


def snaps(tmp_path, rows):
    """Write `rows` -- (t, [vehicle dicts]) -- as a raw archive day and hand back the path."""
    p = tmp_path / "rt-2026-09-26.jsonl.gz"
    with gzip.open(p, "wt") as f:
        for t, vehicles in rows:
            f.write(json.dumps({"t": t, "preds": [], "vehicles": vehicles}) + "\n")
    return p


def veh(stop, status, vid="G-1", route="Green-E", d=0):
    return {"id": vid, "route": route, "trip": "t1", "dir": d, "stop": stop,
            "status": status, "seq": 1, "ts": 0}


T0 = figures.midnight("2026-09-26")


# --- invariant 2: an arrival is a transition ---

def test_a_second_visit_to_the_same_stop_is_a_second_arrival(tmp_path):
    """Keying on (vehicle, stop) records one visit a day. Trains come back."""
    rows = [(T0 + 0, [veh(MAGOUN, "STOPPED_AT")]),
            (T0 + 15, [veh(MAGOUN, "STOPPED_AT")]),
            (T0 + 30, [veh(BALL, "IN_TRANSIT_TO")]),
            (T0 + 45, [veh(MAGOUN, "STOPPED_AT")])]
    got = figures.arrivals(snaps(tmp_path, rows))
    assert [r[0] for r in got] == [T0, T0 + 45]


def test_a_dwell_is_how_long_it_was_still_there(tmp_path):
    rows = [(T0 + 0, [veh(MED, "STOPPED_AT")]),
            (T0 + 15, [veh(MED, "STOPPED_AT")]),
            (T0 + 30, [veh(MED, "STOPPED_AT")]),
            (T0 + 45, [veh(BALL, "IN_TRANSIT_TO")])]
    got = figures.arrivals(snaps(tmp_path, rows))
    assert len(got) == 1 and got[0][5] == 30


def test_a_stop_off_the_corridor_is_not_an_arrival(tmp_path):
    rows = [(T0, [veh("70239", "STOPPED_AT")])]   # Prudential, past Copley
    assert figures.arrivals(snaps(tmp_path, rows)) == []


# --- invariant 1: a run is a vehicle's, not a trip's ---

def test_a_run_survives_the_trip_id_changing_under_it():
    arr = [[T0 + 0, "G-1", "Green-E", 0, 0, 60],
           [T0 + 200, "G-1", "Green-E", 1, 0, 20],
           [T0 + 300, "G-1", "Green-E", 2, 0, 20]]
    runs = figures.runs(arr, "2026-09-26")
    assert len(runs) == 1
    assert runs[0]["p"] == [0, 0, 60, 1, 200, 20, 2, 300, 20]


def test_the_turnaround_ends_a_run():
    """Inbound to Magoun, then back out: two lines on the diagram, not one."""
    arr = [[T0 + 0, "G-1", "Green-E", 0, 0, 0],
           [T0 + 200, "G-1", "Green-E", 1, 0, 0],
           [T0 + 900, "G-1", "Green-E", 1, 1, 0],
           [T0 + 1100, "G-1", "Green-E", 0, 1, 0]]
    runs = figures.runs(arr, "2026-09-26")
    assert [r["d"] for r in runs] == [0, 1]


def test_a_layover_longer_than_the_gap_ends_a_run():
    """Otherwise a train that sat out the evening draws one line across the gap."""
    g = figures.RUN_GAP_S
    arr = [[T0 + 0, "G-1", "Green-E", 0, 0, 0],
           [T0 + 200, "G-1", "Green-E", 1, 0, 0],
           [T0 + 200 + g + 60, "G-1", "Green-E", 2, 0, 0],
           [T0 + 200 + g + 200, "G-1", "Green-E", 3, 0, 0]]
    runs = figures.runs(arr, "2026-09-26")
    assert [len(r["p"]) // 3 for r in runs] == [2, 2]


def test_two_vehicles_are_two_runs():
    arr = [[T0 + 0, "G-1", "Green-E", 0, 0, 0], [T0 + 10, "G-2", "Green-E", 0, 0, 0],
           [T0 + 200, "G-1", "Green-E", 1, 0, 0], [T0 + 210, "G-2", "Green-E", 1, 0, 0]]
    runs = figures.runs(arr, "2026-09-26")
    assert sorted(r["v"] for r in runs) == ["G-1", "G-2"]


def test_an_outbound_run_reaches_the_terminus_it_turned_at():
    """70511 is reported STOPPED_AT three times in two archived days, so without
    this every outbound trip stops a station short of Medford/Tufts."""
    arr = [[T0 + 0, "G-1", "Green-E", 3, 1, 0],
           [T0 + 100, "G-1", "Green-E", 2, 1, 0],
           [T0 + 200, "G-1", "Green-E", 1, 1, 0],     # Ball Square, outbound
           [T0 + 290, "G-1", "Green-E", 0, 0, 240],   # berthed at the terminus
           [T0 + 600, "G-1", "Green-E", 1, 0, 0]]
    out, back = figures.runs(arr, "2026-09-26")
    assert out["d"] == 1 and out["p"][-3] == 0, "the outbound run stops short"
    assert out["p"][-2] == 290, "the join invented a time instead of reusing one"
    assert back["p"][0] == 0, "the inbound run does not start at the terminus"


def test_a_short_turn_starts_the_next_run_where_the_train_turned():
    """2026-09-26: 101 of 117 inbound runs ended at North Station and 62 outbound
    runs then began at Science Park, because the train turned while berthed."""
    arr = [[T0 + 0, "G-1", "Green-E", 5, 0, 0],
           [T0 + 100, "G-1", "Green-E", 6, 0, 0],
           [T0 + 200, "G-1", "Green-E", 7, 0, 120],   # North Station, inbound
           [T0 + 400, "G-1", "Green-E", 6, 1, 0],     # Science Park, outbound
           [T0 + 500, "G-1", "Green-E", 5, 1, 0]]
    inb, outb = figures.runs(arr, "2026-09-26")
    assert inb["p"][-3] == 7
    assert outb["p"][0] == 7, "the outbound run begins one stop past the turn"
    assert outb["p"][1] == 320, "it left when the dwell ended, not before"


def test_a_long_layover_is_not_joined():
    """A train that turns after sitting for an hour did not turn at that moment,
    and drawing a line across the gap says it did."""
    arr = [[T0 + 0, "G-1", "Green-E", 2, 1, 0],
           [T0 + 100, "G-1", "Green-E", 1, 1, 0],
           [T0 + 100 + figures.RUN_GAP_S + 60, "G-1", "Green-E", 0, 0, 0],
           [T0 + 100 + figures.RUN_GAP_S + 200, "G-1", "Green-E", 1, 0, 0]]
    a, b = figures.runs(arr, "2026-09-26")
    assert a["p"][-3] == 1, "a run was joined across a layover"
    assert len(b["p"]) == 6


def test_a_hold_is_a_longer_dwell_not_a_new_run():
    """Measured: G-10162 reported STOPPED_AT Government Center at 16:28:29, left
    the state, and reported it again at 16:33:30. Split there, half the trip
    becomes a second line -- which is what most of the broken segments were."""
    arr = [[T0 + 0, "G-1", "Green-E", 8, 0, 45],
           [T0 + 60, "G-1", "Green-E", 9, 0, 45],
           [T0 + 360, "G-1", "Green-E", 9, 0, 45],    # the same station again
           [T0 + 500, "G-1", "Green-E", 10, 0, 30]]
    runs = figures.runs(arr, "2026-09-26")
    assert len(runs) == 1, "a hold at one station split the trip"
    # Arrived at +60, still there at +360 and said to be for another 45 s: the
    # step is 345 s wide, measured from the arrival, not from the second sighting.
    assert runs[0]["p"][5] == 345, "the hold is not drawn as the dwell it was"
    assert [runs[0]["p"][i] for i in (0, 3, 6)] == [8, 9, 10]


def test_a_real_doubling_back_still_splits():
    arr = [[T0 + 0, "G-1", "Green-E", 8, 0, 0],
           [T0 + 100, "G-1", "Green-E", 9, 0, 0],
           [T0 + 200, "G-1", "Green-E", 7, 0, 0],
           [T0 + 300, "G-1", "Green-E", 8, 0, 0]]
    assert len(figures.runs(arr, "2026-09-26")) == 2


def test_only_the_riders_line_is_published(tmp_path):
    """B and C touch five stations of this corridor and D eight, so on the diagram
    they are hundreds of short lines around the E trains the board is about."""
    rows = [(T0 + 0, [veh(MAGOUN, "STOPPED_AT", "G-1", "Green-E"),
                      veh("70199", "STOPPED_AT", "G-2", "Green-B")])]
    got = figures.arrivals(snaps(tmp_path, rows))
    assert [r[1] for r in got] == ["G-1"]
    assert len(figures.arrivals(snaps(tmp_path, rows), routes=())) == 2


# --- the y axis: measured, and net of the berth ---

def test_the_terminus_layover_does_not_stretch_the_first_leg():
    """The bug this found: the median Medford to Ball leg measured 617 s, which is
    the layover. A y axis built from that draws the first hop ten times too tall."""
    runs = [{"v": "G-1", "r": "E", "d": 0,
             "p": [0, 0, 600,          # ten minutes berthed at Medford/Tufts
                   1, 700, 20,         # then 100 s to Ball Square
                   2, 860, 20]}]       # then 140 s to Magoun
    y = figures.spacing(runs)
    assert y[0] == 0
    assert y[1] == 100
    assert y[2] == 240


def test_spacing_falls_back_rather_than_collapsing_a_station():
    y = figures.spacing([])
    assert len(y) == len(figures.CORRIDOR)
    assert all(b > a for a, b in zip(y, y[1:])), "two stations cannot share a line"


# --- the second y axis ---

def test_distance_is_cumulative_and_only_goes_one_way(tmp_path, monkeypatch):
    ref = {}
    for i, (name, inb, outb) in enumerate(figures.CORRIDOR):
        ref[inb] = {"name": name, "lat": 42.4 - i * 0.01, "lon": -71.1}
        ref[outb] = dict(ref[inb])
    f = tmp_path / "stops.json"
    f.write_text(json.dumps(ref))
    monkeypatch.setattr(figures, "STOPS_REF", f)
    m = figures.distances()
    assert m[0] == 0
    assert all(b > a for a, b in zip(m, m[1:])), "the corridor doubles back on itself"
    # 0.01 degrees of latitude is about 1.11 km, thirteen times.
    assert 14000 < m[-1] < 14600, m[-1]


def test_no_coordinates_means_no_distance_axis(tmp_path, monkeypatch):
    """Not a crash and not a guess: the page hides the choice and uses run time."""
    monkeypatch.setattr(figures, "STOPS_REF", tmp_path / "absent.json")
    assert figures.distances() is None


def test_the_corridor_is_about_as_long_as_it_is():
    """The one pin on the real cache: a coordinate typo or a swapped pair would
    otherwise pass every test above and quietly redraw the line."""
    if not figures.STOPS_REF.exists():
        pytest.skip("no cached stop coordinates on this machine")
    m = figures.distances()
    assert m is not None
    assert 9000 < m[-1] < 10500, f"Medford/Tufts to Copley measured {m[-1]} m"
    # The GLX half is the long, quick half; downtown is short and slow. That is
    # the whole reason the axis is a choice.
    lech = next(i for i, (n, _, _) in enumerate(figures.CORRIDOR) if n == "Lechmere")
    assert m[lech] > (m[-1] - m[lech]), "the GLX half should be the longer one"


def test_both_axes_are_published():
    if not figures.STOPS_REF.exists():
        pytest.skip("no cached stop coordinates on this machine")
    fig = figures.build(marey_days=1, heat_days=1)
    assert all("y" in s and "m" in s for s in fig["stops"]), \
        "a stop is missing one of the two y axes"


# --- the heatmap ---

@pytest.fixture
def lamp(monkeypatch):
    """A tiny LAMP frame: one ordinary day and one all-extras day."""
    import polars as pl

    def rows(date, trips):
        out = []
        for i, (trip, sched, actual) in enumerate(trips):
            out.append({"service_date": date, "stop_id": figures.MAGOUN_IN,
                        "direction_id": False, "trip_id": trip, "vehicle_id": "G-1",
                        "stop_sequence": 6, "route_id": "Green-E",
                        "scheduled_arrival_time": sched,
                        "arr": figures.midnight(
                            f"{date // 10000}-{date // 100 % 100:02d}-{date % 100:02d}")
                        + actual,
                        "move_timestamp": None, "dwell_time_seconds": 0,
                        "travel_time_seconds": 0, "start_time": 0,
                        "direction_destination": "x", "stop_timestamp": 0})
        return out

    data = (rows(20260921, [("t1", 8 * 3600, 8 * 3600 + 120),
                            ("t2", 8 * 3600 + 300, 8 * 3600 + 360),
                            ("ADDED-9", 8 * 3600, 8 * 3600 + 30000)])
            + rows(20260919, [("ADDED-1", 9 * 3600, 9 * 3600 + 40),
                              ("ADDED-2", 9 * 3600 + 300, 9 * 3600 + 80)]))
    monkeypatch.setattr(figures.glx, "load_events", lambda: pl.DataFrame(data))
    return data


def test_late_is_positive_and_early_is_negative(lamp):
    h = figures.heat(30)
    day = next(d for d in h["days"] if d["day"] == "2026-09-21")
    assert day["p50"] == 90, "actual minus scheduled: a train 90 s late reads +90"


def test_an_added_trip_is_counted_and_never_binned(lamp):
    """18.3% of real arrivals are ADDED-*, and LAMP still fills in a scheduled time
    for them: 88% of those land over half an hour out. One of them owns a cell."""
    h = figures.heat(30)
    day = next(d for d in h["days"] if d["day"] == "2026-09-21")
    assert day["n"] == 3 and day["added"] == 1
    assert sum(c[2] for c in day["cells"]) == 2, "an extra reached the median"
    assert all(abs(c[1]) < figures.MAX_DEV_S for c in day["cells"])


def test_a_day_with_nothing_to_score_is_still_a_row(lamp):
    """Four weekend days run entirely as extras. Dropping the rows would draw them
    as if the trains had not run."""
    h = figures.heat(30)
    day = next(d for d in h["days"] if d["day"] == "2026-09-19")
    assert day["cells"] == [] and day["p50"] is None
    assert day["added"] == day["n"] == 2


def test_no_lamp_at_all_is_an_empty_heatmap_not_a_crash(monkeypatch, tmp_path):
    """CI has no data/raw. load_events() concatenates nothing there and polars
    raises, which took figures.build() -- and the axes test with it -- down on
    every machine but this one."""
    monkeypatch.setattr(figures.glx, "RAW", tmp_path)
    h = figures.heat(30)
    assert h["days"] == [] and h["missing"] == []


def test_a_day_nobody_downloaded_is_named_not_skipped(lamp):
    h = figures.heat(30)
    assert h["missing"] == ["2026-09-20"]


# --- the served artifact and the page that reads it ---

def test_the_page_asks_the_backend_for_the_figures_and_nothing_else():
    """It fetches one thing, the backend's /figures, and computes every mark from
    it. The address is model.json's backend_url, never a literal: a literal is a
    host tests/test_regressions.py would have to admit."""
    src = (WEB / "figures.html").read_text()
    assert re.findall(r"fetch\(([^,)]+)", src) == ["`${BACKEND}/figures`"]
    assert "backend_url" in src
    assert '<script src="model.js">' in src, "the page has no way to learn the address"


def test_the_page_is_published_and_its_data_is_not():
    """figures.json changes every night. Published, it was a commit and a push from
    the capture host every day; served, it is a file the backend reads."""
    import publish
    import server
    names = {a.name for a in publish.MANIFEST}
    assert "figures.html" in names
    assert "figures.json" not in names and "figures.json" not in publish.FIX
    assert not (WEB / "figures.json").exists(), "a stale published copy is still in web/"
    assert "/figures" in server.BROWSER_ROUTES
    assert server.FIGURES == figures.OUT, "the backend serves a different file than figures.py writes"


def test_the_board_links_to_it():
    assert 'href="figures.html"' in (WEB / "index.html").read_text()


def test_the_daily_run_rebuilds_it():
    """A figure nothing regenerates is a figure that goes stale."""
    daily = (ROOT / "src" / "daily.sh").read_text()
    assert "src/figures.py" in daily
    assert "src/fetch_history.py" in daily, \
        "the heatmap reads data/raw, which only fetch_history.py fills"


def test_the_corridor_is_in_the_order_the_archive_walks_it():
    """The one check that needs the real archive: the hardcoded corridor order has
    to match the order trains actually pass the stations."""
    days = figures.archives()
    if not days:
        pytest.skip("no archive on this machine")
    day, path = sorted(days.items())[-1]
    runs = figures.runs(figures.arrivals(path), day)
    inbound = [r for r in runs if r["d"] == 0 and len(r["p"]) >= 12]
    if not inbound:
        pytest.skip("no long inbound run in the newest archived day")
    bad = [r for r in inbound
           if any(b <= a for a, b in zip(r["p"][::3], r["p"][3::3]))]
    assert not bad, f"{len(bad)} of {len(inbound)} inbound runs walk the corridor backwards"
