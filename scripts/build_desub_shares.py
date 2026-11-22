"""Bring the per-region inputs of the backcast onto the buses of the substation network.

    python scripts/build_desub_shares.py --network NETWORK_DIR --out OUT_DIR

Reads the buses (zones.csv written by scripts/derive_desub_network.py), the load
share and the capacity per substation (load_shares.csv and
capacity_by_substation.csv in data/networks/desub) and, per region, the efficiency
classes and weather factors of the 38-region network (data/shares/de38,
data/weather/de38), and writes the share, weather and reporting files of
`gridfee backcast` with one row or column per bus.

Load and plants of a substation sit on one bus: its lowest voltage level that is
connected (ASSUMPTION: the 110 kV grid below is fed from 220 kV where a substation
has it). Tee points and the other voltage level carry nothing.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from gridfee.injections.weather import read_capacity_factors, write_capacity_factors

ROOT = Path(__file__).resolve().parents[1]
DESUB = ROOT / "data" / "networks" / "desub"
DE38 = ROOT / "data" / "shares" / "de38"
WEATHER = ("wind_onshore", "wind_offshore", "solar")


def injection_bus(buses: pd.DataFrame) -> pd.Series:
    """Per substation, the bus that takes its load and plants."""
    connected = buses[buses["connected"]].sort_values("voltage_kv")
    return connected.groupby("substation_id")["zone_id"].first()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--network", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--load-shares", type=Path, default=DESUB / "load_shares.csv")
    parser.add_argument("--capacity", type=Path, default=DESUB / "capacity_by_substation.csv")
    parser.add_argument("--region-shares", type=Path, default=DE38)
    parser.add_argument(
        "--capacity-factors",
        type=Path,
        default=ROOT / "data" / "weather" / "de38" / "capacity_factors_2023.csv",
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    zones = pd.read_csv(args.network / "zones.csv", comment="#")
    internal = zones[zones["kind"] == "internal"].set_index("zone_id")
    subs = pd.read_csv(DESUB / "substations.csv", comment="#").set_index("substation_id")
    buses = pd.read_csv(DESUB / "buses.csv", comment="#", keep_default_na=False)
    buses = buses[buses["zone_id"].isin(internal.index)]
    bus_of = injection_bus(buses)
    load = pd.read_csv(args.load_shares, comment="#").set_index("substation_id")
    unknown = sorted(set(load.index) - set(bus_of.index))
    if unknown:
        raise SystemExit(f"load shares for substations without a connected bus: {unknown}")

    # Per bus: region, reporting zone, population and its share of the region.
    table = pd.DataFrame(index=internal.index)
    table["tso_zone"] = internal["tso_zone"]
    station = buses.set_index("zone_id")["substation_id"]
    table["nuts2"] = station.map(subs["nuts2"])
    table["report_zone"] = station.map(subs["report_zone"])
    population = load["population"].rename(index=bus_of)
    table["population"] = population.reindex(table.index).fillna(0.0)
    table["of_region"] = table["population"] / table.groupby("nuts2")["population"].transform("sum")

    of_tso = table["population"] / table.groupby("tso_zone")["population"].transform("sum")
    of_tso.round(9).rename("share").rename_axis("zone_id").to_csv(
        args.out / "population_shares.csv"
    )

    # Plants: MW per substation and technology, on the substation's bus.
    capacity = pd.read_csv(args.capacity, comment="#")
    unknown = sorted(set(capacity["substation_id"]) - set(bus_of.index))
    if unknown:
        raise SystemExit(f"capacity on substations without a connected bus: {unknown}")
    capacity["zone_id"] = capacity["substation_id"].map(bus_of)
    mw = capacity.pivot_table(
        index="zone_id", columns="technology", values="capacity_mw", aggfunc="sum"
    )
    mw = mw.reindex(table.index).fillna(0.0)
    weather = [tech for tech in WEATHER if tech in mw]
    of_tso = mw[weather] / mw[weather].groupby(table["tso_zone"]).transform("sum")
    long = of_tso.fillna(0.0).round(9).rename_axis(columns="technology").stack()
    long.rename("share").reset_index()[["technology", "zone_id", "share"]].to_csv(
        args.out / "capacity_shares.csv", index=False
    )
    other = mw.drop(columns=weather)
    long = (other / other.sum()).round(9).rename_axis(columns="technology").stack()
    long.rename("share").reset_index()[["technology", "zone_id", "share"]].to_csv(
        args.out / "generation_shares.csv", index=False
    )
    # ASSUMPTION: the plants of a fuel at a bus have the efficiency classes of its region.
    region = pd.read_csv(args.region_shares / "plant_efficiency.csv", comment="#")
    classes = region.rename(columns={"zone_id": "nuts2"}).merge(
        table.reset_index()[["zone_id", "nuts2"]], on="nuts2"
    )
    classes[["technology", "zone_id", "efficiency", "share"]].to_csv(
        args.out / "plant_efficiency.csv", index=False
    )

    # Weather: every bus has the hourly factors of its region.
    factors = read_capacity_factors(args.capacity_factors)
    by_bus = {
        tech: frame.reindex(columns=table["nuts2"]).set_axis(table.index, axis=1).fillna(0.0)
        for tech, frame in factors.items()
    }
    write_capacity_factors(by_bus, args.out / "capacity_factors_2023.csv")

    # Reporting: the 22 zones as the population-weighted mean of their buses with load.
    table.reset_index()[["report_zone", "zone_id"]].rename(
        columns={"report_zone": "zone_id", "zone_id": "nuts2"}
    ).to_csv(args.out / "report_zones.csv", index=False)
    table["population"].round(1).rename_axis("zone_id").to_csv(args.out / "report_weights.csv")
    print(f"{len(table)} buses, {int((table['population'] > 0).sum())} with load -> {args.out}")


if __name__ == "__main__":
    main()
