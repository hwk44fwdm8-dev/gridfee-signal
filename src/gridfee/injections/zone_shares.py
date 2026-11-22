"""Zone shares from public data: population (Eurostat) and capacity (MaStR).

The functions here turn amounts per zone into the share files that
``gridfee injections`` and ``gridfee backcast`` read. Downloading and reading the
sources is done by ``scripts/extract_mastr.py`` and ``scripts/build_zone_shares.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from gridfee.network.schema import Zone, ZoneKind


class ZoneShareError(ValueError):
    """The source data cannot be turned into zone shares."""


def zone_of_ags(ags: pd.Series, prefix_to_zone: Mapping[str, str]) -> pd.Series:
    """Zone of each municipality key (AGS), by its longest listed prefix.

    Keys that match no prefix, and missing keys, give a missing zone.
    """
    keys = ags.astype("string").str.strip()
    zone = pd.Series(pd.NA, index=ags.index, dtype="string")
    for length in sorted({len(prefix) for prefix in prefix_to_zone}):
        found = keys.str[:length].map(prefix_to_zone)
        zone = zone.where(found.isna(), found.astype("string"))
    return zone


def fill_zone_from_district(
    zone: pd.Series, state: pd.Series, district: pd.Series
) -> tuple[pd.Series, list[tuple[str, str]]]:
    """Fill missing zones from units of the same state and district that have one.

    MaStR's public export withholds the municipality key of large units but gives
    the district (Landkreis) by name. A district whose other units lie in exactly
    one zone gives that zone; one whose units lie in several zones, or that has
    no other unit, stays missing. Returns the zones and the (state, district)
    pairs found in several zones.
    """
    known = pd.DataFrame({"state": state, "district": district, "zone": zone}).dropna()
    zones_per_district = known.groupby(["state", "district"])["zone"].nunique()
    lookup = (
        known.drop_duplicates(["state", "district"])
        .set_index(["state", "district"])["zone"]
        .loc[zones_per_district[zones_per_district == 1].index]
    )
    keys = pd.MultiIndex.from_arrays([state, district])
    found = pd.Series(lookup.reindex(keys).to_numpy(), index=zone.index, dtype="string")
    ambiguous = [tuple(pair) for pair in zones_per_district[zones_per_district > 1].index]
    return zone.where(zone.notna(), found), ambiguous


def in_operation(units: pd.DataFrame, date: str | pd.Timestamp) -> pd.Series:
    """Whether each unit was in operation on ``date``.

    Uses ``Inbetriebnahmedatum`` (commissioning) and
    ``DatumEndgueltigeStilllegung`` (final shutdown). A unit without a
    commissioning date was never in operation.
    """
    day = pd.Timestamp(date)
    start = pd.to_datetime(units["Inbetriebnahmedatum"], errors="coerce")
    end = pd.to_datetime(units["DatumEndgueltigeStilllegung"], errors="coerce")
    return (start <= day) & (end.isna() | (end > day))


def shares_within_tso(amount: pd.Series, zones: Sequence[Zone]) -> pd.DataFrame:
    """Each zone's share of the amount of its TSO zone (columns ``zone_id``, ``share``).

    ``amount`` is indexed by zone id; zones it does not list count as 0. A TSO
    zone whose amount is 0 in total is left out, so that it is reported as a
    placeholder downstream instead of getting made-up shares.
    """
    internal = [zone for zone in zones if zone.kind is ZoneKind.INTERNAL]
    unknown = sorted(set(amount.index) - {zone.zone_id for zone in internal})
    if unknown:
        raise ZoneShareError(f"amounts for unknown zones {unknown}")
    if (amount < 0).any() or amount.isna().any():
        raise ZoneShareError("amounts must be >= 0 and not missing")
    frame = pd.DataFrame(
        {
            "zone_id": [zone.zone_id for zone in internal],
            "tso_zone": [zone.tso_zone for zone in internal],
        }
    )
    frame["amount"] = frame["zone_id"].map(amount).fillna(0.0)
    total = frame.groupby("tso_zone")["amount"].transform("sum")
    frame = frame[total > 0]
    return pd.DataFrame(
        {"zone_id": frame["zone_id"], "share": frame["amount"] / total[total > 0]}
    ).reset_index(drop=True)


def national_shares(amount: pd.Series, zones: Sequence[Zone]) -> pd.DataFrame:
    """Each internal zone's share of the national amount (``zone_id``, ``share``)."""
    internal = [zone.zone_id for zone in zones if zone.kind is ZoneKind.INTERNAL]
    unknown = sorted(set(amount.index) - set(internal))
    if unknown:
        raise ZoneShareError(f"amounts for unknown zones {unknown}")
    values = amount.reindex(internal).fillna(0.0)
    if (values < 0).any() or values.sum() <= 0:
        raise ZoneShareError("amounts must be >= 0 and not all 0")
    return pd.DataFrame({"zone_id": internal, "share": (values / values.sum()).to_numpy()})
