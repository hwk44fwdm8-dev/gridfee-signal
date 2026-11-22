"""Build data/networks/desub/sector_demand.csv and data/demand/profile_tables.csv.

    python scripts/build_sector_demand.py

Downloads annual electricity use per municipality and sector and the H0 and G0 load
curves (DemandRegio, served by the FfE open data API) and the municipal boundaries
(BKG VG250 of three years), spreads each municipality's use over its population cells
and gives every cell to the nearest substation of its NUTS2 region. Needs the `derive`
extra (geopandas). Method and limits: docs/DATA.md.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

from gridfee.injections.demand import (
    INDUSTRY_COLUMNS,
    SECTORS,
    SHIFT_OF_BRANCH,
    place_on_substations,
    profile_tables_from_year,
    spread_over_cells,
)
from gridfee.injections.population import load_population_grid

ROOT = Path(__file__).resolve().parents[1]
DESUB = ROOT / "data" / "networks" / "desub"
API = "https://api.opendata.ffe.de/demandregio/"
SPATIAL = API + "demandregio_spatial?id_spatial=70"
BRANCHES = API + "demandregio_spatial?id_spatial=25&year=2017"
COLUMNS = [*SECTORS, *INDUSTRY_COLUMNS]
TEMPORAL = API + "demandregio_temporal?id_temporal={id}&year_base={year}"
PROFILE_YEAR = 2019
PROFILE_IDS = {"households": 23, "cts": 22}  # SLP H0 and G0
VG250 = (
    "https://daten.gdz.bkg.bund.de/produkte/vg/vg250_ebenen_1231/{year}/"
    "vg250_12-31.utm32s.shape.ebenen.zip"
)
# The table mixes municipality keys of several years (households in Mecklenburg-Vorpommern
# have the districts of before 2011): a key is looked up in the newest set that has it.
BOUNDARY_YEARS = (2019, 2015, 2010)
# The table's metadata names 2 commerce and 3 industry. It is the other way round: 2 is,
# district by district, the industry table of DemandRegio (id_spatial 25, 240.5 TWh).
SECTOR_OF_ID = {1: "households", 2: "industry", 3: "cts"}


def fetch(source: str) -> bytes:
    """The content of a URL or a local path."""
    if source.startswith(("http://", "https://")):
        response = requests.get(source, headers={"User-Agent": "gridfee-signal"}, timeout=900)
        response.raise_for_status()
        return response.content
    return Path(source).read_bytes()


def municipal_demand(source: str) -> pd.DataFrame:
    """MWh per municipality (8-digit key ``ags``) and sector."""
    body = json.loads(fetch(source))
    data = pd.DataFrame(body["data"])
    data = data[data["internal_id"].map(lambda ids: ids[0] in SECTOR_OF_ID)]
    data = data.assign(
        ags=data["id_region"].astype(int).map("{:08d}".format),
        sector=data["internal_id"].map(lambda ids: SECTOR_OF_ID[ids[0]]),
    )
    wide = data.pivot(index="ags", columns="sector", values="value").fillna(0.0)
    return wide[SECTORS].reset_index()


def industry_by_district(source: str) -> tuple[pd.Series, pd.DataFrame]:
    """Per district (5-digit key): grid draw over use, and grid draw per shift class.

    From DemandRegio's electricity use of industry per district and branch, which gives
    use, grid draw and own generation. The row ``DE`` holds the German totals, for
    municipalities whose district the table does not know.
    """
    data = pd.DataFrame(json.loads(fetch(source))["data"])
    data = data.assign(
        district=(data["id_region"].astype(int) // 1000).map("{:05d}".format),
        branch=data["internal_id"].str[0],
        kind=data["internal_id"].str[1].map({1: "use", 2: "grid"}),
        value=data["value"].clip(lower=0.0),
    ).dropna(subset="kind")
    data["column"] = "industry_" + data["branch"].map(SHIFT_OF_BRANCH)
    if data["column"].isna().any():
        raise SystemExit("an industry branch has no shift class")
    totals = data.pivot_table(index="district", columns="kind", values="value", aggfunc="sum")
    totals.loc["DE"] = totals.sum()
    classes = data[data["kind"] == "grid"].pivot_table(
        index="district", columns="column", values="value", aggfunc="sum"
    )
    classes = classes.reindex(columns=INDUSTRY_COLUMNS, fill_value=0.0).fillna(0.0)
    classes.loc["DE"] = classes.sum()
    return totals["grid"] / totals["use"].where(totals["use"] > 0), classes


def at_grid_draw(municipal: pd.DataFrame, source: str) -> pd.DataFrame:
    """Reduce industry to what it draws from the grid and split it by shift class."""
    ratio, classes = industry_by_district(source)
    mix = classes.div(classes.sum(axis=1).where(classes.sum(axis=1) > 0), axis=0)
    district = municipal["ags"].str[:5]
    known = district.isin(mix.dropna().index) & district.isin(ratio.dropna().index)
    district = district.where(known, "DE")
    industry = municipal["industry"] * district.map(ratio).to_numpy()
    split = mix.loc[district].to_numpy() * industry.to_numpy()[:, None]
    out = municipal.assign(industry=industry, **dict(zip(INDUSTRY_COLUMNS, split.T, strict=True)))
    print(f"industry at national mix: {industry[~known].sum() / 1e6:.2f} TWh")
    return out


def one_profile(source: str) -> np.ndarray:
    """The hourly values of the first district of a DemandRegio load curve."""
    return np.asarray(json.loads(fetch(source))["data"][0]["values"], dtype=float)


def municipalities(source: str) -> gpd.GeoDataFrame:
    """Key ``AGS`` and land area of every municipality of a VG250 ZIP (URL or path)."""
    path = Path(source)
    if source.startswith(("http://", "https://")):
        path = ROOT / "data" / "cache" / ("vg250_" + source.split("/")[-2] + ".zip")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fetch(source))
    names = zipfile.ZipFile(path).namelist()
    name = next(n for n in names if n.lower().endswith("vg250_gem.shp"))
    towns = gpd.read_file(f"zip://{path}!{name}")
    return towns[towns["GF"] == 4][["AGS", "geometry"]].to_crs(3035)  # GF 4: land


def assign_municipalities(cells: pd.DataFrame, towns: gpd.GeoDataFrame) -> pd.DataFrame:
    """Add the municipality key ``ags`` of each cell centre (nearest one if outside all)."""
    points = gpd.GeoDataFrame(
        cells, geometry=gpd.points_from_xy(cells["lon"], cells["lat"]), crs=4326
    ).to_crs(3035)
    inside = gpd.sjoin(points, towns, predicate="within", how="left")["AGS"]
    inside = inside[~inside.index.duplicated()]
    nearest = gpd.sjoin_nearest(points[inside.isna()], towns, how="left")["AGS"]
    nearest = nearest[~nearest.index.duplicated()]
    return cells.assign(ags=inside.fillna(nearest).astype(str))


def spread_with(
    cells: pd.DataFrame, municipal: pd.DataFrame, boundaries: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Spread every municipality over the cells of the first boundary set that knows it."""
    left = municipal
    total = cells.assign(**dict.fromkeys(COLUMNS, 0.0))
    levels = []
    for number, source in enumerate(boundaries):
        towns = municipalities(source)
        last = number == len(boundaries) - 1
        known = left["ags"].isin(set(towns["AGS"]))
        part = left if last else left[known]
        spread, placed = spread_over_cells(assign_municipalities(cells, towns), part, COLUMNS)
        total[COLUMNS] += spread[COLUMNS]
        levels.append(placed.rename(index=lambda level, n=number: f"{level}, set {n + 1}"))
        left = left[~known]
    return total, pd.concat(levels)


def write(path: Path, header: list[str], table: pd.DataFrame, **kwargs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(f"# {line}\n" for line in header) + table.to_csv(index=False, **kwargs)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--demand", default=SPATIAL, help="URL or path of the municipal demand.")
    parser.add_argument(
        "--boundaries",
        nargs="+",
        default=[VG250.format(year=year) for year in BOUNDARY_YEARS],
        help="URLs or paths of VG250 ZIPs, newest first.",
    )
    parser.add_argument(
        "--branches", default=BRANCHES, help="URL or path of industry per district and branch."
    )
    parser.add_argument("--h0", default=TEMPORAL.format(id=23, year=PROFILE_YEAR))
    parser.add_argument("--g0", default=TEMPORAL.format(id=22, year=PROFILE_YEAR))
    parser.add_argument("--substations", type=Path, default=DESUB / "substations.csv")
    parser.add_argument(
        "--grid", type=Path, default=ROOT / "data" / "population" / "population_1km.csv.gz"
    )
    parser.add_argument("--out", type=Path, default=DESUB / "sector_demand.csv")
    parser.add_argument(
        "--tables", type=Path, default=ROOT / "data" / "demand" / "profile_tables.csv"
    )
    args = parser.parse_args()

    municipal = at_grid_draw(municipal_demand(args.demand), args.branches)
    spread, levels = spread_with(load_population_grid(args.grid), municipal, args.boundaries)
    print("TWh placed by boundary set and the level at which a municipality was found:")
    print((levels[SECTORS] / 1e6).round(2).to_string())

    listed = pd.read_csv(args.substations, comment="#")
    placed = place_on_substations(spread, listed[listed["kind"] == "substation"], COLUMNS)
    placed[INDUSTRY_COLUMNS] = placed[INDUSTRY_COLUMNS].round(0)
    placed["industry"] = placed[INDUSTRY_COLUMNS].sum(axis=1)
    placed[SECTORS] = placed[SECTORS].round(0)
    placed = placed[[c for c in placed if c != "load_share"] + ["load_share"]]
    placed["load_share"] = placed["load_share"].round(7)
    print((placed.groupby("tso")[SECTORS].sum() / 1e6).round(1).to_string())
    level = levels[SECTORS].sum(axis=1).groupby(lambda name: name.split(",")[0]).sum()
    by_level = (level / level.sum() * 100).round(2).to_dict()
    write(
        args.out,
        [
            "Annual electricity use in MWh per substation and sector: households,",
            "commerce/trade/services (cts) and industry, from district totals of 2017.",
            "industry is what industry draws from the grid (its use less its own",
            "generation); the industry_* columns split it by the working hours of its",
            "branches (s1/s2/s3 shifts; wt workdays, sa Saturdays, so Sundays).",
            "load_share is the substation's share of the German total.",
            "Sources: DemandRegio, 'Stromverbrauch je Sektor und Gemeinde' (c) FfE 2020,",
            "CC BY 4.0, and 'Stromverbrauch Industrie je LK und WZ' (c) FfE 2019,",
            "(c) Statistisches Bundesamt (Destatis), (c) Statistik der Bundesagentur fuer",
            "Arbeit, CC BY-NC 4.0, https://opendata.ffe.de: THIS FILE IS CC BY-NC 4.0.",
            "Municipal boundaries VG250 of 2019, 2015 and 2010 (c) GeoBasis-DE / BKG,",
            "dl-de/by-2-0 (no boundary data is included); population cells:",
            "data/population/population_1km.csv.gz.",
            "ASSUMPTION: inside a municipality every sector's use follows population.",
            "ASSUMPTION: the municipal table's sector 2 is industry and 3 commerce (its",
            "metadata says the reverse; 2 equals the industry table district by district).",
            f"Per cent of the use placed by municipality, district, state: {by_level}",
            "Built by scripts/build_sector_demand.py; see docs/DATA.md.",
        ],
        placed,
    )

    tables = profile_tables_from_year(one_profile(args.h0), one_profile(args.g0), PROFILE_YEAR)
    write(
        args.tables,
        [
            "Relative hourly electricity use of households (standard load profile H0,",
            "before its seasonal factor) and of commerce/trade/services (G0) by season,",
            "day type and local hour; each column has a mean of 1.",
            "Derived from the DemandRegio load curves 'Lastgang (SLP H0)' and 'Lastgang",
            f"(SLP G0)' of {PROFILE_YEAR} (c) FfE, CC BY 4.0, https://opendata.ffe.de.",
            "Built by scripts/build_sector_demand.py; see docs/DATA.md.",
        ],
        tables,
        float_format="%.5f",
    )


if __name__ == "__main__":
    main()
