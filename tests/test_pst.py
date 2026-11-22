"""Controllable phase shifts on a hand-checkable equal-reactance triangle."""

import numpy as np
import pandas as pd
import pytest

from gridfee.backcast import BackcastInputs, run_backcast
from gridfee.cli import _write_opf
from gridfee.flows import run_dc_opf
from gridfee.flows.opf import PST_MARGINAL_COST
from gridfee.network import Line, NetworkConfigError, Zone, ZoneNetwork, load_network
from gridfee.two_run import run_two_run_backcast

from .conftest import make_generators


def triangle(shift=None):
    return ZoneNetwork(
        zones=tuple(Zone(zone_id=z, name=z, tso_zone="TenneT") for z in "ABC"),
        lines=(
            Line(
                line_id="AB",
                from_zone="A",
                to_zone="B",
                susceptance_mw_per_rad=1000,
                capacity_mw=20,
                max_shift_deg=shift,
            ),
            Line(
                line_id="BC",
                from_zone="B",
                to_zone="C",
                susceptance_mw_per_rad=1000,
                capacity_mw=100,
            ),
            Line(
                line_id="AC",
                from_zone="A",
                to_zone="C",
                susceptance_mw_per_rad=1000,
                capacity_mw=100,
            ),
        ),
    )


def inputs(reverse=False):
    source, sink = ("C", "A") if reverse else ("A", "C")
    index = pd.date_range("2023-01-01", periods=2, freq="h")
    generators = make_generators({"cheap": (source, 100, 10), "dear": (sink, 100, 50)})
    return BackcastInputs(
        load_mw=pd.DataFrame({sink: [90.0, 0.0]}, index=index),
        generators=generators,
        availability=pd.DataFrame(index=index),
        residual_shares=pd.Series(dtype=float),
        placeholder_residual_shares=False,
        skipped_hours=index[:0],
        clipped_exchange=pd.DataFrame(),
        ptdf_reference={sink: 1.0},
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_pst_relaxes_loop_congestion_with_minimal_shift(reverse, tmp_path):
    data = inputs(reverse)
    plain = run_backcast(triangle(), data).opf
    shifted = run_backcast(triangle(3), data).opf
    sign = -1 if reverse else 1
    assert plain.dispatch_mw["cheap"].iloc[0] == pytest.approx(60)
    assert shifted.dispatch_mw["cheap"].iloc[0] == pytest.approx(90)
    assert shifted.flows_mw["AB"].iloc[0] == pytest.approx(sign * 20)
    # Equal reactances give f_AB = (injection_A + link_p) / 3.
    assert shifted.pst_shift_deg["AB"].iloc[0] == pytest.approx(-sign * np.rad2deg(0.03))
    assert shifted.pst_shift_deg.abs().max().max() <= 3
    assert shifted.pst_shift_deg["AB"].iloc[1] == pytest.approx(0)
    assert shifted.objective_eur == pytest.approx(900 + 30 * PST_MARGINAL_COST)
    assert shifted.objective_eur < plain.objective_eur
    assert list(shifted.flows_mw) == ["AB", "BC", "AC"]
    _write_opf(shifted, tmp_path)
    saved = pd.read_csv(tmp_path / "pst_shift_deg.csv", index_col=0)
    np.testing.assert_allclose(saved, shifted.pst_shift_deg)
    _write_opf(plain, tmp_path)
    assert not (tmp_path / "pst_shift_deg.csv").exists()
    assert plain.pst_shift_deg is None


def test_small_range_binds_and_combined_limit_has_a_shadow_price():
    data = inputs()
    shift = np.rad2deg(0.015)
    result = run_backcast(triangle(shift), data).opf
    assert result.pst_shift_deg["AB"].iloc[0] == pytest.approx(-shift)
    assert result.dispatch_mw["cheap"].iloc[0] == pytest.approx(75)
    assert result.flows_mw["AB"].iloc[0] == pytest.approx(20)
    assert result.shadow_prices["AB"].iloc[0] == pytest.approx(120)
    assert result.congested()["AB"].iloc[0]


def test_two_run_grid_uses_pst_and_preserves_shift_results():
    data = inputs()
    plain = run_two_run_backcast(triangle(), data, chunk_hours=1)
    shifted = run_two_run_backcast(triangle(3), data, chunk_hours=1)
    assert shifted.hourly()["raised_mwh"].sum() < plain.hourly()["raised_mwh"].sum()
    assert shifted.hourly()["raised_mwh"].sum() == pytest.approx(0)
    assert shifted.opf.pst_shift_deg["AB"].tolist() == pytest.approx([-np.rad2deg(0.03), 0])


def test_hook_and_blocks_preserve_shifts():
    data = inputs()
    calls = []

    def hook(n, snapshots):
        calls.append(list(snapshots))
        n.model.add_constraints(n.model["Link-p"] >= -15, name="test-shift-bound")

    result = run_dc_opf(
        triangle(3), data.load_mw, data.generators, block_hours=1, extra_functionality=hook
    )
    assert calls == [[0], [1]]
    assert result.pst_shift_deg["AB"].tolist() == pytest.approx([-np.rad2deg(0.015), 0])


@pytest.mark.parametrize("value", [None, "", "0", "3", "-1", "nan", "inf"])
def test_loader_shift_column(tmp_path, value):
    network = triangle()
    network.zones_frame().to_csv(tmp_path / "zones.csv")
    lines = network.lines_frame().drop(columns="max_shift_deg")
    if value is not None:
        lines["max_shift_deg"] = value
    lines.to_csv(tmp_path / "lines.csv")
    if value in ("-1", "nan", "inf"):
        with pytest.raises(NetworkConfigError, match="max_shift_deg"):
            load_network(tmp_path)
    else:
        loaded = load_network(tmp_path)
        assert loaded.lines[0].max_shift_deg == (float(value) if value else None)


def test_zero_shift_is_a_plain_line():
    data = inputs()
    plain = run_backcast(triangle(), data).opf
    zero = run_backcast(triangle(0), data).opf
    pd.testing.assert_frame_equal(plain.flows_mw, zero.flows_mw)
    pd.testing.assert_frame_equal(plain.shadow_prices, zero.shadow_prices)
    assert zero.pst_shift_deg is None
    assert zero.objective_eur == plain.objective_eur
