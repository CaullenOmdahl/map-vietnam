"use strict";
/* Map Vietnam viewer: dated historical maps of Vietnam laid over today's map, with war-era records on top.

   Served by scripts/server.py (run `.venv/bin/python scripts/server.py`, then open http://127.0.0.1:8765/).
   Plain browser script, no build step; MapLibre GL is loaded from a CDN by index.html.

   Layout (Apple Maps style): the map runs edge to edge. A floating sidebar (laptop, >= 800 px wide) or a
   bottom sheet with three detents, peek / half / full (phone), holds search, the home screen and one card at
   a time. Map controls float top right; a bar at the bottom picks the historical map, sets its opacity and
   turns on swipe-compare (a second map clipped to the right of a draggable divider).

   Data flow:
     /catalog.json            -> S.catalog (the old maps installed on this computer) -> S.eras (one choosable map per
                                 sheet); reloaded whenever a map is added or removed, without a page reload
     /api/library             -> S.lib (every map that can be added, with its state); polled only while maps are on
                                 their way. /api/library/add and /remove queue or delete maps: nothing downloads
                                 unless the person taps Add
     /api/library/custom…     -> your own maps: POST uploads a file, /points places a scan, /edit renames; they are
                                 library entries (series "custom"). /api/settings -> where data is kept, space used
     /api/records             -> S.rec (every record source that can be added, with its state); polled only while
                                 one is on its way. /api/records/add, /remove and /cancel queue, delete or stop
                                 sources; /api/records/key saves a Kaggle key if Kaggle asks for one; /api/quit
                                 stops the local server
     /api/evidence?bbox=…     -> fetched per grid cell (CELL / PCELL degrees) as the view moves, cached in
                                 evById / pnById, pushed to the "ev" / "pn" GeoJSON sources of every map
     /api/search, /api/gridref, /api/lonlat -> the search box, grid-square card and coordinate rows
     /api/cases…              -> S.cases / S.kase (the open case: pinned records, candidate sites, notes, exports)
     localStorage ("mvapp.*") -> view position, chosen map, opacity, filters, theme, scroll mode, recently viewed

   State lives in one object, S. Each change goes through a small apply* / render* function that pushes it
   to the maps (layout, paint, filter, setData) or redraws the panel: render() rebuilds the current card
   from VIEWS[S.view.name] and wire() connects its [data-act] buttons to ACTIONS.

   Sections, in order: constants, helpers, state, maps, trackpad, historical maps, compare, evidence loading, filters,
   selection and position, map clicks, panel / sheet, panel views (home, record, nearby, pin, grid square,
   historical map, cases, sites), search, popovers, base map and theme, keyboard, boot. The map library (adding
   and removing old maps) follows the historical-map card, and the records section (adding and removing record
   sources, the first-run welcome and Quit) follows the library. */

// ================================================================ constants
// Today's base maps (raster XYZ tiles, Web Mercator).
const BASES = {
  osm: { name: "Street map", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors', maxzoom: 19 },
  sat: { name: "Satellite", tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"], attribution: "Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community", maxzoom: 19 },
};
// Each evidence kind gets its own colour and shape (shape paths are in a 24×24 box).
const SHAPES = {
  circle: "M12 4a8 8 0 1 0 .001 0Z",
  diamond: "M12 2.5L21.5 12 12 21.5 2.5 12Z",
  square: "M4.5 4.5h15v15h-15Z",
  triangle: "M12 3L21.5 19.5h-19Z",
  star: "M12 2.5l2.8 6.1 6.7.7-5 4.5 1.4 6.6L12 17l-5.9 3.4 1.4-6.6-5-4.5 6.7-.7Z",
  hexagon: "M12 2.5l8.5 4.8v9.4L12 21.5l-8.5-4.8V7.3Z",
};
const KINDS = {
  air_strike:     { label: "Air strikes",      one: "Air strike",              color: "#ea580c", shape: "circle" },
  loss_incident:  { label: "Loss incidents",   one: "Loss incident",           color: "#dc2626", shape: "diamond" },
  field_burial:   { label: "Field burials",    one: "Reported field burial",   color: "#a21caf", shape: "square" },
  aircraft_crash: { label: "Aircraft crashes", one: "Aircraft crash",          color: "#0891b2", shape: "triangle" },
  ground_combat:  { label: "Battles",          one: "Battle / ground combat",  color: "#ca8a04", shape: "star" },
  military_site:  { label: "Military sites",   one: "Military site",           color: "#2563eb", shape: "hexagon" },
  other:          { label: "Other records",    one: "Other record",            color: "#64748b", shape: "circle" },
  placename:      { label: "Place names",      one: "Place name",              color: "#57534e", shape: "circle" },
};
// Evidence kinds proper; place names are a separate, denser layer loaded only when zoomed in.
const EV_KINDS = Object.keys(KINDS).filter(k => k !== "placename");
const CENTER = [105.63, 10.46]; // first-visit view centre [lon, lat] (Cao Lãnh, Đồng Tháp)
const PLACENAME_ZOOM = 12; // place names are drawn, counted and fetched from this zoom
const MAX_FLY_ZOOM = 15; // never zoom past this when flying to a selection
const CELL = 0.25, PCELL = 0.05; // evidence / place names are fetched in square grid cells of this size (degrees)
// ISO 3166-2 province codes as some sources give them, to the province name.
const PROV = {"VN-01":"Lai Châu","VN-02":"Lào Cai","VN-03":"Hà Giang","VN-04":"Cao Bằng","VN-05":"Sơn La","VN-06":"Yên Bái","VN-07":"Tuyên Quang","VN-09":"Lạng Sơn","VN-13":"Quảng Ninh","VN-14":"Hòa Bình","VN-18":"Ninh Bình","VN-20":"Thái Bình","VN-21":"Thanh Hóa","VN-22":"Nghệ An","VN-23":"Hà Tĩnh","VN-24":"Quảng Bình","VN-25":"Quảng Trị","VN-26":"Thừa Thiên Huế","VN-27":"Quảng Nam","VN-28":"Kon Tum","VN-29":"Quảng Ngãi","VN-30":"Gia Lai","VN-31":"Bình Định","VN-32":"Phú Yên","VN-33":"Đắk Lắk","VN-34":"Khánh Hòa","VN-35":"Lâm Đồng","VN-36":"Ninh Thuận","VN-37":"Tây Ninh","VN-39":"Đồng Nai","VN-40":"Bình Thuận","VN-41":"Long An","VN-43":"Bà Rịa–Vũng Tàu","VN-44":"An Giang","VN-45":"Đồng Tháp","VN-46":"Tiền Giang","VN-47":"Kiên Giang","VN-49":"Vĩnh Long","VN-50":"Bến Tre","VN-51":"Trà Vinh","VN-52":"Sóc Trăng","VN-53":"Bắc Kạn","VN-54":"Bắc Giang","VN-55":"Bạc Liêu","VN-56":"Bắc Ninh","VN-57":"Bình Dương","VN-58":"Bình Phước","VN-59":"Cà Mau","VN-61":"Hải Dương","VN-63":"Hà Nam","VN-66":"Hưng Yên","VN-67":"Nam Định","VN-68":"Phú Thọ","VN-69":"Thái Nguyên","VN-70":"Vĩnh Phúc","VN-71":"Điện Biên","VN-72":"Đắk Nông","VN-73":"Hậu Giang","VN-CT":"Cần Thơ","VN-DN":"Đà Nẵng","VN-HN":"Hà Nội","VN-HP":"Hải Phòng","VN-SG":"Hồ Chí Minh"};
// Short names for sources in list rows; other sources use their full name from /api/sources.
const SRC_SHORT = { gns: "NGA gazetteer", hes_gazetteer: "HES hamlet gazetteer", osm: "OpenStreetMap", thor: "THOR air operations", wikidata: "Wikidata" };
// [from, to, label] year-filter chips; [null, null] clears the filter.
const YEAR_PRESETS = [[null, null, "All years"], [1945, 1954, "1945–54"], [1955, 1975, "1955–75"], [1965, 1973, "1965–73"], [1968, 1968, "1968"]];
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const coarse = matchMedia("(pointer: coarse)").matches;

// ================================================================ helpers
const $ = s => document.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
// Only web links become clickable: a record's url comes from outside sources, and "javascript:" must never be one.
const safeUrl = u => typeof u === "string" && /^https?:\/\//i.test(u);
const fmtInt = n => Number(n || 0).toLocaleString("en-US");
// JSON in localStorage under "mvapp.<key>"; falls back to the default when storage is blocked or the value is bad.
const store = {
  get(k, d) { try { const v = localStorage.getItem("mvapp." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("mvapp." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
// Calls the local server; opts.body is sent as JSON. Throws with the server's {error} message on a non-2xx reply.
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const ct = r.headers.get("Content-Type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw Object.assign(new Error((data && data.error) || r.statusText), { data });
  return data;
}
// A short status message; with an undo callback it shows an Undo button (or another label) and stays up for at least 6 s.
let toastTimer;
// Says something to screen readers only (a hidden polite region), for changes that have no toast. Progress is never
// announced: it changes every few seconds and the lists patch it in place.
function announce(msg) {
  const r = $("#srLive");
  r.textContent = "";
  setTimeout(() => { r.textContent = msg; }, 50);
}
function toast(msg, ms = 3200, undo = null, label = "Undo") {
  const t = $("#toast"), u = $("#toastUndo");
  $("#toastText").textContent = msg; t.classList.add("on");
  u.hidden = !undo; u.textContent = label; u.onclick = undo ? async () => { t.classList.remove("on"); await undo(); } : null;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("on"), undo ? Math.max(ms, 6000) : ms);
}
// Dates arrive as ISO strings of any precision ("1968", "1968-05", "1968-05-12").
const yearOf = d => d ? +String(d).slice(0, 4) : null;
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtDay(d) {
  if (!d) return "";
  const m = String(d).match(/^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?/);
  if (!m) return String(d);
  return [m[3] ? +m[3] : null, m[2] ? MON[+m[2] - 1] : null, m[1]].filter(Boolean).join(" ");
}
const fmtDate = (a, b) => !a ? "Undated" : (b && b !== a ? `${fmtDay(a)} – ${fmtDay(b)}` : fmtDay(a));
// Distances: fmtM takes metres, fmtKm kilometres.
const fmtM = m => m >= 1000 ? `${(m / 1000).toFixed(m >= 10000 ? 0 : 1)} km` : `${Math.round(m)} m`;
const fmtKm = km => km == null ? "" : km < 1 ? `${Math.round(km * 1000)} m` : `${km < 10 ? km.toFixed(1) : Math.round(km)} km`;
// File sizes in decimal units, as file browsers show them: "8.1 MB", "96 MB", "1.2 GB".
const fmtMB = b => !b ? "" : b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e7 ? `${Math.round(b / 1e6)} MB` : `${(b / 1e6).toFixed(1)} MB`;
// "1:200000" -> "1:200,000" (catalogues write scales both ways).
const fullScale = s => (s || "").replace(/\s/g, "").replace(/^1:([\d,]+)$/, (_, n) => "1:" + fmtInt(+n.replace(/,/g, "")));
// [west, south, east, north] boxes that share some area.
const boxesOverlap = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
// "1:50,000" -> "1:50k".
const shortScale = s => (s || "").replace(/\s/g, "").replace(/^1:([\d,]+)$/, (_, n) => { const v = +n.replace(/,/g, ""); return "1:" + (v >= 1000 ? (v / 1000) + "k" : v); });
// MapLibre's world is 512 px wide at zoom 0, so one pixel at z0 covers 40075016.686/512 m at the equator,
// shrinking with cos(latitude). Each feature carries r0 = its radius in z0 pixels at its own latitude,
// and the ring radius doubles per zoom level: rings are drawn at true ground scale.
const M_PER_PX_Z0 = 40075016.686 / 512;
const r0Of = (m, lat) => (m || 0) / (M_PER_PX_Z0 * Math.cos(lat * Math.PI / 180));
const ringPx = () => ["interpolate", ["exponential", 2], ["zoom"], 0, ["get", "r0"], 24, ["*", ["get", "r0"], 2 ** 24]];
// A ring wider than RING_MAX_PX across says nothing at that zoom (a record placed only "somewhere in this province"
// would wash the whole screen in its colour): it fades out, and the record's card states its uncertainty. `op` is
// the ring's opacity while it is small enough to read.
const RING_MAX_PX = 300;
const ringFade = op => ["interpolate", ["linear"], ["zoom"],
  ...Array.from({ length: 23 }, (_, z) => [z, ["case", [">", ["*", ["get", "r0"], 2 ** z], RING_MAX_PX / 2], 0, op]]).flat()];
// Circle helpers on a local spherical approximation (111,320 m per degree of latitude): plenty for rings of a
// few km. Inputs are WGS84 degrees and metres.
function circleBounds(lon, lat, m) {
  const dLat = m / 111320, dLon = m / (111320 * Math.cos(lat * Math.PI / 180));
  return [[lon - dLon, lat - dLat], [lon + dLon, lat + dLat]];
}
function circlePolygon(lon, lat, m, n = 64) {
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const a = 2 * Math.PI * i / n;
    pts.push([lon + m * Math.sin(a) / (111320 * Math.cos(lat * Math.PI / 180)), lat + m * Math.cos(a) / 111320]);
  }
  return { type: "Feature", geometry: { type: "Polygon", coordinates: [pts] }, properties: {} };
}
// Great-circle distance (m, haversine on the mean Earth radius) and initial true bearing (degrees) between
// two [lon, lat] points.
function distBearing(from, to) {
  const R = 6371008.8, rad = Math.PI / 180;
  const [l1, p1, l2, p2] = [from[0] * rad, from[1] * rad, to[0] * rad, to[1] * rad];
  const dp = p2 - p1, dl = l2 - l1;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  const d = 2 * R * Math.asin(Math.sqrt(a));
  const y = Math.sin(dl) * Math.cos(p2), x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return { d, b: (Math.atan2(y, x) / rad + 360) % 360 };
}
const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
const compass = b => COMPASS[Math.round(b / 22.5) % 16];
const empty = () => ({ type: "FeatureCollection", features: [] });
const pointFC = (lon, lat, props = {}) => ({ type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: props }] });

// ---- text clean-up: decode entities, make ALL-CAPS dumps readable
function decodeEntities(s) {
  const named = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
  return String(s ?? "").replace(/&(#\d+|#x[0-9a-f]+|amp|lt|gt|quot|apos|nbsp);/gi, (m, e) => {
    if (e[0] === "#") { const n = e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : +e.slice(1); return n > 0 && n < 0x110000 ? String.fromCodePoint(n) : m; }
    return named[e.toLowerCase()] ?? m;
  });
}
const ACRONYMS = new Set("USA US USAF USN USMC VNAF RVNAF RAAF RLAF RVN ARVN NVA PAVN NLF VC HES GVN USID GZSF HGAZ HVGZ THOR GNS MGRS DMS WGS UTM KIA MIA BNR POW CBU GP HE WP LZ FSB FOB AMS NGA OSM CAS SAR UH AH CH OV IED LB LBS MM UXO DMZ II III IV VI VII VIII IX XI XII".split(" "));
const SMALL = new Set("OF IN ON TO AT BY OR AND THE FOR WITH".split(" "));
const titleWord = w => w[0] + w.slice(1).toLowerCase();
// Title-case shouted text: runs of two or more ALL-CAPS words, or single ALL-CAPS words of 5+ letters.
// Acronyms, short codes (PPL, MK-82, AC-47) and tokens glued to digits (SPOOKY52, 500LB) are left alone.
const CAPS_RUN = /\b[A-Z][A-Z'’]*(?:-[A-Z][A-Z'’]*)*\b(?:[\s(),\/&.:-]+\b[A-Z][A-Z'’]*(?:-[A-Z][A-Z'’]*)*\b)*/g;
function readable(s) {
  // THOR glues call signs to their number with "&" (SPOOKY&81 = call sign Spooky 81).
  s = decodeEntities(s).replace(/\\/g, " / ").replace(/([A-Za-z])&(\d)/g, "$1 $2").replace(/\s{2,}/g, " ");
  return s.replace(CAPS_RUN, (run, off, full) => {
    const isRun = (run.match(/[A-Z][A-Z'’]*/g) || []).length >= 2;
    let first = true;
    return run.replace(/[A-Z][A-Z'’]*/g, (w, o) => {
      const atStart = first; first = false;
      if (w.length < 2 || ACRONYMS.has(w)) return w;
      if (/^-\d/.test(full.slice(off + o + w.length, off + o + w.length + 2))) return w; // model codes like MK-82
      if (!isRun && w.length < 5) return w;
      if (SMALL.has(w) && !atStart) return w.toLowerCase();
      return titleWord(w);
    });
  });
}
const titleCase = s => readable(String(s || "").toUpperCase());
// HES hamlet titles end in "(HES hamlet, <district>, <province>)".
function hesTail(title) { const m = String(title || "").match(/\s*\(HES hamlet,\s*([^,]+),\s*([^)]+)\)\s*$/); return m ? { district: m[1], province: m[2] } : null; }
// Without the "Air strike:" prefix, which the symbol and subtitle already say.
const shortTitle = t => cleanTitle(t).replace(/^Air strike:\s*/i, "");
// A readable title without the HES hamlet suffix (which contextOf shows instead).
function cleanTitle(title) { return readable(String(title || "Untitled").replace(/\s*\(HES hamlet[^)]*\)\s*$/, "")); }
// Where a record is, in words: the server's context, a province code or wartime district in the description, or
// the HES hamlet suffix.
function contextOf(p) {
  if (p.context) return p.context;
  const d = p.description || "";
  let m = d.match(/province code (VN-[A-Z0-9]+)/);
  if (m && PROV[m[1]]) return PROV[m[1]];
  m = d.match(/GVN: province (.+?) \/ district (.+?) \//);
  if (m) return `${titleCase(m[2])}, ${titleCase(m[1])} (wartime)`;
  const h = hesTail(p.title);
  if (h) return `${titleCase(h.district)}, ${titleCase(h.province)} (wartime)`;
  return "";
}
function kindOne(p) {
  if (p.kind === "placename") return p.source === "hes_gazetteer" || hesTail(p.title) ? "Wartime hamlet (HES)" : "Place name";
  return (KINDS[p.kind] || KINDS.other).one;
}
// The source's datum tag (e.g. "indian1960 (assumed)") in plain words.
function datumWords(d) {
  if (!d) return "Datum not recorded";
  const s = d.toLowerCase();
  const base = s.startsWith("indian1960") ? "Indian 1960 (wartime map datum)"
    : s.startsWith("wgs84") ? "WGS84 / GPS"
    : s === "article-coordinate" ? "Approximate coordinate from an encyclopedia article (WGS84)" : d;
  const notes = [];
  if (s.includes("assumed")) notes.push("assumed, the source does not say");
  if (s.includes("mgrs re-derived")) notes.push("re-derived from the original grid reference");
  if (s.includes("thor-converted")) notes.push("converted by the THOR dataset");
  return notes.length ? `${base}; ${notes.join("; ")}` : base;
}
const srcShort = id => SRC_SHORT[id] || S.sources[id]?.name || id || "";
// The list/legend symbol for a kind, matching its map symbol (strikes are plain dots, place names "Aa").
const symSVG = (kind, size = 18) => {
  const k = KINDS[kind] || KINDS.other;
  if (kind === "air_strike") return `<span class="sym" aria-hidden="true"><svg viewBox="0 0 24 24" style="width:${size}px;height:${size}px"><circle cx="12" cy="12" r="6" fill="${k.color}"/></svg></span>`;
  if (kind === "placename") return `<span class="sym label" aria-hidden="true">Aa</span>`;
  return `<span class="sym" aria-hidden="true"><svg viewBox="0 0 24 24" style="width:${size}px;height:${size}px"><path d="${SHAPES[k.shape]}" fill="${k.color}"/></svg></span>`;
};
// Inline line icons (24x24, stroked with currentColor by app.css).
const icon = {
  back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg>',
  search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>',
  close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  pin: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-7-6.2-7-11.5A7 7 0 0119 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg>',
  grid: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16"/><path d="M4 12h16M12 4v16"/></svg>',
  plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
  check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  arrow: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l6 16-6-4-6 4 6-16z" fill="currentColor"/></svg>',
  warn: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l10 18H2L12 3z"/><path d="M12 10v5M12 18v.5"/></svg>',
  ext: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/></svg>',
  dl: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
  copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 00-1-1H5a1 1 0 00-1 1v10a1 1 0 001 1h3"/></svg>',
  near: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="2.5"/><circle cx="12" cy="12" r="8" stroke-dasharray="3 3"/></svg>',
  doc: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/></svg>',
  folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>',
  trash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
  note: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>',
  map: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4L3 6.5v13L9 17l6 2.5 6-2.5v-13L15 6.5 9 4z"/><path d="M9 4v13M15 6.5v13"/></svg>',
  gear: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.4" stroke-width="2.6" stroke-dasharray="2.6 3.28"/><circle cx="12" cy="12" r="6.2"/><circle cx="12" cy="12" r="2.4"/></svg>',
  chevR: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>',
};

// ================================================================ state
// An old map is only useful laid over today's map, so it starts see-through. A slider left near 100% last time
// would open on an opaque sheet that hides the street map, so a new visit never starts above 75%.
const startOpacity = () => Math.min(store.get("opacity", 0.6), 0.75);
// The whole app state. Map ids: eraA is the old map on the main (left) map, null for none; eraB is the right-hand
// map when comparing, "today" for none. years is [from, to] or null for all; hidden is the set of kinds turned off.
// target is the selection drawn on the map ({lon, lat, m: radius in metres, spot}); me is the device position.
// kase is the open case (the name avoids the reserved word); view is the card shown, history the Back stack.
const S = {
  catalog: [], eras: [], eraA: null, eraB: "today", compare: false, opacity: startOpacity(),
  base: store.get("base", "osm"), years: null, hidden: new Set(store.get("hidden", [])), heat: store.get("heat", 0.5),
  ev: empty(), pn: empty(), sources: {}, target: null, me: null, watchId: null,
  cases: [], caseId: store.get("caseId", null), kase: null,
  view: { name: "home" }, history: [], placing: false, draft: null,
  nearR: store.get("nearR", 1000), nearPN: false, sheet: "half", panelHidden: false,
  // The map library (see "map library" below): every map that can be added, by id, with its state.
  lib: { maps: [], byId: new Map(), series: {}, queueBytes: 0, legacy: null, loaded: false, err: null }, catalogLoaded: false,
  // The record sources (see "records" below): every source that can be added, by id, with its state.
  rec: { sources: [], byId: new Map(), key: {}, busy: false, firstRun: false, revision: null, loaded: false, err: null },
};
window.S = S; // console handle for debugging (with window.map, set in boot)
// Resolves once the old-map catalogue is in S.catalog (set in boot); overlays are added only after it.
let catalogReady = Promise.resolve();

// ================================================================ maps
// maps[0] is the main map; maps[1] exists only while comparing. Both get identical sources and layers.
const maps = [];
const primary = () => maps[0];
const eachMap = fn => maps.forEach(m => m && m.loadedOverlays && fn(m));
const lastData = {}; // so a comparison map created later starts with the same overlays
const setData = (src, data) => { lastData[src] = data; eachMap(m => m.getSource(src)?.setData(data)); };
const isDesk = () => innerWidth >= 800;
const isDark = () => { const t = document.documentElement.dataset.theme; return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; };

// A raster-only style: every base map is a layer and only the chosen one is visible, so switching is instant.
function makeStyle() {
  return {
    version: 8,
    glyphs: "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf",
    sources: Object.fromEntries(Object.entries(BASES).map(([k, b]) => [k, { type: "raster", tileSize: 256, tiles: b.tiles, attribution: b.attribution, maxzoom: b.maxzoom }])),
    layers: Object.keys(BASES).map(k => ({ id: `base-${k}`, type: "raster", source: k, layout: { visibility: k === S.base ? "visible" : "none" } })),
  };
}
// Creates a map in the given container. The primary map restores the saved view and carries the attribution and
// scale; the comparison map starts from the primary's view.
// A saved view from localStorage, used only if it is still a valid [lon, lat] and zoom.
function savedView() {
  const c = store.get("center", CENTER), z = store.get("zoom", 11);
  const ok = Array.isArray(c) && c.length === 2 && c.every(Number.isFinite) && Math.abs(c[0]) <= 180 && Math.abs(c[1]) <= 90;
  return { center: ok ? c : CENTER, zoom: Number.isFinite(z) && z >= 0 && z <= 22 ? z : 11 };
}
function createMap(container, isPrimary) {
  const view = isPrimary ? savedView() : { center: primary().getCenter(), zoom: primary().getZoom(), bearing: primary().getBearing() };
  // (padding is copied after creation so both halves of a comparison line up exactly)
  const m = new maplibregl.Map({
    container, style: makeStyle(), ...view, attributionControl: false, pitchWithRotate: false, maxPitch: 0, fadeDuration: reduceMotion ? 0 : 300,
  });
  m.touchZoomRotate.disableRotation();
  m.keyboard.disableRotation?.();
  if (isPrimary) {
    setAttribution(m, []);
    m.addControl(new maplibregl.ScaleControl({ unit: "metric", maxWidth: 110 }), "bottom-left");
  }
  if (!isPrimary) m.setPadding(primary().getPadding());
  // Overlays go in as soon as the style is parsed (not after every base tile has arrived), so evidence starts loading at once.
  m.once("style.load", async () => { await catalogReady; addOverlays(m); m.fire("overlays"); });
  m.on("load", () => {
    // Start with the attribution collapsed to its (i) button so it doesn't cover the map on a phone.
    if (!isDesk()) m.getContainer().querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
  });
  m.on("click", e => onMapClick(m, e));
  wireLongPress(m);
  wireTrackpad(m);
  m.on("mousemove", e => {
    if (S.placing || !m.loadedOverlays) return;
    if (S.view.name === "library") { const id = footprintAt(m, e.point); setFpState("hover", id); m.getCanvas().style.cursor = id ? "pointer" : ""; return; }
    m.getCanvas().style.cursor = hitTest(m, e.point).length ? "pointer" : "";
  });
  m.getCanvas().addEventListener("mouseleave", () => setFpState("hover", null));
  m.on("error", e => { if (!/Failed to fetch|could not be decoded|AJAXError/i.test(String(e?.error?.message || ""))) console.warn(e?.error || e); });
  return m;
}

// Rasterises a kind's shape (white halo, fill, thin dark edge) at 2x for crisp symbols on high-DPI screens.
function shapeImage(kind) {
  const k = KINDS[kind], px = 2, size = 24 * px, c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  g.scale(px * 0.92, px * 0.92); g.translate(1, 1);
  const path = new Path2D(SHAPES[k.shape]);
  g.lineJoin = "round"; g.lineWidth = 3.2; g.strokeStyle = "#fff"; g.stroke(path);
  g.fillStyle = k.color; g.fill(path);
  g.lineWidth = 0.8; g.strokeStyle = "rgba(0,0,0,.35)"; g.stroke(path);
  return { data: g.getImageData(0, 0, size, size), pixelRatio: px };
}

// Adds every source and layer the app draws on top of the base map: old-map rasters (hidden until chosen),
// evidence, place names and selections. Layer order is draw order, so selections and position come last.
function addOverlays(m) {
  for (const k of EV_KINDS) if (k !== "air_strike") { const im = shapeImage(k); m.addImage(`k-${k}`, im.data, { pixelRatio: im.pixelRatio }); }
  for (const l of S.catalog) addEraLayer(m, l);
  addFootprintLayers(m);
  for (const id of ["ev", "pn", "sel", "gridsq", "gridsq-c", "sites", "draft", "near", "hover", "line", "meacc", "me", "spot"]) m.addSource(id, { type: "geojson", data: lastData[id] || empty() });
  const color = ["match", ["get", "kind"], ...Object.entries(KINDS).flatMap(([k, v]) => [k, v.color]), "#888"];
  const isStrike = ["==", ["get", "kind"], "air_strike"];

  // Strikes: a muted density surface when zoomed out, individual points from ~z12.
  m.addLayer({ id: "strike-heat", type: "heatmap", source: "ev", maxzoom: 14, filter: isStrike, paint: {
    "heatmap-weight": 1,
    "heatmap-intensity": heatIntensity(),
    "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 6, 3, 9, 7, 11, 11, 13, 18],
    "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(254,240,138,0)", 0.15, "rgba(253,224,71,.25)",
      0.4, "rgba(245,158,11,.4)", 0.7, "rgba(217,119,6,.55)", 1, "rgba(146,64,14,.65)"],
    "heatmap-opacity": heatOpacity() } });
  // Uncertainty rings at true ground scale. Each ring sits on a contrasting halo so it survives satellite imagery
  // and old map sheets (halo colour follows the base map, see applyBase).
  const halo = (id, filter, minzoom = 0, op = 0.55) => m.addLayer({ id, type: "circle", source: "ev", minzoom, filter,
    paint: { "circle-radius": ringPx(), "circle-color": "rgba(0,0,0,0)", "circle-pitch-alignment": "map",
      "circle-stroke-color": "#fff", "circle-stroke-width": 3.25, "circle-stroke-opacity": ringFade(op) } });
  halo("strike-ring-halo", isStrike, 13, 0.22);
  m.addLayer({ id: "strike-ring", type: "circle", source: "ev", minzoom: 13, filter: isStrike,
    paint: { "circle-radius": ringPx(), "circle-color": "rgba(0,0,0,0)", "circle-pitch-alignment": "map",
      "circle-stroke-color": KINDS.air_strike.color, "circle-stroke-width": 1.25,
      // Many overlapping strike rings: faint at z13, clearer as you zoom in (the selected record always gets a bold ring).
      "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 13, 0.25, 15, 0.5] } });
  halo("ev-ring-halo", ["!", isStrike]);
  m.addLayer({ id: "ev-ring", type: "circle", source: "ev", filter: ["!", isStrike],
    paint: { "circle-radius": ringPx(), "circle-color": color, "circle-opacity": ringFade(0.08), "circle-pitch-alignment": "map",
      "circle-stroke-color": color, "circle-stroke-width": 1.75, "circle-stroke-opacity": ringFade(0.75) } });
  m.addLayer({ id: "strike-dot", type: "circle", source: "ev", minzoom: 11.5, filter: isStrike,
    paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 12, 3.5, 16, 7], "circle-color": KINDS.air_strike.color,
      "circle-stroke-color": "#fff", "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 12, 0.8, 15, 1.5],
      // Visible (and tappable, see hitTest) from the layer's minzoom: no half-faded dots that ignore taps.
      "circle-opacity": ["interpolate", ["linear"], ["zoom"], 11.5, 0.6, 12.3, 0.9], "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 11.5, 0.7, 12.3, 1] } });
  m.addLayer({ id: "pn-dot", type: "circle", source: "pn", minzoom: PLACENAME_ZOOM,
    paint: { "circle-radius": 3.5, "circle-color": "#fff", "circle-stroke-color": KINDS.placename.color, "circle-stroke-width": 1.5 } });
  m.addLayer({ id: "pn-label", type: "symbol", source: "pn", minzoom: PLACENAME_ZOOM,
    layout: { "text-field": ["get", "label"], "text-font": ["Noto Sans Medium"], "text-size": ["interpolate", ["linear"], ["zoom"], 12, 12, 16, 14],
      "text-offset": [0, 0.7], "text-anchor": "top", "text-max-width": 9, "text-padding": 6 },
    paint: { "text-color": "#2b2925", "text-halo-color": "rgba(255,255,255,.92)", "text-halo-width": 1.6 } });
  m.addLayer({ id: "ev-sym", type: "symbol", source: "ev", filter: ["!", isStrike],
    layout: { "icon-image": ["concat", "k-", ["get", "kind"]], "icon-size": ["interpolate", ["linear"], ["zoom"], 7, 0.7, 12, 0.95, 16, 1.15],
      "icon-allow-overlap": true, "icon-ignore-placement": true } });
  m.addLayer({ id: "ev-label", type: "symbol", source: "ev", minzoom: 13, filter: ["!", isStrike],
    layout: { "text-field": ["get", "label"], "text-font": ["Noto Sans Medium"], "text-size": 12, "text-offset": [0, 1.2], "text-anchor": "top", "text-max-width": 11, "text-optional": true },
    paint: { "text-color": "#1b1a17", "text-halo-color": "rgba(255,255,255,.92)", "text-halo-width": 1.6 } });

  // The Nearby search area: a faint labelled dashed circle, deliberately weaker than the thing selected.
  m.addLayer({ id: "near-line", type: "line", source: "near", paint: { "line-color": "#0e5a4b", "line-width": 1, "line-dasharray": [3, 3], "line-opacity": 0.55 } });
  m.addLayer({ id: "near-label", type: "symbol", source: "near", layout: { "symbol-placement": "line", "text-field": ["get", "label"], "text-font": ["Noto Sans Medium"],
    "text-size": 11, "symbol-spacing": 400, "text-keep-upright": true }, paint: { "text-color": "#0e5a4b", "text-halo-color": "rgba(255,255,255,.9)", "text-halo-width": 1.5 } });
  // Hover highlight for list rows (Nearby, stacked records).
  m.addLayer({ id: "hover", type: "circle", source: "hover", paint: { "circle-radius": 13, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#1d72f2", "circle-stroke-width": 3 } });
  // Grid square: the strongest shape on screen when it is open.
  m.addLayer({ id: "gridsq-fill", type: "fill", source: "gridsq", paint: { "fill-color": "#f59e0b", "fill-opacity": 0.35 } });
  m.addLayer({ id: "gridsq-halo", type: "line", source: "gridsq", paint: { "line-color": "#fff", "line-width": 6, "line-opacity": 0.85 } });
  m.addLayer({ id: "gridsq-line", type: "line", source: "gridsq", paint: { "line-color": "#b45309", "line-width": 3 } });
  m.addLayer({ id: "gridsq-dot", type: "circle", source: "gridsq-c", paint: { "circle-radius": 4, "circle-color": "#b45309", "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
  m.addLayer({ id: "site-ring-halo", type: "circle", source: "sites", paint: { "circle-radius": ringPx(), "circle-color": "rgba(0,0,0,0)",
    "circle-stroke-color": "#fff", "circle-stroke-width": 5, "circle-stroke-opacity": 0.6, "circle-pitch-alignment": "map" } });
  m.addLayer({ id: "site-ring", type: "circle", source: "sites", paint: { "circle-radius": ringPx(), "circle-color": "#c0267a", "circle-opacity": 0.12,
    "circle-stroke-color": "#c0267a", "circle-stroke-width": 2.5, "circle-pitch-alignment": "map" } });
  m.addLayer({ id: "site-dot", type: "circle", source: "sites", paint: { "circle-radius": 8, "circle-color": "#c0267a", "circle-stroke-color": "#fff", "circle-stroke-width": 2.5 } });
  m.addLayer({ id: "site-label", type: "symbol", source: "sites", layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Medium"], "text-size": 13, "text-offset": [0, 1.2], "text-anchor": "top" },
    paint: { "text-color": "#7a1150", "text-halo-color": "#fff", "text-halo-width": 2 } });
  m.addLayer({ id: "draft-fill", type: "fill", source: "draft", paint: { "fill-color": "#c0267a", "fill-opacity": 0.15 } });
  m.addLayer({ id: "draft-line", type: "line", source: "draft", paint: { "line-color": "#c0267a", "line-width": 2, "line-dasharray": [2, 1.5] } });
  m.addLayer({ id: "sel-ring", type: "line", source: "sel", paint: { "line-color": "#111", "line-width": 3 } });
  m.addLayer({ id: "sel-ring2", type: "line", source: "sel", paint: { "line-color": "#fff", "line-width": 1.2 } });
  m.addLayer({ id: "line", type: "line", source: "line", paint: { "line-color": "#1d72f2", "line-width": 2.5, "line-dasharray": [1, 1.5] } });
  m.addLayer({ id: "meacc", type: "fill", source: "meacc", paint: { "fill-color": "#1d72f2", "fill-opacity": 0.12 } });
  m.addLayer({ id: "me", type: "circle", source: "me", paint: { "circle-radius": 8, "circle-color": "#1d72f2", "circle-stroke-color": "#fff", "circle-stroke-width": 3 } });
  m.addLayer({ id: "spot", type: "circle", source: "spot", paint: { "circle-radius": 7, "circle-color": "#fff", "circle-stroke-color": "#111", "circle-stroke-width": 4 } });

  m.getSource("ev").setData(S.ev);
  m.getSource("pn").setData(S.pn);
  m.loadedOverlays = true;
  updateHeat();
  applyBase(); applyEra(); applyFilters(); drawCase(); drawTarget(); drawMe(); drawNearRing();
  showFootprints(S.view.name === "library", true);
}
// One installed old map as a raster layer (hidden until chosen). Maps added later go in under the coverage outlines
// and the evidence, the same place as the ones present at start.
function addEraLayer(m, l) {
  const id = `ov-${l.id}`;
  if (m.getLayer(id)) return;
  m.addSource(id, { type: "raster", tiles: ["/" + l.tiles.replace(/^\//, "")], tileSize: 256, bounds: l.bounds || undefined,
    attribution: l.attribution ? esc(l.attribution) : undefined,
    minzoom: l.minzoom || 0, maxzoom: l.maxzoom || 15 });
  m.addLayer({ id, type: "raster", source: id, layout: { visibility: "none" }, paint: { "raster-opacity": S.opacity, "raster-fade-duration": 0 } },
    m.getLayer("libfp-fill") ? "libfp-fill" : undefined);
}
function removeEraLayer(m, layerId) {
  const id = `ov-${layerId}`;
  if (m.getLayer(id)) m.removeLayer(id);
  if (m.getSource(id)) m.removeSource(id);
}
const heatOpacity = () => ["interpolate", ["linear"], ["zoom"], 9, S.heat, 12, S.heat * 0.7, 13.5, 0];
// The heat surface is normalised to what is shown: when the year filter leaves few strikes in view, each one
// weighs more, so a single year (e.g. 1968) is as legible as all years.
let heatBoost = 1;
const heatIntensity = () => ["interpolate", ["linear"], ["zoom"], 6, 0.08 * heatBoost, 9, 0.18 * heatBoost, 11, 0.35 * heatBoost, 13, 0.6 * heatBoost];
function updateHeat() { eachMap(m => { m.setPaintProperty("strike-heat", "heatmap-intensity", heatIntensity()); m.setPaintProperty("strike-heat", "heatmap-opacity", heatOpacity()); }); }

// The clickable features under a screen point, with a larger hit box on touch screens.
function hitTest(m, p) {
  const r = coarse ? 22 : 14;
  const box = [[p.x - r, p.y - r], [p.x + r, p.y + r]];
  const layers = ["site-dot", "ev-sym", "strike-dot", "pn-dot", "pn-label"].filter(id => m.getLayer(id));
  // strike-dot's minzoom is the threshold: every dot you can see can be tapped.
  return m.queryRenderedFeatures(box, { layers }).filter(f => f.layer.id !== "strike-dot" || m.getZoom() >= 11.5);
}

// Padding that keeps targets inside the part of the map you can actually see: clear of the sidebar or sheet,
// the map bar and the control column.
function mapPadding() {
  const bar = $("#mapbar"), ctl = $("#controls").getBoundingClientRect();
  const right = Math.round(innerWidth - ctl.left + 16);
  if (isDesk()) return { top: 60, bottom: Math.round(bar.offsetHeight + 20 + 24), left: (S.panelHidden ? 0 : 404) + 30, right };
  const below = sheetVisible() + (S.sheet === "full" ? 0 : bar.offsetHeight + 10) + 16;
  return { top: 70, bottom: Math.round(Math.min(innerHeight * 0.7, below)), left: 24, right };
}
// Old sheets are 256-px raster tiles on MapLibre's 512-px world, so a layer's tile maxzoom z is shown at full detail
// at map zoom z - 1; beyond that it is only enlarged.
const nativeZoom = e => e.maxzoom - 1;
const eraCovers = (e, lon, lat) => lon >= e.bounds[0] && lon <= e.bounds[2] && lat >= e.bounds[1] && lat <= e.bounds[3];
// The deepest zoom worth flying to at a point: no further than the native detail of any old map shown there.
function capZoom(lon, lat) {
  let z = MAX_FLY_ZOOM;
  for (const id of [S.eraA, S.compare ? S.eraB : null]) {
    const e = eraById(id);
    if (e && eraInYears(e) && eraCovers(e, lon, lat)) z = Math.min(z, nativeZoom(e));
  }
  return z;
}
// Frames a point with an uncertainty radius m (metres) in the visible part of the map.
function flyToTarget(lon, lat, m = 0) {
  const pad = mapPadding(), maxZoom = capZoom(lon, lat), duration = reduceMotion ? 0 : 700;
  if (m > 0) primary().fitBounds(circleBounds(lon, lat, Math.max(m * 1.4, 150)), { padding: pad, maxZoom, duration });
  else primary().easeTo({ center: [lon, lat], zoom: Math.min(maxZoom, Math.max(primary().getZoom(), 13)), padding: pad, duration });
}
// A grid square stops at the old sheet's full detail (z14 on the 1:50k sheets, where a 100 m square is ~20 px with
// its thick outline); on coarser sheets it still gets z14 so the square stays findable, and the bar says "enlarged".
function fitSquare(square) {
  const xs = square.map(c => c[0]), ys = square.map(c => c[1]);
  const sw = [Math.min(...xs), Math.min(...ys)], ne = [Math.max(...xs), Math.max(...ys)];
  const pad = mapPadding(), grow = Math.max(ne[0] - sw[0], ne[1] - sw[1]); // square fills about a third of the view
  const maxZoom = Math.min(MAX_FLY_ZOOM, Math.max(14, capZoom((sw[0] + ne[0]) / 2, (sw[1] + ne[1]) / 2)));
  primary().fitBounds([[sw[0] - grow, sw[1] - grow], [ne[0] + grow, ne[1] + grow]], { padding: pad, maxZoom, duration: reduceMotion ? 0 : 700 });
}

// ================================================================ trackpad & scroll navigation
// Apple Maps-style trackpad gestures across every map (main, swipe-compare, and custom-map placement):
//   * Two-finger drag pans the map (via wheel events whose deltas match a trackpad).
//   * Pinch / stretch zooms around the pointer (via ctrlKey wheel events in Chromium/WKWebView or Safari's
//     WebKit gesturestart/change/end events, suppressing duplicate ctrlKey wheel events during a gesture).
//   * A physical notched mouse wheel still zooms as usual when Scrolling is "Automatic" or "Mouse wheel".
const WHEEL_GAP_MS = 180;
const GESTURE_COOLDOWN_MS = 120;
const PAN_END_MS = 140;
const SCROLL_MODES = [["auto", "Automatic"], ["trackpad", "Trackpad"], ["wheel", "Mouse wheel"]];
const scrollMode = () => {
  const m = store.get("scrollMode", "auto");
  return m === "trackpad" || m === "wheel" ? m : "auto";
};
function setScrollMode(m) {
  store.set("scrollMode", m === "trackpad" || m === "wheel" ? m : "auto");
  settingsRefresh(true);
}
function isMouseWheelEvent(e) {
  if (!e || e.ctrlKey || e.metaKey) return false;
  if ((e.deltaMode || 0) !== 0) return true;
  const dx = e.deltaX || 0, dy = e.deltaY || 0, wd = e.wheelDeltaY;
  return dx === 0 && dy !== 0 && Number.isFinite(wd) && Math.abs(wd) >= 120 && Math.abs(wd) % 120 === 0 && wd !== Math.round(-dy * 3);
}
function classifyWheelEvent(e, state, mode, now = Date.now()) {
  if (e.ctrlKey && state.gestureAt && now - state.gestureAt <= (state.gestureActive ? 2000 : GESTURE_COOLDOWN_MS)) return "ignore";
  if (e.ctrlKey) {
    state.mode = null;
    state.lastTime = now;
    return "zoom";
  }
  if (mode === "wheel") return "zoom";
  if (mode === "trackpad") return "pan";
  const fresh = !state.mode || !state.lastTime || now - state.lastTime > WHEEL_GAP_MS;
  let kind;
  if ((e.deltaX || 0) !== 0) kind = "pan";
  else if (!fresh) kind = state.mode;
  else kind = isMouseWheelEvent(e) ? "zoom" : "pan";
  state.mode = kind;
  state.lastTime = now;
  return kind;
}
function zoomAroundMercator(centerLngLat, anchorLngLat, oldZoom, newZoom, MercatorCoordinate) {
  const s = Math.pow(2, newZoom - oldZoom);
  if (!Number.isFinite(s) || s <= 0) return centerLngLat;
  const c = MercatorCoordinate.fromLngLat(centerLngLat), a = MercatorCoordinate.fromLngLat(anchorLngLat);
  const nx = a.x + (c.x - a.x) / s;
  const ny = Math.min(1, Math.max(0, a.y + (c.y - a.y) / s));
  return new MercatorCoordinate(nx, ny, 0).toLngLat();
}
function wireTrackpad(m) {
  const el = m.getCanvasContainer();
  const ac = new AbortController(), sig = ac.signal;
  const wheelState = { mode: null, lastTime: 0, gestureAt: 0, gestureActive: false };
  let accX = 0, accY = 0, raf = 0, panActive = false, endTimer = 0;
  let gZoom = null, lastPt = null;

  const emitMoveEnd = () => {
    if (maps.includes(m)) eachMap(x => x.fire("moveend"));
    else m.fire("moveend");
  };
  const clearPan = () => {
    clearTimeout(endTimer); endTimer = 0;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    accX = 0; accY = 0;
  };
  const rememberPt = e => {
    if (!Number.isFinite(e?.clientX) || !Number.isFinite(e?.clientY)) return;
    const r = el.getBoundingClientRect();
    lastPt = [e.clientX - r.left, e.clientY - r.top];
  };
  const gesturePt = e => {
    const r = el.getBoundingClientRect();
    if (Number.isFinite(e?.clientX) && Number.isFinite(e?.clientY) && (e.clientX !== 0 || e.clientY !== 0 || !lastPt))
      return [e.clientX - r.left, e.clientY - r.top];
    return lastPt || [r.width / 2, r.height / 2];
  };

  el.addEventListener("pointermove", rememberPt, { passive: true, signal: sig });
  el.addEventListener("mousemove", rememberPt, { passive: true, signal: sig });

  el.addEventListener("wheel", e => {
    rememberPt(e);
    const kind = classifyWheelEvent(e, wheelState, scrollMode(), performance.now());
    if (kind === "ignore") {
      e.preventDefault(); e.stopImmediatePropagation();
      return;
    }
    if (kind === "zoom") {
      clearPan(); panActive = false;
      return; // let MapLibre's native ScrollZoomHandler zoom smoothly around the cursor
    }
    e.preventDefault(); e.stopImmediatePropagation();
    const scale = e.deltaMode === 1 ? 20 : e.deltaMode === 2 ? (el.clientHeight || 600) : 1;
    accX += (e.deltaX || 0) * scale;
    accY += (e.deltaY || 0) * scale;
    if (!raf) {
      raf = requestAnimationFrame(() => {
        raf = 0;
        const x = accX, y = accY;
        accX = 0; accY = 0;
        if (!panActive) { panActive = true; m.stop(); }
        if (x || y) m.panBy([x, y], { animate: false }, { trackpadMove: true });
        clearTimeout(endTimer);
        endTimer = setTimeout(() => { panActive = false; endTimer = 0; emitMoveEnd(); }, PAN_END_MS);
      });
    }
  }, { passive: false, capture: true, signal: sig });

  el.addEventListener("gesturestart", e => {
    e.preventDefault();
    clearPan(); panActive = false;
    m.stop();
    gZoom = m.getZoom();
    wheelState.gestureActive = true;
    wheelState.gestureAt = performance.now();
  }, { passive: false, signal: sig });

  el.addEventListener("gesturechange", e => {
    e.preventDefault();
    wheelState.gestureAt = performance.now();
    if (gZoom == null || !Number.isFinite(e.scale) || e.scale <= 0) return;
    const target = Math.min(m.getMaxZoom(), Math.max(m.getMinZoom(), gZoom + Math.log2(e.scale)));
    const oldZoom = m.getZoom();
    if (Math.abs(target - oldZoom) < 1e-6) return;
    const anchor = m.unproject(gesturePt(e));
    const center = zoomAroundMercator(m.getCenter(), anchor, oldZoom, target, maplibregl.MercatorCoordinate);
    m.jumpTo({ center, zoom: target }, { trackpadMove: true });
  }, { passive: false, signal: sig });

  const endGesture = e => {
    e.preventDefault?.();
    const wasPinch = gZoom != null;
    gZoom = null;
    wheelState.gestureActive = false;
    wheelState.gestureAt = performance.now();
    if (wasPinch) emitMoveEnd();
  };
  el.addEventListener("gestureend", endGesture, { passive: false, signal: sig });
  el.addEventListener("gesturecancel", endGesture, { passive: false, signal: sig });
  addEventListener("blur", () => { gZoom = null; wheelState.gestureActive = false; clearPan(); panActive = false; }, { signal: sig });

  m.on("remove", () => { clearPan(); ac.abort(); });
}

// ================================================================ historical maps (eras)
// Every sheet is its own choice: a map is picked for the place on screen, not merged with its neighbours by year.
function buildEras(layers) {
  const eras = [...layers].sort((a, b) => ((yearOf(a.date) || 9999) - (yearOf(b.date) || 9999)) || a.title.localeCompare(b.title))
    .map(l => ({ id: l.id, series: l.series, layers: [l] }));
  for (const e of eras) {
    const ys = e.layers.map(l => yearOf(l.date)).filter(Boolean);
    e.y0 = ys.length ? Math.min(...ys) : null; e.y1 = ys.length ? Math.max(...ys) : null;
    e.dated = e.y0 != null;
    e.years = !e.dated ? "Undated" : e.y0 === e.y1 ? String(e.y0) : `${e.y0}–${String(e.y1).slice(-2)}`;
    e.scale = shortScale(e.layers[0].scale);
    e.short = kindWords(e.layers[0].seriesId, e.series, e.layers[0].scale);
    e.name = e.layers[0].title;
    e.maxzoom = Math.max(...e.layers.map(l => l.maxzoom || 15));
    e.bounds = e.layers.reduce((b, l) => [Math.min(b[0], l.bounds[0]), Math.min(b[1], l.bounds[1]), Math.max(b[2], l.bounds[2]), Math.max(b[3], l.bounds[3])], [180, 90, -180, -90]);
  }
  return eras;
}
const eraById = id => S.eras.find(e => e.id === id);
// What a map is, in the same few words wherever it is named (the map bar, the library card): "US Army 1:50,000",
// "US overview 1:250,000", "French 1:100,000". seriesId is the catalogue's series key, seriesName its full name.
const SERIES_WORDS = { l7014: "US Army", jog1501: "US overview", l909: "US city plan", l509: "US Army", sgi100k: "French", warper: "British chart", custom: "Your map" };
function kindWords(seriesId, seriesName, scale) {
  // US series: Army Map Service sheets and the Defense Mapping Agency's Joint Operations Graphics.
  const who = SERIES_WORDS[seriesId] || (/AMS|Army Map Service|US |Joint Operations Graphic/.test(seriesName || "") ? "US" : "French");
  // Only a real scale ("1:100,000") is named; a series of charts at many scales says just what it is.
  return [who, /^1:/.test(fullScale(scale)) ? fullScale(scale) : ""].filter(Boolean).join(" ");
}
const eraInYears = e => !e || !S.years || !e.dated || (e.y1 >= S.years[0] && e.y0 <= S.years[1]);
const yearsText = () => !S.years ? "All years" : S.years[0] === S.years[1] ? String(S.years[0]) : `${S.years[0]}–${S.years[1]}`;
// The part of the main map the person can see, in canvas pixels: not under the sidebar (laptop), nor under the sheet
// and the map bar (phone). "On screen" and "the centre of the view" mean this part, for counts, order and fitting.
function openArea() {
  const c = primary()?.getContainer();
  if (!c) return null;
  const w = c.clientWidth, h = c.clientHeight;
  let left = 0, bottom = h;
  if (isDesk()) left = Math.min(mapLeft(), Math.max(0, w - 120));
  // A sheet pulled up to full height hides the whole map; then the map behind it is what "on screen" means.
  else if (S.sheet !== "full") bottom = Math.max(120, h - sheetVisible() - $("#mapbar").offsetHeight - 10);
  return { left, top: 0, right: w, bottom };
}
const viewCenter = () => {
  const a = openArea();
  if (!a) return store.get("center", CENTER);
  const c = primary().unproject([(a.left + a.right) / 2, (a.top + a.bottom) / 2]);
  return [c.lng, c.lat];
};
const viewBounds = () => {
  const a = openArea();
  if (!a) return null;
  const pts = [[a.left, a.top], [a.right, a.top], [a.right, a.bottom], [a.left, a.bottom]].map(p => primary().unproject(p));
  const lons = pts.map(p => p.lng), lats = pts.map(p => p.lat);
  return [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
};
const eraInView = e => { const v = viewBounds(); return !v || (e.bounds[0] < v[2] && e.bounds[2] > v[0] && e.bounds[1] < v[3] && e.bounds[3] > v[1]); };
// The most detailed dated sheet under the centre of the view (newest if several); else any dated sheet in view.
function defaultEra() {
  const [lon, lat] = viewCenter();
  const best = list => [...list].sort((x, y) => (y.maxzoom - x.maxzoom) || (y.y1 - x.y1))[0];
  return (best(S.eras.filter(e => e.dated && eraCovers(e, lon, lat))) || best(S.eras.filter(e => e.dated && eraInView(e))))?.id || null;
}

// The primary map's credits. Its control sits above the comparison map, so while comparing it also carries the
// right-hand old map's credit (MapLibre only lists the sources of its own map). Rebuilt only when that text changes.
let attribCtrl = null, attribExtra = null;
function setAttribution(m, extra) {
  const key = extra.join("|");
  if (attribCtrl && key === attribExtra) return;
  if (attribCtrl) m.removeControl(attribCtrl);
  attribCtrl = new maplibregl.AttributionControl({ compact: true, customAttribution: extra.length ? extra : undefined });
  attribExtra = key;
  m.addControl(attribCtrl, isDesk() ? "bottom-right" : "top-left");
}
// On a laptop the credits sit at the bottom right and the map bar in the bottom middle, and a long old-map credit
// would run under the bar. Keep them beside it, wrapping, or, when there is no room beside it, lift them above it.
function fitAttribution() {
  const box = maps[0]?.getContainer().querySelector(".maplibregl-ctrl-bottom-right");
  if (!box) return;
  const bar = $("#mapbar").getBoundingClientRect();
  const room = Math.floor(innerWidth - bar.right - 16);
  const besideOk = !isDesk() || !bar.width || room >= 200;
  const maxWidth = besideOk && isDesk() && bar.width ? room + "px" : "";
  const bottom = besideOk ? "" : Math.ceil(innerHeight - bar.top + 6) + "px";
  if (box.style.maxWidth !== maxWidth) box.style.maxWidth = maxWidth;
  if (box.style.bottom !== bottom) box.style.bottom = bottom;
}
// Shows the chosen old map on each map (and hides the rest), with the shared opacity.
function applyEra() {
  const right = S.compare && S.eraB !== "today" ? eraById(S.eraB) : null;
  const extra = right && eraInYears(right) ? [...new Set(right.layers.map(l => l.attribution).filter(Boolean))].map(esc) : [];
  if (maps[0]) setAttribution(maps[0], extra);
  maps.forEach((m, i) => {
    if (!m?.loadedOverlays) return;
    const want = i === 0 ? S.eraA : (S.eraB === "today" ? null : S.eraB);
    const era = eraById(want);
    for (const l of S.catalog) {
      const on = !!era && era.layers.includes(l) && eraInYears(era);
      m.setLayoutProperty(`ov-${l.id}`, "visibility", on ? "visible" : "none");
      // One opacity for every old map shown (both sides when comparing). Scans keep their own paper colours in dark
      // mode too: dimming them made the sheet, the evidence and the labels on it all muddy.
      m.setPaintProperty(`ov-${l.id}`, "raster-opacity", S.opacity);
      m.setPaintProperty(`ov-${l.id}`, "raster-brightness-max", 1);
    }
  });
  renderMapbar();
}
// Chooses the old map for side "A" (main / left) or "B" (right, when comparing); id "today" means none.
function setEra(id, side = "A") {
  const val = id === "today" ? null : id;
  if (side === "A") { S.eraA = val; store.set("era", S.eraA || "today"); store.set("eraLabel", [eraTitle(val || "today"), eraSub(val || "today")]); }
  else S.eraB = val || "today";
  applyEra();
  // Move to a scale where the chosen map is readable, and to its coverage if we're outside it. A selection
  // (record, grid square, pin) stays the centre of attention: zoom out around it, not around the old centre.
  const e = eraById(val), m = primary(), t = S.target, dur = reduceMotion ? 0 : 500;
  if (e && eraInYears(e)) {
    const c = m.getCenter();
    if (t && eraCovers(e, t.lon, t.lat)) m.easeTo({ center: [t.lon, t.lat], zoom: Math.min(m.getZoom(), nativeZoom(e) + 0.5), padding: mapPadding(), duration: dur });
    else if (!eraCovers(e, c.lng, c.lat)) m.fitBounds([[e.bounds[0], e.bounds[1]], [e.bounds[2], e.bounds[3]]], { padding: mapPadding(), duration: dur });
    else if (m.getZoom() > nativeZoom(e) + 0.5) m.easeTo({ zoom: nativeZoom(e), duration: dur });
  }
  // Cards whose buttons depend on the era (the grid card's "1960s sheet") are redrawn.
  if (["era", "home", "grid"].includes(S.view.name)) render();
  libRefreshView(); // the library card's Show / Hide buttons
}

function eraTitle(id) { const e = eraById(id === "today" ? null : id); return e ? e.years : "Today"; }
function eraSub(id) { const e = eraById(id === "today" ? null : id); return e ? `${e.short} · ${e.name}` : `${BASES[S.base].name}, no old map`; }
// The bottom bar: picker labels, opacity, and a note when the chosen map is hidden, off screen or over-enlarged.
function renderMapbar() {
  // No old map on this computer yet: the bar is a single invitation to add some for the area on screen (nothing
  // downloads until the person taps Add in the library card). Decided only once the catalogue has arrived, so an
  // installed map's label never flickers to the invitation on load.
  const none = S.catalogLoaded && !S.eras.length;
  document.body.classList.toggle("no-old-maps", none);
  const adding = libAddingText();
  if (none) {
    if (S.compare) return setCompare(false); // (it redraws the bar)
    const n = libInView().length;
    $("#pickAtitle").textContent = adding ? "Old maps" : "Add old maps";
    $("#pickAsub").textContent = adding || (!S.lib.loaded ? "For the area on screen" : n ? `${fmtInt(n)} map${n === 1 ? "" : "s"} of this area` : "None of this area yet");
    $("#pickA").setAttribute("aria-label", `${$("#pickAtitle").textContent}: ${$("#pickAsub").textContent}`);
    $("#mbNote").hidden = true;
    return;
  }
  const a = S.eraA || "today", b = S.eraB || "today";
  $("#pickAtitle").textContent = eraTitle(a); $("#pickAsub").textContent = eraSub(a);
  $("#pickBtitle").textContent = eraTitle(b); $("#pickBsub").textContent = eraSub(b);
  $("#pickA").setAttribute("aria-label", `${S.compare ? "Left map" : "Historical map"}: ${eraTitle(a)}, ${eraSub(a)}. Change`);
  $("#pickB").setAttribute("aria-label", `Right map: ${eraTitle(b)}, ${eraSub(b)}. Change`);
  $("#pickB").hidden = !S.compare; $("#pickAside").hidden = !S.compare;
  const eA = eraById(S.eraA);
  $("#mapbar").classList.toggle("disabled-op", !eA || !eraInYears(eA));
  $("#opacity").value = Math.round(S.opacity * 100); $("#opVal").textContent = Math.round(S.opacity * 100) + "%";
  // Never show an inconsistent state: if a chosen map is outside the year range, say so (and offer a map that fits).
  const out = [[S.eraA, S.compare ? "Left " : ""], [S.compare && S.eraB !== "today" ? S.eraB : null, "Right "]]
    .map(([id, side]) => ({ e: eraById(id), side })).filter(x => x.e && !eraInYears(x.e));
  const note = $("#mbNote");
  const m = primary();
  let html = "";
  if (out.length) {
    const alt = out.length === 1 && out[0].side !== "Right " ? suggestEra() : null;
    html = `${icon.warn}<span class="mb-note-t" title="Hidden: outside your years (${esc(yearsText())})">${out.map(x => `${esc(x.e.years)}`).join(" & ")} map${out.length > 1 ? "s" : ""}: outside ${esc(yearsText())}</span>
      ${alt ? `<button class="mb-chip" data-mb="alt" data-id="${alt.id}">Show ${esc(alt.years)} map</button>` : ""}
      <button class="mb-chip" data-mb="clearYears">All years</button>`;
  } else if (m && eA && eraInYears(eA) && !eraInView(eA)) {
    const alt = defaultEra();
    html = `<span class="mb-note-t muted">${esc(eA.name)} is off screen</span>${alt && alt !== eA.id ? `<button class="mb-chip" data-mb="alt" data-id="${alt}">Show ${esc(eraById(alt).name)}</button>`
      : libCanAddHere() && S.view.name !== "library" ? `<button class="mb-chip" data-mb="lib">Add maps here</button>` : ""}`;
  } else if (m && !eA && !S.compare && libCanAddHere() && !S.eras.some(eraInView) && S.view.name !== "library") {
    // (Not while the library card is open: it is already showing the maps to add.)
    // Old maps exist, but none of this area: say so where the choice is made, with the way to get one.
    html = `<span class="mb-note-t muted">No old map of this area yet</span><button class="mb-chip" data-mb="lib">Add maps here</button>`;
  } else if (m && eA && eraInYears(eA)) {
    // Past an old sheet's native detail it is only being enlarged; say so instead of silently blurring.
    const c = m.getCenter();
    if (eraCovers(eA, c.lng, c.lat) && m.getZoom() > nativeZoom(eA) + 0.75)
      html = `<span class="mb-note-t muted">${esc(eA.years)} map (${esc(eA.scale)}) is enlarged past its detail</span><button class="mb-chip" data-mb="zoomOut">Zoom out</button>`;
  }
  // Maps on their way, while the library card is closed: their progress, one tap from the card.
  if (!html && adding && S.view.name !== "library")
    html = `<span class="mb-note-t muted" data-mb-prog>${esc(adding)}</span><button class="mb-chip" data-mb="lib">Show</button>`;
  note.hidden = !html;
  // Progress is not announced (it changes every few seconds): the region goes quiet and the text moves in place.
  const prog = html.includes("data-mb-prog"), shown = note.querySelector("[data-mb-prog]");
  note.setAttribute("aria-live", prog ? "off" : "polite");
  if (prog && shown) shown.textContent = adding;
  else if (note.innerHTML !== html) {
    note.innerHTML = html;
    note.querySelector('[data-mb="clearYears"]')?.addEventListener("click", () => setYears(null));
    note.querySelector('[data-mb="alt"]')?.addEventListener("click", e => setEra(e.currentTarget.dataset.id));
    note.querySelector('[data-mb="zoomOut"]')?.addEventListener("click", () => m.easeTo({ zoom: nativeZoom(eA), duration: reduceMotion ? 0 : 400 }));
    note.querySelector('[data-mb="lib"]')?.addEventListener("click", () => openLibrary());
  }
  note.classList.toggle("warn", out.length > 0);
  // Which old map the opacity slider fades: in a comparison it is every old map on screen.
  const sides = [S.eraA && eraInYears(eraById(S.eraA)) ? "left" : null, S.compare && S.eraB !== "today" && eraInYears(eraById(S.eraB)) ? "right" : null].filter(Boolean);
  const opWho = !S.compare ? "Old map" : sides.length === 2 ? "Both" : sides[0] === "right" ? "Right" : "Left";
  $("#opSide").textContent = S.compare ? opWho : "";
  $("#opSide").hidden = !S.compare;
  $("#opacity").setAttribute("aria-label", S.compare ? `Old map opacity (${opWho.toLowerCase()} side${sides.length === 2 ? "s" : ""})` : "Old map opacity");
  // Short side labels, kept clear of the control column (see placeDivider).
  $("#tagA").textContent = eraTitle(a); $("#tagA").title = `${eraTitle(a)} · ${eraSub(a)}`;
  $("#tagB").textContent = eraTitle(b); $("#tagB").title = `${eraTitle(b)} · ${eraSub(b)}`;
}
// The dated map that best fits the year filter, preferring one that covers the view.
function suggestEra() {
  if (!S.years) return null;
  const c = primary().getCenter(), mid = (S.years[0] + S.years[1]) / 2;
  const covers = e => eraCovers(e, c.lng, c.lat);
  const cands = S.eras.filter(e => e.dated && eraInYears(e));
  cands.sort((x, y) => (covers(y) - covers(x)) || Math.abs((x.y0 + x.y1) / 2 - mid) - Math.abs((y.y0 + y.y1) / 2 - mid));
  return cands[0] || null;
}

// ---- picker popover
let pickSide = "A"; // which side the open picker chooses for

function openMapPicker(side) {
  pickSide = side;
  const cur = (side === "A" ? S.eraA : S.eraB) || "today";
  const opt = (id, yr, t, m, out = false) => `<button class="map-opt${out ? " out" : ""}" role="menuitemradio" aria-checked="${cur === id}" data-era="${id}">
    <span class="yr">${esc(yr)}</span><span class="txt"><span class="t">${esc(t)}</span><span class="m">${esc(m)}</span></span>
    <span class="check">${cur === id ? icon.check : ""}</span></button>`;
  // Only the maps that cover what is on screen; the rest are one tap away, folded.
  const here = S.eras.filter(e => eraInView(e) || e.id === cur), away = S.eras.filter(e => !here.includes(e));
  const dated = here.filter(e => e.dated), undated = here.filter(e => !e.dated);
  const row = e => opt(e.id, e.years, e.name, `${e.short}${eraInView(e) ? "" : " · not in view"}${eraInYears(e) ? "" : " · outside your years"}`, !eraInYears(e));
  const fold = (label, list) => list.length ? `<details ${list.some(e => e.id === cur) ? "open" : ""}><summary>${icon.chevR} ${label} (${list.length})</summary>
      ${list.map(row).join("")}</details>` : "";
  const pop = $("#popMaps");
  // With no dated map of this area on this computer, the first row offers to add some.
  pop.innerHTML = `<div role="menu" aria-label="${side === "B" ? "Right map" : S.compare ? "Left map" : "Historical map"}">
    ${dated.length ? "" : libCanAddHere() ? `<button class="map-opt invite" role="menuitem" data-lib><span class="yr">${icon.plus}</span>
      <span class="txt"><span class="t">Add old maps of this area</span><span class="m">${fmtInt(libInView().length)} can be added</span></span></button>`
      : `<p class="small-t muted" style="margin:8px 14px">No dated old map covers this view.</p>`}
    ${dated.map(row).join("")}
    ${opt("today", "Today", BASES[S.base].name, "No old map")}
    ${fold("Date unknown", undated)}
    ${fold("Elsewhere", away)}</div>
    <div class="pop-foot">${cur !== "today" ? `<button class="linkish" data-about="${cur}">About this map</button>` : ""}
      ${S.years ? `<span class="small-t muted">Years: ${esc(yearsText())}</span>` : ""}
      <button class="linkish" data-lib>Add or remove maps</button></div>`;
  $$(".map-opt[data-era]", pop).forEach(b => b.addEventListener("click", () => { closePopovers(); setEra(b.dataset.era, pickSide); }));
  $$("[data-lib]", pop).forEach(b => b.addEventListener("click", () => openLibrary()));
  pop.querySelector("[data-about]")?.addEventListener("click", e => { closePopovers(); show("era", { id: e.target.dataset.about }); });
  openPopover("popMaps", side === "A" ? $("#pickA") : $("#pickB"));
  // Scroll the list itself (never the page) so the current map sits in the middle with whole rows around it.
  const currentRow = pop.querySelector('[aria-checked="true"]');
  pop.scrollTop = currentRow ? Math.max(0, currentRow.offsetTop - (pop.clientHeight - currentRow.offsetHeight) / 2) : 0;
}

// ================================================================ compare (swipe)
// The right-hand map sits on top of the main one, clipped to the right of the divider at cmpX (px from the
// window's left edge). The two follow each other's moves; `lock` stops the echo of one jump bouncing back.
let cmpX = null, unsync = null;
// Left edge of the visible map: the sidebar (380 px) plus its gutters on a laptop, 0 on a phone.
function mapLeft() { return isDesk() && !S.panelHidden ? 404 : 0; }
function setCompare(on) {
  if (on === S.compare) return;
  S.compare = on;
  document.body.classList.toggle("comparing", on);
  const cmpBtn = $("#mbCmp");
  cmpBtn.setAttribute("aria-pressed", on); cmpBtn.setAttribute("aria-label", on ? "Stop comparing" : "Compare two maps");
  $("#mapB").hidden = !on; $("#divider").hidden = !on;
  if (on) {
    if (!S.eraA) S.eraA = defaultEra();
    if (!S.eraB || S.eraB === S.eraA) S.eraB = "today";
    const a = primary(), b = createMap("mapB", false);
    maps[1] = b;
    let lock = false;
    const follow = (src, dst) => e => { if (lock) return; lock = true; dst.jumpTo({ center: src.getCenter(), zoom: src.getZoom(), bearing: src.getBearing(), padding: src.getPadding() }, e?.trackpadMove ? { trackpadMove: true } : undefined); lock = false; };
    const fa = follow(a, b), fb = follow(b, a);
    a.on("move", fa); b.on("move", fb);
    b.on("moveend", e => { if (!e?.trackpadMove) scheduleLoad(); });
    unsync = () => a.off("move", fa);
    b.on("overlays", () => applyEra());
    cmpX = null; placeDivider();
  } else {
    unsync?.(); unsync = null;
    maps[1]?.remove(); maps.length = 1;
  }
  applyEra();
}
function placeDivider() {
  if (!S.compare) return;
  const L = mapLeft(), W = innerWidth;
  if (cmpX == null || cmpX < L + 24 || cmpX > W - 24) cmpX = Math.round(L + (W - L) / 2);
  $("#divider").style.left = cmpX + "px";
  $("#mapB").style.clipPath = `inset(0 0 0 ${cmpX}px)`;
  // The handle sits in the middle of the visible map, but always below the control column (so it never meets the
  // compass); the side labels hang just under the handle, each inside its own half.
  const ctl = $("#controls").getBoundingClientRect(), bar = $("#mapbar");
  const bottom = isDesk() ? bar.getBoundingClientRect().top : innerHeight - sheetVisible() - (S.sheet === "full" ? 0 : bar.offsetHeight + 10);
  const y = Math.round(Math.max(ctl.bottom + 40, bottom / 2));
  $("#dividerHandle").style.top = y + "px";
  for (const t of [$("#tagA"), $("#tagB")]) t.style.top = (y + 32) + "px";
  $("#tagA").style.maxWidth = Math.max(40, cmpX - L - 12) + "px";
  $("#tagB").style.maxWidth = Math.max(40, innerWidth - cmpX - 12) + "px";
}
(function wireDivider() {
  const h = $("#dividerHandle");
  const clamp = x => Math.min(innerWidth - 24, Math.max(mapLeft() + 24, x));
  h.addEventListener("pointerdown", e => {
    h.setPointerCapture(e.pointerId);
    const move = ev => { cmpX = clamp(ev.clientX); placeDivider(); };
    const up = () => { h.removeEventListener("pointermove", move); h.removeEventListener("pointerup", up); h.removeEventListener("pointercancel", up); };
    h.addEventListener("pointermove", move); h.addEventListener("pointerup", up); h.addEventListener("pointercancel", up);
  });
  h.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { cmpX = clamp(cmpX + (e.key === "ArrowLeft" ? -40 : 40)); placeDivider(); e.preventDefault(); }
  });
})();

// ================================================================ evidence loading (cached grid cells)
// The view is covered by fixed grid cells; each cell is fetched once ("e:x:y" evidence, "p:x:y" place names),
// at most four requests at a time, nearest the centre first. Records are kept by id, so features shared by
// two cells, or also fetched by Nearby, are stored once.
const evById = new Map(), pnById = new Map(), cellState = new Map();
let active = 0, pendingQ = [], loadErrShown = false, tilesBusy = false;
// Adds the derived properties the layers and filters use: ys / ye (start / end year, -1 when undated), r0 (ring
// radius, see r0Of) and a short label.
function prep(f) {
  const p = f.properties;
  p.ys = yearOf(p.date_start) ?? -1;
  p.ye = yearOf(p.date_end) ?? p.ys;
  if (p.uncertainty_m == null) p.uncertainty_m = 0;
  p.r0 = r0Of(p.uncertainty_m, f.geometry.coordinates[1]);
  p.label = cleanTitle(p.title).replace(/\s*\([^)]*\)\s*$/, "");
  return f;
}
function cellsInView(size, padFrac = 0.15) {
  const m = primary(), b = m.getBounds();
  const w = b.getWest(), e = b.getEast(), s = b.getSouth(), n = b.getNorth();
  const px = (e - w) * padFrac, py = (n - s) * padFrac;
  const keys = [];
  for (let x = Math.floor((w - px) / size); x <= Math.floor((e + px) / size); x++)
    for (let y = Math.floor((s - py) / size); y <= Math.floor((n + py) / size); y++) keys.push([x, y]);
  const cx = (w + e) / 2 / size, cy = (s + n) / 2 / size;
  // Nearest the centre first.
  return keys.sort((a, b) => Math.hypot(a[0] + .5 - cx, a[1] + .5 - cy) - Math.hypot(b[0] + .5 - cx, b[1] + .5 - cy));
}
let loadTimer;
function scheduleLoad() { clearTimeout(loadTimer); loadTimer = setTimeout(loadView, 150); }
// Queues the cells for the current view. Below zoom 7, or when a view would need too many cells, nothing new is
// fetched (the map shows what is already cached).
function loadView() {
  const z = primary().getZoom();
  if (z >= 7) {
    const keys = cellsInView(CELL);
    if (keys.length <= 120) for (const k of keys) queueCell("e", k, CELL);
  }
  if (z >= PLACENAME_ZOOM - 0.5 && !S.hidden.has("placename")) {
    const keys = cellsInView(PCELL, 0.1);
    if (keys.length <= 80) for (const k of keys) queueCell("p", k, PCELL);
  }
  pump(); updateLoading();
}
function queueCell(type, [x, y], size) {
  const key = `${type}:${x}:${y}`;
  if (cellState.has(key)) return;
  cellState.set(key, "queued");
  pendingQ.push({ key, type, bbox: [x * size, y * size, (x + 1) * size, (y + 1) * size] });
}
function pump() {
  while (active < 4 && pendingQ.length) {
    const job = pendingQ.shift(), gen = evGen; active++;
    const kinds = job.type === "e" ? EV_KINDS.join(",") : "placename";
    api(`/api/evidence?bbox=${job.bbox.map(v => v.toFixed(4)).join(",")}&kinds=${kinds}&slim=1&limit=50000`).then(fc => {
      if (gen !== evGen) return; // the records changed while this was on its way; the cell is asked for again
      const into = job.type === "e" ? evById : pnById;
      for (const f of fc.features) if (!into.has(f.properties.id)) into.set(f.properties.id, prep(f));
      cellState.set(job.key, "done");
      if (fc.truncated) console.info("cell truncated", job.key);
      flushSoon(job.type);
    }).catch(err => {
      if (gen !== evGen) return;
      cellState.delete(job.key);
      if (!loadErrShown) { loadErrShown = true; toast("Some evidence could not be loaded: " + err.message, 5000); }
    }).finally(() => { active--; pump(); updateLoading(); });
  }
}
// Batches the GeoJSON updates: a burst of cells arriving together costs one setData per source.
const flushT = {};
function flushSoon(type) {
  clearTimeout(flushT[type]);
  flushT[type] = setTimeout(() => {
    if (type === "e") { S.ev = { type: "FeatureCollection", features: [...evById.values()] }; setData("ev", S.ev); }
    else { S.pn = { type: "FeatureCollection", features: [...pnById.values()] }; setData("pn", S.pn); }
    updateCounts();
  }, 120);
}
// The "Loading…" pill, shown only when loading lasts more than 250 ms so quick loads don't flicker.
let loadingShowT;
function updateLoading() {
  const busy = active > 0 || pendingQ.length > 0, el = $("#loading");
  const txt = busy ? "Loading evidence…" : tilesBusy ? "Loading map…" : "";
  clearTimeout(loadingShowT);
  if (!txt) { el.hidden = true; return; }
  $("#loadingText").textContent = txt;
  if (el.hidden) loadingShowT = setTimeout(() => { el.hidden = false; }, 250);
}

// ---- filters & counts
// A MapLibre filter for the year range: undated records (ys < 0) always pass; dated ones pass if they overlap it.
function yearFilter() {
  if (!S.years) return ["to-boolean", 1];
  const [a, b] = S.years;
  return ["any", ["<", ["get", "ys"], 0], ["all", [">=", ["get", "ye"], a], ["<=", ["get", "ys"], b]]];
}
function applyFilters() {
  const yf = yearFilter(), hid = ["literal", [...S.hidden]];
  eachMap(m => {
    const strikeOn = !S.hidden.has("air_strike");
    for (const id of ["strike-heat", "strike-ring-halo", "strike-ring", "strike-dot"]) {
      m.setFilter(id, ["all", ["==", ["get", "kind"], "air_strike"], yf]);
      m.setLayoutProperty(id, "visibility", strikeOn ? "visible" : "none");
    }
    for (const id of ["ev-ring-halo", "ev-ring", "ev-sym", "ev-label"]) m.setFilter(id, ["all", ["!=", ["get", "kind"], "air_strike"], ["!", ["in", ["get", "kind"], hid]], yf]);
    for (const id of ["pn-dot", "pn-label"]) { m.setFilter(id, yf); m.setLayoutProperty(id, "visibility", S.hidden.has("placename") ? "none" : "visible"); }
    m.setPaintProperty("strike-heat", "heatmap-opacity", heatOpacity());
  });
  updateCounts();
}
// Records of each kind inside the visible bounds and years (place names only from PLACENAME_ZOOM, else null).
let counts = {};
const inYears = p => !S.years || p.ys < 0 || (p.ye >= S.years[0] && p.ys <= S.years[1]);
function updateCounts() {
  const m = primary(); if (!m) return;
  const b = m.getBounds(), w = b.getWest(), e = b.getEast(), s = b.getSouth(), n = b.getNorth();
  counts = {};
  let strikesAll = 0;
  for (const f of S.ev.features) {
    const [x, y] = f.geometry.coordinates, p = f.properties;
    if (x < w || x > e || y < s || y > n) continue;
    if (p.kind === "air_strike") strikesAll++;
    if (inYears(p)) counts[p.kind] = (counts[p.kind] || 0) + 1;
  }
  let pnN = 0;
  if (m.getZoom() >= PLACENAME_ZOOM) for (const f of S.pn.features) { const [x, y] = f.geometry.coordinates; if (x >= w && x <= e && y >= s && y <= n) pnN++; }
  counts.placename = m.getZoom() >= PLACENAME_ZOOM ? pnN : null;
  $$("[data-count]").forEach(el => {
    const k = el.dataset.count, v = counts[k];
    el.textContent = k === "placename" && v == null ? `from zoom ${PLACENAME_ZOOM}` : m.getZoom() < 7 && k !== "placename" ? "zoom in" : fmtInt(v || 0);
  });
  // Keep the heat legible when the year filter thins it out.
  const shown = counts.air_strike || 0;
  const boost = S.years && shown ? Math.min(4, Math.max(1, Math.sqrt(strikesAll / shown))) : 1;
  if (Math.abs(boost - heatBoost) > 0.05) { heatBoost = boost; updateHeat(); }
  renderFilterPill();
}
// Kinds with no records anywhere yet (checked once at start); listed in one quiet line instead of as toggles.
const emptyKinds = new Set();
const shownKinds = () => Object.keys(KINDS).filter(k => !emptyKinds.has(k));
function toggleKind(k) {
  S.hidden.has(k) ? S.hidden.delete(k) : S.hidden.add(k);
  kindsChanged(k === "placename");
}
function onlyKind(k) {
  S.hidden = new Set(shownKinds().filter(x => x !== k));
  kindsChanged(true);
}
function showAllKinds() { S.hidden = new Set(); kindsChanged(true); }
function kindsChanged(pn) {
  store.set("hidden", [...S.hidden]);
  applyFilters();
  if (pn) loadView();
  $$("[data-kind]").forEach(el => el.setAttribute("aria-pressed", !S.hidden.has(el.dataset.kind)));
  if (!$("#popLayers").hidden) renderLayersPop(true);
  if (S.view.name === "home") render();
  if (S.view.nearby) renderNearby();
}
function kindRows() {
  return shownKinds().map(k => { const v = KINDS[k]; return `<li class="kind-li"><button class="kind-row" data-kind="${k}" aria-pressed="${!S.hidden.has(k)}">
    ${k === "air_strike" ? `<span class="sym heat" aria-hidden="true"></span>` : symSVG(k)}
    <span class="name">${esc(v.label)}</span><span class="n" data-count="${k}">–</span><span class="tog" aria-hidden="true"></span></button>
    <button class="only" data-only="${k}" aria-label="Show only ${esc(v.label.toLowerCase())}" title="Show only ${esc(v.label.toLowerCase())}">Only</button></li>`; }).join("");
}
// What the map is filtered to, in a few words (used by the filter pill and the home summary).
function filterWords() {
  const vis = shownKinds().filter(k => !S.hidden.has(k) && k !== "placename");
  const all = shownKinds().filter(k => k !== "placename");
  const kinds = vis.length === all.length ? "" : vis.length === 0 ? "No evidence layers" : vis.length === 1 ? `${KINDS[vis[0]].label} only`
    : `${vis.length} of ${all.length} layers`;
  return { kinds, years: S.years ? yearsText() : "" };
}
function renderFilterPill() {
  const pill = $("#filterPill"); if (!pill) return;
  const { kinds, years } = filterWords();
  const on = !!(kinds || years);
  pill.hidden = !on; document.body.classList.toggle("filtered", on);
  if (!on) return;
  const m = primary(), z = m ? m.getZoom() : 0;
  const vis = shownKinds().filter(k => !S.hidden.has(k) && k !== "placename");
  const n = vis.reduce((a, k) => a + (counts[k] || 0), 0);
  // One layer: "1,735 air strikes in view · 1968"; otherwise "3 of 6 layers · 1968 · 2,040 in view".
  const IN = '<span class="in-view"> in view</span>';
  const parts = vis.length === 1 && z >= 7 ? [`${fmtInt(n)} ${esc(KINDS[vis[0]].label.toLowerCase())}${IN}`, esc(years)]
    : [esc(kinds), esc(years), z >= 7 ? `${fmtInt(n)}${IN}` : ""];
  const html = parts.filter(Boolean).join(" · "), text = html.replace(/<[^>]+>/g, "");
  $("#filterText").innerHTML = html;
  $("#filterOpen").setAttribute("aria-label", `Filters: ${text}. Change`);
}
const YEARS_UI = () => `
  <div class="chips" role="group" aria-label="Year presets">${YEAR_PRESETS.map(([a, b, l]) => {
    const on = a == null ? !S.years : S.years && S.years[0] === a && S.years[1] === b;
    return `<button class="chip" data-act="preset" ${a != null ? `data-a="${a}" data-b="${b}"` : ""} aria-pressed="${!!on}">${l}</button>`; }).join("")}</div>
  <form class="years-row yearsForm" novalidate>
    <input name="from" type="number" inputmode="numeric" min="1800" max="2030" placeholder="From" aria-label="From year" value="${S.years ? S.years[0] : ""}">
    <span class="to">to</span>
    <input name="to" type="number" inputmode="numeric" min="1800" max="2030" placeholder="To" aria-label="To year" value="${S.years ? S.years[1] : ""}">
    <button class="btn small primary" type="submit">Apply</button></form>
  <div class="err yearsErr" role="alert"></div>`;
function wireYears(root) {
  $$(".yearsForm", root).forEach(f => f.addEventListener("submit", e => {
    e.preventDefault();
    const err = f.parentElement.querySelector(".yearsErr");
    const a = f.from.value.trim(), b = f.to.value.trim();
    if (!a && !b) { err.textContent = ""; return setYears(null); }
    const x = +(a || b), y = +(b || a); // one box filled = that single year
    if (!(x >= 1800 && x <= 2030 && y >= 1800 && y <= 2030)) { err.textContent = "Years must be between 1800 and 2030."; return; }
    if (x > y) { err.textContent = "“From” must be the same as or before “To”."; return; }
    err.textContent = ""; setYears([x, y]);
  }));
}

function setYears(range) {
  S.years = range;
  // If the old map on screen falls outside the new years, switch to the dated map that fits (and covers the view),
  // with an Undo, instead of leaving an empty map. With nothing suitable, the map bar says why it is hidden.
  const cur = eraById(S.eraA);
  if (cur && !eraInYears(cur)) {
    const alt = suggestEra(), c = primary()?.getCenter();
    if (alt && c && eraCovers(alt, c.lng, c.lat)) {
      const prev = S.eraA;
      setEra(alt.id);
      toast(`Showing the ${alt.years} map: the ${cur.years} map is outside ${yearsText()}`, 6000, async () => setEra(prev));
    }
  }
  applyFilters(); applyEra();
  if (S.view.name === "home") render();
  if (S.view.nearby) renderNearby();
  if (!$("#popLayers").hidden) renderLayersPop(true);
}

// ================================================================ selection target, locate
function setTarget(t) { S.target = t; drawTarget(); }
function clearGrid() { setData("gridsq", empty()); setData("gridsq-c", empty()); }
function drawTarget() {
  const t = S.target;
  setData("sel", t && t.m > 0 ? circlePolygon(t.lon, t.lat, t.m) : empty());
  setData("spot", t && t.spot ? pointFC(t.lon, t.lat) : empty());
  drawLine();
}
function drawLine() {
  const t = S.target, me = S.me;
  setData("line", t && me ? { type: "Feature", geometry: { type: "LineString", coordinates: [[me.lon, me.lat], [t.lon, t.lat]] }, properties: {} } : empty());
}
function navHTML() {
  const t = S.target;
  if (!t || !S.me) return "";
  const { d, b } = distBearing([S.me.lon, S.me.lat], [t.lon, t.lat]);
  return `<div class="nav" aria-live="polite"><div class="arrow"><span style="display:grid;transform:rotate(${b}deg)">${icon.arrow}</span></div>
    <div class="grow"><div class="big">${fmtM(d)} · ${compass(b)} ${Math.round(b)}°</div>
    <div class="sub">from you (true north) · your GPS ±${fmtM(S.me.acc)}${t.m ? ` · target ±${fmtM(t.m)}` : ""}</div></div></div>`;
}
function refreshNav() { const el = $("#navSlot"); if (el) el.innerHTML = navHTML(); }
function drawMe() {
  setData("meacc", S.me ? circlePolygon(S.me.lon, S.me.lat, S.me.acc) : empty());
  setData("me", S.me ? pointFC(S.me.lon, S.me.lat) : empty());
}
function locate() {
  const btn = $("#locBtn");
  if (!("geolocation" in navigator)) return toast("This browser cannot share your position.");
  if (S.watchId != null) { if (S.me) primary().easeTo({ center: [S.me.lon, S.me.lat], zoom: Math.max(primary().getZoom(), 14), padding: mapPadding(), duration: reduceMotion ? 0 : 600 }); return; }
  toast("Finding your position…");
  let first = true;
  S.watchId = navigator.geolocation.watchPosition(pos => {
    S.me = { lon: pos.coords.longitude, lat: pos.coords.latitude, acc: pos.coords.accuracy || 0 };
    btn.setAttribute("aria-pressed", "true");
    drawMe(); drawLine(); refreshNav();
    if (first) {
      first = false; $("#toast").classList.remove("on");
      const inArea = S.me.lon > 102 && S.me.lon < 110 && S.me.lat > 8 && S.me.lat < 24;
      if (inArea) primary().easeTo({ center: [S.me.lon, S.me.lat], zoom: Math.max(primary().getZoom(), 13), padding: mapPadding(), duration: reduceMotion ? 0 : 600 });
      else toast(`You are outside Vietnam (${S.me.lat.toFixed(2)}, ${S.me.lon.toFixed(2)}). Distances to places still work.`, 5000);
    }
  }, err => {
    navigator.geolocation.clearWatch(S.watchId); S.watchId = null; btn.setAttribute("aria-pressed", "false");
    toast(err.code === 1 ? "Location is blocked. Allow it for this page to see distance and bearing." : "Could not get your position: " + err.message, 6000);
  }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
}

// ================================================================ map click
// Like Apple Maps: a click/tap on a symbol opens it; a pin is dropped on an empty spot by a long press (touch),
// a right-click, or (mouse) a single click that isn't the first half of a double-click zoom.
let spotTimer = null; // a pending single-click pin drop, cancelled if the click turns out to be a double-click
function onMapClick(m, e) {
  closePopovers();
  // A tap that lands while the sheet is still settling was meant for the sheet, not the map.
  if (!isDesk() && Date.now() - sheetMovedAt < 450) return;
  if (Date.now() - longPressAt < 600) return; // the long press already dropped a pin
  if (S.placing) { endPlacing(); openSiteForm(e.lngLat.lng, e.lngLat.lat, 100, "", ""); return; }
  if (S.draft) return; // editing a site: drag the pin instead
  if (S.view.name === "library") {
    // The card shows coverage: a tap picks the map under it (the most detailed one where sheets overlap).
    const id = footprintAt(m, e.point);
    if (id) libSelect(id, { fromMap: true });
    else if (S.view.sel) libSelect(null);
    else if (!isDesk() && S.sheet !== "peek") setSheet("peek");
    return;
  }
  const hits = hitTest(m, e.point);
  const sites = hits.filter(f => f.layer.id === "site-dot");
  if (sites.length) return openSite(+sites[0].properties.id);
  const seen = new Set(), recs = [];
  for (const f of hits) { const id = +f.properties.id; if (seen.has(id)) continue; seen.add(id); recs.push(f); }
  if (recs.length) {
    // Several records under one tap: open the nearest at once, with a "1 of N" stepper to go through the rest.
    const p = e.lngLat;
    const list = recs.map(propsOf).map(x => ({ ...x, d: distBearing([p.lng, p.lat], [x.lon, x.lat]).d }))
      .sort((a, b) => a.d - b.d || (a.date_start || "").localeCompare(b.date_start || "") || a.id - b.id).slice(0, 50);
    return openEvidence(list[0].id, list[0], { stack: list.length > 1 ? { recs: list, i: 0, lon: p.lng, lat: p.lat } : null });
  }
  const oe = e.originalEvent, touch = oe?.pointerType ? oe.pointerType === "touch" : coarse;
  if (touch) {
    // Touch: a tap on empty map just gets the sheet out of the way (pins need a long press).
    if (!isDesk() && S.sheet !== "peek") setSheet("peek");
    return;
  }
  clearTimeout(spotTimer);
  spotTimer = setTimeout(() => openSpot(e.lngLat.lng, e.lngLat.lat), 300);
}
let longPressAt = 0; // time of the last touch long press, so the click that follows it is ignored
function wireLongPress(m) {
  m.on("dblclick", () => clearTimeout(spotTimer));
  m.on("movestart", () => clearTimeout(spotTimer));
  m.on("contextmenu", e => { e.preventDefault?.(); if (!S.draft && !S.placing && S.view.name !== "library") { clearTimeout(spotTimer); openSpot(e.lngLat.lng, e.lngLat.lat); } });
  // Touch long press (MapLibre has no event for it): 550 ms without moving more than 10 px.
  const c = m.getCanvasContainer();
  let t = null, start = null;
  const cancel = () => { clearTimeout(t); t = null; };
  c.addEventListener("touchstart", ev => {
    cancel();
    if (ev.touches.length !== 1 || S.draft) return;
    const r = c.getBoundingClientRect(), tt = ev.touches[0];
    start = { x: tt.clientX, y: tt.clientY };
    t = setTimeout(() => {
      t = null;
      if (S.view.name === "library") return; // the card is about maps: no pins while it is open
      longPressAt = Date.now();
      const ll = m.unproject([start.x - r.left, start.y - r.top]);
      if (S.placing) { endPlacing(); openSiteForm(ll.lng, ll.lat, 100, "", ""); } else openSpot(ll.lng, ll.lat);
      navigator.vibrate?.(10);
    }, 550);
  }, { passive: true });
  c.addEventListener("touchmove", ev => { if (t && Math.hypot(ev.touches[0].clientX - start.x, ev.touches[0].clientY - start.y) > 10) cancel(); }, { passive: true });
  c.addEventListener("touchend", cancel, { passive: true });
  c.addEventListener("touchcancel", cancel, { passive: true });
}
// A record's properties plus lon / lat, preferring the cached copy (it has the derived fields) over a rendered
// feature's (MapLibre may round or drop properties).
function propsOf(f) {
  const id = +f.properties.id;
  const src = evById.get(id) || pnById.get(id);
  return src ? { ...src.properties, lon: src.geometry.coordinates[0], lat: src.geometry.coordinates[1] }
    : { ...f.properties, id, lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1] };
}
function openSpot(lon, lat) {
  clearGrid();
  setTarget({ lon, lat, m: 0, spot: true, label: "Dropped pin" });
  show("spot", { lon, lat });
}

// ================================================================ panel / bottom sheet
// On a phone the panel is a full-height sheet slid down by translateY so only sheetVisible() px show; the
// visible height is mirrored in --sheet-vis so the map bar and toasts can sit just above it.
function sheetVisible() {
  if (isDesk()) return 0;
  const p = $("#panel");
  if (S.sheet === "full") return p.offsetHeight;
  if (S.sheet === "half") return Math.round(Math.min(p.offsetHeight, innerHeight * 0.5));
  // Peek shows the search field, plus the card's title when a card is open (like Apple Maps).
  const head = S.view.name !== "home" && results.hidden ? $("#view .card-head") : null;
  return $("#grab").offsetHeight + $("#omniRow").offsetHeight + (head ? head.offsetHeight + 12 : 6);
}
let sheetMovedAt = 0; // when the sheet last changed detent (a tap right after it was meant for the sheet)
function setSheet(state) {
  const p = $("#panel"), moved = state !== S.sheet;
  if (moved) sheetMovedAt = Date.now();
  S.sheet = state;
  p.dataset.state = state;
  document.body.classList.toggle("sheet-full", !isDesk() && state === "full");
  if (isDesk()) { p.style.transform = ""; document.documentElement.style.setProperty("--sheet-vis", "0px"); $("#view").style.paddingBottom = ""; }
  else {
    const vis = sheetVisible(), hidden = p.offsetHeight - vis;
    p.style.transform = `translateY(${hidden}px)`;
    document.documentElement.style.setProperty("--sheet-vis", vis + "px");
    // The off-screen part of the sheet is padding at the end of its content, so everything can be scrolled into view at any height.
    $("#view").style.paddingBottom = state === "peek" ? "" : `${hidden + 24}px`;
    results.style.paddingBottom = state === "peek" ? "" : `${hidden + 12}px`;
  }
  $("#grab").setAttribute("aria-label", state === "full" ? "Collapse panel" : "Expand panel");
  $("#panelScroll").style.overflowY = !isDesk() && state === "peek" ? "hidden" : "";
  if (!isDesk() && state === "peek") $("#panelScroll").scrollTop = 0; // peek shows the search field and the card's title
  placeDivider();
  // The sheet covers part of the map, so what is "on screen" changed with it.
  if (moved) { renderMapbar(); libRefreshView(); }
}
// Dragging the grab handle or the search row moves the sheet; on release it snaps to a detent by distance and
// flick speed (px/ms). A tap on the handle steps through the detents.
(function wireSheet() {
  const p = $("#panel"), grab = $("#grab"), row = $("#omniRow");
  let start = null;
  const down = e => {
    if (isDesk() || e.target.closest("input, button:not(#grab), a, select, textarea")) return;
    start = { y: e.clientY, ty: p.offsetHeight - sheetVisible(), t: Date.now(), moved: false, el: e.currentTarget };
    p.classList.add("dragging"); document.body.classList.add("dragging-sheet");
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const move = e => {
    if (!start) return;
    const dy = e.clientY - start.y; if (Math.abs(dy) > 4) start.moved = true;
    const ty = Math.max(0, start.ty + dy);
    p.style.transform = `translateY(${ty}px)`;
    document.documentElement.style.setProperty("--sheet-vis", (p.offsetHeight - ty) + "px");
  };
  const up = e => {
    if (!start) return;
    p.classList.remove("dragging"); document.body.classList.remove("dragging-sheet");
    const dy = e.clientY - start.y, v = dy / Math.max(1, Date.now() - start.t);
    const order = ["peek", "half", "full"], i = order.indexOf(S.sheet);
    let next = S.sheet;
    if (!start.moved) { if (start.el === grab) next = order[(i + 1) % 3]; }
    else if (dy < -40 || v < -0.5) next = order[Math.min(2, i + (dy < -innerHeight * 0.35 ? 2 : 1))];
    else if (dy > 40 || v > 0.5) next = order[Math.max(0, i - (dy > innerHeight * 0.35 ? 2 : 1))];
    start = null; setSheet(next);
  };
  for (const el of [grab, row]) { el.addEventListener("pointerdown", down); el.addEventListener("pointermove", move); el.addEventListener("pointerup", up); el.addEventListener("pointercancel", up); }
  grab.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSheet(S.sheet === "full" ? "peek" : S.sheet === "half" ? "full" : "half"); }
  });
  let lastDesk = isDesk();
  addEventListener("resize", () => {
    if (isDesk() !== lastDesk) { lastDesk = isDesk(); setSheet("half"); }
    else setSheet(S.sheet);
    syncMapLeft(); placeDivider(); closePopovers();
  });
})();
function syncMapLeft() { document.documentElement.style.setProperty("--map-left", mapLeft() + "px"); }
function setPanelHidden(h) {
  S.panelHidden = h; document.body.classList.toggle("panel-hidden", h);
  const b = $("#panelBtn"); b.setAttribute("aria-pressed", !h); b.setAttribute("aria-label", h ? "Show sidebar" : "Hide sidebar"); b.title = b.getAttribute("aria-label");
  syncMapLeft(); placeDivider();
}

// Opens a card: VIEWS[name] rendered with data. push adds the current card to the Back stack; "home" clears it
// together with the selection.
function show(name, data = {}, { push = true } = {}) {
  closeResults(false);
  const same = S.view.name === name && (S.view.id ?? null) === (data.id ?? null);
  if (push && !same) S.history.push(S.view);
  if (S.history.length > 15) S.history.shift();
  if (name === "home") {
    S.history = []; setTarget(null); clearGrid(); clearNear(); if (q.value && document.activeElement !== q) clearSearch();
    // Cases can change elsewhere (another tab, the Add-to-case form): the home list is always fresh.
    loadCases().then(() => { if (S.view.name === "home" && !$("#view").contains(document.activeElement)) render(); });
  }
  else if (name !== "evidence" && name !== "grid") clearNear();
  if (S.draft && name !== "siteForm") cancelDraft();
  S.view = { name, ...data };
  render();
  // Closing a card returns keyboard focus to the control outside the panel that opened it (openLibrary).
  if (name === "home" && S.opener) {
    if (S.opener.isConnected && !S.opener.closest("[hidden]")) S.opener.focus({ preventScroll: true });
    S.opener = null;
  }
  // Phone: a card opens at half height (map stays visible above it), like Apple Maps.
  if (name !== "home" && !isDesk() && S.sheet !== "half" && document.activeElement !== q) setSheet("half");
  if (isDesk() && S.panelHidden && name !== "home") setPanelHidden(false);
  $("#panelScroll").scrollTop = 0;
}
function back() {
  if (S.draft) cancelDraft();
  const prev = S.history.pop();
  if (!prev || prev.name === "home") return show("home", {}, { push: false });
  S.view = prev; clearNear(); render();
  if (prev.name === "evidence" && prev.p) setTarget({ lon: prev.p.lon, lat: prev.p.lat, m: prev.p.uncertainty_m });
  if (prev.name === "spot") setTarget({ lon: prev.lon, lat: prev.lat, m: 0, spot: true });
}
// Titles are clamped to two lines so the action row below never moves; the full text is one click away.
function headHTML(title, sub = "", full = "") {
  const canBack = S.history.length && S.history.at(-1).name !== "home";
  return `<div class="card-head"><div class="grow"><h2 class="card-title clamp" id="cardTitle"${full ? ` title="${esc(full)}"` : ""}>${title}</h2>${sub ? `<div class="card-sub">${sub}</div>` : ""}</div>
    <div class="head-btns">${canBack ? `<button class="round flat small" data-act="back" aria-label="Back">${icon.back}</button>` : ""}
    <button class="round flat small" data-act="home" aria-label="Close">${icon.close}</button></div></div>`;
}

// Card renderers by view name: VIEWS.x(view) returns HTML; the optional VIEWS.x.after(view) runs once it is in
// the page (listeners, follow-up fetches).
const VIEWS = {};
function render() {
  const v = S.view, fn = VIEWS[v.name] || VIEWS.home;
  $("#view").innerHTML = fn(v);
  wire($("#view"));
  const t = $("#cardTitle");
  if (t && t.scrollHeight > t.clientHeight + 2) {
    t.classList.add("expandable"); t.tabIndex = 0; t.setAttribute("role", "button"); t.setAttribute("aria-expanded", "false");
    const flip = () => { t.classList.toggle("clamp"); t.setAttribute("aria-expanded", !t.classList.contains("clamp")); };
    t.addEventListener("click", flip); t.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
  }
  fn.after?.(v);
  updateCounts();
  showFootprints(v.name === "library");
  document.body.classList.toggle("lib-open", v.name === "library");
  $("#settingsBtn")?.setAttribute("aria-pressed", v.name === "settings");
  renderMapbar(); // its hints depend on whether the library card is open
  if (!isDesk()) requestAnimationFrame(() => setSheet(S.sheet));
}
// Connects [data-act] elements inside root to ACTIONS (once per element), plus the layer-toggle rows.
function wire(root) {
  // Round action buttons are named by their caption (explicitly, for screen readers that skip the icon span).
  $$(".act:not([aria-label])", root).forEach(el => el.setAttribute("aria-label", el.textContent.trim().replace(/\s+/g, " ")));
  $$("[data-act]", root).forEach(el => {
    const act = ACTIONS[el.dataset.act];
    if (act && !el._wired) { el._wired = true; el.addEventListener(el.tagName === "SELECT" || el.type === "range" ? "input" : "click", e => act(el, e)); }
  });
  // Option/Alt-click a layer row = show only that layer (the "Only" button does the same).
  $$(".kind-row[data-kind]", root).forEach(el => { if (!el._wired) { el._wired = true; el.addEventListener("click", e => e.altKey ? onlyKind(el.dataset.kind) : toggleKind(el.dataset.kind)); } });
  $$("[data-only]", root).forEach(el => { if (!el._wired) { el._wired = true; el.addEventListener("click", () => onlyKind(el.dataset.only)); } });
}
async function copyText(text, what = "") {
  try { await navigator.clipboard.writeText(text); toast(`Copied ${what ? what + " " : ""}${text}`); }
  catch { toast("Copying is not available here. Select the text instead."); }
}
// Button handlers by data-act name; each gets the element (its data-* attributes carry the arguments) and event.
const ACTIONS = {
  back: () => back(),
  home: () => show("home"),
  copy: el => copyText(el.dataset.text, el.dataset.what || ""),
  go: el => { if (!isDesk()) setSheet("peek"); flyToTarget(+el.dataset.lon, +el.dataset.lat, +el.dataset.m || 0); },
  rec: el => { const id = +el.dataset.id; openEvidence(id, evById.has(id) || pnById.has(id) ? propsOf({ properties: { id } }) : null, { fly: true }); },
  siteHere: el => openSiteForm(+el.dataset.lon, +el.dataset.lat, +el.dataset.m || 100, el.dataset.name || "", el.dataset.why || ""),
  openCase: async el => { await selectCase(+el.dataset.id); show("case", { id: +el.dataset.id }); },
  preset: el => setYears(el.dataset.a ? [+el.dataset.a, +el.dataset.b] : null),
  layers: () => togglePopover("popLayers", $("#layersBtn")),
  setEra: el => setEra(el.dataset.id),
  fit: el => { const e = eraById(el.dataset.id); if (e) primary().fitBounds([[e.bounds[0], e.bounds[1]], [e.bounds[2], e.bounds[3]]], { padding: mapPadding(), duration: reduceMotion ? 0 : 600 }); },
  theme: el => setTheme(el.dataset.theme),
};

// ---------------------------------------------------------------- Home
// Recently viewed: up to 8 records and grid squares, newest first, kept in localStorage.
function recent() { return store.get("recent", []); }
function pushRecent(p) {
  const r = recent().filter(x => x.id !== p.id);
  r.unshift({ id: p.id, title: p.title, kind: p.kind, date: p.date_start || null, lon: p.lon, lat: p.lat, src: p.source || null, rid: p.source_record_id || null });
  store.set("recent", r.slice(0, 8));
}
// Grid squares looked up go in the same list (id "g:<ref>", so the same square isn't listed twice).
function pushRecentGrid(ref, datum) {
  const id = "g:" + ref, r = recent().filter(x => x.id !== id);
  r.unshift({ id, kind: "gridref", title: ref, datum });
  store.set("recent", r.slice(0, 8));
}
// Second line of a recent row: what it is, when, and the record id (identical titles stay distinguishable).
const recentMeta = r => r.kind === "gridref" ? `Grid square · ${r.datum === "wgs84" ? "WGS84 / GPS" : "Indian 1960"}`
  : [kindOne(r), r.date ? fmtDay(r.date) : "", r.rid ? `${srcShort(r.src).split(" ")[0]} ${r.rid}` : ""].filter(Boolean).join(" · ");
VIEWS.home = v => {
  const rec = recent();
  homeLastSig = homeSig();
  return `
    ${welcomeHTML()}
    ${S.kase ? `<div class="case-pill"><span class="sw site"></span><div class="grow"><span class="small-t muted">Open case</span><b>${esc(S.kase.name)}</b></div>
      <button class="btn small" data-act="openCase" data-id="${S.kase.id}">Open</button></div>` : ""}
    <h3 class="section-title">Cases <span class="right"><button class="linkish" data-act="newCaseToggle">New case</button></span></h3>
    <div class="group">
      <form id="newCase" class="inner stack" hidden>
        <label class="field"><span>Name</span><input type="text" name="name" required placeholder="e.g. UH-1 loss, Cao Lãnh, May 1968"></label>
        <label class="field"><span>One-line summary (optional)</span><input type="text" name="summary"></label>
        <div class="row"><button class="btn primary small" type="submit">Create case</button><button class="btn small" type="button" data-act="newCaseToggle">Cancel</button></div>
        <div class="err" role="alert"></div></form>
      <ul class="list">${S.cases.filter(c => c.id !== S.kase?.id).length ? S.cases.filter(c => c.id !== S.kase?.id).map(c => `<li><span class="sw site"></span>
        <button class="main" data-act="openCase" data-id="${c.id}"><span class="t">${esc(c.name)}</span>
        <span class="m">${esc(c.summary || "No summary")}</span></button>${icon.chevR.replace("<svg", '<svg style="color:var(--faint)"')}</li>`).join("")
        : S.kase ? "" : `<li class="empty">No cases yet. A case gathers evidence, candidate sites and notes for one investigation, and exports them for a GPS.</li>`}</ul>
    </div>
    ${rec.length ? `<h3 class="section-title">Recently viewed <span class="right"><button class="linkish" data-act="clearRecent">Clear</button></span></h3>
      <div class="group"><ul class="list">${rec.map(r => r.kind === "gridref"
        ? `<li><span class="sym" aria-hidden="true" style="color:#b45309">${icon.grid}</span><button class="main" data-act="recGrid" data-ref="${esc(r.title)}" data-datum="${esc(r.datum || "indian1960")}">
          <span class="t mono">${esc(r.title)}</span><span class="m">${esc(recentMeta(r))}</span></button></li>`
        : `<li>${symSVG(r.kind)}<button class="main" data-act="rec" data-id="${r.id}">
        <span class="t">${esc(shortTitle(r.title))}</span><span class="m">${esc(recentMeta(r))}</span></button></li>`).join("")}</ul></div>` : ""}
    <h3 class="section-title">On the map</h3>
    <div class="group"><button class="summary-row" data-act="layers" aria-haspopup="dialog">
      <span class="sym heat" aria-hidden="true"></span>
      <span class="grow"><span class="t">${esc(filterWords().kinds || "All evidence layers")}</span>
        <span class="m">${esc(S.years ? "Years " + yearsText() : "All years")} · ${esc(S.eraA ? eraTitle(S.eraA) + " map" + (eraInYears(eraById(S.eraA)) ? "" : " (hidden: outside years)") : "No old map")}</span></span>
      ${icon.chevR}</button>
      <button class="summary-row" data-act="openLibrary"><span class="row-ico" aria-hidden="true">${icon.map}</span>
        <span class="grow"><span class="t">Old maps</span><span class="m">${esc(libHomeLine())}</span></span>${icon.chevR}</button>
      <button class="summary-row" data-act="openRecords"><span class="row-ico" aria-hidden="true">${S.rec.busy ? '<span class="spin"></span>' : icon.doc}</span>
        <span class="grow"><span class="t">Records</span><span class="m" data-rec-line>${esc(recHomeLine())}</span></span>${icon.chevR}</button></div>
    <div class="foot-note"><span>${coarse ? "Touch and hold the map to drop a pin. " : "Click an empty spot (or right-click) to drop a pin. "}Runs on your computer only. Records can be wrong: check each source.</span></div>
    ${v.quit ? `<div class="confirm-box calm quit-box" role="alertdialog" aria-label="Quit Map Vietnam">
        <span>Quit Map Vietnam? The map stops working in this window until you start it again.${S.rec.busy || libBusyMaps().length ? " What is being added now stops; what was already downloaded is kept." : ""}</span>
        <div class="row"><button class="btn small primary" data-act="quit">Quit</button><button class="btn small" data-act="quitCancel">Cancel</button></div></div>`
      : `<div class="home-links"><button class="linkish quit-link" data-act="openSettings">Settings</button><button class="linkish quit-link" data-act="quitAsk">Quit Map Vietnam</button></div>`}`;
};
VIEWS.home.after = () => {
  $("#newCase")?.addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target, err = f.querySelector(".err");
    const name = f.name.value.trim(); if (!name) { err.textContent = "Give the case a name."; return; }
    try {
      const { id } = await api("/api/cases", { method: "POST", body: { name, summary: f.summary.value.trim() } });
      await loadCases(); await selectCase(id); toast("Case created"); show("case", { id });
    } catch (x) { err.textContent = x.message; }
  });
};
// "2 on this computer · adding 1", or an invitation when there are none yet.
function libHomeLine() {
  const have = S.lib.maps.filter(m => m.state === "installed").length || S.catalog.length, busy = libBusyMaps().length;
  const parts = [have ? `${have} on this computer` : "Add old maps of any area", busy ? `adding ${busy}` : ""];
  return parts.filter(Boolean).join(" · ");
}
ACTIONS.newCaseToggle = () => { const f = $("#newCase"); if (!f) return; f.hidden = !f.hidden; if (!f.hidden) f.name.focus(); };
ACTIONS.clearRecent = () => { store.set("recent", []); render(); };
ACTIONS.recGrid = el => { q.value = el.dataset.ref; $("#qClear").hidden = false; openGridRef(el.dataset.ref, el.dataset.datum); };

// ---------------------------------------------------------------- Evidence (place card)
async function fetchEvidence(id) {
  try {
    const fc = await api(`/api/evidence?ids=${id}&limit=1`);
    const f = fc.features[0]; if (!f) return null;
    prep(f);
    return { ...f.properties, lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1], full: true };
  } catch (e) { toast(e.message); return null; }
}
// Opens a record's card at once from its slim properties (when known), then fills it in from the full record.
// stack carries the other records under the same click for the "1 of N" stepper; replace swaps the card without
// adding a Back step.
async function openEvidence(id, slim, { fly = false, stack = null, replace = false } = {}) {
  hoverPoint();
  if (slim) setTarget({ lon: slim.lon, lat: slim.lat, m: slim.uncertainty_m });
  if (slim && fly) flyToTarget(slim.lon, slim.lat, slim.uncertainty_m);
  clearGrid();
  show("evidence", { id, p: slim ? { ...slim, id } : null, nearby: true, stack }, { push: !replace });
  const v = S.view;
  const p = await fetchEvidence(id);
  if (!p) return;
  pushRecent(p);
  if (S.view !== v) return;
  v.p = p;
  setTarget({ lon: p.lon, lat: p.lat, m: p.uncertainty_m });
  render();
  if (!slim) flyToTarget(p.lon, p.lat, p.uncertainty_m);
}
// The coordinate as the source printed it. The NGA gazetteer (GNS) publishes degrees-minutes-seconds and MGRS; the
// decimal pair stored for it is derived, so show the DMS it gave (and its MGRS as a second form).
function asWritten(p) {
  const d = p.description || "";
  const dms = p.source === "gns" && d.match(/DMS (-?\d+):(\d+):(\d+) (-?\d+):(\d+):(\d+)/);
  if (dms) {
    const f = (dg, mi, se, pos, neg) => `${Math.abs(+dg)}°${mi}′${se}″${+dg < 0 ? neg : pos}`;
    const mgrs = d.match(/MGRS ([0-9]{1,2}[A-Z]{3}[0-9]+)/);
    return { main: `${f(dms[1], dms[2], dms[3], "N", "S")} ${f(dms[4], dms[5], dms[6], "E", "W")}`, also: mgrs ? mgrs[1] : "" };
  }
  return { main: p.original_coord || "—", also: "" };
}
// A description; THOR's "a; b; c" field lists read better as bullets.
function descHTML(d) {
  const s = readable(d || "");
  if (!s) return "";
  const parts = s.split(/;\s+/).map(x => x.trim()).filter(Boolean);
  return parts.length >= 3 ? `<ul class="desc">${parts.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="desc-p">${esc(s)}</p>`;
}
VIEWS.evidence = v => {
  const p = v.p;
  if (!p) return headHTML("Loading…") + `<p class="muted">Fetching the record…</p>`;
  const ctx = contextOf(p);
  const inCase = S.kase?.items.some(i => i.id === p.id);
  const sub = `${symSVG(p.kind, 16)}<span>${[kindOne(p), p.date_start ? fmtDate(p.date_start, p.date_end) : "", ctx].filter(Boolean).map(esc).join(" · ")}</span>`;
  const title = shortTitle(p.title);
  const loading = !p.full;
  const strike = p.kind === "air_strike";
  const st = v.stack;
  const stepper = st ? `<div class="stepper" role="group" aria-label="Records at this spot">
      <button class="round flat small" data-act="step" data-d="-1" aria-label="Previous record" ${st.i === 0 ? "disabled" : ""}>${icon.back}</button>
      <span class="step-t" aria-live="polite"><b>${st.i + 1} of ${st.recs.length}</b> records here</span>
      <button class="round flat small" data-act="step" data-d="1" aria-label="Next record" ${st.i === st.recs.length - 1 ? "disabled" : ""}>${icon.chevR}</button>
      <button class="linkish small-t" data-act="stackList">List all</button></div>` : "";
  return `${headHTML(esc(title), sub, title)}
    ${stepper}
    <div class="actions five">
      <button class="act primary" data-act="addToCase" ${loading ? "disabled" : ""} aria-pressed="${!!inCase}"><span class="ci">${inCase ? icon.check : icon.plus}</span><span>${inCase ? "In case" : "Add to case"}</span></button>
      <button class="act" data-act="siteHere" data-lon="${p.lon}" data-lat="${p.lat}" data-m="${Math.round(Math.max(p.uncertainty_m || 0, 50))}"
        data-why="Near ${esc(srcShort(p.source))} record ${esc(p.source_record_id || "")} (${esc(kindOne(p))}${p.date_start ? ", " + esc(fmtDay(p.date_start)) : ""})"><span class="ci">${icon.pin}</span><span>Place site</span></button>
      <button class="act" data-act="toNearby"><span class="ci">${icon.near}</span><span>Nearby</span></button>
      <button class="act" data-act="copyGrid" ${loading ? "disabled" : ""}><span class="ci">${icon.grid}</span><span>Copy grid ref</span></button>
      ${safeUrl(p.url) ? `<a class="act" href="${esc(p.url)}" target="_blank" rel="noopener"><span class="ci">${icon.ext}</span><span>Source</span></a>`
        : `<button class="act" data-act="toSource" ${loading ? "disabled" : ""}><span class="ci">${icon.doc}</span><span>Source</span></button>`}
    </div>
    <div id="caseSlot"></div>
    <div id="navSlot">${navHTML()}</div>
    <div class="group">
      ${loading ? `<div class="kv-row"><span class="muted">Loading details…</span></div>` : `
      ${(() => { const w = asWritten(p); return `<div class="kv-row"><span class="k">As written in the source</span><span class="v"><span class="mono">${esc(w.main)}</span></span>
        <span class="v2">${esc(datumWords(p.original_datum))}${w.also ? ` · also given as <span class="mono">${esc(w.also)}</span>` : ""}</span></div>`; })()}
      <div class="kv-row"><span class="k">Position on today’s map (WGS84)</span><span class="v mono">${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}</span>
        <span class="inline-btns"><button class="linkish small-t" data-act="copy" data-what="position" data-text="${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}">Copy</button>
        <button class="linkish small-t" data-act="go" data-lon="${p.lon}" data-lat="${p.lat}" data-m="${p.uncertainty_m}">Show on map</button></span></div>
      <div class="kv-row"><span class="k">Accuracy</span><span class="v">Within <b>${fmtM(p.uncertainty_m)}</b> of the point</span><span class="v2">Drawn as a ring at true ground scale.</span></div>
      <div class="kv-row"><span class="k">Date</span><span class="v">${esc(fmtDate(p.date_start, p.date_end))}</span></div>
      <div class="kv-row" id="srcRow"><span class="k">Source</span><span class="v">${esc(S.sources[p.source]?.name || srcShort(p.source))}</span>
        <span class="v2">Record <span class="mono">${esc(p.source_record_id)}</span>${S.sources[p.source]?.retrieved_at ? ` · retrieved ${esc(fmtDay(S.sources[p.source].retrieved_at.slice(0, 10)))}` : ""}</span>
        ${safeUrl(p.url) ? `<span class="inline-btns"><a class="small-t" href="${esc(p.url)}" target="_blank" rel="noopener">Open source record</a></span>` : ""}</div>`}
    </div>
    ${p.description ? `<h3 class="section-title">Description</h3><div class="group"><div class="inner" style="padding-top:12px">${descHTML(p.description)}</div></div>` : ""}
    ${strike ? `<p class="quiet-note">${icon.warn}<span>Strike target: unexploded ordnance may remain within about ${fmtM(p.uncertainty_m || 1000)}. Don’t dig without UXO clearance.</span></p>` : ""}
    ${nearbyShell()}`;
};
let stepFocus = null; // keep keyboard focus on the stepper across the card's re-renders
VIEWS.evidence.after = v => {
  if (stepFocus) { ($(`.stepper [data-d="${stepFocus}"]:not(:disabled)`) || $(".stepper [data-act=step]:not(:disabled)"))?.focus(); if (v.p?.full) stepFocus = null; }
  if (v.p) renderNearby();
};
// Jump (no slow smooth scroll) to a section of the open card.
function jumpTo(el) {
  if (!el) return;
  if (!isDesk() && S.sheet !== "full") setSheet("full");
  const sc = $("#panelScroll");
  sc.scrollTop += el.getBoundingClientRect().top - sc.getBoundingClientRect().top - 8;
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
}
ACTIONS.step = el => {
  const st = S.view.stack; if (!st) return;
  const i = Math.max(0, Math.min(st.recs.length - 1, st.i + +el.dataset.d)); if (i === st.i) return;
  const p = st.recs[i];
  stepFocus = el.dataset.d;
  openEvidence(p.id, p, { stack: { ...st, i }, replace: true });
};
ACTIONS.stackList = () => { const st = S.view.stack; if (st) show("multi", { recs: st.recs, lon: st.lon, lat: st.lat }); };
ACTIONS.toNearby = () => jumpTo($("#nearby"));
ACTIONS.toSource = () => { const r = $("#srcRow"); if (!r) return; r.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" }); r.classList.remove("flash"); void r.offsetWidth; r.classList.add("flash"); };
// Copies the source's own grid reference when it is MGRS, else the Indian 1960 MGRS computed by the server.
ACTIONS.copyGrid = async () => {
  const p = S.view.p; if (!p) return;
  if (p.original_coord && /^\d{1,2}[C-X]?\s*[A-Z]{2}\s*\d{4,10}$/i.test(p.original_coord.replace(/\s/g, ""))) return copyText(p.original_coord, "grid ref");
  try { const r = await api(`/api/lonlat?lon=${p.lon}&lat=${p.lat}`); copyText(r.mgrs_indian1960, "Indian 1960 grid ref"); } catch (e) { toast(e.message); }
};
ACTIONS.addToCase = async el => {
  const p = S.view.p; if (!p) return;
  if (S.kase) {
    if (S.kase.items.some(i => i.id === p.id)) {
      const cid = S.kase.id;
      S.kase = await api(`/api/cases/${cid}/items/${p.id}`, { method: "DELETE" });
      toast(`Removed from “${S.kase.name}”`, 6000, async () => { S.kase = await api(`/api/cases/${cid}/items`, { method: "POST", body: { evidence_id: p.id } }); drawCase(); render(); });
    } else {
      S.kase = await api(`/api/cases/${S.kase.id}/items`, { method: "POST", body: { evidence_id: p.id } });
      toast(`Added to “${S.kase.name}”`);
    }
    drawCase(); render(); return;
  }
  // No case open: choose one inline (nothing is preselected, so it can't go to the wrong case by accident).
  const slot = $("#caseSlot");
  if (slot.innerHTML) { slot.innerHTML = ""; return; }
  await loadCases();
  slot.innerHTML = `<form class="group inner stack" id="caseAddForm" style="padding-top:12px">
    <label class="field"><span>Add to which case?</span><select name="cid" required>
      <option value="" selected disabled>Choose a case…</option>${S.cases.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}<option value="new">+ New case…</option></select></label>
    <label class="field" id="ncWrap" hidden><span>New case name</span><input type="text" name="name" placeholder="e.g. Crash near Mỹ Thọ 1968"></label>
    <div class="row"><button class="btn primary small" type="submit">${icon.plus} Add</button><button class="btn small" type="button" id="caseAddCancel">Cancel</button></div>
    <div class="err" role="alert"></div></form>`;
  const f = $("#caseAddForm");
  f.cid.addEventListener("change", () => { $("#ncWrap").hidden = f.cid.value !== "new"; if (f.cid.value === "new") f.name.focus(); });
  $("#caseAddCancel").addEventListener("click", () => { slot.innerHTML = ""; });
  f.cid.focus();
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const err = f.querySelector(".err");
    try {
      let cid = f.cid.value;
      if (!cid) throw new Error("Choose a case first.");
      if (cid === "new") {
        const n = f.name.value.trim(); if (!n) throw new Error("Give the new case a name.");
        cid = (await api("/api/cases", { method: "POST", body: { name: n, summary: "" } })).id; await loadCases();
      }
      await selectCase(+cid);
      S.kase = await api(`/api/cases/${S.kase.id}/items`, { method: "POST", body: { evidence_id: p.id } });
      toast(`Added to “${S.kase.name}”`); drawCase(); render();
    } catch (x) { err.textContent = x.message; }
  });
};

// ---- Telling similar records apart: full records fetched in one batch, a one-line "what exactly" detail.
const fullById = new Map();
async function fetchFull(ids) {
  const need = [...new Set(ids)].filter(id => !fullById.has(id));
  for (let i = 0; i < need.length; i += 100) {
    const part = need.slice(i, i + 100);
    try { const fc = await api(`/api/evidence?ids=${part.join(",")}&limit=${part.length}`); for (const f of fc.features) fullById.set(f.properties.id, f.properties); }
    catch { /* rows just stay less detailed */ }
  }
}
// What tells similar records apart: { what: "USAF AC-47 · 31 × MK-24", rec: "THOR 1280753" } (what is empty for non-strikes).
function detailParts(id) {
  const p = fullById.get(id); if (!p) return null;
  const bits = [];
  if (p.kind === "air_strike" && p.description) {
    const parts = decodeEntities(p.description).split(/;\s*/);
    const craft = (parts[0] || "").replace(/^(UNITED STATES OF AMERICA|VIETNAM \(SOUTH\)|SOUTH VIETNAM|AUSTRALIA|LAOS|KOREA \(SOUTH\))\s*/i, "").trim();
    if (craft) bits.push(craft);
    const ord = parts.find(x => /^\d+\s*x\s+/i.test(x) && !/^0\s*x/i.test(x));
    if (ord) bits.push(ord.replace(/\s*x\s+/i, " × "));
    const cs = parts.map(x => x.match(/^operation\s.*?\s-\s(.+)$/i)?.[1]).find(Boolean); // call sign, e.g. SPOOKY&81
    if (cs && !/VIETNAM|LAOS|CAMBODIA|THAILAND|UNKNOWN/i.test(cs)) bits.push(cs); // (some give the area, not a call sign)
  }
  return { what: readable(bits.join(" · ")), rec: `${srcShort(p.source).split(" ")[0]} ${p.source_record_id || ""}`.trim() };
}
function detailOf(id) { const d = detailParts(id); return d ? [d.what, d.rec].filter(Boolean).join(" · ") : ""; }
// A list title with an optional "×N" count that stays visible however long the title is.
const badgeTitle = (t, n) => n > 1 ? `<span class="t has-badge"><span class="tt">${esc(t)}</span><span class="times">×${n}</span></span>` : `<span class="t">${esc(t)}</span>`;
// Identical sorties (same title, day and position) collapse into one row "×N".
function groupRecs(items) {
  const groups = new Map();
  for (const x of items) {
    const k = [x.p.kind, x.p.title, x.p.date_start, x.lon.toFixed(5), x.lat.toFixed(5)].join("|");
    if (!groups.has(k)) groups.set(k, { ...x, ids: [] });
    groups.get(k).ids.push(x.p.id);
  }
  return [...groups.values()];
}
function hoverPoint(lon, lat) { setData("hover", lon == null ? empty() : pointFC(lon, lat)); }
function wireHover(root) {
  $$("[data-hlon]", root).forEach(el => {
    if (el._hov) return; el._hov = true;
    const on = () => hoverPoint(+el.dataset.hlon, +el.dataset.hlat), off = () => hoverPoint();
    el.addEventListener("mouseenter", on); el.addEventListener("mouseleave", off);
    el.addEventListener("focus", on); el.addEventListener("blur", off);
  });
}

// ---- Nearby (shared by evidence + grid cards)
const NEAR_R = [250, 500, 1000, 2000, 5000]; // radius choices, metres
function nearbyShell() {
  return `<section id="nearby" aria-label="Nearby evidence"><h3 class="section-title">Nearby <span class="right" id="nearCount"></span></h3>
    <div class="near-tools"><div class="chips" role="group" aria-label="Nearby radius">${NEAR_R.map(r => `<button class="chip" data-act="nearR" data-r="${r}" aria-pressed="${S.nearR === r}">${fmtM(r)}</button>`).join("")}</div>
      <label class="switch"><input type="checkbox" data-act="nearPN" ${S.nearPN ? "checked" : ""}><span class="tog" aria-hidden="true"></span><span>Include place names</span></label></div>
    <div class="group"><ul class="list" id="nearList"><li class="empty">Looking…</li></ul><div id="nearMoreSlot"></div></div></section>`;
}
ACTIONS.nearR = el => { S.nearR = +el.dataset.r; store.set("nearR", S.nearR); $$('[data-act="nearR"]').forEach(b => b.setAttribute("aria-pressed", +b.dataset.r === S.nearR)); renderNearby(); };
ACTIONS.nearPN = el => { S.nearPN = el.checked; renderNearby(); };
let nearReq = null; // { key, promise }: one request per distinct query, shared by re-renders
function nearCentre() { const v = S.view; return v.name === "evidence" && v.p ? { lon: v.p.lon, lat: v.p.lat, self: v.p.id } : v.name === "grid" && v.r ? { lon: v.r.lon, lat: v.r.lat } : null; }
function drawNearRing() {
  const c = nearCentre();
  const f = c && $("#nearby") ? circlePolygon(c.lon, c.lat, S.nearR, 96) : null;
  if (f) f.properties.label = `Nearby · ${fmtM(S.nearR)}`;
  setData("near", f || empty());
}
function clearNear() { setData("near", empty()); hoverPoint(); }
// Lists records within S.nearR of the open record or grid square, nearest first, 25 at a time. The server is
// asked for the bounding box; the exact circle is applied here.
async function renderNearby(showAll = false) {
  const c = nearCentre(), list = $("#nearList"); if (!c || !list) return;
  drawNearRing();
  const R = S.nearR, dLat = R / 110574, dLon = R / (111320 * Math.cos(c.lat * Math.PI / 180));
  const kinds = S.nearPN ? Object.keys(KINDS) : EV_KINDS;
  const q = new URLSearchParams({ bbox: [c.lon - dLon, c.lat - dLat, c.lon + dLon, c.lat + dLat].map(v => v.toFixed(5)).join(","), kinds: kinds.join(","), slim: 1, limit: 5000 });
  if (S.years) { q.set("from", S.years[0]); q.set("to", S.years[1]); }
  const key = q.toString();
  if (nearReq?.key !== key) nearReq = { key, promise: api(`/api/evidence?${key}`) };
  const v = S.view;
  let fc;
  try { fc = await nearReq.promise; }
  catch (e) { if (list.isConnected) list.innerHTML = `<li class="empty">Nearby search failed: ${esc(e.message)}</li>`; return; }
  if (S.view !== v || !list.isConnected || nearReq.key !== key) return;
  const items = fc.features.map(f => ({ p: f.properties, lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1] }))
    .filter(x => x.p.id !== c.self && !(S.hidden.has(x.p.kind) && x.p.kind !== "placename"))
    .map(x => ({ ...x, d: distBearing([c.lon, c.lat], [x.lon, x.lat]).d })).filter(x => x.d <= R).sort((a, b) => a.d - b.d || (a.p.date_start || "").localeCompare(b.p.date_start || ""));
  // Nearby rows open by id; keep their slim props reachable for an instant card.
  for (const x of items) if (!evById.has(x.p.id) && !pnById.has(x.p.id)) (x.p.kind === "placename" ? pnById : evById).set(x.p.id, prep({ type: "Feature", geometry: { type: "Point", coordinates: [x.lon, x.lat] }, properties: x.p }));
  const groups = groupRecs(items);
  $("#nearCount").textContent = `${fmtInt(items.length)}${fc.truncated ? "+" : ""} within ${fmtM(R)}`;
  const shown = showAll ? groups : groups.slice(0, 25);
  const paint = () => {
    // Each row: the title (the "×N" badge never gets cut off), then date and what tells it apart from its neighbours
    // (aircraft, ordnance, call sign, record id).
    list.innerHTML = groups.length ? shown.map((g, i) => {
      const n = g.ids.length, det = detailOf(g.p.id);
      const m = [g.p.date_start ? fmtDate(g.p.date_start, g.p.date_end) : "Undated", det ? det + (n > 1 ? ` +${n - 1} more` : "") : `±${fmtM(g.p.uncertainty_m || 0)}`].join(" · ");
      return `<li>${symSVG(g.p.kind)}
      <button class="main" ${n > 1 ? `data-group="${i}"` : `data-act="rec" data-id="${g.p.id}"`} data-hlon="${g.lon}" data-hlat="${g.lat}">
        ${badgeTitle(shortTitle(g.p.title), n)}<span class="m">${esc(m)}</span></button>
      <span class="d">${fmtM(g.d)}</span></li>`; }).join("")
      : `<li class="empty">Nothing within ${fmtM(R)}${S.years ? ` in ${esc(yearsText())}` : ""}.${S.nearPN ? "" : " Place names are not included."}</li>`;
    wire(list.parentElement); wireHover(list);
    // A group opens its first record (with a "1 of N" stepper) and the map goes to it.
    $$("[data-group]", list).forEach(b => b.addEventListener("click", () => {
      const g = shown[+b.dataset.group];
      const recs = g.ids.map(id => ({ ...propsOf({ properties: { id } }), d: g.d }));
      openEvidence(recs[0].id, recs[0], { fly: true, stack: { recs, i: 0, lon: g.lon, lat: g.lat } });
    }));
    $("#nearMoreSlot").innerHTML = groups.length > shown.length ? `<button class="list-more" id="nearMore">Show all ${fmtInt(groups.length)}</button>` : "";
    $("#nearMore")?.addEventListener("click", () => renderNearby(true));
  };
  paint();
  // Then add the aircraft / ordnance / record id that tell similar rows apart.
  const single = shown.filter(g => g.p.kind !== "placename").map(g => g.p.id);
  if (single.some(id => !fullById.has(id))) { await fetchFull(single); if (S.view === v && list.isConnected && nearReq.key === key) paint(); }
}

// ---------------------------------------------------------------- Several records under one click
VIEWS.multi = v => {
  const recs = [...v.recs];
  const dates = new Set(recs.map(p => p.date_start || "")), kinds = new Set(recs.map(p => p.kind));
  const oneDate = dates.size === 1, p0 = recs[0];
  // Group by what happened (title), each group sorted by date then record; repeated sorties read as "×N".
  const groups = new Map();
  for (const p of recs.sort((a, b) => (a.d - b.d) || (a.date_start || "").localeCompare(b.date_start || "") || a.id - b.id)) {
    const k = shortTitle(p.title); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(p);
  }
  const single = groups.size === 1;
  // The count goes in the subtitle, so a long title never pushes "×12" onto a line of its own.
  const title = single ? [...groups.keys()][0] : `${recs.length} records here`;
  const sub = `${kinds.size === 1 ? symSVG(p0.kind, 16) : ""}<span>${esc([kinds.size === 1 ? kindOne(p0) : "", oneDate && p0.date_start ? fmtDate(p0.date_start, p0.date_end) : "",
    single ? `${recs.length} records here` : `${groups.size} kinds of record`].filter(Boolean).join(" · "))}</span>`;
  // The heading (or the card title) already says what happened, so each row leads with what tells it apart.
  const row = p => {
    const d = detailParts(p.id);
    const t = d?.what || (single && d ? d.rec : shortTitle(p.title));
    const m = [oneDate ? "" : (p.date_start ? fmtDate(p.date_start, p.date_end) : "Undated"), d && t !== d.rec ? d.rec : "", `±${fmtM(p.uncertainty_m || 0)}`].filter(Boolean).join(" · ");
    return `<li>${symSVG(p.kind)}<button class="main" data-act="rec" data-id="${p.id}" data-hlon="${p.lon}" data-hlat="${p.lat}">
      <span class="t">${esc(t)}</span><span class="m">${esc(m)}</span></button>${p.d > 1 ? `<span class="d">${fmtM(p.d)}</span>` : ""}</li>`;
  };
  return `${headHTML(esc(title), sub, title)}
  <div id="multiList">${[...groups.entries()].map(([t, ps]) => `${single ? "" : `<h3 class="sub-head"><span class="tt">${esc(t)}</span><span class="times">×${ps.length}</span></h3>`}
    <div class="group" style="margin-top:${single ? 12 : 6}px"><ul class="list">${ps.map(row).join("")}</ul></div>`).join("")}</div>`;
};
VIEWS.multi.after = async v => {
  wireHover($("#multiList"));
  const ids = v.recs.filter(p => p.kind !== "placename").map(p => p.id);
  if (!v.fetched && ids.some(id => !fullById.has(id))) { v.fetched = true; await fetchFull(ids); if (S.view === v) render(); }
};

// ---------------------------------------------------------------- Clicked empty spot
VIEWS.spot = v => `${headHTML("Dropped pin", `<span class="mono">${v.lat.toFixed(5)}, ${v.lon.toFixed(5)}</span>`)}
  <div class="actions three">
    <button class="act primary" data-act="siteHere" data-lon="${v.lon}" data-lat="${v.lat}" data-m="100"><span class="ci">${icon.pin}</span><span>Place site</span></button>
    <button class="act" data-act="copySpotGrid"><span class="ci">${icon.grid}</span><span>Copy grid ref</span></button>
    <button class="act" data-act="copy" data-what="position" data-text="${v.lat.toFixed(5)}, ${v.lon.toFixed(5)}"><span class="ci">${icon.copy}</span><span>Copy position</span></button>
  </div>
  <div id="navSlot">${navHTML()}</div>
  <div class="group" id="grids">
    <div class="kv-row"><span class="k">Wartime grid · Indian 1960</span><span class="v mono">…</span></div>
    <div class="kv-row"><span class="k">GPS grid · WGS84</span><span class="v mono">…</span></div></div>
  <p class="small-t muted" style="margin:8px 2px 0">The two grids differ by a few hundred metres. 1960s reports and sheets use the wartime grid.</p>`;
VIEWS.spot.after = async v => {
  try {
    const r = v.grids || (v.grids = await api(`/api/lonlat?lon=${v.lon}&lat=${v.lat}`));
    if (S.view !== v) return;
    $("#grids").innerHTML = gridRows(r); wire($("#grids"));
  } catch (e) { $("#grids").innerHTML = `<div class="kv-row err">${esc(e.message)}</div>`; }
};
ACTIONS.copySpotGrid = () => { const g = S.view.grids; if (g) copyText(g.mgrs_indian1960, "Indian 1960 grid ref"); };
const gridRows = r => `
  <div class="kv-row"><span class="k">Wartime grid · Indian 1960 (wartime map datum)</span><span class="v"><span class="mono">${esc(r.mgrs_indian1960)}</span></span>
    <span class="inline-btns"><button class="linkish small-t" data-act="copy" data-what="Indian 1960 grid" data-text="${esc(r.mgrs_indian1960)}">Copy</button></span></div>
  <div class="kv-row"><span class="k">GPS grid · WGS84</span><span class="v"><span class="mono">${esc(r.mgrs_wgs84)}</span></span>
    <span class="inline-btns"><button class="linkish small-t" data-act="copy" data-what="WGS84 grid" data-text="${esc(r.mgrs_wgs84)}">Copy</button></span></div>`;

// ---------------------------------------------------------------- Grid reference square
async function openGridRef(ref, datum = "indian1960") {
  try {
    const r = await api(`/api/gridref?${new URLSearchParams({ ref, datum, zone: "48P" })}`);
    setData("gridsq", { type: "Feature", geometry: { type: "Polygon", coordinates: [r.square] }, properties: {} });
    setData("gridsq-c", pointFC(r.lon, r.lat));
    setTarget({ lon: r.lon, lat: r.lat, m: 0 });
    const nice = ref.toUpperCase().replace(/\s+/g, " ").trim();
    show("grid", { ref: nice, datum, r, nearby: true }, { push: S.view.name !== "grid" });
    pushRecentGrid(nice, datum);
    fitSquare(r.square);
  } catch (e) { toast("Not a grid reference I can read: " + e.message, 5000); }
}
VIEWS.grid = v => {
  const l7014 = S.eras.find(x => /L7014/.test(x.series) && eraCovers(x, v.r.lon, v.r.lat));
  return `${headHTML(`<span class="mono">${esc(v.ref)}</span>`, `${icon.grid.replace("<svg", '<svg style="width:16px;height:16px"')}<span>Grid square · ${fmtM(v.r.precision_m)} · read on ${v.datum === "wgs84" ? "WGS84 / GPS" : "Indian 1960 (wartime map datum)"}</span>`)}
    <div class="actions">
      <button class="act primary" data-act="siteHere" data-lon="${v.r.lon}" data-lat="${v.r.lat}" data-m="${Math.round(v.r.uncertainty_m)}" data-name="Grid ${esc(v.ref)}" data-why="Grid reference ${esc(v.ref)} read on ${v.datum === "wgs84" ? "WGS84" : "Indian 1960"}"><span class="ci">${icon.pin}</span><span>Place site</span></button>
      <button class="act" data-act="toNearby"><span class="ci">${icon.near}</span><span>Nearby</span></button>
      <button class="act" data-act="copy" data-what="WGS84 grid" data-text="${esc(v.r.wgs84_mgrs)}"><span class="ci">${icon.copy}</span><span>Copy GPS grid</span></button>
      ${!l7014 ? "" : S.eraA === l7014.id && eraInYears(l7014)
        ? `<button class="act" disabled aria-label="The 1960s sheet is already showing" title="The 1960s sheet is already showing"><span class="ci">${icon.check}</span><span>1960s sheet shown</span></button>`
        : `<button class="act" data-act="sheet60" data-id="${l7014.id}" aria-label="Show the 1960s sheet"><span class="ci">${icon.map}</span><span>1960s sheet</span></button>`}
    </div>
    <div id="navSlot">${navHTML()}</div>
    <div class="group">
      <div class="kv-row"><span class="k">Read on which datum?</span><span class="v"><span class="seg" role="group" aria-label="Datum">
        <button data-act="datum" data-d="indian1960" aria-pressed="${v.datum === "indian1960"}">Indian 1960 (wartime)</button>
        <button data-act="datum" data-d="wgs84" aria-pressed="${v.datum === "wgs84"}">WGS84 (GPS)</button></span></span></div>
      <div class="kv-row"><span class="k">Square</span><span class="v">${fmtM(v.r.precision_m)} × ${fmtM(v.r.precision_m)}</span><span class="v2">A grid reference names a square, not a point.</span></div>
      <div class="kv-row"><span class="k">Centre (WGS84)</span><span class="v mono">${v.r.lat.toFixed(5)}, ${v.r.lon.toFixed(5)}</span></div>
      <div class="kv-row"><span class="k">Same square on the GPS grid</span><span class="v mono">${esc(v.r.wgs84_mgrs)}</span></div>
    </div>
    <p class="small-t muted" style="margin:8px 2px 0">References read off the L7014 sheets (1965–70) are on Indian 1960, a few hundred metres from GPS.</p>
    ${nearbyShell()}`;
};
VIEWS.grid.after = () => renderNearby();
ACTIONS.datum = el => openGridRef(S.view.ref, el.dataset.d);
// The 1960s sheet (and, if the year filter hides it, the years it needs): never a button that silently does nothing.
ACTIONS.sheet60 = el => { const e = eraById(el.dataset.id); if (e && !eraInYears(e)) { setYears(null); toast("Showing all years, so the 1960s sheet is visible"); } setEra(el.dataset.id); };

// ---------------------------------------------------------------- Historical map info
VIEWS.era = v => {
  const e = eraById(v.id);
  if (!e) return headHTML("Historical map") + `<p class="muted">No map selected.</p>`;
  return `${headHTML(esc(e.name), `<span>${esc(e.years)} · ${esc(e.short)} · ${esc(e.series)}</span>`)}
    <div class="row" style="margin-top:14px"><button class="btn small" data-act="fit" data-id="${e.id}">Zoom to coverage</button>
      ${S.eraA !== e.id ? `<button class="btn small primary" data-act="setEra" data-id="${e.id}">Show this map</button>` : ""}
      <button class="btn small danger" data-act="eraRemove" data-id="${e.id}">${icon.trash}Remove</button></div>
    ${!eraInYears(e) ? `<p class="quiet-note">${icon.warn}<span>Outside your years (${esc(yearsText())}), so it is hidden on the map.</span></p>` : ""}
    <p class="small-t muted">Positions on old maps can be off by hundreds of metres or more. Check each sheet’s note.</p>
    <div class="group"><ul class="list">${e.layers.map(l => `<li style="display:block;padding:10px 14px"><div class="t" style="white-space:normal;font-weight:500">${esc(l.title)}</div>
      <div class="small-t muted">${esc(l.date || "Date unknown")} · ${esc(l.scale)}</div>
      ${l.dateNote ? `<details style="margin-top:4px"><summary class="small-t" style="cursor:pointer;color:var(--accent)">Dating &amp; accuracy note</summary><p class="small-t muted" style="margin:4px 0 0">${esc(l.dateNote)}</p></details>` : ""}
      ${safeUrl(l.source) ? `<a class="small-t" href="${esc(l.source.split(" ")[0])}" target="_blank" rel="noopener">Original scan</a>` : ""}</li>`).join("")}</ul></div>`;
};

// ================================================================ map library (adding and removing old maps)
// No old map is downloaded unless the person asks for it. The library card ("Old maps") lists the maps that cover
// the area on screen, from /api/library (the catalogue plus each map's state: available, queued, downloading,
// building, installed or failed), with Add, Stop and Remove. While the card is open the maps' footprints are drawn
// on the map, so coverage is visible, and tapping one selects that map. The server adds one map at a time in the
// background, so the viewer polls only while something is on its way; when a map is ready it reloads catalog.json
// and adds the new layer to the maps in place, and the map picker offers it at once.
const LIB_BUSY = new Set(["queued", "downloading", "building"]);
const LIB_POLL_OPEN_MS = 1000, LIB_POLL_CLOSED_MS = 3000; // card open / closed; no polling at all when idle
const BULK_ASK = { maps: 8, bytes: 100e6 }; // "Add all" asks first above either of these
const BULK_MAX = 60; // and above this many maps it asks to zoom in instead (a whole region is many GB)
// Each series' label in the series switch.
// The scale chips of the Old maps card, most detailed first: each map falls in the first whose test its scale passes.
const LIB_KINDS = [
  { k: "town", label: "Town plans", test: n => n && n <= 15000 },
  { k: "25k", label: "1:25,000", test: n => n === 25000 },
  { k: "50k", label: "1:50,000", test: n => n === 50000 },
  { k: "100k", label: "1:100,000", test: n => n === 100000 },
  { k: "250k", label: "1:250,000", test: n => n === 250000 },
  { k: "other", label: "Other scales", test: () => true },
];
const LIB_SORTS = [["here", "Best here"], ["old", "Oldest"], ["new", "Newest"]];
const LIB_LIST_MAX = 120; // rows listed at once; past this the card asks to zoom in or filter
const LIB_OUTLINES_MAX = 40; // available maps outlined at once; past this only added, pointed-at and chosen maps are
const scaleNumber = t => { const m = /1\s*:\s*([\d\s.,]+)/.exec(t || ""); return m ? +m[1].replace(/\D/g, "") || null : null; };
// The footprint layers (see addFootprintLayers), shown only while the library card is open.
const FP_LAYERS = ["libfp-fill", "libfp-halo", "libfp-line-av", "libfp-line-in", "libfp-sel", "libfp-label"];

const libInView = () => { const v = viewBounds(); return S.lib.maps.filter(m => m.bounds && (!v || boxesOverlap(m.bounds, v))); };
const libCanAddHere = () => libInView().some(m => m.state === "available");
const libBusyMaps = () => S.lib.maps.filter(m => LIB_BUSY.has(m.state));
// Disk space once added: the server's estimate for each map (scripts/library.py, from the catalogue's ratios).
const estDisk = list => list.reduce((a, m) => a + (m.disk_estimate || 0), 0);
const libScale = m => m.scale || (m.series !== "scans" ? S.lib.series[m.series]?.scale : "");
const libWhat = m => kindWords(m.series, m.series_name, libScale(m)) || "Map";
const libKind = m => LIB_KINDS.find(x => x.test(m.scale_n || scaleNumber(libScale(m)))).k;
// Does a map match the card's filter? Every word typed must appear in its place, title, sheet number, year or kind,
// accents and case ignored ("hue 1968", "feuille 221", "lang son").
function libMatches(m, words) {
  const text = fold([m.title, m.place, m.sheet, libYear(m), libWhat(m), m.id].filter(Boolean).join(" "));
  return words.every(w => text.includes(w));
}
// The year when it is known: an installed map's from its own margin (catalog.json), a scan's from its control file.
function libYear(m) {
  const e = eraById(m.id);
  if (e?.dated) return e.years;
  return yearOf(m.date) ? String(yearOf(m.date)) : m.year ? String(m.year) : "";
}
// A footprint's label: the place name alone ("Giàng Mưng Pho (5453 I)" -> "Giàng Mưng Pho"); of a long scan title
// only its last part ("Carte de l'Indochine 1:100 000, feuille 228 Long-Xuyên" -> "feuille 228 Long-Xuyên").
function libLabel(m) {
  let t = String(m.title || m.id).replace(/\s*\([^)]*\)\s*$/, "");
  if (t.length > 24 && t.includes(", ")) t = t.slice(t.lastIndexOf(", ") + 2);
  return t.length > 28 ? t.slice(0, 27).trimEnd() + "…" : t;
}
const pct = p => `${Math.round((p || 0) * 100)}%`;
// The second line of a map's row: what it is and its size, or what is happening to it.
function libMeta(m) {
  if (m.state === "queued") return `Waiting to download · ${fmtMB(m.bytes)}`;
  if (m.state === "downloading") return m.progress == null ? `Downloading · ${fmtMB(m.bytes)}` : `Downloading · ${pct(m.progress)} of ${fmtMB(m.bytes)}`;
  if (m.state === "building") return `Preparing the map · ${pct(m.progress)}`;
  if (m.state === "failed") return "Could not be added";
  return [libYear(m) || "Undated", libWhat(m), fmtMB(m.state === "installed" ? m.disk_bytes : m.bytes)].filter(Boolean).join(" · ");
}
// The same line as HTML, its year in bold: the year is what tells two maps of one place apart. An edition's row is
// already named by its year, so it is left out there.
function libMetaHTML(m, edition = false) {
  if (!["installed", "available"].includes(m.state)) return esc(libMeta(m));
  const rest = [libWhat(m), fmtMB(m.state === "installed" ? m.disk_bytes : m.bytes)].filter(Boolean).join(" · ");
  return edition ? esc(rest) : `<b class="yr">${esc(libYear(m) || "Undated")}</b> · ${esc(rest)}`;
}
const leftText = () => S.lib.queueBytes ? `${fmtMB(S.lib.queueBytes)} left to download` : "Preparing the last map";
// One line for the map bar while maps are on their way: "Adding Cao Lãnh · 42%", "Adding 3 maps · 12 MB left".
function libAddingText() {
  const busy = libBusyMaps();
  if (!busy.length) return "";
  const now = busy.find(m => m.state !== "queued") || busy[0];
  if (busy.length > 1) return `Adding ${busy.length} maps · ${leftText().replace(" to download", "")}`;
  return `Adding ${libLabel(now)}${now.state === "queued" ? "" : ` · ${pct(now.progress)}`}`;
}

// ---- talking to the server
let libTimer = null, libInflight = false;
async function loadLibrary() {
  if (libInflight) return;
  libInflight = true; clearTimeout(libTimer);
  try { applyLibrary(await api("/api/library")); }
  catch (e) { S.lib.err = e.message; S.lib.loaded = true; libRefreshView(); }
  finally { libInflight = false; scheduleLibraryPoll(); }
}
// Polls only while a map is on its way: quickly while the card shows its progress, slowly otherwise (enough to
// put a finished map in the picker).
function scheduleLibraryPoll() {
  clearTimeout(libTimer);
  if ((libBusyMaps().length || S.lib.legacy?.state === "converting") && !stopped) libTimer = setTimeout(loadLibrary, S.view.name === "library" ? LIB_POLL_OPEN_MS : LIB_POLL_CLOSED_MS);
}
let libWasBusy = false;
let libBatchToasted = false; // a several-map add started in this window: its toast already says so, so no second announcement
const justInstalled = new Set(); // maps seen to finish, announced once their layer is on the map
function applyLibrary(r) {
  const before = S.lib.byId, first = !S.lib.loaded, wasConverting = S.lib.legacy?.state === "converting";
  const maps = r.maps || [];
  // A sheet's catalogue corners are on its own old datum, a few hundred metres off today's map; the server sends
  // each map's outline on WGS84 (its real extent once added), which is what is drawn and compared with the view.
  for (const m of maps) if (m.outline) m.bounds = m.outline;
  S.lib = { maps, byId: new Map(maps.map(m => [m.id, m])), series: r.series || {}, queueBytes: r.queue_bytes || 0, legacy: r.legacy || null, loaded: true, err: null };
  if (!first) for (const m of maps) {
    const was = before.get(m.id)?.state;
    if (!was || was === m.state) continue;
    if (m.state === "installed") justInstalled.add(m.id);
    if (m.state === "failed") toast(`${m.title} could not be added. Open Old maps to try again.`, 6000);
  }
  // catalog.json lists exactly the installed maps; reload it whenever the two disagree.
  const installed = maps.filter(m => m.state === "installed").map(m => m.id), have = new Set(S.catalog.map(l => l.id));
  if (installed.length !== have.size || installed.some(id => !have.has(id))) refreshCatalog();
  else announceInstalled();
  const nowBusy = libBusyMaps().length;
  if (!first && nowBusy && !libWasBusy && !libBatchToasted) announce(`Adding ${nowBusy} map${nowBusy === 1 ? "" : "s"}. You can keep using the map.`);
  if (!first && !wasConverting && S.lib.legacy?.state === "converting") announce("Converting maps from an older version. You can keep using the map.");
  libWasBusy = nowBusy > 0;
  drawFootprints(); renderMapbar(); libRefreshView(); settingsRefresh(); scheduleLibraryPoll();
  if (S.view.name === "home") render();
}
async function libAdd(ids) {
  if (S.view.name === "library") S.view.confirm = null;
  libBatchToasted = ids.length > 1;
  try { applyLibrary(await api("/api/library/add", { method: "POST", body: { ids } })); }
  catch (e) { toast("Could not add: " + e.message, 5000); return; }
  finally { libBatchToasted = false; }
  if (ids.length > 1) toast(`Adding ${ids.length} maps, one at a time. You can keep using the map.`, 5000);
}
// Removes maps (deleting their tiles and download) or, for maps still on their way, stops them.
async function libRemove(ids, stopping = false) {
  const list = ids.map(id => S.lib.byId.get(id)).filter(Boolean);
  const freed = list.reduce((a, m) => a + (m.disk_bytes || 0), 0);
  if (S.view.name === "library" || S.view.name === "settings") S.view.confirm = null;
  try { applyLibrary(await api("/api/library/remove", { method: "POST", body: { ids } })); }
  catch (e) { toast("Could not remove: " + e.message, 5000); return; }
  const what = list.length === 1 ? list[0].title : `${list.length} maps`;
  toast(stopping ? `Stopped adding ${what}` : `Removed ${what}${freed ? ` · ${fmtMB(freed)} freed` : ""}`);
}

// ---- installed maps on the map, without a reload
let catalogBusy = false, catalogAgain = false;
async function refreshCatalog(tries = 0) {
  if (catalogBusy) { catalogAgain = true; return; }
  catalogBusy = true;
  try {
    const layers = ((await api("/catalog.json", { cache: "no-store" })).layers || []).filter(l => l.bounds);
    const want = S.lib.maps.filter(m => m.state === "installed" && m.bounds);
    // The server writes catalog.json before it reports a map installed; retry briefly in case of a race anyway.
    if (want.some(m => !layers.some(l => l.id === m.id)) && tries < 4) { catalogBusy = false; setTimeout(() => refreshCatalog(tries + 1), 800); return; }
    setCatalog(layers);
    announceInstalled();
  } catch { /* the next library change tries again */ }
  catalogBusy = false;
  if (catalogAgain) { catalogAgain = false; refreshCatalog(); }
}
// Swaps in a new list of installed maps: adds the new layers to every map, drops the removed ones, and moves off a
// removed map if it was on screen.
function setCatalog(layers) {
  const now = new Set(layers.map(l => l.id)), had = new Set(S.catalog.map(l => l.id));
  for (const l of S.catalog) if (!now.has(l.id)) eachMap(m => removeEraLayer(m, l.id));
  for (const l of layers) if (!had.has(l.id)) eachMap(m => addEraLayer(m, l));
  S.catalog = layers; S.eras = buildEras(layers);
  if (S.eraA && !eraById(S.eraA)) { S.eraA = null; store.set("era", "today"); }
  if (S.eraB !== "today" && !eraById(S.eraB)) S.eraB = "today";
  applyEra();
  if (S.view.name === "library") libRefreshView(true);
}
// A map has just finished: show it straight away if it is the first old map of the area on screen (that is what
// adding it asked for), otherwise say it is ready and offer to show it.
function announceInstalled() {
  const ready = [...justInstalled].map(eraById).filter(Boolean);
  if (!ready.length) return;
  ready.forEach(e => justInstalled.delete(e.id));
  const [lon, lat] = viewCenter(), e = ready.at(-1);
  const firstHere = !S.eraA && eraCovers(e, lon, lat) && !S.eras.some(x => !ready.includes(x) && eraCovers(x, lon, lat));
  const name = ready.length === 1 ? e.name : `${ready.length} maps`;
  if (firstHere) { setEra(e.id); toast(`${name} ${ready.length === 1 ? "is" : "are"} ready`); }
  else if (S.eraA === e.id) toast(`${name} is ready`);
  else toast(`${name} ${ready.length === 1 ? "is" : "are"} ready`, 6000, async () => setEra(e.id), "Show");
}

// ---- footprints on the map
// Every map's sheet outline: solid for installed maps, dashed for ones that can be added, amber while adding.
// One polygon source (promoteId, so selection and hover are feature state, not new data) plus label points.
function fpColors() {
  const lit = S.base === "sat" || isDark();
  return lit ? { installed: "#6fd0b4", busy: "#fbbf24", available: "#f1efe8", sel: "#5ea2ff", halo: "#000", text: "#f1efe8", textHalo: "rgba(10,12,11,.88)" }
    : { installed: "#0e5a4b", busy: "#b45309", available: "#3f3d36", sel: "#1d72f2", halo: "#fff", text: "#1b1a17", textHalo: "rgba(255,255,255,.92)" };
}
const fpTextColor = c => ["match", ["get", "st"], "installed", c.installed, "busy", c.busy, c.text];
function addFootprintLayers(m) {
  m.addSource("libfp", { type: "geojson", data: lastData.libfp || empty(), promoteId: "id" });
  const c = fpColors(), none = { visibility: "none" };
  const isOn = k => ["boolean", ["feature-state", k], false];
  m.addLayer({ id: "libfp-fill", type: "fill", source: "libfp", layout: none, paint: {
    "fill-color": ["match", ["get", "st"], "installed", c.installed, "busy", c.busy, c.available],
    // Only the sheet pointed at or chosen is tinted: tinting every added or adding sheet turned a whole area
    // amber while it downloaded. The outlines say which are added and which are adding. The fill stays (at 0)
    // because it is what a click or hover hits (footprintAt).
    "fill-opacity": ["case", isOn("sel"), 0.22, isOn("hover"), 0.14, 0] } });
  m.addLayer({ id: "libfp-halo", type: "line", source: "libfp", layout: none, paint: { "line-color": c.halo, "line-width": 3, "line-opacity": 0.4 } });
  // Overview (1:250,000) sheets get a heavier dash than the 1:50,000 ones, so the two series read apart.
  m.addLayer({ id: "libfp-line-av", type: "line", source: "libfp", layout: none, filter: ["in", ["get", "st"], ["literal", ["available", "failed"]]],
    paint: { "line-color": c.available, "line-opacity": 0.8, "line-width": ["match", ["get", "kind"], "250k", 1.8, "other", 1.6, 1.2], "line-dasharray": [3, 2] } });
  m.addLayer({ id: "libfp-line-in", type: "line", source: "libfp", layout: none, filter: ["in", ["get", "st"], ["literal", ["installed", "busy"]]],
    paint: { "line-color": ["match", ["get", "st"], "installed", c.installed, c.busy], "line-width": 2.2 } });
  m.addLayer({ id: "libfp-sel", type: "line", source: "libfp", layout: none,
    paint: { "line-color": c.sel, "line-width": 4, "line-opacity": ["case", isOn("sel"), 1, isOn("hover"), 0.55, 0] } });
  // Each sheet's name runs along its own outline, in the outline's colour and small capitals, so it reads as the
  // name of that sheet (not of a town) and stays in sight wherever the sheet's edge is on screen.
  m.addLayer({ id: "libfp-label", type: "symbol", source: "libfp", minzoom: 7.5, layout: { ...none, "text-field": ["get", "label"],
    "symbol-placement": "line", "symbol-spacing": 420, "text-font": ["Noto Sans Medium"], "text-size": 10.5,
    "text-transform": "uppercase", "text-letter-spacing": 0.06, "text-max-angle": 20, "text-padding": 4, "symbol-sort-key": ["get", "area"] },
    paint: { "text-color": fpTextColor(c), "text-halo-color": c.textHalo, "text-halo-width": 1.6 } });
}
// Retunes the footprint colours for the base map and theme (called from applyBase).
function styleFootprints(m) {
  if (!m.getLayer("libfp-fill")) return;
  const c = fpColors();
  m.setPaintProperty("libfp-fill", "fill-color", ["match", ["get", "st"], "installed", c.installed, "busy", c.busy, c.available]);
  m.setPaintProperty("libfp-halo", "line-color", c.halo);
  m.setPaintProperty("libfp-line-av", "line-color", c.available);
  m.setPaintProperty("libfp-line-in", "line-color", ["match", ["get", "st"], "installed", c.installed, c.busy]);
  m.setPaintProperty("libfp-sel", "line-color", c.sel);
  m.setPaintProperty("libfp-label", "text-color", fpTextColor(c));
  m.setPaintProperty("libfp-label", "text-halo-color", c.textHalo);
}
let fpKey = "";
function drawFootprints() {
  const key = S.lib.maps.map(m => m.id + m.state).join();
  if (key === fpKey) return;
  fpKey = key;
  const polys = [];
  for (const m of S.lib.maps) {
    if (!m.bounds) continue;
    const [w, s, e, n] = m.bounds;
    const st = m.state === "installed" ? "installed" : LIB_BUSY.has(m.state) ? "busy" : m.state === "failed" ? "failed" : "available";
    const props = { id: m.id, st, kind: libKind(m), area: (e - w) * (n - s), label: libLabel(m) };
    polys.push({ type: "Feature", properties: props, geometry: { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] } });
  }
  setData("libfp", { type: "FeatureCollection", features: polys });
  setFpState("sel", S.view.name === "library" ? S.view.sel : null, true);
}
let fpOn = false;
function showFootprints(on, force = false) {
  if (on === fpOn && !force) return;
  fpOn = on;
  eachMap(m => FP_LAYERS.forEach(id => m.getLayer(id) && m.setLayoutProperty(id, "visibility", on ? "visible" : "none")));
  if (!on) { setFpState("sel", null); setFpState("hover", null); }
  applyFootprintFilter();
}
// Whether the available maps' outlines are drawn: only while few enough are listed to read them apart. Hundreds of
// overlapping sheets at once tell nothing; the one pointed at or chosen is always outlined (libfp-sel).
const libOutlinesShown = (md = libModel()) => md.shown.filter(m => m.state !== "installed").length <= LIB_OUTLINES_MAX;
// The scale chips and the filter field filter the footprints as well as the list.
function applyFootprintFilter() {
  const lib = S.view.name === "library";
  const few = lib && libOutlinesShown(), lines = outlinesOn();
  const kind = lib && S.view.kind && S.view.kind !== "all" ? S.view.kind : null;
  const words = lib ? fold(S.view.q || "").split(/\s+/).filter(Boolean) : [];
  const parts = [];
  if (kind) parts.push(["==", ["get", "kind"], kind]);
  if (words.length) parts.push(["in", ["get", "id"], ["literal", S.lib.maps.filter(m => libMatches(m, words)).map(m => m.id)]]);
  const sf = parts.length ? ["all", ...parts] : ["to-boolean", 1];
  eachMap(m => {
    if (!m.getLayer("libfp-fill")) return;
    const mine = ["in", ["get", "st"], ["literal", ["installed", "busy"]]];
    const shown = few ? sf : ["all", sf, mine], nothing = ["==", ["get", "st"], "none"];
    m.setFilter("libfp-fill", shown);
    for (const id of ["libfp-halo", "libfp-label"]) m.setFilter(id, lines ? shown : nothing);
    m.setFilter("libfp-sel", sf);
    m.setFilter("libfp-line-av", lines && few ? ["all", ["in", ["get", "st"], ["literal", ["available", "failed"]]], sf] : nothing);
    m.setFilter("libfp-line-in", lines ? ["all", ["in", ["get", "st"], ["literal", ["installed", "busy"]]], sf] : nothing);
  });
}
// Settings: whether the sheet outlines are drawn while the Old maps card is open (the sheet pointed at or chosen is
// outlined either way, so the map and the list still match).
const outlinesOn = () => store.get("outlines", true) !== false;
function setOutlines(on) {
  store.set("outlines", on);
  applyFootprintFilter();
  if (S.view.name === "library") libRefreshView(true);
  settingsRefresh(true);
  if (!$("#popStyle").hidden) { renderStylePop(); $(`#popStyle [data-act="outlines"][data-on="${on ? 1 : 0}"]`)?.focus(); }
}
const fpState = { sel: null, hover: null };
function setFpState(key, id, force = false) {
  if (fpState[key] === id && !force) return;
  eachMap(m => {
    if (!m.getSource("libfp")) return;
    if (fpState[key]) m.setFeatureState({ source: "libfp", id: fpState[key] }, { [key]: false });
    if (id) m.setFeatureState({ source: "libfp", id }, { [key]: true });
  });
  fpState[key] = id;
}
// The map whose footprint is under a point: the smallest (most detailed) when sheets overlap.
function footprintAt(m, p) {
  if (!fpOn || !m.getLayer("libfp-fill")) return null;
  const fs = m.queryRenderedFeatures([[p.x - 1, p.y - 1], [p.x + 1, p.y + 1]], { layers: ["libfp-fill"] });
  fs.sort((a, b) => a.properties.area - b.properties.area);
  return fs[0]?.properties.id || null;
}

// ---- the library card
function openLibrary(sel = null, confirm = null) {
  const from = document.activeElement;
  closePopovers();
  const keep = S.view.name === "library" ? S.view : {};
  show("library", { sel, confirm, kind: keep.kind || "all", sort: keep.sort || "here", q: keep.q || "", open: keep.open || null });
  setFpState("sel", sel);
  if (sel) fitFootprintIfHidden(sel);
  // Keyboard focus moves into the card, and back to what opened it when the card closes (see show).
  if (from && from !== document.body && !$("#panel").contains(from)) S.opener = from;
  focusCard();
  loadLibrary();
}
function focusCard() {
  const t = $("#cardTitle");
  if (!t) return;
  if (!t.hasAttribute("tabindex")) t.tabIndex = -1;
  t.focus({ preventScroll: true });
}
// What the card shows, shared by the card and its change check (libSig). Without a filter it lists the maps on
// screen; with one, every map in the catalogue that matches. Editions of one sheet (same sheet_key) share a row.
function libModel() {
  const v = S.view, [cx, cy] = viewCenter();
  const words = fold(v.q || "").split(/\s+/).filter(Boolean);
  const pool = words.length ? S.lib.maps.filter(m => m.bounds && libMatches(m, words)) : libInView();
  const kinds = LIB_KINDS.map(x => ({ ...x, n: pool.filter(m => libKind(m) === x.k).length })).filter(x => x.n);
  const kind = v.kind && kinds.some(x => x.k === v.kind) ? v.kind : "all";
  const shown = pool.filter(m => kind === "all" || libKind(m) === kind);
  const covers = m => cx >= m.bounds[0] && cx <= m.bounds[2] && cy >= m.bounds[1] && cy <= m.bounds[3];
  const area = m => (m.bounds[2] - m.bounds[0]) * (m.bounds[3] - m.bounds[1]);
  const dist = m => Math.hypot((m.bounds[0] + m.bounds[2]) / 2 - cx, (m.bounds[1] + m.bounds[3]) / 2 - cy);
  const yr = m => +libYear(m).slice(0, 4) || null;
  // "Best here": the maps under the centre of the view first, most detailed first; then the rest, nearest first.
  // "Oldest" / "Newest": by year, undated last; nearest first among the same year.
  const sort = LIB_SORTS.some(([k]) => k === v.sort) ? v.sort : "here";
  const order = sort === "here"
    ? (a, b) => (covers(b) - covers(a)) || (covers(a) && covers(b) ? area(a) - area(b) : 0) || dist(a) - dist(b)
    : (a, b) => ((yr(a) == null) - (yr(b) == null)) || (sort === "old" ? yr(a) - yr(b) : yr(b) - yr(a)) || dist(a) - dist(b);
  const rank = { downloading: 0, building: 0, queued: 1, failed: 2 };
  return {
    words, kinds, kind, sort, shown,
    adding: S.lib.maps.filter(m => m.state in rank).sort((a, b) => rank[a.state] - rank[b.state]), // wherever they are
    installed: libGroup(shown.filter(m => m.state === "installed").sort(order)),
    available: libGroup(shown.filter(m => m.state === "available").sort(order)),
  };
}
// Rows from maps in order: a map alone, or the editions of one sheet together (newest first) at the place of the
// first of them. A group is { group: true, id: "g:<sheet_key>", maps, first }.
function libGroup(maps) {
  const by = new Map();
  for (const m of maps) {
    const k = m.sheet_key || m.id;
    if (!by.has(k)) by.set(k, []);
    by.get(k).push(m);
  }
  return [...by.entries()].map(([k, list]) => {
    if (list.length === 1) return list[0];
    const sorted = [...list].sort((a, b) => (+libYear(b).slice(0, 4) || 0) - (+libYear(a).slice(0, 4) || 0));
    return { group: true, id: "g:" + k, maps: sorted, first: sorted[0] };
  });
}
// The maps a row stands for, and the one its main control acts on (a group's newest edition).
const rowMaps = r => r.group ? r.maps : [r];
const rowLead = r => r.group ? r.first : r;
// Everything the card's HTML depends on except progress numbers, which are patched in place (libPatchProgress), so
// the card is not rebuilt (losing a click in progress) every time a download moves on.
function libSig(md = libModel()) {
  const ids = l => l.flatMap(rowMaps).map(m => m.id + ":" + m.state).join();
  return JSON.stringify([S.lib.loaded, S.lib.err, md.kind, md.kinds.map(x => x.k + x.n), md.sort, md.words, ids(md.adding),
    ids(md.installed), ids(md.available), S.view.sel, S.view.open, S.view.confirm, S.eraA, S.catalog.length, S.lib.legacy]);
}
let libLastSig = "";
function libRefreshView(force = false) {
  if (S.view.name !== "library") return;
  if (force || libSig() !== libLastSig) rerenderKeepingFocus();
  else libPatchProgress();
}
// Re-renders the card keeping its scroll position and the focused control (or, if that control changed, the
// row it belonged to), so keyboard users are not thrown back to the top.
// The opened row (else the focused one) also keeps its place on screen: moving the map re-sorts the list, and the
// row just picked must not slide away from under the pointer.
function rerenderKeepingFocus() {
  const a = document.activeElement, sc = $("#panelScroll"), top = sc.scrollTop;
  const mine = $("#view").contains(a) && a.dataset.act;
  const sel = mine ? `[data-act="${a.dataset.act}"]${a.dataset.id ? `[data-id="${CSS.escape(a.dataset.id)}"]` : ""}${a.dataset.s ? `[data-s="${a.dataset.s}"]` : ""}${a.dataset.theme ? `[data-theme="${a.dataset.theme}"]` : ""}${a.dataset.on ? `[data-on="${a.dataset.on}"]` : ""}` : null;
  const anchorId = S.view.sel || (mine && a.dataset.id) || null;
  const rowOf = id => id && $(`#view [data-lib-row="${CSS.escape(id)}"]`);
  const anchorTop = rowOf(anchorId)?.getBoundingClientRect().top;
  render();
  sc.scrollTop = top;
  const row = anchorTop != null && rowOf(anchorId);
  if (row) sc.scrollTop += row.getBoundingClientRect().top - anchorTop;
  if (!sel) return;
  const el = $("#view").querySelector(sel) || (a.dataset.id && $("#view").querySelector(`[data-act="libSel"][data-id="${CSS.escape(a.dataset.id)}"]`));
  el?.focus({ preventScroll: true });
}
const RING_C = 2 * Math.PI * 9; // circumference of the progress ring (r = 9 in a 24 box)
const ringOffset = m => m.state === "queued" || m.progress == null ? RING_C * 0.75 : RING_C * (1 - m.progress);
function libPatchProgress() {
  for (const m of libBusyMaps()) {
    const id = CSS.escape(m.id);
    const bar = $(`#view [data-prog="${id}"]`);
    if (bar) { bar.style.strokeDashoffset = ringOffset(m); bar.parentElement.classList.toggle("wait", m.state === "queued" || m.progress == null); }
    $$(`#view [data-prog-t="${id}"]`).forEach(t => { t.textContent = libMeta(m); });
  }
  const left = $("#view [data-lib-left]");
  if (left) left.textContent = leftText();
}
// The control at the end of a row: Add, a progress ring that stops the map, Show / Hide, or Retry.
function libControl(m) {
  const t = esc(m.title), id = esc(m.id);
  if (m.state === "available") return `<button class="lib-pill" data-act="libAdd" data-id="${id}" aria-label="Add ${t} (${fmtMB(m.bytes)})">Add</button>`;
  if (m.state === "failed") return `<button class="lib-pill" data-act="libAdd" data-id="${id}" aria-label="Try adding ${t} again">Retry</button>`;
  if (LIB_BUSY.has(m.state)) {
    const wait = m.state === "queued" || m.progress == null;
    return `<button class="lib-ring" data-act="libStop" data-id="${id}" aria-label="Stop adding ${t}" title="Stop">
      <svg viewBox="0 0 24 24" class="${wait ? "wait" : ""}" aria-hidden="true"><circle class="track" cx="12" cy="12" r="9"/>
      <circle class="bar" data-prog="${id}" cx="12" cy="12" r="9" style="stroke-dasharray:${RING_C};stroke-dashoffset:${ringOffset(m)}"/>
      <rect class="stop" x="9" y="9" width="6" height="6" rx="1"/></svg></button>`;
  }
  // A toggle: its name stays "Show …" and aria-pressed says whether it is on, while the word on it says what a tap does.
  const on = S.eraA === m.id;
  return `<button class="lib-pill${on ? " on" : ""}" data-act="libShow" data-id="${id}" aria-pressed="${on}" aria-label="Show ${t} on the map">${on ? "Hide" : "Show"}</button>`;
}
// The opened row: what the map is, its size, its licence, and every action on it.
function libDetail(m) {
  const ser = S.lib.series[m.series] || {}, t = esc(m.title), id = esc(m.id), year = libYear(m);
  const years = year || (ser.years ? ser.years[0].toUpperCase() + ser.years.slice(1).replace(/(\d)-(\d)/g, "$1–$2") : "");
  // Who made it, in words: the publisher's first name for the US series, the scan's own series otherwise.
  const maker = m.custom ? "Your own map, added from this computer" : m.series === "scans" ? m.series_name : (ser.publisher || "").split(";")[0];
  const size = m.state === "installed" ? `Uses ${fmtMB(m.disk_bytes)} on this computer`
    : m.bytes ? `Downloads ${fmtMB(m.bytes)}, then takes about ${fmtMB(estDisk([m]))} on this computer` : "";
  const licence = m.licence || ser.licence;
  // The row's own control (Add, Retry, Show / Hide, or the progress ring that stops it) is the main action; the
  // opened row adds only what the row cannot do.
  const acts = (m.custom ? `<button class="btn small" data-act="cmManage" data-id="${id}">Edit or place again</button>` : "")
    + (m.state === "installed" ? `<button class="btn small danger" data-act="libAskRemove" data-id="${id}">${icon.trash}Remove</button>` : "");
  const confirm = S.view.confirm === "remove:" + m.id ? `<div class="confirm-box" role="alertdialog" aria-label="Remove ${t}">
      <span>Remove <b>${t}</b> from this computer? This frees ${fmtMB(m.disk_bytes) || "its space"}. You can add it again later.</span>
      <div class="row"><button class="btn small danger solid" data-act="libRemove" data-id="${id}">Remove</button>
      <button class="btn small" data-act="libCancel">Keep it</button></div></div>` : "";
  return `<div class="lib-detail" id="libd-${id}">
    <p>${esc(libWhat(m))}${years ? ` · ${esc(years)}` : ""}</p>
    ${maker ? `<p class="muted">${esc(maker)}</p>` : ""}
    ${size ? `<p class="muted">${esc(size)}</p>` : ""}
    ${m.error ? `<p class="err">${esc(m.error)}</p>` : ""}
    <div class="row">${acts}<button class="btn small" data-act="libFit" data-id="${id}">Zoom to map</button></div>
    ${confirm}
    ${licence ? `<details><summary>Licence</summary><p class="muted">${esc(licence)}</p></details>` : ""}</div>`;
}
// One map's row. As an edition inside a sheet's row it is named by its year ("1952 edition").
function libRow(m, edition = false) {
  const sel = S.view.sel === m.id, id = esc(m.id), year = libYear(m);
  const title = edition ? (year ? `${year} edition` : "Undated edition") : m.title;
  return `<li class="lib-row${sel ? " sel" : ""}${edition ? " edition" : ""}" data-lib-row="${id}">
    <button class="main" data-act="libSel" data-id="${id}" aria-expanded="${sel}"${sel ? ` aria-controls="libd-${id}"` : ""}>
      <span class="t">${esc(title)}</span><span class="m${m.state === "failed" ? " bad" : ""}" data-prog-t="${id}">${libMetaHTML(m, edition)}</span></button>
    ${libControl(m)}${sel ? libDetail(m) : ""}</li>`;
}
// A sheet with several editions: one row naming the sheet and its years; its control acts on the newest edition
// (Show if one is on this computer, else Add); opening it lists every edition with its own controls.
function libGroupRow(g) {
  const open = S.view.open === g.id, id = esc(g.id);
  const on = g.maps.find(m => m.state === "installed" && S.eraA === m.id) || g.maps.find(m => m.state === "installed");
  const lead = on || g.maps.find(m => m.state === "available") || g.first;
  // "2 editions, 1949–1952" (the row is short; opening it lists each edition by its year).
  const ys = g.maps.map(m => +libYear(m).slice(0, 4)).filter(Boolean), lo = Math.min(...ys), hi = Math.max(...ys);
  const years = !ys.length ? "Undated" : lo === hi ? String(lo) : `${lo}\u2060–\u2060${hi}`; // word joiners keep the range on one line
  const title = String(g.first.title).replace(/,\s*\d{4}$/, "");
  return `<li class="lib-row lib-group${open ? " sel" : ""}" data-lib-row="${esc(g.first.id)}">
    <button class="main" data-act="libOpen" data-id="${id}" aria-expanded="${open}">
      <span class="t">${esc(title)}</span><span class="m"><b class="yr">${esc(years)}</b> · ${esc(libWhat(g.first))} · ${g.maps.length} editions</span></button>
    ${libControl(lead)}
    ${open ? `<ul class="list lib-list lib-editions">${g.maps.map(m => libRow(m, true)).join("")}</ul>` : ""}</li>`;
}
const libItem = r => r.group ? libGroupRow(r) : libRow(r);
// Maps added by an older version of Map Vietnam are stored the old way and do not show until they are converted.
function legacyHTML() {
  const l = S.lib.legacy;
  if (!l || !l.count) return "";
  const n = `${l.count} map${l.count === 1 ? "" : "s"}`;
  if (l.state === "converting") return `<div class="lib-status"><span class="spin" aria-hidden="true"></span>
    <span class="grow"><b>Converting ${n} from an older version</b><span class="small-t muted">${l.converted ? `${l.converted} done. ` : ""}They show here as each one finishes.</span></span></div>`;
  return `<div class="confirm-box calm" role="group" aria-label="Maps from an older version">
    <span><b>${n} from an older version</b> of Map Vietnam ${l.count === 1 ? "is" : "are"} on this computer${l.bytes ? ` (${fmtMB(l.bytes)})` : ""}. Convert ${l.count === 1 ? "it" : "them"} to use ${l.count === 1 ? "it" : "them"} here; nothing is downloaded.</span>
    ${l.state === "failed" && l.error ? `<span class="err">${esc(l.error)}</span>` : ""}
    <div class="row"><button class="btn small primary" data-act="libMigrate">${l.state === "failed" ? "Try again" : `Convert ${l.count === 1 ? "it" : "them"}`}</button></div></div>`;
}
ACTIONS.libMigrate = async () => {
  try { applyLibrary(await api("/api/library/migrate", { method: "POST", body: {} })); }
  catch (e) { toast("Could not convert: " + e.message, 6000); }
};
VIEWS.library = v => {
  const md = libModel();
  libLastSig = libSig(md);
  // Counted as the list shows them: in the scale chosen, on screen (or matching the filter).
  const n = md.shown.length, scale = md.kind === "all" ? "" : ` at ${LIB_KINDS.find(x => x.k === md.kind).label}`;
  const typed = md.words.length ? `“${v.q.trim()}”` : "";
  const sub = !S.lib.loaded ? "Looking for maps…" : S.lib.err ? "The list of maps could not be loaded"
    : typed ? (n ? `${fmtInt(n)} map${n === 1 ? "" : "s"}${scale} match ${typed}` : `No maps match ${typed}`)
    : n ? `${fmtInt(n)} map${n === 1 ? "" : "s"}${scale} cover the area on screen` : "No old maps cover the area on screen";
  const busy = md.adding.filter(m => LIB_BUSY.has(m.state)), failed = md.adding.filter(m => m.state === "failed");
  // Long lists are cut at LIB_LIST_MAX rows; the installed maps come first, so they are never the ones cut.
  let room = LIB_LIST_MAX;
  const take = rows => { const out = rows.slice(0, Math.max(0, room)); room -= out.length; return out; };
  const installed = take(md.installed), available = take(md.available);
  const hidden = md.installed.length + md.available.length - installed.length - available.length;
  const rows = items => `<div class="group"><ul class="list lib-list">${items.map(libItem).join("")}</ul></div>`;
  const list = (title, items) => items.length ? `<h3 class="sub-head">${title}</h3>${rows(items)}` : "";
  // "Add all": the newest edition of each sheet listed. A quiet button for a few maps; above BULK_ASK it asks first;
  // above BULK_MAX it asks to zoom in.
  const av = md.available.map(r => r.group ? r.maps.find(m => m.state === "available") : r), avBytes = av.reduce((a, m) => a + (m.bytes || 0), 0);
  let bulk = "";
  if (av.length > BULK_MAX) bulk = "";
  else if (v.confirm === "bulk") bulk = `<div class="confirm-box calm" role="alertdialog" aria-label="Add ${av.length} maps">
      <span>Add <b>${av.length} maps</b>? They download ${fmtMB(avBytes)} and take about ${fmtMB(estDisk(av))} on this computer. They are added one at a time; you can keep using the map.</span>
      <div class="row"><button class="btn small primary" data-act="libAddAll" data-yes="1">Add ${av.length} maps</button><button class="btn small" data-act="libCancel">Cancel</button></div></div>`;
  else if (av.length >= 2) bulk = `<button class="btn lib-bulk" data-act="libAddAll">${icon.plus}<span>Add all ${av.length} maps ${typed ? "listed" : "here"}</span><span class="muted">${fmtMB(avBytes)}</span></button>`;
  const used = S.lib.maps.filter(m => m.state === "installed").reduce((a, m) => a + (m.disk_bytes || 0), 0);
  const chip = (k, label, count) => `<button data-act="libKind" data-s="${k}" aria-pressed="${md.kind === k}">${esc(label)}${count != null ? ` <span class="n">${fmtInt(count)}</span>` : ""}</button>`;
  // The filter, the scale chips and the sort come first and stay put; the maps on their way follow under one status
  // line (it is their heading), then the ones that failed, then the rest.
  return `${headHTML("Old maps", `<span>${esc(sub)}</span>`)}
    ${legacyHTML()}
    <div class="lib-tools">
      <label class="lib-filter"><span class="ci" aria-hidden="true">${icon.search}</span>
        <input id="libQ" type="search" data-act="libQ" autocomplete="off" spellcheck="false" enterkeyhint="search"
          placeholder="Filter by place, sheet or year" aria-label="Filter old maps by place, sheet number or year" value="${esc(v.q || "")}"></label>
      ${md.kinds.length > 1 ? `<div class="seg lib-seg" role="group" aria-label="Show maps of one scale">${chip("all", "All", null)}${md.kinds.map(x => chip(x.k, x.label, x.n)).join("")}</div>` : ""}
      ${n > 1 ? `<div class="lib-sort" role="group" aria-label="Order of the list"><span class="small-t muted">Order</span>
        ${LIB_SORTS.map(([k, label]) => `<button class="linkish small-t" data-act="libSort" data-s="${k}" aria-pressed="${md.sort === k}">${k === "here" && typed ? "Nearest" : label}</button>`).join("")}</div>` : ""}
      ${outlinesSeg("lib-sort")}
    </div>
    ${busy.length ? `<div class="lib-status"><span class="spin" aria-hidden="true"></span>
      <span class="grow"><b>Adding ${busy.length} map${busy.length === 1 ? "" : "s"}</b><span class="small-t muted" data-lib-left>${esc(leftText())}</span></span>
      ${busy.length > 1 ? `<button class="linkish small-t" data-act="libStopAll">Stop all</button>` : ""}</div>${rows(busy)}` : ""}
    ${list("Could not be added", failed)}
    ${list("On this computer", installed)}
    ${available.length ? `<h3 class="sub-head">${typed ? "Can be added" : "Available here"}</h3>${bulk}${rows(available)}` : ""}
    ${n && !libOutlinesShown(md) ? `<p class="small-t muted lib-bulk-note">Too many maps here to outline them all: point at one to see where it lies, or zoom in or pick a scale.</p>` : ""}
    ${hidden > 0 ? `<p class="small-t muted lib-bulk-note">${fmtInt(hidden)} more ${typed ? "match" : "cover this view"}. Zoom in, pick a scale or type a place to narrow the list.</p>` : ""}
    ${S.lib.loaded && !n && !S.lib.err && typed ? `<p class="muted">Check the spelling, or <button class="linkish" data-act="libClear">clear the filter</button> to see the maps on screen.</p>` : ""}
    ${S.lib.loaded && !n && !S.lib.err && !typed ? `<p class="muted">Move the map over Vietnam, or type a place above, to find old maps.</p>` : ""}
    <p class="foot-note">Maps come from public archives and stay on this computer. Nothing downloads until you add it.${used ? ` Old maps use ${fmtMB(used)} here.` : ""}</p>`;
};
let libQTimer = null;
VIEWS.library.after = () => {
  // Typing filters as you go; the card is redrawn a moment after the last key, keeping the caret in the field.
  const q = $("#libQ");
  q?.addEventListener("input", () => {
    S.view.q = q.value; clearTimeout(libQTimer);
    libQTimer = setTimeout(() => { S.view.sel = null; S.view.open = null; rerenderKeepingFocus(); }, 150);
  });
  q?.addEventListener("keydown", e => { if (e.key === "Escape" && q.value) { e.stopPropagation(); S.view.q = ""; rerenderKeepingFocus(); } });
  if (document.activeElement === q) q.setSelectionRange(q.value.length, q.value.length);
  const seg = $("#view .lib-seg");
  if (seg) {
    const edge = () => seg.classList.toggle("end", seg.scrollLeft + seg.clientWidth >= seg.scrollWidth - 4);
    seg.classList.toggle("more", seg.scrollWidth > seg.clientWidth + 4);
    seg.addEventListener("scroll", edge, { passive: true });
    edge();
    seg.querySelector('[aria-pressed="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  // Pointing at a row lights up its footprint on the map.
  $$("#view [data-lib-row]").forEach(li => {
    li.addEventListener("mouseenter", () => setFpState("hover", li.dataset.libRow));
    li.addEventListener("mouseleave", () => setFpState("hover", null));
  });
  applyFootprintFilter();
};
// Selects a map (its row opens, its footprint is outlined). From the list, the map moves to show the whole sheet;
// from the map, the list scrolls to the row.
function libSelect(id, { fromMap = false } = {}) {
  const v = S.view;
  if (v.name !== "library") return;
  v.sel = !fromMap && v.sel === id ? null : id;
  v.confirm = null;
  setFpState("sel", v.sel);
  rerenderKeepingFocus();
  if (!v.sel) return;
  if (fromMap && !isDesk() && S.sheet === "peek") setSheet("half");
  reveal($(`#view [data-lib-row="${CSS.escape(id)}"]`));
  if (!fromMap) fitFootprintIfHidden(id);
}
// Scrolls the card so `el` is in the part of the panel that is on screen. On a phone the sheet's lower part lies
// below the bottom of the screen, where the browser's own scrollIntoView and focus() consider it visible.
function reveal(el) {
  if (!el) return;
  const sc = $("#panelScroll"), r = el.getBoundingClientRect(), box = sc.getBoundingClientRect();
  const top = box.top + 8, bottom = Math.min(box.bottom, innerHeight) - 12;
  if (r.bottom > bottom) sc.scrollTop += Math.max(0, Math.min(r.bottom - bottom, r.top - top));
  else if (r.top < top) sc.scrollTop -= top - r.top;
}
function fitFootprintIfHidden(id, always = false) {
  const m = S.lib.byId.get(id), v = viewBounds();
  if (!m?.bounds) return;
  const [w, s, e, n] = m.bounds;
  if (!always && v && w >= v[0] && e <= v[2] && s >= v[1] && n <= v[3]) return;
  primary().fitBounds([[w, s], [e, n]], { padding: mapPadding(), maxZoom: 13, duration: reduceMotion ? 0 : 600 });
}
ACTIONS.openLibrary = () => openLibrary();
ACTIONS.libSel = el => libSelect(el.dataset.id);
ACTIONS.libAdd = el => libAdd([el.dataset.id]);
// Stopping a map of your own deletes the only copy of the file you added, so that asks first.
function stopMaps(ids) {
  const own = ids.map(id => S.lib.byId.get(id)).filter(m => m?.custom);
  if (own.length && !window.confirm(`Stop adding ${own.length === 1 ? own[0].title : "your own maps"}? Your original file is deleted too, so keep a copy if you want to add it again.`)) return;
  libRemove(ids, true);
}
ACTIONS.libStop = el => stopMaps([el.dataset.id]);
ACTIONS.libStopAll = () => stopMaps(libBusyMaps().map(m => m.id));
ACTIONS.libShow = el => setEra(S.eraA === el.dataset.id ? "today" : el.dataset.id);
ACTIONS.libFit = el => { if (!isDesk()) setSheet("peek"); fitFootprintIfHidden(el.dataset.id, true); };
ACTIONS.libKind = el => { S.view.kind = el.dataset.s; S.view.sel = null; S.view.open = null; S.view.confirm = null; setFpState("sel", null); rerenderKeepingFocus(); };
ACTIONS.libSort = el => { S.view.sort = el.dataset.s; rerenderKeepingFocus(); };
ACTIONS.libQ = () => {}; // the filter field: typing is handled in VIEWS.library.after
ACTIONS.libClear = () => { S.view.q = ""; rerenderKeepingFocus(); $("#libQ")?.focus(); };
// Opens or closes a sheet's editions; its outline is picked out on the map, and the map moves to it if off screen.
ACTIONS.libOpen = el => {
  const id = el.dataset.id, g = [...libModel().installed, ...libModel().available].find(r => r.id === id);
  S.view.open = S.view.open === id ? null : id; S.view.sel = null; S.view.confirm = null;
  setFpState("sel", S.view.open && g ? g.first.id : null);
  rerenderKeepingFocus();
  if (S.view.open && g) fitFootprintIfHidden(g.first.id);
};
ACTIONS.libCancel = () => { S.view.confirm = null; rerenderKeepingFocus(); };
ACTIONS.libAskRemove = el => {
  S.view.confirm = "remove:" + el.dataset.id;
  rerenderKeepingFocus();
  const yes = $(`#view [data-act="libRemove"][data-id="${CSS.escape(el.dataset.id)}"]`);
  yes?.focus({ preventScroll: true });
  reveal(yes?.closest(".confirm-box"));
};
ACTIONS.libRemove = el => libRemove([el.dataset.id]);
ACTIONS.libAddAll = el => {
  const av = libModel().available.map(r => r.group ? r.maps.find(m => m.state === "available") : r), bytes = av.reduce((a, m) => a + (m.bytes || 0), 0);
  if (!el.dataset.yes && (av.length > BULK_ASK.maps || bytes > BULK_ASK.bytes)) {
    S.view.confirm = "bulk"; rerenderKeepingFocus();
    $('#view [data-act="libAddAll"][data-yes]')?.focus();
    return;
  }
  libAdd(av.map(m => m.id));
};

ACTIONS.eraRemove = el => openLibrary(el.dataset.id, "remove:" + el.dataset.id);

// ================================================================ records (adding and removing record sources)
// The records on the map come from public archives, one "source" at a time (place names, hamlets, casualty
// records, air strikes and so on). Nothing is downloaded until the person asks: the Records card lists every
// source from /api/records with its state (available, queued, running, added or failed) and Add, Stop, Update and
// Remove. The server adds one source at a time in the background, so the viewer polls only while something is on
// its way. Each answer carries a revision number that changes when the stored records changed; the evidence on
// the map is then fetched again.
const REC_BUSY = new Set(["queued", "running"]);
const REC_POLL_OPEN_MS = 1000, REC_POLL_CLOSED_MS = 3000; // card open / closed; no polling at all when idle
const BIG_AREA_KM = 250; // above this width or height, the air-strike chooser suggests zooming in first
let recTimer = null, recInflight = false, recWasAdding = false, stopped = false;
const recBusy = () => S.rec.sources.filter(s => REC_BUSY.has(s.state));
async function loadRecords() {
  if (recInflight || stopped) return;
  recInflight = true; clearTimeout(recTimer);
  try { applyRecords(await api("/api/records")); }
  catch (e) { S.rec.err = e.message; S.rec.loaded = true; recRefreshView(); }
  finally { recInflight = false; scheduleRecordsPoll(); }
}
function scheduleRecordsPoll() {
  clearTimeout(recTimer);
  if (S.rec.busy && !stopped) recTimer = setTimeout(loadRecords, S.view.name === "records" ? REC_POLL_OPEN_MS : REC_POLL_CLOSED_MS);
}
function applyRecords(r) {
  const before = S.rec, first = !before.loaded;
  const wasRemoving = before.sources?.some(s => s.removing);
  const sources = r.sources || [];
  S.rec = { sources, byId: new Map(sources.map(s => [s.id, s])), key: r.key || {}, busy: !!r.busy, firstRun: !!r.first_run, revision: r.revision, loaded: true, err: null };
  if (!first) for (const s of sources) {
    const was = before.byId.get(s.id);
    if (was && was.state !== "failed" && s.state === "failed")
      toast(`${s.title} could not be ${s.removing ? "removed" : "added"}.`, 7000, async () => openRecords(s.id), "See why");
  }
  if (!first && !wasRemoving && sources.some(s => s.removing)) announce("Removing records. You can keep using the map.");
  // One note when the whole queue is done, not one per source.
  if (sources.some(s => REC_BUSY.has(s.state) && !s.removing)) {
    if (!first && !recWasAdding) announce("Adding records. You can keep using the map.");
    recWasAdding = true;
  }
  else if (recWasAdding) {
    recWasAdding = false;
    if (!sources.some(s => s.state === "failed")) toast("The records are ready");
  }
  if (!first && before.revision !== r.revision) reloadEvidence();
  // A phone starts with the sheet tucked away; on the very first run it opens so the welcome is seen.
  if (first && S.rec.firstRun && !isDesk() && S.view.name === "home" && S.sheet === "peek") setSheet("half");
  recRefreshView(); scheduleRecordsPoll();
}
// The stored records changed: forget the cached cells and fetch the view again, with the source and layer lists.
let evGen = 0; // answers to requests made before the last reload are dropped (see pump)
function reloadEvidence() {
  evGen++;
  evById.clear(); pnById.clear(); cellState.clear(); pendingQ = []; fullById.clear();
  loadView(); flushSoon("e"); flushSoon("p");
  loadSources(); checkEmptyKinds();
}
async function recAdd(ids, extra = {}) {
  if (S.view.name === "records") S.view.ask = null;
  try { applyRecords(await api("/api/records/add", { method: "POST", body: { ids, ...extra } })); }
  catch (e) { toast("Could not add: " + e.message, 7000); recRefreshView(true); }
}
// Stops sources that are queued or running (what was downloaded is kept), or clears a failure.
async function recCancel(ids) {
  try { applyRecords(await api("/api/records/cancel", { method: "POST", body: { ids } })); }
  catch (e) { toast("Could not stop: " + e.message, 5000); }
}
async function recRemove(id) {
  const s = S.rec.byId.get(id);
  if (!s) return;
  if (S.view.name === "records") S.view.ask = null;
  try { applyRecords(await api("/api/records/remove", { method: "POST", body: { ids: [id], cascade: s.needed_by.length > 0 } })); }
  catch (e) { toast("Could not remove: " + e.message, 6000); recRefreshView(true); return; }
  const now = S.rec.byId.get(id);
  if (now?.state === "available") toast(`Removed ${s.title}`);
}
function openRecords(sel = null, ask = null) {
  const from = document.activeElement;
  closePopovers();
  show("records", { sel, ask });
  if (from && from !== document.body && !$("#panel").contains(from)) S.opener = from;
  focusCard();
  loadRecords();
  if (sel) reveal($(`#view [data-lib-row="${CSS.escape(sel)}"]`));
}

// ---- words
const kmWide = a => (a[2] - a[0]) * 111.32 * Math.cos((a[1] + a[3]) / 2 * Math.PI / 180), kmTall = a => (a[3] - a[1]) * 110.6;
const sizeWords = a => `about ${fmtInt(Math.max(1, Math.round(kmWide(a))))} km wide and ${fmtInt(Math.max(1, Math.round(kmTall(a))))} km tall`;
// The part of the map on screen that the air strikes can be added for ([w, s, e, n], to about 100 m), or null
// when the view lies outside the region the records cover.
function strikeArea() {
  const v = viewBounds(), lim = S.rec.byId.get("thor")?.area_choice?.within;
  if (!v || !lim) return null;
  const a = [Math.max(v[0], lim[0]), Math.max(v[1], lim[1]), Math.min(v[2], lim[2]), Math.min(v[3], lim[3])].map(x => +x.toFixed(3));
  return a[0] < a[2] && a[1] < a[3] ? a : null;
}
function strikeAreaWords() {
  const a = strikeArea();
  if (!a) return "Move the map over Vietnam, Laos or Cambodia first.";
  const big = kmWide(a) > BIG_AREA_KM || kmTall(a) > BIG_AREA_KM;
  return `The map now shows an area ${sizeWords(a)}.${big ? " That may hold too many strikes: zoom in for a result you can count on." : ""}`;
}
const dayWords = iso => { const d = new Date(iso); return isNaN(d) ? "" : `${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`; };
const keyAskWords = () => S.rec.key.present ? "Kaggle did not accept the saved key" : "Kaggle asked for a key";
// The second line of a source's row: what is happening to it, how many records it holds, or what adding it costs.
function recMeta(s) {
  if (REC_BUSY.has(s.state)) return s.status_text || (s.removing ? "Removing these records" : "Waiting for its turn");
  if (s.state === "failed") return s.removing ? "Could not be removed" : s.wants_key ? keyAskWords() : "Could not be added";
  if (s.state === "added") return `${fmtInt(s.records)} record${s.records === 1 ? "" : "s"}`;
  return [s.size_mb ? `${fmtInt(s.size_mb)} MB` : "", s.time_text].filter(Boolean).join(" · ");
}
// One line for the home card: what is being added now, or what is on this computer.
function recHomeLine() {
  if (!S.rec.loaded) return "";
  const busy = recBusy(), now = busy.find(s => s.state === "running") || busy[0];
  if (now) return `${now.removing ? "Removing" : "Adding"} ${now.title}${now.status_text && now.state === "running" ? ": " + now.status_text : ""}`;
  if (S.rec.busy) return "Being changed in another window";
  const added = S.rec.sources.filter(s => s.state === "added");
  if (!added.length) return "None yet. Add place names, bases, losses and more";
  return `${added.length} source${added.length === 1 ? "" : "s"} · ${fmtInt(added.reduce((a, s) => a + (s.records || 0), 0))} records`;
}
// "A", "A and B", "A, B and C".
const listWords = l => l.length < 2 ? l.join("") : `${l.slice(0, -1).join(", ")} and ${l.at(-1)}`;
const stdToAdd = () => S.rec.sources.filter(s => s.standard && (s.state === "available" || s.state === "failed"));
const stdMB = list => list.reduce((a, s) => a + (s.size_mb || 0), 0);
// "about 15 minutes" for a list of sources, from each one's own estimate, rounded up to five minutes.
function stdTime(list) {
  const mins = list.reduce((a, s) => a + (+(/(\d+) minute/.exec(s.time_text || "") || [0, 0])[1]), 0);
  return mins ? `about ${Math.ceil(mins / 5) * 5} minutes` : "a while";
}

// ---- the card
// Everything the card's HTML depends on except progress and status words, which are patched in place (recPatch).
function recSig() {
  return JSON.stringify([S.rec.loaded, S.rec.err, S.rec.busy, S.rec.key, S.view.sel, S.view.ask,
    S.rec.sources.map(s => [s.id, s.state, s.removing, s.wants_key, s.error, s.records, s.held_in_cases, s.area_text, s.disk_bytes > 0, s.progress == null])]);
}
let recLastSig = "";
function recRefreshView(force = false) {
  if (S.view.name === "home") return homeRefresh();
  if (S.view.name !== "records") return;
  if (force || recSig() !== recLastSig) rerenderKeepingFocus();
  else recPatch();
}
function recPatch() {
  for (const s of recBusy()) {
    const id = CSS.escape(s.id), bar = $(`#view [data-prog="${id}"]`);
    if (bar) bar.style.strokeDashoffset = ringOffset({ state: s.state === "queued" ? "queued" : "", progress: s.progress });
    $$(`#view [data-prog-t="${id}"]`).forEach(t => { t.textContent = recMeta(s); });
  }
  const area = $("#view [data-rec-area]");
  if (area) { area.textContent = strikeAreaWords(); const go = $('#view [data-act="recAddArea"]'); if (go) go.disabled = !strikeArea(); }
}
// The control at the end of a row: Add, Retry, a progress ring that stops it, or a chevron for a source that is
// on this computer (its row opens to Update and Remove).
function recControl(s) {
  const t = esc(s.title), id = esc(s.id);
  if (s.state === "available") return `<button class="lib-pill" data-act="recAdd" data-id="${id}" aria-label="Add ${t}">Add</button>`;
  if (s.state === "failed") return `<button class="lib-pill" data-act="recRetry" data-id="${id}" aria-label="Try ${t} again">Retry</button>`;
  if (REC_BUSY.has(s.state)) {
    const wait = s.state === "queued" || s.progress == null;
    return `<button class="lib-ring" data-act="recStop" data-id="${id}" aria-label="Stop ${s.removing ? "removing" : "adding"} ${t}" title="Stop"${s.removing ? " disabled" : ""}>
      <svg viewBox="0 0 24 24" class="${wait ? "wait" : ""}" aria-hidden="true"><circle class="track" cx="12" cy="12" r="9"/>
      <circle class="bar" data-prog="${id}" cx="12" cy="12" r="9" style="stroke-dasharray:${RING_C};stroke-dashoffset:${ringOffset({ state: wait ? "queued" : "", progress: s.progress })}"/>
      ${s.removing ? "" : '<rect class="stop" x="9" y="9" width="6" height="6" rx="1"/>'}</svg></button>`;
  }
  return `<span class="rec-chev" aria-hidden="true">${icon.chevR}</span>`;
}
// How to get a Kaggle key and where to put it. The file is read here in the browser; only the user name and key
// in it are sent on to the local server, which saves them on this computer.
const keyBox = (asked = true) => `<div class="confirm-box calm key-box">
    <b>${asked ? keyAskWords() : "Add your Kaggle key"}</b>
    <ol class="steps"><li>Sign in at <a href="https://www.kaggle.com/settings" target="_blank" rel="noopener">kaggle.com</a>. An account is free.</li>
      <li>Open Settings and, under API, click “Create Legacy API Key”. A file called kaggle.json is downloaded.</li>
      <li>Choose that file here.</li></ol>
    <div class="row"><button class="btn small primary" data-act="recKeyPick">Choose kaggle.json</button><input type="file" id="keyFile" accept=".json,application/json" hidden></div>
    <details><summary>Or type the user name and key</summary>
      <form id="keyForm" class="stack" autocomplete="off">
        <label class="field"><span>Kaggle user name</span><input type="text" name="username" required autocapitalize="none" spellcheck="false"></label>
        <label class="field"><span>Key</span><input type="password" class="txt" name="key" required autocomplete="off"></label>
        <div class="row"><button class="btn small primary" type="submit">${asked ? "Save and add the air strikes" : "Save the key"}</button></div></form></details>
    <div class="err" id="keyErr" role="alert"></div>
    <p class="muted small-t">The key is saved on this computer and is only ever sent to Kaggle.${S.rec.key.present && S.rec.key.source === "env file" ? ` <button class="linkish" data-act="recKeyForget">Forget the saved key</button>` : ""}</p>
    ${asked ? "" : `<div class="row"><button class="btn small" data-act="recAskCancel">Cancel</button></div>`}</div>`;
// The opened row: what the source is, what it costs or holds, and every action on it.
function recDetail(s) {
  const t = esc(s.title), id = esc(s.id), ask = S.view.ask, busy = REC_BUSY.has(s.state);
  const facts = s.state === "added"
    ? [dayWords(s.added_at) ? `Added ${dayWords(s.added_at)}` : "", s.disk_bytes ? `keeps ${fmtMB(s.disk_bytes)} of downloads` : ""].filter(Boolean).join(" · ")
    : !busy ? [s.size_mb ? `Downloads about ${fmtInt(s.size_mb)} MB` : "Nothing to download", s.time_text].filter(Boolean).join(" · ") : "";
  const also = s.state !== "added" && s.also_adds.length ? `Adding it also adds ${listWords(s.also_adds.map(x => `“${esc(x.title)}”`))}, which it is made from.` : "";
  let box = "";
  if (s.id === "thor" && s.wants_key && s.state === "failed") box = keyBox();
  else if (ask === "key:" + s.id) box = keyBox(false);
  else if (ask === "area:" + s.id) box = `<div class="confirm-box calm" role="group" aria-label="Choose the area for the air strikes">
      <span>Air strikes are added for one area at a time, up to ${fmtInt(s.area_choice.max_records)} strikes. Move and zoom the map until it shows the area you want.</span>
      <span class="muted" data-rec-area>${esc(strikeAreaWords())}</span>
      <div class="row"><button class="btn small primary" data-act="recAddArea" data-id="${id}"${strikeArea() ? "" : " disabled"}>${s.state === "added" ? "Replace with strikes for this area" : "Add strikes for this area"}</button>
      <button class="btn small" data-act="recAskCancel">Cancel</button></div></div>`;
  else if (ask === "accept:" + s.id) box = `<div class="confirm-box calm" role="alertdialog" aria-label="Add ${t}">
      <span>${esc(s.warning)}</span>
      <div class="row"><button class="btn small primary" data-act="recAccept" data-id="${id}">Add ${t.toLowerCase()}</button><button class="btn small" data-act="recAskCancel">Cancel</button></div></div>`;
  else if (ask === "remove:" + s.id) box = `<div class="confirm-box" role="alertdialog" aria-label="Remove ${t}">
      <span>Remove <b>${t}</b> from this computer? Its ${fmtInt(s.records)} records leave the map${s.disk_bytes ? ` and ${fmtMB(s.disk_bytes)} of downloads are deleted` : ""}.
        ${s.needed_by.length ? `This also removes ${listWords(s.needed_by.map(x => `<b>${esc(x.title)}</b>`))}, which ${s.needed_by.length === 1 ? "is" : "are"} made from it.` : ""}
        Records you put in a case are kept. You can add it again later.</span>
      <div class="row"><button class="btn small danger solid" data-act="recRemove" data-id="${id}">Remove</button><button class="btn small" data-act="recAskCancel">Keep it</button></div></div>`;
  // Where the strikes are, in words a person can picture; "Zoom to its area" shows it on the map.
  const box4 = s.id === "thor" && s.state === "added" && s.options?.area;
  const area = box4 ? `<p>Covers ${/^Around /.test(s.area_text || "") ? "the area a" + s.area_text.split(":")[0].slice(1) : "one area"}, ${esc(sizeWords(box4))}.</p>` : "";
  let acts = "";
  if (s.state === "added") acts = `${s.id === "thor" ? `<button class="btn small" data-act="recFitArea">Zoom to its area</button><button class="btn small" data-act="recAskArea" data-id="${id}">Change area</button>` : ""}
      ${s.standard ? `<button class="btn small" data-act="recUpdate" data-id="${id}" title="Download it again, for corrections made since">Update</button>` : ""}
      <button class="btn small danger" data-act="recAskRemove" data-id="${id}">${icon.trash}Remove</button>`;
  else if (s.state === "failed") acts = `<button class="btn small" data-act="recDismiss" data-id="${id}">${s.wants_key ? "Not now" : "Dismiss"}</button>`;
  const savedKey = s.id === "thor" && S.rec.key.present && S.rec.key.source === "env file" && !(s.wants_key && s.state === "failed")
    ? `<p class="muted">A Kaggle key is saved on this computer. <button class="linkish" data-act="recKeyForget">Forget it</button></p>`
    : s.id === "thor" && !S.rec.key.present && !s.wants_key && ask !== "key:thor"
      ? `<p class="muted">Kaggle may ask for a free key. <button class="linkish" data-act="recAskKey">Add a Kaggle key</button></p>` : "";
  return `<div class="lib-detail" id="recd-${id}">
    <p>${esc(s.description)}</p>
    ${area}
    ${facts ? `<p class="muted">${esc(facts)}</p>` : ""}
    ${also ? `<p class="muted">${also}</p>` : ""}
    ${s.held_in_cases ? `<p class="muted">${fmtInt(s.held_in_cases)} of its records ${s.held_in_cases === 1 ? "is" : "are"} kept because a case holds ${s.held_in_cases === 1 ? "it" : "them"}.</p>` : ""}
    ${s.state === "failed" && s.error && !s.wants_key ? `<p class="err">${esc(s.error)}</p>${s.error_detail ? `<details><summary>Details</summary><p class="muted mono">${esc(s.error_detail)}</p></details>` : ""}` : ""}
    ${acts ? `<div class="row">${acts}</div>` : ""}
    ${box}${savedKey}
    ${s.licence ? `<details><summary>Licence</summary><p class="muted">${esc(s.licence)}</p></details>` : ""}</div>`;
}
function recRow(s) {
  const sel = S.view.sel === s.id, id = esc(s.id);
  return `<li class="lib-row${sel ? " sel" : ""}" data-lib-row="${id}">
    <button class="main" data-act="recSel" data-id="${id}" aria-expanded="${sel}"${sel ? ` aria-controls="recd-${id}"` : ""}>
      <span class="t">${esc(s.title)}</span><span class="m${s.state === "failed" ? " bad" : ""}" data-prog-t="${id}">${esc(recMeta(s))}</span></button>
    ${recControl(s)}${sel ? recDetail(s) : ""}</li>`;
}
VIEWS.records = () => {
  recLastSig = recSig();
  const src = S.rec.sources, by = st => src.filter(s => s.state === st);
  const busy = recBusy(), added = by("added"), failed = by("failed"), available = by("available");
  const total = added.reduce((a, s) => a + (s.records || 0), 0);
  const sub = !S.rec.loaded ? "Looking…" : S.rec.err ? "The list of records could not be loaded"
    : added.length ? `${fmtInt(total)} records from ${added.length} source${added.length === 1 ? "" : "s"} on this computer` : "No records on this computer yet";
  const rows = list => `<div class="group"><ul class="list lib-list">${list.map(recRow).join("")}</ul></div>`;
  const list = (title, l) => l.length ? `<h3 class="sub-head">${title}</h3>${rows(l)}` : "";
  const std = stdToAdd(), adding = busy.filter(s => !s.removing).length;
  const bulk = std.length >= 2 ? `<button class="btn lib-bulk" data-act="recAddStd">${icon.plus}<span>Add the standard records</span><span class="muted">${fmtInt(stdMB(std))} MB</span></button>` : "";
  return `${headHTML("Records", `<span>${esc(sub)}</span>`)}
    ${busy.length ? `<div class="lib-status"><span class="spin" aria-hidden="true"></span>
      <span class="grow"><b>${adding ? `Adding ${adding} source${adding === 1 ? "" : "s"}` : "Removing"}</b><span class="small-t muted">One at a time. You can keep using the map.</span></span>
      ${adding > 1 ? `<button class="linkish small-t" data-act="recStopAll">Stop all</button>` : ""}</div>${rows(busy)}` : ""}
    ${!busy.length && S.rec.busy ? `<div class="lib-status"><span class="spin" aria-hidden="true"></span><span class="grow"><b>Busy in another window</b><span class="small-t muted">Records are being changed from a terminal or another Map Vietnam window.</span></span></div>` : ""}
    ${list("Could not be added", failed)}
    ${list("On this computer", added)}
    ${available.length ? `<h3 class="sub-head">Available</h3>${bulk}${rows(available)}` : ""}
    <p class="foot-note">Records come from public archives and stay on this computer. Nothing downloads until you add it. Records can be wrong: check each source.</p>`;
};
function wireKeyBox() {
  // The key, from the file Kaggle hands out or typed in. Neither is kept in the page afterwards.
  const err = msg => { const e = $("#keyErr"); if (e) e.textContent = msg; };
  $("#keyFile")?.addEventListener("change", async e => {
    const file = e.target.files[0];
    if (!file) return;
    let cfg = null;
    try { cfg = JSON.parse(await file.text()); } catch { /* not JSON: reported below */ }
    e.target.value = "";
    if (!cfg || typeof cfg.username !== "string" || typeof cfg.key !== "string") return err("That is not a kaggle.json file: it should hold a user name and a key. Download a new one from Kaggle and choose it again.");
    saveKey(cfg.username, cfg.key, err);
  });
  $("#keyForm")?.addEventListener("submit", e => {
    e.preventDefault();
    const f = e.target; saveKey(f.username.value.trim(), f.key.value.trim(), err);
  });
}
VIEWS.records.after = wireKeyBox; // (the same key box also appears in settings)
// Saves the key on this computer, then tries the air strikes again for the area that was asked for.
async function saveKey(username, key, err) {
  try { applyRecords(await api("/api/records/key", { method: "POST", body: { username, key } })); }
  catch (e) { return err(e.message); }
  const thor = S.rec.byId.get("thor");
  toast("Key saved on this computer");
  if (S.view.name === "records" && S.view.ask === "key:thor") { S.view.ask = null; rerenderKeepingFocus(); }
  if (S.view.name === "settings") { S.view.keyOpen = false; settingsRefresh(true); }
  if (thor && thor.state === "failed") recAdd(["thor"], thor.options ? { options: { thor: thor.options } } : {});
}
function recSelect(id) {
  const v = S.view;
  if (v.name !== "records") return;
  v.sel = v.sel === id ? null : id; v.ask = null;
  rerenderKeepingFocus();
  if (v.sel) reveal($(`#view [data-lib-row="${CSS.escape(id)}"]`));
}
// Opens a row on one of its questions (the area, the long-download warning, removal) and puts focus on its answer.
function recAsk(id, ask) {
  if (S.view.name !== "records") return openRecords(id, `${ask}:${id}`);
  S.view.sel = id; S.view.ask = `${ask}:${id}`;
  rerenderKeepingFocus();
  const box = $(`#view [data-lib-row="${CSS.escape(id)}"] .confirm-box`);
  box?.querySelector("button:not([disabled])")?.focus({ preventScroll: true });
  reveal(box);
}
ACTIONS.openRecords = () => openRecords();
ACTIONS.recSel = el => recSelect(el.dataset.id);
ACTIONS.recAdd = el => {
  const s = S.rec.byId.get(el.dataset.id);
  if (!s) return;
  if (s.id === "thor") recAsk(s.id, "area");
  else if (s.opt_in) recAsk(s.id, "accept");
  else recAdd([s.id]);
};
ACTIONS.recRetry = el => {
  const s = S.rec.byId.get(el.dataset.id);
  if (!s) return;
  if (s.wants_key) { if (S.view.sel !== s.id) recSelect(s.id); return; } // the key box is the way forward
  if (s.removing) return recRemove(s.id);
  recAdd([s.id], { ...(s.opt_in ? { accept: [s.id] } : {}), ...(s.options ? { options: { [s.id]: s.options } } : {}) });
};
ACTIONS.recAddArea = el => { const area = strikeArea(); if (area) recAdd([el.dataset.id], { options: { [el.dataset.id]: { area } } }); };
ACTIONS.recAccept = el => recAdd([el.dataset.id], { accept: [el.dataset.id] });
ACTIONS.recAskArea = el => recAsk(el.dataset.id, "area");
ACTIONS.recAskRemove = el => recAsk(el.dataset.id, "remove");
ACTIONS.recAskCancel = () => { S.view.ask = null; S.view.keyOpen = false; rerenderKeepingFocus(); };
ACTIONS.recRemove = el => recRemove(el.dataset.id);
ACTIONS.recUpdate = el => { const s = S.rec.byId.get(el.dataset.id); if (s) recAdd([s.id], { refresh: true, ...(s.opt_in ? { accept: [s.id] } : {}) }); };
ACTIONS.recStop = el => recCancel([el.dataset.id]);
ACTIONS.recDismiss = el => recCancel([el.dataset.id]);
ACTIONS.recStopAll = () => recCancel(recBusy().filter(s => !s.removing).map(s => s.id));
ACTIONS.recAddStd = () => recAdd(stdToAdd().map(s => s.id));
ACTIONS.recFitArea = () => {
  const a = S.rec.byId.get("thor")?.options?.area;
  if (!a) return;
  if (!isDesk()) setSheet("peek");
  primary().fitBounds([[a[0], a[1]], [a[2], a[3]]], { padding: mapPadding(), duration: reduceMotion ? 0 : 600 });
};
ACTIONS.recKeyPick = () => $("#keyFile")?.click();
ACTIONS.recAskKey = () => { closePopovers(); recAsk("thor", "key"); };
ACTIONS.outlines = el => setOutlines(el.dataset.on === "1");
ACTIONS.recKeyForget = async () => {
  try { applyRecords(await api("/api/records/key", { method: "DELETE" })); toast("The key was removed from this computer"); if (!$("#popStyle").hidden) renderStylePop(); settingsRefresh(true); }
  catch (e) { toast("Could not remove the key: " + e.message, 5000); }
};

// ---- first run, and quitting
// The first time, the home card offers the standard records in one tap. Nothing downloads before that tap.
function welcomeHTML() {
  if (!S.rec.loaded || !S.rec.firstRun || S.rec.busy) return "";
  const std = stdToAdd();
  if (!std.length) return "";
  return `<div class="welcome"><h2>Add records to the map</h2>
    <p>Place names, wartime hamlets, bases, battles and casualty records, from public archives. About ${fmtInt(stdMB(std))} MB to download. It takes ${stdTime(std)}, and you can use the map meanwhile.</p>
    <div class="row"><button class="btn primary" data-act="welcomeAdd">Add the standard records</button><button class="btn" data-act="welcomeChoose">Choose which</button></div>
    <button class="linkish small-t" data-act="welcomeSkip">Not now</button></div>`;
}
const dismissWelcome = () => api("/api/records/welcome", { method: "POST", body: { dismissed: true } }).then(applyRecords).catch(() => {});
ACTIONS.welcomeAdd = async () => { await recAdd(stdToAdd().map(s => s.id)); dismissWelcome(); };
ACTIONS.welcomeChoose = () => { dismissWelcome(); openRecords(); };
ACTIONS.welcomeSkip = () => { S.rec.firstRun = false; render(); dismissWelcome(); };
// The home card shows a line about the records; it is patched in place while that is all that changed, so typing
// in the new-case form is never interrupted by a poll.
const homeSig = () => JSON.stringify([S.rec.loaded, S.rec.firstRun, S.rec.busy, recBusy().length]);
let homeLastSig = "";
function homeRefresh() {
  const line = $("#view [data-rec-line]");
  if (line && homeSig() === homeLastSig) { line.textContent = recHomeLine(); return; }
  // Typing in the new-case form is never interrupted: the line is patched and the rest waits for the next change.
  if (document.activeElement?.closest?.("#view input, #view textarea")) { if (line) line.textContent = recHomeLine(); return; }
  rerenderKeepingFocus();
}
ACTIONS.quitAsk = () => { S.view.quit = true; render(); $('#view [data-act="quit"]')?.focus(); };
ACTIONS.quitCancel = () => { S.view.quit = false; render(); $('#view [data-act="quitAsk"]')?.focus(); };
// Stops the local server. The page cannot go on without it, so it is replaced by a note saying how to start again.
ACTIONS.quit = async () => {
  try { await api("/api/quit", { method: "POST", body: {} }); }
  catch (e) { toast("Could not quit: " + e.message, 5000); return; }
  stopped = true; clearTimeout(recTimer); clearTimeout(libTimer);
  $("#gone").hidden = false;
  $("#goneTitle").focus();
};

// ================================================================ cases
// A case gathers records, candidate sites (a point with a search radius) and notes, and exports them as
// GPX / KML / GeoJSON. Every change returns the updated case from the server, which replaces S.kase.
async function loadCases() { try { S.cases = await api("/api/cases"); } catch { S.cases = []; } }
async function selectCase(id) {
  S.caseId = id || null; store.set("caseId", S.caseId); S.kase = null;
  if (id) { try { S.kase = await api(`/api/cases/${id}`); } catch { S.caseId = null; store.set("caseId", null); } }
  drawCase();
}
function drawCase() {
  const sites = S.kase?.sites || [];
  setData("sites", { type: "FeatureCollection", features: sites.map(s => ({ type: "Feature", geometry: { type: "Point", coordinates: [s.lon, s.lat] },
    properties: { id: s.id, name: s.name, uncertainty_m: s.uncertainty_m, r0: r0Of(s.uncertainty_m, s.lat) } })) });
}
VIEWS.case = () => {
  const c = S.kase;
  if (!c) return headHTML("Case") + `<p class="muted">This case is no longer available.</p>`;
  return `${headHTML(esc(c.name), `<span class="sw site"></span><span>Case · ${c.items.length} record${c.items.length === 1 ? "" : "s"} · ${c.sites.length} site${c.sites.length === 1 ? "" : "s"}</span>`)}
    ${c.summary ? `<p style="margin:8px 0 0">${esc(c.summary)}</p>` : ""}
    <div class="actions">
      <button class="act primary" data-act="place"><span class="ci">${icon.pin}</span><span>Place site</span></button>
      <button class="act" data-act="scrollTo" data-to="caseNotes"><span class="ci">${icon.note}</span><span>Notes</span></button>
      <button class="act" data-act="scrollTo" data-to="caseExport"><span class="ci">${icon.dl}</span><span>Export</span></button>
      <button class="act" data-act="closeCase" aria-label="Close case (it stays saved)" title="Close this case; everything in it stays saved"><span class="ci">${icon.folder}</span><span>Close case</span></button>
    </div>
    <h3 class="section-title">Candidate sites <span class="right">${c.sites.length}</span></h3>
    <div class="group"><ul class="list">${c.sites.map(s => `<li><span class="sw site"></span>
      <button class="main" data-act="site" data-id="${s.id}"><span class="t">${esc(s.name)}</span><span class="m">±${fmtM(s.uncertainty_m)}${s.rationale ? " · " + esc(s.rationale) : ""}</span></button></li>`).join("")
      || `<li class="empty">No sites yet. Use “Place site”, or open a record or grid square and choose “Place candidate site here”.</li>`}</ul></div>
    <h3 class="section-title">Evidence <span class="right">${c.items.length}</span></h3>
    <div class="group"><ul class="list">${c.items.map(e => `<li>${symSVG(e.kind)}
      <button class="main" data-act="rec" data-id="${e.id}"><span class="t">${esc(cleanTitle(e.title))}</span><span class="m">${esc(kindOne(e))} · ${esc(fmtDate(e.date_start, e.date_end))} · ±${fmtM(e.uncertainty_m || 0)}${e.withdrawn_reason ? ` · <em>withdrawn: ${esc(e.withdrawn_reason)}</em>` : ""}</span></button>
      <button class="round ghost small" data-act="rmItem" data-id="${e.id}" aria-label="Remove ${esc(cleanTitle(e.title))} from case" title="Remove from case">${icon.close}</button></li>`).join("")
      || `<li class="empty">Open a record on the map, then “Add to case”.</li>`}</ul></div>
    <h3 class="section-title" id="caseNotes">Notes <span class="right">${c.notes.length}</span></h3>
    <div class="group"><div class="inner stack" style="padding-top:12px">
      ${c.notes.map(n => `<div class="row" style="align-items:flex-start"><p style="margin:0;flex:1"><span class="small-t muted">${esc(fmtDay((n.created_at || "").slice(0, 10)))}</span><br>${esc(n.body)}</p>
        <button class="round ghost small" data-act="rmNote" data-id="${n.id}" aria-label="Delete note" title="Delete note">${icon.close}</button></div>`).join("")}
      <form id="noteForm" class="stack"><textarea class="txt" name="body" placeholder="Add a note: who said what, where, when" aria-label="New note"></textarea>
        <div><button class="btn small" type="submit">Add note</button></div></form></div></div>
    <h3 class="section-title" id="caseExport">Field export</h3>
    <div class="group"><div class="inner" style="padding-top:12px">
      <div class="row">${["gpx", "kml", "geojson"].map(f => `<a class="btn small" href="/api/cases/${c.id}/export.${f}" download="${esc(c.name)}.${f}">${icon.dl} ${f.toUpperCase()}</a>`).join("")}</div>
      <p class="quiet-note">${icon.warn}<span>Exports list casualty, aircraft-loss and air-strike locations. Strike areas may hold unexploded ordnance. Think about who will read an export before you share it.</span></p></div></div>
    <div id="delSlot" style="margin-top:22px"><button class="btn danger small" data-act="delAsk">${icon.trash} Delete case…</button></div>`;
};
VIEWS.case.after = () => {
  $("#noteForm")?.addEventListener("submit", async e => {
    e.preventDefault();
    const body = e.target.body.value.trim(); if (!body) return;
    try { S.kase = await api(`/api/cases/${S.kase.id}/notes`, { method: "POST", body: { body } }); render(); toast("Note added"); } catch (x) { toast(x.message); }
  });
};
ACTIONS.scrollTo = el => jumpTo($("#" + el.dataset.to));
ACTIONS.closeCase = async () => { const n = S.kase?.name; await selectCase(null); show("home"); toast(`“${n}” saved`); };
ACTIONS.rmNote = async el => {
  const cid = S.kase.id, note = S.kase.notes.find(n => n.id === +el.dataset.id);
  try {
    S.kase = await api(`/api/cases/${cid}/notes/${note.id}`, { method: "DELETE" }); render();
    toast("Note deleted", 6000, async () => { S.kase = await api(`/api/cases/${cid}/notes`, { method: "POST", body: { body: note.body } }); render(); });
  } catch (x) { toast(x.message); }
};
ACTIONS.rmItem = async el => {
  const cid = S.kase.id, eid = +el.dataset.id;
  try {
    S.kase = await api(`/api/cases/${cid}/items/${eid}`, { method: "DELETE" }); render();
    toast("Removed from case", 6000, async () => { S.kase = await api(`/api/cases/${cid}/items`, { method: "POST", body: { evidence_id: eid } }); drawCase(); render(); });
  } catch (x) { toast(x.message); }
};
ACTIONS.delAsk = () => {
  const c = S.kase, slot = $("#delSlot");
  slot.innerHTML = `<div class="confirm-box" role="alertdialog" aria-labelledby="delQ"><div id="delQ"><b>Delete “${esc(c.name)}”?</b><br>
    <span class="small-t muted">Its ${c.items.length} linked record${c.items.length === 1 ? "" : "s"}, ${c.sites.length} site${c.sites.length === 1 ? "" : "s"} and ${c.notes.length} note${c.notes.length === 1 ? "" : "s"} go with it. The evidence itself stays. This can’t be undone.</span></div>
    <div class="row"><button class="btn danger solid small" id="delYes">Delete case</button><button class="btn small" id="delNo">Keep</button></div><div class="err" role="alert"></div></div>`;
  $("#delNo").focus();
  $("#delNo").addEventListener("click", () => render());
  $("#delYes").addEventListener("click", async () => {
    try { await api(`/api/cases/${c.id}`, { method: "DELETE" }); await selectCase(null); await loadCases(); show("home"); toast(`Deleted “${c.name}”`); }
    catch (x) { slot.querySelector(".err").textContent = x.message; }
  });
};
ACTIONS.site = el => openSite(+el.dataset.id);
ACTIONS.place = () => startPlacing();

function openSite(id) {
  const s = S.kase?.sites.find(x => x.id === id);
  if (!s) return;
  setTarget({ lon: s.lon, lat: s.lat, m: s.uncertainty_m });
  show("site", { id, s });
}
VIEWS.site = v => `${headHTML(esc(v.s.name), `<span class="sw site"></span><span>Candidate site · ${esc(S.kase?.name || "")}</span>`)}
  <div id="navSlot">${navHTML()}</div>
  <div class="group" style="margin-top:14px">
    <div class="kv-row"><span class="k">Search radius</span><span class="v">${fmtM(v.s.uncertainty_m)}</span></div>
    <div class="kv-row"><span class="k">Why here</span><span class="v">${esc(v.s.rationale || "—")}</span></div>
    <div class="kv-row"><span class="k">Position (WGS84)</span><span class="v mono">${v.s.lat.toFixed(5)}, ${v.s.lon.toFixed(5)}</span></div>
  </div>
  <div class="group" id="grids"></div>
  <div class="row" style="margin-top:14px"><button class="btn small" data-act="go" data-lon="${v.s.lon}" data-lat="${v.s.lat}" data-m="${v.s.uncertainty_m}">Show on map</button>
    <span id="rmSite"><button class="btn small danger" data-act="rmSiteAsk">Remove site…</button></span></div>`;
VIEWS.site.after = async v => {
  try { const r = await api(`/api/lonlat?lon=${v.s.lon}&lat=${v.s.lat}`); if (S.view === v) { $("#grids").innerHTML = gridRows(r); wire($("#grids")); } } catch { $("#grids").remove(); }
};
ACTIONS.rmSiteAsk = () => {
  $("#rmSite").innerHTML = `<span class="row"><span class="small-t">Remove this site?</span><button class="btn small danger solid" id="rmYes">Remove</button><button class="btn small" id="rmNo">Keep</button></span>`;
  $("#rmNo").focus();
  $("#rmYes").addEventListener("click", async () => {
    const site = S.view.s, cid = S.kase.id;
    try {
      S.kase = await api(`/api/cases/${cid}/sites/${site.id}`, { method: "DELETE" }); drawCase(); S.history = []; show("case", { id: cid }, { push: false });
      toast("Site removed", 6000, async () => {
        S.kase = await api(`/api/cases/${cid}/sites`, { method: "POST", body: { name: site.name, lon: site.lon, lat: site.lat, uncertainty_m: site.uncertainty_m, rationale: site.rationale || "" } });
        drawCase(); if (S.view.name === "case") render();
      });
    }
    catch (x) { toast(x.message); }
  });
  $("#rmNo").addEventListener("click", () => render());
};

// ---- placing / site form (inline, draggable pin, live radius ring)
function startPlacing() {
  if (S.compare) setCompare(false);
  S.placing = true; document.body.classList.add("placing"); $("#placing").hidden = false;
  $("#placing span").textContent = coarse ? "Tap the map where the candidate site is" : "Click the map where the candidate site is";
  if (!isDesk()) setSheet("peek");
}
function endPlacing() { S.placing = false; document.body.classList.remove("placing"); $("#placing").hidden = true; }
$("#placingCancel").addEventListener("click", endPlacing);

let draftMarker = null;
async function openSiteForm(lon, lat, m, name, why = "") {
  if (S.compare) setCompare(false);
  if (!S.kase) await loadCases();
  const n = name || `Candidate site ${(S.kase?.sites.length || 0) + 1}`;
  show("siteForm", {});
  S.draft = { lon, lat, m: Math.max(5, Math.round(m)), name: n, why };
  render();
  draftMarker?.remove();
  const el = document.createElement("div"); el.className = "draft-pin"; el.setAttribute("aria-label", "Drag to move the site"); el.title = "Drag to move";
  draftMarker = new maplibregl.Marker({ element: el, draggable: true, anchor: "bottom-left", offset: [-4, 4] }).setLngLat([lon, lat]).addTo(primary());
  draftMarker.on("drag", () => { const p = draftMarker.getLngLat(); S.draft.lon = p.lng; S.draft.lat = p.lat; drawDraft(); });
  draftMarker.on("dragend", () => updateDraftGrids());
  drawDraft(); updateDraftGrids();
  setTarget({ lon, lat, m: 0 });
  flyToTarget(lon, lat, S.draft.m);
}
function drawDraft() { const d = S.draft; setData("draft", d ? circlePolygon(d.lon, d.lat, d.m) : empty()); const c = $("#draftCoord"); if (c && d) c.textContent = `${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}`; }
function cancelDraft() { S.draft = null; draftMarker?.remove(); draftMarker = null; setData("draft", empty()); }
async function updateDraftGrids() {
  const d = S.draft, el = $("#draftGrid"); if (!d || !el) return;
  try { const r = await api(`/api/lonlat?lon=${d.lon}&lat=${d.lat}`); if ($("#draftGrid")) $("#draftGrid").innerHTML = `Wartime grid <span class="mono">${esc(r.mgrs_indian1960)}</span><br>GPS grid <span class="mono">${esc(r.mgrs_wgs84)}</span>`; } catch { /* optional */ }
}
// The radius slider is logarithmic from 5 m to 5 km; values are rounded to 5 / 10 / 50 m steps by size.
const rToSlider = m => Math.round(Math.log(m / 5) / Math.log(5000 / 5) * 100);
const sliderToR = v => { const r = 5 * Math.pow(5000 / 5, v / 100); return r < 100 ? Math.round(r / 5) * 5 : r < 1000 ? Math.round(r / 10) * 10 : Math.round(r / 50) * 50; };
VIEWS.siteForm = () => {
  const d = S.draft;
  if (!d) return headHTML("New candidate site") + `<p class="muted">Preparing…</p>`;
  return `${headHTML("New candidate site", S.kase ? `<span class="sw site"></span><span>${esc(S.kase.name)}</span>` : "")}
    <form id="siteForm" class="stack" style="margin-top:12px">
      <div class="small-t"><span class="mono" id="draftCoord">${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}</span><div id="draftGrid" class="muted"></div></div>
      ${S.kase ? "" : `<label class="field"><span>Case</span><select name="cid" required><option value="" selected disabled>Choose a case…</option>${S.cases.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}<option value="new">+ New case…</option></select></label>
        <label class="field" id="ncWrap" hidden><span>New case name</span><input type="text" name="newcase"></label>`}
      <label class="field"><span>Site name</span><input type="text" name="name" required value="${esc(d.name)}"></label>
      <div class="field"><span>Search radius</span>
        <div class="radius-row"><input type="range" min="0" max="100" value="${rToSlider(d.m)}" name="rs" aria-label="Search radius">
        <label><input type="number" name="r" min="1" max="50000" value="${d.m}" aria-label="Search radius in metres" style="min-height:44px;width:76px;border-radius:10px;border:1px solid var(--line);background:var(--surface-2);padding:0 8px;font-size:16px"><span class="small-t">m</span></label></div></div>
      <label class="field"><span>Why here?</span><textarea name="why" placeholder="Witness account, crash record, grid ref from a report…">${esc(d.why)}</textarea></label>
      <div class="row"><button class="btn site" type="submit">Save site</button><button class="btn" type="button" data-act="back">Cancel</button></div>
      <div class="err" role="alert"></div></form>`;
};
VIEWS.siteForm.after = () => {
  const f = $("#siteForm"); if (!f) return;
  const setR = (m, from) => { S.draft.m = m; if (from !== "n") f.r.value = m; if (from !== "s") f.rs.value = rToSlider(m); drawDraft(); };
  f.rs.addEventListener("input", () => setR(sliderToR(+f.rs.value), "s"));
  f.r.addEventListener("input", () => { const v = +f.r.value; if (v > 0) setR(v, "n"); });
  f.cid?.addEventListener("change", () => { $("#ncWrap").hidden = f.cid.value !== "new"; });
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const err = f.querySelector(".err"); err.textContent = "";
    const r = +f.r.value, name = f.name.value.trim();
    if (!name) { err.textContent = "Give the site a name."; return; }
    if (!(r > 0)) { err.textContent = "The radius must be more than 0 m."; return; }
    try {
      if (!S.kase) {
        let cid = f.cid.value;
        if (!cid) throw new Error("Choose a case for this site.");
        if (cid === "new") { const n = f.newcase.value.trim(); if (!n) throw new Error("Give the new case a name."); cid = (await api("/api/cases", { method: "POST", body: { name: n, summary: "" } })).id; await loadCases(); }
        await selectCase(+cid);
      }
      const d = S.draft;
      S.kase = await api(`/api/cases/${S.kase.id}/sites`, { method: "POST", body: { name, lon: d.lon, lat: d.lat, uncertainty_m: r, rationale: f.why.value.trim() } });
      cancelDraft(); drawCase(); toast("Site saved"); S.history = []; show("case", { id: S.kase.id }, { push: false });
    } catch (x) { err.textContent = x.message; }
  });
};

// ================================================================ search
// Typing searches after a 150 ms pause. qReq numbers each request so a slow, stale reply is dropped; qItems are
// the listed results and qActive the keyboard-highlighted one.
const q = $("#q"), results = $("#results");
let qTimer, qReq = 0, qItems = [], qActive = -1;
q.addEventListener("focus", () => { if (!isDesk()) { setSheet("full"); $("#qCancel").hidden = false; } });
q.addEventListener("input", () => {
  $("#qClear").hidden = !q.value;
  clearTimeout(qTimer);
  if (q.value.trim().length < 2) { qReq++; closeResults(false); return; }
  // Replace whatever the panel shows (a card, the stacked-records picker) at once, then fill in results.
  if (results.hidden) { results.innerHTML = `<li class="r-empty" role="presentation">Searching…</li>`; results.hidden = false; $("#view").hidden = true; }
  qTimer = setTimeout(runSearch, 150);
});
q.addEventListener("keydown", e => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (!qItems.length) return;
    e.preventDefault(); qActive = (qActive + (e.key === "ArrowDown" ? 1 : -1) + qItems.length) % qItems.length; paintActive();
  } else if (e.key === "Enter") {
    e.preventDefault(); clearTimeout(qTimer);
    if (qItems.length && !results.hidden) pickResult(qItems[Math.max(0, qActive)]); else runSearch(true);
  } else if (e.key === "Escape") { if (!results.hidden) closeResults(); else q.blur(); }
});
$("#qClear").addEventListener("click", () => { q.value = ""; $("#qClear").hidden = true; qReq++; closeResults(false); q.focus(); });
$("#qCancel").addEventListener("click", () => { clearSearch(); q.blur(); $("#qCancel").hidden = true; setSheet(S.view.name === "home" ? "peek" : "half"); });
function clearSearch() { q.value = ""; $("#qClear").hidden = true; qReq++; clearTimeout(qTimer); closeResults(); }

// Accent-free lower case, for matching titles against what was typed.
const fold = t => String(t || "").normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/gi, "d").toLowerCase();
// Wartime district for a GNS name: the district of the nearest HES hamlet (they carry it). Cached per ~5 km cell.
const districtCache = new Map();
async function districtNear(lon, lat) {
  const key = `${(lon / 0.05).toFixed(0)}:${(lat / 0.05).toFixed(0)}`;
  if (!districtCache.has(key)) districtCache.set(key, (async () => {
    const d = 0.045;
    const fc = await api(`/api/evidence?bbox=${[lon - d, lat - d, lon + d, lat + d].map(v => v.toFixed(4)).join(",")}&kinds=placename&slim=1&limit=2000`);
    let best = null, bd = Infinity;
    for (const f of fc.features) {
      const h = hesTail(f.properties.title); if (!h) continue;
      const dd = distBearing([lon, lat], f.geometry.coordinates).d;
      if (dd < bd) { bd = dd; best = h; }
    }
    return best && bd < 6000 ? `${titleCase(best.district)} district (wartime)` : "";
  })().catch(() => ""));
  return districtCache.get(key);
}
// Groups results (grid square, places, places in a same-named wartime district, records), nearest the map centre
// first within each group. pickFirst (Enter before the list arrived) opens the top result directly.
async function runSearch(pickFirst = false) {
  const text = q.value.trim(), req = ++qReq;
  if (text.length < 2) return;
  const c = primary().getCenter();
  try {
    const raw = await api(`/api/search?q=${encodeURIComponent(text)}&near=${c.lng.toFixed(4)},${c.lat.toFixed(4)}&limit=20`);
    if (req !== qReq) return;
    const ft = fold(text);
    for (const it of raw) {
      if (it.type === "gridref") { it.group = "grid"; continue; }
      it.distance_km ??= distBearing([c.lng, c.lat], [it.lon, it.lat]).d / 1000;
      it.dir = compass(distBearing([c.lng, c.lat], [it.lon, it.lat]).b);
      const nameHit = fold(cleanTitle(it.title)).includes(ft);
      it.group = it.kind !== "placename" ? "rec" : nameHit ? "place" : "district";
      it.score = it.distance_km; // strictly nearest first within each group, as the distances on each row say
    }
    const GROUPS = [["grid", ""], ["place", "Places"], ["district", "In a wartime district of that name"], ["rec", "Records"]];
    const items = GROUPS.flatMap(([g]) => raw.filter(it => it.group === g).sort((a, b) => (a.score ?? 0) - (b.score ?? 0))).slice(0, 15);
    qItems = items; qActive = items.length ? 0 : -1;
    if (pickFirst && items.length) return pickResult(items[0]);
    const usedGroups = new Set(items.map(it => it.group));
    const showHeads = [...usedGroups].filter(g => g !== "grid").length > 1;
    let html = items.some(it => it.type !== "gridref") ? `<li class="r-note" role="presentation">Nearest to the map centre first</li>` : "", last = null;
    items.forEach((it, i) => {
      if (showHeads && it.group !== last && it.group !== "grid") html += `<li class="r-head" role="presentation">${esc(GROUPS.find(g => g[0] === it.group)[1])}</li>`;
      last = it.group;
      if (it.type === "gridref") { html += `<li role="option" id="res${i}" data-i="${i}"><span class="r-ico" style="background:#d97706">${icon.grid}</span>
        <div class="r-main"><div class="r-title mono">${esc(text.toUpperCase())}</div><div class="r-meta">Grid square · Indian 1960 (wartime map datum)</div></div>
        <span class="r-dist">${esc(fmtKm(it.distance_km))}</span></li>`; return; }
      const k = KINDS[it.kind] || KINDS.other;
      const ctx = it.context || "";
      const meta = [kindOne(it), ctx, it.date_start ? fmtDay(it.date_start) : ""].filter(Boolean).join(" · ");
      const needDistrict = it.kind === "placename" && !/wartime/.test(ctx);
      html += `<li role="option" id="res${i}" data-i="${i}"><span class="r-ico" style="background:${k.color}">${icon.pin}</span>
        <div class="r-main"><div class="r-title">${esc(cleanTitle(it.title))}</div><div class="r-meta">${esc(meta)}${needDistrict ? `<span class="r-dist-name" data-dl="${i}"></span>` : ""}</div></div>
        <span class="r-dist">${esc(fmtKm(it.distance_km))}<span class="r-dir">${esc(it.dir || "")}</span></span></li>`;
    });
    results.innerHTML = html || `<li class="r-empty">Nothing found for “${esc(text)}”. Try a hamlet name without accents, or a grid reference like WS 683 427.</li>`;
    results.hidden = false; $("#view").hidden = true; q.setAttribute("aria-expanded", "true");
    $$("li[data-i]", results).forEach(li => li.addEventListener("click", () => pickResult(qItems[+li.dataset.i])));
    $("#panelScroll").scrollTop = 0;
    paintActive();
    // Fill in the wartime district for same-named places, so "Mỹ Hiệp" rows can be told apart.
    $$("[data-dl]", results).forEach(async el => {
      const it = items[+el.dataset.dl], d = await districtNear(it.lon, it.lat);
      if (req === qReq && d && el.isConnected) el.textContent = " · " + d;
    });
  } catch (e) { if (req === qReq) { results.innerHTML = `<li class="r-empty">Search failed: ${esc(e.message)}</li>`; } }
}
function paintActive() {
  $$("li[data-i]", results).forEach(li => li.setAttribute("aria-selected", +li.dataset.i === qActive));
  q.setAttribute("aria-activedescendant", qActive >= 0 ? `res${qActive}` : "");
  results.querySelector(`#res${qActive}`)?.scrollIntoView({ block: "nearest" });
}
function closeResults(clearItems = true) {
  results.hidden = true; $("#view").hidden = false; q.setAttribute("aria-expanded", "false");
  if (clearItems) { qItems = []; qActive = -1; }
}
async function pickResult(it) {
  closeResults(); q.blur(); $("#qCancel").hidden = true;
  if (it.type !== "gridref") q.value = cleanTitle(it.title); // like Apple Maps: the box shows what's open
  if (!isDesk()) setSheet("half");
  if (it.type === "gridref") return openGridRef(q.value.trim());
  openEvidence(it.id, { ...it, title: it.title }, { fly: true });
}

// ================================================================ popovers: style, layers, maps
// Positions a popover next to its anchor: the control column (style, layers) or above the map bar (map picker).
// On a phone the CSS stretches every popover across the screen.
function openPopover(id, anchor) {
  closePopovers(id);
  const pop = $("#" + id); pop.hidden = false;
  anchor?.setAttribute("aria-expanded", "true");
  pop._anchor = anchor;
  if (isDesk() || id !== "popMaps") {
    const r = anchor.getBoundingClientRect();
    if (id === "popMaps") {
      const w = pop.offsetWidth;
      pop.style.left = Math.max(12, Math.min(innerWidth - w - 12, r.left)) + "px";
      pop.style.top = ""; pop.style.right = "";
      pop.style.bottom = (innerHeight - r.top + 8) + "px";
      pop.style.maxHeight = (r.top - 20) + "px";
    } else {
      // On a phone the popover spans the screen, so it starts below the (i) attribution button at the top left.
      pop.style.top = isDesk() ? r.top + "px" : `max(${r.top}px, calc(env(safe-area-inset-top) + ${ATTRIB_CLEAR}px))`;
      pop.style.right = (innerWidth - $("#controls").getBoundingClientRect().left + 8) + "px";
      pop.style.left = ""; pop.style.bottom = "";
      // Laptop: stop above the map bar, so its opacity slider and compare button stay usable.
      const floor = isDesk() ? $("#mapbar").getBoundingClientRect().top - 10 : innerHeight - 12;
      // The phone's top edge may have been pushed down past the anchor, so measure where it really landed.
      const top = isDesk() ? r.top : Math.max(r.top, pop.getBoundingClientRect().top);
      pop.style.maxHeight = Math.max(200, floor - top) + "px";
    }
  } else {
    const r = $("#mapbar").getBoundingClientRect();
    pop.style.top = ""; pop.style.bottom = (innerHeight - r.top + 8) + "px"; pop.style.maxHeight = (r.top - 20) + "px";
  }
  const first = pop.querySelector('[aria-checked="true"], [aria-pressed="true"], button, input');
  first?.focus({ preventScroll: true });
}
const ATTRIB_CLEAR = 52; // px from the top of a phone screen left free for the (i) attribution button
function closePopovers(except) {
  for (const id of ["popStyle", "popLayers", "popMaps"]) {
    if (id === except) continue;
    const p = $("#" + id);
    if (!p.hidden) { p.hidden = true; p._anchor?.setAttribute("aria-expanded", "false"); }
  }
}
function togglePopover(id, anchor) {
  if (!$("#" + id).hidden) { closePopovers(); anchor.focus(); return; }
  if (id === "popStyle") renderStylePop();
  if (id === "popLayers") renderLayersPop();
  openPopover(id, anchor);
}
// The Show/Hide pair for the sheet outlines, in the Old maps card and in settings.
function outlinesSeg(cls) {
  const on = outlinesOn(), b = (v, label) => `<button class="${cls === "lib-sort" ? "linkish small-t" : ""}" data-act="outlines" data-on="${v}" aria-pressed="${on === !!v}">${label}</button>`;
  return `<div class="${cls}" role="group" aria-label="Sheet outlines on the map">${cls === "lib-sort" ? '<span class="small-t muted">Outlines</span>' : ""}${b(1, "Show")}${b(0, "Hide")}</div>`;
}
// The Kaggle key, in settings: whether one is saved, and the way to add or forget it.
function keySettingsHTML() {
  const k = S.rec.key || {};
  if (k.present) return `<p class="small-t muted">A Kaggle key is saved${k.source && k.source !== "env file" ? ` (from ${esc(k.source)})` : " on this computer"}. It is only ever sent to Kaggle.</p>
    ${k.source === "env file" ? `<div class="row"><button class="btn small" data-act="recKeyForget">Forget the key</button></div>` : ""}`;
  return `<p class="small-t muted">Kaggle may ask for a free key when you add the air strikes.</p>
    <div class="row"><button class="btn small" data-act="recAskKey">Add a Kaggle key</button></div>`;
}
function renderStylePop() {
  const t = store.get("theme", "auto");
  $("#popStyle").innerHTML = `<div class="sec"><h3>Today’s map</h3><div class="style-cards">
      ${Object.entries(BASES).map(([k, b]) => `<button class="style-card" data-base="${k}" aria-pressed="${S.base === k}"><span class="thumb ${k}"></span><span>${b.name}</span></button>`).join("")}</div></div>
    <div class="sec"><h3>Appearance</h3><div class="seg" role="group" aria-label="Appearance">
      ${["auto", "light", "dark"].map(x => `<button data-act="theme" data-theme="${x}" aria-pressed="${t === x}">${x === "auto" ? "Automatic" : x[0].toUpperCase() + x.slice(1)}</button>`).join("")}</div></div>
    <div class="sec"><h3>Old map outlines</h3><p class="small-t muted">The sheet edges drawn while the Old maps card is open.</p>${outlinesSeg("seg")}</div>
    <div class="sec"><h3>Kaggle key</h3>${keySettingsHTML()}</div>
    <div class="sec"><button class="linkish" data-act="openSettings">All settings</button></div>`;
  $$("[data-base]", $("#popStyle")).forEach(b => b.addEventListener("click", () => { setBase(b.dataset.base); renderStylePop(); $(`#popStyle [data-base="${b.dataset.base}"]`).focus(); }));
  wire($("#popStyle"));
}
function renderLayersPop(keepFocus = false) {
  const pop = $("#popLayers");
  const focusKey = keepFocus && pop.contains(document.activeElement) ? (document.activeElement.dataset.kind ? `[data-kind="${document.activeElement.dataset.kind}"]`
    : document.activeElement.dataset.only ? `[data-only="${document.activeElement.dataset.only}"]` : document.activeElement.dataset.act === "preset" ? '[data-act="preset"][aria-pressed="true"]' : null) : null;
  const anyHidden = shownKinds().some(k => S.hidden.has(k));
  const none = [...emptyKinds].map(k => KINDS[k].label);
  pop.innerHTML = `<div class="sec"><h3>On the map ${anyHidden ? `<button class="linkish h-act" data-act="showAll">Show all</button>` : ""}</h3>
      <ul class="kind-list">${kindRows()}</ul>
      ${none.length ? `<p class="small-t muted" style="margin:6px 8px 0">No records yet: ${esc(none.join(", "))}.</p>` : ""}</div>
    <div class="sec"><h3>Years <span class="h-act muted" style="text-transform:none;letter-spacing:0">${esc(yearsText())}</span></h3>${YEARS_UI()}</div>
    <div class="sec"><h3>Strike heat</h3>
      <label class="row" style="gap:10px"><span class="small-t muted">Faint</span><input class="grow" type="range" id="heatR" min="0" max="100" value="${Math.round(S.heat * 100)}" aria-label="Strike heat strength"><span class="small-t muted">Strong</span></label></div>
    <div class="sec"><p class="quiet-note" style="margin:0">${icon.warn}<span>Includes casualty and aircraft-loss locations. Strike areas may hold unexploded ordnance: never dig without clearance from the local authorities.</span></p></div>`;
  wire(pop); wireYears(pop);
  $("#heatR").addEventListener("input", e => { S.heat = +e.target.value / 100; store.set("heat", S.heat); updateHeat(); });
  updateCounts();
  if (focusKey) pop.querySelector(focusKey)?.focus({ preventScroll: true });
}
ACTIONS.showAll = () => showAllKinds();
document.addEventListener("pointerdown", e => {
  if (e.target.closest(".popover, #styleBtn, #layersBtn, #pickA, #pickB")) return;
  closePopovers();
});
// A popover is not modal: when keyboard focus Tabs out of it (and is not on the button that opened it, which
// toggles it itself), it closes. Focus is already where the person sent it, so it is not moved back.
document.addEventListener("focusin", e => {
  if (!["popStyle", "popLayers", "popMaps"].some(id => { const p = $("#" + id); return !p.hidden && (p.contains(e.target) || p._anchor?.contains(e.target)); })) closePopovers();
});

// ================================================================ settings and your own maps
// The Settings card (preferences, the Kaggle key, your own maps, storage) and the Placement screen, a full-window
// overlay where a plain scan is matched to today's map. The server does the work: /api/library/custom takes the file,
// /api/library/custom/<id>/points saves the matched spots and queues the build, /edit renames, /api/settings reports
// where data is kept and how much space it uses. Your own maps are library entries with series "custom" (S.lib), so
// they show in the Old maps card and on the map like any other installed map.
const CUSTOM_KIND_WORDS = { geotiff: "GeoTIFF", geopdf: "GeoPDF", scan: "Scan" };
const UPLOAD_MAX = 2 * 1024 ** 3; // the server's limit
const customMaps = () => S.lib.maps.filter(m => m.custom);
const sizeText = b => fmtMB(b) || "0 MB";
// up: the upload in flight ({ name, loaded, total, xhr }); err: why the last one failed; disk: GET /api/settings.
S.cm = { up: null, err: "", disk: null, diskErr: "" };

function openSettings(sel = null) {
  const from = document.activeElement;
  closePopovers();
  show("settings", { sel, confirm: null, keyOpen: false });
  if (from && from !== document.body && !$("#panel").contains(from)) S.opener = from;
  focusCard();
  loadLibrary(); loadSettings();
  if (sel) reveal($(`#view [data-lib-row="${CSS.escape(sel)}"]`));
}
async function loadSettings() {
  try { S.cm.disk = await api("/api/settings"); S.cm.diskErr = ""; }
  catch (e) { S.cm.diskErr = e.message; }
  const el = $("#setDisk");
  if (el && S.view.name === "settings") el.innerHTML = storageHTML();
}
function storageHTML() {
  const s = S.cm.disk;
  if (!s) return `<p class="small-t muted">${S.cm.diskErr ? "Could not check the space used: " + esc(S.cm.diskErr) : "Checking…"}</p>`;
  const d = s.disk || {}, row = (k, v, cls = "") => `<div class="set-row"><span class="set-k">${k}</span><span class="${cls}">${v}</span></div>`;
  return `<div class="group">${row("Kept in", esc(s.data_folder), "mono set-path")}${row("Old maps", sizeText(d.maps))}${row("Records", sizeText(d.records))}
    ${row("Downloads", sizeText(d.downloads))}${row("<b>Total</b>", `<b>${sizeText(d.total)}</b>`)}</div>
    <p class="small-t muted">Downloads are the files maps were built from. Removing a map frees its space.</p>`;
}

const settingsSig = () => JSON.stringify([S.lib.loaded, S.lib.err, customMaps().map(m => [m.id, m.state, m.title, m.year, m.kind, !!m.bounds, m.error]),
  S.view.sel, S.view.confirm, S.view.keyOpen, !!S.cm.up, S.cm.err, S.base, store.get("theme", "auto"), scrollMode(), outlinesOn(), S.rec.key, S.eraA]);
let settingsLastSig = "";
// Redraws the card when something it shows changed; otherwise only moves the progress rings. A field being typed in is
// never torn out from under the caret.
function settingsRefresh(force = false) {
  if (S.view.name !== "settings") return;
  const typing = $("#view").contains(document.activeElement) && document.activeElement.matches("input");
  // Unsaved text in a Name or Year box also holds the redraw back, whether or not it has focus.
  const dirty = [...document.querySelectorAll("#view .cm-edit input")].some(i => i.value !== i.defaultValue);
  if ((force || settingsSig() !== settingsLastSig) && !typing && !dirty) { rerenderKeepingFocus(); loadSettings(); }
  else { if (force) cmPatchUpload(); libPatchProgress(); }
}
// The upload button / progress and its error line, swapped on their own so a finished or failed upload shows up even
// while someone is typing elsewhere in the view.
function cmPatchUpload() {
  const up = $("#cmUp"), err = $("#cmErr");
  if (up) up.innerHTML = uploadHTML();
  if (err) err.textContent = S.cm.err;
}

function cmMeta(m) {
  if (m.state === "needs_points") return "Needs placing on today’s map";
  if (m.state === "failed") return m.error ? `Could not be built: ${m.error}` : "Could not be built";
  if (m.state === "queued") return "Waiting to be built";
  if (m.state === "building") return libMeta(m);
  return [libYear(m) || "Undated", CUSTOM_KIND_WORDS[m.kind] || "Your map", fmtMB(m.disk_bytes)].filter(Boolean).join(" · ");
}
function cmControl(m) {
  const t = esc(m.title), id = esc(m.id);
  if (m.state === "needs_points") return `<button class="lib-pill on" data-act="cmPlace" data-id="${id}" aria-label="Place ${t} on today’s map">Place</button>`;
  if (m.state === "failed") return `<button class="lib-pill" data-act="libAskRemove" data-id="${id}" aria-label="Remove ${t}">Remove</button>`;
  return libControl(m); // a progress ring while it is built, Show / Hide once it is ready
}
function cmDetail(m) {
  const t = esc(m.title), id = esc(m.id), ready = m.state === "installed";
  const confirm = S.view.confirm === "remove:" + m.id ? `<div class="confirm-box" role="alertdialog" aria-label="Remove ${t}">
      <span>Remove <b>${t}</b> from this computer? Your original file is deleted too, so keep a copy if you want to add it again.</span>
      <div class="row"><button class="btn small danger solid" data-act="libRemove" data-id="${id}">Remove</button>
      <button class="btn small" data-act="libCancel">Keep it</button></div></div>` : "";
  return `<div class="lib-detail" id="libd-${id}">
    <p>${esc(CUSTOM_KIND_WORDS[m.kind] || "Map")}${ready ? ` · uses ${esc(sizeText(m.disk_bytes))}` : ""}</p>
    ${m.state === "needs_points" ? `<p class="muted">Match three or more spots on it to today’s map to place it.</p>` : ""}
    <form class="cm-edit stack" data-id="${id}" autocomplete="off">
      <label class="field"><span>Name</span><input type="text" name="mtitle" required maxlength="120" value="${esc(m.title)}"></label>
      <label class="field"><span>Year (optional)</span><input type="number" name="myear" min="1800" max="2100" inputmode="numeric" placeholder="e.g. 1968" value="${m.year ? esc(m.year) : ""}"></label>
      <div class="row"><button class="btn small primary" type="submit">Save</button></div>
      <div class="err" role="alert"></div></form>
    <div class="row">${m.state === "needs_points" ? "" : m.kind === "scan" ? `<button class="btn small" data-act="cmPlace" data-id="${id}">Place again</button>` : ""}
      ${ready && m.bounds ? `<button class="btn small" data-act="cmFit" data-id="${id}">Zoom to map</button>` : ""}
      <button class="btn small danger" data-act="libAskRemove" data-id="${id}">${icon.trash}Remove</button></div>
    ${confirm}</div>`;
}
function cmRow(m) {
  const sel = S.view.sel === m.id, id = esc(m.id);
  return `<li class="lib-row${sel ? " sel" : ""}" data-lib-row="${id}">
    <button class="main" data-act="cmSel" data-id="${id}" aria-expanded="${sel}"${sel ? ` aria-controls="libd-${id}"` : ""}>
      <span class="t">${esc(m.title)}</span><span class="m${m.state === "failed" ? " bad" : ""}" data-prog-t="${id}">${esc(cmMeta(m))}</span></button>
    ${cmControl(m)}${sel ? cmDetail(m) : ""}</li>`;
}
function uploadHTML() {
  const u = S.cm.up;
  if (!u) return `<button class="btn" data-act="cmPick">${icon.plus}<span>Add your own map</span></button>
    <input type="file" id="cmFile" hidden accept=".tif,.tiff,.jpg,.jpeg,.png,.pdf,image/tiff,image/jpeg,image/png,application/pdf">`;
  return `<div class="lib-status" role="group" aria-label="Uploading ${esc(u.name)}"><span class="spin" aria-hidden="true"></span>
    <span class="grow"><b>Adding ${esc(u.name)}</b><span class="small-t muted" data-up-t>${esc(uploadText(u))}</span>
    <progress class="up-bar" max="100" value="${uploadPct(u)}" data-up-bar aria-label="Upload progress"></progress></span>
    <button class="linkish small-t" data-act="cmUpCancel">Cancel</button></div>`;
}
const uploadPct = u => u.total ? Math.round(100 * u.loaded / u.total) : 0;
const uploadText = u => u.loaded >= u.total ? "Checking the file…" : `${fmtMB(u.loaded) || "0 MB"} of ${fmtMB(u.total)} · ${uploadPct(u)}%`;
function keySectionHTML() {
  const k = S.rec.key || {};
  if (S.view.keyOpen) return keyBox(false);
  const status = k.present ? `A Kaggle key is saved${k.source && k.source !== "env file" ? ` (from ${esc(k.source)})` : " on this computer"}. It is only ever sent to Kaggle.`
    : "No key saved. Kaggle may ask for a free one when you add the air strikes.";
  return `<p class="small-t muted">${status}</p>
    <div class="row"><button class="btn small" data-act="setKeyAdd">${k.present ? "Replace the key" : "Add a Kaggle key"}</button>
    ${k.present && k.source === "env file" ? `<button class="btn small" data-act="recKeyForget">Forget the key</button>` : ""}</div>`;
}
VIEWS.settings = v => {
  settingsLastSig = settingsSig();
  const theme = store.get("theme", "auto"), mode = scrollMode(), mine = customMaps();
  const seg = (label, inner) => `<div class="set-row"><span class="set-k">${label}</span><div class="seg" role="group" aria-label="${label}">${inner}</div></div>`;
  return `${headHTML("Settings", "Preferences, your own maps and storage")}
    <h3 class="sub-head">Preferences</h3>
    <div class="group">
      ${seg("Today’s map", Object.entries(BASES).map(([k, b]) => `<button data-act="setBase" data-s="${k}" aria-pressed="${S.base === k}">${esc(b.name)}</button>`).join(""))}
      ${seg("Appearance", ["auto", "light", "dark"].map(x => `<button data-act="theme" data-theme="${x}" aria-pressed="${theme === x}">${x === "auto" ? "Automatic" : x[0].toUpperCase() + x.slice(1)}</button>`).join(""))}
      ${seg("Scrolling", SCROLL_MODES.map(([k, label]) => `<button data-act="scrollMode" data-m="${k}" aria-pressed="${mode === k}">${label}</button>`).join(""))}
      <div class="set-row"><span class="set-k">Old map outlines</span>${outlinesSeg("seg")}</div>
    </div>
    <h3 class="sub-head">Kaggle key</h3>
    <div class="set-block">${keySectionHTML()}</div>
    <h3 class="sub-head">My maps</h3>
    <div class="set-block">
      <p class="small-t muted">Add a map from your computer. A GeoTIFF or GeoPDF is placed for you. A photo or scan (JPG, PNG or PDF) is placed by matching three or more spots on it to today’s map.</p>
      <div id="cmUp">${uploadHTML()}</div>
      <div class="err" id="cmErr" role="alert">${esc(S.cm.err)}</div>
      ${mine.length ? `<div class="group"><ul class="list lib-list" aria-label="Your own maps">${mine.map(cmRow).join("")}</ul></div>`
        : S.lib.loaded ? `<p class="small-t muted">You have not added a map of your own yet.</p>` : ""}
    </div>
    <h3 class="sub-head">Storage</h3>
    <div id="setDisk">${storageHTML()}</div>
    <p class="foot-note">Everything stays on this computer.</p>`;
};
VIEWS.settings.after = () => {
  $("#cmFile")?.addEventListener("change", e => { const f = e.target.files[0]; e.target.value = ""; if (f) cmUpload(f); });
  $$("#view .cm-edit").forEach(f => f.addEventListener("submit", cmSaveEdit));
  wireKeyBox();
};
ACTIONS.openSettings = () => openSettings();
ACTIONS.setBase = el => setBase(el.dataset.s);
ACTIONS.scrollMode = el => setScrollMode(el.dataset.m);
ACTIONS.setKeyAdd = () => { S.view.keyOpen = true; rerenderKeepingFocus(); $('#view [data-act="recKeyPick"]')?.focus({ preventScroll: true }); };
ACTIONS.cmPick = () => $("#cmFile")?.click();
ACTIONS.cmUpCancel = () => S.cm.up?.xhr.abort();
ACTIONS.cmSel = el => {
  const id = el.dataset.id;
  S.view.sel = S.view.sel === id ? null : id; S.view.confirm = null;
  rerenderKeepingFocus();
  if (S.view.sel) reveal($(`#view [data-lib-row="${CSS.escape(id)}"]`));
};
ACTIONS.cmFit = el => {
  const b = S.lib.byId.get(el.dataset.id)?.bounds;
  if (!b) return;
  if (!isDesk()) setSheet("peek");
  primary().fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: mapPadding(), maxZoom: 15, duration: reduceMotion ? 0 : 600 });
};
ACTIONS.cmPlace = el => { const m = S.lib.byId.get(el.dataset.id); if (m) openPlacement(m, el); };
ACTIONS.cmManage = el => openSettings(el.dataset.id);

async function cmSaveEdit(e) {
  e.preventDefault();
  const f = e.target, err = f.querySelector(".err"), id = f.dataset.id;
  const title = f.mtitle.value.trim(), y = f.myear.value.trim(), year = y ? +y : null;
  if (!title) { err.textContent = "Give the map a name."; return; }
  if (y && !(year >= 1800 && year <= 2100)) { err.textContent = "The year should be between 1800 and 2100, or empty."; return; }
  try { applyLibrary(await api(`/api/library/custom/${encodeURIComponent(id)}/edit`, { method: "POST", body: { title, year } })); }
  catch (x) { err.textContent = x.message; return; }
  refreshCatalog();
  toast("Saved");
}

// ---- adding a file
// Sent as the raw file with XMLHttpRequest, which (unlike fetch) reports upload progress. A big scan is never held in
// memory by the page: the browser streams it from disk.
function cmUpload(file) {
  if (S.cm.up) return;
  S.cm.err = "";
  if (file.size > UPLOAD_MAX) { S.cm.err = "That file is larger than 2 GB, which is the most Map Vietnam can take."; settingsRefresh(true); return; }
  const title = file.name.replace(/\.[^.]+$/, "").replace(/[_]+/g, " ").trim() || "My map";
  const y = file.name.match(/(?:^|\D)(1[89]\d\d|20[0-2]\d)(?:\D|$)/);
  const qs = new URLSearchParams({ filename: file.name, title, year: y ? y[1] : "" });
  const xhr = new XMLHttpRequest(), up = { name: file.name, loaded: 0, total: file.size, xhr };
  const done = msg => { S.cm.up = null; if (msg) S.cm.err = msg; settingsRefresh(true); };
  S.cm.up = up;
  xhr.open("POST", "/api/library/custom?" + qs);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = e => {
    up.loaded = e.loaded;
    const bar = $("#view [data-up-bar]"), t = $("#view [data-up-t]");
    if (bar) bar.value = uploadPct(up);
    if (t) t.textContent = uploadText(up);
  };
  xhr.onload = () => {
    let r = null;
    try { r = JSON.parse(xhr.responseText); } catch { /* not JSON: reported below */ }
    if (xhr.status !== 200 || !r?.map) return done((r && r.error) || "The map could not be added.");
    S.cm.up = null;
    applyLibrary(r.library);
    settingsRefresh(true);
    if (r.map.state === "needs_points") { toast("Now place it: match three spots on it to today’s map", 5000); openPlacement(S.lib.byId.get(r.map.id) || r.map); }
    else toast("Added. Your map is being prepared and will appear in Old maps.", 5000);
  };
  xhr.onerror = () => done("The upload was interrupted. Check that Map Vietnam is still running, then try again.");
  xhr.onabort = () => { done(""); toast("Stopped adding the file"); };
  xhr.send(file);
  settingsRefresh(true);
}

// ---- the Placement screen
// The scan is drawn by a MapLibre map in "pixel space": a flat rectangle laid out near the equator, so a click converts
// back to a pixel of the scan exactly (MapLibre draws the image linearly in Mercator space; PL_SCALE is its size in
// Mercator units). Today's map beside it is a second map with the same base style as the main one. Spots are numbered
// pairs; from three on, a least-squares affine fit (the same model the server fits) lays the scan over today's map.
const PL_SCALE = 2 ** -10;
let pl = null; // the open placement, or null
const placeOpen = () => !!pl;
const PL_HIDE = ["maps", "panel", "controls", "mapbar", "filterPill", "loading", "placing"];

function plToLL(col, row) { return new maplibregl.MercatorCoordinate(0.5 + (col - pl.w / 2) * pl.k, 0.5 + (row - pl.h / 2) * pl.k, 0).toLngLat(); }
function plFromLL(ll) { const c = maplibregl.MercatorCoordinate.fromLngLat(ll); return [(c.x - 0.5) / pl.k + pl.w / 2, (c.y - 0.5) / pl.k + pl.h / 2]; }

// Solves the 3x3 system A x = b by Cramer's rule; null when the points are in a line (or too close together).
function solve3(A, b) {
  const det = M => M[0][0] * (M[1][1] * M[2][2] - M[1][2] * M[2][1]) - M[0][1] * (M[1][0] * M[2][2] - M[1][2] * M[2][0]) + M[0][2] * (M[1][0] * M[2][1] - M[1][1] * M[2][0]);
  const d = det(A);
  if (!Number.isFinite(d) || Math.abs(d) < 1e-5) return null;
  return [0, 1, 2].map(i => det(A.map((row, r) => row.map((v, c) => c === i ? b[r] : v))) / d);
}
// lon = a·col + b·row + c, and the same for lat, fitted to the pairs by least squares. Columns and rows are scaled to
// 0..1 first, so the system is well conditioned. Returns { at(col, row) -> [lon, lat], rms (metres) } or null.
function fitAffine(pairs, w, h) {
  if (pairs.length < 3) return null;
  const rows = pairs.map(p => [p.col / w, p.row / h, 1]);
  const solve = ys => {
    const A = [[0, 0, 0], [0, 0, 0], [0, 0, 0]], b = [0, 0, 0];
    rows.forEach((r, i) => { for (let a = 0; a < 3; a++) { b[a] += r[a] * ys[i]; for (let c = 0; c < 3; c++) A[a][c] += r[a] * r[c]; } });
    return solve3(A, b);
  };
  const lon = solve(pairs.map(p => p.lon)), lat = solve(pairs.map(p => p.lat));
  if (!lon || !lat) return null;
  const at = (col, row) => [lon[0] * col / w + lon[1] * row / h + lon[2], lat[0] * col / w + lat[1] * row / h + lat[2]];
  const sq = pairs.reduce((a, p) => { const [x, y] = at(p.col, p.row); return a + ((x - p.lon) * 111320 * Math.cos(p.lat * Math.PI / 180)) ** 2 + ((y - p.lat) * 110540) ** 2; }, 0);
  return { at, rms: Math.sqrt(sq / pairs.length) };
}

function openPlacement(entry, opener = document.activeElement) {
  if (pl || !entry?.image) { if (!entry?.image) toast("This map has no picture to place."); return; }
  closePopovers();
  const w = entry.image.width, h = entry.image.height;
  pl = { entry, w, h, k: PL_SCALE / Math.max(w, h), url: `/api/library/custom/${encodeURIComponent(entry.id)}/preview`,
    pairs: (entry.points || []).map(([col, row, lon, lat]) => ({ col, row, lon, lat })), pending: null, opacity: 0.6, marks: [], todayReady: false, opener, discard: false };
  const dark = isDark();
  document.body.insertAdjacentHTML("beforeend", `<div id="place" role="dialog" aria-modal="true" aria-labelledby="plTitle">
    <header class="pl-bar"><div class="pl-head"><h2 id="plTitle" tabindex="-1">Place ${esc(entry.title)}</h2><p id="plStep" class="pl-step" role="status"></p></div>
      <div class="pl-btns"><button class="btn" id="plUndo">Undo last</button><button class="btn" id="plClear">Clear</button>
        <button class="btn" id="plCancel">Cancel</button><button class="btn primary" id="plSave" disabled>Place map</button></div></header>
    <div class="pl-panes">
      <section class="pl-pane" id="plScanPane" aria-label="Your map"><span class="pl-tag">Your map</span><div class="pl-map" id="plScan"></div>
        <svg class="pl-cross" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v16M4 12h16"/></svg>
        <button class="btn small pl-mark" data-pl="scan" aria-label="Mark the middle of your map (the + sign)">Mark the +</button></section>
      <section class="pl-pane" id="plTodayPane" aria-label="Today’s map"><span class="pl-tag">Today’s map</span><div class="pl-map" id="plToday"></div>
        <svg class="pl-cross" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v16M4 12h16"/></svg>
        <button class="btn small pl-mark" data-pl="today" aria-label="Mark the middle of today’s map (the + sign)">Mark the +</button></section>
    </div>
    <div class="pl-tray">
      <ol id="plPairs" class="pl-pairs" aria-label="Matched spots"></ol>
      <div class="pl-fields">
        <label class="field"><span>Name</span><input type="text" id="plName" maxlength="120" value="${esc(entry.title)}"></label>
        <label class="field"><span>Year (optional)</span><input type="number" id="plYear" min="1800" max="2100" inputmode="numeric" placeholder="e.g. 1968" value="${entry.year ? esc(entry.year) : ""}"></label>
        <label class="field pl-op"><span>See-through <span id="plOpVal" class="mono">60%</span></span><input type="range" id="plOp" min="10" max="100" value="60" disabled aria-label="Opacity of your map over today’s map"></label>
      </div>
      <div class="err" id="plErr" role="alert"></div>
      <div class="confirm-box" id="plDiscard" role="alertdialog" aria-label="Discard your matched spots" hidden>
        <span>Throw away the spots you have matched so far?</span>
        <div class="row"><button class="btn small danger solid" id="plYes">Discard</button><button class="btn small" id="plNo">Keep working</button></div></div>
    </div></div>`);
  PL_HIDE.forEach(id => { const el = document.getElementById(id); if (el) el.inert = true; });
  const bg = dark ? "#161715" : "#d9d5c8", ctl = () => new maplibregl.NavigationControl({ showCompass: false });
  const corners = [[0, 0], [w, 0], [w, h], [0, h]].map(([c, r]) => plToLL(c, r)), arr = corners.map(c => c.toArray());
  const D = Math.max(w, h), common = { attributionControl: false, dragRotate: false, pitchWithRotate: false, doubleClickZoom: false, fadeDuration: 0 };
  pl.scan = new maplibregl.Map({ ...common, container: "plScan", maxZoom: Math.max(13, 10 + Math.log2(D / 128)), renderWorldCopies: false,
    style: { version: 8, sources: { scan: { type: "image", url: pl.url, coordinates: arr } },
      layers: [{ id: "bg", type: "background", paint: { "background-color": bg } }, { id: "scan", type: "raster", source: "scan", paint: { "raster-fade-duration": 0 } }] } });
  pl.scan.fitBounds([arr[3], arr[1]], { padding: 24, duration: 0 });
  pl.scan.setMinZoom(pl.scan.getZoom() - 1.5);
  pl.scan.addControl(ctl(), "top-right");
  pl.scan.on("click", e => plClickScan(e.lngLat));
  pl.scan.on("error", e => { if (/image|Failed|404|decode/i.test(String(e?.error?.message || ""))) $("#plErr").textContent = "The picture of your map could not be loaded."; });
  const c = primary().getCenter();
  pl.today = new maplibregl.Map({ ...common, container: "plToday", style: makeStyle(), center: c, zoom: primary().getZoom() });
  pl.today.addControl(ctl(), "top-right");
  pl.today.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
  pl.today.on("click", e => plClickToday(e.lngLat));
  pl.today.on("load", () => { pl.todayReady = true; setBasePaint(pl.today); plDraw(); });
  [pl.scan, pl.today].forEach(m => { m.touchZoomRotate.disableRotation(); m.keyboard.disableRotation?.(); wireTrackpad(m); });
  const b = entry.bounds || (pl.pairs.length ? pl.pairs.reduce((a, p) => [Math.min(a[0], p.lon), Math.min(a[1], p.lat), Math.max(a[2], p.lon), Math.max(a[3], p.lat)], [180, 90, -180, -90]) : null);
  if (b && b[2] > b[0] && b[3] > b[1]) pl.today.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 40, duration: 0, maxZoom: 15 });
  $("#plUndo").onclick = () => { if (pl.pending) pl.pending = null; else pl.pairs.pop(); plRender(); };
  $("#plClear").onclick = () => { pl.pairs = []; pl.pending = null; plRender(); };
  $("#plCancel").onclick = plAskCancel;
  $("#plYes").onclick = () => closePlacement();
  $("#plNo").onclick = () => { $("#plDiscard").hidden = true; pl.discard = false; $("#plCancel").focus(); };
  $("#plSave").onclick = plSave;
  $("#plOp").oninput = e => { pl.opacity = +e.target.value / 100; $("#plOpVal").textContent = e.target.value + "%"; if (pl.todayReady && pl.today.getLayer("pl-scan")) pl.today.setPaintProperty("pl-scan", "raster-opacity", pl.opacity); };
  $$("#place .pl-mark").forEach(btn => btn.onclick = () => btn.dataset.pl === "scan" ? plClickScan(pl.scan.getCenter()) : plClickToday(pl.today.getCenter()));
  $("#plPairs").onclick = e => { const x = e.target.closest("[data-del]"); if (x) { pl.pairs.splice(+x.dataset.del, 1); plRender(); } };
  pl.keys = e => { if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); plAskCancel(); } };
  document.addEventListener("keydown", pl.keys, true);
  pl.resize = () => { pl.scan.resize(); pl.today.resize(); };
  addEventListener("resize", pl.resize);
  plRender();
  $("#plTitle").focus({ preventScroll: true });
}
function plAskCancel() {
  if (!pl) return;
  // Esc while the question is up means "keep working"
  if (pl.discard) { $("#plNo").click(); return; }
  if (!pl.pairs.length && !pl.pending) { closePlacement(); return; }
  pl.discard = true; $("#plDiscard").hidden = false; $("#plNo").focus();
}
function closePlacement() {
  if (!pl) return;
  document.removeEventListener("keydown", pl.keys, true);
  removeEventListener("resize", pl.resize);
  pl.marks.forEach(m => m.remove());
  pl.scan.remove(); pl.today.remove();
  $("#place").remove();
  PL_HIDE.forEach(id => { const el = document.getElementById(id); if (el) el.inert = false; });
  const back = pl.opener;
  pl = null;
  if (back?.isConnected && !back.closest("[hidden]")) back.focus({ preventScroll: true }); else focusCard();
}
function plClickScan(ll) {
  const [col, row] = plFromLL(ll);
  if (col < 0 || row < 0 || col > pl.w || row > pl.h) { toast("That spot is off your map. Click on the map itself."); return; }
  pl.pending = { col, row };
  plRender();
}
function plClickToday(ll) {
  if (!pl.pending) { toast("First click a spot on your map (left or top), then the same spot on today’s map."); return; }
  pl.pairs.push({ col: pl.pending.col, row: pl.pending.row, lon: ll.lng, lat: ll.lat });
  pl.pending = null;
  plRender();
}
// A numbered pin; each pair has its own colour so the two halves of a pair are easy to match by eye.
function plPin(n, pending = false) {
  const el = document.createElement("div");
  el.className = "pl-pin" + (pending ? " pending" : "");
  el.style.setProperty("--pc", `hsl(${(n * 67) % 360} 65% 36%)`);
  el.textContent = n;
  return el;
}
function plFit() { return fitAffine(pl.pairs, pl.w, pl.h); }
function plRender() {
  const n = pl.pairs.length, fit = plFit();
  pl.marks.forEach(m => m.remove()); pl.marks = [];
  pl.pairs.forEach((p, i) => {
    pl.marks.push(new maplibregl.Marker({ element: plPin(i + 1) }).setLngLat(plToLL(p.col, p.row)).addTo(pl.scan));
    pl.marks.push(new maplibregl.Marker({ element: plPin(i + 1) }).setLngLat([p.lon, p.lat]).addTo(pl.today));
  });
  if (pl.pending) pl.marks.push(new maplibregl.Marker({ element: plPin(n + 1, true) }).setLngLat(plToLL(pl.pending.col, pl.pending.row)).addTo(pl.scan));
  let step;
  if (pl.pending) step = `<b>Spot ${n + 1}, step 2.</b> Now click the same spot on today’s map.`;
  else if (n < 3) step = `<b>1.</b> Click a spot on your map. <b>2.</b> Click the same spot on today’s map. ${n ? `${n} matched, at least 3 needed. Spread them out.` : "Use corners, road crossings or bridges."}`;
  else if (!fit) step = "Those spots are in a line or very close together. Add one further away from the others.";
  else step = `Your map now lies over today’s map. Check the roads and rivers line up; drag the slider to compare. ${n >= 4 ? `Spots are off by about ${fit.rms < 10 ? "under 10" : Math.round(fit.rms)} m on average. ` : ""}Add more spots to improve it, then place the map.`;
  $("#plStep").innerHTML = step;
  $("#plScanPane").classList.toggle("want", !pl.pending);
  $("#plTodayPane").classList.toggle("want", !!pl.pending);
  $("#plPairs").innerHTML = pl.pairs.map((p, i) => `<li><span class="pl-pin sm" style="--pc:hsl(${((i + 1) * 67) % 360} 65% 36%)">${i + 1}</span>
    <span class="mono">${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}</span>
    <button class="round flat small" data-del="${i}" aria-label="Delete spot ${i + 1}">${icon.close}</button></li>`).join("");
  $("#plUndo").disabled = !n && !pl.pending;
  $("#plClear").disabled = !n && !pl.pending;
  $("#plSave").disabled = !fit;
  $("#plOp").disabled = !fit;
  $("#plErr").textContent = "";
  plDraw();
}
// The live preview: the scan's four corners at the fitted places, as an image layer over today's map.
function plDraw() {
  if (!pl?.todayReady) return;
  const m = pl.today, fit = plFit();
  if (!fit) { if (m.getLayer("pl-scan")) m.setLayoutProperty("pl-scan", "visibility", "none"); return; }
  const coordinates = [fit.at(0, 0), fit.at(pl.w, 0), fit.at(pl.w, pl.h), fit.at(0, pl.h)];
  if (!m.getSource("pl-scan")) {
    m.addSource("pl-scan", { type: "image", url: pl.url, coordinates });
    m.addLayer({ id: "pl-scan", type: "raster", source: "pl-scan", paint: { "raster-opacity": pl.opacity, "raster-fade-duration": 0 } });
  } else { m.getSource("pl-scan").setCoordinates(coordinates); m.setLayoutProperty("pl-scan", "visibility", "visible"); }
}
async function plSave() {
  const name = $("#plName").value.trim(), y = $("#plYear").value.trim(), year = y ? +y : null, err = $("#plErr");
  if (!name) { err.textContent = "Give the map a name."; $("#plName").focus(); return; }
  if (y && !(year >= 1800 && year <= 2100)) { err.textContent = "The year should be between 1800 and 2100, or empty."; $("#plYear").focus(); return; }
  const cur = pl, btn = $("#plSave"), id = pl.entry.id;
  btn.disabled = true; btn.textContent = "Placing…";
  // col / row are pixels of the full-size picture, which is what the scan is laid out in here.
  const points = pl.pairs.map(p => [Math.round(p.col * 10) / 10, Math.round(p.row * 10) / 10, +p.lon.toFixed(7), +p.lat.toFixed(7)]);
  let lib;
  try { lib = await api(`/api/library/custom/${encodeURIComponent(id)}/points`, { method: "POST", body: { points, title: name, year } }); }
  catch (e) { if (pl === cur) { err.textContent = e.message; btn.textContent = "Place map"; btn.disabled = false; } return; }
  applyLibrary(lib);
  // The placement may have been closed (or another opened) while this was in flight; only close our own.
  if (pl === cur) closePlacement();
  toast(`${name} is being prepared. It will appear in Old maps.`, 5000);
  if (pl) return; // another placement is open now; leave it alone
  if (S.view.name !== "settings") openSettings(id); else { S.view.sel = id; settingsRefresh(true); }
}

// ================================================================ base map, theme
function setBase(k) { S.base = k; store.set("base", k); applyBase(); renderMapbar(); settingsRefresh(true); }
// Shows the chosen base map on one map, tuned for the theme. Also used by the Placement screen's map of today.
function setBasePaint(m) {
  const dark = isDark();
  for (const b of Object.keys(BASES)) {
    m.setLayoutProperty(`base-${b}`, "visibility", b === S.base ? "visible" : "none");
    // Dark street map: invert lightness and rotate hue back, so land is dark and water stays blue (like Apple Maps).
    // Land ends up dark grey, not black, so the "Today" half of a comparison still reads.
    const inv = dark && b === "osm";
    m.setPaintProperty(`base-${b}`, "raster-brightness-min", inv ? 0.92 : 0);
    m.setPaintProperty(`base-${b}`, "raster-brightness-max", inv ? 0.22 : dark ? 0.9 : 1);
    m.setPaintProperty(`base-${b}`, "raster-hue-rotate", inv ? 180 : 0);
    m.setPaintProperty(`base-${b}`, "raster-saturation", inv ? -0.55 : 0);
    m.setPaintProperty(`base-${b}`, "raster-contrast", inv ? -0.1 : 0);
  }
}
// Shows the chosen base map and retunes overlay colours for it and for the theme.
function applyBase() {
  const dark = isDark(), sat = S.base === "sat";
  eachMap(m => {
    setBasePaint(m);
    // Ring halos: dark on imagery and in dark mode, light on the street map.
    const haloC = sat || dark ? "#000" : "#fff";
    for (const id of ["strike-ring-halo", "ev-ring-halo", "site-ring-halo"]) m.setPaintProperty(id, "circle-stroke-color", haloC);
    // On dark backgrounds the strike rings get a lighter orange and more weight, so they don't sink into the map.
    const lit = sat || dark;
    m.setPaintProperty("strike-ring", "circle-stroke-color", lit ? "#fdba74" : KINDS.air_strike.color);
    m.setPaintProperty("strike-ring", "circle-stroke-width", lit ? 1.6 : 1.25);
    m.setPaintProperty("strike-ring", "circle-stroke-opacity", ["interpolate", ["linear"], ["zoom"], 13, lit ? 0.45 : 0.25, 15, lit ? 0.75 : 0.5]);
    m.setPaintProperty("strike-ring-halo", "circle-stroke-opacity", ringFade(lit ? 0.35 : 0.22));
    m.setPaintProperty("ev-ring", "circle-stroke-opacity", ringFade(lit ? 0.95 : 0.75));
    m.setPaintProperty("strike-dot", "circle-color", lit ? "#fb923c" : KINDS.air_strike.color);
    m.setPaintProperty("pn-label", "text-color", dark && !sat ? "#e9e6dd" : "#2b2925");
    m.setPaintProperty("pn-label", "text-halo-color", dark && !sat ? "rgba(20,20,18,.9)" : "rgba(255,255,255,.92)");
    m.setPaintProperty("ev-label", "text-color", dark && !sat ? "#f1efe8" : "#1b1a17");
    m.setPaintProperty("ev-label", "text-halo-color", dark && !sat ? "rgba(20,20,18,.9)" : "rgba(255,255,255,.92)");
    const nearC = dark || sat ? "#8fe3c9" : "#0e5a4b";
    m.setPaintProperty("near-line", "line-color", nearC);
    m.setPaintProperty("near-label", "text-color", nearC);
    m.setPaintProperty("near-label", "text-halo-color", dark || sat ? "rgba(10,12,11,.85)" : "rgba(255,255,255,.9)");
    styleFootprints(m);
  });
}
function setTheme(t) {
  store.set("theme", t);
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  applyBase(); applyEra(); settingsRefresh(true);
  if (!$("#popStyle").hidden) { renderStylePop(); $(`#popStyle [data-theme="${t}"]`)?.focus(); }
}
(function initTheme() { const t = store.get("theme", "auto"); if (t !== "auto") document.documentElement.dataset.theme = t; })();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { applyBase(); applyEra(); });

// ================================================================ keyboard
document.addEventListener("keydown", e => {
  if (placeOpen()) return; // the Placement screen has its own keys
  const typing = e.target.closest?.("input, textarea, select");
  if (e.key === "/" && !typing) { e.preventDefault(); q.focus(); q.select(); }
  else if (e.key === "Escape") {
    const open = ["popStyle", "popLayers", "popMaps"].find(id => !$("#" + id).hidden);
    if (open) { const a = $("#" + open)._anchor; closePopovers(); a?.focus(); return; }
    // An open confirm box ("Remove this map?") closes first, like its Cancel button.
    const cancel = e.target.closest?.(".confirm-box")?.querySelector('[data-act$="Cancel"], #delNo');
    if (cancel) { e.preventDefault(); cancel.click(); return; }
    if (typing) return;
    if (S.placing) endPlacing(); else if (S.view.name !== "home") back();
  }
});

// The names and record counts of the sources in the store, for record cards.
function loadSources() {
  return api("/api/sources").then(list => { S.sources = Object.fromEntries(list.map(s => [s.id, s])); }).catch(() => {});
}
// Kinds with no records at all are listed quietly instead of as toggles.
function checkEmptyKinds() {
  const none = new Set();
  return Promise.all(EV_KINDS.map(k => api(`/api/evidence?bbox=100,7,112,24&kinds=${k}&slim=1&limit=1`).then(fc => { if (!fc.features.length) none.add(k); }).catch(() => {})))
    .then(() => { emptyKinds.clear(); none.forEach(k => emptyKinds.add(k)); if (!$("#popLayers").hidden) renderLayersPop(true); updateCounts(); });
}

// ================================================================ boot
(async function boot() {
  // MapLibre comes from unpkg; offline, blocked or failing its integrity check, say so instead of a blank page.
  if (!window.maplibregl) {
    document.body.insertAdjacentHTML("afterbegin", `<div role="alert" style="position:fixed;inset:0;z-index:9999;display:grid;place-items:center;padding:24px;background:#fff;color:#111;font:16px/1.5 system-ui,sans-serif;text-align:center">
      <div><p><b>The map library could not be loaded.</b></p><p>Map Vietnam loads MapLibre from unpkg.com, so it needs an internet connection.<br>Check your connection (or any content blocker), then reload this page.</p></div></div>`);
    return;
  }
  setSheet(isDesk() ? "half" : "peek"); // before any await, so the sheet's first paint is already in place
  // Until the catalogue arrives, the picker shows the label saved with the last map chosen.
  { const [t, sub] = store.get("eraLabel", [null, null]); if (t) { $("#pickAtitle").textContent = t; $("#pickAsub").textContent = sub || ""; } }
  // Start-up runs in parallel: the map, the old-map catalogue and the first evidence cells are all requested at
  // once (evidence does not wait for the map's style), so the map is never blank for long.
  syncMapLeft();
  catalogReady = api("/catalog.json", { cache: "no-store" }).then(c => (c.layers || []).filter(l => l.bounds))
    .catch(() => { toast("The old-map catalogue could not be loaded."); return []; })
    .then(layers => {
      S.catalog = layers; S.eras = buildEras(S.catalog); S.catalogLoaded = true;
      const saved = store.get("era", null);
      const [lon, lat] = viewCenter();
      S.eraA = saved === "today" ? null : (eraById(saved) && eraCovers(eraById(saved), lon, lat) ? saved : defaultEra());
      renderMapbar();
    });
  catalogReady.then(loadLibrary);
  const m = createMap("mapA", true);
  maps.push(m); window.map = m;
  tilesBusy = true; // until the first "idle"
  // The credits follow the map bar's size and their own (a new old map changes their text), and the window's.
  const fitObserver = new ResizeObserver(() => fitAttribution());
  fitObserver.observe($("#mapbar"));
  m.on("load", () => { const box = m.getContainer().querySelector(".maplibregl-ctrl-bottom-right"); if (box) fitObserver.observe(box); fitAttribution(); });
  addEventListener("resize", fitAttribution);
  loadView();

  $("#panelBtn").addEventListener("click", () => setPanelHidden(!S.panelHidden));
  $("#styleBtn").addEventListener("click", () => togglePopover("popStyle", $("#styleBtn")));
  $("#layersBtn").addEventListener("click", () => togglePopover("popLayers", $("#layersBtn")));
  $("#settingsBtn").addEventListener("click", () => S.view.name === "settings" ? show("home") : openSettings());
  $("#mbCmp").addEventListener("click", () => setCompare(!S.compare));
  // With no old map on this computer, the picker button is the invitation to add some.
  $("#pickA").addEventListener("click", () => S.catalogLoaded && !S.eras.length ? openLibrary()
    : $("#popMaps").hidden || pickSide !== "A" ? openMapPicker("A") : closePopovers());
  $("#pickB").addEventListener("click", () => $("#popMaps").hidden || pickSide !== "B" ? openMapPicker("B") : closePopovers());
  $("#opacity").addEventListener("input", e => { S.opacity = +e.target.value / 100; store.set("opacity", S.opacity); $("#opVal").textContent = e.target.value + "%"; applyEra(); });
  $("#locBtn").addEventListener("click", locate);
  $("#filterOpen").addEventListener("click", () => togglePopover("popLayers", $("#layersBtn")));
  $("#filterClear").addEventListener("click", () => { S.hidden = new Set(); S.years = null; kindsChanged(true); setYears(null); });
  $("#zin").addEventListener("click", () => m.zoomIn());
  $("#zout").addEventListener("click", () => m.zoomOut());
  $("#compass").addEventListener("click", () => m.easeTo({ bearing: 0, duration: reduceMotion ? 0 : 300 }));
  m.on("rotate", () => { $("#compass svg").style.transform = `rotate(${-m.getBearing()}deg)`; });

  m.on("dataloading", e => { if (e.dataType === "source" && !tilesBusy) { tilesBusy = true; updateLoading(); } });
  m.on("idle", () => { if (tilesBusy) { tilesBusy = false; updateLoading(); } });
  let saveViewTimer;
  m.on("moveend", e => {
    if (e?.trackpadMove) return;
    clearTimeout(saveViewTimer);
    saveViewTimer = setTimeout(() => { store.set("center", m.getCenter().toArray()); store.set("zoom", m.getZoom()); }, 250);
    updateCounts(); scheduleLoad(); renderMapbar(); libRefreshView(); if (S.view.name === "records") recRefreshView();
  });

  show("home", {}, { push: false });
  // Enable sheet animation only after the first layout, so the sheet doesn't visibly jump on load.
  requestAnimationFrame(() => requestAnimationFrame(() => $("#panel").classList.remove("booting")));
  // Checked once the first view has drawn, so these requests don't hold up the evidence on screen.
  m.once("idle", checkEmptyKinds);
  loadSources();
  loadRecords();
  await loadCases();
  if (S.caseId) await selectCase(S.caseId);
  if (S.view.name === "home") render();
})();
