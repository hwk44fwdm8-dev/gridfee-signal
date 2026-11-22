"""Convert a local published TYNDP market workbook; requires the derive extra."""

import argparse
from pathlib import Path

import pandas as pd

from gridfee.data.tyndp import read_market, replay, split_other_res, write_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", type=Path, required=True)
    parser.add_argument("--plants", type=Path, required=True, help="DE00 PEMMDB workbook")
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, default=Path("data/tyndp/technology_mapping.csv"))
    parser.add_argument(
        "--border-routing", type=Path, default=Path("data/tyndp/border_routing.csv")
    )
    parser.add_argument("--scenario", default="NT")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--climate-year", type=int, required=True)
    parser.add_argument("--reference-year", type=int, default=2023)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    market, exchanges = read_market(args.market, args.scenario, args.year, args.climate_year)
    market = split_other_res(market, args.plants, args.scenario, args.year)
    tables, national, gap, missing = replay(
        market,
        exchanges,
        pd.read_csv(args.mapping, dtype={"code": str}),
        args.raw,
        args.year,
        args.reference_year,
        border_routing=pd.read_csv(args.border_routing),
    )
    args.out.mkdir(parents=True, exist_ok=True)
    print("Annual generation TWh\n" + (tables["generation_de"].sum() / 1e6).to_string())
    print("Annual load TWh\n" + (tables["load_tso"].sum() / 1e6).to_string())
    print("Annual net export TWh\n" + (national.sum() / 1e6).to_string())
    for name in ["wind_onshore_tso", "wind_offshore_tso", "solar_tso", "physical_flows_tso"]:
        print(f"Annual {name} TWh\n" + (tables[name].sum() / 1e6).to_string())
    print(f"Balance signed TWh: {gap.sum() / 1e6:.9f}; absolute TWh: {gap.abs().sum() / 1e6:.9f}")
    if missing:
        national.to_csv(args.out / "unrouted_national_exchanges.csv")
        raise ValueError(f"No 2023 SMARD routing for {missing}; no SMARD tables written")
    write_replay(tables, args.out, args.year)
    print(f"Wrote {len(market)} hours to {args.out}")


if __name__ == "__main__":
    main()
