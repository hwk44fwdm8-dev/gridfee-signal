"""Bring the per-region inputs of the backcast onto the buses of the substation network.

    python scripts/build_desub_shares.py --network NETWORK_DIR --out OUT_DIR

Reads the buses (zones.csv written by scripts/derive_desub_network.py), the load
share per substation (data/networks/desub/load_shares.csv) and the per-region
inputs of the 38-region network (data/shares/de38, data/weather/de38), and writes
the same files with one row or column per bus, for `gridfee backcast`.

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


def injection_bus(buses: pd.DataFrame) -> pd.Series:
    """Per substation, the bus that takes its load and plants."""
    connected = buses[buses["connected"]].sort_values("voltage_kv")
    return connected.groupby("substation_id")["zone_id"].first()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--network", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--load-shares", type=Path, default=DESUB / "load_shares.csv")
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

    # INTERIM: a region's plants are spread over its substations like its load.
    for name, per in (
        ("capacity_shares.csv", ["technology"]),
        ("generation_shares.csv", ["technology"]),
        ("plant_efficiency.csv", ["technology", "efficiency"]),
    ):
        region = pd.read_csv(args.region_shares / name, comment="#")
        spread = region.rename(columns={"zone_id": "nuts2"}).merge(
            table.reset_index()[["zone_id", "nuts2", "of_region"]], on="nuts2"
        )
        if name == "plant_efficiency.csv":
            spread = spread[spread["of_region"] > 0]
        else:
            spread["share"] = spread["share"] * spread["of_region"]
        spread[[*per[:1], "zone_id", *per[1:], "share"]].to_csv(args.out / name, index=False)

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
