"""Pin the refit diffs, because they are what stands between a rating change and a
quietly weakened contract test.

The dangerous refit is not the one that fails the suite -- that one gets read. It is
the one where the fixtures are regenerated, the suite goes green, and one fewer tier
is covered than before. `fixture_diff` has to shout about that specific case.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import refit  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
GRID = [round(0.02 * i, 3) for i in range(1, 50)]


def _model(**over):
    m = {"grid": list(GRID), "n_legs": 100, "days": 35,
         "headway_median_s": 528.0,
         "tiers": {"ball_dep": {"n": 10, "q": [float(i) for i in range(49)]},
                   "med_dep": {"n": 10, "q": [float(i) for i in range(49)]}},
         "sched": {"n": 10, "q": [float(i) for i in range(49)]},
         "berth": {"n": 10, "q": [float(i) for i in range(49)]},
         "rides": {"70199": {"name": "Park Street", "n": 10,
                             "q": [float(i) for i in range(49)]}},
         "constants": {"veto_window_s": 480}}
    m.update(over)
    return m


# --- model_diff ---

def test_an_unchanged_model_reports_nothing():
    """The refit is a no-op today; it has to say so rather than invent movement."""
    assert refit.model_diff(_model(), _model()) == []


def test_the_real_model_is_a_no_op_against_itself():
    m = json.loads((ROOT / "data" / "model.json").read_text())
    assert refit.model_diff(m, m) == []


def test_a_thirty_second_schedule_shift_is_reported():
    """The exact injection the fixtures are sensitive to: all 28 cases fail on it."""
    after = _model()
    after["sched"] = {"n": 10, "q": [q + 30 for q in after["sched"]["q"]]}
    out = "\n".join(refit.model_diff(_model(), after))
    assert "sched" in out
    assert "+30.0s" in out


def test_a_sub_second_move_is_not_noise():
    """Quantiles are seconds; 0.1 s of float drift is not a rating change."""
    after = _model()
    after["sched"] = {"n": 10, "q": [q + 0.1 for q in after["sched"]["q"]]}
    assert refit.model_diff(_model(), after) == []


def test_a_removed_tier_is_called_out():
    after = _model()
    del after["tiers"]["med_dep"]
    out = "\n".join(refit.model_diff(_model(), after))
    assert "med_dep" in out and "REMOVED" in out


def test_a_changed_grid_is_called_out():
    """Every consumer indexes the quantile arrays by this grid."""
    after = _model(grid=[round(0.05 * i, 3) for i in range(1, 20)])
    assert any("GRID CHANGED" in l for l in refit.model_diff(_model(), after))


def _regridded(n):
    """A model whose quantile arrays are as long as its grid."""
    grid = [round(i / (n + 1), 4) for i in range(1, n + 1)]
    band = {"n": 10, "q": [float(i) for i in range(n)]}
    return {"grid": grid, "tiers": {"ball_dep": dict(band)}, "sched": dict(band),
            "berth": dict(band), "rides": {}}


@pytest.mark.parametrize("before_n,after_n", [(19, 49), (49, 19)])
def test_a_regrid_diffs_instead_of_crashing(before_n, after_n):
    """Each side has to be indexed by its OWN grid.

    Looking both arrays up through the new grid raises IndexError as soon as the grid
    gets finer, and because that aborts model_diff the refit that most needs reading
    prints a traceback instead of a diff.
    """
    lines = refit.model_diff(_regridded(before_n), _regridded(after_n))
    assert any("GRID CHANGED" in l for l in lines)
    assert any("regridded" in l for l in lines), \
        "an element-wise worst-case across different grids is meaningless"


def test_scalar_and_constant_changes_are_reported():
    out = "\n".join(refit.model_diff(_model(), _model(days=42)))
    assert "days" in out and "35 -> 42" in out
    after = _model()
    after["constants"] = {"veto_window_s": 900}
    assert any("veto_window_s" in l for l in refit.model_diff(_model(), after))


def test_a_changed_sample_size_alone_is_reported():
    """More history with identical quantiles is still a refit worth seeing."""
    after = _model()
    after["sched"] = {"n": 4000, "q": list(after["sched"]["q"])}
    assert any("sched" in l for l in refit.model_diff(_model(), after))


# --- fixture_diff ---

def _row(source="mbta", **over):
    r = {"eta": 1000.0, "lo": 900.0, "hi": 1100.0, "leave_in": 10.0,
         "source": source, "vehicle": "G-1", "backed": True, "skipped": False,
         "catchable": True}
    r.update(over)
    return r


def _cases(sources_per_case):
    return {"cases-2026-09-24.json":
            [{"expected": [_row(s) for s in srcs]} for srcs in sources_per_case]}


def test_identical_fixtures_report_nothing():
    a = _cases([["mbta", "schedule"]])
    assert refit.fixture_diff(a, a) == []


def test_a_tier_that_stopped_appearing_is_shouted_about():
    """The silent failure: regenerate, go green, cover one fewer source.

    architecture.md M1 keeps a test that all six sources appear. This says it in the
    diff too, at the moment the human is deciding whether to accept the refit.
    """
    before = _cases([["mbta", "schedule"], ["berthed at Medford/Tufts"]])
    after = _cases([["mbta", "schedule"], ["mbta"]])
    out = "\n".join(refit.fixture_diff(before, after))
    assert "A TIER STOPPED APPEARING" in out
    assert "berthed at Medford/Tufts" in out
    assert "Do not accept this" in out


def test_a_new_tier_is_noted_but_not_shouted():
    before = _cases([["mbta"]])
    after = _cases([["not stopping here"]])
    out = "\n".join(refit.fixture_diff(before, after))
    assert "new source" in out
    assert "A TIER STOPPED APPEARING" in out, "mbta also vanished here"


def test_a_shrinking_case_count_is_called_out():
    """Fewer cases is less coverage, whatever the reason."""
    before = _cases([["mbta"], ["mbta"], ["mbta"]])
    after = _cases([["mbta"]])
    out = "\n".join(refit.fixture_diff(before, after))
    assert "CASE COUNT 3 -> 1" in out and "less coverage" in out


def test_a_dropped_row_inside_a_case_is_called_out():
    """Measured for real: a +30 s schedule shift took two cases from 6 rows to 5."""
    before = _cases([["mbta", "schedule"]])
    after = _cases([["mbta"]])
    out = "\n".join(refit.fixture_diff(before, after))
    assert "2 rows -> 1" in out


def test_numeric_moves_report_the_worst_field():
    before = _cases([["mbta"]])
    after = {"cases-2026-09-24.json": [{"expected": [_row(eta=1030.0, lo=930.0)]}]}
    out = "\n".join(refit.fixture_diff(before, after))
    assert "eta" in out and "+30.0" in out
    assert "1 of 1 cases changed" in out


def test_a_flipped_boolean_is_listed_not_averaged():
    """`skipped` and `backed` are decisions; a mean of them would say nothing."""
    before = _cases([["mbta"]])
    after = {"cases-2026-09-24.json": [{"expected": [_row(skipped=True)]}]}
    out = "\n".join(refit.fixture_diff(before, after))
    assert "skipped False -> True" in out


def test_a_vanished_field_is_listed():
    before = _cases([["mbta"]])
    row = _row()
    del row["vehicle"]
    after = {"cases-2026-09-24.json": [{"expected": [row]}]}
    out = "\n".join(refit.fixture_diff(before, after))
    assert "vehicle" in out


def test_the_real_fixtures_are_a_no_op_against_themselves():
    got = refit._cases(ROOT / "tests" / "fixtures")
    assert sum(len(v) for v in got.values()) == 28, "the fixture set changed size"
    assert refit.fixture_diff(got, got) == []


def test_every_source_the_board_can_emit_appears_in_the_real_fixtures():
    """Same assertion M1 makes, restated here so the diff's baseline is honest."""
    got = refit._sources(refit._cases(ROOT / "tests" / "fixtures"))
    assert len(got) >= 6, f"only {sorted(got)} are covered"


# --- the procedure itself ---

def test_refit_never_regenerates_fixtures_without_being_asked():
    """The one rule: fixtures are not regenerated to make a failure go away."""
    sh = (ROOT / "src" / "refit.sh").read_text()
    phase1 = sh.split('if [ "${1:-}" = "--fixtures" ]; then')[0]
    body = sh.split("exit 0\nfi\n", 1)[1]
    assert "make_fixtures" not in phase1
    assert "make_fixtures" not in body, \
        "the default refit path regenerates fixtures; that has to be a separate run"
    assert "Do NOT regenerate fixtures" in body


def test_refit_rebuilds_the_dataset_not_just_the_fit():
    """fit.py reads data/magoun.parquet, which nothing else schedules."""
    sh = (ROOT / "src" / "refit.sh").read_text()
    assert "build_dataset.py" in sh
    assert sh.index("build_dataset.py") < sh.index("src/fit.py")


def test_refit_checks_the_published_set():
    assert "publish.py --check" in (ROOT / "src" / "refit.sh").read_text()
