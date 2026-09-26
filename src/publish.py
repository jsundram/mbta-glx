"""Move the published artifacts to the static origin, and refuse to move a partial set.

The origin is `web/` in this repo: a GitHub Actions workflow uploads that directory
to Pages as-is, so there is no second copy of index.html or app.js to fall behind.
What can still fall behind is the derived half of the directory, and that is the
failure this file exists to prevent.

**A refit publishes three artifacts, not one.** `fit.py` writes `data/model.json`,
`web/model.json` and `web/model.js` -- the last because a board opened as `file://`
cannot fetch a sibling file at all (Chromium: `URL scheme "file" is not supported`),
so the model has to be reachable as a script too. A publisher that carries only the
first leaves the board predicting from the previous rating with nothing on screen to
say so. So the manifest below is explicit, every derived entry names the file it
must equal, and `--check` fails on any one of them.

This moves files. It never derives them: a missing `data/model.json` is an error
telling you to run `fit.py`, not an invitation to fit. Fitting, scoring and
simulating stay out of the publish path entirely.

Usage:
  python src/publish.py --check        # verify the origin, write nothing
  python src/publish.py               # refresh web/ from data/, report what moved
  python src/publish.py --to DIR      # also stage a copy for some other origin
  python src/publish.py --commit      # commit what moved; never pushes
"""
import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = ROOT / "web"


def as_script(text: str) -> str:
    """The same bytes as model.json, reachable from file:// where fetch() is not.

    Must stay byte-identical to fit.py's `_as_script`; `script_text` is its inverse
    and the round-trip is tested both ways.
    """
    return ("globalThis.Magoun = globalThis.Magoun || {};\n"
            "Magoun._modelText = " + json.dumps(text) + ";\n")


def script_text(js: str) -> str | None:
    """Pull the model back out of model.js, or None if it is not in app.js's shape."""
    m = re.search(r"Magoun\._modelText = (\".*\");", js, re.S)
    return json.loads(m.group(1)) if m else None


class Artifact:
    """One file served from the static origin.

    `source` is the canonical copy under data/ that the published file must equal.
    `None` means the file is authored in web/ and published as it stands.
    """

    def __init__(self, name: str, source: pathlib.Path | None = None,
                 script: bool = False, why: str = ""):
        self.name, self.source, self.script, self.why = name, source, script, why

    @property
    def path(self) -> pathlib.Path:
        return WEB / self.name

    def wanted(self) -> str:
        """What the published file should contain, given its source."""
        assert self.source is not None
        text = self.source.read_text()
        return as_script(text) if self.script else text

    def state(self) -> str:
        """`ok`, `stale`, `missing`, or `no source` -- never a silent pass."""
        if self.source is not None and not self.source.exists():
            return "no source"
        if not self.path.exists():
            return "missing"
        if self.source is None:
            return "ok"
        return "ok" if self.path.read_text() == self.wanted() else "stale"


# The published set, in the order the board needs it. Adding a file the board
# fetches without adding it here is how an origin goes out incomplete.
MANIFEST = [
    Artifact("index.html", why="the board"),
    Artifact("app.js", why="the port of compute_rows"),
    Artifact("model.json", DATA / "model.json", why="fitted quantiles, fetched"),
    Artifact("model.js", DATA / "model.json", script=True,
             why="the same bytes as a script, for file://"),
    Artifact("stats.json", DATA / "stats.json", why="the self-score"),
]
FIX = {"model.json": "src/fit.py", "model.js": "src/fit.py",
       "stats.json": "src/stats.py"}


def check() -> list[tuple[Artifact, str]]:
    """Every artifact whose state is not `ok`."""
    return [(a, s) for a in MANIFEST if (s := a.state()) != "ok"]


def publish(dry_run: bool = False) -> list[str]:
    """Bring the origin up to date with data/. Returns the names that moved."""
    moved = []
    for a in MANIFEST:
        if a.source is None:
            continue
        if not a.source.exists():
            continue
        want = a.wanted()
        if not a.path.exists() or a.path.read_text() != want:
            if not dry_run:
                a.path.write_text(want)
            moved.append(a.name)
    return moved


def stage(dest: pathlib.Path) -> list[str]:
    """Copy the published set somewhere else, for an origin that is not web/."""
    dest = dest.resolve()
    if dest == WEB or WEB in dest.parents or dest in WEB.parents:
        sys.exit(f"--to {dest} overlaps the source directory {WEB}")
    dest.mkdir(parents=True, exist_ok=True)
    for a in MANIFEST:
        shutil.copy2(a.path, dest / a.name)
    return [a.name for a in MANIFEST]


def commit(paths: list[pathlib.Path]) -> bool:
    """Commit the published artifacts locally. Never pushes: that is your call."""
    rel = [str(p.relative_to(ROOT)) for p in paths if p.exists()]
    if not rel:
        # An empty pathspec inverts every guard below: `git add` becomes a no-op,
        # `git diff --cached --` lists the whole index, and `git commit --` commits
        # it. That is exactly the sweep the scoping is meant to prevent.
        print("nothing to commit; none of the published artifacts exist")
        return False
    subprocess.run(["git", "-C", str(ROOT), "add", *rel], check=True)
    # Scope both the message and the commit to our own paths. Reading the whole
    # index would sweep whatever else you had staged into a commit labelled
    # "Publish model", and committing without a pathspec would commit it too.
    staged = subprocess.run(["git", "-C", str(ROOT), "diff", "--cached",
                             "--name-only", "--", *rel],
                            capture_output=True, text=True, check=True).stdout.split()
    if not staged:
        print("nothing to commit; the origin already matches data/")
        return False
    model = json.loads((DATA / "model.json").read_text())
    stats = (json.loads((DATA / "stats.json").read_text())
             if (DATA / "stats.json").exists() else {})
    msg = (f"Publish model ({model['n_legs']} legs over {model['days']} days)"
           + (f" and stats ({stats['window_days']}d to {stats['as_of']})"
              if stats else "")
           + "\n\n" + "\n".join(f"  {p}" for p in staged) + "\n")
    subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m", msg, "--", *rel],
                   check=True)
    print("committed:\n" + "\n".join(f"  {p}" for p in staged))
    print("not pushed -- push when you are ready, and Pages deploys web/")
    return True


def report() -> None:
    for a in MANIFEST:
        s = a.state()
        size = f"{a.path.stat().st_size / 1024:7.1f} KB" if a.path.exists() else "  absent"
        print(f"  {a.name:12s} {size}  {s:9s} {a.why}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify the origin and write nothing")
    ap.add_argument("--to", type=pathlib.Path, metavar="DIR",
                    help="also stage the published set into DIR")
    ap.add_argument("--commit", action="store_true",
                    help="commit what moved (never pushes)")
    a = ap.parse_args()

    if a.check:
        bad = check()
        print(f"static origin {WEB}")
        report()
        if bad:
            print("\nthe origin is not publishable:")
            for art, s in bad:
                fix = FIX.get(art.name, "add the file to web/")
                print(f"  {art.name}: {s} -- run {fix}")
            sys.exit(1)
        print("\nall five artifacts present and current")
        return

    moved = publish()
    bad = check()
    if bad:
        # A stale artifact publish() could not fix means its source is missing.
        # Refuse: a partial origin is the failure mode this file exists for.
        print(f"static origin {WEB}")
        report()
        print("\nrefusing to publish a partial set:")
        for art, s in bad:
            print(f"  {art.name}: {s} -- run {FIX.get(art.name, 'add it to web/')}")
        sys.exit(1)

    print(f"static origin {WEB}")
    report()
    print("\n" + (f"moved: {', '.join(moved)}" if moved
                  else "nothing moved; the origin already matched data/"))
    if a.to:
        names = stage(a.to)
        print(f"staged {len(names)} artifacts into {a.to}")
    if a.commit:
        commit([art.path for art in MANIFEST]
               + [DATA / "model.json", DATA / "stats.json", DATA / "scores.jsonl"])


if __name__ == "__main__":
    main()
