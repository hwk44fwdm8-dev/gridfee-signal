"""Demand per substation and sector, and hourly load built from it.

Annual electricity use per municipality and sector (households, commerce/trade/services
"cts", industry) comes from the DemandRegio project; ``scripts/build_sector_demand.py``
places it on the substations and writes ``data/networks/desub/sector_demand.csv``.
Hourly load per substation is the observed load of its TSO area, split by what the
sectors at each substation use in that hour. See docs/DATA.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from gridfee.injections.population import _nearest

SECTORS = ["households", "cts", "industry"]
SEASONS = ["winter", "transition", "summer"]
DAY_TYPES = ["workday", "saturday", "sunday"]
TABLE_COLUMNS = ["season", "day_type", "hour", "households", "cts"]
# Working hours of industry (DemandRegio "shift profiles"): the local hours of one, two
# or three shifts and the day types worked. Outside them a plant draws LOW of its load.
SHIFT_HOURS = {"s1": (8.0, 16.5), "s2": (6.0, 23.0), "s3": (0.0, 24.0)}
SHIFT_DAYS = {
    "wt": ["workday"],
    "wt_sa": ["workday", "saturday"],
    "wt_sa_so": ["workday", "saturday", "sunday"],
}
SHIFT_CLASSES = [f"{shift}_{days}" for shift in SHIFT_HOURS for days in SHIFT_DAYS]
INDUSTRY_COLUMNS = [f"industry_{name}" for name in SHIFT_CLASSES]
CONTINUOUS = "industry_s3_wt_sa_so"
LOW = 0.4
# Shift class per industry branch (WZ 2008 division), as in DemandRegio's disaggregator.
SHIFT_OF_BRANCH = {
    5: "s3_wt_sa", 6: "s3_wt_sa_so", 7: "s3_wt_sa", 8: "s3_wt_sa", 9: "s3_wt_sa",
    10: "s2_wt", 11: "s3_wt", 12: "s3_wt_sa", 13: "s2_wt", 14: "s2_wt", 15: "s2_wt_sa",
    16: "s2_wt_sa", 17: "s3_wt_sa_so", 18: "s3_wt_sa_so", 19: "s3_wt_sa_so",
    20: "s3_wt_sa_so", 21: "s3_wt_sa_so", 22: "s2_wt_sa", 23: "s3_wt_sa_so",
    24: "s3_wt_sa_so", 25: "s3_wt", 26: "s2_wt", 27: "s2_wt_sa", 28: "s2_wt", 29: "s3_wt",
    30: "s3_wt_sa_so", 31: "s1_wt_sa", 32: "s3_wt_sa_so", 33: "s2_wt_sa",
}  # fmt: skip
LOCAL_TIME = "Europe/Berlin"


class DemandError(ValueError):
    """Sector demand or its profiles are not usable."""


def public_holidays(year: int) -> set[date]:
    """The nine public holidays that all German states share."""
    a, b, c = year % 19, year // 100, year % 100
    d, e, f = b // 4, b % 4, (b + 8) // 25
    h = (19 * a + b - d - (b - f + 1) // 3 + 15) % 30
    k = (32 + 2 * e + 2 * (c // 4) - h - c % 4) % 7
    m = (a + 11 * h + 22 * k) // 451
    easter = date(year, (h + k - 7 * m + 114) // 31, (h + k - 7 * m + 114) % 31 + 1)
    return {
        date(year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        date(year, 5, 1),
        easter + timedelta(days=39),
        easter + timedelta(days=50),
        date(year, 10, 3),
        date(year, 12, 25),
        date(year, 12, 26),
    }


def day_classes(local: pd.DatetimeIndex) -> pd.DataFrame:
    """``season`` and ``day_type`` of every timestamp of a local-time index.

    Seasons are the meteorological ones DemandRegio uses (winter December to February,
    summer June to August); a public holiday counts as a Sunday.
    """
    month = local.month.to_numpy()
    season = np.where(
        (month == 12) | (month <= 2), "winter", np.where((month >= 6) & (month <= 8), "summer", "")
    )
    season = np.where(season == "", "transition", season)
    holidays = set().union(*(public_holidays(int(y)) for y in np.unique(local.year)))
    weekday = local.dayofweek.to_numpy()
    free = (weekday == 6) | np.fromiter((d in holidays for d in local.date), bool, len(local))
    day_type = np.where(free, "sunday", np.where(weekday == 5, "saturday", "workday"))
    return pd.DataFrame({"season": season, "day_type": day_type}, index=local)


def household_dynamisation(day_of_year: np.ndarray) -> np.ndarray:
    """Seasonal factor of the H0 standard load profile for the day of the year (from 1)."""
    d = np.asarray(day_of_year, dtype=float)
    return -3.92e-10 * d**4 + 3.2e-7 * d**3 - 7.02e-5 * d**2 + 2.1e-3 * d + 1.24


def profile_tables_from_year(households: np.ndarray, cts: np.ndarray, year: int) -> pd.DataFrame:
    """The 3 seasons x 3 day types x 24 hours behind two hourly profiles of one year.

    ``households`` and ``cts`` are local-time hourly series of ``year`` without the
    clock-change hours (24 values per day), as DemandRegio publishes its H0 and G0 load
    curves. The household series is divided by the dynamisation factor first. Each table
    is scaled to a mean of 1 over its 216 values.
    """
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    classes = day_classes(days)
    key = pd.MultiIndex.from_frame(classes)
    tables = {}
    for name, series, factor in [
        ("households", households, household_dynamisation(days.dayofyear.to_numpy())),
        ("cts", cts, np.ones(len(days))),
    ]:
        if len(series) != 24 * len(days):
            raise DemandError(f"{name}: expected {24 * len(days)} hours, got {len(series)}")
        daily = pd.DataFrame(np.asarray(series).reshape(len(days), 24) / factor[:, None], index=key)
        table = daily.groupby(level=[0, 1]).median()
        tables[name] = table / table.to_numpy().mean()
    rows = [
        (season, day_type, hour, tables["households"].loc[(season, day_type), hour])
        + (tables["cts"].loc[(season, day_type), hour],)
        for season in SEASONS
        for day_type in DAY_TYPES
        for hour in range(24)
    ]
    return pd.DataFrame(rows, columns=TABLE_COLUMNS)


def load_profile_tables(path: str | Path) -> pd.DataFrame:
    """Read ``data/demand/profile_tables.csv``."""
    tables = pd.read_csv(path, comment="#")
    if list(tables.columns) != TABLE_COLUMNS or len(tables) != 216:
        raise DemandError(f"expected 216 rows with columns {TABLE_COLUMNS}")
    if tables[["season", "day_type", "hour"]].duplicated().any():
        raise DemandError("a season, day type and hour is listed twice")
    return tables


def standard_profiles(snapshots: pd.DatetimeIndex, tables: pd.DataFrame) -> pd.DataFrame:
    """Relative hourly use of ``households`` and ``cts`` at the UTC ``snapshots``.

    Looked up by local season, day type and hour; households are multiplied by the
    dynamisation factor of the day. Values are relative: only their ratio matters.
    """
    if snapshots.tz is None:
        raise DemandError("snapshots must be timezone-aware")
    local = snapshots.tz_convert(LOCAL_TIME)
    keys = day_classes(local).assign(hour=local.hour).reset_index(drop=True)
    looked_up = keys.merge(tables, on=["season", "day_type", "hour"], how="left")
    profiles = looked_up[["households", "cts"]].set_axis(snapshots)
    profiles["households"] *= household_dynamisation(local.dayofyear.to_numpy())
    return profiles


def shift_profiles(snapshots: pd.DatetimeIndex) -> pd.DataFrame:
    """Relative hourly use of industry per shift class at the UTC ``snapshots``.

    One column per class of ``INDUSTRY_COLUMNS``: 1 during the class's working hours
    (the worked share of an hour that is only partly worked), ``LOW`` outside them and
    on days it does not work. 24 and 31 December count as Saturdays, public holidays
    as Sundays.
    """
    if snapshots.tz is None:
        raise DemandError("snapshots must be timezone-aware")
    local = snapshots.tz_convert(LOCAL_TIME)
    day_type = day_classes(local)["day_type"].to_numpy()
    eve = (local.month == 12) & local.day.isin([24, 31]) & (day_type == "workday")
    day_type = np.where(eve, "saturday", day_type)
    hour = local.hour.to_numpy()
    out = {}
    for shift, (start, end) in SHIFT_HOURS.items():
        worked = np.clip(np.minimum(hour + 1, end) - np.maximum(hour, start), 0.0, 1.0)
        for days, types in SHIFT_DAYS.items():
            share = np.where(np.isin(day_type, types), worked, 0.0)
            out[f"industry_{shift}_{days}"] = LOW + (1 - LOW) * share
    return pd.DataFrame(out, index=snapshots)


def read_sector_demand(path: str | Path) -> pd.DataFrame:
    """Read ``zone_id`` and the annual MWh per sector and per shift class of industry.

    Without the ``INDUSTRY_COLUMNS`` all industry is taken to run continuously; with
    them they must add up to ``industry``. Other columns are dropped.
    """
    frame = pd.read_csv(path, comment="#", dtype={"zone_id": str})
    missing = [column for column in ["zone_id", *SECTORS] if column not in frame]
    if missing:
        raise DemandError(f"{path}: columns {missing} are missing")
    if not set(INDUSTRY_COLUMNS) & set(frame.columns):
        frame = frame.assign(**{CONTINUOUS: frame["industry"]})
    for column in INDUSTRY_COLUMNS:
        if column not in frame:
            frame[column] = 0.0
    frame = frame[["zone_id", *SECTORS, *INDUSTRY_COLUMNS]]
    if frame["zone_id"].duplicated().any():
        raise DemandError(f"{path}: a zone is listed twice")
    values = frame.drop(columns="zone_id")
    if values.isna().any().any() or (values < 0).any().any():
        raise DemandError(f"{path}: annual use must be given and not negative")
    gap = (frame[INDUSTRY_COLUMNS].sum(axis=1) - frame["industry"]).abs()
    if (gap > 1e-6 * frame["industry"] + len(INDUSTRY_COLUMNS)).any():
        raise DemandError(f"{path}: the shift classes do not add up to industry")
    return frame


def sector_demand_split(
    tso_of: Mapping[str, str],
    tso_load_mw: pd.DataFrame,
    sector_demand: pd.DataFrame,
    tables: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Demand shares and hourly factors per zone from annual use per sector.

    ``tso_of`` gives the TSO zone of every internal zone, ``tso_load_mw`` the observed
    hourly load per TSO zone (UTC index), ``sector_demand`` the annual MWh per sector of
    the zones that have load (:func:`read_sector_demand`). Returns the shares
    (``quantity``, ``zone_id``, ``share`` of the TSO zone's annual use) and the factors
    (hours x zones, mean 1): a zone's relative hourly use, the mean of the profiles of
    households, cts and the shift classes of its industry, weighted by their annual
    use. ``build_historical_injections`` multiplies the two and scales every hour to the
    observed load of the TSO zone, so a zone with much continuous industry gets a
    flatter curve than a residential one.
    """
    unknown = sorted(set(sector_demand["zone_id"]) - set(tso_of))
    if unknown:
        raise DemandError(f"sector demand for unknown zones: {unknown[:5]}")
    parts = ["households", "cts", *INDUSTRY_COLUMNS]
    annual = sector_demand.set_index("zone_id")[parts].reindex(list(tso_of)).fillna(0.0)
    tso = pd.Series(tso_of)
    snapshots = tso_load_mw.index
    shapes = pd.concat([standard_profiles(snapshots, tables), shift_profiles(snapshots)], axis=1)
    shapes = (shapes / shapes.mean())[parts]
    shares = pd.Series(0.0, index=annual.index)
    factors = pd.DataFrame(0.0, index=snapshots, columns=annual.index)
    for tso_zone, local in annual.groupby(tso):
        use = local.sum(axis=1)
        if use.sum() <= 0:
            raise DemandError(f"no sector demand in TSO zone {tso_zone}")
        if tso_zone not in tso_load_mw:
            raise DemandError(f"no load series for TSO zone {tso_zone}")
        loaded = use[use > 0].index
        mix = local.loc[loaded].div(use[loaded], axis=0)
        factors[loaded] = shapes.to_numpy() @ mix.to_numpy().T
        shares[local.index] = use / use.sum()
    table = pd.DataFrame(
        {"quantity": "demand", "zone_id": shares.index, "share": shares.to_numpy()}
    )
    return table, factors


def spread_over_cells(
    cells: pd.DataFrame, municipal: pd.DataFrame, columns: Sequence[str] = SECTORS
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Annual MWh per sector on every population cell, and where each amount was placed.

    ``cells`` has ``ags`` (8-digit municipality key) and ``population``; ``municipal`` has
    ``ags`` and the MWh per sector (``columns``). A municipality's use is spread over its cells by
    population. One without cells (a key the boundaries do not know) is spread over the
    cells of its district (first 5 digits), failing that over its state (first 2).
    Returns the cells with the sector columns added, and MWh per sector and level.
    """
    if municipal["ags"].duplicated().any():
        raise DemandError("a municipality is listed twice")
    out = cells.assign(**dict.fromkeys(columns, 0.0))
    left = municipal.set_index("ags")[list(columns)]
    placed = {}
    for level, digits in [("municipality", 8), ("district", 5), ("state", 2)]:
        area = out["ags"].str[:digits]
        population = out.groupby(area)["population"].sum()
        totals = left.groupby(left.index.str[:digits]).sum()
        here = totals[totals.index.isin(population.index)]
        weight = out["population"] / area.map(population)
        for sector in columns:
            out[sector] += (weight * area.map(here[sector])).fillna(0.0)
        placed[level] = here.sum()
        left = left[~left.index.str[:digits].isin(population.index)]
    if len(left):
        raise DemandError(f"no cells in the states of municipalities {sorted(left.index)[:5]}")
    return out, pd.DataFrame(placed).T


def place_on_substations(
    cells: pd.DataFrame, substations: pd.DataFrame, columns: Sequence[str] = SECTORS
) -> pd.DataFrame:
    """Annual MWh per sector and substation: every cell goes to the nearest one.

    As for population (``gridfee.injections.population``): the nearest of
    ``substations`` (``substation_id``, ``tso``, ``nuts2``, ``lat``, ``lon``) in the
    cell's own NUTS2 region; substations at one position share its cells equally.
    ``load_share`` is the substation's share of the annual use of the ``SECTORS``.
    """
    without = sorted(set(cells["nuts2"]) - set(substations["nuts2"]))
    if without:
        raise DemandError(f"regions with demand but no substation: {without}")
    placed = pd.DataFrame(0.0, index=pd.Index(substations["substation_id"]), columns=list(columns))
    for region, part in cells.groupby("nuts2"):
        local = substations[substations["nuts2"] == region]
        sites = local[["lat", "lon"]].drop_duplicates()
        at_site = part[list(columns)].groupby(_nearest(part, sites)).sum()
        at_site = at_site.reindex(range(len(sites)), fill_value=0.0)
        site = local.merge(sites.assign(site=range(len(sites))), on=["lat", "lon"])
        sharing = site.groupby("site")["site"].transform("size").to_numpy()
        placed.loc[site["substation_id"]] = at_site.loc[site["site"]].to_numpy() / sharing[:, None]
    total = placed[SECTORS].sum(axis=1)
    return pd.DataFrame(
        {
            "substation_id": placed.index,
            "tso": substations["tso"].to_numpy(),
            "nuts2": substations["nuts2"].to_numpy(),
            **{sector: placed[sector].to_numpy() for sector in columns},
            "load_share": (total / total.sum()).to_numpy(),
        }
    )
