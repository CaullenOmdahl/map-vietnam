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
#     library, certifi's certificate bundle (records are fetched over HTTPS).
#
# paths.py finds all of this under sys._MEIPASS (APP_ROOT) and keeps what the app writes in the
# per-user data folder, so the installed folder is never written to.

import ast
import sys
import tomllib
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


hiddenimports = script_modules()
hiddenimports += ["webview", "certifi", "ssl", "sqlite3", "zipfile", "csv"]
# rasterio's compiled modules import each other by name, which tracing cannot see.
hiddenimports += collect_submodules("rasterio") + collect_submodules("pyproj") + collect_submodules("morecantile")
if sys.platform == "win32":
    hiddenimports += ["msvcrt"]  # files.py's lock
    hiddenimports += collect_submodules("webview.platforms.winforms") + ["clr", "clr_loader", "pythonnet"]  # clr is the module pythonnet installs
elif sys.platform == "darwin":
    hiddenimports += collect_submodules("webview.platforms.cocoa") + ["objc", "Foundation", "AppKit", "WebKit"]
else:
    hiddenimports += ["fcntl"]
    # The Qt window is optional on Linux (requirements-qt.txt); with it, bundle that backend.
    try:
        import qtpy  # noqa: F401

        hiddenimports += collect_submodules("webview.platforms.qt") + ["qtpy", "PyQt6.QtWebEngineCore"]
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
]
datas += collect_data_files("certifi")
datas += collect_data_files("rasterio")  # gdal_data, proj_data
datas += collect_data_files("pyproj")  # proj_dir: proj.db and grids
datas += collect_data_files("morecantile")  # the tile matrix set definitions
datas += collect_data_files("webview", includes=["js/*"])
binaries = collect_dynamic_libs("pypdfium2_raw") + collect_dynamic_libs("pypdfium2")

# The one-folder copy is already large; leave out what the app never imports.
excludes = ["webview.platforms.gtk"] if sys.platform.startswith("linux") else []  # desktop.py asks for Qt
excludes += ["tkinter", "matplotlib", "pytest", "IPython", "scipy", "pandas", "PyQt6.QtQml", "PyQt6.QtQuick"]

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
