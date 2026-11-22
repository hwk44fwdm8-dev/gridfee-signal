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
from gridfee.plots.summary import TIMEZONE, hourly_unserved
from gridfee.signal.asset import AssetResult, asset_fees
from gridfee.signal.fee import SHEDDING_TOLERANCE_MW


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
    if tables.unserved_mw is not None and set(tables.unserved_mw.columns) != set(network.zone_ids):
        from gridfee.backcast import restore_tso_border_splits

        network = restore_tso_border_splits(network, set(tables.unserved_mw.columns))
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
    if tables.unserved_mw is not None and not tables.shadow_prices.index.sort_values().equals(
        tables.unserved_mw.index.sort_values()
    ):
        raise PlotError("load_shedding_mw must cover the same solved hours as shadow_prices.")
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


def _series(
    series: pd.Series, label: str, *, hourly: bool = False, value_format: str = ",.2f"
) -> None:
    if series.empty:
        return
    period = series.index.strftime("%H:%M %Z" if hourly else "%Y-%m-%d")
    frame = pd.DataFrame(
        {
            "period": period,
            "value": series.values,
            "segment": series.isna().cumsum().to_numpy(),
        }
    )
    if hourly:
        x = alt.X(
            "period:O",
            title="Hour (Europe/Berlin)",
            sort=period.tolist(),
            axis=alt.Axis(values=period[::3].tolist(), labelAngle=0, labelOverlap="greedy"),
        )
        time_tooltip = alt.Tooltip("period:N", title="Hour (Europe/Berlin)")
    elif len(period) == 1:
        frame["period"] = series.index.strftime("%d %b %Y")
        x = alt.X("period:O", title=None, axis=alt.Axis(labelAngle=0))
        time_tooltip = alt.Tooltip("period:N", title="Day")
    else:
        step = max(1, (len(period) - 1) // 6)
        ticks = series.index[::step].union(series.index[-1:])
        tick_values = [
            alt.DateTime(year=day.year, month=day.month, date=day.day, utc=True) for day in ticks
        ]
        x = alt.X(
            "period:T",
            title=None,
            scale=alt.Scale(
                type="utc",
                domain=[tick_values[0], tick_values[-1]],
                nice=False,
            ),
            axis=alt.Axis(values=tick_values, format="%d %b %Y", labelOverlap="greedy"),
        )
        time_tooltip = alt.Tooltip(
            "period:T", title="Day", format="%d %b %Y", timeUnit="utcyearmonthdate"
        )
    chart = (
        alt.Chart(frame)
        .mark_line(point=True)
        .encode(
            x=x,
            y=alt.Y("value:Q", title=label),
            detail="segment:N",
            tooltip=[
                time_tooltip,
                alt.Tooltip("value:Q", title=label, format=value_format),
            ],
        )
        .properties(height=245)
    )
    st.altair_chart(chart, width="stretch")


def _map_limit(signal: pd.DataFrame, zones: pd.DataFrame) -> float:
    mapped = zones.dropna(subset=["lat", "lon"]).index.intersection(signal.columns)
    if len(mapped) == 0 or signal.empty:
        return 0.01
    return max(float(daily_signal(signal[mapped]).abs().max().max()), 0.01)


def _map(
    zones: pd.DataFrame,
    summary: pd.DataFrame,
    selected: str,
    fixed_limit: float | None,
    *,
    key: str = "zone_map",
    choice_key: str = "zone_choice",
) -> None:
    points = zones.join(summary).dropna(subset=["lat", "lon", "mean_signal"]).reset_index()
    if points.empty:
        st.info("The network has no coordinates for the zones in this view.")
        return
    points["selected"] = (points["zone_id"] == selected).astype(int)
    limit = fixed_limit if fixed_limit is not None else max(points["mean_signal"].abs().max(), 0.01)
    scale_type = "Fixed across dates" if fixed_limit is not None else "Scaled to selected dates"
    st.caption(
        "**Red (+):** charging relieves congestion; feeding in adds to it. "
        "**Blue (−):** feeding in relieves congestion; charging adds to it."
    )
    st.caption(f"{scale_type} · ±{limit:,.4g} EUR/MWh. Click a marker to select a zone.")
    size = 140 if len(points) <= 40 else 28
    selection = alt.selection_point(name="zone_pick", fields=["zone_id"], toggle=False)
    positions = alt.Chart(points).encode(longitude="lon:Q", latitude="lat:Q")
    markers = (
        positions.mark_circle(opacity=0.9, stroke="#334155", strokeWidth=0.7)
        .encode(
            color=alt.Color(
                "mean_signal:Q",
                title="Mean · EUR/MWh",
                scale=alt.Scale(domain=[-limit, limit], scheme="redblue", reverse=True, clamp=True),
                legend=alt.Legend(values=[-limit, 0, limit], format=",.4g"),
            ),
            size=alt.condition(
                alt.datum.selected == 1, alt.value(max(120, size * 2)), alt.value(size)
            ),
            order=alt.Order("selected:Q", sort="ascending"),
            tooltip=[
                alt.Tooltip("name:N", title="Zone"),
                alt.Tooltip("zone_id:N", title="ID"),
                alt.Tooltip("mean_signal:Q", title="Mean · EUR/MWh", format=",.2f"),
                alt.Tooltip("positive_hours:Q", title="Positive hours"),
                alt.Tooltip("negative_hours:Q", title="Negative hours"),
            ],
        )
        .add_params(selection)
    )
    labels = (
        (positions.transform_filter(alt.datum.selected == 1) if len(points) > 8 else positions)
        .mark_text(dy=-13, fontSize=11, strokeWidth=0)
        .encode(text="zone_id:N")
    )

    def select_zone() -> None:
        picked = st.session_state[key]["selection"]["zone_pick"]
        if picked:
            st.session_state[choice_key] = picked[0]["zone_id"]

    chart = alt.layer(markers, labels).project(type="mercator").properties(height=290)
    st.altair_chart(
        chart,
        width="stretch",
        key=key,
        on_select=select_zone,
        selection_mode="zone_pick",
    )
    st.caption("Markers show network coordinates, not zone boundaries.")


def _show_signal(
    signal: pd.DataFrame,
    network: ZoneNetwork,
    threshold: float,
    full_signal: pd.DataFrame | None = None,
) -> None:
    zones = network.zones_frame().loc[network.internal_zone_ids]
    selected = st.selectbox(
        "Zone",
        zones.index,
        format_func=lambda zone: f"{zones.at[zone, 'name']} ({zone})",
        key="zone_choice",
    )
    if signal.empty or zones.empty:
        st.info("No internal-zone fee signal for these dates.")
        return
    signal = signal[zones.index]
    summary = zone_summary(signal, threshold)
    st.caption("The fee signal is a modeled congestion effect, not a tariff or payment.")
    history, geography = st.columns([3, 2])
    with history:
        st.subheader("Daily mean fee signal")
        _series(daily_signal(signal[[selected]])[selected], "Fee signal · EUR/MWh")
    with geography:
        st.subheader("Zone locations")
        if zones[["lat", "lon"]].dropna().empty:
            st.caption(
                "Positive: charging relieves congestion; feeding in adds to it. "
                "Negative: feeding in relieves congestion; charging adds to it."
            )
        _map(
            zones,
            summary,
            selected,
            None if full_signal is None else _map_limit(full_signal, zones),
        )
    st.subheader("Zone summary")
    st.dataframe(
        zones[["name"]]
        .join(summary)
        .assign(
            charge_mean=signal.where(signal > threshold).mean(),
            feed_in_mean=-signal.where(signal < -threshold).mean(),
        )
        .rename(
            columns={
                "name": "Zone",
                "positive_hours": "Positive hours",
                "negative_hours": "Negative hours",
                "mean_signal": "Mean signal (EUR/MWh)",
            }
        )
        .reset_index(),
        hide_index=True,
        column_order=[
            "Zone",
            "Mean signal (EUR/MWh)",
            "Positive hours",
            "Negative hours",
            "charge_mean",
            "feed_in_mean",
            "zone_id",
        ],
        column_config={
            "Zone": st.column_config.TextColumn(width=140, pinned=True),
            "Mean signal (EUR/MWh)": st.column_config.NumberColumn("Mean (EUR/MWh)", width=125),
            "charge_mean": st.column_config.NumberColumn(
                "Charging · mean relief (EUR/MWh)",
                help="Mean positive signal, using only hours above the neutral band.",
            ),
            "feed_in_mean": st.column_config.NumberColumn(
                "Feed-in · mean relief (EUR/MWh)",
                help="Mean absolute negative signal, using only hours below the neutral band.",
            ),
            "zone_id": "Zone ID",
        },
    )


def _hours_of(day: date) -> pd.DatetimeIndex:
    return pd.date_range(
        pd.Timestamp(day, tz=TIMEZONE),
        pd.Timestamp(day + timedelta(days=1), tz=TIMEZONE),
        freq="h",
        inclusive="left",
    )


def _show_lines(tables: ResultTables, network: ZoneNetwork, day: date | None = None) -> None:
    lines = network.lines_frame()
    if lines.empty:
        st.info("This network has no lines.")
        return
    with st.expander("What do price and loading mean?"):
        st.markdown(
            "- **Shadow price:** the modeled savings if that line could carry one more "
            "megawatt for the hour, at the margin. It is not a tariff.\n"
            "- **Mean absolute price:** average price magnitude across fee-covered hours "
            "above the cutoff.\n"
            "- **Mean loading:** average of absolute hourly flow divided by line capacity. "
            "Peak loading is the highest such value."
        )
    cutoff = st.number_input(
        "Line shadow-price cutoff (EUR/MWh)",
        min_value=0.0,
        value=0.0,
        step=0.1,
        help="An hour counts if the absolute line price is greater than this cutoff. "
        "To match gridfee plot --threshold, use the same value here and in the fee neutral band.",
    )
    summary = line_summary(
        tables, lines["capacity_mw"], cutoff, loading_all_solved=True
    ).sort_values("priced_hours", ascending=False, kind="stable")
    zone_names = network.zones_frame()["name"]
    from_name = lines["from_zone"].map(zone_names)
    to_name = lines["to_zone"].map(zone_names)
    line_labels = from_name + " → " + to_name + " (" + lines.index + ")"
    if "line_choice" not in st.session_state:
        st.session_state.line_choice = summary.index[0]
    selected = st.selectbox(
        "Line", lines.index, format_func=line_labels.__getitem__, key="line_choice"
    )
    if len(tables.flows_mw.index) == 0:
        st.info("No solved hours for these dates.")
        return
    if tables.signal.empty:
        st.info("No fee-signal coverage for these dates. Loading still uses the solved flows.")
        summary["priced_hours"] = pd.Series(pd.NA, index=summary.index, dtype="Int64")
    elif day is None:
        counts = daily_line_hours(tables, cutoff, include_unpriced=True)
        solved_days = tables.flows_mw.index.normalize()
        days = pd.date_range(solved_days.min(), solved_days.max(), freq="D")
        _series(counts[selected].reindex(days), "Priced hours", value_format=",.0f")
    if day is not None:
        hours = _hours_of(day)
        st.subheader("Hourly line loading")
        loading = tables.flows_mw[selected].abs() / lines.at[selected, "capacity_mw"]
        _series(loading.reindex(hours), "Loading", hourly=True, value_format=".1%")
        if not tables.signal.empty:
            st.subheader("Hourly line shadow price")
            price = tables.shadow_prices.loc[tables.signal.index, selected].abs()
            _series(price.reindex(hours), "Absolute shadow price · EUR/MWh", hourly=True)
    if (summary["max_loading"] > 1.001).any():
        st.warning(
            "Some solved flows exceed the supplied line capacities. "
            "Check that --network uses the capacities from this run."
        )
    table = lines[["from_zone", "to_zone", "capacity_mw"]].join(summary).loc[summary.index]

    def select_line() -> None:
        picked = st.session_state["line_table"]["selection"]["rows"]
        if picked:
            st.session_state.line_choice = table.index[picked[0]]

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
        key="line_table",
        on_select=select_line,
        selection_mode="single-row",
    )
    st.caption(
        "Prices use only hours with fee-signal data; gaps show days without coverage. "
        "Loading uses all solved hours. Select a row to show that line."
    )


def _show_unserved(tables: ResultTables, day: date | None = None) -> None:
    if len(tables.shadow_prices.index) == 0:
        st.info("No solved hours for these dates.")
        return
    if tables.unserved_mw is None:
        st.info("This run has no load shedding table.")
        return
    if day is None:
        values = daily_unserved(tables)
    else:
        values = hourly_unserved(tables.unserved_mw, SHEDDING_TOLERANCE_MW).reindex(_hours_of(day))
    if values.empty:
        st.info(f"No modeled unserved load above {SHEDDING_TOLERANCE_MW:g} MW total in any hour.")
        return
    _series(
        values.sum(axis=1, min_count=1),
        "Total unserved load · MWh",
        hourly=day is not None,
        value_format=",.4~g",
    )
    st.dataframe(values.sum().sort_values(ascending=False).rename("Unserved load (MWh)"))
    st.caption(
        "Modeled shortfall, not observed outages. Backcasts omit these hours from the fee signal."
    )


@st.cache_data(show_spinner="Computing asset fee look-back…", max_entries=64)
def _cached_asset_fees(
    site: str,
    signal: pd.Series,
    kind: str,
    power_mw: float,
    energy_mwh: float,
    efficiency: float,
) -> AssetResult:
    return asset_fees(signal, kind, power_mw, energy_mwh, efficiency)


def _asset_export(hourly: pd.DataFrame) -> pd.DataFrame:
    frame = hourly.reset_index(drop=True)
    frame.insert(0, "timestamp_utc", hourly.index.tz_convert("UTC").map(lambda h: h.isoformat()))
    frame.insert(
        0, "timestamp_europe_berlin", hourly.index.tz_convert(TIMEZONE).map(lambda h: h.isoformat())
    )
    return frame


def _asset_pattern(hourly: pd.DataFrame) -> pd.DataFrame:
    local = hourly.index.tz_convert(TIMEZONE)
    frame = hourly.assign(
        hour_of_day=local.hour,
        day=local.date,
        charging=hourly["charge_mw"] > 1e-7,
        discharging=hourly["discharge_mw"] > 1e-7,
    )
    means = frame.groupby("hour_of_day")["signal"].mean()
    days = frame.groupby(["hour_of_day", "day"])[["charging", "discharging"]].any()
    return means.to_frame("mean_signal").join(days.groupby("hour_of_day").sum()).reindex(range(24))


def _show_asset(
    tables: ResultTables, network: ZoneNetwork, full_signal: pd.DataFrame | None = None
) -> None:
    zones = network.zones_frame()
    selected = st.selectbox(
        "Site",
        zones.index,
        format_func=lambda zone: f"{zones.at[zone, 'name']} ({zone})",
        key="asset_site",
    )
    kind = st.selectbox("Asset kind", ["load", "generator", "battery"])
    power = st.number_input("Power (MW)", min_value=0.01, value=1.0)
    energy, efficiency = 0.0, 0.9
    if kind == "battery":
        energy = st.number_input("Energy capacity (MWh)", min_value=0.0, value=2.0)
        efficiency = st.number_input(
            "Round-trip efficiency", min_value=0.01, max_value=1.0, value=0.9
        )
        st.caption(
            "Idealised best case: perfect foresight of each Europe/Berlin day's signal; "
            "chronological state of charge stays within usable capacity and starts/ends "
            "each day empty. No simultaneous charging/discharging; multiple cycles allowed. "
            "Losses apply on discharge."
        )
    hours = tables.shadow_prices.index
    years = ", ".join(map(str, sorted(set(hours.tz_convert(TIMEZONE).year))))
    st.subheader(f"Look-back: what this asset would have paid/earned in {years}")
    st.caption(
        "Positive (red): charging relieves congestion; feeding in adds to it. "
        "Negative (blue): feeding in relieves congestion; charging adds to it. "
        "This is a modeled signal, not a tariff or payment."
    )
    st.caption(
        "Fee = signal × net feed-in (generation/battery discharge positive; load/battery "
        "charge negative). Positive fee = asset would pay; negative = would be paid, "
        "under this modelled signal. This look-back is not a forecast."
    )
    signal = tables.signal.reindex(index=hours, columns=zones.index).copy()
    if tables.unserved_mw is not None:
        unserved = tables.unserved_mw.reindex(hours).sum(axis=1) > SHEDDING_TOLERANCE_MW
        signal.loc[unserved] = np.nan
    result = _cached_asset_fees(selected, signal[selected], kind, power, energy, efficiency)
    summary = result.summary
    metrics = st.columns(3)
    for column, label, key in zip(
        metrics,
        ["Total fee (EUR)", "EUR per MW", "EUR per MWh moved"],
        ["total_eur", "eur_per_mw", "eur_per_mwh_moved"],
        strict=True,
    ):
        value = summary[key]
        column.metric(label, f"{value:,.2f}" if pd.notna(value) else "N/A")
    st.caption(
        f"Hours paying: {summary['hours_paying']} · earning: {summary['hours_earning']} · "
        f"neutral: {summary['hours_neutral']} · excluded (no signal / unserved): "
        f"{summary['hours_excluded']}. MWh moved counts both charge and discharge."
    )
    history, geography = st.columns([3, 2])
    with history:
        st.subheader("Hourly asset fee (EUR)")
        st.line_chart(result.hourly[["fee_eur"]])
        if kind == "battery":
            st.subheader("Hourly net injection (MW)")
            st.line_chart(result.hourly[["injection_mw"]])
    with geography:
        _map(
            zones,
            zone_summary(signal),
            selected,
            None if full_signal is None else _map_limit(full_signal, zones),
            key="asset_map",
            choice_key="asset_site",
        )
    st.subheader("Hourly details")
    st.caption(
        "One row per solved hour, not a daily mean. Times include UTC offsets for repeated "
        "local hours. Battery state of charge is at the end of the hour; charge/discharge "
        "columns are battery-only (zero for other assets)."
    )
    export = _asset_export(result.hourly)
    st.dataframe(export)
    st.download_button(
        "Download hourly CSV",
        export.to_csv(index=False).encode("utf-8"),
        file_name=f"asset_{selected.replace('/', '_')}_{kind}.csv",
        mime="text/csv",
    )
    st.subheader("Hour-of-day pattern (Europe/Berlin)")
    pattern = _asset_pattern(result.hourly).rename_axis("hour_of_day").reset_index()
    mean_chart = (
        alt.Chart(pattern)
        .mark_bar()
        .encode(
            x=alt.X("hour_of_day:O", title="Local hour of day"),
            y=alt.Y("mean_signal:Q", title="Mean signal · EUR/MWh"),
            color=alt.condition(
                alt.datum.mean_signal >= 0, alt.value("#b2182b"), alt.value("#2166ac")
            ),
            tooltip=["hour_of_day:O", "mean_signal:Q", "charging:Q", "discharging:Q"],
        )
    )
    st.altair_chart(mean_chart, width="stretch")
    if kind == "battery":
        st.bar_chart(pattern.set_index("hour_of_day")[["charging", "discharging"]])
        st.caption(
            "Charging/discharging: count of calendar days with activity in each local hour. "
            "A repeated autumn clock-change hour counts at most once per day and direction."
        )
    st.subheader("Monthly fees (EUR)")
    monthly = (
        result.hourly["fee_eur"].groupby(result.hourly.index.strftime("%Y-%m")).sum(min_count=1)
    )
    st.dataframe(monthly.rename_axis("Month").rename("Fee (EUR)"))


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
        help="Changes which fee values count as positive or negative, not the fee values. "
        "Set the line shadow-price cutoff separately in Lines.",
    )
    fixed_map = st.sidebar.checkbox(
        "Fixed map scale across dates",
        value=True,
        help="Use the strongest daily mean from the whole run as the scale limit.",
    )
    if len(dates) == 2:
        st.session_state["last_complete_range"] = dates
    else:
        st.sidebar.info("Pick an end date. The charts still show the last complete range.")
    selected_dates = st.session_state.get("last_complete_range", (first, last))
    day = selected_dates[0] if selected_dates[0] == selected_dates[1] else None
    full_signal = tables.signal if fixed_map else None
    tables = _select_dates(tables, *selected_dates)
    solved_hours = len(tables.shadow_prices.index)
    signal_hours = len(tables.signal.index)
    metrics = st.columns(3)
    metrics[0].metric("Solved hours", f"{solved_hours:,}")
    metrics[1].metric(
        "Hours with fee-signal data", f"{signal_hours:,}", help="Includes rows with a zero signal."
    )
    missing = tables.shadow_prices.index.difference(tables.signal.index)
    metrics[2].metric("Solved hours without fee-signal data", f"{len(missing):,}")
    if tables.unserved_mw is not None:
        shed = tables.unserved_mw.sum(axis=1)
        missing = missing.difference(shed.index[shed > SHEDDING_TOLERANCE_MW])
    if len(missing):
        st.warning(
            f"{len(missing):,} solved hours are missing fee-signal data and are not explained "
            "by the saved load shedding. Check that the fee-signal file is complete."
        )
    signal_tab, lines_tab, unserved_tab, asset_tab = st.tabs(
        ["Fee signal", "Lines", "Unserved load", "Site and asset"]
    )
    with signal_tab:
        _show_signal(tables.signal, network, threshold, full_signal)
    with lines_tab:
        _show_lines(tables, network, day)
    with unserved_tab:
        _show_unserved(tables, day)
    with asset_tab:
        _show_asset(tables, network, full_signal)


if __name__ == "__main__":
    main()
