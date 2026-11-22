"""Two-run fee method: a market run without the German grid, then a grid run.

The single run (:func:`gridfee.backcast.run_backcast`) dispatches every plant with
the line limits in one optimisation. The two-run method follows how the German
market and the TSOs work:

1. **Market run.** Germany is one price zone: all load, all generators and the
   observed exchange with the neighbours sit on one bus without any line. Plants
   are dispatched by cost alone. Its price is the cost of the last plant needed.
2. **Grid run.** Starting from the market run's dispatch, plants are moved until
   no line is overloaded, at the least cost of moving them (:class:`RedispatchCosts`).
   What is moved is the modelled redispatch; the shadow prices of the line limits
   of this run give the fee signal, with the same PTDF and sign convention as the
   single run.

Loads, generators, their costs and the lines are those of the single run
(:class:`gridfee.backcast.BackcastInputs`); only the method differs. The hours are
independent of each other in both runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

from gridfee.backcast import SHEDDING_TOLERANCE_MW, BackcastError, BackcastInputs
from gridfee.flows import OPFResult, run_dc_opf
from gridfee.network import Zone, ZoneNetwork
from gridfee.signal import FeeSignal, compute_fee_signal

SINGLE_RUN = "single"
TWO_RUN = "two-run"
METHODS = (SINGLE_RUN, TWO_RUN)
MARKET_ZONE = "market"
_KEPT = "kept "
_RAISED = "raised "
REDISPATCH_TOLERANCE_MW = 1e-6
DEFAULT_CHUNK_HOURS = 1000


@dataclass(frozen=True)
class RedispatchCosts:
    """What moving a plant away from the market run's dispatch costs. ASSUMPTIONS.

    markup: EUR per MWh moved, in either direction. A plant that is raised is paid
        its marginal cost plus the markup; a plant that is lowered pays back the
        marginal cost it saves less the markup. It stands for the cost of changing
        a schedule and makes the grid run move as little as needed.
    curtailment_cost: EUR per MWh by which feed-in without a marginal cost (wind,
        solar, feed-in that follows its observed output, must-run output, imports)
        is lowered, on top of the markup. None: the market run's price of the hour,
        i.e. the market revenue the curtailed plant loses.
    """

    markup: float = 1.0
    curtailment_cost: float | None = None

    def __post_init__(self) -> None:
        if not 0 < self.markup < float("inf"):
            raise BackcastError(f"the redispatch markup must be > 0, got {self.markup!r}")
        cost = self.curtailment_cost
        if cost is not None and not 0 <= cost < float("inf"):
            raise BackcastError(f"the curtailment cost must be >= 0, got {cost!r}")


@dataclass(frozen=True)
class TwoRunResult:
    """Results of :func:`run_two_run_backcast`. All frames are indexed by hour.

    market_dispatch_mw: hours x generators, dispatch of the market run.
    market_price: EUR/MWh, the one price of the market run.
    market_unserved_mw: load the market run could not serve (0 in a consistent input).
    opf: the grid run; ``dispatch_mw`` is the dispatch after redispatch.
    redispatch_mw: hours x generators, grid run minus market run: positive where a
        generator is raised, negative where it is lowered. With hourly data, MW is
        MWh per hour.
    redispatch_cost_eur: cost of the movements per hour under ``costs``.
    signal: fee signal from the line shadow prices of the grid run.
    """

    inputs: BackcastInputs
    costs: RedispatchCosts
    market_dispatch_mw: pd.DataFrame
    market_price: pd.Series
    market_unserved_mw: pd.Series
    opf: OPFResult
    redispatch_mw: pd.DataFrame
    redispatch_cost_eur: pd.Series
    signal: FeeSignal

    def by_zone(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Raised and lowered feed-in per zone, both hours x zones and >= 0."""
        zones = self.inputs.generators["zone"]
        raised = self.redispatch_mw.clip(lower=0.0).T.groupby(zones).sum().T
        lowered = (-self.redispatch_mw).clip(lower=0.0).T.groupby(zones).sum().T
        return raised.rename_axis(columns="zone_id"), lowered.rename_axis(columns="zone_id")

    def hourly(self) -> pd.DataFrame:
        """Per hour: MWh raised, MWh lowered and the cost of it."""
        return pd.DataFrame(
            {
                "raised_mwh": self.redispatch_mw.clip(lower=0.0).sum(axis=1),
                "lowered_mwh": (-self.redispatch_mw).clip(lower=0.0).sum(axis=1),
                "cost_eur": self.redispatch_cost_eur,
            }
        )


def run_two_run_backcast(
    network: ZoneNetwork,
    inputs: BackcastInputs,
    hours: Sequence | None = None,
    load_shedding_cost: float | None = None,
    threshold: float = 0.0,
    costs: RedispatchCosts | None = None,
    chunk_hours: int = DEFAULT_CHUNK_HOURS,
    block_hours: int | None = None,
    workers: int = 1,
) -> TwoRunResult:
    """Market run on one bus, then the grid run that removes the overloads.

    Arguments as for :func:`gridfee.backcast.run_backcast`. ``chunk_hours`` hours are
    solved at a time to bound memory; the results do not depend on it.
    ``block_hours`` and ``workers`` are those of :func:`gridfee.flows.run_dc_opf`
    and apply to both runs; with ``block_hours`` the hours are not chunked a second
    time.

    Hours in which either run leaves load unserved are not part of the signal, as in
    the single run. Where several generators have the same cost, the market run's
    choice among them is arbitrary, and so is the redispatch between them.
    """
    costs = costs or RedispatchCosts()
    if chunk_hours < 1:
        raise BackcastError("chunk_hours must be at least 1")
    index = inputs.load_mw.index if hours is None else pd.Index(hours)
    if block_hours is not None:
        chunk_hours = max(chunk_hours, len(index))
    chunks = [index[start : start + chunk_hours] for start in range(0, len(index), chunk_hours)]
    generators = inputs.generators

    market_network = ZoneNetwork(
        zones=(
            Zone(
                zone_id=MARKET_ZONE,
                name="Germany as one price zone",
                kind="boundary",
                country="DE",
            ),
        )
    )
    market_runs = [
        run_dc_opf(
            market_network,
            load_mw=inputs.load_mw.sum(axis=1).to_frame(MARKET_ZONE),
            generators=generators.assign(zone=MARKET_ZONE),
            availability=inputs.availability,
            marginal_cost=inputs.marginal_cost,
            snapshots=chunk,
            load_shedding_cost=load_shedding_cost,
            block_hours=block_hours,
            workers=workers,
        )
        for chunk in chunks
    ]
    market = pd.concat([run.dispatch_mw for run in market_runs])[generators.index]
    price = pd.concat([run.nodal_prices[MARKET_ZONE] for run in market_runs]).rename(
        "price_eur_per_mwh"
    )
    market_unserved = pd.Series(0.0, index=market.index, name="unserved_mw")
    if load_shedding_cost is not None:
        market_unserved += pd.concat([run.load_shedding_mw[MARKET_ZONE] for run in market_runs])

    hourly_index = market.index
    cost = pd.DataFrame(
        {name: float(value) for name, value in generators["marginal_cost_eur_per_mwh"].items()},
        index=hourly_index,
    )
    varying = inputs.marginal_cost
    if varying is not None:
        cost[varying.columns] = varying.loc[index].to_numpy()
    curtailment = (
        price.clip(lower=0.0)
        if costs.curtailment_cost is None
        else pd.Series(costs.curtailment_cost, index=hourly_index)
    )
    # Output kept from the market run is priced at what lowering it gives up.
    kept_cost = (cost - costs.markup).where(cost != 0, -(curtailment + costs.markup), axis=0)
    raised_cost = cost + costs.markup

    capacity = generators["p_nom_mw"].astype(float)
    available = pd.DataFrame(1.0, index=index, columns=generators.index)
    if not inputs.availability.empty:
        columns = inputs.availability.columns
        available[columns] = inputs.availability.loc[index, columns].to_numpy()
    available = (available * capacity).set_axis(hourly_index)
    scheduled = market.clip(lower=0.0)
    kept_peak = scheduled.max()
    headroom = (available - scheduled).clip(lower=0.0)
    safe_capacity = capacity.where(capacity > 0, 1.0)

    grid_generators = pd.concat(
        [
            _prefixed(
                pd.DataFrame(
                    {
                        "zone": generators["zone"],
                        "p_nom_mw": kept_peak,
                        "marginal_cost_eur_per_mwh": kept_cost.mean(),
                    }
                ),
                _KEPT,
            ),
            _prefixed(
                pd.DataFrame(
                    {
                        "zone": generators["zone"],
                        "p_nom_mw": capacity,
                        "marginal_cost_eur_per_mwh": raised_cost.mean(),
                    }
                ),
                _RAISED,
            ),
        ]
    )
    grid_availability = pd.concat(
        [
            (scheduled / kept_peak.where(kept_peak > 0, 1.0)).clip(0.0, 1.0).add_prefix(_KEPT),
            (headroom / safe_capacity).clip(0.0, 1.0).add_prefix(_RAISED),
        ],
        axis=1,
    )
    grid_cost = pd.concat([kept_cost.add_prefix(_KEPT), raised_cost.add_prefix(_RAISED)], axis=1)
    load = inputs.load_mw.loc[index].set_axis(hourly_index)

    grid_runs = [
        run_dc_opf(
            network,
            load_mw=load,
            generators=grid_generators,
            availability=grid_availability,
            marginal_cost=grid_cost,
            snapshots=market_runs[position].dispatch_mw.index,
            load_shedding_cost=load_shedding_cost,
            ptdf_reference=inputs.ptdf_reference,
            block_hours=block_hours,
            workers=workers,
        )
        for position in range(len(chunks))
    ]
    moved = pd.concat([run.dispatch_mw for run in grid_runs])
    kept = moved[_KEPT + generators.index].set_axis(generators.index, axis=1)
    raised = moved[_RAISED + generators.index].set_axis(generators.index, axis=1)
    redispatch = raised - (scheduled - kept)
    redispatch = redispatch.where(redispatch.abs() > REDISPATCH_TOLERANCE_MW, 0.0)
    redispatch_cost = (raised * raised_cost).sum(axis=1) - ((scheduled - kept) * kept_cost).sum(
        axis=1
    )

    shedding = None
    if load_shedding_cost is not None:
        shedding = pd.concat([run.load_shedding_mw for run in grid_runs])
    opf = OPFResult(
        flows_mw=pd.concat([run.flows_mw for run in grid_runs]),
        shadow_prices=pd.concat([run.shadow_prices for run in grid_runs]),
        nodal_prices=pd.concat([run.nodal_prices for run in grid_runs]),
        dispatch_mw=(kept + raised).rename_axis(columns="generator_id"),
        load_shedding_mw=shedding,
        ptdf=grid_runs[0].ptdf,
        objective_eur=float(sum(run.objective_eur for run in grid_runs)),
    )
    prices = opf.shadow_prices
    unserved = market_unserved > SHEDDING_TOLERANCE_MW
    if shedding is not None:
        unserved |= shedding.sum(axis=1) > SHEDDING_TOLERANCE_MW
    signal = compute_fee_signal(prices[~unserved], opf.ptdf[network.internal_zone_ids], threshold)
    return TwoRunResult(
        inputs=inputs,
        costs=costs,
        market_dispatch_mw=market,
        market_price=price,
        market_unserved_mw=market_unserved,
        opf=opf,
        redispatch_mw=redispatch.rename_axis(columns="generator_id"),
        redispatch_cost_eur=redispatch_cost.rename("cost_eur"),
        signal=signal,
    )


def _prefixed(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
    return frame.set_axis(prefix + frame.index, axis=0).rename_axis("generator_id")
