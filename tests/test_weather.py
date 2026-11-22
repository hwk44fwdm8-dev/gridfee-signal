"""Capacity factors from weather and the hourly split of TSO-zone feed-in with them."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gridfee.injections import build_historical_injections
from gridfee.injections.weather import (
    read_capacity_factors,
    solar_capacity_factor,
    wind_capacity_factor,
    write_capacity_factors,
    zone_mean,
)
from gridfee.network import load_zones
from tests.test_injections import CAPACITY, DE22, HOURS, LOAD, POPULATION, RES, ZONES

SHIPPED = DE22.parents[2] / "weather" / "capacity_factors_2023.csv"


def test_wind_power_curve():
    factor = wind_capacity_factor([0.0, 3.0, 12.0, 20.0, 25.0, 30.0])
    assert factor.tolist() == [0.0, 0.0, 1.0, 1.0, 0.0, 0.0]
    half = wind_capacity_factor(((12.0**3 + 3.0**3) / 2) ** (1 / 3))
    assert half == pytest.approx(0.5)


def test_solar_factor_is_clipped():
    assert solar_capacity_factor([-1.0, 0.0, 500.0, 1200.0]).tolist() == [0.0, 0.0, 0.5, 1.0]


def test_zone_mean_leaves_out_cells_without_zone():
    cells = pd.DataFrame([[1.0, 3.0, 10.0, 99.0]], columns=["a", "b", "c", "d"])
    zone = pd.Series({"a": "Z1", "b": "Z1", "c": "Z2"})
    assert zone_mean(cells, zone).iloc[0].to_dict() == {"Z1": 2.0, "Z2": 10.0}


def factors(a1, a2, t1=(1.0, 1.0, 1.0)) -> pd.DataFrame:
    return pd.DataFrame({"A1": a1, "A2": a2, "T1": t1}, index=HOURS.tz_convert("UTC"))


def test_feed_in_is_split_by_share_times_factor():
    # Shares are 0.9 / 0.1. Hour 0: same weather. Hour 1: only A2 has wind.
    # Hour 2: A2 has nine times the wind of A1, so both get half.
    wind = factors([0.5, 0.0, 0.1], [0.5, 0.2, 0.9])
    result = build_historical_injections(
        ZONES, LOAD, RES, POPULATION, CAPACITY, capacity_factors={"wind_onshore": wind}
    )
    onshore = result.res_mw["wind_onshore"]
    assert onshore["A1"].tolist() == pytest.approx([9.0, 0.0, 15.0])
    assert onshore["A2"].tolist() == pytest.approx([1.0, 20.0, 15.0])
    assert onshore["T1"].tolist() == [5.0, 6.0, 7.0]
    # Other quantities are untouched.
    assert result.demand_mw["A1"].tolist() == [25.0, 50.0, 75.0]
    assert result.res_mw["wind_offshore"]["T1"].tolist() == [1.0, 2.0, 3.0]


def test_hours_without_any_factor_fall_back_to_shares():
    wind = factors([0.0, 0.3, 0.3], [0.0, 0.3, 0.3])
    result = build_historical_injections(
        ZONES, LOAD, RES, POPULATION, CAPACITY, capacity_factors={"wind_onshore": wind}
    )
    assert result.res_mw["wind_onshore"]["A1"].tolist() == pytest.approx([9.0, 18.0, 27.0])


def test_factors_must_cover_hours_and_zones():
    wind = factors([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
    with pytest.raises(ValueError, match="1 hours are not covered"):
        build_historical_injections(
            ZONES, LOAD, RES, POPULATION, CAPACITY, capacity_factors={"wind_onshore": wind[1:]}
        )
    with pytest.raises(ValueError, match=r"missing zones \['A2'\]"):
        build_historical_injections(
            ZONES,
            LOAD,
            RES,
            POPULATION,
            CAPACITY,
            capacity_factors={"wind_onshore": wind.drop(columns="A2")},
        )
    with pytest.raises(ValueError, match="technologies without data"):
        build_historical_injections(
            ZONES, LOAD, RES, POPULATION, CAPACITY, capacity_factors={"solar": wind}
        )
    wind.iloc[0, 0] = -0.1
    with pytest.raises(ValueError, match="not negative"):
        build_historical_injections(
            ZONES, LOAD, RES, POPULATION, CAPACITY, capacity_factors={"wind_onshore": wind}
        )


def test_zone_without_capacity_needs_no_factor():
    capacity = CAPACITY.copy()
    capacity.loc[capacity["zone_id"] == "A1", "share"] = 1.0
    capacity.loc[capacity["zone_id"] == "A2", "share"] = 0.0
    wind = factors([0.5, 0.0, 0.5], [0.5, 0.5, 0.5]).drop(columns="A2")
    result = build_historical_injections(
        ZONES, LOAD, RES, POPULATION, capacity, capacity_factors={"wind_onshore": wind}
    )
    # Hour 1: the only zone with capacity has no wind, so the share decides.
    assert result.res_mw["wind_onshore"]["A1"].tolist() == [10.0, 20.0, 30.0]
    assert result.res_mw["wind_onshore"]["A2"].tolist() == [0.0, 0.0, 0.0]


def test_factor_file_round_trip(tmp_path):
    wind = factors([0.5, 0.0, 0.1], [0.5, 0.2, 0.9])
    path = tmp_path / "factors.csv"
    write_capacity_factors({"wind_onshore": wind, "solar": wind / 2}, path, "line 1\nline 2")

    assert path.read_text().startswith("# line 1\n# line 2\nhour_utc,wind_onshore/A1")
    read = read_capacity_factors(path)
    assert set(read) == {"wind_onshore", "solar"}
    np.testing.assert_allclose(read["solar"].to_numpy(), (wind / 2).to_numpy())
    assert read["wind_onshore"].index.equals(wind.index)


def test_factor_file_errors(tmp_path):
    path = tmp_path / "factors.csv"
    path.write_text("hour_utc,wind_onshore\n2023-01-01T00:00Z,0.5\n")
    with pytest.raises(ValueError, match="not technology/zone_id"):
        read_capacity_factors(path)
    path.write_text("hour_utc,solar/A1\n2023-01-01T00:00Z,\n")
    with pytest.raises(ValueError, match="not missing"):
        read_capacity_factors(path)
    path.write_text("hour_utc,solar/A1\n2023-01-01T00:00Z,0.1\n2023-01-01T00:00Z,0.2\n")
    with pytest.raises(ValueError, match="more than once"):
        read_capacity_factors(path)


def test_shipped_factors_cover_2023_and_all_zones():
    factors = read_capacity_factors(SHIPPED)
    zone_ids = {zone.zone_id for zone in load_zones(DE22) if zone.tso_zone}
    hours = pd.date_range("2023-01-01", "2023-12-31 23:00", freq="h", tz="Europe/Berlin")

    assert set(factors) == {"wind_onshore", "solar"}
    for frame in factors.values():
        assert set(frame.columns) == zone_ids
        assert frame.index.equals(hours.tz_convert("UTC"))
        assert float(frame.max().max()) <= 1.0
    # No sun at midnight in winter, wind somewhere in every hour of the year.
    assert factors["solar"].loc["2023-01-15T00:00Z"].sum() == 0.0
    assert (factors["wind_onshore"].sum(axis=1) > 0).all()
