"""Validation checks and the Markdown report on hand-checkable numbers."""

from __future__ import annotations

import pandas as pd
import pytest

from gridfee.network import Line, Zone, ZoneNetwork
from gridfee.validation import (
    FAIL,
    INFO,
    PASS,
    ValidationError,
    check_cross_border,
    check_residual_generation,
    check_wind_north_share,
    render_report,
)

HOURS = pd.date_range("2023-01-01", periods=2, freq="h", tz="Europe/Berlin", name="snapshot")


def _zone(zone_id: str, tso: str) -> Zone:
    return Zone(zone_id=zone_id, name=zone_id, tso_zone=tso)


def _line(a: str, b: str) -> Line:
    return Line(
        line_id=f"{a}-{b}", from_zone=a, to_zone=b, susceptance_mw_per_rad=1, capacity_mw=1e4
    )


NETWORK = ZoneNetwork(
    zones=(
        _zone("N", "TenneT"),
        _zone("S", "Amprion"),
        _zone("W", "TransnetBW"),
        Zone(zone_id="FR", name="France", kind="boundary", country="FR"),
    ),
    lines=(_line("N", "S"), _line("S", "W"), _line("S", "FR"), _line("FR", "W")),
)
WIND_TSO = {
    "wind_onshore": pd.DataFrame({"TenneT": [600.0, 600.0], "Amprion": [200.0, 200.0]}, HOURS),
    "wind_offshore": pd.DataFrame({"TenneT": [200.0, 200.0]}, HOURS),
}


def _dispatch(north_onshore: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "wind_onshore:N": [north_onshore] * 2,
            "wind_offshore:N": [200.0] * 2,
            "wind_onshore:S": [200.0] * 2,
            "residual:S": [50.0] * 2,
        },
        index=HOURS,
    )


def test_wind_north_share_passes_without_curtailment():
    row = check_wind_north_share(NETWORK, _dispatch(600.0), WIND_TSO).iloc[0]

    assert row["observed"] == pytest.approx(0.8)
    assert row["modelled"] == pytest.approx(0.8)
    assert row["status"] == PASS


def test_wind_north_share_fails_when_the_model_curtails_the_north():
    # North dispatch 200 + 200 of 600 total: share 2/3 instead of 0.8, 16.7 % off.
    row = check_wind_north_share(NETWORK, _dispatch(200.0), WIND_TSO).iloc[0]

    assert row["modelled"] == pytest.approx(2 / 3)
    assert row["deviation"] == pytest.approx(1 / 6)
    assert row["status"] == FAIL
    assert (
        check_wind_north_share(NETWORK, _dispatch(200.0), WIND_TSO, tolerance=0.2).iloc[0]["status"]
        == PASS
    )


def test_wind_check_needs_observations_for_every_modelled_hour():
    short = {tech: frame.iloc[:1] for tech, frame in WIND_TSO.items()}
    with pytest.raises(ValidationError, match="no observation for 1 modelled hours"):
        check_wind_north_share(NETWORK, _dispatch(600.0), short)
    with pytest.raises(ValidationError, match="no wind generators"):
        check_wind_north_share(NETWORK, _dispatch(600.0)[["residual:S"]], WIND_TSO)


FLOWS = pd.DataFrame(
    {"N-S": [0.0, 0.0], "S-W": [0.0, 0.0], "S-FR": [100.0, -40.0], "FR-W": [20.0, -10.0]},
    index=HOURS,
)
OBSERVED = pd.DataFrame(
    {"Amprion_FR": [90.0, -50.0], "TransnetBW_FR": [-10.0, 30.0], "Amprion_BE": [None, None]},
    index=HOURS,
    dtype=float,
)


def test_cross_border_compares_neighbours_and_tso_zones():
    checks = check_cross_border(NETWORK, FLOWS, OBSERVED).set_index(["check", "item"])

    # Model: S -> FR 100, -40; W -> FR is minus FR-W: -20, 10. Net 80 - 30 = 50 MWh.
    # Observed: 80 - 20 = 60 MWh net, 100 MWh gross.
    row = checks.loc[("net export per neighbour", "FR")]
    assert row["modelled"] == pytest.approx(50e-6)
    assert row["observed"] == pytest.approx(60e-6)
    assert row["deviation"] == pytest.approx(0.10)
    assert row["status"] == PASS
    per_tso = checks.loc["net export per TSO zone and neighbour"]
    assert per_tso.loc["Amprion - FR", "modelled"] == pytest.approx(60e-6)
    assert per_tso.loc["TransnetBW - FR", "modelled"] == pytest.approx(-10e-6)
    assert (per_tso["status"] == INFO).all()
    assert "Amprion - BE" not in per_tso.index  # no data for the whole period


def test_cross_border_fails_outside_the_tolerance_and_on_missing_observations():
    checks = check_cross_border(NETWORK, FLOWS, OBSERVED, tolerance=0.05)
    assert checks.iloc[0]["status"] == FAIL

    gap = OBSERVED.copy()
    gap.iloc[0, 0] = float("nan")
    with pytest.raises(ValidationError, match="missing in modelled hours"):
        check_cross_border(NETWORK, FLOWS, gap)


def test_tso_border_without_a_modelled_line_is_listed_without_a_model_value():
    observed = OBSERVED.assign(TenneT_FR=[5.0, 5.0])

    checks = check_cross_border(NETWORK, FLOWS, observed).set_index("item")

    assert pd.isna(checks.loc["TenneT - FR", "modelled"])
    assert checks.loc["TenneT - FR", "observed"] == pytest.approx(10e-6)


def test_residual_generation_is_compared_to_the_national_total():
    generation = pd.DataFrame(
        {"gas": [40.0, 40.0], "biomass": [20.0, 20.0], "wind_onshore": [800.0, 800.0]}, index=HOURS
    )

    checks = check_residual_generation(_dispatch(600.0), generation).set_index("check")

    row = checks.loc["generation other than wind and solar"]
    assert row["observed"] == pytest.approx(120e-6)
    assert row["modelled"] == pytest.approx(100e-6)
    assert row["deviation"] == pytest.approx(1 / 6)
    assert checks.loc["wind_onshore fed in", "modelled"] == pytest.approx(1600e-6)
    assert (checks["status"] == INFO).all()


def test_report_has_a_pass_fail_table_and_notes():
    checks = pd.concat(
        [
            check_wind_north_share(NETWORK, _dispatch(200.0), WIND_TSO),
            check_cross_border(NETWORK, FLOWS, OBSERVED),
        ]
    )

    report = render_report(checks, "Backcast 2023", ["PLACEHOLDER shares"])

    assert report.startswith("# Backcast 2023\n")
    assert "**2 checks with a tolerance: 1 FAILED.**" in report
    assert (
        "| wind north share | north = 50Hertz + TenneT | 80.0% | 66.7% | share | 16.7% | 10% "
        "| **FAIL** |"
    ) in report
    assert (
        "| net export per neighbour | FR | 0.00 | 0.00 | TWh | 10.0% | 10% | **PASS** |" in report
    )
    assert "## Comparisons without a verdict" in report
    assert "- PLACEHOLDER shares" in report
