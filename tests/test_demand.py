"""Demand per sector: profiles, placement on substations, the hourly split."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from gridfee.injections import build_historical_injections
from gridfee.injections.demand import (
    CONTINUOUS,
    INDUSTRY_COLUMNS,
    SECTORS,
    SHIFT_OF_BRANCH,
    DemandError,
    day_classes,
    household_dynamisation,
    load_profile_tables,
    place_on_substations,
    profile_tables_from_year,
    public_holidays,
    read_sector_demand,
    sector_demand_split,
    shift_profiles,
    spread_over_cells,
    standard_profiles,
)
from gridfee.network.schema import Zone

ROOT = Path(__file__).parents[1]
TABLES = ROOT / "data" / "demand" / "profile_tables.csv"
DESUB = ROOT / "data" / "networks" / "desub"


def hours(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="h", tz="UTC", inclusive="left", name="hour")


def test_public_holidays_of_2023() -> None:
    assert public_holidays(2023) == {
        date(2023, 1, 1),
        date(2023, 4, 7),
        date(2023, 4, 10),
        date(2023, 5, 1),
        date(2023, 5, 18),
        date(2023, 5, 29),
        date(2023, 10, 3),
        date(2023, 12, 25),
        date(2023, 12, 26),
    }


def test_day_classes_know_seasons_weekends_and_holidays() -> None:
    days = pd.DatetimeIndex(["2023-01-02", "2023-05-01", "2023-07-15", "2023-10-01", "2023-12-25"])
    classes = day_classes(days)
    assert list(classes["season"]) == ["winter", "transition", "summer", "transition", "winter"]
    assert list(classes["day_type"]) == ["workday", "sunday", "saturday", "sunday", "sunday"]


def test_tables_are_recovered_from_a_year_of_hourly_profiles() -> None:
    tables = load_profile_tables(TABLES)
    local = pd.date_range("2019-01-01", "2020-01-01", freq="h", inclusive="left")
    keys = day_classes(local).assign(hour=local.hour).reset_index(drop=True)
    year = keys.merge(tables, on=["season", "day_type", "hour"], how="left")
    households = year["households"].to_numpy() * household_dynamisation(local.dayofyear)
    rebuilt = profile_tables_from_year(households, year["cts"].to_numpy(), 2019)
    assert rebuilt[["households", "cts"]].to_numpy() == pytest.approx(
        tables[["households", "cts"]].to_numpy(), rel=1e-3
    )
    with pytest.raises(DemandError, match="expected 8760 hours"):
        profile_tables_from_year(households[:-1], households, 2019)


def test_shipped_tables_are_complete_and_plausible() -> None:
    tables = load_profile_tables(TABLES)
    assert tables[["households", "cts"]].mean().to_numpy() == pytest.approx([1.0, 1.0], abs=1e-4)
    by_day = tables.groupby("day_type")[["households", "cts"]].mean()
    assert by_day.loc["sunday", "cts"] < 0.7 * by_day.loc["workday", "cts"]
    night = tables[tables["hour"] == 3]["households"].mean()
    evening = tables[tables["hour"] == 19]["households"].mean()
    assert evening > 2 * night


def test_standard_profiles_follow_local_time_and_the_season() -> None:
    tables = load_profile_tables(TABLES)
    profiles = standard_profiles(hours("2023-01-01", "2024-01-01"), tables)
    assert not profiles.isna().any().any()
    winter = profiles.loc["2023-01-11 11:00+00:00"]  # Wednesday, 12:00 local
    summer = profiles.loc["2023-07-12 10:00+00:00"]  # Wednesday, 12:00 local
    row = tables[(tables["day_type"] == "workday") & (tables["hour"] == 12)].set_index("season")
    assert winter["cts"] == pytest.approx(row.loc["winter", "cts"])
    assert summer["cts"] == pytest.approx(row.loc["summer", "cts"])
    assert winter["households"] > summer["households"]
    with pytest.raises(DemandError, match="timezone"):
        standard_profiles(pd.date_range("2023-01-01", periods=3, freq="h"), tables)


def test_shift_profiles_follow_working_hours_and_days() -> None:
    index = hours("2023-12-17 23:00", "2024-01-01")  # Monday 18 December to Sunday 31 December
    shifts = shift_profiles(index).set_axis(index.tz_convert("Europe/Berlin"))
    assert list(shifts.columns) == INDUSTRY_COLUMNS
    assert set(SHIFT_OF_BRANCH.values()) <= {c.removeprefix("industry_") for c in shifts}
    assert (shifts[CONTINUOUS] == 1.0).all()
    monday = shifts.loc["2023-12-18"]
    one = monday["industry_s1_wt"]
    assert one.iloc[7] == 0.4 and one.iloc[8] == 1.0 and one.iloc[15] == 1.0
    assert one.iloc[16] == pytest.approx(0.7)  # worked until 16:30
    two = monday["industry_s2_wt"]
    assert two.iloc[5] == 0.4 and two.iloc[6] == 1.0 and two.iloc[22] == 1.0 and two.iloc[23] == 0.4
    saturday, sunday = shifts.loc["2023-12-23"], shifts.loc["2023-12-24"]
    assert (saturday["industry_s3_wt"] == 0.4).all()
    assert (saturday["industry_s3_wt_sa"] == 1.0).all()
    assert (sunday["industry_s3_wt_sa"] == 0.4).all()
    # Christmas Day is a holiday; 27 December a workday; Sunday 31 December stays a Sunday.
    assert (shifts.loc["2023-12-25", "industry_s3_wt_sa"] == 0.4).all()
    assert (shifts.loc["2023-12-27", "industry_s3_wt"] == 1.0).all()
    assert (shifts.loc["2023-12-31", "industry_s3_wt_sa"] == 0.4).all()
    # 24 December on a workday counts as a Saturday.
    eve = shift_profiles(hours("2024-12-24 06:00", "2024-12-24 18:00"))
    assert (eve["industry_s3_wt"] == 0.4).all() and (eve["industry_s3_wt_sa"] == 1.0).all()
    with pytest.raises(DemandError, match="timezone-aware"):
        shift_profiles(pd.date_range("2023-01-01", periods=3, freq="h"))


def test_spread_falls_back_to_district_and_state() -> None:
    cells = pd.DataFrame(
        {
            "ags": ["01001000", "01001000", "01002000", "02000000"],
            "population": [10, 30, 60, 100],
        }
    )
    municipal = pd.DataFrame(
        {
            "ags": ["01001000", "01002999", "01999999", "02000000"],
            "households": [40.0, 100.0, 0.0, 7.0],
            "cts": [0.0, 0.0, 50.0, 7.0],
            "industry": [8.0, 0.0, 0.0, 7.0],
        }
    )
    spread, levels = spread_over_cells(cells, municipal)
    # 01001000 by population; 01002999 over district 01002; 01999999 over state 01.
    assert list(spread["households"]) == [10.0, 30.0, 100.0, 7.0]
    assert list(spread["cts"]) == [5.0, 15.0, 30.0, 7.0]
    assert list(spread["industry"]) == [2.0, 6.0, 0.0, 7.0]
    assert levels.loc["district", "households"] == 100.0
    assert levels.loc["state", "cts"] == 50.0
    assert spread[SECTORS].sum().to_numpy() == pytest.approx(municipal[SECTORS].sum().to_numpy())
    with pytest.raises(DemandError, match="no cells in the states"):
        spread_over_cells(cells, municipal.assign(ags=["01001000", "01002999", "09000000", "02"]))


def test_cells_go_to_the_nearest_substation_of_their_region() -> None:
    cells = pd.DataFrame(
        {
            "lon": [9.0, 9.1, 13.4],
            "lat": [48.7, 48.8, 52.5],
            "nuts2": ["DE11", "DE11", "DE30"],
            "households": [1.0, 2.0, 3.0],
            "cts": [0.0, 4.0, 0.0],
            "industry": [10.0, 0.0, 0.0],
        }
    )
    substations = pd.DataFrame(
        {
            "substation_id": ["south", "north", "berlin_a", "berlin_b"],
            "tso": ["T", "T", "F", "F"],
            "nuts2": ["DE11", "DE11", "DE30", "DE30"],
            "lat": [48.7, 48.8, 52.5, 52.5],
            "lon": [9.0, 9.1, 13.4, 13.4],
        }
    )
    placed = place_on_substations(cells, substations).set_index("substation_id")
    assert list(placed.loc["south", SECTORS]) == [1.0, 0.0, 10.0]
    assert list(placed.loc["north", SECTORS]) == [2.0, 4.0, 0.0]
    assert list(placed.loc["berlin_a", SECTORS]) == [1.5, 0.0, 0.0]  # shared position
    assert placed["load_share"].sum() == pytest.approx(1.0)
    with pytest.raises(DemandError, match="no substation"):
        place_on_substations(cells, substations[substations["nuts2"] == "DE11"])


def test_split_keeps_the_tso_load_and_gives_industry_a_flatter_curve() -> None:
    tables = load_profile_tables(TABLES)
    index = hours("2023-01-01", "2023-03-01")
    profiles = standard_profiles(index, tables)
    load = pd.DataFrame(
        {
            "TenneT": 1000 * (profiles["households"] + profiles["cts"]) + 1500,
            "50Hertz": 800.0 + 100 * profiles["cts"],
        }
    )
    zones = [
        Zone(zone_id="homes", name="homes", tso_zone="TenneT"),
        Zone(zone_id="works", name="works", tso_zone="TenneT"),
        Zone(zone_id="tee", name="tee", tso_zone="TenneT"),
        Zone(zone_id="other", name="other", tso_zone="50Hertz"),
    ]
    sectors = pd.DataFrame(
        {
            "zone_id": ["homes", "works", "other"],
            "households": [900.0, 100.0, 5.0],
            "cts": [600.0, 400.0, 5.0],
            "industry": [0.0, 2000.0, 5.0],
            "industry_s3_wt_sa_so": [0.0, 2000.0, 0.0],
            "industry_s1_wt": [0.0, 0.0, 5.0],
        }
    )
    sectors = sectors.reindex(columns=["zone_id", *SECTORS, *INDUSTRY_COLUMNS], fill_value=0.0)
    tso_of = {zone.zone_id: zone.tso_zone for zone in zones}
    shares, factors = sector_demand_split(tso_of, load, sectors, tables)
    by_zone = shares.set_index("zone_id")["share"]
    assert by_zone["homes"] == pytest.approx(1500 / 4000)
    assert by_zone["tee"] == 0.0
    assert by_zone["other"] == 1.0

    result = build_historical_injections(zones, load, {}, shares, demand_factors=factors)
    demand = result.demand_mw
    assert (demand["homes"] + demand["works"]).to_numpy() == pytest.approx(
        load["TenneT"].to_numpy()
    )
    assert demand["other"].to_numpy() == pytest.approx(load["50Hertz"].to_numpy())
    assert (demand["tee"] == 0).all()
    # Annual energy follows the annual shares to within the effect of the hourly scaling.
    assert demand["homes"].sum() / load["TenneT"].sum() == pytest.approx(1500 / 4000, rel=0.02)
    spread = demand.std() / demand.mean()
    assert spread["works"] < 0.5 * spread["homes"]

    with pytest.raises(DemandError, match="unknown zones"):
        sector_demand_split({"homes": "TenneT"}, load, sectors, tables)
    with pytest.raises(DemandError, match="no sector demand in TSO zone 50Hertz"):
        sector_demand_split(tso_of, load, sectors[sectors["zone_id"] != "other"], tables)


def test_read_sector_demand_checks_its_columns(tmp_path: Path) -> None:
    path = tmp_path / "sectors.csv"
    path.write_text("# note\nzone_id,households,cts,industry,extra\na,1,2,3,x\n")
    read = read_sector_demand(path)
    assert list(read.columns) == ["zone_id", *SECTORS, *INDUSTRY_COLUMNS]
    assert read[CONTINUOUS].item() == 3.0  # without shift classes industry runs continuously
    path.write_text("zone_id,households,cts,industry,industry_s2_wt\na,1,2,300,100\n")
    with pytest.raises(DemandError, match="do not add up"):
        read_sector_demand(path)
    path.write_text("zone_id,households,cts\na,1,2\n")
    with pytest.raises(DemandError, match="industry"):
        read_sector_demand(path)
    path.write_text("zone_id,households,cts,industry\na,1,2,-3\n")
    with pytest.raises(DemandError, match="not negative"):
        read_sector_demand(path)


def test_shipped_sector_demand_covers_every_substation() -> None:
    placed = pd.read_csv(DESUB / "sector_demand.csv", comment="#")
    listed = pd.read_csv(DESUB / "substations.csv", comment="#")
    substations = listed[listed["kind"] == "substation"]
    assert list(placed["substation_id"]) == list(substations["substation_id"])
    assert placed["load_share"].sum() == pytest.approx(1.0, abs=1e-4)
    twh = placed[SECTORS].sum() / 1e6
    # DemandRegio: households 129.5, commerce 153.1 and industry 183.7 TWh from the grid
    # (240.5 TWh of use less 56.8 TWh of own generation).
    assert twh.to_numpy() == pytest.approx([129.5, 153.1, 183.7], abs=0.1)
    assert placed[INDUSTRY_COLUMNS].sum(axis=1).to_numpy() == pytest.approx(
        placed["industry"].to_numpy(), abs=1.0
    )
    # Chemicals and metals run around the clock: more than half of industry's grid draw.
    assert 0.5 < placed[CONTINUOUS].sum() / placed["industry"].sum() < 0.6
    population = pd.read_csv(DESUB / "load_shares.csv", comment="#").set_index("substation_id")
    households = placed.set_index("substation_id")["households"]
    per_head = households / population["population"].reindex(households.index)
    assert per_head[population["population"] > 50_000].between(1.2, 2.0).all()
