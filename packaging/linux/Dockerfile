# Linux build environment: Python 3.12 on Ubuntu 22.04, plus the system libraries Qt's web view loads.
# packaging/build.sh linux builds this image and uses it; there is no need to run it by hand.
#
# Ubuntu 22.04 on purpose: the app links against the build system's glibc (2.35 here), so it starts
# on that and anything newer (Debian 12, Fedora, Arch, Mint 21...). A build on a newer system would
# not start on older ones. Older images lack libxcb-cursor0, which Qt 6 needs to open a window.
# Python comes from python-build-standalone (via uv), as Ubuntu 22.04's own is 3.10.

FROM ubuntu:22.04

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
COPY --from=ghcr.io/goreleaser/nfpm:v2.45.0 /usr/bin/nfpm /usr/local/bin/nfpm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates binutils xvfb xauth \
      libgl1 libegl1 libopengl0 libglib2.0-0 libfontconfig1 libfreetype6 libdbus-1-3 \
      libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
      libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libx11-xcb1 \
      libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxshmfence1 libxkbfile1 libnss3 libasound2 \
 && rm -rf /var/lib/apt/lists/* \
 && uv python install 3.12 \
 && chmod -R a+rX /opt/python \
 && uv python find 3.12
