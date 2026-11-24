#!/usr/bin/env bash
# Builds the desktop app on this machine, in Docker, and puts the files to publish in dist/release/.
#
#   packaging/build.sh              both: the Windows installer and the Linux download
#   packaging/build.sh windows      Map-Vietnam-<version>-Setup.exe
#   packaging/build.sh linux        Map-Vietnam-<version>-linux-x86_64.tar.gz
#
# Run it from anywhere on a Linux machine with Docker; nothing else needs installing. The first run
# builds the two build images (a few minutes, about 3 GB); later runs reuse them.
#
#   * Windows is built under Wine (packaging/windows/Dockerfile): the Windows build of Python makes
#     the app with PyInstaller, exactly as on a Windows PC, and NSIS (which runs natively on Linux)
#     makes the installer from packaging/windows/map-vietnam.nsi.
#   * Linux is built on Ubuntu 22.04 (packaging/linux/Dockerfile), so it runs on older systems too.
#
# Each build is smoke-tested (packaging/smoke_test.py): the app starts, serves the viewer and the
# map library, runs a child job and quits. The Windows installer is also installed and uninstalled,
# silently, and the installed copy tested. Under Wine the window itself cannot be checked (Wine has no WebView2),
# so that one check runs only for Linux; open the app on a real Windows PC before publishing.
#
# The version is [project].version in pyproject.toml. To publish, with the GitHub CLI:
#   gh release create v<version> dist/release/* --generate-notes
#
# The Mac app has to be built on a Mac: pyinstaller packaging/map-vietnam.spec (see CONTRIBUTING.md).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/dist/release"
PYINSTALLER_VERSION="6.22.3"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
# Windows file properties want four numbers: 0.1.0 -> 0.1.0.0 (anything after the numbers is dropped).
VIVERSION="$(echo "$VERSION" | grep -oE '^[0-9]+(\.[0-9]+)*' | awk -F. '{print $1"."($2+0)"."($3+0)"."($4+0)}')"
TARGETS=("${@:-windows linux}")
read -r -a TARGETS <<<"${TARGETS[*]}"

# The source goes into each container as a copy without the git history, the virtual environment,
# earlier builds or downloaded data, so a build never sees anything that is not in the repository.
COPY_SOURCE='mkdir -p /tmp/build && tar -C /src --exclude=./.git --exclude=./.venv --exclude=./build --exclude=./dist \
  --exclude=./data/raw --exclude=./data/work --exclude=./data/tiles --exclude=./data/map_vietnam.sqlite \
  --exclude="*/__pycache__" -cf - . | tar -xf - -C /tmp/build && cd /tmp/build'

build_windows() {
  echo "== Windows $VERSION (under Wine)"
  docker build -q -t map-vietnam-build-windows "$ROOT/packaging/windows" >/dev/null
  docker run --rm -v "$ROOT":/src:ro -v "$OUT":/out map-vietnam-build-windows bash -c "
    set -euo pipefail
    $COPY_SOURCE
    wine python -m pip install -q --no-warn-script-location -r requirements.txt pyinstaller==$PYINSTALLER_VERSION
    wine python -m PyInstaller packaging/map-vietnam.spec --noconfirm --log-level WARN
    wine python packaging/smoke_test.py --no-window-check 'dist/Map Vietnam/Map Vietnam.exe'
    mkdir -p dist/installer
    makensis -V2 -DVERSION=$VERSION -DVIVERSION=$VIVERSION packaging/windows/map-vietnam.nsi
    setup='Map-Vietnam-$VERSION-Setup.exe'
    wine \"dist/installer/\$setup\" /S '/D=C:\\installed' && wineserver -w
    wine python packaging/smoke_test.py --no-window-check 'C:\\installed\\Map Vietnam.exe'
    wine 'C:\\installed\\Uninstall.exe' /S && wineserver -w && sleep 2
    if [ -e \"\$WINEPREFIX/drive_c/installed/Map Vietnam.exe\" ]; then echo 'The uninstaller left the app behind' >&2; exit 1; fi
    cp \"dist/installer/\$setup\" /out/
    chown $(id -u):$(id -g) \"/out/\$setup\"
  "
}

build_linux() {
  echo "== Linux $VERSION"
  docker build -q -t map-vietnam-build-linux "$ROOT/packaging/linux" >/dev/null
  docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$ROOT":/src:ro -v "$OUT":/out \
    map-vietnam-build-linux bash -c "
    set -euo pipefail
    $COPY_SOURCE
    uv venv -q --seed --python 3.12 /tmp/venv && . /tmp/venv/bin/activate
    pip install -q -r requirements.txt -r requirements-qt.txt pyinstaller==$PYINSTALLER_VERSION
    pyinstaller packaging/map-vietnam.spec --noconfirm --log-level WARN
    xvfb-run -a python packaging/smoke_test.py 'dist/Map Vietnam/Map Vietnam'
    tar -czf '/out/Map-Vietnam-$VERSION-linux-x86_64.tar.gz' -C dist 'Map Vietnam'
  "
}

mkdir -p "$OUT"
for target in "${TARGETS[@]}"; do
  case "$target" in
    windows) build_windows ;;
    linux) build_linux ;;
    *) echo "Unknown target '$target' (use windows or linux)" >&2; exit 2 ;;
  esac
done
echo "== Done:"
ls -lh "$OUT"
