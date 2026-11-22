from datetime import date
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from gridfee import viewer  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "networks"


def _app(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", [viewer.__file__, *map(str, args)])
    return AppTest.from_file(viewer.__file__).run()


def test_demo_filters_and_keeps_the_repeated_hour(monkeypatch):
    app = _app(monkeypatch)
    assert not app.exception
    assert "Synthetic demo" in app.info[0].value
    assert [m.value for m in app.metric] == ["337", "334", "3"]
    assert [tab.label for tab in app.tabs] == ["Fee signal", "Lines", "Unserved load"]
    assert [heading.value for heading in app.subheader][:2] == [
        "Daily mean fee signal",
        "Zone locations",
    ]

    day = date(2023, 10, 29)
    app.date_input[0].set_value((day, day))
    app.selectbox[0].select("S")
    app.number_input[0].set_value(100.0)
    app.run()
    assert not app.exception
    assert app.metric[0].value == "25"
    assert app.selectbox[0].value == "S"
    assert (app.dataframe[0].value[["Positive hours", "Negative hours"]] == 0).all().all()


@pytest.mark.parametrize("extension", ["csv", "parquet"])
def test_existing_backcast_with_internal_only_signal(monkeypatch, tmp_path, extension):
    network_dir = EXAMPLES / "three_zone"
    network = viewer.load_network(network_dir)
    hours = pd.date_range("2023-01-01", periods=2, freq="h", tz="Europe/Berlin", name="hour")
    for name, ids in (
        ("fee_signal", network.internal_zone_ids),
        ("shadow_prices", network.line_ids),
        ("flows_mw", network.line_ids),
    ):
        frame = pd.DataFrame(0.0, index=hours, columns=ids)
        getattr(frame, f"to_{extension}")(tmp_path / f"{name}.{extension}")

    app = _app(monkeypatch, "--results", tmp_path, "--network", network_dir)
    assert not app.exception and not app.error
    assert app.metric[1].value == "2"
    assert len(app.dataframe[0].value) == 3
    assert any("no coordinates" in info.value for info in app.info)
    assert any("no load shedding table" in info.value for info in app.info)
    assert len(list(tmp_path.iterdir())) == 3

    mismatch = _app(monkeypatch, "--results", tmp_path, "--network", EXAMPLES / "two_zone")
    assert not mismatch.exception
    assert "does not match the network" in mismatch.error[0].value


def test_run_with_no_signal_hours_still_shows_unserved_load(monkeypatch, tmp_path):
    network_dir = EXAMPLES / "three_zone"
    network = viewer.load_network(network_dir)
    hour = pd.date_range("2023-01-01", periods=1, freq="h", tz="Europe/Berlin", name="hour")
    pd.DataFrame(columns=network.internal_zone_ids, index=hour[:0]).to_csv(
        tmp_path / "fee_signal.csv"
    )
    for name, ids, value in (
        ("shadow_prices", network.line_ids, 100),
        ("flows_mw", network.line_ids, 0),
        ("load_shedding_mw", network.zone_ids, 1),
    ):
        pd.DataFrame(value, index=hour, columns=ids).to_csv(tmp_path / f"{name}.csv")

    app = _app(monkeypatch, "--results", tmp_path, "--network", network_dir)
    assert not app.exception and not app.error
    assert [m.value for m in app.metric] == ["1", "0", "1"]
    assert any("No internal-zone fee signal" in info.value for info in app.info)
    assert app.dataframe[0].value["Unserved load (MWh)"].sum() == 4


def test_incomplete_launch_arguments_or_missing_files(monkeypatch, tmp_path):
    app = _app(monkeypatch, "--results", tmp_path)
    assert not app.exception
    assert "Pass both --results and --network" in app.error[0].value

    app = _app(monkeypatch, "--results", tmp_path, "--network", EXAMPLES / "three_zone")
    assert not app.exception
    assert "fee_signal.csv or fee_signal.parquet" in app.error[0].value
