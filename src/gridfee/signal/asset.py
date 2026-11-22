"""Hourly asset look-back, not a forecast. Positive fees mean the asset pays.

Battery operation is an idealised best case with perfect foresight of each
Europe/Berlin day's signal. Stored energy stays between zero and the usable
capacity E, starting and ending each day empty. Losses apply on discharge;
discharge energy is withdrawn stored energy times round-trip efficiency. Each
hour is used for only one direction; multiple cycles per day are allowed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp


@dataclass(frozen=True)
class AssetResult:
    hourly: pd.DataFrame
    summary: dict[str, float | int]


def _battery_day(day: pd.Series, power: float, capacity: float, efficiency: float) -> np.ndarray:
    n = len(day)
    identity = np.eye(n)
    cumulative = np.tril(np.ones((n, n)))
    zeros = np.zeros((n, n))
    # Variables: grid-side charge, grid-side discharge, binary charging mode.
    balance = np.hstack([cumulative, -cumulative / efficiency, zeros])
    charge_limit = np.hstack([identity, zeros, -power * identity])
    discharge_limit = np.hstack([zeros, identity, power * identity])
    matrix = np.vstack([balance, charge_limit, discharge_limit])
    lower = np.concatenate([np.zeros(n), np.full(2 * n, -np.inf)])
    upper = np.concatenate([np.full(n, capacity), np.zeros(n), np.full(n, power)])
    upper[n - 1] = 0.0
    prices = day.to_numpy()
    result = milp(
        c=np.concatenate([-prices, prices, np.zeros(n)]),
        integrality=np.concatenate([np.zeros(2 * n), np.ones(n)]),
        bounds=Bounds(np.zeros(3 * n), np.concatenate([np.full(2 * n, power), np.ones(n)])),
        constraints=LinearConstraint(matrix, lower, upper),
        options={"mip_rel_gap": 0.0},
    )
    if not result.success:
        raise RuntimeError(f"Battery scheduling failed for {day.index[0].date()}: {result.message}")
    return result.x[n : 2 * n] - result.x[:n]


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
    elif energy_mwh > 0:
        days = valid.index.tz_convert("Europe/Berlin").date
        for _, day in valid.groupby(days):
            injection.loc[day.index] = _battery_day(day, power_mw, energy_mwh, efficiency)
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
