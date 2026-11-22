"""Rebuild data/networks/de38 from JAO's Core Static Grid Model.

The 38-region network takes every AC line from the circuits that the four German
TSOs publish through JAO: reactance, voltage and maximum current. DC links come
from PyPSA-Eur's OpenStreetMap network as in the 22-zone network, because JAO
gives a cable's current but not the converter's power.

Needs ``pip install -e ".[derive]"`` and, besides the two downloads of
scripts/derive_de22_network.py, two files from https://www.jao.eu/static-grid-model
(Core Static Grid Model, publication of March 2023):

- the workbook, ``20230301_Core Static Grid Model_3.xlsx``
- the map, the ``.html`` file inside ``20230224_Core Static Grid Model Map_3.zip``

    python scripts/derive_de38_network.py --osm <dir> --nuts <file.geojson> \\
        --jao-workbook <file.xlsx> --jao-map <file.html>
"""

from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
from derive_de22_network import AREA_NAMES, NETWORKS, build_busmap, read_commented_csv

from gridfee.network import jao
from gridfee.network.pypsa_eur import LINE_COLUMNS, derive_zone_lines, read_prebuilt_network

NETWORK_DIR = NETWORKS / "de38"
# The two HVDC cables in the Tielines sheet (ALEGrO, NordLink); see the docstring.
HVDC_EIC = {"22T201903146---W", "10T-NO-DE-00002J"}
COUNTRY_AREA = {"DK": "DK1"}


def place_zones(positions: pd.DataFrame, nuts_file: Path) -> pd.Series:
    """NUTS2 region of each position in Germany, country code elsewhere."""
    regions = gpd.read_file(nuts_file)[["NUTS_ID", "CNTR_CODE", "geometry"]]
    points = gpd.GeoDataFrame(
        positions, geometry=gpd.points_from_xy(positions["x"], positions["y"]), crs="EPSG:4326"
    )
    found = gpd.sjoin(points, regions, predicate="within", how="left")
    found = found[~found.index.duplicated()]
    country = found["CNTR_CODE"].replace(COUNTRY_AREA)
    return found["NUTS_ID"].where(found["CNTR_CODE"] == "DE", country).dropna()


def resolve_places(circuits: pd.DataFrame, zones: pd.Series, places: pd.DataFrame) -> pd.DataFrame:
    """Add ``bus0``, ``bus1`` (places) and ``zone0``, ``zone1`` to the circuits."""
    fixed = places[places["kind"] == "zone"]
    by_tso = fixed[fixed["tso"].notna()].set_index(["name", "tso"])["value"]
    anywhere = fixed[fixed["tso"].isna()].set_index("name")["value"]

    circuits = circuits.copy()
    for end in ("sub1", "sub2"):
        # A name that one TSO uses for a different substation gets its own place.
        own = pd.MultiIndex.from_arrays([circuits[end], circuits["tso"]]).isin(by_tso.index)
        circuits.loc[own, end] = circuits.loc[own, end] + " (" + circuits.loc[own, "tso"] + ")"
    place = jao.merge_stubs(circuits)
    # Tee points are not taken from the map, which draws them schematically.
    zone = pd.concat(
        [
            zones[~zones.index.str.startswith("Y-")].drop(anywhere.index, errors="ignore"),
            anywhere,
            pd.Series(by_tso.to_numpy(), index=[f"{n} ({t})" for n, t in by_tso.index]),
        ]
    )
    # A tee point that jao_places.csv places is listed there without its circuit.
    tees = [n for n in place.index if " [" in n and n.split(" [")[0] in anywhere.index]
    zone = pd.concat([zone, anywhere.loc[[n.split(" [")[0] for n in tees]].set_axis(tees)])
    place_zone = {}
    for target in place.unique():
        members = place.index[place == target]
        candidates = {zone[n] for n in members if n in zone.index}
        if len(candidates) > 1 and any(not c.startswith("DE") for c in candidates):
            raise ValueError(f"{target}: stub across a border, zones {sorted(candidates)}")
        if target in zone.index:
            place_zone[target] = zone[target]
        elif candidates:
            place_zone[target] = sorted(candidates)[0]
    circuits["bus0"], circuits["bus1"] = circuits["sub1"].map(place), circuits["sub2"].map(place)
    # A tee point lies in the zone of the substation at the end of its shortest leg.
    legs = circuits[circuits["bus0"] != circuits["bus1"]].sort_values("length_km")
    legs = pd.concat(
        [
            legs[["bus0", "bus1"]].set_axis(["tee", "other"], axis=1),
            legs[["bus1", "bus0"]].set_axis(["tee", "other"], axis=1),
        ]
    ).sort_index(kind="stable")
    legs = legs.loc[circuits.sort_values("length_km").index.intersection(legs.index).unique()]
    changed = True
    while changed:
        changed = False
        for row in legs[legs["tee"].str.startswith("Y-")].itertuples():
            if row.tee not in place_zone and row.other in place_zone:
                place_zone[row.tee] = place_zone[row.other]
                changed = True
    circuits["zone0"] = circuits["bus0"].map(place_zone)
    circuits["zone1"] = circuits["bus1"].map(place_zone)
    missing = circuits[circuits["zone0"].isna() | circuits["zone1"].isna()]
    if len(missing):
        raise ValueError(
            "substations without a position; add them to jao_places.csv:\n"
            + missing[["name", "tso", "sub1", "sub2"]].to_string()
        )
    return circuits


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--osm", type=Path, required=True)
    parser.add_argument("--nuts", type=Path, required=True)
    parser.add_argument("--jao-workbook", type=Path, required=True)
    parser.add_argument("--jao-map", type=Path, required=True)
    args = parser.parse_args()

    circuits = jao.german_circuits(
        jao.read_sheet(args.jao_workbook, "Lines"), jao.read_sheet(args.jao_workbook, "Tielines")
    )
    circuits = circuits[~circuits.index.isin(HVDC_EIC)]
    corrections = pd.read_csv(NETWORK_DIR / "jao_corrections.csv", comment="#").set_index("eic")
    circuits.loc[corrections.index, ["sub1", "sub2"]] = corrections[["sub1", "sub2"]]
    places = pd.read_csv(NETWORK_DIR / "jao_places.csv", comment="#")
    alias = places[places["kind"] == "alias"].set_index("name")["value"]
    circuits[["sub1", "sub2"]] = circuits[["sub1", "sub2"]].replace(alias)
    circuits = jao.name_tee_points(circuits)
    positions = jao.substation_positions(circuits, jao.read_map_positions(args.jao_map))
    circuits = resolve_places(circuits, place_zones(positions, args.nuts), places)

    ends = pd.concat(
        [
            circuits[["bus0", "zone0"]].set_axis(["bus", "zone"], axis=1),
            circuits[["bus1", "zone1"]].set_axis(["bus", "zone"], axis=1),
        ]
    ).drop_duplicates("bus")
    busmap = ends.set_index("bus")["zone"]
    osm_buses, osm_lines, links = read_prebuilt_network(args.osm)
    osm_busmap = build_busmap(osm_buses, osm_lines, links, args.nuts, NETWORK_DIR)
    # Zone centres as in the 22-zone network: mean of the OpenStreetMap buses, which
    # are surveyed positions. Places the map does not show (the foreign ends of tie
    # lines) are put there.
    _, centres = derive_zone_lines(osm_buses, osm_lines, links, osm_busmap)
    where = positions.reindex(busmap.index)
    jao_buses = pd.DataFrame(
        {
            "voltage": jao.BASE_KV,
            "dc": "f",
            "x": where["x"].fillna(busmap.map(centres["lon"])),
            "y": where["y"].fillna(busmap.map(centres["lat"])),
        },
        index=busmap.index,
    )
    link_buses = osm_buses.loc[sorted(set(links["bus0"]) | set(links["bus1"]))]
    link_buses = link_buses[link_buses.index.isin(osm_busmap.index)]
    buses = pd.concat([jao_buses, link_buses[["voltage", "dc", "x", "y"]]])
    zone_lines, _ = derive_zone_lines(
        buses, jao.to_lines(circuits), links, pd.concat([busmap, osm_busmap.loc[link_buses.index]])
    )

    crossing = circuits[circuits["zone0"] != circuits["zone1"]]
    pairs = pd.DataFrame({"a": crossing["zone0"], "b": crossing["zone1"]}).apply(sorted, axis=1)
    listing = pd.DataFrame(
        {
            "line_id": pairs.str.join("-").to_numpy(),
            "eic": crossing.index.to_numpy(),
            "voltage_kv": crossing["voltage_kv"].astype(int).to_numpy(),
            "limit_kind": crossing["limit_kind"].to_numpy(),
        }
    ).sort_values(["line_id", "eic"])
    (NETWORK_DIR / "jao_circuits.csv").write_text(
        CIRCUITS_HEADER + listing.to_csv(index=False), encoding="utf-8"
    )

    zones = read_commented_csv(NETWORK_DIR / "zones.csv")
    zones = zones[zones["kind"] == "internal"].reindex(
        columns=["zone_id", "name", "kind", "tso_zone", "country", "lat", "lon"]
    )
    used = set(zone_lines["from_zone"]) | set(zone_lines["to_zone"])
    areas = sorted(used - set(zones["zone_id"]))
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
    parts = parts.merge(
        listing.groupby("line_id").size().rename("circuits").reset_index(), how="left"
    ).fillna({"circuits": 0})
    parts["circuits"] = parts["circuits"].astype(int)
    parts.to_csv(NETWORK_DIR / "lines_parts.csv", index=False)
    print(zone_lines.to_string(index=False))


LINES_HEADER = """\
# One equivalent line per pair of zones, derived by scripts/derive_de38_network.py.
# AC: the circuits between the two zones that the German TSOs publish in JAO's Core
# Static Grid Model (March 2023), listed in jao_circuits.csv. Their reactances give
# the susceptance; capacity_mw = 0.7 x the sum of sqrt(3) x voltage x maximum current.
# The currents and reactances are TSO data. ASSUMPTIONS: the 0.7 security margin,
# the zone each substation is put in (from JAO's map), and one line per pair.
# DC links: rating from PyPSA-Eur's prebuilt network 0.8 (OpenStreetMap, ODbL 1.0).
# See docs/NETWORK.md.
"""
CIRCUITS_HEADER = """\
# The circuits behind each AC line of lines.csv, by the EIC code under which JAO's
# Core Static Grid Model (March 2023, https://www.jao.eu/static-grid-model) lists
# them. Written by scripts/derive_de38_network.py. limit_kind says which published
# current was used: fixed, period (lowest seasonal limit) or dlr_max (upper end of
# the dynamic line rating: an upper bound). The currents themselves are JAO's data
# and are not reproduced here; look them up by EIC code in the workbook.
"""

if __name__ == "__main__":
    main()
