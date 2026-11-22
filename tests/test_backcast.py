"""Backcast on a small network with hand-checkable numbers, and the shipped network."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from gridfee.backcast import (
    BackcastError,
    PlantCosts,
    build_backcast_inputs,
    neighbour_net_export,
    read_cost_parameters,
    read_fuel_prices,
    read_generation_costs,
    read_generation_shares,
    read_plant_efficiency,
    run_backcast,
    split_borders_by_tso,
)
from gridfee.cli import app
from gridfee.data import smard
from gridfee.injections import build_historical_injections
from gridfee.network import Line, Zone, ZoneNetwork, load_network
from gridfee.signal import NEGATIVE, NEUTRAL, POSITIVE
from gridfee.validation.checks import check_cross_border, check_residual_generation

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


def test_backcast_in_blocks_of_hours_gives_the_same_signal():
    one = run_backcast(NETWORK, _inputs(residual_cost=40.0), load_shedding_cost=1000.0)
    blocks = run_backcast(
        NETWORK, _inputs(residual_cost=40.0), load_shedding_cost=1000.0, block_hours=1
    )

    pd.testing.assert_frame_equal(blocks.signal.signal, one.signal.signal, atol=1e-9)
    pd.testing.assert_frame_equal(blocks.signal.classification, one.signal.classification)
    pd.testing.assert_frame_equal(blocks.opf.shadow_prices, one.opf.shadow_prices, atol=1e-9)


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

    # In blocks of hours, in two processes: the same files.
    blocks = CliRunner().invoke(
        app,
        ["backcast", "--year", "2023", "--network", str(tmp_path / "net"), "--data"]
        + [str(tmp_path / "raw"), "--residual-cost", "40", "--out", str(tmp_path / "blocks")]
        + ["--block-hours", "1", "--workers", "2"],
    )
    assert blocks.exit_code == 1, blocks.output
    for name in ("fee_signal.csv", "fee_signal_class.csv"):
        pd.testing.assert_frame_equal(
            pd.read_csv(tmp_path / "blocks" / name, index_col=0),
            pd.read_csv(out / name, index_col=0),
        )

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


# Plant classes: lignite in N in two classes, gas in S in one. Cost = 30 / efficiency.
PLANT_PARAMETERS = pd.DataFrame(
    {
        "fuel_eur_per_mwh_th": 30.0,
        "efficiency": 0.5,
        "co2_t_per_mwh_th": 0.0,
        "vom_eur_per_mwh": 0.0,
        "co2_price_eur_per_t": 0.0,
    },
    index=["lignite", "gas"],
)
PLANT_EFFICIENCY = pd.DataFrame(
    {
        "technology": ["lignite", "lignite", "gas"],
        "zone_id": ["N", "N", "S"],
        "efficiency": [0.3, 0.5, 0.6],
        "share": [0.25, 0.75, 1.0],
    }
)


def test_plant_classes_are_bounded_by_the_observed_output_of_the_day():
    """All four hours are one day: lignite 0 to 120 MW, gas 20 to 100 MW."""
    inputs = _fuel_inputs(plant_costs=PlantCosts(PLANT_PARAMETERS, PLANT_EFFICIENCY))
    generators = inputs.generators

    # The day's lowest gas output runs at no cost, the rest by efficiency class.
    assert generators.loc["gas:S@must_run"].tolist() == ["S", 100.0, 0.0]
    assert inputs.availability["gas:S@must_run"].tolist() == pytest.approx([0.2] * 4)
    assert generators.loc["gas:S@0.60"].tolist() == ["S", 100.0, pytest.approx(50.0)]
    assert inputs.availability["gas:S@0.60"].tolist() == pytest.approx([0.8] * 4)
    assert "lignite:N@must_run" not in generators.index
    assert generators.loc["lignite:N@0.30"].tolist() == ["N", 30.0, pytest.approx(100.0)]
    assert generators.loc["lignite:N@0.50"].tolist() == ["N", 90.0, pytest.approx(60.0)]
    assert inputs.marginal_cost is None
    # Biomass has no cost row and feeds in its observed output.
    assert generators.loc["biomass:S"].tolist() == ["S", 10.0, 0.0]


def test_plant_classes_close_the_hourly_balance_of_the_observations():
    """Load 200 MW. Observed supply less exports: 200, 350, 230 and 170 MW (h3 cut to 50)."""
    inputs = _fuel_inputs(plant_costs=PlantCosts(PLANT_PARAMETERS, PLANT_EFFICIENCY))

    # h1 and h2: 150 and 30 MW more supply than load become demand, 1/4 in N.
    assert inputs.load_mw["N"].tolist() == pytest.approx([50.0, 87.5, 57.5, 50.0])
    # h3: 30 MW more load than supply become a supply, 3/4 in S.
    assert inputs.generators.loc["balance:S"].tolist() == ["S", 22.5, 0.0]
    assert inputs.availability["balance:S"].tolist() == pytest.approx([0.0, 0.0, 0.0, 1.0])

    # With it h3 can be served: S has 200 MW of load and export and 232.5 MW of supply.
    result = run_backcast(NETWORK, inputs, HOURS[[3]], load_shedding_cost=1000.0)
    assert result.opf.load_shedding_mw.to_numpy().sum() == pytest.approx(0.0, abs=1e-6)
    checks = check_residual_generation(result.opf.dispatch_mw, FUEL_GENERATION).set_index("check")
    assert checks.loc["gas generated", "modelled"] > 0


def test_monthly_fuel_prices_change_the_cost_of_a_class():
    prices = pd.DataFrame({"gas": [60.0]}, index=pd.Index([1], name="month"))
    weekly = PlantCosts(PLANT_PARAMETERS, PLANT_EFFICIENCY, prices, window="week")
    inputs = _fuel_inputs(plant_costs=weekly)

    assert inputs.generators.loc["gas:S@0.60", "marginal_cost_eur_per_mwh"] == pytest.approx(100.0)
    with pytest.raises(BackcastError, match="lack months"):
        _fuel_inputs(
            plant_costs=PlantCosts(PLANT_PARAMETERS, PLANT_EFFICIENCY, prices.rename({1: 2}))
        )


def test_plant_costs_are_checked():
    plants = PlantCosts(PLANT_PARAMETERS, PLANT_EFFICIENCY)
    with pytest.raises(BackcastError, match="not both"):
        _fuel_inputs(plant_costs=plants, generation_costs={"gas": 1.0})
    with pytest.raises(BackcastError, match="window"):
        PlantCosts(PLANT_PARAMETERS, PLANT_EFFICIENCY, window="month")
    with pytest.raises(BackcastError, match="lacks lignite in zone N"):
        _fuel_inputs(
            plant_costs=PlantCosts(
                PLANT_PARAMETERS, PLANT_EFFICIENCY.assign(zone_id=["S", "S", "S"])
            )
        )


def test_plant_files_are_read_and_checked(tmp_path):
    path = tmp_path / "classes.csv"
    path.write_text("# note\ntechnology,zone_id,efficiency,share\ngas,S,0.4,0.5\ngas,S,0.6,0.5\n")
    assert read_plant_efficiency(path)["efficiency"].tolist() == [0.4, 0.6]
    path.write_text("technology,zone_id,efficiency,share\ngas,S,0.4,0.5\n")
    with pytest.raises(BackcastError, match="sum to 1"):
        read_plant_efficiency(path)
    path.write_text("technology,zone_id,efficiency,share\ngas,S,0.401,0.5\ngas,S,0.404,0.5\n")
    with pytest.raises(BackcastError, match="more than once"):
        read_plant_efficiency(path)
    path.write_text("technology,zone_id,efficiency,share\ngas,S,0.435,0.5\ngas,S,0.444,0.5\n")
    assert len(read_plant_efficiency(path)) == 2  # ids @0.43 and @0.44

    path.write_text("month,technology,fuel_eur_per_mwh_th\n1,gas,60\n2,gas,50\n")
    assert read_fuel_prices(path)["gas"].to_dict() == {1: 60.0, 2: 50.0}
    path.write_text("month,technology,fuel_eur_per_mwh_th\n13,gas,60\n")
    with pytest.raises(BackcastError, match="1 to 12"):
        read_fuel_prices(path)


def test_shipped_plant_files_cover_the_fuels_with_classes():
    data = DE22.parents[1]
    classes = read_plant_efficiency(data / "shares" / "plant_efficiency.csv")
    shares = read_generation_shares(data / "shares" / "generation_shares.csv")
    parameters = read_cost_parameters(data / "costs" / "generation_costs_2023.csv")

    assert set(classes["technology"]) == {"lignite", "hard_coal", "gas"} <= set(parameters.index)
    for fuel, of_fuel in classes.groupby("technology"):
        assert set(shares.columns[shares.loc[fuel] > 0]) <= set(of_fuel["zone_id"])
    assert read_fuel_prices(data / "costs" / "fuel_prices_2023.csv")["gas"].notna().sum() == 12


# X is reached from N (TenneT) and from S (Amprion).
TWO_BORDERS = ZoneNetwork(
    zones=NETWORK.zones,
    lines=(
        *NETWORK.lines,
        Line(line_id="N-X", from_zone="N", to_zone="X", susceptance_mw_per_rad=100, capacity_mw=50),
    ),
)
TWO_FLOWS = pd.DataFrame(
    {"Amprion_X": [0.0, 0.0, -30.0, 20.0], "TenneT_X": [10.0, 0.0, 0.0, -40.0]}, index=HOURS
)


def test_borders_by_tso_fix_the_observed_flow_of_each_border():
    network = split_borders_by_tso(TWO_BORDERS, TWO_FLOWS)

    assert network.boundary_zone_ids == ["X/Amprion", "X/TenneT"]
    ends = {line.line_id: (line.from_zone, line.to_zone) for line in network.lines}
    assert ends == {"N-S": ("N", "S"), "S-X": ("S", "X/Amprion"), "N-X": ("N", "X/TenneT")}
    assert split_borders_by_tso(NETWORK, FLOWS) is NETWORK  # one TSO zone at the border
    # a flow from a TSO zone without a line to X: X stays one zone
    assert split_borders_by_tso(TWO_BORDERS, TWO_FLOWS.assign(TransnetBW_X=1.0)) is TWO_BORDERS

    zonal = build_historical_injections(network.zones[:2], TSO_LOAD, TSO_RES)
    inputs = build_backcast_inputs(network, zonal, GENERATION, TWO_FLOWS)
    assert inputs.load_mw["X/Amprion"].tolist() == [0.0, 0.0, 0.0, 20.0]
    assert inputs.load_mw["X/TenneT"].tolist() == [10.0, 0.0, 0.0, 0.0]
    assert inputs.availability["import:X/TenneT"].tolist() == [0.0, 0.0, 0.0, 1.0]

    # h3: 40 MW come in from X at N while 20 MW leave to X at S, as observed. With one
    # zone for X the model would only see the net import of 20 MW.
    result = run_backcast(network, inputs, HOURS[[3]], load_shedding_cost=1000.0)
    flows = result.opf.flows_mw.iloc[0]
    assert flows["N-X"] == pytest.approx(-40.0)
    assert flows["S-X"] == pytest.approx(20.0)

    checks = check_cross_border(network, result.opf.flows_mw, TWO_FLOWS.loc[HOURS[[3]]])
    per_neighbour = checks[checks["check"] == "net export per neighbour"]
    assert per_neighbour["item"].tolist() == ["X"]
    assert per_neighbour["modelled"].iloc[0] == pytest.approx(-20.0 / 1e6)


def test_borders_by_tso_need_a_line_for_every_observed_border():
    network = split_borders_by_tso(TWO_BORDERS, TWO_FLOWS)
    zonal = build_historical_injections(network.zones[:2], TSO_LOAD, TSO_RES)
    flows = TWO_FLOWS.assign(TransnetBW_X=1.0)

    with pytest.raises(BackcastError, match="TransnetBW_X"):
        build_backcast_inputs(network, zonal, GENERATION, flows)


def test_hours_with_unserved_load_are_not_part_of_the_signal():
    off = {"residual:N": 0.0, "residual:S": 0.0}
    scarce = replace(_inputs(), availability=_inputs().availability.assign(**off))
    result = run_backcast(NETWORK, scarce, load_shedding_cost=1000.0)

    unserved = result.opf.load_shedding_mw.sum(axis=1) > 1e-3
    assert unserved.any()
    assert result.signal.signal.index.equals(unserved.index[~unserved])
    assert result.opf.shadow_prices.index.equals(unserved.index)


def test_cli_backcast_also_writes_the_signal_for_reporting_zones(tmp_path):
    _write_tables(tmp_path / "raw")
    _write_network(tmp_path / "net")
    (tmp_path / "zones.csv").write_text("zone_id,nuts2\nZ,N\nZ,S\n")
    (tmp_path / "weights.csv").write_text("zone_id,population\nN,100\nS,300\n")
    common = ["backcast", "--year", "2023", "--network", str(tmp_path / "net"), "--data"]
    common += [str(tmp_path / "raw"), "--residual-cost", "40", "--out", str(tmp_path / "out")]

    CliRunner().invoke(
        app,
        [*common, "--report-zones", str(tmp_path / "zones.csv")]
        + ["--report-weights", str(tmp_path / "weights.csv")],
    )

    report = pd.read_csv(tmp_path / "out" / "fee_signal_report.csv", index_col=0)
    assert list(report.columns) == ["Z"]
    # (100 * 30 + 300 * -10) / 400
    assert report.iloc[1].round(6).tolist() == [0.0]
    labels = pd.read_csv(tmp_path / "out" / "fee_signal_report_class.csv", index_col=0)
    assert labels.iloc[1].tolist() == ["neutral"]
    assert (tmp_path / "out" / "fee_signal_report_summary.csv").exists()

    alone = CliRunner().invoke(app, [*common, "--report-zones", str(tmp_path / "zones.csv")])
    assert "--report-zones needs --report-weights" in alone.output
    (tmp_path / "zones.csv").write_text("zone_id,nuts2\nZ,N\n")
    partial = CliRunner().invoke(
        app,
        [*common, "--report-zones", str(tmp_path / "zones.csv")]
        + ["--report-weights", str(tmp_path / "weights.csv")],
    )
    assert "does not list the zones ['S']" in partial.output
