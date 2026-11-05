"""Turn one NGA GeoPDF map sheet into a single MBTiles file of web tiles.

Works for every GeoPDF series in the map library: the AMS L7014 family at 1:50,000 and the Joint
Operations Graphics (JOG 1501) at 1:250,000. Each sheet carries an OGC "LGIDict" with its
projection (transverse Mercator on UTM zone 48 or 49) and datum, a neatline, and the page ->
grid georeferencing in one of two forms (a sheet with an inset, such as an island drawn apart,
has one LGIDict per map on the page; the main map is the one whose neatline is largest):
  - /Registration: points [page x, page y, easting, northing]; we fit an affine transform to them;
  - /CTM only (some reissued sheets, LGIDict version 2): the affine transform itself, as six
    numbers [a b c d e f] with easting = a x + c y + e and northing = b x + d y + f.
We render the page with pdfium (the pypdfium2 wheel, so neither poppler nor GDAL has to be
installed), make everything outside the map frame transparent, and hand the image to
tiling.write_tiles. The edition and date printed in the PDF's metadata go into the MBTiles
metadata so the viewer can say how old the map is.

The map frame: the neatline is not always the printed frame. On the 1:250,000 sheets it runs
around the whole scanned map image, so a strip of margin and grid labels lies inside it along two
sides; on some reissued 1:50,000 sheets it is the whole page, legend and all. So the sheet is
also cut at its frame: the corners the PDF records in its metadata (ll_long, ll_lat, ur_long,
ur_lat, on the datum of its georeferencing), or, for a PDF without them, --frame (the corners
from the catalogue) on --frame-datum. A side of the frame cuts only where the neatline reaches
past it along nearly its whole length: that is margin. Where the neatline reaches past only in
part, it is a real extension of the map (a peninsula drawn beyond the frame) and is kept.

Resolution: render at 250 dpi for 1:50,000 sheets (about 5 m a pixel, so z15 is native) and at
300 dpi for 1:250,000 sheets (their scans are 300 dpi, about 21 m a pixel: z13). The deepest zoom
follows from the render unless --max-zoom says otherwise. Nothing is left in data/work: the page
is rendered in memory, and the only output is the .mbtiles file (written as .partial, then renamed).

Usage: .venv/bin/python scripts/build_tiles.py SHEET.pdf [OUT.mbtiles] [--dpi 250] [--max-zoom Z]
                                                [--frame W,S,E,N [--frame-datum ING-A]] [--progress]
       (OUT defaults to web/tiles/<sheet id>.mbtiles; --progress prints "progress 0.42" lines)
"""

import argparse
import json
import math
from pathlib import Path

import l7014_meta
import numpy as np
import pypdfium2
from datums import BY_DMA_CODE
from pypdf import PdfReader
from rasterio.crs import CRS
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.warp import transform as warp_points
from tiling import write_tiles

ROOT = Path(__file__).resolve().parent.parent
TILES = ROOT / "web" / "tiles"
# Default render resolution: right for 1:50,000 sheets (see the module docstring).
DPI = 250
PT_PER_INCH = 72.0  # PDF user-space units per inch
# Share of a build's time spent rendering the page (measured: 23 s of 59 s for one sheet); the
# rest is warping and encoding tiles. Only used to make the progress bar move evenly.
RENDER_SHARE = 0.4
# LGIDict projection types this builder understands: TC = transverse Mercator, UT = UTM. Both
# usually come with an explicit central meridian, scale factor and false origin; a UT projection
# may instead name only its /Zone (and /Hemisphere), from which the standard UTM values follow.
TRANSVERSE_MERCATOR = {"TC", "UT"}
UTM_SCALE = 0.9996
UTM_FALSE_EASTING = 500_000
UTM_FALSE_NORTHING_SOUTH = 10_000_000
# Points per frame edge when it is drawn on the sheet's grid: a parallel is a curve in transverse
# Mercator (about 20 m off a straight line across a 1:250,000 sheet), so edges are densified.
FRAME_EDGE_POINTS = 32
# Margin test (see the module docstring): the neatline is probed this far beyond each side of the
# frame, as a share of the frame's size (1 % is 270 m on a 1:50,000 sheet, well past registration
# error, and well inside the 3 % strip of margin on a 1:250,000 one) ...
EDGE_PROBE = 0.01
# ... and the side cuts if at least this share of the probes lie inside the neatline.
EDGE_CUT_SHARE = 0.9
# The PDF metadata fields that give the sheet's own frame.
CORNER_FIELDS = ("ll_long", "ll_lat", "ur_long", "ur_lat")


def layer_id_for(pdf):
    """The map id a GeoPDF file name stands for: "L701461292_geo.pdf" -> "L701461292"."""
    return pdf.name.removesuffix(".pdf").removesuffix("_geo")


def lgi(pdf):
    """The crop box and the LGIDict georeferencing of the first page of a GeoPDF.

    box: (x0, y0, x1, y1) of the crop box (the part pdfium renders) in page points;
    reg: [[page_x_pt, page_y_pt, easting_m, northing_m], ...], page y measured up from the bottom
         (the sheet's registration points, or the crop box corners mapped through its /CTM);
    georef: "registration" or "ctm", whichever reg came from;
    neatline: flat [x0, y0, x1, y1, ...] in page points (the crop box if the sheet has none);
    cm/k/fe/fn: transverse Mercator parameters; datum: the DMA datum code (e.g. "ING-A", "WGE").
    """
    page = PdfReader(pdf).pages[0]
    if "/LGIDict" not in page:
        raise ValueError(f"{pdf.name} has no georeferencing (no LGIDict): it is not an NGA GeoPDF")
    d = page["/LGIDict"]
    d = max((x.get_object() for x in d), key=neatline_area) if isinstance(d, list) else d.get_object()
    box = page.cropbox
    box = (float(box.left), float(box.bottom), float(box.right), float(box.top))
    x0, y0, x1, y1 = box
    if "/Registration" in d:
        reg, georef = [[float(v) for v in p] for p in d["/Registration"]], "registration"
    elif "/CTM" in d:
        a, b, c, dd, e, f = (float(v) for v in d["/CTM"])
        reg = [[x, y, a * x + c * y + e, b * x + dd * y + f] for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        georef = "ctm"
    else:
        raise ValueError(f"{pdf.name} has an LGIDict without registration points or a /CTM transform")
    neatline = [float(v) for v in d["/Neatline"]] if "/Neatline" in d else [x0, y0, x1, y0, x1, y1, x0, y1]
    return {"box": box, "reg": reg, "georef": georef, "neatline": neatline, **projection(pdf, d["/Projection"])}


def neatline_area(d):
    """Page area (points squared) inside an LGIDict's neatline; 0 without one."""
    if "/Neatline" not in d:
        return 0.0
    v = [float(x) for x in d["/Neatline"]]
    xs, ys = v[0::2], v[1::2]
    return abs(sum(xs[i] * ys[i - 1] - xs[i - 1] * ys[i] for i in range(len(xs)))) / 2


def own_corners(pdf):
    """The sheet's frame [w, s, e, n] as its PDF metadata records it, or None if it does not."""
    fields = {**l7014_meta.xmp(pdf), **l7014_meta.info(pdf)}
    try:
        w, s, e, n = (float(fields[k]) for k in CORNER_FIELDS)
    except (KeyError, ValueError):
        return None
    return [w, s, e, n] if w < e and s < n else None


def projection(pdf, proj):
    """{"cm", "k", "fe", "fn", "datum"} of an LGIDict /Projection (transverse Mercator only)."""
    kind = str(proj.get("/ProjectionType", "TC"))
    if kind not in TRANSVERSE_MERCATOR:
        raise ValueError(f"{pdf.name} uses projection type {kind!r}; only transverse Mercator sheets are supported")
    datum = str(proj["/Datum"])
    if "/CentralMeridian" in proj:
        return {
            "cm": float(proj["/CentralMeridian"]),
            "k": float(proj["/ScaleFactor"]),
            "fe": float(proj["/FalseEasting"]),
            "fn": float(proj["/FalseNorthing"]),
            "datum": datum,
        }
    if kind == "UT" and "/Zone" in proj:
        south = str(proj.get("/Hemisphere", "N")).upper().startswith("S")
        return {
            "cm": int(proj["/Zone"]) * 6 - 183,
            "k": UTM_SCALE,
            "fe": UTM_FALSE_EASTING,
            "fn": UTM_FALSE_NORTHING_SOUTH if south else 0,
            "datum": datum,
        }
    raise ValueError(f"{pdf.name}'s projection has neither a central meridian nor a UTM zone")


def datum_of(g):
    """The PROJ datum fragment (datums.py) for the sheet's datum code; ValueError if unknown."""
    datum = BY_DMA_CODE.get(g["datum"])
    if datum is None:
        raise ValueError(f"unknown datum code {g['datum']!r} in the sheet's georeferencing")
    return datum


def crs_for(g):
    """The sheet's transverse Mercator CRS on its own datum (Indian 1960 or WGS84 here)."""
    return CRS.from_proj4(
        f"+proj=tmerc +lat_0=0 +lon_0={g['cm']} +k={g['k']} +x_0={g['fe']} +y_0={g['fn']} {datum_of(g)} +units=m +no_defs"
    )


def render_size(pdf, dpi=DPI):
    """(width, height) in pixels of page 1 rendered at dpi: the crop box scaled to pixels and
    rounded up, as poppler's pdftoppm sizes it. The box comes from pypdf in double precision;
    pdfium works in single precision, so a page exactly 7240 px tall can come out 7241."""
    box = PdfReader(pdf).pages[0].cropbox
    s = dpi / PT_PER_INCH
    # The 1e-6 px slack absorbs floating-point noise in the product before rounding up.
    return math.ceil(float(box.width) * s - 1e-6), math.ceil(float(box.height) * s - 1e-6)


def render_page(pdf, dpi=DPI):
    """Page 1 of pdf as an RGB (h, w, 3) uint8 array of render_size(pdf, dpi)."""
    doc = pypdfium2.PdfDocument(str(pdf))
    try:
        image = doc[0].render(scale=dpi / PT_PER_INCH).to_pil().convert("RGB")
    finally:
        doc.close()
    # pdfium may add one row/column of page-edge pixels past the true size: drop it.
    return np.asarray(image.crop((0, 0, *render_size(pdf, dpi))))


def frame_ring(frame, datum, crs, n=FRAME_EDGE_POINTS):
    """The outline of `frame` ([w, s, e, n] in degrees on `datum`, a datums.py fragment) as grid
    metres in `crs`: a ring, each edge densified to n points."""
    w, s, e, nn = frame
    lons = [*np.linspace(w, e, n), *[e] * n, *np.linspace(e, w, n), *[w] * n]
    lats = [*[s] * n, *np.linspace(s, nn, n), *[nn] * n, *np.linspace(nn, s, n)]
    xs, ys = warp_points(CRS.from_proj4(f"+proj=longlat {datum} +no_defs"), crs, lons, lats)
    return list(zip(xs, ys, strict=True))


def ring_mask(ring, shape, transform):
    """Pixels inside a ring of grid coordinates, as a boolean array of `shape`."""
    return ~geometry_mask([{"type": "Polygon", "coordinates": [[*ring, ring[0]]]}], shape, transform)


def cut_at_frame(inside, frame, datum, crs, transform):
    """`inside` (the neatline mask) cut at the sides of `frame` where the neatline overshoots
    along nearly the whole side (see the module docstring). Returns the mask, and the sides cut."""
    w, s, e, n = frame
    dx, dy = (e - w) * EDGE_PROBE, (n - s) * EDGE_PROBE
    along = np.linspace(0.05, 0.95, 19)
    probes = {
        "west": ([w - dx] * len(along), s + (n - s) * along),
        "east": ([e + dx] * len(along), s + (n - s) * along),
        "south": (w + (e - w) * along, [s - dy] * len(along)),
        "north": (w + (e - w) * along, [n + dy] * len(along)),
    }
    geographic = CRS.from_proj4(f"+proj=longlat {datum} +no_defs")
    h, wpx = inside.shape
    cut = set()
    for side, (lons, lats) in probes.items():
        xs, ys = warp_points(geographic, crs, list(lons), list(lats))
        cols, rows = ~transform @ (np.array(xs), np.array(ys))
        on = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < wpx)
        hits = np.zeros(len(along), dtype=bool)
        hits[on] = inside[rows[on].astype(int), cols[on].astype(int)]
        if hits.mean() >= EDGE_CUT_SHARE:
            cut.add(side)
    if not cut:
        return inside, cut
    # Sides not cut are pushed a whole frame away, so they cut nothing.
    box = [
        w if "west" in cut else w - (e - w),
        s if "south" in cut else s - (n - s),
        e if "east" in cut else e + (e - w),
        n if "north" in cut else n + (n - s),
    ]
    return inside & ring_mask(frame_ring(box, datum, crs), inside.shape, transform), cut


def build(pdf, out, *, dpi=DPI, max_zoom=None, frame=None, frame_datum=None, progress=None):
    """Render, georeference and tile one sheet into the MBTiles file `out`. Returns a report dict:
    {"id", "tiles", "bounds", "maxzoom", "fit_m", "cut"}; fit_m is the worst registration misfit,
    cut the sides of the frame that cut off margin.

    frame, frame_datum: the sheet's corners [w, s, e, n] in degrees on the DMA datum frame_datum
    (default: the sheet's), used when the PDF records no corners of its own (see the docstring).
    """
    report = progress or (lambda fraction: None)
    g = lgi(pdf)
    crs = crs_for(g)

    # 1. Render the page.
    img = render_page(pdf, dpi)
    h, w = img.shape[:2]
    report(RENDER_SHARE)

    # 2. Pixel -> grid affine, least-squares fit to the registration points.
    #    PDF y runs up from the bottom; image rows run down from the top of the crop box.
    s = dpi / PT_PER_INCH
    x0, _, _, top = g["box"]
    px = np.array([[(x - x0) * s, (top - y) * s, 1.0] for x, y, _, _ in g["reg"]])
    world = np.array([[e, n] for _, _, e, n in g["reg"]])
    coef, *_ = np.linalg.lstsq(px, world, rcond=None)
    fit_m = float(np.abs(px @ coef - world).max())
    transform = Affine(coef[0, 0], coef[1, 0], coef[2, 0], coef[0, 1], coef[1, 1], coef[2, 1])

    # 3. Make everything outside the map frame (legend, margins) transparent.
    nl = g["neatline"]
    ring = [transform @ ((x - x0) * s, (top - y) * s) for x, y in zip(nl[0::2], nl[1::2], strict=True)]
    inside = ring_mask(ring, (h, w), transform)
    corners = own_corners(pdf)
    if corners:
        frame, frame_datum = corners, g["datum"]
    cut = set()
    if frame is not None:
        datum = datum_of({"datum": frame_datum or g["datum"]})
        inside, cut = cut_at_frame(inside, frame, datum, crs, transform)
    if not inside.any():
        raise ValueError(f"{pdf.name}: nothing of the map lies inside its neatline")
    bands = np.concatenate([img.transpose(2, 0, 1), (inside * np.uint8(255))[None]], axis=0)

    # The map's extent in WGS84 is the layer's: tiles beyond it would all be transparent. Taken
    # from the corners of the pixels kept, so it is never smaller than the map.
    rows, cols = np.nonzero(inside.any(axis=1))[0], np.nonzero(inside.any(axis=0))[0]
    box = [(cols[0], rows[0]), (cols[-1] + 1, rows[0]), (cols[-1] + 1, rows[-1] + 1), (cols[0], rows[-1] + 1)]
    grid = [transform @ p for p in box]
    lons, lats = warp_points(crs, "EPSG:4326", [p[0] for p in grid], [p[1] for p in grid])
    extent = [min(lons), min(lats), max(lons), max(lats)]
    del img, inside  # the page render is the biggest allocation: free it before warping

    # 4. Cut tiles, recording what the sheet says about itself.
    f = l7014_meta.facts(pdf, g["datum"])
    n, bounds, zoom = write_tiles(
        bands,
        crs,
        out,
        transform=transform,
        max_zoom=max_zoom,
        bounds=extent,
        name=f["title"] or layer_id_for(pdf),
        meta={
            "date": f["date"],
            "edition": f["edition"],
            "date_note": f["date_note"],
            "sheet_id": f["sheet_id"],
            "datum": g["datum"],
            "georeferencing": g["georef"],
            "registration_fit_m": f"{fit_m:.1f}",
        },
        progress=lambda fraction: report(RENDER_SHARE + (1 - RENDER_SHARE) * fraction),
    )
    report_cut = sorted(cut)
    return {
        "id": layer_id_for(pdf),
        "tiles": n,
        "bounds": bounds,
        "maxzoom": zoom,
        "fit_m": round(fit_m, 1),
        "cut": report_cut,
    }


def parse_frame(text):
    """[w, s, e, n] from "W,S,E,N" (degrees) on the command line."""
    try:
        w, s, e, n = (float(v) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"a frame is four numbers west,south,east,north, not {text!r}") from None
    if not (w < e and s < n):
        raise argparse.ArgumentTypeError("a frame needs west < east and south < north")
    return [w, s, e, n]


def main(argv=None):
    """Build one sheet; print a one-line report (and progress lines with --progress)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", type=Path, help="the GeoPDF sheet")
    ap.add_argument("out", type=Path, nargs="?", help="MBTiles file to write (default web/tiles/<id>.mbtiles)")
    ap.add_argument("--dpi", type=int, default=DPI, help="render resolution (default %(default)s)")
    ap.add_argument("--max-zoom", type=int, help="deepest zoom (default: the render's native zoom)")
    ap.add_argument(
        "--frame", type=parse_frame, metavar="W,S,E,N", help="the sheet's corners, if its PDF does not record them"
    )
    ap.add_argument("--frame-datum", metavar="CODE", help="DMA datum code of --frame (default: the sheet's)")
    ap.add_argument("--progress", action="store_true", help='print "progress <0..1>" lines while working')
    args = ap.parse_args(argv)

    def progress(fraction):
        print(f"progress {fraction:.3f}", flush=True)

    out = args.out or TILES / f"{layer_id_for(args.pdf)}.mbtiles"
    result = build(
        args.pdf,
        out,
        dpi=args.dpi,
        max_zoom=args.max_zoom,
        frame=args.frame,
        frame_datum=args.frame_datum,
        progress=progress if args.progress else None,
    )
    print(json.dumps({"done": result}), flush=True)


if __name__ == "__main__":
    main()
