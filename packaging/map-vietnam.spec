# PyInstaller spec: Map Vietnam as a one-folder desktop app, built from scripts/desktop.py.
#
#     pyinstaller packaging/map-vietnam.spec --noconfirm       (from the repository root)
#
# makes dist/Map Vietnam/ on Windows and Linux and dist/Map Vietnam.app on macOS. The app is
# windowed (no console) on Windows and macOS. Its version is [project].version in pyproject.toml,
# the one place it is written.
#
# What goes in, and why:
#   * the project's scripts, twice. Imported ones are compiled into the executable. Child jobs
#     (the map and record downloads) run as `<exe> --run-script X.py`, which loads scripts/X.py by
#     path (paths.run_script_from_argv), so the .py files also ship as data under scripts/, and
#     everything those scripts import is listed here as hidden imports, found by reading them;
#   * the viewer (web/app) and the data that ships with it (data/sources, data/scans, data/maps.json);
#   * what the native libraries read at run time: GDAL's and PROJ's data folders, pypdfium2's
#     library, certifi's certificate bundle (records are fetched over HTTPS);
#   * the licence files of every bundled Python package, under licenses/ (see licence_files below),
#     and on Linux the LGPL v3 and GPL v3 texts that Qt's licence refers to. NOTICE lists the rest.
#
# paths.py finds all of this under sys._MEIPASS (APP_ROOT) and keeps what the app writes in the
# per-user data folder, so the installed folder is never written to.

import ast
import os
import re
import subprocess
import sys
import sysconfig
import tomllib
from importlib import metadata
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # SPECPATH is the folder of this spec file
SCRIPTS = ROOT / "scripts"
NAME = "Map Vietnam"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def script_modules():
    """Every module the project's scripts import, by reading them. The scripts that run through
    --run-script are loaded by path at run time, so PyInstaller's own import tracing never sees
    their imports; naming them here makes it bundle them."""
    names = set()
    for source in [*SCRIPTS.glob("*.py"), *(SCRIPTS / "ingest").glob("*.py")]:
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module)
    # The windowing toolkits are per platform: pywebview picks its own backend (see below).
    return sorted(n for n in names if n not in {"webview", "msvcrt", "fcntl"})


ROOT_DISTRIBUTIONS = [
    "rasterio", "pypdf", "pypdfium2", "pillow", "numpy", "morecantile", "pyproj", "certifi", "pywebview",
    "pythonnet", "clr_loader", "pyobjc-core", "pyobjc-framework-Cocoa", "pyobjc-framework-WebKit",
    "QtPy", "PySide6-Essentials", "PySide6-Addons", "PySide6-WebEngine", "shiboken6",
]  # fmt: skip  (what the app imports; the ones not installed on this platform are skipped)
LICENCE_FILE = re.compile(r"licen[cs]e|copying|notice|authors", re.IGNORECASE)


def licence_files():
    """The licence, notice and authors files of the packages above and of everything they need, as
    PyInstaller data: (source file, folder inside the app). They land in licenses/<package>/ so that
    the installers carry the licences the packages ask to be kept with their binaries."""
    from packaging.requirements import Requirement

    seen, queue, found = set(), list(ROOT_DISTRIBUTIONS), []
    while queue:
        name = re.sub(r"[-_.]+", "-", queue.pop()).lower()
        if name in seen:
            continue
        seen.add(name)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for line in dist.requires or []:
            req = Requirement(line)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                queue.append(req.name)
        for file in dist.files or []:
            parts = file.parts
            if parts[0].endswith(".dist-info") and LICENCE_FILE.search("/".join(parts[1:])):
                inner = "/".join(parts[1:-1]).removeprefix("licenses").strip("/")  # keeps a LICENSES/ subfolder
                found.append((str(dist.locate_file(file)), "/".join(["licenses", name, inner]).rstrip("/")))
    for folder in (Path(sys.base_prefix), Path(sysconfig.get_paths()["stdlib"])):  # CPython's, where it ships
        if (folder / "LICENSE.txt").is_file():
            found.append((str(folder / "LICENSE.txt"), "licenses/python"))
            break
    return found


hiddenimports = script_modules()
hiddenimports += ["webview", "certifi", "ssl", "sqlite3", "zipfile", "csv"]
# rasterio's compiled modules import each other by name, which tracing cannot see.
hiddenimports += collect_submodules("rasterio") + collect_submodules("pyproj") + collect_submodules("morecantile")
if sys.platform == "win32":
    hiddenimports += ["msvcrt"]  # files.py's lock
    hiddenimports += collect_submodules("webview.platforms.winforms") + ["clr", "clr_loader", "pythonnet"]  # clr is the module pythonnet installs
elif sys.platform == "darwin":
    hiddenimports += collect_submodules("webview.platforms.cocoa") + [
        "objc", "Foundation", "AppKit", "WebKit", "PyObjCTools.AppHelper",
    ]  # fmt: skip
else:
    hiddenimports += ["fcntl"]
    # The Qt window is optional on Linux (requirements-qt.txt); with it, bundle that backend.
    # PySide6 (LGPL v3) is the only Qt binding this app may ship: PyQt is GPL v3 (see excludes).
    try:
        import PySide6  # noqa: F401
        import qtpy  # noqa: F401

        hiddenimports += collect_submodules("webview.platforms.qt") + [
            "qtpy", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtNetwork",
            "PySide6.QtWebChannel", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        ]  # fmt: skip
        QT_LICENCES = [
            (str(ROOT / "packaging" / "licenses" / "LGPL-3.0.txt"), "licenses/qt"),
            (str(ROOT / "packaging" / "licenses" / "GPL-3.0.txt"), "licenses/qt"),  # the LGPL is written on top of it
        ]
    except ImportError:
        pass

datas = [
    (str(ROOT / "web" / "app"), "web/app"),
    (str(ROOT / "data" / "sources"), "data/sources"),
    (str(ROOT / "data" / "scans"), "data/scans"),
    (str(ROOT / "data" / "maps.json"), "data"),
    (str(SCRIPTS), "scripts"),  # the sources run_script_from_argv loads by path
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "NOTICE"), "."),
    (str(ROOT / "packaging" / "icons" / "map-vietnam.png"), "packaging/icons"),  # the Linux window's icon
]
LICENCES = licence_files() + globals().get("QT_LICENCES", [])
missing = {"rasterio", "numpy", "pillow", "pypdf", "pypdfium2", "pyproj", "certifi", "pywebview"} - {
    dest.split("/")[1] for _, dest in LICENCES
}
if missing:  # a package without its licence file must fail the build, not ship
    raise SystemExit(f"No licence files found for: {', '.join(sorted(missing))}")
datas += LICENCES
datas += collect_data_files("certifi")
datas += collect_data_files("rasterio")  # gdal_data, proj_data
datas += collect_data_files("pyproj")  # proj_dir: proj.db and grids
datas += collect_data_files("morecantile")  # the tile matrix set definitions
datas += collect_data_files("webview", includes=["js/*"])
binaries = collect_dynamic_libs("pypdfium2_raw") + collect_dynamic_libs("pypdfium2")

# The one-folder copy is already large; leave out what the app never imports.
excludes = ["webview.platforms.gtk"] if sys.platform.startswith("linux") else []  # desktop.py asks for Qt
excludes += ["tkinter", "matplotlib", "pytest", "IPython", "scipy", "pandas"]
# Never bundle a GPL Qt binding, even if one is installed in the build environment: PyQt is GPL v3
# (or commercial), which cannot ship inside this Apache-2.0 app. PySide6 is the one to use.
excludes += ["PyQt5", "PyQt6", "PyQt6_Qt6", "PySide2"]
# The web view needs Qt's core, gui, widgets, network, web channel and web engine; the other
# PySide6 modules (QML, Quick, 3D, charts, multimedia) are only dead weight.
excludes += [f"PySide6.{m}" for m in ("QtQml", "QtQuick", "QtQuickWidgets", "QtQuick3D", "QtCharts", "QtDataVisualization",
                                       "Qt3DCore", "Qt3DRender", "Qt3DInput", "Qt3DLogic", "Qt3DAnimation", "Qt3DExtras",
                                       "QtMultimedia", "QtMultimediaWidgets", "QtPositioning", "QtSensors", "QtSql",
                                       "QtTest", "QtDesigner", "QtHelp", "QtSvg", "QtSvgWidgets", "QtPdf", "QtPdfWidgets",
                                       "QtWebEngineQuick", "QtWebView", "QtWebSockets", "QtTextToSpeech", "QtSerialPort",
                                       "QtRemoteObjects", "QtScxml", "QtStateMachine", "QtLocation", "QtGraphs")]  # fmt: skip

ICON = {
    "win32": ROOT / "packaging" / "icons" / "map-vietnam.ico",
    "darwin": ROOT / "packaging" / "icons" / "map-vietnam.icns",
}.get(sys.platform)

a = Analysis(
    [str(SCRIPTS / "desktop.py")],
    pathex=[str(SCRIPTS), str(SCRIPTS / "ingest")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
# PySide6-Addons also carries Qt Virtual Keyboard, which Qt licenses under GPL v3 or commercial only,
# not the LGPL. Nothing here uses it, so drop its libraries and input-context plugin from the bundle.
GPL_ONLY_QT = ("virtualkeyboard",)
a.binaries = [b for b in a.binaries if not any(w in b[0].lower() for w in GPL_ONLY_QT)]
a.datas = [d for d in a.datas if not any(w in d[0].lower() for w in GPL_ONLY_QT)]
# Linux: leave out the system libraries the user's own graphics driver and desktop load alongside
# the app. Bundled from the build image (Ubuntu 22.04), they shadow the system's newer copies, and
# a newer Mesa driver then fails to load (it needs a newer libstdc++), EGL never starts and Qt
# WebEngine aborts. Every distribution with glibc 2.35 or newer has these. The wheels' own,
# hash-named copies (libpng16-<hash>.so...) are private to their packages and stay.
HOST_LIBS = (
    "libstdc++.so.6", "libgcc_s.so.1", "libgbm.so.1", "libdrm.so.2", "libEGL.so.1", "libGL.so.1",
    "libGLX.so.0", "libGLdispatch.so.0", "libOpenGL.so.0", "libglib-2.0.so.0", "libgio-2.0.so.0",
    "libgobject-2.0.so.0", "libgmodule-2.0.so.0", "libdbus-1.so.3", "libfontconfig.so.1",
    "libfreetype.so.6", "libexpat.so.1", "libffi.so.8", "libpcre2-8.so.0", "libpcre.so.3",
    "libmount.so.1", "libblkid.so.1", "libselinux.so.1", "libuuid.so.1",
)  # fmt: skip
if sys.platform.startswith("linux"):
    a.binaries = [b for b in a.binaries if Path(b[0]).name not in HOST_LIBS]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # one-folder: the libraries stay beside the executable
    name=NAME,
    console=False,  # no console window; the child jobs and --no-window runs still print to a pipe or file
    icon=str(ICON) if ICON else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=str(ICON),
        bundle_identifier="io.github.caullenomdahl.mapvietnam",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "15.0",  # the pinned rasterio wheels need macOS 15
        },
    )


def own_libraries_first(lib_root):
    """macOS only. The rasterio and pyproj wheels each bring their own PROJ, under the same file name
    (libproj.25.*.dylib) and install name but built differently: rasterio's has its symbols renamed.
    PyInstaller links every binary to one copy by name, and macOS' loader also reuses an already
    loaded library with the same install name, so one package always gets the other's PROJ and fails
    to load ("Symbol not found: _proj_context_create"). The Linux and Windows wheels give their copies
    distinct names, so this is not needed there. Here every package library that differs from the
    shared copy of the same name is renamed (<package>-<name>), given that as its install name, and
    the package's binaries are pointed at it; changed files are re-signed ad hoc, as PyInstaller
    signs them."""
    for own_dir in lib_root.glob("*/__dot__dylibs"):
        package = own_dir.parent
        renamed = {}
        for lib in sorted(own_dir.iterdir()):
            shared = lib_root / lib.name
            if lib.is_symlink() or not shared.exists() or shared.resolve() == lib.resolve():
                continue
            unique = f"{package.name}-{lib.name}"
            lib.rename(own_dir / unique)
            subprocess.run(["install_name_tool", "-id", f"@rpath/{unique}", str(own_dir / unique)], check=True, capture_output=True)
            renamed[lib.name] = unique
        if not renamed:
            continue
        for binary in [*package.rglob("*.so"), *package.rglob("*.dylib")]:
            if binary.is_symlink():
                continue
            listing = subprocess.run(["otool", "-L", str(binary)], capture_output=True, text=True, check=True).stdout
            changes = []
            for line in listing.splitlines()[1:]:
                dep = line.strip().split(" (")[0]
                name = Path(dep).name
                if name in renamed and not dep.endswith(f"/{renamed[name]}"):
                    relative = Path(os.path.relpath(own_dir, binary.parent)) / renamed[name]
                    changes += ["-change", dep, f"@loader_path/{relative.as_posix()}"]
            if changes:
                subprocess.run(["install_name_tool", *changes, str(binary)], check=True, capture_output=True)
            if changes or binary.parent == own_dir:
                subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True, capture_output=True)


if sys.platform == "darwin":
    own_libraries_first(Path(DISTPATH) / NAME / "_internal")
    app_path = Path(DISTPATH) / f"{NAME}.app"
    own_libraries_first(app_path / "Contents" / "Frameworks")
    # The changed files broke the bundle's ad-hoc seal; seal it again.
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app_path)], check=True, capture_output=True)
