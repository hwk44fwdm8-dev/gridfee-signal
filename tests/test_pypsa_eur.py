"""Aggregation of a small transmission grid to zone lines, with hand-checked numbers."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from gridfee.network.pypsa_eur import (
    LINE_COLUMNS,
    apply_published_limits,
    derive_zone_lines,
    read_prebuilt_network,
)


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = np.radians([*a, *b])
    h = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return float(2 * 6371.0 * np.arcsin(np.sqrt(h)))


A, B, C = (10.0, 50.0), (11.0, 50.0), (11.0, 51.0)
AB_M = _haversine_km(A, B) * 1000
BC_M = _haversine_km(B, C) * 1000

BUSES = pd.DataFrame(
    {
        "voltage": [380, 380, 380, 380, 400, 400],
        "dc": ["f", "f", "f", "f", "t", "t"],
        "x": [A[0], A[0], B[0], C[0], B[0], 12.0],
        "y": [A[1], A[1], B[1], C[1], B[1], 55.0],
        "under_construction": ["f"] * 6,
        "country": ["DE"] * 5 + ["SE"],
    },
    index=pd.Index(["a1", "a2", "b1", "c1", "b_dc", "d_dc"], name="bus_id"),
)
LINES = pd.DataFrame(
    {
        "bus0": ["a1", "a2", "a1", "b1"],
        "bus1": ["b1", "b1", "a2", "c1"],
        "voltage": [380] * 4,
        "x": [20.0, 30.0, 1.0, 40.0],
        "r": [2.0, 3.0, 0.1, 4.0],
        "s_nom": [1000.0, 500.0, 9999.0, 800.0],
        "length": [AB_M, AB_M, 10.0, BC_M],
        "under_construction": ["f"] * 4,
    },
    index=pd.Index(["l1", "l2", "inside_a", "l3"], name="line_id"),
)
LINKS = pd.DataFrame(
    {"bus0": ["b_dc"], "bus1": ["d_dc"], "p_nom": [600.0], "under_construction": ["f"]},
    index=pd.Index(["cable"], name="link_id"),
)
BUSMAP = pd.Series(
    {"a1": "ZA", "a2": "ZA", "b1": "ZB", "c1": "ZC", "b_dc": "ZB", "d_dc": "SE"}, name="zone_id"
)


def test_parallel_lines_add_up_and_lines_inside_a_zone_disappear():
    lines, _ = derive_zone_lines(BUSES, LINES, LINKS, BUSMAP)
    lines = lines.set_index("line_id")

    assert sorted(lines.index) == ["SE-ZB", "ZA-ZB", "ZB-ZC"]
    ab = lines.loc["ZA-ZB"]
    # Ratings add up; 70 % of them is usable.
    assert ab["ac_rating_mva"] == pytest.approx(1500.0)
    assert ab["capacity_mw"] == pytest.approx(0.7 * 1500.0)
    # 20 and 30 ohm in parallel are 12 ohm: 380 kV ** 2 / 12 ohm.
    assert ab["susceptance_mw_per_rad"] == pytest.approx(380.0**2 / 12.0, rel=1e-3)
    assert lines.loc["ZB-ZC", "susceptance_mw_per_rad"] == pytest.approx(380.0**2 / 40, rel=1e-3)
    assert lines.loc["ZB-ZC", "capacity_mw"] == pytest.approx(560.0)


def test_dc_link_to_a_zone_without_other_lines_keeps_its_rating():
    lines, centres = derive_zone_lines(BUSES, LINES, LINKS, BUSMAP)
    cable = lines.set_index("line_id").loc["SE-ZB"]

    assert cable["dc_rating_mw"] == 600.0
    assert cable["capacity_mw"] == 600.0  # no security margin on a controlled link
    assert cable["susceptance_mw_per_rad"] == 600.0
    assert centres.loc["SE", ["lon", "lat"]].tolist() == [12.0, 55.0]
    assert centres.loc["ZA", ["lon", "lat"]].tolist() == [10.0, 50.0]
    assert set(LINE_COLUMNS) <= set(lines.columns)


def test_dc_link_parallel_to_ac_lines_adds_capacity_and_warns(caplog):
    busmap = BUSMAP.replace({"SE": "ZC"})
    # A third line closes the triangle, so the link is no longer the only way to ZC.
    triangle = pd.concat([LINES, LINES.loc[["l3"]].assign(bus0="a1").rename({"l3": "l4"})])

    with caplog.at_level(logging.WARNING):
        lines, _ = derive_zone_lines(BUSES, triangle, LINKS, busmap)

    bc = lines.set_index("line_id").loc["ZB-ZC"]
    assert bc["capacity_mw"] == pytest.approx(0.7 * 800.0 + 600.0)
    assert bc["susceptance_mw_per_rad"] == pytest.approx(380.0**2 / 40, rel=1e-3)
    assert "modelled as an AC line" in caplog.text


def test_buses_missing_from_the_busmap_are_left_out():
    lines, _ = derive_zone_lines(BUSES, LINES, LINKS, BUSMAP.drop(["c1", "d_dc"]))

    assert lines["line_id"].tolist() == ["ZA-ZB"]


def test_read_prebuilt_network_keeps_what_is_in_service_at_220_kv_and_above(tmp_path):
    buses = BUSES.copy()
    buses.loc["a2", "voltage"] = 110
    buses.loc["c1", "under_construction"] = "t"
    buses.to_csv(tmp_path / "buses.csv")
    lines = LINES.assign(geometry="'LINESTRING (1 2, 3 4)'")
    lines.to_csv(tmp_path / "lines.csv", quotechar="'")
    LINKS.to_csv(tmp_path / "links.csv")

    read_buses, read_lines, read_links = read_prebuilt_network(tmp_path)

    assert sorted(read_buses.index) == ["a1", "b1", "b_dc", "d_dc"]
    assert read_lines.index.tolist() == ["l1"]
    assert read_links.index.tolist() == ["cable"]


def test_published_limits_replace_the_rating_of_the_listed_lines_only():
    zone_lines = pd.DataFrame(
        {
            "line_id": ["A-B", "A-C"],
            "ac_rating_mva": [1000.0, 500.0],
            "dc_rating_mw": [0.0, 100.0],
            "capacity_mw": [700.0, 450.0],
        }
    )
    circuits = pd.DataFrame(
        {"line_id": ["A-B", "A-B"], "voltage_kv": [380, 220], "max_current_a": [3000, 1000]}
    )
    result = apply_published_limits(zone_lines, circuits)
    rating = np.sqrt(3) * (380 * 3000 + 220 * 1000) / 1000
    assert result["ac_rating_mva"].tolist() == pytest.approx([rating, 500.0])
    assert result["capacity_mw"].tolist() == pytest.approx([0.7 * rating, 450.0])
    assert zone_lines["capacity_mw"].tolist() == [700.0, 450.0]


def test_published_limits_for_an_unknown_line_are_rejected():
    zone_lines = pd.DataFrame(
        {"line_id": ["A-B"], "ac_rating_mva": [1.0], "dc_rating_mw": [0.0], "capacity_mw": [0.7]}
    )
    circuits = pd.DataFrame({"line_id": ["A-X"], "voltage_kv": [380], "max_current_a": [1]})
    with pytest.raises(ValueError, match="A-X"):
        apply_published_limits(zone_lines, circuits)


def test_the_de22_lines_listed_in_jao_circuits_carry_the_published_limits():
    from pathlib import Path

    network = Path(__file__).parents[1] / "data" / "networks" / "de22"
    circuits = pd.read_csv(network / "jao_circuits.csv", comment="#")
    lines = pd.read_csv(network / "lines.csv", comment="#").set_index("line_id")
    mva = np.sqrt(3) * circuits["voltage_kv"] * circuits["max_current_a"] / 1000
    expected = 0.7 * mva.groupby(circuits["line_id"]).sum()
    assert len(expected) == 4
    assert lines.loc[expected.index, "capacity_mw"].to_numpy() == pytest.approx(
        expected.to_numpy(), abs=0.06
    )
