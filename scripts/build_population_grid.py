"""Build data/population/ from the Zensus 2022 1 km grid.

    python scripts/build_population_grid.py

Downloads the grid (Statistisches Bundesamt), the NUTS 2021 level-2 boundaries
(Eurostat GISCO, 1:1 million) and the Eurostat population per NUTS2 region, and writes
population_1km.csv.gz and nuts2_check.csv. Needs the `derive` extra (geopandas).
Method and limits: docs/DATA.md.
"""

from __future__ import annotations

import argparse
import gzip
import io
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from gridfee.injections.population import COLUMNS, compare_with_regions, read_census_grid

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "population"
GRID = "https://www.destatis.de/static/DE/zensus/gitterdaten/Zensus2022_Bevoelkerungszahl.zip"
GRID_MEMBER = "Zensus2022_Bevoelkerungszahl_1km-Gitter.csv"
NUTS = (
    "https://gisco-services.ec.europa.eu/distribution/v2/nuts/download/"
    "ref-nuts-2021-01m.geojson.zip"
)
NUTS_MEMBER = "NUTS_RG_01M_2021_4326_LEVL_2.geojson"
EUROSTAT = (
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/demo_r_d2jan/"
    "A.NR.T.TOTAL.?format=SDMX-CSV&startPeriod={year}&endPeriod={year}"
)


def member(source: str, name: str) -> io.BytesIO:
    """One file of a ZIP given as a URL or a path."""
    if source.startswith(("http://", "https://")):
        response = requests.get(source, headers={"User-Agent": "gridfee-signal"}, timeout=600)
        response.raise_for_status()
        archive = zipfile.ZipFile(io.BytesIO(response.content))
    else:
        archive = zipfile.ZipFile(source)
    return io.BytesIO(archive.read(name))


def assign_regions(cells: pd.DataFrame, regions: gpd.GeoDataFrame) -> tuple[pd.DataFrame, int]:
    """Add ``lon``, ``lat`` and the region (index of ``regions``) of each cell centre.

    A centre outside every region (coast, border) gets the nearest region. Returns the
    cells and how many were placed that way.
    """
    regions = regions[["geometry"]].rename_axis("region").to_crs(3035)
    points = gpd.GeoDataFrame(cells, geometry=gpd.points_from_xy(cells["x"], cells["y"]), crs=3035)
    inside = gpd.sjoin(points, regions, predicate="within", how="left")["region"]
    inside = inside[~inside.index.duplicated()]
    outside = points[inside.isna()]
    nearest = gpd.sjoin_nearest(outside, regions, how="left")["region"]
    nearest = nearest[~nearest.index.duplicated()]
    wgs = points.geometry.to_crs(4326)
    placed = cells.assign(
        lon=wgs.x.round(5), lat=wgs.y.round(5), nuts2=inside.fillna(nearest).astype("string")
    )
    return placed, len(outside)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--grid", default=GRID, help="URL or path of the Zensus 2022 grid ZIP.")
    parser.add_argument("--nuts", default=NUTS, help="URL or path of the GISCO NUTS 2021 ZIP.")
    parser.add_argument("--population-year", type=int, default=2023, help="Eurostat, 1 January.")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    cells = read_census_grid(member(args.grid, GRID_MEMBER))
    regions = gpd.read_file(member(args.nuts, NUTS_MEMBER))
    regions = regions[regions["CNTR_CODE"] == "DE"].set_index("NUTS_ID")
    placed, outside = assign_regions(cells, regions)
    grid = placed.sort_values(["nuts2", "y", "x"])[COLUMNS]
    print(
        f"{len(grid):,} cells, {grid['population'].sum():,} people; {outside} cell centres "
        "outside every region were given the nearest one"
    )

    response = requests.get(EUROSTAT.format(year=args.population_year), timeout=300)
    response.raise_for_status()
    eurostat = pd.read_csv(io.StringIO(response.text)).set_index("geo")["OBS_VALUE"]
    check = compare_with_regions(grid, eurostat.reindex(regions.index).astype("int64"))
    print(check.to_string(index=False))
    total = check["grid"].sum() / check["reference"].sum() - 1
    print(f"Germany: {100 * total:+.2f} % against Eurostat")

    args.out.mkdir(parents=True, exist_ok=True)
    header = [
        "Population per 1 km grid cell: cell centre (WGS 84), NUTS2 region, residents.",
        "Source: Statistisches Bundesamt, Zensus 2022 (15 May 2022), Bevoelkerungszahl im",
        "1-km-Gitter, Datenlizenz Deutschland - Namensnennung - Version 2.0.",
        "Regions: NUTS 2021 level 2 (Eurostat GISCO, 1:1 million), by cell centre.",
        "Built by scripts/build_population_grid.py; see docs/DATA.md.",
    ]
    text = "".join(f"# {line}\n" for line in header) + grid.to_csv(index=False)
    path = args.out / "population_1km.csv.gz"
    with gzip.GzipFile(path, "wb", mtime=0) as file:  # mtime=0: same bytes on every build
        file.write(text.encode("utf-8"))
    print(f"wrote {path}")
    check_header = [
        "Population per NUTS2 region: grid cells summed (Zensus 2022, 15 May 2022) against",
        f"Eurostat demo_r_d2jan, 1 January {args.population_year} (reference).",
        "Built by scripts/build_population_grid.py; see docs/DATA.md.",
    ]
    path = args.out / "nuts2_check.csv"
    path.write_text(
        "".join(f"# {line}\n" for line in check_header) + check.to_csv(index=False),
        encoding="utf-8",
    )
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
