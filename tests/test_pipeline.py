"""The publish pipeline's wiring: the rating watch, and the workflows that run it.

Nothing here touches the network. `rating.parse` takes text for exactly that reason,
and the workflow assertions are plain string checks on the YAML rather than a parse,
so the suite keeps running under `--with pytest --with numpy --with polars` and
nothing has to learn a new dependency to stay green.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import rating  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# Real rows from cdn.mbta.com/archive/archived_feeds.txt, newest first. The middle
# field of feed_version is a build timestamp and moves on every republish; the rows
# below are three republishes of one rating followed by the previous one.
FEEDS = '''feed_start_date,feed_end_date,feed_version,archive_url,archive_note
20260917,20261212,"Fall 2026, 2026-09-24T17:51:42+00:00, version D",https://cdn.mbtace.com/archive/20260917.zip,
20260916,20260916,"Fall 2026, 2026-09-23T18:54:01+00:00, version D",https://cdn.mbtace.com/archive/20260916.zip,
20260914,20260915,"Fall 2026, 2026-09-21T21:08:52+00:00, version D",https://cdn.mbtace.com/archive/20260914.zip,
20260825,20260908,"Summer 2026, 2026-08-22T19:02:11+00:00, version D",https://cdn.mbtace.com/archive/20260825.zip,
'''


# --- what identifies a rating ---

def test_the_current_rating_is_the_first_row():
    r = rating.parse(FEEDS)
    assert r["season"] == "Fall 2026"
    assert r["version"] == "version D"
    assert r["feed_end_date"] == "20261212"


def test_the_build_timestamp_is_not_part_of_the_identity():
    """1016 rows, 1016 distinct start dates: republishing is not a rating change.

    Watching feed_start_date or the whole feed_version string would fire weekly and
    mean nothing, and a watcher that cries wolf is a watcher nobody reads.
    """
    republished = FEEDS.replace("2026-09-24T17:51:42+00:00",
                                "2026-10-01T09:00:00+00:00").replace(
                                    "20260917,20261212", "20260924,20261212")
    before, after = rating.parse(FEEDS), rating.parse(republished)
    assert after["feed_start_date"] != before["feed_start_date"]
    assert after["feed_version"] != before["feed_version"]
    assert rating.changed(before, after) == []


def test_a_new_season_is_a_rating_change():
    before = rating.parse(FEEDS)
    after = rating.parse(FEEDS.replace("Fall 2026, 2026-09-24", "Winter 2027, 2026-12-12"))
    moved = rating.changed(before, after)
    assert any("season" in m for m in moved)


def test_a_version_bump_within_a_season_is_a_rating_change():
    """A revision inside a season still moves the timetable this is all fitted to."""
    before = rating.parse(FEEDS)
    after = rating.parse(FEEDS.replace("version D\"", "version E\"", 1))
    assert any("version" in m for m in rating.changed(before, after))


def test_a_moved_end_date_is_a_rating_change():
    """The end date is how 2026-12-12 is known at all; it is not typed in anywhere."""
    before = rating.parse(FEEDS)
    after = rating.parse(FEEDS.replace("20261212", "20270102", 1))
    assert any("feed_end_date" in m for m in rating.changed(before, after))


def test_nothing_recorded_yet_counts_as_moved():
    assert rating.changed(None, rating.parse(FEEDS)) == ["nothing recorded yet"]


def test_an_empty_feed_index_is_an_error_not_an_empty_rating():
    """A silent empty parse would record a blank baseline and never fire again."""
    with pytest.raises(ValueError):
        rating.parse("feed_start_date,feed_end_date,feed_version\n")


def test_the_recorded_baseline_is_a_real_rating():
    r = rating.recorded()
    assert r is not None, "data/rating.json is missing: run src/rating.py --record"
    assert set(rating.KEYS) <= set(r)
    assert r["feed_end_date"].isdigit() and len(r["feed_end_date"]) == 8


def test_parse_reaches_for_no_network():
    import inspect
    src = inspect.getsource(rating.parse) + inspect.getsource(rating.changed)
    assert "urlopen" not in src and "requests" not in src


# --- the workflows ---

def _wf(name: str) -> str:
    p = WORKFLOWS / name
    assert p.exists(), f"{name} is missing"
    return p.read_text()


def _steps(name: str) -> str:
    """The workflow with its comments removed.

    The comments explain the traps -- "not `... | tee`", "no docs/ copy" -- so a
    "must not contain" assertion against the raw text matches the explanation and
    fails on a correct file.
    """
    return "\n".join(l for l in _wf(name).splitlines()
                      if not l.lstrip().startswith("#"))


def test_the_suite_workflow_runs_the_whole_suite():
    s = _wf("suite.yml")
    assert "python -m pytest tests/ -q" in s
    assert "setup-node" in s, "no node means the JS port is compared to nothing"


def test_the_suite_workflow_sets_ci_so_a_missing_node_fails():
    """Pairs with test_contract.test_the_javascript_side_actually_runs_here."""
    assert 'CI: "1"' in _wf("suite.yml")


def test_the_suite_workflow_has_a_weekly_drift_run():
    assert "schedule:" in _wf("suite.yml")


def test_pages_deploys_the_web_directory_itself():
    """web/ is the origin. A second copy of board.html is a copy that can drift."""
    s = _steps("pages.yml")
    assert "upload-pages-artifact" in s
    assert "path: web" in s
    assert "docs" not in s


def test_pages_refuses_a_partial_origin_before_uploading():
    """The gate that makes "nobody copied it by hand" true rather than hoped for."""
    s = _wf("pages.yml")
    assert "src/publish.py --check" in s
    assert s.index("src/publish.py --check") < s.index("upload-pages-artifact")


def test_pages_asks_for_no_secret():
    assert "${{ secrets." not in _steps("pages.yml")


def test_the_rating_workflow_only_detects():
    """The refit needs data/raw, which is gitignored. CI cannot do it."""
    assert "src/rating.py --check" in _steps("rating.yml")
    assert "refit.sh" not in _steps("rating.yml").split("jobs:")[1], \
        "the workflow must not try to refit; the archive is not in the repo"
    assert "refit.sh" in _wf("rating.yml"), "it should still say where the refit runs"


def test_the_rating_workflow_reads_the_scripts_exit_code_not_tees():
    """`... | tee f` makes $? tee's status, always 0, and the issue never opens."""
    s = _steps("rating.yml")
    assert "rating.py --check > rating.txt" in s
    assert "| tee" not in s


def test_the_rating_workflow_can_actually_open_the_first_issue():
    """`jq '.[0].number'` prints the literal "null" for an empty list.

    `[ -n "null" ]` is true, so the very first rating change would run
    `gh issue comment null`, fail, and never create the issue -- leaving a red
    workflow as the only signal that every fitted constant is now wrong.
    """
    s = _steps("rating.yml")
    assert "number // empty" in s, "an empty issue list yields \"null\", not \"\""


def test_the_rating_workflow_uses_only_the_builtin_token():
    assert "secrets.GITHUB_TOKEN" in _wf("rating.yml")
    assert len([l for l in _wf("rating.yml").splitlines()
                if "secrets." in l and "GITHUB_TOKEN" not in l]) == 0


def test_no_workflow_pretends_to_roll_up_the_archive():
    """data/live, data/pairs and data/raw are gitignored; a workflow has no archive.

    A scheduled job that "rolls up" an empty directory succeeds every night and
    produces nothing, which is worse than not having it.
    """
    for f in sorted(WORKFLOWS.glob("*.yml")):
        body = _steps(f.name).split("jobs:")[1]
        for script in ("rollup.py", "stats.py", "fit.py", "build_dataset.py",
                       "make_fixtures.py"):
            assert script not in body, f"{f.name} runs {script}, which needs the archive"


def test_the_archive_really_is_out_of_the_repo():
    """The premise of the two tests above, checked rather than assumed."""
    ignored = (ROOT / ".gitignore").read_text()
    for d in ("data/live/", "data/pairs/", "data/raw/"):
        assert d in ignored


def test_the_published_artifacts_really_are_in_the_repo():
    """The other half of the premise: Pages can only deploy committed files."""
    import subprocess
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                             capture_output=True, text=True, check=True).stdout.split()
    for f in ("web/board.html", "web/app.js", "web/model.json", "web/model.js",
              "data/model.json"):
        assert f in tracked, f"{f} is not committed; Pages would deploy without it"
