"""Maps and time series of a results directory, as PNG files (needs matplotlib)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.collections import LineCollection, PolyCollection  # noqa: E402
from matplotlib.colors import Normalize, TwoSlopeNorm  # noqa: E402

from gridfee.network import ZoneNetwork  # noqa: E402
from gridfee.plots.summary import (  # noqa: E402
    PlotError,
    ResultTables,
    daily_line_hours,
    daily_signal,
    daily_unserved,
    line_summary,
    zone_summary,
)

EMPTY = "#e6e6e6"
OUTLINE = "#8a8a8a"
DPI = 150
# Zones shown by name in the unserved-load figure; the rest are summed as "other".
MAX_UNSERVED_ZONES = 8

Regions = dict[str, list[np.ndarray]]


def read_regions(path: str | Path, key: str = "NUTS_ID") -> Regions:
    """Outlines per zone from a GeoJSON file in longitude and latitude (EPSG:4326).

    ``key`` names the feature property that holds the zone id. For the 38-region
    network that is ``NUTS_ID`` of Eurostat GISCO's NUTS 2021 level-2 file.
    """
    try:
        features = json.loads(Path(path).read_text(encoding="utf-8"))["features"]
    except (OSError, ValueError, KeyError) as err:
        raise PlotError(f"{path} is not a readable GeoJSON feature collection") from err
    regions: Regions = {}
    for feature in features:
        zone_id = (feature.get("properties") or {}).get(key)
        geometry = feature.get("geometry") or {}
        if zone_id is None or geometry.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        polygons = geometry["coordinates"]
        if geometry["type"] == "Polygon":
            polygons = [polygons]
        # Outer ring of every part; holes are not drawn.
        rings = [np.asarray(polygon[0], dtype=float)[:, :2] for polygon in polygons]
        regions.setdefault(str(zone_id), []).extend(rings)
    if not regions:
        raise PlotError(f"{path} has no polygon with the property {key!r}")
    return regions


def plot_results(
    tables: ResultTables,
    network: ZoneNetwork,
    out_dir: str | Path,
    threshold: float = 0.0,
    regions: Regions | None = None,
) -> list[Path]:
    """Write every figure the results allow and return the files written.

    Maps need coordinates for every zone of ``network``; without them only the time
    series are written. With ``regions`` the zones are drawn as areas, otherwise as
    points.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    internal = network.internal_zone_ids
    missing = sorted(set(internal) - set(tables.signal.columns))
    if missing:
        raise PlotError(f"the fee signal lacks the zones {missing} of the network")
    signal = tables.signal[internal]
    zones = network.zones_frame()
    lines = network.lines_frame()
    line_table = line_summary(tables, lines["capacity_mw"], threshold)

    figures = {}
    if zones[["lat", "lon"]].notna().all().all():
        zone_table = zone_summary(signal, threshold)
        figures["map_signal_hours"] = _map_signal_hours(zone_table, zones, lines, regions)
        figures["map_mean_signal"] = _map_mean_signal(zone_table, zones, lines, regions)
        figures["map_lines"] = _map_lines(tables, line_table, zones, lines, regions)
    figures["timeseries_signal"] = _timeseries_signal(signal, zones, threshold)
    figures["timeseries_lines"] = _timeseries_lines(tables, threshold)
    figures["timeseries_unserved"] = _timeseries_unserved(tables)

    paths = []
    for name, figure in figures.items():
        if figure is None:
            continue
        path = out_dir / f"{name}.png"
        figure.savefig(path, dpi=DPI, bbox_inches="tight")
        plt.close(figure)
        paths.append(path)
    return paths


# Maps


def _map_axes(count: int):
    figure, axes = plt.subplots(1, count, figsize=(6.2 * count, 7.4), squeeze=False)
    return figure, list(axes[0])


def _draw_base(ax, zones: pd.DataFrame, lines: pd.DataFrame, regions: Regions | None) -> None:
    """Outlines, lines in grey and the neighbouring countries, with a map-like aspect."""
    internal = zones[zones["kind"] == "internal"]
    if regions:
        rings = [ring for zone_id in internal.index for ring in regions.get(zone_id, [])]
        ax.add_collection(
            PolyCollection(rings, facecolors=EMPTY, edgecolors="white", linewidths=0.6, zorder=1)
        )
    ax.add_collection(
        LineCollection(_segments(lines, zones), colors=OUTLINE, linewidths=0.5, zorder=3)
    )
    boundary = zones[zones["kind"] == "boundary"]
    ax.scatter(boundary["lon"], boundary["lat"], s=18, marker="s", color=OUTLINE, zorder=4)
    for zone_id, zone in boundary.iterrows():
        ax.annotate(
            zone["country"] or zone_id,
            (zone["lon"], zone["lat"]),
            xytext=(4, 4),
            textcoords="offset points",
            fontsize=7,
            color="#555555",
        )
    ax.set_aspect(1 / math.cos(math.radians(internal["lat"].mean())))
    ax.update_datalim(zones[["lon", "lat"]].to_numpy())
    ax.autoscale_view()
    ax.margins(0.04)
    ax.set_axis_off()


def _segments(lines: pd.DataFrame, zones: pd.DataFrame) -> list[np.ndarray]:
    start = zones.loc[lines["from_zone"], ["lon", "lat"]].to_numpy()
    end = zones.loc[lines["to_zone"], ["lon", "lat"]].to_numpy()
    return list(np.stack([start, end], axis=1))


def _fill_zones(ax, values: pd.Series, zones, regions, cmap, norm, hide_zero: bool = True):
    """Colour the internal zones by ``values``; zones at 0 stay grey when ``hide_zero``."""
    shown = values[values != 0] if hide_zero else values
    colors = plt.get_cmap(cmap)(norm(shown.to_numpy(dtype=float)))
    if regions and set(shown.index) <= set(regions):
        for zone_id, color in zip(shown.index, colors, strict=True):
            ax.add_collection(
                PolyCollection(
                    regions[zone_id], facecolors=[color], edgecolors="white", linewidths=0.6
                )
            ).set_zorder(2)
    else:
        if hide_zero:
            rest = zones.loc[values.index.difference(shown.index)]
            ax.scatter(rest["lon"], rest["lat"], s=150, color=EMPTY, edgecolors=OUTLINE, zorder=5)
        at = zones.loc[shown.index]
        ax.scatter(at["lon"], at["lat"], s=150, color=colors, edgecolors=OUTLINE, zorder=5)
    return plt.cm.ScalarMappable(norm=norm, cmap=cmap)


def _colorbar(figure, mappable, ax, label: str) -> None:
    figure.colorbar(mappable, ax=ax, orientation="horizontal", pad=0.02, shrink=0.7, label=label)


def _map_signal_hours(zone_table, zones, lines, regions):
    figure, axes = _map_axes(2)
    top = max(1, int(zone_table[["positive_hours", "negative_hours"]].to_numpy().max()))
    panels = (
        ("positive_hours", "Reds", "Hours with a positive signal\n(charging relieves congestion)"),
        ("negative_hours", "Blues", "Hours with a negative signal\n(feeding in relieves it)"),
    )
    for ax, (column, cmap, title) in zip(axes, panels, strict=True):
        _draw_base(ax, zones, lines, regions)
        mappable = _fill_zones(ax, zone_table[column], zones, regions, cmap, Normalize(0, top))
        _colorbar(figure, mappable, ax, "hours (grey: none)")
        ax.set_title(title, fontsize=11)
    return figure


def _map_mean_signal(zone_table, zones, lines, regions):
    figure, (ax,) = _map_axes(1)
    mean = zone_table["mean_signal"]
    size = float(mean.abs().max()) or 1.0
    _draw_base(ax, zones, lines, regions)
    norm = TwoSlopeNorm(0.0, -size, size)
    mappable = _fill_zones(ax, mean, zones, regions, "RdBu_r", norm, hide_zero=False)
    _colorbar(figure, mappable, ax, "EUR/MWh (red: feeding in adds to congestion)")
    ax.set_title("Mean fee signal over the hours of the signal", fontsize=11)
    return figure


def _map_lines(tables, line_table, zones, lines, regions):
    figure, (ax,) = _map_axes(1)
    _draw_base(ax, zones, lines, regions)
    priced = line_table.loc[line_table["priced_hours"] > 0, "priced_hours"].sort_values()
    if len(priced):
        norm = Normalize(0, priced.max())
        collection = LineCollection(
            _segments(lines.loc[priced.index], zones),
            cmap="YlOrRd",
            norm=norm,
            linewidths=1.5 + 3.5 * norm(priced.to_numpy(dtype=float)),
            zorder=6,
        )
        collection.set_array(priced.to_numpy(dtype=float))
        ax.add_collection(collection)
        _colorbar(figure, collection, ax, "hours in which the line is full and has a price")
    unserved = daily_unserved(tables).sum() / 1000.0
    unserved = unserved[unserved.index.isin(zones.index)]
    if len(unserved):
        at = zones.loc[unserved.index]
        ax.scatter(
            at["lon"],
            at["lat"],
            s=600 * unserved / unserved.max(),
            facecolors="none",
            edgecolors="black",
            linewidths=1.2,
            zorder=7,
            label=f"unserved load (largest: {unserved.max():,.1f} GWh)",
        )
        ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.set_title("Full lines and unserved load", fontsize=11)
    return figure


# Time series


def _date_axis(ax) -> None:
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=13))
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))


def _day_edges(days: pd.DatetimeIndex) -> np.ndarray:
    naive = days.tz_localize(None)
    return mdates.date2num(naive.append(naive[-1:] + pd.Timedelta(days=1)))


def _heatmap_height(rows: int) -> float:
    return min(12.0, max(1.5, 0.22 * rows))


def _timeseries_signal(signal: pd.DataFrame, zones: pd.DataFrame, threshold: float):
    """Top: hours per day in which any zone has a signal. Bottom: mean signal per day
    and zone, zones from north to south where coordinates exist."""
    daily = daily_signal(signal)
    if daily.empty:
        return None
    order = zones.loc[daily.columns, "lat"].sort_values(ascending=False, na_position="last").index
    daily = daily[order]
    active = (signal.abs() > threshold).any(axis=1).resample("D").sum().reindex(daily.index)
    edges = _day_edges(daily.index)

    figure, (top, bottom) = plt.subplots(
        2,
        1,
        figsize=(12, 2.4 + _heatmap_height(len(order))),
        sharex=True,
        height_ratios=(1.6, _heatmap_height(len(order))),
        constrained_layout=True,
    )
    top.bar(edges[:-1], active.fillna(0), width=1.0, align="edge", color="#4c4c4c")
    top.set_ylabel("hours per day")
    top.set_title("Hours with a fee signal in at least one zone", fontsize=11, loc="left")
    top.spines[["top", "right"]].set_visible(False)

    size = float(np.nanmax(np.abs(daily.to_numpy()))) or 1.0
    mesh = bottom.pcolormesh(
        edges,
        np.arange(len(order) + 1),
        np.ma.masked_invalid(daily.to_numpy().T),
        cmap="RdBu_r",
        norm=TwoSlopeNorm(0.0, -size, size),
    )
    bottom.set_facecolor(EMPTY)
    bottom.set_yticks(np.arange(len(order)) + 0.5, _labels(order, zones), fontsize=7)
    bottom.invert_yaxis()
    bottom.set_title(
        "Mean fee signal per day and zone (grey: no hour of the day is in the signal)",
        fontsize=11,
        loc="left",
    )
    figure.colorbar(mesh, ax=bottom, pad=0.01, label="EUR/MWh")
    _date_axis(bottom)
    return figure


def _labels(zone_ids, zones: pd.DataFrame) -> list[str]:
    return [f"{zones.at[z, 'name']} ({z})" if zones.at[z, "name"] != z else z for z in zone_ids]


def _timeseries_lines(tables: ResultTables, threshold: float):
    daily = daily_line_hours(tables, threshold)
    if daily.empty or not len(daily.columns):
        return None
    daily = daily[daily.sum().sort_values(ascending=False).index]
    figure, ax = plt.subplots(
        figsize=(12, 1.2 + _heatmap_height(len(daily.columns))), constrained_layout=True
    )
    mesh = ax.pcolormesh(
        _day_edges(daily.index),
        np.arange(len(daily.columns) + 1),
        np.ma.masked_equal(daily.to_numpy().T, 0),
        cmap="YlOrRd",
        norm=Normalize(0, 24),
    )
    ax.set_facecolor("#f4f4f4")
    labels = [f"{line} ({hours:,} h)" for line, hours in daily.sum().items()]
    ax.set_yticks(np.arange(len(daily.columns)) + 0.5, labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_title("Hours per day in which a line is full and has a price", fontsize=11, loc="left")
    figure.colorbar(mesh, ax=ax, pad=0.01, label="hours per day")
    _date_axis(ax)
    return figure


def _timeseries_unserved(tables: ResultTables):
    daily = daily_unserved(tables)
    if daily.empty or not len(daily.columns):
        return None
    daily = daily[daily.sum().sort_values(ascending=False).index] / 1000.0
    if len(daily.columns) > MAX_UNSERVED_ZONES:
        head = daily.iloc[:, : MAX_UNSERVED_ZONES - 1]
        daily = head.assign(other=daily.iloc[:, MAX_UNSERVED_ZONES - 1 :].sum(axis=1))
    figure, ax = plt.subplots(figsize=(12, 3.6), constrained_layout=True)
    edges = _day_edges(daily.index)[:-1]
    bottom = np.zeros(len(daily))
    colors = plt.get_cmap("tab10").colors
    for number, zone_id in enumerate(daily.columns):
        color = "#b0b0b0" if zone_id == "other" else colors[number]
        ax.bar(edges, daily[zone_id], width=1.0, align="edge", bottom=bottom, color=color)
        bottom += daily[zone_id].to_numpy()
    ax.legend(
        [f"{zone_id} ({daily[zone_id].sum():,.1f} GWh)" for zone_id in daily.columns],
        fontsize=8,
        frameon=False,
        ncols=4,
    )
    ax.set_ylabel("GWh per day")
    ax.set_title(
        "Unserved load per day (these hours are not in the fee signal)", fontsize=11, loc="left"
    )
    ax.spines[["top", "right"]].set_visible(False)
    _date_axis(ax)
    return figure
