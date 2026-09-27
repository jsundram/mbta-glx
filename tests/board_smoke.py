"""Drive web/index.html in a real browser: from file://, and over HTTP.

The contract test proves the JS computes the same rows as Python. It says nothing
about whether the page loads at all, and three things here were wrong until
measured: a board opened as file:// cannot fetch a sibling file (hence web/model.js);
ntfy's Sequence-ID does not replace a pending scheduled message, so re-arming on a
timer queues real deliveries rather than refining one; and an armed alert therefore
has to be handed to the notifier to be refined at all. So this replays a fixture as
if it were happening now and watches what the page actually posts.

Both shapes are covered because they differ. From file:// no sibling file can be
fetched, so the model arrives as a script. Over HTTP -- which is what Pages serves --
it is fetched, and so is the home-screen icon; that half is the only run that takes
either branch. The self-score panel is neither: it asks the backend how trains have
run today, and today is a question no published file can answer.

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
BOARD = "file://" + str(ROOT / "web" / "index.html")
WALK_S = 390
REARM_WAIT_S = 75        # index.html reconsiders the alert every 60 s
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
        # make_fixtures keeps only the fields compute_rows reads, so no sampled
        # vehicle has `carriages` -- and the board paints the lead car number in
        # the hero, on the map and in the terminus box. Give the stub one, rather
        # than regenerating 28 frozen fixtures to exercise a display field.
        v["attributes"].setdefault("carriages", [{"label": "3" + v["id"][-3:]}])
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
          label.lower().startswith("train")
          and label.lower().endswith("at the station"), label)
    check("and it is named, so the hero cannot be about a different train",
          bool(re.search(r"\b3\d\d\d\b", label)), label)
    check("its dwell is counting in seconds",
          bool(re.fullmatch(r"\d+:\d\d", big)), big)
    check("the hero is marked as boardable now",
          "here" in page.eval_on_selector("#hero", "e => e.className"))
    page.close()


def _case_with_a_skip():
    """A fixture whose skip set actually produces a 'not stopping here' row.

    Synthesised by make_fixtures, not sampled: skips are ~10/day system-wide and
    the contract test would otherwise never see the tier (invariant 7's shape).
    """
    for f in sorted((ROOT / "tests" / "fixtures").glob("cases-*.json")):
        for i, c in enumerate(json.loads(f.read_text())):
            if c.get("skipped") and any(r.get("skipped") for r in c["expected"]):
                return c, f"{f.name}#{i}"
    return None, None


def skip_scenario(browser, check) -> None:
    """M4's done-when, as far as a fixture can take it: a skipped train struck
    through on the board.

    The whole path, not just the CSS -- the board fetches <backend_url>/skips,
    the trip ids come back, computeRows turns the matching schedule slots into
    'not stopping here' rows, and the stylesheet strikes them out. Nothing else
    checks that the endpoint's answer reaches the screen; the contract test stops
    at the rows, and until now a deleted rule or a renamed field would have gone
    unnoticed.

    A real MBTA skip is still unobserved. This proves the board can show one.
    """
    case, index = _case_with_a_skip()
    if case is None:
        check("a fixture carries a skipped train", False)
        return
    live = shifted(case, time.time() - case["now"] + 30)
    consts = json.loads((ROOT / "web" / "model.json").read_text())["constants"]
    url = consts["backend_url"] + "/skips"
    host = re.escape(url.split("/")[2])

    page = browser.new_page()
    page.add_init_script(f"""localStorage.setItem("magoun.walk", "{WALK_S}");
                             localStorage.removeItem("magoun.berths");""")
    stub(page, live)
    page.route(re.compile(r"ntfy\.sh"), lambda r: r.fulfill(status=200, json={}))
    served = []

    def backend(route):
        u = route.request.url
        served.append(u)
        body = ({"as_of": time.time(), "stale_after_s": 600} if u.endswith("/capture")
                else {"as_of": time.time(), "trips": case["skipped"], "ttl_s": 60})
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route(re.compile(host), backend)
    page.goto(BOARD)
    page.wait_for_timeout(6000)

    print(f"\n  replaying {index} with the skip endpoint answering")
    check("the board asked the published endpoint for the skip set",
          any(u.endswith("/skips") for u in served), f"{len(served)} backend requests")
    n = page.eval_on_selector_all(".row.skipped", "e => e.length")
    check("a skipped train is listed", n >= 1, f"{n} skipped rows")
    if n:
        print("  row:", page.inner_text(".row.skipped").replace("\n", " "))
        deco = page.eval_on_selector(
            ".row.skipped .v", "e => getComputedStyle(e).textDecorationLine")
        check("and it is struck through", "line-through" in deco, deco)

    # The other half: the endpoint is the only source, so losing it must lose the
    # strikethrough and nothing else. An unreachable Mac is the normal case.
    page2 = browser.new_page()
    page2.add_init_script(f"""localStorage.setItem("magoun.walk", "{WALK_S}");
                              localStorage.removeItem("magoun.berths");""")
    stub(page2, live)
    page2.route(re.compile(r"ntfy\.sh"), lambda r: r.fulfill(status=200, json={}))
    page2.route(re.compile(host), lambda r: r.abort())
    errs = []
    page2.on("pageerror", lambda e: errs.append(str(e)[:150]))
    page2.goto(BOARD)
    page2.wait_for_timeout(6000)
    check("with the endpoint unreachable the board still paints",
          bool(page2.inner_text("#heroBig").strip()) and not errs,
          "; ".join(errs[:2]))
    check("and simply shows no skipped train",
          page2.eval_on_selector_all(".row.skipped", "e => e.length") == 0)
    page.close()
    page2.close()


def ratelimit_scenario(browser, check) -> None:
    """Being rate-limited must be visible, fixable, and must not make it worse.

    Found live: the board simply stopped updating. tick() calls snapshot() first
    and getJSON throws on a 429, so it aborted before the skip and capture
    fetches were even issued -- they looked unanswered when the fault was two
    lines upstream. 20 requests/minute is per client IP and one board is about
    12.5 of them, so a second device on the same wifi is enough to cause it.
    """
    fixture = sorted((ROOT / "tests" / "fixtures").glob("cases-*.json"))[0]
    case = json.loads(fixture.read_text())[0]
    live = shifted(case, time.time() - case["now"] + 30)

    page = browser.new_page()
    page.add_init_script(f"""localStorage.setItem("magoun.walk", "{WALK_S}");
                             localStorage.removeItem("magoun.mbtakey");
                             localStorage.removeItem("magoun.berths");""")
    seen_keys, limited = [], {"on": True}

    def v3(route):
        u = route.request.url
        seen_keys.append("api_key=" in u)
        if limited["on"]:
            return route.fulfill(status=429, content_type="application/json",
                                 body='{"errors":[{"code":"rate_limited"}]}')
        if "/predictions" in u:
            return route.fulfill(json={"data": live["preds"]},
                                 content_type="application/json")
        if "/vehicles" in u:
            return route.fulfill(json={"data": live["vehicles"]},
                                 content_type="application/json")
        if "/schedules" in u:
            return route.fulfill(json=schedule_body(live["slots"]),
                                 content_type="application/json")
        return route.fulfill(json={"data": []}, content_type="application/json")

    page.route(re.compile(r"api-v3\.mbta\.com"), v3)
    page.route(re.compile(r"ntfy\.sh"), lambda r: r.fulfill(status=200, json={}))
    page.goto(BOARD)
    page.wait_for_timeout(5000)

    print("\n  api-v3 answering 429")
    foot = page.inner_text("#foot")
    print(f"  foot: {foot[:110]}")
    check("the board says it is being rate-limited, not just 'stale'",
          "rate-limiting" in foot.lower(), foot[:90])
    check("and offers somewhere to paste a key", not page.is_hidden("#keyask"))

    # Hammering a limit you are already over keeps you over it.
    before = len(seen_keys)
    page.wait_for_timeout(12000)
    check("it backs off instead of retrying every poll",
          len(seen_keys) - before == 0,
          f"{len(seen_keys) - before} more requests in 12 s at a 10 s poll")

    limited["on"] = False
    page.fill("#keyin", "smoke-test-key")
    page.click("#keyask button")
    page.wait_for_timeout(4000)
    check("saving a key retries at once rather than serving out the backoff",
          len(seen_keys) > before, f"{len(seen_keys) - before} requests after save")
    check("and the key is actually sent", seen_keys and seen_keys[-1],
          "no api_key= in the last request")
    check("the warning clears once requests succeed",
          page.is_hidden("#keyask")
          and "rate-limiting" not in page.inner_text("#foot").lower(),
          page.inner_text("#foot")[:90])
    page.close()


def capture_scenario(browser, check) -> None:
    """A dead archiver has to be visible, and an unreachable one must not look
    like a dead one.

    An un-captured day cannot be recovered -- the v3 /schedules endpoint serves
    about eight days back and nothing else keeps them -- so launch-plan.md calls
    this the top failure mode. Nothing anywhere used to say a word about it.
    """
    fixture = sorted((ROOT / "tests" / "fixtures").glob("cases-*.json"))[0]
    case = json.loads(fixture.read_text())[0]
    live = shifted(case, time.time() - case["now"] + 30)
    host = re.escape(json.loads((ROOT / "web" / "model.json").read_text())
                     ["constants"]["backend_url"].split("/")[2])

    def run(capture_body, label, two_polls=False):
        page = browser.new_page()
        page.add_init_script(f"""localStorage.setItem("magoun.walk", "{WALK_S}");
                                 localStorage.removeItem("magoun.berths");""")
        stub(page, live)
        page.route(re.compile(r"ntfy\.sh"), lambda r: r.fulfill(status=200, json={}))
        page.route(re.compile(host), lambda r: (
            r.abort() if capture_body is None else
            r.fulfill(status=200, content_type="application/json",
                      body=json.dumps(capture_body if r.request.url.endswith("/capture")
                                      else {"as_of": time.time(), "trips": [],
                                            "ttl_s": 60}))))
        page.goto(BOARD)
        page.wait_for_timeout(6000)
        first = page.inner_text("#foot")
        # The warning needs two consecutive bad reads, so a second poll is
        # required before it can appear at all -- see below.
        if two_polls:
            page.wait_for_timeout(11000)
        foot = page.inner_text("#foot")
        page.close()
        print(f"  {label}: {foot[:130]}")
        return first, foot

    print("\n  the archiver's heartbeat, as the footer reports it")
    _, fresh = run({"as_of": time.time() - 20, "stale_after_s": 600}, "writing")
    check("a live archiver says nothing", "archiver" not in fresh.lower(), fresh[:80])

    one, dead = run({"as_of": time.time() - 4000, "stale_after_s": 600},
                    "silent 66m", two_polls=True)
    # server.py answers the moment the Mac is up while record_rt may not have
    # appended yet, so one bad read on wake would paint "silent 8:00:00" and
    # teach the rider to ignore the line.
    check("one bad read does not raise the alarm",
          "archiver" not in one.lower(), one[-80:])
    check("a silent archiver is called out on the second", "archiver silent" in dead.lower(),
          dead[-90:])
    check("and it says data is being lost", "losing data" in dead.lower())

    _, gone = run(None, "backend unreachable")
    check("an unreachable backend is NOT reported as a dead archiver",
          "archiver" not in gone.lower(),
          "off the tailnet must not look like data loss: " + gone[:80])

    # as_of 0 is the backend saying "no archive file exists at all". Reachable
    # exactly when this matters: the archiver dies at 23:00, daily.sh compacts and
    # unlinks yesterday's file at 03:00, and nothing is writing a new one. `now-0`
    # is the whole Unix epoch, which rendered as a six-figure counter.
    _, never = run({"as_of": 0, "stale_after_s": 600}, "nothing ever written",
                   two_polls=True)
    check("an empty archive is reported without a nonsense age",
          "losing data" in never.lower() and "written nothing" in never.lower(),
          never[-90:])
    check("and no six-figure counter leaks into the footer",
          not re.search(r"\d{4,}:\d\d", never), never[-90:])

    # Six hours dead is the case the line has to survive; mmss would say 360:00.
    _, long = run({"as_of": time.time() - 21600, "stale_after_s": 600},
                  "silent 6h", two_polls=True)
    check("a long silence reads in hours", "6h" in long, long[-90:])


def _settings_page(browser, live, seed: str):
    """A board with `seed` in localStorage and every ntfy post captured."""
    page = browser.new_page()
    page.add_init_script(seed)
    stub(page, live)
    posts = []
    page.route(re.compile(r"ntfy\.sh"), lambda r: (
        posts.append(r.request.url), r.fulfill(status=200, json={"id": "stub"})))
    # Nothing here may be a native dialog any more: the panel replaced both prompts,
    # and a prompt() left behind would block the page in a way this would not see.
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(BOARD)
    page.wait_for_timeout(5000)
    return page, posts, dialogs


def firstrun_scenario(browser, check) -> None:
    """A board nobody has configured -- which is everyone but its author.

    The topic is one person's phone and the walk is one person's front door, so a
    stranger inherits both silently and gets a native prompt asking for an "ntfy
    topic (from ops/ntfy.env)" the first time they tap the bell. The board itself
    needs no configuration at all, so the panel must NOT be in the way on load, and
    must be what the bell opens when there is nowhere to send an alert.
    """
    fixture = sorted((ROOT / "tests" / "fixtures").glob("cases-*.json"))[0]
    case = json.loads(fixture.read_text())[0]
    live = shifted(case, time.time() - case["now"] + 30)
    page, posts, dialogs = _settings_page(browser, live, """
      localStorage.clear();
    """)
    print("\n  a board nobody has configured yet")
    check("nothing blocks a first load", not dialogs and page.is_hidden("#panel"))
    check("and the board still works with no settings at all",
          page.eval_on_selector_all(".row", "e => e.length") >= 1)
    page.click("#bell")
    page.wait_for_timeout(500)
    check("the bell opens the settings rather than a native prompt",
          not page.is_hidden("#panel") and not dialogs,
          f"{len(dialogs)} dialog(s)")
    check("and says what is missing",
          "ntfy topic" in page.inner_text("#setneed"), page.inner_text("#setneed"))
    check("nothing was published on the way", not posts, f"{len(posts)} posts")
    # The walk is prefilled from the published default, in minutes, so a stranger
    # can see whose walk they have been handed.
    check("the walk is shown, in minutes, from model.json",
          page.input_value("#setwalk") == f"{WALK_S / 60:.1f}".rstrip("0").rstrip("."),
          page.input_value("#setwalk"))
    page.fill("#setwalk", "9")
    page.fill("#setntfy", "smoke-firstrun")
    page.click("#setform button[type=submit]")
    page.wait_for_timeout(500)
    check("saving stores the walk in seconds",
          page.evaluate("localStorage.getItem('magoun.walk')") == "540",
          str(page.evaluate("localStorage.getItem('magoun.walk')")))
    check("and it took effect without a reload",
          page.evaluate("Magoun.walkSeconds()") == 540)
    page.click("#setclose")
    page.click("#bell")
    page.wait_for_timeout(1500)
    sched = [u for u in posts if "smoke-firstrun" in u]
    check("the bell then arms, to the topic just entered", len(sched) >= 1,
          f"{len(posts)} posts")
    page.close()


def today_scenario(browser, check) -> None:
    """The self-score panel, which now asks the backend how trains have run TODAY.

    It was a table of MBTA prediction-error quantiles binned by lead time, read off
    the published multi-day stats.json -- a question for whoever is fitting the
    model, not for someone standing on a platform. Today cannot come from a
    published file at all: stats.json is written from CLOSED days and this page is
    served from Pages, so nothing computed on the Mac during the day could reach it.

    Both halves matter. The panel has to render the backend's aggregate, and it has
    to disappear when the backend is unreachable -- which off the tailnet, or on a
    Mac without power, is the normal state and must not be a stale number wearing
    today's label.
    """
    fixture = sorted((ROOT / "tests" / "fixtures").glob("cases-*.json"))[0]
    case = json.loads(fixture.read_text())[0]
    live = shifted(case, time.time() - case["now"] + 30)
    consts = json.loads((ROOT / "web" / "model.json").read_text())["constants"]
    host = re.escape(consts["backend_url"].split("/")[2])
    payload = {"day": "2026-09-27", "as_of": time.time(), "ttl_s": 60,
               "walk_s": WALK_S, "close_s": 120, "min_trains": 3,
               "gap_s": 0, "max_gap_s": 0, "trains": 47,
               "early": 13, "close": 23, "late": 11,
               "caught": 21, "median_wait_s": 147}

    def page_with(body, asked):
        page = browser.new_page()
        page.add_init_script(f"""localStorage.setItem("magoun.walk", "{WALK_S}");
                                 localStorage.removeItem("magoun.berths");""")
        stub(page, live)
        page.route(re.compile(r"ntfy\.sh"), lambda r: r.fulfill(status=200, json={}))

        def backend(route):
            u = route.request.url
            asked.append(u)
            if "/today" in u:
                if body is None:
                    return route.abort()
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps(body))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"as_of": time.time(), "trips": [],
                                           "ttl_s": 60, "stale_after_s": 600}))
        page.route(re.compile(host), backend)
        page.goto(BOARD)
        page.wait_for_timeout(6000)
        return page

    asked = []
    page = page_with(payload, asked)
    print("\n  how trains have run today, from the backend")
    check("the board asked the backend for today's score",
          any("/today" in u for u in asked),
          " ".join(u.split("/")[-1] for u in asked[:3]))
    check("and sent its own walk, not the published default",
          any(f"walk={WALK_S / 60:.2f}" in u for u in asked),
          next((u for u in asked if "/today" in u), "")[-30:])
    check("the panel is shown", not page.is_hidden("#hist"))
    window = page.inner_text("#histWindow")
    bars = page.inner_text("#histBars").replace("\n", " ")
    note = page.inner_text("#histNote").replace("\n", " ")
    print(f"  {window}\n  {bars[:90]}\n  {note[:100]}")
    check("it says today, and how many trains", "today" in window and "47" in window,
          window)
    # 13/47 = 28%, 23/47 = 49%, 11/47 = 23%. Percentages of the trains scored, not
    # of anything else: three numbers that do not add up are a different question's
    # answer.
    check("the three buckets are percentages of today's trains",
          all(p in bars for p in ("28%", "49%", "23%")), bars[:80])
    check("and they are named in minutes, from the threshold the backend used",
          "2 min early" in bars and "within 2 min" in bars and "2 min late" in bars,
          bars[:80])
    check("the catch rate and the median wait are stated as a sentence",
          "45%" in note and "2.5 min" in note, note[:90])
    # The bars are a magnitude each, not one stacked bar in three colours: this
    # page's amber and green are 4.2 apart under deuteranopia in light mode, and its
    # green and red 0.7 apart in dark, so touching segments would read as one shape.
    widths = page.eval_on_selector_all(
        "#histBars .bfill", "es => es.map(e => e.style.width)")
    check("each bucket is drawn as its own bar", len(widths) == 3, str(widths))
    check("and the bars are the percentages, not a fixed shape",
          widths == ["28%", "49%", "23%"], str(widths))
    page.close()

    # A holed record is not a footnote: arrivals are STOPPED_AT transitions, so a
    # minute of missing snapshots loses whole trains and the day scores worse than
    # it ran. A storm weekend is exactly when this matters.
    holed = dict(payload, gap_s=900, max_gap_s=600)
    page = page_with(holed, [])
    check("a holed capture is said out loud, not swallowed",
          "not recorded" in page.inner_text("#histWindow"),
          page.inner_text("#histWindow"))
    page.close()

    # Too few trains: a percentage of two is a rounding artifact, not a measurement.
    page = page_with(dict(payload, trains=2, early=1, close=1, late=0), [])
    check("and it hides rather than make a percentage out of two trains",
          page.is_hidden("#hist"))
    page.close()

    page = page_with(None, [])
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)[:150]))
    check("with the backend unreachable the panel hides",
          page.is_hidden("#hist"))
    check("and the board itself is unaffected",
          bool(page.inner_text("#heroBig").strip()) and not errs,
          "; ".join(errs[:2]))
    page.close()


def upgrade_scenario(browser, check) -> None:
    """A board that was set up before the handoff existed.

    Everyone who has ever used the bell already has `magoun.ntfy` set, so a question
    asked only on first run never runs again for them. Asking for the command topic
    only there meant the handoff silently never happened for the entire population
    the feature is for -- and the main scenario below cannot see it, because it
    seeds `magoun.cmd` itself.
    """
    fixture = sorted((ROOT / "tests" / "fixtures").glob("cases-*.json"))[0]
    case = json.loads(fixture.read_text())[0]
    live = shifted(case, time.time() - case["now"] + 30)
    page, posts, dialogs = _settings_page(browser, live, f"""
      localStorage.clear();
      localStorage.setItem("magoun.ntfy", "smoke-test-topic");
      localStorage.setItem("magoun.walk", "{WALK_S}");
    """)
    print("\n  a board set up before the handoff existed")
    page.click("#bell")
    page.wait_for_timeout(500)
    check("it asks an existing install for the command topic",
          not page.is_hidden("#panel")
          and "command topic" in page.inner_text("#setneed").lower(),
          page.inner_text("#setneed")[:60])
    check("and did not arm on the way", not posts, f"{len(posts)} posts")
    page.fill("#setcmd", "smoke-upgrade-cmd")
    page.click("#setform button[type=submit]")
    page.wait_for_timeout(500)
    check("and remembers it",
          page.evaluate("localStorage.getItem('magoun.cmd')") == "smoke-upgrade-cmd",
          str(page.evaluate("localStorage.getItem('magoun.cmd')")))
    page.click("#setclose")
    posts.clear()
    page.click("#bell")                      # now it arms, and hands off
    page.wait_for_timeout(3000)
    check("so the handoff then reaches the command topic",
          any("smoke-upgrade-cmd" in u for u in posts),
          f"{len(posts)} ntfy posts")
    # Answered once, whatever the answer: a board that asks every time is a board
    # whose owner stops reading what it asks.
    page.click("#bell")                      # cancels
    page.wait_for_timeout(300)
    page.click("#bell")                      # arms again, and must not ask
    page.wait_for_timeout(300)
    check("and never asks again", page.is_hidden("#panel"))
    page.close()


def served_scenario(browser, check) -> None:
    """The same page over HTTP from the directory root: the deployed shape.

    Everything else here runs from file://, where a sibling file cannot be fetched
    at all. Pages serves web/ over HTTP from a directory, so the assets that a
    file:// board reaches by fallback -- model.json rather than model.js -- are only
    exercised here, and so is the icon a home-screen shortcut asks for.
    """
    import functools
    import http.server
    import socketserver
    import threading

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
        # The directory root, not the filename: the whole point of the rename is
        # that https://<pages>/mbta-glx/ serves the board.
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_timeout(5000)

        print(f"\n  serving web/ over HTTP on {port} (the deployed shape)")
        check("the board paints when served from the directory root",
              bool(page.inner_text("#heroBig").strip()))
        # From file:// the model arrives as a script; over HTTP it is fetched, and
        # this is the only run that takes that branch.
        check("the model was fetched, not injected as a script",
              page.evaluate("!!document.querySelector('script[src=\"model.js\"]') === false"))
        icon = page.evaluate("""async () => {
            const el = document.querySelector('link[rel="apple-touch-icon"]');
            if (!el) return "no apple-touch-icon";
            const r = await fetch(el.getAttribute("href"));
            return r.ok ? r.headers.get("content-type") : `HTTP ${r.status}`;
        }""")
        # A home-screen shortcut with a 404 behind it gets a screenshot of the page,
        # which at icon size is a grey smear -- and nothing in the browser says so.
        check("the home-screen icon is actually served", "image/png" in (icon or ""),
              str(icon))
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
        who = page.inner_text("#heroWho").replace("\n", " ")
        foot = page.inner_text("#foot")
        print(f"  hero: {hero}  |  {sub[:60]}  |  {who[:60]}")
        print(f"  foot: {foot[:130]}\n")

        check("the page loads with no script error", not errors, "; ".join(errors[:2]))
        check("a countdown or a due train is shown",
              bool(re.fullmatch(r"-?\d+:\d\d", hero) or hero == "due"), hero)
        # No symmetric +/- : lo and hi are fitted q10/q90 offsets that sit well
        # off centre, so one number for both sides invented an early side.
        check("the hero quotes a clock time and no invented symmetric band",
              "±" not in sub and bool(re.search(r"\d+:\d\d", sub)), sub[:70])
        # Where the train IS, read off data.line -- the array the map is drawn
        # from -- not the tier that produced the prediction.
        check("it says where that train actually is", bool(who.strip()), who[:70])
        veh = page.evaluate("data.next && data.next.vehicle")
        if veh:
            check("and names the train, so hero and map cannot disagree",
                  bool(re.search(r"\b3\d\d\d\b", page.inner_text("#heroLabel"))),
                  page.inner_text("#heroLabel"))
        check("the feed reads live, not stale", foot.startswith("live"))
        check("the headway is stated as what it means for the rider",
              "a train every" in foot, foot[-60:])
        check("following trains are listed",
              page.eval_on_selector_all(".row", "e => e.length") >= 1)
        # Four rows was a cap, not a limit of the data: the list scrolls now, and
        # the timetable tier reaches an hour out.
        check("the list is not capped at four trains",
              page.evaluate("(data.following || []).length") >= 4,
              f"{page.evaluate('(data.following || []).length')} rows")
        vals = page.eval_on_selector_all(
            "#following .row:not(.skipped) .v", "es => es.map(e => e.textContent)")
        check("later trains are counted in whole minutes, not ticking seconds",
              bool(vals) and all(re.search(r"\u00b7\s*(due|\d+ min)", v) for v in vals),
              " | ".join(v.strip() for v in vals[:2]))
        check("a later train can be armed from its own row",
              page.eval_on_selector_all("#following button[data-arm]",
                                        "e => e.length") >= 1)
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
              "leave alert set" in page.inner_text("#foot"))
        # One alert at a time: ntfy cannot withdraw a scheduled publish, so a
        # second arm would add a stale "Leave now" rather than move the first.
        check("and no other row offers to arm a second one",
              page.eval_on_selector_all("button[data-arm]", "e => e.length") == 1)
        # No backend stubbed in this scenario, so /today goes unanswered and the
        # panel hides -- the same degradation as a phone off the tailnet.
        check("the self-score panel hides itself with no backend answering",
              page.is_hidden("#hist"))
        dwell_scenario(browser, check)
        today_scenario(browser, check)
        firstrun_scenario(browser, check)
        upgrade_scenario(browser, check)
        skip_scenario(browser, check)
        capture_scenario(browser, check)
        ratelimit_scenario(browser, check)
        served_scenario(browser, check)
        browser.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("all checks passed: the board runs from file:// with no backend, "
          "and from the directory root the way Pages serves it")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, default=0)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.case, a.headed))
