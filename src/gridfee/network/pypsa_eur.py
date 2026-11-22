"""Derive the lines of a zone network from PyPSA-Eur's public transmission grid.

The input is the "prebuilt electricity network" that PyPSA-Eur publishes (built
from OpenStreetMap, ODbL): ``buses.csv``, ``lines.csv`` and ``links.csv``. The
buses are mapped to zones and the grid is aggregated with PyPSA's
``get_clustering_from_busmap``, so no PyPSA-Eur or Snakemake run is needed.

The result is an assumption about the grid, not TSO data: see docs/NETWORK.md.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
from pypsa.clustering.spatial import get_clustering_from_busmap

logger = logging.getLogger(__name__)

MIN_VOLTAGE_KV = 220
# Share of the thermal rating usable in normal operation, PyPSA-Eur's stand-in for
# the N-1 security margin (``lines: s_max_pu`` in its config.default.yaml).
PYPSA_EUR_S_MAX_PU = 0.7
LINE_COLUMNS = ("line_id", "from_zone", "to_zone", "susceptance_mw_per_rad", "capacity_mw")


def read_prebuilt_network(directory: str | Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read buses, AC lines and DC links that are in service.

    AC buses and lines below 220 kV are dropped, as PyPSA-Eur does by default.
    """
    directory = Path(directory)
    buses = pd.read_csv(directory / "buses.csv", index_col="bus_id")
    lines = pd.read_csv(directory / "lines.csv", index_col="line_id", quotechar="'")
    links = pd.read_csv(directory / "links.csv", index_col="link_id", quotechar="'")

    buses = buses[buses["under_construction"] == "f"]
    is_dc = buses["dc"] == "t"
    buses = buses[is_dc | (buses["voltage"] >= MIN_VOLTAGE_KV)]

    def in_service(frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame[frame["under_construction"] == "f"]
        return frame[frame["bus0"].isin(buses.index) & frame["bus1"].isin(buses.index)]

    lines = in_service(lines[lines["voltage"] >= MIN_VOLTAGE_KV])
    return buses, lines, in_service(links)


def derive_zone_lines(
    buses: pd.DataFrame,
    lines: pd.DataFrame,
    links: pd.DataFrame,
    busmap: pd.Series,
    s_max_pu: float = PYPSA_EUR_S_MAX_PU,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate the grid to one line per pair of zones.

    ``busmap`` maps bus ids to zone ids; buses it does not list are left out
    together with their lines and links. Returns the lines (``LINE_COLUMNS``, plus
    the parts they are made of) and the zone coordinates (``lon``, ``lat``: mean of
    the mapped buses).

    AC lines between two zones become one equivalent line: PyPSA sums the thermal
    ratings and combines the reactances in parallel after scaling each to the
    distance between the two zone centres. ``capacity_mw`` is ``s_max_pu`` times the
    summed rating. DC links between two zones add their rating to the capacity; a
    pair connected by DC only gets a susceptance equal to its capacity, a
    convention that has no effect when one of the two zones has no other line.
    """
    busmap = busmap[busmap.index.isin(buses.index)]
    buses = buses.loc[busmap.index]
    ac = buses[buses["dc"] != "t"]
    lines = lines[lines["bus0"].isin(ac.index) & lines["bus1"].isin(ac.index)]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        n = pypsa.Network()
        n.add("Bus", ac.index, v_nom=ac["voltage"], x=ac["x"], y=ac["y"])
        n.add(
            "Line",
            lines.index,
            bus0=lines["bus0"],
            bus1=lines["bus1"],
            x=lines["x"],
            r=lines["r"],
            s_nom=lines["s_nom"],
            length=lines["length"] / 1000.0,
        )
        clustered = get_clustering_from_busmap(n, busmap.loc[ac.index]).n

    aggregated = clustered.lines
    v_nom = aggregated["bus0"].map(clustered.buses["v_nom"])
    rows = pd.DataFrame(
        {
            "from_zone": aggregated["bus0"],
            "to_zone": aggregated["bus1"],
            "susceptance_mw_per_rad": v_nom**2 / aggregated["x"],
            "ac_rating_mva": aggregated["s_nom"],
            "dc_rating_mw": 0.0,
        }
    ).set_index(["from_zone", "to_zone"])

    link_zones = pd.DataFrame(
        {"a": links["bus0"].map(busmap), "b": links["bus1"].map(busmap), "p": links["p_nom"]}
    ).dropna()
    link_zones = link_zones[link_zones["a"] != link_zones["b"]]
    for row in link_zones.itertuples():
        pair = tuple(sorted((row.a, row.b)))
        if pair not in rows.index:
            rows.loc[pair, ["susceptance_mw_per_rad", "ac_rating_mva", "dc_rating_mw"]] = 0.0
        rows.loc[pair, "dc_rating_mw"] += row.p

    rows = rows.sort_index().reset_index()
    rows["capacity_mw"] = s_max_pu * rows["ac_rating_mva"] + rows["dc_rating_mw"]
    dc_only = rows["ac_rating_mva"] == 0
    rows.loc[dc_only, "susceptance_mw_per_rad"] = rows.loc[dc_only, "capacity_mw"]
    rows.insert(0, "line_id", rows["from_zone"] + "-" + rows["to_zone"])

    degree = pd.concat([rows["from_zone"], rows["to_zone"]]).value_counts()
    for row in rows[rows["dc_rating_mw"] > 0].itertuples():
        if min(degree[row.from_zone], degree[row.to_zone]) > 1:
            logger.warning(
                "%s has a DC link but both zones have other lines: it is modelled as an "
                "AC line, so its flow follows the angles instead of being controlled",
                row.line_id,
            )

    # Zones reached by DC links only have no AC bus, so their DC buses count too.
    centres = ac[["x", "y"]].groupby(busmap.loc[ac.index]).mean()
    dc_centres = buses[["x", "y"]].groupby(busmap).mean()
    centres = centres.combine_first(dc_centres)
    centres = centres.rename(columns={"x": "lon", "y": "lat"}).rename_axis("zone_id")
    return rows, centres


def apply_published_limits(
    zone_lines: pd.DataFrame,
    circuits: pd.DataFrame,
    s_max_pu: float = PYPSA_EUR_S_MAX_PU,
) -> pd.DataFrame:
    """Replace the AC rating of the lines that ``circuits`` lists by published limits.

    ``circuits`` has one row per physical circuit between two zones: ``line_id``,
    ``voltage_kv`` and ``max_current_a``. A listed line's ``ac_rating_mva`` becomes
    the sum of ``sqrt(3) * voltage * current`` over its circuits, and its
    ``capacity_mw`` is recomputed with the same ``s_max_pu``. Susceptances and the
    other lines stay as derived.
    """
    unknown = sorted(set(circuits["line_id"]) - set(zone_lines["line_id"]))
    if unknown:
        raise ValueError(f"published limits for lines that do not exist: {unknown}")
    mva = np.sqrt(3) * circuits["voltage_kv"] * circuits["max_current_a"] / 1000.0
    rating = mva.groupby(circuits["line_id"]).sum()
    result = zone_lines.copy()
    listed = result["line_id"].isin(rating.index)
    result.loc[listed, "ac_rating_mva"] = result.loc[listed, "line_id"].map(rating)
    result["capacity_mw"] = s_max_pu * result["ac_rating_mva"] + result["dc_rating_mw"]
    return result
