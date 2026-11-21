"""Markdown report of the validation checks."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from gridfee.validation.checks import FAIL, INFO, PASS

_LABEL = {PASS: "PASS", FAIL: "FAIL", INFO: "info"}


def _number(value: float, unit: str = "") -> str:
    if pd.isna(value):
        return "–"
    if unit == "share":
        return f"{value:.1%}"
    return f"{value:,.2f}"


def _table(rows: pd.DataFrame, verdict: bool) -> list[str]:
    head = ["Check", "Item", "Observed", "Modelled", "Unit", "Deviation"]
    head += ["Tolerance", "Result"] if verdict else []
    lines = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    for row in rows.itertuples():
        relative = verdict or row.unit != "TWh" or not row.check.startswith("net export")
        deviation = f"{row.deviation:.1%}" if relative else _number(row.deviation) + " TWh"
        cells = [
            row.check,
            row.item,
            _number(row.observed, row.unit),
            _number(row.modelled, row.unit),
            row.unit,
            "–" if pd.isna(row.deviation) else deviation,
        ]
        if verdict:
            cells += [f"{row.tolerance:.0%}", f"**{_LABEL[row.status]}**"]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render_report(checks: pd.DataFrame, title: str, notes: Sequence[str] = ()) -> str:
    """Report with a pass/fail table, a table of comparisons without verdict and notes."""
    judged = checks[checks["status"] != INFO]
    info = checks[checks["status"] == INFO]
    failed = int((judged["status"] == FAIL).sum())
    lines = [f"# {title}", ""]
    if judged.empty:
        lines.append("No check with a tolerance was run.")
    else:
        verdict = "all passed" if failed == 0 else f"{failed} FAILED"
        lines += [f"**{len(judged)} checks with a tolerance: {verdict}.**", ""]
        lines += _table(judged, verdict=True)
    if not info.empty:
        lines += ["", "## Comparisons without a verdict", ""]
        lines += _table(info, verdict=False)
    if notes:
        lines += ["", "## Notes", ""] + [f"- {note}" for note in notes]
    return "\n".join(lines) + "\n"


def write_report(
    checks: pd.DataFrame, path: str | Path, title: str, notes: Sequence[str] = ()
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(checks, title, notes), encoding="utf-8")
    return path
