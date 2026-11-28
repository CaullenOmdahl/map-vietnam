"""Map Vietnam Intermediary Hub Server package.

Provides an optional Docker/Railway-deployable hub backed by an attached persistent volume:
  - auth.py     scoped client API keys (salted scrypt) and upstream credential vault
  - polite.py   per-host polite rate limiter and single-flight request deduplication
  - storage.py  infinite write-once cache (/data) for raw archives, MBTiles, DEMs and user uploads
  - hub_server.py  HTTP API (/hub/v1/*) and operator CLI
"""
