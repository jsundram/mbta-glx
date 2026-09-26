"""The notifier: holds today's plan, fires the triggers, listens for taps.

Notification budget (launch-plan.md, decision 1): 4 normal, 6 worst case.
  brief            -- the options, with buttons to pick one
  leave now        -- for the committed train, max priority
  adjust           -- once the train is moving and its ETA is sharp (+/-33 s or better)
  recovery         -- only if the committed train stops being predicted

Commands arrive from action buttons on the phone via the ntfy command topic:
  pick N | left | bump | cancel | brief | status
"""
import datetime as dt
import json
import os
import sys
import threading
import time

import brief
import notify
import service

STATE = service.ROOT / "data" / "plan.json"
TICK = 20.0
WALK = service.DEFAULT_WALK
MATCH = 300          # normal re-match window for a committed train
WIDE = 900           # fallback window: MBTA predictions flap by several minutes
# Observed flap durations: 90 s (16:23->16:31->16:23) and 90 s (17:19->17:11).
# A debounce shorter than the flap turns feed noise into a false no-show, so this
# is deliberately generous -- a true no-show is still caught minutes before the
# rider would need to leave.
MISS_TICKS = 12      # ~240 s of consecutive misses before declaring a no-show
DRIFT_ALERT = 240    # first "running late" alert once the train slips this far
REVISE_BY = 90       # announce a new ETA once it moves this far from the last one
REVISE_GAP = 90      # ...but no more often than this, so revisions cannot spam
ADJUST_SOURCES = ("departed Medford/Tufts", "departed Ball Sq")


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save(p: dict) -> None:
    STATE.write_text(json.dumps(p, indent=1))


def match_target(rows: list[dict], committed: dict, headway: float) -> dict | None:
    """Find the committed train among current predictions, or None.

    Pure so it can be tested without the network. Two rules earn their place:
    a vehicle id is proof of identity, and positional drift is capped below half a
    headway so a flapping prediction can never re-point the commitment at the
    NEXT train (which silently slides the plan and suppresses leave-now).
    """
    tgt = committed["target_eta"]
    vid = committed.get("vehicle")
    if vid:
        same = [r for r in rows if r.get("vehicle") == vid]
        if same:
            return min(same, key=lambda r: abs(r["eta"] - tgt))
    near = [r for r in rows if abs(r["eta"] - tgt) <= MATCH]
    if not near:
        limit = min(WIDE, headway * 0.45)
        near = [r for r in rows if abs(r["eta"] - tgt) <= limit]
    return min(near, key=lambda r: abs(r["eta"] - tgt)) if near else None


def count_arrivals(snapshots, stop: str, direction: int = 0) -> list[float]:
    """Arrival times at `stop`, counted as transitions into STOPPED_AT.

    Keying on (vehicle, stop) instead undercounts badly -- trains cycle through
    Magoun many times a day and only the first visit would register.
    """
    prev: dict[str, tuple] = {}
    out: list[float] = []
    for snap in snapshots:
        for v in snap["vehicles"]:
            cur = (v.get("stop"), v.get("status"))
            if (v.get("dir") == direction and v.get("stop") == stop
                    and v.get("status") == "STOPPED_AT" and prev.get(v["id"]) != cur):
                out.append(snap["t"])
            prev[v["id"]] = cur
    return out


def log(msg: str) -> None:
    """Timestamped line to watch.log -- the only window into what the phone sent."""
    print(f"{dt.datetime.now(service.TZ):%H:%M:%S} {msg}", flush=True)


def fmt(t: float) -> str:
    return dt.datetime.fromtimestamp(t, service.TZ).strftime("%-I:%M")


class Watcher:
    def __init__(self):
        self.model = service.Model()
        self.berths = service.BerthTracker()
        self.lock = threading.Lock()

    # ---- plan lifecycle ----
    def new_plan(self, dest: str, hhmm: str, conf: float = 0.90) -> dict:
        dest = brief.resolve(dest, self.model)
        now = dt.datetime.now(service.TZ)
        h, m = (int(x) for x in hhmm.split(":"))
        dl = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if dl < now:
            dl += dt.timedelta(days=1)
        p = {"dest": dest, "deadline": dl.timestamp(), "conf": conf, "walk": WALK,
             "committed": None, "left_at": None, "fired": {}, "created": time.time()}
        save(p)
        self.send_brief(p)
        return p

    def _snapshot(self):
        snap = service.snapshot()
        return snap, self.berths.update(snap)

    def send_brief(self, p: dict) -> None:
        snap, b = self._snapshot()
        opts = brief.options(p["dest"], p["deadline"], p["walk"], self.model, snap, b)
        name = self.model.m["rides"][p["dest"]]["name"]
        title, body = brief.render(opts, brief.health(self.model), name,
                                   p["deadline"], p["conf"])
        pickable = [o for o in opts if o["p_catch"] >= 0.5][:3]
        actions = [notify.reply_action(f"{fmt(o['eta'])} ({o['p_ontime']:.0%})",
                                       f"pick {i}", clear=True)
                   for i, o in enumerate(pickable)]
        notify.send(title, body, priority=3, tags=["tram"], actions=actions)
        p["options"] = [{"eta": o["eta"], "p": o["p_ontime"],
                         "vehicle": o.get("vehicle")} for o in pickable]
        save(p)

    def commit(self, p: dict, idx: int) -> None:
        opts = p.get("options") or []
        if not 0 <= idx < len(opts):
            return
        p["committed"] = {"target_eta": opts[idx]["eta"],
                          "original_eta": opts[idx]["eta"],
                          "vehicle": opts[idx].get("vehicle"),
                          "at": time.time(), "misses": 0}
        p["fired"] = {}
        p["left_at"] = None
        save(p)
        log(f"committed to {fmt(opts[idx]['eta'])} (p={opts[idx]['p']:.0%})")
        notify.send("Locked in", f"Watching the {fmt(opts[idx]['eta'])}. "
                    f"I'll tell you when to leave.", priority=2, tags=["white_check_mark"])

    def _detail(self, p: dict, snap: dict, berths: dict, row: dict) -> dict | None:
        """Live catch / on-time / 95%-there numbers for the committed train."""
        try:
            opts = brief.options(p["dest"], p["deadline"], p["walk"],
                                 self.model, snap, berths)
        except Exception:  # noqa: BLE001 - never let the leave-now push fail on this
            return None
        near = [o for o in opts if abs(o["eta"] - row["eta"]) <= MATCH]
        return min(near, key=lambda o: abs(o["eta"] - row["eta"])) if near else None

    # ---- the tick ----
    def tick(self) -> None:
        with self.lock:
            p = load()
            if not p or not p.get("committed"):
                return
            if time.time() > p["deadline"] + 1800:
                STATE.unlink(missing_ok=True)
                return
            snap, b = self._snapshot()
            rows = service.etas(snap, self.model, p["walk"], berths=b)
            com = p["committed"]
            tgt = com["target_eta"]
            # A skipped/cancelled train is stated, not inferred. Act at once rather
            # than waiting out the 240 s debounce built for a noisy feed.
            hit = [r for r in rows if r.get("skipped")
                   and abs(r["eta"] - tgt) <= MATCH]
            if hit and not p["fired"].get("recover"):
                log(f"MBTA says the {fmt(tgt)} is not stopping at Magoun")
                self._recover(p, rows, snap)
                return
            row = match_target(rows, com, self.model.headway)
            if row is not None and abs(row["eta"] - tgt) > MATCH:
                log(f"target drifted {(row['eta']-tgt)/60:+.1f} min "
                    f"to {fmt(row['eta'])} (still tracking)")
            if row is None:
                miss = p["committed"].get("misses", 0) + 1
                p["committed"]["misses"] = miss
                log(f"no match for {fmt(tgt)} ({miss}/{MISS_TICKS})")
                save(p)
                if miss >= MISS_TICKS:
                    # Silence is not a statement. The feed going quiet for 240 s is
                    # a flap as often as a no-show, so the commitment is kept and
                    # re-pointed rather than dropped -- _recover is for the cases
                    # where something was actually said (a skip) or there is
                    # genuinely nothing left to point at.
                    self._uncertain(p, rows, snap)
                return
            com["misses"] = 0
            com["target_eta"] = row["eta"]
            if row.get("vehicle"):
                com["vehicle"] = row["vehicle"]
            now = snap["t"]
            # Keep the rider current: any material ETA move gets announced, both
            # ways, for as long as it keeps moving.
            slip = row["eta"] - com.get("original_eta", row["eta"])
            if com.get("announced_eta") is not None:
                self._announce(p, row, now, "Updated arrival time")
            elif slip >= DRIFT_ALERT:
                self._announce(p, row, now, "Your train is running late")

            leave_by = row["lo"] - p["walk"]
            if not p["fired"].get("leave") and now >= leave_by - TICK / 2:
                o = self._detail(p, snap, b, row)
                extra = (f"\ncatch {o['p_catch']:.0%} · on time {o['p_ontime']:.0%}"
                         f" · 95% there by {fmt(o['dest_p95'])}") if o else ""
                notify.send(
                    "Leave now", f"{fmt(row['eta'])} train · "
                    f"{(row['eta']-now)/60:.0f} min out · via {row['source']}"
                    f" (+/-{(row['hi']-row['lo'])/2:.0f}s){extra}",
                    priority=5, tags=["runner"],
                    actions=[notify.reply_action("On my way", "left"),
                             notify.reply_action("Next one", "bump"),
                             notify.reply_action("Cancel", "cancel")])
                p["fired"]["leave"] = True
                log(f"fired LEAVE NOW for {fmt(row['eta'])} via {row['source']}")
            # Adjust: fires once the train is moving and the ETA is sharp.
            if (p.get("left_at") and not p["fired"].get("adjust")
                    and row["source"] in ADJUST_SOURCES):
                arrive = p["left_at"] + p["walk"]
                slack = row["eta"] - arrive
                verb = ("you're fine" if slack > 45 else
                        "pick it up" if slack > -30 else "you'll miss it")
                notify.send(
                    f"{(row['eta']-now)/60:.1f} min to your train",
                    f"{verb} · {slack:+.0f}s of slack · +/-"
                    f"{(row['hi']-row['lo'])/2:.0f}s",
                    priority=4, tags=["steam_locomotive"])
                p["fired"]["adjust"] = True
                log(f"fired ADJUST slack={slack:+.0f}s via {row['source']}")
            save(p)

    def _announce(self, p: dict, row: dict, now: float, why: str) -> None:
        """Tell the rider the ETA moved. Revisions continue for as long as it keeps
        moving -- a silently changing plan was the original sin here."""
        com = p["committed"]
        last = com.get("announced_eta", com.get("original_eta"))
        since = now - com.get("announced_at", 0)
        if abs(row["eta"] - last) < REVISE_BY or since < REVISE_GAP:
            return
        delta = row["eta"] - com.get("original_eta", row["eta"])
        notify.send(
            why,
            f"Now expected {fmt(row['eta'])} ({delta/60:+.0f} min vs your pick) · "
            f"+/-{(row['hi']-row['lo'])/2:.0f}s · leave {fmt(row['lo'] - p['walk'])}",
            priority=3, tags=["hourglass"],
            actions=[notify.reply_action("Show options", "brief")])
        com["announced_eta"] = row["eta"]
        com["announced_at"] = now
        log(f"announced revision -> {fmt(row['eta'])} ({delta/60:+.1f} min)")

    def _uncertain(self, p: dict, rows: list, snap: dict) -> None:
        """Nothing matched for MISS_TICKS. Adopt the best candidate and SAY SO.

        The commitment is not dropped: a flap of about one headway looks exactly
        like a no-show, and dropping it silently is worse than following the wrong
        train loudly. Revisions keep flowing, so a train that comes back gets
        announced right after.

        The wording asks rather than asserts -- "may be running late" -- because
        only a vehicle id could tell these apart and a vanished train has none.
        """
        com = p["committed"]
        # A train MBTA has said is not stopping here is not a candidate, however
        # well its time fits: adopting one would point the plan at a train that
        # will pass through the platform.
        cand = [r for r in rows if r["eta"] > snap["t"] + p["walk"] * 0.5
                and not r.get("skipped")]
        if not cand:
            # Nothing to follow. That is the one case where dropping the
            # commitment is honest, and _recover is what says so with options.
            self._recover(p, rows, snap)
            return
        row = min(cand, key=lambda r: abs(r["eta"] - com["target_eta"]))
        com["target_eta"] = row["eta"]
        com["vehicle"] = row.get("vehicle") or com.get("vehicle")
        com["misses"] = 0
        log(f"adopted {fmt(row['eta'])} after {MISS_TICKS} misses")
        self._announce(p, row, snap["t"], "Your train may be running late")
        save(p)

    def _recover(self, p: dict, rows: list, snap: dict) -> None:
        if p["fired"].get("recover"):
            return
        opts = brief.options(p["dest"], p["deadline"], p["walk"],
                             self.model, snap, self.berths.seen)
        nxt = next((o for o in opts if o["catchable"]), None)
        if nxt:
            # It may be the same train running late -- the feed cannot distinguish
            # that from a no-show when the gap is about one headway. Ask.
            gap = nxt["eta"] - p["committed"]["target_eta"] if p.get("committed") else 0
            same_ish = 0 < gap < self.model.headway * 1.5
            head = ("Your train may be running late" if same_ish
                    else "That train is not stopping at Magoun")
            notify.send(
                head,
                f"Best now is {fmt(nxt['eta'])} · {nxt['p_ontime']:.0%} for "
                f"{fmt(p['deadline'])} · leave {fmt(nxt['leave_by'])}",
                priority=5, tags=["warning"],
                actions=[notify.reply_action("Track it", "pick 0"),
                         notify.reply_action("Show options", "brief")])
            p["options"] = [{"eta": o["eta"], "p": o["p_ontime"]} for o in opts[:3]]
        else:
            notify.send("That train vanished", "No good option left for your deadline.",
                        priority=5, tags=["warning"])
        log("fired RECOVERY: committed train no longer predicted")
        p["fired"]["recover"] = True
        p["committed"] = None
        save(p)

    # ---- commands from the phone ----
    def on_command(self, text: str, _raw: dict) -> None:
        log(f"command: {text!r}")
        cmd, *rest = text.split()
        with self.lock:
            p = load()
        if cmd == "pick" and rest and p:
            self.commit(p, int(rest[0]))
        elif cmd == "left" and p:
            p["left_at"] = time.time()
            save(p)
        elif cmd == "bump" and p and p.get("options"):
            cur = p["committed"]["target_eta"] if p.get("committed") else 0
            later = [i for i, o in enumerate(p["options"]) if o["eta"] > cur + 60]
            if later:
                self.commit(p, later[0])
        elif cmd == "cancel":
            STATE.unlink(missing_ok=True)
            notify.send("Cancelled", "Not watching anything.", priority=2)
        elif cmd == "brief" and p:
            self.send_brief(p)
        elif cmd == "status":
            notify.send("Status", json.dumps(p, indent=1)[:900] if p else "no plan",
                        priority=2)
        else:
            log(f"  (no handler for {cmd!r}"
                f"{'; no plan active' if not p else ''})")


def main() -> None:
    if not notify.TOPIC or not notify.CMD:
        print("set MAGOUN_NTFY_TOPIC and MAGOUN_NTFY_CMD", file=sys.stderr)
        raise SystemExit(2)
    w = Watcher()
    if len(sys.argv) > 2 and sys.argv[1] == "plan":
        w.new_plan(sys.argv[2], sys.argv[3],
                   float(sys.argv[4]) if len(sys.argv) > 4 else 0.90)
        print("plan created and brief sent")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        w.tick()
        return
    threading.Thread(target=notify.watch_commands, args=(w.on_command,),
                     daemon=True).start()
    print(f"watching · topic {notify.TOPIC} · cmd {notify.CMD}", flush=True)
    while True:
        try:
            w.tick()
        except Exception as e:  # noqa: BLE001 - never let one tick kill the notifier
            print(f"tick error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(TICK)


if __name__ == "__main__":
    main()
