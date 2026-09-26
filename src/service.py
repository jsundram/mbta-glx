"""Live inbound ETAs with error bars for Magoun Square (Green Line, toward downtown).

Why this exists: the MBTA publishes ~13 predictions for the Medford/Tufts-bound
platform at Magoun but only reaches 8-13 minutes ahead on the downtown-bound side,
which is not enough notice to leave the house.

How it predicts, in order of sharpness (measured over 35 days of history):
  departed Ball Square   +/-  14 s   ~51 s of lead
  departed Medford/Tufts +/-  65 s   ~190 s of lead
  MBTA's own prediction              up to ~13 min of lead
  schedule + bias        +/- 318 s   unlimited lead
A train sitting at the terminus is NOT used as a clock -- the layover is far too
variable (q10 213 s, q90 1036 s) -- only as evidence the scheduled train exists.
"""
import bisect
import datetime as dt
import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# Everything the notifier needs is configurable, so it can move off this Mac to a
# cloud host without code changes: it depends only on the MBTA API plus a small
# bundle (model.json + today's schedule), never on the raw archive.
ROOT = pathlib.Path(os.environ.get("MAGOUN_ROOT",
                                   pathlib.Path(__file__).resolve().parent.parent))
TZ = ZoneInfo("America/New_York")   # must be a real zone: EDT->EST flips 2026-11-01
MAGOUN_IN, BALL_IN, MED_IN, MED_OUT = "70508", "70510", "70512", "70511"
# The GLX, ordered Medford/Tufts -> Lechmere. Inbound trains run down this list,
# outbound trains run up it, and both share the terminus at the top.
GLX_STOPS = [
    ("Medford/Tufts", "70512", "70511"),
    ("Ball Square", "70510", "70509"),
    ("Magoun Square", "70508", "70507"),
    ("Gilman Square", "70506", "70505"),
    ("East Somerville", "70514", "70513"),
    ("Lechmere", "70502", "70501"),
]
IN_IDX = {sid: i for i, (_, sid, _) in enumerate(GLX_STOPS)}
OUT_IDX = {sid: i for i, (_, _, sid) in enumerate(GLX_STOPS)}
def _config() -> dict:
    """Rider preferences, not fitted values. data/config.json is the one copy.

    This used to be the literal 390 in eight files. It is a preference, so it is
    not in model.json's fitted block -- but fit.py does publish it into the
    constants the board reads, so the board's default walk and the walk the backend
    scores with cannot silently disagree.
    """
    path = ROOT / "data" / "config.json"
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError):
        # A truncated or conflict-mangled file must not take down `import service`,
        # which would restart-loop watch.py under launchd KeepAlive. data/ lives in
        # Dropbox and churns daily.
        return {}
    return cfg if isinstance(cfg, dict) else {}


CONFIG = _config()
# The env var still wins, for one-off experiments without editing the config.
# `or` rather than a default chain: a null walk_s in the config must fall through
# to the literal rather than reaching int(None).
CONFIG_WALK = int(CONFIG.get("walk_s") or 390)      # the config's walk, override ignored
DEFAULT_WALK_ENV = int(os.environ.get("MAGOUN_WALK_S") or CONFIG_WALK)
KEY = os.environ.get("MBTA_API_KEY")
VETO_WINDOW = 480        # a scheduled train this close with nothing upstream is a no-show
DEFAULT_WALK = DEFAULT_WALK_ENV
STALE_VEHICLE = 180      # a position older than this is a parked ghost, not live service
BERTH_STATE = ROOT / "data" / "berth.json"


def _get(path: str, params: dict, tries: int = 3) -> dict:
    url = f"https://api-v3.mbta.com/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": KEY} if KEY else {})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * 2 ** i)
    raise RuntimeError("unreachable")


class Model:
    def __init__(self, path: pathlib.Path | None = None):
        self.m = json.loads((path or ROOT / "data" / "model.json").read_text())
        self.grid = self.m["grid"]

    def _q(self, arr: list[float], q: float) -> float:
        i = min(range(len(self.grid)), key=lambda k: abs(self.grid[k] - q))
        return arr[i]

    def residual(self, tier: str, elapsed: float, q: float) -> float | None:
        """Remaining seconds to Magoun at quantile q, given `elapsed` already passed."""
        d = self.m["tiers"][tier]["q"]
        i = bisect.bisect_right(d, elapsed)
        surv = d[i:]
        if len(surv) < 4:
            return None
        k = min(int(q * len(surv)), len(surv) - 1)
        return max(surv[k] - elapsed, 0.0)

    def sched_offset(self, q: float) -> float:
        return self._q(self.m["sched"]["q"], q)

    def berth_offset(self, q: float) -> float:
        return self._q(self.m["berth"]["q"], q)

    def pred_offset(self, lead: float, q: float) -> float:
        """How much later than MBTA's own prediction the train actually turns up.

        Fitted (fit.py `pred`), asymmetric, and a function of how far ahead the
        prediction was made. It replaced three eyeballed symmetric half-widths --
        +/-75 s, +/-33 s, +/-7 s -- which were the only numbers in the model that
        nothing had measured. Measured, the train is late at EVERY lead, so the
        early side of a symmetric band was pure pessimism: 75 s of platform wait
        per trip that never had to happen.

        Interpolated between bin centres rather than stepped. A step would move
        the quoted ETA by tens of seconds the moment a train's lead crossed a bin
        edge, which on the board is indistinguishable from the feed flapping.
        """
        p = self.m["pred"]
        xs, bins = p["lead_s"], p["bins"]
        if lead <= xs[0]:
            return self._q(bins[0]["q"], q)
        if lead >= xs[-1]:
            return self._q(bins[-1]["q"], q)
        i = bisect.bisect_right(xs, lead) - 1
        f = (lead - xs[i]) / (xs[i + 1] - xs[i])
        a, b = self._q(bins[i]["q"], q), self._q(bins[i + 1]["q"], q)
        return a + (b - a) * f

    @property
    def berth_const(self) -> tuple[int, int]:
        b = self.m["berth"]
        return b["turn_plus_run"], b["sched_bias"]

    def const(self, name: str, default=None):
        """Constants live in model.json so both implementations read one source."""
        return self.m.get("constants", {}).get(name, default)

    @property
    def headway(self) -> float:
        return self.m["headway_median_s"]


class ArrivalTracker:
    """When each train first appeared stopped at a platform.

    The vehicle feed refreshes a stopped train's timestamp, so it cannot say when
    the train got there; that has to be observed across polls.
    """

    GRACE = 75.0     # a vehicle missing from one poll has not necessarily left

    def __init__(self):
        self.at: dict[str, float] = {}       # vehicle -> when it arrived
        self.seen: dict[str, float] = {}     # vehicle -> last poll that saw it here
        self.recent: list[float] = []        # arrival times, newest last

    def update(self, snap: dict, stop: str = MAGOUN_IN, direction: int = 0) -> dict:
        """Which trains are stopped here, and since when.

        Deliberately tolerant: a stopped train's position can go stale or drop out
        of a single poll, and treating that as a departure made the dwell counter
        reset to zero and then jump. A vehicle is only considered gone once it has
        been unseen for GRACE seconds; liveness is not required to KEEP a train
        that is already known to be sitting here, only to start counting one.
        """
        now = snap["t"]
        for v in snap["vehicles"]:
            a, rel = v["attributes"], v["relationships"]
            at_our_stop = (a["direction_id"] == direction
                           and _revenue(a)
                           and (rel["stop"]["data"] or {}).get("id") == stop
                           and a["current_status"] == "STOPPED_AT")
            if not at_our_stop:
                # Seen somewhere else, so it has definitely left. Grace applies only
                # to vehicles missing from the feed entirely, not to ones we can see.
                self.at.pop(v["id"], None)
                self.seen.pop(v["id"], None)
                continue
            if v["id"] not in self.at:
                if not _live(a, now):
                    continue                    # do not start counting on a ghost
                self.at[v["id"]] = now
                self.recent.append(now)
                del self.recent[:-12]
            self.seen[v["id"]] = now
        for vid, last in list(self.seen.items()):
            if now - last > self.GRACE:
                self.at.pop(vid, None)
                self.seen.pop(vid, None)
        # Stable order: oldest arrival first, so the display never swaps trains.
        return dict(sorted(self.at.items(), key=lambda kv: kv[1]))


class BerthTracker:
    """Remembers when each train first appeared on the Medford/Tufts inbound platform.

    The vehicle feed refreshes a stopped train's timestamp, so it does not tell us
    when the train berthed -- that has to be observed. State is persisted so a
    server restart does not lose trains already sitting there.

    It does NOT require `current_status == "STOPPED_AT"`, and that is measured
    rather than sloppy. The obvious worry is that a train still rolling into the
    terminus starts the berth clock early, which would make every berth-tier ETA
    optimistic AND disagree with the fitted offsets (build_dataset measures the
    berth from the STOPPED_AT transition, per invariant 2). Over 2026-09-24..25 all
    210 inbound terminus visits were already STOPPED_AT the first time the feed put
    them there, with a zero-second gap at every percentile. So the check would
    change nothing, and adding it would be an unmeasured change to the contract
    that looks like a fix.
    """

    def __init__(self, path: pathlib.Path = BERTH_STATE):
        self.path = path
        try:
            self.seen: dict[str, float] = json.loads(path.read_text())
        except Exception:  # noqa: BLE001 - a missing or corrupt file just starts fresh
            self.seen = {}

    def update(self, snap: dict) -> dict[str, float]:
        now, present = snap["t"], set()
        for v in snap["vehicles"]:
            a, rel = v["attributes"], v["relationships"]
            if not _live(a, now) or not _revenue(a):
                continue
            stop = (rel["stop"]["data"] or {}).get("id")
            if stop == MED_IN and a["direction_id"] == 0:
                present.add(v["id"])
                self.seen.setdefault(v["id"], now)
        for vid in list(self.seen):
            if vid not in present:
                del self.seen[vid]
        try:
            self.path.write_text(json.dumps(self.seen))
        except OSError:
            pass
        return dict(self.seen)


def _live(attrs: dict, now: float, stale_s: float = STALE_VEHICLE) -> bool:
    """False for stale positions: parked, out-of-service trains sit for hours."""
    u = _iso(attrs.get("updated_at"))
    return u is not None and (now - u) <= stale_s


def _revenue(attrs: dict) -> bool:
    """A non-revenue train runs express and cannot be boarded.

    It still appears in the vehicle feed, so without this a deadhead at the
    terminus satisfies the no-show veto and a deadhead passing Magoun reads as
    "train at the station". The v3 API exposes this; the protobuf feed does not.
    """
    return attrs.get("revenue", "REVENUE") != "NON_REVENUE"


def _iso(s: str | None) -> float | None:
    return dt.datetime.fromisoformat(s).timestamp() if s else None


# `t` advances only on success, so it is also the answer to "how old is this set".
# `fail_t` is separate and exists because without it a dead upstream means every
# single call pays a fresh 20 s timeout: the ttl shortcut below is keyed on the
# SUCCESS time, which stops moving exactly when the fetches start failing.
_SKIP_CACHE: dict = {"t": 0.0, "trips": set(), "fail_t": 0.0}
SKIP_FAIL_BACKOFF = 30.0


def skipped_trips(stop: str = MAGOUN_IN, ttl: float = 30.0) -> set[str]:
    """Trips MBTA has declared will NOT stop here.

    Only the protobuf feed carries these: the v3 JSON API drops a stop_time_update
    that has no times, which is exactly what a skipped stop looks like. Measured
    lead time is poor -- the marker lands around two minutes before the scheduled
    arrival and lingers for an hour -- so this is a confirmation, not a warning.
    Its value is that it is DEFINITIVE, which the no-show veto never was.
    """
    now = time.time()
    if now - _SKIP_CACHE["t"] < ttl:
        return _SKIP_CACHE["trips"]
    # Back off after a failure. server.py runs single-threaded HTTPServer, so
    # without this a cdn.mbta.com outage serialises a 20 s urlopen per request
    # and /board and /status stop answering too -- the skip set is reached from
    # `etas`, not just from /skips, and the board polls every 10 s.
    if now - _SKIP_CACHE["fail_t"] < SKIP_FAIL_BACKOFF:
        return _SKIP_CACHE["trips"]
    try:
        try:
            from google.transit import gtfs_realtime_pb2 as pb
        except ImportError:
            # Not a bad day upstream -- a launcher missing a dependency, which
            # never fixes itself. Say so once rather than returning an empty set
            # forever, which is what `watch.sh` did from the day it was written.
            if not _SKIP_CACHE.get("warned"):
                _SKIP_CACHE["warned"] = True
                print("skipped_trips: gtfs-realtime-bindings is not installed; "
                      "SKIPPED/CANCELED markers are invisible to this process",
                      file=sys.stderr, flush=True)
            raise
        with urllib.request.urlopen(
                "https://cdn.mbta.com/realtime/TripUpdates.pb", timeout=20) as r:
            msg = pb.FeedMessage()
            msg.ParseFromString(r.read())
        # Both must be scoped to THIS stop. A system-wide CANCELED union pulled in
        # 67 trips, nearly all of them other routes, against ~10 real skips a day.
        trips = {
            e.trip_update.trip.trip_id
            for e in msg.entity
            for su in e.trip_update.stop_time_update
            if su.stop_id == stop and su.schedule_relationship == 1   # SKIPPED
        } | {
            e.trip_update.trip.trip_id
            for e in msg.entity
            if e.trip_update.trip.schedule_relationship == 3          # CANCELED
            and any(su.stop_id == stop for su in e.trip_update.stop_time_update)
        }
    except Exception:  # noqa: BLE001 - absence of this must never break the ETAs
        _SKIP_CACHE["fail_t"] = now
        return _SKIP_CACHE["trips"]
    _SKIP_CACHE.update(t=now, trips=trips, fail_t=0.0)
    return trips


def skip_set(stop: str = MAGOUN_IN, ttl: float = 30.0) -> tuple[set[str], float]:
    """The skip set, and when it was last successfully derived.

    `skipped_trips` hands back the previous set when the protobuf fetch fails,
    which is right for the ETAs -- absence of this must never break them -- but it
    leaves a caller unable to tell a fresh empty set from a stale one. Anything
    serving this onward has to know the difference: "no skips" and "I cannot see
    skips" are different answers, and only the second one should make a board stop
    striking trains through. 0.0 means never successfully fetched.
    """
    trips = skipped_trips(stop, ttl)
    return trips, _SKIP_CACHE["t"]


def schedule_today(day: dt.date) -> list[float]:
    cache = ROOT / "data" / "sched" / f"{day}.json"
    if cache.exists():
        rows = json.loads(cache.read_text())
    else:
        body = _get("schedules", {"filter[stop]": MAGOUN_IN, "filter[date]": str(day),
                                  "filter[direction_id]": "0", "page[limit]": "500"})
        rows = [{"trip": s["relationships"]["trip"]["data"]["id"],
                 "arr": s["attributes"]["arrival_time"],
                 "dep": s["attributes"]["departure_time"]} for s in body["data"]]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rows))
    return sorted(t for t in (_iso(r["arr"] or r["dep"]) for r in rows) if t)


_SCHED_ROWS: dict = {}


def schedule_rows(day: dt.date) -> list[tuple[float, str]]:
    """(scheduled arrival, trip_id) so a slot can be matched against skips.

    Memoised: the replay harness calls etas() thousands of times a day and the
    file read dominated everything else.
    """
    if day in _SCHED_ROWS:
        return _SCHED_ROWS[day]
    cache = ROOT / "data" / "sched" / f"{day}.json"
    if not cache.exists():
        schedule_today(day)
    try:
        rows = json.loads(cache.read_text())
    except Exception:  # noqa: BLE001
        return []
    out = [(_iso(r.get("arr") or r.get("dep")), r.get("trip")) for r in rows]
    res = sorted((t, tr) for t, tr in out if t)
    _SCHED_ROWS[day] = res
    return res


_ALERT_CACHE: dict = {"t": 0.0, "data": []}


def alerts(ttl: float = 120.0) -> list[dict]:
    """Active and upcoming Green Line alerts, with the stops each one informs.

    Without this the service will happily quote a 90%-confident arrival at a
    station no train is serving. Observed live: a nine-day suspension of Green
    Line service south of North Station, which silently invalidates every
    downtown destination while Magoun itself looks perfectly normal.
    """
    now = time.time()
    if now - _ALERT_CACHE["t"] < ttl:
        return _ALERT_CACHE["data"]
    try:
        body = _get("alerts", {"filter[route]": "Green-B,Green-C,Green-D,Green-E"})
    except Exception:  # noqa: BLE001 - never let an alert lookup break the ETAs
        return _ALERT_CACHE["data"]
    out = []
    for a in body["data"]:
        at = a["attributes"]
        out.append({
            "id": a["id"], "effect": at.get("effect"),
            "severity": at.get("severity"), "lifecycle": at.get("lifecycle"),
            "header": at.get("header"), "short": at.get("service_effect"),
            "stops": sorted({e.get("stop") for e in at.get("informed_entity", [])
                             if e.get("stop")}),
            "periods": [(p.get("start"), p.get("end"))
                        for p in at.get("active_period", [])],
        })
    _ALERT_CACHE.update(t=now, data=out)
    return out


# Every platform a Magoun rider passes through heading downtown. An alert that
# touches none of these is someone else's problem -- showing it trains the eye to
# ignore the banner, which is worse than showing nothing.
CORRIDOR = {
    "70512", "70510", "70508", "70506", "70514", "70502",   # GLX inbound
    "70208", "70207", "70206", "70205",                      # Science Pk, North Sta
    "70204", "70203", "70202", "70201",                      # Haymarket, Govt Ctr
    "70200", "70199", "70198", "70197", "70196",             # Park St
    "70159", "70158", "70155", "70154",                      # Boylston, Copley
    "place-mgngl", "place-balsq", "place-mdftf", "place-gilmn",
    "place-esomr", "place-lech", "place-north", "place-gover",
    "place-pktrm", "place-haecl", "place-boyls", "place-coecl",
}


def relevant(al: list[dict] | None = None) -> list[dict]:
    """Alerts touching the Magoun-to-downtown corridor, worst first."""
    out = []
    for a in (al if al is not None else alerts()):
        stops = set(a["stops"])
        # A route-wide alert carries no stops at all; keep it only if it is severe.
        hits = bool(stops & CORRIDOR) or (not stops and (a["severity"] or 0) >= 7)
        if hits:
            out.append(a)
    return sorted(out, key=lambda a: -(a["severity"] or 0))


def blocking(stop: str, when: float | None = None,
             al: list[dict] | None = None) -> list[dict]:
    """Alerts that stop you reaching `stop` at time `when`."""
    when = when or time.time()
    out = []
    for a in (al if al is not None else alerts()):
        if a["effect"] not in ("SUSPENSION", "STATION_CLOSURE", "NO_SERVICE"):
            continue
        if stop not in a["stops"]:
            continue
        for start, end in a["periods"] or [(None, None)]:
            s0 = _iso(start) if start else 0
            s1 = _iso(end) if end else float("inf")
            if s0 <= when <= s1:
                out.append(a)
                break
    return out


def snapshot() -> dict:
    preds = _get("predictions", {
        "filter[stop]": ",".join([MAGOUN_IN, BALL_IN, MED_IN, MED_OUT]),
        "sort": "arrival_time"})
    veh = _get("vehicles", {"filter[route]": "Green-B,Green-C,Green-D,Green-E"})
    return {"t": time.time(), "preds": preds["data"], "vehicles": veh["data"]}


def line_map(snap: dict) -> list[dict]:
    """Where every Green Line train sits on the GLX, as a fractional stop index.

    A train IN_TRANSIT_TO / INCOMING_AT a stop is drawn half a segment before it,
    which is what makes the map read as movement rather than a row of dots.
    """
    out = []
    for v in snap["vehicles"]:
        a, rel = v["attributes"], v["relationships"]
        stop = (rel["stop"]["data"] or {}).get("id")
        if stop is None:
            continue
        inbound = a["direction_id"] == 0
        idx = (IN_IDX if inbound else OUT_IDX).get(stop)
        if idx is None:
            continue
        stopped = a["current_status"] == "STOPPED_AT"
        # Inbound runs down the list, outbound runs up it.
        pos = idx if stopped else (idx - 0.5 if inbound else idx + 0.5)
        # `label` is the real coupled car numbers, e.g. "3677-3875"; the lead car
        # is what is painted on the front of the train you actually board.
        cars = [c.get("label") for c in (a.get("carriages") or []) if c.get("label")]
        out.append({
            "id": v["id"], "dir": 0 if inbound else 1, "pos": round(pos, 2),
            "stopped": stopped, "stale": not _live(a, snap["t"]),
            "revenue": _revenue(a),
            "label": a.get("label"), "car": cars[0] if cars else None,
            "route": (rel["route"]["data"] or {}).get("id"),
        })
    return out


def upstream_state(snap: dict, stale_s: float = STALE_VEHICLE) -> dict:
    """Where each inbound GLX train is right now, from vehicle positions."""
    out = {"departed_ball": [], "departed_med": [], "at_terminus": 0,
           "ghosts": 0, "non_revenue": 0}
    for v in snap["vehicles"]:
        a, rel = v["attributes"], v["relationships"]
        stop = (rel["stop"]["data"] or {}).get("id")
        if not _live(a, snap["t"], stale_s):
            out["ghosts"] += 1
            continue
        if not _revenue(a):
            out["non_revenue"] += 1
            continue
        if a["direction_id"] != 0:
            # Outbound train sitting at / approaching the Medford/Tufts terminus.
            if stop in (MED_OUT,):
                out["at_terminus"] += 1
            continue
        if stop == MAGOUN_IN and a["current_status"] in ("IN_TRANSIT_TO", "INCOMING_AT"):
            out["departed_ball"].append(v["id"])
        elif stop == BALL_IN and a["current_status"] in ("IN_TRANSIT_TO", "INCOMING_AT"):
            out["departed_med"].append(v["id"])
        elif stop == MED_IN:
            out["at_terminus"] += 1
    return out


def compute_rows(now: float, preds: list, vehicles: list, model: Model,
                 walk: int, qs, horizon: int, berths: dict,
                 slots: list, skipped: set) -> list[dict]:
    """Pure: everything the prediction needs is an argument, nothing is fetched.

    This is the function a JavaScript frontend must reproduce exactly, so it takes
    the schedule and the skip set as data rather than reading them. Keep it free of
    I/O, clocks and globals -- `tests/fixtures/` pins its behaviour and the same
    fixtures are what a JS port will be checked against.
    """
    ql, qm, qh = qs
    veto = model.const("veto_window_s", VETO_WINDOW)
    dedupe = model.const("dedupe_s", 240)
    min_gap = model.const("min_gap_s", 120)
    state = upstream_state({"t": now, "vehicles": vehicles},
                           stale_s=model.const("stale_vehicle_s", STALE_VEHICLE))
    rows: list[dict] = []

    # 1. MBTA's own inbound predictions -- the operator's model, use it first.
    mbta = []
    for p in preds:
        a, rel = p["attributes"], p["relationships"]
        if rel["stop"]["data"]["id"] != MAGOUN_IN or a["direction_id"] != 0:
            continue
        t = _iso(a.get("arrival_time") or a.get("departure_time"))
        if t and t > now:
            mbta.append((t, (rel.get("vehicle", {}).get("data") or {}).get("id")))
    for t, vid in sorted(mbta):
        src = "mbta"
        if vid and vid in state["departed_ball"]:
            src = "departed Ball Sq"
        elif vid and vid in state["departed_med"]:
            src = "departed Medford/Tufts"
        # MBTA's prediction is the anchor, not the answer. Measured over 11,225
        # paired predictions here, the train is late at every lead -- by 14 s at
        # one minute out and 70 s at eight -- so the fitted quantiles sit around
        # it, off centre, rather than a symmetric guess sitting on top of it.
        lead = t - now
        rows.append({"eta": t + model.pred_offset(lead, qm),
                     "lo": t + model.pred_offset(lead, ql),
                     "hi": t + model.pred_offset(lead, qh),
                     "source": src, "backed": True, "vehicle": vid})

    # 2. Trains already berthed at Medford/Tufts: departure is bounded below by
    #    the physical turnaround, which the timetable cannot express.
    turn_run, bias = model.berth_const
    slot_times = [t for t, _ in slots]
    covered = {r.get("vehicle") for r in rows}
    for vid, berth in sorted(berths.items(), key=lambda kv: kv[1]):
        if vid in covered:
            continue
        nxt = [t for t in slot_times if t + bias > berth]
        base = berth + turn_run
        if nxt:
            base = max(base, nxt[0] + bias)
        mid = base + model.berth_offset(qm)
        if mid <= now or mid > now + horizon:
            continue
        rows.append({"eta": mid, "lo": base + model.berth_offset(ql),
                     "hi": base + model.berth_offset(qh),
                     "source": "berthed at Medford/Tufts", "backed": True,
                     "vehicle": vid})

    # 3. Schedule beyond everything visible, corrected for measured bias.
    last = max([r["eta"] for r in rows], default=now)
    for s, trip in slots_with_trips(slots, skipped):
        mid = s + model.sched_offset(qm)
        if mid <= max(now, last + min_gap) or mid > now + horizon:
            continue
        backed = not (s - now < veto and state["at_terminus"] == 0)
        if any(abs(r["eta"] - mid) < dedupe for r in rows):
            continue
        rows.append({"eta": mid, "lo": s + model.sched_offset(ql),
                     "hi": s + model.sched_offset(qh),
                     "source": "schedule", "backed": backed, "vehicle": None})

    for t in skipped_slot_times(slots, skipped, now):
        rows.append({"eta": t, "lo": t, "hi": t, "source": "not stopping here",
                     "backed": False, "vehicle": None, "skipped": True})

    rows.sort(key=lambda r: r["eta"])
    for r in rows:
        r.setdefault("skipped", False)
        r["catchable"] = r["lo"] >= now + walk
        r["leave_in"] = r["lo"] - walk - now
    return rows


def slots_with_trips(slots, skipped):
    """Scheduled slots MBTA has not declared as skipping."""
    return [(t, tr) for t, tr in slots if tr not in skipped]


def skipped_slot_times(slots, skipped, now):
    return [t for t, tr in slots if tr in skipped and t > now]


def etas(snap: dict, model: Model, walk: int = DEFAULT_WALK,
         qs=(0.1, 0.5, 0.9), horizon: int = 45 * 60,
         berths: dict[str, float] | None = None) -> list[dict]:
    """Thin wrapper: gather the I/O, then call the pure function."""
    now = snap["t"]
    day = dt.datetime.fromtimestamp(now, TZ).date()
    return compute_rows(now, snap["preds"], snap["vehicles"], model, walk, qs,
                        horizon, berths or {}, schedule_rows(day), skipped_trips())


def render(rows: list[dict], now: float, walk: int) -> str:
    out = [f"Magoun Square -> downtown   {dt.datetime.fromtimestamp(now, TZ):%-I:%M:%S %p}"
           f"   (walk {walk // 60} min)", ""]
    shown = 0
    for r in rows:
        if shown >= 6:
            break
        shown += 1
        eta = dt.datetime.fromtimestamp(r["eta"], TZ)
        mins = (r["eta"] - now) / 60
        # Both sides. No tier is symmetric about its own eta, and one half-width
        # printed for both invented an early side that the fit does not have.
        early, late = r["eta"] - r["lo"], r["hi"] - r["eta"]
        flag = "" if r["backed"] else "  [unconfirmed: no train upstream]"
        if r["catchable"]:
            lv = r["leave_in"]
            act = "LEAVE NOW" if lv <= 0 else f"leave in {lv/60:4.1f} min"
        else:
            act = "can't make it"
        out.append(f"  {eta:%-I:%M} ({mins:4.1f} min)  -{early:3.0f}/+{late:3.0f}s  "
                   f"{act:16s}  via {r['source']}{flag}")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    walk = int(sys.argv[1]) * 60 if len(sys.argv) > 1 else DEFAULT_WALK
    m = Model()
    s = snapshot()
    b = BerthTracker().update(s)
    print(render(etas(s, m, walk, berths=b), s["t"], walk))
