"""Comparison of the fee signal with the redispatch the TSOs published.

The four German TSOs publish every redispatch measure on netztransparenz.de: the
interval, the reason, the direction (feed-in raised or lowered), the energy, the TSO
that instructed the plant and the plant. The plant's zone is not published, so the
comparison is made per TSO control area: the fee signal says where feed-in adds to
congestion (positive) and where it relieves it (negative), and redispatch lowers
feed-in in the first kind of area and raises it in the second.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from gridfee.validation.checks import ValidationError

CURRENT_REDISPATCH = "Strombedingter Redispatch"
UP = "Wirkleistungseinspeisung erhöhen"
DOWN = "Wirkleistungseinspeisung reduzieren"
_UTC_OFFSET = {"CET": "+01:00", "CEST": "+02:00"}
_COLUMNS = {
    "BEGINN_DATUM",
    "BEGINN_UHRZEIT",
    "ZEITZONE_VON",
    "ENDE_DATUM",
    "ENDE_UHRZEIT",
    "ZEITZONE_BIS",
    "GRUND_DER_MASSNAHME",
    "RICHTUNG",
    "GESAMTE_ARBEIT_MWH",
    "ANWEISENDER_UENB",
}
_PLANT_COLUMN = "BETROFFENE_ANLAGE"
# netztransparenz.de writes "TenneT DE", zones.csv "TenneT".
_TSO_NAMES = {"TenneT DE": "TenneT"}
_PRICE_TOLERANCE = 1e-6
_LINE_COLUMNS = ["hours", "hours_with_redispatch", "mean_redispatch_mwh"]
_DIRECTION_COLUMNS = [
    "mean_signal",
    "mean_net_redispatch_mwh",
    "hours_compared",
    "hours_same_direction",
]


def _instants(date: pd.Series, time: pd.Series, zone: pd.Series, path: Path) -> pd.Series:
    unknown = set(zone) - set(_UTC_OFFSET)
    if unknown:
        raise ValidationError(f"{path}: unknown time zone(s) {sorted(unknown)}")
    return pd.to_datetime(
        date + " " + time + zone.map(_UTC_OFFSET), format="%d.%m.%Y %H:%M%z", utc=True
    )


def read_redispatch(path: str | Path) -> pd.DataFrame:
    """Read the CSV export of netztransparenz.de's redispatch page.

    Returns one row per measure with ``start`` and ``end`` (UTC), ``reason``, ``tso``
    (the instructing TSO, named as in zones.csv), ``direction`` (+1 feed-in raised,
    -1 lowered), ``energy_mwh`` and, if the export names it, ``plant``.
    """
    path = Path(path)
    raw = pd.read_csv(path, sep=";", decimal=",", encoding="utf-8-sig", dtype=str)
    missing = _COLUMNS - set(raw.columns)
    if missing:
        raise ValidationError(f"{path}: missing column(s) {sorted(missing)}")
    unknown = set(raw["RICHTUNG"]) - {UP, DOWN}
    if unknown:
        raise ValidationError(f"{path}: unknown direction(s) {sorted(unknown)}")
    measures = pd.DataFrame(
        {
            "start": _instants(
                raw["BEGINN_DATUM"], raw["BEGINN_UHRZEIT"], raw["ZEITZONE_VON"], path
            ),
            "end": _instants(raw["ENDE_DATUM"], raw["ENDE_UHRZEIT"], raw["ZEITZONE_BIS"], path),
            "reason": raw["GRUND_DER_MASSNAHME"],
            "tso": raw["ANWEISENDER_UENB"].replace(_TSO_NAMES),
            "direction": raw["RICHTUNG"].map({UP: 1, DOWN: -1}),
            "energy_mwh": pd.to_numeric(
                raw["GESAMTE_ARBEIT_MWH"].str.replace(",", ".", regex=False)
            ),
        }
    )
    if _PLANT_COLUMN in raw.columns:
        measures["plant"] = raw[_PLANT_COLUMN].str.strip()
    if (measures["end"] <= measures["start"]).any():
        raise ValidationError(f"{path}: measure(s) that end before they start")
    return measures


def hourly_redispatch(
    measures: pd.DataFrame, reason: str = CURRENT_REDISPATCH, by: str = "tso"
) -> pd.DataFrame:
    """Energy per hour (UTC), with columns (``by``, direction).

    ``by`` is the column of ``measures`` to sum over, the instructing TSO unless
    given. A measure's energy is spread evenly over its interval.
    """
    rows = measures[measures["reason"] == reason].reset_index(drop=True)
    hour = pd.Timedelta(hours=1)
    pieces = pd.DataFrame(
        {
            "hour": [
                pd.date_range(start.floor("h"), end.ceil("h"), freq="h", inclusive="left")
                for start, end in zip(rows["start"], rows["end"], strict=True)
            ]
        }
    ).explode("hour")
    pieces["hour"] = pd.to_datetime(pieces["hour"], utc=True)
    rows = rows.loc[pieces.index].reset_index(drop=True)
    hours = pieces["hour"].reset_index(drop=True)
    overlap = rows["end"].where(rows["end"] < hours + hour, hours + hour) - rows["start"].where(
        rows["start"] > hours, hours
    )
    energy = rows["energy_mwh"] * overlap / (rows["end"] - rows["start"])
    return (
        pd.DataFrame({"hour": hours, by: rows[by], "direction": rows["direction"]})
        .assign(energy_mwh=energy)
        .pivot_table(index="hour", columns=[by, "direction"], values="energy_mwh", aggfunc="sum")
        .fillna(0.0)
    )


@dataclass(frozen=True)
class RedispatchComparison:
    """Tables of :func:`compare_with_redispatch`."""

    hours: pd.DataFrame
    deciles: pd.DataFrame
    lines: pd.DataFrame
    directions: pd.DataFrame


def compare_with_redispatch(
    signal: pd.DataFrame,
    shadow_prices: pd.DataFrame,
    tso_of_zone: Mapping[str, str],
    redispatch: pd.DataFrame,
    line_zones: Mapping[str, tuple[str, str]],
) -> RedispatchComparison:
    """Compare the fee signal (hours x zones) with hourly redispatch per TSO.

    ``shadow_prices`` (hours x lines) tells which line is priced in an hour; the
    lines of ``line_zones`` (line id to its two zones) that join two zones of
    ``tso_of_zone`` are reported. All indexes are timezone-aware. Neutral signals
    are expected as 0, the others as their value. Hours between the first and the
    last hour of ``redispatch`` that it does not list had no redispatch.
    """
    index = signal.index.tz_convert("UTC")
    if (
        redispatch.empty
        or not ((index >= redispatch.index.min()) & (index <= redispatch.index.max())).any()
    ):
        raise ValidationError("the redispatch data covers none of the fee signal's hours")
    signal = signal.set_axis(index)
    redispatch = redispatch.reindex(index, fill_value=0.0)
    volume = redispatch.sum(axis=1)
    tsos = sorted(set(tso_of_zone.values()))
    net = pd.DataFrame(
        {tso: redispatch.get((tso, 1), 0.0) - redispatch.get((tso, -1), 0.0) for tso in tsos},
        index=index,
    )
    zones = [zone for zone in signal.columns if zone in tso_of_zone]
    by_tso = signal[zones].T.groupby([tso_of_zone[zone] for zone in zones]).mean().T
    active = signal[zones].abs().gt(0).any(axis=1)

    def describe(mask: pd.Series) -> dict:
        return {
            "hours": int(mask.sum()),
            "hours_with_redispatch": int((volume[mask] > 0).sum()),
            "mean_redispatch_mwh": volume[mask].mean(),
            "redispatch_twh": volume[mask].sum() / 1e6,
        }

    hours = pd.DataFrame(
        {"signal": describe(active), "neutral": describe(~active), "all": describe(active | True)}
    ).T.astype({"hours": int, "hours_with_redispatch": int})
    rank = volume.rank(method="first")
    decile = ((rank - 1) * 10 // len(rank)).astype(int) + 1
    deciles = pd.DataFrame(
        {
            "hours": volume.groupby(decile).size(),
            "mean_redispatch_mwh": volume.groupby(decile).mean(),
            "signal_hours": active.groupby(decile).sum(),
        }
    ).rename_axis("decile")

    prices = shadow_prices.set_axis(shadow_prices.index.tz_convert("UTC")).reindex(index)
    internal = [
        line
        for line in prices.columns
        if line in line_zones and all(zone in tso_of_zone for zone in line_zones[line])
    ]
    line_rows, direction_rows = [], []
    masks = {"all signal hours": active} | {
        line: active & prices[line].abs().gt(_PRICE_TOLERANCE) for line in internal
    }
    for name, mask in masks.items():
        if not mask.any():
            continue
        line_rows.append({"line": name} | describe(mask))
        for tso in tsos:
            mean_signal, net_tso = by_tso.loc[mask, tso], net.loc[mask, tso]
            signed = mean_signal.abs().gt(0) & net_tso.abs().gt(0)
            agree = signed & ((mean_signal > 0) == (net_tso < 0))
            direction_rows.append(
                {
                    "line": name,
                    "tso_zone": tso,
                    "mean_signal": mean_signal.mean(),
                    "mean_net_redispatch_mwh": net_tso.mean(),
                    "hours_compared": int(signed.sum()),
                    "hours_same_direction": int(agree.sum()),
                }
            )
    return RedispatchComparison(
        hours.rename_axis("hours_of"),
        deciles,
        pd.DataFrame(line_rows, columns=["line", *_LINE_COLUMNS]).set_index("line"),
        pd.DataFrame(direction_rows, columns=["line", "tso_zone", *_DIRECTION_COLUMNS]).set_index(
            ["line", "tso_zone"]
        ),
    )


def _markdown(table: pd.DataFrame) -> str:
    frame = table.reset_index()
    cells = [
        [f"{value:,.2f}" if isinstance(value, float) else str(value) for value in row]
        for row in frame.itertuples(index=False)
    ]
    lines = ["| " + " | ".join(frame.columns) + " |", "|" + " --- |" * len(frame.columns)]
    return "\n".join(lines + ["| " + " | ".join(row) + " |" for row in cells])


def render_redispatch_report(comparison: RedispatchComparison, title: str) -> str:
    """Markdown report of a comparison."""
    return (
        "\n\n".join(
            [
                f"# {title}",
                "Redispatch: current-driven measures (`Strombedingter Redispatch`) from "
                "netztransparenz.de, energy in MWh per hour summed over raised and lowered "
                "feed-in. A measure's energy is spread evenly over its interval.",
                "## Hours with and without a fee signal",
                _markdown(comparison.hours),
                "## Hours by redispatch volume",
                "Decile 10 holds the tenth of the hours with the most redispatch.",
                _markdown(comparison.deciles),
                "## Hours by priced line",
                "An hour counts for every line between German zones that is priced in it.",
                _markdown(comparison.lines),
                "## Direction per TSO zone",
                "`mean_signal` is the mean fee signal (EUR/MWh) of the TSO zone's zones; "
                "positive means feed-in adds to congestion. `mean_net_redispatch_mwh` is "
                "raised minus lowered feed-in of the plants the TSO instructed. An hour is "
                "compared if both are non-zero and has the same direction if redispatch "
                "lowered feed-in where the signal is positive or raised it where it is "
                "negative. The plants' zones are not published, so the direction inside a "
                "TSO zone (north and south of TenneT, say) is not compared.",
                _markdown(comparison.directions),
            ]
        )
        + "\n"
    )
