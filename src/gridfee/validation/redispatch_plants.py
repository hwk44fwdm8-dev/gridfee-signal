"""Where the plants named in the published redispatch stand in the substation list.

netztransparenz.de names the plant of every redispatch measure, not its place in the
grid. ``data/redispatch/plants_2023.csv`` lists every name of the 2023 export with how
it was located: by the plant list entries it stands for, by a connection point from
another source, by the substation, the landing point or the substation code its name
gives, or not at all, with the reason.
:func:`locate_plants` turns the plant list entries into a ``substation_id``.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from gridfee.validation.checks import ValidationError
from gridfee.validation.redispatch import CURRENT_REDISPATCH, hourly_redispatch

PLANT_LIST = "plant_list"
SAME_SITE = "same_site"
SUBSTATION_NAME = "substation_name"
LANDING_POINT = "landing_point"
CONNECTION_POINT = "connection_point"
CLUSTER_CODE = "cluster_code"
NOT_LOCATED = "none"
LOCATED_BY = (
    PLANT_LIST,
    SAME_SITE,
    CONNECTION_POINT,
    SUBSTATION_NAME,
    LANDING_POINT,
    CLUSTER_CODE,
    NOT_LOCATED,
)
COLUMNS = [
    "plant",
    "located_by",
    "plant_list_ids",
    "substation_id",
    "basis",
    "distance_km",
    "reason",
    "note",
]
LEVELS = ("substation_id", "report_zone", "nuts2")
# A connection point of the plant list further away than this is not used: the list
# names the far end of a line for some plants (Opladen for Neurath).
MAX_NODE_KM = 20.0
_EARTH_RADIUS_KM = 6371.0
_NODE_PREFIX = re.compile(
    r"^(?:uw|ua|sa|umspannwerk|(?:110|220|380) ?kv[ -]schaltanlage)[ -]+", re.IGNORECASE
)
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _normalised(name: str) -> str:
    return _NODE_PREFIX.sub("", name.strip()).lower().translate(_UMLAUTS)


def _distance_km(lat: float, lon: float, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    lat, lon, lats, lons = (np.radians(value) for value in (lat, lon, lats, lons))
    half = (
        np.sin((lats - lat) / 2) ** 2 + np.cos(lat) * np.cos(lats) * np.sin((lons - lon) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(half))


def read_plant_table(path: str | Path) -> pd.DataFrame:
    """Read ``plants_2023.csv``: one row per plant name of the redispatch export."""
    path = Path(path)
    table = pd.read_csv(path, comment="#", dtype=str, keep_default_na=False)
    missing = set(COLUMNS) - set(table.columns)
    if missing:
        raise ValidationError(f"{path}: missing column(s) {sorted(missing)}")
    if table["plant"].duplicated().any():
        raise ValidationError(f"{path}: plant name(s) listed twice")
    unknown = set(table["located_by"]) - set(LOCATED_BY)
    if unknown:
        raise ValidationError(f"{path}: unknown located_by {sorted(unknown)}")
    located = table["located_by"] != NOT_LOCATED
    if (table.loc[~located, "reason"] == "").any() or (table.loc[~located, "substation_id"]).any():
        raise ValidationError(f"{path}: a plant that is not located needs a reason, no substation")
    return table[COLUMNS].assign(distance_km=pd.to_numeric(table["distance_km"]))


def locate_plants(
    plants: pd.DataFrame, plant_list: pd.DataFrame, substations: pd.DataFrame
) -> pd.DataFrame:
    """Fill ``substation_id``, ``basis`` and ``distance_km`` of the located plants.

    ``plant_list`` is Open Power System Data's list of conventional plants (``id``,
    ``lat``, ``lon``, ``network_node``), ``substations`` the substation list. A plant
    with ``plant_list_ids`` stands at the mean position of those entries. It goes to
    the substation the entries name as their connection point (basis
    ``network_node``) if they agree on one of the list within ``MAX_NODE_KM``, else
    to the nearest substation (basis ``nearest``). Tee points are not used. Plants
    located by their name keep their substation (basis ``name``).
    """
    places = substations[substations["kind"] == "substation"].reset_index(drop=True)
    by_name = dict(zip(places["name"].map(_normalised), places.index, strict=True))
    entries = plant_list.drop_duplicates("id").set_index("id")
    known = set(substations["substation_id"])
    rows = []
    for row in plants.to_dict("records"):
        if row["located_by"] in (PLANT_LIST, SAME_SITE):
            ids = row["plant_list_ids"].split()
            unlisted = [entry for entry in ids if entry not in entries.index]
            if not ids or unlisted:
                raise ValidationError(f"{row['plant']}: plant list id(s) {unlisted} not found")
            listed = entries.loc[ids]
            distance = _distance_km(
                listed["lat"].mean(), listed["lon"].mean(), places["lat"], places["lon"]
            )
            nodes = {by_name.get(_normalised(str(node))) for node in listed["network_node"]}
            node = nodes.pop() if len(nodes) == 1 else None
            if node is not None and distance[node] <= MAX_NODE_KM:
                basis, place = "network_node", node
            else:
                basis, place = "nearest", int(distance.argmin())
            row |= {
                "substation_id": places.loc[place, "substation_id"],
                "basis": basis,
                "distance_km": round(float(distance[place]), 1),
            }
        elif row["located_by"] in (CONNECTION_POINT, SUBSTATION_NAME, LANDING_POINT, CLUSTER_CODE):
            if row["substation_id"] not in known:
                raise ValidationError(f"{row['plant']}: unknown substation {row['substation_id']}")
            row |= {"basis": "name", "distance_km": np.nan}
        else:
            row |= {"substation_id": "", "basis": "", "distance_km": np.nan}
        rows.append(row)
    return pd.DataFrame(rows, columns=COLUMNS)


def plant_locations(plants: pd.DataFrame, substations: pd.DataFrame) -> pd.DataFrame:
    """The located plants, indexed by name, with ``substation_id``, ``report_zone``, ``nuts2``."""
    located = plants[plants["located_by"] != NOT_LOCATED]
    places = substations.set_index("substation_id")[["report_zone", "nuts2"]]
    unknown = set(located["substation_id"]) - set(places.index)
    if unknown:
        raise ValidationError(f"substation(s) {sorted(unknown)} are not in the substation list")
    return located.set_index("plant")[["substation_id"]].join(places, on="substation_id")


def _of_reason(measures: pd.DataFrame, reason: str | None) -> pd.DataFrame:
    if "plant" not in measures.columns:
        raise ValidationError("the redispatch export has no column BETROFFENE_ANLAGE")
    return measures if reason is None else measures[measures["reason"] == reason]


def redispatch_coverage(
    measures: pd.DataFrame, plants: pd.DataFrame, reason: str | None = CURRENT_REDISPATCH
) -> pd.DataFrame:
    """Plants, measures and energy per way of locating and, if not located, reason.

    ``reason`` selects the measures as in :func:`hourly_redispatch`; None takes all.
    Plant names the table does not list count as ``none`` / ``not_in_table``.
    """
    rows = _of_reason(measures, reason).merge(
        plants[["plant", "located_by", "reason"]].rename(columns={"reason": "why"}),
        on="plant",
        how="left",
    )
    rows = rows.fillna({"located_by": NOT_LOCATED, "why": "not_in_table"})
    table = (
        rows.groupby(["located_by", "why"])
        .agg(plants=("plant", "nunique"), measures=("plant", "size"), gwh=("energy_mwh", "sum"))
        .rename_axis(["located_by", "reason"])
    )
    table["gwh"] /= 1e3
    table["share"] = table["gwh"] / table["gwh"].sum()
    order = {name: rank for rank, name in enumerate(LOCATED_BY)}
    return table.sort_index(key=lambda level: level.map(lambda name: order.get(name, name)))


def hourly_redispatch_by_location(
    measures: pd.DataFrame,
    locations: pd.DataFrame,
    level: str = "substation_id",
    reason: str = CURRENT_REDISPATCH,
) -> pd.DataFrame:
    """Energy per hour (UTC) of the located plants, with columns (``level``, direction).

    ``locations`` is :func:`plant_locations`; ``level`` one of ``LEVELS``. Measures
    of plants without a location are left out.
    """
    if level not in LEVELS:
        raise ValidationError(f"unknown level {level!r}, expected one of {LEVELS}")
    placed = _of_reason(measures, reason).join(locations[level], on="plant", how="inner")
    return hourly_redispatch(placed, reason, by=level)


def _markdown(table: pd.DataFrame) -> str:
    frame = table.reset_index()
    formats = {"gwh": "{:,.1f}", "share": "{:.1%}"}
    cells = [
        [
            formats.get(column, "{}").format(value)
            for column, value in zip(frame.columns, row, strict=True)
        ]
        for row in frame.itertuples(index=False)
    ]
    lines = ["| " + " | ".join(frame.columns) + " |", "|" + " --- |" * len(frame.columns)]
    return "\n".join(lines + ["| " + " | ".join(row) + " |" for row in cells])


def render_coverage(table: pd.DataFrame) -> str:
    """Markdown table of :func:`redispatch_coverage` with a line on the located share."""
    located = table[table.index.get_level_values("located_by") != NOT_LOCATED]
    return (
        f"{_markdown(table)}\n\nLocated on a substation: {located['gwh'].sum():,.1f} of "
        f"{table['gwh'].sum():,.1f} GWh ({located['share'].sum():.1%}), "
        f"{located['plants'].sum()} of {table['plants'].sum()} plant names.\n"
    )
