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

  With one cost everywhere, producing on the other side of a full line is free, so
  full lines get no shadow price and the fee signal stays neutral.
- With ``generation_shares`` and ``generation_costs`` the national generation is
  taken apart by fuel instead (SMARD's national series per fuel). A fuel with a cost
  gets one generator per zone at that cost, with the zone's share of the year's
  highest national output of the fuel as capacity; it is available between the
  first and the last hour in which the fuel produced. A fuel without a cost
  (biomass, hydro, ...) feeds in its observed national output, split by the zone's
  share, at no cost and curtailable, like wind and solar.

The backcast therefore shows congestion caused by wind, solar and cross-border
flows that the lines cannot carry. It is not a market model: plants are dispatched
by one cost per fuel, without start-up costs, heat demand, outages or reserves.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

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
# "<kind>:<zone id>@<class>" for several generators of one kind in a zone.
CLASS_SEPARATOR = "@"
MUST_RUN = "must_run"
BALANCE = "balance"
# boundary zone of one neighbour towards one TSO zone: "<neighbour>/<TSO zone>"
BORDER_SEPARATOR = "/"
_WINDOWS = {"day": "D", "week": "W", "year": "Y"}
SHEDDING_TOLERANCE_MW = 1e-3
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
    # snapshots x generator ids, EUR/MWh, for generators whose cost changes over the year.
    marginal_cost: pd.DataFrame | None = None


@dataclass(frozen=True)
class PlantCosts:
    """Costs of the fuels that are dispatched by plant efficiency class.

    parameters: the cost file (:func:`read_cost_parameters`), indexed by technology.
    efficiency: rows ``technology, zone_id, efficiency, share`` (:func:`read_plant_efficiency`).
        Fuels of ``parameters`` without rows here get the one efficiency of ``parameters``.
    window: "day", "week" or "year" (Europe/Berlin). Per fuel, the capacity available in an hour
        is the fuel's highest observed national output of that day or week, and its
        lowest output of that day or week runs at no cost.
    fuel_prices: month (1-12) x technology, EUR per MWh of heat (:func:`read_fuel_prices`);
        replaces the fuel price of ``parameters`` for the technologies it lists.
    """

    parameters: pd.DataFrame
    efficiency: pd.DataFrame
    fuel_prices: pd.DataFrame | None = None
    window: str = "day"

    def __post_init__(self) -> None:
        if self.window not in _WINDOWS:
            raise BackcastError(f"window must be one of {sorted(_WINDOWS)}, got {self.window!r}")


@dataclass(frozen=True)
class BackcastResult:
    inputs: BackcastInputs
    opf: OPFResult
    signal: FeeSignal


def generator_id(kind: str, zone_id: str) -> str:
    return f"{kind}{GENERATOR_SEPARATOR}{zone_id}"


def neighbour_of(zone_id: str) -> str:
    """The neighbour a boundary zone of :func:`split_borders_by_tso` stands for."""
    return zone_id.split(BORDER_SEPARATOR)[0]


def split_borders_by_tso(network: ZoneNetwork, physical_flows_tso: pd.DataFrame) -> ZoneNetwork:
    """Give a neighbour reached from several TSO zones one boundary zone per TSO zone.

    SMARD publishes the physical flow per TSO zone and neighbour. With one zone per
    neighbour the model decides how that flow divides over the neighbour's lines, by
    their assumed susceptances; with one zone per TSO zone and neighbour
    (``FR/Amprion``, ``FR/TransnetBW``) each border carries what was observed. Line ids
    do not change. A neighbour is left as it is if it has lines to other boundary zones
    or if SMARD has a flow to it from a TSO zone that has no line to it in the network
    (Amprion to Austria in the 22-zone network).
    """
    observed: dict[str, set[str]] = {}
    for column in physical_flows_tso.dropna(axis=1, how="all").columns:
        tso, neighbour = column.split("_", 1)
        observed.setdefault(neighbour, set()).add(tso)
    boundary = set(network.boundary_zone_ids)
    tso_of = network.zone_to_tso
    tso_at: dict[str, dict[str, str]] = {}
    keep = set()
    for line in network.lines:
        ends = (line.from_zone, line.to_zone)
        outside = [end for end in ends if end in boundary]
        if len(outside) == 2:
            keep.update(outside)
        elif outside:
            inside = ends[0] if ends[1] == outside[0] else ends[1]
            tso_at.setdefault(outside[0], {})[line.line_id] = tso_of[inside]
    split = {
        zone_id: lines
        for zone_id, lines in tso_at.items()
        if zone_id not in keep
        and len(set(lines.values())) > 1
        and observed.get(zone_id, set()) <= set(lines.values())
    }
    if not split:
        return network

    zones = []
    for zone in network.zones:
        if zone.zone_id not in split:
            zones.append(zone)
            continue
        for tso in sorted(set(split[zone.zone_id].values())):
            zones.append(
                zone.model_copy(
                    update={
                        "zone_id": f"{zone.zone_id}{BORDER_SEPARATOR}{tso}",
                        "name": f"{zone.name} ({tso})",
                    }
                )
            )
    lines = []
    for line in network.lines:
        update = {
            end: f"{zone_id}{BORDER_SEPARATOR}{split[zone_id][line.line_id]}"
            for end in ("from_zone", "to_zone")
            if (zone_id := getattr(line, end)) in split
        }
        lines.append(line.model_copy(update=update) if update else line)
    return ZoneNetwork(zones=tuple(zones), lines=tuple(lines))


def boundary_net_export(network: ZoneNetwork, physical_flows_tso: pd.DataFrame) -> pd.DataFrame:
    """Observed net export per boundary zone, also for those of :func:`split_borders_by_tso`."""
    exports = neighbour_net_export(physical_flows_tso)
    flows = physical_flows_tso.dropna(axis=1, how="all")
    for zone_id in network.boundary_zone_ids:
        if BORDER_SEPARATOR not in zone_id:
            continue
        neighbour, tso = zone_id.split(BORDER_SEPARATOR, 1)
        column = f"{tso}_{neighbour}"
        if column in flows:
            exports[zone_id] = flows[column]
    for neighbour in {neighbour_of(z) for z in exports.columns if BORDER_SEPARATOR in z}:
        zones = [z for z in network.boundary_zone_ids if neighbour_of(z) == neighbour]
        observed = {c for c in flows.columns if c.split("_", 1)[1] == neighbour}
        unplaced = sorted(observed - {"_".join(z.split(BORDER_SEPARATOR)[::-1]) for z in zones})
        if unplaced:
            raise BackcastError(
                f"cross-border flows {unplaced} have no line from that TSO zone in the network"
            )
        exports = exports.drop(columns=neighbour, errors="ignore")
    return exports


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
    generation_shares: pd.DataFrame | None = None,
    generation_costs: Mapping[str, float] | None = None,
    plant_costs: PlantCosts | None = None,
) -> BackcastInputs:
    """Turn injections, national generation and cross-border flows into OPF inputs.

    generation_de: hours x fuels, MW (``generation_de`` table of the collector).
    physical_flows_tso: hours x ``<TSO zone>_<neighbour>``, net export in MW.
    residual_cost: marginal cost of the residual generators, EUR/MWh. Shadow prices
        and the fee signal scale with it.
    residual_shares: zone id -> share of the national residual capacity, summing
        to 1 over the internal zones. Default: placeholder, the share of demand.

    generation_shares: fuels x zone ids, each zone's share of Germany's capacity of
        the fuel (:func:`read_generation_shares`). With it the national generation
        is modelled per fuel and ``residual_cost`` and ``residual_shares`` are not
        used. Every fuel of ``generation_de`` other than wind and solar that
        produced must be listed.
    generation_costs: fuel -> marginal cost in EUR/MWh
        (:func:`read_generation_costs`). Fuels of ``generation_shares`` without a
        cost feed in their observed national output.

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

    exports = boundary_net_export(network, physical_flows_tso)
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

    by_fuel = generation_shares is not None
    if (generation_costs is not None or plant_costs is not None) and not by_fuel:
        raise BackcastError("generation_costs and plant_costs need generation_shares")
    if generation_costs is not None and plant_costs is not None:
        raise BackcastError("give generation_costs or plant_costs, not both")
    placeholder = residual_shares is None and not by_fuel
    if by_fuel:
        residual_shares = _capacity_weighted_shares(
            generation_shares, generation_de.drop(columns=res_fuels), internal
        )
    elif placeholder:
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
        if residual_shares.isna().any():
            raise BackcastError("residual_shares has zones without a share")
        if (residual_shares < 0).any() or abs(residual_shares.sum() - 1) > _SHARE_SUM_TOLERANCE:
            raise BackcastError("residual_shares must be >= 0 and sum to 1")
        residual_shares = residual_shares.loc[internal]

    load = demand.loc[usable, internal].copy()
    for zone_id in exports.columns:
        load[zone_id] = exports.loc[usable, zone_id].clip(lower=0.0)

    rows: dict[str, tuple[str, float, float]] = {}
    availability: dict[str, pd.Series] = {}
    annual_demand = demand.loc[usable, internal].sum()
    demand_shares = annual_demand / annual_demand.sum()
    varying_cost: dict[str, pd.Series] = {}

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
    if by_fuel:
        costs = {} if generation_costs is None else dict(generation_costs)
        if plant_costs is not None:
            costs = dict.fromkeys(plant_costs.parameters.index, 0.0)
        unknown = sorted(set(costs) - set(generation_shares.index))
        if unknown:
            raise BackcastError(f"generation_costs for fuels without shares: {unknown}")
        if any(not cost >= 0 for cost in costs.values()):
            raise BackcastError("generation_costs must be >= 0")
        for fuel in generation_de.columns.difference(res_fuels):
            national = generation_de[fuel]
            if not national.max() > 0:
                continue
            share = generation_shares.loc[fuel]
            if fuel not in costs:
                for zone_id in internal:
                    add_profile(fuel, zone_id, national[usable] * float(share[zone_id]))
                continue
            if plant_costs is not None:
                _add_plant_classes(
                    fuel,
                    national,
                    share[share > 0],
                    plant_costs,
                    hours[usable],
                    rows,
                    availability,
                    varying_cost,
                )
                continue
            produced = national.index[national > 0]
            window = (hours[usable] >= produced[0]) & (hours[usable] <= produced[-1])
            for zone_id in internal:
                if share[zone_id] > 0:
                    name = generator_id(fuel, zone_id)
                    # The year's peak, also when that hour itself is left out.
                    rows[name] = (zone_id, float(national.max() * share[zone_id]), costs[fuel])
                    if not window.all():
                        availability[name] = pd.Series(window.astype(float), index=hours[usable])
        if plant_costs is not None:
            # SMARD's load, generation and flows do not balance hour by hour. With
            # capacities bounded by the observed output the difference would become
            # unserved load or a wrong fuel mix, so it is spread over the zones like
            # demand: as a supply where load is higher, as demand where supply is.
            renewable = sum(frame.sum(axis=1) for frame in injections.res_mw.values())
            supply = residual_de + renewable - exports.sum(axis=1)
            gap = (demand[internal].sum(axis=1) - supply)[usable]
            for zone_id in internal:
                add_profile(BALANCE, zone_id, gap.clip(lower=0.0) * float(demand_shares[zone_id]))
                load[zone_id] += (-gap).clip(lower=0.0) * float(demand_shares[zone_id])
    else:
        # The year's peak, also when that hour itself is left out.
        peak_residual = float(residual_de.max())
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
        marginal_cost=pd.DataFrame(varying_cost).rename_axis("snapshot") if varying_cost else None,
    )


def _add_plant_classes(
    fuel: str,
    national: pd.Series,
    share: pd.Series,
    plant_costs: PlantCosts,
    hours: pd.DatetimeIndex,
    rows: dict[str, tuple[str, float, float]],
    availability: dict[str, pd.Series],
    varying_cost: dict[str, pd.Series],
) -> None:
    """One generator per zone and efficiency class of ``fuel``, plus its must-run part.

    Capacity available in a day or week (``plant_costs.window``) is the fuel's highest
    national output of it; its lowest output of it runs at no cost (plants that stay on
    for heat, contracts or because starting and stopping costs money).
    """
    peak = float(national.max())
    local = national.index.tz_convert("Europe/Berlin").tz_localize(None)
    in_window = national.groupby(local.to_period(_WINDOWS[plant_costs.window]))
    must_run = (in_window.transform("min").fillna(0.0) / peak)[hours]
    flexible = (in_window.transform("max").fillna(0.0) / peak)[hours] - must_run
    # A fuel that stops for good within the window (nuclear in April 2023) has none after.
    produced = national.index[national > 0]
    flexible = flexible.where((hours >= produced[0]) & (hours <= produced[-1]), 0.0)

    parameters = plant_costs.parameters.loc[fuel]
    price = pd.Series(float(parameters["fuel_eur_per_mwh_th"]), index=hours)
    if plant_costs.fuel_prices is not None and fuel in plant_costs.fuel_prices.columns:
        months = pd.Index(hours.tz_convert("Europe/Berlin").month)
        monthly = plant_costs.fuel_prices[fuel]
        if not months.isin(monthly.dropna().index).all():
            raise BackcastError(f"fuel_prices lack months for {fuel}")
        price = pd.Series(monthly.reindex(months).to_numpy(), index=hours)
    heat = price + parameters["co2_price_eur_per_t"] * parameters["co2_t_per_mwh_th"]

    classes = plant_costs.efficiency[plant_costs.efficiency["technology"] == fuel]
    for zone_id, zone_share in share.items():
        if must_run.max() > 0:
            name = f"{generator_id(fuel, zone_id)}{CLASS_SEPARATOR}{MUST_RUN}"
            rows[name] = (zone_id, peak * zone_share, 0.0)
            availability[name] = must_run
        of_zone = classes[classes["zone_id"] == zone_id]
        if of_zone.empty:
            if not classes.empty:
                raise BackcastError(f"plant efficiency lacks {fuel} in zone {zone_id}")
            of_zone = pd.DataFrame({"efficiency": [parameters["efficiency"]], "share": [1.0]})
        for efficiency, class_share in zip(of_zone["efficiency"], of_zone["share"], strict=True):
            name = f"{generator_id(fuel, zone_id)}{CLASS_SEPARATOR}{efficiency:.2f}"
            cost = heat / efficiency + parameters["vom_eur_per_mwh"]
            rows[name] = (zone_id, peak * zone_share * class_share, float(cost.mean()))
            availability[name] = flexible
            if cost.nunique() > 1:
                varying_cost[name] = cost


def _capacity_weighted_shares(
    generation_shares: pd.DataFrame, generation_de: pd.DataFrame, internal: Sequence[str]
) -> pd.Series:
    """Check the fuel shares; return each zone's share of all fuels, by annual energy."""
    produced = [fuel for fuel in generation_de.columns if generation_de[fuel].max() > 0]
    missing = sorted(set(produced) - set(generation_shares.index))
    if missing:
        raise BackcastError(f"generation_shares lack the fuels {missing}")
    if set(generation_shares.columns) != set(internal):
        raise BackcastError("generation_shares must list every internal zone for every fuel")
    shares = generation_shares.loc[produced, list(internal)].astype(float)
    if shares.isna().any().any() or (shares < 0).any().any():
        raise BackcastError("generation_shares must be >= 0 and not missing")
    if ((shares.sum(axis=1) - 1).abs() > _SHARE_SUM_TOLERANCE).any():
        raise BackcastError("generation_shares must sum to 1 for every fuel")
    energy = generation_de[produced].sum()
    return shares.mul(energy / energy.sum(), axis=0).sum()


def read_generation_shares(path: str | Path) -> pd.DataFrame:
    """Read ``technology,zone_id,share`` into fuels x zone ids (missing pairs are 0)."""
    frame = pd.read_csv(path, comment="#", dtype={"zone_id": str, "technology": str})
    missing = {"technology", "zone_id", "share"} - set(frame.columns)
    if missing:
        raise BackcastError(f"{path}: missing columns {sorted(missing)}")
    if frame.duplicated(["technology", "zone_id"]).any():
        raise BackcastError(f"{path}: a technology and zone is listed more than once")
    if frame["share"].isna().any():
        raise BackcastError(f"{path}: rows without a share")
    return frame.pivot(index="technology", columns="zone_id", values="share").fillna(0.0)


_COST_COLUMNS = (
    "fuel_eur_per_mwh_th",
    "efficiency",
    "co2_t_per_mwh_th",
    "vom_eur_per_mwh",
    "co2_price_eur_per_t",
)


def read_plant_efficiency(path: str | Path) -> pd.DataFrame:
    """Read ``technology,zone_id,efficiency,share``; shares sum to 1 per fuel and zone."""
    frame = pd.read_csv(path, comment="#", dtype={"zone_id": str, "technology": str})
    missing = {"technology", "zone_id", "efficiency", "share"} - set(frame.columns)
    if missing:
        raise BackcastError(f"{path}: missing columns {sorted(missing)}")
    values = frame[["efficiency", "share"]]
    if values.isna().any().any() or (values <= 0).any().any() or (values > 1).any().any():
        raise BackcastError(f"{path}: efficiency and share must lie in (0, 1]")
    # generator ids carry the efficiency with two decimals
    if (
        frame.assign(efficiency=frame["efficiency"].map("{:.2f}".format))
        .duplicated(["technology", "zone_id", "efficiency"])
        .any()
    ):
        raise BackcastError(
            f"{path}: an efficiency class is listed more than once (classes must differ "
            "at two decimals)"
        )
    totals = frame.groupby(["technology", "zone_id"])["share"].sum()
    if ((totals - 1).abs() > _SHARE_SUM_TOLERANCE).any():
        raise BackcastError(f"{path}: shares must sum to 1 per technology and zone")
    return frame


def read_fuel_prices(path: str | Path) -> pd.DataFrame:
    """Read ``month,technology,fuel_eur_per_mwh_th`` into months (1-12) x technologies."""
    frame = pd.read_csv(path, comment="#", dtype={"technology": str})
    missing = {"month", "technology", "fuel_eur_per_mwh_th"} - set(frame.columns)
    if missing:
        raise BackcastError(f"{path}: missing columns {sorted(missing)}")
    if frame.duplicated(["month", "technology"]).any():
        raise BackcastError(f"{path}: a month and technology is listed more than once")
    if not frame["month"].isin(range(1, 13)).all():
        raise BackcastError(f"{path}: month must be 1 to 12")
    if not (frame["fuel_eur_per_mwh_th"] >= 0).all():
        raise BackcastError(f"{path}: prices must be >= 0 and not missing")
    return frame.pivot(index="month", columns="technology", values="fuel_eur_per_mwh_th")


def read_cost_parameters(path: str | Path) -> pd.DataFrame:
    """The cost file of :func:`read_generation_costs` as technology x parameters."""
    frame = pd.read_csv(path, comment="#", dtype={"technology": str})
    missing = {"technology", *_COST_COLUMNS} - set(frame.columns)
    if missing:
        raise BackcastError(f"{path}: missing columns {sorted(missing)}")
    if frame["technology"].duplicated().any():
        raise BackcastError(f"{path}: a technology is listed more than once")
    values = frame.set_index("technology")[list(_COST_COLUMNS)].astype(float)
    if values.isna().any().any() or (values < 0).any().any() or (values["efficiency"] <= 0).any():
        raise BackcastError(f"{path}: values must be >= 0, efficiency > 0, none missing")
    return values


def read_generation_costs(path: str | Path) -> pd.Series:
    """Marginal cost per technology in EUR/MWh of electricity from a cost file.

    cost = (fuel + co2_price x co2_intensity) / efficiency + vom
    """
    values = read_cost_parameters(path)
    heat = (
        values["fuel_eur_per_mwh_th"] + values["co2_price_eur_per_t"] * values["co2_t_per_mwh_th"]
    )
    return (heat / values["efficiency"] + values["vom_eur_per_mwh"]).rename("cost_eur_per_mwh")


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
    return pd.DataFrame(limited, index=exports.index), clipped


def run_backcast(
    network: ZoneNetwork,
    inputs: BackcastInputs,
    hours: Sequence | None = None,
    load_shedding_cost: float | None = None,
    threshold: float = 0.0,
    block_hours: int | None = None,
    workers: int = 1,
) -> BackcastResult:
    """Solve the DC OPF for the backcast and compute the fee signal.

    The PTDF, and with it the fee signal, uses a slack spread over the German
    zones in proportion to their demand: the signal of a zone is measured against
    the demand-weighted average of Germany. The signal covers the German zones
    only. Boundary zones are not fee zones, and where an observed flow was cut to
    the line capacity their prices are not determined by the model.

    Hours with unserved load are not part of the signal: their shadow prices are
    set by ``load_shedding_cost``, not by the data. They stay in ``opf``.

    ``block_hours`` and ``workers`` are those of :func:`gridfee.flows.run_dc_opf`:
    a large network needs the year solved in blocks of hours.
    """
    opf = run_dc_opf(
        network,
        load_mw=inputs.load_mw,
        generators=inputs.generators,
        availability=inputs.availability,
        marginal_cost=inputs.marginal_cost,
        snapshots=hours,
        load_shedding_cost=load_shedding_cost,
        ptdf_reference=inputs.ptdf_reference,
        block_hours=block_hours,
        workers=workers,
    )
    ptdf = opf.ptdf[network.internal_zone_ids]
    prices = opf.shadow_prices
    if opf.load_shedding_mw is not None:
        unserved = opf.load_shedding_mw.sum(axis=1) > SHEDDING_TOLERANCE_MW
        prices = prices[~unserved]
    return BackcastResult(inputs, opf, compute_fee_signal(prices, ptdf, threshold))
