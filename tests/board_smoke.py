"""Drive web/board.html in a real browser: from file://, and over HTTP.

The contract test proves the JS computes the same rows as Python. It says nothing
about whether the page loads at all, and three things here were wrong until
measured: a board opened as file:// cannot fetch a sibling file (hence web/model.js);
ntfy's Sequence-ID does not replace a pending scheduled message, so re-arming on a
timer queues real deliveries rather than refining one; and an armed alert therefore
has to be handed to the notifier to be refined at all. So this replays a fixture as
if it were happening now and watches what the page actually posts.

Both shapes are covered because they differ. From file:// no sibling file can be
fetched, so the model arrives as a script and the self-score panel hides itself.
Over HTTP -- which is what Pages serves -- stats.json is reachable and the panel is
populated; that half is the only check on the published self-score.

Not collected by pytest on purpose: it needs playwright, a browser and ~90 s.

    PLAYWRIGHT_BROWSERS_PATH=~/.cache/ms-playwright \\
      uv run --with playwright==1.61.0 python tests/board_smoke.py [--headed] [--case N]

The pin matters: playwright only drives the browser build it shipped with, and
1.61.0 is the one matching the cached chromium-1228 / webkit-2311 on this Mac.
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
REARM_WAIT_S = 75        # board.html reconsiders the alert every 60 s
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


def stub(page, live):
    """Serve one shifted fixture in place of every MBTA endpoint the board calls."""
    page.route(re.compile(r"api-v3\.mbta\.com/predictions"), lambda r: r.fulfill(
        json={"data": live["preds"]}, content_type="application/json"))
    page.route(re.compile(r"api-v3\.mbta\.com/vehicles"), lambda r: r.fulfill(
        json={"data": live["vehicles"]}, content_type="application/json"))
    page.route(re.compile(r"api-v3\.mbta\.com/schedules"), lambda r: r.fulfill(
        json=schedule_body(live["slots"]), content_type="application/json"))
    page.route(re.compile(r"api-v3\.mbta\.com/alerts"), lambda r: r.fulfill(
        json={"data": []}, content_type="application/json"))


def dwell_scenario(browser, check) -> None:
    """A train sitting at Magoun: the one thing ArrivalTracker exists to show.

    The dwell counter cannot be derived from a single snapshot -- the feed refreshes
    a stopped train's timestamp -- so it is observed across polls, and nothing else
    in the test suite touches the ported tracker.
    """
    case, index = _case_stopped_at_magoun()
    if case is None:
        check("a fixture has a train stopped at Magoun", False)
        return
    live = shifted(case, time.time() - case["now"] + 30)
    page = browser.new_page()
    stub(page, live)
    page.route(re.compile(r"ntfy\.sh"), lambda r: r.fulfill(status=200, json={}))
    page.goto(BOARD)
    page.wait_for_timeout(5000)
    label = page.inner_text("#heroLabel").strip()
    big = page.inner_text("#heroBig").strip()
    print(f"\n  replaying {index} (a train stopped at Magoun)")
    print(f"  hero: {label} / {big}  |  {page.inner_text('#heroSub')[:70]}")
    # .label is uppercased by CSS, so compare case-insensitively.
    check("a train at the platform is shown as such",
          label.lower() == "train at the station", label)
    check("its dwell is counting in seconds",
          bool(re.fullmatch(r"\d+:\d\d", big)), big)
    check("the hero is marked as boardable now",
          "here" in page.eval_on_selector("#hero", "e => e.className"))
    page.close()


def served_scenario(browser, check) -> None:
    """The same page over HTTP, which is the only place the self-score can appear.

    A board opened as file:// cannot fetch a sibling file at all, so stats.json is
    unreachable there and the panel hides itself -- by design, and verified below.
    Pages serves web/ over HTTP, so this is the deployed shape: the one where
    stats.json is fetched and the panel is populated. Nothing else in this file
    covers it, because until M3 there was no stats.json to fetch.
    """
    import functools
    import http.server
    import socketserver
    import threading

    stats = json.loads((ROOT / "web" / "stats.json").read_text())
    # Quiet: the request log would bury the checks. This has to be a subclass --
    # setting .log_message on a functools.partial succeeds silently and does
    # nothing, because the partial is not the handler class.
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k):
            pass

    handler = functools.partial(Quiet, directory=str(ROOT / "web"))
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        fixture = sorted((ROOT / "tests" / "fixtures").glob("cases-*.json"))[0]
        case = json.loads(fixture.read_text())[0]
        live = shifted(case, time.time() - case["now"] + 30)

        page = browser.new_page()
        stub(page, live)
        page.route(re.compile(r"ntfy\.sh"), lambda r: r.fulfill(status=200, json={}))
        page.goto(f"http://127.0.0.1:{port}/board.html")
        page.wait_for_timeout(5000)

        print(f"\n  serving web/ over HTTP on {port} (the deployed shape)")
        check("the self-score panel is shown when stats.json is reachable",
              not page.is_hidden("#hist"))
        score = page.inner_text("#histScore")
        print(f"  score: {score}")
        check("it reports the real caught count",
              f"caught {stats['caught']}/{stats['of']}" in score, score)
        check("platform wait is shown in minutes",
              f"{stats['mean_platform_wait_s'] / 60:.1f} min" in score)
        # Platform wait alone rewards dawdling, so the board has to show both.
        check("door-to-train is shown beside it",
              "door-to-train" in score
              and f"{stats['mean_door_to_train_s'] / 60:.1f} min" in score)
        check("the window it scored is named",
              f"{stats['window_days']}d to {stats['as_of']}" in score)
        rows = page.inner_text("#histRows")
        check("every lead bin is rendered",
              all(b["bin"] in rows for b in stats["by_lead"]),
              f"{len(stats['by_lead'])} bins")
        check("a bin shows its sample size and its p50",
              f"n={stats['by_lead'][0]['n']}" in rows and "p50" in rows)
        page.close()
        httpd.shutdown()


def _case_stopped_at_magoun():
    magoun = json.loads((ROOT / "web" / "model.json").read_text())["constants"]["stops"]["magoun_in"]
    for f in sorted((ROOT / "tests" / "fixtures").glob("cases-*.json")):
        for i, c in enumerate(json.loads(f.read_text())):
            for v in c["vehicles"]:
                a = v["attributes"]
                if (a["direction_id"] == 0 and a["current_status"] == "STOPPED_AT"
                        and (v["relationships"]["stop"]["data"] or {}).get("id") == magoun):
                    return c, f"{f.stem}#{i}"
    return None, None


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
        # Not armed: the bell is tapped below, because arming and then holding an
        # alert are different behaviours now and the difference is the whole point.
        # A scheduled ntfy message cannot be re-pointed, so the page arms ONCE and
        # the notifier does the refining -- an already-armed page that published
        # again would be adding a second delivery, not moving the first.
        page.add_init_script(f"""
          localStorage.setItem("magoun.ntfy", "smoke-test-topic");
          localStorage.setItem("magoun.cmd", "smoke-test-cmd");
          localStorage.setItem("magoun.walk", "{WALK_S}");
          localStorage.removeItem("magoun.armed");
          localStorage.removeItem("magoun.armedEta");
          localStorage.removeItem("magoun.berths");
        """)
        stub(page, live)

        def ntfy(route):
            req = route.request
            posts.append({"method": req.method, "at": req.header_value("at"),
                          "seq": req.header_value("sequence-id"), "t": time.time(),
                          "url": req.url, "body": req.post_data})
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
        check("nothing is pushed until the rider arms something", not posts,
              f"{len(posts)} posts before the bell was tapped")
        page.click("#bell")
        page.wait_for_timeout(2000)
        sched = [q for q in posts if q["at"]]
        check("tapping the bell schedules the fallback alert", len(sched) == 1,
              f"{len(sched)} scheduled posts")
        if sched:
            at = int(sched[0]["at"])
            lo = json.loads(page.evaluate("JSON.stringify(data.next)"))["lo"]
            # Either the quantile the rider acts on, minus the walk, or -- when that
            # moment has already gone -- armAlert's floor of now + 11 s, which is
            # the "LEAVE NOW" case rather than an alert scheduled into the past.
            aimed = abs(at - (lo - WALK_S)) <= 2
            clamped = abs(at - (sched[0]["t"] + 11)) <= 2
            check("it fires at lo minus the walk, or now if that has passed",
                  aimed or clamped,
                  f"At={at} lo-walk={lo - WALK_S:.0f} "
                  f"{'(clamped to now+11)' if clamped else ''}")
            check("it never schedules an alert into the past", at >= sched[0]["t"])
            # Sequence-ID is sent so the ntfy APP collapses these in the tray. It
            # does not replace the pending message: measured against ntfy.sh, three
            # publishes with one Sequence-ID and one At deliver three times. The
            # count below is the check that matters, not the header.
            check("the alert is tagged for the app to collapse",
                  sched[0]["seq"] == "magoun-leave", str(sched[0]["seq"]))

        print(f"\n  waiting {REARM_WAIT_S}s for the re-arm...")
        page.wait_for_timeout(REARM_WAIT_S * 1000)
        # Every publish is a delivery, so re-arming on a timer buys nothing and
        # costs a buzz. The page re-points the alert only when the leave time has
        # actually moved, and hands the train to the notifier, which refines one
        # alert instead of queueing more. Both posts below are to ntfy.sh: the
        # scheduled leave-now, and the `arm` command.
        leave = [q for q in posts if q["at"]]
        cmds = [q for q in posts if "smoke-test-cmd" in (q["url"] or "")]
        check("an armed page never schedules a second alert",
              len(leave) == 1, f"{len(leave)} scheduled posts in {REARM_WAIT_S + 8}s")
        check("but it keeps handing the train to the notifier",
              len(cmds) >= 2, f"{len(cmds)} command posts")
        if cmds:
            check("the handoff names the train as an epoch",
                  re.fullmatch(r"arm \d{10} \S+", cmds[0]["body"] or ""),
                  str(cmds[0]["body"]))
            # Not data.next: once the armed train arrives, next is the one behind
            # it, and handing THAT over walks the notifier onto a different train.
            check("every handoff names the same train",
                  len({q["body"] for q in cmds}) == 1,
                  " | ".join(sorted({str(q["body"]) for q in cmds})))
        check("the alert stays armed in the footer",
              "alert armed" in page.inner_text("#foot"))
        # The documented degradation: from file:// stats.json cannot be fetched at
        # all, so the panel hides rather than showing an empty box.
        check("the self-score panel hides itself from file://",
              page.is_hidden("#hist"))
        dwell_scenario(browser, check)
        served_scenario(browser, check)
        browser.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("all checks passed: the board runs from file:// with no backend, "
          "and serves its self-score over HTTP")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, default=0)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.case, a.headed))
