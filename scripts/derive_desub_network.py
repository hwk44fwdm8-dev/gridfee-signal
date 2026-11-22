"""Derive the substation-level network: one bus per substation and voltage level.

Every circuit and every 380/220 kV transformer the four German TSOs publish in
JAO's Core Static Grid Model (March 2023) becomes a branch of its own, on the
substations of data/networks/desub/substations.csv. Neighbouring countries stay
one bus each.

    python scripts/derive_desub_network.py --nuts FILE --jao-workbook XLSX \\
        --jao-map HTML --out DIR

``DIR`` receives ``zones.csv`` and ``lines.csv`` in the format of the other
networks. They hold each branch's susceptance and limit, which are JAO's
reactances and currents in another form, so they are not committed (JAO's terms
restrict reproduction). The repository keeps what is not JAO's data: the buses
(``buses.csv``) and which circuit or transformer joins which (``branches.csv``,
by EIC code).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import derive_de38_network as de38
import derive_desub_substations as substations
import numpy as np
import pandas as pd

from gridfee.network import jao
from gridfee.network.loader import load_network

NETWORK_DIR = substations.NETWORK_DIR
MARGIN = 0.7  # as on the lines of the other networks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nuts", type=Path, required=True)
    parser.add_argument("--jao-workbook", type=Path, required=True)
    parser.add_argument("--jao-map", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    subs = pd.read_csv(NETWORK_DIR / "substations.csv", comment="#").set_index("name")
    circuits, _ = substations.placed_circuits(args.jao_workbook, args.jao_map, args.nuts)
    level = circuits["voltage_kv"].map(jao.voltage_level)

    def bus(place: pd.Series, zone: pd.Series, kilovolt: pd.Series) -> pd.Series:
        german = place.isin(subs.index)
        ids = place.map(subs["substation_id"]) + "-" + kilovolt.astype(str)
        return ids.where(german, zone)

    lines = pd.DataFrame(
        {
            "line_id": circuits.index,
            "kind": "line",
            "from_zone": bus(circuits["bus0"], circuits["zone0"], level),
            "to_zone": bus(circuits["bus1"], circuits["zone1"], level),
            "voltage_kv": level,
            "limit_kind": circuits["limit_kind"],
            "susceptance_mw_per_rad": circuits["voltage_kv"] ** 2 / circuits["x"],
            "capacity_mw": MARGIN
            * np.sqrt(3)
            * circuits["voltage_kv"]
            * circuits["max_current_a"]
            / 1000.0,
        }
    )
    lines = lines[lines["from_zone"] != lines["to_zone"]]

    units = substations.placed_transformers(args.jao_workbook, circuits)
    sides = units[["primary_kv", "secondary_kv"]].map(jao.voltage_level)
    units = units[units["place"].isin(subs.index) & (sides["primary_kv"] != sides["secondary_kv"])]
    current = units["i_fixed"].fillna(units["i_min"]).fillna(units["i_max"])
    kind = np.where(
        units["i_fixed"].notna(), "fixed", np.where(units["i_min"].notna(), "min", "max")
    )
    station = units["place"].map(subs["substation_id"])
    transformers = pd.DataFrame(
        {
            "line_id": units["eic"],
            "kind": "transformer",
            "from_zone": station + "-380",
            "to_zone": station + "-220",
            "voltage_kv": 380,
            "limit_kind": kind,
            "susceptance_mw_per_rad": units["primary_kv"] ** 2 / units["x"],
            "capacity_mw": MARGIN * np.sqrt(3) * units["primary_kv"] * current / 1000.0,
        }
    )
    transformers = transformers[transformers["susceptance_mw_per_rad"].notna()]
    transformers = transformers[transformers["capacity_mw"] > 0]

    links = pd.read_csv(NETWORK_DIR / "dc_links.csv", comment="#")
    by_id = subs.reset_index().set_index("substation_id")
    top = np.where(by_id["has_380kv"], "-380", "-220")
    dc = pd.DataFrame(
        {
            "line_id": links["line_id"],
            "kind": "dc_link",
            "from_zone": links["substation_id"]
            + pd.Series(top, by_id.index).loc[links["substation_id"]].to_numpy(),
            "to_zone": links["area"],
            "voltage_kv": 0,
            "limit_kind": "rating",
            "susceptance_mw_per_rad": links["capacity_mw"].astype(float),
            "capacity_mw": links["capacity_mw"].astype(float),
        }
    )
    branches = pd.concat([lines, transformers, dc], ignore_index=True)
    if branches["line_id"].duplicated().any():
        raise ValueError("branch ids are not unique")

    # Buses: every level a substation has, whether or not a branch ends there. Their
    # tso_zone is that of the NUTS2 region, as in the 38-region network: it says
    # which control area's load and feed-in a bus gets a share of.
    regions = pd.read_csv(de38.NETWORK_DIR / "zones.csv", comment="#").set_index("zone_id")
    rows = []
    for name, row in subs.iterrows():
        for kilovolt in (380, 220):
            if row[f"has_{kilovolt}kv"]:
                rows.append(
                    {
                        "zone_id": f"{row['substation_id']}-{kilovolt}",
                        "name": f"{name} {kilovolt} kV",
                        "kind": "internal",
                        "tso_zone": regions.at[row["nuts2"], "tso_zone"],
                        "country": "",
                        "lat": row["lat"],
                        "lon": row["lon"],
                        "substation_id": row["substation_id"],
                        "voltage_kv": kilovolt,
                    }
                )
    buses = pd.DataFrame(rows)
    used = set(branches["from_zone"]) | set(branches["to_zone"])
    missing = used - set(buses["zone_id"])
    abroad = regions.reset_index()
    abroad = abroad[abroad["zone_id"].isin(missing)].assign(substation_id="", voltage_kv=0)
    if missing - set(abroad["zone_id"]):
        raise ValueError(
            f"branches end at unknown buses: {sorted(missing - set(abroad['zone_id']))}"
        )
    buses = pd.concat([buses, abroad[buses.columns]], ignore_index=True)

    # The model needs one connected network. Parts that JAO's data leave separate
    # are reported and left out.
    parent = {b: b for b in buses["zone_id"]}

    def find(b: str) -> str:
        while parent[b] != b:
            parent[b] = parent[parent[b]]
            b = parent[b]
        return b

    for a, b in zip(branches["from_zone"], branches["to_zone"], strict=True):
        parent[find(a)] = find(b)
    buses["island"] = buses["zone_id"].map(find)
    main_island = buses["island"].value_counts().index[0]
    buses["connected"] = buses["island"] == main_island
    kept = set(buses.loc[buses["connected"], "zone_id"])
    branches["connected"] = branches["from_zone"].isin(kept)
    print(buses[~buses["connected"]].groupby("island")["zone_id"].apply(list).to_string())

    columns = ["zone_id", "substation_id", "voltage_kv", "connected"]
    (NETWORK_DIR / "buses.csv").write_text(
        BUSES_HEADER + buses[columns].to_csv(index=False), encoding="utf-8"
    )
    columns = ["line_id", "kind", "from_zone", "to_zone", "voltage_kv", "limit_kind", "connected"]
    listing = branches[columns].sort_values(["kind", "line_id"])
    (NETWORK_DIR / "branches.csv").write_text(
        BRANCHES_HEADER + listing.to_csv(index=False), encoding="utf-8"
    )

    args.out.mkdir(parents=True, exist_ok=True)
    zones = buses[buses["connected"]]
    zones[["zone_id", "name", "kind", "tso_zone", "country", "lat", "lon"]].to_csv(
        args.out / "zones.csv", index=False
    )
    out = branches[branches["connected"]].copy()
    out["susceptance_mw_per_rad"] = out["susceptance_mw_per_rad"].round(1)
    out["capacity_mw"] = out["capacity_mw"].round(1)
    out[["line_id", "from_zone", "to_zone", "susceptance_mw_per_rad", "capacity_mw"]].to_csv(
        args.out / "lines.csv", index=False
    )
    network = load_network(args.out)
    print(f"{len(network.zones)} buses, {len(network.lines)} branches")
    print(branches.groupby(["kind", "connected"]).size().to_string())


BUSES_HEADER = """\
# Buses of the substation-level network, written by scripts/derive_desub_network.py:
# one per substation of substations.csv and voltage level, plus one per neighbouring
# bidding zone (no substation). connected is False for buses that no published
# circuit or transformer joins to the rest; they are left out of the model.
"""
BRANCHES_HEADER = """\
# Branches of the substation-level network, written by
# scripts/derive_desub_network.py: the circuits (kind line) and 380/220 kV
# transformers that JAO's Core Static Grid Model (March 2023,
# https://www.jao.eu/static-grid-model) lists for the four German TSOs, by EIC code
# (line_id), and the DC links of dc_links.csv. limit_kind says which published
# current sets the limit: fixed, period (lowest seasonal limit), dlr_max or max
# (upper end of a weather-dependent range: an upper bound), min (its lower end).
# Reactances and currents are JAO's data and are not reproduced here; the script
# writes them to a directory outside the repository. ASSUMPTIONS: the 0.7 margin on
# every limit, transformers included; a transformer's current is taken at its
# primary voltage; phase shifters are left out.
"""

if __name__ == "__main__":
    main()
