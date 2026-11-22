"""Hourly asset look-back, not a forecast. Positive fees mean the asset pays.

Battery operation is an idealised best case with perfect foresight of each
Europe/Berlin day's signal: one cycle at most, no inter-day energy transfer,
and no chronological state-of-charge constraint. E is the charge-side energy
budget; discharge energy is charge energy times round-trip efficiency. Each
hour is used for only one direction, including fractional final hours.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class AssetResult:
    hourly: pd.DataFrame
    summary: dict[str, float | int]


def asset_fees(
    signal: pd.Series,
    kind: str,
    power_mw: float,
    energy_mwh: float = 0.0,
    efficiency: float = 0.9,
) -> AssetResult:
    """Price hourly MW injections at EUR/MWh; NaN signals exclude hours.

    Pass the full solved-hour index, reindexing missing signals to NaN and
    masking hours with any unserved load. Input timestamps must be unique,
    timezone-aware hourly observations. Empty input returns zero totals and
    NaN EUR/MWh moved (also used whenever no energy moves).
    """
    if kind not in {"load", "generator", "battery"}:
        raise ValueError("kind must be load, generator or battery")
    if not np.isfinite(power_mw) or power_mw <= 0:
        raise ValueError("power_mw must be finite and positive")
    if not np.isfinite(energy_mwh) or energy_mwh < 0:
        raise ValueError("energy_mwh must be finite and non-negative")
    if not np.isfinite(efficiency) or not 0 < efficiency <= 1:
        raise ValueError("efficiency must be in (0, 1]")
    if (
        not isinstance(signal.index, pd.DatetimeIndex)
        or signal.index.tz is None
        or not signal.index.is_unique
        or signal.index.hasnans
    ):
        raise ValueError("signal needs unique timezone-aware hourly timestamps")
    signal = signal.astype(float).sort_index()
    if np.isinf(signal.to_numpy()).any():
        raise ValueError("signal must be finite or NaN")
    injection = pd.Series(0.0, index=signal.index)
    valid = signal.dropna()
    if kind != "battery":
        injection.loc[valid.index] = power_mw * (-1 if kind == "load" else 1)
    else:
        days = valid.index.tz_convert("Europe/Berlin").date
        for _, day in valid.groupby(days):
            ordered = day.sort_values(kind="stable")
            low, high = 0, len(ordered) - 1
            remaining = energy_mwh
            while low < high and remaining > 1e-10:
                discharge, charge = ordered.index[low], ordered.index[high]
                if efficiency * ordered.iloc[low] >= ordered.iloc[high]:
                    break
                amount = min(
                    remaining,
                    power_mw + injection.at[charge],
                    (power_mw - injection.at[discharge]) / efficiency,
                )
                injection.at[charge] -= amount
                injection.at[discharge] += amount * efficiency
                remaining -= amount
                if injection.at[charge] <= -power_mw + 1e-10:
                    high -= 1
                if injection.at[discharge] >= power_mw - 1e-10:
                    low += 1
    fee = signal * injection
    moved = injection.abs().sum()
    total = float(fee.sum())
    return AssetResult(
        pd.DataFrame({"injection_mw": injection, "signal": signal, "fee_eur": fee}),
        {
            "total_eur": total,
            "eur_per_mw": total / power_mw,
            "eur_per_mwh_moved": total / moved if moved else float("nan"),
            "hours_paying": int((fee > 0).sum()),
            "hours_earning": int((fee < 0).sum()),
            "hours_neutral": int((fee == 0).sum()),
            "hours_excluded": int(signal.isna().sum()),
        },
    )
