import numpy as np
import pandas as pd
import pytest

from gridfee.signal.asset import asset_fees


def signal(values, start="2023-01-01"):
    return pd.Series(
        values, index=pd.date_range(start, periods=len(values), freq="h", tz="Europe/Berlin")
    )


@pytest.mark.parametrize("kind,sign", [("load", -1), ("generator", 1)])
def test_constant_asset_sign_and_summary(kind, sign):
    result = asset_fees(signal([10, -4, 0, np.nan]), kind, 2)
    assert result.hourly.injection_mw.tolist() == [sign * 2] * 3 + [0]
    assert result.summary == {
        "total_eur": sign * 12,
        "eur_per_mw": sign * 6,
        "eur_per_mwh_moved": sign * 2,
        "hours_paying": 1,
        "hours_earning": 1,
        "hours_neutral": 1,
        "hours_excluded": 1,
    }
    assert np.isnan(result.hourly.fee_eur.iloc[-1])


def test_battery_efficiency_fractional_and_excluded():
    result = asset_fees(signal([10, 8, -4, -2, np.nan]), "battery", 2, 3, 0.5)
    assert result.hourly.injection_mw.tolist() == [-2, -1, 1.5, 0, 0]
    assert result.summary["total_eur"] == -34
    assert result.summary["hours_excluded"] == 1
    assert result.summary["eur_per_mwh_moved"] == pytest.approx(-34 / 4.5)


def test_battery_only_operates_when_beneficial_and_resets_local_day():
    assert asset_fees(signal([-1, -1]), "battery", 1, 1).hourly.injection_mw.eq(0).all()
    result = asset_fees(signal([10, -5, 10, -5], "2023-01-01 22:00"), "battery", 1, 1, 1)
    assert result.hourly.injection_mw.tolist() == [-1, 1, -1, 1]


def test_battery_cannot_discharge_before_charging():
    result = asset_fees(signal([-10, 10]), "battery", 1, 1, 0.9)
    assert result.hourly.injection_mw.tolist() == [0, 0]
    assert result.summary["total_eur"] == 0


def test_battery_capacity_power_exclusions_and_multiple_cycles():
    result = asset_fees(signal([10, np.nan, -10, 8, -8, 10]), "battery", 2, 1.5, 0.8)
    injection = result.hourly.injection_mw.to_numpy()
    assert injection == pytest.approx([-1.5, 0, 1.2, -1.5, 1.2, 0])
    soc = np.cumsum(np.maximum(-injection, 0) - np.maximum(injection, 0) / 0.8)
    assert soc.min() >= -1e-8
    assert soc.max() <= 1.5 + 1e-8
    assert soc[-1] == pytest.approx(0)
    assert result.hourly.state_of_charge_mwh.to_numpy() == pytest.approx(soc)
    assert result.hourly.charge_mw.to_numpy() == pytest.approx(np.maximum(-injection, 0))
    assert result.hourly.discharge_mw.to_numpy() == pytest.approx(np.maximum(injection, 0))
    assert result.hourly.excluded.tolist() == [False, True, False, False, False, False]
    assert np.abs(injection).max() <= 2
    assert result.summary["total_eur"] == pytest.approx(-48.6)


def test_battery_no_simultaneous_charge_discharge_on_single_hour():
    result = asset_fees(signal([10]), "battery", 1, 1, 0.5)
    assert result.hourly.injection_mw.tolist() == [0]


def test_battery_zero_capacity():
    assert asset_fees(signal([10, -10]), "battery", 1, 0).hourly.injection_mw.eq(0).all()


def test_battery_solver_failure(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "gridfee.signal.asset.milp",
        lambda **kwargs: SimpleNamespace(success=False, message="solver failure"),
    )
    with pytest.raises(RuntimeError, match="Battery scheduling failed.*solver failure"):
        asset_fees(signal([10, -10]), "battery", 1, 1)


@pytest.mark.parametrize("values", [[], [np.nan, np.nan]])
def test_empty_or_missing_signal(values):
    result = asset_fees(signal(values), "battery", 1, 2)
    assert result.summary["total_eur"] == 0
    assert result.summary["hours_excluded"] == len(values)
    assert np.isnan(result.summary["eur_per_mwh_moved"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "other"},
        {"power_mw": 0},
        {"energy_mwh": -1},
        {"efficiency": 0},
        {"efficiency": 1.1},
    ],
)
def test_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        asset_fees(signal([1]), **({"kind": "battery", "power_mw": 1} | kwargs))


def test_invalid_signal():
    with pytest.raises(ValueError, match="timestamps"):
        asset_fees(pd.Series([1]), "load", 1)
    with pytest.raises(ValueError, match="finite"):
        asset_fees(signal([np.inf]), "load", 1)
