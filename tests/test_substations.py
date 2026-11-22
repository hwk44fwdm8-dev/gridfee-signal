"""The committed substation list of the substation-level network."""

from pathlib import Path

import pandas as pd

from gridfee.network import jao

NETWORKS = Path(__file__).parents[1] / "data" / "networks"


def test_substation_list_is_consistent():
    subs = pd.read_csv(NETWORKS / "desub" / "substations.csv", comment="#")
    regions = pd.read_csv(NETWORKS / "de22" / "zone_nuts2.csv", comment="#")
    assert subs["substation_id"].is_unique and subs["name"].is_unique
    assert subs["substation_id"].str.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*").all()
    assert (subs["has_220kv"] | subs["has_380kv"]).all()
    assert subs["lat"].between(47.2, 55.1).all() and subs["lon"].between(5.8, 15.1).all()
    zone = regions.set_index("nuts2")["zone_id"]
    assert (subs["nuts2"].map(zone) == subs["report_zone"]).all()
    assert set(subs["nuts2"]) == set(regions["nuts2"])
    assert subs.loc[subs["position_basis"] != "neighbours", "osm_id"].notna().all()


def test_transformer_substation_names():
    assert jao.transformer_substation("TR Audorf/S T422") == "Audorf/S"
    assert jao.transformer_substation("TR Eula 220/400 401") == "Eula"
    assert jao.transformer_substation("TR / 380-220kV / Kuehmoos Trafo 401") == "Kuehmoos"
    assert jao.transformer_substation("PST Wuergau T442B") == "Wuergau"
    assert jao.voltage_level(231) == 220 and jao.voltage_level(410) == 380


def test_buses_and_branches_refer_to_the_substations():
    subs = pd.read_csv(NETWORKS / "desub" / "substations.csv", comment="#")
    buses = pd.read_csv(NETWORKS / "desub" / "buses.csv", comment="#", keep_default_na=False)
    branches = pd.read_csv(NETWORKS / "desub" / "branches.csv", comment="#")
    german = buses[buses["substation_id"] != ""]
    assert buses["zone_id"].is_unique and branches["line_id"].is_unique
    assert set(german["substation_id"]) == set(subs["substation_id"])
    assert (
        german["zone_id"] == german["substation_id"] + "-" + german["voltage_kv"].astype(str)
    ).all()
    assert set(branches["from_zone"]) | set(branches["to_zone"]) <= set(buses["zone_id"])
    transformers = branches[branches["kind"] == "transformer"]
    stem = transformers["from_zone"].str.removesuffix("-380")
    assert (stem + "-220" == transformers["to_zone"]).all()
    # Every substation keeps at least one bus in the connected network.
    connected = german[german["connected"].astype(str) == "True"]
    assert set(connected["substation_id"]) >= set(
        subs.loc[subs["kind"] == "substation", "substation_id"]
    )
