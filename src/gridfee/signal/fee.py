"""Fee signal per zone and hour from line shadow prices and the PTDF.

Sign convention
---------------
``signal[h, z] = sum over lines l of ptdf[l, z] * shadow_price[h, l]``, in EUR/MWh.

``ptdf[l, z]`` is the flow that 1 MW injected at zone z puts on line l in its
``from_zone -> to_zone`` direction, and ``shadow_price[h, l]`` is positive when the
line is congested in that direction and negative when it is congested the other
way. Each term is therefore positive when an injection at z loads the line in the
direction in which it is already full. So:

- **positive**: feeding in at z (generation, a discharging battery) adds to the
  congestion, and taking power out at z (load, a charging battery) relieves it.
  The value is the system cost one more MWh fed in causes, and the saving one more
  MWh taken out brings.
- **negative**: the reverse. Feeding in relieves congestion, taking out adds to it.
- **zero**: no line is congested, or the zone has no net effect on those that are.

The signal equals the nodal price at the PTDF reference minus the nodal price of
the zone. Its level therefore depends on the reference the PTDF was computed with
(with one reference zone the signal there is always 0), while the difference
between two zones in an hour does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

POSITIVE = "positive"
NEGATIVE = "negative"
NEUTRAL = "neutral"


class SignalError(ValueError):
    """Shadow prices and PTDF do not fit together, or a bad threshold or format."""


@dataclass(frozen=True)
class FeeSignal:
    """``signal``: hours x zones, EUR/MWh. ``classification``: same shape, labels."""

    signal: pd.DataFrame
    classification: pd.DataFrame
    threshold: float

    def summary(self) -> pd.DataFrame:
        """Per zone: hours in each class and the mean signal."""
        counts = {
            f"{label}_hours": (self.classification == label).sum()
            for label in (POSITIVE, NEGATIVE, NEUTRAL)
        }
        return pd.DataFrame({**counts, "mean_signal": self.signal.mean()}).rename_axis("zone_id")


def compute_fee_signal(
    shadow_prices: pd.DataFrame, ptdf: pd.DataFrame, threshold: float = 0.0
) -> FeeSignal:
    """Fee signal and its classification.

    shadow_prices: hours x lines, signed as in :class:`gridfee.flows.OPFResult`.
    ptdf: lines x zones. Must cover exactly the lines of ``shadow_prices``.
    threshold: see :func:`classify_signal`.
    """
    if not shadow_prices.columns.is_unique or not ptdf.index.is_unique:
        raise SignalError("line ids must be unique in shadow_prices and ptdf")
    if not ptdf.columns.is_unique:
        raise SignalError("zone ids must be unique in ptdf")
    only_prices = sorted(set(shadow_prices.columns) - set(ptdf.index))
    only_ptdf = sorted(set(ptdf.index) - set(shadow_prices.columns))
    if only_prices or only_ptdf:
        raise SignalError(
            "shadow_prices and ptdf must cover the same lines; "
            f"only in shadow_prices: {only_prices}, only in ptdf: {only_ptdf}"
        )
    if shadow_prices.isna().any().any() or ptdf.isna().any().any():
        raise SignalError("shadow_prices and ptdf must not contain missing values")

    aligned = ptdf.loc[shadow_prices.columns]
    signal = pd.DataFrame(
        shadow_prices.to_numpy(dtype=float) @ aligned.to_numpy(dtype=float),
        index=shadow_prices.index,
        columns=pd.Index(ptdf.columns, name="zone_id"),
    )
    return FeeSignal(signal, classify_signal(signal, threshold), threshold)


def aggregate_signal(result: FeeSignal, groups: pd.DataFrame) -> FeeSignal:
    """Fee signal of reporting zones that are each made of several solved zones.

    ``groups`` has one row per solved zone: ``zone_id``, ``report_zone`` and
    ``weight`` (> 0). The signal of a reporting zone is the weighted mean of the
    signals of its zones, hour by hour, classified with the same threshold. Zones
    that ``groups`` does not list (the boundary zones) are passed through.

    The mean hides differences inside a reporting zone: when a line between two of
    its zones is congested, their signals have opposite signs and the mean is
    closer to zero than either.
    """
    if groups["zone_id"].duplicated().any():
        raise SignalError("a zone is listed twice in the reporting groups")
    unknown = sorted(set(groups["zone_id"]) - set(result.signal.columns))
    if unknown:
        raise SignalError(f"reporting groups name zones without a signal: {unknown}")
    if not (groups["weight"] > 0).all():
        raise SignalError("weights of the reporting groups must be positive")
    grouped = groups.set_index("zone_id")
    weights = grouped["weight"] / grouped.groupby("report_zone")["weight"].transform("sum")
    weighted = result.signal[grouped.index] * weights
    report = weighted.T.groupby(grouped["report_zone"]).sum().T
    others = result.signal.drop(columns=grouped.index)
    overlap = sorted(set(report.columns) & set(others.columns))
    if overlap:
        raise SignalError(f"reporting zones named like zones that are passed through: {overlap}")
    signal = pd.concat([report, others], axis=1).rename_axis(columns="zone_id")
    return FeeSignal(signal, classify_signal(signal, result.threshold), result.threshold)


def classify_signal(signal: pd.DataFrame, threshold: float = 0.0) -> pd.DataFrame:
    """Label each value positive (> threshold), negative (< -threshold) or neutral.

    ``threshold`` is in the unit of the signal (EUR/MWh) and must not be negative.
    Values whose size is at most the threshold are neutral.
    """
    if not threshold >= 0 or threshold == float("inf"):
        raise SignalError(f"threshold must be a finite number >= 0, got {threshold!r}")
    labels = pd.DataFrame(NEUTRAL, index=signal.index, columns=signal.columns)
    labels = labels.mask(signal > threshold, POSITIVE).mask(signal < -threshold, NEGATIVE)
    return labels


def write_signal(
    result: FeeSignal, out_dir: str | Path, file_format: str = "csv", stem: str = "fee_signal"
) -> list[Path]:
    """Write ``fee_signal``, ``fee_signal_class`` and ``fee_signal_summary``.

    ``file_format`` is ``csv`` or ``parquet`` (needs pyarrow); the summary is
    always a CSV. ``stem`` replaces ``fee_signal`` in the three file names.
    """
    if file_format not in ("csv", "parquet"):
        raise SignalError(f"format must be 'csv' or 'parquet', got {file_format!r}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, frame in ((stem, result.signal), (f"{stem}_class", result.classification)):
        path = out_dir / f"{name}.{file_format}"
        if file_format == "csv":
            frame.to_csv(path)
        else:
            try:
                frame.to_parquet(path)
            except ImportError as err:
                raise SignalError(
                    'Parquet output needs pyarrow: pip install "gridfee-signal[parquet]"'
                ) from err
        paths.append(path)
    summary = out_dir / f"{stem}_summary.csv"
    result.summary().to_csv(summary)
    return [*paths, summary]
