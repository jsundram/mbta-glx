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
WALK = int(os.environ.get("MAGOUN_WALK_S", "390"))
MATCH = 300          # normal re-match window for a committed train
WIDE = 900           # fallback window: MBTA predictions flap by several minutes
MISS_TICKS = 4       # consecutive failed matches before declaring a no-show
ADJUST_SOURCES = ("departed Medford/Tufts", "departed Ball Sq")


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save(p: dict) -> None:
    STATE.write_text(json.dumps(p, indent=1))


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
        p["options"] = [{"eta": o["eta"], "p": o["p_ontime"]} for o in pickable]
        save(p)

    def commit(self, p: dict, idx: int) -> None:
        opts = p.get("options") or []
        if not 0 <= idx < len(opts):
            return
        p["committed"] = {"target_eta": opts[idx]["eta"], "at": time.time(),
                          "misses": 0}
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
            tgt = p["committed"]["target_eta"]
            near = [r for r in rows if abs(r["eta"] - tgt) <= MATCH]
            if not near:
                # MBTA predictions flap: one observed jump went 16:23 -> 16:31 ->
                # 16:23 within 90 s. Widen before giving up, then require several
                # consecutive misses, or a single noisy tick cries no-show.
                near = [r for r in rows if abs(r["eta"] - tgt) <= WIDE]
                if near:
                    j = min(near, key=lambda r: abs(r["eta"] - tgt))
                    log(f"target drifted {(j['eta']-tgt)/60:+.1f} min "
                        f"to {fmt(j['eta'])} (still tracking)")
            if not near:
                miss = p["committed"].get("misses", 0) + 1
                p["committed"]["misses"] = miss
                log(f"no match for {fmt(tgt)} ({miss}/{MISS_TICKS})")
                save(p)
                if miss >= MISS_TICKS:
                    self._recover(p, rows, snap)
                return
            row = min(near, key=lambda r: abs(r["eta"] - tgt))
            p["committed"]["misses"] = 0
            p["committed"]["target_eta"] = row["eta"]

            leave_by = row["lo"] - p["walk"]
            now = snap["t"]
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

    def _recover(self, p: dict, rows: list, snap: dict) -> None:
        if p["fired"].get("recover"):
            return
        opts = brief.options(p["dest"], p["deadline"], p["walk"],
                             self.model, snap, self.berths.seen)
        nxt = next((o for o in opts if o["catchable"]), None)
        if nxt:
            notify.send(
                "That train vanished",
                f"Next is {fmt(nxt['eta'])} · {nxt['p_ontime']:.0%} for "
                f"{fmt(p['deadline'])} · leave {fmt(nxt['leave_by'])}",
                priority=5, tags=["warning"],
                actions=[notify.reply_action("Take it", "pick 0")])
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
