"""Checks of a backcast against observed SMARD data.

Every check returns rows with the columns ``CHECK_COLUMNS``. ``status`` is
``pass`` or ``fail`` for checks with a tolerance and ``info`` for comparisons that
are reported without a verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from gridfee.backcast import (
    CLASS_SEPARATOR,
    GENERATOR_SEPARATOR,
    IMPORT,
    RESIDUAL,
    neighbour_net_export,
    neighbour_of,
)
from gridfee.data.smard import TSO_RES_FILTERS
from gridfee.network import ZoneNetwork
from gridfee.two_run import RENEWABLE_KINDS

PASS = "pass"
FAIL = "fail"
INFO = "info"
CHECK_COLUMNS = [
    "check",
    "item",
    "observed",
    "modelled",
    "unit",
    "deviation",
    "tolerance",
    "status",
]
WIND = ("wind_onshore", "wind_offshore")
# The control areas that reach the North Sea and Baltic coasts.
NORTH_TSO_ZONES = ("50Hertz", "TenneT")
_MWH_PER_TWH = 1e6
_SMARD_REDISPATCH_FUELS = "https://www.smard.de/page/home/topic-article/211972/213384/netzengpassmanagement-je-energietraeger"
_SMARD_2023 = (
    "https://www.smard.de/page/home/topic-article/444/213590/netzengpassmanagement-im-jahr-2023"
)
_REDISPATCH_2023_TWH = (10.87648, 2.97231, 10.47942, float("nan"))


class ValidationError(ValueError):
    """Model results and observations do not fit together."""


def _row(check, item, observed, modelled, unit, deviation, tolerance=None) -> dict:
    status = INFO if tolerance is None else PASS if deviation <= tolerance else FAIL
    return dict(
        zip(
            CHECK_COLUMNS,
            [check, item, observed, modelled, unit, deviation, tolerance, status],
            strict=True,
        )
    )


def check_redispatch_volumes(
    redispatch_mw: pd.DataFrame, year: int
) -> tuple[pd.DataFrame, list[str]]:
    """Compare served-hour movements (grid minus market) with annual SMARD context."""
    kinds = redispatch_mw.columns.str.split(GENERATOR_SEPARATOR, n=1).str[0]
    renewable = kinds.isin(RENEWABLE_KINDS)
    imports = kinds == IMPORT
    conventional = kinds.isin(
        ("lignite", "hard_coal", "gas", "nuclear", "other_conventional", "pumped_storage", RESIDUAL)
    )
    volumes = (
        redispatch_mw.loc[:, conventional].clip(lower=0.0).to_numpy().sum(),
        -redispatch_mw.loc[:, conventional].clip(upper=0.0).to_numpy().sum(),
        -redispatch_mw.loc[:, renewable].clip(upper=0.0).to_numpy().sum(),
        -redispatch_mw.loc[:, imports].clip(upper=0.0).to_numpy().sum(),
    )
    items = ("conventional raised", "conventional lowered", "renewable curtailment", "import cuts")
    observed = _REDISPATCH_2023_TWH if year == 2023 else (float("nan"),) * len(items)
    rows = [
        _row("redispatch volume", item, target, volume / _MWH_PER_TWH, "TWh", float("nan"))
        for item, target, volume in zip(items, observed, volumes, strict=True)
    ]
    notes = [
        f"Modelled redispatch volumes cover {len(redispatch_mw):,} served hours only. They exclude "
        "input flow clipping and synthetic balancing feed-in. If generation is aggregated, the "
        "residual generator counts as conventional."
    ]
    if year == 2023:
        notes.extend(
            [
                f"Published observations are whole-year Germany totals from [SMARD by energy "
                f"source]({_SMARD_REDISPATCH_FUELS}), summed by month and fuel. Conventional "
                "includes pumped storage; unknown fuel (e.g. exchange trades) is excluded. "
                "They are not restricted to the model's hours or current-driven measures.",
                f"No published import-cuts-only volume was identified. [SMARD reports "
                f"6.016 TWh of countertrading]({_SMARD_2023}) in 2023, a broader category, not "
                "a like-for-like import cut observation.",
            ]
        )
    return pd.DataFrame(rows, columns=CHECK_COLUMNS), notes


def _same_hours(observed: pd.DataFrame, modelled: pd.DataFrame, label: str) -> pd.DataFrame:
    missing = modelled.index.difference(observed.index)
    if len(missing):
        raise ValidationError(f"{label}: no observation for {len(missing)} modelled hours")
    return observed.loc[modelled.index]


def _dispatch_by_kind(dispatch_mw: pd.DataFrame) -> pd.DataFrame:
    """Columns as (kind, zone id) from generator ids ``<kind>:<zone id>``."""
    parts = dispatch_mw.columns.str.split(GENERATOR_SEPARATOR, n=1)
    if (parts.str.len() != 2).any():
        raise ValidationError("generator ids must look like '<kind>:<zone id>'")
    # "<kind>:<zone id>@<class>": the classes of a kind in a zone are added up.
    pairs = [(kind, zone.split(CLASS_SEPARATOR)[0]) for kind, zone in parts]
    columns = pd.MultiIndex.from_tuples(pairs, names=["kind", "zone"])
    return (
        dispatch_mw.set_axis(columns, axis=1).T.groupby(level=["kind", "zone"], sort=False).sum().T
    )


def check_wind_north_share(
    network: ZoneNetwork,
    dispatch_mw: pd.DataFrame,
    wind_tso_mw: Mapping[str, pd.DataFrame],
    north_tso_zones: Sequence[str] = NORTH_TSO_ZONES,
    tolerance: float = 0.10,
) -> pd.DataFrame:
    """Share of wind energy fed in in the northern control areas, model vs SMARD.

    The modelled share is taken from the dispatch, i.e. after the curtailment the
    OPF needs to keep the lines within their capacity; SMARD reports what was
    actually fed in. Before curtailment the two are equal by construction, because
    the zonal wind is the SMARD series of the TSO zone split over its zones. The
    check therefore fails if the model curtails wind in the north (or the south)
    far more than happened. ``deviation`` is relative to the observed share.
    """
    by_kind = _dispatch_by_kind(dispatch_mw)
    modelled = by_kind.loc[:, by_kind.columns.get_level_values("kind").isin(WIND)]
    if modelled.empty:
        raise ValidationError("the dispatch has no wind generators")
    tso_of = modelled.columns.get_level_values("zone").map(network.zone_to_tso)
    modelled_tso = modelled.T.groupby(tso_of).sum().T.sum()

    observed_tso = pd.Series(0.0, index=modelled_tso.index)
    for tech in WIND:
        if tech in wind_tso_mw:
            frame = _same_hours(wind_tso_mw[tech], dispatch_mw, tech)
            observed_tso = observed_tso.add(frame.sum(), fill_value=0.0)
    if observed_tso.sum() <= 0 or modelled_tso.sum() <= 0:
        raise ValidationError("no wind energy to compare")

    def north_share(energy: pd.Series) -> float:
        return float(energy[energy.index.isin(north_tso_zones)].sum() / energy.sum())

    observed, model = north_share(observed_tso), north_share(modelled_tso)
    if observed <= 0:
        raise ValidationError("no observed wind in the northern TSO zones")
    item = "north = " + " + ".join(north_tso_zones)
    row = _row(
        "wind north share",
        item,
        observed,
        model,
        "share",
        abs(model - observed) / observed,
        tolerance,
    )
    return pd.DataFrame([row], columns=CHECK_COLUMNS)


def check_cross_border(
    network: ZoneNetwork,
    flows_mw: pd.DataFrame,
    physical_flows_tso: pd.DataFrame,
    tolerance: float = 0.10,
) -> pd.DataFrame:
    """Net export per neighbour, and per TSO zone and neighbour, model vs SMARD.

    Per neighbour (pass/fail): observed flows are an input of the backcast, so the
    model reproduces them unless a line into the boundary zone is too small; a
    failure points at the derived line capacities. ``deviation`` is the difference
    of the net energy divided by the observed gross exchange (sum of the hourly
    absolute values) with that neighbour.

    Per TSO zone and neighbour (info): how the model splits a neighbour's flow
    over the control areas. Each model zone counts towards the TSO of its largest
    part, so these rows are approximate by construction.
    """
    observed_tso = _same_hours(physical_flows_tso, flows_mw, "cross-border flows")
    observed_tso = observed_tso.dropna(axis=1, how="all")
    if observed_tso.isna().any().any():
        raise ValidationError("observed cross-border flows are missing in modelled hours")
    observed = neighbour_net_export(observed_tso)

    boundary = set(network.boundary_zone_ids)
    tso_of = network.zone_to_tso
    modelled: dict[tuple[str, str], pd.Series] = {}
    for line in network.lines:
        if (line.from_zone in boundary) == (line.to_zone in boundary):
            continue
        outward = line.to_zone in boundary
        neighbour = neighbour_of(line.to_zone if outward else line.from_zone)
        key = (tso_of[line.from_zone if outward else line.to_zone], neighbour)
        flow = flows_mw[line.line_id] if outward else -flows_mw[line.line_id]
        modelled[key] = modelled.get(key, 0.0) + flow

    rows = []
    for neighbour in sorted(set(observed.columns) | {n for _, n in modelled}):
        model = sum(
            (s for (_, n), s in modelled.items() if n == neighbour), 0.0 * flows_mw.iloc[:, 0]
        )
        obs = observed.get(neighbour, 0.0 * model)
        gross = float(obs.abs().sum())
        difference = abs(float(model.sum() - obs.sum()))
        deviation = difference / gross if gross > 0 else (0.0 if difference == 0 else float("inf"))
        rows.append(
            _row(
                "net export per neighbour",
                neighbour,
                obs.sum() / _MWH_PER_TWH,
                model.sum() / _MWH_PER_TWH,
                "TWh",
                deviation,
                tolerance,
            )
        )
    observed_keys = {tuple(column.split("_", 1)) for column in observed_tso.columns}
    for tso, neighbour in sorted(observed_keys | set(modelled)):
        column = f"{tso}_{neighbour}"
        obs = observed_tso[column].sum() if column in observed_tso else float("nan")
        model = modelled[(tso, neighbour)].sum() if (tso, neighbour) in modelled else float("nan")
        rows.append(
            _row(
                "net export per TSO zone and neighbour",
                f"{tso} - {neighbour}",
                obs / _MWH_PER_TWH,
                model / _MWH_PER_TWH,
                "TWh",
                abs(model - obs) / _MWH_PER_TWH,
            )
        )
    return pd.DataFrame(rows, columns=CHECK_COLUMNS)


def check_residual_generation(
    dispatch_mw: pd.DataFrame, generation_de: pd.DataFrame
) -> pd.DataFrame:
    """National generation other than wind and solar, model vs SMARD (info).

    Also reports the wind and solar energy and the imports the model curtailed.
    """
    by_kind = _dispatch_by_kind(dispatch_mw).T.groupby(level="kind").sum().T.sum()
    observed = _same_hours(generation_de, dispatch_mw, "national generation")
    res = [fuel for fuel in TSO_RES_FILTERS if fuel in observed.columns]
    observed_residual = observed.drop(columns=res).sum().sum()
    # One generator kind for all of it, or one per fuel (gridfee.backcast).
    fuels = [fuel for fuel in observed.columns.difference(res) if fuel in by_kind]
    modelled_residual = by_kind.get(RESIDUAL, 0.0) + sum(by_kind[fuel] for fuel in fuels)
    rows = [
        _row(
            "generation other than wind and solar",
            "Germany",
            observed_residual / _MWH_PER_TWH,
            modelled_residual / _MWH_PER_TWH,
            "TWh",
            abs(modelled_residual - observed_residual) / observed_residual,
        )
    ]
    for fuel in [*fuels, *res]:
        if fuel in by_kind:
            energy = observed[fuel].sum()
            rows.append(
                _row(
                    f"{fuel} generated" if fuel in fuels else f"{fuel} fed in",
                    "Germany",
                    energy / _MWH_PER_TWH,
                    by_kind[fuel] / _MWH_PER_TWH,
                    "TWh",
                    abs(by_kind[fuel] - energy) / energy,
                )
            )
    return pd.DataFrame(rows, columns=CHECK_COLUMNS)


def validate_backcast(
    network: ZoneNetwork,
    flows_mw: pd.DataFrame,
    dispatch_mw: pd.DataFrame,
    wind_tso_mw: Mapping[str, pd.DataFrame],
    physical_flows_tso: pd.DataFrame,
    generation_de: pd.DataFrame,
    wind_tolerance: float = 0.10,
    flow_tolerance: float = 0.10,
) -> pd.DataFrame:
    """All checks as one table. Observations must be indexed like the results."""
    return pd.concat(
        [
            check_wind_north_share(network, dispatch_mw, wind_tso_mw, tolerance=wind_tolerance),
            check_cross_border(network, flows_mw, physical_flows_tso, flow_tolerance),
            check_residual_generation(dispatch_mw, generation_de),
        ],
        ignore_index=True,
    )
