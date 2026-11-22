"""Reading JAO's Static Grid Model: limits, tie lines, tee points and positions."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from gridfee.network import jao


def _rows(*rows: dict) -> pd.DataFrame:
    base = {
        "name": "A - B",
        "tso": "TENNETGMBH",
        "sub1": "A",
        "sub2": "B",
        "voltage_kv": 380.0,
        "x": 10.0,
        "r": 1.0,
        "length_km": 40.0,
        "sheet": "Lines",
    }
    frame = pd.DataFrame([{**base, **row} for row in rows])
    return frame.reindex(columns=[*jao.COLUMNS, "sheet"])


def test_maximum_current_prefers_fixed_then_lowest_period_then_dlr():
    frame = _rows(
        {"eic": "1", "fixed": 2000.0, "period_1": 2500.0, "dlr_max": 3000.0},
        {"eic": "2", "period_1": 2500.0, "period_2": 2200.0, "dlr_max": 3000.0},
        {"eic": "3", "dlr_min": 1800.0, "dlr_max": 3000.0},
        {"eic": "4"},
    )
    limits = jao.maximum_current(frame)
    assert limits["max_current_a"].tolist()[:3] == [2000.0, 2200.0, 3000.0]
    assert np.isnan(limits["max_current_a"].iloc[3])
    assert limits["limit_kind"].tolist() == ["fixed", "period", "dlr_max", "none"]


def test_german_circuits_merges_the_two_sides_of_a_tie_line():
    lines = _rows(
        {"eic": "L1", "fixed": 2000.0},
        {"eic": "L2", "tso": "RTE", "fixed": 2000.0},
        {"eic": "L3"},
    )
    tielines = _rows(
        {"eic": "T1", "tso": "PSE", "sub1": "KRAJNIK", "fixed": 1500.0, "x": 3.9, "sheet": "T"},
        {"eic": "T1", "tso": "50HERTZ", "sub2": "Krajnik", "period_1": 2520.0, "x": 3.5},
        {"eic": "T2", "tso": "APG", "fixed": 900.0},
    ).assign(sheet="Tielines")
    circuits = jao.german_circuits(lines, tielines)

    # L2 is not German, L3 has no limit, T2 is not listed by a German TSO.
    assert sorted(circuits.index) == ["L1", "T1"]
    tie = circuits.loc["T1"]
    assert (tie["tso"], tie["sub2"]) == ("50HERTZ", "Krajnik")
    assert (tie["max_current_a"], tie["limit_kind"], tie["x"]) == (1500.0, "fixed", 3.9)


def test_merge_stubs_joins_tee_points_to_their_substation_but_not_across_tie_lines():
    circuits = _rows(
        {"eic": "1", "sub1": "Y-A", "sub2": "A", "length_km": 0.2},
        {"eic": "2", "sub1": "Y-A", "sub2": "B"},
        {"eic": "3", "sub1": "B", "sub2": "C", "length_km": 0.5, "sheet": "Tielines"},
    )
    place = jao.merge_stubs(circuits)
    assert place.to_dict() == {"Y-A": "A", "A": "A", "B": "B", "C": "C"}


def test_to_lines_refers_reactance_to_380_kv_and_rates_by_current():
    circuits = _rows(
        {"eic": "1", "voltage_kv": 220.0, "x": 10.0},
        {"eic": "2", "sub2": "A"},
    ).set_index("eic")
    circuits["max_current_a"] = 1000.0
    circuits["bus0"], circuits["bus1"] = circuits["sub1"], circuits["sub2"]
    lines = jao.to_lines(circuits)
    assert list(lines.index) == ["1"]  # a circuit inside one place is no line
    assert lines.loc["1", "x"] == pytest.approx(10.0 * (380 / 220) ** 2)
    assert lines.loc["1", "s_nom"] == pytest.approx(np.sqrt(3) * 220)
    assert lines.loc["1", "length"] == 40_000.0


def _map(tmp_path, shapes):
    call = {
        "method": "addPolylines",
        "args": [
            [[[{"lng": list(x), "lat": list(y)}]] for _, x, y in shapes],
            None,
            "Lines",
            {},
            [f"<b>NE name: n</b><br><b>EIC Code:<b> {eic}<br>" for eic, _, _ in shapes],
        ],
    }
    payload = json.dumps({"x": {"calls": [{"method": "addTiles", "args": []}, call]}})
    path = tmp_path / "map.html"
    path.write_text(f'<script type="application/json" data-for="htmlwidget-1">{payload}</script>')
    return path


def test_positions_are_the_median_over_the_circuits_of_a_substation(tmp_path):
    path = _map(
        tmp_path,
        [("1", (8.0, 9.0), (50.0, 51.0)), ("2", (8.02, 7.0), (50.02, 49.0)), ("9", (0, 1), (0, 1))],
    )
    positions = jao.read_map_positions(path)
    assert positions.loc["2"].tolist() == [8.02, 50.02, 7.0, 49.0]

    circuits = _rows(
        {"eic": "1", "sub1": "A", "sub2": "B"},
        {"eic": "2", "sub1": "A", "sub2": "C"},
        {"eic": "9", "sub1": "A", "sub2": "D", "sheet": "Tielines"},
    ).set_index("eic")
    where = jao.substation_positions(circuits, positions)
    assert where.loc["A"].tolist() == pytest.approx([8.01, 50.01])
    assert "D" not in where.index  # tie lines are drawn to the border only


def test_map_without_the_widget_data_is_rejected(tmp_path):
    path = tmp_path / "other.html"
    path.write_text("<html></html>")
    with pytest.raises(ValueError, match="not the leaflet map"):
        jao.read_map_positions(path)
