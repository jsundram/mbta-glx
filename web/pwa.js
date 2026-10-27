/* The board as an installed app: service worker, "tap to update" tag, pull-to-refresh.
 *
 * The service-worker slice of pwa-starter's app.js (checkVer, forceUpdate,
 * requestShellTopUp, and its boot wiring), not the whole file -- the board already
 * owns its own data loop, theme and resume handling, so the rest has no job here.
 * Not stamped for that reason: PROPAGATE.md tracks whole-file copies only.
 *
 * Why it exists: an iOS home-screen app resumes rather than reloads, so a deploy
 * can sit unseen for days -- it did, the first time the alert banner changed. The
 * tag compares the version baked into THIS file (APP_V) with the one the server's
 * sw.js declares, and offers the update when they differ. APP_V, never a cache
 * key: after skipWaiting()/clients.claim() the keys are already the new version
 * while this page is still running the old code (pwa-starter #17).
 *
 * Nothing here is needed for the board to work. From file:// there is no service
 * worker and no Cache API, and every function below degrades to doing nothing.
 */
(function () {
  const VER_PREFIX = "magoun-v";   // sw.js's V stem
  const APP_V = "magoun-v4";       // = sw.js's V; src/pwa_version.py moves both together

  /** Show the tag when the server is ahead of the code running here. Returns
   *  whether it is, so pull-to-refresh can apply the update instead of polling. */
  async function checkVer() {
    const tag = document.getElementById("ver");
    // A worker installed at all? That is all cache keys can honestly answer, and it
    // keeps the tag out of a plain tab or file:// (where `caches` does not exist).
    let installed = false;
    try { installed = (await caches.keys()).some(k => k.startsWith(VER_PREFIX)); } catch (e) {}
    if (!installed) { if (tag) tag.hidden = true; return false; }

    let latest = "";
    try {   // ?_= + no-store dodges both the SW cache and the HTTP cache
      const src = await (await fetch("./sw.js?_=" + Date.now(), {cache: "no-store"})).text();
      // The declaration, not the first prefix-shaped string: sw.js's comments
      // mention version names, and matching one pins a tag that does nothing.
      latest = (src.match(/const V\s*=\s*"([^"]*)"/) || ["", ""])[1];
    } catch (e) {}   // offline: no claim either way
    const behind = !!latest && latest !== APP_V;
    if (tag) {
      tag.hidden = !behind;
      tag.onclick = behind ? forceUpdate : null;
    }
    return behind;
  }

  /** The hammer: drop every cache and reload, so the worker refetches the shell. */
  async function forceUpdate() {
    try { await Promise.all((await caches.keys()).map(k => caches.delete(k))); } catch (e) {}
    location.reload();
  }

  /** iOS can evict Cache API contents while keeping the registration, and sw.js
   *  only precaches on install. One online launch with this ping repairs it. */
  function requestShellTopUp() {
    if (!("serviceWorker" in navigator) || !navigator.onLine) return;
    navigator.serviceWorker.getRegistration()
      .then(reg => { if (reg && reg.active) reg.active.postMessage("ensure-shell"); })
      .catch(() => {});
  }

  /** `refresh` is the board's own re-poll; pull-to-refresh runs it, or applies a
   *  waiting update instead, since that is what a rider pulling down wants. */
  function boot({refresh}) {
    // Plain text, not a control: the update tag and pull-to-refresh already act on it.
    const build = document.getElementById("build");
    if (build) build.textContent = APP_V;
    // http(s) only: from file:// register() rejects, which is harmless but noisy.
    if ("serviceWorker" in navigator && /^https?:$/.test(location.protocol)) {
      navigator.serviceWorker.register("./sw.js").catch(() => {});
      // A registration can exist with no ACTIVE worker for a moment (first install,
      // or mid-update), and the top-up ping would be dropped; retry on takeover.
      navigator.serviceWorker.addEventListener("controllerchange", requestShellTopUp);
    }
    checkVer();
    requestShellTopUp();
    if (window.PullToRefresh) {
      new PullToRefresh({
        onRefresh: async () => { if (await checkVer()) return forceUpdate(); await refresh(); },
      }).init();   // standalone only; a no-op in a browser tab
    }
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") { checkVer(); requestShellTopUp(); }
    });
  }

  window.Pwa = {boot: boot, checkVer: checkVer, APP_V: APP_V};
})();
