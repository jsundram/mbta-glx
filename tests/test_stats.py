"""Pin the self-score panel to the numbers behind it, and to arithmetic that must
not drift.

The panel degrades quietly by design -- it hides itself on any throw -- so a renamed
field does not fail, it shows nothing. That is the failure this file exists to catch,
from both sides: every name index.html reads has to be in the payload, and the
payload has to carry nothing less than the contract.

It used to pin web/stats.json, which the panel fetched as a sibling asset. The panel
asks the backend for TODAY now, because a published file cannot answer that question:
stats.json is written from CLOSED days and the board is served from Pages, so nothing
computed on the Mac during the day can reach it. data/stats.json is still the
project's record and is still checked below; it is simply no longer what the rider
reads.
"""
import json
import pathlib
import re
import sys

import polars as pl
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import server  # noqa: E402
import stats  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BOARD = WEB / "index.html"

# architecture.md 2, the published contract for data/stats.json -- the record, not
# the panel. Extra keys are fine (invariant 5: schema changes are additive).
CONTRACT = {"as_of", "window_days", "caught", "of", "mean_platform_wait_s",
            "mean_door_to_train_s", "by_lead"}
BIN_CONTRACT = {"bin", "n", "p10", "p50", "p90"}


def _panel_source() -> str:
    """The body of index.html's todayPanel(), which is the only reader of /today."""
    src = BOARD.read_text()
    i = src.index("async function todayPanel()")
    # The function is at top level, so the first line-initial "}" closes it.
    j = src.index("\n}", i)
    return src[i:j]


def _scored(arrival, predicted, *, caught=True, wait=60.0):
    """One replay.score row, in the shape today_summary reads."""
    return {"told": arrival - 600, "arrival": arrival, "predicted": predicted,
            "caught": caught, "wait_s": wait}


def test_the_panel_asks_the_backend_for_today():
    """If it stops fetching /today, the pin below is pinning nothing."""
    src = _panel_source()
    assert "/today?walk=" in src, "the panel no longer asks for today"
    assert "Magoun.backendURL()" in src, \
        "the panel invented its own copy of the backend host"


def test_every_field_the_panel_reads_is_in_the_payload():
    """The two-sided pin: rename a field on either side and this fails.

    Keyed off index.html itself rather than a list typed twice, so the test cannot
    quietly agree with a stale copy of the contract.
    """
    want = set(re.findall(r"\bd\.(\w+)", _panel_source()))
    assert len(want) >= 7, f"failed to parse todayPanel(); got only {want}"
    got = set(server.today_summary([], "2026-09-27", 390, 0.0))
    assert want <= got, \
        f"the panel reads fields /today does not send: {sorted(want - got)}"


def test_the_panel_hides_itself_below_a_handful_of_trains():
    """A percentage of two trains is a rounding artifact, not a measurement."""
    src = _panel_source()
    assert "min_trains" in src and "hidden = true" in src
    assert server.TODAY_MIN_TRAINS >= 3


def test_the_three_buckets_partition_the_trains():
    """early + close + late == trains, at any threshold. A rider reading three
    percentages that do not add up has been handed a different question's answer."""
    # Deliberately lopsided, and on the boundaries: |err| == close_s is "close",
    # one second past it is not. A symmetric fixture would pass with the early and
    # late buckets swapped, which is the mistake that matters -- err is
    # arrival - predicted, so a train that came BEFORE the quoted time has a
    # NEGATIVE err, and getting that backwards files the dangerous trains under the
    # reassuring word.
    rows = [_scored(2000, 2000 + 300),                             # 300 s early
            _scored(3000, 3000),                                   # dead on
            _scored(6000, 6000 - 120), _scored(7000, 7000 + 120),  # exactly at the edge
            _scored(1000, 1000 - 300), _scored(4000, 4000 - 121)]  # late, and just over
    d = server.today_summary(rows, "2026-09-27", 390, 1.0)
    assert d["trains"] == len(rows) == d["early"] + d["close"] + d["late"]
    assert (d["early"], d["close"], d["late"]) == (1, 3, 2)


def test_a_row_the_board_never_spoke_about_is_not_scored():
    """`told is None` is an arrival with no prediction and no slot: the board said
    nothing, so it is not evidence about what the board said."""
    rows = [_scored(1000, 1000), {"told": None, "arrival": 2000, "predicted": None,
                               "caught": False, "wait_s": None}]
    assert server.today_summary(rows, "2026-09-27", 390, 1.0)["trains"] == 1


def test_the_median_wait_is_over_the_trains_that_were_caught():
    """A missed train has a negative wait; mixing those in makes a bad morning
    produce a small reassuring median."""
    rows = [_scored(1000, 1000, caught=True, wait=120.0),
            _scored(2000, 2000, caught=True, wait=180.0),
            _scored(3000, 3000, caught=False, wait=-600.0)]
    d = server.today_summary(rows, "2026-09-27", 390, 1.0)
    assert d["caught"] == 2 and d["median_wait_s"] == 150
    assert server.today_summary([], "2026-09-27", 390, 1.0)["median_wait_s"] is None


def test_the_payload_states_the_threshold_it_measured_against():
    """The board prints "within N min" from this, rather than restating 120."""
    d = server.today_summary([], "2026-09-27", 390, 0.0)
    assert d["close_s"] == server.TODAY_CLOSE_S
    assert "close_s" in _panel_source(), "the board hardcodes the threshold"


def test_a_holed_capture_is_reported_not_swallowed():
    """Arrivals are STOPPED_AT transitions, so a minute of missing snapshots loses
    whole trains -- and the score then comes out worse than the day really was."""
    d = server.today_summary([], "2026-09-27", 390, 1.0, gaps=(900, 600))
    assert (d["gap_s"], d["max_gap_s"]) == (900, 600)
    assert "gap_s" in _panel_source(), "the board never mentions a holed record"


def test_published_stats_satisfies_the_contract():
    d = json.loads((ROOT / "data" / "stats.json").read_text())
    assert CONTRACT <= set(d), f"stats.json is missing {sorted(CONTRACT - set(d))}"
    for b in d["by_lead"]:
        assert BIN_CONTRACT <= set(b)
        assert b["p10"] <= b["p50"] <= b["p90"], b
        assert b["n"] > 0
    assert d["of"] >= d["caught"] >= 0
    assert d["window_days"] >= 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d["as_of"])


def test_the_record_is_not_published_to_the_origin():
    """It is nobody's asset now. A file in web/ that nothing fetches goes stale
    there with every test still green, because no test has a reason to read it."""
    assert not (WEB / "stats.json").exists(), \
        "web/stats.json is back; the panel does not read it"
    assert "stats.json" not in {a.name for a in __import__("publish").MANIFEST}


# ---- the aggregation, on synthetic days so the arithmetic is checkable ----

def _day(day, told, caught, riders, pw_sum, dtt_sum):
    return {"day": day, "arrivals": told, "told": told, "caught": caught,
            "riders": riders, "platform_wait_sum_s": pw_sum,
            "door_to_train_sum_s": dtt_sum, "walk_s": 390, "scored_at": 0}


def test_window_mean_is_weighted_by_riders_not_a_mean_of_means():
    """A quiet day and a busy day do not count equally.

    10 riders averaging 60 s and 90 riders averaging 600 s is 546 s, not 330 s.
    """
    scores = {"2026-09-01": _day("2026-09-01", 10, 10, 10, 600, 6000),
              "2026-09-02": _day("2026-09-02", 90, 80, 90, 54000, 540000)}
    d = stats.build(scores, 7)
    assert d["mean_platform_wait_s"] == round(54600 / 100) == 546
    assert d["mean_door_to_train_s"] == round(546000 / 100) == 5460
    assert (d["caught"], d["of"]) == (90, 100)
    assert d["window_days"] == 2
    assert d["as_of"] == "2026-09-02"


def test_window_takes_the_last_n_days_and_reports_what_it_used():
    scores = {f"2026-09-{i:02d}": _day(f"2026-09-{i:02d}", 10, 9, 10, 1000, 5000)
              for i in range(1, 21)}
    d = stats.build(scores, 7)
    assert d["window_days"] == 7
    assert d["as_of"] == "2026-09-20"
    assert d["of"] == 70

    # Fewer scored days than the window is not an error; it is a young scoreboard.
    d = stats.build({k: scores[k] for k in list(scores)[:3]}, 7)
    assert d["window_days"] == 3


def test_a_day_with_no_riders_reports_absent_not_zero():
    """NaN on the board would render as "NaN min", and 0 would read as a free ride."""
    d = stats.build({"2026-09-01": _day("2026-09-01", 4, 4, 0, 0, 0)}, 7)
    assert d["mean_platform_wait_s"] == -1
    assert d["mean_door_to_train_s"] == -1
    assert d["caught"] == 4


def test_build_returns_none_when_nothing_has_been_scored():
    """Better no file at all than a file full of zeroes: the panel hides itself."""
    assert stats.build({}, 7) is None


def test_scoreboard_round_trips_whole_rows(tmp_path, monkeypatch):
    """Invariant 4: compare the whole object, not a few fields.

    The scoreboard is the only copy of a score once data/live is pruned, so a
    lossy round-trip here loses history permanently.
    """
    monkeypatch.setattr(stats, "SCORES", tmp_path / "scores.jsonl")
    scores = {"2026-09-01": _day("2026-09-01", 4, 3, 7, 700, 3500),
              "2026-09-02": _day("2026-09-02", 5, 5, 9, 900, 4500)}
    stats.write_scores(scores)
    assert stats.read_scores() == scores

    # Adding a day must not disturb the ones already written.
    scores["2026-09-03"] = _day("2026-09-03", 6, 6, 11, 1100, 5500)
    stats.write_scores(scores)
    assert stats.read_scores() == scores


def test_the_scoreboard_is_replaced_atomically(tmp_path, monkeypatch):
    """It is the only copy of a score once data/live is pruned.

    A truncating in-place rewrite loses every day at once if it dies midway, so the
    write goes beside the file and renames -- and leaves no debris behind.
    """
    monkeypatch.setattr(stats, "SCORES", tmp_path / "scores.jsonl")
    stats.write_scores({"2026-09-01": _day("2026-09-01", 4, 3, 7, 700, 3500)})
    assert list(stats.read_scores()) == ["2026-09-01"]
    assert list(tmp_path.iterdir()) == [tmp_path / "scores.jsonl"], \
        f"left debris: {[p.name for p in tmp_path.iterdir()]}"

    src = inspect_source(stats.write_scores)
    assert "replace" in src, "write_scores truncates in place"


def inspect_source(fn):
    import inspect
    return inspect.getsource(fn)


def test_the_service_day_comes_from_a_real_zone(tmp_path):
    """Invariant 8. Archive filenames are ET days; the host's clock may not be ET."""
    src = (ROOT / "src" / "stats.py").read_text()
    assert 'time.strftime("%Y-%m-%d")' not in src, \
        "uses the host's local date to decide which day is still open"
    assert "service.TZ" in src


def test_a_day_with_no_schedule_snapshot_is_not_scored(tmp_path, monkeypatch):
    """Scoring it would record a permanent undercount.

    replay pins the schedule to the day being scored; with no snapshot for that day
    there is no timetable tier at all, which took 2026-09-24 from 69/74 to 60/74 when
    the wrong day was used. A day already in the scoreboard is never rescored, so the
    wrong number would be final.
    """
    import replay
    monkeypatch.setattr(replay, "SCHED", tmp_path / "empty")
    with pytest.raises(FileNotFoundError) as e:
        replay._schedule_slots("2026-09-24")
    # gzip.open would raise FileNotFoundError on its own, so asserting the type
    # alone pins nothing. What the explicit check adds is a message that says which
    # day and why it can never be recovered.
    msg = str(e.value)
    assert "2026-09-24" in msg
    assert "schedule snapshot" in msg
    assert "8 days" in msg, "the message does not say why the day is unrecoverable"


def test_pruned_days_still_count_toward_the_window(tmp_path, monkeypatch):
    """The whole point of the scoreboard: the archive goes, the score stays."""
    monkeypatch.setattr(stats, "SCORES", tmp_path / "scores.jsonl")
    monkeypatch.setattr(stats, "PAIRS", tmp_path / "pairs")
    old = {f"2026-01-{i:02d}": _day(f"2026-01-{i:02d}", 10, 9, 10, 2000, 9000)
           for i in range(1, 4)}
    stats.write_scores(old)
    d = stats.build(stats.read_scores(), 7)
    assert d["window_days"] == 3 and d["of"] == 30
    assert d["by_lead"] == []          # no pairs for those days, and that is fine


# ---- by_lead, on a synthetic pairs file ----

def _pairs(tmp_path, rows):
    p = tmp_path / "pairs"
    p.mkdir(exist_ok=True)
    df = pl.DataFrame(rows, schema={
        "day": pl.String, "stop": pl.String, "dir": pl.Int8, "route": pl.String,
        "veh": pl.String, "trip": pl.String, "made_at": pl.Int64,
        "pred_arr": pl.Int64, "actual_arr": pl.Int64, "lead_s": pl.Int32,
        "err_s": pl.Int32, "unc_s": pl.Int32})
    for day, part in df.group_by("day"):
        part.write_parquet(p / f"pairs-{day[0]}.parquet")
    return p


def _row(day, lead, err, stop=stats.MAGOUN_IN, direction=0):
    return {"day": day, "stop": stop, "dir": direction, "route": "Green-E",
            "veh": "G-1", "trip": "t", "made_at": 0, "pred_arr": 0,
            "actual_arr": 0, "lead_s": lead, "err_s": err, "unc_s": 60}


def test_by_lead_drops_mispaired_long_leads(tmp_path, monkeypatch):
    """Measured: past 20 min these are predictions attributed to the next visit.

    One such row is worth thousands of seconds of error, so leaving them in does
    not blur the bins, it inverts them.
    """
    rows = ([_row("2026-09-01", 100, 10) for _ in range(20)]
            + [_row("2026-09-01", 4000, -7555)])
    monkeypatch.setattr(stats, "PAIRS", _pairs(tmp_path, rows))
    out = stats.by_lead(["2026-09-01"])
    assert [b["bin"] for b in out] == ["0-5min"]
    assert out[0]["n"] == 20
    assert out[0]["p50"] == 10


def test_the_cap_is_the_last_bin_edge_and_not_a_second_number():
    """As two independent constants these drifted by one, and nothing could see it.

    A row at exactly the cap passed the filter and fell through every bin -- no wrong
    output, just a row silently going nowhere. Deriving the cap removes the class of
    bug, so this pins the derivation rather than the symptom.
    """
    assert stats.MAX_LEAD_S is stats.LEAD_BINS[-1][1]
    src = (ROOT / "src" / "stats.py").read_text()
    assert "MAX_LEAD_S = LEAD_BINS[-1][1]" in src, "the cap is a second number again"


def test_every_row_the_filter_keeps_lands_in_exactly_one_bin(tmp_path, monkeypatch):
    """Boundary rows, counted against the bins rather than against a restated rule."""
    leads = [0, 1, 299, 300, 301, 899, 900, 901, 1199]
    rows = [_row("2026-09-01", lead, 5) for lead in leads]
    monkeypatch.setattr(stats, "PAIRS", _pairs(tmp_path, rows))
    out = stats.by_lead(["2026-09-01"])
    assert sum(b["n"] for b in out) == len(leads)
    assert [b["bin"] for b in out] == [name for _, _, name in stats.LEAD_BINS]


def test_by_lead_is_magoun_inbound_only(tmp_path, monkeypatch):
    """Scope: one rider, one platform, one direction. Other stops are in pairs too."""
    rows = ([_row("2026-09-01", 100, 10) for _ in range(5)]
            + [_row("2026-09-01", 100, 999, stop="70501") for _ in range(50)]
            + [_row("2026-09-01", 100, 999, direction=1) for _ in range(50)])
    monkeypatch.setattr(stats, "PAIRS", _pairs(tmp_path, rows))
    out = stats.by_lead(["2026-09-01"])
    assert out[0]["n"] == 5 and out[0]["p50"] == 10


def test_by_lead_bins_are_contiguous_and_cover_the_cap():
    lo_edges = [lo for lo, _, _ in stats.LEAD_BINS]
    hi_edges = [hi for _, hi, _ in stats.LEAD_BINS]
    assert lo_edges[0] == 0
    assert hi_edges[-1] == stats.MAX_LEAD_S
    assert lo_edges[1:] == hi_edges[:-1], "a gap or overlap would silently drop rows"


def test_by_lead_reads_only_the_days_in_the_window(tmp_path, monkeypatch):
    rows = ([_row("2026-09-01", 100, 1) for _ in range(10)]
            + [_row("2026-09-02", 100, 500) for _ in range(10)])
    monkeypatch.setattr(stats, "PAIRS", _pairs(tmp_path, rows))
    assert stats.by_lead(["2026-09-01"])[0]["p50"] == 1
    assert stats.by_lead(["2026-09-02"])[0]["p50"] == 500
    assert stats.by_lead(["2026-09-01", "2026-09-02"])[0]["n"] == 20


def test_by_lead_on_a_missing_day_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "PAIRS", tmp_path / "nothing")
    assert stats.by_lead(["2026-09-01"]) == []


@pytest.mark.parametrize("fn", ["read_scores", "write_scores", "by_lead", "build"])
def test_the_pure_helpers_take_no_clock_and_no_network(fn):
    """stats.py is scored from files; anything reaching for now() is a bug."""
    import inspect
    src = inspect.getsource(getattr(stats, fn))
    for bad in ("time.time", "requests", "urlopen", "datetime.now"):
        assert bad not in src, f"{fn} reaches for {bad}"
