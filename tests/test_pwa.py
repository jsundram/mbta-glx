"""The board as an installed app: service worker, manifest, share card.

Everything here fails silently in a browser. A shell change without a new V never
reaches a phone; a live page left out of SHELL is served stale until the next bump; a
cached model.json stops a refit reaching a board left running; a share card that is
relative, an SVG or too big previews as a grey box. pwa-starter checks most of these
with a pre-commit lint (scripts/sw-lint.py); this repo's guards live in the suite, so
they are ported here.
"""
import json
import pathlib
import re
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
sys.path.insert(0, str(ROOT / "src"))

import pwa_version  # noqa: E402

SW = (WEB / "sw.js").read_text()
PWA = (WEB / "pwa.js").read_text()
INDEX = (WEB / "index.html").read_text()
SITE = "https://jsundram.github.io/mbta-glx/"


# --- the version: the one rule ---

def test_the_shell_has_not_moved_without_a_new_version():
    """pwa-starter's THE ONE RULE. A shell edit with the same V never reaches a phone.

    Fails on a hand edit to any precached file -- index.html, app.js, an icon --
    until src/pwa_version.py has bumped V for it. fit.py and publish.py run it
    themselves, so a refit does not trip this.
    """
    assert not pwa_version.moved(WEB), (
        f"web/ shell changed since {pwa_version.version(WEB)} was cut; "
        "run `uv run python src/pwa_version.py`")


def test_the_version_has_a_numeric_tail():
    """The tail orders cache generations: sw.js only collects strictly older ones."""
    assert re.search(r"\d+$", pwa_version.version(WEB))


def test_the_running_code_reports_the_same_version_as_the_worker():
    """APP_V is what the update tag compares with the server. Drift and a stale
    device reads as current (pwa-starter #17)."""
    v = pwa_version.version(WEB)
    assert pwa_version.APP_V_RE.search(PWA).group(1) == v
    prefix = re.search(r'const VER_PREFIX\s*=\s*"([^"]*)"', PWA).group(1)
    assert prefix == re.sub(r"\d+$", "", v), "VER_PREFIX must be V's stem"


def test_a_bump_moves_both_versions_and_settles(tmp_path):
    web = tmp_path / "web"
    shutil.copytree(WEB, web)
    before = pwa_version.version(web)
    assert pwa_version.sync(web) is None, "nothing moved, so nothing to bump"
    (web / "index.html").write_text(INDEX + "\n<!-- edited -->\n")
    assert pwa_version.moved(web)
    after = pwa_version.sync(web)
    n = int(re.search(r"\d+$", before).group())
    assert after == re.sub(r"\d+$", str(n + 1), before)
    assert pwa_version.APP_V_RE.search((web / "pwa.js").read_text()).group(1) == after
    assert not pwa_version.moved(web), "writing APP_V must not move the fingerprint"
    assert pwa_version.sync(web) is None


def test_publish_bumps_the_version_when_a_refit_moves_model_js(tmp_path, monkeypatch):
    """A refit rewrites model.js with nobody touching the page, and it is in the
    shell: without a bump, installed copies keep the old model offline."""
    import publish
    web = tmp_path / "web"
    shutil.copytree(WEB, web)
    monkeypatch.setattr(publish, "WEB", web)
    # A refit: data/model.json moves, and the published pair is behind it.
    refit = tmp_path / "model.json"
    refit.write_text('{"grid": [0.5]}')
    for a in publish.MANIFEST:
        if a.source is not None and a.source.name == "model.json":
            monkeypatch.setattr(a, "source", refit)
    before = pwa_version.version(web)
    assert [a.name for a, _ in publish.check()] == ["model.json", "model.js"]
    moved = publish.publish()
    assert {"model.json", "model.js", "sw.js"} <= set(moved)
    assert pwa_version.version(web) != before
    assert publish.check() == []


def test_check_reports_an_unbumped_shell(tmp_path, monkeypatch):
    import publish
    web = tmp_path / "web"
    shutil.copytree(WEB, web)
    monkeypatch.setattr(publish, "WEB", web)
    (web / "index.html").write_text(INDEX + "\n<!-- edited -->\n")
    assert [(a.name, s) for a, s in publish.check()] == [("sw.js", "stale")]


# --- the shell list ---

SHELL = pwa_version.shell_entries(SW)
SHELL_FILES = set(pwa_version.shell_files(WEB))


def test_every_shell_entry_exists():
    """A SHELL entry that 404s wedges the old-generation collect on every device."""
    missing = [f for f in SHELL_FILES if not (WEB / f).exists()]
    assert not missing, f"SHELL lists files web/ does not have: {missing}"


def test_no_shell_entry_is_cross_origin():
    assert not [e for e in SHELL if "://" in e]


def test_every_live_page_and_script_is_in_the_shell():
    """sw.js serves .html/.js cache-first. One left out of SHELL is cached by the
    first visit and then never refreshed until the next V -- the shell is the only
    thing a bump refetches."""
    live = {p.name for p in WEB.iterdir() if p.suffix in (".html", ".js")} - {"sw.js"}
    assert live <= SHELL_FILES, f"not in SHELL: {sorted(live - SHELL_FILES)}"


def test_the_model_bypasses_the_worker():
    """app.js reloads the board when model.json's text changes. A cached copy would
    compare with itself forever, so a refit would never reach a board left running."""
    assert "model.json" not in SHELL_FILES
    assert 'u.pathname.endsWith("/model.json")) return;' in SW
    assert "model.js" in SHELL_FILES, "the offline fallback for the model"


def test_boot_dependencies_are_precached():
    deps = re.search(r"const BOOT_DEPS = \{(.*?)\};", SW, re.S).group(1)
    for dep in set(re.findall(r'"\./([^"]+)"', deps)):
        assert dep in SHELL_FILES, f"BOOT_DEPS names {dep}, which is not precached"


# --- the worker's behaviour, in node ---

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_worker_behaves():
    """Cache-first, bounded on lie-fi, never a blank screen; model.json and api-v3
    pass through. Runs web/sw.js unmodified under mocked worker globals."""
    out = subprocess.run([NODE, str(ROOT / "tests" / "sw.test.mjs"), str(WEB / "sw.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr


def test_node_is_present_in_ci():
    """Invariant 9: a skipped test is not a passing one."""
    import os
    if os.environ.get("CI"):
        assert NODE, "node is missing in CI: the service worker is not being tested"


# --- install ---

def test_the_manifest_is_linked_and_complete():
    m = json.loads((WEB / "manifest.json").read_text())
    assert 'rel="manifest" href="manifest.json"' in INDEX
    assert m["start_url"] == "./" and m["scope"] == "./" and m["display"] == "standalone"
    assert m["name"] and m["short_name"]
    assert "orientation" not in m, "the board also runs on a tablet, either way up"
    for icon in m["icons"]:
        path = WEB / icon["src"].removeprefix("./")
        w, h = struct.unpack(">II", path.read_bytes()[16:24])
        assert f"{w}x{h}" == icon["sizes"], icon
    assert any(i.get("purpose") == "maskable" for i in m["icons"])


def test_the_new_icons_are_the_ones_make_icon_draws():
    """Same guard as icon-180's in test_publish, on the decoded pixels."""
    import zlib
    import make_icon
    for size in make_icon.SIZES:
        raw = (WEB / f"icon-{size}.png").read_bytes()
        idat, i = b"", 8
        while i < len(raw):
            n = struct.unpack(">I", raw[i:i + 4])[0]
            if raw[i + 4:i + 8] == b"IDAT":
                idat += raw[i + 8:i + 8 + n]
            i += 12 + n
        stride, lines = size * 3, zlib.decompress(idat)
        got = b"".join(lines[j + 1:j + 1 + stride] for j in range(0, len(lines), stride + 1))
        assert got == make_icon.pixels(size), f"icon-{size}.png is not what make_icon draws"


@pytest.mark.parametrize("tag", [
    '<meta name="apple-mobile-web-app-capable" content="yes">',
    '<meta name="apple-mobile-web-app-title" content="Magoun">',
    '<link rel="apple-touch-icon" href="icon-180.png">',
    '<meta name="color-scheme" content="light dark">',
])
def test_the_ios_install_tags_are_there(tag):
    """iOS mostly ignores the manifest and reads these."""
    assert tag in INDEX


def test_the_layout_clears_the_notch():
    """viewport-fit=cover without safe-area padding puts the header under the notch."""
    assert "viewport-fit=cover" in INDEX
    assert "env(safe-area-inset-top)" in INDEX


def test_the_update_tag_is_a_button():
    assert re.search(r'<button[^>]*id="ver"[^>]*hidden', INDEX)


# --- the share card ---

def _meta(prop):
    m = re.search(rf'<meta (?:property|name)="{re.escape(prop)}" content="([^"]*)"', INDEX)
    assert m, f"no {prop}"
    return m.group(1)


def test_the_share_card_is_absolute_and_a_raster():
    """Scrapers reject a relative og:image and will not render an SVG."""
    assert _meta("og:url") == SITE
    for prop in ("og:image", "twitter:image"):
        assert _meta(prop) == SITE + "og.png"
    assert _meta("twitter:card") == "summary_large_image"
    for prop in ("og:title", "og:description", "og:image:alt", "description"):
        assert _meta(prop).strip()


def test_the_share_card_is_scrapable():
    """1200x630, under the ~300 KB WhatsApp skips past (src/make_og.sh's MAX_BYTES)."""
    raw = (WEB / "og.png").read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", raw[16:24]) == (1200, 630)
    assert int(_meta("og:image:width")) == 1200 and int(_meta("og:image:height")) == 630
    assert len(raw) <= 250_000


def test_the_published_address_is_where_pages_serves_it():
    """og:url names one place; if the Pages URL ever moves, the card points at a 404."""
    claude = (ROOT / "CLAUDE.md").read_text()
    assert SITE in claude
