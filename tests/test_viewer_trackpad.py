"""Static and Node-backed behavioral checks for Apple Maps-style trackpad navigation in web/app."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "web" / "app" / "app.js"
APP_CSS = ROOT / "web" / "app" / "app.css"
INDEX_HTML = ROOT / "web" / "app" / "index.html"


def _extract_trackpad_helpers():
    text = APP_JS.read_text(encoding="utf-8")
    start = text.index("const WHEEL_GAP_MS = ")
    end = text.index("function wireTrackpad(m) {")
    return text[start:end]


# ---- static checks ---------------------------------------------------------------------------------
def test_overscroll_behavior_is_disabled_on_page_and_map_containers():
    css = APP_CSS.read_text(encoding="utf-8")
    for selector in ("html, body", "#maps", ".pl-map"):
        m = re.search(rf"{re.escape(selector)}\s*\{{[^}}]*overscroll-behavior:\s*none\b", css, re.S)
        assert m, f"{selector} should set overscroll-behavior: none"


def test_trackpad_handler_is_wired_to_all_maps_and_guarded_against_frame_thrash():
    js = APP_JS.read_text(encoding="utf-8")
    assert "wireTrackpad(m);" in js
    assert re.search(r"\[pl\.scan,\s*pl\.today\]\.forEach\([^\n]*wireTrackpad\(m\)", js)
    assert "SCROLL_MODES" in js and 'ACTIONS.scrollMode' in js
    assert "e?.trackpadMove" in js


def test_overlay_backgrounds_use_60_percent_frosted_glass():
    css = APP_CSS.read_text(encoding="utf-8")
    glass_vals = re.findall(r"--glass:\s*rgba\([^)]+\);", css)
    assert len(glass_vals) == 3
    for val in glass_vals:
        assert ".60)" in val, f"expected 60% opacity in {val}"
    assert re.search(r"\.popover\s*\{[^}]*background:\s*var\(--glass\)[^}]*backdrop-filter:", css, re.S)


def test_index_html_bumps_cache_busters_for_css_and_js():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css_v = re.search(r"/app/app\.css\?v=(\d+)", html)
    js_v = re.search(r"/app/app\.js\?v=(\d+)", html)
    assert css_v and int(css_v.group(1)) >= 38
    assert js_v and int(js_v.group(1)) >= 43


# ---- behavioral JS checks via Node -----------------------------------------------------------------
def test_app_js_passes_node_syntax_check():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "--check", str(APP_JS)], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr


def test_trackpad_classification_and_mercator_zoom_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    helpers_js = _extract_trackpad_helpers()
    script = f"""
"use strict";
const assert = require("node:assert/strict");
const store = {{ get: (_k, d) => d, set: () => {{}} }};
const settingsRefresh = () => {{}};

{helpers_js}

// 1. isMouseWheelEvent: discrete wheel vs smooth trackpad
assert.equal(isMouseWheelEvent({{ deltaMode: 1, deltaX: 0, deltaY: 3 }}), true);
assert.equal(isMouseWheelEvent({{ deltaMode: 2, deltaX: 0, deltaY: 1 }}), true);
assert.equal(isMouseWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: 100, wheelDeltaY: -120 }}), true);
assert.equal(isMouseWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: -100, wheelDeltaY: 120 }}), true);
assert.equal(isMouseWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: 40, wheelDeltaY: -120 }}), false);
assert.equal(isMouseWheelEvent({{ deltaMode: 0, deltaX: 12, deltaY: 100, wheelDeltaY: -120 }}), false);
assert.equal(isMouseWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: 18.5, wheelDeltaY: -55 }}), false);
assert.equal(isMouseWheelEvent({{ deltaMode: 1, deltaX: 0, deltaY: 3, ctrlKey: true }}), false);
assert.equal(isMouseWheelEvent({{ deltaMode: 1, deltaX: 0, deltaY: 3, metaKey: true }}), false);

// 2. classifyWheelEvent in "auto" mode: trackpad flick momentum lock & horizontal override
const st = {{ mode: null, lastTime: 0, gestureAt: 0, gestureActive: false }};
assert.equal(classifyWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: 8.2, wheelDeltaY: -24 }}, st, "auto", 1000), "pan");
// Even if a tail frame looks wheel-like within 180ms, momentum stays locked to pan
assert.equal(classifyWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: 100, wheelDeltaY: -120 }}, st, "auto", 1050), "pan");
// After >180ms gap, a notched mouse wheel is classified as zoom
assert.equal(classifyWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: 100, wheelDeltaY: -120 }}, st, "auto", 1300), "zoom");
// Horizontal delta always locks to pan immediately
assert.equal(classifyWheelEvent({{ deltaMode: 0, deltaX: -6, deltaY: 0, wheelDeltaY: 0 }}, st, "auto", 1320), "pan");

// 3. Pinch & WebKit gesture deduplication
assert.equal(classifyWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: -4, ctrlKey: true }}, st, "auto", 1600), "zoom");
st.gestureActive = true;
st.gestureAt = 2000;
assert.equal(classifyWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: -4, ctrlKey: true }}, st, "auto", 2020), "ignore");
st.gestureActive = false;
// Two-finger pan immediately after gestureend still pans
assert.equal(classifyWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: 10, ctrlKey: false }}, st, "auto", 2040), "pan");
// Cooldown expired (>120ms) lets ctrlKey wheel zoom again
assert.equal(classifyWheelEvent({{ deltaMode: 0, deltaX: 0, deltaY: -4, ctrlKey: true }}, st, "auto", 2200), "zoom");

// 4. Explicit scroll mode overrides
assert.equal(classifyWheelEvent({{ deltaMode: 1, deltaX: 0, deltaY: 3 }}, {{}}, "trackpad", 3000), "pan");
assert.equal(classifyWheelEvent({{ deltaMode: 1, deltaX: 0, deltaY: 3, ctrlKey: true }}, {{}}, "trackpad", 3010), "zoom");
assert.equal(classifyWheelEvent({{ deltaMode: 0, deltaX: 15, deltaY: 12 }}, {{}}, "wheel", 3020), "zoom");

// 5. zoomAroundMercator keeps anchor pixel invariant relative to center
class MercatorCoordinate {{
  constructor(x, y, z = 0) {{ this.x = x; this.y = y; this.z = z; }}
  static fromLngLat(ll) {{
    const lng = Array.isArray(ll) ? ll[0] : ll.lng;
    const lat = Array.isArray(ll) ? ll[1] : ll.lat;
    const x = (180 + lng) / 360;
    const sin = Math.sin(lat * Math.PI / 180);
    const y = (180 - (180 / Math.PI) * Math.log((1 + sin) / (1 - sin)) / 2) / 360;
    return new MercatorCoordinate(x, y, 0);
  }}
  toLngLat() {{
    const lng = this.x * 360 - 180;
    const y2 = (180 - this.y * 360) * Math.PI / 180;
    const lat = (360 / Math.PI) * Math.atan(Math.exp(y2)) - 90;
    return {{ lng, lat }};
  }}
}}
const oldCenter = {{ lng: 105.63, lat: 10.46 }};
const anchor = {{ lng: 106.15, lat: 10.82 }};
const oldZ = 10.5, newZ = 13.25;
const newCenter = zoomAroundMercator(oldCenter, anchor, oldZ, newZ, MercatorCoordinate);
const c0 = MercatorCoordinate.fromLngLat(oldCenter);
const c1 = MercatorCoordinate.fromLngLat(newCenter);
const a0 = MercatorCoordinate.fromLngLat(anchor);
const dxBefore = (a0.x - c0.x) * Math.pow(2, oldZ);
const dyBefore = (a0.y - c0.y) * Math.pow(2, oldZ);
const dxAfter = (a0.x - c1.x) * Math.pow(2, newZ);
const dyAfter = (a0.y - c1.y) * Math.pow(2, newZ);
assert.ok(Math.abs(dxBefore - dxAfter) < 1e-9);
assert.ok(Math.abs(dyBefore - dyAfter) < 1e-9);

// Clamped Mercator Y at poles when zooming out aggressively
const clamped = zoomAroundMercator({{ lng: 105, lat: 84 }}, {{ lng: 105, lat: 0 }}, 12, 1, MercatorCoordinate);
assert.ok(Number.isFinite(clamped.lat) && clamped.lat <= 85.052);
console.log(JSON.stringify({{ ok: true }}));
"""
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, f"stderr: {done.stderr}\nstdout: {done.stdout}"
    assert json.loads(done.stdout.strip()) == {"ok": True}
