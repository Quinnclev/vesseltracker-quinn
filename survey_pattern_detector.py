#!/usr/bin/env python3
"""
SURVEY PATTERN DETECTOR  (v3)
=============================

Finds "lawnmower" seafloor-mapping patterns in vessel position data:
runs of long, straight, parallel lines driven in alternating directions,
evenly spaced, and joined by short turns. For survey blocks that are
still in progress, it also projects where the next lines should fall.

Works on
--------
* CSV / TSV / TXT and Excel (.xlsx) files. GeoJSON, GeoPackage, Shapefile
  and KML also work if geopandas is installed.
* A single file, several files, or a whole folder, e.g. one export per ship.
  Each file or folder given is its own dataset: results carry a `dataset`
  column and the same MMSI in two datasets stays two tracks.
* One vessel or many. Tracks are split by an MMSI / IMO / name column when
  there is one; otherwise each file is treated as one vessel.
* Vessel details (name, MMSI, IMO, call sign, type, flag, size, destination)
  are read when the export has them; the flag falls back to the MMSI.
* Each survey area is matched to nearby ports and to rare-earth processing
  sites, mines and cargo ports (see port_intel.py and reference_data/).
* Column names are matched loosely: Latitude/lat/LAT, Longitude/lon/lng,
  Timestamp/BaseDateTime/time, Speed/SOG, Course/COG, MMSI/IMO/ShipName...

Usage
-----
    python survey_pattern_detector.py MarineTraffic_export.csv
    python survey_pattern_detector.py exports_folder/ --out results --workers 4
    python survey_pattern_detector.py ship_a.csv ship_b.csv --min-lines-per-block 4

Outputs (default folder: ./survey_results)
------------------------------------------
    survey_blocks.csv        one row per detected survey block (with a score)
    survey_lines.csv         every straight line found and the block it belongs to
    survey_areas.csv         blocks of the same survey joined into areas
    predictions.csv          projected next lines for blocks still in progress
    vessels.csv              one row per vessel: identity, track summary, survey status
    nearby_ports.csv         nearest ports and rare-earth linked ports per survey area
    positions_processed.csv  every input point with its cleaning status, line and block
    survey_results.geojson   tracks, areas, blocks, lines, predictions, ports, port
                             routes and rare-earth sites for QGIS/ArcGIS
    maps/<dataset>__<vessel>.png  quick-look map per vessel (needs matplotlib)

Requirements: python 3.9+, numpy, pandas.
Optional: matplotlib (maps), geopandas (GIS formats), openpyxl (Excel),
searoute (sea-route distances to ports; straight-line without it).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

import port_intel


# ============================================================
# SETTINGS
# ============================================================

@dataclass
class Config:
    # --- cleaning ---------------------------------------------------------
    min_speed_kn: float = 1.5          # below this the ship is loitering/drifting
    min_step_km: float = 0.05          # closer than this to the previous point = stationary
    max_implied_speed_kn: float = 30.0 # faster than this between fixes = bad position
    max_gap_hours: float = 48.0        # split the track at longer reporting gaps
    max_gap_km_no_time: float = 60.0   # without timestamps, split at bigger jumps

    # --- straight-line extraction ----------------------------------------
    rdp_tolerance_km: float = 1.0      # how far a track may wander and still be "straight"
    min_segment_km: float = 3.0        # shorter pieces are treated as turns/connectors
    merge_heading_tol_deg: float = 15.0
    merge_offset_km: float = 2.0       # pieces this close to one line get merged
    merge_gap_km: float = 10.0         # ...across at most this much turn/dog-leg
    min_line_km: float = 8.0           # shortest straight line counted as a survey line
    min_points_per_line: int = 3

    # --- lawnmower pattern -----------------------------------------------
    antiparallel_tol_deg: float = 20.0 # next line must run within this of opposite way
    block_axis_tol_deg: float = 15.0   # every line must stay within this of block axis
    min_spacing_km: float = 0.05
    max_spacing_km: float = 25.0       # widest plausible gap between adjacent lines
    min_overlap_frac: float = 0.3      # adjacent lines must cover the same stretch
    min_length_ratio: float = 0.25     # shorter/longer line length
    max_connector_km: float = 40.0     # longest turn between the end of one line and the next
    max_skip_lines: int = 2            # odd lines (turn pieces, cross-lines) tolerated inside a block
    min_lines_per_block: int = 3
    area_join_km: float = 15.0         # blocks this close with the same axis form one survey area
    # --- prediction -------------------------------------------------------
    predict_lines: int = 3             # how many upcoming lines to project
    min_progression_for_prediction: float = 0.6
    # --- ports --------------------------------------------------------------
    port_country: str = "flag"         # also list ports of: "flag" = each vessel's flag state,
                                       # a country name (e.g. "China") = that country for every
                                       # vessel, "none" = skip


EARTH_RADIUS_KM = 6371.0088
KM_PER_NM = 1.852


# ============================================================
# READING ANY INPUT
# ============================================================

TABULAR_EXT = {".csv", ".txt", ".tsv"}
EXCEL_EXT = {".xlsx", ".xlsm", ".xls"}
GIS_EXT = {".geojson", ".json", ".gpkg", ".shp", ".kml", ".gml"}
SUPPORTED_EXT = TABULAR_EXT | EXCEL_EXT | GIS_EXT

COLUMN_ALIASES = {
    "lat": ["latitude", "lat", "ycoord", "y"],
    "lon": ["longitude", "lon", "lng", "long", "xcoord", "x"],
    "time": ["timestamp", "basedatetime", "datetime", "timestamputc", "time",
             "utc", "date", "positiontime", "ts"],
    "vessel": ["mmsi", "imo", "vesselid", "shipid", "vesselname", "shipname",
               "vessel", "name", "callsign"],
    "speed": ["speed", "sog", "speedkn", "speedknots", "speedoverground"],
    "course": ["course", "cog", "courseoverground", "coursedeg", "cogdeg"],
}

# vessel details carried through to vessels.csv when the export has them
INFO_ALIASES = {
    "name": ["shipname", "vesselname", "name", "ship"],
    "mmsi": ["mmsi", "mmsinumber", "userid"],
    "imo": ["imo", "imonumber", "imono"],
    "callsign": ["callsign", "call"],
    "ship_type": ["shiptype", "vesseltype", "shiptypename", "vesseltypename", "typeofship",
                  "shipandcargotype", "aistype", "type"],
    "flag": ["flag", "flagcountry", "flagstate", "flagname"],
    "length_m": ["length", "loa", "lengthm", "shiplength"],
    "width_m": ["width", "beam", "widthm", "shipwidth"],
    "draught_m": ["draught", "draft", "draughtm", "draftm"],
    "destination": ["destination", "dest"],
}

DATE_PATTERN = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
)


def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).replace("﻿", "").lower())


def find_input_files(paths):
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED_EXT)
        elif p.exists():
            files.append(p)
        else:
            raise FileNotFoundError(f"Could not find: {p.resolve()}")
    if not files:
        raise FileNotFoundError("No supported data files found.")
    return files


def read_any(path: Path) -> pd.DataFrame:
    ext = path.suffix.lower()

    if ext in TABULAR_EXT:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            sample = fh.read(20000)
        try:
            sep = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            sep = ","
        return pd.read_csv(path, sep=sep, encoding="utf-8-sig", low_memory=False)

    if ext in EXCEL_EXT:
        return pd.read_excel(path)

    if ext in GIS_EXT:
        try:
            import geopandas as gpd
        except ImportError as exc:
            raise ImportError(
                f"{path.name}: reading {ext} files needs geopandas "
                "(pip install geopandas), or export the layer to CSV."
            ) from exc
        gdf = gpd.read_file(path)
        if gdf.crs is not None:
            gdf = gdf.to_crs(4326)
        gdf = gdf.explode(index_parts=False)
        rows = []
        for _, rec in gdf.iterrows():
            geom = rec.geometry
            attrs = rec.drop(labels="geometry").to_dict()
            if geom is None or geom.is_empty:
                continue
            coords = [(geom.x, geom.y)] if geom.geom_type == "Point" else list(geom.coords)
            for x, y, *_ in coords:
                rows.append({**attrs, "Longitude": x, "Latitude": y})
        return pd.DataFrame(rows)

    raise ValueError(f"Unsupported file type: {path.name}")


def parse_times(series: pd.Series):
    """Return UTC timestamps, or None when the column has no usable dates."""
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().mean() > 0.9 and numeric.median() > 1e9:  # unix epoch
        unit = "ms" if numeric.median() > 1e12 else "s"
        return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")

    text = series.astype(str)
    if text.str.contains(DATE_PATTERN).mean() < 0.9:
        return None  # e.g. "55:01.0": Excel stripped the date away
    try:
        parsed = pd.to_datetime(text, errors="coerce", utc=True, format="mixed")
    except (TypeError, ValueError):
        parsed = pd.to_datetime(text, errors="coerce", utc=True)
    return parsed if parsed.notna().mean() >= 0.9 else None


def standardize(raw: pd.DataFrame, source_name: str):
    """Map whatever columns the file has onto the internal schema."""
    lookup = {_norm(c): c for c in raw.columns}

    def pick(key):
        for alias in COLUMN_ALIASES[key]:
            if alias in lookup:
                return lookup[alias]
        return None

    lat_col, lon_col = pick("lat"), pick("lon")
    if lat_col is None or lon_col is None:
        raise ValueError(
            f"{source_name}: could not find latitude/longitude columns. "
            f"Columns present: {list(raw.columns)}"
        )

    out = pd.DataFrame({
        "lat": pd.to_numeric(raw[lat_col], errors="coerce"),
        "lon": pd.to_numeric(raw[lon_col], errors="coerce"),
    })
    for key, name in [("speed", "speed_kn"), ("course", "course")]:
        col = pick(key)
        out[name] = pd.to_numeric(raw[col], errors="coerce") if col else np.nan

    vessel_col = pick("vessel")
    out["vessel"] = raw[vessel_col].astype(str).str.strip() if vessel_col else source_name
    for key, aliases in INFO_ALIASES.items():
        col = next((lookup[a] for a in aliases if a in lookup), None)
        if col is not None:
            out[f"info_{key}"] = raw[col].astype(str).str.strip().replace({"nan": "", "None": ""})
    # single-vessel exports (e.g. MarineTraffic position history) carry no identity:
    # take an MMSI / IMO written into the file name, e.g. 413123456_positions.csv
    named = ids_from_name(source_name) if vessel_col is None else {}   # one-vessel files only
    for key, value in named.items():
        if f"info_{key}" not in out:
            out[f"info_{key}"] = value

    time_col = pick("time")
    times = parse_times(raw[time_col]) if time_col else None
    out["time"] = times if times is not None else pd.NaT
    out["time_raw"] = raw[time_col].astype(str) if time_col else ""
    out["source_file"] = source_name
    out["row_in_file"] = np.arange(len(raw))

    notes = []
    if time_col and times is None:
        notes.append(
            f"{source_name}: '{time_col}' has no usable dates (values like "
            f"'{raw[time_col].iloc[0]}'). This usually means the CSV was opened "
            "and saved in Excel. Row order is used instead; durations and ETAs "
            "are relative only."
        )
    if named:
        notes.append(f"{source_name}: " + " and ".join(f"{k.upper()} {v}" for k, v in named.items())
                     + " taken from the file name.")
    if vessel_col is None:
        has_id = "info_mmsi" in out or "info_flag" in out
        notes.append(f"{source_name}: no vessel ID column, so the file is treated as one vessel"
                     + ("." if has_id else ", and its flag is unknown. Give its MMSI or flag in the "
                        "web app, or put the MMSI in the file name (e.g. 413123456_" + source_name + ")."))

    valid = out["lat"].between(-90, 90) & out["lon"].between(-180, 180)
    return out[valid].reset_index(drop=True), notes


def ids_from_name(name: str) -> dict:
    """MMSI (9 digits, first digit 2-7) and IMO ('IMO' + 7 digits) found in a file name."""
    out = {}
    mmsi = re.search(r"(?<!\d)([2-7]\d{8})(?!\d)", name)
    imo = re.search(r"IMO[\s_-]?(\d{7})(?!\d)", name, re.IGNORECASE)
    if mmsi:
        out["mmsi"] = mmsi.group(1)
    if imo:
        out["imo"] = imo.group(1)
    return out


def apply_identities(data: pd.DataFrame, identities) -> pd.DataFrame:
    """
    Fill in vessel identity the data lacks. `identities` maps (dataset, vessel)
    -- or (dataset, None) for every vessel of a dataset -- to e.g.
    {"mmsi": "413123456", "flag": "China", "name": "..."}; blank values are ignored.
    """
    for key, given in dict(identities or {}).items():
        ds, vessel = key if isinstance(key, tuple) else (key, None)
        rows = data["dataset"] == str(ds)
        if vessel is not None:
            rows &= data["vessel"].astype(str) == str(vessel)
        for field, value in dict(given).items():
            if field in INFO_ALIASES and str(value or "").strip():
                if f"info_{field}" not in data:
                    data[f"info_{field}"] = ""
                data.loc[rows, f"info_{field}"] = str(value).strip()
                if field == "flag":   # where the flag came from, for vessels.csv / the app
                    data.loc[rows, "flag_entered"] = str(dict(given).get("source") or "entered")
    return data


def load_inputs(paths):
    frames, notes = [], []
    for f in find_input_files(paths):
        df, n = standardize(read_any(f), f.stem)
        frames.append(df)
        notes += n
    return pd.concat(frames, ignore_index=True), notes


# ============================================================
# GEOMETRY (vectorised)
# ============================================================

def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def bearing_deg(lat1, lon1, lat2, lon2):
    lat1, lat2 = np.radians(lat1), np.radians(lat2)
    dlon = np.radians(np.asarray(lon2) - np.asarray(lon1))
    x = np.sin(dlon) * np.cos(lat2)
    y = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(dlon)
    return (np.degrees(np.arctan2(x, y)) + 360) % 360


def angle_diff(a, b):
    """Smallest difference between two directions, 0-180."""
    return np.abs((np.asarray(a) - np.asarray(b) + 180) % 360 - 180)


def axis_diff(a, b):
    """Difference between two undirected axes, 0-90."""
    d = np.abs(np.asarray(a) % 180 - np.asarray(b) % 180)
    return np.minimum(d, 180 - d)


def axial_mean(angles, weights=None):
    """Mean of undirected axes (0-180), correct across the 0/180 wrap."""
    r = np.radians(np.asarray(angles, float) * 2)
    w = np.ones_like(r) if weights is None else np.asarray(weights, float)
    return (np.degrees(np.arctan2((w * np.sin(r)).sum(), (w * np.cos(r)).sum())) / 2) % 180


def to_local_xy(lat, lon, lat0, lon0):
    """Equirectangular projection in km around (lat0, lon0). Accurate locally."""
    dlon = (np.asarray(lon) - lon0 + 180) % 360 - 180
    x = EARTH_RADIUS_KM * np.radians(dlon) * math.cos(math.radians(lat0))
    y = EARTH_RADIUS_KM * np.radians(np.asarray(lat) - lat0)
    return x, y


def from_local_xy(x, y, lat0, lon0):
    lat = lat0 + np.degrees(np.asarray(y) / EARTH_RADIUS_KM)
    lon = lon0 + np.degrees(np.asarray(x) / (EARTH_RADIUS_KM * math.cos(math.radians(lat0))))
    return lat, (lon + 180) % 360 - 180


def xy_heading(dx, dy):
    return (np.degrees(np.arctan2(dx, dy)) + 360) % 360


def rdp_vertices(x, y, eps):
    """Ramer-Douglas-Peucker: indices of points that define straight pieces."""
    n = len(x)
    keep = np.zeros(n, bool)
    keep[[0, n - 1]] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        px, py = x[i + 1:j] - x[i], y[i + 1:j] - y[i]
        dx, dy = x[j] - x[i], y[j] - y[i]
        seg2 = dx * dx + dy * dy
        if seg2 == 0:
            d = np.hypot(px, py)
        else:
            t = np.clip((px * dx + py * dy) / seg2, 0, 1)
            d = np.hypot(px - t * dx, py - t * dy)
        k = int(np.argmax(d))
        if d[k] > eps:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return np.flatnonzero(keep)


def convex_hull(points):
    pts = sorted(set(map(tuple, points)))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def polygon_area(pts):
    if len(pts) < 3:
        return 0.0
    x, y = np.array(pts).T
    return 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))


# ============================================================
# STEP 1 - ORDER AND CLEAN EACH VESSEL TRACK
# ============================================================

def course_agreement(g: pd.DataFrame) -> float:
    """Share of moves whose direction matches the reported course."""
    if g["course"].notna().sum() < 5 or len(g) < 5:
        return np.nan
    la, lo = g["lat"].to_numpy(), g["lon"].to_numpy()
    b = bearing_deg(la[:-1], lo[:-1], la[1:], lo[1:])
    d = haversine_km(la[:-1], lo[:-1], la[1:], lo[1:])
    c = g["course"].to_numpy()[1:]
    ok = (d > 0.2) & np.isfinite(c)
    return float((angle_diff(b[ok], c[ok]) < 30).mean()) if ok.any() else np.nan


def order_track(v: pd.DataFrame, has_time: bool):
    if has_time:
        return v.sort_values(["time", "row_in_file"]).reset_index(drop=True), "timestamps"

    parts, how = [], "file order"
    for _, g in v.groupby("source_file", sort=False):
        g = g.sort_values("row_in_file")
        fwd, rev = course_agreement(g), course_agreement(g.iloc[::-1])
        if np.isfinite(rev) and (not np.isfinite(fwd) or rev > fwd):
            g, how = g.iloc[::-1], "file order reversed (newest-first export, detected from Course)"
        parts.append(g)
    return pd.concat(parts).reset_index(drop=True), how


def clean_track(v: pd.DataFrame, cfg: Config, has_time: bool) -> pd.DataFrame:
    v = v.copy()
    v["status"] = "kept"

    dup_keys = ["lat", "lon"] + (["time"] if has_time else [])
    v.loc[v.duplicated(subset=dup_keys, keep="first"), "status"] = "duplicate"

    slow = v["speed_kn"] < cfg.min_speed_kn          # NaN speeds are kept
    v.loc[slow & (v["status"] == "kept"), "status"] = "low_speed"

    for _ in range(2):
        k = v.index[v["status"] == "kept"]
        if len(k) < 2:
            break
        d = haversine_km(v.loc[k[:-1], "lat"].values, v.loc[k[:-1], "lon"].values,
                         v.loc[k[1:], "lat"].values, v.loc[k[1:], "lon"].values)
        near = np.r_[False, d < cfg.min_step_km]
        v.loc[k[near], "status"] = "stationary"

    if has_time:
        for _ in range(3):
            k = v.index[v["status"] == "kept"]
            if len(k) < 3:
                break
            spd = implied_speed_kn(v.loc[k])
            spike = np.r_[False, (spd[:-1] > cfg.max_implied_speed_kn)
                          & (spd[1:] > cfg.max_implied_speed_kn), False]
            if not spike.any():
                break
            v.loc[k[spike], "status"] = "position_spike"
    return v


def implied_speed_kn(p: pd.DataFrame):
    la, lo = p["lat"].to_numpy(), p["lon"].to_numpy()
    d = haversine_km(la[:-1], lo[:-1], la[1:], lo[1:])
    dt_h = np.diff(p["time"].to_numpy()).astype("timedelta64[s]").astype(float) / 3600
    return d / KM_PER_NM / np.maximum(dt_h, 1 / 3600)


def split_chunks(p: pd.DataFrame, cfg: Config, has_time: bool):
    la, lo = p["lat"].to_numpy(), p["lon"].to_numpy()
    d = haversine_km(la[:-1], lo[:-1], la[1:], lo[1:])
    if has_time:
        dt_h = np.diff(p["time"].to_numpy()).astype("timedelta64[s]").astype(float) / 3600
        brk = (implied_speed_kn(p) > cfg.max_implied_speed_kn) | (dt_h > cfg.max_gap_hours)
    else:
        brk = d > cfg.max_gap_km_no_time
    return np.cumsum(np.r_[0, brk])


# ============================================================
# STEP 2 - EXTRACT STRAIGHT LINES
# ============================================================

def lines_in_chunk(lat, lon, cfg: Config):
    """Return (start, end) point-index pairs of straight lines in one chunk."""
    if len(lat) < 2:
        return []
    x, y = to_local_xy(lat, lon, float(np.median(lat)), float(np.median(lon)))
    verts = rdp_vertices(x, y, cfg.rdp_tolerance_km)

    pieces, cur, zigzag_km = [], None, 0.0
    for a, b in zip(verts[:-1], verts[1:]):
        length = math.hypot(x[b] - x[a], y[b] - y[a])
        if length < cfg.min_segment_km:            # part of a turn or a jitter
            zigzag_km += length
            if cur is not None and zigzag_km > cfg.merge_gap_km:
                pieces.append(cur)
                cur = None
            continue

        heading = xy_heading(x[b] - x[a], y[b] - y[a])
        if cur is not None:
            s, e = cur
            L = math.hypot(x[e] - x[s], y[e] - y[s])
            ux, uy = (x[e] - x[s]) / L, (y[e] - y[s]) / L
            # continuity at the joint: the new piece must start on (the extension of)
            # the current line and keep roughly the same heading. Gently curving
            # survey lines are rejoined this way instead of being split in two.
            off = abs((x[a] - x[s]) * uy - (y[a] - y[s]) * ux)
            same_line = (angle_diff(heading, xy_heading(ux, uy)) <= cfg.merge_heading_tol_deg
                         and off <= cfg.merge_offset_km
                         and zigzag_km <= cfg.merge_gap_km)
            if same_line:
                cur = (s, b)
                zigzag_km = 0.0
                continue
            if length + zigzag_km <= cfg.merge_gap_km:
                # short dog-leg (ice, obstacle): keep the line open and see
                # whether the track comes back onto it
                zigzag_km += length
                continue
            pieces.append(cur)
        cur, zigzag_km = (a, b), 0.0

    if cur is not None:
        pieces.append(cur)
    return pieces


def describe_line(p: pd.DataFrame, i0: int, i1: int, has_time: bool) -> dict:
    seg = p.iloc[i0:i1 + 1]
    la, lo = seg["lat"].to_numpy(), seg["lon"].to_numpy()
    chord = float(haversine_km(la[0], lo[0], la[-1], lo[-1]))
    path = float(haversine_km(la[:-1], lo[:-1], la[1:], lo[1:]).sum())
    heading = float(bearing_deg(la[0], lo[0], la[-1], lo[-1]))
    return {
        "i0": i0, "i1": i1,
        "n_points": len(seg),
        "start_lat": la[0], "start_lon": lo[0],
        "end_lat": la[-1], "end_lon": lo[-1],
        "length_km": chord,
        "path_km": path,
        "straightness": chord / path if path > 0 else 1.0,
        "heading_deg": heading,
        "axis_deg": heading % 180,
        "median_speed_kn": float(seg["speed_kn"].median()) if seg["speed_kn"].notna().any() else np.nan,
        "start_time": seg["time"].iloc[0] if has_time else pd.NaT,
        "end_time": seg["time"].iloc[-1] if has_time else pd.NaT,
        "start_row_in_file": int(seg["row_in_file"].iloc[0]),
        "end_row_in_file": int(seg["row_in_file"].iloc[-1]),
    }


# ============================================================
# STEP 3 - FIND LAWNMOWER BLOCKS
# ============================================================

def compare_lines(A: dict, B: dict):
    """Geometry of line B relative to line A, in a local frame centred on A."""
    lat0 = (A["start_lat"] + A["end_lat"]) / 2
    lon0 = (A["start_lon"] + A["end_lon"]) / 2
    ax, ay = to_local_xy([A["start_lat"], A["end_lat"]], [A["start_lon"], A["end_lon"]], lat0, lon0)
    bx, by = to_local_xy([B["start_lat"], B["end_lat"]], [B["start_lon"], B["end_lon"]], lat0, lon0)

    ua = np.array([ax[1] - ax[0], ay[1] - ay[0]])
    ua /= np.linalg.norm(ua)
    ub = np.array([bx[1] - bx[0], by[1] - by[0]])
    ub /= np.linalg.norm(ub)
    normal = np.array([ua[1], -ua[0]])   # starboard side of line A

    between = math.degrees(math.acos(float(np.clip(ua @ ub, -1, 1))))
    mid_b = np.array([bx.mean(), by.mean()])            # A's midpoint is the origin
    along_a = sorted([np.array([ax[0], ay[0]]) @ ua, np.array([ax[1], ay[1]]) @ ua])
    along_b = sorted([np.array([bx[0], by[0]]) @ ua, np.array([bx[1], by[1]]) @ ua])
    overlap = max(0.0, min(along_a[1], along_b[1]) - max(along_a[0], along_b[0]))

    return {
        "antiparallel_dev": 180 - between,
        "offset_km": float(mid_b @ normal),
        "overlap_frac": overlap / min(A["length_km"], B["length_km"]),
        "length_ratio": min(A["length_km"], B["length_km"]) / max(A["length_km"], B["length_km"]),
        "connector_km": float(math.hypot(bx[0] - ax[1], by[0] - ay[1])),
    }


def lines_pair(A, B, cfg: Config, check_connector=True):
    c = compare_lines(A, B)
    ok = (c["antiparallel_dev"] <= cfg.antiparallel_tol_deg
          and cfg.min_spacing_km <= abs(c["offset_km"]) <= cfg.max_spacing_km
          and c["overlap_frac"] >= cfg.min_overlap_frac
          and c["length_ratio"] >= cfg.min_length_ratio
          and (not check_connector or c["connector_km"] <= cfg.max_connector_km))
    return ok, c


def find_blocks(lines: list, cfg: Config):
    """Walk the lines in time order and grow runs that look like a lawnmower."""
    blocks, skipped = [], set()
    if not lines:
        return blocks, skipped

    def axis_ok(block, k):
        mean = axial_mean([lines[i]["axis_deg"] for i in block],
                          [lines[i]["length_km"] for i in block])
        return axis_diff(mean, lines[k]["axis_deg"]) <= cfg.block_axis_tol_deg

    def close(block):
        if len(block) >= cfg.min_lines_per_block:
            blocks.append(list(block))

    cur, k = [0], 1
    while k < len(lines):
        if lines_pair(lines[cur[-1]], lines[k], cfg)[0] and axis_ok(cur, k):
            cur.append(k)
            k += 1
            continue
        jumped = False
        if len(cur) >= 2:
            for j in range(k + 1, min(k + 1 + cfg.max_skip_lines, len(lines))):
                if (lines_pair(lines[cur[-1]], lines[j], cfg, check_connector=False)[0]
                        and axis_ok(cur, j)):
                    skipped.update(range(k, j))
                    cur.append(j)
                    k = j + 1
                    jumped = True
                    break
        if jumped:
            continue
        close(cur)
        cur, k = [k], k + 1
    close(cur)
    return blocks, skipped


def trim_overshoot(line: dict, neighbour: dict, axis: float, p: pd.DataFrame,
                   cfg: Config, has_time: bool) -> dict:
    """
    Cut the ends of a block's first/last line that run past its neighbour and
    drift off the line. A transit leg into or out of a survey arrives almost in
    line with it, so RDP keeps it as part of the first/last survey line.

    The part of `line` alongside `neighbour` is the core; a straight line is
    fitted to it, and the line is extended outward only while the track stays
    on that fit.
    """
    lat0 = (line["start_lat"] + line["end_lat"]) / 2
    lon0 = (line["start_lon"] + line["end_lon"]) / 2
    u = np.array([math.sin(math.radians(axis)), math.cos(math.radians(axis))])
    n = np.array([u[1], -u[0]])

    def project(l):
        seg = p.iloc[l["i0"]:l["i1"] + 1]
        x, y = to_local_xy(seg["lat"].to_numpy(), seg["lon"].to_numpy(), lat0, lon0)
        return x * u[0] + y * u[1], x * n[0] + y * n[1]

    along, cross = project(line)
    n_along, n_cross = project(neighbour)
    alongside = (along >= n_along.min()) & (along <= n_along.max())
    fit = alongside.copy()
    fit[[0, -1]] = False                  # end vertices are often turn corners
    core = np.flatnonzero(fit)
    if len(core) < 2 or np.ptp(along[core]) < 1.0:
        return line                       # too little side-by-side to judge

    slope, icpt = np.polyfit(along[core], cross[core], 1)
    resid = np.abs(cross - (icpt + slope * along))
    spacing = abs(float(np.median(cross[core]) - np.median(n_cross)))
    base_tol = min(cfg.rdp_tolerance_km, max(0.15, 0.1 * spacing))
    # allow a small angle as well, so sparse fixes far from the core are not
    # cut for the fit's slope error; transit legs come in at a larger angle
    angle_tol = math.tan(math.radians(3.0))

    def kept_outward(order, anchor):
        """
        Points kept walking away from the core: stop at two off-line fixes in a
        row. Fixes alongside the neighbour are never cut; with sparse AIS a
        turn corner may be the only fix marking where the line ends.
        """
        off = ((resid[order] > base_tol + angle_tol * np.abs(along[order] - along[anchor]))
               & ~alongside[order])
        for k in range(len(order)):
            if off[k] and (k == len(order) - 1 or off[k + 1]):
                return k
        return len(order)

    first, last = core[0], core[-1]
    i0 = line["i0"] + first - kept_outward(np.arange(first - 1, -1, -1), first)
    i1 = line["i0"] + last + kept_outward(np.arange(last + 1, len(along)), last)
    if (i0, i1) == (line["i0"], line["i1"]):
        return line
    trimmed = {**line, **describe_line(p, i0, i1, has_time)}
    trimmed["trimmed_km"] = round(line["length_km"] - trimmed["length_km"], 3)
    return trimmed


def trim_block_ends(block: list, lines: list, p: pd.DataFrame, cfg: Config, has_time: bool):
    """Trim transit legs off a block's first and last lines; drop them if little is left."""
    inner = block[1:-1]
    axis = axial_mean([lines[i]["axis_deg"] for i in inner],
                      [lines[i]["length_km"] for i in inner])
    for edge, nb in ((0, 1), (-1, -2)):
        k = block[edge]
        lines[k] = trim_overshoot(lines[k], lines[block[nb]], axis, p, cfg, has_time)
    return [k for k in block
            if lines[k]["length_km"] >= cfg.min_line_km
            and lines[k]["n_points"] >= cfg.min_points_per_line]


def summarize_block(vessel, block_id, idx, lines, cfg: Config, has_time: bool):
    L = [lines[i] for i in idx]
    lat0 = float(np.mean([l[k] for l in L for k in ("start_lat", "end_lat")]))
    lon0 = float(np.mean([l[k] for l in L for k in ("start_lon", "end_lon")]))

    axis = axial_mean([l["axis_deg"] for l in L], [l["length_km"] for l in L])
    u = np.array([math.sin(math.radians(axis)), math.cos(math.radians(axis))])
    n = np.array([u[1], -u[0]])

    ends, mids = [], []
    for l in L:
        x, y = to_local_xy([l["start_lat"], l["end_lat"]], [l["start_lon"], l["end_lon"]], lat0, lon0)
        ends += list(zip(x, y))
        mids.append((x.mean(), y.mean()))
    mids = np.array(mids)
    cross = mids @ n                       # where each line sits across the block
    along = mids @ u                       # where each line is centred along the block
    steps = np.diff(cross)
    spacing = np.abs(steps)

    overall = np.sign(cross[-1] - cross[0]) or 1.0
    progression = float((np.sign(steps) == overall).mean())
    advance = n * overall
    advance_bearing = float(xy_heading(advance[0], advance[1]))

    pair = [compare_lines(a, b) for a, b in zip(L[:-1], L[1:])]
    anti_dev = np.array([c["antiparallel_dev"] for c in pair])
    overlap = np.array([c["overlap_frac"] for c in pair])
    ratio = np.array([c["length_ratio"] for c in pair])
    spacing_cv = float(spacing.std() / spacing.mean()) if spacing.mean() > 0 else 1.0

    score = (0.25 * min(1.0, (len(L) - 1) / 5)
             + 0.20 * max(0.0, 1 - anti_dev.mean() / cfg.antiparallel_tol_deg)
             + 0.20 * max(0.0, 1 - spacing_cv)
             + 0.15 * float(np.clip(overlap, 0, 1).mean())
             + 0.10 * float(ratio.mean())
             + 0.10 * progression)
    confidence = "high" if score >= 0.75 else "medium" if score >= 0.55 else "low"

    hull_xy = convex_hull(ends)
    hlat, hlon = from_local_xy([p[0] for p in hull_xy], [p[1] for p in hull_xy], lat0, lon0)

    speeds = [l["median_speed_kn"] for l in L if np.isfinite(l["median_speed_kn"])]
    summary = {
        "vessel": vessel,
        "block_id": block_id,
        "n_lines": len(L),
        "confidence": confidence,
        "score": round(score, 3),
        "line_axis_deg": round(axis, 1),
        "line_axis_compass": f"{compass(axis)}-{compass(axis + 180)}",
        "median_spacing_km": round(float(np.median(spacing)), 3),
        "spacing_cv": round(spacing_cv, 3),
        "mean_line_km": round(float(np.mean([l["length_km"] for l in L])), 2),
        "total_line_km": round(float(np.sum([l["length_km"] for l in L])), 1),
        "area_km2": round(polygon_area(hull_xy), 1),
        "pattern": "progressive" if progression >= 0.8 else "interleaved / racetrack",
        "progression_frac": round(progression, 2),
        "advancing_toward_deg": round(advance_bearing, 1),
        "advancing_toward": compass(advance_bearing),
        "mean_antiparallel_dev_deg": round(float(anti_dev.mean()), 1),
        "mean_overlap_frac": round(float(overlap.mean()), 2),
        "survey_speed_kn": round(float(np.median(speeds)), 1) if speeds else np.nan,
        "centroid_lat": round(lat0, 5),
        "centroid_lon": round(lon0, 5),
        "start_time": L[0]["start_time"] if has_time else pd.NaT,
        "end_time": L[-1]["end_time"] if has_time else pd.NaT,
        "first_row_in_file": L[0]["start_row_in_file"],
        "last_row_in_file": L[-1]["end_row_in_file"],
    }
    if has_time:
        summary["duration_hours"] = round(
            (L[-1]["end_time"] - L[0]["start_time"]).total_seconds() / 3600, 1)

    geometry = {
        "hull": list(zip(hlon, hlat)),
        "frame": (lat0, lon0, u, n, cross, along, overall),
        "median_line_km": float(np.median([l["length_km"] for l in L])),
    }
    return summary, geometry


# ============================================================
# STEP 3b - JOIN BLOCKS INTO SURVEY AREAS
# ============================================================

def _point_in_polygon(pt, poly):
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _point_segment_distance(p, a, b):
    p, a, b = map(np.asarray, (p, a, b))
    ab = b - a
    t = 0.0 if not ab.any() else float(np.clip((p - a) @ ab / (ab @ ab), 0, 1))
    return float(np.linalg.norm(p - (a + t * ab)))


def hull_distance_km(h1, h2):
    """Distance between two lon/lat hulls (0 if they overlap)."""
    pts = h1 + h2
    lat0 = float(np.mean([p[1] for p in pts]))
    lon0 = float(np.mean([p[0] for p in pts]))
    a = list(zip(*to_local_xy([p[1] for p in h1], [p[0] for p in h1], lat0, lon0)))
    b = list(zip(*to_local_xy([p[1] for p in h2], [p[0] for p in h2], lat0, lon0)))
    if len(a) >= 3 and any(_point_in_polygon(p, a) for p in b):
        return 0.0
    if len(b) >= 3 and any(_point_in_polygon(p, b) for p in a):
        return 0.0
    best = np.inf
    for P, Q in ((a, b), (b, a)):
        edges = list(zip(Q, Q[1:] + Q[:1])) if len(Q) > 1 else [(Q[0], Q[0])]
        for p in P:
            for s, e in edges:
                best = min(best, _point_segment_distance(p, s, e))
    return best


def group_survey_areas(vessel, blocks, geoms, cfg: Config, has_time: bool):
    """Blocks with the same line axis that touch or nearly touch = one survey area."""
    n = len(blocks)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if axis_diff(blocks[i]["line_axis_deg"], blocks[j]["line_axis_deg"]) > cfg.block_axis_tol_deg:
                continue
            if hull_distance_km(geoms[blocks[i]["block_id"]]["hull"],
                                geoms[blocks[j]["block_id"]]["hull"]) <= cfg.area_join_km:
                parent[find(j)] = find(i)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    areas, hulls = [], {}
    ordered = sorted(groups.values(), key=lambda g: min(blocks[i]["first_row_in_file"] for i in g))
    for aid, members in enumerate(ordered, 1):
        B = [blocks[i] for i in members]
        for b in B:
            b["area_id"] = aid
        pts = [p for b in B for p in geoms[b["block_id"]]["hull"]]
        lat0 = float(np.mean([p[1] for p in pts]))
        lon0 = float(np.mean([p[0] for p in pts]))
        xy = list(zip(*to_local_xy([p[1] for p in pts], [p[0] for p in pts], lat0, lon0)))
        hull_xy = convex_hull(xy)
        hlat, hlon = from_local_xy([p[0] for p in hull_xy], [p[1] for p in hull_xy], lat0, lon0)
        hulls[aid] = list(zip(hlon, hlat))
        weights = [b["total_line_km"] for b in B]
        area = {
            "vessel": vessel,
            "area_id": aid,
            "status": "IN PROGRESS at last fix" if any(b["status"].startswith("IN") for b in B) else "completed",
            "n_blocks": len(B),
            "block_ids": " ".join(str(b["block_id"]) for b in B),
            "n_lines": int(sum(b["n_lines"] for b in B)),
            "best_confidence": max(B, key=lambda b: b["score"])["confidence"],
            "line_axis_deg": round(axial_mean([b["line_axis_deg"] for b in B], weights), 1),
            "median_spacing_km": round(float(np.median([b["median_spacing_km"] for b in B])), 3),
            "total_line_km": round(float(sum(weights)), 1),
            "area_km2": round(polygon_area(hull_xy), 1),
            "centroid_lat": round(lat0, 5),
            "centroid_lon": round(lon0, 5),
            "first_row_in_file": min(b["first_row_in_file"] for b in B),
            "last_row_in_file": max(b["last_row_in_file"] for b in B),
        }
        area["line_axis_compass"] = f"{compass(area['line_axis_deg'])}-{compass(area['line_axis_deg'] + 180)}"
        if has_time:
            area["start_time"] = min(b["start_time"] for b in B)
            area["end_time"] = max(b["end_time"] for b in B)
        areas.append(area)
    return areas, hulls


def compass(deg):
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int(((deg % 360) + 11.25) // 22.5) % 16]


# ============================================================
# STEP 4 - PREDICT THE NEXT LINES OF AN ONGOING SURVEY
# ============================================================

def recent_heading(track: pd.DataFrame, min_km: float = 3.0) -> float:
    """Heading over the last few km of track (steadier than one COG report), else the last COG."""
    la, lo = track["lat"].to_numpy(), track["lon"].to_numpy()
    far = np.flatnonzero(haversine_km(la, lo, la[-1], lo[-1]) >= min_km)
    if len(far):
        return float(bearing_deg(la[far[-1]], lo[far[-1]], la[-1], lo[-1]))
    return float(track["course"].iloc[-1])


def predict_next_lines(summary, geometry, last_line, track: pd.DataFrame, cfg: Config, has_time):
    """
    Project the lines a vessel should run next, assuming it keeps the same
    axis, line length and spacing, and keeps advancing the same way.
    """
    lat0, lon0, u, n, cross, along, overall = geometry["frame"]
    spacing = summary["median_spacing_km"]
    length = geometry["median_line_km"]
    center_along = float(np.median(along))
    speed = summary["survey_speed_kn"]
    speed_kmh = (speed if np.isfinite(speed) else 8.0) * KM_PER_NM

    last_point = track.iloc[-1]
    tx, ty = to_local_xy(track["lat"].to_numpy(), track["lon"].to_numpy(), lat0, lon0)
    pos = np.array([tx[-1], ty[-1]])

    # current motion: the reported course at the last fix if there is one
    # (it catches a turn the positions don't show yet), else the track itself
    course = last_point.get("course", np.nan)
    if np.isfinite(course):
        motion = np.array([math.sin(math.radians(course)), math.cos(math.radians(course))])
    else:
        back = np.hypot(tx - tx[-1], ty - ty[-1])
        far = np.flatnonzero(back >= 3.0)
        motion = pos - np.array([tx[far[-1]], ty[far[-1]]]) if len(far) else np.zeros(2)
    norm = np.linalg.norm(motion)
    on_a_line = norm > 0 and abs(motion @ u) / norm >= math.cos(math.radians(30))

    rows, hours = [], 0.0
    if on_a_line:
        running = float(np.sign(motion @ u))
        cur_cross = float(pos @ n)
        end_along = center_along + running * length / 2
        remaining = max(0.0, (end_along - pos @ u) * running)
        direction = -running
        here = pos
        if remaining >= 0.5:   # otherwise the line is done and the ship is about to turn
            hours = remaining / speed_kmh
            here = u * end_along + n * cur_cross
            rows.append(_prediction_row(summary, 0, "finish current line", pos, here,
                                        lat0, lon0, 0.0, hours, last_point, has_time, running * u))
    else:
        lx, ly = to_local_xy([last_line["start_lat"], last_line["end_lat"]],
                             [last_line["start_lon"], last_line["end_lon"]], lat0, lon0)
        last_dir = float(np.sign(np.array([lx[1] - lx[0], ly[1] - ly[0]]) @ u)) or 1.0
        cur_cross = float(np.array([lx.mean(), ly.mean()]) @ n)
        direction = -last_dir
        here = pos

    for j in range(1, cfg.predict_lines + 1):
        c = cur_cross + overall * spacing * j
        start = u * (center_along - direction * length / 2) + n * c
        end = u * (center_along + direction * length / 2) + n * c
        start_h = hours + float(np.linalg.norm(start - here)) / speed_kmh
        hours = start_h + length / speed_kmh
        rows.append(_prediction_row(summary, j, "next line", start, end,
                                    lat0, lon0, start_h, hours, last_point, has_time,
                                    direction * u))
        direction, here = -direction, end
    return rows


def _prediction_row(summary, j, kind, start, end, lat0, lon0, h0, h1, last_point, has_time, vec):
    slat, slon = from_local_xy(start[0], start[1], lat0, lon0)
    elat, elon = from_local_xy(end[0], end[1], lat0, lon0)
    row = {
        "vessel": summary["vessel"],
        "block_id": summary["block_id"],
        "line_ahead": j,
        "kind": kind,
        "start_lat": round(float(slat), 5), "start_lon": round(float(slon), 5),
        "end_lat": round(float(elat), 5), "end_lon": round(float(elon), 5),
        "heading_deg": round(float(xy_heading(vec[0], vec[1])), 1),
        "hours_after_last_fix_start": round(h0, 1),
        "hours_after_last_fix_end": round(h1, 1),
    }
    if has_time:
        t = last_point["time"]
        row["est_start_utc"] = t + pd.Timedelta(hours=h0)
        row["est_end_utc"] = t + pd.Timedelta(hours=h1)
    return row


# ============================================================
# PER-VESSEL PIPELINE
# ============================================================

def detect_surveys(vessel, v: pd.DataFrame, cfg: Config) -> dict:
    """Clean one vessel's track, find its straight lines, survey blocks and areas, and predict."""
    has_time = bool(v["time"].notna().mean() > 0.9)
    v, order_note = order_track(v, has_time)
    v = clean_track(v, cfg, has_time)

    kept = v[v["status"] == "kept"]
    p = kept.reset_index().rename(columns={"index": "vid"})
    v["chunk"] = -1
    v["line_id"] = -1
    v["block_id"] = -1

    result = {"vessel": vessel, "order": order_note, "has_time": has_time,
              "positions": v, "lines": [], "blocks": [], "predictions": [],
              "areas": [], "area_hulls": {},
              "geometry": {}, "track": list(zip(kept["lon"], kept["lat"]))}
    v["area_id"] = -1
    if len(p) < 3:
        return result

    p["chunk"] = split_chunks(p, cfg, has_time)
    v.loc[p["vid"], "chunk"] = p["chunk"].to_numpy()

    lines = []
    for _, g in p.groupby("chunk", sort=True):
        offset = g.index[0]
        for a, b in lines_in_chunk(g["lat"].to_numpy(), g["lon"].to_numpy(), cfg):
            info = describe_line(p, a + offset, b + offset, has_time)
            if info["length_km"] >= cfg.min_line_km and info["n_points"] >= cfg.min_points_per_line:
                info["chunk"] = int(g["chunk"].iloc[0])
                lines.append(info)

    for i, l in enumerate(lines, 1):
        l["vessel"] = vessel
        l["line_id"] = i
        l["block_id"] = -1
        l["role"] = "not in a survey block"
        l["trimmed_km"] = 0.0

    blocks, skipped = find_blocks(lines, cfg)
    blocks = [b for b in (trim_block_ends(b, lines, p, cfg, has_time) for b in blocks)
              if len(b) >= cfg.min_lines_per_block]
    for k in skipped:
        lines[k]["role"] = "skipped inside block (cross-line?)"
    for l in lines:
        v.loc[p.loc[l["i0"]:l["i1"], "vid"], "line_id"] = l["line_id"]

    for bid, idx in enumerate(blocks, 1):
        summary, geom = summarize_block(vessel, bid, idx, lines, cfg, has_time)
        for i in idx:
            lines[i]["block_id"] = bid
            lines[i]["role"] = "survey line"
            v.loc[p.loc[lines[i]["i0"]:lines[i]["i1"], "vid"], "block_id"] = bid

        last_line = lines[idx[-1]]
        last_point = p.iloc[-1]
        # still surveying = the last fix is in the block, turning at its edge, or
        # already running the next line parallel to the others; a ship heading
        # off at an angle has finished and is transiting away
        spacing = summary["median_spacing_km"]
        gap_km = hull_distance_km([(last_point["lon"], last_point["lat"])], geom["hull"])
        parallel = axis_diff(recent_heading(p), summary["line_axis_deg"]) <= 10.0
        ongoing = (idx[-1] == len(lines) - 1
                   and (gap_km <= max(spacing, 1.0) or (parallel and gap_km <= 2 * spacing)))
        summary["status"] = "IN PROGRESS at last fix" if ongoing else "completed"
        if ongoing and summary["progression_frac"] >= cfg.min_progression_for_prediction:
            result["predictions"] += predict_next_lines(summary, geom, last_line,
                                                        p, cfg, has_time)
        result["blocks"].append(summary)
        result["geometry"][bid] = geom

    result["areas"], result["area_hulls"] = group_survey_areas(
        vessel, result["blocks"], result["geometry"], cfg, has_time)
    area_of = {b["block_id"]: b["area_id"] for b in result["blocks"]}
    for l in lines:
        l["area_id"] = area_of.get(l["block_id"], -1)
    v["area_id"] = v["block_id"].map(area_of).fillna(-1).astype(int)
    result["lines"] = lines
    return result


def process_vessel(args):
    """Survey detection for one vessel of one dataset, plus its identity and nearby ports."""
    dataset, vessel, v, cfg = args
    result = detect_surveys(vessel, v, cfg)
    result["dataset"] = dataset
    for key in ("lines", "blocks", "areas", "predictions"):
        result[key] = [{"dataset": dataset, **row} for row in result[key]]
    result["info"] = info = vessel_info(result)
    info["port_country"], info["port_country_basis"] = port_match_country(info, cfg)
    result["nearby_ports"] = survey_ports(result, info["port_country"])
    return result


def port_match_country(info: dict, cfg: Config):
    """(country whose ports to list, why) for a vessel, from cfg.port_country."""
    mode = str(cfg.port_country).strip()
    if mode.lower() in ("none", "off", "no"):
        return "", ""
    if mode.lower() in ("flag", ""):
        country = port_intel.normalize_country(info["flag"])
        source = {"data": "flag in data", "MMSI": "flag from MMSI", "entered": "flag entered",
                  "library": "flag from ship library"}.get(info["flag_source"], "flag")
        return country, (source if country else "")
    return port_intel.normalize_country(mode) or mode, "chosen country"


def survey_ports(result: dict, country: str = "") -> list:
    """Nearest ports, rare-earth linked ports and (with `country`) that country's ports per survey area."""
    rows = []
    for a in result["areas"]:
        ports = port_intel.nearby_ports(a["centroid_lat"], a["centroid_lon"], country=country)
        rank = {}
        for port in ports:
            rank[port["category"]] = rank.get(port["category"], 0) + 1
            rows.append({"dataset": result["dataset"], "vessel": result["vessel"],
                         "area_id": a["area_id"], "area_status": a["status"],
                         "rank": rank[port["category"]], **port})
    return rows


def _most_common(series: pd.Series) -> str:
    values = series.dropna().astype(str).str.strip()   # NaN where another dataset had the column
    values = values[(values != "") & (values.str.lower() != "nan")]
    return str(values.mode().iloc[0]) if len(values) else ""


def vessel_info(result: dict) -> dict:
    """Identity and track summary for vessels.csv and the web app."""
    pos = result["positions"]
    kept = pos[pos["status"] == "kept"]
    info = {key: _most_common(pos[f"info_{key}"]) if f"info_{key}" in pos else ""
            for key in INFO_ALIASES}
    vessel = str(result["vessel"])
    mmsi = info["mmsi"] or (vessel if re.fullmatch(r"\d{9}", vessel) else "")
    mmsi = mmsi.split(".")[0]
    flag = info["flag"] or port_intel.flag_from_mmsi(mmsi)

    track_km = 0.0
    if len(kept) > 1:
        la, lo, ch = kept["lat"].to_numpy(), kept["lon"].to_numpy(), kept["chunk"].to_numpy()
        steps = haversine_km(la[:-1], lo[:-1], la[1:], lo[1:])
        track_km = float(steps[ch[:-1] == ch[1:]].sum())   # no jumps across reporting gaps

    areas = result["areas"]
    live = any(a["status"].startswith("IN") for a in areas)
    last = kept.iloc[-1] if len(kept) else None
    has_time = result["has_time"]
    return {
        "dataset": result["dataset"],
        "vessel": vessel,
        "name": info["name"],
        "mmsi": mmsi,
        "imo": info["imo"].split(".")[0],
        "callsign": info["callsign"],
        "flag": flag,
        "flag_source": ((_most_common(pos["flag_entered"]) or "data") if "flag_entered" in pos
                        else "data") if info["flag"] else ("MMSI" if flag else ""),
        "ship_type": port_intel.ship_type_name(info["ship_type"]) if info["ship_type"] else "",
        "length_m": info["length_m"],
        "width_m": info["width_m"],
        "draught_m": info["draught_m"],
        "destination": info["destination"],
        "survey_status": "Surveying now" if live else "Survey found" if areas else "No survey pattern",
        "survey_areas": len(areas),
        "survey_blocks": len(result["blocks"]),
        "positions": len(pos),
        "positions_used": len(kept),
        "track_km": round(track_km, 1),
        "first_time": kept["time"].iloc[0] if has_time and len(kept) else pd.NaT,
        "last_time": kept["time"].iloc[-1] if has_time and len(kept) else pd.NaT,
        "last_lat": round(float(last["lat"]), 5) if last is not None else np.nan,
        "last_lon": round(float(last["lon"]), 5) if last is not None else np.nan,
        "last_speed_kn": last["speed_kn"] if last is not None else np.nan,
        "last_course": last["course"] if last is not None else np.nan,
        "ordered_by": result["order"],
        "marinetraffic_url": (f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}"
                              if mmsi else ""),
    }


def dataset_sources(sources) -> dict:
    """
    {dataset name: [paths]}. A dict is used as given; otherwise each file or
    folder in the list is its own dataset, named after it.
    """
    if isinstance(sources, dict):
        return {str(k): [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
                for k, v in sources.items()}
    out = {}
    for src in sources:
        name = Path(src).stem or str(src)
        base, i = name, 2
        while name in out:
            name, i = f"{base} ({i})", i + 1
        out[name] = [str(src)]
    return out


def run(sources, cfg: Config, out_dir: Path, workers: int = 1, make_maps: bool = True,
        identities=None):
    """identities: vessel details to fill in, see apply_identities()."""
    frames, notes = [], []
    for name, paths in dataset_sources(sources).items():
        data, n = load_inputs(paths)
        frames.append(data.assign(dataset=name))
        notes += n
    data = apply_identities(pd.concat(frames, ignore_index=True), identities)
    # drop "flag is unknown" from files whose identity has now been given
    known = set()
    for col in ("info_mmsi", "info_flag"):
        if col in data:
            known |= set(data.loc[data[col].fillna("").astype(str).str.strip() != "", "source_file"])
    unknown_tail = ", and its flag is unknown"
    notes = [n.split(unknown_tail)[0] + "." if unknown_tail in n and n.split(":")[0] in known else n
             for n in notes]
    order = {name: i for i, name in enumerate(data["dataset"].unique())}
    jobs = sorted(((ds, vid, g.copy(), cfg)
                   for (ds, vid), g in data.groupby(["dataset", "vessel"], sort=False)),
                  key=lambda j: (order[j[0]], str(j[1])))

    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(process_vessel, jobs))
    else:
        results = [process_vessel(j) for j in jobs]

    out_dir.mkdir(parents=True, exist_ok=True)
    write_outputs(results, out_dir, make_maps)
    return results, notes, len(data)


# ============================================================
# OUTPUTS
# ============================================================

LINE_COLUMNS = ["dataset", "vessel", "line_id", "area_id", "block_id", "role", "n_points", "length_km",
                "trimmed_km", "path_km", "straightness", "heading_deg", "axis_deg", "median_speed_kn",
                "start_lat", "start_lon", "end_lat", "end_lon", "start_time", "end_time",
                "start_row_in_file", "end_row_in_file", "chunk"]


def write_outputs(results, out_dir: Path, make_maps: bool):
    positions = pd.concat([r["positions"] for r in results], ignore_index=True)
    positions.drop(columns=["time"] if positions["time"].isna().all() else [], inplace=True)
    positions.insert(0, "dataset", positions.pop("dataset"))
    positions.to_csv(out_dir / "positions_processed.csv", index=False)

    vessels = pd.DataFrame([r["info"] for r in results])
    vessels.dropna(axis=1, how="all").to_csv(out_dir / "vessels.csv", index=False)

    ports = pd.DataFrame([{k: v for k, v in row.items() if k != "route"}
                          for r in results for row in r["nearby_ports"]])
    ports.to_csv(out_dir / "nearby_ports.csv", index=False)

    lines = pd.DataFrame([l for r in results for l in r["lines"]])
    if len(lines):
        lines = lines[[c for c in LINE_COLUMNS if c in lines]].round(4)
    lines.to_csv(out_dir / "survey_lines.csv", index=False)

    blocks = pd.DataFrame([b for r in results for b in r["blocks"]])
    if len(blocks):
        blocks = blocks.sort_values(["score"], ascending=False)
        blocks = blocks.dropna(axis=1, how="all")
    blocks.to_csv(out_dir / "survey_blocks.csv", index=False)

    areas = pd.DataFrame([a for r in results for a in r["areas"]])
    areas.to_csv(out_dir / "survey_areas.csv", index=False)

    preds = pd.DataFrame([p for r in results for p in r["predictions"]])
    preds.to_csv(out_dir / "predictions.csv", index=False)

    write_geojson(results, out_dir / "survey_results.geojson")
    if make_maps:
        write_maps(results, out_dir / "maps")


def _clean_props(d):
    out = {}
    for k, val in d.items():
        if k in ("i0", "i1", "route"):
            continue
        if isinstance(val, pd.Timestamp):
            val = val.isoformat()
        elif isinstance(val, (np.floating, float)):
            val = None if not np.isfinite(val) else round(float(val), 5)
        elif isinstance(val, np.integer):
            val = int(val)
        elif val is pd.NaT:
            val = None
        out[k] = val
    return out


def simplified_track(track, tolerance_km: float = 0.2):
    """[[lon, lat], ...] of a track thinned with RDP: lighter, same shape."""
    if len(track) < 2:
        return [list(map(float, t)) for t in track]
    lon, lat = np.array(track).T
    x, y = to_local_xy(lat, lon, float(np.median(lat)), float(np.median(lon)))
    return [[float(lon[i]), float(lat[i])] for i in rdp_vertices(x, y, tolerance_km)]


def write_geojson(results, path: Path):
    feats = []
    for r in results:
        if len(r["track"]) > 1:
            feats.append({"type": "Feature",
                          "properties": {"layer": "track", "dataset": r["dataset"],
                                         "vessel": r["vessel"]},
                          "geometry": {"type": "LineString",
                                       "coordinates": simplified_track(r["track"])}})
        for a in r["areas"]:
            ring = r["area_hulls"][a["area_id"]]
            if len(ring) >= 3:
                feats.append({"type": "Feature",
                              "properties": {"layer": "survey_area", **_clean_props(a)},
                              "geometry": {"type": "Polygon",
                                           "coordinates": [[list(map(float, p)) for p in ring + ring[:1]]]}})
        for b in r["blocks"]:
            ring = r["geometry"][b["block_id"]]["hull"]
            if len(ring) >= 3:
                feats.append({"type": "Feature",
                              "properties": {"layer": "survey_block", **_clean_props(b)},
                              "geometry": {"type": "Polygon",
                                           "coordinates": [[list(map(float, p)) for p in ring + ring[:1]]]}})
        for l in r["lines"]:
            feats.append({"type": "Feature",
                          "properties": {"layer": "line", **_clean_props(l)},
                          "geometry": {"type": "LineString",
                                       "coordinates": [[l["start_lon"], l["start_lat"]],
                                                       [l["end_lon"], l["end_lat"]]]}})
        for p in r["predictions"]:
            feats.append({"type": "Feature",
                          "properties": {"layer": "predicted_line", **_clean_props(p)},
                          "geometry": {"type": "LineString",
                                       "coordinates": [[p["start_lon"], p["start_lat"]],
                                                       [p["end_lon"], p["end_lat"]]]}})
        for port in r["nearby_ports"]:
            feats.append({"type": "Feature",
                          "properties": {"layer": "nearby_port", **_clean_props(port)},
                          "geometry": {"type": "Point", "coordinates": [port["lon"], port["lat"]]}})
            if port.get("route"):
                feats.append({"type": "Feature",
                              "properties": {"layer": "port_route", "dataset": port["dataset"],
                                             "vessel": port["vessel"], "area_id": port["area_id"],
                                             "port": port["port"], "sea_km": port["sea_km"],
                                             "distance_basis": port["distance_basis"],
                                             "ree_link": port["ree_link"],
                                             "category": port["category"],
                                             "match_country": port["match_country"]},
                              "geometry": {"type": "LineString", "coordinates": port["route"]}})
    for site in port_intel.load_ree_sites().to_dict("records"):
        feats.append({"type": "Feature",
                      "properties": {"layer": "ree_site", **_clean_props(site)},
                      "geometry": {"type": "Point", "coordinates": [site["lon"], site["lat"]]}})
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))

    # one file per layer for GIS imports (the ArcGIS toolbox reads these)
    folder = path.parent / "arcgis"
    folder.mkdir(exist_ok=True)
    for layer, stem in ARCGIS_LAYERS.items():
        part = [f for f in feats if f["properties"]["layer"] == layer]
        if part:
            (folder / f"{stem}.geojson").write_text(
                json.dumps({"type": "FeatureCollection", "features": part}))


# GeoJSON layer -> per-layer file name in survey_results/arcgis/
ARCGIS_LAYERS = {"track": "vessel_tracks", "survey_area": "survey_areas",
                 "survey_block": "survey_blocks", "line": "survey_lines",
                 "predicted_line": "predicted_lines", "port_route": "port_routes",
                 "nearby_port": "nearby_ports", "ree_site": "ree_sites"}


def map_filename(dataset, vessel) -> str:
    """maps/<dataset>__<vessel>.png, with characters unsafe in file names replaced."""
    def safe(x):
        return re.sub(r"[^A-Za-z0-9_.-]", "_", str(x))
    return f"{safe(dataset)}__{safe(vessel)}.png"


def write_maps(results, folder: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    folder.mkdir(exist_ok=True)
    palette = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf",
               "#8c564b", "#e377c2", "#bcbd22", "#7f7f7f"]

    for r in results:
        if len(r["track"]) < 2:
            continue
        lon, lat = np.array(r["track"]).T
        lat0, lon0 = float(np.median(lat)), float(np.median(lon))
        proj = lambda la, lo: to_local_xy(la, lo, lat0, lon0)
        x, y = proj(lat, lon)

        zoom = r["areas"] and len(r["area_hulls"]) > 0
        fig, axes = plt.subplots(1, 2 if zoom else 1, figsize=(18 if zoom else 10, 9))
        axes = np.atleast_1d(axes)

        for ax in axes:
            ax.plot(x, y, color="0.75", lw=0.7, zorder=1, label="track (cleaned)")
            for l in r["lines"]:
                lx, ly = proj([l["start_lat"], l["end_lat"]], [l["start_lon"], l["end_lon"]])
                if l["block_id"] > 0:
                    ax.annotate("", xy=(lx[1], ly[1]), xytext=(lx[0], ly[0]), zorder=3,
                                arrowprops=dict(arrowstyle="-|>", lw=1.4,
                                                color=palette[(l["block_id"] - 1) % len(palette)]))
                else:
                    ax.plot(lx, ly, color="0.45", lw=0.9, ls=":", zorder=2)
            for b in r["blocks"]:
                ring = r["geometry"][b["block_id"]]["hull"]
                if len(ring) >= 3:
                    hx, hy = proj([p[1] for p in ring + ring[:1]], [p[0] for p in ring + ring[:1]])
                    col = palette[(b["block_id"] - 1) % len(palette)]
                    ax.fill(hx, hy, color=col, alpha=0.10, zorder=0,
                            label=f"block {b['block_id']}: {b['n_lines']} lines, "
                                  f"{b['median_spacing_km']:.1f} km spacing ({b['confidence']})")
            for a in r["areas"]:
                ring = r["area_hulls"][a["area_id"]]
                if len(ring) >= 3:
                    hx, hy = proj([p[1] for p in ring + ring[:1]], [p[0] for p in ring + ring[:1]])
                    ax.plot(hx, hy, color="k", lw=1.2, ls="-.", zorder=2)
                    ax.text(hx.max(), hy.max(), f" AREA {a['area_id']}", fontsize=9,
                            fontweight="bold", va="bottom")
            for p in r["predictions"]:
                px, py = proj([p["start_lat"], p["end_lat"]], [p["start_lon"], p["end_lon"]])
                ax.annotate("", xy=(px[1], py[1]), xytext=(px[0], py[0]), zorder=4,
                            arrowprops=dict(arrowstyle="-|>", lw=1.8, ls="--", color="black"))
            ax.scatter([x[-1]], [y[-1]], s=110, marker="*", color="gold", edgecolor="k",
                       zorder=5, label="last fix")
            ax.set_aspect("equal")
            ax.set_xlabel("km east of track centre")
            ax.set_ylabel("km north of track centre")
            ax.grid(alpha=0.3)

        axes[0].plot([], [], color="0.45", ls=":", label="straight line, not in a block")
        if r["areas"]:
            axes[0].plot([], [], "k-.", label="survey area outline")
        if r["predictions"]:
            axes[0].plot([], [], "k--", label="predicted next lines")
        axes[0].set_title(f"{r['dataset']} / {r['vessel']}: whole track")
        axes[0].legend(loc="best", fontsize=7)

        if zoom:
            pts = [p for ring in r["area_hulls"].values() for p in ring]
            pts += [(p[k + "_lon"], p[k + "_lat"]) for p in r["predictions"] for k in ("start", "end")]
            zx, zy = proj([p[1] for p in pts], [p[0] for p in pts])
            pad = 5
            axes[1].set_xlim(zx.min() - pad, zx.max() + pad)
            axes[1].set_ylim(zy.min() - pad, zy.max() + pad)
            axes[1].set_title("survey areas (zoomed); arrows show direction of travel")

        fig.tight_layout()
        fig.savefig(folder / map_filename(r["dataset"], r["vessel"]), dpi=110)
        plt.close(fig)


# ============================================================
# COMMAND LINE
# ============================================================

def build_parser():
    ap = argparse.ArgumentParser(description="Detect lawnmower survey patterns in vessel tracks.")
    ap.add_argument("paths", nargs="*", help="data files and/or folders")
    ap.add_argument("--out", default="survey_results", help="output folder")
    ap.add_argument("--workers", type=int, default=1, help="process vessels in parallel")
    ap.add_argument("--no-maps", action="store_true")
    for f in fields(Config):
        flag = "--" + f.name.replace("_", "-")
        if f.type in ("bool", bool):
            ap.add_argument(flag, type=lambda s: s.lower() in ("1", "true", "yes"), default=None)
        else:
            ap.add_argument(flag, type=type(f.default), default=None)
    return ap


def main():
    args = build_parser().parse_args()
    paths = args.paths
    if not paths:  # convenience: behave like v1 when run with no arguments
        here = Path(__file__).resolve().parent
        paths = [str(p) for p in sorted(here.glob("MarineTraffic*.csv"))]
        if not paths:
            build_parser().error("give one or more data files or folders")

    cfg = Config(**{f.name: getattr(args, f.name) for f in fields(Config)
                    if getattr(args, f.name) is not None})

    print("=" * 64)
    print("SURVEY PATTERN DETECTOR  v3")
    print("=" * 64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        results, notes, n_rows = run(paths, cfg, Path(args.out), args.workers, not args.no_maps)

    print(f"Positions read : {n_rows:,}")
    print(f"Datasets       : {len({r['dataset'] for r in results})}")
    print(f"Vessels        : {len(results)}")
    for n in notes:
        print(f"NOTE: {n}")

    for r in results:
        st = r["positions"]["status"].value_counts().to_dict()
        print("\n" + "-" * 64)
        info = r["info"]
        ident = ", ".join(x for x in (info["name"], f"MMSI {info['mmsi']}" if info["mmsi"] else "",
                                       info["flag"], info["ship_type"]) if x)
        print(f"VESSEL {r['vessel']}  [{r['dataset']}]" + (f"  {ident}" if ident else ""))
        print(f"  ordered by   : {r['order']}")
        print(f"  points       : " + ", ".join(f"{k} {v}" for k, v in st.items()))
        print(f"  straight lines: {len(r['lines'])}")
        if not r["blocks"]:
            print("  no lawnmower survey blocks found")
        for a in r["areas"]:
            print(f"  SURVEY AREA {a['area_id']}  {a['status']}  "
                  f"({a['n_blocks']} blocks, {a['n_lines']} lines, best confidence {a['best_confidence']})")
            print(f"     axis {a['line_axis_compass']}, spacing ~{a['median_spacing_km']} km, "
                  f"{a['total_line_km']} line-km over ~{a['area_km2']} km2, "
                  f"centre {a['centroid_lat']}, {a['centroid_lon']}")
        for b in r["blocks"]:
            print(f"  BLOCK {b['block_id']}  [{b['confidence']} confidence, score {b['score']}]  {b['status']}")
            print(f"     {b['n_lines']} lines, axis {b['line_axis_compass']}, "
                  f"spacing {b['median_spacing_km']} km (cv {b['spacing_cv']}), "
                  f"lines ~{b['mean_line_km']} km")
            print(f"     {b['pattern']}, advancing toward {b['advancing_toward']}, "
                  f"area ~{b['area_km2']} km2, centre {b['centroid_lat']}, {b['centroid_lon']}")
        for p in r["predictions"]:
            print(f"  -> {p['kind']:20s} {p['start_lat']:.4f},{p['start_lon']:.4f} -> "
                  f"{p['end_lat']:.4f},{p['end_lon']:.4f}  "
                  f"(+{p['hours_after_last_fix_start']}h to +{p['hours_after_last_fix_end']}h)")
        for a in r["areas"]:
            near = [x for x in r["nearby_ports"] if x["area_id"] == a["area_id"]]
            first = next((x for x in near if x["category"] == "nearest port"), None)
            linked = [x for x in near if x["ree_link"] and not x["match_country"]][:3]
            home = [x for x in near if x["match_country"]]
            if first:
                print(f"  AREA {a['area_id']} nearest port: {first['port']} ({first['country']}), "
                      f"{first['sea_km']:.0f} km by {first['distance_basis']}")
            if home:
                print(f"     ports in {home[0]['match_country']} ({info['port_country_basis']}): " + "; ".join(
                    f"{x['port']} {x['sea_km']:.0f} km" + (" [rare-earth link]" if x["ree_link"] else "")
                    for x in home[:3] + [x for x in home[3:] if x["ree_link"]][:1]))
            for x in linked:
                print(f"     rare-earth link (any country): {x['port']} ({x['country']}), {x['sea_km']:.0f} km - "
                      f"{x['ree_link'].lower()}" + (f": {x['ree_site']} {x['ree_site_km']:.0f} km"
                                                   if x["ree_site"] else ""))

    print(f"\nResults written to: {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
