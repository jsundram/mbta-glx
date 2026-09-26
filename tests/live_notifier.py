"""Drive the real notifier against the real feed, and say what was actually seen.

WHY THIS EXISTS. M5's done-when is "an alert armed at 08:00 and then abandoned
still tracks a train that slips". `tests/test_watch.py` proves the logic by
stubbing three seams; nothing in the suite proves the notifier works against
MBTA's feed, ntfy's real delivery, and a train that exists. Three of the faults
found in M5 -- `now` read before assignment, `_uncertain` never called, `arm`
overwriting a destination plan -- were invisible to reading and only showed up
once something ran. This is the cheapest way to run it.

WHEN TO RUN IT. After changing watch.py, notify.py, brief.py, or the board's
handoff; before claiming M5 works; and whenever a measured constant here is in
doubt. Not in CI: it needs the live feed, a real ntfy round trip, and ~25 min of
wall clock. Trains have to be running -- roughly 05:00-01:00, and the horizon is
only interesting when service is frequent.

WHAT IT DOES NOT PROVE. A revision needs a train to slip past DRIFT_ALERT and a
recovery needs one to vanish or be skipped (~9.6% and ~10/day system-wide), so
neither can be summoned. The verdict at the end says which triggers were
exercised and which never got the chance -- "not observed" is coverage missing,
not a failure, and reporting it as green would be the same lie as a skipped test
that looks passing (invariant 9).

    R="uv run --with numpy --with gtfs-realtime-bindings python"
    $R tests/live_notifier.py                 # 25 min
    $R tests/live_notifier.py --minutes 40
    $R tests/live_notifier.py --real-topics   # buzzes the phone

gtfs-realtime-bindings is not optional here either: without it the skip lookup
raises and is swallowed, so a run reports "no skips" whether or not there were
any -- and the skip path is one of the two this check exists to exercise.

SAFETY, because this talks to the outside world and to a host that may be
running the real notifier:

  * Throwaway ntfy topics by default, minted per run. The rider's phone stays
    quiet and `ops/ntfy.env` is never read. `--real-topics` opts in explicitly;
    it is the only way to check that the pushes actually land on the phone.
  * A scratch plan and berth file. It refuses to touch `data/plan.json`, so the
    launchd notifier keeps whatever it was watching.
  * Read-only against everything else: it fetches the same public endpoints the
    board does and writes nothing under `data/`.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import random
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def topics(real: bool) -> tuple[str, str]:
    """Where the pushes go. Throwaway unless the rider asked for their own."""
    if not real:
        tag = f"{random.getrandbits(48):012x}"
        return f"magoun-live-{tag}", f"magoun-livecmd-{tag}"
    env = ROOT / "ops" / "ntfy.env"
    if not env.exists():
        raise SystemExit("--real-topics needs ops/ntfy.env")
    got = {}
    for line in env.read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() in ("MAGOUN_NTFY_TOPIC", "MAGOUN_NTFY_CMD"):
            got[k.strip()] = v.strip().strip("'\"")
    if len(got) != 2:
        raise SystemExit("ops/ntfy.env is missing MAGOUN_NTFY_TOPIC or _CMD")
    return got["MAGOUN_NTFY_TOPIC"], got["MAGOUN_NTFY_CMD"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=25,
                    help="how long to watch after arming (default 25)")
    ap.add_argument("--arm-in", type=float, default=10,
                    help="arm the first train at least this many minutes out. The "
                         "default is past MBTA's 8-13 min horizon on this platform, "
                         "so the arm starts on a schedule row with no vehicle id -- "
                         "which is the case the project exists for (default 10)")
    ap.add_argument("--real-topics", action="store_true",
                    help="use ops/ntfy.env, so the pushes reach the phone")
    ap.add_argument("--no-walk", action="store_true",
                    help="do not send `left` after leave-now; the mid-walk adjust "
                         "then cannot fire, because it has no reference point")
    a = ap.parse_args()

    out, cmd = topics(a.real_topics)
    os.environ["MAGOUN_NTFY_TOPIC"], os.environ["MAGOUN_NTFY_CMD"] = out, cmd

    import notify                       # noqa: E402 - after the env is set
    import service                      # noqa: E402
    import watch                        # noqa: E402

    scratch = pathlib.Path(tempfile.mkdtemp(prefix="magoun-live-"))
    watch.STATE = scratch / "plan.json"
    assert watch.STATE != service.ROOT / "data" / "plan.json"
    w = watch.Watcher()
    w.berths = service.BerthTracker(scratch / "berth.json")

    tz = service.TZ
    f = lambda t: dt.datetime.fromtimestamp(t, tz).strftime("%H:%M:%S")
    say = lambda m: print(f"{dt.datetime.now(tz):%H:%M:%S} | {m}", flush=True)
    seen: dict[str, str] = {}
    pushes: list[dict] = []

    def record(title, message, **kw):
        """Tee every push, and keep whether ntfy accepted it.

        Delivery is half the thing being tested, and a subscriber piped to a file
        is not the way to see it -- curl buffers, so a run can look silent while
        every push landed. notify.send already returns the HTTP outcome.
        """
        ok = real_send(title, message, **kw)
        pushes.append({"t": time.time(), "title": title, "message": message,
                       "delivered": bool(ok)})
        say(f"  PUSH{'' if ok else ' (NOT DELIVERED)'} [{title}] "
            f"{message.replace(chr(10), ' / ')}")
        return ok

    real_send = notify.send
    notify.send = record             # still delivers; this only tees a copy

    print(__doc__.split("\n\n")[0])
    print(f"\n  topics      {out} / {cmd}"
          f"{'   (REAL -- the phone will buzz)' if a.real_topics else '   (throwaway)'}")
    print(f"  state       {scratch}")
    print(f"  watching    {a.minutes:.0f} min, tick {watch.TICK:.0f}s, "
          f"walk {service.DEFAULT_WALK}s\n")

    threading.Thread(target=notify.watch_commands, args=(w.on_command,),
                     daemon=True).start()
    time.sleep(3)                    # let the subscription attach before arming

    def post(body: str) -> None:
        """Exactly what web/index.html posts: a command on the command topic."""
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{cmd}", data=body.encode(),
            headers={"Priority": "min", "Title": "cmd"}), timeout=20).read()

    snap, b = w._snapshot()
    rows = service.etas(snap, w.model, service.DEFAULT_WALK, berths=b)
    if not rows:
        say("nothing predicted at all -- is the line running?")
        return 1
    pick = next((r for r in rows
                 if r["eta"] - snap["t"] > a.arm_in * 60 and not r.get("skipped")), None)
    if not pick:
        say(f"no train more than {a.arm_in:.0f} min out; "
            f"next is {(rows[0]['eta'] - snap['t']) / 60:.1f} min. "
            "Try --arm-in, or wait for service to thin out.")
        return 1

    say(f"ARMING {f(pick['eta'])} · {pick['source']} · veh={pick.get('vehicle')} "
        f"· leave {f(pick['lo'] - service.DEFAULT_WALK)}")
    post(f"arm {int(pick['eta'])} {pick.get('vehicle') or '-'}")
    time.sleep(4)
    if not (watch.load().get("committed") or {}):
        say("FAIL: the arm never reached the notifier (ntfy round trip)")
        return 1
    seen["handoff"] = "the bell's `arm` reached watch.py over ntfy"
    seen[f"tier:{pick['source']}"] = "armed on this tier"
    say("--- page closed; the notifier is on its own from here ---")

    end, last, errors, throttled = time.time() + a.minutes * 60, None, 0, 0
    while time.time() < end:
        try:
            w.tick()
        except Exception as e:  # noqa: BLE001
            # 429 is not a bug in the notifier, it is too many pollers on one
            # unauthenticated key. One tick is two requests; the archiver is
            # another two every 15 s. Counted apart so the verdict is not a lie
            # in either direction.
            if "429" in str(e):
                throttled += 1
                say(f"THROTTLED (429) -- see the rate-limit note in CLAUDE.md")
            else:
                errors += 1
                say(f"TICK ERROR {type(e).__name__}: {e}")
        p = watch.load()
        com = (p or {}).get("committed")
        fired = tuple(sorted((p or {}).get("fired") or {}))
        if com:
            seen["tracking"] = "followed the train across ticks"
            cur = (round(com["target_eta"]), com.get("vehicle"),
                   com.get("misses"), fired)
            if cur != last:
                drift = com["target_eta"] - com["original_eta"]
                if abs(drift) >= 1:
                    seen["drift"] = f"the ETA moved {drift:+.0f}s from the armed time"
                if com.get("vehicle"):
                    seen["vehicle"] = f"latched vehicle {com['vehicle']}"
                if com.get("misses"):
                    seen["misses"] = "the feed dropped the train for at least one tick"
                say(f"tracking {f(com['target_eta'])} ({drift:+.0f}s) "
                    f"veh={com.get('vehicle')} misses={com.get('misses')} fired={fired}")
                last = cur
            # What tier is quoting it now? The walk from schedule to a sharp tier
            # is the horizon this project exists to extend, so it is worth naming.
            row = watch.match_target(
                service.etas(snap, w.model, p["walk"], berths=b), com, w.model.headway)
            if row:
                seen[f"tier:{row['source']}"] = "the train was quoted by this tier"
        if "leave" in fired and "left" not in seen and not a.no_walk:
            seen["left"] = "sent `left`, as the On my way button does"
            post("left")
        if not com and fired:
            say(f"commitment released; fired={fired}")
            break
        snap, b = w._snapshot()
        time.sleep(max(0.0, watch.TICK - 2))

    for k, why in (("leave", "leave-now"), ("adjust", "mid-walk adjust"),
                   ("recover", "recovery")):
        if k in ((watch.load() or {}).get("fired") or {}):
            seen[why] = "fired"
    for p_ in pushes:
        if p_["title"].startswith(("Your train", "Updated arrival")):
            seen["revision"] = "the moved ETA was announced"

    print("\n" + "=" * 70)
    undelivered = [p_ for p_ in pushes if not p_["delivered"]]
    print(f"{len(pushes)} push(es), {len(undelivered)} undelivered, "
          f"{errors} tick error(s), {throttled} throttled\n")
    for p_ in pushes:
        print(f"  {f(p_['t'])}  [{p_['title']}]"
              f"{'' if p_['delivered'] else ' (NOT DELIVERED)'} "
              f"{p_['message'].replace(chr(10), ' / ')}")
    if throttled:
        print(f"\n  {throttled} tick(s) lost to HTTP 429. Each tick is two v3"
              "\n  requests and the archiver is already polling; unauthenticated"
              "\n  is 20/min. Set MBTA_API_KEY, or stop the other pollers.")
    print("\nobserved live:")
    for k in sorted(seen):
        print(f"  ok       {k} -- {seen[k]}")
    # Naming what did NOT happen is the point. A live run that exercised only the
    # happy path and printed nothing else would read exactly like a full one.
    missed = [k for k in ("revision", "recovery", "mid-walk adjust", "leave-now")
              if k not in seen]
    for k in missed:
        print(f"  --       {k} -- no chance to fire in this window")
    ok = (errors == 0 and not undelivered
          and ("leave-now" in seen or "recovery" in seen))
    print("\n" + ("PASSED: the notifier tracked a real train and acted on it"
                  if ok else
                  "INCONCLUSIVE: no trigger fired" if not errors else
                  f"FAILED: {errors} tick error(s)"))
    print(f"(state left in {scratch})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
