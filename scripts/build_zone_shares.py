"""Build the zone share files in data/shares/ from Eurostat and MaStR.

    python scripts/extract_mastr.py --zip <url or path> --out <mastr dir>
    python scripts/build_zone_shares.py --mastr <mastr dir>

Writes population_shares.csv, capacity_shares.csv, residual_shares.csv and
capacity_by_zone.csv. Method and limits: docs/DATA.md.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pandas as pd
import requests

from gridfee.injections.zone_shares import (
    fill_zone_from_district,
    in_operation,
    national_shares,
    shares_within_tso,
    zone_of_ags,
)
from gridfee.network import load_zones

ROOT = Path(__file__).resolve().parents[1]
DE22 = ROOT / "data" / "networks" / "de22"
SHARES = ROOT / "data" / "shares"
EUROSTAT = (
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/demo_r_d2jan/"
    "A.NR.T.TOTAL.?format=SDMX-CSV&startPeriod={year}&endPeriod={year}"
)
GERMANY = "84"  # MaStR catalogue id of "Deutschland"
ONSHORE, OFFSHORE = "888", "889"  # "Windkraft an Land", "Windkraft auf See"
RESIDUAL_FILES = (
    "EinheitenVerbrennung",
    "EinheitenBiomasse",
    "EinheitenWasser",
    "EinheitenKernkraft",
    "EinheitenGeothermieGrubengasDruckentspannung",
)


def read_units(mastr: Path, pattern: str) -> pd.DataFrame:
    files = sorted(mastr.glob(pattern))
    if not files:
        raise SystemExit(f"no {pattern} in {mastr}; run scripts/extract_mastr.py first")
    units = pd.concat([pd.read_csv(path, dtype=str) for path in files], ignore_index=True)
    for column in ("Bruttoleistung", "Nettonennleistung"):
        units[column] = pd.to_numeric(units[column]) / 1000.0  # kW -> MW
    return units


def by_zone(units: pd.DataFrame, zone: pd.Series, column: str, what: str) -> pd.Series:
    """MW per zone; units without a zone are left out and reported."""
    missing = zone.isna()
    if missing.any():
        print(
            f"{what}: {int(missing.sum())} units ({units.loc[missing, column].sum():,.1f} MW of "
            f"{units[column].sum():,.1f} MW) cannot be placed in a zone and are left out"
        )
    return units.loc[~missing, column].groupby(zone[~missing]).sum()


def offshore_zone(units: pd.DataFrame, catalogue: pd.Series) -> pd.Series:
    landing = pd.read_csv(SHARES / "offshore_landing.csv", comment="#")
    sea = units["Seelage"].map(catalogue)
    area = (
        units["GebietNachDemFlaechenentwicklungsplanNordsee"]
        .fillna(units["GebietNachDemFlaechenentwicklungsplanOstsee"])
        .map(catalogue)
    )
    exact = {(row.sea, row.area): row.zone_id for row in landing.itertuples() if row.area != "*"}
    default = {row.sea: row.zone_id for row in landing.itertuples() if row.area == "*"}
    return pd.Series(
        [exact.get(key, default.get(key[0])) for key in zip(sea, area, strict=True)],
        index=units.index,
        dtype="string",
    )


def write(path: Path, header: list[str], frame: pd.DataFrame) -> None:
    text = "".join(f"# {line}\n" for line in header) + frame.to_csv(index=False)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mastr", required=True, type=Path, help="Output of extract_mastr.py.")
    parser.add_argument("--date", default="2023-07-01", help="Units in operation on this day.")
    parser.add_argument("--population-year", type=int, default=2023, help="1 January of.")
    args = parser.parse_args()

    zones = load_zones(DE22 / "zones.csv")
    regions = pd.read_csv(DE22 / "zone_nuts2.csv", comment="#").set_index("nuts2")["zone_id"]
    prefixes = pd.read_csv(SHARES / "ags_nuts2.csv", comment="#", dtype=str)
    prefix_to_zone = dict(zip(prefixes["ags_prefix"], prefixes["nuts2"].map(regions), strict=True))
    source = (args.mastr / "SOURCE.txt").read_text(encoding="utf-8").strip()
    mastr_note = f"MaStR full export {source.rsplit('/', 1)[-1]}, units in operation on {args.date}"

    # Population
    response = requests.get(EUROSTAT.format(year=args.population_year), timeout=300)
    response.raise_for_status()
    population = pd.read_csv(io.StringIO(response.text)).set_index("geo")["OBS_VALUE"]
    per_zone = population.reindex(regions.index).groupby(regions).sum(min_count=1)
    write(
        SHARES / "population_shares.csv",
        [
            "Share of each zone in the population of its TSO zone (zones.csv).",
            f"Source: Eurostat demo_r_d2jan, NUTS2, 1 January {args.population_year}.",
            "Built by scripts/build_zone_shares.py; see docs/DATA.md.",
        ],
        shares_within_tso(per_zone, zones).round(9),
    )

    # Wind and solar
    catalogue = pd.read_csv(args.mastr / "Katalogwerte.csv", dtype=str).set_index("Id")["Wert"]
    wind = read_units(args.mastr, "EinheitenWind.csv")
    wind = wind[in_operation(wind, args.date)]
    onshore = wind[(wind["WindAnLandOderAufSee"] == ONSHORE) & (wind["Land"] == GERMANY)]
    offshore = wind[wind["WindAnLandOderAufSee"] == OFFSHORE]
    solar = read_units(args.mastr, "EinheitenSolar_*.csv")
    solar = solar[in_operation(solar, args.date) & (solar["Land"] == GERMANY)]
    capacity = {
        "wind_onshore": by_zone(
            onshore,
            zone_of_ags(onshore["Gemeindeschluessel"], prefix_to_zone),
            "Bruttoleistung",
            "wind_onshore",
        ),
        "wind_offshore": by_zone(
            offshore, offshore_zone(offshore, catalogue), "Bruttoleistung", "wind_offshore"
        ),
        "solar": by_zone(
            solar,
            zone_of_ags(solar["Gemeindeschluessel"], prefix_to_zone),
            "Bruttoleistung",
            "solar",
        ),
    }
    shares = pd.concat(
        [shares_within_tso(mw, zones).assign(technology=tech) for tech, mw in capacity.items()]
    )
    write(
        SHARES / "capacity_shares.csv",
        [
            "Share of each zone in the installed capacity of its TSO zone (zones.csv).",
            f"Source: {mastr_note}; gross capacity.",
            "Zone by municipality key (ags_nuts2.csv). wind_offshore: ASSUMPTION on where it",
            "comes ashore (offshore_landing.csv).",
            "Built by scripts/build_zone_shares.py; see docs/DATA.md.",
        ],
        shares[["technology", "zone_id", "share"]].round(9),
    )

    # Generation other than wind and solar
    other = pd.concat([read_units(args.mastr, f"{name}.csv") for name in RESIDUAL_FILES])
    other = other[in_operation(other, args.date) & (other["Land"] == GERMANY)]
    other = other[other["Notstromaggregat"] != "1"]  # emergency generators do not feed in
    zone = zone_of_ags(other["Gemeindeschluessel"], prefix_to_zone)
    print(
        f"other generation: {other.loc[zone.isna(), 'Nettonennleistung'].sum():,.1f} MW of "
        f"{other['Nettonennleistung'].sum():,.1f} MW have no municipality key; "
        "taking the zone of other units in the same district"
    )
    zone, ambiguous = fill_zone_from_district(zone, other["Bundesland"], other["Landkreis"])
    if ambiguous:
        print(f"districts found in several zones, not used: {ambiguous}")
    capacity["other"] = by_zone(other, zone, "Nettonennleistung", "other generation")
    write(
        SHARES / "residual_shares.csv",
        [
            "Share of each zone in Germany's capacity other than wind and solar.",
            f"Source: {mastr_note}; net capacity of",
            "combustion, biomass, hydro, nuclear and geothermal units without emergency",
            "generators. Large units have no municipality key in the public export and take",
            "the zone of the other units in their district (Landkreis). ASSUMPTION: generation",
            "follows capacity, whatever the fuel. Units in the storage file of MaStR",
            "(batteries, pumped hydro) are not included.",
            "Built by scripts/build_zone_shares.py; see docs/DATA.md.",
        ],
        national_shares(capacity["other"], zones).round(9),
    )

    table = pd.DataFrame(capacity).reindex([z.zone_id for z in zones if z.tso_zone]).fillna(0.0)
    table.insert(0, "population", per_zone)
    write(
        SHARES / "capacity_by_zone.csv",
        [f"Population (Eurostat, 1 January {args.population_year}) and MW per zone.", mastr_note],
        table.round(1).rename_axis("zone_id").reset_index(),
    )
    print(table.round(0).to_string())
    print(table.sum().round(0).to_string())


if __name__ == "__main__":
    main()
