"""Two-run method on the small network of test_backcast, with hand-checkable numbers."""

from __future__ import annotations

import pandas as pd
import pytest
from typer.testing import CliRunner

from gridfee.backcast import BackcastError, run_backcast
from gridfee.cli import app
from gridfee.signal import NEGATIVE, NEUTRAL, POSITIVE
from gridfee.two_run import RedispatchCosts, run_two_run_backcast

from .test_backcast import HOURS, NETWORK, _fuel_inputs, _inputs, _write_network, _write_tables


def _two_run(**kwargs):
    return run_two_run_backcast(NETWORK, _inputs(residual_cost=40.0), None, 1000.0, **kwargs)


def test_market_run_ignores_the_line_and_the_grid_run_moves_what_overloads_it():
    """h1: 250 MW of wind in N for 200 MW of load; the 100 MW line takes 100 of N's surplus."""
    result = _two_run()

    # Market: wind covers all load at a price of 0, 150 MW of it would flow over N-S.
    assert result.market_dispatch_mw.loc[HOURS[1], "wind_onshore:N"] == pytest.approx(200.0)
    assert result.market_price.round(6).tolist() == [40.0, 0.0, 40.0, 40.0]
    # Grid: 50 MW of wind down, 50 MW in S up; nothing moves in the other hours.
    moved = result.redispatch_mw.loc[HOURS[1]]
    assert moved["wind_onshore:N"] == pytest.approx(-50.0)
    assert moved["residual:S"] == pytest.approx(50.0)
    assert result.redispatch_mw.drop(index=HOURS[1]).abs().to_numpy().max() == 0.0
    assert result.opf.flows_mw["N-S"].abs().max() <= 100.0 + 1e-6
    assert result.opf.dispatch_mw.loc[HOURS[1], "wind_onshore:N"] == pytest.approx(150.0)
    hourly = result.hourly()
    assert hourly.loc[HOURS[1]].tolist() == pytest.approx([50.0, 50.0, 50 * 41.0 + 50 * 1.0])
    raised, lowered = result.by_zone()
    assert raised.loc[HOURS[1], "S"] == pytest.approx(50.0)
    assert lowered.loc[HOURS[1], "N"] == pytest.approx(50.0)
    assert raised.sum().sum() == pytest.approx(50.0)


def test_fee_signal_comes_from_the_grid_run():
    """The line is worth what one MW more of it saves: 41 up in S and 1 down in N."""
    result = _two_run()

    assert abs(result.opf.shadow_prices.loc[HOURS[1], "N-S"]) == pytest.approx(42.0)
    assert result.signal.signal.loc[HOURS[1]].tolist() == pytest.approx([31.5, -10.5])
    labels = result.signal.classification
    assert labels.loc[HOURS[1]].to_dict() == {"N": POSITIVE, "S": NEGATIVE}
    assert (labels.drop(index=HOURS[1]) == NEUTRAL).all().all()


def test_curtailment_cost_and_markup_set_the_cost_of_moving():
    result = _two_run(costs=RedispatchCosts(markup=2.0, curtailment_cost=70.0))

    assert result.redispatch_mw.loc[HOURS[1], "wind_onshore:N"] == pytest.approx(-50.0)
    assert result.redispatch_cost_eur[HOURS[1]] == pytest.approx(50 * 42.0 + 50 * 72.0)
    assert abs(result.opf.shadow_prices.loc[HOURS[1], "N-S"]) == pytest.approx(114.0)
    with pytest.raises(BackcastError, match="markup"):
        RedispatchCosts(markup=0.0)
    with pytest.raises(BackcastError, match="curtailment"):
        RedispatchCosts(curtailment_cost=-1.0)


def test_cheaper_plant_behind_the_line_is_lowered_for_a_dearer_one():
    """h0 of test_fuel_costs_put_a_price_on_the_full_line: lignite (30) in N, gas (90) in S."""
    inputs = _fuel_inputs(generation_costs={"lignite": 30.0, "gas": 90.0})
    result = run_two_run_backcast(NETWORK, inputs, HOURS[[0]], 1000.0)
    single = run_backcast(NETWORK, inputs, HOURS[[0]], 1000.0)

    moved = result.redispatch_mw.loc[HOURS[0]]
    assert set(moved[moved < 0].index.str.split(":").str[0]) == {"lignite"}
    assert set(moved[moved > 0].index.str.split(":").str[0]) == {"gas"}
    assert moved.sum() == pytest.approx(0.0, abs=1e-6)
    # The grid run ends at the single run's dispatch; its line price holds two markups more.
    after = result.opf.dispatch_mw.loc[HOURS[0]]
    assert after.tolist() == pytest.approx(single.opf.dispatch_mw.loc[HOURS[0], after.index])
    assert abs(result.opf.shadow_prices.loc[HOURS[0], "N-S"]) == pytest.approx(62.0)
    volume = result.hourly().loc[HOURS[0]]
    assert volume["cost_eur"] == pytest.approx(volume["raised_mwh"] * 62.0)


def test_results_do_not_depend_on_the_chunks_or_a_subset_of_hours():
    whole = _two_run()
    chunked = _two_run(chunk_hours=3)
    subset = run_two_run_backcast(NETWORK, _inputs(residual_cost=40.0), HOURS[[1]], 1000.0)

    pd.testing.assert_frame_equal(whole.redispatch_mw, chunked.redispatch_mw)
    pd.testing.assert_frame_equal(whole.signal.signal, chunked.signal.signal, check_freq=False)
    pd.testing.assert_frame_equal(whole.redispatch_mw.loc[HOURS[[1]]], subset.redispatch_mw)


def test_cli_backcast_two_run_writes_the_redispatch_and_leaves_the_single_run_as_it_is(tmp_path):
    _write_tables(tmp_path / "raw")
    _write_network(tmp_path / "net")
    base = ["backcast", "--year", "2023", "--network", str(tmp_path / "net"), "--data"]
    base += [str(tmp_path / "raw"), "--residual-cost", "40", "--out"]

    single = CliRunner().invoke(app, base + [str(tmp_path / "single")])
    CliRunner().invoke(app, base + [str(tmp_path / "chosen"), "--method", "single"])
    two = CliRunner().invoke(app, base + [str(tmp_path / "two"), "--method", "two-run"])

    assert "validation failed" in two.output and "Two-run method" in two.output
    assert "0.1 GWh raised and 0.1 GWh lowered in 1 hours" in two.output
    signal = pd.read_csv(tmp_path / "two" / "fee_signal.csv", index_col=0)
    assert signal.iloc[1].round(6).tolist() == [31.5, -10.5]
    moved = pd.read_csv(tmp_path / "two" / "redispatch_mw.csv", index_col=0)
    assert moved.iloc[1].round(6).to_dict() == {
        "wind_onshore:N": -50.0,
        "import:X": 0.0,
        "residual:N": 0.0,
        "residual:S": 50.0,
    }
    hourly = pd.read_csv(tmp_path / "two" / "redispatch_hourly.csv", index_col=0)
    assert hourly["raised_mwh"].round(6).tolist() == [0.0, 50.0, 0.0, 0.0]
    lowered = pd.read_csv(tmp_path / "two" / "redispatch_lowered_by_zone_mw.csv", index_col=0)
    assert lowered["N"].round(6).tolist() == [0.0, 50.0, 0.0, 0.0]

    # Without the option, and with --method single, the files are those of the single run.
    assert "Two-run" not in single.output
    names = sorted(path.name for path in (tmp_path / "single").iterdir())
    assert not [name for name in names if "redispatch" in name or "market" in name]
    assert names == sorted(path.name for path in (tmp_path / "chosen").iterdir())
    for name in names:
        left = (tmp_path / "single" / name).read_text(encoding="utf-8")
        assert left == (tmp_path / "chosen" / name).read_text(encoding="utf-8"), name
    signal = pd.read_csv(tmp_path / "single" / "fee_signal.csv", index_col=0)
    assert signal.iloc[1].round(6).tolist() == [30.0, -10.0]

    # A single run into the directory of a two-run leaves none of its redispatch files.
    CliRunner().invoke(app, base + [str(tmp_path / "two")])
    assert names == sorted(path.name for path in (tmp_path / "two").iterdir())

    bad = CliRunner().invoke(app, base + [str(tmp_path / "bad"), "--method", "three"])
    assert bad.exit_code == 1 and "--method must be one of single, two-run" in bad.output
