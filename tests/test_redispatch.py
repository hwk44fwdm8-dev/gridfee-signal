import pandas as pd
import pytest
from typer.testing import CliRunner

from gridfee.cli import app
from gridfee.validation import (
    ValidationError,
    compare_with_redispatch,
    hourly_redispatch,
    read_redispatch,
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


def _model():
    hours = pd.date_range("2023-01-01 00:00", periods=3, freq="h", tz="Europe/Berlin")
    signal = pd.DataFrame({"N": [0.0, 20.0, 0.0], "S": [0.0, -30.0, 0.0]}, index=hours)
    prices = pd.DataFrame({"N-S": [0.0, 50.0, 0.0], "N-PL": [0.0, 0.0, 5.0]}, index=hours)
    return signal, prices


def test_compare(tmp_path):
    signal, prices = _model()
    redispatch = hourly_redispatch(read_redispatch(_write(tmp_path / "rd.csv")))
    result = compare_with_redispatch(signal, prices, {"N": "50Hertz", "S": "TenneT"}, redispatch)

    assert result.hours.loc["signal", "hours"] == 1
    assert result.hours.loc["signal", "hours_with_redispatch"] == 1
    assert result.hours.loc["signal", "mean_redispatch_mwh"] == pytest.approx(180.5)
    assert result.hours.loc["neutral", "mean_redispatch_mwh"] == pytest.approx(25)
    assert result.deciles["signal_hours"].sum() == 1
    # Only the line between two German zones is listed.
    assert result.lines.index.tolist() == ["all signal hours", "N-S"]
    north = result.directions.loc[("N-S", "50Hertz")]
    assert north["mean_net_redispatch_mwh"] == pytest.approx(-100)
    assert (north["hours_compared"], north["hours_same_direction"]) == (1, 1)
    south = result.directions.loc[("N-S", "TenneT")]
    assert (south["hours_compared"], south["hours_same_direction"]) == (1, 1)

    opposite = compare_with_redispatch(-signal, prices, {"N": "50Hertz", "S": "TenneT"}, redispatch)
    assert opposite.directions["hours_same_direction"].sum() == 0


def test_compare_needs_overlapping_hours(tmp_path):
    signal, prices = _model()
    redispatch = hourly_redispatch(read_redispatch(_write(tmp_path / "rd.csv", ROWS[3:])))
    with pytest.raises(ValidationError, match="covers none"):
        compare_with_redispatch(signal, prices, {"N": "50Hertz", "S": "TenneT"}, redispatch)


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
    assert "| N-S | 50Hertz | 20.00 | -100.00 | 1 | 1 |" in report
