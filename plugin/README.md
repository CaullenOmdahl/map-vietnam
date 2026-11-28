# Map Vietnam MCP Plugin

Connects Antigravity (or any MCP harness such as Claude Code, Codex, or Cursor) directly to a running Map Vietnam instance (`scripts/server.py` or `Map Vietnam.app`).

## Capabilities
- **Live viewer control:** navigate to coordinates/bounds (`map_navigate`), switch base maps & historical overlays (`map_set_layer`), open panels (`open_viewer_panel`), and capture viewport screenshots (`map_screenshot`).
- **Visual image inspection:** inspect custom scan previews, rendered map tiles, or archive flight-index cards directly as image blocks (`view_image`).
- **Geospatial & archive queries:** search places/SGI sheets/MGRS (`search_places`, `convert_gridref`), query historical evidence (`query_evidence`), inspect cases (`list_cases`), sample elevation/transects (`query_elevation`), and search/import/place/chain aerial archives (`search_aerials`, `import_aerial`, `place_map`, `chain_strip`).

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
