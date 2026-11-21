import pandas as pd
from typer.testing import CliRunner

from gridfee.cli import app

from .conftest import EXAMPLES

runner = CliRunner()
INPUTS = EXAMPLES / "inputs" / "three_zone"
NETWORK = EXAMPLES / "networks" / "three_zone"


def _run(tmp_path, *extra: str):
    return runner.invoke(
        app,
        [
            "run",
            "--network",
            str(NETWORK),
            "--load",
            str(INPUTS / "load.csv"),
            "--generators",
            str(INPUTS / "generators.csv"),
            "--out",
            str(tmp_path / "out"),
            *extra,
        ],
    )


def test_run_writes_results(tmp_path):
    result = _run(tmp_path)
    assert result.exit_code == 0, result.output
    assert "Solved 3 hours on 4 zones and 4 lines; 2 hours with congestion" in result.output

    shadow = pd.read_csv(tmp_path / "out" / "shadow_prices.csv", index_col=0)
    assert shadow.index.tolist() == pd.read_csv(INPUTS / "load.csv")["snapshot"].tolist()
    assert shadow["A-C"].round(6).tolist() == [60.0, 60.0, 0.0]
    for name in ("flows_mw", "nodal_prices", "dispatch_mw", "ptdf"):
        assert (tmp_path / "out" / f"{name}.csv").is_file()
    assert not (tmp_path / "out" / "load_shedding_mw.csv").exists()


def test_run_hour_subset(tmp_path):
    hours = tmp_path / "hours.csv"
    hours.write_text("snapshot\n2023-01-01T02:00+01:00\n2023-01-01T00:00+01:00\n")
    result = _run(tmp_path, "--hours", str(hours))
    assert result.exit_code == 0, result.output
    shadow = pd.read_csv(tmp_path / "out" / "shadow_prices.csv", index_col=0)
    assert shadow["A-C"].round(6).tolist() == [0.0, 60.0]


def test_run_reports_bad_network(tmp_path):
    result = runner.invoke(
        app,
        [
            "run",
            "--network",
            str(tmp_path),
            "--load",
            str(INPUTS / "load.csv"),
            "--generators",
            str(INPUTS / "generators.csv"),
        ],
    )
    assert result.exit_code == 1
    assert "zones.csv" in result.output


def test_rerun_removes_stale_load_shedding_file(tmp_path):
    assert _run(tmp_path, "--load-shedding-cost", "3000").exit_code == 0
    assert (tmp_path / "out" / "load_shedding_mw.csv").is_file()
    assert _run(tmp_path).exit_code == 0
    assert not (tmp_path / "out" / "load_shedding_mw.csv").exists()


def _write_smard_tables(directory, year=2023):
    from gridfee.data import smard

    index = pd.date_range(f"{year}-01-01", periods=48, freq="h", tz="Europe/Berlin", name="hour")
    tsos = ["50Hertz", "Amprion", "TenneT", "TransnetBW"]
    directory.mkdir()
    for name in ("load_tso", "wind_onshore_tso", "solar_tso"):
        frame = pd.DataFrame({tso: 100.0 * (n + 1) for n, tso in enumerate(tsos)}, index=index)
        frame.to_csv(directory / smard.table_filename(name, year))
    offshore = pd.DataFrame({"50Hertz": 10.0, "TenneT": 30.0}, index=index)
    offshore.to_csv(directory / smard.table_filename("wind_offshore_tso", year))


DE22 = EXAMPLES.parent / "data" / "networks" / "de22" / "zones.csv"


def test_injections_command_flags_placeholders(tmp_path):
    _write_smard_tables(tmp_path / "raw")
    out = tmp_path / "out"
    args = ["injections", "--zones", str(DE22), "--data", str(tmp_path / "raw"), "--year", "2023"]
    result = runner.invoke(app, [*args, "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert "PLACEHOLDER shares" in result.output
    demand = pd.read_csv(out / "demand_mw.csv", index_col=0)
    assert demand.shape == (48, 22)
    assert demand["DE19"].iloc[0] == 100.0  # TransnetBW: 400 MW over 4 zones
    assert demand["DE10"].iloc[0] == 50.0  # Amprion: 200 MW over 4 zones
    shares = pd.read_csv(out / "shares.csv")
    # Offshore wind exists for 50Hertz (6 zones) and TenneT (8 zones) only.
    assert shares["placeholder"].all() and len(shares) == 3 * 22 + 14
    balance = pd.read_csv(out / "energy_balance.csv")
    assert balance["passed"].all() and len(balance) == 16
    net = pd.read_csv(out / "net_injection_mw.csv", index_col=0)
    assert net.sum().sum() == 48 * (2 * 1000 + 40 - 1000)

    population = tmp_path / "population.csv"
    population.write_text("zone_id,share\nDE19,0.4\nDE20,0.3\nDE21,0.2\nDE22,0.1\n")
    result = runner.invoke(app, [*args, "--out", str(out), "--population-shares", str(population)])
    assert result.exit_code == 0, result.output
    assert pd.read_csv(out / "demand_mw.csv", index_col=0)["DE19"].iloc[0] == 160.0
    shares = pd.read_csv(out / "shares.csv").set_index(["quantity", "zone_id"])["placeholder"]
    assert not shares["demand", "DE19"] and shares["demand", "DE01"]


def test_injections_command_reports_missing_data(tmp_path):
    result = runner.invoke(
        app, ["injections", "--zones", str(DE22), "--data", str(tmp_path), "--year", "2023"]
    )
    assert result.exit_code == 1
    assert "gridfee collect --year 2023" in result.output
