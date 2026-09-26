"""Drive web/board.html in a real browser, from file://, with no server running.

The contract test proves the JS computes the same rows as Python. It says nothing
about whether the page loads at all, and two things here were wrong until measured:
a board opened as file:// cannot fetch a sibling file (hence web/model.js), and the
whole point of leaving a tablet open is the alert re-arming, which only the browser
does. So this replays a fixture as if it were happening now and watches the page.

Not collected by pytest on purpose: it needs playwright, a browser and ~90 s.

    PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \\
      uv run --with playwright python tests/board_smoke.py [--headed] [--case N]
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOARD = "file://" + str(ROOT / "web" / "board.html")
WALK_S = 390
REARM_WAIT_S = 75        # board.html re-arms a pending alert every 60 s
ISO_KEYS = ("arrival_time", "departure_time", "updated_at")


def shifted(case: dict, delta: float) -> dict:
    """The fixture, moved forward so its trains are still in the future."""
    stamp = lambda t: (dt.datetime.fromtimestamp(t, dt.timezone.utc)
                       .replace(microsecond=0).isoformat())

    def move(obj):
        for k in ISO_KEYS:
            if obj.get(k):
                obj[k] = stamp(dt.datetime.fromisoformat(obj[k]).timestamp() + delta)
        return obj

    out = json.loads(json.dumps(case))
    for p in out["preds"]:
        move(p["attributes"])
    for v in out["vehicles"]:
        move(v["attributes"])
    out["slots"] = [[t + delta, tr] for t, tr in out["slots"]]
    return out


def schedule_body(slots) -> dict:
    stamp = lambda t: (dt.datetime.fromtimestamp(t, dt.timezone.utc)
                       .replace(microsecond=0).isoformat())
    return {"data": [{"attributes": {"arrival_time": stamp(t), "departure_time": None},
                      "relationships": {"trip": {"data": {"id": tr}}}}
                     for t, tr in slots]}


def run(case_index: int, headed: bool) -> int:
    from playwright.sync_api import sync_playwright

    fixture = sorted((ROOT / "tests" / "fixtures").glob("cases-*.json"))[0]
    case = json.loads(fixture.read_text())[case_index]
    # +30 s so even the soonest prediction is still ahead of the page's clock.
    live = shifted(case, time.time() - case["now"] + 30)
    posts, failures = [], []

    def check(name, ok, detail=""):
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
        if not ok:
            failures.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        # Armed before the page opens: this is the tablet that has been sitting
        # there since breakfast, not a fresh visit.
        page.add_init_script(f"""
          localStorage.setItem("magoun.ntfy", "smoke-test-topic");
          localStorage.setItem("magoun.armed", "1");
          localStorage.setItem("magoun.armedEta", "{int(time.time()) + 3600}");
          localStorage.setItem("magoun.walk", "{WALK_S}");
          localStorage.removeItem("magoun.berths");
        """)
        page.route(re.compile(r"api-v3\.mbta\.com/predictions"), lambda r: r.fulfill(
            json={"data": live["preds"]}, content_type="application/json"))
        page.route(re.compile(r"api-v3\.mbta\.com/vehicles"), lambda r: r.fulfill(
            json={"data": live["vehicles"]}, content_type="application/json"))
        page.route(re.compile(r"api-v3\.mbta\.com/schedules"), lambda r: r.fulfill(
            json=schedule_body(live["slots"]), content_type="application/json"))
        page.route(re.compile(r"api-v3\.mbta\.com/alerts"), lambda r: r.fulfill(
            json={"data": []}, content_type="application/json"))

        def ntfy(route):
            req = route.request
            posts.append({"method": req.method, "at": req.header_value("at"),
                          "seq": req.header_value("sequence-id"), "t": time.time()})
            route.fulfill(status=200, json={"id": "stub"})
        # Nothing may reach the real ntfy.sh from a test.
        page.route(re.compile(r"ntfy\.sh"), ntfy)

        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        page.goto(BOARD)
        page.wait_for_timeout(6000)

        print(f"replaying {fixture.name}#{case_index} as now, from file://\n")
        hero = page.inner_text("#heroBig").strip()
        sub = page.inner_text("#heroSub").replace("\n", " ")
        foot = page.inner_text("#foot")
        print(f"  hero: {hero}  |  {sub[:90]}")
        print(f"  foot: {foot[:130]}\n")

        check("the page loads with no script error", not errors, "; ".join(errors[:2]))
        check("a countdown or a due train is shown",
              bool(re.fullmatch(r"-?\d+:\d\d", hero) or hero == "due"), hero)
        check("the band and the tier are named",
              "±" in sub and any(s in sub for s in
                                 ("mbta", "departed", "berthed", "schedule")))
        check("the feed reads live, not stale", foot.startswith("live"))
        check("the median headway came from model.json", "median headway" in foot)
        check("following trains are listed",
              page.eval_on_selector_all(".row", "e => e.length") >= 1)
        check("the line map drew trains",
              page.eval_on_selector_all(".train", "e => e.length") >= 1)
        check("the five stops below the terminus box are drawn",
              page.eval_on_selector_all(".stopname", "e => e.length") == 5)
        check("berth state is persisted across a reload",
              page.evaluate("localStorage.getItem('magoun.berths')") is not None)
        check("the schedule is cached per service day",
              any(k.startswith("magoun.sched.") for k in page.evaluate(
                  "Object.keys(localStorage)")))
        check("the armed alert was posted on the first paint", len(posts) >= 1)
        if posts:
            at = int(posts[0]["at"])
            lo = json.loads(page.evaluate("JSON.stringify(data.next)"))["lo"]
            # Either the quantile the rider acts on, minus the walk, or -- when that
            # moment has already gone -- armAlert's floor of now + 11 s, which is
            # the "LEAVE NOW" case rather than an alert scheduled into the past.
            aimed = abs(at - (lo - WALK_S)) <= 2
            clamped = abs(at - (posts[0]["t"] + 11)) <= 2
            check("it fires at lo minus the walk, or now if that has passed",
                  aimed or clamped,
                  f"At={at} lo-walk={lo - WALK_S:.0f} "
                  f"{'(clamped to now+11)' if clamped else ''}")
            check("it never schedules an alert into the past", at >= posts[0]["t"])
            check("it replaces the pending alert rather than adding one",
                  posts[0]["seq"] == "magoun-leave", str(posts[0]["seq"]))

        print(f"\n  waiting {REARM_WAIT_S}s for the re-arm...")
        page.wait_for_timeout(REARM_WAIT_S * 1000)
        check("the armed alert re-arms while the page stays open",
              len(posts) >= 2, f"{len(posts)} posts in {REARM_WAIT_S + 6}s")
        if len(posts) >= 2:
            gap = posts[1]["t"] - posts[0]["t"]
            check("it re-arms about once a minute", 55 <= gap <= 90, f"{gap:.0f}s apart")
        check("the alert stays armed in the footer",
              "alert armed" in page.inner_text("#foot"))
        browser.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("all checks passed: the static board runs from file:// with no backend")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, default=0)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.case, a.headed))
