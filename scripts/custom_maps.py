"""The user's own maps: files they upload, kept apart from the catalogue's old maps.

A map is one folder, data/custom_maps/<id>/ (id = "custom_" + 10 hex digits):
  source.<ext>   the uploaded file, kept as it came (a GeoTIFF or GeoPDF is built from it directly)
  work.jpg       scans only: the image the control points refer to (a PDF page is rendered into it,
                 a TIFF/PNG/JPEG is converted), at full resolution
  preview.jpg    scans only: work.jpg shrunk to at most PREVIEW_PX on its longest side, for the
                 Placement screen
  map.json       the map's entry (see below)
  control.json   scans only, once placed: the control file scripts/georef_scan.py builds from
The tiles go to web/tiles/<id>.mbtiles like any other map's, so the viewer treats it as installed.

What is in an upload decides what happens to it (sniff/ingest):
  GeoTIFF   a TIFF with a coordinate system and a position  -> kind "geotiff", built at once
  GeoPDF    a PDF with the georeferencing build_tiles reads -> kind "geopdf", built at once
  scan      a JPEG, PNG or TIFF without georeferencing, or a single-page PDF without it
            (the page is rendered)                          -> kind "scan"; state "needs_points"
            until the person places it on today's map by pairing points (save_points)
Anything else is refused with a plain-English message (CustomMapError).

map.json: {"id", "title", "year", "date", "series": "custom", "kind", "custom": true, "filename",
"bytes", "bounds" (WGS84 [w, s, e, n], or null until known), "image": {"width", "height"} for
scans, "points": [[col, row, lon, lat], ...] (col/row in work.jpg's full-resolution pixels), plus
the empty fields the library's other entries carry (region, urls, sha256, scale)}.

Import it; there is nothing to run.
"""

import contextlib
import math
import re
import secrets
import threading
import warnings
from pathlib import Path

import numpy as np
from files import delete, path_bytes, read_json, write_json
from PIL import Image, ImageOps

SERIES = "custom"
SERIES_INFO = {"name": "Your own maps", "credit": "Added by you", "scale": ""}
CREDIT = "Your own map"
ID_PATTERN = re.compile(r"custom_[0-9a-f]{10}")
MAX_UPLOAD_BYTES = 2 << 30
# Longest side of the preview the Placement screen shows (a browser handles this comfortably).
PREVIEW_PX = 4096
# The longest side a rendered PDF page gets, and the resolution it is rendered at otherwise.
RENDER_MAX_SIDE = 10_000
RENDER_DPI = 300
# A scan bigger than this many pixels would need several GB to build.
MAX_IMAGE_PIXELS = 400_000_000
# The deepest zoom a placed scan is tiled to (a guess from badly placed points could ask for far more).
MAX_ZOOM = 17
MIN_POINTS = 3
# Below this many points the build fits a plain affine transform; more allow a polynomial warp.
POLYNOMIAL_POINTS = 6
TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
# The scan decoder is ours: the size check is MAX_IMAGE_PIXELS, not Pillow's bomb guard.
Image.MAX_IMAGE_PIXELS = None

NOT_A_MAP = (
    "Map Vietnam cannot use that file. Add a GeoTIFF or GeoPDF (placed automatically), "
    "or a JPG, PNG or TIFF picture of a map (you place it yourself)."
)


class CustomMapError(ValueError):
    """An upload or request that cannot be honoured; str() is plain English for the user."""


class NoSuchMap(CustomMapError):
    """The id names no map of the user's own."""

    def __init__(self):
        super().__init__("That map is not one of your own maps (it may have been removed).")


def is_custom_id(map_id):
    """True for an id made by this module."""
    return bool(ID_PATTERN.fullmatch(str(map_id)))


def parse_year(value):
    """The year as an int, None for empty/null; CustomMapError for anything else."""
    if value in (None, ""):
        return None
    try:
        year = int(str(value).strip())
    except ValueError:
        raise CustomMapError("The year must be a number such as 1968.") from None
    if not 1000 <= year <= 2100:
        raise CustomMapError("The year must be four digits, such as 1968.")
    return year


def clean_title(value, fallback):
    """A title trimmed to one line of at most 200 characters; `fallback` when it is empty."""
    title = " ".join(str(value or "").split())[:200]
    return title or fallback


def title_from_filename(filename):
    """ "hue_1968-sheet.TIF" -> "hue 1968 sheet": a readable first guess for the title."""
    stem = Path(str(filename or "")).stem
    return " ".join(re.sub(r"[_\-]+", " ", stem).split()) or "My map"


# ---- reading an upload -----------------------------------------------------------------------
def geotiff_info(path):
    """(kind, WGS84 [w, s, e, n]) of a GeoTIFF: kind is "dem" for single-band float elevation
    rasters and "geotiff" for visual rasters; returns (None, None) when the TIFF is unplaced."""
    import rasterio
    from rasterio.errors import RasterioError
    from rasterio.transform import array_bounds
    from rasterio.warp import transform_bounds

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ds = rasterio.open(path)
        with ds:
            if ds.crs is None or ds.transform.is_identity:
                return None, None
            bounds = list(transform_bounds(ds.crs, "EPSG:4326", *array_bounds(ds.height, ds.width, ds.transform)))
            kind = "dem" if ds.count == 1 and str(ds.dtypes[0]) in ("float32", "float64") else "geotiff"
            return kind, bounds
    except RasterioError:
        return None, None
    except Exception as e:  # a coordinate system PROJ cannot work with
        raise CustomMapError(f"The GeoTIFF's coordinate system could not be used ({e}).") from None


def geotiff_bounds(path):
    """WGS84 [w, s, e, n] of a GeoTIFF, or None if the file has no coordinate system and
    position (so it is a plain picture, or not a TIFF rasterio can read)."""
    return geotiff_info(path)[1]


def is_geopdf(path):
    """True when scripts/build_tiles.py can read the PDF's georeferencing."""
    import build_tiles

    try:
        build_tiles.crs_for(build_tiles.lgi(path))
    except Exception:
        return False
    return True


def pdf_page_count(path):
    """Pages in a PDF; CustomMapError when it cannot be read at all."""
    from pypdf import PdfReader

    try:
        return len(PdfReader(path).pages)
    except Exception:
        raise CustomMapError("That PDF could not be read. It may be damaged or password protected.") from None


def render_pdf_page(path):
    """Page 1 of a PDF as an RGB image: 300 dpi, or smaller so the longest side is at most
    RENDER_MAX_SIDE pixels."""
    import pypdfium2

    try:
        doc = pypdfium2.PdfDocument(str(path))
    except Exception:
        raise CustomMapError("That PDF could not be read. It may be damaged or password protected.") from None
    try:
        page = doc[0]
        w, h = page.get_size()
        scale = min(RENDER_DPI / 72, RENDER_MAX_SIDE / max(w, h))
        return page.render(scale=scale).to_pil().convert("RGB")
    finally:
        doc.close()


def to_rgb(img):
    """An RGB copy of a picture; 16-bit and float pictures are stretched to 8 bits first."""
    img = ImageOps.exif_transpose(img)
    if img.mode.startswith(("I", "F")):
        a = np.asarray(img, dtype=np.float64)
        lo, hi = np.percentile(a[::8, ::8], [1, 99])
        a = np.clip((a - lo) / ((hi - lo) or 1), 0, 1) * 255
        return Image.fromarray(a.astype(np.uint8)).convert("RGB")
    return img.convert("RGB")


MAX_SIDE_PX = 65000  # JPEG cannot store a side longer than 65,535 pixels


def open_picture(path):
    """The first frame of a JPEG, PNG or TIFF as an RGB image; CustomMapError if Pillow cannot read it."""
    try:
        with Image.open(path) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise CustomMapError(
                    f"That picture is very large ({img.width} x {img.height} pixels). "
                    "Shrink it to under 20,000 pixels on its longest side and add it again."
                )
            if max(img.size) > MAX_SIDE_PX:
                raise CustomMapError(
                    f"That picture is too wide or tall ({img.width} x {img.height} pixels). "
                    "Shrink it to under 20,000 pixels on its longest side and add it again."
                )
            img.load()
            return to_rgb(img)
    except CustomMapError:
        raise
    except Exception:
        raise CustomMapError(NOT_A_MAP) from None


def sniff(path):
    """What an uploaded file is: ("geotiff", bounds), ("geopdf", None) or ("scan", None).
    Raises CustomMapError for anything else."""
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(b"%PDF"):
        if is_geopdf(path):
            return "geopdf", None
        pages = pdf_page_count(path)
        if pages != 1:
            raise CustomMapError(
                f"That PDF has {pages} pages and no map position. Add a PDF with one page, "
                "or a picture of the map you want."
            )
        return "scan", None
    if head.startswith(TIFF_MAGIC):
        kind, bounds = geotiff_info(path)
        return (kind, bounds) if bounds else ("scan", None)
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
        return "scan", None
    raise CustomMapError(NOT_A_MAP)


def scale_to(img, longest):
    """`img` shrunk (never enlarged) so its longest side is at most `longest` pixels."""
    if max(img.size) <= longest:
        return img
    k = longest / max(img.size)
    return img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)


# ---- placing a scan --------------------------------------------------------------------------
def affine_from_points(points):
    """Least-squares (col, row) -> (lon, lat) fit: a 3x2 matrix, or None if the points do not
    pin a map down (fewer than 3 distinct spots, or all on one line)."""
    a = np.array([[p[0], p[1], 1.0] for p in points])
    if np.linalg.matrix_rank(a, tol=1e-6 * max(1.0, np.abs(a).max())) < 3:
        return None
    coef, *_ = np.linalg.lstsq(a, np.array([[p[2], p[3]] for p in points]), rcond=None)
    # Singular in the other direction: the picture would collapse to a line on the map.
    if abs(np.linalg.det(coef[:2])) < 1e-18:
        return None
    return coef


def placed_bounds(points, width, height):
    """WGS84 [w, s, e, n] of a scan placed by `points`, from its corners; None if they are no fit."""
    coef = affine_from_points(points)
    if coef is None:
        return None
    corners = np.array([[0, 0, 1], [width, 0, 1], [width, height, 1], [0, height, 1]], float) @ coef
    return [
        float(corners[:, 0].min()),
        float(corners[:, 1].min()),
        float(corners[:, 0].max()),
        float(corners[:, 1].max()),
    ]


def clean_points(points, image):
    """The control points as a list of [col, row, lon, lat] floats, or CustomMapError saying
    in plain words what is wrong. `image` is {"width", "height"} of work.jpg."""
    if not isinstance(points, list):
        raise CustomMapError("Send the matching points as a list.")
    out = []
    for p in points:
        try:
            col, row, lon, lat = (float(v) for v in p)
        except (TypeError, ValueError):
            raise CustomMapError("Each point needs a position on the picture and a position on the map.") from None
        if not all(math.isfinite(v) for v in (col, row, lon, lat)):
            raise CustomMapError("Each point needs a position on the picture and a position on the map.")
        if not (-0.5 <= col <= image["width"] + 0.5 and -0.5 <= row <= image["height"] + 0.5):
            raise CustomMapError("A point lies outside the picture.")
        if not (-180 <= lon <= 180 and -85 <= lat <= 85):
            raise CustomMapError("A point lies outside the map's range.")
        out.append([col, row, lon, lat])
    if len(out) < MIN_POINTS:
        raise CustomMapError(f"Place at least {MIN_POINTS} matching points (6 or more is better) before saving.")
    if placed_bounds(out, image["width"], image["height"]) is None:
        raise CustomMapError("These points do not fix the map's position. Spread them out, not along one line.")
    return out


class CustomMaps:
    """The folders of the user's own maps under `root`/data/custom_maps."""

    def __init__(self, root):
        self.root = Path(root) / "data" / "custom_maps"
        self.data_root = Path(root)  # control files name their image relative to this
        # update() is read-merge-write; the build worker and request threads both call it.
        self._lock = threading.Lock()

    def folder(self, map_id):
        return self.root / map_id

    def entry_path(self, map_id):
        return self.folder(map_id) / "map.json"

    def control_path(self, map_id):
        return self.folder(map_id) / "control.json"

    def work_image(self, map_id):
        return self.folder(map_id) / "work.jpg"

    def preview_path(self, map_id):
        return self.folder(map_id) / "preview.jpg"

    def entries(self):
        """Every custom map's entry, oldest folder name first. A folder without a readable
        map.json (an upload that did not finish) is not a map."""
        if not self.root.is_dir():
            return []
        found = []
        for folder in sorted(self.root.iterdir()):
            entry = read_json(folder / "map.json") if is_custom_id(folder.name) else None
            if isinstance(entry, dict) and entry.get("id") == folder.name:
                found.append(entry)
        return found

    def get(self, map_id):
        """One entry; NoSuchMap if there is none."""
        entry = read_json(self.entry_path(map_id)) if is_custom_id(map_id) else None
        if not isinstance(entry, dict):
            raise NoSuchMap()
        return entry

    def incoming_path(self):
        """A fresh file name for an upload still arriving (kept outside the map folders)."""
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f".incoming-{secrets.token_hex(6)}.part"

    def clean_incoming(self):
        """Delete uploads cut short (the program stopped while one arrived). Call at start-up."""
        if self.root.is_dir():
            for p in self.root.glob(".incoming-*.part"):
                delete(p)

    def build_input(self, entry):
        """The file the builder reads for an entry: the upload, or a scan's work image."""
        if entry["kind"] == "scan":
            return self.work_image(entry["id"])
        return next(self.folder(entry["id"]).glob("source.*"), self.folder(entry["id"]) / "source")

    def buildable(self, entry):
        """True when the map can be built now: a scan needs its control points first."""
        return entry["kind"] != "scan" or self.control_path(entry["id"]).exists()

    # ---- adding ------------------------------------------------------------------------------
    def ingest(self, upload, filename, title, year):
        """Turn a finished upload (the file `upload`, which is moved away) into a map and return
        its entry. Raises CustomMapError, leaving nothing behind, if the file is no use."""
        year = parse_year(year)
        size = Path(upload).stat().st_size
        map_id = f"custom_{secrets.token_hex(5)}"
        folder = self.folder(map_id)
        try:
            kind, bounds = sniff(upload)
            folder.mkdir(parents=True)
            entry = {
                "id": map_id,
                "title": clean_title(title, title_from_filename(filename)),
                "year": year,
                "date": str(year) if year else "",
                "series": SERIES,
                "kind": kind,
                "custom": True,
                "filename": Path(str(filename or "")).name[:200],
                "bytes": size,
                "bounds": bounds,
                "region": "",
                "scale": "",
                "urls": [],
                "sha256": None,
                "points": [],
            }
            if kind == "scan":
                entry["image"] = self.make_work_image(upload, folder)
            Path(upload).replace(folder / f"source{extension_of(upload, kind)}")
            write_json(self.entry_path(map_id), entry)
        except BaseException:
            with contextlib.suppress(OSError):
                delete(folder)
            raise
        return entry

    def make_work_image(self, upload, folder):
        """Write work.jpg and preview.jpg of a scan; returns {"width", "height"} of work.jpg."""
        img = render_pdf_page(upload) if is_pdf(upload) else open_picture(upload)
        try:
            img.save(folder / "work.jpg", "JPEG", quality=92, subsampling=0)
            scale_to(img, PREVIEW_PX).save(folder / "preview.jpg", "JPEG", quality=88)
        except (OSError, ValueError):
            raise CustomMapError(
                "That picture could not be prepared for placing. Try saving it again as a normal JPEG or PNG."
            ) from None
        return {"width": img.width, "height": img.height}

    # ---- changing ----------------------------------------------------------------------------
    def update(self, map_id, **changes):
        """Merge `changes` into a map's entry and save it; returns the new entry."""
        with self._lock:
            entry = {**self.get(map_id), **changes}
            write_json(self.entry_path(map_id), entry)
            return entry

    def edit(self, map_id, title, year):
        """New title and year for a map."""
        entry = self.get(map_id)
        year = parse_year(year)
        return self.update(map_id, title=clean_title(title, entry["title"]), year=year, date=str(year) if year else "")

    def save_points(self, map_id, points, title, year):
        """Save a scan's control points and its control file; returns the new entry. The caller
        queues the build."""
        entry = self.get(map_id)
        if entry["kind"] != "scan":
            raise CustomMapError("This map was placed automatically from its own file; it has no points to set.")
        points = clean_points(points, entry["image"])
        year = parse_year(year)
        title = clean_title(title, entry["title"])
        bounds = placed_bounds(points, entry["image"]["width"], entry["image"]["height"])
        control = {
            "image": self.work_image(map_id).relative_to(self.data_root).as_posix(),
            "crs": "EPSG:4326",
            "gcps": points,
            "max_zoom": min(MAX_ZOOM, native_zoom_for(bounds, entry["image"]["width"])),
            "meta": {
                "title": title,
                "date": str(year) if year else "",
                "dateNote": "",
                "series": SERIES_INFO["name"],
                "source": CREDIT,
            },
        }
        if len(points) < POLYNOMIAL_POINTS:
            control["order"] = 1
        elif len(points) >= 8:
            control["tps"] = True
        write_json(self.control_path(map_id), control)
        return self.update(map_id, points=points, bounds=bounds, title=title, year=year, date=str(year) if year else "")

    def record_built(self, map_id, bounds):
        """Remember the extent the build actually covers (None keeps what the entry has)."""
        if bounds and self.entry_path(map_id).exists():
            self.update(map_id, bounds=list(bounds))

    def disk_bytes(self):
        """Bytes the stored uploads and working images take."""
        return path_bytes(self.root)


def native_zoom_for(bounds, width):
    """The zoom at which the scan's pixels are as fine as the tiles (see tiling.native_zoom)."""
    import tiling

    return tiling.native_zoom(bounds, width)


def extension_of(path, kind):
    """The file extension the stored upload gets, from what it is (not from the name it came with)."""
    if kind in ("geotiff", "dem"):
        return ".tif"
    if kind == "geopdf" or is_pdf(path):
        return ".pdf"
    with open(path, "rb") as f:
        head = f.read(8)
    return ".tif" if head.startswith(TIFF_MAGIC) else ".png" if head.startswith(b"\x89PNG") else ".jpg"


def is_pdf(path):
    """True when the file starts like a PDF."""
    with open(path, "rb") as f:
        return f.read(4) == b"%PDF"
