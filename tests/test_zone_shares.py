"""Zone shares from amounts per zone, and the shipped share files."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from gridfee.injections import read_shares, resolve_shares
from gridfee.injections.zone_shares import (
    ZoneShareError,
    fill_zone_from_district,
    in_operation,
    national_shares,
    shares_within_tso,
    zone_of_ags,
)
from gridfee.network import Zone, load_zones

ROOT = Path(__file__).parents[1]
SHARES = ROOT / "data" / "shares"
ZONES = [
    Zone(zone_id="A", name="A", tso_zone="TenneT"),
    Zone(zone_id="B", name="B", tso_zone="TenneT"),
    Zone(zone_id="C", name="C", tso_zone="Amprion"),
    Zone(zone_id="X", name="X", kind="boundary", country="XX"),
]


def test_zone_of_ags_uses_the_longest_prefix():
    ags = pd.Series(["09162000", "05111000", "01001000", None, "99999999", " 0916"])

    zone = zone_of_ags(ags, {"091": "South", "05": "West", "051": "Rhine", "01": "North"})

    assert zone.tolist()[:3] == ["South", "Rhine", "North"]
    assert zone.isna().tolist() == [False, False, False, True, True, False]


def test_in_operation_uses_commissioning_and_final_shutdown():
    units = pd.DataFrame(
        {
            "Inbetriebnahmedatum": ["2020-01-01", "2023-07-01", "2023-07-02", None, "2001-05-05"],
            "DatumEndgueltigeStilllegung": [None, None, None, None, "2023-04-15"],
        }
    )

    assert in_operation(units, "2023-07-01").tolist() == [True, True, False, False, False]
    assert in_operation(units, "2023-01-01").tolist() == [True, False, False, False, True]


def test_shares_within_tso_sum_to_one_per_tso_zone():
    shares = shares_within_tso(pd.Series({"A": 30.0, "B": 10.0, "C": 5.0}), ZONES)

    assert shares.set_index("zone_id")["share"].to_dict() == {"A": 0.75, "B": 0.25, "C": 1.0}


def test_tso_zone_without_any_amount_is_left_out():
    shares = shares_within_tso(pd.Series({"A": 30.0}), ZONES)

    assert shares.set_index("zone_id")["share"].to_dict() == {"A": 1.0, "B": 0.0}


def test_shares_reject_unknown_zones_and_bad_amounts():
    with pytest.raises(ZoneShareError, match="unknown zones"):
        shares_within_tso(pd.Series({"X": 1.0}), ZONES)
    with pytest.raises(ZoneShareError, match=">= 0"):
        shares_within_tso(pd.Series({"A": -1.0}), ZONES)
    with pytest.raises(ZoneShareError, match="not all 0"):
        national_shares(pd.Series({"A": 0.0}), ZONES)


def test_national_shares_cover_every_internal_zone():
    shares = national_shares(pd.Series({"A": 1.0, "C": 3.0}), ZONES)

    assert shares.set_index("zone_id")["share"].to_dict() == {"A": 0.25, "B": 0.0, "C": 0.75}


def test_shipped_shares_are_complete_for_the_22_zones():
    zones = load_zones(ROOT / "data" / "networks" / "de22" / "zones.csv")
    tso_zones = ["50Hertz", "Amprion", "TenneT", "TransnetBW"]

    population = resolve_shares(
        zones, "demand", tso_zones, read_shares(SHARES / "population_shares.csv")
    )
    assert not population["placeholder"].any()
    capacity = read_shares(SHARES / "capacity_shares.csv", "technology")
    for technology in ("wind_onshore", "solar"):
        given = capacity[capacity["quantity"] == technology]
        assert not resolve_shares(zones, technology, tso_zones, given)["placeholder"].any()
    offshore = capacity[capacity["quantity"] == "wind_offshore"]
    assert set(offshore.loc[offshore["share"] > 0, "zone_id"]) == {"DE03", "DE07", "DE08"}
    residual = pd.read_csv(SHARES / "residual_shares.csv", comment="#")
    assert residual["zone_id"].tolist() == [z.zone_id for z in zones if z.tso_zone]
    assert residual["share"].sum() == pytest.approx(1.0, abs=1e-4)


def test_ags_prefixes_cover_every_nuts2_region_once():
    prefixes = pd.read_csv(SHARES / "ags_nuts2.csv", comment="#", dtype=str)
    regions = pd.read_csv(ROOT / "data" / "networks" / "de22" / "zone_nuts2.csv", comment="#")

    assert prefixes["ags_prefix"].is_unique
    assert sorted(prefixes["nuts2"]) == sorted(regions["nuts2"])


def test_fill_zone_from_district_uses_only_unambiguous_districts():
    zone = pd.Series(["Z1", None, "Z1", "Z2", None, None, "Z3", None], dtype="string")
    state = pd.Series(["s1", "s1", "s1", "s1", "s1", "s2", "s2", "s1"])
    district = pd.Series(["Neuss", "Neuss", "Split", "Split", "Split", "Neuss", "Other", None])

    filled, ambiguous = fill_zone_from_district(zone, state, district)

    assert filled.tolist()[:4] == ["Z1", "Z1", "Z1", "Z2"]
    assert filled.isna().tolist() == [False, False, False, False, True, True, False, True]
    assert ambiguous == [("s1", "Split")]
