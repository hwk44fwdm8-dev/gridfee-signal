"""Hourly capacity factors of wind and solar per zone from weather data.

The factors only say how a TSO zone's observed feed-in is split over its zones
hour by hour: a zone's part is its capacity share times its factor, divided by
the sum over the TSO zone. Their absolute level therefore does not matter, only
how they differ between the zones of one TSO zone.

The power curve and the solar conversion are simple ASSUMPTIONS, not a model of
the installed plants.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

CUT_IN_M_S = 3.0
RATED_M_S = 12.0
CUT_OUT_M_S = 25.0
STANDARD_IRRADIANCE_W_M2 = 1000.0
_SEPARATOR = "/"


def wind_capacity_factor(speed_m_s):
    """Generic turbine: 0 below cut-in and from cut-out on, cubic up to rated, then 1."""
    speed = np.asarray(speed_m_s, dtype=float)
    ramp = (speed**3 - CUT_IN_M_S**3) / (RATED_M_S**3 - CUT_IN_M_S**3)
    factor = np.clip(ramp, 0.0, 1.0)
    return np.where(speed >= CUT_OUT_M_S, 0.0, factor)


def solar_capacity_factor(irradiance_w_m2):
    """Output proportional to the radiation on a horizontal plane, 1 at 1000 W/m2."""
    return np.clip(np.asarray(irradiance_w_m2, dtype=float) / STANDARD_IRRADIANCE_W_M2, 0.0, 1.0)


def zone_mean(cell_values: pd.DataFrame, cell_zone: pd.Series) -> pd.DataFrame:
    """Mean over the grid cells of each zone (hours x cells -> hours x zones).

    ``cell_zone`` gives the zone of each column of ``cell_values``; cells without
    a zone are left out.
    """
    zone = cell_zone.reindex(cell_values.columns)
    kept = zone.notna().to_numpy()
    return cell_values.loc[:, kept].T.groupby(zone[kept].to_numpy()).mean().T


def write_capacity_factors(
    factors: dict[str, pd.DataFrame], path: str | Path, comment: str = ""
) -> None:
    """Write hours x zones factors of several technologies to one CSV."""
    wide = pd.concat({tech: frame for tech, frame in factors.items()}, axis=1)
    wide.columns = [f"{tech}{_SEPARATOR}{zone}" for tech, zone in wide.columns]
    wide.index = wide.index.tz_convert("UTC").strftime("%Y-%m-%dT%H:%MZ")
    wide.index.name = "hour_utc"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        for line in comment.splitlines():
            handle.write(f"# {line}\n")
        wide.round(4).to_csv(handle)


def read_capacity_factors(path: str | Path) -> dict[str, pd.DataFrame]:
    """Read a file written by :func:`write_capacity_factors`.

    Returns, per technology, hours (UTC) x zones. Columns are named
    ``technology/zone_id``; lines starting with ``#`` are comments.
    """
    wide = pd.read_csv(path, comment="#", index_col="hour_utc")
    wide.index = pd.to_datetime(wide.index, utc=True)
    if wide.index.duplicated().any():
        raise ValueError(f"{path}: an hour is listed more than once")
    if wide.isna().any().any() or (wide < 0).any().any():
        raise ValueError(f"{path}: capacity factors must be >= 0 and not missing")
    factors: dict[str, dict[str, pd.Series]] = {}
    for column in wide.columns:
        tech, separator, zone = column.partition(_SEPARATOR)
        if not separator or not tech or not zone:
            raise ValueError(f"{path}: column {column!r} is not technology/zone_id")
        factors.setdefault(tech, {})[zone] = wide[column]
    return {tech: pd.DataFrame(columns) for tech, columns in factors.items()}
