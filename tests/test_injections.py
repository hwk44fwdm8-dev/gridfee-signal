"""Historical injections on hand-checkable numbers, and the shipped 22-zone list."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from gridfee.injections import (
    EnergyBalanceError,
    ZonalInjections,
    build_historical_injections,
    check_energy_balance,
    read_shares,
)
from gridfee.network import NetworkConfigError, Zone, load_zones

DE22 = Path(__file__).parents[1] / "data" / "networks" / "de22" / "zones.csv"

ZONES = [
    Zone(zone_id="A1", name="A1", tso_zone="Amprion"),
    Zone(zone_id="A2", name="A2", tso_zone="Amprion"),
    Zone(zone_id="T1", name="T1", tso_zone="TenneT"),
    Zone(zone_id="X", name="Neighbour", kind="boundary", country="XX"),
]
HOURS = pd.date_range("2023-01-01", periods=3, freq="h", tz="Europe/Berlin")
LOAD = pd.DataFrame({"Amprion": [100.0, 200.0, 300.0], "TenneT": [50.0, 60.0, 70.0]}, index=HOURS)
RES = {
    "wind_onshore": pd.DataFrame({"Amprion": [10.0, 20, 30], "TenneT": [5.0, 6, 7]}, index=HOURS),
    "wind_offshore": pd.DataFrame({"TenneT": [1.0, 2.0, 3.0]}, index=HOURS),
}


def shares(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["quantity", "zone_id", "share"])


POPULATION = shares([("demand", "A1", 0.25), ("demand", "A2", 0.75), ("demand", "T1", 1.0)])
CAPACITY = shares(
    [
        ("wind_onshore", "A1", 0.9),
        ("wind_onshore", "A2", 0.1),
        ("wind_onshore", "T1", 1.0),
        ("wind_offshore", "T1", 1.0),
    ]
)


def test_series_are_split_by_share():
    result = build_historical_injections(ZONES, LOAD, RES, POPULATION, CAPACITY)

    assert list(result.demand_mw.columns) == ["A1", "A2", "T1"]
    assert result.demand_mw["A1"].tolist() == [25.0, 50.0, 75.0]
    assert result.demand_mw["A2"].tolist() == [75.0, 150.0, 225.0]
    assert result.demand_mw["T1"].tolist() == [50.0, 60.0, 70.0]
    assert result.res_mw["wind_onshore"]["A1"].tolist() == [9.0, 18.0, 27.0]
    # No offshore series for Amprion: its zones get none.
    assert result.res_mw["wind_offshore"].iloc[2].tolist() == [0.0, 0.0, 3.0]
    assert result.net_injection_mw.iloc[0].tolist() == pytest.approx([-16.0, -74.0, -44.0])
    assert result.demand_mw.index.equals(HOURS)
    assert not result.shares["placeholder"].any()
    assert result.placeholder_quantities == []


def test_missing_shares_become_flagged_uniform_placeholders(caplog):
    with caplog.at_level(logging.WARNING, logger="gridfee.injections.historical"):
        result = build_historical_injections(ZONES, LOAD, RES)

    assert result.demand_mw["A1"].tolist() == [50.0, 100.0, 150.0]
    assert result.demand_mw["A1"].equals(result.demand_mw["A2"])
    assert result.shares["placeholder"].all()
    assert result.placeholder_quantities == ["demand", "wind_offshore", "wind_onshore"]
    assert "demand in Amprion: no shares given, using uniform PLACEHOLDER shares" in caplog.text


def test_placeholders_are_flagged_per_tso_zone_and_quantity():
    population = shares([("demand", "A1", 0.25), ("demand", "A2", 0.75)])
    capacity = CAPACITY[CAPACITY["quantity"] == "wind_offshore"]
    result = build_historical_injections(ZONES, LOAD, RES, population, capacity)

    flags = result.shares.set_index(["quantity", "zone_id"])["placeholder"]
    assert not flags["demand", "A1"] and flags["demand", "T1"]
    assert flags["wind_onshore"].all() and not flags["wind_offshore"].any()
    assert result.placeholder_quantities == ["demand", "wind_onshore"]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([("demand", "A1", 0.25), ("demand", "A2", 0.7)], "sum to 0.95"),
        ([("demand", "A1", 1.0)], r"missing zones \['A2'\]"),
        ([("demand", "A1", 1.5), ("demand", "A2", -0.5)], "not negative"),
        ([("demand", "A1", 0.5), ("demand", "A1", 0.5)], "more than once"),
        ([("demand", "Q9", 1.0)], "unknown zones"),
        ([("demand", "X", 1.0)], "unknown zones"),
    ],
)
def test_bad_population_shares_are_rejected(rows, message):
    with pytest.raises(ValueError, match=message):
        build_historical_injections(ZONES, LOAD, RES, shares(rows))


def test_bad_inputs_are_rejected():
    with pytest.raises(ValueError, match="technologies without data"):
        build_historical_injections(ZONES, LOAD, RES, None, shares([("solar", "A1", 1.0)]))
    with pytest.raises(ValueError, match="same hours"):
        build_historical_injections(ZONES, LOAD.iloc[:2], RES)
    with pytest.raises(ValueError, match="missing values"):
        build_historical_injections(ZONES, LOAD.mask(LOAD > 250), RES)
    with pytest.raises(ValueError, match=r"no load series for TSO zones \['TenneT'\]"):
        build_historical_injections(ZONES, LOAD[["Amprion"]], RES)
    with pytest.raises(ValueError, match="no zone belongs to TSO zone '50Hertz'"):
        build_historical_injections(ZONES, LOAD.assign(**{"50Hertz": 1.0}), RES)


def test_energy_balance_report():
    result = build_historical_injections(ZONES, LOAD, RES, POPULATION, CAPACITY)
    balance = check_energy_balance(ZONES, result, LOAD, RES).set_index(["tso_zone", "quantity"])

    assert len(balance) == 6 and balance["passed"].all()
    assert balance.loc[("Amprion", "demand"), "tso_twh"] == pytest.approx(600 / 1e6)
    assert balance.loc[("Amprion", "demand"), "zonal_twh"] == pytest.approx(600 / 1e6)
    assert balance.loc[("Amprion", "wind_offshore"), "zonal_twh"] == 0


def test_energy_balance_catches_lost_and_misplaced_energy():
    good = build_historical_injections(ZONES, LOAD, RES, POPULATION, CAPACITY)

    lost = ZonalInjections(
        good.demand_mw.assign(A2=good.demand_mw["A2"] * 0.99), good.res_mw, good.shares
    )
    balance = check_energy_balance(ZONES, lost, LOAD, RES).set_index(["tso_zone", "quantity"])
    assert not balance.loc[("Amprion", "demand"), "passed"]
    assert balance.loc[("Amprion", "demand"), "max_abs_diff_mw"] == pytest.approx(2.25)
    assert balance.drop(("Amprion", "demand"))["passed"].all()

    # Offshore feed-in in a TSO zone that has no offshore series.
    offshore = good.res_mw["wind_offshore"].assign(A1=1.0)
    misplaced = ZonalInjections(
        good.demand_mw, {**good.res_mw, "wind_offshore": offshore}, good.shares
    )
    balance = check_energy_balance(ZONES, misplaced, LOAD, RES).set_index(["tso_zone", "quantity"])
    assert not balance.loc[("Amprion", "wind_offshore"), "passed"]


def test_energy_balance_rejects_missing_hours_and_values():
    good = build_historical_injections(ZONES, LOAD, RES, POPULATION, CAPACITY)
    short = ZonalInjections(good.demand_mw.iloc[:2], good.res_mw, good.shares)
    with pytest.raises(EnergyBalanceError, match="demand: zonal and TSO-zone series cover"):
        check_energy_balance(ZONES, short, LOAD, RES)
    holey = ZonalInjections(good.demand_mw.mask(good.demand_mw > 200), good.res_mw, good.shares)
    with pytest.raises(EnergyBalanceError, match="demand: series contain missing values"):
        check_energy_balance(ZONES, holey, LOAD, RES)


def test_build_raises_when_balance_fails(monkeypatch):
    import gridfee.injections.historical as historical

    def leaky(*args, **kwargs):
        frame = real(*args, **kwargs)
        return frame.assign(share=frame["share"] * 0.5)

    real = historical.resolve_shares
    monkeypatch.setattr(historical, "resolve_shares", leaky)
    with pytest.raises(EnergyBalanceError, match="Amprion"):
        build_historical_injections(ZONES, LOAD, RES)


def test_read_shares(tmp_path):
    population = tmp_path / "population.csv"
    population.write_text("# source: test\nzone_id,share\n01,0.4\n02,0.6\n")
    frame = read_shares(population)
    assert frame.to_dict("list") == {
        "quantity": ["demand", "demand"],
        "zone_id": ["01", "02"],
        "share": [0.4, 0.6],
    }

    capacity = tmp_path / "capacity.csv"
    capacity.write_text("technology,zone_id,share\nsolar,01,1.0\n")
    assert read_shares(capacity, "technology")["quantity"].tolist() == ["solar"]
    with pytest.raises(ValueError, match="expected the columns"):
        read_shares(capacity)


def test_shipped_22_zones():
    zones = [zone for zone in load_zones(DE22) if zone.kind == "internal"]
    assert [zone.zone_id for zone in zones] == [f"DE{n:02d}" for n in range(1, 23)]
    by_tso: dict[str, list[str]] = {}
    for zone in zones:
        by_tso.setdefault(zone.tso_zone, []).append(zone.zone_id)
    assert by_tso == {
        "50Hertz": ["DE01", "DE02", "DE03", "DE04", "DE05", "DE06"],
        "TenneT": ["DE07", "DE08", "DE09", "DE12", "DE13", "DE14", "DE15", "DE16"],
        "Amprion": ["DE10", "DE11", "DE17", "DE18"],
        "TransnetBW": ["DE19", "DE20", "DE21", "DE22"],
    }


def test_load_zones_rejects_duplicates(tmp_path):
    path = tmp_path / "zones.csv"
    path.write_text("zone_id,name,tso_zone\nA,A,TenneT\nA,B,TenneT\n")
    with pytest.raises(NetworkConfigError, match="duplicate zone ids"):
        load_zones(path)
