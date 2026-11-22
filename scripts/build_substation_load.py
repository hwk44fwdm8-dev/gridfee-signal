"""Place load on the substations of the substation-level network by population.

    python scripts/build_substation_load.py

Reads data/networks/desub/substations.csv, data/population/population_1km.csv.gz and
the Eurostat totals in data/population/nuts2_check.csv, and writes
data/networks/desub/load_shares.csv. Method and limits: docs/DATA.md.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from gridfee.injections.population import load_population_grid, place_on_substations

ROOT = Path(__file__).resolve().parents[1]
DESUB = ROOT / "data" / "networks" / "desub"
POPULATION = ROOT / "data" / "population"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--substations", type=Path, default=DESUB / "substations.csv")
    parser.add_argument("--grid", type=Path, default=POPULATION / "population_1km.csv.gz")
    parser.add_argument("--totals", type=Path, default=POPULATION / "nuts2_check.csv")
    parser.add_argument("--out", type=Path, default=DESUB / "load_shares.csv")
    args = parser.parse_args()

    listed = pd.read_csv(args.substations, comment="#")
    substations = listed[listed["kind"] == "substation"]
    totals = pd.read_csv(args.totals, comment="#").set_index("nuts2")["reference"]
    shares = place_on_substations(load_population_grid(args.grid), substations, totals)
    shares["population"] = shares["population"].round(1)
    shares["load_share"] = shares["load_share"].round(8)

    header = [
        "Share of German load per substation of data/networks/desub/substations.csv,",
        "written by scripts/build_substation_load.py. ASSUMPTION: load follows population.",
        "Every 1 km cell of data/population/population_1km.csv.gz (Zensus 2022, Destatis,",
        "dl-de/by-2-0) goes to the nearest substation of its own NUTS2 region (nuts2 of",
        "the substation list), with the cells of a region scaled to its Eurostat population",
        "of 1 January 2023, so population summed per nuts2 is the Eurostat figure.",
        "Substations at one position share its cells equally. Tee points carry no load",
        "and are not listed. See docs/DATA.md.",
    ]
    text = "".join(f"# {line}\n" for line in header) + shares.to_csv(index=False)
    args.out.write_text(text, encoding="utf-8")
    empty = shares.loc[shares["population"] == 0, "substation_id"].tolist()
    print(f"wrote {args.out}: {len(shares)} substations, without population: {empty}")
    print(shares.sort_values("load_share").iloc[[0, len(shares) // 2, -3, -2, -1]].to_string())


if __name__ == "__main__":
    main()
