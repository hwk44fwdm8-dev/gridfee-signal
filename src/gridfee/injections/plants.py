"""Where generation units are, and which substation each one is placed on.

MaStR's public export gives coordinates for most units of 30 kW and more, only a
municipality key for smaller ones, and neither for most large conventional units.
``locate_units`` fills the gaps step by step and says which step gave each position;
``nearest_substation`` places positions on a substation list. Reading the sources is
done by ``scripts/build_plant_table.py`` and ``scripts/place_plants.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EARTH_RADIUS_KM = 6371.0
# Positions outside this box are not in Germany or its waters and count as missing.
LAT_RANGE, LON_RANGE = (47.0, 56.5), (3.0, 15.5)
# A position further than this from the other units of its municipality is taken
# to be a typing error, and the municipality's position is used instead.
MAX_KM_FROM_MUNICIPALITY = 50.0
BASES = ("coordinates", "plant_list", "municipality", "postcode", "district")


class PlantError(ValueError):
    """The plant data cannot be located or placed."""


def distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance; the arguments broadcast like numpy arrays."""
    lat1, lon1, lat2, lon2 = (
        np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2)
    )
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def valid_position(lat: pd.Series, lon: pd.Series) -> pd.Series:
    return lat.between(*LAT_RANGE) & lon.between(*LON_RANGE)


def plant_number_positions(plants: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Positions by BNetzA plant number: per block (``BNA0124a``) and per plant (``BNA0124``).

    ``plants`` has the columns ``id``, ``lat`` and ``lon``. The plant's position is the
    mean of its blocks and is only given where they lie within 5 km of it.
    """
    known = plants.dropna(subset=["id", "lat", "lon"])
    blocks = known.groupby("id")[["lat", "lon"]].mean()
    site = known.assign(site=known["id"].str.replace(r"(?<=\d)[A-Za-z]+$", "", regex=True))
    sites = site.groupby("site")[["lat", "lon"]].mean()
    spread = distance_km(
        site["lat"], site["lon"], site["site"].map(sites["lat"]), site["site"].map(sites["lon"])
    )
    apart = set(site.loc[spread > 5.0, "site"])
    return blocks, sites[~sites.index.isin(apart)]


def reference_positions(units: pd.DataFrame, by: str | list[str]) -> pd.DataFrame:
    """Median position (``lat``, ``lon``) of the units with a valid one, per ``by``."""
    ok = valid_position(units["lat"], units["lon"])
    keys = [by] if isinstance(by, str) else by
    return units[ok].dropna(subset=keys).groupby(keys)[["lat", "lon"]].median()


def locate_units(
    units: pd.DataFrame,
    *,
    municipalities: pd.DataFrame,
    postcodes: pd.DataFrame,
    districts: pd.DataFrame,
    plant_blocks: pd.DataFrame | None = None,
    plant_sites: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Position of each unit: columns ``lat``, ``lon`` and ``location_basis``.

    ``units`` has ``lat``, ``lon`` (may be missing), ``ags``, ``postcode``, ``state``,
    ``district`` and ``plant_number``. The reference tables are indexed by municipality
    key, postcode and (state, district), as ``reference_positions`` returns them.
    The first of these that applies gives the position:

    coordinates   the unit's own, if valid and near its municipality
    plant_list    the block, else the plant, of its BNetzA number in a plant list
    municipality  median of the located units with the same municipality key
    postcode      the same by postcode
    district      the same by state and district (large units; tens of km off)

    A unit that none applies to has no position and the basis ``missing``.
    """
    lat = pd.to_numeric(units["lat"], errors="coerce")
    lon = pd.to_numeric(units["lon"], errors="coerce")
    own = valid_position(lat, lon)
    home_lat = units["ags"].map(municipalities["lat"])
    home_lon = units["ags"].map(municipalities["lon"])
    far = (
        own
        & home_lat.notna()
        & (
            distance_km(lat, lon, home_lat.fillna(0.0), home_lon.fillna(0.0))
            > MAX_KM_FROM_MUNICIPALITY
        )
    )
    result = pd.DataFrame(
        {"lat": lat.where(own & ~far), "lon": lon.where(own & ~far), "location_basis": "missing"},
        index=units.index,
    )
    result.loc[own & ~far, "location_basis"] = "coordinates"

    def fill(found_lat: pd.Series, found_lon: pd.Series, basis: str) -> None:
        use = (result["location_basis"] == "missing") & found_lat.notna() & found_lon.notna()
        result.loc[use, "lat"] = found_lat[use]
        result.loc[use, "lon"] = found_lon[use]
        result.loc[use, "location_basis"] = basis

    number = units["plant_number"]
    if plant_blocks is not None:
        fill(number.map(plant_blocks["lat"]), number.map(plant_blocks["lon"]), "plant_list")
    if plant_sites is not None:
        site = number.str.replace(r"(?<=\d)[A-Za-z]+$", "", regex=True)
        fill(site.map(plant_sites["lat"]), site.map(plant_sites["lon"]), "plant_list")
    fill(home_lat, home_lon, "municipality")
    fill(
        units["postcode"].map(postcodes["lat"]), units["postcode"].map(postcodes["lon"]), "postcode"
    )
    keys = pd.MultiIndex.from_arrays([units["state"], units["district"]])
    found = districts.reindex(keys)
    fill(
        pd.Series(found["lat"].to_numpy(), index=units.index),
        pd.Series(found["lon"].to_numpy(), index=units.index),
        "district",
    )
    return result


def nearest_substation(plants: pd.DataFrame, substations: pd.DataFrame) -> pd.DataFrame:
    """The nearest substation of each plant: ``substation_id`` and ``distance_km``.

    Both frames have ``lat`` and ``lon``; ``substations`` also ``substation_id``. A
    plant without a position gets no substation.
    """
    if substations.empty or substations[["lat", "lon"]].isna().any().any():
        raise PlantError("substations need a position each, and at least one is needed")
    if substations["substation_id"].duplicated().any():
        raise PlantError("substation ids must be unique")
    located = plants["lat"].notna() & plants["lon"].notna()
    ids = pd.Series(pd.NA, index=plants.index, dtype="string")
    km = pd.Series(np.nan, index=plants.index)
    sub_lat, sub_lon = substations["lat"].to_numpy(), substations["lon"].to_numpy()
    lat, lon = plants.loc[located, "lat"].to_numpy(), plants.loc[located, "lon"].to_numpy()
    for start in range(0, len(lat), 20000):
        part = slice(start, start + 20000)
        d = distance_km(lat[part, None], lon[part, None], sub_lat[None, :], sub_lon[None, :])
        best = d.argmin(axis=1)
        index = plants.index[located][part]
        ids.loc[index] = substations["substation_id"].to_numpy()[best]
        km.loc[index] = d[np.arange(len(best)), best]
    return pd.DataFrame({"substation_id": ids, "distance_km": km})


def capacity_by_substation(plants: pd.DataFrame, placement: pd.DataFrame) -> pd.DataFrame:
    """MW per substation and technology; plants without a substation are an error."""
    if placement["substation_id"].isna().any():
        raise PlantError(
            f"{int(placement['substation_id'].isna().sum())} plants have no substation"
        )
    table = (
        plants.assign(substation_id=placement["substation_id"])
        .groupby(["substation_id", "technology"], as_index=False)["capacity_mw"]
        .sum()
    )
    return table.sort_values(["substation_id", "technology"], ignore_index=True)
