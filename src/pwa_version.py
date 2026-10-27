"""Move web/sw.js's version whenever a file it precaches moves, so phones get the change.

The service worker serves the board from its own cache, and only a new `V` makes it
fetch the shell again. pwa-starter's answer is "bump V on every shell change" plus a
pre-commit lint. Here the shell also changes with nobody editing it -- `fit.py`
rewrites web/model.js on every refit -- so the bump is a function both writers call,
and the suite is the guard rather than a hook: tests/test_pwa.py fails when the shell
moved and `V` did not, which is what catches a hand edit to index.html.

How it knows: sw.js carries a `SHELL FINGERPRINT` line, a hash of every SHELL file as
of the last bump. A mismatch means bump. pwa.js's APP_V is part of the bump, so it is
blanked out of the hash -- otherwise writing it would move the fingerprint it records.

    uv run python src/pwa_version.py            # bump if the shell moved
    uv run python src/pwa_version.py --check    # exit 1 if it moved and V did not
"""
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

V_RE = re.compile(r'const V\s*=\s*"([^"]*)"')
APP_V_RE = re.compile(r'const APP_V\s*=\s*"([^"]*)"')
STAMP_RE = re.compile(r"// SHELL FINGERPRINT: (\S+)")


def shell_entries(sw_src: str) -> list[str]:
    """The SHELL list's string literals, as written ("./", "./index.html", ...)."""
    m = re.search(r"const SHELL\s*=\s*\[(.*?)\]", sw_src, re.S)
    if not m:
        raise ValueError("no `const SHELL = [...]` in sw.js")
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return re.findall(r'"([^"]+)"', body)


def shell_files(web: pathlib.Path) -> list[str]:
    """SHELL as file names under web/. "./" is index.html, so it is counted once."""
    names = set()
    for e in shell_entries((web / "sw.js").read_text()):
        p = e.removeprefix("./")
        names.add(p + "index.html" if p == "" or p.endswith("/") else p)
    return sorted(names)


def fingerprint(web: pathlib.Path) -> str:
    """Hash of every SHELL file's bytes, with pwa.js's APP_V blanked."""
    h = hashlib.sha256()
    for name in shell_files(web):
        data = (web / name).read_bytes()
        if name == "pwa.js":
            data = APP_V_RE.sub('const APP_V = ""', data.decode()).encode()
        h.update(name.encode() + b"\0" + data + b"\0")
    return h.hexdigest()[:16]


def version(web: pathlib.Path) -> str:
    return V_RE.search((web / "sw.js").read_text()).group(1)


def stamp(web: pathlib.Path) -> str | None:
    m = STAMP_RE.search((web / "sw.js").read_text())
    return m.group(1) if m else None


def moved(web: pathlib.Path = WEB) -> bool:
    """True when the shell is not what V was last bumped for."""
    return stamp(web) != fingerprint(web)


def sync(web: pathlib.Path = WEB) -> str | None:
    """Bump V (and APP_V) if the shell moved. Returns the new V, or None."""
    if not moved(web):
        return None
    sw, pwa = web / "sw.js", web / "pwa.js"
    old = version(web)
    m = re.search(r"(\d+)$", old)
    if not m:
        raise ValueError(f'sw.js V is "{old}": it needs a numeric tail to bump')
    new = old[:m.start()] + str(int(m.group(1)) + 1)
    pwa.write_text(APP_V_RE.sub(f'const APP_V = "{new}"', pwa.read_text(), count=1))
    src = V_RE.sub(f'const V = "{new}"', sw.read_text(), count=1)
    sw.write_text(STAMP_RE.sub(f"// SHELL FINGERPRINT: {fingerprint(web)}", src, count=1))
    return new


def main() -> None:
    if "--check" in sys.argv:
        if moved():
            sys.exit(f"web/ shell moved since {version(WEB)}; run src/pwa_version.py")
        print(f"{version(WEB)} matches the shell")
        return
    new = sync()
    print(f"bumped to {new}" if new else f"{version(WEB)} already matches the shell")


if __name__ == "__main__":
    main()
