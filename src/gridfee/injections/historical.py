"""Historical injections: split the series of each TSO zone over its zones.

Zonal demand is the load of the TSO zone times the zone's population share; zonal
wind and solar feed-in is the TSO zone's profile times the zone's capacity share.
Shares are fractions of the TSO zone and sum to 1 within it. Where no shares are
given, every zone of a TSO zone gets the same share and is flagged as a
placeholder: such results show the mechanics only, not where power is produced
or used.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from gridfee.network.schema import Zone, ZoneKind

logger = logging.getLogger(__name__)

DEMAND = "demand"
SHARE_COLUMNS = ["quantity", "tso_zone", "zone_id", "share", "placeholder"]
_SHARE_SUM_TOLERANCE = 1e-6


class EnergyBalanceError(ValueError):
    """Zonal series do not add up to the series of their TSO zone."""


@dataclass(frozen=True)
class ZonalInjections:
    """Hourly MW per zone. ``net_injection_mw`` is feed-in minus demand."""

    demand_mw: pd.DataFrame
    res_mw: dict[str, pd.DataFrame]
    shares: pd.DataFrame

    @property
    def net_injection_mw(self) -> pd.DataFrame:
        return sum(self.res_mw.values(), -self.demand_mw)

    @property
    def placeholder_quantities(self) -> list[str]:
        """Quantities (``demand`` or a technology) that use any placeholder share."""
        flagged = self.shares[self.shares["placeholder"]]
        return sorted(flagged["quantity"].unique())


def read_shares(path: str | Path, quantity_column: str | None = None) -> pd.DataFrame:
    """Read ``zone_id,share`` rows, with a leading quantity column if one is named.

    Population shares have the columns ``zone_id,share``; capacity shares have
    ``technology,zone_id,share``. Lines starting with ``#`` are comments.
    """
    columns = ([quantity_column] if quantity_column else []) + ["zone_id", "share"]
    frame = pd.read_csv(path, comment="#", dtype={"zone_id": str})
    if sorted(frame.columns) != sorted(columns):
        raise ValueError(f"{path}: expected the columns {columns}, got {list(frame.columns)}")
    if quantity_column:
        frame = frame.rename(columns={quantity_column: "quantity"})
    else:
        frame["quantity"] = DEMAND
    return frame[["quantity", "zone_id", "share"]]


def resolve_shares(
    zones: Sequence[Zone],
    quantity: str,
    tso_zones: Sequence[str],
    given: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Shares of one quantity for every zone of the listed TSO zones.

    ``given`` holds ``zone_id`` and ``share``. A TSO zone none of whose zones are in
    ``given`` gets uniform placeholder shares. A TSO zone that is only partly
    covered, or whose shares do not sum to 1, is an error.
    """
    internal = [zone for zone in zones if zone.kind is ZoneKind.INTERNAL]
    known = {zone.zone_id for zone in internal}
    values: dict[str, float] = {}
    if given is not None:
        if given["zone_id"].duplicated().any():
            raise ValueError(f"{quantity} shares: a zone is listed more than once")
        unknown = sorted(set(given["zone_id"]) - known)
        if unknown:
            raise ValueError(f"{quantity} shares: unknown zones {unknown}")
        values = dict(zip(given["zone_id"], given["share"].astype(float), strict=True))
        bad = sorted(z for z, share in values.items() if not (np.isfinite(share) and share >= 0))
        if bad:
            raise ValueError(f"{quantity} shares: must be finite and not negative, see {bad}")

    rows = []
    for tso in tso_zones:
        members = [zone.zone_id for zone in internal if zone.tso_zone == tso]
        if not members:
            raise ValueError(f"{quantity}: no zone belongs to TSO zone {tso!r}")
        covered = [zone_id for zone_id in members if zone_id in values]
        if not covered:
            logger.warning(
                "%s in %s: no shares given, using uniform PLACEHOLDER shares over %d zones",
                quantity,
                tso,
                len(members),
            )
            rows += [(quantity, tso, zone_id, 1.0 / len(members), True) for zone_id in members]
            continue
        if len(covered) != len(members):
            missing = sorted(set(members) - set(covered))
            raise ValueError(f"{quantity} shares in {tso}: missing zones {missing}")
        total = sum(values[zone_id] for zone_id in members)
        if abs(total - 1.0) > _SHARE_SUM_TOLERANCE:
            raise ValueError(f"{quantity} shares in {tso} sum to {total:.6f}, not 1")
        rows += [(quantity, tso, zone_id, values[zone_id], False) for zone_id in members]
    return pd.DataFrame(rows, columns=SHARE_COLUMNS)


def build_historical_injections(
    zones: Sequence[Zone],
    tso_load_mw: pd.DataFrame,
    tso_res_mw: Mapping[str, pd.DataFrame],
    population_shares: pd.DataFrame | None = None,
    capacity_shares: pd.DataFrame | None = None,
    balance_tolerance: float = 1e-6,
) -> ZonalInjections:
    """Split TSO-zone series (hours x TSO zones, MW) over the zones.

    ``population_shares`` and ``capacity_shares`` are frames as returned by
    :func:`read_shares`; ``None`` means uniform placeholders. The energy balance of
    every TSO zone is checked before the result is returned.
    """
    for technology, frame in tso_res_mw.items():
        if not frame.index.equals(tso_load_mw.index):
            raise ValueError(f"{technology} and load do not cover the same hours")
    if capacity_shares is not None:
        unknown = sorted(set(capacity_shares["quantity"]) - set(tso_res_mw))
        if unknown:
            raise ValueError(f"capacity shares for technologies without data: {unknown}")

    zone_ids = [zone.zone_id for zone in zones if zone.kind is ZoneKind.INTERNAL]
    without_load = sorted(
        {z.tso_zone for z in zones if z.kind is ZoneKind.INTERNAL} - set(tso_load_mw.columns)
    )
    if without_load:
        raise ValueError(f"no load series for TSO zones {without_load}")
    share_frames = []

    def split(quantity: str, tso_mw: pd.DataFrame, given: pd.DataFrame | None) -> pd.DataFrame:
        if tso_mw.isna().any().any():
            raise ValueError(f"{quantity}: TSO-zone series contain missing values")
        shares = resolve_shares(zones, quantity, list(tso_mw.columns), given)
        share_frames.append(shares)
        zonal = pd.DataFrame(0.0, index=tso_mw.index, columns=zone_ids)
        for row in shares.itertuples():
            zonal[row.zone_id] = tso_mw[row.tso_zone] * row.share
        return zonal

    def for_quantity(frame: pd.DataFrame | None, quantity: str) -> pd.DataFrame | None:
        if frame is None:
            return None
        subset = frame[frame["quantity"] == quantity]
        return subset if len(subset) else None

    demand = split(DEMAND, tso_load_mw, for_quantity(population_shares, DEMAND))
    res = {
        technology: split(technology, frame, for_quantity(capacity_shares, technology))
        for technology, frame in tso_res_mw.items()
    }
    result = ZonalInjections(demand, res, pd.concat(share_frames, ignore_index=True))
    balance = check_energy_balance(zones, result, tso_load_mw, tso_res_mw, balance_tolerance)
    if not balance["passed"].all():
        failed = balance[~balance["passed"]]
        raise EnergyBalanceError(f"energy balance violated:\n{failed.to_string(index=False)}")
    return result


def check_energy_balance(
    zones: Sequence[Zone],
    injections: ZonalInjections,
    tso_load_mw: pd.DataFrame,
    tso_res_mw: Mapping[str, pd.DataFrame],
    tolerance: float = 1e-6,
) -> pd.DataFrame:
    """Compare, per TSO zone and quantity, the zonal sum with the TSO-zone series.

    One row per TSO zone and quantity: both energies in TWh, the largest hourly
    difference in MW and ``passed``, which is true when that difference is at most
    ``tolerance`` times the largest hourly TSO-zone value (and at most ``tolerance``
    MW when the series is all zero). A TSO zone without a series for a technology
    must have no zonal feed-in of it.
    """
    tso_of = {zone.zone_id: zone.tso_zone for zone in zones if zone.kind is ZoneKind.INTERNAL}
    tso_zones = sorted(set(tso_of.values()))
    quantities = {DEMAND: (injections.demand_mw, tso_load_mw)}
    for technology, frame in tso_res_mw.items():
        quantities[technology] = (injections.res_mw[technology], frame)

    rows = []
    for quantity, (zonal, tso_mw) in quantities.items():
        if not zonal.index.equals(tso_mw.index):
            raise EnergyBalanceError(f"{quantity}: zonal and TSO-zone series cover different hours")
        if zonal.isna().any().any() or tso_mw.isna().any().any():
            raise EnergyBalanceError(f"{quantity}: series contain missing values")
        zonal_by_tso = zonal.T.groupby(zonal.columns.map(tso_of)).sum().T
        for tso in tso_zones:
            zonal_sum = zonal_by_tso.get(tso, pd.Series(0.0, index=zonal.index))
            source = tso_mw.get(tso, pd.Series(0.0, index=zonal.index))
            difference = float((zonal_sum - source).abs().max())
            scale = max(float(source.abs().max()), 1.0)
            rows.append(
                {
                    "tso_zone": tso,
                    "quantity": quantity,
                    "tso_twh": float(source.sum()) / 1e6,
                    "zonal_twh": float(zonal_sum.sum()) / 1e6,
                    "max_abs_diff_mw": difference,
                    "passed": bool(difference <= tolerance * scale),
                }
            )
    return pd.DataFrame(rows)
