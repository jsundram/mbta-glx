"""The notifier: holds today's plan, fires the triggers, listens for taps.

Notification budget (launch-plan.md, decision 1): 4 normal, 6 worst case.
  brief            -- the options, with buttons to pick one
  leave now        -- for the committed train, max priority
  adjust           -- once the train is moving and its ETA is sharp (+/-33 s or better)
  recovery         -- only if the committed train stops being predicted

Commands arrive on the ntfy command topic -- from action buttons on the phone, and
from the board's bell, which is how an alert armed on a page that then closes keeps
being refined:
  arm <eta> [vehicle] | pick N | left | bump | disarm | cancel | brief | status
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
# Consecutive throttled ticks before the rider is told the notifier has gone
# blind. ~5 min: long enough to ride out a burst, short enough to still be
# actionable before a leave-now would have fired.
BLIND_TICKS = 15


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save(p: dict) -> None:
    """Write beside and rename, so a reader never sees half a plan.

    `tick` reads this without the lock to decide whether it has anything to do,
    and a torn read parses as nothing -- which `load` turns into "no plan" and the
    tick skips. Same reasoning as scores.jsonl: the cheap fix is atomicity, not
    more locking. os.replace is atomic within a filesystem.
    """
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=1))
    os.replace(tmp, STATE)


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
        # `lock` guards the plan file. `berth_lock` guards the tracker, which is
        # now touched from both threads because snapshots are taken outside `lock`.
        self.lock = threading.Lock()
        self.berth_lock = threading.Lock()

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

    def arm_train(self, eta: float, vehicle: str | None = None) -> dict:
        """Take over an alert the board armed, and keep refining it.

        The bell on the static board schedules its own leave-now with ntfy and,
        while the page is open, re-points it every minute. That re-pointing is the
        only reason to leave a tablet running -- and it stops the moment the page
        closes, leaving a push aimed at wherever the train was last seen. This is
        the handoff: the bell also names the train here, and the notifier carries
        on refining it with no page open.

        There is no destination and no deadline, because the bell does not know an
        errand -- only which train. So this plan gets leave-now, revisions and
        recovery, and no probability of arriving anywhere by any time.

        Re-arming the same train refreshes it rather than replacing it. A page left
        open sends this every minute, and a fresh plan each time would forget that
        leave-now had already fired, forget an adopted train, and reset the miss
        count -- so the tablet being open would break the notifier watching.

        And a plan the rider made deliberately outranks the bell. `plan park 09:00`
        carries a destination, a deadline and possibly a leave-now already sent;
        the bell carries a train. Because a destination plan never has `armed_by`,
        the refresh below could never match one, so the arm fell through and built
        a bare plan over the top of it -- silently, and once a minute for as long
        as a tablet was open, which made a destination plan impossible to keep.
        """
        p = load()
        if p.get("dest"):
            # Not `and p.get("committed")`: new_plan writes committed=None and
            # sends the brief, so that test left the plan unguarded for exactly as
            # long as the rider takes to pick -- and the tablet arms into that
            # window every 60 s, deleting the options the pick buttons refer to.
            log(f"  (arm {fmt(eta)} ignored: a plan for {p['dest']} is active)")
            return p
        com = (p or {}).get("committed") or {}
        # Against `original_eta`, NOT the live target. The board re-sends the ETA it
        # armed and never updates it, while the target follows the train -- so
        # comparing the two made a heartbeat look like a new arm as soon as the
        # train slipped past MATCH, clearing `fired` (leave-now fires twice),
        # forgetting an adopted train and dragging the target back. The live run on
        # 2026-09-26 slipped 658 s, so this is where trains actually go.
        same = com and (
            (vehicle and com.get("vehicle") == vehicle)
            or abs(com.get("original_eta", 0) - eta) <= MATCH)
        if same:
            # A repeat arm is a heartbeat, not new information. The board sends the
            # train it armed, unchanged, because ntfy does not replay for anonymous
            # topics and a handoff lost while the notifier was down has no other
            # way back. So the eta here is the ORIGINAL one and the notifier's own
            # tracking is the better number -- adopting it would drag a train that
            # has since slipped back to where it was first seen.
            com.setdefault("vehicle", None)
            if vehicle and not com["vehicle"]:
                com["vehicle"] = vehicle
            com["misses"] = 0
            p["deadline"] = max(p.get("deadline", 0), com["target_eta"] + 1800)
            save(p)
            return p
        p = {"dest": None, "deadline": eta + 1800, "conf": None, "walk": WALK,
             "committed": {"target_eta": eta, "original_eta": eta,
                           "vehicle": vehicle, "at": time.time(), "misses": 0},
             "left_at": None, "fired": {}, "created": time.time(),
             "options": [], "armed_by": "board"}
        save(p)
        log(f"armed from the board: {fmt(eta)}"
            + (f" ({vehicle})" if vehicle else " (no vehicle id)"))
        return p

    def _snapshot(self):
        """Fetch, then fold into the berth tracker. Deliberately not under `lock`:
        this is a network round trip and the plan does not need protecting for it."""
        snap = service.snapshot()
        with self.berth_lock:
            return snap, dict(self.berths.update(snap))

    def send_brief(self, p: dict) -> None:
        """Fetch, render and push. Runs OUTSIDE the plan lock -- it is nearly all
        network -- and takes it only to record the options it offered."""
        snap, b = self._snapshot()
        if not p.get("dest"):
            return self._send_train_list(p, snap, b)
        opts = brief.options(p["dest"], p["deadline"], p["walk"], self.model, snap, b)
        name = self.model.m["rides"][p["dest"]]["name"]
        title, body = brief.render(opts, brief.health(self.model), name,
                                   p["deadline"], p["conf"])
        pickable = [o for o in opts if o["p_catch"] >= 0.5][:3]
        actions = [notify.reply_action(f"{fmt(o['eta'])} ({o['p_ontime']:.0%})",
                                       f"pick {i}", clear=True)
                   for i, o in enumerate(pickable)]
        notify.send(title, body, priority=3, tags=["tram"], actions=actions)
        with self.lock:
            p = load()
            if not p:
                return
            p["options"] = [{"eta": o["eta"], "p": o["p_ontime"],
                             "vehicle": o.get("vehicle")} for o in pickable]
            save(p)

    def _send_train_list(self, p: dict, snap: dict, b: dict) -> None:
        """The brief for a plan armed from the board: trains and leave times.

        No destination means no P(on time) to rank by, so this shows what the board
        itself shows -- the next inbound trains and when to leave for each -- with
        the same pick buttons, so a bare arm can still be re-pointed by hand.
        """
        rows = service.etas(snap, self.model, p["walk"], berths=b)
        opts = self._options(p, rows, snap)[:3]
        if not opts:
            notify.send("Nothing predicted", "No inbound trains upstream right now.",
                        priority=2, tags=["tram"])
            return
        lines = [f"{fmt(o['eta'])} train · leave {fmt(o['leave_by'])}"
                 + ("" if o["catchable"] else " · too late") for o in opts]
        notify.send("Next inbound at Magoun", "\n".join(lines), priority=3,
                    tags=["tram"],
                    actions=[notify.reply_action(fmt(o["eta"]), f"pick {i}", clear=True)
                             for i, o in enumerate(opts)])
        with self.lock:
            p = load()
            if not p:
                return
            p["options"] = [{"eta": o["eta"], "p": None, "vehicle": o.get("vehicle")}
                            for o in opts]
            save(p)

    def commit(self, p: dict, idx: int):
        """Point the plan at option `idx`. Returns the push to send once the lock
        is released -- see on_command."""
        opts = p.get("options") or []
        if not 0 <= idx < len(opts):
            return None
        p["committed"] = {"target_eta": opts[idx]["eta"],
                          "original_eta": opts[idx]["eta"],
                          "vehicle": opts[idx].get("vehicle"),
                          "at": time.time(), "misses": 0}
        p["fired"] = {}
        p["left_at"] = None
        save(p)
        odds = f" (p={opts[idx]['p']:.0%})" if opts[idx].get("p") is not None else ""
        log(f"committed to {fmt(opts[idx]['eta'])}{odds}")
        when = fmt(opts[idx]["eta"])
        return lambda: notify.send(
            "Locked in", f"Watching the {when}. I'll tell you when to leave.",
            priority=2, tags=["white_check_mark"])

    def _detail(self, p: dict, snap: dict, berths: dict, row: dict) -> dict | None:
        """Live catch / on-time / 95%-there numbers for the committed train.

        None for a plan armed from the board: those numbers are about reaching a
        destination by a deadline, and it has neither. Checked rather than left to
        the except below, which is there to swallow a surprise, not a known case.
        """
        if not p.get("dest"):
            return None
        try:
            opts = brief.options(p["dest"], p["deadline"], p["walk"],
                                 self.model, snap, berths)
        except Exception:  # noqa: BLE001 - never let the leave-now push fail on this
            return None
        near = [o for o in opts if abs(o["eta"] - row["eta"]) <= MATCH]
        return min(near, key=lambda o: abs(o["eta"] - row["eta"])) if near else None

    # ---- the tick ----
    def tick(self) -> None:
        # Cheap unlocked read first: with no plan there is nothing to fetch for.
        if not load().get("committed"):
            return
        snap, b = self._snapshot()          # network, before taking the lock
        with self.lock:
            p = load()
            if not p or not p.get("committed"):
                return
            if time.time() > p["deadline"] + 1800:
                STATE.unlink(missing_ok=True)
                return
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
                # The notifier fires at a wall-clock instant; the host it runs on
                # does not guarantee one. A sleeping Mac runs the missed tick on
                # wake, and launchd will happily deliver it minutes late. By then
                # the walk may no longer fit before the train, and "Leave now" for
                # a train that cannot be reached is worse than the silence it
                # replaces -- it sends the rider out for nothing. The walk is the
                # test, so no new constant: if it still fits, go.
                if row["eta"] - now < p["walk"]:
                    log(f"LEAVE NOW for {fmt(row['eta'])} is "
                        f"{(now - leave_by):.0f}s late; the walk no longer fits")
                    p["fired"]["leave"] = True
                    save(p)
                    self._recover(p, rows, snap, head="You can't make that one")
                    return
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
        # Once leave-now has gone out, a leave time is the wrong advice: the rider
        # is walking, or already standing on the platform. Measured live on
        # 2026-09-26 -- told to leave at 1:31, the train never came, and the 1:41
        # revision said "leave 1:42" to someone who had been there four minutes.
        # What is true wherever they are is how far off the train now is.
        where = (f"{(row['eta'] - now) / 60:.0f} min away"
                 if p.get("fired", {}).get("leave")
                 else f"leave {fmt(row['lo'] - p['walk'])}")
        notify.send(
            why,
            f"Now expected {fmt(row['eta'])} ({delta/60:+.0f} min vs your pick) · "
            f"+/-{(row['hi']-row['lo'])/2:.0f}s · {where}",
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

    def _options(self, p: dict, rows: list, snap: dict) -> list[dict]:
        """Ranked alternatives to the committed train.

        A plan made by `plan` has a destination and a deadline, so the ranking is
        brief.options and every option carries P(on time). A plan armed from the
        board's bell has neither -- the bell knows one train, not an errand -- so
        the alternatives are simply the next trains still reachable on foot, and
        `p_ontime` is None rather than a number nothing computed.
        """
        if p.get("dest"):
            return brief.options(p["dest"], p["deadline"], p["walk"],
                                 self.model, snap, self.berths.seen)
        return [{"eta": r["eta"], "leave_by": r["lo"] - p["walk"], "p_ontime": None,
                 "vehicle": r.get("vehicle"),
                 "catchable": r["lo"] - p["walk"] >= snap["t"]}
                for r in rows if not r.get("skipped")]

    def _recover(self, p: dict, rows: list, snap: dict, head: str = "") -> None:
        """Offer the best remaining train and let go of the committed one.

        `head` overrides the headline for a caller that already knows why -- the
        default wording is about a train that stopped being predicted, which is
        not the same story as a leave-now the host was asleep for.
        """
        if p["fired"].get("recover"):
            return
        opts = self._options(p, rows, snap)
        nxt = next((o for o in opts if o["catchable"]), None)
        if nxt:
            # It may be the same train running late -- the feed cannot distinguish
            # that from a no-show when the gap is about one headway. Ask.
            gap = nxt["eta"] - p["committed"]["target_eta"] if p.get("committed") else 0
            same_ish = 0 < gap < self.model.headway * 1.5
            head = head or ("Your train may be running late" if same_ish
                            else "That train is not stopping at Magoun")
            odds = (f" · {nxt['p_ontime']:.0%} for {fmt(p['deadline'])}"
                    if nxt.get("p_ontime") is not None else "")
            notify.send(
                head,
                f"Best now is {fmt(nxt['eta'])}{odds} · leave {fmt(nxt['leave_by'])}",
                priority=5, tags=["warning"],
                actions=[notify.reply_action("Track it", "pick 0"),
                         notify.reply_action("Show options", "brief")])
            # The message names `nxt` and the button posts `pick 0`, so the
            # stored options must START at nxt. They were stored in ETA order,
            # and the first by ETA is often a train too close to walk to -- so
            # "Track it" committed to a train the rider could not reach and the
            # message had not mentioned.
            ordered = [nxt] + [o for o in opts if o is not nxt]
            p["options"] = [{"eta": o["eta"], "p": o["p_ontime"],
                             "vehicle": o.get("vehicle")} for o in ordered[:3]]
        else:
            # A board arm has no deadline -- `deadline` there is a synthetic
            # eta + 1800 -- so naming one describes a commitment never made.
            why = ("for your deadline" if p.get("dest") else "you can still walk to")
            notify.send(head or "That train vanished",
                        f"No good option left {why}.",
                        priority=5, tags=["warning"])
        log("fired RECOVERY: committed train no longer predicted")
        p["fired"]["recover"] = True
        p["committed"] = None
        save(p)

    # ---- commands from the phone ----
    def on_command(self, text: str, _raw: dict) -> None:
        """Handle one tap, or one handoff from the board.

        This runs on the ntfy subscription thread while `tick` runs on the main
        one, and both read-modify-write plan.json. Taking the lock only for the
        load left a window where a tick that started first would save over the tap
        -- "On my way" lost that way costs the mid-walk adjust, which is one of the
        three things this file exists to send.

        Nothing is allowed to escape, either: an exception here would propagate out
        of notify.watch_commands and kill the subscription thread, and the notifier
        would go on ticking with every button on the phone silently dead.

        The lock covers the plan's read-modify-write and nothing else. `_dispatch`
        touches no network; anything to push comes back as a callable and is sent
        after the lock is released, because `notify.send` retries three times with
        20 s timeouts -- about 64 s with an unreachable ntfy -- and holding the lock
        through that would delay the leave-now this whole file exists to send.
        """
        log(f"command: {text!r}")
        try:
            with self.lock:
                after = self._dispatch(text)
        except Exception as e:  # noqa: BLE001
            log(f"  (command {text!r} failed: {type(e).__name__}: {e})")
            return
        try:
            if after:
                after()
        except Exception as e:  # noqa: BLE001
            log(f"  (sending for {text!r} failed: {type(e).__name__}: {e})")

    def _dispatch(self, text: str):
        """Apply `text` to the plan. Returns what to send afterwards, or None.

        Runs under the lock, so nothing here may touch the network.
        """
        cmd, *rest = text.split() or [""]
        p = load()
        if cmd == "arm" and rest:
            # From the board's bell: `arm <eta epoch> [vehicle]`. An epoch, not a
            # clock time, so there is no zone to get wrong on either side.
            try:
                eta = float(rest[0])
            except ValueError:
                log(f"  (arm: {rest[0]!r} is not an epoch)")
                return None
            if eta < time.time():
                log(f"  (arm: {fmt(eta)} is in the past)")
                return None
            vid = rest[1] if len(rest) > 1 and rest[1] != "-" else None
            self.arm_train(eta, vid)
        elif cmd == "pick" and rest and p:
            return self.commit(p, int(rest[0]))
        elif cmd == "left" and p:
            p["left_at"] = time.time()
            save(p)
        elif cmd == "bump" and p and p.get("options"):
            cur = p["committed"]["target_eta"] if p.get("committed") else 0
            later = [i for i, o in enumerate(p["options"]) if o["eta"] > cur + 60]
            if later:
                return self.commit(p, later[0])
        elif cmd == "disarm":
            # From the board, automatically, when its armed train has come and
            # gone. Only the board's own alert may be dropped this way: `cancel`
            # deletes any plan at all, so sending that from paint() destroyed a
            # `plan park 09:00` through the other door from the guard in
            # arm_train. No push either -- nobody asked for this one.
            if p.get("armed_by") == "board":
                STATE.unlink(missing_ok=True)
                log("  (the board disarmed its own alert)")
            else:
                log("  (disarm ignored: the running plan is not the board's)")
        elif cmd == "cancel":
            # A person looking at a notification and tapping Cancel means all of it.
            STATE.unlink(missing_ok=True)
            return lambda: notify.send("Cancelled", "Not watching anything.",
                                       priority=2)
        elif cmd == "brief" and p:
            return lambda: self.send_brief(p)
        elif cmd == "status":
            body = json.dumps(p, indent=1)[:900] if p else "no plan"
            return lambda: notify.send("Status", body, priority=2)
        else:
            log(f"  (no handler for {cmd!r}"
                f"{'; no plan active' if not p else ''})")
        return None


def run_tick(w: "Watcher", throttled: int) -> int:
    """One tick and its error handling. Returns the consecutive-throttle count.

    Separate from main's loop only so it can be tested: the behaviour that matters
    here is what happens on the bad ticks, and a `while True` cannot be asserted on.
    """
    try:
        w.tick()
        return 0
    except Exception as e:  # noqa: BLE001 - never let one tick kill the notifier
        # A throttled notifier is not a broken one, but it is not a working one
        # either, and "tick error" buries it among real faults. v3 allows 20
        # requests/minute unauthenticated and a snapshot is two of them, so one
        # extra poller is enough to do this -- measured 2026-09-26.
        if "429" not in str(e):
            print(f"tick error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return throttled
        throttled += 1
        print(f"THROTTLED by api-v3 ({throttled} ticks): set MBTA_API_KEY, "
              "or stop another poller", file=sys.stderr, flush=True)
        # Silence is what this costs, and silence is indistinguishable from "no
        # train yet". Say it once, and only when a plan is riding on it -- ntfy's
        # own reconnect loop earned this lesson already.
        if throttled == BLIND_TICKS and (load() or {}).get("committed"):
            notify.send("Notifier is blind",
                        f"MBTA has been rate-limiting me for "
                        f"{BLIND_TICKS * TICK / 60:.0f} min. I cannot see your "
                        "train; check the board yourself.",
                        priority=4, tags=["warning"])
        return throttled


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
    throttled = 0
    while True:
        throttled = run_tick(w, throttled)
        time.sleep(TICK)


if __name__ == "__main__":
    main()
