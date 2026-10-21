"""
WHAT IS THIS SURVEY LIKELY LOOKING FOR?
=======================================

A lawnmower pattern says a ship is mapping the seabed, not why. Where it maps
is the best clue: polymetallic nodules lie on abyssal plains (the CCZ), cobalt
crusts on seamount flanks, sulphides on ridges and back-arc basins, oil and gas
on continental shelves, and Arctic mapping is mostly for shelf claims and science.

reference_data/seabed_resources.csv lists those regions as rough lon/lat boxes,
with the commodity, typical depth and a source. Boxes are approximate on
purpose: they indicate, they do not prove. The live ISA contract layer on the
map has the exact contract boundaries. Edit the CSV to add or refine regions.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

REGIONS = Path(__file__).resolve().parent / "reference_data" / "seabed_resources.csv"

# deep-sea minerals first, then the rest, when two regions are equally specific
CATEGORY_ORDER = ["Polymetallic nodules", "Rare-earth-rich mud", "Cobalt-rich crusts",
                  "Seafloor massive sulphides", "Phosphorite", "Marine placers", "Gas hydrates",
                  "Oil and gas", "Scientific / sovereignty mapping"]
CATEGORY_COLORS = {"Polymetallic nodules": "#1e88e5", "Rare-earth-rich mud": "#d81b60",
                   "Cobalt-rich crusts": "#8d6e63", "Seafloor massive sulphides": "#ef6c00",
                   "Phosphorite": "#7cb342", "Marine placers": "#fbc02d", "Gas hydrates": "#26a69a",
                   "Oil and gas": "#455a64", "Scientific / sovereignty mapping": "#7e57c2"}


@lru_cache(maxsize=1)
def regions() -> pd.DataFrame:
    if not REGIONS.exists():
        return pd.DataFrame()
    df = pd.read_csv(REGIONS)
    df["box_deg2"] = (df["max_lon"] - df["min_lon"]) * (df["max_lat"] - df["min_lat"])
    df["rank"] = df["category"].map({c: i for i, c in enumerate(CATEGORY_ORDER)}).fillna(99)
    return df


def regions_at(lat: float, lon: float) -> pd.DataFrame:
    """Regions whose box contains the point, most specific (smallest) first."""
    df = regions()
    if df.empty:
        return df
    lon = (lon + 180) % 360 - 180
    hit = df[(df["min_lat"] <= lat) & (lat <= df["max_lat"]) & (df["min_lon"] <= lon) & (lon <= df["max_lon"])]
    return hit.sort_values(["box_deg2", "rank"])


def vessel_hint(name: str, ship_type: str) -> str:
    """What the ship's own name or type says about its job, if anything."""
    text = f"{name} {ship_type}".upper().replace(" ", "")
    if "COASTGUARD" in text:
        return "coast guard"
    if any(k in text for k in ("SEISMIC", "OIL", "GAS", "HAIYANGSHIYOU", "BINHAI", "DONGFANGMINGZHU")):
        return "oil and gas"
    if any(k in text for k in ("CABLE",)):
        return "cable"
    return ""


def describe(lat: float, lon: float, name: str = "", ship_type: str = "") -> dict:
    """Likely survey target for a survey area centred at lat, lon."""
    hits = regions_at(lat, lon)
    hint = vessel_hint(name, ship_type)
    out = {"likely_target": "", "target_region": "", "target_commodities": "",
           "target_alternatives": "", "target_basis": ""}

    if hint == "coast guard":
        out.update(likely_target="Patrol (coast guard), probably not a resource survey",
                   target_basis="vessel is a coast guard ship")
    elif hint == "cable":
        out.update(likely_target="Cable route survey", target_basis="vessel name or type")
    elif hint == "oil and gas" and (hits.empty or "Oil and gas" in set(hits["category"])):
        oil = hits[hits["category"] == "Oil and gas"]
        out.update(likely_target="Oil and gas",
                   target_region=oil["name"].iloc[0] if len(oil) else "",
                   target_basis="oil and gas vessel" + (" in an oil and gas region" if len(oil) else ""))

    if hits.empty:
        if not out["likely_target"]:
            out.update(likely_target="Unknown: no known resource region here",
                       target_basis="outside every region in seabed_resources.csv; could be charting, "
                                    "cable route, science or an unlisted prospect")
        return out

    top = hits.iloc[0]
    if not out["likely_target"]:
        out.update(likely_target=top["category"], target_region=top["name"],
                   target_commodities=top["commodities"],
                   target_basis=f"survey lies in {top['name']} ({top['depth']}); approximate region")
    elif not out["target_region"]:
        out["target_region"] = top["name"]
    others = [f"{r['category']} ({r['name']})" for _, r in hits.iloc[1:].iterrows()
              if r["category"] != out["likely_target"]]
    if hint and top["category"] != out["likely_target"]:
        others.insert(0, f"{top['category']} ({top['name']})")
    out["target_alternatives"] = "; ".join(dict.fromkeys(others))
    return out
