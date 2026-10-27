// pwa-starter: pullToRefresh.js @ af148d7
// Pull-to-refresh for installed (standalone) PWAs. OPTIONAL — delete this file
// and its <script> + CSS if your app doesn't need it.
//
// WHY: mobile Safari has a native pull-to-refresh, but iOS strips it in
// standalone mode (no browser chrome, so no reload button either). An installed
// app therefore has NO way to force a refresh unless you add one. This provides
// the gesture in-app and, crucially, only enables in standalone so it never
// double-fires with the browser's native PTR.
//
// The fiddly bits it gets right (the reason this is a file, not three lines):
//   - only starts a pull when the page is scrolled to the very top;
//   - bails if the page starts scrolling mid-gesture (a real scroll, not a pull);
//   - touchmove is passive:false so it can preventDefault the iOS rubber-band
//     during an active pull — but only then;
//   - a damping factor maps pulled distance to a smaller visual offset so it
//     feels rubber-banded rather than 1:1.
//
// Loaded as a classic script before pwa.js; exposes a global `PullToRefresh`.
// Styling lives in index.html's <style> (#ptr-indicator / .ptr-spinner).

window.PullToRefresh = class {
  constructor({ onRefresh }) {
    this.onRefresh = onRefresh;
    this.startY = null;
    this.currentPull = 0;
    this.refreshing = false;
    this.indicator = null;
    this.THRESHOLD = 80;   // px pulled past which release triggers a refresh
    this.MAX_PULL = 120;   // visual cap so the indicator can't fly off-screen
    this.DAMPING = 0.5;    // pulled px → half the visual offset (rubber-band feel)
  }

  init() {
    const standalone = matchMedia("(display-mode: standalone)").matches
      || window.navigator.standalone === true;
    if (!standalone) return;   // browser tab: Safari's native PTR already covers this

    this._createIndicator();
    document.addEventListener("touchstart", e => this._onStart(e), { passive: true });
    // passive:false so _onMove can preventDefault during an active pull only.
    document.addEventListener("touchmove", e => this._onMove(e), { passive: false });
    document.addEventListener("touchend", () => this._onEnd(), { passive: true });
    // mbta-glx: not mid-refresh -- resetting then clears `refreshing` while onRefresh is still
    // running, and the next pull starts a second refresh on top of the first.
    document.addEventListener("touchcancel", () => { if (!this.refreshing) this._reset(); }, { passive: true });
  }

  _createIndicator() {
    const div = document.createElement("div");
    div.id = "ptr-indicator";
    div.innerHTML = '<div class="ptr-spinner"></div>';
    document.body.appendChild(div);
    this.indicator = div;
  }

  _scrollTop() { return window.scrollY || document.documentElement.scrollTop || 0; }

  // mbta-glx: the page can be at the top while a box inside it is scrolled (the board's train
  // list scrolls on its own). A downward drag there is the list scrolling back up, not a pull.
  _inScrolledBox(el) {
    for (; el && el !== document.body; el = el.parentElement) if (el.scrollTop > 0) return true;
    return false;
  }

  _onStart(e) {
    if (this.refreshing) return;
    if (this._scrollTop() > 0 || this._inScrolledBox(e.target)) { this.startY = null; return; }  // only from the top
    this.startY = e.touches[0].clientY;
    this.currentPull = 0;
  }

  _onMove(e) {
    if (this.refreshing || this.startY === null) return;
    if (this._scrollTop() > 0) { this._reset(); return; }       // turned into a scroll — abandon
    const delta = e.touches[0].clientY - this.startY;
    if (delta <= 0) return;                                     // pulling up, not down
    e.preventDefault();                                         // suppress the rubber-band, now that it's a real pull
    this.currentPull = Math.min(delta * this.DAMPING, this.MAX_PULL);
    this.indicator.classList.add("pulling");
    this.indicator.style.setProperty("--ptr-y", `${this.currentPull}px`);
    this.indicator.style.setProperty("--ptr-opacity", String(Math.min(this.currentPull / this.THRESHOLD, 1)));
  }

  _onEnd() {
    if (this.refreshing || this.startY === null) return;
    this.indicator.classList.remove("pulling");
    if (this.currentPull >= this.THRESHOLD) this._trigger();
    else this._reset();
  }

  async _trigger() {
    this.refreshing = true;
    this.indicator.classList.add("refreshing");
    this.indicator.style.setProperty("--ptr-y", `${this.THRESHOLD}px`);
    this.indicator.style.setProperty("--ptr-opacity", "1");
    try { await this.onRefresh(); }
    catch (e) { console.error("Pull-to-refresh failed", e); }
    this._reset();
  }

  _reset() {
    this.refreshing = false;
    this.startY = null;
    this.currentPull = 0;
    if (!this.indicator) return;
    this.indicator.classList.remove("pulling", "refreshing");
    this.indicator.style.setProperty("--ptr-y", "0px");
    this.indicator.style.setProperty("--ptr-opacity", "0");
  }
};
