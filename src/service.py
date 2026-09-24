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
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
TZ = dt.timezone(dt.timedelta(hours=-4))          # America/New_York, EDT
MAGOUN_IN, BALL_IN, MED_IN, MED_OUT = "70508", "70510", "70512", "70511"
KEY = os.environ.get("MBTA_API_KEY")
VETO_WINDOW = 480        # a scheduled train this close with nothing upstream is a no-show
DEFAULT_WALK = 360
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

    @property
    def berth_const(self) -> tuple[int, int]:
        b = self.m["berth"]
        return b["turn_plus_run"], b["sched_bias"]

    @property
    def headway(self) -> float:
        return self.m["headway_median_s"]


class BerthTracker:
    """Remembers when each train first appeared on the Medford/Tufts inbound platform.

    The vehicle feed refreshes a stopped train's timestamp, so it does not tell us
    when the train berthed -- that has to be observed. State is persisted so a
    server restart does not lose trains already sitting there.
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
            if not _live(a, now):
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


def _live(attrs: dict, now: float) -> bool:
    """False for stale positions: parked, out-of-service trains sit for hours."""
    u = _iso(attrs.get("updated_at"))
    return u is not None and (now - u) <= STALE_VEHICLE


def _iso(s: str | None) -> float | None:
    return dt.datetime.fromisoformat(s).timestamp() if s else None


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


def snapshot() -> dict:
    preds = _get("predictions", {
        "filter[stop]": ",".join([MAGOUN_IN, BALL_IN, MED_IN, MED_OUT]),
        "sort": "arrival_time"})
    veh = _get("vehicles", {"filter[route]": "Green-B,Green-C,Green-D,Green-E"})
    return {"t": time.time(), "preds": preds["data"], "vehicles": veh["data"]}


def upstream_state(snap: dict) -> dict:
    """Where each inbound GLX train is right now, from vehicle positions."""
    out = {"departed_ball": [], "departed_med": [], "at_terminus": 0, "ghosts": 0}
    for v in snap["vehicles"]:
        a, rel = v["attributes"], v["relationships"]
        stop = (rel["stop"]["data"] or {}).get("id")
        if not _live(a, snap["t"]):
            out["ghosts"] += 1
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


def etas(snap: dict, model: Model, walk: int = DEFAULT_WALK,
         qs=(0.1, 0.5, 0.9), horizon: int = 45 * 60,
         berths: dict[str, float] | None = None) -> list[dict]:
    now = snap["t"]
    ql, qm, qh = qs
    state = upstream_state(snap)
    berths = berths or {}
    rows: list[dict] = []

    # 1. MBTA's own inbound predictions at Magoun -- the operator's model, use it first.
    mbta = []
    for p in snap["preds"]:
        a, rel = p["attributes"], p["relationships"]
        if rel["stop"]["data"]["id"] != MAGOUN_IN or a["direction_id"] != 0:
            continue
        t = _iso(a["arrival_time"] or a["departure_time"])
        if t and t > now:
            mbta.append((t, (rel.get("vehicle", {}).get("data") or {}).get("id")))
    for t, vid in sorted(mbta):
        src, band = "mbta", 75.0
        if vid and vid in state["departed_ball"]:
            src, band = "departed Ball Sq", 7.0
        elif vid and vid in state["departed_med"]:
            src, band = "departed Medford/Tufts", 33.0
        rows.append({"eta": t, "lo": t - band, "hi": t + band,
                     "source": src, "backed": True, "vehicle": vid})

    # 2. Trains already berthed at Medford/Tufts. Their departure is bounded below
    #    by the physical turnaround, which the timetable cannot express -- this is
    #    what fixes the case where a train is running behind schedule.
    slots = schedule_today(dt.datetime.fromtimestamp(now, TZ).date())
    turn_run, bias = model.berth_const
    covered = {r.get("vehicle") for r in rows}
    for vid, berth in berths.items():
        if vid in covered:
            continue                       # MBTA already predicts this one, sharper
        nxt = [s for s in slots if s + bias > berth]
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

    # 3. Schedule beyond everything we can physically see, corrected for bias.
    last = max([r["eta"] for r in rows], default=now)
    for s in slots:
        mid = s + model.sched_offset(qm)
        if mid <= max(now, last + 120) or mid > now + horizon:
            continue
        backed = not (s - now < VETO_WINDOW and state["at_terminus"] == 0)
        if any(abs(r["eta"] - mid) < 240 for r in rows):
            continue                       # something visible already times this train
        rows.append({"eta": mid, "lo": s + model.sched_offset(ql),
                     "hi": s + model.sched_offset(qh),
                     "source": "schedule", "backed": backed, "vehicle": None})

    rows.sort(key=lambda r: r["eta"])
    for r in rows:
        r["catchable"] = r["lo"] >= now + walk
        r["leave_in"] = r["lo"] - walk - now
    return rows


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
        band = (r["hi"] - r["lo"]) / 2
        flag = "" if r["backed"] else "  [unconfirmed: no train upstream]"
        if r["catchable"]:
            lv = r["leave_in"]
            act = "LEAVE NOW" if lv <= 0 else f"leave in {lv/60:4.1f} min"
        else:
            act = "can't make it"
        out.append(f"  {eta:%-I:%M} ({mins:4.1f} min)  +/- {band:3.0f}s  "
                   f"{act:16s}  via {r['source']}{flag}")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    walk = int(sys.argv[1]) * 60 if len(sys.argv) > 1 else DEFAULT_WALK
    m = Model()
    s = snapshot()
    b = BerthTracker().update(s)
    print(render(etas(s, m, walk, berths=b), s["t"], walk))
