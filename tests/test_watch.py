"""The notifier's tick, driven end to end over a scripted feed.

`match_target` and `count_arrivals` were tested; everything built on top of them was
not, because the tick reads the clock, the network and the plan file. Nothing
exercised the path that actually pushes -- which is how `now` came to be read on
watch.py's revision branch two lines before it was assigned. Every tick that tried
to announce a slip raised UnboundLocalError, `main` swallowed it as a bad tick, and
the leave-now below it never ran either. A train slipping is the one case M5 exists
for, so it is the one case that has to be driven, not reasoned about.

The three seams are stubbed and nothing else: where the snapshot comes from, what
`etas` makes of it, and where a push goes. The plan file, the matching, the
debounce and the decision to fire are the real code.
"""
import os
import pathlib
import threading
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("MAGOUN_NTFY_TOPIC", "test")
os.environ.setdefault("MAGOUN_NTFY_CMD", "test-cmd")

import watch  # noqa: E402

HEADWAY = 528.0


def row(eta, *, vehicle=None, source="mbta", band=75, skipped=False):
    return {"eta": eta, "lo": eta - band, "hi": eta + band, "source": source,
            "vehicle": vehicle, "backed": True, "skipped": skipped}


class Feed:
    """The clock, the rows and the push -- the only three things stubbed."""

    def __init__(self):
        self.now = float(int(time.time()))   # whole seconds: `arm` carries an int
        self.rows: list[dict] = []
        self.sent: list[dict] = []

    # seam 1+2: the snapshot and the berth state the tick would have fetched
    def snapshot(self):
        return {"t": self.now, "preds": [], "vehicles": []}, {}

    # seam 3: what compute_rows would have made of it
    def etas(self, snap, *a, **k):
        return list(self.rows)

    # seam 4: the push
    def send(self, title, message, **k):
        self.sent.append({"title": title, "message": message, **k})
        return True

    @property
    def titles(self):
        return [s["title"] for s in self.sent]


@pytest.fixture
def feed(tmp_path, monkeypatch):
    f = Feed()
    monkeypatch.setattr(watch, "STATE", tmp_path / "plan.json")
    monkeypatch.setattr(watch.service, "etas", f.etas)
    monkeypatch.setattr(watch.notify, "send", f.send)
    # brief.options asks whether an alert blocks the destination, which fetches.
    # No test here is about alerts, and a suite that reaches the network is a suite
    # that fails on a train.
    monkeypatch.setattr(watch.service, "alerts", lambda *a, **k: [])
    return f


@pytest.fixture
def w(feed, monkeypatch):
    """A Watcher whose only stub is where a snapshot comes from."""
    watcher = watch.Watcher()
    monkeypatch.setattr(watcher, "_snapshot", feed.snapshot)
    return watcher


def arm(feed, target, *, vehicle=None, walk=390, fired=None):
    """Write the plan a commitment leaves behind, as `commit` would have."""
    p = {"dest": "70199", "deadline": feed.now + 5400, "conf": 0.90, "walk": walk,
         "committed": {"target_eta": target, "original_eta": target,
                       "vehicle": vehicle, "at": feed.now, "misses": 0},
         "left_at": None, "fired": dict(fired or {}), "created": feed.now,
         "options": []}
    watch.save(p)
    return p


# --- bug: `now` was read on the revision branch before it was assigned ---

def test_a_slipping_train_is_still_tracked_after_the_page_is_gone(w, feed):
    """M5's done-when: armed, abandoned, and the train slips five minutes."""
    eta = feed.now + 1200
    arm(feed, eta, vehicle="G-10065")
    feed.rows = [row(eta, vehicle="G-10065")]
    w.tick()
    assert feed.titles == [], "an on-time train has nothing to announce"

    feed.now += 120
    feed.rows = [row(eta + 300, vehicle="G-10065")]
    w.tick()

    assert feed.titles == ["Your train is running late"]
    com = watch.load()["committed"]
    assert com["target_eta"] == eta + 300, "the plan must follow the train it slipped to"
    assert com["vehicle"] == "G-10065"


def test_revisions_keep_coming_while_the_eta_keeps_moving(w, feed):
    """Three moves, three messages: the sequence launch-plan.md specifies."""
    eta = feed.now + 1800
    arm(feed, eta, vehicle="G-10065")
    for delta in (0, 300, 120, 240):
        feed.now += watch.REVISE_GAP + 10
        feed.rows = [row(eta + delta, vehicle="G-10065")]
        w.tick()
    assert feed.titles == ["Your train is running late",
                           "Updated arrival time", "Updated arrival time"]
    assert "min vs your pick" in feed.sent[-1]["message"]


def test_a_revision_cannot_spam(w, feed):
    """REVISE_GAP holds even when the ETA is thrashing every tick."""
    eta = feed.now + 1800
    arm(feed, eta, vehicle="G-10065")
    for delta in (300, 600, 900):
        feed.now += watch.TICK
        feed.rows = [row(eta + delta, vehicle="G-10065")]
        w.tick()
    assert len(feed.sent) == 1


def test_leave_now_fires_on_the_tick_that_slipped(w, feed):
    """The announce branch sits above leave-now; an exception there ate both.

    So the tick that matters most -- the ETA moved *and* it is time to go -- is the
    one where the bug cost the rider the train rather than merely a message.
    """
    walk, band = 390, 75
    t0 = feed.now
    arm(feed, t0 + walk + 600, vehicle="G-10065", walk=walk)
    feed.rows = [row(t0 + walk + 900, vehicle="G-10065", band=band)]
    w.tick()
    assert feed.titles == ["Your train is running late"]

    feed.now = t0 + 1000                       # past lo - walk for the slipped ETA
    feed.rows = [row(t0 + walk + 1020, vehicle="G-10065", band=band)]
    w.tick()
    assert feed.titles == ["Your train is running late", "Updated arrival time",
                           "Leave now"]
    assert watch.load()["fired"]["leave"] is True


def test_leave_now_waits_for_the_low_quantile_not_the_median(w, feed):
    """lo - walk is the leave time. Quoting eta would cost a minute nine times in ten."""
    walk, band = 390, 75
    eta = feed.now + walk + band + 2 * watch.TICK
    arm(feed, eta, walk=walk)
    feed.rows = [row(eta, band=band)]
    w.tick()
    assert feed.titles == [], "still one tick early against lo - walk"
    feed.now += 2 * watch.TICK
    w.tick()
    assert feed.titles == ["Leave now"]


# --- launch-plan.md:460 -- "a commitment is no longer dropped when the match is
#     lost". The code that does that was written and never called: tick sent the
#     debounce straight to _recover, which sets committed = None. ---

def test_a_vanished_train_is_re_pointed_rather_than_dropped(w, feed):
    """The 9.6% case. After the debounce the plan follows the next real train."""
    eta = feed.now + 1200
    later = eta + HEADWAY                      # one headway on: flap or no-show?
    arm(feed, eta, vehicle="G-10065")
    feed.rows = [row(later, vehicle="G-10199")]
    for _ in range(watch.MISS_TICKS):
        feed.now += watch.TICK
        w.tick()

    p = watch.load()
    assert p["committed"] is not None, "silence is not a statement; do not drop it"
    assert p["committed"]["target_eta"] == later
    assert feed.titles == ["Your train may be running late"]
    assert "Show options" in [a["label"] for a in feed.sent[0]["actions"]]

    feed.now += watch.TICK                     # and it keeps tracking from there
    feed.rows = [row(later, vehicle="G-10199")]
    w.tick()
    assert watch.load()["committed"]["misses"] == 0


def test_the_debounce_rides_out_a_flap_without_saying_anything(w, feed):
    """MISS_TICKS * TICK is 240 s against a measured 90 s flap. Nothing is said."""
    eta = feed.now + 1200
    arm(feed, eta, vehicle="G-10065")
    for _ in range(watch.MISS_TICKS - 1):
        feed.now += watch.TICK
        feed.rows = []
        w.tick()
    feed.now += watch.TICK
    feed.rows = [row(eta, vehicle="G-10065")]  # back, as a flap comes back
    w.tick()
    assert feed.titles == []
    assert watch.load()["committed"]["misses"] == 0


def test_nothing_left_to_follow_drops_the_commitment_and_says_so(w, feed):
    """The one honest case for dropping it -- and it still owes an explanation."""
    eta = feed.now + 1200
    arm(feed, eta, vehicle="G-10065")
    for _ in range(watch.MISS_TICKS):
        feed.now += watch.TICK
        feed.rows = []
        w.tick()
    p = watch.load()
    assert p["committed"] is None
    assert feed.titles == ["That train vanished"]


def test_a_skipped_train_is_never_adopted(w, feed):
    """Adopting one would point the plan at a train that passes the platform."""
    eta = feed.now + 1200
    arm(feed, eta, vehicle="G-10065")
    feed.rows = [row(eta + HEADWAY, vehicle="G-10199", skipped=True)]
    for _ in range(watch.MISS_TICKS):
        feed.now += watch.TICK
        w.tick()
    assert watch.load()["committed"] is None
    assert feed.titles == ["That train vanished"]


def test_a_stated_skip_does_not_wait_out_the_debounce(w, feed):
    """MBTA said it. That is evidence, not silence, so act on the first tick."""
    eta = feed.now + 1200
    arm(feed, eta, vehicle="G-10065")
    feed.rows = [row(eta, vehicle="G-10065", skipped=True)]
    w.tick()
    assert feed.titles == ["That train vanished"]
    assert watch.load()["committed"] is None


# --- M5: the board's bell hands its alert to the notifier ---
#
# The bell schedules an ntfy push and, while the page is open, re-points it every
# minute. Measured against ntfy.sh, a re-point is a NEW delivery -- Sequence-ID does
# not replace a pending message and `delete` does not unschedule one -- so the page
# cannot refine anything after it closes, and queueing more pushes is not refinement.
# The handoff is a command on the topic on_command already listens to.

def test_the_bell_hands_its_train_to_the_notifier(w, feed):
    eta = feed.now + 1200
    w.on_command(f"arm {int(eta)} G-10065", {})
    com = watch.load()["committed"]
    assert com["target_eta"] == eta and com["vehicle"] == "G-10065"


def test_an_armed_alert_tracks_a_slip_with_no_page_open(w, feed):
    """M5's done-when, by the path the board actually arms through."""
    eta = feed.now + 1200
    w.on_command(f"arm {int(eta)} G-10065", {})
    feed.rows = [row(eta, vehicle="G-10065")]
    w.tick()
    feed.now += 120
    feed.rows = [row(eta + 300, vehicle="G-10065")]
    w.tick()
    assert feed.titles == ["Your train is running late"]
    assert watch.load()["committed"]["target_eta"] == eta + 300


def test_an_armed_alert_recovers_without_a_destination(w, feed):
    """A bare arm has no deadline, so recovery quotes a train, not a probability.

    brief.options needs a destination; reaching it with None raises inside the
    tick, where main swallows it -- the recovery would simply never arrive.
    """
    eta = feed.now + 1200
    w.on_command(f"arm {int(eta)} G-10065", {})
    feed.rows = [row(eta + HEADWAY, vehicle="G-10199", skipped=True)]
    for _ in range(watch.MISS_TICKS):
        feed.now += watch.TICK
        w.tick()
    assert feed.titles == ["That train vanished"]

    # ...and with something left to offer, it names it and offers to track it.
    feed.sent.clear()
    w.on_command(f"arm {int(eta)} G-10065", {})
    feed.rows = [row(eta, vehicle="G-10065", skipped=True),
                 row(eta + HEADWAY, vehicle="G-10199")]
    w.tick()
    assert len(feed.sent) == 1
    msg = feed.sent[0]
    assert "Best now is" in msg["message"] and "leave" in msg["message"]
    assert "%" not in msg["message"], "no deadline means no P(on time) to quote"
    assert "Track it" in [a["label"] for a in msg["actions"]]


def test_a_bare_arm_still_fires_leave_now(w, feed):
    """_detail needs a destination. Returning None must not cost the push."""
    walk, band = 390, 75
    eta = feed.now + walk + band
    w.on_command(f"arm {int(eta)} G-10065", {})
    feed.rows = [row(eta, vehicle="G-10065", band=band)]
    w.tick()
    assert feed.titles == ["Leave now"]
    assert "catch" not in feed.sent[0]["message"], "no destination, no odds"


def test_arm_refuses_what_is_not_a_future_epoch(w, feed):
    for bad in ("arm 08:21", "arm", f"arm {int(feed.now - 60)}"):
        w.on_command(bad, {})
        assert watch.load() == {}, f"{bad!r} should not have armed anything"


def test_the_board_hands_off_on_the_command_topic_it_already_listens_to(w):
    """No new endpoint and no widened CORS: the reply path M2 already built."""
    board = (pathlib.Path(__file__).resolve().parent.parent
             / "web" / "board.html").read_text()
    assert "magoun.cmd" in board, "the bell must know the command topic"
    assert "`arm ${Math.round(next.eta)}" in board
    assert "/live-extras.json" not in board.split("armAlert")[0]


def test_re_arming_the_same_train_does_not_reset_the_plan(w, feed):
    """An open tablet sends `arm` every minute. A fresh plan each time would
    forget that leave-now had fired, and fire it again on the next tick."""
    walk, band = 390, 75
    eta = feed.now + walk + band
    w.on_command(f"arm {int(eta)} G-10065", {})
    feed.rows = [row(eta, vehicle="G-10065", band=band)]
    w.tick()
    assert feed.titles == ["Leave now"]

    for _ in range(3):                       # the page, still open, re-arms
        feed.now += 60
        w.on_command(f"arm {int(eta)} G-10065", {})
        w.tick()
    assert feed.titles == ["Leave now"], "one train, one leave-now"


def test_arming_a_different_train_starts_over(w, feed):
    """Tapping the bell for the next train is a new commitment, not a refresh."""
    eta = feed.now + 600
    w.on_command(f"arm {int(eta)} G-10065", {})
    watch.save({**watch.load(), "fired": {"leave": True}})
    later = eta + 2 * HEADWAY
    w.on_command(f"arm {int(later)} G-10199", {})
    p = watch.load()
    assert p["committed"]["target_eta"] == later
    assert p["committed"]["vehicle"] == "G-10199"
    assert p["fired"] == {}, "a different train has not been left for"


# --- the command thread and the tick thread both write plan.json ---

def test_a_tap_arriving_mid_tick_is_not_overwritten(w, feed, monkeypatch):
    """`On my way` is what unlocks the mid-walk adjust. Losing it loses that push.

    The tap arrives on the ntfy subscription thread while a tick is in flight on
    the main one. Holding the lock only across `load()` left the tick free to save
    its own copy over the tap.
    """
    eta = feed.now + 1200
    arm(feed, eta, vehicle="G-10065")
    feed.rows = [row(eta, vehicle="G-10065")]

    threads = []

    def tap_during(snap, *a, **k):
        # The tick is inside the lock here. Give the tap every chance to land
        # first: unlocked, it completes, and then the tick saves over it.
        t = threading.Thread(target=w.on_command, args=("left", {}))
        t.start()
        t.join(timeout=0.5)
        threads.append(t)
        return list(feed.rows)

    monkeypatch.setattr(watch.service, "etas", tap_during)
    w.tick()
    threads[0].join(timeout=2)
    assert watch.load()["left_at"] is not None, "the tick saved over the tap"


def test_a_bad_command_does_not_kill_the_button_thread(w, feed):
    """An exception here escapes into notify.watch_commands and ends the
    subscription -- the notifier keeps ticking with every button silently dead."""
    eta = feed.now + 1200
    arm(feed, eta)
    for junk in ("pick zero", "pick 99", "", "arm", "nonsense", "pick"):
        w.on_command(junk, {})
    w.on_command("left", {})
    assert watch.load()["left_at"] is not None, "the handler stopped working"


# --- the mid-walk adjust: launch-plan.md's "start jogging / ease up" ---

def test_the_adjust_fires_once_the_train_is_moving_and_sharp(w, feed):
    """Only from the two tiers with a tight band -- +/-33 s and +/-7 s."""
    walk = 390
    eta = feed.now + 300
    arm(feed, eta, vehicle="G-10065", walk=walk, fired={"leave": True})
    watch.save({**watch.load(), "left_at": feed.now - 120})

    feed.rows = [row(eta, vehicle="G-10065", source="mbta", band=75)]
    w.tick()
    assert feed.titles == [], "an mbta row at +/-75 s is not sharp enough to jog on"

    feed.rows = [row(eta, vehicle="G-10065", source="departed Ball Sq", band=7)]
    w.tick()
    assert len(feed.sent) == 1
    assert "min to your train" in feed.sent[0]["title"]
    assert "s of slack" in feed.sent[0]["message"]

    feed.rows = [row(eta, vehicle="G-10065", source="departed Ball Sq", band=7)]
    w.tick()
    assert len(feed.sent) == 1, "the adjust fires once, not every tick"


def test_the_adjust_says_which_way_to_lean(w, feed):
    """A countdown, not prose: the verb has to change with the slack."""
    walk = 390
    said = {}
    for label, left_ago, offset in (("fine", 120, 400), ("hurry", 120, 150)):
        feed.sent.clear()
        eta = feed.now + offset
        arm(feed, eta, vehicle="G-10065", walk=walk, fired={"leave": True})
        watch.save({**watch.load(), "left_at": feed.now - left_ago})
        feed.rows = [row(eta, vehicle="G-10065", source="departed Medford/Tufts",
                         band=33)]
        w.tick()
        said[label] = feed.sent[0]["message"] if feed.sent else ""
    assert "you're fine" in said["fine"], said["fine"]
    assert ("pick it up" in said["hurry"] or "you'll miss it" in said["hurry"]), \
        said["hurry"]


def test_no_adjust_until_the_rider_says_they_left(w, feed):
    """Without a reference point the slack is invented. A missed nudge beats a
    wrong one -- launch-plan.md's resolved decision on how it knows you left."""
    eta = feed.now + 300
    arm(feed, eta, vehicle="G-10065", fired={"leave": True})
    feed.rows = [row(eta, vehicle="G-10065", source="departed Ball Sq", band=7)]
    w.tick()
    assert feed.titles == []
