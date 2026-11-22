"""Build the per-plant table data/plants/plants_2023.csv from MaStR.

    python scripts/extract_mastr.py --zip <url or path> --out <mastr dir>
    python scripts/build_plant_table.py --mastr <mastr dir> --plant-list <csv>

The units, their technology and their capacity are those of
scripts/build_zone_shares.py, so the totals agree with data/shares/capacity_by_zone.csv.
--plant-list is Open Power System Data's list of conventional power plants
(conventional_power_plants_DE.csv, see scripts/build_plant_efficiency.py); it gives
the position of large units by their BNetzA plant number, which MaStR's public export
withholds. Method and limits: docs/DATA.md.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from build_zone_shares import (
    DE22,
    FUELS,
    GERMANY,
    NUCLEAR_DATE,
    OFFSHORE,
    ONSHORE,
    PUMPED_STORAGE,
    RESIDUAL_FILES,
    SHARES,
    offshore_zone,
    read_units,
    write,
)

from gridfee.injections.plants import (
    BASES,
    locate_units,
    plant_number_positions,
    reference_positions,
)
from gridfee.injections.zone_shares import fill_zone_from_district, in_operation, zone_of_ags

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "plants" / "plants_2023.csv"
# Not committed (data/cache is ignored): the plant list states no licence.
PLANT_LIST_POSITIONS = ROOT / "data" / "cache" / "plant_list_positions.csv"
COLUMNS = {
    "EinheitMastrNummer": "plant_id",
    "Gemeindeschluessel": "ags",
    "Postleitzahl": "postcode",
    "Landkreis": "district",
    "Kraftwerksnummer": "plant_number",
    "Breitengrad": "lat",
    "Laengengrad": "lon",
}


def unit_name(units: pd.DataFrame) -> pd.Series:
    name = units["NameStromerzeugungseinheit"].fillna(units["NameKraftwerk"])
    return (
        name.fillna(units["NameWindpark"]).str.replace(r"[\s,;#\"]+", " ", regex=True).str.strip()
    )


def select_units(
    mastr: Path, date: str, network: Path, shares_dir: Path
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The units behind capacity_by_zone.csv, and all units as position reference.

    The first frame has technology, capacity_mw and zone.

    Follows scripts/build_zone_shares.py step by step; units it leaves out for lack
    of a zone are kept here, with a missing zone.
    """
    regions = pd.read_csv(network / "zone_nuts2.csv", comment="#").set_index("nuts2")["zone_id"]
    prefixes = pd.read_csv(SHARES / "ags_nuts2.csv", comment="#", dtype=str)
    prefix_to_zone = dict(zip(prefixes["ags_prefix"], prefixes["nuts2"].map(regions), strict=True))
    catalogue = pd.read_csv(mastr / "Katalogwerte.csv", dtype=str).set_index("Id")["Wert"]

    def zone_of(units: pd.DataFrame) -> pd.Series:
        return zone_of_ags(units["Gemeindeschluessel"], prefix_to_zone)

    def part(units: pd.DataFrame, technology, zone: pd.Series, column: str) -> pd.DataFrame:
        return units.assign(technology=technology, zone=zone, capacity_mw=units[column])

    wind = read_units(mastr, "EinheitenWind.csv")
    wind = wind[in_operation(wind, date)]
    onshore = wind[(wind["WindAnLandOderAufSee"] == ONSHORE) & (wind["Land"] == GERMANY)]
    offshore = wind[wind["WindAnLandOderAufSee"] == OFFSHORE]
    solar = read_units(mastr, "EinheitenSolar_*.csv")
    reference = [solar, wind]
    solar = solar[in_operation(solar, date) & (solar["Land"] == GERMANY)]
    parts = [
        part(onshore, "wind_onshore", zone_of(onshore), "Bruttoleistung"),
        part(
            offshore,
            "wind_offshore",
            offshore_zone(offshore, catalogue, shares_dir),
            "Bruttoleistung",
        ),
        part(solar, "solar", zone_of(solar), "Bruttoleistung"),
    ]

    other = pd.concat([read_units(mastr, f"{name}.csv") for name in RESIDUAL_FILES])
    reference.append(other)
    other = other[in_operation(other, date) & (other["Land"] == GERMANY)]
    other = other[other["Notstromaggregat"] != "1"]
    zone, _ = fill_zone_from_district(zone_of(other), other["Bundesland"], other["Landkreis"])
    fuel_of = {code: fuel for fuel, codes in FUELS.items() for code in codes}
    technology = other["Energietraeger"].map(fuel_of)
    keep = technology.notna() & (technology != "nuclear")
    parts.append(part(other[keep], technology[keep], zone[keep], "Nettonennleistung"))

    nuclear = read_units(mastr, "EinheitenKernkraft.csv")
    nuclear = nuclear[in_operation(nuclear, NUCLEAR_DATE) & (nuclear["Land"] == GERMANY)]
    nuclear = nuclear[nuclear["Energietraeger"].isin(FUELS["nuclear"])]
    nuclear_zone, _ = fill_zone_from_district(
        zone_of(nuclear), nuclear["Bundesland"], nuclear["Landkreis"]
    )
    lookup = pd.DataFrame(
        {"land": other["Bundesland"], "kreis": other["Landkreis"], "zone": zone}
    ).dropna()
    lookup = lookup.drop_duplicates().drop_duplicates(["land", "kreis"], keep=False)
    found = pd.Series(
        list(zip(nuclear["Bundesland"], nuclear["Landkreis"], strict=True)), index=nuclear.index
    ).map(lookup.set_index(["land", "kreis"])["zone"].to_dict())
    parts.append(part(nuclear, "nuclear", nuclear_zone.fillna(found), "Nettonennleistung"))

    pumped = read_units(mastr, "EinheitenStromSpeicher_*.csv")
    pumped = pumped[in_operation(pumped, date) & (pumped["Land"] == GERMANY)]
    pumped_zone, _ = fill_zone_from_district(
        zone_of(pumped), pumped["Bundesland"], pumped["Landkreis"]
    )
    parts.append(part(pumped, PUMPED_STORAGE, pumped_zone, "Nettonennleistung"))

    def tidy(units: pd.DataFrame) -> pd.DataFrame:
        units = units.rename(columns=COLUMNS).reset_index(drop=True)
        units["state"] = units["Bundesland"].map(catalogue)
        for column in ("lat", "lon"):
            units[column] = pd.to_numeric(units[column], errors="coerce")
        return units

    units = tidy(pd.concat(parts, ignore_index=True))
    units["name"] = unit_name(units)
    columns = ["ags", "postcode", "state", "district", "lat", "lon"]
    return units, tidy(pd.concat(reference, ignore_index=True))[columns]


def build_table(
    units: pd.DataFrame,
    position: pd.DataFrame,
    min_mw: float,
    municipalities: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per unit of ``min_mw`` and more; smaller ones summed per place.

    Small units are summed per technology, zone, municipality (or district) and
    position, and take their municipality's position where it is known.
    """
    table = units[
        ["plant_id", "name", "technology", "capacity_mw", "state", "district", "ags", "zone"]
    ].join(position)
    table["units"] = 1
    small = table["capacity_mw"] < min_mw
    if municipalities is not None:
        home = small & table["ags"].isin(municipalities.index)
        table.loc[home, "lat"] = table.loc[home, "ags"].map(municipalities["lat"])
        table.loc[home, "lon"] = table.loc[home, "ags"].map(municipalities["lon"])
        table.loc[home, "location_basis"] = "municipality"
    keys = ["technology", "zone", "state", "district", "ags", "lat", "lon", "location_basis"]
    summed = (
        table[small]
        .groupby(keys, dropna=False, as_index=False)
        .agg(capacity_mw=("capacity_mw", "sum"), units=("units", "sum"))
    )
    place = summed["ags"].fillna(summed["state"].fillna("") + "/" + summed["district"].fillna(""))
    summed["plant_id"] = "small-" + summed["technology"] + "-" + place.str.replace(" ", "_")
    extra = summed.groupby("plant_id").cumcount()
    summed["plant_id"] += ("-" + (extra + 1).astype(str)).where(extra > 0, "")
    summed["name"] = "units below " + f"{min_mw:g} MW"
    table = pd.concat([table[~small], summed], ignore_index=True)
    table = table.sort_values(["technology", "plant_id"], ignore_index=True)
    return table.rename(columns={"zone": "zone_de22"})[
        [
            "plant_id",
            "name",
            "technology",
            "capacity_mw",
            "units",
            "state",
            "district",
            "ags",
            "zone_de22",
            "lat",
            "lon",
            "location_basis",
        ]
    ]


def summary(table: pd.DataFrame) -> pd.DataFrame:
    """MW per technology and location basis, and the units behind them."""
    mw = table.pivot_table(
        index="technology", columns="location_basis", values="capacity_mw", aggfunc="sum"
    ).reindex(columns=[*BASES, "missing"])
    mw = mw.fillna(0.0)
    mw.insert(0, "units", table.groupby("technology")["units"].sum())
    mw.insert(1, "capacity_mw", table.groupby("technology")["capacity_mw"].sum())
    mw.loc["total"] = mw.sum()
    return mw.astype({"units": int}).round(1).reset_index()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mastr", required=True, type=Path, help="Output of extract_mastr.py.")
    parser.add_argument("--plant-list", required=True, type=Path, help="OPSD plant list (csv).")
    parser.add_argument("--date", default="2023-07-01", help="Units in operation on this day.")
    parser.add_argument("--min-mw", type=float, default=1.0, help="Smaller units are summed.")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--positions-out", type=Path, default=PLANT_LIST_POSITIONS, help="Not for committing."
    )
    args = parser.parse_args()
    if not (args.mastr / "COMPLETE.txt").exists():
        raise SystemExit(
            f"{args.mastr} is not a finished extraction; run scripts/extract_mastr.py again"
        )
    header = pd.read_csv(args.mastr / "EinheitenWind.csv", nrows=0).columns
    if not {"Postleitzahl", "NameStromerzeugungseinheit", "Kraftwerksnummer"} <= set(header):
        raise SystemExit(
            f"{args.mastr} was extracted without the columns of the plant table; "
            "run scripts/extract_mastr.py again into an empty directory"
        )
    source = (args.mastr / "SOURCE.txt").read_text(encoding="utf-8").strip().rsplit("/", 1)[-1]

    units, reference = select_units(args.mastr, args.date, DE22, SHARES)
    blocks, sites = plant_number_positions(pd.read_csv(args.plant_list))
    municipalities = reference_positions(reference, "ags")
    position = locate_units(
        units,
        municipalities=municipalities,
        postcodes=reference_positions(reference, "postcode"),
        districts=reference_positions(reference, ["state", "district"]),
        plant_blocks=blocks,
        plant_sites=sites,
    )
    table = build_table(units, position, args.min_mw, municipalities)
    table[["lat", "lon"]] = table[["lat", "lon"]].round(4)
    table["capacity_mw"] = table["capacity_mw"].round(4)
    listed = table["location_basis"] == "plant_list"
    args.positions_out.parent.mkdir(parents=True, exist_ok=True)
    write(
        args.positions_out,
        [
            "Positions of the plants with location_basis plant_list, from Open Power System",
            "Data, Conventional power plants 2020-10-01, which states no licence: for local",
            "use by scripts/place_plants.py, not for committing or passing on.",
        ],
        table.loc[listed, ["plant_id", "lat", "lon"]],
    )
    table.loc[listed, ["lat", "lon"]] = float("nan")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write(
        args.out,
        [
            f"Generation units in Germany in operation on {args.date} (nuclear: on",
            f"{NUCLEAR_DATE}), from the MaStR full export {source}",
            "(Bundesnetzagentur, dl-de/by-2-0). The units, technologies and capacities are",
            "those of data/shares/capacity_by_zone.csv: gross MW for wind and solar, net MW",
            f"otherwise. Units below {args.min_mw:g} MW are summed per technology and municipality",
            "(units: how many), at the municipality's position where it has one. zone_de22",
            "is the zone the zone shares count the unit in; empty where they leave it out",
            "for lack of a municipality key. location_basis says where lat/lon come from:",
            "  coordinates   the unit's own in MaStR",
            "  plant_list    by BNetzA plant number from Open Power System Data, Conventional",
            "                power plants 2020-10-01 (MaStR withholds the place of large units).",
            "                That list states no licence, so lat/lon are left empty here and",
            f"                written to {args.positions_out.name} beside the build instead",
            "  municipality  median of MaStR's located units with the same municipality key",
            "  postcode      the same by postcode",
            "  district      ASSUMPTION: the same by district (Landkreis), tens of km off",
            "  missing       none of these; no position (and no lat/lon)",
            "Offshore wind has its position at sea, not where it comes ashore.",
            "Built by scripts/build_plant_table.py; see docs/DATA.md.",
        ],
        table,
    )
    result = summary(table)
    write(
        args.out.with_name(args.out.stem + "_summary.csv"),
        [
            f"MW per technology and location basis in {args.out.name}, and the number of units.",
            "Built by scripts/build_plant_table.py.",
        ],
        result,
    )
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
