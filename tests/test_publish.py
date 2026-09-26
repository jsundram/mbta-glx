"""Pin the publisher to the contract it exists to enforce.

architecture.md M3: a refit produces `data/model.json`, `web/model.json` and
`web/model.js`, and "a publisher that carries only the first leaves the board
predicting from the previous rating with nothing to show it." So the tests that
matter here are the ones that fail when an artifact is dropped, not the ones that
confirm a copy succeeded.
"""
import json
import pathlib
import re
import shutil
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import publish  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


@pytest.fixture
def origin(tmp_path, monkeypatch):
    """A throwaway copy of web/, so staleness can be injected without touching it."""
    dest = tmp_path / "web"
    shutil.copytree(WEB, dest)
    monkeypatch.setattr(publish, "WEB", dest)
    return dest


# --- the manifest is the published set, and it has to be complete ---

def test_the_manifest_carries_all_three_refit_artifacts():
    """The named M3 failure: publishing data/model.json and stopping there."""
    from_model = [a.name for a in publish.MANIFEST
                  if a.source == publish.DATA / "model.json"]
    assert from_model == ["model.json", "model.js"], (
        "a refit publishes model.json AND model.js; the manifest lost one")
    js = next(a for a in publish.MANIFEST if a.name == "model.js")
    assert js.script, "model.js must be published script-wrapped, not as raw JSON"


def test_the_manifest_covers_every_asset_the_board_asks_for():
    """Two-sided: add a fetched asset without adding it here and this fails.

    An asset the board requests but the origin never publishes is a 404 the board
    swallows -- stats.json hides its panel and model.js falls back. Neither fails
    loudly, so the completeness of the origin cannot be checked in a browser.

    The skip set is no longer in this set at all: it is an absolute URL published
    in model.json's constants, so it is another origin's problem and the filter
    below drops it. tests/test_regressions.py is what polices that host.
    """
    src = (WEB / "index.html").read_text() + (WEB / "app.js").read_text()
    asked = set(re.findall(r'fetch\("([\w.-]+)"', src))
    asked |= set(re.findall(r'\.src = "([\w.-]+)"', src))
    # Absolute URLs are other people's origins (api-v3.mbta.com, ntfy.sh).
    asked = {a for a in asked if not a.startswith("http")}
    published = {a.name for a in publish.MANIFEST}
    assert asked <= published, f"the board asks for unpublished assets: {sorted(asked - published)}"


def test_the_board_itself_is_published():
    """web/ is the origin, so the page and its port are part of the set."""
    assert {"index.html", "app.js"} <= {a.name for a in publish.MANIFEST}


def test_every_derived_artifact_names_how_to_rebuild_it():
    """A `--check` failure has to tell the operator which script to run."""
    for a in publish.MANIFEST:
        if a.source is not None:
            assert a.name in publish.FIX, f"{a.name} has no repair hint"


# --- staleness has to be detected, not assumed ---

def test_a_stale_model_script_is_caught(origin):
    js = next(a for a in publish.MANIFEST if a.name == "model.js")
    assert js.state() == "ok"
    (origin / "model.js").write_text(publish.as_script('{"grid": [0.5]}'))
    assert js.state() == "stale"
    assert [a.name for a, _ in publish.check()] == ["model.js"]


def test_a_stale_model_json_is_caught(origin):
    (origin / "model.json").write_text('{"grid": [0.5]}')
    assert [a.name for a, _ in publish.check()] == ["model.json"]


def test_a_stale_stats_json_is_caught(origin):
    (origin / "stats.json").write_text('{"as_of": "1999-01-01"}')
    assert [a.name for a, _ in publish.check()] == ["stats.json"]


@pytest.mark.parametrize("name", ["index.html", "app.js", "model.json", "model.js",
                                  "stats.json"])
def test_a_missing_artifact_is_caught(origin, name):
    (origin / name).unlink()
    assert [(a.name, s) for a, s in publish.check()] == [(name, "missing")]


def test_a_model_script_in_the_wrong_shape_is_caught(origin):
    """app.js reads Magoun._modelText; anything else is a silent empty model."""
    (origin / "model.js").write_text("window.model = {};\n")
    assert [a.name for a, _ in publish.check()] == ["model.js"]
    assert publish.script_text((origin / "model.js").read_text()) is None


# --- publishing repairs the origin, and only the origin ---

def test_publish_repairs_a_stale_web_copy(origin):
    (origin / "model.json").write_text('{"grid": [0.5]}')
    (origin / "model.js").write_text("window.model = {};\n")
    assert sorted(publish.publish()) == ["model.js", "model.json"]
    assert publish.check() == []
    assert json.loads((origin / "model.json").read_text()) == \
        json.loads((publish.DATA / "model.json").read_text())


def test_publish_is_idempotent(origin):
    assert publish.publish() == []
    assert publish.check() == []


def test_dry_run_writes_nothing(origin):
    (origin / "model.json").write_text('{"grid": [0.5]}')
    assert publish.publish(dry_run=True) == ["model.json"]
    assert (origin / "model.json").read_text() == '{"grid": [0.5]}'


def test_staging_copies_the_whole_set(origin, tmp_path):
    names = publish.stage(tmp_path / "elsewhere")
    assert names == [a.name for a in publish.MANIFEST]
    for a in publish.MANIFEST:
        assert (tmp_path / "elsewhere" / a.name).read_bytes() == a.path.read_bytes()


def test_staging_refuses_to_overwrite_the_source(origin):
    with pytest.raises(SystemExit):
        publish.stage(origin)
    with pytest.raises(SystemExit):
        publish.stage(origin / "nested")


# --- the script wrapper is a third copy of the model, so it is checked both ways ---

def test_script_wrapper_round_trips():
    text = json.dumps({"grid": [0.02, 0.5], "n_legs": 3})
    assert publish.script_text(publish.as_script(text)) == text


def test_script_wrapper_matches_the_one_fit_writes():
    """Two copies of the same wrapper. If they drift, model.js stops loading."""
    import fit
    text = (publish.DATA / "model.json").read_text()
    assert publish.as_script(text) == fit._as_script(text)


def test_the_published_script_is_the_published_json(origin):
    """The same assertion test_contract makes, from the publisher's side."""
    got = publish.script_text((origin / "model.js").read_text())
    assert json.loads(got) == json.loads((origin / "model.json").read_text())


# --- the publisher moves files; it must not become a second pipeline ---

def test_publish_does_not_derive_anything():
    """Fitting or scoring inside the publish path is how the origin forks.

    architecture.md 3: anything that fits, simulates or scores stays in Python and
    never ships. The publisher is the one step that runs on the way out, so it is
    the one most tempting to make clever.
    """
    src = (ROOT / "src" / "publish.py").read_text()
    for bad in ("import polars", "import numpy", "import service", "import simulate",
                "import replay", "import stats", "fit.main", "stats.main"):
        assert bad not in src, f"publish.py reaches for {bad}"


def test_publish_never_pushes():
    """Ask before anything that reaches a remote. --commit stops at a local commit."""
    src = (ROOT / "src" / "publish.py").read_text()
    assert '"push"' not in src and "git push" not in src
    assert "never pushes" in src


# --- --commit, in a throwaway repo so the real history stays out of it ---

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with a data/ and a web/, standing in for ROOT."""
    import subprocess
    for d in ("data", "web"):
        (tmp_path / d).mkdir()
    (tmp_path / "data" / "model.json").write_text(json.dumps({"n_legs": 7, "days": 2}))
    (tmp_path / "data" / "stats.json").write_text(
        json.dumps({"window_days": 2, "as_of": "2026-09-25"}))
    (tmp_path / "web" / "model.json").write_text("first")
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)
    monkeypatch.setattr(publish, "ROOT", tmp_path)
    monkeypatch.setattr(publish, "DATA", tmp_path / "data")
    monkeypatch.setattr(publish, "WEB", tmp_path / "web")
    return tmp_path


def _log(repo):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "log", "--oneline"],
                          capture_output=True, text=True, check=True).stdout.split("\n")


def test_commit_records_the_changed_artifacts(repo):
    import subprocess
    (repo / "web" / "model.json").write_text("second")
    assert publish.commit([repo / "web" / "model.json", repo / "data" / "model.json"])
    files = subprocess.run(["git", "-C", str(repo), "show", "--stat", "--format=%s"],
                           capture_output=True, text=True, check=True).stdout
    assert "web/model.json" in files
    assert "Publish model (7 legs over 2 days) and stats (2d to 2026-09-25)" in files
    assert len([l for l in _log(repo) if l]) == 2


def test_commit_is_a_no_op_when_nothing_moved(repo):
    assert publish.commit([repo / "web" / "model.json"]) is False
    assert len([l for l in _log(repo) if l]) == 1


def test_commit_bails_when_no_artifact_exists(repo):
    """An empty pathspec inverts the scoping instead of narrowing it.

    `git add` with no paths is a no-op, `git diff --cached --` then lists the whole
    index, and `git commit --` commits it -- the exact sweep the scoping prevents.
    """
    import subprocess
    (repo / "notes.md").write_text("mine")
    subprocess.run(["git", "-C", str(repo), "add", "notes.md"], check=True)
    assert publish.commit([repo / "web" / "does-not-exist.json"]) is False
    assert len([l for l in _log(repo) if l]) == 1, "committed with an empty pathspec"


def test_commit_leaves_unrelated_staged_work_alone(repo):
    """Publishing must not sweep whatever else you had staged into its commit."""
    import subprocess
    (repo / "notes.md").write_text("mine")
    subprocess.run(["git", "-C", str(repo), "add", "notes.md"], check=True)
    (repo / "web" / "model.json").write_text("second")
    assert publish.commit([repo / "web" / "model.json"])
    shown = subprocess.run(["git", "-C", str(repo), "show", "--stat", "--format="],
                           capture_output=True, text=True, check=True).stdout
    assert "web/model.json" in shown
    assert "notes.md" not in shown, "publish swept an unrelated staged file into its commit"
