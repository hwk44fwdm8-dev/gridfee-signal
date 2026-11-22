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
