#!/usr/bin/env bash
# Builds the desktop app on this machine, in Docker, and puts the files to publish in dist/release/.
#
#   packaging/build.sh                      the Windows installer and the Linux download
#   packaging/build.sh windows              Map-Vietnam-<version>-Setup.exe
#   packaging/build.sh linux                Map-Vietnam-<version>-linux-x86_64.tar.gz + .deb, .rpm, .pkg.tar.zst
#   MAC_HOST=my-mac packaging/build.sh mac  Map-Vietnam-<version>-macos-<arch>.dmg, built over SSH
#   MAC_HOST=my-mac packaging/build.sh all  all three
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
# The Mac app has to be built on a Mac. With MAC_HOST set to a Mac this machine can reach with
# `ssh MAC_HOST` (no password prompt) that has uv (https://docs.astral.sh/uv/) and macOS 15 or newer,
# "mac" copies the source there, builds and smoke-tests the .app in a temporary folder, packs it in a
# disk image (with an Applications shortcut to drag it onto), tests it again from the mounted image
# and brings the .dmg back. It is not signed or notarized (docs/troubleshooting.md tells users what to do).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/dist/release"
PYINSTALLER_VERSION="6.22.3"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
# Windows file properties want four numbers: 0.1.0 -> 0.1.0.0 (anything after the numbers is dropped).
VIVERSION="$(echo "$VERSION" | grep -oE '^[0-9]+(\.[0-9]+)*' | awk -F. '{print $1"."($2+0)"."($3+0)"."($4+0)}')"
TARGETS=("${@:-windows linux}")
read -r -a TARGETS <<<"${TARGETS[*]}"
[ "${TARGETS[*]}" = "all" ] && TARGETS=(windows linux mac)

# The source goes into each container as a copy without the git history, the virtual environment,
# earlier builds or downloaded data, so a build never sees anything that is not in the repository.
EXCLUDES=(--exclude=./.git --exclude=./.venv --exclude=./build --exclude=./dist --exclude=./data/raw
  --exclude=./data/work --exclude=./data/tiles --exclude=./data/map_vietnam.sqlite --exclude="*/__pycache__")
COPY_SOURCE="mkdir -p /tmp/build && tar -C /src $(printf '%q ' "${EXCLUDES[@]}")-cf - . | tar -xf - -C /tmp/build && cd /tmp/build"

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
    python -c '
from pathlib import Path
from PIL import Image
out = Path(\"dist/icons\")
out.mkdir(parents=True, exist_ok=True)
with Image.open(\"packaging/icons/map-vietnam.png\") as img:
    for size in (256, 512):
        img.resize((size, size), Image.LANCZOS).save(out / f\"map-vietnam-{size}.png\")
'
    VERSION='$VERSION' nfpm package --config packaging/linux/nfpm.yaml --packager deb --target '/out/map-vietnam_${VERSION}_amd64.deb'
    VERSION='$VERSION' nfpm package --config packaging/linux/nfpm.yaml --packager rpm --target '/out/map-vietnam-${VERSION}-1.x86_64.rpm'
    VERSION='$VERSION' nfpm package --config packaging/linux/nfpm.yaml --packager archlinux --target '/out/map-vietnam-${VERSION}-1-x86_64.pkg.tar.zst'
  "
}

build_mac() {
  if [ -z "${MAC_HOST:-}" ]; then
    echo "Set MAC_HOST to a Mac reachable with ssh, for example: MAC_HOST=my-mac packaging/build.sh mac" >&2
    exit 2
  fi
  echo "== macOS $VERSION (on $MAC_HOST)"
  local remote arch dmg
  remote="$(ssh "$MAC_HOST" 'mktemp -d /tmp/map-vietnam-build.XXXXXX')"
  # shellcheck disable=SC2064  # expand now: the folder name is known here
  trap "ssh '$MAC_HOST' 'hdiutil detach -quiet $remote/mnt 2>/dev/null; rm -rf $remote' >/dev/null 2>&1 || true" RETURN
  tar -C "$ROOT" "${EXCLUDES[@]}" -cf - . | ssh "$MAC_HOST" "tar -xf - -C $remote"
  arch="$(ssh "$MAC_HOST" 'uname -m')"
  dmg="Map-Vietnam-$VERSION-macos-$arch.dmg"
  ssh "$MAC_HOST" "set -euo pipefail; export PATH=/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:\$PATH
    cd $remote
    command -v uv >/dev/null || { echo 'uv is needed on the Mac: brew install uv' >&2; exit 1; }
    uv venv -q --python 3.12 .build-venv
    VIRTUAL_ENV=.build-venv uv pip install -q -r requirements.txt pyinstaller==$PYINSTALLER_VERSION
    .build-venv/bin/pyinstaller packaging/map-vietnam.spec --noconfirm --log-level WARN
    codesign --verify --deep --strict 'dist/Map Vietnam.app'
    .build-venv/bin/python packaging/smoke_test.py 'dist/Map Vietnam.app/Contents/MacOS/Map Vietnam'
    # A disk image people open and drag the app from onto the Applications shortcut beside it.
    mkdir dmg-root
    ditto 'dist/Map Vietnam.app' 'dmg-root/Map Vietnam.app'
    ln -s /Applications dmg-root/Applications
    hdiutil create -quiet -volname 'Map Vietnam' -srcfolder dmg-root -fs HFS+ -format UDZO -ov '$dmg'
    hdiutil verify -quiet '$dmg'
    # Test the app as it comes off the image, the way a downloader first meets it.
    mkdir mnt
    hdiutil attach -quiet -nobrowse -readonly -mountpoint mnt '$dmg'
    codesign --verify --deep --strict 'mnt/Map Vietnam.app'
    .build-venv/bin/python packaging/smoke_test.py 'mnt/Map Vietnam.app/Contents/MacOS/Map Vietnam'
    hdiutil detach -quiet mnt"
  scp -q "$MAC_HOST:$remote/$dmg" "$OUT/$dmg"
}

mkdir -p "$OUT"
for target in "${TARGETS[@]}"; do
  case "$target" in
    windows) build_windows ;;
    linux) build_linux ;;
    mac) build_mac ;;
    *) echo "Unknown target '$target' (use windows, linux, mac or all)" >&2; exit 2 ;;
  esac
done
echo "== Done:"
ls -lh "$OUT"
