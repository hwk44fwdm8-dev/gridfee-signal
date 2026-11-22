import json

import pandas as pd
import pytest
from typer.testing import CliRunner

from gridfee.cli import app
from gridfee.network import load_network
from gridfee.plots import (
    PlotError,
    ResultTables,
    daily_line_hours,
    daily_signal,
    daily_unserved,
    line_summary,
    read_results,
    zone_summary,
)

ZONES = """zone_id,name,kind,tso_zone,country,lat,lon
A,North,internal,TenneT,,54.0,10.0
B,South,internal,TenneT,,48.0,11.0
X,Neighbour,boundary,,XX,51.0,6.0
"""
LINES = """line_id,from_zone,to_zone,susceptance_mw_per_rad,capacity_mw
A-B,A,B,1000,100
X-A,X,A,1000,200
"""


def _square(lon: float, lat: float) -> list:
    return [[[lon, lat], [lon + 1, lat], [lon + 1, lat + 1], [lon, lat + 1], [lon, lat]]]


@pytest.fixture
def network_dir(tmp_path):
    directory = tmp_path / "network"
    directory.mkdir()
    (directory / "zones.csv").write_text(ZONES)
    (directory / "lines.csv").write_text(LINES)
    return directory


@pytest.fixture
def tables() -> ResultTables:
    # Two days; hour 3 has unserved load and is not in the fee signal.
    hours = pd.DatetimeIndex(
        ["2023-01-01 00:00", "2023-01-01 01:00", "2023-01-02 00:00", "2023-01-02 01:00"],
        tz="Europe/Berlin",
        name="hour",
    )
    shadow = pd.DataFrame({"A-B": [0.0, 10.0, -20.0, 500.0], "X-A": 0.0}, index=hours)
    flows = pd.DataFrame({"A-B": [50.0, 100.0, -100.0, 100.0], "X-A": 20.0}, index=hours)
    signal = pd.DataFrame(
        {"A": [0.0, 5.0, -10.0], "B": [0.0, -5.0, 10.0], "X": 0.0}, index=hours[:3]
    )
    unserved = pd.DataFrame({"A": 0.0, "B": [0.0, 0.0, 0.0, 30.0], "X": 0.0}, index=hours)
    return ResultTables(signal, shadow, flows, unserved)


def _write(tables: ResultTables, directory) -> None:
    directory.mkdir()
    tables.signal.to_csv(directory / "fee_signal.csv")
    tables.shadow_prices.to_csv(directory / "shadow_prices.csv")
    tables.flows_mw.to_csv(directory / "flows_mw.csv")
    tables.unserved_mw.to_csv(directory / "load_shedding_mw.csv")


def test_zone_summary_counts_hours_by_sign(tables):
    summary = zone_summary(tables.signal)
    assert summary.loc["A"].tolist() == [1, 1, pytest.approx(-5 / 3)]
    assert summary.loc["B", ["positive_hours", "negative_hours"]].tolist() == [1, 1]
    assert zone_summary(tables.signal, threshold=7).loc["A", "positive_hours"] == 0


def test_line_summary_leaves_out_hours_without_signal(tables, network_dir):
    capacity = load_network(network_dir).lines_frame()["capacity_mw"]
    summary = line_summary(tables, capacity)
    # The price of 500 belongs to the hour with unserved load.
    assert summary.loc["A-B", "priced_hours"] == 2
    assert summary.loc["A-B", "mean_price_when_priced"] == pytest.approx(15.0)
    assert summary.loc["A-B", "max_loading"] == pytest.approx(1.0)
    assert summary.loc["X-A", "priced_hours"] == 0
    assert summary.loc["X-A", "mean_loading"] == pytest.approx(0.1)


def test_daily_tables(tables):
    assert daily_signal(tables.signal)["A"].tolist() == [2.5, -10.0]
    lines = daily_line_hours(tables)
    assert lines.columns.tolist() == ["A-B"]
    assert lines["A-B"].tolist() == [1, 1]
    unserved = daily_unserved(tables)
    assert unserved.columns.tolist() == ["B"]
    assert unserved["B"].tolist() == [0.0, 30.0]


def test_read_results_round_trip(tables, tmp_path):
    _write(tables, tmp_path / "results")
    read = read_results(tmp_path / "results")
    pd.testing.assert_frame_equal(read.signal, tables.signal, check_freq=False)
    assert read.unserved_mw["B"].sum() == 30.0


def test_read_results_names_missing_table(tmp_path):
    with pytest.raises(PlotError, match="fee_signal"):
        read_results(tmp_path)


def test_plot_command_writes_figures(tables, network_dir, tmp_path):
    _write(tables, tmp_path / "results")
    regions = tmp_path / "regions.geojson"
    features = [
        {
            "type": "Feature",
            "properties": {"NUTS_ID": zone_id},
            "geometry": {"type": "Polygon", "coordinates": _square(lon, lat)},
        }
        for zone_id, lon, lat in (("A", 9.5, 53.5), ("B", 10.5, 47.5))
    ]
    regions.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    for extra in ([], ["--regions", str(regions)]):
        result = CliRunner().invoke(
            app,
            ["plot", "--results", str(tmp_path / "results"), "--network", str(network_dir)] + extra,
        )
        assert result.exit_code == 0, result.output
        written = sorted(p.name for p in (tmp_path / "results" / "plots").glob("*.png"))
        assert written == [
            "map_lines.png",
            "map_mean_signal.png",
            "map_signal_hours.png",
            "timeseries_lines.png",
            "timeseries_signal.png",
            "timeseries_unserved.png",
        ]
        assert all(
            (tmp_path / "results" / "plots" / name).stat().st_size > 5000 for name in written
        )


def test_plot_command_without_coordinates_skips_maps(tables, network_dir, tmp_path):
    _write(tables, tmp_path / "results")
    zones = (network_dir / "zones.csv").read_text().replace(",54.0,10.0", ",,")
    (network_dir / "zones.csv").write_text(zones)
    result = CliRunner().invoke(
        app, ["plot", "--results", str(tmp_path / "results"), "--network", str(network_dir)]
    )
    assert result.exit_code == 0, result.output
    written = {p.name for p in (tmp_path / "results" / "plots").glob("*.png")}
    assert written == {"timeseries_lines.png", "timeseries_signal.png", "timeseries_unserved.png"}


def test_plot_command_reports_zone_missing_from_signal(tables, network_dir, tmp_path):
    _write(tables, tmp_path / "results")
    tables.signal.drop(columns="B").to_csv(tmp_path / "results" / "fee_signal.csv")
    result = CliRunner().invoke(
        app, ["plot", "--results", str(tmp_path / "results"), "--network", str(network_dir)]
    )
    assert result.exit_code == 1
    assert "lacks the zones ['B']" in result.output
