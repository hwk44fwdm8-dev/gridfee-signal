from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from gridfee.cli import app
from gridfee.validation import (
    ValidationError,
    hourly_redispatch_by_location,
    locate_plants,
    plant_locations,
    read_plant_table,
    read_redispatch,
    redispatch_coverage,
    render_coverage,
)

from .test_redispatch import _hour, _write

DATA = Path(__file__).parents[1] / "data"
TABLE = DATA / "redispatch" / "plants_2023.csv"
SUBSTATIONS = pd.DataFrame(
    {
        "substation_id": ["nord-w", "sued", "abzweig"],
        "name": ["Nord/W", "Süd", "Abzweig"],
        "kind": ["substation", "substation", "tee"],
        "report_zone": ["Z1", "Z2", "Z2"],
        "nuts2": ["R1", "R2", "R2"],
        "lat": [53.0, 48.0, 50.05],
        "lon": [10.0, 10.0, 10.0],
    }
)
PLANT_LIST = pd.DataFrame(
    {
        "id": ["P1", "P2", "P3", "P4"],
        "lat": [50.0, 50.2, 52.9, 48.1],
        "lon": [10.0, 10.0, 10.0, 10.0],
        # P1 names the far substation, P3 the near one, P4 one the list lacks.
        "network_node": ["UW Nord/W", None, "Nord/W", "Schaltanlage vor Ort"],
    }
)
COLUMNS = "plant,located_by,plant_list_ids,substation_id,basis,distance_km,reason,note"
ROWS = [
    "A,plant_list,P1 P2,,,,,",
    "B,same_site,P3,,,,,",
    "C,substation_name,,sued,,,,",
    "D,none,,,,,market,",
    "E,plant_list,P4,,,,,",
]


def _table(path, rows=ROWS):
    path.write_text("# comment\n" + "\n".join([COLUMNS, *rows]) + "\n", encoding="utf-8")
    return path


def test_locate_plants(tmp_path):
    located = locate_plants(read_plant_table(_table(tmp_path / "p.csv")), PLANT_LIST, SUBSTATIONS)
    by_plant = located.set_index("plant")
    # Mean position 50.1 N: the tee point is nearest but is not a substation, and the
    # two entries do not agree on a connection point.
    assert by_plant.loc["A", ["substation_id", "basis"]].tolist() == ["sued", "nearest"]
    assert by_plant.loc["A", "distance_km"] == pytest.approx(233.5, abs=0.1)
    assert by_plant.loc["B", ["substation_id", "basis"]].tolist() == ["nord-w", "network_node"]
    assert by_plant.loc["C", ["substation_id", "basis"]].tolist() == ["sued", "name"]
    assert by_plant.loc["D", ["substation_id", "basis"]].tolist() == ["", ""]
    assert by_plant.loc["E", ["substation_id", "basis"]].tolist() == ["sued", "nearest"]

    # A connection point further away than the limit is not used.
    far = PLANT_LIST.assign(network_node=["Nord/W", "Nord/W", None, None])
    assert locate_plants(located, far, SUBSTATIONS).set_index("plant").loc["A", "basis"] == (
        "nearest"
    )
    places = plant_locations(located, SUBSTATIONS)
    assert places.loc["B"].tolist() == ["nord-w", "Z1", "R1"]
    assert "D" not in places.index


def test_table_errors(tmp_path):
    path = tmp_path / "p.csv"
    with pytest.raises(ValidationError, match="listed twice"):
        read_plant_table(_table(path, [ROWS[0], ROWS[0]]))
    with pytest.raises(ValidationError, match="unknown located_by"):
        read_plant_table(_table(path, ["A,guess,,,,,,"]))
    with pytest.raises(ValidationError, match="needs a reason"):
        read_plant_table(_table(path, ["A,none,,,,,,"]))
    with pytest.raises(ValidationError, match="not found"):
        locate_plants(
            read_plant_table(_table(path, ["A,plant_list,P9,,,,,"])), PLANT_LIST, SUBSTATIONS
        )
    with pytest.raises(ValidationError, match="unknown substation"):
        locate_plants(
            read_plant_table(_table(path, ["A,substation_name,,west,,,,"])), PLANT_LIST, SUBSTATIONS
        )


def test_coverage_and_hourly_by_location(tmp_path):
    # The export of test_redispatch names plants A (150 MWh lowered), B (80.5 and 10
    # raised) and C (voltage-driven).
    measures = read_redispatch(_write(tmp_path / "rd.csv"))
    assert measures["plant"].tolist() == ["A", "B", "C", "B"]
    table = locate_plants(
        read_plant_table(_table(tmp_path / "p.csv", [ROWS[0], ROWS[2]])), PLANT_LIST, SUBSTATIONS
    )
    coverage = redispatch_coverage(measures, table)
    assert coverage.loc[("plant_list", ""), ["plants", "measures"]].tolist() == [1, 1]
    assert coverage.loc[("plant_list", ""), "share"] == pytest.approx(150 / 240.5)
    # B is not in the table.
    assert coverage.loc[("none", "not_in_table"), "gwh"] == pytest.approx(0.0905)
    assert redispatch_coverage(measures, table, None)["gwh"].sum() == pytest.approx(0.2495)
    assert "150" not in render_coverage(coverage) and "62.4%" in render_coverage(coverage)

    places = plant_locations(table, SUBSTATIONS)
    hourly = hourly_redispatch_by_location(measures, places)
    assert hourly.columns.tolist() == [("sued", -1)]
    assert hourly.loc[_hour("2023-01-01 00:00"), ("sued", -1)] == pytest.approx(100)
    zones = hourly_redispatch_by_location(
        measures, places, "report_zone", reason="Spannungsbedingter Redispatch"
    )
    assert zones.columns.tolist() == [("Z2", 1)]
    with pytest.raises(ValidationError, match="unknown level"):
        hourly_redispatch_by_location(measures, places, "tso")
    with pytest.raises(ValidationError, match="BETROFFENE_ANLAGE"):
        redispatch_coverage(measures.drop(columns="plant"), table)


def test_committed_table():
    table = read_plant_table(TABLE)
    substations = pd.read_csv(DATA / "networks" / "desub" / "substations.csv", comment="#")
    places = plant_locations(table, substations)
    assert len(table) == 286 and len(places) == 242
    assert places.notna().all().all()
    kind = substations.set_index("substation_id")["kind"]
    assert (kind[places["substation_id"]] == "substation").all()
    by_list = table["located_by"].isin(["plant_list", "same_site"])
    assert table.loc[by_list, "plant_list_ids"].str.fullmatch(r"BNA\d+[a-z]?( BNA\d+[a-z]?)*").all()
    assert table.loc[by_list, "basis"].isin(["network_node", "nearest"]).all()
    assert table.loc[by_list, "distance_km"].between(0, 40).all()
    assert (table.loc[~by_list & (table["located_by"] != "none"), "basis"] == "name").all()
    assumed = table["located_by"].isin(["same_site", "landing_point", "cluster_code"])
    assert table.loc[assumed, "note"].ne("").all()
    # A cluster code is the short name of a substation of the grid operator's area.
    assert places.loc["50H_AVA_CR_WOL"].tolist() == ["wolmirstedt", "DE05", "DEE0"]
    # Offshore wind that lands at Buettel is in Schleswig-Holstein's zone.
    assert places.loc["OWP UW Büttel", "report_zone"] == "DE07"


def test_cli(tmp_path):
    table = locate_plants(read_plant_table(_table(tmp_path / "p.csv")), PLANT_LIST, SUBSTATIONS)
    table.to_csv(tmp_path / "p.csv", index=False)
    SUBSTATIONS.to_csv(tmp_path / "s.csv", index=False)
    args = ["redispatch-plants", "--plants", str(tmp_path / "p.csv")]
    args += ["--substations", str(tmp_path / "s.csv")]
    result = CliRunner().invoke(app, [*args, "--redispatch", str(_write(tmp_path / "rd.csv"))])
    assert result.exit_code == 0, result.output
    assert "Located on a substation: 0.2 of 0.2 GWh (100.0%), 2 of 2 plant names." in result.output
    every = CliRunner().invoke(
        app, [*args, "--all-reasons", "--redispatch", str(tmp_path / "rd.csv")]
    )
    assert "3 of 3 plant names" in every.output
    missing = CliRunner().invoke(app, [*args, "--redispatch", str(tmp_path / "none.csv")])
    assert missing.exit_code == 1
