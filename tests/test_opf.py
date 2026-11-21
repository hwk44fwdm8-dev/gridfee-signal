"""DC OPF on synthetic networks whose answers are known by hand."""

import numpy as np
import pandas as pd
import pytest

from gridfee.flows import OPFError, run_dc_opf
from gridfee.network import Line, ZoneNetwork

from .conftest import make_generators

TWO_ZONE_GENERATORS = make_generators({"cheap_N": ("N", 500, 20), "costly_S": ("S", 500, 50)})
THREE_ZONE_GENERATORS = make_generators({"cheap_A": ("A", 500, 10), "costly_C": ("C", 500, 50)})


def assert_frame(frame: pd.DataFrame, expected: dict[str, list[float]]) -> None:
    assert list(frame.columns) == list(expected)
    np.testing.assert_allclose(frame.to_numpy(), np.array(list(expected.values())).T, atol=1e-6)


def test_two_zone_shadow_price_equals_price_difference(two_zone, hours):
    # Line N -> S carries at most 60 MW. Hours 0 and 1 need more than that in S, so
    # the costly generator in S sets the price there; hours 2 and 3 are uncongested.
    load = pd.DataFrame({"S": [100.0, 250.0, 60.0, 30.0]}, index=hours)
    result = run_dc_opf(two_zone, load, TWO_ZONE_GENERATORS)

    assert_frame(result.flows_mw, {"N-S": [60, 60, 60, 30]})
    assert_frame(result.dispatch_mw, {"cheap_N": [60, 60, 60, 30], "costly_S": [40, 190, 0, 0]})
    assert_frame(result.nodal_prices[["N"]], {"N": [20, 20, 20, 20]})
    assert_frame(result.nodal_prices.iloc[[0, 1, 3]][["S"]], {"S": [50, 50, 20]})
    assert_frame(result.shadow_prices.iloc[[0, 1, 3]], {"N-S": [30, 30, 0]})
    # Hour 2 is degenerate (flow exactly at the limit, no redispatch needed), so the
    # dual is not unique, but it always equals the price difference.
    spread = result.nodal_prices["S"] - result.nodal_prices["N"]
    np.testing.assert_allclose(result.shadow_prices["N-S"], spread, atol=1e-6)

    assert result.congested()["N-S"].iloc[[0, 1, 3]].tolist() == [True, True, False]
    assert result.objective_eur == pytest.approx(60 * 20 * 3 + 30 * 20 + (40 + 190) * 50)
    assert result.load_shedding_mw is None
    assert result.flows_mw.index.equals(hours)
    assert result.flows_mw.index.name == "snapshot"


def test_reverse_congestion_gives_negative_shadow_price(two_zone, hours):
    # Same case mirrored: the load sits in N, so the line is congested S -> N,
    # against its from -> to direction.
    generators = make_generators({"costly_N": ("N", 500, 50), "cheap_S": ("S", 500, 20)})
    load = pd.DataFrame({"N": [100.0]}, index=hours[:1])
    result = run_dc_opf(two_zone, load, generators)

    assert_frame(result.flows_mw, {"N-S": [-60]})
    assert_frame(result.shadow_prices, {"N-S": [-30]})
    assert_frame(result.nodal_prices, {"N": [50], "S": [20]})


def test_three_zone_loop_flow(three_zone, hours):
    # Cheap generation in A serves load in C. With equal susceptances 2/3 of it flows
    # over A-C (50 MW limit) and 1/3 via B, so A can deliver at most 75 MW.
    # One more MW of A-C capacity lets 1.5 MW of cheap energy replace costly energy:
    # shadow price = 1.5 * (50 - 10) = 60. B sits halfway on the detour: price 30.
    load = pd.DataFrame({"C": [100.0, 200.0, 20.0]}, index=hours[:3])
    result = run_dc_opf(three_zone, load, THREE_ZONE_GENERATORS)

    assert_frame(
        result.flows_mw,
        {"A-B": [25, 25, 20 / 3], "B-C": [25, 25, 20 / 3], "A-C": [50, 50, 40 / 3], "C-X": [0] * 3},
    )
    assert_frame(result.dispatch_mw, {"cheap_A": [75, 75, 20], "costly_C": [25, 125, 0]})
    assert_frame(
        result.shadow_prices, {"A-B": [0] * 3, "B-C": [0] * 3, "A-C": [60, 60, 0], "C-X": [0] * 3}
    )
    assert_frame(
        result.nodal_prices,
        {"A": [10, 10, 10], "B": [30, 30, 10], "C": [50, 50, 10], "X": [50, 50, 10]},
    )


@pytest.mark.parametrize("reference", ["A", "C", {"A": 1, "B": 2, "C": 3}])
def test_nodal_prices_decompose_into_ptdf_and_shadow_prices(three_zone, hours, reference):
    load = pd.DataFrame({"B": [40.0, 10.0], "C": [100.0, 20.0], "X": [30.0, 0.0]}, index=hours[:2])
    result = run_dc_opf(three_zone, load, THREE_ZONE_GENERATORS, ptdf_reference=reference)
    assert result.congested().iloc[0].any()

    congestion = -result.shadow_prices @ result.ptdf
    energy = result.nodal_prices - congestion
    # What is left after removing the congestion component is one price per hour.
    np.testing.assert_allclose(
        energy.to_numpy(), energy.to_numpy()[:, [0]].repeat(energy.shape[1], axis=1), atol=1e-6
    )


def test_flows_follow_ptdf_times_injections(three_zone, hours):
    load = pd.DataFrame({"B": [40.0], "C": [100.0], "X": [30.0]}, index=hours[:1])
    result = run_dc_opf(three_zone, load, THREE_ZONE_GENERATORS)

    generation = result.dispatch_mw.T.groupby(THREE_ZONE_GENERATORS["zone"]).sum().T
    injections = generation.reindex(columns=three_zone.zone_ids, fill_value=0.0) - load.reindex(
        columns=three_zone.zone_ids, fill_value=0.0
    )
    assert injections.sum(axis=1).iloc[0] == pytest.approx(0, abs=1e-6)
    np.testing.assert_allclose(
        result.flows_mw.to_numpy(), (injections @ result.ptdf.T).to_numpy(), atol=1e-6
    )


def test_parallel_lines_share_flow_by_susceptance(two_zone, hours):
    network = ZoneNetwork(
        zones=two_zone.zones,
        lines=(
            Line(
                line_id="strong",
                from_zone="N",
                to_zone="S",
                susceptance_mw_per_rad=300,
                capacity_mw=1000,
            ),
            Line(
                line_id="weak",
                from_zone="N",
                to_zone="S",
                susceptance_mw_per_rad=100,
                capacity_mw=20,
            ),
        ),
    )
    load = pd.DataFrame({"S": [40.0, 100.0]}, index=hours[:2])
    result = run_dc_opf(network, load, TWO_ZONE_GENERATORS)
    # The weak line takes 1/4 of the transfer and limits it to 80 MW in total.
    assert_frame(result.flows_mw, {"strong": [30, 60], "weak": [10, 20]})
    assert_frame(result.shadow_prices, {"strong": [0, 0], "weak": [0, 4 * 30]})


def test_hour_subset_matches_full_run(three_zone):
    hours = pd.date_range("2023-01-01", periods=48, freq="h", tz="Europe/Berlin")
    rng = np.random.default_rng(0)
    load = pd.DataFrame(rng.uniform(0, 150, size=(48, 2)), index=hours, columns=["B", "C"])
    availability = pd.DataFrame({"cheap_A": rng.uniform(0.1, 1, size=48)}, index=hours)

    full = run_dc_opf(three_zone, load, THREE_ZONE_GENERATORS, availability)
    subset = hours[[40, 3, 17]]
    part = run_dc_opf(three_zone, load, THREE_ZONE_GENERATORS, availability, snapshots=subset)

    assert part.flows_mw.index.equals(subset)
    for name in ("flows_mw", "shadow_prices", "nodal_prices", "dispatch_mw"):
        pd.testing.assert_frame_equal(
            getattr(part, name), getattr(full, name).loc[subset], atol=1e-6, check_freq=False
        )
    # Solving twice gives identical numbers.
    again = run_dc_opf(three_zone, load, THREE_ZONE_GENERATORS, availability, snapshots=subset)
    pd.testing.assert_frame_equal(again.shadow_prices, part.shadow_prices)


def test_availability_limits_dispatch(two_zone, hours):
    load = pd.DataFrame({"S": [50.0, 50.0]}, index=hours[:2])
    generators = make_generators({"wind_N": ("N", 100, 0), "costly_S": ("S", 500, 50)})
    availability = pd.DataFrame({"wind_N": [0.2, 1.0]}, index=hours[:2])
    result = run_dc_opf(two_zone, load, generators, availability)
    assert_frame(result.dispatch_mw, {"wind_N": [20, 50], "costly_S": [30, 0]})
    assert_frame(result.shadow_prices, {"N-S": [0, 0]})


def test_infeasible_without_load_shedding(two_zone, hours):
    load = pd.DataFrame({"S": [100.0]}, index=hours[:1])
    generators = make_generators({"cheap_N": ("N", 500, 20)})
    with pytest.raises(OPFError, match="load_shedding_cost"):
        run_dc_opf(two_zone, load, generators)


def test_load_shedding_covers_shortfall(two_zone, hours):
    load = pd.DataFrame({"S": [100.0, 40.0]}, index=hours[:2])
    generators = make_generators({"cheap_N": ("N", 500, 20)})
    result = run_dc_opf(two_zone, load, generators, load_shedding_cost=3000)

    assert_frame(result.load_shedding_mw, {"N": [0, 0], "S": [40, 0]})
    assert_frame(result.dispatch_mw, {"cheap_N": [60, 40]})
    assert_frame(result.nodal_prices, {"N": [20, 20], "S": [3000, 20]})
    assert_frame(result.shadow_prices, {"N-S": [2980, 0]})


def test_load_shedding_never_exceeds_the_zones_own_load(two_zone, hours):
    # S is short only in hour 0. In hour 1 its load is small, while N lacks
    # generation: shedding in S must not export to N, where it would be cheaper
    # than the costly generator in N.
    load = pd.DataFrame({"N": [0.0, 50.0], "S": [200.0, 10.0]}, index=hours[:2])
    generators = make_generators({"costly_N": ("N", 30, 5000), "cheap_S": ("S", 100, 20)})
    result = run_dc_opf(two_zone, load, generators, load_shedding_cost=3000)

    assert_frame(result.load_shedding_mw, {"N": [0, 0], "S": [100, 0]})
    assert_frame(result.flows_mw, {"N-S": [0, -50]})
    assert_frame(result.dispatch_mw, {"costly_N": [0, 0], "cheap_S": [100, 60]})
    assert (result.load_shedding_mw <= load + 1e-6).all().all()


def test_zero_cost_generators_only(two_zone, hours):
    load = pd.DataFrame({"S": [30.0, 80.0]}, index=hours[:2])
    generators = make_generators({"wind_N": ("N", 100, 0), "solar_S": ("S", 25, 0)})
    result = run_dc_opf(two_zone, load, generators)

    assert list(result.dispatch_mw.columns) == ["wind_N", "solar_S"]
    np.testing.assert_allclose(result.dispatch_mw.sum(axis=1), [30, 80], atol=1e-6)
    assert result.flows_mw["N-S"].iloc[1] >= 55 - 1e-6
    assert_frame(result.shadow_prices, {"N-S": [0, 0]})
    assert_frame(result.nodal_prices, {"N": [0, 0], "S": [0, 0]})
    assert result.objective_eur == pytest.approx(0)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"load": pd.DataFrame({"Q": [1.0]})}, "not zones"),
        ({"load": pd.DataFrame({"S": [float("nan")]})}, "missing values"),
        ({"load": pd.DataFrame({"S": [1.0, 2.0]}, index=[0, 0])}, "duplicate snapshots"),
        ({"load": pd.DataFrame({"S": []})}, "no snapshots"),
        ({"snapshots": [7]}, "snapshots must be"),
        ({"generators": make_generators({"g": ("Q", 1, 1)})}, "unknown zones"),
        ({"generators": make_generators({"g": ("N", -1, 1)})}, "non-negative"),
        ({"generators": make_generators({"objective_anchor": ("N", 1, 1)})}, "must not"),
        ({"generators": make_generators({"load_shedding N": ("N", 1, 1)})}, "must not"),
        ({"generators": pd.DataFrame({"zone": ["N"]}, index=["g"])}, "missing columns"),
        ({"availability": pd.DataFrame({"zzz": [1.0]})}, "not generators"),
        ({"availability": pd.DataFrame({"cheap_N": [1.5]})}, "between 0 and 1"),
        ({"availability": pd.DataFrame({"cheap_N": [1.0]}, index=[5])}, "lacks snapshots"),
    ],
)
def test_rejects_invalid_inputs(two_zone, change, message):
    arguments = {
        "load": pd.DataFrame({"S": [10.0]}),
        "generators": TWO_ZONE_GENERATORS,
        "availability": None,
        "snapshots": None,
    }
    arguments.update(change)
    with pytest.raises(OPFError, match=message):
        run_dc_opf(
            two_zone,
            arguments["load"],
            arguments["generators"],
            arguments["availability"],
            arguments["snapshots"],
        )
