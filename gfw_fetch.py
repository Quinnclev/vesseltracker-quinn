#!/usr/bin/env python3
"""
GLOBAL FISHING WATCH TRACK FETCHER
==================================

Pulls hourly vessel tracks from the Global Fishing Watch (GFW) API into the
ship library, so the web app and detector can look for survey patterns.

    python gfw_fetch.py                         # last 12 months, Chinese survey vessels
    python gfw_fetch.py --months 3
    python gfw_fetch.py --start 2025-01-01 --end 2025-12-31
    python gfw_fetch.py --flag CHN --types seismic_vessel

How: GFW's "4Wings" AIS vessel-presence report, grouped by vessel at hourly
time and 0.01-degree (~1 km) space resolution, gives one grid cell per vessel
per hour: in effect an hourly track, from satellite and terrestrial AIS
worldwide. Reports cannot be filtered to one vessel, so this pulls every
vessel of the chosen flag and GFW vessel types (default: China, "seismic_vessel",
GFW's class for survey ships) and keeps them all.

Output:
  library/tracks/gfw_<MMSI>.csv   one file per vessel (merged with earlier pulls)
  library/vessels.csv             existing ships get their track linked; new
                                  vessels are added (identity from GFW)

Needs a GFW API token (free, non-commercial use only) in gfw_token.txt next to
this file, or in the GFW_TOKEN environment variable. Never upload the token.

Data: Powered by Global Fishing Watch (https://globalfishingwatch.org),
CC BY-NC 4.0. Tracks are hourly positions snapped to ~1 km cells, so survey
lines closer together than about 1-2 km blur together.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
LIBRARY = HERE / "library"
API = "https://gateway.api.globalfishingwatch.org/v3/4wings/report"
DATASET = "public-global-presence:latest"
WORLD = {"type": "Polygon", "coordinates": [[[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]]]}
CREDIT = "Global Fishing Watch 4Wings AIS presence, hourly ~1 km (CC BY-NC 4.0, Powered by Global Fishing Watch)"
FLAG_NAMES = {"CHN": "China", "USA": "United States", "JPN": "Japan", "KOR": "South Korea", "RUS": "Russia",
              "IND": "India", "DEU": "Germany", "FRA": "France", "GBR": "United Kingdom", "NOR": "Norway"}


def token() -> str:
    t = os.environ.get("GFW_TOKEN") or ""
    f = HERE / "gfw_token.txt"
    if not t and f.exists():
        t = f.read_text().strip()
    if not t:
        sys.exit("No GFW token: put it in gfw_token.txt next to this script (or set GFW_TOKEN).")
    return t


def months(start: dt.date, end: dt.date):
    """Calendar-month chunks [a, b) covering start..end."""
    a = start
    while a <= end:
        nxt = (a.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        yield a, min(nxt, end + dt.timedelta(days=1))
        a = nxt


def report(tok: str, start: dt.date, end: dt.date, flag: str, types: list[str]) -> list[dict]:
    filt = f"flag in ('{flag}')" + (" AND vessel_type in (" + ",".join(f"'{t}'" for t in types) + ")" if types else "")
    q = urllib.parse.urlencode({
        "spatial-resolution": "HIGH", "temporal-resolution": "HOURLY", "group-by": "VESSEL_ID",
        "datasets[0]": DATASET, "format": "JSON", "date-range": f"{start},{end}", "filters[0]": filt})
    body = json.dumps({"geojson": WORLD}).encode()
    for attempt in range(4):
        req = urllib.request.Request(f"{API}?{q}", data=body, method="POST", headers={
            "Authorization": f"Bearer {tok}", "Content-Type": "application/json",
            "User-Agent": "SurveyDetector/1.0 (non-commercial research)"})   # default Python UA is blocked
        try:
            d = json.load(urllib.request.urlopen(req, timeout=600))
            entries = d.get("entries") or [{}]
            return next(iter(entries[0].values()), []) if entries[0] else []
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(20 * (attempt + 1))
                continue
            sys.exit(f"GFW API error {e.code}: {msg}")
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(20)
                continue
            sys.exit(f"Network error: {e}")
    return []


def kind_of(name: str, geartype: str) -> str:
    n = name.upper().replace(" ", "")
    if "COASTGUARD" in n:
        return "Coast guard (GFW classes it as survey)"
    if n.startswith(("HAIYANGSHIYOU", "BINHAI", "DONGFANGMINGZHU")):
        return "Offshore oil & gas seismic vessel"
    return "Survey / research vessel (GFW: seismic)" if "SEISMIC" in (geartype or "").upper() else \
        f"{(geartype or 'unknown').replace('_', ' ').lower()} (GFW)"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=12, help="how far back from today (default 12)")
    ap.add_argument("--start", help="YYYY-MM-DD (overrides --months)")
    ap.add_argument("--end", help="YYYY-MM-DD (default: 4 days ago, GFW's lag)")
    ap.add_argument("--flag", default="CHN", help="ISO3 flag code (default CHN)")
    ap.add_argument("--types", default="seismic_vessel",
                    help="comma-separated GFW vessel types (default seismic_vessel; '' for all types)")
    args = ap.parse_args()

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today() - dt.timedelta(days=4)
    if args.start:
        start = dt.date.fromisoformat(args.start)
    else:   # first day of the month, args.months calendar months back (counting the current one)
        k = end.year * 12 + end.month - 1 - (args.months - 1)
        start = dt.date(k // 12, k % 12 + 1, 1)
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    tok = token()

    rows = []
    for a, b in months(start, end):
        t0 = time.time()
        got = report(tok, a, b, args.flag, types)
        rows += got
        print(f"{a} .. {b - dt.timedelta(days=1)}: {len(got):,} vessel-hours ({time.time() - t0:.0f}s)", flush=True)
    if not rows:
        sys.exit("No data returned.")

    df = pd.DataFrame(rows)
    df["mmsi"] = df["mmsi"].astype(str).str.replace(r"\.0$", "", regex=True)
    df["Timestamp"] = pd.to_datetime(df["date"]) + pd.Timedelta(minutes=30)   # middle of the hour
    (LIBRARY / "tracks").mkdir(parents=True, exist_ok=True)
    catalog = pd.read_csv(LIBRARY / "vessels.csv", dtype=str).fillna("")
    for col in ("callsign", "built", "source"):
        if col not in catalog:
            catalog[col] = ""
    flag_name = FLAG_NAMES.get(args.flag.upper(), args.flag)

    added = linked = 0
    for mmsi, g in df.groupby("mmsi"):
        if not re.fullmatch(r"\d{9}", mmsi):
            continue
        g = g.sort_values("Timestamp")
        info = g.iloc[-1]
        track = pd.DataFrame({
            "Timestamp": g["Timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S"), "Latitude": g["lat"],
            "Longitude": g["lon"], "Speed": g["speed"].round(2) if "speed" in g else "",
            "MMSI": mmsi, "IMO": info.get("imo") or "", "CallSign": info.get("callsign") or "",
            "ShipName": info.get("shipName") or "", "Flag": flag_name})
        fname = f"gfw_{mmsi}.csv"
        path = LIBRARY / "tracks" / fname
        if path.exists():   # merge with earlier pulls: keep all hours, newest pull wins
            old = pd.read_csv(path, dtype=str)
            track = (pd.concat([old, track.astype(str)]).drop_duplicates("Timestamp", keep="last")
                     .sort_values("Timestamp"))
        track.to_csv(path, index=False)

        hit = catalog.index[catalog["mmsi"] == mmsi]
        if len(hit):
            i = hit[0]
            if not catalog.loc[i, "track_file"] or catalog.loc[i, "track_file"].startswith("gfw_"):
                catalog.loc[i, "track_file"] = fname
                if "Global Fishing Watch" not in catalog.loc[i, "source"]:
                    catalog.loc[i, "source"] = (catalog.loc[i, "source"] + "; track: " + CREDIT).strip("; ")
                linked += 1
        else:
            name = str(info.get("shipName") or mmsi).title().replace("Xiangyanghong", "Xiang Yang Hong ")
            catalog.loc[len(catalog)] = {
                "name": name, "flag": flag_name, "mmsi": mmsi, "imo": str(info.get("imo") or ""),
                "callsign": str(info.get("callsign") or ""), "built": "",
                "ship_type": kind_of(str(info.get("shipName") or ""), str(info.get("geartype") or "")),
                "operator": "", "notes": "Added from Global Fishing Watch survey-vessel search.",
                "source": "Identity and track: " + CREDIT, "track_file": fname}
            added += 1
    catalog.to_csv(LIBRARY / "vessels.csv", index=False)
    print(f"\n{df['mmsi'].nunique()} vessels, {len(df):,} hourly positions, {start} to {end}.")
    print(f"Library: {linked} existing ships linked to tracks, {added} new ships added.")
    print("Powered by Global Fishing Watch (https://globalfishingwatch.org), CC BY-NC 4.0.")


if __name__ == "__main__":
    main()
