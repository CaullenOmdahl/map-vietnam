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
RUN grep -Ev '^(pywebview|bottle|pytest)==' /app/requirements.txt > /tmp/requirements-hub.txt \
    && pip install --no-cache-dir -r /tmp/requirements-hub.txt \
    && rm -f /tmp/requirements-hub.txt

# Optional learned Auto-GCP matcher (SuperPoint + LightGlue, ONNX Runtime CPU).
# Off by default: the SuperPoint weights are licensed for non-commercial research use only
# (Magic Leap), so client images must opt in explicitly with `--build-arg AUTO_GCP_WEIGHTS=superpoint`.
# Without the weights the Hub falls back to the built-in NumPy matcher.
ARG AUTO_GCP_WEIGHTS=none
ARG AUTO_GCP_MODEL_URL=https://github.com/fabio-sim/LightGlue-ONNX/releases/download/v2.0/superpoint_lightglue_pipeline.onnx
ARG AUTO_GCP_MODEL_SHA256=228994cea8c010146fa2aef933baa3ffaa4bcdc522bc8aa560087fcff8134526
ENV AUTO_GCP_MODEL_DIR=/opt/auto-gcp
RUN if [ "$AUTO_GCP_WEIGHTS" = "superpoint" ]; then \
        pip install --no-cache-dir onnxruntime==1.32.0 \
        && mkdir -p /opt/auto-gcp \
        && python -c "import hashlib, sys, urllib.request; d = urllib.request.urlopen(sys.argv[1], timeout=120).read(); h = hashlib.sha256(d).hexdigest(); assert h == sys.argv[2], h; open('/opt/auto-gcp/superpoint_lightglue_pipeline.onnx', 'wb').write(d)" "$AUTO_GCP_MODEL_URL" "$AUTO_GCP_MODEL_SHA256"; \
    fi

COPY pyproject.toml LICENSE NOTICE /app/
COPY data/sources /app/data/sources
COPY data/scans /app/data/scans
COPY data/maps.json /app/data/maps.json
COPY scripts /app/scripts

RUN chmod +x /app/scripts/hub/entrypoint.sh && mkdir -p /data && chown -R mapvietnam:mapvietnam /data /app

VOLUME ["/data"]
EXPOSE 8780

ENTRYPOINT ["/app/scripts/hub/entrypoint.sh"]
