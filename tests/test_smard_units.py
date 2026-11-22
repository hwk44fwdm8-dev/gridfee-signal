"""Per-unit SMARD collector, tested against a fake HTTP session shaped like the real API."""

from __future__ import annotations

import pandas as pd
import pytest
from typer.testing import CliRunner

from gridfee.cli import app
from gridfee.data import smard, smard_units
from gridfee.data.smard import SmardClient, SmardError

from .test_smard import FIRST, FakeResponse, FakeSession, ms


def block(unit_id: str, production_id: int | None, resource: str, power: float) -> dict:
    return {
        "id": unit_id,
        "name": f"KW-Name.{unit_id}",
        "blockCode": f"EIC-{unit_id}",
        "productionId": production_id,
        "commissioning": 2000,
        "status": "KW-Status.In Betrieb",
        "power": power,
        "resource": f"KW-Energieträger.{resource}",
    }


METADATA = {
    "plants": [
        {
            "name": "KW-Name.PLANT-A",
            "code": "PLANT-A",
            "regionId": "50Hertz",
            "postalCode": "03130",
            "city": "KW-Stadt.Spremberg",
            "country": "KW-Land.Deutschland",
            "coordinates": [51.5, 14.4],
            "blocks": [
                block("SEE2", 3002, "Braunkohle", 750.0),
                block("SEE1", 3001, "Braunkohle", 750.0),
                block("BNA9", None, "Braunkohle", 500.0),  # no output series
            ],
        },
        {
            "name": "KW-Name.PLANT-B",
            "code": "PLANT-B",
            "regionId": "Amprion",
            "postalCode": "50735",
            "city": "KW-Stadt.Köln",
            "country": "KW-Land.Deutschland",
            "coordinates": [51.0, 7.0],
            "blocks": [
                block("SEE3", 3003, "Erdgas", 400.0),
                block("SEE4", 3004, "Wärme", 150.0),
                block("SEE6", 3003, "Wärme", 200.0),  # shares the series of SEE3
                block("SEE5", 3005, "Erdgas", 100.0),  # series without values
            ],
        },
    ]
}
TRANSLATIONS = {
    "KW-Name.PLANT-A": "Kraftwerk Schwarze Pumpe",
    "KW-Name.SEE1": "Block A",
    "KW-Energieträger.Braunkohle": "Braunkohle",
}


class UnitSession(FakeSession):
    """Also serves the power plant list; unit 3005 has an empty index."""

    def __init__(self, missing: set[int] = frozenset(), metadata: dict = METADATA) -> None:
        super().__init__(FIRST, 54, missing)
        self.metadata = metadata

    def get(self, url: str, timeout: float) -> FakeResponse:
        if url == f"{smard_units.APP_URL}/{smard_units.METADATA_NAME}":
            return FakeResponse(self.metadata)
        if url == f"{smard_units.APP_URL}/{smard_units.TRANSLATION_NAME}":
            return FakeResponse(TRANSLATIONS)
        if "/3005/" in url:
            self.urls.append(url)
            return FakeResponse({"timestamps": []})
        return super().get(url, timeout)


def test_unit_list_has_only_units_with_an_output_series():
    units = smard_units.read_unit_list(SmardClient(session=UnitSession()))
    assert list(units.columns) == smard_units.UNIT_COLUMNS
    assert units["unit_id"].tolist() == ["SEE1", "SEE2", "SEE3", "SEE4", "SEE5", "SEE6"]
    assert units["series_unit_id"].tolist() == ["SEE1", "SEE2", "SEE3", "SEE4", "SEE5", "SEE3"]
    first = units.iloc[0]
    assert (first["plant"], first["unit"]) == ("Kraftwerk Schwarze Pumpe", "Block A")
    assert (first["production_id"], first["tso"], first["unit_eic"]) == (
        3001,
        "50Hertz",
        "EIC-SEE1",
    )
    assert (first["latitude"], first["longitude"]) == (51.5, 14.4)
    # Untranslated keys keep their text.
    assert units.iloc[2]["city"] == "Köln" and units.iloc[2]["resource"] == "Erdgas"
    assert units["fuel"].tolist() == [
        "lignite",
        "lignite",
        "gas",
        "not_compared",
        "gas",
        "not_compared",
    ]


def test_repeated_ids_in_the_list_are_an_error():
    metadata = {"plants": [METADATA["plants"][0], METADATA["plants"][0]]}
    with pytest.raises(SmardError, match="repeats unit_id"):
        smard_units.read_unit_list(SmardClient(session=UnitSession(metadata=metadata)))


def test_collect_units_year_writes_tables(tmp_path):
    session = UnitSession(missing={ms("2023-06-01 12:00")})
    tables = smard_units.collect_units_year(
        2023, tmp_path / "raw", SmardClient(tmp_path / "cache", session)
    )
    generation, units = tables["unit_generation"], tables["units"]

    # The unit without values is listed but has no column.
    assert generation.columns.tolist() == ["SEE1", "SEE2", "SEE3", "SEE4"]
    assert len(generation) == 8760 and str(generation.index.tz) == "Europe/Berlin"
    # The missing hour stays missing.
    assert generation.isna().sum().tolist() == [1, 1, 1, 1]
    assert pd.isna(generation.loc[pd.Timestamp("2023-06-01 12:00", tz="Europe/Berlin"), "SEE1"])
    # The unit that shares a series has no hours or energy of its own.
    assert units["hours"].iloc[:5].tolist() == [8759, 8759, 8759, 8759, 0]
    assert pd.isna(units["hours"].iloc[5])
    assert units["energy_mwh"].iloc[0] == generation["SEE1"].sum()
    assert units["energy_mwh"].iloc[4] == 0 and pd.isna(units["energy_mwh"].iloc[5])
    # Only the ids of the list are requested, each in its TSO's region.
    requested = {tuple(url.split("/")[-3:-1]) for url in session.urls if "chart_data" in url}
    assert requested == {
        ("3001", "50Hertz"),
        ("3002", "50Hertz"),
        ("3003", "Amprion"),
        ("3004", "Amprion"),
        ("3005", "Amprion"),
    }

    read = smard.read_table(tmp_path / "raw", "unit_generation", 2023)
    pd.testing.assert_frame_equal(read, generation, check_freq=False)
    assert smard_units.read_units(tmp_path / "raw", 2023)["unit_id"].tolist() == [
        "SEE1",
        "SEE2",
        "SEE3",
        "SEE4",
        "SEE5",
        "SEE6",
    ]
    with pytest.raises(SmardError, match="gridfee collect-units"):
        smard_units.read_units(tmp_path / "raw", 2022)


def test_fuel_coverage_compares_with_national_generation():
    hours = pd.date_range("2023-01-01", periods=4, freq="h", tz="Europe/Berlin", name="hour")
    generation = pd.DataFrame(
        {
            "SEE1": [100.0, 100.0, None, 100.0],
            "SEE2": [50.0, 50.0, 50.0, 250.0],
            "SEE3": [10.0, 10.0, 10.0, 10.0],
            "SEE4": [5.0, 5.0, 5.0, 5.0],
        },
        index=hours,
    )
    units = pd.DataFrame(
        {
            "unit_id": ["SEE1", "SEE2", "SEE3", "SEE4", "SEE5", "SEE6"],
            "series_unit_id": ["SEE1", "SEE2", "SEE3", "SEE4", "SEE5", "SEE3"],
            "fuel": ["lignite", "lignite", "gas", "not_compared", "gas", "not_compared"],
            "power_mw": [750.0, 750.0, 400.0, 150.0, 100.0, 200.0],
        }
    )
    national = pd.DataFrame(
        {"lignite": [300.0] * 4, "gas": [40.0] * 4, "nuclear": [20.0] * 4}, index=hours
    )
    coverage = smard_units.fuel_coverage(generation, units, national).set_index("fuel")

    assert coverage.index.tolist() == ["lignite", "gas", "nuclear", "not_compared"]
    lignite = coverage.loc["lignite"]
    assert (lignite["units"], lignite["capacity_mw"]) == (2, 1500.0)
    assert lignite["unit_twh"] == pytest.approx(700 / 1e6)
    assert lignite["share"] == pytest.approx(700 / 1200)
    # In the last hour the two units exceed the national series by 50 MW.
    assert lignite["max_hourly_excess_mw"] == 50.0
    assert lignite["missing_unit_hours"] == 1
    # The series of SEE3 counts as gas, with the unit that shares it.
    assert (coverage.loc["gas", "units"], coverage.loc["gas", "capacity_mw"]) == (2, 600.0)
    assert coverage.loc["gas", "share"] == pytest.approx(0.25)
    assert coverage.loc["gas", "max_hourly_excess_mw"] == 0.0
    assert coverage.loc["nuclear", "units"] == 0 and coverage.loc["nuclear", "share"] == 0.0
    assert coverage.loc["not_compared", "units"] == 1
    assert pd.isna(coverage.loc["not_compared", "share"])

    with pytest.raises(ValueError, match="same hours"):
        smard_units.fuel_coverage(generation.iloc[:3], units, national)
    with pytest.raises(ValueError, match="not in the unit list"):
        smard_units.fuel_coverage(generation.assign(SEE9=1.0), units, national)


def test_collect_units_command(tmp_path, monkeypatch):
    session = UnitSession()
    monkeypatch.setattr(smard, "SmardClient", lambda cache: SmardClient(cache, session=session))
    out = tmp_path / "raw"
    args = ["collect-units", "--year", "2023", "--out", str(out), "--cache", str(tmp_path / "c")]

    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Wrote 4 hourly output series" in result.output
    assert "gridfee collect --year 2023" in result.output
    assert not (out / "smard_unit_coverage_2023.csv").exists()

    national = smard.read_table(out, "unit_generation", 2023)[["SEE1"]] * 4
    national.columns = ["lignite"]
    national.to_csv(out / "smard_generation_de_2023.csv")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    coverage = pd.read_csv(out / "smard_unit_coverage_2023.csv").set_index("fuel")
    assert coverage.loc["lignite", "share"] == pytest.approx(0.5)
    assert coverage.loc["not_compared", "units"] == 1
