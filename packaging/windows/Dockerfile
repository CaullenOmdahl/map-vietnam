# Windows build environment that runs on Linux: Wine with the Windows build of Python (for
# PyInstaller and the smoke tests) and NSIS, whose compiler runs natively on Linux.
# packaging/build.sh windows builds this image and uses it; there is no need to run it by hand.
#
# Wine 11 or newer is needed: numpy calls ucrtbase's crealf, which older Wine does not have.

FROM debian:trixie-slim

ARG PYTHON_VERSION=3.12.10

ENV DEBIAN_FRONTEND=noninteractive \
    WINEPREFIX=/opt/wineprefix \
    WINEARCH=win64 \
    WINEDEBUG=-all \
    PYTHONUTF8=1

RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg xvfb xauth nsis \
 && mkdir -pm755 /etc/apt/keyrings \
 && curl -fsSL https://dl.winehq.org/wine-builds/winehq.key | gpg --dearmor -o /etc/apt/keyrings/winehq-archive.key \
 && curl -fsSL -o /etc/apt/sources.list.d/winehq-trixie.sources https://dl.winehq.org/wine-builds/debian/dists/trixie/winehq-trixie.sources \
 && apt-get update \
 && apt-get install -y --install-recommends winehq-stable \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/python.exe "https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-amd64.exe" \
 && wineboot --init && wineserver -w \
 && xvfb-run -a wine /tmp/python.exe /quiet InstallAllUsers=1 TargetDir='C:\Python' PrependPath=1 Include_test=0 Include_launcher=0 \
 && wineserver -w \
 && rm /tmp/python.exe \
 && wine 'C:\Python\python.exe' --version \
 && makensis -VERSION
