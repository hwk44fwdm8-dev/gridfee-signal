import sys
from pathlib import Path

import pandas as pd
import pytest

from gridfee.injections.plants import (
    PlantError,
    capacity_by_substation,
    distance_km,
    locate_units,
    nearest_substation,
    plant_number_positions,
    reference_positions,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_plant_table import CONNECTIONS, PLANT_LIST_POSITIONS, build_table, grid_connections  # noqa: E402
from place_plants import place, with_plant_list_positions  # noqa: E402

PLANTS = ROOT / "data" / "plants" / "plants_2023.csv"
DESUB = ROOT / "data" / "networks" / "desub"


def units(rows):
    columns = ["lat", "lon", "ags", "postcode", "state", "district", "plant_number"]
    return pd.DataFrame(rows, columns=columns)


def locate(frame, **extra):
    reference = units(
        [
            [50.0, 8.0, "06001", "60000", "Hessen", "Kreis A", None],
            [50.2, 8.2, "06001", "60000", "Hessen", "Kreis A", None],
            [50.4, 8.4, "06001", "60001", "Hessen", "Kreis A", None],
            [51.0, 9.0, "06002", "61000", "Hessen", "Kreis B", None],
        ]
    )
    return locate_units(
        frame,
        municipalities=reference_positions(reference, "ags"),
        postcodes=reference_positions(reference, "postcode"),
        districts=reference_positions(reference, ["state", "district"]),
        **extra,
    )


def test_distance_of_one_degree_of_latitude():
    assert distance_km(50.0, 8.0, 51.0, 8.0) == pytest.approx(111.2, abs=0.1)


def test_each_unit_takes_the_first_basis_that_applies():
    frame = units(
        [
            [50.1, 8.1, "06001", "60000", "Hessen", "Kreis A", None],
            [None, None, "06001", "60000", "Hessen", "Kreis A", "BNA0001a"],
            [None, None, "06001", "61000", "Hessen", "Kreis B", None],
            [None, None, None, "61000", "Hessen", "Kreis A", None],
            [None, None, None, None, "Hessen", "Kreis A", None],
            [None, None, None, None, "Hessen", "Kreis C", None],
        ]
    )
    blocks = pd.DataFrame({"lat": [50.3], "lon": [8.3]}, index=["BNA0001a"])
    found = locate(frame, plant_blocks=blocks)
    assert list(found["location_basis"]) == [
        "coordinates",
        "plant_list",
        "municipality",
        "postcode",
        "district",
        "missing",
    ]
    assert list(found["lat"][:5]) == [50.1, 50.3, 50.2, 51.0, 50.2]
    assert pd.isna(found["lat"][5]) and pd.isna(found["lon"][5])


def test_coordinates_outside_germany_or_far_from_the_municipality_are_not_used():
    frame = units(
        [
            [8.0, 50.0, "06001", None, None, None, None],  # latitude and longitude swapped
            [53.0, 13.0, "06001", None, None, None, None],  # valid, but 400 km from 06001
            [53.0, 13.0, None, None, None, None, None],  # nothing to check it against
        ]
    )
    found = locate(frame)
    assert list(found["location_basis"]) == ["municipality", "municipality", "coordinates"]
    assert list(found["lat"]) == [50.2, 50.2, 53.0]


def test_a_plant_number_matches_its_block_then_its_plant():
    listed = pd.DataFrame(
        {
            "id": ["BNA0001a", "BNA0001b", "BNA0002a", "BNA0002b", None],
            "lat": [50.0, 50.02, 50.0, 52.0, 49.0],
            "lon": [8.0, 8.0, 8.0, 8.0, 8.0],
        }
    )
    blocks, sites = plant_number_positions(listed)
    assert blocks.loc["BNA0001b", "lat"] == 50.02
    assert sites.loc["BNA0001", "lat"] == pytest.approx(50.01)
    assert "BNA0002" not in sites.index  # its blocks are 220 km apart
    frame = units(
        [
            [None, None, None, None, None, None, "BNA0001b"],
            [None, None, None, None, None, None, "BNA0001c"],
            [None, None, None, None, None, None, "BNA0002c"],
        ]
    )
    found = locate(frame, plant_blocks=blocks, plant_sites=sites)
    assert list(found["location_basis"]) == ["plant_list", "plant_list", "missing"]
    assert list(found["lat"][:2]) == pytest.approx([50.02, 50.01])


SUBSTATIONS = pd.DataFrame({"substation_id": ["A", "B"], "lat": [50.0, 52.0], "lon": [8.0, 8.0]})


def plants(rows):
    frame = pd.DataFrame(
        rows, columns=["plant_id", "technology", "capacity_mw", "zone_de22", "lat", "lon"]
    )
    return frame


def test_plants_go_to_the_nearest_substation():
    frame = plants([["p1", "gas", 10.0, "z1", 50.5, 8.0], ["p2", "gas", 5.0, "z1", 51.6, 8.0]])
    placement = nearest_substation(frame, SUBSTATIONS)
    assert list(placement["substation_id"]) == ["A", "B"]
    assert placement["distance_km"][0] == pytest.approx(55.6, abs=0.1)


def test_offshore_wind_goes_to_its_grid_connection_however_far():
    frame = plants(
        [
            ["p1", "wind_offshore", 10.0, "z1", 50.1, 8.0],
            ["p2", "wind_offshore", 5.0, "z1", 50.1, 8.0],
        ]
    ).assign(grid_connection=["B", None])
    placement = place(frame, SUBSTATIONS)
    assert list(placement["substation_id"]) == ["B", "A"]
    assert list(placement["placement_basis"]) == ["grid_connection", "nearest"]
    assert placement["distance_km"][0] == pytest.approx(211.3, abs=0.1)
    with pytest.raises(PlantError, match="not in the substation list"):
        place(frame.assign(grid_connection=["C", None]), SUBSTATIONS)


def test_grid_connections_are_looked_up_by_location_for_offshore_wind_only():
    units = pd.DataFrame(
        {
            "LokationMaStRNummer": ["L1", "L2", "L3", "L1"],
            "technology": ["wind_offshore", "wind_offshore", "wind_offshore", "wind_onshore"],
        }
    )
    connections = pd.DataFrame({"location_id": ["L1", "L2"], "substation_id": ["A", None]})
    found = grid_connections(units, connections)
    assert found.iloc[0] == "A"
    assert found.iloc[1:].isna().all()


def test_offshore_connections_name_substations_of_the_list_and_cover_the_farms():
    connections = pd.read_csv(CONNECTIONS, comment="#")
    assert connections["location_id"].is_unique
    substations = pd.read_csv(DESUB / "substations.csv", comment="#")
    ids = set(substations.loc[substations["kind"] == "substation", "substation_id"])
    assert set(connections["substation_id"].dropna()) <= ids
    # One connection system ends at one substation.
    known = connections[connections["system"] != "AC"]
    assert (known.groupby("system")["substation_id"].nunique() == 1).all()
    table, _ = repository_tables()
    offshore = table[table["technology"] == "wind_offshore"]
    connected = offshore["grid_connection"].notna()
    assert set(offshore.loc[connected, "grid_connection"]) <= ids
    assert table.loc[table["technology"] != "wind_offshore", "grid_connection"].isna().all()
    # All but alpha ventus, whose substation is not in the list.
    assert offshore.loc[~connected, "capacity_mw"].sum() == pytest.approx(60.0, abs=0.5)


def test_a_plant_without_position_follows_its_technology_in_its_zone_then_in_germany():
    frame = plants(
        [
            ["p1", "gas", 10.0, "z1", 50.1, 8.0],
            ["p2", "gas", 30.0, "z2", 51.9, 8.0],
            ["p3", "gas", 7.0, "z1", None, None],
            ["p4", "gas", 2.0, "z3", None, None],
            ["p5", "gas", 1.0, None, None, None],
        ]
    )
    placement = place(frame, SUBSTATIONS)
    assert list(placement["substation_id"]) == ["A", "B", "A", "B", "B"]
    assert list(placement["placement_basis"]) == [
        "nearest",
        "nearest",
        "largest_of_zone_de22",
        "largest_of_technology",
        "largest_of_technology",
    ]
    table = capacity_by_substation(frame, placement)
    assert table["capacity_mw"].sum() == pytest.approx(frame["capacity_mw"].sum())
    assert dict(zip(table["substation_id"], table["capacity_mw"], strict=True)) == {
        "A": 17.0,
        "B": 33.0,
    }


def test_placement_rejects_plants_left_without_a_substation_and_bad_substation_lists():
    frame = plants([["p1", "gas", 10.0, "z1", None, None]])
    with pytest.raises(PlantError, match="no substation"):
        capacity_by_substation(frame, place(frame, SUBSTATIONS))
    with pytest.raises(PlantError, match="unique"):
        nearest_substation(frame, pd.concat([SUBSTATIONS, SUBSTATIONS]))
    with pytest.raises(PlantError, match="position"):
        nearest_substation(frame, SUBSTATIONS.assign(lat=[50.0, None]))


def test_small_units_are_summed_per_place_and_capacity_is_kept():
    frame = pd.DataFrame(
        {
            "plant_id": ["u1", "u2", "u3", "u4"],
            "name": ["a", "b", "c", "d"],
            "technology": ["solar", "solar", "solar", "gas"],
            "capacity_mw": [0.01, 0.02, 5.0, 0.5],
            "state": "Hessen",
            "district": "Kreis A",
            "ags": ["06001", "06001", "06001", None],
            "zone": "z1",
        }
    )
    position = pd.DataFrame(
        {
            "lat": [50.0, 50.0, 50.1, None],
            "lon": [8.0, 8.0, 8.1, None],
            "location_basis": ["municipality", "municipality", "coordinates", "missing"],
        }
    )
    table = build_table(frame, position, min_mw=1.0)
    assert list(table["plant_id"]) == ["small-gas-Hessen/Kreis_A", "small-solar-06001", "u3"]
    assert list(table["units"]) == [1, 2, 1]
    assert table["capacity_mw"].sum() == pytest.approx(5.53)
    assert table["plant_id"].is_unique


# The data files of the repository


def repository_tables():
    table = pd.read_csv(PLANTS, comment="#", dtype={"ags": str})
    by_zone = pd.read_csv(ROOT / "data" / "shares" / "capacity_by_zone.csv", comment="#")
    return table, by_zone.set_index("zone_id").drop(columns=["population", "other"])


def test_summed_offshore_units_keep_their_grid_connection():
    units = pd.DataFrame(
        {
            "plant_id": ["u1", "u2", "u3"],
            "name": ["a", "b", "c"],
            "technology": "wind_offshore",
            "capacity_mw": [5.0, 5.0, 6.0],
            "state": "Ausschließliche Wirtschaftszone",
            "district": None,
            "ags": None,
            "zone": "z1",
            "grid_connection": ["A", "A", "B"],
        }
    )
    position = pd.DataFrame(
        {"lat": 54.0, "lon": 7.0, "location_basis": "coordinates"}, index=[0, 1, 2]
    )
    table = build_table(units, position, min_mw=10.0)
    assert table["plant_id"].is_unique
    mw = table.groupby("grid_connection")["capacity_mw"].sum()
    assert mw.to_dict() == {"A": 10.0, "B": 6.0}


def test_plant_table_has_one_row_per_plant_and_a_position_or_missing():
    table, _ = repository_tables()
    assert table["plant_id"].is_unique
    assert (table["capacity_mw"] >= 0).all()
    missing = table["location_basis"] == "missing"
    # The plant list's positions state no licence and are not in the repository.
    unpublished = missing | (table["location_basis"] == "plant_list")
    assert table.loc[unpublished, ["lat", "lon"]].isna().all().all()
    assert table.loc[~unpublished, ["lat", "lon"]].notna().all().all()
    assert table.loc[~unpublished, "lat"].between(47.0, 56.5).all()
    assert table.loc[~unpublished, "lon"].between(3.0, 15.5).all()
    summary = pd.read_csv(PLANTS.with_name("plants_2023_summary.csv"), comment="#")
    total = summary.set_index("technology").loc["total"]
    assert total["capacity_mw"] == pytest.approx(table["capacity_mw"].sum(), abs=0.5)
    assert total["missing"] == pytest.approx(table.loc[missing, "capacity_mw"].sum(), abs=0.5)
    assert total["units"] == table["units"].sum()


def test_plant_table_agrees_with_the_zone_shares_per_technology_and_zone():
    table, by_zone = repository_tables()
    assert set(table["technology"]) == set(by_zone.columns)
    zoned = table.dropna(subset=["zone_de22"])
    mine = zoned.pivot_table(
        index="zone_de22", columns="technology", values="capacity_mw", aggfunc="sum"
    )
    mine = mine.reindex(index=by_zone.index, columns=by_zone.columns).fillna(0.0)
    # capacity_by_zone.csv is rounded to 0.1 MW, the plants to 0.0001 MW
    assert (mine - by_zone).abs().max().max() < 0.1
    # Plants the zone shares leave out for lack of a zone are a negligible part.
    unzoned = table.loc[table["zone_de22"].isna(), "capacity_mw"].sum()
    assert unzoned < 0.001 * table["capacity_mw"].sum()


def test_substation_capacity_keeps_the_totals_per_technology():
    table, _ = repository_tables()
    capacity = pd.read_csv(DESUB / "capacity_by_substation.csv", comment="#")
    substations = pd.read_csv(DESUB / "substations.csv", comment="#")
    real = substations.loc[substations["kind"] == "substation", "substation_id"]
    assert set(capacity["substation_id"]) <= set(real)
    assert not capacity.duplicated(["substation_id", "technology"]).any()
    placed = capacity.groupby("technology")["capacity_mw"].sum()
    plants_mw = table.groupby("technology")["capacity_mw"].sum()
    assert set(placed.index) == set(plants_mw.index)
    assert (placed - plants_mw).abs().max() < 0.5
    placement = pd.read_csv(DESUB / "plant_substations.csv", comment="#")
    assert list(placement["plant_id"]) == list(table["plant_id"])
    assert placement["substation_id"].notna().all()


def test_plant_list_positions_are_filled_in_from_the_local_file(tmp_path):
    plants = pd.DataFrame(
        {
            "plant_id": ["a", "b"],
            "location_basis": ["plant_list", "coordinates"],
            "lat": [None, 50.0],
            "lon": [None, 8.0],
        }
    )
    with pytest.raises(SystemExit, match="not committed"):
        with_plant_list_positions(plants, tmp_path / "none.csv")
    path = tmp_path / "positions.csv"
    path.write_text("# local\nplant_id,lat,lon\nc,1,2\n")
    with pytest.raises(SystemExit, match="not of this plant table"):
        with_plant_list_positions(plants, path)
    path.write_text("# local\nplant_id,lat,lon\na,51.5,9.5\n")
    filled = with_plant_list_positions(plants, path)
    assert filled[["lat", "lon"]].to_numpy().tolist() == [[51.5, 9.5], [50.0, 8.0]]
    assert plants["lat"].isna().iloc[0]


def test_placement_files_are_what_the_script_builds_from_the_lists():
    if not PLANT_LIST_POSITIONS.exists():
        pytest.skip("needs the plant list positions of a local build_plant_table.py run")
    table, _ = repository_tables()
    table = with_plant_list_positions(table, PLANT_LIST_POSITIONS)
    substations = pd.read_csv(DESUB / "substations.csv", comment="#")
    substations = substations[substations["kind"] == "substation"].reset_index(drop=True)
    built = capacity_by_substation(table, place(table, substations))
    stored = pd.read_csv(DESUB / "capacity_by_substation.csv", comment="#")
    assert list(built["substation_id"]) == list(stored["substation_id"])
    assert list(built["technology"]) == list(stored["technology"])
    assert built["capacity_mw"].to_numpy() == pytest.approx(stored["capacity_mw"], abs=1e-3)
