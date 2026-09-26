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
        self.now = time.time()
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
