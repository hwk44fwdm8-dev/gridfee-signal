"""Backcast on a small network with hand-checkable numbers, and the shipped network."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from gridfee.backcast import (
    BackcastError,
    build_backcast_inputs,
    neighbour_net_export,
    read_generation_costs,
    read_generation_shares,
    run_backcast,
)
from gridfee.cli import app
from gridfee.data import smard
from gridfee.injections import build_historical_injections
from gridfee.network import Line, Zone, ZoneNetwork, load_network
from gridfee.signal import NEGATIVE, NEUTRAL, POSITIVE
from gridfee.validation.checks import check_residual_generation

DE22 = Path(__file__).parents[1] / "data" / "networks" / "de22"
HOURS = pd.date_range("2023-01-01", periods=4, freq="h", tz="Europe/Berlin", name="hour")

# North (wind) - South (load) - neighbour X. N-S carries 100 MW.
NETWORK = ZoneNetwork(
    zones=(
        Zone(zone_id="N", name="North", tso_zone="TenneT"),
        Zone(zone_id="S", name="South", tso_zone="Amprion"),
        Zone(zone_id="X", name="Neighbour", kind="boundary", country="XX"),
    ),
    lines=(
        Line(
            line_id="N-S", from_zone="N", to_zone="S", susceptance_mw_per_rad=100, capacity_mw=100
        ),
        Line(line_id="S-X", from_zone="S", to_zone="X", susceptance_mw_per_rad=100, capacity_mw=50),
    ),
)
TSO_LOAD = pd.DataFrame({"TenneT": [50.0] * 4, "Amprion": [150.0] * 4}, index=HOURS)
# h0: wind fits on the line. h1: 250 MW of wind for 50 MW of load and a 100 MW line.
TSO_RES = {"wind_onshore": pd.DataFrame({"TenneT": [100.0, 250.0, 0.0, 100.0]}, index=HOURS)}
GENERATION = pd.DataFrame(
    {"wind_onshore": [100.0, 250.0, 0.0, 100.0], "gas": [100.0, 100.0, 200.0, 120.0]}, index=HOURS
)
# h2: 30 MW import. h3: 80 MW export, above the 50 MW of S-X.
FLOWS = pd.DataFrame({"Amprion_X": [0.0, 0.0, -30.0, 80.0]}, index=HOURS)


def _inputs(**kwargs):
    zonal = build_historical_injections(NETWORK.zones[:2], TSO_LOAD, TSO_RES)
    return build_backcast_inputs(NETWORK, zonal, GENERATION, FLOWS, **kwargs)


def test_inputs_hold_demand_exports_imports_and_residual_generators():
    inputs = _inputs(residual_cost=40.0)

    assert inputs.load_mw["N"].tolist() == [50.0] * 4
    assert inputs.load_mw["X"].tolist() == [0.0, 0.0, 0.0, 50.0]  # export, cut to 50 MW
    generators = inputs.generators
    assert generators.loc["wind_onshore:N", ["zone", "p_nom_mw"]].tolist() == ["N", 250.0]
    assert generators.loc["import:X", ["zone", "p_nom_mw"]].tolist() == ["X", 30.0]
    assert inputs.availability["import:X"].tolist() == [0.0, 0.0, 1.0, 0.0]
    # Residual: the year's peak of 200 MW, split like demand (50 : 150).
    assert inputs.placeholder_residual_shares
    assert generators.loc["residual:N", "p_nom_mw"] == pytest.approx(50.0)
    assert generators.loc["residual:S", "p_nom_mw"] == pytest.approx(150.0)
    assert generators.loc["residual:S", "marginal_cost_eur_per_mwh"] == 40.0
    assert generators.loc["wind_onshore:N", "marginal_cost_eur_per_mwh"] == 0.0
    assert inputs.ptdf_reference == {"N": 200.0, "S": 600.0}


def test_flows_above_the_line_capacity_are_cut_and_reported():
    clipped = _inputs().clipped_exchange

    assert clipped.to_dict("records") == [
        {
            "neighbour": "X",
            "line_capacity_mw": 50.0,
            "max_observed_mw": 80.0,
            "hours": 1,
            "clipped_mwh": 30.0,
        }
    ]


def test_hours_with_missing_flows_are_left_out():
    flows = FLOWS.copy()
    flows.iloc[1, 0] = float("nan")
    zonal = build_historical_injections(NETWORK.zones[:2], TSO_LOAD, TSO_RES)

    inputs = build_backcast_inputs(NETWORK, zonal, GENERATION, flows)

    assert inputs.skipped_hours.tolist() == [HOURS[1]]
    assert inputs.load_mw.index.tolist() == [HOURS[0], HOURS[2], HOURS[3]]


def test_neighbour_net_export_sums_tso_zones_and_ignores_empty_series():
    flows = pd.DataFrame(
        {
            "Amprion_FR": [1.0, 2.0],
            "TransnetBW_FR": [10.0, float("nan")],
            "Amprion_BE": [float("nan")] * 2,
            "TenneT_DK1": [5.0, 6.0],
        }
    )

    exports = neighbour_net_export(flows)

    assert sorted(exports.columns) == ["DK1", "FR"]
    assert exports["FR"].iloc[0] == 11.0
    assert pd.isna(exports["FR"].iloc[1])
    assert exports["DK1"].tolist() == [5.0, 6.0]


def test_given_residual_shares_are_used_and_checked():
    inputs = _inputs(residual_shares=pd.Series({"N": 0.0, "S": 1.0}))

    assert not inputs.placeholder_residual_shares
    assert "residual:N" not in inputs.generators.index
    assert inputs.generators.loc["residual:S", "p_nom_mw"] == pytest.approx(200.0)
    with pytest.raises(BackcastError, match="sum to 1"):
        _inputs(residual_shares=pd.Series({"N": 0.5, "S": 0.6}))
    with pytest.raises(BackcastError, match="every internal zone"):
        _inputs(residual_shares=pd.Series({"S": 1.0}))
    with pytest.raises(BackcastError, match="without a share"):
        _inputs(residual_shares=pd.Series({"N": float("nan"), "S": 1.0}))


def test_residual_capacity_uses_the_peak_of_a_left_out_hour():
    flows = FLOWS.copy()
    flows.iloc[2, 0] = float("nan")  # the hour with the highest other generation
    zonal = build_historical_injections(NETWORK.zones[:2], TSO_LOAD, TSO_RES)

    inputs = build_backcast_inputs(NETWORK, zonal, GENERATION, flows)

    assert inputs.generators.loc["residual:S", "p_nom_mw"] == pytest.approx(150.0)


def test_without_any_flow_observation_the_exchange_is_zero():
    zonal = build_historical_injections(NETWORK.zones[:2], TSO_LOAD, TSO_RES)

    inputs = build_backcast_inputs(NETWORK, zonal, GENERATION, FLOWS * float("nan"))

    assert len(inputs.skipped_hours) == 0
    assert list(inputs.load_mw.columns) == ["N", "S"]


def test_unknown_neighbour_and_mismatched_hours_are_rejected():
    zonal = build_historical_injections(NETWORK.zones[:2], TSO_LOAD, TSO_RES)
    with pytest.raises(BackcastError, match="not boundary zones"):
        build_backcast_inputs(
            NETWORK, zonal, GENERATION, FLOWS.rename(columns={"Amprion_X": "Amprion_Y"})
        )
    with pytest.raises(BackcastError, match="same hours"):
        build_backcast_inputs(NETWORK, zonal, GENERATION.iloc[:3], FLOWS)
    with pytest.raises(BackcastError, match="residual_cost"):
        build_backcast_inputs(NETWORK, zonal, GENERATION, FLOWS, residual_cost=0.0)


def test_backcast_signal_shows_the_congested_hour():
    result = run_backcast(NETWORK, _inputs(residual_cost=40.0), load_shedding_cost=1000.0)
    opf, signal = result.opf, result.signal

    # h0: all 100 MW of wind are used and no line limit costs anything.
    assert opf.dispatch_mw.loc[HOURS[0], "wind_onshore:N"] == pytest.approx(100.0)
    assert opf.shadow_prices.loc[HOURS[0]].abs().max() == pytest.approx(0.0)
    assert (signal.classification.loc[HOURS[0]] == NEUTRAL).all()
    # h1: N-S is full, 100 MW of wind are curtailed and S runs residual at 40.
    assert opf.flows_mw.loc[HOURS[1], "N-S"] == pytest.approx(100.0)
    assert opf.dispatch_mw.loc[HOURS[1], "wind_onshore:N"] == pytest.approx(150.0)
    assert opf.shadow_prices.loc[HOURS[1], "N-S"] == pytest.approx(40.0)
    # Feeding in north of the full line adds to the congestion, south of it relieves it.
    # The reference is demand-weighted (N 1/4, S 3/4), so the 40 split 30 : -10.
    assert signal.signal.loc[HOURS[1]].to_dict() == pytest.approx({"N": 30.0, "S": -10.0})
    assert signal.classification.loc[HOURS[1]].to_dict() == {"N": POSITIVE, "S": NEGATIVE}
    assert list(signal.signal.columns) == ["N", "S"]  # boundary zones carry no fee signal


def test_backcast_solves_a_subset_of_hours():
    result = run_backcast(NETWORK, _inputs(), hours=[HOURS[1]], load_shedding_cost=1000.0)

    assert result.signal.signal.index.tolist() == [HOURS[1]]


def _write_tables(directory: Path) -> None:
    directory.mkdir()
    tables = {
        "load_tso": TSO_LOAD,
        "wind_onshore_tso": TSO_RES["wind_onshore"],
        "wind_offshore_tso": pd.DataFrame(index=HOURS),
        "solar_tso": pd.DataFrame(index=HOURS),
        "generation_de": GENERATION,
        "physical_flows_tso": FLOWS,
    }
    for name, frame in tables.items():
        frame.to_csv(directory / smard.table_filename(name, 2023))


def _write_network(directory: Path) -> None:
    directory.mkdir()
    NETWORK.zones_frame().reset_index().to_csv(directory / "zones.csv", index=False)
    NETWORK.lines_frame().reset_index().to_csv(directory / "lines.csv", index=False)


def test_cli_backcast_runs_end_to_end(tmp_path):
    _write_tables(tmp_path / "raw")
    _write_network(tmp_path / "net")
    out = tmp_path / "out"

    result = CliRunner().invoke(
        app,
        ["backcast", "--year", "2023", "--network", str(tmp_path / "net"), "--data"]
        + [str(tmp_path / "raw"), "--residual-cost", "40", "--out", str(out)],
    )

    # The 30 MWh cut from the export to X are 27 % of the exchange with X: reported.
    assert result.exit_code == 1, result.output
    assert "Hours with congestion: 1 between German zones" in result.output
    assert "PLACEHOLDER zone shares were used for: demand, wind_onshore, generation" in (
        result.output
    )
    assert "validation failed for net export per neighbour (X)" in result.output
    signal = pd.read_csv(out / "fee_signal.csv", index_col=0)
    assert signal.iloc[1].round(6).tolist() == [30.0, -10.0]
    labels = pd.read_csv(out / "fee_signal_class.csv", index_col=0)
    assert labels.iloc[1].tolist() == [POSITIVE, NEGATIVE]
    report = (out / "validation.md").read_text(encoding="utf-8")
    assert "| wind north share | north = 50Hertz + TenneT | 100.0% | 100.0% |" in report
    assert "**FAIL**" in report and "ASSUMPTION" in report
    assert pd.read_csv(out / "clipped_exchange.csv")["clipped_mwh"].tolist() == [30.0]

    # validate and signal reproduce their parts from the written results.
    again = CliRunner().invoke(
        app,
        ["validate", "--results", str(out), "--year", "2023", "--network", str(tmp_path / "net")]
        + ["--data", str(tmp_path / "raw"), "--flow-tolerance", "0.5", "--out"]
        + [str(tmp_path / "report.md")],
    )
    assert again.exit_code == 0, again.output
    assert "2 of 2 checks passed" in again.output
    again = CliRunner().invoke(
        app, ["signal", "--results", str(out), "--threshold", "20", "--out", str(tmp_path / "s")]
    )
    assert again.exit_code == 0, again.output
    labels = pd.read_csv(tmp_path / "s" / "fee_signal_class.csv", index_col=0)
    # `signal` uses the PTDF as written, all zones included.
    assert labels.iloc[1][["N", "S"]].tolist() == [POSITIVE, NEUTRAL]


def test_cli_backcast_rejects_hours_without_utc_offset(tmp_path):
    _write_tables(tmp_path / "raw")
    _write_network(tmp_path / "net")
    (tmp_path / "hours.csv").write_text("hour\n2023-01-01 01:00:00\n")

    result = CliRunner().invoke(
        app,
        ["backcast", "--year", "2023", "--network", str(tmp_path / "net"), "--data"]
        + [str(tmp_path / "raw"), "--hours", str(tmp_path / "hours.csv")]
        + ["--out", str(tmp_path / "out")],
    )

    assert result.exit_code == 1
    assert "hours need a UTC offset" in result.output


def test_cli_validate_keeps_the_backcast_report_and_rejects_csv(tmp_path):
    _write_tables(tmp_path / "raw")
    _write_network(tmp_path / "net")
    out = tmp_path / "out"
    common = ["--year", "2023", "--network", str(tmp_path / "net"), "--data", str(tmp_path / "raw")]
    CliRunner().invoke(app, ["backcast", *common, "--out", str(out)])
    report = (out / "validation.md").read_text(encoding="utf-8")

    CliRunner().invoke(app, ["validate", "--results", str(out), *common])
    as_csv = CliRunner().invoke(
        app, ["validate", "--results", str(out), *common, "--out", str(tmp_path / "r.csv")]
    )

    assert (out / "validation.md").read_text(encoding="utf-8") == report
    assert (out / "revalidation.md").exists()
    assert as_csv.exit_code == 1
    assert "the report is Markdown" in as_csv.output
    assert not (tmp_path / "r.csv").exists()


def test_cli_backcast_reports_missing_data(tmp_path):
    result = CliRunner().invoke(
        app, ["backcast", "--year", "2023", "--network", str(DE22), "--data", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "run `gridfee collect --year 2023` first" in result.output


def test_shipped_network_loads_and_matches_the_smard_borders():
    network = load_network(DE22)

    assert len(network.internal_zone_ids) == 22
    smard_neighbours = {n for borders in smard.PHYSICAL_FLOW_FILTERS.values() for n in borders}
    assert set(network.boundary_zone_ids) == smard_neighbours
    lines = network.lines_frame()
    assert len(lines) == 53
    # Every line id is "<from>-<to>" and no pair of zones has two lines.
    assert (lines.index == lines["from_zone"] + "-" + lines["to_zone"]).all()
    header = (DE22 / "lines.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("# ASSUMPTION, not TSO data")


def test_shipped_zone_regions_cover_every_zone_once():
    regions = pd.read_csv(DE22 / "zone_nuts2.csv", comment="#")

    assert regions["nuts2"].is_unique
    assert len(regions) == 38
    assert sorted(regions["zone_id"].unique()) == load_network(DE22).internal_zone_ids


# Fuel model: lignite in the north, gas in the south, biomass half and half.
FUEL_GENERATION = pd.DataFrame(
    {
        "wind_onshore": [100.0, 250.0, 0.0, 100.0],
        "lignite": [60.0, 60.0, 120.0, 0.0],
        "gas": [20.0, 20.0, 60.0, 100.0],
        "biomass": [20.0, 20.0, 20.0, 20.0],
        "nuclear": [0.0] * 4,
    },
    index=HOURS,
)
FUEL_SHARES = pd.DataFrame(
    {"N": [1.0, 0.0, 0.5], "S": [0.0, 1.0, 0.5]}, index=["lignite", "gas", "biomass"]
)


def _fuel_inputs(**kwargs):
    zonal = build_historical_injections(NETWORK.zones[:2], TSO_LOAD, TSO_RES)
    return build_backcast_inputs(
        NETWORK, zonal, FUEL_GENERATION, FLOWS, generation_shares=FUEL_SHARES, **kwargs
    )


def test_fuels_with_a_cost_are_dispatchable_and_the_others_follow_their_output():
    inputs = _fuel_inputs(generation_costs={"lignite": 30.0, "gas": 90.0})

    generators = inputs.generators
    assert not inputs.placeholder_residual_shares
    assert "residual:N" not in generators.index
    assert generators.loc["lignite:N"].tolist() == ["N", 120.0, 30.0]
    assert generators.loc["gas:S"].tolist() == ["S", 100.0, 90.0]
    assert "lignite:S" not in generators.index and "nuclear:N" not in generators.index
    # Lignite last produced in h2, so it is not available in h3.
    assert inputs.availability["lignite:N"].tolist() == [1.0, 1.0, 1.0, 0.0]
    assert "gas:S" not in inputs.availability.columns
    # Biomass: observed output, half per zone, at no cost.
    assert generators.loc["biomass:N"].tolist() == ["N", 10.0, 0.0]
    assert inputs.availability["biomass:S"].tolist() == [1.0] * 4
    # PTDF reference: shares of all fuels by energy (lignite 240, gas 200, biomass 80 MWh).
    assert inputs.residual_shares["N"] == pytest.approx(280 / 520)


def test_without_costs_every_fuel_follows_its_observed_output():
    inputs = _fuel_inputs()

    assert inputs.generators.loc["lignite:N"].tolist() == ["N", 120.0, 0.0]
    assert inputs.availability["lignite:N"].tolist() == [0.5, 0.5, 1.0, 0.0]


def test_fuel_costs_put_a_price_on_the_full_line():
    """h0: S needs 140 MW; wind and lignite in N fill the 100 MW line, gas covers the rest."""
    costs = {"lignite": 30.0, "gas": 90.0}
    result = run_backcast(NETWORK, _fuel_inputs(generation_costs=costs), HOURS[[0]], 1000.0)

    opf = result.opf
    assert opf.flows_mw.loc[HOURS[0], "N-S"] == pytest.approx(100.0)
    assert abs(opf.shadow_prices.loc[HOURS[0], "N-S"]) == pytest.approx(60.0)
    assert result.signal.classification.loc[HOURS[0]].to_dict() == {"N": POSITIVE, "S": NEGATIVE}
    # The validation compares the dispatch per fuel with the observed output.
    checks = check_residual_generation(opf.dispatch_mw, FUEL_GENERATION).set_index("check")
    assert checks.loc["lignite generated", ["observed", "modelled"]].tolist() == pytest.approx(
        [60e-6, 40e-6]  # TWh
    )
    assert checks.loc["generation other than wind and solar", "modelled"] == pytest.approx(100e-6)


def test_fuel_shares_and_costs_are_checked():
    with pytest.raises(BackcastError, match="lack the fuels"):
        _fuel_inputs_with(FUEL_SHARES.drop(index="biomass"))
    with pytest.raises(BackcastError, match="sum to 1"):
        _fuel_inputs_with(FUEL_SHARES * 0.5)
    with pytest.raises(BackcastError, match="every internal zone"):
        _fuel_inputs_with(FUEL_SHARES[["N"]])
    with pytest.raises(BackcastError, match="without shares"):
        _fuel_inputs(generation_costs={"oil": 1.0})
    with pytest.raises(BackcastError, match=">= 0"):
        _fuel_inputs(generation_costs={"gas": -1.0})
    with pytest.raises(BackcastError, match="need generation_shares"):
        _inputs(generation_costs={"gas": 1.0})


def _fuel_inputs_with(shares):
    zonal = build_historical_injections(NETWORK.zones[:2], TSO_LOAD, TSO_RES)
    return build_backcast_inputs(NETWORK, zonal, FUEL_GENERATION, FLOWS, generation_shares=shares)


def test_cost_file_gives_cost_per_mwh_of_electricity(tmp_path):
    path = tmp_path / "costs.csv"
    path.write_text(
        "# note\ntechnology,fuel_eur_per_mwh_th,efficiency,co2_t_per_mwh_th,vom_eur_per_mwh,"
        "co2_price_eur_per_t\ngas,40,0.5,0.2,4,80\n",
        encoding="utf-8",
    )

    assert read_generation_costs(path).to_dict() == {"gas": pytest.approx((40 + 16) / 0.5 + 4)}

    path.write_text(path.read_text(encoding="utf-8") + "gas,1,1,1,1,1\n", encoding="utf-8")
    with pytest.raises(BackcastError, match="more than once"):
        read_generation_costs(path)
    path.write_text("technology,efficiency\ngas,0.5\n", encoding="utf-8")
    with pytest.raises(BackcastError, match="missing columns"):
        read_generation_costs(path)


def test_shares_file_is_read_per_fuel(tmp_path):
    path = tmp_path / "shares.csv"
    path.write_text("# note\ntechnology,zone_id,share\ngas,N,0.25\ngas,S,0.75\nlignite,N,1\n")

    shares = read_generation_shares(path)

    assert shares.loc["gas"].to_dict() == {"N": 0.25, "S": 0.75}
    assert shares.loc["lignite", "S"] == 0.0


def test_shipped_cost_file_orders_the_fuels():
    costs = read_generation_costs(DE22.parents[1] / "costs" / "generation_costs_2023.csv")

    assert list(costs.sort_values().index) == ["nuclear", "gas", "lignite", "hard_coal"]
