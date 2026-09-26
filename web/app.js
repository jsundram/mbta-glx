/* Magoun inbound ETAs, computed in the browser with no backend.
 *
 * This is a port of src/service.py's `compute_rows` and the helpers it needs.
 * The two implementations must agree exactly: tests/fixtures/cases-*.json are run
 * through both by tests/test_contract.py, via tests/run_cases.js. If you change
 * anything above the "the board" divider, expect that test to have an opinion.
 *
 * It stays a port, not a second model: every fitted number and every constant is
 * read from model.json. Nothing here fits, simulates or scores.
 *
 * Loads as a plain script (window.Magoun) or as a CommonJS module in node.
 */
"use strict";
(function (exports) {

// ---------------------------------------------------------------- the contract

class Model {
  constructor(m) {
    this.m = m;
    this.grid = m.grid;
  }

  /** Nearest quantile level on the grid; ties take the lower index, as Python's
   *  min() does. */
  _q(arr, q) {
    let best = 0;
    for (let k = 1; k < this.grid.length; k++)
      if (Math.abs(this.grid[k] - q) < Math.abs(this.grid[best] - q)) best = k;
    return arr[best];
  }

  schedOffset(q) { return this._q(this.m.sched.q, q); }
  berthOffset(q) { return this._q(this.m.berth.q, q); }

  get berthConst() {
    return [this.m.berth.turn_plus_run, this.m.berth.sched_bias];
  }

  /** Constants live in model.json so both implementations read one source.
   *  Unlike the Python side this has no literal fallbacks on purpose: a published
   *  model.json missing a constant is a broken deploy, and a silent default would
   *  make the board quietly disagree with the tested implementation. */
  need(name) {
    const c = this.m.constants || {};
    if (!(name in c)) throw new Error(`model.json has no constant ${name}`);
    return c[name];
  }

  get headway() { return this.m.headway_median_s; }
  get stops() { return this.need("stops"); }
}

const iso = s => (s ? Date.parse(s) / 1000 : null);

const stopOf = rel =>
  ((rel && rel.stop && rel.stop.data) || {}).id ?? null;

/** False for stale positions: parked, out-of-service trains sit for hours. */
const isLive = (attrs, now, staleS) => {
  const u = iso(attrs.updated_at ?? null);
  return u !== null && (now - u) <= staleS;
};

/** A non-revenue train runs express and cannot be boarded; without this a
 *  deadhead at the terminus satisfies the no-show veto. v3 only. */
const isRevenue = attrs => (attrs.revenue ?? "REVENUE") !== "NON_REVENUE";

const MOVING = ["IN_TRANSIT_TO", "INCOMING_AT"];

/** Where each inbound GLX train is right now, from vehicle positions. */
function upstreamState(snap, model, staleS) {
  const s = model.stops;
  if (staleS === undefined) staleS = model.need("stale_vehicle_s");
  const out = {departed_ball: [], departed_med: [], at_terminus: 0,
               ghosts: 0, non_revenue: 0};
  for (const v of snap.vehicles) {
    const a = v.attributes, stop = stopOf(v.relationships);
    if (!isLive(a, snap.t, staleS)) { out.ghosts++; continue; }
    if (!isRevenue(a)) { out.non_revenue++; continue; }
    if (a.direction_id !== 0) {
      // Outbound train sitting at / approaching the Medford/Tufts terminus.
      if (stop === s.med_out) out.at_terminus++;
      continue;
    }
    if (stop === s.magoun_in && MOVING.includes(a.current_status))
      out.departed_ball.push(v.id);
    else if (stop === s.ball_in && MOVING.includes(a.current_status))
      out.departed_med.push(v.id);
    else if (stop === s.med_in) out.at_terminus++;
  }
  return out;
}

const slotsWithTrips = (slots, skipped) =>
  slots.filter(([, tr]) => !skipped.has(tr));

const skippedSlotTimes = (slots, skipped, now) =>
  slots.filter(([t, tr]) => skipped.has(tr) && t > now).map(([t]) => t);

/** Python sorts (time, vehicle) tuples. Vehicle ids are ASCII, so a plain string
 *  compare matches; a null id only ever sorts against another null in practice. */
function byTimeThenVehicle(x, y) {
  if (x[0] !== y[0]) return x[0] - y[0];
  const a = x[1], b = y[1];
  if (a === b) return 0;
  if (a === null) return -1;
  if (b === null) return 1;
  return a < b ? -1 : 1;
}

/** Pure: everything the prediction needs is an argument, nothing is fetched.
 *  The mirror of service.compute_rows -- keep them line-for-line comparable. */
function computeRows(now, preds, vehicles, model, walk, qs, horizon, berths,
                     slots, skipped) {
  const [ql, qm, qh] = qs;
  const bands = model.need("band_s");
  const veto = model.need("veto_window_s");
  const dedupe = model.need("dedupe_s");
  const minGap = model.need("min_gap_s");
  const stops = model.stops;
  const state = upstreamState({t: now, vehicles}, model,
                              model.need("stale_vehicle_s"));
  const rows = [];

  // 1. MBTA's own inbound predictions -- the operator's model, use it first.
  const mbta = [];
  for (const p of preds) {
    const a = p.attributes, rel = p.relationships;
    if (stopOf(rel) !== stops.magoun_in || a.direction_id !== 0) continue;
    const t = iso(a.arrival_time || a.departure_time);
    if (t && t > now)
      mbta.push([t, ((rel.vehicle || {}).data || {}).id ?? null]);
  }
  mbta.sort(byTimeThenVehicle);
  for (const [t, vid] of mbta) {
    let src = "mbta";
    if (vid && state.departed_ball.includes(vid)) src = "departed Ball Sq";
    else if (vid && state.departed_med.includes(vid)) src = "departed Medford/Tufts";
    const band = Number(bands[src]);
    rows.push({eta: t, lo: t - band, hi: t + band,
               source: src, backed: true, vehicle: vid});
  }

  // 2. Trains already berthed at Medford/Tufts: departure is bounded below by
  //    the physical turnaround, which the timetable cannot express.
  const [turnRun, bias] = model.berthConst;
  const slotTimes = slots.map(([t]) => t);
  const covered = new Set(rows.map(r => r.vehicle));
  const berthed = Object.entries(berths).sort((a, b) => a[1] - b[1]);
  for (const [vid, berth] of berthed) {
    if (covered.has(vid)) continue;
    const nxt = slotTimes.filter(t => t + bias > berth);
    let base = berth + turnRun;
    if (nxt.length) base = Math.max(base, nxt[0] + bias);
    const mid = base + model.berthOffset(qm);
    if (mid <= now || mid > now + horizon) continue;
    rows.push({eta: mid, lo: base + model.berthOffset(ql),
               hi: base + model.berthOffset(qh),
               source: "berthed at Medford/Tufts", backed: true, vehicle: vid});
  }

  // 3. Schedule beyond everything visible, corrected for measured bias.
  const last = rows.length ? Math.max(...rows.map(r => r.eta)) : now;
  for (const [s] of slotsWithTrips(slots, skipped)) {
    const mid = s + model.schedOffset(qm);
    if (mid <= Math.max(now, last + minGap) || mid > now + horizon) continue;
    const backed = !(s - now < veto && state.at_terminus === 0);
    if (rows.some(r => Math.abs(r.eta - mid) < dedupe)) continue;
    rows.push({eta: mid, lo: s + model.schedOffset(ql),
               hi: s + model.schedOffset(qh),
               source: "schedule", backed: backed, vehicle: null});
  }

  for (const t of skippedSlotTimes(slots, skipped, now))
    rows.push({eta: t, lo: t, hi: t, source: "not stopping here",
               backed: false, vehicle: null, skipped: true});

  rows.sort((a, b) => a.eta - b.eta);
  for (const r of rows) {
    if (r.skipped === undefined) r.skipped = false;
    r.catchable = r.lo >= now + walk;
    r.leave_in = r.lo - walk - now;
  }
  return rows;
}

// ------------------------------------------------- state observed across polls

/** When each train first appeared on the Medford/Tufts inbound platform.
 *
 * The vehicle feed refreshes a stopped train's timestamp, so it cannot say when
 * the train berthed -- that has to be observed across polls. Without this the
 * berth tier never fires at all. `seen` is handed in and out so the caller can
 * persist it: a tablet dashboard gets reloaded under memory pressure.
 */
class BerthTracker {
  constructor(seen) { this.seen = seen || {}; }

  update(snap, model) {
    const staleS = model.need("stale_vehicle_s"), medIn = model.stops.med_in;
    const now = snap.t, present = new Set();
    for (const v of snap.vehicles) {
      const a = v.attributes;
      if (!isLive(a, now, staleS) || !isRevenue(a)) continue;
      if (stopOf(v.relationships) === medIn && a.direction_id === 0) {
        present.add(v.id);
        if (!(v.id in this.seen)) this.seen[v.id] = now;
      }
    }
    for (const vid of Object.keys(this.seen))
      if (!present.has(vid)) delete this.seen[vid];
    return Object.assign({}, this.seen);
  }
}

/** When each train first appeared stopped at a platform -- the dwell counter.
 *
 * Deliberately tolerant: a stopped train's position can go stale or drop out of a
 * single poll, and treating that as a departure made the counter reset and jump.
 */
class ArrivalTracker {
  constructor() {
    this.at = {};       // vehicle -> when it arrived
    this.seen = {};     // vehicle -> last poll that saw it here
    this.recent = [];   // arrival times, newest last
  }

  update(snap, model, stop, direction) {
    const staleS = model.need("stale_vehicle_s");
    if (stop === undefined) stop = model.stops.magoun_in;
    if (direction === undefined) direction = 0;
    const now = snap.t;
    for (const v of snap.vehicles) {
      const a = v.attributes;
      const here = a.direction_id === direction && isRevenue(a)
        && stopOf(v.relationships) === stop && a.current_status === "STOPPED_AT";
      if (!here) {
        // Seen somewhere else, so it has definitely left. The grace period below
        // applies only to vehicles missing from the feed entirely.
        delete this.at[v.id];
        delete this.seen[v.id];
        continue;
      }
      if (!(v.id in this.at)) {
        if (!isLive(a, now, staleS)) continue;   // do not start counting a ghost
        this.at[v.id] = now;
        this.recent.push(now);
        this.recent = this.recent.slice(-12);
      }
      this.seen[v.id] = now;
    }
    for (const [vid, last] of Object.entries(this.seen))
      if (now - last > ArrivalTracker.GRACE) {
        delete this.at[vid];
        delete this.seen[vid];
      }
    // Stable order: oldest arrival first, so the display never swaps trains.
    const out = {};
    for (const [vid, t] of Object.entries(this.at).sort((a, b) => a[1] - b[1]))
      out[vid] = t;
    return out;
  }
}
ArrivalTracker.GRACE = 75;   // a vehicle missing from one poll has not left

/** Where every Green Line train sits on the GLX, as a fractional stop index. */
function lineMap(snap, model) {
  const glx = model.need("glx");
  const inIdx = {}, outIdx = {};
  glx.forEach(([, i, o], k) => { inIdx[i] = k; outIdx[o] = k; });
  const out = [];
  for (const v of snap.vehicles) {
    const a = v.attributes, rel = v.relationships || {};
    const stop = stopOf(rel);
    if (stop === null) continue;
    const inbound = a.direction_id === 0;
    const idx = (inbound ? inIdx : outIdx)[stop];
    if (idx === undefined) continue;
    const stopped = a.current_status === "STOPPED_AT";
    // Inbound runs down the list, outbound runs up it; a moving train is drawn
    // half a segment before the stop it is heading for.
    const pos = stopped ? idx : (inbound ? idx - 0.5 : idx + 0.5);
    const cars = (a.carriages || []).map(c => c.label).filter(Boolean);
    out.push({
      id: v.id, dir: inbound ? 0 : 1, pos: Math.round(pos * 100) / 100,
      stopped: stopped, stale: !isLive(a, snap.t, model.need("stale_vehicle_s")),
      revenue: isRevenue(a), label: a.label ?? null,
      car: cars.length ? cars[0] : null,
      route: ((rel.route || {}).data || {}).id ?? null,
    });
  }
  return out;
}

/** Alerts touching the Magoun-to-downtown corridor, worst first.
 *  An alert that touches none of it is someone else's problem: showing it trains
 *  the eye to ignore the banner, which is worse than showing nothing. */
function relevant(alerts, model) {
  const corridor = new Set(model.need("alert_corridor"));
  return alerts
    .filter(a => a.stops.some(s => corridor.has(s))
                 || (!a.stops.length && (a.severity || 0) >= 7))
    .sort((a, b) => (b.severity || 0) - (a.severity || 0));
}

exports.Model = Model;
exports.computeRows = computeRows;
exports.upstreamState = upstreamState;
exports.lineMap = lineMap;
exports.relevant = relevant;
exports.BerthTracker = BerthTracker;
exports.ArrivalTracker = ArrivalTracker;
exports.iso = iso;
exports.isLive = isLive;
exports.isRevenue = isRevenue;
exports.slotsWithTrips = slotsWithTrips;
exports.skippedSlotTimes = skippedSlotTimes;

// ==================================================================== the board
// Everything below is I/O and assembly: it gathers what the pure function needs
// and hands the board the same object shape the Mac server's /status returned,
// so src/status.html renders unchanged. Nothing below is part of the contract.

const API = "https://api-v3.mbta.com";
const ROUTES = "Green-B,Green-C,Green-D,Green-E";
const POLL_MS = 10000;          // the Mac server refreshed on a 10 s cache too
const MODEL_RECHECK_MS = 3600000;
const QS = [0.1, 0.5, 0.9];     // mirrors service.etas' default quantiles
// The default walk is published in model.json so the board and the backend that
// scores it cannot drift apart. Set once the model loads, which always happens
// before walkSeconds() can be reached (it is only called with a row in hand).
let walkDefaultS = null;

const store = {
  get(k, dflt) {
    try { const v = localStorage.getItem(k); return v === null ? dflt : v; }
    catch (e) { return dflt; }
  },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};

/** The rider's walk, in seconds. One definition, read by the board too.
 *  Their own setting wins; the published default is the fallback. */
const walkSeconds = () => {
  const v = store.get("magoun.walk", null);
  if (v !== null) return Number(v);
  if (walkDefaultS === null) throw new Error("model.json has no constant walk_s");
  return walkDefaultS;
};

/** Today's service date in the agency's timezone -- never a fixed offset, so
 *  EDT->EST on 2026-11-01 does not shift the schedule by an hour. */
function serviceDate(now) {
  return new Intl.DateTimeFormat("en-CA", {timeZone: "America/New_York",
    year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date(now * 1000));
}

/** Every fetch here is cross-origin and none of them may hang the tick: the poll
 *  loop awaits them in sequence and paints only afterwards, so one unanswered
 *  socket stops the board rather than degrading it. A refused connection fails
 *  instantly; a DROPPED one -- an asleep Mac, a phone off the tailnet -- does not,
 *  and waits out the browser's connect timeout instead. */
async function getJSON(url, timeoutMs) {
  const r = await fetch(url, {cache: "no-store",
                              signal: AbortSignal.timeout(timeoutMs || 8000)});
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

const qs = params => Object.entries(params)
  .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");

/** An api-v3 URL, with this device's key if it has one.
 *
 *  The anonymous limit is 20 requests/minute PER CLIENT IP, and one board is
 *  ~12.5 of them. A home network puts every device behind one public IP, and
 *  mobile carriers put thousands behind one, so "my phone is elsewhere" is not
 *  the same as "my phone has the budget to itself". A key makes the limit
 *  per-key and the contention goes away.
 *
 *  Per device in localStorage, never in the repo: this page is served from a
 *  public origin and model.json with it, so a key published as a constant would
 *  be a key given away. Same reasoning as the ntfy topics.
 *
 *  A query parameter rather than the x-api-key header service.py uses: a custom
 *  header makes this a non-simple request, and the CORS preflight would DOUBLE
 *  the request count -- the opposite of the point. */
const apiKey = () => { try { return store.get("magoun.mbtakey") || ""; }
                       catch (e) { return ""; } };
const apiURL = (path, params) => `${API}/${path}?`
  + qs(apiKey() ? Object.assign({}, params, {api_key: apiKey()}) : params);

/** The same two calls service.snapshot() makes. Both endpoints send
 *  access-control-allow-origin: *, verified 2026-09-25, and need no key. */
async function snapshot(model) {
  const s = model.stops;
  const [preds, veh] = await Promise.all([
    getJSON(apiURL("predictions", {
      "filter[stop]": [s.magoun_in, s.ball_in, s.med_in, s.med_out].join(","),
      "sort": "arrival_time"})),
    getJSON(apiURL("vehicles", {"filter[route]": ROUTES})),
  ]);
  return {t: Date.now() / 1000, preds: preds.data, vehicles: veh.data};
}

/** (scheduled arrival, trip id) for today, so a slot can be matched to a skip.
 *  Cached per service date: the timetable does not change during the day, and a
 *  tablet left open should not refetch 500 rows every poll. */
async function scheduleSlots(model, day) {
  const key = `magoun.sched.${day}`;
  const cached = store.get(key, null);
  if (cached) {
    try { return JSON.parse(cached); } catch (e) { /* refetch */ }
  }
  const body = await getJSON(apiURL("schedules", {
    "filter[stop]": model.stops.magoun_in, "filter[date]": day,
    "filter[direction_id]": "0", "page[limit]": "500"}));
  const slots = body.data
    .map(s => [iso(s.attributes.arrival_time || s.attributes.departure_time),
               s.relationships.trip.data.id])
    .filter(([t]) => t)
    .sort((a, b) => a[0] - b[0]);
  store.set(key, JSON.stringify(slots));
  return slots;
}

/** Trips MBTA has declared will not stop here. Only the protobuf feed carries
 *  these and cdn.mbta.com has no CORS, so the backend parses it and serves the
 *  answer at <backend_url>/skips.
 *
 *  Nothing here may block the board. The endpoint lives on a tailnet host, so
 *  when the Mac is asleep or the phone is off the tailnet the connection is
 *  dropped rather than refused -- a fetch with no timeout then hangs for tens of
 *  seconds, every tick, and the board stops painting altogether. It used to be a
 *  same-origin sibling file that 404'd instantly, which is why this was safe
 *  before and is not now. So: a short timeout, and a backoff that stops a dead
 *  host costing a stall on every poll.
 *
 *  The last good set is kept and reused until its own ttl runs out, so a single
 *  missed poll does not blink the strikethrough off a train that is still
 *  skipped. Four ways this yields nothing, all the same to the rider and all of
 *  them how the board behaved before skips existed: no endpoint published, the
 *  endpoint unreachable, the backoff still running with nothing cached, or a set
 *  older than the ttl the backend quoted.
 *
 *  Read off constants directly rather than through need(): a board with no
 *  endpoint configured must lose the strikethrough, not the whole page. */
const SKIP_TIMEOUT_MS = 2500;   // a quarter of a poll; it is one small GET
const SKIP_BACKOFF_S = 60;      // after a failure, stop asking for a while
let skipCache = {trips: new Set(), asOf: 0, ttl: 60, nextTry: 0};

async function skippedTrips(now, model) {
  const base = (model.m.constants || {}).backend_url;
  if (!base) return new Set();
  const url = `${base}/skips`;
  if (now >= skipCache.nextTry) {
    try {
      const d = await getJSON(url, SKIP_TIMEOUT_MS);
      skipCache = {trips: new Set(d.trips || []), asOf: d.as_of || 0,
                   ttl: d.ttl_s || 60, nextTry: 0};
    } catch (e) {
      skipCache.nextTry = now + SKIP_BACKOFF_S;
    }
  }
  if (!skipCache.asOf || now - skipCache.asOf > skipCache.ttl) return new Set();
  return skipCache.trips;
}

/** When the archive was last appended to, and whether that is alarming.
 *
 *  Only the backend knows: an un-captured day is gone for good, because the v3
 *  /schedules endpoint serves about eight days back and nothing else keeps them.
 *  launch-plan.md calls a silently dead archiver the top failure mode, and until
 *  now nothing anywhere would have said so.
 *
 *  Three states, and the third has to be distinguishable from the second or this
 *  is worse than useless: fresh, stale (the archiver is not writing -- data is
 *  being lost right now), and unknown (the backend is unreachable, which is the
 *  normal state of a phone off the tailnet and says nothing about the archiver).
 *  Same timeout and backoff as the skip set, for the same reason. */
const capture = {asOf: 0, staleAfter: 600, seen: false, nextTry: 0, badReads: 0};

async function captureAge(now, model) {
  const base = (model.m.constants || {}).backend_url;
  if (!base) return null;
  if (now >= capture.nextTry) {
    try {
      const d = await getJSON(`${base}/capture`, SKIP_TIMEOUT_MS);
      capture.asOf = d.as_of || 0;
      capture.staleAfter = d.stale_after_s || 600;
      capture.seen = true;
      capture.nextTry = 0;
    } catch (e) {
      capture.seen = false;
      capture.nextTry = now + SKIP_BACKOFF_S;
    }
  }
  if (!capture.seen) { capture.badReads = 0; return null; }   // unknown, not dead

  // as_of 0 means the backend found no archive file at all -- which is a real
  // alarm, but `now - 0` is the whole Unix epoch and would print as a six-figure
  // counter. Reachable exactly when this feature matters: the archiver dies at
  // 23:00, daily.sh compacts and unlinks yesterday's file at 03:00, and there is
  // no today file because nothing is writing one.
  const never = capture.asOf <= 0;
  const age = never ? null : now - capture.asOf;
  const bad = never || age > capture.staleAfter;

  // Two consecutive bad reads before crying wolf. server.py answers the moment
  // the Mac is up, while record_rt may not have appended yet, so a wake from
  // overnight sleep would otherwise paint one frame of "silent 8:00:00" -- and a
  // line that is wrong once is a line the rider stops reading.
  capture.badReads = bad ? capture.badReads + 1 : 0;
  return {age: age, never: never, stale: bad && capture.badReads >= 2};
}

async function fetchAlerts() {
  const body = await getJSON(apiURL("alerts", {"filter[route]": ROUTES}));
  return body.data.map(a => ({
    id: a.id, effect: a.attributes.effect, severity: a.attributes.severity,
    lifecycle: a.attributes.lifecycle, header: a.attributes.header,
    short: a.attributes.service_effect,
    stops: [...new Set((a.attributes.informed_entity || [])
      .map(e => e.stop).filter(Boolean))].sort(),
  }));
}

const nearestScheduled = (t, slots) => slots.length
  ? slots.reduce((best, s) => Math.abs(s - t) < Math.abs(best - t) ? s : best)
  : null;

/** Build exactly what /status used to return, so status.html is untouched. */
function buildStatus(now, snap, model, rows, berths, here, recent, line,
                     alerts, slots) {
  const magounPos = model.need("glx").findIndex(g => g[1] === model.stops.magoun_in);
  // A train is only "at the station" if the same snapshot also places it stopped
  // at Magoun; otherwise the hero and the map can disagree, which is worse than
  // either being briefly wrong on its own.
  const atMagoun = new Set(line.filter(t => t.dir === 0 && t.stopped
                                       && t.pos === magounPos).map(t => t.id));
  const slotTimes = slots.map(([t]) => t);
  const at_station = [];
  for (const [vid, since] of Object.entries(here)) {
    if (!atMagoun.has(vid)) continue;
    const sched = nearestScheduled(since, slotTimes);
    at_station.push({vehicle: vid, since: since, dwell_s: now - since,
                     late_s: sched === null ? null : since - sched});
  }
  const nxt = rows.find(r => r.eta > now) || null;
  return {
    now: now, version: model.version, at_station: at_station,
    // `vehicle` is carried so the bell can name the train when it hands the alert
    // to the notifier. A vehicle id is the only thing that tells a prediction flap
    // from a no-show -- the timing of the two is identical -- so an arm without one
    // costs the notifier its recovery.
    next: nxt && {eta: nxt.eta, lo: nxt.lo, hi: nxt.hi, source: nxt.source,
                  backed: nxt.backed, vehicle: nxt.vehicle},
    following: rows.slice(1, 5).map(r => ({eta: r.eta, source: r.source,
                                           skipped: r.skipped})),
    upstream: upstreamState({t: now, vehicles: snap.vehicles}, model),
    line: line,
    stops: model.need("glx").map(g => g[0]),
    recent: recent,
    alerts: relevant(alerts, model).filter(a => (a.severity || 0) >= 5).slice(0, 3),
    headway_median_s: model.headway,
    berthed: Object.keys(berths).length,
  };
}

/** Poll MBTA, compute, hand the board a /status-shaped object. Returns a stop().
 *
 *  `onData` is called on every successful poll; failures are swallowed so the
 *  board keeps painting from the last good reading, exactly as it did against the
 *  Mac server. The berth tracker's state is persisted because a reload that loses
 *  it silently drops the berth tier until the next train berths.
 */
function start(onData, onError) {
  let model = null, alerts = [], stopped = false, lastError = null;
  const berthTracker = new BerthTracker(JSON.parse(store.get("magoun.berths", "{}")));
  const arrivals = new ArrivalTracker();
  let modelText = null, lastAlerts = 0, lastModelCheck = 0;
  let fromScript = false;   // model came from model.js, not model.json

  /** The model, as text, so a republished one can be spotted by comparison.
   *
   *  Measured 2026-09-26, Chromium: a board opened as file:// cannot fetch a
   *  sibling file at all -- "URL scheme file is not supported", before any CORS
   *  question. A script tag is allowed, so the same bytes are also published as
   *  model.js and that is the fallback. fit.py writes both. */
  async function readModelText() {
    try {
      return await (await fetch("model.json", {cache: "no-store"})).text();
    } catch (e) {
      await new Promise((ok, no) => {
        const el = document.createElement("script");
        el.src = "model.js";
        el.onload = ok;
        el.onerror = () => no(e);
        document.head.appendChild(el);
      });
      fromScript = true;
      const t = (globalThis.Magoun || {})._modelText;
      if (!t) throw e;
      return t;
    }
  }

  async function loadModel() {
    const text = await readModelText();
    if (modelText !== null && text !== modelText) return location.reload();
    modelText = text;
    model = new Model(JSON.parse(text));
    walkDefaultS = model.need("walk_s");   // throws on a deploy that dropped it
    // status.html reloads the page when `version` changes; tie it to the model so
    // a refit published under a tablet reaches the rider without a manual reload.
    model.version = `m${text.length}:${model.m.days}:${model.m.n_legs}`;
    return model;
  }

  async function tick() {
    if (stopped) return;
    try {
      const now = Date.now() / 1000;
      // Re-read the model occasionally so a refit reaches a tablet left running.
      // Not worth it on the file:// path: re-injecting the script cannot see a
      // newer file, and the page has to be reloaded there anyway.
      if (!model || (!fromScript && now - lastModelCheck > MODEL_RECHECK_MS / 1000)) {
        lastModelCheck = now;
        await loadModel();
      }
      const snap = await snapshot(model);
      const berths = berthTracker.update(snap, model);
      store.set("magoun.berths", JSON.stringify(berths));
      const here = arrivals.update(snap, model);
      const day = serviceDate(snap.t);
      const slots = await scheduleSlots(model, day);
      // Together, not in sequence: both are the same unreachable host off the
      // tailnet, and awaiting them one after the other doubles the stall this
      // whole timeout exists to bound.
      const [skipped, cap] = await Promise.all([
        skippedTrips(snap.t, model), captureAge(snap.t, model)]);
      if (snap.t - lastAlerts > 120) {
        lastAlerts = snap.t;
        alerts = await fetchAlerts().catch(() => alerts);
      }
      // walk 0: the board draws its own leave time from `lo`, as /status did.
      const rows = computeRows(snap.t, snap.preds, snap.vehicles, model, 0, QS,
                               model.need("horizon_s"), berths, slots, skipped);
      onData(Object.assign(
        buildStatus(snap.t, snap, model, rows, berths, here,
                    arrivals.recent.slice(-5), lineMap(snap, model), alerts, slots),
        {capture: cap}));
      lastError = null;
    } catch (e) {
      // 429 is not "the feed is down", it is "you are asking too often", and the
      // rider can do something about it. Without this the board simply stops
      // updating and goes stale with no clue why -- which is what it did.
      lastError = /\b429\b/.test(String(e && e.message)) ? "ratelimit" : "error";
      if (onError) onError(e);
    }
  }

  tick();
  const timer = setInterval(tick, POLL_MS);
  return {
    stop() { stopped = true; clearInterval(timer); },
    refresh: tick,
    get feedError() { return lastError; },
  };
}

exports.start = start;
exports.walkSeconds = walkSeconds;
exports.serviceDate = serviceDate;
exports.store = store;
exports.POLL_MS = POLL_MS;

})(typeof module === "object" && module.exports
   ? module.exports
   : (globalThis.Magoun = {}));
