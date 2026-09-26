"""Pin stats.json to the board that reads it, and to arithmetic that must not drift.

The panel degrades quietly by design -- `history()` hides it on any throw -- so a
renamed field does not fail, it shows an empty box. That is the failure this file
exists to catch, from both sides: every name index.html reads has to be in the
published file, and the file has to carry nothing less than the contract.
"""
import json
import pathlib
import re
import sys

import polars as pl
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import stats  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BOARD = WEB / "index.html"

# architecture.md 2, the published contract. Extra keys are fine (invariant 5:
# schema changes are additive); missing ones are not.
CONTRACT = {"as_of", "window_days", "caught", "of", "mean_platform_wait_s",
            "mean_door_to_train_s", "by_lead"}
BIN_CONTRACT = {"bin", "n", "p10", "p50", "p90"}


def _history_source() -> str:
    """The body of index.html's history(), which is the only reader of stats.json."""
    src = BOARD.read_text()
    i = src.index("async function history()")
    # The function is at top level, so the first line-initial "}" closes it.
    j = src.index("\n}", i)
    return src[i:j]


def _fields_read(var: str) -> set[str]:
    return set(re.findall(rf"\b{var}\.(\w+)", _history_source()))


def test_board_reads_stats_json_by_fetch():
    """If the board stops fetching it, this whole file is pinning nothing."""
    assert 'fetch("stats.json"' in _history_source()


def test_every_field_the_board_reads_is_published():
    """The two-sided pin: rename a field on either side and this fails.

    Keyed off index.html itself rather than a list typed twice, so the test cannot
    agree with a stale copy of the contract.
    """
    want = _fields_read("d")
    assert len(want) >= 7, f"failed to parse history(); got only {want}"
    got = set(json.loads((WEB / "stats.json").read_text()))
    assert want <= got, f"index.html reads fields stats.json does not have: {sorted(want - got)}"


def test_every_bin_field_the_board_reads_is_published():
    want = _fields_read("b")
    assert len(want) >= 5, f"failed to parse the by_lead loop; got only {want}"
    bins = json.loads((WEB / "stats.json").read_text())["by_lead"]
    assert bins, "no lead bins published"
    for b in bins:
        assert want <= set(b), f"bin {b.get('bin')} is missing {sorted(want - set(b))}"


def test_published_stats_satisfies_the_contract():
    d = json.loads((WEB / "stats.json").read_text())
    assert CONTRACT <= set(d), f"stats.json is missing {sorted(CONTRACT - set(d))}"
    for b in d["by_lead"]:
        assert BIN_CONTRACT <= set(b)
    assert d["of"] >= d["caught"] >= 0
    assert d["window_days"] >= 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d["as_of"])


def test_web_stats_matches_data_stats():
    """Same guard as web/model.json: a stale published copy is a silent fork."""
    assert json.loads((WEB / "stats.json").read_text()) == \
        json.loads((ROOT / "data" / "stats.json").read_text()), \
        "web/stats.json is out of date: re-run src/stats.py"


def test_quantiles_are_ordered_and_binned_by_lead():
    for b in json.loads((WEB / "stats.json").read_text())["by_lead"]:
        assert b["p10"] <= b["p50"] <= b["p90"], b
        assert b["n"] > 0


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


def test_the_board_decodes_the_absent_sentinel():
    """-1 only helps if the reader knows what it means.

    `(x/60).toFixed(1)` renders -1 as "-0.0 min", which reads as a real measurement
    of almost nothing rather than as no data. The panel has to branch on it.
    """
    src = _history_source()
    assert "< 0" in src or "<0" in src, \
        "history() does not guard the negative sentinel; -1 renders as -0.0 min"


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
