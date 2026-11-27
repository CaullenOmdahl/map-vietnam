# syntax=docker/dockerfile:1
# Map Vietnam Intermediary Hub Server — Railway Docker + Attached Persistent Volume (/data)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HUB_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        gosu \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system mapvietnam \
    && useradd --system --gid mapvietnam --home-dir /app --no-create-home mapvietnam

WORKDIR /app

# Install the pinned geospatial & raster wheels shared with the desktop app (skipping desktop GUI pywebview).
COPY requirements.txt /app/requirements.txt
RUN grep -Ev '^(pywebview|pytest)==' /app/requirements.txt > /tmp/requirements-hub.txt \
    && pip install --no-cache-dir -r /tmp/requirements-hub.txt \
    && rm -f /tmp/requirements-hub.txt

COPY pyproject.toml LICENSE NOTICE /app/
COPY data/sources /app/data/sources
COPY data/scans /app/data/scans
COPY data/maps.json /app/data/maps.json
COPY scripts /app/scripts

RUN chmod +x /app/scripts/hub/entrypoint.sh && mkdir -p /data && chown -R mapvietnam:mapvietnam /data /app

VOLUME ["/data"]
EXPOSE 8780

ENTRYPOINT ["/app/scripts/hub/entrypoint.sh"]
