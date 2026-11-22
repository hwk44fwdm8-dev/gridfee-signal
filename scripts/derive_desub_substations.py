"""Derive the substation list of the substation-level network (data/networks/desub).

The substations, their names and their voltage levels come from the circuits and
transformers the four German TSOs publish in JAO's Core Static Grid Model (March
2023); the NUTS2 region of each is the one found for the 38-region network. JAO
publishes no coordinates and restricts reuse of its data, so each position is that
of the OpenStreetMap substation of the same name (via PyPSA-Eur's prebuilt network
0.8 and the OpenStreetMap API, ODbL 1.0); JAO's map only narrows the search.

    python scripts/derive_desub_substations.py --osm DIR --nuts FILE \\
        --jao-workbook XLSX --jao-map HTML --osm-tags JSON

``--osm-tags`` is a cache of the OpenStreetMap tags of the substations; it is
downloaded when the file does not exist.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from pathlib import Path

import derive_de38_network as de38
import numpy as np
import pandas as pd
import requests

from gridfee.network import jao

NETWORK_DIR = de38.NETWORKS / "desub"
SEARCH_KM = 40.0  # JAO's map is schematic for TenneT and TransnetBW: up to 30 km off
NEAREST_KM = 3.0
NAME_KEYS = ("name", "short_name", "alt_name", "official_name", "old_name", "ref")
FILLER = (
    r"\b(umspannwerk|umspannanlage|uw|ua|station|schaltanlage|schaltwerk|kv|380|220|110|400"
    r"|tennet|amprion|50hertz|transnetbw|netzknoten)\b"
)
ENDINGS = {"/N": "nord", "/S": "sued", "/W": "west", "/O": "ost"}


def normalise(name: str) -> str:
    name = name.lower()
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        name = name.replace(umlaut, plain)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", re.sub(FILLER, " ", name))


def spellings(name: str) -> list[set[str]]:
    """Ways to write a JAO name, most specific first."""
    stem = re.sub(r"/[A-Za-z0-9]+$", "", name)
    full = {normalise(name)}
    for ending, word in ENDINGS.items():
        if name.endswith(ending):
            full.add(normalise(stem) + word)
    loose = {normalise(stem), normalise(re.sub(r"\s+\d+$", "", name))}
    return [{s for s in full if len(s) >= 4}, {s for s in loose if len(s) >= 4}]


def slug(name: str) -> str:
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        name = name.replace(umlaut, plain)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def distance_km(lon, lat, lons, lats):
    return np.hypot((lons - lon) * 111.32 * np.cos(np.radians(lat)), (lats - lat) * 110.57)


def osm_substations(osm_dir: Path, cache: Path) -> pd.DataFrame:
    """German OpenStreetMap substations at 220 kV and above with their name tags."""
    buses = pd.read_csv(osm_dir / "buses.csv")
    buses = buses[(buses["country"] == "DE") & (buses["dc"] == "f") & (buses["voltage"] >= 220)]
    stations = buses.groupby("tags").agg(lon=("x", "mean"), lat=("y", "mean"))
    if not cache.exists():
        tags: dict[str, dict] = {}
        for kind in ("way", "relation"):
            ids = sorted(t.split("/")[1] for t in stations.index if t.startswith(kind + "/"))
            for start in range(0, len(ids), 100):
                reply = requests.get(
                    f"https://api.openstreetmap.org/api/0.6/{kind}s.json",
                    params={kind + "s": ",".join(ids[start : start + 100])},
                    headers={"User-Agent": "gridfee-signal"},
                    timeout=60,
                )
                reply.raise_for_status()
                for element in reply.json()["elements"]:
                    tags[f"{kind}/{element['id']}"] = element.get("tags", {})
                time.sleep(1)
        cache.write_text(json.dumps(tags), encoding="utf-8")
    tags = json.loads(cache.read_text(encoding="utf-8"))
    stations["names"] = [
        {normalise(v) for k, v in tags.get(i, {}).items() if k in NAME_KEYS and v}
        for i in stations.index
    ]
    return stations


def match(
    name: str, lon: float, lat: float, stations: pd.DataFrame, taken: frozenset[str] = frozenset()
) -> tuple[str, str]:
    """OpenStreetMap substation for a JAO name and how it was found.

    ``taken`` are substations that carry another JAO name in full: a partial match
    with one of them (``Frankfurt/SW`` in ``Frankfurt Nord``) is not accepted.
    """
    distance = distance_km(lon, lat, stations["lon"].to_numpy(), stations["lat"].to_numpy())
    near = np.flatnonzero(distance < SEARCH_KM)
    near = near[np.argsort(distance[near])]
    full, loose = spellings(name)
    tests = (
        ("name", lambda a, b: a == b, full),
        ("name", lambda a, b: a == b, loose),
        ("name_part", lambda a, b: min(len(a), len(b)) >= 5 and (a in b or b in a), full | loose),
    )
    for basis, same, wanted in tests:
        for i in near:
            if basis == "name_part" and stations.index[i] in taken:
                continue
            if any(same(a, b) for a in wanted for b in stations["names"].iloc[i]):
                return stations.index[i], basis
    nearest = int(distance.argmin())
    return stations.index[nearest], "nearest" if distance[nearest] < NEAREST_KM else "none"


def placed_circuits(workbook: Path, jao_map: Path, nuts: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """JAO's German AC circuits with the place and region of both ends; the map positions."""
    # A few circuits are listed in sections under one EIC code (a line with a branch
    # to a third substation). Each section is a circuit of its own here: the later
    # ones get "/2", "/3" after the code.
    listed = jao.read_sheet(workbook, "Lines")
    ends = listed[["sub1", "sub2"]].apply(lambda row: "|".join(sorted(row)), axis=1)
    section = ends.groupby(listed["eic"]).transform(lambda s: pd.factorize(s)[0] + 1)
    listed["eic"] = listed["eic"].where(section == 1, listed["eic"] + "/" + section.astype(str))
    circuits = jao.german_circuits(listed, jao.read_sheet(workbook, "Tielines"))
    circuits = circuits[~circuits.index.isin(de38.HVDC_EIC)]
    corrections = pd.read_csv(de38.NETWORK_DIR / "jao_corrections.csv", comment="#")
    corrections = corrections.set_index("eic")
    circuits.loc[corrections.index, ["sub1", "sub2"]] = corrections[["sub1", "sub2"]]
    places = pd.read_csv(de38.NETWORK_DIR / "jao_places.csv", comment="#")
    alias = places[places["kind"] == "alias"].set_index("name")["value"]
    circuits[["sub1", "sub2"]] = circuits[["sub1", "sub2"]].replace(alias)
    circuits = jao.name_tee_points(circuits)
    on_map = jao.substation_positions(circuits, jao.read_map_positions(jao_map))
    circuits = de38.resolve_places(circuits, de38.place_zones(on_map, nuts), places)
    return circuits, on_map


def placed_transformers(workbook: Path, circuits: pd.DataFrame) -> pd.DataFrame:
    """JAO's German transformers with the place (as in ``circuits``) they stand in."""
    place = pd.concat([circuits.set_index("sub1")["bus0"], circuits.set_index("sub2")["bus1"]])
    place = place.groupby(level=0).first()
    places = pd.read_csv(de38.NETWORK_DIR / "jao_places.csv", comment="#")
    alias = places[places["kind"] == "alias"].set_index("name")["value"]
    transformers = jao.read_transformers(workbook)
    return transformers.assign(place=transformers["substation"].replace(alias).map(place))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--osm", type=Path, required=True)
    parser.add_argument("--nuts", type=Path, required=True)
    parser.add_argument("--jao-workbook", type=Path, required=True)
    parser.add_argument("--jao-map", type=Path, required=True)
    parser.add_argument("--osm-tags", type=Path, required=True)
    args = parser.parse_args()

    circuits, on_map = placed_circuits(args.jao_workbook, args.jao_map, args.nuts)
    transformers = placed_transformers(args.jao_workbook, circuits)
    ends = pd.concat(
        [
            circuits[[f"bus{i}", f"zone{i}", "voltage_kv", "tso", "sheet"]].set_axis(
                ["place", "nuts2", "kv", "tso", "sheet"], axis=1
            )
            for i in (0, 1)
        ]
    )
    ends = ends[ends["nuts2"].str.fullmatch(r"DE[0-9A-G]{2}")]

    stations = osm_substations(args.osm, args.osm_tags)
    overrides = pd.read_csv(NETWORK_DIR / "substation_positions.csv", comment="#")
    overrides = overrides.set_index("name")
    exact = {
        name: match(name, *on_map.loc[name], stations)
        for name in ends["place"].unique()
        if name in on_map.index
    }
    taken = {name: osm_id for name, (osm_id, basis) in exact.items() if basis == "name"}
    rows = []
    for name, group in ends.groupby("place"):
        levels = {jao.voltage_level(v) for v in group["kv"]}
        units = transformers[transformers["place"] == name]
        for unit in units.itertuples():
            levels |= {jao.voltage_level(unit.primary_kv), jao.voltage_level(unit.secondary_kv)}
        own = group[group["sheet"] == "Lines"]["tso"].value_counts()
        tee = name.startswith("Y-") or name.endswith("(Abzweig)") or " - " in name
        row = {
            "name": name,
            "kind": "tee" if tee else "substation",
            "tso": own.index[0] if len(own) else group["tso"].iloc[0],
            "nuts2": group["nuts2"].iloc[0],
            "has_220kv": 220 in levels,
            "has_380kv": 380 in levels,
            "transformers": len(units),
            "osm_id": "",
            "position_basis": "neighbours",
        }
        if name in overrides.index:
            row["osm_id"], row["position_basis"] = overrides.at[name, "osm_id"], "manual"
        elif not tee and name in on_map.index:
            row["osm_id"], row["position_basis"] = match(
                name,
                *on_map.loc[name],
                stations,
                frozenset(set(taken.values()) - {taken.get(name)}),
            )
            if row["position_basis"] == "none":
                row["osm_id"], row["position_basis"] = "", "neighbours"
        rows.append(row)
    subs = pd.DataFrame(rows).set_index("name")
    subs["lon"] = subs["osm_id"].map(stations["lon"])
    subs["lat"] = subs["osm_id"].map(stations["lat"])

    # Tee points and substations OpenStreetMap does not have: the mean of the places
    # their circuits lead to, repeated until every one has a position.
    links = circuits[circuits["bus0"].isin(subs.index) & circuits["bus1"].isin(subs.index)]
    pairs = pd.concat(
        [links[["bus0", "bus1"]], links[["bus1", "bus0"]].set_axis(["bus0", "bus1"], axis=1)]
    ).drop_duplicates()
    fixed = subs["lon"].notna()
    for _ in range(50):
        known = pairs[pairs["bus1"].map(subs["lon"].notna())]
        mean = (
            known.assign(lon=known["bus1"].map(subs["lon"]), lat=known["bus1"].map(subs["lat"]))
            .groupby("bus0")[["lon", "lat"]]
            .mean()
        )
        mean = mean[~fixed.reindex(mean.index)]
        subs.loc[mean.index, ["lon", "lat"]] = mean
    if subs["lon"].isna().any():
        raise ValueError(f"no position for {list(subs.index[subs['lon'].isna()])}")

    report = pd.read_csv(de38.NETWORKS / "de22" / "zone_nuts2.csv", comment="#")
    subs["report_zone"] = subs["nuts2"].map(report.set_index("nuts2")["zone_id"])
    subs["substation_id"] = [slug(name) for name in subs.index]
    if subs["substation_id"].duplicated().any():
        raise ValueError("substation ids are not unique")
    subs[["lat", "lon"]] = subs[["lat", "lon"]].round(4)
    columns = ["substation_id", "name", "kind", "tso", "nuts2", "report_zone", "lat", "lon"]
    columns += ["has_220kv", "has_380kv", "transformers", "position_basis", "osm_id"]
    out = subs.reset_index().sort_values("substation_id")[columns]
    (NETWORK_DIR / "substations.csv").write_text(HEADER + out.to_csv(index=False), encoding="utf-8")
    print(out["position_basis"].value_counts().to_string())
    print(out[["has_220kv", "has_380kv"]].value_counts().to_string())


HEADER = """\
# Substations of the substation-level network, written by
# scripts/derive_desub_substations.py. One row per place where circuits of the four
# German TSOs end in JAO's Core Static Grid Model (March 2023,
# https://www.jao.eu/static-grid-model); name is JAO's name. kind "tee" is a branch
# point on a line, not a substation. has_220kv / has_380kv: the voltage levels of
# the circuits and transformers JAO lists there; transformers: how many it lists.
# Positions are OpenStreetMap's (ODbL 1.0), not JAO's. position_basis:
#   name        OpenStreetMap substation (osm_id) of the same name
#   name_part   one name contains the other
#   nearest     unnamed OpenStreetMap substation within 3 km of JAO's map position
#   manual      set in substation_positions.csv, with the reason
#   neighbours  ASSUMPTION: mean of the places its circuits lead to (tee points and
#               substations OpenStreetMap lacks)
# nuts2 is the region of the 38-region network, report_zone the zone of de22.
"""

if __name__ == "__main__":
    main()
