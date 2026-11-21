"""Historical backcast: SMARD data and zone shares -> DC OPF -> fee signal.

What is observed and what is assumed:

- Zonal demand and wind/solar feed-in come from :mod:`gridfee.injections` (TSO-zone
  series times zone shares). Wind and solar cost nothing and can be curtailed.
- Neighbouring countries: SMARD's physical net export per neighbour is a load at
  the boundary zone in hours of export, and a free, curtailable feed-in there in
  hours of import. Where an observed flow is larger than the capacity of the
  lines to that neighbour in the network, it is cut to that capacity and the cut
  is reported: the flow did happen, so the line data is too low there.
- Everything else (conventional plants, biomass, hydro, pumped storage) is only
  known as a national total, not by zone. It is modelled as one "residual"
  generator per zone at one marginal cost. Its capacity is the zone's share of the
  year's highest national residual generation. Without a share file the shares
  are PLACEHOLDERS equal to the zone's share of demand, i.e. generation is
  assumed to sit where the load is.

The backcast therefore shows congestion caused by wind, solar and cross-border
flows that the lines cannot carry. It is not a market model.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

from gridfee.data.smard import TSO_RES_FILTERS
from gridfee.flows import OPFResult, run_dc_opf
from gridfee.injections import ZonalInjections
from gridfee.network import ZoneNetwork
from gridfee.signal import FeeSignal, compute_fee_signal

logger = logging.getLogger(__name__)

RESIDUAL = "residual"
IMPORT = "import"
GENERATOR_SEPARATOR = ":"
_SHARE_SUM_TOLERANCE = 1e-6


class BackcastError(ValueError):
    """The inputs of a backcast do not fit together."""


@dataclass(frozen=True)
class BackcastInputs:
    """Inputs of :func:`gridfee.flows.run_dc_opf` for the hours that can be solved."""

    load_mw: pd.DataFrame
    generators: pd.DataFrame
    availability: pd.DataFrame
    residual_shares: pd.Series
    placeholder_residual_shares: bool
    skipped_hours: pd.DatetimeIndex
    clipped_exchange: pd.DataFrame
    ptdf_reference: dict[str, float]


@dataclass(frozen=True)
class BackcastResult:
    inputs: BackcastInputs
    opf: OPFResult
    signal: FeeSignal


def generator_id(kind: str, zone_id: str) -> str:
    return f"{kind}{GENERATOR_SEPARATOR}{zone_id}"


def neighbour_net_export(physical_flows_tso: pd.DataFrame) -> pd.DataFrame:
    """Sum the ``<TSO zone>_<neighbour>`` columns per neighbour.

    An hour is missing for a neighbour if it is missing for any of its TSO zones,
    except for series that are empty for the whole period (no interconnector).
    """
    flows = physical_flows_tso.dropna(axis=1, how="all")
    neighbours = flows.columns.str.split("_", n=1).str[1]
    return (
        flows.T.groupby(neighbours)
        .sum(min_count=1)
        .T.where(flows.notna().T.groupby(neighbours).all().T)
    )


def build_backcast_inputs(
    network: ZoneNetwork,
    injections: ZonalInjections,
    generation_de: pd.DataFrame,
    physical_flows_tso: pd.DataFrame,
    residual_cost: float = 1.0,
    residual_shares: pd.Series | None = None,
) -> BackcastInputs:
    """Turn injections, national generation and cross-border flows into OPF inputs.

    generation_de: hours x fuels, MW (``generation_de`` table of the collector).
    physical_flows_tso: hours x ``<TSO zone>_<neighbour>``, net export in MW.
    residual_cost: marginal cost of the residual generators, EUR/MWh. Shadow prices
        and the fee signal scale with it.
    residual_shares: zone id -> share of the national residual capacity, summing
        to 1 over the internal zones. Default: placeholder, the share of demand.

    Hours in which a cross-border series or the national generation is missing are
    left out and listed in ``skipped_hours``. ``clipped_exchange`` lists, per
    neighbour, the hours and energy by which observed flows exceed the capacity
    of the lines to it; the backcast uses the capacity in those hours.
    """
    if not residual_cost > 0:
        raise BackcastError(f"residual_cost must be > 0, got {residual_cost!r}")
    internal = network.internal_zone_ids
    boundary = network.boundary_zone_ids
    demand = injections.demand_mw
    hours = demand.index
    missing = sorted(set(internal) - set(demand.columns))
    if missing:
        raise BackcastError(f"injections have no demand for the zones {missing}")
    for name, frame in (("generation_de", generation_de), ("physical flows", physical_flows_tso)):
        if not frame.index.equals(hours):
            raise BackcastError(f"{name} and the injections must cover the same hours")

    exports = neighbour_net_export(physical_flows_tso)
    unknown = sorted(set(exports.columns) - set(boundary))
    if unknown:
        raise BackcastError(
            f"cross-border flows to {unknown}, which are not boundary zones of the network"
        )
    without_flows = sorted(set(boundary) - set(exports.columns))
    if without_flows:
        logger.warning("no cross-border flows for the boundary zones %s: set to 0", without_flows)

    exports, clipped = _clip_to_line_capacity(network, exports)

    res_fuels = [fuel for fuel in TSO_RES_FILTERS if fuel in generation_de.columns]
    residual_de = generation_de.drop(columns=res_fuels).sum(axis=1, skipna=False)

    usable = exports.notna().all(axis=1) & residual_de.notna()
    skipped = hours[~usable]
    if len(skipped):
        logger.warning(
            "%d hours left out because cross-border flows or national generation are missing",
            len(skipped),
        )
    if not usable.any():
        raise BackcastError("no hour has complete cross-border flows and national generation")

    placeholder = residual_shares is None
    if placeholder:
        annual = demand[internal].sum()
        residual_shares = annual / annual.sum()
        logger.warning(
            "PLACEHOLDER residual generation shares (equal to the share of demand) are used: "
            "the location of conventional generation is not known"
        )
    else:
        residual_shares = residual_shares.astype(float)
        if set(residual_shares.index) != set(internal):
            raise BackcastError("residual_shares must list every internal zone exactly once")
        if (residual_shares < 0).any() or abs(residual_shares.sum() - 1) > _SHARE_SUM_TOLERANCE:
            raise BackcastError("residual_shares must be >= 0 and sum to 1")
        residual_shares = residual_shares.loc[internal]

    load = demand.loc[usable, internal].copy()
    for zone_id in exports.columns:
        load[zone_id] = exports.loc[usable, zone_id].clip(lower=0.0)

    rows: dict[str, tuple[str, float, float]] = {}
    availability: dict[str, pd.Series] = {}

    def add_profile(kind: str, zone_id: str, series: pd.Series) -> None:
        peak = float(series.max())
        if peak <= 0:
            return
        name = generator_id(kind, zone_id)
        rows[name] = (zone_id, peak, 0.0)
        availability[name] = (series / peak).clip(0.0, 1.0)

    for tech, frame in injections.res_mw.items():
        for zone_id in frame.columns:
            add_profile(tech, zone_id, frame.loc[usable, zone_id])
    for zone_id in exports.columns:
        add_profile(IMPORT, zone_id, (-exports.loc[usable, zone_id]).clip(lower=0.0))
    peak_residual = float(residual_de[usable].max())
    for zone_id in internal:
        if residual_shares[zone_id] > 0:
            rows[generator_id(RESIDUAL, zone_id)] = (
                zone_id,
                peak_residual * float(residual_shares[zone_id]),
                residual_cost,
            )

    generators = pd.DataFrame.from_dict(
        rows, orient="index", columns=["zone", "p_nom_mw", "marginal_cost_eur_per_mwh"]
    ).rename_axis("generator_id")
    reference = demand.loc[usable, internal].sum()
    return BackcastInputs(
        load_mw=load.rename_axis("snapshot"),
        generators=generators,
        availability=pd.DataFrame(availability).rename_axis("snapshot"),
        residual_shares=residual_shares.rename("share").rename_axis("zone_id"),
        placeholder_residual_shares=placeholder,
        skipped_hours=skipped,
        clipped_exchange=clipped,
        ptdf_reference={zone_id: float(reference[zone_id]) for zone_id in internal},
    )


def _clip_to_line_capacity(
    network: ZoneNetwork, exports: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    lines = network.lines_frame()
    rows = []
    limited = {}
    for zone_id in exports.columns:
        at_zone = (lines["from_zone"] == zone_id) | (lines["to_zone"] == zone_id)
        capacity = float(lines.loc[at_zone, "capacity_mw"].sum())
        observed = exports[zone_id]
        limited[zone_id] = observed.clip(-capacity, capacity)
        excess = (observed.abs() - capacity).clip(lower=0.0)
        if (excess > 0).any():
            rows.append(
                {
                    "neighbour": zone_id,
                    "line_capacity_mw": capacity,
                    "max_observed_mw": float(observed.abs().max()),
                    "hours": int((excess > 0).sum()),
                    "clipped_mwh": float(excess.sum()),
                }
            )
    clipped = pd.DataFrame(
        rows, columns=["neighbour", "line_capacity_mw", "max_observed_mw", "hours", "clipped_mwh"]
    )
    for row in clipped.itertuples():
        logger.warning(
            "%s: observed flow above the line capacity of %.0f MW in %d hours (up to %.0f MW); "
            "cut to the capacity",
            row.neighbour,
            row.line_capacity_mw,
            row.hours,
            row.max_observed_mw,
        )
    return pd.DataFrame(limited), clipped


def run_backcast(
    network: ZoneNetwork,
    inputs: BackcastInputs,
    hours: Sequence | None = None,
    load_shedding_cost: float | None = None,
    threshold: float = 0.0,
) -> BackcastResult:
    """Solve the DC OPF for the backcast and compute the fee signal.

    The PTDF, and with it the fee signal, uses a slack spread over the German
    zones in proportion to their demand: the signal of a zone is measured against
    the demand-weighted average of Germany. The signal covers the German zones
    only. Boundary zones are not fee zones, and where an observed flow was cut to
    the line capacity their prices are not determined by the model.
    """
    opf = run_dc_opf(
        network,
        load_mw=inputs.load_mw,
        generators=inputs.generators,
        availability=inputs.availability,
        snapshots=hours,
        load_shedding_cost=load_shedding_cost,
        ptdf_reference=inputs.ptdf_reference,
    )
    ptdf = opf.ptdf[network.internal_zone_ids]
    return BackcastResult(inputs, opf, compute_fee_signal(opf.shadow_prices, ptdf, threshold))
