#!/bin/sh
set -eu

VOLUME_DIR="${HUB_DATA_DIR:-${RAILWAY_VOLUME_MOUNT_PATH:-/data}}"
mkdir -p "$VOLUME_DIR"

if [ "$(id -u)" = "0" ]; then
    chown -R mapvietnam:mapvietnam "$VOLUME_DIR"
    exec gosu mapvietnam python3 scripts/hub/hub_server.py serve --host 0.0.0.0 --port "${PORT:-8780}" --data "$VOLUME_DIR"
fi

exec python3 scripts/hub/hub_server.py serve --host 0.0.0.0 --port "${PORT:-8780}" --data "$VOLUME_DIR"
