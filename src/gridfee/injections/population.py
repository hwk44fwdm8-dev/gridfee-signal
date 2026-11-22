"""Population per 1 km grid cell, for placing load on substations.

``scripts/build_population_grid.py`` builds ``data/population/population_1km.csv.gz``
from the Zensus 2022 grid; the functions here read the source table, read the built
table and compare or scale it to population totals per region. See docs/DATA.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO

import pandas as pd

COLUMNS = ["lon", "lat", "nuts2", "population"]


class PopulationGridError(ValueError):
    """The population grid is not usable."""


def read_census_grid(source: str | Path | IO[bytes]) -> pd.DataFrame:
    """Cells of the Zensus 2022 1 km table ``Zensus2022_Bevoelkerungszahl_1km-Gitter.csv``.

    Returns the cell centres ``x``, ``y`` in metres (EPSG:3035) and ``population``.
    """
    cells = pd.read_csv(source, sep=";")
    expected = ["GITTER_ID_1km", "x_mp_1km", "y_mp_1km", "Einwohner"]
    if list(cells.columns) != expected:
        raise PopulationGridError(f"expected columns {expected}, got {list(cells.columns)}")
    cells = cells.rename(columns={"x_mp_1km": "x", "y_mp_1km": "y", "Einwohner": "population"})
    if cells["GITTER_ID_1km"].duplicated().any():
        raise PopulationGridError("a grid cell is listed twice")
    if cells[["x", "y", "population"]].isna().any().any() or (cells["population"] <= 0).any():
        raise PopulationGridError("cells must have a position and a population above 0")
    return cells[["x", "y", "population"]].reset_index(drop=True)


def load_population_grid(path: str | Path) -> pd.DataFrame:
    """The built table: ``lon``, ``lat`` (cell centre, WGS 84), ``nuts2``, ``population``."""
    grid = pd.read_csv(path, comment="#", dtype={"nuts2": "string"})
    if list(grid.columns) != COLUMNS:
        raise PopulationGridError(f"expected columns {COLUMNS}, got {list(grid.columns)}")
    if grid.isna().any().any() or (grid["population"] <= 0).any():
        raise PopulationGridError("cells must have a position, a region and a population above 0")
    return grid


def compare_with_regions(grid: pd.DataFrame, totals: pd.Series) -> pd.DataFrame:
    """Grid population per region next to ``totals`` (indexed by region id).

    Columns ``nuts2``, ``grid``, ``reference``, ``difference`` and ``difference_percent``
    (grid minus reference, relative to the reference). Fails if the two do not cover
    the same regions.
    """
    summed = grid.groupby("nuts2")["population"].sum()
    if set(summed.index) != set(totals.index):
        odd = sorted(set(summed.index) ^ set(totals.index))
        raise PopulationGridError(f"regions in only one of grid and totals: {odd}")
    reference = totals.reindex(summed.index)
    difference = summed - reference
    return pd.DataFrame(
        {
            "nuts2": summed.index.astype(str),
            "grid": summed.to_numpy(),
            "reference": reference.to_numpy(),
            "difference": difference.to_numpy(),
            "difference_percent": (100.0 * difference / reference).round(3).to_numpy(),
        }
    )


def scale_to_regions(grid: pd.DataFrame, totals: pd.Series) -> pd.DataFrame:
    """The grid with every region's cells scaled so that the region sums to ``totals``."""
    check = compare_with_regions(grid, totals).set_index("nuts2")
    factor = grid["nuts2"].map(check["reference"] / check["grid"])
    return grid.assign(population=grid["population"] * factor.astype(float))
