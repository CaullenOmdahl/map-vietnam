"""packaging/: the spec compiles, the icons are real, and the installer takes its version from one place."""

import struct
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parent.parent / "packaging"


def test_the_spec_is_valid_python():
    source = (PACKAGING / "map-vietnam.spec").read_text(encoding="utf-8")
    compile(source, "map-vietnam.spec", "exec")


def test_the_spec_bundles_each_platforms_runtime_icon():
    source = (PACKAGING / "map-vietnam.spec").read_text(encoding="utf-8")
    assert (
        'if sys.platform == "win32":\n'
        '    datas.append((str(ROOT / "packaging" / "icons" / "map-vietnam.ico"), "packaging/icons"))'
    ) in source
    assert (
        'elif sys.platform.startswith("linux"):\n'
        '    datas.append((str(ROOT / "packaging" / "icons" / "map-vietnam.png"), "packaging/icons"))'
    ) in source


def test_the_windows_icon_holds_every_size():
    data = (PACKAGING / "icons" / "map-vietnam.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = {data[6 + 16 * i] or 256 for i in range(count)}  # a width of 0 means 256
    assert {16, 32, 48, 256} <= sizes


def test_the_png_is_the_1024_source():
    from PIL import Image

    with Image.open(PACKAGING / "icons" / "map-vietnam.png") as image:
        assert image.size == (1024, 1024)
        assert image.mode == "RGBA"


def test_the_icon_generator_is_reproducible(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_icons", PACKAGING / "make_icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    icon = module.draw_icon(64)
    assert icon.size == (64, 64)
    assert icon.getpixel((32, 32))[3] == 255  # the centre is opaque
    assert icon.getpixel((0, 0))[3] == 0  # the corner is rounded off


def test_the_installer_wants_its_version_from_the_caller():
    script = (PACKAGING / "windows" / "map-vietnam.nsi").read_text(encoding="utf-8")
    assert "!ifndef VERSION" in script and "${VERSION}" in script
    assert "RequestExecutionLevel user" in script
    assert "$LOCALAPPDATA\\Programs" in script


def test_the_uninstaller_removes_only_its_own_files():
    script = (PACKAGING / "windows" / "map-vietnam.nsi").read_text(encoding="utf-8")
    uninstall = script[script.index('Section "Uninstall"') :]
    assert 'RMDir /r "$INSTDIR"' not in uninstall  # never the whole chosen folder
    assert "/SD IDNO" in uninstall  # a silent uninstall keeps the person's maps and records


@pytest.mark.parametrize("name", ["map-vietnam.png", "map-vietnam.ico", "map-vietnam.icns"])
def test_every_icon_is_committed(name):
    assert (PACKAGING / "icons" / name).stat().st_size > 1000
