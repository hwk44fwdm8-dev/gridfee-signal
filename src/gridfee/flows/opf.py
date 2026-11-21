"""Network-constrained DC optimal power flow with PyPSA and HiGHS."""

from __future__ import annotations

import logging
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd
import pypsa

from gridfee.flows.ptdf import compute_ptdf
from gridfee.network.schema import ZoneNetwork

GENERATOR_COLUMNS = ("zone", "p_nom_mw", "marginal_cost_eur_per_mwh")
_SHEDDING_PREFIX = "load_shedding "
_ANCHOR = "objective_anchor"


class OPFError(RuntimeError):
    """The OPF inputs are inconsistent or the problem could not be solved."""


@dataclass(frozen=True)
class OPFResult:
    """Hourly OPF results. All frames are indexed by snapshot.

    flows_mw: line flows, positive from ``from_zone`` to ``to_zone``.
    shadow_prices: signed shadow price of each line's capacity in EUR/MWh per MW.
        Its size is the cost saved by one more MW of capacity. It is positive when
        the line is congested from ``from_zone`` to ``to_zone``, negative when
        congested in the reverse direction and 0 when the line is not congested.
    nodal_prices: marginal cost of supplying 1 MWh more at each zone, EUR/MWh.
        For any two zones n and m:
        price[n] - price[m] = -sum_l (ptdf[l, n] - ptdf[l, m]) * shadow_price[l].
    dispatch_mw: generator output.
    load_shedding_mw: unserved load per zone; None if load shedding is disabled.
    ptdf: lines x zones, see :func:`gridfee.flows.ptdf.compute_ptdf`.
    objective_eur: total cost over the solved snapshots, unweighted.
    """

    flows_mw: pd.DataFrame
    shadow_prices: pd.DataFrame
    nodal_prices: pd.DataFrame
    dispatch_mw: pd.DataFrame
    load_shedding_mw: pd.DataFrame | None
    ptdf: pd.DataFrame
    objective_eur: float

    def congested(self, tolerance: float = 1e-6) -> pd.DataFrame:
        """Boolean frame, True where a line's capacity limit is binding at a cost."""
        return self.shadow_prices.abs() > tolerance


def build_pypsa_network(network: ZoneNetwork) -> pypsa.Network:
    """Translate the zone network into a PyPSA network without any injections."""
    n = pypsa.Network()
    n.add("Carrier", "AC")
    zones = network.zones_frame()
    # With v_nom = 1 kV the per-unit reactance equals x in ohm, so x = 1 / susceptance
    # makes PyPSA's linear flow equal susceptance * angle difference in MW.
    n.add(
        "Bus",
        zones.index,
        v_nom=1.0,
        carrier="AC",
        x=zones["lon"].astype(float).fillna(0.0).to_numpy(),
        y=zones["lat"].astype(float).fillna(0.0).to_numpy(),
    )
    lines = network.lines_frame()
    if not lines.empty:
        n.add(
            "Line",
            lines.index,
            bus0=lines["from_zone"].to_numpy(),
            bus1=lines["to_zone"].to_numpy(),
            x=(1.0 / lines["susceptance_mw_per_rad"]).to_numpy(),
            s_nom=lines["capacity_mw"].to_numpy(),
            carrier="AC",
        )
    return n


def run_dc_opf(
    network: ZoneNetwork,
    load_mw: pd.DataFrame,
    generators: pd.DataFrame,
    availability: pd.DataFrame | None = None,
    snapshots: Iterable | None = None,
    load_shedding_cost: float | None = None,
    ptdf_reference: str | Mapping[str, float] | None = None,
) -> OPFResult:
    """Minimise generation cost subject to zonal balances and line capacities.

    load_mw: snapshots x zones, MW. Zones without a column have no load.
    generators: indexed by generator id, with the columns ``zone``, ``p_nom_mw``
        and ``marginal_cost_eur_per_mwh``. Generators may be dispatched anywhere
        between 0 and their available capacity, so renewables entered as generators
        can be curtailed for free.
    availability: snapshots x generator ids, share of ``p_nom_mw`` available in each
        hour (0..1). Generators without a column are fully available.
    snapshots: subset of the index of ``load_mw`` to solve, e.g. representative
        hours. The hours are independent of each other, so a subset gives the same
        hourly results as the full run.
    load_shedding_cost: if set, load can be left unserved at this cost in EUR/MWh,
        which keeps the problem feasible. If None, an infeasible hour raises OPFError.
    ptdf_reference: passed to :func:`gridfee.flows.ptdf.compute_ptdf`.
    """
    load_mw, generators, availability = _validate_inputs(
        network, load_mw, generators, availability, snapshots
    )

    # PyPSA rejects timezone-aware snapshots, so it solves on positions and the
    # results get the caller's index back.
    index = load_mw.index
    positions = pd.RangeIndex(len(index))
    load_mw = load_mw.set_axis(positions)
    if availability is not None:
        availability = availability.set_axis(positions)

    n = build_pypsa_network(network)
    n.set_snapshots(positions)
    n.add("Load", load_mw.columns, bus=load_mw.columns, p_set=load_mw)
    n.add(
        "Generator",
        generators.index,
        bus=generators["zone"].to_numpy(),
        p_nom=generators["p_nom_mw"].to_numpy(),
        marginal_cost=generators["marginal_cost_eur_per_mwh"].to_numpy(),
        p_max_pu=availability if availability is not None else 1.0,
    )
    if load_shedding_cost is not None:
        # Shedding is capped at the zone's own load in each hour, so it can never
        # act as generation that exports to other zones.
        shedding_zones = pd.Index(network.zone_ids)
        sheddable = load_mw.reindex(columns=shedding_zones, fill_value=0.0).clip(lower=0.0)
        peak = sheddable.max()
        n.add(
            "Generator",
            _SHEDDING_PREFIX + shedding_zones,
            bus=shedding_zones,
            p_nom=peak.to_numpy(),
            p_max_pu=sheddable.div(peak.where(peak > 0, 1.0)).add_prefix(_SHEDDING_PREFIX),
            marginal_cost=load_shedding_cost,
        )
    if not (n.generators["marginal_cost"] != 0).any():
        # PyPSA cannot build an objective without any cost term (e.g. renewables
        # only). A generator without capacity adds one and changes nothing else.
        n.add("Generator", _ANCHOR, bus=network.zone_ids[0], p_nom=0.0, marginal_cost=1.0)

    _solve(n)

    dispatch = n.generators_t.p.reindex(columns=n.generators.index, fill_value=0.0)
    dispatch = dispatch.drop(columns=_ANCHOR, errors="ignore")
    is_shedding = dispatch.columns.str.startswith(_SHEDDING_PREFIX)
    load_shedding = None
    if load_shedding_cost is not None:
        load_shedding = dispatch.loc[:, is_shedding]
        load_shedding.columns = load_shedding.columns.str.removeprefix(_SHEDDING_PREFIX)

    line_ids = network.line_ids
    mu_upper = n.lines_t.mu_upper.reindex(columns=line_ids, fill_value=0.0)
    mu_lower = n.lines_t.mu_lower.reindex(columns=line_ids, fill_value=0.0)

    def named(frame: pd.DataFrame, columns_name: str) -> pd.DataFrame:
        # "+ 0.0" turns the solver's -0.0 into 0.0
        frame = frame.set_axis(index.rename("snapshot")).rename_axis(columns=columns_name)
        return frame + 0.0

    return OPFResult(
        flows_mw=named(n.lines_t.p0.reindex(columns=line_ids, fill_value=0.0), "line_id"),
        # PyPSA reports the raw duals of the cost minimisation: mu_upper <= 0 for
        # flow <= capacity and mu_lower >= 0 for flow >= -capacity.
        shadow_prices=named(-(mu_upper + mu_lower), "line_id"),
        nodal_prices=named(n.buses_t.marginal_price[network.zone_ids], "zone_id"),
        dispatch_mw=named(dispatch.loc[:, ~is_shedding], "generator_id"),
        load_shedding_mw=None if load_shedding is None else named(load_shedding, "zone_id"),
        ptdf=compute_ptdf(network, ptdf_reference),
        objective_eur=float(n.objective),
    )


def _validate_inputs(
    network: ZoneNetwork,
    load_mw: pd.DataFrame,
    generators: pd.DataFrame,
    availability: pd.DataFrame | None,
    snapshots: Iterable | None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None]:
    zone_ids = set(network.zone_ids)

    if not load_mw.index.is_unique:
        raise OPFError("load_mw has duplicate snapshots")
    unknown = sorted(set(load_mw.columns) - zone_ids)
    if unknown:
        raise OPFError(f"load_mw has columns that are not zones: {unknown}")

    if snapshots is not None:
        snapshots = pd.Index(snapshots)
        missing = snapshots.difference(load_mw.index)
        if len(missing) or not snapshots.is_unique:
            raise OPFError(
                f"snapshots must be unique and part of load_mw.index; missing: {list(missing[:5])}"
            )
        load_mw = load_mw.loc[snapshots]
    if load_mw.empty:
        raise OPFError("no snapshots to solve")
    if load_mw.isna().any().any():
        raise OPFError("load_mw contains missing values")

    missing = sorted(set(GENERATOR_COLUMNS) - set(generators.columns))
    if missing:
        raise OPFError(f"generators is missing columns: {missing}")
    if not generators.index.is_unique:
        raise OPFError("generators has duplicate ids")
    ids = generators.index.astype(str)
    if ids.str.startswith(_SHEDDING_PREFIX).any() or (ids == _ANCHOR).any():
        raise OPFError(
            f"generator ids must not start with {_SHEDDING_PREFIX!r} or equal {_ANCHOR!r}"
        )
    unknown = sorted(set(generators["zone"]) - zone_ids)
    if unknown:
        raise OPFError(f"generators refer to unknown zones: {unknown}")
    numeric = generators[["p_nom_mw", "marginal_cost_eur_per_mwh"]]
    if numeric.isna().any().any() or (generators["p_nom_mw"] < 0).any():
        raise OPFError("generators need a non-negative p_nom_mw and a marginal cost")

    if availability is not None:
        unknown = sorted(set(availability.columns) - set(generators.index))
        if unknown:
            raise OPFError(f"availability has columns that are not generators: {unknown}")
        missing = load_mw.index.difference(availability.index)
        if len(missing):
            raise OPFError(f"availability lacks snapshots, e.g. {list(missing[:5])}")
        availability = availability.loc[load_mw.index].reindex(
            columns=generators.index, fill_value=1.0
        )
        if availability.isna().any().any() or ((availability < 0) | (availability > 1)).any().any():
            raise OPFError("availability must lie between 0 and 1 without gaps")

    return load_mw, generators, availability


def _solve(n: pypsa.Network) -> None:
    # PyPSA and linopy log every solve at INFO and warn about lines without
    # resistance, which the linear flow model does not use.
    loggers = [logging.getLogger(name) for name in ("pypsa", "linopy")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            status, condition = n.optimize(
                solver_name="highs",
                assign_all_duals=True,
                solver_options={"output_flag": False, "log_to_console": False},
            )
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)
    if status != "ok" or condition != "optimal":
        raise OPFError(
            f"DC OPF did not solve to optimality (status={status}, condition={condition}). "
            "Generation may be short of load or exports may exceed line capacity; "
            "set load_shedding_cost to locate the shortfall."
        )
