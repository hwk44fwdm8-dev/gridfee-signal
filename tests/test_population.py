"""The population grid: reading the census table, the region check, the shipped file."""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest

from gridfee.injections.population import (
    PopulationGridError,
    compare_with_regions,
    load_population_grid,
    read_census_grid,
    scale_to_regions,
)

ROOT = Path(__file__).parents[1]
POPULATION = ROOT / "data" / "population"
CENSUS = (
    "GITTER_ID_1km;x_mp_1km;y_mp_1km;Einwohner\n"
    "CRS3035RES1000mN2689000E4337000;4337500;2689500;4\n"
    "CRS3035RES1000mN2689000E4341000;4341500;2689500;11\n"
)
GRID = pd.DataFrame(
    {
        "lon": [9.0, 9.1, 13.4],
        "lat": [48.7, 48.8, 52.5],
        "nuts2": ["DE11", "DE11", "DE30"],
        "population": [30, 70, 200],
    }
)


def eurostat_2023() -> pd.Series:
    """Eurostat population per NUTS2 region as the repo uses it for its zone shares."""
    zones = pd.read_csv(ROOT / "data" / "shares" / "de38" / "capacity_by_zone.csv", comment="#")
    return zones.set_index("zone_id")["population"]


def test_read_census_grid_gives_cell_centres_and_population():
    cells = read_census_grid(io.StringIO(CENSUS))

    assert cells.to_dict("list") == {
        "x": [4337500, 4341500],
        "y": [2689500, 2689500],
        "population": [4, 11],
    }


@pytest.mark.parametrize(
    "text",
    [
        CENSUS.replace("Einwohner", "Wohnungen"),
        CENSUS + "CRS3035RES1000mN2689000E4341000;4341500;2689500;11\n",
        CENSUS.replace(";11", ";0"),
        CENSUS.replace(";11", ";"),
    ],
)
def test_read_census_grid_rejects_an_unexpected_table(text):
    with pytest.raises(PopulationGridError):
        read_census_grid(io.StringIO(text))


def test_compare_with_regions_reports_the_difference_per_region():
    check = compare_with_regions(GRID, pd.Series({"DE30": 250, "DE11": 100}))

    assert check.to_dict("list") == {
        "nuts2": ["DE11", "DE30"],
        "grid": [100, 200],
        "reference": [100, 250],
        "difference": [0, -50],
        "difference_percent": [0.0, -20.0],
    }


def test_compare_with_regions_needs_the_same_regions_on_both_sides():
    with pytest.raises(PopulationGridError, match="DE30"):
        compare_with_regions(GRID, pd.Series({"DE11": 100}))
    with pytest.raises(PopulationGridError, match="DE40"):
        compare_with_regions(GRID, pd.Series({"DE11": 100, "DE30": 250, "DE40": 1}))


def test_scale_to_regions_keeps_the_pattern_within_a_region():
    scaled = scale_to_regions(GRID, pd.Series({"DE11": 150, "DE30": 250}))

    assert scaled["population"].tolist() == pytest.approx([45.0, 105.0, 250.0])
    assert scaled[["lon", "lat", "nuts2"]].equals(GRID[["lon", "lat", "nuts2"]])


def test_load_population_grid_rejects_missing_values(tmp_path):
    path = tmp_path / "grid.csv"
    path.write_text("# note\nlon,lat,nuts2,population\n9.0,48.7,,30\n", encoding="utf-8")

    with pytest.raises(PopulationGridError):
        load_population_grid(path)


def test_shipped_grid_covers_germany_at_1_km():
    grid = load_population_grid(POPULATION / "population_1km.csv.gz")

    assert len(grid) > 200_000
    assert not grid.duplicated(["lon", "lat"]).any()
    assert grid["lon"].between(5.8, 15.1).all() and grid["lat"].between(47.2, 55.1).all()
    assert set(grid["nuts2"]) == set(eurostat_2023().index)
    # Zensus 2022 counted 82.7 million residents
    assert grid["population"].sum() == pytest.approx(82.7e6, rel=0.001)


def test_shipped_grid_agrees_with_eurostat_per_nuts2_region():
    grid = load_population_grid(POPULATION / "population_1km.csv.gz")

    check = compare_with_regions(grid, eurostat_2023())

    # The census day is 7.5 months before Eurostat's 1 January 2023; see docs/DATA.md.
    assert check["difference_percent"].abs().max() < 2.0
    assert check["grid"].sum() / check["reference"].sum() == pytest.approx(1.0, abs=0.006)
    shipped = pd.read_csv(POPULATION / "nuts2_check.csv", comment="#")
    pd.testing.assert_frame_equal(shipped, check)


def test_shipped_grid_puts_the_city_states_in_their_own_region():
    grid = load_population_grid(POPULATION / "population_1km.csv.gz")

    berlin = grid[grid["lon"].between(13.35, 13.45) & grid["lat"].between(52.48, 52.55)]
    hamburg = grid[grid["lon"].between(9.95, 10.05) & grid["lat"].between(53.53, 53.60)]

    assert set(berlin["nuts2"]) == {"DE30"} and len(berlin) > 30
    assert set(hamburg["nuts2"]) == {"DE60"} and len(hamburg) > 30
