"""Zero-dependency Model Context Protocol (MCP) stdio server for Map Vietnam.

Lets any MCP-compatible AI harness (Antigravity, Claude Code, Codex, Cursor) interact directly with a
running Map Vietnam instance: navigate the live map, switch layers, take screenshots, inspect aerial
scans/flight-index cards/tiles as images, search places & MGRS grid references, query historical
evidence, inspect research cases, search/import aerial archives, and run auto-matching or placement.

Connection order:
  1. `MAP_VIETNAM_URL` environment variable (e.g. `http://127.0.0.1:8765`)
  2. `~/.cache/map-vietnam/server_url` (written automatically by `server.py` and `Map Vietnam.app`)
  3. Default `http://127.0.0.1:8765`
"""

import argparse
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "map-vietnam", "version": "0.1.0"}
URL_FILE = Path.home() / ".cache" / "map-vietnam" / "server_url"
DEFAULT_URL = "http://127.0.0.1:8765"


def resolve_base_url():
    """Find the active local Map Vietnam server URL."""
    env_url = (os.environ.get("MAP_VIETNAM_URL") or "").strip().rstrip("/")
    if env_url:
        return env_url
    try:
        saved = URL_FILE.read_text(encoding="utf-8").strip().rstrip("/")
        if saved.startswith("http://127.0.0.1:"):
            return saved
    except OSError:
        pass
    return DEFAULT_URL


def _api(method, route, *, body=None, params=None, timeout=30):
    """Call Map Vietnam's local JSON/binary HTTP API."""
    base = resolve_base_url()
    qs = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"{base}{route}{qs}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "*/*"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type") or ""
            return raw, ctype
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw).get("error") or f"HTTP {e.code}"
        except ValueError:
            msg = raw[:300] or f"HTTP {e.code}"
        raise RuntimeError(f"Map Vietnam API error ({e.code}): {msg}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Could not reach Map Vietnam at {base} ({e.reason}). Start Map Vietnam (or scripts/server.py) first."
        ) from None


def _api_json(method, route, *, body=None, params=None, timeout=30):
    raw, _ = _api(method, route, body=body, params=params, timeout=timeout)
    return json.loads(raw.decode("utf-8"))


def _resize_jpeg(raw_bytes, max_side=1280):
    """Decode any image bytes and return JPEG bytes scaled to at most `max_side` px on the longest side."""
    with Image.open(io.BytesIO(raw_bytes)) as img:
        img = img.convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue(), img.size


def _text(obj):
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
    return [{"type": "text", "text": text}]


def _image_and_text(jpeg_bytes, caption):
    return [
        {"type": "image", "data": base64.b64encode(jpeg_bytes).decode("ascii"), "mimeType": "image/jpeg"},
        {"type": "text", "text": caption if isinstance(caption, str) else json.dumps(caption, ensure_ascii=False)},
    ]


TOOLS = [
    {
        "name": "map_status",
        "description": "Check Map Vietnam server health, live viewer viewport state, Hub connection, and map/case counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "map_navigate",
        "description": "Fly or fit the live Map Vietnam viewer to coordinates (lon, lat, zoom) or a bounding box [west, south, east, north].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lon": {"type": "number", "description": "Target longitude (WGS84)"},
                "lat": {"type": "number", "description": "Target latitude (WGS84)"},
                "zoom": {"type": "number", "description": "Optional zoom level (e.g. 12)"},
                "bounds": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "Bounding box [west, south, east, north] (alternative to lon/lat)",
                },
            },
        },
    },
    {
        "name": "map_set_layer",
        "description": "Change the live viewer's base map (sat, osm, topo, oam) and/or select a historical map overlay by id (or null for today only) and opacity (0..1).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "enum": ["sat", "osm", "topo", "oam"]},
                "era_id": {"type": ["string", "null"], "description": "Historical map id, or null to hide overlay"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    },
    {
        "name": "map_screenshot",
        "description": "Capture the live Map Vietnam viewport as a JPEG image so you can visually inspect the map, overlays, and alignment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "max_side": {"type": "integer", "default": 1280, "minimum": 320, "maximum": 2048},
            },
        },
    },
    {
        "name": "open_viewer_panel",
        "description": "Open a panel in the live Map Vietnam viewer (aerials, library, cases, settings, or home) and optional aerial tab.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["home", "aerials", "library", "cases", "settings"]},
                "tab": {"type": "string", "enum": ["nakala", "landsat", "usgs", "allmaps", "oam", "terrain"]},
            },
            "required": ["view"],
        },
    },
    {
        "name": "search_places",
        "description": "Search Map Vietnam for towns, built-in SGI 1:100k sheets, evidence records, decimal coordinates, or MGRS grid references.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Query string (place name, MGRS ref, or lat, lon)"},
                "limit": {"type": "integer", "default": 15, "minimum": 1, "maximum": 100},
            },
            "required": ["q"],
        },
    },
    {
        "name": "convert_gridref",
        "description": "Convert an MGRS grid reference (e.g. WS683427) to WGS84 lon/lat, or convert lon/lat to Indian 1960 & WGS84 MGRS.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "MGRS grid ref (e.g. WS683427)"},
                "datum": {"type": "string", "enum": ["indian1960", "wgs84"], "default": "indian1960"},
                "zone": {"type": "string", "default": "48P"},
                "lon": {"type": "number", "description": "Longitude (for reverse lookup)"},
                "lat": {"type": "number", "description": "Latitude (for reverse lookup)"},
            },
        },
    },
    {
        "name": "query_evidence",
        "description": "Query historical evidence records inside a bounding box [west, south, east, north].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bbox": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                },
                "from_year": {"type": "integer"},
                "to_year": {"type": "integer"},
                "kinds": {"type": "string", "description": "Comma-separated evidence kinds"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": ["bbox"],
        },
    },
    {
        "name": "list_maps",
        "description": "List maps in the Map Vietnam library, optionally filtered by state (installed, needs_points, available), text query, or custom_only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["installed", "needs_points", "available", "building", "queued"]},
                "query": {"type": "string"},
                "custom_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 40, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "list_cases",
        "description": "List research cases, or fetch full details (sites, notes, shapes, items) when case_id is given.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer", "description": "Optional case id for full detail"},
            },
        },
    },
    {
        "name": "search_aerials",
        "description": "Search historical aerial/satellite archives: nakala (French SHD 1948-56, uses q), landsat (1972-75 MSS GeoTIFFs, uses bbox), usgs (CORONA/Keyhole, uses bbox), allmaps (georeferenced IIIF, uses bbox), or oam (UAV, uses bbox).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["nakala", "landsat", "usgs", "allmaps", "oam"]},
                "q": {"type": "string", "description": "Search query (for nakala)"},
                "bbox": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "[west, south, east, north] (for landsat, usgs, allmaps, oam)",
                },
                "page": {"type": "integer", "default": 1},
                "size": {"type": "integer", "default": 8},
            },
            "required": ["provider"],
        },
    },
    {
        "name": "import_aerial",
        "description": "Import a historical aerial scan, flight-index card, Landsat GeoTIFF, or Allmaps IIIF map into Map Vietnam.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "https:// image or GeoTIFF URL"},
                "title": {"type": "string"},
                "year": {"type": "string"},
                "kind": {"type": "string", "enum": ["photo", "map"], "default": "photo"},
                "bounds": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                "vicinity_label": {"type": "string"},
                "plot_url": {"type": "string"},
                "plot_label": {"type": "string"},
            },
            "required": ["url", "title"],
        },
    },
    {
        "name": "view_image",
        "description": "View an image directly as an image block: either a custom scan preview (map_id), a rendered map tile (map_id + z, x, y), or an external https:// preview/flight-index URL (url).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "map_id": {
                    "type": "string",
                    "description": "Custom map id (custom_...) or installed map id (with z/x/y)",
                },
                "z": {"type": "integer"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "url": {"type": "string", "description": "Optional https:// image URL (e.g. NAKALA plot or preview)"},
                "max_side": {"type": "integer", "default": 1280, "minimum": 256, "maximum": 2048},
            },
        },
    },
    {
        "name": "place_map",
        "description": "Save control points [[col, row, lon, lat], ...] for a custom scan (in full work.jpg pixels) and queue its tile build.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "map_id": {"type": "string"},
                "points": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "minItems": 3,
                },
                "title": {"type": "string"},
                "year": {"type": ["string", "integer"]},
                "warp_mode": {"type": "string", "enum": ["auto", "homography", "tps", "affine"], "default": "auto"},
            },
            "required": ["map_id", "points"],
        },
    },
    {
        "name": "chain_strip",
        "description": "Plan or apply survey-strip chaining from a placed custom frame to its overlapping neighbours (runs on Hub when connected).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "map_id": {"type": "string"},
                "confidence_threshold": {"type": "number", "default": 0.8},
                "apply": {
                    "type": "array",
                    "description": "Optional list of proposals from a previous plan to apply immediately",
                },
            },
            "required": ["map_id"],
        },
    },
    {
        "name": "query_elevation",
        "description": "Query terrain elevation at a point (lon, lat) or along a line transect [[lon, lat], ...].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lon": {"type": "number"},
                "lat": {"type": "number"},
                "line": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                    "description": "Optional polyline [[lon, lat], ...] for a transect profile",
                },
            },
        },
    },
]


def call_tool(name, args):
    args = args or {}
    if name == "map_status":
        lib = _api_json("GET", "/api/library")
        hub = _api_json("GET", "/api/hub")
        cases = _api_json("GET", "/api/cases")
        remote = _api_json("GET", "/api/remote/state")
        maps = lib.get("maps") or []
        return _text(
            {
                "server_url": resolve_base_url(),
                "viewer": remote,
                "hub": {"configured": hub.get("configured"), "connected": hub.get("connected"), "url": hub.get("url")},
                "maps_total": len(maps),
                "maps_installed": sum(1 for m in maps if m.get("state") == "installed"),
                "maps_needs_points": sum(1 for m in maps if m.get("state") == "needs_points"),
                "cases_count": len(cases),
            }
        )

    if name == "map_navigate":
        params = {}
        if args.get("bounds"):
            params["bounds"] = [float(v) for v in args["bounds"][:4]]
        elif "lon" in args and "lat" in args:
            params["lon"] = float(args["lon"])
            params["lat"] = float(args["lat"])
            if args.get("zoom") is not None:
                params["zoom"] = float(args["zoom"])
        else:
            raise ValueError("Provide either bounds=[w,s,e,n] or lon and lat.")
        return _text(_api_json("POST", "/api/remote/command", body={"action": "navigate", "params": params}))

    if name == "map_set_layer":
        params = {k: args[k] for k in ("base", "era_id", "opacity") if k in args}
        return _text(_api_json("POST", "/api/remote/command", body={"action": "set_layer", "params": params}))

    if name == "map_screenshot":
        max_side = int(args.get("max_side") or 1280)
        res = _api_json("POST", "/api/remote/command", body={"action": "screenshot", "params": {"max_side": max_side}})
        b64 = (res.get("result") or {}).get("image_b64") or ""
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        if not b64:
            raise RuntimeError("Viewer did not return a screenshot image.")
        raw = base64.b64decode(b64)
        return _image_and_text(raw, {"state": res.get("state")})

    if name == "open_viewer_panel":
        params = {"view": args["view"]}
        if args.get("tab"):
            params["tab"] = args["tab"]
        return _text(_api_json("POST", "/api/remote/command", body={"action": "open_view", "params": params}))

    if name == "search_places":
        return _text(_api_json("GET", "/api/search", params={"q": args["q"], "limit": int(args.get("limit") or 15)}))

    if name == "convert_gridref":
        if args.get("ref"):
            return _text(
                _api_json(
                    "GET",
                    "/api/gridref",
                    params={
                        "ref": args["ref"],
                        "datum": args.get("datum") or "indian1960",
                        "zone": args.get("zone") or "48P",
                    },
                )
            )
        if "lon" in args and "lat" in args:
            return _text(_api_json("GET", "/api/lonlat", params={"lon": args["lon"], "lat": args["lat"]}))
        raise ValueError("Provide either ref (MGRS) or lon and lat.")

    if name == "query_evidence":
        bbox = ",".join(str(float(v)) for v in args["bbox"][:4])
        params = {"bbox": bbox, "limit": int(args.get("limit") or 50)}
        if args.get("from_year") is not None:
            params["from"] = int(args["from_year"])
        if args.get("to_year") is not None:
            params["to"] = int(args["to_year"])
        if args.get("kinds"):
            params["kinds"] = str(args["kinds"])
        return _text(_api_json("GET", "/api/evidence", params=params))

    if name == "list_maps":
        lib = _api_json("GET", "/api/library")
        maps = lib.get("maps") or []
        if args.get("custom_only"):
            maps = [m for m in maps if m.get("custom")]
        if args.get("state"):
            maps = [m for m in maps if m.get("state") == args["state"]]
        if args.get("query"):
            q = str(args["query"]).lower()
            maps = [m for m in maps if q in (m.get("title") or "").lower() or q in (m.get("id") or "").lower()]
        limit = int(args.get("limit") or 40)
        summary = [
            {
                "id": m.get("id"),
                "title": m.get("title"),
                "state": m.get("state"),
                "kind": m.get("kind"),
                "subject": m.get("subject"),
                "year": m.get("year") or m.get("date"),
                "bounds": m.get("bounds"),
                "custom": bool(m.get("custom")),
                "points_count": len(m.get("points") or []),
                "image": m.get("image"),
                "vicinity_label": m.get("vicinity_label"),
                "plot_url": m.get("plot_url"),
            }
            for m in maps[:limit]
        ]
        return _text({"total_matching": len(maps), "maps": summary})

    if name == "list_cases":
        if args.get("case_id") is not None:
            return _text(_api_json("GET", f"/api/cases/{int(args['case_id'])}"))
        return _text(_api_json("GET", "/api/cases"))

    if name == "search_aerials":
        provider = str(args["provider"])
        params = {}
        if provider == "nakala":
            params = {"q": args.get("q") or "", "page": int(args.get("page") or 1), "size": int(args.get("size") or 8)}
        else:
            if not args.get("bbox"):
                raise ValueError(f"{provider} search requires bbox=[west, south, east, north].")
            params = {"bbox": ",".join(str(float(v)) for v in args["bbox"][:4])}
        return _text(_api_json("GET", f"/api/aerial/{provider}", params=params, timeout=45))

    if name == "import_aerial":
        body = {
            k: args[k]
            for k in ("url", "title", "year", "kind", "bounds", "vicinity_label", "plot_url", "plot_label")
            if k in args
        }
        res = _api_json("POST", "/api/aerial/import", body=body, timeout=120)
        return _text({"map": res.get("map")})

    if name == "view_image":
        max_side = int(args.get("max_side") or 1280)
        if args.get("map_id") and all(k in args for k in ("z", "x", "y")):
            mid = urllib.parse.quote(str(args["map_id"]), safe="")
            raw, _ = _api("GET", f"/tiles/{mid}/{int(args['z'])}/{int(args['x'])}/{int(args['y'])}.webp")
            jpeg, size = _resize_jpeg(raw, max_side=max_side)
            return _image_and_text(jpeg, {"source": "tile", "map_id": args["map_id"], "size": list(size)})
        if args.get("map_id"):
            mid = urllib.parse.quote(str(args["map_id"]), safe="")
            raw, _ = _api("GET", f"/api/library/custom/{mid}/preview")
            jpeg, size = _resize_jpeg(raw, max_side=max_side)
            return _image_and_text(jpeg, {"source": "custom_preview", "map_id": args["map_id"], "size": list(size)})
        if args.get("url"):
            u = str(args["url"]).strip()
            if not u.startswith("https://"):
                raise ValueError("External image url must use https://.")
            req = urllib.request.Request(u, headers={"User-Agent": "MapVietnam-MCP/0.1"})
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read(32 << 20)
            jpeg, size = _resize_jpeg(raw, max_side=max_side)
            return _image_and_text(jpeg, {"source": "url", "url": u, "size": list(size)})
        raise ValueError("Provide map_id (optionally with z, x, y) or an https:// url.")

    if name == "place_map":
        mid = urllib.parse.quote(str(args["map_id"]), safe="")
        body = {k: args[k] for k in ("points", "title", "year", "warp_mode") if k in args}
        res = _api_json("POST", f"/api/library/custom/{mid}/points", body=body)
        placed = next((m for m in res.get("maps", []) if m.get("id") == args["map_id"]), None)
        return _text({"ok": True, "map": placed})

    if name == "chain_strip":
        mid = urllib.parse.quote(str(args["map_id"]), safe="")
        body = (
            {"apply": args["apply"]}
            if args.get("apply") is not None
            else {"confidence_threshold": float(args.get("confidence_threshold", 0.8))}
        )
        return _text(_api_json("POST", f"/api/library/custom/{mid}/chain", body=body, timeout=180))

    if name == "query_elevation":
        if args.get("line"):
            return _text(_api_json("POST", "/api/terrain/transect", body={"line": args["line"]}))
        if "lon" in args and "lat" in args:
            return _text(_api_json("GET", "/api/terrain/elevation", params={"lon": args["lon"], "lat": args["lat"]}))
        raise ValueError("Provide either lon & lat or line=[[lon, lat], ...].")

    raise ValueError(f"Unknown tool: {name}")


def handle_rpc(msg):
    """Process one JSON-RPC 2.0 request dict; returns a response dict or None for notifications."""
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "notifications/initialized" or req_id is None:
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                content = call_tool(params.get("name"), params.get("arguments") or {})
                result = {"content": content, "isError": False}
            except Exception as e:
                result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the Map Vietnam Model Context Protocol (MCP) stdio server.")
    ap.add_argument("--url", default=None, help="Map Vietnam server URL (overrides MAP_VIETNAM_URL and auto-discovery)")
    args = ap.parse_args(argv)
    if args.url:
        os.environ["MAP_VIETNAM_URL"] = args.url
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_rpc(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
