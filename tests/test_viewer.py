from datetime import date
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from gridfee import viewer  # noqa: E402
from gridfee.backcast import split_borders_by_tso  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "networks"


def _app(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", [viewer.__file__, *map(str, args)])
    return AppTest.from_file(viewer.__file__).run()


def test_demo_filters_and_keeps_the_repeated_hour(monkeypatch):
    app = _app(monkeypatch)
    assert not app.exception
    assert "Synthetic demo" in app.info[0].value
    assert [m.value for m in app.metric][:3] == ["337", "334", "3"]
    assert app.metric[1].label == "Hours with fee-signal data"
    assert not app.warning
    assert [tab.label for tab in app.tabs] == [
        "Fee signal",
        "Lines",
        "Unserved load",
        "Site and asset",
    ]
    assert [heading.value for heading in app.subheader][:2] == [
        "Daily mean fee signal",
        "Zone locations",
    ]

    day = date(2023, 10, 29)
    app.selectbox[0].select("S")
    app.selectbox[1].select("W-S")
    app.run()
    app.date_input[0].set_value((day,)).run()
    assert not app.exception
    assert app.metric[0].value == "337"
    assert [box.value for box in app.selectbox][:2] == ["S", "W-S"]

    app.date_input[0].set_value((day, day))
    app.sidebar.number_input[0].set_value(100.0)
    app.run()
    assert not app.exception
    assert app.metric[0].value == "25"
    assert [box.value for box in app.selectbox][:2] == ["S", "W-S"]
    assert (app.dataframe[0].value[["Positive hours", "Negative hours"]] == 0).all().all()
    assert app.dataframe[0].value[["charge_mean", "feed_in_mean"]].isna().all().all()
    assert "Hourly line loading" in [heading.value for heading in app.tabs[1].subheader]
    assert list(viewer._hours_of(day).strftime("%H:%M %Z"))[2:4] == ["02:00 CEST", "02:00 CET"]
    assert len(viewer._hours_of(date(2023, 3, 26))) == 23


def test_line_cutoff_and_fixed_map_scale(monkeypatch):
    app = _app(monkeypatch)
    assert any("Fixed across dates" in caption.value for caption in app.caption)
    assert (app.dataframe[1].value["Priced hours"] > 0).any()
    assert app.selectbox[1].value == app.dataframe[1].value.index[0]
    app.tabs[1].number_input[0].set_value(1000)
    app.sidebar.checkbox[0].set_value(False)
    app.run()
    assert not app.exception
    assert any("Scaled to selected dates" in caption.value for caption in app.caption)
    assert (app.dataframe[1].value["Priced hours"] == 0).all()
    assert (app.dataframe[0].value["charge_mean"] > 0).any()
    assert (app.dataframe[0].value["feed_in_mean"] > 0).any()


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
        ("flows_mw", network.line_ids, 10),
        ("load_shedding_mw", network.zone_ids, 1),
    ):
        pd.DataFrame(value, index=hour, columns=ids).to_csv(tmp_path / f"{name}.csv")

    app = _app(monkeypatch, "--results", tmp_path, "--network", network_dir)
    assert not app.exception and not app.error
    assert [m.value for m in app.metric][:3] == ["1", "0", "1"]
    assert any("No internal-zone fee signal" in info.value for info in app.info)
    lines, unserved = (frame.value for frame in app.dataframe)
    assert lines["Priced hours"].isna().all()
    assert (lines["Peak loading"] > 0).all()
    assert unserved["Unserved load (MWh)"].sum() == 4
    assert not app.warning


def _write_run(directory, hours, *, shed_hours=None, fee_hours=None, flow=0.0):
    network_dir = EXAMPLES / "three_zone"
    network = viewer.load_network(network_dir)
    for name, values, ids, index in (
        ("fee_signal", 0.0, network.internal_zone_ids, fee_hours),
        ("shadow_prices", 0.0, network.line_ids, hours),
        ("flows_mw", flow, network.line_ids, hours),
        ("load_shedding_mw", 0.0008, network.zone_ids, shed_hours),
    ):
        if name == "load_shedding_mw" and index is None:
            continue
        pd.DataFrame(values, columns=ids, index=hours if index is None else index).rename_axis(
            "hour"
        ).to_csv(directory / f"{name}.csv")
    return ("--results", directory, "--network", network_dir)


@pytest.mark.parametrize("problem", ["short", "shifted", "naive"])
def test_rejects_mismatched_shedding_hours_and_naive_dates(monkeypatch, tmp_path, problem):
    hours = pd.date_range("2023-03-26", periods=23, freq="h", tz="Europe/Berlin")
    if problem == "naive":
        args = _write_run(tmp_path, hours.tz_localize(None))
        expected = "must hold hours with a UTC offset"
    else:
        shed_hours = hours[:3] if problem == "short" else hours + pd.Timedelta(days=2)
        args = _write_run(tmp_path, hours, shed_hours=shed_hours)
        expected = "load_shedding_mw must cover the same solved hours"
    app = _app(monkeypatch, *args)
    assert not app.exception
    assert expected in app.error[0].value


def test_warns_about_missing_fee_data_and_over_capacity(monkeypatch, tmp_path):
    hours = pd.date_range("2023-03-26", periods=23, freq="h", tz="Europe/Berlin")
    args = _write_run(tmp_path, hours, fee_hours=hours[:3], flow=500.0)
    app = _app(monkeypatch, *args)
    assert not app.exception and not app.error
    assert [m.value for m in app.metric] == ["23", "3", "20"]
    assert "20 solved hours are missing fee-signal data" in app.warning[0].value
    assert "exceed the supplied line capacities" in app.warning[1].value


def test_unserved_load_for_no_solve_and_distributed_shortfall(monkeypatch, tmp_path):
    hours = pd.DatetimeIndex(["2023-01-01", "2023-01-03"], tz="Europe/Berlin")
    args = _write_run(tmp_path, hours, shed_hours=hours, fee_hours=hours[:0])
    app = _app(monkeypatch, *args)
    assert not app.exception and not app.warning
    assert app.dataframe[-1].value["Unserved load (MWh)"].sum() == pytest.approx(0.0064)
    day = date(2023, 1, 2)
    app.date_input[0].set_value((day, day)).run()
    assert [m.value for m in app.metric] == ["0", "0", "0"]
    assert app.tabs[2].info[0].value == "No solved hours for these dates."


@pytest.mark.parametrize("invalid_border", [False, True])
def test_backcast_with_split_border_zones(monkeypatch, tmp_path, invalid_border):
    network_dir = tmp_path / "network"
    network_dir.mkdir()
    source = EXAMPLES / "three_zone"
    (network_dir / "zones.csv").write_text((source / "zones.csv").read_text())
    (network_dir / "lines.csv").write_text(
        (source / "lines.csv").read_text().rstrip() + "\nA-X,A,X,100,100\n"
    )
    hours = pd.date_range("2023-01-01", periods=2, freq="h", tz="Europe/Berlin", name="hour")
    exchanges = pd.DataFrame({"50Hertz_X": 1.0, "Amprion_X": 1.0}, index=hours)
    network = split_borders_by_tso(viewer.load_network(network_dir), exchanges)
    pd.DataFrame(0.0, index=hours[1:], columns=network.internal_zone_ids).to_csv(
        tmp_path / "fee_signal.csv"
    )
    for name in ("shadow_prices", "flows_mw"):
        pd.DataFrame(0.0, index=hours, columns=network.line_ids).to_csv(tmp_path / f"{name}.csv")
    unserved = pd.DataFrame(0.0, index=hours, columns=network.zone_ids)
    unserved.loc[hours[0], "X/Amprion"] = 3.0
    if invalid_border:
        unserved = unserved.rename(columns={"X/Amprion": "X/TransnetBW"})
    unserved.to_csv(tmp_path / "load_shedding_mw.csv")

    app = _app(monkeypatch, "--results", tmp_path, "--network", network_dir)
    assert not app.exception
    if invalid_border:
        assert "load_shedding_mw does not match" in app.error[0].value
    else:
        assert not app.error
        assert [m.value for m in app.metric][:3] == ["2", "1", "1"]
        lines = app.dataframe[1].value
        assert lines.loc["A-X", "To"] == "X/50Hertz"
        assert lines.loc["C-X", "To"] == "X/Amprion"
        assert app.dataframe[2].value.loc["X/Amprion", "Unserved load (MWh)"] == 3.0


def test_incomplete_launch_arguments_or_missing_files(monkeypatch, tmp_path):
    app = _app(monkeypatch, "--results", tmp_path)
    assert not app.exception
    assert "Pass both --results and --network" in app.error[0].value

    app = _app(monkeypatch, "--results", tmp_path, "--network", EXAMPLES / "three_zone")
    assert not app.exception
    assert "fee_signal.csv or fee_signal.parquet" in app.error[0].value


def test_site_asset_demo_and_battery(monkeypatch):
    app = _app(monkeypatch)
    assert not app.exception
    assert any(
        "Look-back:" in heading.value and "2023" in heading.value for heading in app.subheader
    )
    app.selectbox[2].select("S")
    app.selectbox[3].select("battery").run()
    assert not app.exception
    assert any("excluded (no signal / unserved): 3" in text.value for text in app.caption)
    assert any("Hourly net injection" in heading.value for heading in app.subheader)
    app.date_input[0].set_value((date(2023, 10, 29), date(2023, 10, 29))).run()
    assert not app.exception
    assert any("perfect foresight" in text.value for text in app.caption)
    assert app.dataframe[-1].value.index.tolist() == ["2023-10"]
    assert app.dataframe[-1].value.index.name == "Month"
    hourly = app.dataframe[-2].value
    assert len(hourly) == 25
    assert hourly.timestamp_utc.nunique() == 25
    assert "state_of_charge_mwh" in hourly
    assert any("not a tariff or payment" in text.value for text in app.caption)
    assert app.get("download_button")[0].label == "Download hourly CSV"
    app.date_input[0].set_value((date(2023, 10, 30), date(2023, 10, 30))).run()
    assert len(app.dataframe[-2].value) == 24


def test_asset_pattern_counts_days_once_across_repeated_hour():
    tables, _ = viewer._demo()
    hours = tables.shadow_prices.index
    frame = pd.DataFrame({"signal": 2.0, "charge_mw": 1.0, "discharge_mw": 0.0}, index=hours)
    pattern = viewer._asset_pattern(frame)
    assert pattern.loc[2, "charging"] == 14
    assert pattern.loc[2, "mean_signal"] == 2
    assert pattern["discharging"].eq(0).all()
    assert viewer._asset_pattern(frame.iloc[:0])["mean_signal"].isna().all()


def test_asset_cache_reuses_results_and_invalidates_inputs(monkeypatch):
    from unittest.mock import Mock

    compute = Mock(wraps=viewer.asset_fees)
    monkeypatch.setattr(viewer, "asset_fees", compute)
    viewer._cached_asset_fees.clear()
    hours = pd.date_range("2023-01-01", periods=2, freq="h", tz="Europe/Berlin")
    signal = pd.Series([10.0, -10.0], index=hours)
    args = ["N", signal, "battery", 1.0, 2.0, 0.9]
    try:
        first = viewer._cached_asset_fees(*args)
        second = viewer._cached_asset_fees(*args)
        pd.testing.assert_frame_equal(first.hourly, second.hourly)
        assert compute.call_count == 1
        for index, value in enumerate(["S", signal.iloc[:1], "load", 2.0, 3.0, 0.8]):
            changed = args.copy()
            changed[index] = value
            viewer._cached_asset_fees(*changed)
        changed_signal = signal + 1
        viewer._cached_asset_fees("N", changed_signal, "battery", 1.0, 2.0, 0.9)
        assert compute.call_count == 8
    finally:
        viewer._cached_asset_fees.clear()
