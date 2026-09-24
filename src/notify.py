"""ntfy transport: push to the phone, and receive taps back.

The reply path matters as much as the push. An action button on a notification can
POST to a *second* ntfy topic which this process subscribes to, so the phone can
talk back without this machine needing an inbound port, a tunnel, or a fixed IP.
That keeps the notifier host-portable (see launch-plan.md, Phase 5).

Verified against ntfy.sh: live delivery carries title, priority 5 and `http` action
buttons intact. Note that anonymous topics do NOT replay from cache -- `poll=1` and
`since=all` come back empty -- so a tap made while this process is down is simply
lost. Subscriptions therefore use `since=now` and the rider just taps again.

Topics are unguessable secrets -- there is no account -- so keep them out of git.

    export MAGOUN_NTFY_TOPIC=magoun-<random>       # service -> phone: SUBSCRIBE here
    export MAGOUN_NTFY_CMD=magoun-cmd-<random>     # phone -> service: do NOT subscribe

Subscribing to the command topic on the phone echoes your own button taps back as
notifications, which looks like the notifier spamming you.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator

SERVER = os.environ.get("MAGOUN_NTFY_SERVER", "https://ntfy.sh")
TOPIC = os.environ.get("MAGOUN_NTFY_TOPIC", "")
CMD = os.environ.get("MAGOUN_NTFY_CMD", "")


def reply_action(label: str, cmd: str, clear: bool = True) -> dict:
    """An action button that posts `cmd` back to the command topic when tapped.

    The POST carries min priority so that a phone which is (wrongly) subscribed to
    the command topic does not buzz with an echo of its own button tap.
    """
    return {"action": "http", "label": label, "url": f"{SERVER}/{CMD}",
            "method": "POST", "body": cmd, "clear": clear,
            "headers": {"Priority": "min", "Title": "cmd"}}


def send(title: str, message: str, *, priority: int = 3, tags: list[str] | None = None,
         actions: list[dict] | None = None, topic: str = "", tries: int = 3) -> bool:
    """Push a notification. ntfy allows at most 3 action buttons."""
    body = {"topic": topic or TOPIC, "title": title, "message": message,
            "priority": priority}
    if tags:
        body["tags"] = tags
    if actions:
        body["actions"] = actions[:3]
    data = json.dumps(body).encode()
    req = urllib.request.Request(SERVER, data=data,
                                 headers={"Content-Type": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status < 300
        except Exception:
            if i == tries - 1:
                return False
            time.sleep(1.5 * 2 ** i)
    return False


def listen(topic: str = "", since: str = "") -> Iterator[dict]:
    """Yield messages posted to a topic. Reconnects for as long as it is iterated.

    Pass no `since`: the bare stream delivers new messages, which is what we want.
    ntfy rejects `since=now` with HTTP 400 (only durations, timestamps, message ids
    and "all" are valid), and there is no catch-up to be had anyway because
    ntfy.sh does not replay cached messages for anonymous topics.
    """
    topic = topic or CMD
    fails = 0
    while True:
        try:
            url = f"{SERVER}/{topic}/json"
            if since:
                url += f"?since={since}"
            with urllib.request.urlopen(url, timeout=None) as r:
                fails = 0
                for line in r:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("event") == "message":
                        yield m
        except Exception as e:  # noqa: BLE001
            # Never fail silently here: a bad URL retried forever looks identical
            # to "nobody tapped anything", which cost real debugging time once.
            fails += 1
            if fails <= 3 or fails % 20 == 0:
                print(f"ntfy listen({topic}) failed x{fails}: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
            time.sleep(min(5 * fails, 60))


def watch_commands(handler: Callable[[str, dict], None], topic: str = "") -> None:
    """Blocking loop: call `handler(command_text, raw)` for each tap from the phone."""
    for m in listen(topic):
        text = (m.get("message") or "").strip()
        if text:
            handler(text, m)


if __name__ == "__main__":
    import sys
    if not TOPIC:
        print("set MAGOUN_NTFY_TOPIC (and MAGOUN_NTFY_CMD) first", file=sys.stderr)
        raise SystemExit(2)
    ok = send(
        "Magoun notifier connected",
        "If you can read this, the push channel works.\n"
        "Tap a button to check the reply path.",
        tags=["tram"], priority=4,
        actions=[reply_action("It works", "selftest ok"),
                 reply_action("Nope", "selftest fail")],
    )
    print("sent" if ok else "FAILED")
