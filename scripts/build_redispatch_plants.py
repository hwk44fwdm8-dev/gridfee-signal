"""Substation of the plants named in the 2023 redispatch export (see docs/DATA.md).

    python scripts/build_redispatch_plants.py [--plants <conventional_power_plants_DE.csv>]

Rewrites data/redispatch/plants_2023.csv: the columns plant, located_by,
plant_list_ids, reason and note are kept as they are (and substation_id of the plants
located by their name); substation_id, basis and distance_km of the plants located by
the plant list are computed from the list of conventional power plants of Open Power
System Data (version 2020-10-01, downloaded unless given) and
data/networks/desub/substations.csv. With --redispatch <export.csv> it also prints
the share of the export's energy that is located.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pandas as pd
import requests

from gridfee.validation import (
    locate_plants,
    read_plant_table,
    read_redispatch,
    redispatch_coverage,
    render_coverage,
)

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "data" / "redispatch" / "plants_2023.csv"
SUBSTATIONS = ROOT / "data" / "networks" / "desub" / "substations.csv"
PLANT_LIST_URL = (
    "https://data.open-power-system-data.org/conventional_power_plants/2020-10-01/"
    "conventional_power_plants_DE.csv"
)
HEADER = """\
# Where the plants named in the 2023 redispatch export of netztransparenz.de stand in
# data/networks/desub/substations.csv. Written by scripts/build_redispatch_plants.py;
# the rows and the columns plant, located_by, plant_list_ids, reason and note are kept
# by hand (docs/DATA.md). plant is BETROFFENE_ANLAGE as published. located_by:
#   plant_list       the entries plant_list_ids of the list of conventional power
#                    plants of Open Power System Data, version 2020-10-01
#                    (https://doi.org/10.25832/conventional_power_plants/2020-10-01,
#                    primary data: BNetzA Kraftwerksliste), matched by name and block
#   same_site        ASSUMPTION: a unit the plant list lacks, put at the listed units
#                    of the same power station (note)
#   substation_name  the name gives a substation of the list
#   landing_point    offshore wind farm, at the substation where its grid connection
#                    comes ashore; note gives the TSO's statement, or ASSUMPTION
#   cluster_code     ASSUMPTION: 50Hertz's code for a group of renewable plants in a
#                    distribution grid (50H_<grid operator>_CR_<code>), read as the
#                    short name of a substation in 50Hertz's static grid model (note)
#   none             not located. reason: market (exchange trades), area (a federal
#                    state, a distribution grid area or a pool), cluster_code
#                    (a code that is no short name of the static grid model),
#                    abroad (plants in Austria), not_in_plant_list (in
#                    operation after 2020), not_identified, substation_not_in_list,
#                    ambiguous (note)
# basis: network_node = the substation the plant list names as the plant's connection
# point, if it is in the substation list within 20 km of the plant; nearest = the
# nearest substation (tee points are not used); name = given by located_by.
# distance_km: from the plant's position in the plant list to the substation.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plants", type=Path, help="OPSD plant list (default: download).")
    parser.add_argument("--substations", type=Path, default=SUBSTATIONS)
    parser.add_argument("--table", type=Path, default=TABLE)
    parser.add_argument("--redispatch", type=Path, help="Export to report the coverage of.")
    args = parser.parse_args()
    if args.plants:
        plant_list = pd.read_csv(args.plants)
    else:
        response = requests.get(PLANT_LIST_URL, timeout=120)
        response.raise_for_status()
        plant_list = pd.read_csv(io.BytesIO(response.content))
    substations = pd.read_csv(args.substations, comment="#")
    table = locate_plants(read_plant_table(args.table), plant_list, substations)
    args.table.write_text(HEADER + table.to_csv(index=False), encoding="utf-8")
    print(table["basis"].replace("", "not located").value_counts().to_string())
    if args.redispatch:
        print(render_coverage(redispatch_coverage(read_redispatch(args.redispatch), table)))


if __name__ == "__main__":
    main()
