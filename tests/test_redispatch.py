import pandas as pd
import pytest
from typer.testing import CliRunner

from gridfee.cli import app
from gridfee.validation import (
    ValidationError,
    compare_with_redispatch,
    hourly_redispatch,
    read_redispatch,
    render_redispatch_report,
)

HEADER = (
    "BEGINN_DATUM;BEGINN_UHRZEIT;ZEITZONE_VON;ENDE_DATUM;ENDE_UHRZEIT;ZEITZONE_BIS;"
    "GRUND_DER_MASSNAHME;RICHTUNG;MITTLERE_LEISTUNG_MW;MAXIMALE_LEISTUNG_MW;"
    "GESAMTE_ARBEIT_MWH;ANWEISENDER_UENB;ANFORDERNDER_UENB;BETROFFENE_ANLAGE;PRIMAERENERGIEART"
)
UP = "Wirkleistungseinspeisung erhöhen"
DOWN = "Wirkleistungseinspeisung reduzieren"
ROWS = [
    # 00:30 to 02:00 CET, 150 MWh lowered in 50Hertz: 50 MWh in hour 0, 100 in hour 1.
    f"01.01.2023;00:30;CET;01.01.2023;02:00;CET;Strombedingter Redispatch;{DOWN};100;100;150;"
    "50Hertz;50Hertz;A;Konventionell",
    f"01.01.2023;01:00;CET;01.01.2023;02:00;CET;Strombedingter Redispatch;{UP};80;80;80,5;"
    "TenneT DE;50Hertz;B;Konventionell",
    f"01.01.2023;01:00;CET;01.01.2023;02:00;CET;Spannungsbedingter Redispatch;{UP};9;9;9;"
    "TenneT DE;TenneT DE;C;Konventionell",
    # Summer time: 12:00 CEST is 10:00 UTC.
    f"01.07.2023;12:00;CEST;01.07.2023;13:00;CEST;Strombedingter Redispatch;{UP};10;10;10;"
    "TenneT DE;TenneT DE;B;Konventionell",
]


def _write(path, rows=ROWS):
    path.write_text("\ufeff" + "\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def _hour(text):
    return pd.Timestamp(text, tz="UTC")


def test_read_and_spread_over_hours(tmp_path):
    measures = read_redispatch(_write(tmp_path / "rd.csv"))
    assert measures["tso"].tolist() == ["50Hertz", "TenneT", "TenneT", "TenneT"]
    assert measures["direction"].tolist() == [-1, 1, 1, 1]

    hourly = hourly_redispatch(measures)
    assert hourly.loc[_hour("2022-12-31 23:00"), ("50Hertz", -1)] == pytest.approx(50)
    assert hourly.loc[_hour("2023-01-01 00:00"), ("50Hertz", -1)] == pytest.approx(100)
    # The voltage-driven measure is left out.
    assert hourly.loc[_hour("2023-01-01 00:00"), ("TenneT", 1)] == pytest.approx(80.5)
    assert hourly.loc[_hour("2023-07-01 10:00"), ("TenneT", 1)] == pytest.approx(10)
    assert hourly.to_numpy().sum() == pytest.approx(240.5)


def test_read_rejects_other_files(tmp_path):
    path = tmp_path / "rd.csv"
    path.write_text("a;b\n1;2\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="missing column"):
        read_redispatch(path)
    with pytest.raises(ValidationError, match="unknown direction"):
        read_redispatch(_write(path, [ROWS[0].replace(DOWN, "anders")]))


TSOS = {"N": "50Hertz", "S": "TenneT"}
# Line ids need not name their zones.
LINES = {"corridor": ("N", "S"), "N-PL": ("N", "PL")}


def _model():
    hours = pd.date_range("2023-01-01 00:00", periods=3, freq="h", tz="Europe/Berlin")
    signal = pd.DataFrame({"N": [0.0, 20.0, 0.0], "S": [0.0, -30.0, 0.0]}, index=hours)
    prices = pd.DataFrame({"corridor": [0.0, 50.0, 0.0], "N-PL": [0.0, 0.0, 5.0]}, index=hours)
    return signal, prices


def test_compare(tmp_path):
    signal, prices = _model()
    redispatch = hourly_redispatch(read_redispatch(_write(tmp_path / "rd.csv")))
    result = compare_with_redispatch(signal, prices, TSOS, redispatch, LINES)

    assert result.hours.loc["signal", "hours"] == 1
    assert result.hours.loc["signal", "hours_with_redispatch"] == 1
    assert result.hours.loc["signal", "mean_redispatch_mwh"] == pytest.approx(180.5)
    assert result.hours.loc["neutral", "mean_redispatch_mwh"] == pytest.approx(25)
    assert result.deciles["signal_hours"].sum() == 1
    # Only the line between two German zones is listed.
    assert result.lines.index.tolist() == ["all signal hours", "corridor"]
    north = result.directions.loc[("corridor", "50Hertz")]
    assert north["mean_net_redispatch_mwh"] == pytest.approx(-100)
    assert (north["hours_compared"], north["hours_same_direction"]) == (1, 1)
    south = result.directions.loc[("corridor", "TenneT")]
    assert (south["hours_compared"], south["hours_same_direction"]) == (1, 1)

    opposite = compare_with_redispatch(-signal, prices, TSOS, redispatch, LINES)
    assert opposite.directions["hours_same_direction"].sum() == 0


def test_compare_needs_overlapping_hours(tmp_path):
    signal, prices = _model()
    redispatch = hourly_redispatch(read_redispatch(_write(tmp_path / "rd.csv", ROWS[3:])))
    with pytest.raises(ValidationError, match="covers none"):
        compare_with_redispatch(signal, prices, TSOS, redispatch, LINES)
    # Nor do hours on both sides of the export, or no hours at all.
    around = signal.set_axis(signal.index[:2].append(signal.index[2:] + pd.Timedelta(days=365)))
    for frame in (around, signal.iloc[:0]):
        with pytest.raises(ValidationError, match="covers none"):
            compare_with_redispatch(frame, prices, TSOS, redispatch, LINES)


def test_compare_without_signal_or_redispatch(tmp_path):
    signal, prices = _model()
    redispatch = hourly_redispatch(read_redispatch(_write(tmp_path / "rd.csv")))
    neutral = compare_with_redispatch(signal * 0, prices, TSOS, redispatch, LINES)
    assert neutral.hours.loc["signal", "hours"] == 0
    assert neutral.lines.empty and neutral.directions.empty
    assert "## Direction per TSO zone" in render_redispatch_report(neutral, "t")

    # Hours inside the export's range without a measure had no redispatch.
    calm = signal.iloc[[2]]
    assert calm.index[0] not in redispatch.index
    result = compare_with_redispatch(calm, prices, TSOS, redispatch, LINES)
    assert result.hours.loc["all", "hours_with_redispatch"] == 0


def test_cli(tmp_path):
    signal, prices = _model()
    zones = tmp_path / "zones.csv"
    zones.write_text(
        "zone_id,name,kind,tso_zone,country,lat,lon\n"
        "N,North,internal,50Hertz,,53,10\nS,South,internal,TenneT,,48,10\n"
        "PL,Poland,boundary,,PL,52,15\n",
        encoding="utf-8",
    )
    signal.to_csv(tmp_path / "fee_signal.csv", index_label="snapshot")
    signal.apply(lambda col: col.map(lambda v: "neutral" if v == 0 else "x")).to_csv(
        tmp_path / "fee_signal_class.csv", index_label="snapshot"
    )
    (tmp_path / "lines.csv").write_text(
        "line_id,from_zone,to_zone,susceptance_mw_per_rad,capacity_mw\n"
        "corridor,N,S,1000,500\nN-PL,N,PL,1000,500\n",
        encoding="utf-8",
    )
    prices.to_csv(tmp_path / "shadow_prices.csv", index_label="snapshot")
    result = CliRunner().invoke(
        app,
        [
            "redispatch",
            "--results",
            str(tmp_path),
            "--redispatch",
            str(_write(tmp_path / "rd.csv")),
            "--network",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 hours with a fee signal, 1 of them with redispatch" in result.output
    report = (tmp_path / "redispatch.md").read_text(encoding="utf-8")
    assert "| corridor | 50Hertz | 20.00 | -100.00 | 1 | 1 |" in report


def test_cli_reads_the_newer_parquet_signal(tmp_path):
    test_cli(tmp_path)
    signal, _ = _model()
    # A rerun as Parquet without a signal leaves the earlier CSV files behind.
    (signal * 0).to_parquet(tmp_path / "fee_signal.parquet")
    signal.map(lambda _: "neutral").to_parquet(tmp_path / "fee_signal_class.parquet")
    args = ["redispatch", "--results", str(tmp_path), "--redispatch", str(tmp_path / "rd.csv")]
    result = CliRunner().invoke(app, [*args, "--network", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "0 hours with a fee signal" in result.output
