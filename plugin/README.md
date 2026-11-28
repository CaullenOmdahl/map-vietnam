# Map Vietnam MCP Plugin

Connects Antigravity (or any MCP harness such as Claude Code, Codex, or Cursor) directly to a running Map Vietnam instance (`scripts/server.py` or `Map Vietnam.app`).

## Capabilities

- **Live viewer control:** navigate to coordinates/bounds (`map_navigate`), switch base maps & historical overlays (`map_set_layer`), open panels (`open_viewer_panel`), and capture viewport screenshots (`map_screenshot`).
- **Visual image inspection:** inspect custom scan previews, rendered map tiles, or archive flight-index cards directly as image blocks (`view_image`).
- **Geospatial & archive queries:** search places/SGI sheets/MGRS (`search_places`, `convert_gridref`), query historical evidence (`query_evidence`), inspect cases (`list_cases`), sample elevation/transects (`query_elevation`), and search aerial archives (`search_aerials`).
- **Batch ingestion & library management:** stream local folders of scans/GeoTIFFs/PDFs in one call (`import_local_files`), batch-import up to 40 archive aerials in a single round-trip (`batch_import_aerials`, or single `import_aerial`), queue overlapping stepping-stone catalogue maps by bbox/series or edit/remove maps (`manage_maps`), and block until background tile builds finish (`wait_for_builds`).
- **Automated placement, strip stitching & Hub sharing:** auto-match a scan against the live viewer viewport and optionally place it (`auto_match_map`), place manually (`place_map`), chain overlapping survey-strip frames (`chain_strip`), stitch placed frames into one composite strip mosaic (`build_strip_mosaic`), and push installed maps to a connected Hub (`push_to_hub`).

## Batch workflow examples

### 1. Ingest a local folder of scans, auto-match, and wait for tiles
1. Import every scan in a local directory:
   - `import_local_files(paths=["/path/to/1952-strip"], year="1952", title_prefix="SHD 1952")`
2. Position the live viewer over the target area:
   - `map_navigate(lon=105.63, lat=10.46, zoom=13)`
3. Auto-match and place the anchor frame in one step:
   - `auto_match_map(map_id="custom_...", confidence_threshold=0.75, auto_place=True)`
4. Chain the rest of the strip, stitch a composite mosaic, and wait for builds:
   - `chain_strip(map_id="custom_...")` -> `chain_strip(map_id="custom_...", apply=[...])`
   - `build_strip_mosaic(map_id="custom_...")`
   - `wait_for_builds(ids=["custom_..."], timeout_s=180)`

### 2. Stepping-stone catalogue + batch archive aerials + Hub publish
1. Queue overlapping L7014 1:50k sheets for a region of interest:
   - `manage_maps(action="add", bbox=[105.5, 10.3, 105.8, 10.6], series="l7014")`
2. Search an archive and import a whole flight strip in one call:
   - `search_aerials(provider="nakala", q="Cao Lanh")`
   - `batch_import_aerials(items=[{"url": "https://...", "title": "Frame 01", "year": "1952"}, ...])`
3. Wait for tile builds to finish and publish to the team Hub:
   - `wait_for_builds(ids=["l7014_...", "custom_..."])`
   - `push_to_hub(ids=["custom_..."], visibility="hub")`

## Setup in other MCP harnesses

Point your MCP client at `scripts/mcp_server.py` using the repo's Python 3.12 virtualenv:

```json
{
  "mcpServers": {
    "map-vietnam": {
      "command": "/path/to/map-vietnam/.venv/bin/python",
      "args": ["/path/to/map-vietnam/scripts/mcp_server.py"]
    }
  }
}
```

The server automatically discovers the running Map Vietnam URL via `MAP_VIETNAM_URL`, `~/.cache/map-vietnam/server_url` (written automatically by `server.py` and `Map Vietnam.app`), or `http://127.0.0.1:8765`.
