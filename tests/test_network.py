import shutil
from pathlib import Path

import pytest

from gridfee.network import NetworkConfigError, ZoneKind, load_network

from .conftest import EXAMPLES

THREE_ZONE = EXAMPLES / "networks" / "three_zone"


def test_loads_zones_lines_and_tso_mapping(three_zone):
    assert three_zone.zone_ids == ["A", "B", "C", "X"]
    assert three_zone.internal_zone_ids == ["A", "B", "C"]
    assert three_zone.boundary_zone_ids == ["X"]
    assert three_zone.zone_to_tso == {"A": "50Hertz", "B": "TenneT", "C": "Amprion"}
    assert three_zone.zones_of_tso("TenneT") == ["B"]
    assert three_zone.zones_of_tso("TransnetBW") == []

    boundary = three_zone.zones[3]
    assert boundary.kind is ZoneKind.BOUNDARY
    assert boundary.country == "XX"
    assert boundary.tso_zone is None

    lines = three_zone.lines_frame()
    assert list(lines.index) == ["A-B", "B-C", "A-C", "C-X"]
    assert lines.loc["A-C", "capacity_mw"] == 50
    assert lines.loc["A-C", "susceptance_mw_per_rad"] == 100


def test_unknown_tso_query_fails(three_zone):
    with pytest.raises(KeyError):
        three_zone.zones_of_tso("Tennet")


def _network_dir(tmp_path: Path, zones: str | None = None, lines: str | None = None) -> Path:
    shutil.copytree(THREE_ZONE, tmp_path, dirs_exist_ok=True)
    if zones is not None:
        (tmp_path / "zones.csv").write_text(zones)
    if lines is not None:
        (tmp_path / "lines.csv").write_text(lines)
    return tmp_path


ZONES_HEADER = "zone_id,name,kind,tso_zone,country,lat,lon\n"
LINES_HEADER = "line_id,from_zone,to_zone,susceptance_mw_per_rad,capacity_mw\n"


@pytest.mark.parametrize(
    ("zones", "lines", "message"),
    [
        (ZONES_HEADER + "A,Zone A,internal,Tennet,,,\n", LINES_HEADER, "tso_zone"),
        (ZONES_HEADER + "A,Zone A,internal,,,,\n", LINES_HEADER, "tso_zone"),
        (
            ZONES_HEADER + "X,Abroad,boundary,TenneT,XX,,\n",
            LINES_HEADER,
            "must not have a tso_zone",
        ),
        (ZONES_HEADER + "X,Abroad,boundary,,,,\n", LINES_HEADER, "country"),
        (ZONES_HEADER + "A,Zone A,region,TenneT,,,\n", LINES_HEADER, "kind"),
        (ZONES_HEADER + "A,a,internal,TenneT,,,\nA,b,internal,TenneT,,,\n", None, "duplicate"),
        ("zone_id,name,tso\nA,Zone A,TenneT\n", None, "unknown columns ['tso']"),
        ("zone_id,kind\nA,internal\n", None, "missing columns ['name']"),
        (None, LINES_HEADER + "A-B,A,B,100,1000\nB-C,B,C,100,1000\n", "not connected"),
        (None, LINES_HEADER + "A-Z,A,Z,100,1000\n", "unknown zone 'Z'"),
        (None, LINES_HEADER + "A-A,A,A,100,1000\n", "to itself"),
        (None, LINES_HEADER + "A-B,A,B,0,1000\n", "susceptance_mw_per_rad"),
        (None, LINES_HEADER + "A-B,A,B,100,-5\n", "capacity_mw"),
        (None, LINES_HEADER + "A-B,A,B,100,\n", "capacity_mw"),
        (None, LINES_HEADER + "A-B,A,B,100,inf\n", "capacity_mw"),
        (
            None,
            LINES_HEADER + "L,A,B,100,1000\nL,B,C,100,1000\nM,C,X,100,1000\n",
            "duplicate line_id",
        ),
    ],
)
def test_rejects_invalid_config(tmp_path, zones, lines, message):
    with pytest.raises(NetworkConfigError) as err:
        load_network(_network_dir(tmp_path, zones, lines))
    assert message in str(err.value)


def test_missing_file_is_reported(tmp_path):
    (tmp_path / "zones.csv").write_text(ZONES_HEADER + "A,Zone A,internal,TenneT,,,\n")
    with pytest.raises(NetworkConfigError, match="lines.csv"):
        load_network(tmp_path)


def test_error_names_file_and_line(tmp_path):
    lines = LINES_HEADER + "A-B,A,B,100,1000\nB-C,B,C,abc,1000\n"
    with pytest.raises(NetworkConfigError, match=r"lines\.csv, line 3"):
        load_network(_network_dir(tmp_path, lines=lines))


def test_ids_keep_leading_zeros_and_parallel_lines_are_allowed(tmp_path):
    (tmp_path / "zones.csv").write_text(
        ZONES_HEADER + "01,One,internal,TenneT,,53.5,10.0\n02,Two,internal,Amprion,,,\n"
    )
    (tmp_path / "lines.csv").write_text(LINES_HEADER + "a,01,02,100,10\nb,01,02,200,20\n")
    network = load_network(tmp_path)
    assert network.zone_ids == ["01", "02"]
    assert network.line_ids == ["a", "b"]
    assert network.zones[0].lat == 53.5
    assert network.zones[1].lat is None


def test_error_line_number_counts_comments_and_blank_lines(tmp_path):
    lines = "# comment\n\n" + LINES_HEADER + "A-B,A,B,100,1000\n# note\n\nB-C,B,C,abc,1000\n"
    with pytest.raises(NetworkConfigError, match=r"lines\.csv, line 7"):
        load_network(_network_dir(tmp_path, lines=lines))


def test_rejects_extra_values_and_duplicate_columns(tmp_path):
    with pytest.raises(NetworkConfigError, match=r"line 2: 6 values for 5 columns"):
        load_network(_network_dir(tmp_path, lines=LINES_HEADER + "A-B,A,B,100,1000,7\n"))
    with pytest.raises(NetworkConfigError, match="at most once"):
        load_network(_network_dir(tmp_path, lines="line_id," + LINES_HEADER))


def test_quoted_values_and_empty_file(tmp_path):
    zones = ZONES_HEADER + 'A,"Zone A, north",internal,TenneT,,,\n'
    network = load_network(_network_dir(tmp_path, zones=zones, lines=LINES_HEADER))
    assert network.zones[0].name == "Zone A, north"
    with pytest.raises(NetworkConfigError, match="no header row"):
        load_network(_network_dir(tmp_path, zones="# only a comment\n"))
