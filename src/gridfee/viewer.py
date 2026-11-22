"""Read-only Streamlit explorer for a finished gridfee run."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from gridfee.network import Line, Zone, ZoneNetwork, load_network
from gridfee.plots import (
    PlotError,
    ResultTables,
    daily_line_hours,
    daily_signal,
    daily_unserved,
    line_summary,
    read_results,
    zone_summary,
)
from gridfee.plots.summary import TIMEZONE


def _demo() -> tuple[ResultTables, ZoneNetwork]:
    network = ZoneNetwork(
        zones=(
            Zone(zone_id="N", name="Demo North", tso_zone="TenneT", lat=53.5, lon=10.2),
            Zone(zone_id="W", name="Demo West", tso_zone="Amprion", lat=51.4, lon=7.4),
            Zone(zone_id="S", name="Demo South", tso_zone="TransnetBW", lat=48.9, lon=9.3),
        ),
        lines=(
            Line(
                line_id="N-W",
                from_zone="N",
                to_zone="W",
                capacity_mw=100,
                susceptance_mw_per_rad=1000,
            ),
            Line(
                line_id="W-S",
                from_zone="W",
                to_zone="S",
                capacity_mw=120,
                susceptance_mw_per_rad=1000,
            ),
        ),
    )
    hours = pd.date_range("2023-10-23", periods=337, freq="h", tz=TIMEZONE, name="hour")
    phase = np.arange(len(hours)) * (2 * np.pi / 24)
    north = np.maximum(0, 16 * np.sin(phase) + 10 * np.sin(phase / 6) - 4)
    south = np.maximum(0, 12 * np.cos(phase / 2) - 7)
    shadow = pd.DataFrame({"N-W": north, "W-S": south}, index=hours)
    flows = pd.DataFrame(
        {"N-W": np.where(north > 0, 100, 60), "W-S": np.where(south > 0, 120, 70)},
        index=hours,
    )
    unserved = pd.DataFrame(0.0, index=hours, columns=network.zone_ids)
    unserved.loc[hours[149:152], "S"] = 12.0
    signal = pd.DataFrame({"N": north, "W": -north + south, "S": -south}, index=hours)
    return ResultTables(signal.loc[~unserved.any(axis=1)], shadow, flows, unserved), network


@st.cache_resource(show_spinner="Reading results…")
def _load(results: Path, network_dir: Path) -> tuple[ResultTables, ZoneNetwork]:
    network = load_network(network_dir)
    tables = read_results(results)
    zones = set(network.zone_ids)
    signal_zones = set(tables.signal.columns)
    if not set(network.internal_zone_ids) <= signal_zones <= zones:
        raise PlotError("fee_signal does not match the network. Use the network for this run.")
    lines = set(network.line_ids)
    expected = {"shadow_prices": lines, "flows_mw": lines, "load_shedding_mw": zones}
    for name, frame in (
        ("fee_signal", tables.signal),
        ("shadow_prices", tables.shadow_prices),
        ("flows_mw", tables.flows_mw),
        ("load_shedding_mw", tables.unserved_mw),
    ):
        if frame is None:
            continue
        if name in expected and set(frame.columns) != expected[name]:
            raise PlotError(f"{name} does not match the network. Use the network for this run.")
        if not frame.index.is_unique or frame.index.hasnans:
            raise PlotError(f"{name} must have one row per timestamp.")
        if not frame.empty and (
            not all(pd.api.types.is_numeric_dtype(dtype) for dtype in frame.dtypes)
            or not np.isfinite(frame.to_numpy(dtype=float, na_value=np.nan)).all()
        ):
            raise PlotError(f"{name} must contain finite numeric values.")
    if len(tables.shadow_prices.index) == 0:
        raise PlotError("This results directory has no solved hours.")
    if not tables.shadow_prices.index.sort_values().equals(tables.flows_mw.index.sort_values()):
        raise PlotError("shadow_prices and flows_mw cover different hours.")
    return tables, network


def _select_dates(tables: ResultTables, first: date, last: date) -> ResultTables:
    start = pd.Timestamp(first, tz=TIMEZONE)
    stop = pd.Timestamp(last + timedelta(days=1), tz=TIMEZONE)

    def between(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.loc[(frame.index >= start) & (frame.index < stop)].sort_index()

    return ResultTables(
        between(tables.signal),
        between(tables.shadow_prices),
        between(tables.flows_mw),
        between(tables.unserved_mw) if tables.unserved_mw is not None else None,
    )


def _series(series: pd.Series, label: str) -> None:
    frame = pd.DataFrame({"day": series.index.strftime("%Y-%m-%d"), "value": series.values})
    chart = (
        alt.Chart(frame)
        .mark_line(point=True)
        .encode(
            x=alt.X("day:T", title=None, scale=alt.Scale(type="utc")),
            y=alt.Y("value:Q", title=label),
            tooltip=[
                alt.Tooltip("day:T", title="Day", format="%d %b %Y", timeUnit="utcyearmonthdate"),
                alt.Tooltip("value:Q", title=label, format=",.2f"),
            ],
        )
        .properties(height=245)
    )
    st.altair_chart(chart, width="stretch")


def _map(zones: pd.DataFrame, summary: pd.DataFrame, selected: str) -> None:
    points = zones.join(summary).dropna(subset=["lat", "lon", "mean_signal"]).reset_index()
    if points.empty:
        st.info("The network has no coordinates for the zones in this view.")
        return
    points["selected"] = points["zone_id"] == selected
    limit = max(points["mean_signal"].abs().max(), 0.01)
    chart = (
        alt.Chart(points)
        .mark_circle(opacity=0.9, stroke="#334155", strokeWidth=1)
        .encode(
            longitude="lon:Q",
            latitude="lat:Q",
            color=alt.Color(
                "mean_signal:Q",
                title="Mean · EUR/MWh",
                scale=alt.Scale(domain=[-limit, limit], scheme="redblue", reverse=True),
            ),
            size=alt.condition(alt.datum.selected, alt.value(340), alt.value(140)),
            tooltip=[
                alt.Tooltip("name:N", title="Zone"),
                alt.Tooltip("zone_id:N", title="ID"),
                alt.Tooltip("mean_signal:Q", title="Mean · EUR/MWh", format=",.2f"),
                alt.Tooltip("positive_hours:Q", title="Positive hours"),
                alt.Tooltip("negative_hours:Q", title="Negative hours"),
            ],
        )
        .project(type="mercator")
        .properties(height=290)
    )
    st.altair_chart(chart, width="stretch")
    st.caption("Zones shown at their network coordinates; this is not a map of grid boundaries.")


def _show_signal(signal: pd.DataFrame, network: ZoneNetwork, threshold: float) -> None:
    zones = network.zones_frame().loc[network.internal_zone_ids]
    if signal.empty or zones.empty:
        st.info("No internal-zone fee signal for these dates.")
        return
    signal = signal[zones.index]
    summary = zone_summary(signal, threshold)
    selected = st.selectbox(
        "Zone", zones.index, format_func=lambda zone: f"{zones.at[zone, 'name']} ({zone})"
    )
    history, geography = st.columns([3, 2])
    with history:
        st.subheader("Daily mean fee signal")
        _series(daily_signal(signal[[selected]])[selected], "Fee signal · EUR/MWh")
        st.caption("Positive: charging relieves congestion. Negative: charging worsens it.")
    with geography:
        st.subheader("Zone locations")
        _map(zones, summary, selected)
    st.subheader("Zone summary")
    st.dataframe(
        zones[["name"]]
        .join(summary)
        .rename(
            columns={
                "name": "Zone",
                "positive_hours": "Positive hours",
                "negative_hours": "Negative hours",
                "mean_signal": "Mean signal (EUR/MWh)",
            }
        ),
    )


def _show_lines(tables: ResultTables, network: ZoneNetwork) -> None:
    if tables.signal.empty or tables.shadow_prices.shape[1] == 0:
        st.info("No lines with fee-signal coverage for these dates.")
        return
    lines = network.lines_frame()
    summary = line_summary(tables, lines["capacity_mw"]).sort_values(
        "priced_hours", ascending=False
    )
    selected = st.selectbox("Line", summary.index)
    counts = daily_line_hours(tables)
    if selected in counts:
        _series(counts[selected], "Hours with a shadow price")
    else:
        st.info("This line had no non-zero shadow price in the selected period.")
    table = lines[["from_zone", "to_zone", "capacity_mw"]].join(summary).loc[summary.index]
    st.dataframe(
        table.rename(
            columns={
                "from_zone": "From",
                "to_zone": "To",
                "capacity_mw": "Capacity (MW)",
                "priced_hours": "Priced hours",
                "mean_price_when_priced": "Mean absolute price (EUR/MWh)",
                "mean_loading": "Mean loading",
                "max_loading": "Peak loading",
            }
        ),
        column_config={
            "Mean loading": st.column_config.NumberColumn(format="percent"),
            "Peak loading": st.column_config.NumberColumn(format="percent"),
        },
    )
    st.caption("Only hours with a fee signal count. A priced line has a non-zero shadow price.")


def _show_unserved(tables: ResultTables) -> None:
    if tables.unserved_mw is None:
        st.info("This run has no load shedding table.")
        return
    by_day = daily_unserved(tables)
    if by_day.empty:
        st.info("No modeled unserved load above 0.001 MW per zone for these dates.")
        return
    _series(by_day.sum(axis=1), "Total unserved load · MWh")
    st.dataframe(by_day.sum().sort_values(ascending=False).rename("Unserved load (MWh)"))
    st.caption(
        "Modeled shortfall, not observed outages. Backcasts omit these hours from the fee signal."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, help="Finished run directory.")
    parser.add_argument("--network", type=Path, help="Matching network directory.")
    args = parser.parse_args()
    st.set_page_config(page_title="Grid fee results", layout="wide")
    st.title("Grid fee results")
    if bool(args.results) != bool(args.network):
        st.error("Pass both --results and --network, or neither to open the synthetic demo.")
        st.stop()
    if args.results is None:
        st.info("Synthetic demo — illustrative values, not German grid results.")
        tables, network = _demo()
    else:
        st.caption(f"Run: {args.results.name} · Network: {args.network.name}")
        try:
            tables, network = _load(args.results.expanduser(), args.network.expanduser())
        except (ValueError, OSError, ImportError) as exc:
            st.error(f"Could not load this run: {exc}")
            st.stop()

    hours = tables.shadow_prices.index
    first, last = hours.min().date(), hours.max().date()
    st.sidebar.header("View settings")
    dates = st.sidebar.date_input("Dates (Europe/Berlin)", (first, last), first, last)
    threshold = st.sidebar.number_input(
        "Neutral band (± EUR/MWh)",
        min_value=0.0,
        value=0.0,
        step=0.1,
        help="Only changes the positive and negative hour counts, not the fee values.",
    )
    if len(dates) != 2:
        st.info("Select a start and an end date.")
        st.stop()
    tables = _select_dates(tables, *dates)
    solved_hours = len(tables.shadow_prices.index)
    signal_hours = len(tables.signal.index)
    metrics = st.columns(3)
    metrics[0].metric("Solved hours", f"{solved_hours:,}")
    metrics[1].metric("Hours with fee signal", f"{signal_hours:,}")
    metrics[2].metric("Solved hours without fee signal", f"{solved_hours - signal_hours:,}")
    signal_tab, lines_tab, unserved_tab = st.tabs(["Fee signal", "Lines", "Unserved load"])
    with signal_tab:
        _show_signal(tables.signal, network, threshold)
    with lines_tab:
        _show_lines(tables, network)
    with unserved_tab:
        _show_unserved(tables)


if __name__ == "__main__":
    main()
