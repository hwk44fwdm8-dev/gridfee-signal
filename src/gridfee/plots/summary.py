"""Tables behind the figures: per zone, per line and per day.

Hours with unserved load are not in the fee signal (their prices reflect the
model's penalty, not data). The line tables are therefore limited to the hours of
the fee signal, and unserved load is summarised on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from gridfee.signal import NEGATIVE, POSITIVE, classify_signal
from gridfee.signal.fee import SHEDDING_TOLERANCE_MW

TIMEZONE = "Europe/Berlin"


class PlotError(ValueError):
    """The results directory lacks a table, or the tables do not fit together."""


@dataclass(frozen=True)
class ResultTables:
    """Hourly tables of one results directory, indexed by hour in Europe/Berlin.

    ``signal``: hours x zones, EUR/MWh. ``shadow_prices`` and ``flows_mw``: hours x
    lines. ``unserved_mw``: hours x zones, or None when the run had no load
    shedding. ``signal`` may hold fewer hours than the other tables.
    """

    signal: pd.DataFrame
    shadow_prices: pd.DataFrame
    flows_mw: pd.DataFrame
    unserved_mw: pd.DataFrame | None = None


def read_results(results: str | Path) -> ResultTables:
    """Read the tables that ``gridfee backcast`` (or ``run`` and ``signal``) wrote."""
    results = Path(results)
    if not results.is_dir():
        raise PlotError(f"{results}: --results must be an existing run directory, not a file")
    signal = _read_hourly(results, "fee_signal")
    shadow = _read_hourly(results, "shadow_prices")
    flows = _read_hourly(results, "flows_mw")
    unknown = signal.index.difference(shadow.index)
    if len(unknown):
        raise PlotError(
            f"{results}: fee_signal has {len(unknown)} hours that shadow_prices lacks, "
            f"first {unknown[0]}"
        )
    unserved = None
    if any((results / f"load_shedding_mw.{ext}").exists() for ext in ("csv", "parquet")):
        unserved = _read_hourly(results, "load_shedding_mw")
    return ResultTables(signal, shadow, flows, unserved)


def _read_hourly(results: Path, name: str) -> pd.DataFrame:
    paths = [p for p in (results / f"{name}.csv", results / f"{name}.parquet") if p.exists()]
    if not paths:
        raise PlotError(f"no {name}.csv or {name}.parquet in {results}")
    path = max(paths, key=lambda p: p.stat().st_mtime)
    frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path, index_col=0)
    try:
        index = frame.index
        has_timezone = isinstance(index, pd.DatetimeIndex) and index.tz is not None
        if (
            len(index)
            and not has_timezone
            and not index.astype(str).str.contains(r"(?:[Zz]|[+-]\d{2}:?\d{2})$").all()
        ):
            raise ValueError("missing UTC offset")
        frame.index = pd.to_datetime(index, format="mixed", utc=True).tz_convert(TIMEZONE)
    except (ValueError, TypeError) as err:
        raise PlotError(f"{path}: the first column must hold hours with a UTC offset") from err
    return frame.rename_axis("hour")


def zone_summary(signal: pd.DataFrame, threshold: float = 0.0) -> pd.DataFrame:
    """Per zone: hours with a positive and a negative signal, and the mean signal."""
    labels = classify_signal(signal, threshold)
    return pd.DataFrame(
        {
            "positive_hours": (labels == POSITIVE).sum(),
            "negative_hours": (labels == NEGATIVE).sum(),
            "mean_signal": signal.mean(),
        }
    ).rename_axis("zone_id")


def line_summary(
    tables: ResultTables,
    capacity_mw: pd.Series,
    threshold: float = 0.0,
    *,
    loading_all_solved: bool = False,
) -> pd.DataFrame:
    """Per line, over the hours of the fee signal: hours with a shadow price above
    ``threshold`` (the line is full and that costs something), the mean size of the
    price in those hours, and mean and highest loading (optionally over every solved hour)."""
    hours = tables.signal.index
    price = tables.shadow_prices.loc[hours].abs()
    priced = price > threshold
    missing = sorted(set(price.columns) - set(capacity_mw.index))
    if missing:
        raise PlotError(f"the network lacks the lines {missing} of shadow_prices")
    flow = tables.flows_mw if loading_all_solved else tables.flows_mw.loc[hours]
    loading = flow[price.columns].abs() / capacity_mw[price.columns]
    return pd.DataFrame(
        {
            "priced_hours": priced.sum(),
            "mean_price_when_priced": price.where(priced).mean(),
            "mean_loading": loading.mean(),
            "max_loading": loading.max(),
        }
    ).rename_axis("line_id")


def daily_signal(signal: pd.DataFrame) -> pd.DataFrame:
    """Mean signal per day (rows) and zone; days without an hour in the signal are NaN."""
    return _full_days(signal.resample("D").mean())


def daily_line_hours(
    tables: ResultTables, threshold: float = 0.0, *, include_unpriced: bool = False
) -> pd.DataFrame:
    """Hours per day (rows) in which each line has a shadow price above ``threshold``,
    over the hours of the fee signal. Days without coverage are NaN."""
    priced = tables.shadow_prices.loc[tables.signal.index].abs() > threshold
    if not include_unpriced:
        priced = priced.loc[:, priced.any()]
    return _full_days(priced.resample("D").sum(min_count=1))


def hourly_unserved(unserved_mw: pd.DataFrame, tolerance_mw: float) -> pd.DataFrame:
    """Keep zonal shortfalls where the total in that hour exceeds the tolerance."""
    unserved = unserved_mw.where(unserved_mw.sum(axis=1) > tolerance_mw, 0.0, axis=0)
    return unserved.loc[:, unserved.sum() > 0]


def daily_unserved(
    tables: ResultTables, tolerance_mw: float = SHEDDING_TOLERANCE_MW
) -> pd.DataFrame:
    """Unserved load in MWh per day (rows) and zone; only zones that have any."""
    if tables.unserved_mw is None:
        return pd.DataFrame()
    unserved = hourly_unserved(tables.unserved_mw, tolerance_mw)
    return _full_days(unserved.resample("D").sum(min_count=1))


def _full_days(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return daily
    days = pd.date_range(daily.index[0], daily.index[-1], freq="D", name="day")
    return daily.reindex(days)
