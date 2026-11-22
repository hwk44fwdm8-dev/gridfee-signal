"""Efficiency classes of lignite, hard coal and gas plants per zone (see docs/DATA.md).

Reads the list of conventional power plants in Germany of Open Power System Data
(version 2020-10-01, from the BNetzA Kraftwerksliste and the UBA plant database):
https://data.open-power-system-data.org/conventional_power_plants/2020-10-01/conventional_power_plants_DE.csv

    python scripts/build_plant_efficiency.py --plants <conventional_power_plants_DE.csv> \
        --nuts <NUTS level 2 geojson>

The NUTS file is the one scripts/derive_de22_network.py uses. Writes
data/shares/plant_efficiency.csv: per fuel and zone, the share of the zone's capacity
in each efficiency class. A zone without a listed plant of a fuel gets Germany's classes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DE22 = ROOT / "data" / "networks" / "de22"
OUT = ROOT / "data" / "shares" / "plant_efficiency.csv"
FUELS = {"Lignite": "lignite", "Hard coal": "hard_coal", "Natural gas": "gas"}
CLASS_WIDTH = 0.02
HEADER = """\
# Share of a zone's capacity per fuel in each efficiency class (net electrical efficiency,
# classes of 0.02). Built by scripts/build_plant_efficiency.py from: Open Power System
# Data. 2020. Data Package Conventional power plants. Version 2020-10-01.
# https://doi.org/10.25832/conventional_power_plants/2020-10-01 (primary data: BNetzA
# Kraftwerksliste, Umweltbundesamt). Plants with status "operating" in that list, by
# net capacity; the plant's researched efficiency where the list has one, else its
# estimate from the year of commissioning. ASSUMPTION: the list is of 2020, not 2023;
# zones without a listed plant of the fuel get Germany's classes (docs/DATA.md).
"""


def plant_zones(plants: pd.DataFrame, nuts_file: Path) -> pd.Series:
    zone_of_nuts = pd.read_csv(DE22 / "zone_nuts2.csv", comment="#").set_index("nuts2")["zone_id"]
    regions = gpd.read_file(nuts_file).set_index("NUTS_ID").loc[zone_of_nuts.index, ["geometry"]]
    zones = regions.assign(zone_id=zone_of_nuts).dissolve("zone_id")
    points = gpd.GeoDataFrame(
        index=plants.index, geometry=gpd.points_from_xy(plants["lon"], plants["lat"]), crs=4326
    )
    inside = gpd.sjoin(points, zones, predicate="within", how="left")["zone_id"]
    outside = points[inside.isna()].to_crs(3035)
    nearest = gpd.sjoin_nearest(outside, zones.to_crs(3035), how="left")["zone_id"]
    return inside.fillna(nearest)


def efficiency_classes(plants: pd.DataFrame, zone_ids: list[str]) -> pd.DataFrame:
    """Rows technology, zone_id, efficiency, share; shares sum to 1 per fuel and zone."""
    plants = plants.assign(
        efficiency=(plants["efficiency"] / CLASS_WIDTH).round() * CLASS_WIDTH,
        technology=plants["energy_source"].map(FUELS),
    )
    rows = []
    for fuel, of_fuel in plants.groupby("technology"):
        national = of_fuel.groupby("efficiency")["capacity"].sum()
        by_zone = {
            zone: part.groupby("efficiency")["capacity"].sum()
            for zone, part in of_fuel.groupby("zone_id")
        }
        for zone_id in zone_ids:
            capacity = by_zone.get(zone_id, national)
            for efficiency, share in (capacity / capacity.sum()).items():
                rows.append((fuel, zone_id, round(efficiency, 2), round(share, 8)))
    return pd.DataFrame(rows, columns=["technology", "zone_id", "efficiency", "share"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plants", type=Path, required=True)
    parser.add_argument("--nuts", type=Path, required=True)
    args = parser.parse_args()

    plants = pd.read_csv(args.plants)
    plants = plants[(plants["status"] == "operating") & plants["energy_source"].isin(FUELS)]
    plants = plants.assign(
        efficiency=plants["efficiency_data"].fillna(plants["efficiency_estimate"]),
        capacity=plants["capacity_net_bnetza"],
    ).dropna(subset=["efficiency", "capacity", "lat", "lon"])
    plants["zone_id"] = plant_zones(plants, args.nuts)
    internal = pd.read_csv(DE22 / "zone_nuts2.csv", comment="#")["zone_id"].drop_duplicates()
    classes = efficiency_classes(plants, sorted(internal))
    OUT.write_text(HEADER + classes.to_csv(index=False))
    summary = plants.groupby("energy_source").apply(
        lambda p: pd.Series(
            {
                "MW": p["capacity"].sum(),
                "mean efficiency": (p["efficiency"] * p["capacity"]).sum() / p["capacity"].sum(),
                "MW with researched efficiency": p.loc[
                    p["efficiency_data"].notna(), "capacity"
                ].sum(),
            }
        )
    )
    print(summary.round(3).to_string())
    print(f"wrote {OUT} ({len(classes)} rows)")


if __name__ == "__main__":
    main()
