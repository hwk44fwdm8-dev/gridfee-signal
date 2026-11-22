"""Summaries and figures of a results directory (``gridfee run`` or ``gridfee backcast``).

:mod:`gridfee.plots.summary` reduces the result tables to what a figure shows and
needs only pandas, so an interface can use it without matplotlib.
:mod:`gridfee.plots.figures` draws them and needs the ``plots`` extra.
"""

from gridfee.plots.summary import (
    PlotError,
    ResultTables,
    daily_line_hours,
    daily_signal,
    daily_unserved,
    line_summary,
    read_results,
    zone_summary,
)

__all__ = [
    "PlotError",
    "ResultTables",
    "daily_line_hours",
    "daily_signal",
    "daily_unserved",
    "line_summary",
    "read_results",
    "zone_summary",
]
