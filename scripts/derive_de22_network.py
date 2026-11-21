"""Rebuild data/networks/de22/lines.csv and the boundary zones from PyPSA-Eur.

Needs geopandas (``pip install -e ".[derive]"``) and two public downloads:

- PyPSA-Eur's prebuilt electricity network, version 0.8 (ODbL): buses.csv,
  lines.csv and links.csv from https://data.pypsa.org/workflows/eur/osm/0.8/
  (mirror of https://zenodo.org/records/23896564)
- NUTS 2021 regions, 1:1 million, level 2, EPSG:4326, from Eurostat GISCO:
  NUTS_RG_01M_2021_4326_LEVL_2.geojson in
  https://gisco-services.ec.europa.eu/distribution/v2/nuts/download/ref-nuts-2021-01m.geojson.zip

    python scripts/derive_de22_network.py --osm <dir> --nuts <file.geojson>
"""

from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

from gridfee.network.pypsa_eur import LINE_COLUMNS, derive_zone_lines, read_prebuilt_network

NETWORK_DIR = Path(__file__).parents[1] / "data" / "networks" / "de22"
# Denmark is two bidding zones, which SMARD reports separately. Buses west of the
# Great Belt (this longitude) are DK1, the others DK2.
DK_SPLIT_LONGITUDE = 10.9
# Neighbour areas as SMARD names them, by the country of the bus in the OSM data.
SMARD_AREA = {"NO": "NO2", "SE": "SE4"}
AREA_NAMES = {
    "AT": "Austria",
    "BE": "Belgium",
    "CH": "Switzerland",
    "CZ": "Czechia",
    "DK1": "Denmark West",
    "DK2": "Denmark East",
    "FR": "France",
    "LU": "Luxembourg",
    "NL": "Netherlands",
    "NO2": "Norway (NO2)",
    "PL": "Poland",
    "SE4": "Sweden (SE4)",
}


def read_commented_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, comment="#", dtype=str)


def build_busmap(buses: pd.DataFrame, lines, links, nuts_file: Path) -> pd.Series:
    """Map German buses to zones by NUTS2 region and border buses to neighbour areas."""
    zone_of_nuts = read_commented_csv(NETWORK_DIR / "zone_nuts2.csv").set_index("nuts2")["zone_id"]
    regions = gpd.read_file(nuts_file).set_index("NUTS_ID")
    regions = regions.loc[zone_of_nuts.index, ["geometry"]]

    german = buses[buses["country"] == "DE"]
    points = gpd.GeoDataFrame(
        german, geometry=gpd.points_from_xy(german["x"], german["y"]), crs="EPSG:4326"
    )
    inside = gpd.sjoin(points, regions, predicate="within", how="left")["NUTS_ID"]
    # Offshore and coastal buses lie outside the generalised shapes: nearest region.
    outside = points[inside.isna()].to_crs(3035)
    nearest = gpd.sjoin_nearest(outside, regions.to_crs(3035), how="left")["NUTS_ID"]
    nearest = nearest[~nearest.index.duplicated()]
    logging.info("%d German buses outside all regions mapped to the nearest", len(nearest))
    busmap = inside.fillna(nearest).map(zone_of_nuts)

    # Foreign buses: only those at the far end of a line or link from Germany.
    ends = pd.concat([lines[["bus0", "bus1"]], links[["bus0", "bus1"]]])
    partner = pd.concat([ends.set_index("bus0")["bus1"], ends.set_index("bus1")["bus0"]]).rename(
        "partner"
    )
    partner = partner[partner.isin(german.index)]
    foreign = buses.loc[partner.index.unique()].query("country != 'DE'")
    area = foreign["country"].replace(SMARD_AREA)
    is_dk = foreign["country"] == "DK"
    area[is_dk] = foreign.loc[is_dk, "x"].lt(DK_SPLIT_LONGITUDE).map({True: "DK1", False: "DK2"})
    return pd.concat([busmap, area]).rename("zone_id")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--osm", type=Path, required=True)
    parser.add_argument("--nuts", type=Path, required=True)
    args = parser.parse_args()

    buses, lines, links = read_prebuilt_network(args.osm)
    busmap = build_busmap(buses, lines, links, args.nuts)
    zone_lines, centres = derive_zone_lines(buses, lines, links, busmap)

    zones = read_commented_csv(NETWORK_DIR / "zones.csv")
    zones = zones[zones["kind"] == "internal"].reindex(
        columns=["zone_id", "name", "kind", "tso_zone", "country", "lat", "lon"]
    )
    areas = sorted(set(centres.index) - set(zones["zone_id"]))
    boundary = pd.DataFrame(
        {
            "zone_id": areas,
            "name": [AREA_NAMES[a] for a in areas],
            "kind": "boundary",
            "country": [a[:2] for a in areas],
        }
    )
    zones = pd.concat([zones, boundary], ignore_index=True)
    zones["lat"] = zones["zone_id"].map(centres["lat"]).round(3)
    zones["lon"] = zones["zone_id"].map(centres["lon"]).round(3)

    header = (NETWORK_DIR / "zones.csv").read_text(encoding="utf-8").splitlines()
    comments = [line for line in header if line.startswith("#")]
    with (NETWORK_DIR / "zones.csv").open("w", newline="", encoding="utf-8") as file:
        file.write("\n".join(comments) + "\n")
        zones.to_csv(file, index=False, quoting=csv.QUOTE_MINIMAL)

    zone_lines["susceptance_mw_per_rad"] = zone_lines["susceptance_mw_per_rad"].round(1)
    zone_lines["capacity_mw"] = zone_lines["capacity_mw"].round(1)
    with (NETWORK_DIR / "lines.csv").open("w", newline="", encoding="utf-8") as file:
        file.write(LINES_HEADER)
        zone_lines[list(LINE_COLUMNS)].to_csv(file, index=False)
    parts = zone_lines[["line_id", "ac_rating_mva", "dc_rating_mw"]].round(1)
    parts.to_csv(NETWORK_DIR / "lines_parts.csv", index=False)
    print(zone_lines.to_string(index=False))


LINES_HEADER = """\
# ASSUMPTION, not TSO data: one equivalent line per pair of zones, derived by
# scripts/derive_de22_network.py from PyPSA-Eur's prebuilt electricity network 0.8
# (OpenStreetMap data, 220 kV and above, in service). See docs/NETWORK.md.
# capacity_mw = 0.7 x summed thermal rating of the AC lines + rating of DC links.
# Data licence: ODbL 1.0, (c) OpenStreetMap contributors; see LICENSE-DATA.md.
"""

if __name__ == "__main__":
    main()
