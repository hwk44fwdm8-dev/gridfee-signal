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
