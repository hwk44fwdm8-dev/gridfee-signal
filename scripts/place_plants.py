"""Place the plants of data/plants/plants_2023.csv on the substations of a network.

    python scripts/place_plants.py --network data/networks/desub

Each plant goes to the nearest substation of substations.csv (rows of kind
"substation"; tee points take no plants), whatever its voltage levels; offshore wind
goes to the substation its cable ends at (grid_connection) where the plant table names
one. Writes, into the
network directory, plant_substations.csv (plant, substation, distance) and
capacity_by_substation.csv (MW per substation and technology). A plant without a
position goes to the substation that the located capacity of its technology in its
zone (zone_de22, else all of Germany) mostly sits on. Method and limits: docs/DATA.md.

The plants with location_basis plant_list have no position in plants_2023.csv (their
source states no licence); theirs are read from the file scripts/build_plant_table.py
writes beside the build (--positions), so that build has to have run on this machine.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from build_plant_table import PLANT_LIST_POSITIONS
from build_zone_shares import write

from gridfee.injections.plants import (
    PlantError,
    capacity_by_substation,
    distance_km,
    nearest_substation,
)

ROOT = Path(__file__).resolve().parents[1]
PLANTS = ROOT / "data" / "plants" / "plants_2023.csv"
DESUB = ROOT / "data" / "networks" / "desub"


def place(plants: pd.DataFrame, substations: pd.DataFrame) -> pd.DataFrame:
    """``plant_id``, ``substation_id``, ``distance_km`` and ``placement_basis`` per plant."""
    placement = nearest_substation(plants, substations)
    placement["placement_basis"] = "nearest"
    if "grid_connection" in plants:
        connected = plants["grid_connection"].notna()
        sites = substations.set_index("substation_id")[["lat", "lon"]]
        unknown = set(plants.loc[connected, "grid_connection"]) - set(sites.index)
        if unknown:
            raise PlantError(f"grid connections not in the substation list: {sorted(unknown)}")
        end = sites.loc[plants.loc[connected, "grid_connection"]]
        placement.loc[connected, "substation_id"] = end.index.to_numpy()
        placement.loc[connected, "distance_km"] = distance_km(
            plants.loc[connected, "lat"].to_numpy(),
            plants.loc[connected, "lon"].to_numpy(),
            end["lat"].to_numpy(),
            end["lon"].to_numpy(),
        )
        placement.loc[connected, "placement_basis"] = "grid_connection"
    lost = placement["substation_id"].isna()
    if lost.any():
        placed = plants.assign(substation_id=placement["substation_id"])[~lost]
        for keys in (["technology", "zone_de22"], ["technology"]):
            mw = placed.groupby([*keys, "substation_id"])["capacity_mw"].sum().reset_index()
            largest = mw.sort_values("capacity_mw").drop_duplicates(keys, keep="last")
            found = plants.loc[lost, keys].merge(largest, on=keys, how="left")["substation_id"]
            found.index = plants.index[lost]
            fill = found.notna() & placement.loc[lost, "substation_id"].isna()
            placement.loc[fill[fill].index, "substation_id"] = found[fill]
            placement.loc[fill[fill].index, "placement_basis"] = "largest_of_" + keys[-1]
    placement.insert(0, "plant_id", plants["plant_id"])
    return placement


def with_plant_list_positions(plants: pd.DataFrame, path: Path) -> pd.DataFrame:
    """The plants with the positions of ``path`` filled in for those of basis plant_list."""
    listed = plants["location_basis"] == "plant_list"
    if not listed.any():
        return plants
    if not path.exists():
        raise SystemExit(
            f"{path} is missing: it has the positions of the {listed.sum()} plants located by "
            "plant list, which are not committed; run scripts/build_plant_table.py first"
        )
    positions = pd.read_csv(path, comment="#").set_index("plant_id")
    ids = plants.loc[listed, "plant_id"]
    if not ids.isin(positions.index).all():
        raise SystemExit(f"{path} is not of this plant table; run build_plant_table.py again")
    plants = plants.copy()
    plants.loc[listed, ["lat", "lon"]] = positions.loc[ids, ["lat", "lon"]].to_numpy()
    return plants


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plants", type=Path, default=PLANTS)
    parser.add_argument("--positions", type=Path, default=PLANT_LIST_POSITIONS)
    parser.add_argument("--network", type=Path, default=DESUB, help="Has substations.csv.")
    args = parser.parse_args()
    plants = pd.read_csv(args.plants, comment="#", dtype={"ags": str})
    plants = with_plant_list_positions(plants, args.positions)
    substations = pd.read_csv(args.network / "substations.csv", comment="#")
    substations = substations[substations["kind"] == "substation"].reset_index(drop=True)
    placement = place(plants, substations)
    table = capacity_by_substation(plants, placement)
    source = [
        f"Source: {args.plants.name} (MaStR) and substations.csv of this directory.",
        "ASSUMPTION: a plant feeds in at the substation nearest to its position, whatever",
        "the voltage level. Offshore wind feeds in where its cable ends",
        "(data/plants/offshore_connections.csv); alpha ventus, whose substation is not in",
        "the list, at the one nearest to its position at sea.",
        "Built by scripts/place_plants.py; see docs/DATA.md.",
    ]
    write(
        args.network / "plant_substations.csv",
        [
            "The substation each plant is placed on, and how far away it is. placement_basis:",
            "nearest, grid_connection (offshore wind: where its cable ends), or for plants",
            "without a position largest_of_zone_de22 /",
            "largest_of_technology (the substation with most of that technology's located",
            "capacity in the plant's zone, or in Germany).",
            *source,
        ],
        placement.assign(distance_km=placement["distance_km"].round(1)),
    )
    write(
        args.network / "capacity_by_substation.csv",
        ["MW per substation and technology; the totals per technology are the plants'.", *source],
        table.assign(capacity_mw=table["capacity_mw"].round(3)),
    )
    weight = plants["capacity_mw"]
    for technology, rows in plants.groupby("technology"):
        km = placement.loc[rows.index, "distance_km"]
        mean = (km * weight[rows.index]).sum() / weight[rows.index][km.notna()].sum()
        print(
            f"{technology}: {weight[rows.index].sum():,.1f} MW, mean {mean:.0f} km, "
            f"max {km.max():.0f} km, without position {weight[rows.index][km.isna()].sum():,.1f} MW"
        )


if __name__ == "__main__":
    main()
