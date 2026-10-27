// pwa-starter: sw.js @ 45d5f8a
// Service worker: offline shell + cache-busting.
//
// mbta-glx: V is bumped by src/pwa_version.py, not by hand. fit.py and publish.py run it
// whenever they write web/, and tests/test_pwa.py fails when a shell file moved and V did not
// -- which is what catches a hand edit to index.html. The SHELL FINGERPRINT line below is
// that script's record of the shell V was last bumped for.
//
// THE ONE RULE: bump V whenever you change a precached SHELL file. A new V is what refreshes the
// shell — forget the bump and your fix ships to the repo but never to anyone's installed
// home-screen copy (iOS caches the SW aggressively). scripts/sw-lint.py guards this, and app.js
// surfaces a "tap to update" tag so a stuck phone is fixable in one tap.
//
// Strategy, by what the file IS rather than where it lives:
//   HTML/JS + navigations → CACHE-FIRST once installed: paint from the precache with NO network on
//     the critical path, so a load is instant and identical on a fast link, a slow one, or none.
//     Freshness is handled OFF the critical path, because the shell is owned per-generation
//     (cachePut won't overwrite it) and a new deploy MUST bump V anyway (THE ONE RULE): a V bump
//     installs the new shell and lights app.js's update tag. A background revalidate here would
//     just be discarded, so there isn't one. Only an uncached or unbootable request falls through
//     to a bounded network-first fetch, which ALWAYS ends at a real Response. NB this rests on
//     every live url being a SHELL file (it is as shipped): a future NON-shell .html/.js would be
//     served cache-first with no revalidation until a V bump collects the old generation — add
//     such a file to SHELL (and bump V), don't lean on opportunistic caching to refresh it.
//   JSON → paint from cache now. PRECACHED json is owned by the install, so a V bump is what
//     refreshes it; any other same-origin json is stale-while-revalidate
//   images and everything else → cache-first for speed; a V bump is what refreshes them
//   cross-origin data (your APIs) → straight through, never cached here
//
// The precache is PER-FILE — deliberately never a bare cache.addAll(). That buys self-healing and
// costs a third cache state, PARTIAL, which this whole file then has to reason about. Three
// questions addAll answered implicitly become live, and every function below answers one of them:
//   1. is this version's cache COMPLETE?     → ensureShell() / topUpThenCollect()
//   2. which GENERATION answers a read?      → cacheLookup(), the directional collect
//   3. is a complete-looking cache BOOTABLE? → bootable() / offlineFallback()
// Get one wrong and the failure is a blank white screen offline, a lost offline copy, or a stale
// one that never updates. The seven review rounds behind this design: #7.

const V = "magoun-v6";   // <-- bumped by src/pwa_version.py on every shell change
// SHELL FINGERPRINT: 0efc8393685b0d65

// "magoun-v" — the stem shared by every cache generation. pwa.js's VER_PREFIX must match it, and the
// NUMERIC TAIL is load-bearing: it is what makes the collect below DIRECTIONAL (delete strictly
// older generations, never "everything that isn't me"). app.js no longer ranks these names —
// checkVer() compares its own baked-in APP_V, not a cache key (#17) — so this file is now the
// only reader of the tail. scripts/sw-lint.py rejects a V without digits at commit time.
const V_STEM = V.replace(/\d+$/, "");

// Numeric generation of a cache name, or null if it isn't one of ours. Used to make the collect
// directional: a worker may only delete caches OLDER than its own.
function verNum(name) {
  const tail = name.startsWith(V_STEM) ? name.slice(V_STEM.length) : "";
  return /^\d+$/.test(tail) ? parseInt(tail, 10) : null;
}

// Every .html and .js the origin serves, except this file -- tests/test_pwa.py enforces that,
// because a live url left out is served stale until the next V bump. model.json is NOT here, on
// purpose: see MODEL below. og.png is not either: no page loads it, only link-preview scrapers.
const SHELL = [
  "./", "./index.html", "./app.js", "./model.js", "./pwa.js", "./pullToRefresh.js",
  "./figures.html", "./proof.html",
  "./manifest.json", "./icon-180.png", "./icon-192.png", "./icon-512.png",
];

// Absolute hrefs of the SHELL, resolved once so cachePut()'s ownership check is a Set lookup and
// not a URL parse per request. self.location is the sw.js URL, so "./" resolves to the scope root.
// MUST stay below the SHELL declaration — a const read from its own TDZ throws at script
// evaluation, which kills the whole worker: no install, no precache, blank screen offline.
const SHELL_HREFS = new Set(SHELL.map(u => new URL(u, self.location).href));

// Which SHELL entries this version's cache is missing. No network — pure cache reads.
//
// One parallel batch rather than an await per entry: app.js pings "ensure-shell" on every load,
// every controllerchange, and every foreground, and each ping runs this TWICE (the top-up, then
// the re-check in topUpThenCollect). Serialized, that's dozens of sequential Cache API round
// trips every time an iOS home-screen app foregrounds with a complete shell and nothing to do.
async function missingFromShell(cache) {
  const c = cache || await caches.open(V);
  const found = await Promise.all(SHELL.map(url => c.match(url)));
  return SHELL.filter((_, i) => !found[i]);
}

// TRANSIENT = a retry could fix it (5xx mid-deploy blip, 408, 429); any other non-ok status is a
// definite server answer. Shared by ensureShellOnce()'s classification and the live branch's
// navigation-error split — one predicate, so the two sites can't drift.
const isTransientStatus = s => s >= 500 || s === 408 || s === 429;

// Precache top-up. Deliberately NOT cache.addAll(): addAll is atomic, so a single 404 (a shell
// file renamed and the list not updated, a file deleted during scaffolding, icons not yet
// generated by make-icons.sh, a mid-deploy blip) rejects the whole install and the device ends up
// with NO cache at all — offline then shows a blank screen. Per-file puts degrade instead:
// whatever fetched is cached, the rest retries.
//
// It also only fetches what's MISSING, which makes it safe to call repeatedly — that's how an
// evicted cache repairs itself. iOS reclaims script-writable storage (Cache API included) under
// pressure and after ~7 idle days, and can leave the cache NAME behind while dropping the
// contents. install only runs on a V bump, so without this top-up a once-evicted cache stays
// empty forever and the app is permanently blank offline.
//
// Returns { transient, permanent }: HOW MANY entries failed in a way a retry could fix, and WHICH
// entries failed in a way no retry ever will. topUpThenCollect() keys the old-cache collect off
// that split, so both have to mean "not cached", not "attempted".
//
// Why the split: the collect waits for a complete precache, so a SHELL entry that can never be
// fetched — a renamed file, a typo'd path — would otherwise keep it waiting FOREVER. Both cache
// generations then live on the device permanently, with the old one still answering (via
// cacheLookup's whole-store fallback) for anything absent from the new one. A 404 is a bug in the
// SHELL list that no amount of retrying repairs, so it must not hold the collect hostage;
// everything genuinely retryable still does. scripts/sw-lint.py catches the repo-side version of
// this at commit time, before it can ship — this path is damage control, not the guard.
//
// Deliberately CONSERVATIVE about what counts as permanent, because guessing wrong trades a
// working offline copy for an empty one. A REDIRECT is transient — a captive portal redirects
// everything, and that is a mobile-normal state, not a broken shell list. 5xx is transient (a
// mid-deploy blip). 408/429 are transient by definition. A 206 is transient — a partial means the
// file IS on the server and some hop answered a plain GET with a range. Only a definite "this URL
// is not on the server" — 4xx other than those — is permanent.
//
// KNOWN LIMITATION: a put() that fails for QUOTA is counted transient, which keeps the old cache
// and so keeps consuming the quota that just ran out. Harmless at this skeleton's shell size, but
// CLAUDE.md §Offline tells adopters to precache PDFs and media — exactly where the shell gets big
// enough for this to bite. A larger shell should evict the old version to make room instead of
// holding both.
async function ensureShellOnce() {
  try {
    const c = await caches.open(V);
    const missing = await missingFromShell(c);
    const outcome = await Promise.all(missing.map(url =>
      fetch(url, { cache: "reload" })
        .then(resp => {
          // A redirected response can't satisfy a navigation (the SW spec rejects it), and
          // resp.ok is TRUE for a 206 but put() then throws. Skip both rather than poison the
          // entry. Every SHELL entry is same-origin, so there's no opaque case to exempt here.
          if (resp.redirected || resp.status === 206) return "transient";
          if (!resp.ok) return isTransientStatus(resp.status) ? "transient" : "permanent";
          return c.put(url, resp).then(() => "ok", () => "transient");
        })
        .catch(() => "transient")   // offline: leave it for the next attempt
    ));
    return {
      transient: outcome.filter(r => r === "transient").length,
      permanent: missing.filter((_, i) => outcome[i] === "permanent"),
    };
  } catch {
    // CacheStorage itself is unavailable — site data blocked, storage corrupt. Transient by
    // definition, and reporting the whole shell as retryable keeps the old cache as the net.
    return { transient: SHELL.length, permanent: [] };
  }
}

// install, activate, and the "ensure-shell" message can all fire close together; without this a
// V bump would fetch the whole shell 2-3x on a cellular connection. Callers that arrive mid-run
// join it instead of starting their own.
let shellRun = null;
function ensureShell() {
  return shellRun ??= ensureShellOnce().finally(() => { shellRun = null; });
}

self.addEventListener("install", e => {
  e.waitUntil(ensureShell().then(() => self.skipWaiting()));
});

// REPAIR BEFORE COLLECT, and only collect once THIS version's cache is complete.
//
// addAll's atomicity was a liability (one 404 lost the whole precache) but it was also a guard:
// a failed install meant the new SW never activated, so the previous cache kept serving. Per-file
// puts removed that guard — install now always resolves — so collecting first would let a V bump
// on a dead connection trade a working stale offline copy for an empty new one.
//
// "Complete" here means EVERY FETCHABLE SHELL URL IS PRESENT. It also means they came from the
// SAME deploy, because cachePut() refuses to write SHELL urls (see the note there): each
// generation's shell is fetched once, by the install that created it, so the net this keeps is
// coherent rather than merely complete by entry count.
//
// Keeping the old cache is NOT free, which is why this has to be re-runnable rather than a
// one-shot in activate: CacheStorage.match() iterates caches in CREATION order, so while an old
// version lingers it ANSWERS FIRST and shadows the current shell (verified in both engines —
// caches ['app-v6','app-v7'] both holding a URL resolve to the v6 copy). A lingering old cache
// means the device serves the previous release offline. cacheLookup() closes the read-path
// shadowing by construction, but the storage cost persists until something collects — and
// activate fires once per SW version, hence the retry from the message handler below, the only
// hook that runs after activation. (This used to confuse app.js's version tag too; it no longer
// can — checkVer() reads APP_V, not these keys. #17)
async function topUpThenCollect() {
  const { transient, permanent } = await ensureShell();
  if (transient > 0) return transient;              // keep the old cache as a net, try again later

  try {
    // Re-verify rather than trusting that count. ensureShell() dedupes concurrent callers, so a
    // joiner receives a completeness reading taken BEFORE it joined — if eviction landed mid-run,
    // "complete" is already false and we'd collect the net out from under a broken shell. Cheap
    // (cache reads only) and it makes the collect depend on current state, not a stale promise.
    //
    // Entries this run proved PERMANENTLY unfetchable are excluded: they are missing by definition
    // and always will be, so counting them here would re-wedge the collect that the transient/
    // permanent split exists to unwedge.
    const recheck = (await missingFromShell()).filter(u => !permanent.includes(u)).length;
    if (recheck > 0) return recheck;

    // Don't collect while another version is mid-install. From this worker's perspective the
    // incoming release's cache is merely "not V", so deleting it would throw away a precache that
    // is being built right now — and app.js pings us on load, which is exactly when an update
    // installs. Whichever worker activates next runs this same step and collects then.
    const reg = self.registration;
    if (reg && (reg.installing || reg.waiting)) return 0;

    // ...but that guard alone is NOT enough, so the collect is also DIRECTIONAL: only strictly
    // OLDER generations, never "everything that isn't me". An incoming worker calls skipWaiting()
    // as soon as its install resolves, at which point it is `active` with installing and waiting
    // both null — so an outgoing worker still finishing a slow top-up sails past the guard above
    // and deletes the new version's freshly built precache (observed downstream: the incoming
    // cache stuck at 7-9 of 13 entries while the old complete one survived, deleted mid-fill).
    // Comparing generations makes that impossible from either side regardless of who runs when.
    //
    // Caches that aren't ours (verNum → null) are left alone rather than swept: this worker has
    // no claim on them, and "delete everything unfamiliar" is how a SW eats a sibling app's
    // storage on a shared origin. V MUST END IN DIGITS: without a numeric tail verNum(V) is null,
    // `n < null` is false for every cache, and collection silently stops — no error, no symptom,
    // until generations pile up. scripts/sw-lint.py rejects that shape at commit time; this is
    // the runtime half of the contract.
    const mine = verNum(V);
    if (mine === null) return 0;

    const ks = await caches.keys();
    await Promise.all(ks
      .filter(k => { const n = verNum(k); return n !== null && n < mine; })
      .map(k => caches.delete(k)));
  } catch {
    // Storage went away mid-collect. Keep the old cache and retry on the next ping rather than
    // rejecting: this runs inside install's and the message handler's waitUntil().
    return SHELL.length;
  }
  return 0;
}

self.addEventListener("activate", e => {
  e.waitUntil(topUpThenCollect().then(() => self.clients.claim()));
});

// app.js pings this on every online load, foreground, and controllerchange, so an evicted
// precache heals on the next launch with a connection instead of waiting for the next V bump —
// and a collect deferred at activate time (incomplete shell, or an install in flight) gets
// retried here.
//
// NB: the collect deliberately lives in topUpThenCollect() and not inside ensureShellOnce(),
// which also runs during install — deleting the old cache then would strand pages still
// controlled by the previous worker.
self.addEventListener("message", e => {
  if (e.data === "ensure-shell") e.waitUntil(topUpThenCollect());
});

// Cache-write gate. A fetch() only REJECTS on a network failure — a 404 or a mid-deploy 502
// arrives as a RESOLVED response, so an ungated c.put() happily overwrites a good cached copy
// with an error body, which then survives as the offline fallback until the next V bump. (Same
// principle as data.js refusing to cache an empty 200: a 200-shaped response isn't necessarily a
// good one.)
//
// A redirected response can't be used to satisfy a navigation, so caching one is another route to
// a blank screen — if "./" ever grows a redirect, don't store it. 206 needs its own clause
// because resp.ok is TRUE for a partial (verified in both engines) and cache.put() then throws;
// irrelevant to JSON and icons, immediately relevant to any adopter caching audio or video.
//
// mbta-glx: the starter exempts OPAQUE responses here for its Google Fonts branch. The board uses
// system fonts and that branch is gone, so nothing cross-origin reaches cachePut and a bare
// `resp.ok` gate is the whole rule.
function cachePut(req, resp) {
  // SHELL entries are owned by ensureShellOnce() and by nothing else. Letting opportunistic
  // request traffic write them too is what produced MIXED-GENERATION caches: V is whatever the
  // CURRENT worker declares, so a shell file whose bytes changed on the server got overwritten in
  // the current cache one file at a time while its neighbours kept their older entries — no V
  // bump needed, one redeployed file is enough. This skeleton's shell is two coupled sets
  // (index.html + styles.css + app.js + theme.js + data.js, and usage/index.html +
  // usage/crunch.js); a document from one deploy driving scripts from another is exactly the
  // confusing-bug class. Skipping SHELL here makes "a V bump is what refreshes them" literally
  // true: each generation's shell is fetched once, together, by the install that created it.
  if (SHELL_HREFS.has(req.url)) return;
  if (resp.redirected || resp.status === 206) return;
  if (!resp.ok) return;
  const copy = resp.clone();
  // The .catch() matters: non-GET requests, 206s, and quota exhaustion all surface at put(), and
  // an uncaught rejection in a SW is just noise in a log nobody reads — the caller's response has
  // already been returned either way.
  caches.open(V).then(c => c.put(req, copy)).catch(() => {});
}

// Read the CURRENT version first, then fall back to the whole store.
//
// CacheStorage.match() scans caches in CREATION order, so a lingering old version outranks the
// current one — collecting promptly only closes that shadowing by timing. Scoping the first
// lookup to V closes it by construction: the old cache can still fill a gap (it's the net that
// makes a failed V bump survivable) but it can no longer outrank a complete current shell.
// If only one thing from this file propagates to a downstream copy, it should be this.
//
// NEVER REJECTS. This is called from inside the offline catch handler, the last stop before
// offlineFallback() — a throw there escapes as a rejected respondWith(), and WebKit paints the
// same blank white screen this file exists to prevent. A bare caches.match() can't throw, but
// caches.open(V) can (site data blocked in Safari, corrupt or evicted storage). Resolve undefined
// instead and let the caller reach the fallback.
async function cacheLookup(req) {
  try {
    const c = await caches.open(V);
    const hit = await c.match(req);
    if (hit) return hit;
  } catch {}
  try {
    return await caches.match(req);
  } catch {
    return undefined;
  }
}

// Subresources a cached document cannot BOOT without, per document. Per-file precaching means the
// cache can legitimately hold a document while a script it needs is still missing (one 500 during
// install), and the shell fallback below is navigation-only by design — so offline, the document
// is served, its script request gets an empty 504, and the page boots broken: a bare header, no
// content, no error, no hint. Serving the honest offline page instead is strictly better — it
// says what to do, and one online launch repairs the precache.
//
// List only what the DOCUMENT dies without, not everything it loads — gating on a nice-to-have
// replaces a working offline page with an error page. As shipped:
//   - the root page renders its placeholder markup with NO scripts at all, so its honest list is
//     EMPTY. The moment your paint() owns the content, ./theme.js, ./data.js and ./app.js all
//     become boot-critical (a missing theme.js throws in boot() before render() ever runs), as
//     does any vendored chart lib — list them here then.
//   - usage/ is the opposite: crunch.js builds the whole dashboard, so without it the page sticks
//     at "loading…" forever.
//
// mbta-glx: the board dies without app.js (it is the whole port). Not model.js: online the model
// comes from model.json over the network, and offline there is no feed to draw anyway. Not pwa.js
// either -- without it the board runs and only the update tag is missing.
const BOOT_DEPS = {
  "": ["./app.js"], "index.html": ["./app.js"],
};

// Request pathname → BOOT_DEPS key, relative to the SW scope (works at user.github.io/repo/).
const SCOPE_PATH = new URL("./", self.location).pathname;
function docKey(pathname) {
  return pathname.startsWith(SCOPE_PATH) ? pathname.slice(SCOPE_PATH.length) : pathname;
}

// Uses cacheLookup(), not a V-scoped read: a dep that only survives in the previous generation's
// cache will still be SERVED from there, so it counts as present. The gate has to model what the
// subresource request will actually get, or it fires on pages that would have booted fine.
async function bootable(pathname) {
  const deps = BOOT_DEPS[docKey(pathname)] || [];
  return (await Promise.all(deps.map(u => cacheLookup(u)))).every(Boolean);
}

// The FALLBACK network fetch is BOUNDED by these timers. Cache-first (the live branch) means the
// common, fully-cached load never reaches them; they run only when the cache can't answer — a
// first run, or an evicted/partial shell — where a slow-but-alive link ("lie-fi": a weak cell
// signal, a captive portal that half-answers) would otherwise hang respondWith() on a fetch that
// never settles: the very blank screen this file fights, now with no end. Bounding turns that into
// an eventual real Response.
//
// TWO bounds, because a timeout costs differently in the two states:
//   WARM (a cached copy in hand — even a non-bootable one to fall back to): short. There's a real
//     page one lookup away, so a stale-but-instant paint beats waiting on a dead-slow link.
//   COLD (NOTHING cached — a first run, or a shell iOS evicted under storage pressure / after ~7
//     idle days, the routine case ensureShellOnce() documents): longer, because the only fallback
//     is offlineFallback()'s "try again" page and a working-but-slow link that would have delivered
//     the real app at 8s shouldn't be cut off at 3s. But NOT unbounded: an evicted shell on a weak
//     signal is exactly the reported failure, and an eventual honest page beats a permanent blank.
const NET_TIMEOUT_MS = 3000;
const NET_TIMEOUT_COLD_MS = 15000;

// Reject `promise` if it hasn't settled within `ms`, so a timeout routes into the offline catch
// rather than stranding respondWith() on a fetch that never settles. The underlying fetch is
// untouched — racing a timer against it doesn't abort it — so the caller keeps it alive under
// waitUntil. clearTimeout on settle so a resolved fetch doesn't hold a pending timer (and the SW)
// awake for the remainder of the window.
function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("network timeout")), ms);
    promise.then(
      v => { clearTimeout(timer); resolve(v); },
      err => { clearTimeout(timer); reject(err); }
    );
  });
}

self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);

  // cache.put() rejects for anything but GET ("Request method is not GET"), and a form POST is
  // mode === "navigate" — so without this it walks straight into the live branch and cachePut().
  // The skeleton has no POSTs; an adopter's form, sendBeacon, or Apps Script write (§Analytics)
  // inevitably is one.
  if (e.request.method !== "GET") return;

  // Cross-origin data (your APIs, third-party JSON): straight to network, don't touch the cache.
  if (u.origin !== location.origin) return;

  // The SW must never intercept or cache its own script: checkVer() probes
  // ./sw.js?_=<ts> to read the live version, and caching those probes both bloats
  // the cache (a new dead entry per resume) and — .js IS served cache-first now, so
  // an ignoreSearch match would serve a stale version back — wedges the "tap to
  // update" tag. The unique query string keeps exact-match lookups missing today;
  // this guard is what makes that a contract instead of an accident.
  if (u.pathname.endsWith("/sw.js")) return;

  // MODEL: model.json goes straight to the network, like cross-origin data. app.js fetches it
  // with cache:"no-store" every hour and reloads the page when the text changes, which is how a
  // refit reaches a board left running. Answered from here, that fetch would get the precached
  // copy forever (precached json is never revalidated, below) or a stale-while-revalidate copy
  // an hour late, and the reload would quietly stop happening. Offline the fetch fails and
  // app.js falls back to model.js, which IS in the shell.
  if (u.pathname.endsWith("/model.json")) return;

  // Navigations are decided FIRST, ahead of the .json test below: a document request must always
  // end at a real page — including a direct navigation to a .json URL, which the SWR branch would
  // otherwise answer offline with a rejected respondWith (a bare network error), not the fallback.
  const live = e.request.mode === "navigate" || u.pathname.endsWith("/") || /\.(html|js)$/.test(u.pathname);

  // Same-origin JSON → serve the cached copy IMMEDIATELY, refresh behind it. This is the SW twin
  // of data.js's cache-first paint. JSON here is DATA (a committed dataset, the manifest), and
  // network-first made every cold start block on a round trip for it even with a perfectly good
  // cached copy. The tradeoff is real but small: a JSON change lands one load later than an
  // HTML/JS change. If some .json of yours is genuinely code-like and must be live, move it into
  // the `live` test above.
  if (!live && /\.json$/.test(u.pathname)) {
    e.respondWith(cacheLookup(e.request).then(cached => {
      // PRECACHED json is NOT revalidated: cachePut() refuses to write SHELL urls (they're
      // ensureShellOnce()'s to own), so revalidating one would fetch the payload and discard it —
      // cellular spent for nothing on every launch. A V bump is what refreshes it, which THE ONE
      // RULE already requires. Conditional on a cached copy EXISTING, so a first run or an
      // evicted entry still falls through to the network.
      if (cached && SHELL_HREFS.has(e.request.url)) return cached;
      const net = fetch(e.request).then(resp => { cachePut(e.request, resp); return resp; });
      e.waitUntil(net.catch(() => {}));   // keep the SW alive for the refresh; offline is fine
      // No cached copy (first run) → wait for the network, but END AT A RESPONSE: a first run
      // offline was the one branch left that could settle respondWith() with a rejection.
      return cached || net.catch(() => offlineFallback(e.request));
    }));
    return;
  }

  // Same-origin: HTML/JS + navigations → CACHE-FIRST; other assets (images) → cache-first too.
  //
  // The old strategy here was network-first, and it hid a mobile-common failure: fetch() only
  // rejects on a real failure, so a connection that is UP but crawling ("lie-fi" — a weak cell
  // signal, a captive portal that half-answers) makes the fetch hang rather than reject. The
  // offline catch never fired, respondWith() stayed pending, and WebKit painted a blank screen —
  // "internet, but too slow to answer." Network-first also fetched the shell on every load only
  // to DISCARD it: cachePut() refuses to overwrite SHELL urls, and every live url is a SHELL
  // file. So the latency bought no freshness the update tag doesn't already deliver.
  //
  // Cache-first fixes both: paint straight from the precache, no network on the critical path,
  // instant whether the link is fast, slow, or gone. Freshness is off this path (a V bump
  // installs the new shell and lights the tag; see the strategy note up top). The network is
  // touched only when the cache CAN'T answer — a first run, or an evicted/partial shell — and
  // that fetch is bounded so even it can't hang on lie-fi.
  //
  // One accepted consequence of serving the shell from cache rather than the network: during a
  // partial install (V bumped, top-up not yet complete), cacheLookup()'s whole-store fallback can
  // pair a new-generation document with an old-generation subresource — index.html@vN with
  // app.js@vN-1 — until the collect completes. bootable() gates only boot-critical deps (as
  // shipped, usage/'s crunch.js), so the window is cosmetic and self-heals.
  if (live) {
    e.respondWith((async () => {
      const cached = await cacheLookup(e.request);

      // Serve the cached copy immediately. A navigation must also be BOOTABLE — a cached document
      // missing a script it dies without (usage/ without crunch.js sticks at "loading…" forever)
      // renders broken with no hint, worse than the honest fallback — so a non-bootable
      // navigation drops through to the network path instead.
      if (cached && (e.request.mode !== "navigate" || await bootable(u.pathname))) {
        return cached;
      }

      // No usable cached copy: first run, or an evicted/partial shell. Go to the network, bounded
      // EITHER WAY (see the try below): short when a fallback page is in hand, longer
      // (NET_TIMEOUT_COLD_MS) on a true first run where offlineFallback() is the only floor — but
      // never unbounded, so it always ends at a real Response.
      const net = fetch(e.request).then(resp => {
        cachePut(e.request, resp);   // a no-op for SHELL urls; repair is ensureShell()'s job
        // A navigation's fetch runs with redirect mode "manual", so a server 301/302 arrives as
        // an OPAQUEREDIRECT — status 0, ok FALSE, but a healthy answer the browser must be handed
        // back so it can follow it (GitHub Pages 301s the slashless "/repo/usage"). Treating it
        // as an error would show the offline page while fully online. Subresources never see one
        // (their redirect mode is "follow"), so no navigation gate is needed.
        if (resp.type === "opaqueredirect") return resp;
        if (!resp.ok) {
          // A 4xx/5xx is a RESOLVED fetch, not a rejection. For a subresource, a good cached copy
          // beats handing the app an error body. For a NAVIGATION, split by whether a retry could
          // ever fix it — isTransientStatus(), the same judgment ensureShellOnce() applies:
          //   TRANSIENT (5xx mid-deploy, 408, 429) → throw into the catch. The only cached copy
          //     reachable here already FAILED bootable() (cache-first would have served it
          //     otherwise), so the honest try-again page beats both it and a raw error body.
          //   PERMANENT (other 4xx — a typo'd link, a deleted page) → serve the server's answer.
          //     The offline page would LIE to an online user ("open it once with a connection")
          //     behind a Try Again loop that can never win; the real 404 is actionable.
          if (e.request.mode === "navigate") {
            if (isTransientStatus(resp.status)) throw new Error("http " + resp.status);
            return resp;
          }
          return cacheLookup(e.request).then(r => r || resp);
        }
        return resp;
      });
      try {
        // Bounded either way (see NET_TIMEOUT_*): WARM has a page to fall back to, COLD has only
        // offlineFallback — but NEITHER may hang. A timeout, an offline rejection, or a navigation
        // 5xx (thrown above) all land in the catch.
        return await withTimeout(net, cached ? NET_TIMEOUT_MS : NET_TIMEOUT_COLD_MS);
      } catch {
        // Park the in-flight fetch under waitUntil: a timeout doesn't abort it, so this swallows
        // its eventual rejection instead of leaking an unhandled one (and lets a late success for
        // any non-SHELL live url still land in the cache — for SHELL urls cachePut discards it;
        // repair is ensureShell()'s job). Resolving respondWith() to undefined is the original
        // blank-screen bug — WebKit fails the navigation with "Returned response is null" and iOS
        // paints a blank white page, no text, nothing to act on; Chromium survives it, which is
        // why it never shows up in desktop testing — so every branch below ends at a real
        // Response.
        e.waitUntil(net.catch(() => {}));

        // For a navigation, a document that can't load a boot-critical script renders broken —
        // worse than the honest fallback. Re-checked LIVE (not from the pre-network snapshot): an
        // "ensure-shell" repair can land inside the timeout window and flip this true.
        if (e.request.mode === "navigate" && !(await bootable(u.pathname))) {
          return offlineFallback(e.request);
        }
        // The ./index.html fallback is doubly gated:
        //   - NAVIGATIONS only: `live` also matches .js, and handing HTML to an uncached
        //     theme.js request makes the script fail to PARSE instead of failing cleanly.
        //   - the ROOT document only: this is a multi-page skeleton, and answering an uncached
        //     /usage/ navigation with the root page hands the user the wrong document — the
        //     honest offline page beats a page they didn't ask for. What the root fallback still
        //     catches: query-variant navigations like "./?data=…", which miss the exact-match
        //     lookup above.
        const key = docKey(u.pathname);
        const shell = e.request.mode === "navigate" && (key === "" || key === "index.html")
          ? await cacheLookup("./index.html") : null;
        // Re-read the cache rather than trusting the pre-network snapshot: an "ensure-shell"
        // repair (app.js pings it on every load) can land during the timeout window, and the
        // fresh copy should win over the snapshot when it does.
        return (await cacheLookup(e.request)) || cached || shell || offlineFallback(e.request);
      }
    })());
  } else {
    e.respondWith(
      cacheLookup(e.request).then(r => r || fetch(e.request).then(resp => {
        cachePut(e.request, resp);
        return resp;
      }).catch(() => offlineFallback(e.request)))
    );
  }
});

// Terminal fallback: always a real Response, never undefined. Navigations get a readable page
// (the precache is empty or unbootable — the one thing that fixes it is one online launch, which
// ensureShell() then uses to repair itself); subresources get a plain 504.
//
// This page is a deliberate exception to two repo rules, so a copier doesn't "fix" it:
//   - CSS is INLINE, not in styles.css. It exists precisely for the case where the precache is
//     empty, so styles.css is by definition unavailable — a <link> would render it unstyled in
//     the one situation it's for. (Same reasoning as index.html's pre-paint theme stamp.)
//   - It HARDCODES the board's palette (index.html's --bg / --ink / --dim / --go, both schemes)
//     and system-ui — a fallback page that depends on a cached font defeats itself.
function offlineFallback(req) {
  if (req.mode !== "navigate") {
    return new Response("", { status: 504, statusText: "Offline" });
  }
  return new Response(
    `<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Offline — Magoun</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#f6f8f5;color:#16201b;font:16px/1.5 system-ui,-apple-system,sans-serif}
  main{max-width:22em;padding:2em;text-align:center}
  h1{font-size:1.15em;margin:0 0 .6em}
  p{margin:.6em 0;color:#5e6a63}
  button{margin-top:1.2em;border:0;border-radius:999px;padding:.7em 1.3em;
         background:#00843D;color:#fff;font-size:1em;cursor:pointer}
  @media (prefers-color-scheme:dark){ body{background:#11150f;color:#e8efe8} p{color:#8d9a90} }
</style>
<main>
  <h1>Offline, and nothing cached yet</h1>
  <p>The offline copy of this app hasn't been stored on this device — or the system reclaimed it
     to free up space.</p>
  <p>Open it once with a connection and it will rebuild itself for offline use.</p>
  <button onclick="location.reload()">Try again</button>
</main>`,
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}
