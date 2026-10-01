# TODO — Tailscale and the board's origin

Written 2026-10-01, after moving the board's public surface onto a port of its own
(commit `56dcd82`). Everything here is about how this Mac publishes several apps to
the tailnet; none of it blocks the board, which is live and working.

**The rule this is all converging on: one origin per app, never a shared path
space.** Paths on one hostname is how two apps end up answering for each other — an
unmapped path does not fail, it falls through to whichever app owns `/` and 404s
from over there, which is indistinguishable from a backend that is down. That cost
an hour on 2026-09-27 and would have cost it again on the next route.

## Current state

```
mini.tail8b0808.ts.net:443        / -> 127.0.0.1:8770   Deck
                                  /capture -> 127.0.0.1:8723/capture   DEAD, see below
                                  /skips   -> 127.0.0.1:8723/skips     DEAD, see below
mini.tail8b0808.ts.net:8443       / -> 127.0.0.1:8724   the board's public origin
```

`8724` serves `server.BROWSER_ROUTES` and nothing else — `/api`, `/status`, `/board`,
`/history` and `/` all 404 there, and they stay on `8723`, loopback only. That is the
whole surface a browser can reach, enforced in the process where `tests/test_server.py`
can walk it, rather than by which paths somebody remembered to mount.

`./ops/status.sh` checks all of it and reads both route lists out of `src/server.py`.

---

## 1. Remove the dead path mounts on 443

`/skips` and `/capture` on 443 still point at `8723`. Nothing uses them: the board
reads `constants.backend_url`, which is now `https://mini.tail8b0808.ts.net:8443`.
They are stale config that will confuse the next person who reads `serve status` —
which is the exact failure mode this whole change was about.

```bash
tailscale serve --https 443 --set-path /skips   off
tailscale serve --https 443 --set-path /capture off
tailscale serve status            # Deck must still be on / of 443
curl -s -o /dev/null -w '%{http_code}\n' https://mini.tail8b0808.ts.net/   # expect 200
```

**Not run yet, on purpose.** `/` of 443 is Deck, and the `off` form with `--set-path`
is unverified here — a wrong invocation could clear more of 443 than intended. There
is no `get-config` backup for node-level serve config (`get-config` is scoped to
Services, `svc:`), so the restore is by hand:

```bash
tailscale serve --bg --https 443 http://127.0.0.1:8770          # Deck, if it is lost
```

Do it when you can watch Deck, not during a storm.

## 2. Decide: keep the port, or give the board a Service

Today the board's URL is `https://mini.tail8b0808.ts.net:8443`. A Tailscale Service
would make it `https://magoun.tail8b0808.ts.net` — its own hostname, its own virtual
IP, no port, and no shared path space by construction. Same for Deck.

Verified on this machine (Tailscale 1.102.4):

- `tailscale serve --service <svc> ...` exists.
- `tailscale serve get-config <file> [--service|--all]` and `set-config <file>` —
  declarative, file-based, scriptable.
- `advertise` / `drain` / `clear` for moving a service between hosts.

**Not verified, and the thing to check first:** whether creating the Service itself
(the VIP and the hostname) needs an admin-console click, or an ACL grant letting this
node advertise it. The node half is clearly built for automation; the tailnet half is
unknown from here. If it turns out to be a grant, the policy file is API-writable, so
it stays scriptable.

Nothing in the code depends on the answer. `backend_url` lives in `data/config.json`
and is published through `fit.py` into `model.json`, so switching is: edit the URL,
re-run `fit.py`, re-publish. Beware that a `fit.py` run is a **real refit** — it picks
up whatever is in `data/pairs` at the time, which on 2026-09-27 moved the MBTA band
and broke all 28 fixtures. Read `src/refit.sh` before doing it.

## 3. If another app on this Mac ever gets served

Currently listening and **not** published to the tailnet: Plex (32400), and whatever
is on 8787 and 8000. If any of them goes onto the tailnet, give it its own origin —
a port now, a Service later. Do not add a path to 443.

## 4. Docs still describe the old recipe

`CLAUDE.md` (4 mentions) and `architecture.md` (1) still tell you to publish each
route with `tailscale serve --set-path`. That is no longer how it works: the public
origin is mounted at a root, once, and adding a route to `BROWSER_ROUTES` needs no
proxy change at all.

Deliberately left out of `56dcd82`: both files were being edited by a second Claude
session working in this tree on the figures page, and two agents doing
read-modify-write on one file loses work. Update them once that lands.

## 5. Re-creating serve config from scratch

`tailscale serve` config survives reboots but is lost if Tailscale is reinstalled or
`tailscale serve --https=443 off` is run. There are now two mounts across two ports,
one of which belongs to a different app:

```bash
tailscale serve --bg --https 443  http://127.0.0.1:8770    # Deck
tailscale serve --bg --https 8443 http://127.0.0.1:8724    # the board's public origin
```

Then `./ops/status.sh` should say `all good`.
