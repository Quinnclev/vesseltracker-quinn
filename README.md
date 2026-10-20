# Survey Pattern Detector (v3)

Finds "lawnmower" seafloor-survey patterns in AIS vessel tracks: runs of long,
straight, parallel lines driven in alternating directions. For surveys still
underway at the last position, it projects where the next lines will fall. Each
survey is matched to nearby ports, and ports linked to rare-earth processing,
mining or rare-earth cargo are highlighted.

- `survey_pattern_detector.py`: the detector (command line or imported)
- `port_intel.py`: nearby ports, rare-earth links, flag from MMSI, AIS ship types
- `reference_data/world_ports.csv`: about 3,800 ports from the NGA World Port Index (public domain)
- `reference_data/ree_sites.csv`: rare-earth processing plants, magnet plants, mines,
  cargo ports and seabed-mineral areas. Curated, with approximate locations; add or
  correct rows freely.
- `app.py`: web app with a world map of every uploaded dataset
- `run_survey_workbook.py`: Excel workflow (`Survey_Detector.xlsx`)
- `Survey Detector.pyt`: ArcGIS Pro toolbox
- `library/`: the ship library the web app opens with: `vessels.csv` (catalog) and
  `tracks/` (one position file per ship)

## Web app

**Ship library.** The app opens on a searchable list of ships (name, MMSI, IMO,
operator, flag). Pick ships that have track data, choose a timeframe (all data,
the last 7 / 30 / 90 / 365 days of the track, or a custom date range) and the
analysis runs on that part of each track, using the catalog's flag for port
matching. Ships listed without data are a watchlist still to be exported.

To add a ship: export its position history from MarineTraffic, put the file in
`library/tracks/`, and add (or fill in) its row in `library/vessels.csv` with
`track_file` set to the file name. Fill in `mmsi` and `imo` from the vessel's
MarineTraffic page; `flag` drives the flag-state port matching. On GitHub: open
the `library/tracks` folder, **Add file -> Upload files**; then open
`library/vessels.csv`, click the pencil icon, edit, and commit.

You can also upload your own CSV / TSV / TXT / Excel exports, one ship or many per file.
Each file becomes its own **dataset**. Columns such as Latitude/Longitude,
Timestamp, MMSI, IMO, ship name, ship type, SOG and COG are recognised automatically.

- **World map.** All datasets on one map you can pan and zoom anywhere. The panel
  at the top right switches layers on and off: per dataset, the survey patterns,
  predicted next lines, tracks and vessel positions; then the ports near the
  surveys, the routes to them, and the rare-earth sites. Basemaps (ocean,
  satellite, light, dark) are at the bottom left.
- **Seabed mining areas** (layer group on the map): every International Seabed
  Authority exploration contract area, coloured by mineral (nodules, sulphides,
  cobalt crusts) with China-sponsored contracts outlined in red, plus ISA reserved
  areas, protected areas (APEIs), relinquished areas (off by default), the
  Clarion-Clipperton Zone outline, and two national zones (Cook Islands EEZ,
  Japan's Minamitorishima EEZ). Click an area for contractor, sponsoring state,
  mineral, size and contract year. The ISA boundaries are not stored in this
  project: the map loads them live from the ISA's public map service, so they stay
  current. ISA terms allow personal, non-commercial use; republishing needs the
  ISA's written permission (news@isa.org.jm). The two national zones come from
  Marine Regions (EEZ v12, CC BY 4.0) in `reference_data/national_seabed_areas.geojson`.
- **Vessel panel.** Pick a vessel to zoom the map to it and see its identity
  (name, MMSI, IMO, flag, type, size, destination, MarineTraffic link), its track,
  each survey area, the ports nearest the survey with rare-earth linked ports
  highlighted, and the closest rare-earth sites.
- **Downloads.** Everything as a ZIP, an Excel workbook, GeoJSON for QGIS/ArcGIS,
  and the interactive map as a standalone HTML page.

**Flag-state ports.** Each survey is also matched to the nearest ports of its
vessel's flag state (Chinese ships to Chinese ports, American ships to American
ports, and so on), including that country's nearest rare-earth linked port. The
flag comes from a flag column in the data, or else from the first three digits of
the MMSI. China includes Hong Kong and Macau; the United States, France, the
Netherlands and Norway include their territories. In the sidebar, **Port matching**
can instead match every vessel to one chosen country, or turn this off, and the
**Vessel flags** filter limits the map and tables to, say, Chinese vessels only.
A ship registered under a flag of convenience (Panama, Liberia...) is matched to
that flag; AIS does not carry ownership.

**Files without an MMSI or flag** (such as MarineTraffic's per-vessel position
export: Timestamp, Source, Speed, Course, Latitude, Longitude, Heading) cannot say
which ship they are. Either:

- put the MMSI in the file name, e.g. `413123456_MarineTraffic_Vessel_positions_Export.csv`
  (the detector reads a 9-digit MMSI, or `IMO1234567`, from the name of a
  single-vessel file); or
- in the web app, open **Identify vessels with an unknown flag**, enter the MMSI
  from the vessel's MarineTraffic page (or pick its flag), and press
  **Apply and re-run**.

A port is **rare-earth linked** when it is a known rare-earth cargo port, or lies
within 300 km of a rare-earth processing or magnet plant or a mine. Port distances
follow shipping lanes (searoute package). Where that network has no sensible
route (it breaks around the 180° line, e.g. in the Bering Strait), the
straight-line distance is used and labelled as such.

### Put it online (Streamlit Community Cloud, free)

1. Create a GitHub repository (Private is fine).
2. Upload only these:
   - `app.py`
   - `survey_pattern_detector.py`
   - `port_intel.py`
   - `requirements.txt`
   - `README.md`
   - the `reference_data` folder
   - the `library` folder

   Leave out `Survey_Detector.xlsx`, `input_files/` and `survey_results/`: they
   hold your own data.
3. At <https://share.streamlit.io> click **Create app**, choose the repository,
   branch `main`, main file `app.py`, and **Deploy**.
4. Under the app's **Settings → Sharing**, choose who can open it.

To replace an existing app, upload these files into its repository instead (the
old app has no `port_intel.py` or `reference_data`, so add those too). The app
redeploys by itself.

### Run it on your own computer

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Command line

```bash
python survey_pattern_detector.py fleet_a.csv fleet_b.csv --out results
```

Each file or folder given is a separate dataset. Results go to `results/`:
`vessels.csv`, `nearby_ports.csv`, `survey_areas.csv`, `survey_blocks.csv`,
`survey_lines.csv`, `predictions.csv`, `positions_processed.csv`,
`survey_results.geojson` (plus one GeoJSON per layer in `arcgis/`) and `maps/`.
Every setting in `Config` is also a command-line flag, e.g. `--min-lines-per-block 4`.
`--port-country China` matches every vessel to Chinese ports (`flag`, the default,
uses each vessel's own flag; `none` turns it off). In the Excel workbook, add a
`port_country` row to the Settings sheet.
