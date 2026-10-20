"""
SURVEY DETECTOR WEB APP  (v3)
=============================

Upload AIS exports; every file is a dataset. All datasets are drawn on one
interactive world map with switchable layers (tracks, survey patterns,
predicted lines and vessel positions per dataset, plus ports and rare-earth
sites), and each vessel gets an information panel with the ports nearest its
survey, highlighting those linked to rare-earth processing.

    streamlit run app.py            # run locally at http://localhost:8501

Detection: survey_pattern_detector.py. Ports and rare-earth sites:
port_intel.py with reference_data/.
"""

from __future__ import annotations

import html
import io
import json
import re
import tempfile
import warnings
import zipfile
from dataclasses import asdict
from pathlib import Path

import folium
from branca.element import MacroElement
from jinja2 import Template
import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from folium.plugins import FastMarkerCluster, GroupedLayerControl

import port_intel
import survey_pattern_detector as spd

HERE = Path(__file__).resolve().parent
LIBRARY = HERE / "library"          # vessels.csv catalog + tracks/ position files
UPLOAD_TYPES = ["csv", "txt", "tsv", "xlsx", "xlsm"]
TABLES = ["vessels", "nearby_ports", "survey_areas", "survey_blocks", "survey_lines",
          "predictions", "positions_processed"]

SETTINGS = {
    "Cleaning": {
        "min_speed_kn": "Points slower than this are loitering/drifting and dropped (knots).",
        "min_step_km": "A point closer than this to the previous one counts as stationary.",
        "max_implied_speed_kn": "Faster than this between fixes = bad position.",
        "max_gap_hours": "Split the track at reporting gaps longer than this.",
        "max_gap_km_no_time": "Without timestamps, split the track at jumps longer than this.",
    },
    "Straight lines": {
        "rdp_tolerance_km": "How far a track may wander and still count as straight.",
        "min_segment_km": "Shorter pieces are treated as turns.",
        "merge_heading_tol_deg": "Pieces within this heading of each other can join into one line.",
        "merge_offset_km": "Pieces this close to one line get merged.",
        "merge_gap_km": "...across at most this much turn or dog-leg.",
        "min_line_km": "Shortest straight line counted as a survey line.",
        "min_points_per_line": "Fewest position reports a line may have.",
    },
    "Lawnmower pattern": {
        "antiparallel_tol_deg": "Next line must run within this of the opposite direction.",
        "block_axis_tol_deg": "Every line must stay within this of the block's axis.",
        "min_spacing_km": "Narrowest gap between adjacent lines.",
        "max_spacing_km": "Widest gap between adjacent lines.",
        "min_overlap_frac": "Adjacent lines must cover at least this share of the same stretch.",
        "min_length_ratio": "Shorter line / longer line, for adjacent lines.",
        "max_connector_km": "Longest turn between the end of one line and the next.",
        "max_skip_lines": "Odd lines (cross-lines, turn pieces) tolerated inside a block.",
        "min_lines_per_block": "Fewest lines that make a survey block.",
        "area_join_km": "Blocks this close with the same axis form one survey area.",
    },
    "Prediction": {
        "predict_lines": "How many upcoming lines to project.",
        "min_progression_for_prediction": "Only predict when at least this share of steps advance the same way.",
    },
}

# one colour per dataset; red/orange/gold are kept for rare-earth ports, magenta for predictions
DATASET_COLORS = ["#2f6fdf", "#16a34a", "#8e44ad", "#0e9aa7", "#8d5a3b", "#d63384",
                  "#5c6f82", "#9aa20f"]
PREDICTION_COLOR = "#ff2fb3"
LINK_COLORS = {"Known rare-earth cargo port": "#d62728",
               "Near rare-earth processing site": "#f28e2b",
               "Near rare-earth mine": "#c9a227"}
PLAIN_PORT_COLOR = "#3b5b7d"
FLAG_PORT_COLOR = "#ffc400"     # ring around ports of the vessel's flag state / chosen country
FLAG_CATEGORIES = ("flag-state port", "flag-state rare-earth linked port")
SITE_STYLES = {  # type -> (colour, marker sides (4 diamond, 3 triangle), layer name)
    "processing": ("#7b1fa2", 4, "Processing & separation plants"),
    "magnet": ("#c2185b", 4, "Magnet & metal plants"),
    "mine": ("#6d4c41", 3, "Mines & deposits"),
    "logistics port": ("#d62728", 4, "Rare-earth cargo ports"),
}


# Seabed mining areas. ISA boundaries are not stored in this project: the map loads
# them in the viewer's browser from the ISA's own public map service (the data
# behind isa.org.jm/deepdata-database/maps), credited to the ISA; its terms allow
# personal, non-commercial use and ask for written permission for republishing.
ISA_SERVICE = ("https://services5.arcgis.com/VcAAb5oBhdAAnFj2/arcgis/rest/services/"
               "fclContractAreas_20240724/FeatureServer")
ISA_LAYERS = {"active": 32, "reserved": 33, "relinquished": 34, "apei": 35}
CCZ_OUTLINE = ("https://geo.vliz.be/geoserver/MarineRegions/wfs?service=WFS&version=1.0.0"
               "&request=GetFeature&typeName=MarineRegions:isa_ccz_managementarea"
               "&outputFormat=application/json")
NATIONAL_SEABED = HERE / "reference_data" / "national_seabed_areas.geojson"
# ContractID -> (contractor, sponsoring state), from the ISA's per-contractor layers
ISA_CONTRACTORS = {
    "UKSRLPMN1": ("Loke CCZ (formerly UK Seabed Resources)", "United Kingdom"),
    "UKSRLPMN2": ("Loke CCZ (formerly UK Seabed Resources)", "United Kingdom"),
    "CIICPMN1": ("Cook Islands Investment Corporation (CIIC)", "Cook Islands"),
    "COMRAPMN1": ("China Ocean Mineral Resources R&D Association (COMRA)", "China"),
    "COMRAPMS1": ("China Ocean Mineral Resources R&D Association (COMRA)", "China"),
    "COMRACRFC1": ("China Ocean Mineral Resources R&D Association (COMRA)", "China"),
    "BPHDCPMN1": ("Beijing Pioneer Hi-Tech Development Corporation", "China"),
    "CMMPMN1": ("China Minmetals Corporation", "China"),
    "BrazilCRFC1": ("Companhia de Pesquisa de Recursos Minerais (CPRM)", "Brazil"),
    "IOMPMN1": ("Interoceanmetal Joint Organization (IOM)",
                "Bulgaria, Cuba, Czechia, Poland, Russia, Slovakia"),
    "POLPMS1": ("Government of the Republic of Poland", "Poland"),
    "BMJPMN1": ("Blue Minerals Jamaica Ltd", "Jamaica"),
    "GSRPMN1": ("Global Sea Mineral Resources NV (GSR)", "Belgium"),
    "IFREMERPMN1": ("IFREMER", "France"), "IFREMERPMS1": ("IFREMER", "France"),
    "KOREAPMN1": ("Government of the Republic of Korea", "South Korea"),
    "KOREAPMS1": ("Government of the Republic of Korea", "South Korea"),
    "KOREACRFC1": ("Government of the Republic of Korea", "South Korea"),
    "OMSPMN1": ("Ocean Mineral Singapore Pte Ltd", "Singapore"),
    "RUSFEDPMS1": ("Government of the Russian Federation", "Russia"),
    "RUSMNRCRFC1": ("Ministry of Natural Resources and Environment of Russia", "Russia"),
    "RUSMNCRFC1": ("Ministry of Natural Resources and Environment of Russia", "Russia"),
    "YUZHPMN1": ("JSC Yuzhmorgeologiya", "Russia"),
    "TOMLPMN1": ("Tonga Offshore Mining Limited", "Tonga"),
    "NORIPMN1": ("Nauru Ocean Resources Inc. (NORI)", "Nauru"),
    "MARAWAPMN1": ("Marawa Research and Exploration Ltd", "Kiribati"),
    "JOGMECCRFC1": ("Japan Organization for Metals and Energy Security (JOGMEC)", "Japan"),
    "DORDPMN1": ("Deep Ocean Resources Development Co. (DORD)", "Japan"),
    "IndiaPMN1": ("Government of India", "India"), "IndiaPMS1": ("Government of India", "India"),
    "BGRPMN1": ("Federal Institute for Geosciences and Natural Resources (BGR)", "Germany"),
    "BGRPMS1": ("Federal Institute for Geosciences and Natural Resources (BGR)", "Germany"),
}
MINERAL_COLORS = {"nodules": "#1e88e5", "sulphides": "#ef6c00", "crusts": "#8d6e63"}
CHINA_OUTLINE = "#d50000"


class LiveSeabedAreas(MacroElement):
    """
    Fills the given (empty) feature groups in the viewer's browser from the ISA
    service and the Marine Regions CCZ outline, re-centred on longitude c like
    everything else on the map. A layer is fetched the first time it is shown.
    """
    _template = Template("""
{% macro script(this, kwargs) %}
(function () {
  var map = {{ this._parent.get_name() }};
  var c = {{ this.c }};
  var G = {{ this.groups_js }};
  var SERVICE = {{ this.service|tojson }}, LAYERS = {{ this.layers|tojson }};
  var CCZ = {{ this.ccz|tojson }};
  var WHO = {{ this.contractors|tojson }}, COLORS = {{ this.colors|tojson }};
  var CHINA = {{ this.china|tojson }};
  map.attributionControl.addAttribution(
    'Seabed mining areas: <a href="https://www.isa.org.jm/deepdata-database/maps/" target="_blank">' +
    'International Seabed Authority</a>; EEZs: <a href="https://www.marineregions.org" target="_blank">' +
    'Marine Regions</a> (CC BY 4.0)');

  function wrap(lon, ref) { return ref + ((((lon - ref + 180) % 360) + 360) % 360) - 180; }
  function ring(r) {
    var out = [], prev = null;
    for (var i = 0; i < r.length; i++) {
      var lon = wrap(r[i][0], prev === null ? c : prev);
      out.push([lon, r[i][1]]); prev = lon;
    }
    return out;
  }
  function recentre(g) {
    if (!g) return g;
    if (g.type === 'Polygon') g.coordinates = g.coordinates.map(ring);
    else if (g.type === 'MultiPolygon') g.coordinates = g.coordinates.map(function (p) { return p.map(ring); });
    return g;
  }
  function mineral(id) {
    id = String(id || '');
    return /CRFC/i.test(id) ? 'crusts' : /PMS/i.test(id) ? 'sulphides' : 'nodules';
  }
  var NAMES = {nodules: 'Polymetallic nodules', sulphides: 'Polymetallic sulphides',
               crusts: 'Cobalt-rich ferromanganese crusts'};
  function row(k, v) { return (v === undefined || v === null || v === '' || v === ' ' || v === 'NA') ? '' :
    '<tr><td style="color:#666;padding-right:8px">' + k + '</td><td>' + v + '</td></tr>'; }
  function contractPopup(p, kind) {
    var w = WHO[p.ContractID] || [p.ContractID, ''];
    var year = p.ActDate ? new Date(p.ActDate).getUTCFullYear() : '';
    return '<table style="font-size:12px">' + row('Area', '<b>' + kind + '</b>') +
      row('Contractor', w[0]) + row('Sponsoring state', w[1]) + row('Mineral', NAMES[mineral(p.ContractID)]) +
      row('Contract', p.ContractID) + row('Sub-area', p.SubArea) + row('Block', p.BlkCell) +
      row('Size', p.AreaKM2 ? Math.round(Number(p.AreaKM2)).toLocaleString() + ' km²' : '') + row('Since', year) +
      row('Status', p.Status) + row('Source', 'International Seabed Authority') + '</table>';
  }
  function style(kind, p) {
    if (kind === 'apei') return {color: '#2e7d32', weight: 1.5, fillColor: '#66bb6a', fillOpacity: 0.25};
    if (kind === 'relinquished') return {color: '#9e9e9e', weight: 0.8, dashArray: '3 4', fillOpacity: 0.05};
    var col = COLORS[mineral(p.ContractID)], china = (WHO[p.ContractID] || [])[1] === 'China';
    if (kind === 'reserved') return {color: col, weight: 1.2, dashArray: '6 4', fillColor: col, fillOpacity: 0.08};
    return {color: china ? CHINA : col, weight: china ? 2.5 : 1, fillColor: col, fillOpacity: 0.3};
  }

  function fetchAll(url, done, offset, acc) {
    offset = offset || 0; acc = acc || [];
    var page = url.indexOf('/query?') < 0 ? url :
      url + '&resultOffset=' + offset + '&resultRecordCount=1000';
    fetch(page).then(function (r) { return r.json(); }).then(function (d) {
      var f = d.features || [];
      acc = acc.concat(f);
      var more = d.exceededTransferLimit || (d.properties && d.properties.exceededTransferLimit);
      if (url.indexOf('/query?') >= 0 && more && f.length) fetchAll(url, done, offset + f.length, acc);
      else done(acc);
    }).catch(function (e) { console.warn('Seabed layer failed to load', e); done(acc); });
  }
  function isaQuery(id, fields) {   // the APEI layer has no contract fields: asking for them fails
    return SERVICE + '/' + id + '/query?where=1%3D1&outSR=4326&f=geojson&returnGeometry=true' +
      '&maxAllowableOffset=0.01&geometryPrecision=3' +
      '&outFields=' + (fields || 'ContractID,SubArea,BlkCell,AreaType,AreaKM2,ActDate,Status');
  }
  function draw(features, kind, target) {
    features.forEach(function (f) {
      if (!f.geometry) return;
      f.geometry = recentre(f.geometry);
      var p = f.properties || {};
      var tip = kind === 'apei' ? (p.Remarks || 'APEI') + ': protected from mining' :
        ((WHO[p.ContractID] || [p.ContractID])[0] + ' · ' + NAMES[mineral(p.ContractID)] +
         (kind === 'reserved' ? ' (reserved area)' : kind === 'relinquished' ? ' (relinquished)' : ''));
      L.geoJSON(f, {style: function () { return style(kind, p); }})
        .bindTooltip(tip, {sticky: true})
        .bindPopup(kind === 'apei' ?
          '<table style="font-size:12px">' + row('Area', '<b>Area of Particular Environmental Interest</b>') +
          row('Name', p.Remarks) +
          row('Purpose', 'Protected from mining under the CCZ environmental management plan') +
          row('Size', p.AreaKM2 ? Math.round(Number(p.AreaKM2)).toLocaleString() + ' km²' : '') +
          row('Source', 'International Seabed Authority') + '</table>' :
          contractPopup(p, kind === 'reserved' ? 'ISA reserved area' :
                        kind === 'relinquished' ? 'Relinquished area' : 'ISA exploration contract'))
        .addTo(typeof target === 'function' ? target(p) : target);
    });
  }
  function whenShown(groups, load) {   // fetch the first time any of the groups is on the map
    var done = false;
    function go() { if (!done) { done = true; load(); } }
    groups.forEach(function (g) { if (map.hasLayer(g)) go(); else g.once('add', go); });
  }

  whenShown([G.nodules, G.sulphides, G.crusts], function () {
    fetchAll(isaQuery(LAYERS.active), function (f) {
      draw(f, 'active', function (p) { return G[mineral(p.ContractID)]; });
    });
  });
  whenShown([G.reserved], function () { fetchAll(isaQuery(LAYERS.reserved), function (f) { draw(f, 'reserved', G.reserved); }); });
  whenShown([G.apei], function () { fetchAll(isaQuery(LAYERS.apei, 'AreaKM2,Status,Remarks'), function (f) { draw(f, 'apei', G.apei); }); });
  whenShown([G.relinquished], function () {
    fetchAll(isaQuery(LAYERS.relinquished), function (f) { draw(f, 'relinquished', G.relinquished); });
  });
  whenShown([G.ccz], function () {
    fetchAll(CCZ, function (f) {
      f.forEach(function (x) {
        x.geometry = recentre(x.geometry);
        L.geoJSON(x, {style: {color: '#1565c0', weight: 2, dashArray: '10 6', fill: false}})
          .bindTooltip('Clarion-Clipperton Zone management area (ISA)', {sticky: true}).addTo(G.ccz);
      });
    });
  });
})();
{% endmacro %}
""")

    def __init__(self, c: float, groups: dict):
        super().__init__()
        self._name = "LiveSeabedAreas"
        self.c = round(float(c), 6)
        self.groups_js = "{" + ", ".join(f"{k}: {g.get_name()}" for k, g in groups.items()) + "}"
        self.service, self.layers, self.ccz = ISA_SERVICE, ISA_LAYERS, CCZ_OUTLINE
        self.contractors = {k: list(v) for k, v in ISA_CONTRACTORS.items()}
        self.colors, self.china = MINERAL_COLORS, CHINA_OUTLINE


# ============================================================
# RUNNING THE DETECTOR
# ============================================================

@st.cache_data(show_spinner=False, max_entries=6)
def analyse(groups: tuple, settings: dict, identities: tuple = ()) -> dict:
    """
    Run the detector and collect everything the page shows.
    groups: ((dataset name, ((file name, bytes), ...)), ...), one per dataset.
    identities: ((dataset, vessel or None, mmsi, flag, name, source, imo), ...) for vessels
    whose files carry no identity (library catalog entries, the identity form).
    """
    cfg = spd.Config(**settings)
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "input", Path(tmp) / "results"
        src.mkdir()
        sources, i = {}, 0
        for dataset, files in groups:
            for name, data in files:
                path = src / f"{i:03d}" / Path(name).name    # each file keeps its own name
                path.parent.mkdir()
                path.write_bytes(data)
                sources.setdefault(dataset, []).append(str(path))
                i += 1

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            results, notes, n_rows = spd.run(
                sources, cfg, out, workers=1, make_maps=True,
                identities={(ds, vs): {"mmsi": m, "flag": f, "name": n, "source": how, "imo": imo}
                            for ds, vs, m, f, n, how, imo in identities})

        tables = {}
        for key in TABLES:
            try:
                tables[key] = pd.read_csv(out / f"{key}.csv", low_memory=False)
            except (FileNotFoundError, pd.errors.EmptyDataError):
                tables[key] = pd.DataFrame()
        maps = {p.name: p.read_bytes() for p in sorted((out / "maps").glob("*.png"))}
        geojson = json.loads((out / "survey_results.geojson").read_text())
        xlsx = _excel(tables)
        zip_bytes = _zip_folder(out, extra={"survey_results.xlsx": xlsx})
    return {"notes": notes, "n_rows": n_rows, "tables": tables, "maps": maps,
            "geojson": geojson, "zip": zip_bytes, "xlsx": xlsx}


def _excel(tables: dict) -> bytes:
    names = {"vessels": "Vessels", "nearby_ports": "Nearby Ports", "survey_areas": "Survey Areas",
             "survey_blocks": "Survey Blocks", "survey_lines": "Survey Lines",
             "predictions": "Predictions", "positions_processed": "Positions"}
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for key, sheet in names.items():
            tables[key].to_excel(xl, sheet_name=sheet, index=False)
    return buf.getvalue()


def _zip_folder(folder: Path, extra: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, Path("survey_results") / p.relative_to(folder))
        for name, data in extra.items():
            z.writestr(f"survey_results/{name}", data)
    return buf.getvalue()


# ============================================================
# MAP
# ============================================================

def _wrap(lon: float, c: float) -> float:
    """Longitude moved into [c - 180, c + 180), the frame the map is drawn in."""
    return c + ((lon - c + 180) % 360) - 180


def _unwrap(coords, c: float):
    """
    [[lon, lat], ...] -> [[lat, lon], ...] for Leaflet, continuous around central
    longitude c: a line or area crossing the 180° line stays in one piece, on the
    same copy of the world as everything else drawn.
    """
    out = []
    for lon, lat in coords:
        lon = _wrap(lon, out[-1][1] if out else c)
        out.append([lat, lon])
    return out


def _polyline(coords, group, c: float, **style):
    pts = _unwrap(coords, c)
    if len(pts) > 1:
        folium.PolyLine(pts, **style).add_to(group)


def _central_lon(lons) -> float:
    """Circular mean longitude: the middle of a set that may straddle 180°."""
    if not lons:
        return 0.0
    r = np.radians(np.asarray(lons, float))
    return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())))


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan")


def _rows(pairs) -> str:
    """Popup table from (label, value) pairs, skipping empty values."""
    cells = "".join(f"<tr><td style='color:#666;padding-right:8px;vertical-align:top'>"
                    f"{html.escape(str(k))}</td><td>{v}</td></tr>" for k, v in pairs if not _blank(v))
    return f"<table style='font-size:12px'>{cells}</table>"


def _km(x) -> str:
    return "" if _blank(x) else f"{float(x):,.0f} km"


def vessel_label(row) -> str:
    name, vessel = row.get("name") or "", str(row["vessel"])
    if not name or name == vessel or safe_name(name) == vessel:   # library ships: id is the name
        return name or vessel
    return f"{name} ({vessel})"


def _vessel_popup(v) -> str:
    link = (f"<a href='{html.escape(v['marinetraffic_url'])}' target='_blank'>MarineTraffic</a>"
            if v["marinetraffic_url"] else "")
    return _rows([
        ("Vessel", f"<b>{html.escape(vessel_label(v))}</b>"), ("Dataset", html.escape(v["dataset"])),
        ("MMSI", v["mmsi"]), ("IMO", v["imo"]), ("Flag", v["flag"]), ("Type", v["ship_type"]),
        ("Status", f"<b>{v['survey_status']}</b>"),
        ("Last position", f"{v['last_lat']:.4f}, {v['last_lon']:.4f}"),
        ("Last report", v.get("last_time")), ("Track", _km(v.get("track_km"))), ("Links", link)])


def _match(f, ds, vessel=None) -> bool:
    p = f["properties"]
    return str(p.get("dataset")) == ds and (vessel is None or str(p.get("vessel")) == vessel)


def build_map(geojson: dict, vessels: pd.DataFrame, focus: tuple | None) -> folium.Map:
    matched = any(f["properties"]["layer"] == "port_route" and f["properties"].get("category") in FLAG_CATEGORIES
                  for f in geojson["features"])
    feats = geojson["features"]

    keep = set(zip(vessels["dataset"], vessels["vessel"]))   # vessels passing the flag filter

    def layer(name):
        return [f for f in feats if f["properties"]["layer"] == name
                and (name == "ree_site" or (str(f["properties"].get("dataset")),
                                            str(f["properties"].get("vessel"))) in keep)]

    # central longitude: the focused vessel's survey, else all surveys and vessels
    anchor = []
    for f in feats:
        p = f["properties"]
        if p["layer"] not in ("survey_area", "track") or (str(p.get("dataset")), str(p.get("vessel"))) not in keep:
            continue
        if focus is not None and not _match(f, *focus):
            continue
        coords = f["geometry"]["coordinates"]
        anchor += [lon for lon, _ in (coords[0] if p["layer"] == "survey_area" else coords[-1:])]
    c = _central_lon(anchor)

    m = folium.Map(location=[15, c], zoom_start=2, min_zoom=2, tiles=None,
                   control_scale=True, prefer_canvas=True,
                   # everything is drawn within 180° of longitude c, so panning is held to
                   # that one copy of the world; on any other copy the data would vanish
                   world_copy_jump=False, max_bounds=True, min_lat=-85, max_lat=85,
                   min_lon=c - 180, max_lon=c + 180, max_bounds_viscosity=1.0)
    # Esri basemaps need no API key (Carto's now do)
    esri = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"
    for service, name, attr, native in (
            ("Ocean/World_Ocean_Base", "Ocean", "Esri, GEBCO, NOAA, Garmin, HERE", 10),
            ("World_Imagery", "Satellite", "Esri, Maxar, Earthstar Geographics", 18),
            ("Canvas/World_Light_Gray_Base", "Light", "Esri, HERE, Garmin", 16),
            ("Canvas/World_Dark_Gray_Base", "Dark", "Esri, HERE, Garmin", 16)):
        folium.TileLayer(esri.format(service), name=name, attr=f"Tiles &copy; {attr}",
                         max_native_zoom=native, max_zoom=18, show=name == "Ocean").add_to(m)

    groups, bounds = {}, []
    for i, ds in enumerate(dict.fromkeys(vessels["dataset"])):
        color = DATASET_COLORS[i % len(DATASET_COLORS)]
        survey = folium.FeatureGroup("Survey patterns")
        preds = folium.FeatureGroup("Predicted next lines")
        tracks = folium.FeatureGroup("Vessel tracks")
        boats = folium.FeatureGroup("Vessel positions")

        for f in layer("track"):
            if _match(f, ds):
                _polyline(f["geometry"]["coordinates"], tracks, c, color=color, weight=1.5, opacity=0.55,
                          tooltip=f"{f['properties']['vessel']} track")
        for f in layer("survey_block"):
            if not _match(f, ds):
                continue
            p = f["properties"]
            ring = _unwrap(f["geometry"]["coordinates"][0], c)
            folium.Polygon(ring, color=color, weight=1, fill=True, fill_opacity=0.18,
                           tooltip=f"{p['vessel']} · block {p['block_id']} · {p['confidence']} confidence",
                           popup=folium.Popup(_rows([
                               ("Vessel", p["vessel"]), ("Block", p["block_id"]), ("Status", p["status"]),
                               ("Confidence", f"{p['confidence']} ({p['score']})"), ("Lines", p["n_lines"]),
                               ("Line direction", p["line_axis_compass"]),
                               ("Spacing", f"{p['median_spacing_km']} km"),
                               ("Area", f"{p['area_km2']} km²")]), max_width=320)).add_to(survey)
        for f in layer("survey_area"):
            if not _match(f, ds):
                continue
            p = f["properties"]
            ring = _unwrap(f["geometry"]["coordinates"][0], c)
            bounds += ring
            folium.Polygon(ring, color=color, weight=2.5, dash_array="6 4", fill=False,
                           tooltip=f"{p['vessel']} · survey area {p['area_id']} · {p['status']}").add_to(survey)
        for f in layer("line"):
            if _match(f, ds) and (f["properties"].get("block_id") or -1) > 0:
                _polyline(f["geometry"]["coordinates"], survey, c, color=color, weight=3, opacity=0.95)
        has_preds = False
        for f in layer("predicted_line"):
            if _match(f, ds):
                has_preds = True
                p = f["properties"]
                _polyline(f["geometry"]["coordinates"], preds, c, color=PREDICTION_COLOR, weight=4,
                          dash_array="8 6",
                          tooltip=f"{p['vessel']}: {p['kind']} (+{p['hours_after_last_fix_start']} h)")

        for _, v in vessels[vessels["dataset"] == ds].iterrows():
            if pd.isna(v["last_lat"]):
                continue
            live = v["survey_status"] == "Surveying now"
            folium.Marker([v["last_lat"], _wrap(v["last_lon"], c)],
                          tooltip=f"{vessel_label(v)} · {v['survey_status']}",
                          popup=folium.Popup(_vessel_popup(v), max_width=340),
                          icon=folium.Icon(color="red" if live else "blue" if v["survey_areas"] else "gray",
                                           icon="ship", prefix="fa")).add_to(boats)
            if not v["survey_areas"]:
                bounds.append([v["last_lat"], _wrap(v["last_lon"], c)])

        groups[f"Dataset: {ds}"] = [survey] + ([preds] if has_preds else []) + [tracks, boats]

    # --- ports near surveys and the routes to them ---------------------------
    linked_ports = folium.FeatureGroup("Rare-earth linked ports")
    plain_ports = folium.FeatureGroup("Other nearby ports")
    flag_ports = folium.FeatureGroup("Ports in flag state / chosen country")
    linked_routes = folium.FeatureGroup("Routes to rare-earth linked ports (any country)", show=not matched)
    flag_routes = folium.FeatureGroup("Routes to flag-state / chosen-country ports")
    plain_routes = folium.FeatureGroup("Routes to other nearby ports", show=False)
    by_port = {}
    for f in layer("nearby_port"):
        p = f["properties"]
        by_port.setdefault((p["port"], p["country"], p["lat"], p["lon"]), []).append(p)
    for (name, country, lat, lon), rows in by_port.items():
        p = rows[0]
        link = "" if _blank(p.get("ree_link")) else p["ree_link"]
        surveys = "<br>".join(f"{html.escape(str(r['vessel']))} area {r['area_id']}: "
                              f"{_km(r['sea_km'])} ({r['distance_basis']})" for r in rows)
        folium.CircleMarker(
            [lat, _wrap(lon, c)], radius=8 if link else 5, color="#222", weight=1, fill=True,
            fill_color=LINK_COLORS.get(link, PLAIN_PORT_COLOR), fill_opacity=0.95,
            tooltip=f"{name} ({country})" + (f" · {link}" if link else ""),
            popup=folium.Popup(_rows([
                ("Port", f"<b>{html.escape(name)}</b>"), ("Country", country),
                ("Harbour size", p["harbor_size"]), ("Rare-earth link", f"<b>{link}</b>" if link else ""),
                ("Linked site", p.get("ree_site")),
                ("Site distance", _km(p.get("ree_site_km")) if link else ""),
                ("Note", p.get("ree_note")), ("Max draught (m)", p.get("max_vessel_draft_m")),
                ("Bulk cargo", p.get("solid_bulk")), ("Railway", p.get("railway")),
                ("Distance from surveys", surveys)]), max_width=380),
        ).add_to(linked_ports if link else plain_ports)
        home = sorted({r["match_country"] for r in rows if r["category"] in FLAG_CATEGORIES})
        if home:   # a ring marks ports of the matched country
            folium.CircleMarker([lat, _wrap(lon, c)], radius=12, color=FLAG_PORT_COLOR, weight=3, fill=False,
                                tooltip=f"{name}: port in {', '.join(home)}").add_to(flag_ports)
    for f in layer("port_route"):
        p = f["properties"]
        linked = not _blank(p.get("ree_link"))
        home = p.get("category") in FLAG_CATEGORIES
        _polyline(f["geometry"]["coordinates"], c=c, group=
                  flag_routes if home else linked_routes if linked else plain_routes,
                  color=FLAG_PORT_COLOR if home else
                  LINK_COLORS["Known rare-earth cargo port"] if linked else PLAIN_PORT_COLOR,
                  weight=3 if home else 1.5, opacity=0.9 if home else 0.6, dash_array="4 6",
                  tooltip=f"{p['vessel']} area {p['area_id']} → {p['port']}: {_km(p['sea_km'])} "
                          f"({p['distance_basis']})")

    # --- rare-earth sites ------------------------------------------------------
    site_groups = {t: folium.FeatureGroup(name) for t, (_, _, name) in SITE_STYLES.items()}
    for f in layer("ree_site"):
        p = f["properties"]
        if p["type"] not in site_groups:
            continue
        color, sides, _ = SITE_STYLES[p["type"]]
        tip = f"{p['name']} · {p['type']} ({p['status']})"
        popup = folium.Popup(_rows([("Site", f"<b>{html.escape(p['name'])}</b>"), ("Type", p["type"]),
                                    ("Status", p["status"]), ("Operator", p.get("operator")),
                                    ("Country", p["country"]), ("Note", p.get("notes"))]), max_width=340)
        # HTML shapes: RegularPolygonMarker breaks on the canvas renderer
        shape = (f"<div style='width:11px;height:11px;margin:2px;background:{color};"
                 f"border:1px solid #222;transform:rotate(45deg)'></div>" if sides == 4 else
                 f"<div style='width:0;height:0;border-left:8px solid transparent;"
                 f"border-right:8px solid transparent;border-bottom:14px solid {color}'></div>")
        folium.Marker([p["lat"], _wrap(p["lon"], c)], tooltip=tip, popup=popup,
                      icon=folium.DivIcon(html=shape, icon_size=(16, 16), icon_anchor=(8, 8),
                                          class_name="ree-site")).add_to(site_groups[p["type"]])

    # --- seabed mining areas ------------------------------------------------------
    seabed = {
        "ccz": folium.FeatureGroup("Clarion-Clipperton Zone outline"),
        "nodules": folium.FeatureGroup("ISA contracts: polymetallic nodules"),
        "sulphides": folium.FeatureGroup("ISA contracts: polymetallic sulphides"),
        "crusts": folium.FeatureGroup("ISA contracts: cobalt-rich crusts"),
        "reserved": folium.FeatureGroup("ISA reserved areas"),
        "apei": folium.FeatureGroup("Protected areas (APEIs)"),
        "relinquished": folium.FeatureGroup("Relinquished areas", show=False),
    }
    national = folium.FeatureGroup("National seabed mineral zones (EEZ)")
    if NATIONAL_SEABED.exists():
        for f in json.loads(NATIONAL_SEABED.read_text())["features"]:
            p, geom = f["properties"], f["geometry"]
            polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
            for poly in polys:
                folium.Polygon([_unwrap(r, c) for r in poly], color="#00838f", weight=2, dash_array="8 5",
                               fill=True, fill_opacity=0.06,
                               tooltip=f"{p['name']} ({p['country']})",
                               popup=folium.Popup(_rows([("Zone", f"<b>{html.escape(p['name'])}</b>"),
                                                         ("Country", p["country"]), ("Mineral", p["mineral"]),
                                                         ("Note", p["note"]), ("Outline", p["source"])]),
                                                  max_width=360)).add_to(national)

    world_ports = folium.FeatureGroup("All world ports (3,800)", show=False)
    ports = port_intel.load_ports()
    if len(ports):
        FastMarkerCluster([[la, _wrap(lo, c), name] for la, lo, name in ports[["lat", "lon", "port"]].values],
                          callback=("function (row) { return L.circleMarker(new L.LatLng(row[0], row[1]),"
                                    " {radius: 4, color: '#3b5b7d', fillOpacity: 0.8})"
                                    ".bindTooltip(row[2]); }")).add_to(world_ports)

    groups["Ports near surveys"] = [linked_ports, plain_ports, flag_ports, linked_routes,
                                    flag_routes, plain_routes]
    groups["Seabed mining areas"] = list(seabed.values()) + [national]
    groups["Rare-earth sites"] = list(site_groups.values())
    groups["Reference"] = [world_ports]
    for members in groups.values():
        for g in members:
            g.add_to(m)
    GroupedLayerControl(groups, exclusive_groups=False, collapsed=True).add_to(m)   # opens on hover / tap
    LiveSeabedAreas(c, seabed).add_to(m)
    folium.LayerControl(position="bottomleft", collapsed=True).add_to(m)   # basemaps

    if focus is not None:
        ds, vessel = focus
        fb = []
        for f in feats:
            if not _match(f, ds, vessel):
                continue
            kind, coords = f["properties"]["layer"], f["geometry"]["coordinates"]
            if kind == "survey_area":
                fb += _unwrap(coords[0], c)
            elif kind == "predicted_line":
                fb += _unwrap(coords, c)
        v = vessels[(vessels["dataset"] == ds) & (vessels["vessel"] == vessel)]
        if len(v) and not pd.isna(v.iloc[0]["last_lat"]):
            fb.append([v.iloc[0]["last_lat"], _wrap(v.iloc[0]["last_lon"], c)])
        # include the matched country's nearest port and nearest rare-earth linked port
        fb += [[f["properties"]["lat"], _wrap(f["properties"]["lon"], c)] for f in layer("nearby_port")
               if _match(f, ds, vessel) and f["properties"].get("category") in FLAG_CATEGORIES
               and f["properties"].get("rank") == 1]
        if len(fb) <= 1:   # no survey: frame the whole track
            fb += [pt for f in layer("track") if _match(f, ds, vessel)
                   for pt in _unwrap(f["geometry"]["coordinates"], c)]
        bounds = fb or bounds
    if bounds:
        lats, lons = [b[0] for b in bounds], [b[1] for b in bounds]
        pad_lat = max(0.3, (max(lats) - min(lats)) * 0.15)
        pad_lon = max(0.3, (max(lons) - min(lons)) * 0.15)
        # the web map stretches without limit toward the poles: keep the frame inside ±82°
        m.fit_bounds([[max(min(lats) - pad_lat, -82), min(lons) - pad_lon],
                      [min(max(lats) + pad_lat, 82), max(lons) + pad_lon]])
    return m


@st.cache_data(show_spinner=False, max_entries=24)
def map_html(geojson: dict, vessels: pd.DataFrame, focus: tuple | None) -> str:
    return build_map(geojson, vessels, focus).get_root().render()


# ============================================================
# SHIP LIBRARY
# ============================================================

def library_files() -> tuple:
    """(file, modified time) for the catalog and every track: the cache key for the library."""
    files = [LIBRARY / "vessels.csv"] + sorted((LIBRARY / "tracks").glob("*"))
    return tuple((str(f), f.stat().st_mtime) for f in files if f.is_file())


@st.cache_data(show_spinner=False)
def load_library(stamp: tuple) -> pd.DataFrame:
    """The catalog, with each track's date span and number of positions added."""
    path = LIBRARY / "vessels.csv"
    if not path.exists():
        return pd.DataFrame()
    cat = pd.read_csv(path, dtype=str).fillna("")
    first, last, count = [], [], []
    for track in cat.get("track_file", pd.Series([""] * len(cat))):
        f = LIBRARY / "tracks" / track
        if not track or not f.is_file():
            first.append(None); last.append(None); count.append(0)
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            std, _ = spd.standardize(spd.read_any(f), f.stem)
        times = std["time"].dropna()
        first.append(times.min().date() if len(times) else None)
        last.append(times.max().date() if len(times) else None)
        count.append(len(std))
    return cat.assign(data_from=first, data_to=last, positions=count,
                      has_data=[n > 0 for n in count])


@st.cache_data(show_spinner=False, max_entries=64)
def library_track(track: str, stamp: float, start, end) -> bytes:
    """A library track cut to [start, end] (dates), as CSV bytes in its original columns."""
    f = LIBRARY / "tracks" / track
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = spd.read_any(f)
        std, _ = spd.standardize(raw, f.stem)
    if std["time"].notna().any() and start and end:
        day = std["time"].dt.date
        raw = raw.iloc[std.loc[(day >= start) & (day <= end), "row_in_file"]]
    return raw.to_csv(index=False).encode()


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "vessel"


# ============================================================
# PAGE
# ============================================================

st.set_page_config(page_title="Survey Pattern Detector", page_icon=":ship:", layout="wide")

with st.sidebar:
    st.header("Settings")
    st.caption("The defaults suit most survey vessels. Changes re-run the analysis.")
    defaults = asdict(spd.Config())
    if st.button("Reset to defaults"):
        for name in defaults:
            st.session_state.pop(f"cfg_{name}", None)
    settings = {}
    for group, items in SETTINGS.items():
        with st.expander(group):
            for name, help_text in items.items():
                default = defaults[name]
                label = (name.replace("_", " ").replace(" kn", " (kn)").replace(" km", " (km)")
                         .replace(" deg", " (°)"))
                if isinstance(default, int):
                    settings[name] = int(st.number_input(label, value=default, min_value=0, step=1,
                                                         help=help_text, key=f"cfg_{name}"))
                else:
                    settings[name] = float(st.number_input(label, value=float(default), min_value=0.0,
                                                           format="%g", help=help_text, key=f"cfg_{name}"))
    settings = {**defaults, **settings}   # anything not listed above keeps its default

    st.header("Port matching")
    port_mode = st.radio("Also list the nearest ports of", ["Each vessel's flag state",
                                                             "One country for every vessel", "Nobody"],
                         key="port_mode",
                         help="Matches each survey to ports of a country: its vessel's flag state "
                              "(Chinese ships to Chinese ports, American ships to American ports...), "
                              "or one country you pick for every vessel.")
    if port_mode == "One country for every vessel":
        countries = port_intel.port_countries()
        settings["port_country"] = st.selectbox(
            "Country", countries, index=countries.index("China") if "China" in countries else 0,
            key="port_country_pick")
    else:
        settings["port_country"] = "flag" if port_mode.startswith("Each") else "none"
    st.caption("Flags come from a flag column or, failing that, from the MMSI. China includes Hong "
               "Kong and Macau; the United States includes its territories.")

st.title("Survey Pattern Detector")
st.write("Finds lawnmower seafloor-survey patterns in AIS vessel tracks, draws them on one world map, "
         "and lists the ports near each survey, highlighting those linked to rare-earth processing "
         "and those in the vessel's own flag state. Pick ships from the library, set a timeframe, "
         "or upload your own exports: each ship or file becomes its own map layer.")

# --- ship library: search, pick ships, pick a timeframe ---------------------------
lib = load_library(library_files())
groups, lib_ids = [], []
st.subheader("Ship library")
if not len(lib):
    st.info("No ship library found (library/vessels.csv). Upload AIS files below instead.")
else:
    s_col, f_col, d_col = st.columns([3, 2, 1])
    query = s_col.text_input("Search ships", placeholder="Name, MMSI, IMO, operator...",
                             key="lib_query")
    lib_flags = sorted({f for f in lib["flag"] if f})
    flag_pick = f_col.multiselect("Flag", lib_flags, key="lib_flags", placeholder="All flags")
    with_data = d_col.toggle("With data only", value=False, key="lib_with_data")
    view = lib
    if query:
        cols = [c for c in ("name", "mmsi", "imo", "callsign", "operator", "ship_type", "notes") if c in lib]
        text = lib[cols].agg(" ".join, axis=1)
        view = view[text.str.contains(re.escape(query), case=False)]
    if flag_pick:
        view = view[view["flag"].isin(flag_pick)]
    if with_data:
        view = view[view["has_data"]]
    shown_lib = view.assign(status=view["has_data"].map({True: "data", False: "no data yet"}))
    st.dataframe(shown_lib[[c for c in ["name", "flag", "ship_type", "mmsi", "imo", "callsign", "built", "operator",
                                        "status", "data_from", "data_to", "positions", "notes", "source"]
                            if c in shown_lib]],
                 hide_index=True, width="stretch", height=min(38 * (len(view) + 1) + 3, 360),
                 column_config={"name": "Ship", "flag": "Flag", "ship_type": "Type", "mmsi": "MMSI",
                                "imo": "IMO", "callsign": "Call sign", "built": "Built",
                                "operator": "Operator", "status": "Data", "source": "Identity source",
                                "data_from": "From", "data_to": "To", "positions": "Positions",
                                "notes": "Notes"})

    have = view[view["has_data"]]
    pick_names = st.multiselect(
        "Ships to analyse", list(have["name"]), default=list(have["name"])[:5], key="lib_pick",
        help="Only ships with track data can be analysed. Type to search.",
        placeholder="Choose ships with data")
    chosen = lib[lib["name"].isin(pick_names) & lib["has_data"]]
    if len(chosen):
        span_from = min(d for d in chosen["data_from"] if d) if chosen["data_from"].notna().any() else None
        span_to = max(d for d in chosen["data_to"] if d) if chosen["data_to"].notna().any() else None
        start, end = span_from, span_to
        if span_from and span_to:
            t_col, r_col = st.columns([2, 3])
            frame = t_col.selectbox("Timeframe", ["All data", "Last 7 days", "Last 30 days",
                                                  "Last 90 days", "Last 365 days", "Custom range"],
                                    key="lib_frame",
                                    help="'Last N days' counts back from each ship's own newest position, "
                                         "so ships tracked in different years can be compared.")
            last_n = int(frame.split()[1]) if frame.startswith("Last") else None
            if frame == "Custom range":
                picked = r_col.date_input("Dates", value=(span_from, span_to), min_value=span_from,
                                          max_value=span_to, key="lib_dates")
                if isinstance(picked, (tuple, list)) and len(picked) == 2:
                    start, end = picked
            r_col.caption(f"Data available {span_from} to {span_to}. " + (
                f"Analysing the **last {last_n} days of each ship's track**." if last_n else
                f"Analysing **{start} to {end}**."))
        else:
            last_n = None
        for _, row in chosen.iterrows():
            f = LIBRARY / "tracks" / row["track_file"]
            s_i, e_i = start, end
            if last_n and row["data_to"]:   # per ship: its own newest position
                e_i = row["data_to"]
                s_i = max(row["data_from"], e_i - pd.Timedelta(days=last_n - 1))
            data = library_track(row["track_file"], f.stat().st_mtime, s_i, e_i)
            groups.append((row["name"], ((f"{safe_name(row['name'])}.csv", data),)))
            lib_ids.append((row["name"], None, row["mmsi"], row["flag"], row["name"], "library",
                            row.get("imo", "")))

st.subheader("Your own files")
uploads = st.file_uploader("AIS position files", type=UPLOAD_TYPES, accept_multiple_files=True,
                           help="CSV, TSV, TXT or Excel; one ship or many per file. Columns such as "
                                "Latitude/Longitude, Timestamp, MMSI, SOG, COG, ship name and type "
                                "are recognised automatically. Up to 200 MB per file.")
separate = True
if uploads and len(uploads) > 1:
    separate = not st.toggle("Combine all files into one dataset", value=False,
                             help="Use this when the files are parts of one export, e.g. one file per day.")
if uploads:
    taken = {g[0] for g in groups}
    if separate:
        for u in uploads:
            name, n = Path(u.name).stem, 2
            while name in taken:
                name, n = f"{Path(u.name).stem} ({n})", n + 1
            taken.add(name)
            groups.append((name, ((u.name, u.getvalue()),)))
    else:
        groups.append(("Combined upload", tuple((u.name, u.getvalue()) for u in uploads)))

if not groups:
    st.info("Choose ships from the library above, or upload AIS files, to analyse them.")
    st.stop()

given = st.session_state.setdefault("identities", {})   # (dataset, vessel) -> {mmsi, flag, name}
with st.spinner("Detecting survey patterns and searching nearby ports..."):
    try:
        res = analyse(tuple(groups), settings,
                      tuple(lib_ids) + tuple(sorted((k[0], k[1], v["mmsi"], v["flag"], v["name"], "entered", "")
                                                    for k, v in given.items())))
    except (ValueError, FileNotFoundError, ImportError) as exc:
        st.error(f"Could not read the data: {exc}")
        st.stop()

T = res["tables"]
vessels, ports_tbl, areas, preds = (T["vessels"].copy(), T["nearby_ports"].copy(),
                                    T["survey_areas"], T["predictions"])
for col in ("dataset", "vessel", "name", "mmsi", "imo", "callsign", "flag", "flag_source",
            "ship_type", "destination", "marinetraffic_url", "port_country", "port_country_basis"):
    vessels[col] = (vessels[col].fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
                    if col in vessels else "")
if len(ports_tbl):
    text_cols = ports_tbl.select_dtypes(include="object").columns
    ports_tbl[text_cols] = ports_tbl[text_cols].fillna("")     # blank, not "None", in tables
    ports_tbl["ree_link"] = ports_tbl["ree_link"].fillna("").astype(str)   # all-empty reads as float
    ports_tbl[["dataset", "vessel"]] = ports_tbl[["dataset", "vessel"]].astype(str)
if len(ports_tbl) and "match_country" not in ports_tbl:
    ports_tbl["match_country"] = ""
linked_tbl = ports_tbl[ports_tbl["ree_link"] != ""] if len(ports_tbl) else ports_tbl
home_tbl = ports_tbl[ports_tbl["match_country"] != ""] if len(ports_tbl) else ports_tbl

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Datasets", vessels["dataset"].nunique())
c2.metric("Vessels", len(vessels))
c3.metric("Survey areas", len(areas))
c4.metric("Surveying now", int((vessels["survey_status"] == "Surveying now").sum()))
c5.metric("Rare-earth linked ports nearby", linked_tbl["port"].nunique() if len(linked_tbl) else 0)
lib_files = {safe_name(name) for name, *_ in lib_ids}
for note in res["notes"]:
    if note.split(":")[0] in lib_files and "no vessel ID column" in note:
        continue   # the library catalog already says which ship it is
    st.warning(note)

# --- vessels whose files carry no identity: let the user say who they are ------
was_given = pd.Series([k in given for k in zip(vessels["dataset"], vessels["vessel"])], index=vessels.index)
need = vessels[(vessels["flag"] == "") | vessels["flag_source"].eq("entered") | was_given]
if len(need):
    open_now = bool((need["flag"] == "").any())
    with st.expander(f"Identify vessels with an unknown flag ({int((need['flag'] == '').sum())} unknown)",
                     expanded=open_now):
        st.caption("Their files carry no MMSI or flag (MarineTraffic position exports, for example). "
                   "Enter the MMSI from the vessel's MarineTraffic page, and its flag follows from the "
                   "first three digits (413 = China, 366 = United States...), or pick the flag. "
                   "To skip this step, put the MMSI in the file name, e.g. 413123456_export.csv.")
        flag_names = sorted(set(port_intel.port_countries())
                            | {re.sub(r"\s*\(.*\)", "", c) for c in port_intel.MID_COUNTRY.values()})
        with st.form("identities_form"):
            typed = {}
            for _, v in need.iterrows():
                key = (v["dataset"], v["vessel"])
                old = given.get(key, {"mmsi": "", "flag": "", "name": ""})
                c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
                where = "" if v["vessel"] in (v["dataset"], safe_name(v["dataset"])) else f" · {v['dataset']}"
                c1.markdown(f"**{vessel_label(v)}**{where}  \n{v['survey_status']}"
                            + (f" · now **{v['flag']}**" if v["flag"] else ""))
                tag = f"{v['dataset']}|{v['vessel']}"
                typed[key] = {
                    "mmsi": c2.text_input("MMSI", value=old["mmsi"], key=f"id_mmsi_{tag}",
                                          placeholder="e.g. 413123456"),
                    "flag": c3.selectbox("or flag", [""] + flag_names, key=f"id_flag_{tag}",
                                         index=([""] + flag_names).index(old["flag"])
                                         if old["flag"] in flag_names else 0),
                    "name": c4.text_input("Name (optional)", value=old["name"], key=f"id_name_{tag}"),
                }
            if st.form_submit_button("Apply and re-run"):
                bad = [k for k, t in typed.items() if t["mmsi"] and not re.fullmatch(r"\d{9}", t["mmsi"].strip())]
                if bad:
                    st.error("An MMSI is 9 digits: check " + ", ".join(vs for _, vs in bad) + ".")
                else:
                    st.session_state["identities"] = {
                        k: {f: t[f].strip() for f in t} for k, t in typed.items() if any(t.values())}
                    st.rerun()

# --- flag filter and vessel picker: they drive the map and the panels below -----
vessels["flag_shown"] = vessels["flag"].replace("", "Unknown")
counts = vessels["flag_shown"].value_counts()
f_col, v_col = st.columns([2, 3])
with f_col:
    flags = st.multiselect("Vessel flags", list(counts.index), default=list(counts.index),
                           format_func=lambda f: f"{f} ({counts[f]})",
                           help="Show only vessels flying these flags, e.g. just China.")
shown = vessels[vessels["flag_shown"].isin(flags)]
order = shown.assign(rank=shown["survey_status"].map({"Surveying now": 0, "Survey found": 1}).fillna(2))
choices = {"All vessels shown": None}
for _, v in order.sort_values(["rank", "dataset", "vessel"]).iterrows():
    where = ("" if v["vessel"] in (v["dataset"], safe_name(v["dataset"]))   # one ship per file / library
             else f" · {v['dataset']}")
    flag = f" · {v['flag']}" if v["flag"] else ""
    choices[f"{vessel_label(v)}{where}{flag} · {v['survey_status']}"] = (v["dataset"], v["vessel"])
with v_col:
    pick = st.selectbox("Vessel", list(choices), help="Zooms the map to the vessel and shows its details below.")
focus = choices[pick]
if not len(shown):
    st.info("No vessels have the chosen flags.")

st.subheader("World map")
page_map = map_html(res["geojson"], shown, focus)
components.html(page_map, height=640)
legend = [f"<span style='color:{c}'>●</span> {k}" for k, c in LINK_COLORS.items()]
legend += [f"<span style='color:{PLAIN_PORT_COLOR}'>●</span> other port",
           f"<span style='color:{FLAG_PORT_COLOR}'>◯</span> port in flag state / chosen country",
           f"<span style='color:{PREDICTION_COLOR}'>╍╍</span> predicted next lines",
           f"<span style='color:{SITE_STYLES['processing'][0]}'>◆</span> processing plant",
           f"<span style='color:{SITE_STYLES['magnet'][0]}'>◆</span> magnet / metal plant",
           f"<span style='color:{SITE_STYLES['mine'][0]}'>▲</span> mine / deposit",
           f"<span style='color:{MINERAL_COLORS['nodules']}'>■</span> ISA nodule contract",
           f"<span style='color:{MINERAL_COLORS['sulphides']}'>■</span> sulphide contract",
           f"<span style='color:{MINERAL_COLORS['crusts']}'>■</span> crust contract",
           f"<span style='color:{CHINA_OUTLINE}'>□</span> China-sponsored contract",
           "<span style='color:#2e7d32'>■</span> protected area (APEI)"]
st.markdown("<div style='font-size:0.85rem;line-height:1.8'>" + " &nbsp; ".join(legend) +
            "<br>Layer switches: the layers button at the top right of the map (hover or tap to open); "
            "basemaps: the button at the bottom left. Each "
            "dataset has its own colour. Ship icons: red = surveying now, blue = survey found, "
            "grey = no survey. Click anything for details.</div>", unsafe_allow_html=True)

# --- details -------------------------------------------------------------------
PORT_VIEW = {"category": "List", "rank": "#", "port": "Port", "country": "Country", "harbor_size": "Size",
             "sea_km": st.column_config.NumberColumn("Distance (km)", format="%.0f"),
             "hours_at_10kn": st.column_config.NumberColumn("Hours at 10 kn", format="%.0f"),
             "distance_basis": "Measured by", "ree_link": "Rare-earth link", "ree_site": "Linked site",
             "ree_site_km": st.column_config.NumberColumn("Site distance (km)", format="%.0f"),
             "ree_site_status": "Site status", "solid_bulk": "Bulk cargo", "railway": "Railway",
             "max_vessel_draft_m": "Max draught (m)", "ree_note": "Note"}


def list_name(row) -> str:
    country = row.get("match_country") or ""
    return {"flag-state port": f"nearest port in {country}",
            "flag-state rare-earth linked port": f"rare-earth linked port in {country}"
            }.get(row["category"], row["category"])


def port_table(df: pd.DataFrame, extra: tuple = ()):
    cols = list(extra) + [c for c in PORT_VIEW if c in df]
    view = df.assign(category=df.apply(list_name, axis=1))[cols].reset_index(drop=True)
    home = df["match_country"].ne("").to_numpy() if "match_country" in df else [False] * len(df)

    def tint(r):
        color = ("rgba(214, 39, 40, 0.14)" if r["ree_link"] else
                 "rgba(255, 196, 0, 0.16)" if home[r.name] else "")
        return [f"background-color: {color}" if color else ""] * len(r)
    styled = view.style.apply(tint, axis=1)
    st.dataframe(styled, hide_index=True, width="stretch",
                 column_config={k: v for k, v in PORT_VIEW.items() if k in cols})


if focus is None:
    st.subheader("Vessels")

    def first_port(tbl: pd.DataFrame) -> dict:
        out = {}
        for (ds, vs), g in tbl.sort_values("sea_km").groupby(["dataset", "vessel"]) if len(tbl) else []:
            r = g.iloc[0]
            out[(ds, vs)] = f"{r['port']} ({r['country']}), {r['sea_km']:,.0f} km"
        return out
    keys = list(zip(shown["dataset"], shown["vessel"]))
    best, home, home_ree = (first_port(linked_tbl),
                            first_port(home_tbl[home_tbl["category"] == "flag-state port"] if len(home_tbl) else home_tbl),
                            first_port(home_tbl[home_tbl["ree_link"] != ""] if len(home_tbl) else home_tbl))
    overview = shown.assign(nearest_ree_port=[best.get(k, "") for k in keys],
                            home_port=[home.get(k, "") for k in keys],
                            home_ree_port=[home_ree.get(k, "") for k in keys])
    st.dataframe(overview[["dataset", "vessel", "name", "flag", "ship_type", "survey_status", "survey_areas",
                           "port_country", "home_ree_port", "home_port", "nearest_ree_port",
                           "positions_used", "track_km", "marinetraffic_url"]],
                 hide_index=True, width="stretch",
                 column_config={"dataset": "Dataset", "vessel": "Vessel", "name": "Name", "flag": "Flag",
                                "ship_type": "Type", "survey_status": "Status", "survey_areas": "Areas",
                                "nearest_ree_port": "Nearest rare-earth linked port (any country)",
                                "port_country": "Matched country",
                                "home_port": "Nearest port there",
                                "home_ree_port": "Nearest rare-earth linked port there",
                                "positions_used": "Positions",
                                "track_km": st.column_config.NumberColumn("Track (km)", format="%.0f"),
                                "marinetraffic_url": st.column_config.LinkColumn("MarineTraffic",
                                                                                 display_text="open")})
    shown_keys = set(keys)
    if len(linked_tbl):   # only vessels passing the flag filter
        linked_tbl = linked_tbl[[k in shown_keys for k in zip(linked_tbl["dataset"], linked_tbl["vessel"])]]
    if len(linked_tbl):
        st.subheader("Rare-earth linked ports near detected surveys")
        st.caption("Ports within 300 km of a rare-earth processing site or mine, or known to handle "
                   "rare-earth cargo. Pick a vessel above for its full port list.")
        port_table(linked_tbl.sort_values("sea_km"), extra=("dataset", "vessel", "area_id"))
else:
    ds, vs = focus
    v = vessels[(vessels["dataset"] == ds) & (vessels["vessel"] == vs)].iloc[0]
    st.subheader(vessel_label(v))
    left, right = st.columns(2)
    with left:
        size = " × ".join(f"{v[c]} m" for c in ("length_m", "width_m") if c in v and not _blank(v[c]))
        ident = [("Dataset", v["dataset"]), ("Name", v["name"]), ("MMSI", v["mmsi"]), ("IMO", v["imo"]),
                 ("Call sign", v["callsign"]),
                 ("Flag", f"{v['flag']} (from {v['flag_source']})" if v["flag"] else ""),
                 ("Ship type", v["ship_type"]), ("Size", size), ("Destination", v["destination"]),
                 ("Ports matched to", f"{v['port_country']} ({v['port_country_basis']})"
                                      if v["port_country"] else "")]
        st.markdown("**Identity**")
        st.markdown("  \n".join(f"{k}: **{val}**" for k, val in ident if not _blank(val)))
        if v["marinetraffic_url"]:
            st.link_button("Open on MarineTraffic", v["marinetraffic_url"])
        elif not v["mmsi"]:
            st.caption("No MMSI in the data, so there is no MarineTraffic link"
                       + ("." if v["flag"] else " and the flag is unknown."))
    with right:
        period = (f"{str(v['first_time'])[:16]} → {str(v['last_time'])[:16]} UTC"
                  if "first_time" in v and not _blank(v["first_time"]) else "no timestamps (file order used)")
        st.markdown("**Track**")
        st.markdown(f"Status: **{v['survey_status']}**  \n"
                    f"Positions used: **{int(v['positions_used']):,}** of {int(v['positions']):,}  \n"
                    f"Distance covered: **{v['track_km']:,.0f} km**  \n"
                    f"Period: {period}  \n"
                    f"Last position: **{v['last_lat']:.4f}, {v['last_lon']:.4f}**")

    v_areas = (areas[(areas["dataset"].astype(str) == ds) & (areas["vessel"].astype(str) == vs)]
               if len(areas) else areas)
    if not len(v_areas):
        st.info("No lawnmower survey pattern found for this vessel, so no port search was made.")
    for _, a in v_areas.iterrows():
        live = str(a["status"]).startswith("IN")
        st.markdown(f"#### Survey area {a['area_id']} " + (":orange[IN PROGRESS]" if live else ":green[completed]"))
        st.markdown(f"{a['n_lines']} lines in {a['n_blocks']} block(s), best confidence {a['best_confidence']}. "
                    f"Lines run {a['line_axis_compass']}, ~{a['median_spacing_km']} km apart; "
                    f"{a['total_line_km']} line-km over ~{a['area_km2']} km², centred at "
                    f"{a['centroid_lat']}, {a['centroid_lon']}.")
        near = (ports_tbl[(ports_tbl["dataset"] == ds) & (ports_tbl["vessel"] == vs)
                          & (ports_tbl["area_id"] == a["area_id"])] if len(ports_tbl) else ports_tbl)
        if len(near):
            def describe(r) -> str:
                site = (f": {r['ree_site']} ({r['ree_site_status']}) is {r['ree_site_km']:,.0f} km from the port"
                        if not _blank(r["ree_site"]) else "")
                return (f"**{r['port']} ({r['country']})**, ~{r['sea_km']:,.0f} km by {r['distance_basis']}. "
                        f"{r['ree_link']}{site}.")

            any_ree = near[(near["ree_link"] != "") & (near["match_country"] == "")].sort_values("sea_km").head(1)
            home_rows = near[near["match_country"] != ""]
            if len(home_rows):   # the matched country (flag state or chosen) is the headline
                country = home_rows.iloc[0]["match_country"]
                first = home_rows[home_rows["category"] == "flag-state port"].sort_values("sea_km").head(1)
                h_ree = home_rows[home_rows["ree_link"] != ""].sort_values("sea_km").head(1)
                head = f"Matched to **{country}** ({v['port_country_basis']}). "
                head += (f"Closest rare-earth linked port in {country}: " + describe(h_ree.iloc[0])
                         if len(h_ree) else f"No rare-earth linked port in {country} is listed.")
                if len(first):
                    f0 = first.iloc[0]
                    head += (f"  \nNearest port in {country}: **{f0['port']}**, ~{f0['sea_km']:,.0f} km "
                             f"by {f0['distance_basis']}.")
                st.error(head)
                if len(any_ree):
                    st.caption("For comparison, the closest rare-earth linked port in any country: "
                               + describe(any_ree.iloc[0]))
            else:
                if len(any_ree):
                    st.error("Closest rare-earth linked port: " + describe(any_ree.iloc[0]))
                if settings["port_country"] == "flag" and not v["port_country"]:
                    st.info("This vessel's flag is unknown, so it is not matched to a country's ports. "
                            "Give its MMSI or flag under Identify vessels above, or choose a country "
                            "under Port matching in the sidebar.")
            st.markdown("**Ports near this survey** (red rows: rare-earth linked; gold rows: matched country)")
            first = {"flag-state port": 0, "flag-state rare-earth linked port": 1}   # matched country on top
            port_table(near.sort_values(["category", "rank"],
                                        key=lambda c: c.map(first).fillna(2) if c.name == "category" else c))
        sites = pd.DataFrame(port_intel.nearby_ree_sites(a["centroid_lat"], a["centroid_lon"], 5))
        if len(sites):
            st.markdown("**Closest rare-earth sites** (straight-line distance)")
            st.dataframe(sites[["name", "type", "status", "country", "distance_km", "notes"]],
                         hide_index=True, width="stretch",
                         column_config={"distance_km": st.column_config.NumberColumn("Distance (km)",
                                                                                     format="%.0f")})

    v_preds = (preds[(preds["dataset"].astype(str) == ds) & (preds["vessel"].astype(str) == vs)]
               if len(preds) else preds)
    if len(v_preds):
        st.markdown("**Predicted next lines**")
        cols = [c for c in ["line_ahead", "kind", "start_lat", "start_lon", "end_lat", "end_lon",
                            "heading_deg", "hours_after_last_fix_start", "est_start_utc"] if c in v_preds]
        st.dataframe(v_preds[cols], hide_index=True, width="stretch")
    png = res["maps"].get(spd.map_filename(ds, vs))
    if png:
        with st.expander("Detector map (PNG)"):
            st.image(png, width="stretch")

st.markdown("<div style='font-size:0.8rem;opacity:0.75'>Library tracks for Chinese survey vessels: "
            "<a href='https://globalfishingwatch.org' target='_blank'>Powered by Global Fishing Watch</a> "
            "(CC BY-NC 4.0; hourly positions on a ~1 km grid). US reference tracks: NOAA Marine Cadastre "
            "(public domain).</div>", unsafe_allow_html=True)
st.caption("Ports: NGA World Port Index (public domain). Sea distances follow a shipping-lane network "
           "(searoute package) and are straight-line where no reliable route exists. Rare-earth sites "
           "are a curated list with approximate locations (reference_data/ree_sites.csv); verify "
           "before relying on them.")

st.subheader("All results")
tabs = st.tabs(["Vessels", "Nearby ports", "Survey areas", "Survey blocks", "Survey lines",
                "Predictions", "Positions"])
for tab, key in zip(tabs, TABLES):
    with tab:
        if len(T[key]):
            df = T[key].copy()
            text = df.select_dtypes(include="object").columns
            df[text] = df[text].fillna("")
            st.dataframe(df, hide_index=True, width="stretch")
        else:
            st.write("Nothing found.")

st.subheader("Download")
d1, d2, d3, d4 = st.columns(4)
d1.download_button("Everything (ZIP)", res["zip"], "survey_results.zip", "application/zip",
                   help="CSVs, Excel workbook, GeoJSON (combined and per layer) and map images.")
d2.download_button("Excel workbook", res["xlsx"], "survey_results.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
d3.download_button("GeoJSON (QGIS / ArcGIS)", json.dumps(res["geojson"]), "survey_results.geojson",
                   "application/geo+json")
d4.download_button("Interactive map (HTML)", map_html(res["geojson"], vessels, None), "survey_map.html",
                   "text/html", help="The world map with all datasets as a standalone page to open or share.")
