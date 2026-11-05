"""Geodetic datums used by wartime Vietnam maps, as PROJ fragments.

Shared by the map pipeline (building tiles from scanned sheets) and the grid-reference tool, so a
coordinate read off a sheet and the sheet itself are always converted to WGS84 the same way.
Import the constants; there is nothing to run.

Each fragment is an ellipsoid plus a 3-parameter shift to WGS84 (+towgs84=dX,dY,dZ in metres,
no rotation or scale). Append it to a projection, e.g. "+proj=utm +zone=48 " + INDIAN_1960.
"""

# Indian 1960 (Everest 1830 ellipsoid) with the DMA shift for mainland Vietnam ("ING-A").
# Con Son island used its own shift ("ING-B"). The DMA codes are those of NIMA TR8350.2:
# ING = Indian 1960, INF = Indian 1954, INH = Indian 1975, and IND = the "Indian" datum of
# India, Nepal (IND-I) and Bangladesh (IND-B).
INDIAN_1960 = "+ellps=evrst30 +towgs84=198,881,317,0,0,0,0"
INDIAN_1960_CON_SON = "+ellps=evrst30 +towgs84=182,915,344,0,0,0,0"
WGS84 = "+datum=WGS84"

# Keyed by the datum code in sheet margins and in a GeoPDF's LGIDict.
#   "WGE" is WGS84: the 1986-2000 reprints of some 1:250,000 Joint Operations Graphics were recast
#   on it. "WE" is the same on one NIMA reprint, whose margin names the World Geodetic System.
#   "IND-I", "IND-B" and "INF-A" are mislabels in the georeferencing of some Vietnam sheets. Their
#   printed margins say "Indian datum 1960" (or just "Indian datum" on the 1:250,000 sheets), and
#   their corner grid coordinates match neighbouring Indian 1960 sheets, so they are read as
#   Indian 1960. (Read as Indian 1954, an INF-A sheet would move about 60 m.)
BY_DMA_CODE = {
    "ING-A": INDIAN_1960,
    "ING-B": INDIAN_1960_CON_SON,
    "IND-I": INDIAN_1960,
    "IND-B": INDIAN_1960,
    "INF-A": INDIAN_1960,
    "WGE": WGS84,
    "WE": WGS84,
}
# Keyed by the short names the grid-reference tool and the API accept.
BY_NAME = {"indian1960": INDIAN_1960, "indian1960-conson": INDIAN_1960_CON_SON, "wgs84": WGS84}
