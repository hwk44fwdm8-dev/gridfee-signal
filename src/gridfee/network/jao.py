"""Read the circuits of the German grid from JAO's Core Static Grid Model.

JAO publishes, for the TSOs of the Core capacity calculation region, every
220 kV and 380 kV circuit with its substations, its reactance and the maximum
current the TSO allows on it (https://www.jao.eu/static-grid-model). This module
turns the workbook into buses and lines in the shape that
``gridfee.network.pypsa_eur.derive_zone_lines`` aggregates, so that a zone
network can take both its reactances and its limits from published TSO data.

The workbook says where no substation is; the positions come from the map JAO
publishes with it. JAO restricts reproduction of both files, so neither is in
this repository: see docs/NETWORK.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

GERMAN_TSOS = ("50HERTZ", "AMPRION GMBH", "TENNETGMBH", "TRANSNETBW")
COLUMNS = (
    ["name", "eic", "tso", "sub1", "sub2", "voltage_kv"]
    + [f"period_{i}" for i in range(1, 7)]
    + ["fixed", "dlr_min", "dlr_max", "r", "x", "b", "length_km", "comment"]
)
PERIODS = [f"period_{i}" for i in range(1, 7)]
# Circuits shorter than this join a substation to a tee point or to the other
# TSO's bay in the same substation: both ends count as one place.
STUB_KM = 1.0
BASE_KV = 380.0


def read_sheet(workbook: str | Path, sheet: str) -> pd.DataFrame:
    """Read the ``Lines`` or ``Tielines`` sheet with plain column names."""
    frame = pd.read_excel(workbook, sheet_name=sheet, header=None, skiprows=2, names=COLUMNS)
    frame = frame[frame["eic"].notna()].copy()
    for column in ("name", "eic", "tso", "sub1", "sub2"):
        frame[column] = frame[column].astype(str).str.strip()
    numeric = [c for c in COLUMNS if c not in ("name", "eic", "tso", "sub1", "sub2", "comment")]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    return frame.assign(sheet=sheet)


def maximum_current(frame: pd.DataFrame) -> pd.DataFrame:
    """The current limit of each row and which column it comes from.

    The fixed limit if there is one, else the lowest of the seasonal limits, else
    the upper end of the dynamic line rating: an upper bound, since the weather
    decides what is allowed in a given hour.
    """
    seasonal = frame[PERIODS].min(axis=1)
    current = frame["fixed"].fillna(seasonal).fillna(frame["dlr_max"])
    kind = np.select(
        [frame["fixed"].notna(), seasonal.notna(), frame["dlr_max"].notna()],
        ["fixed", "period", "dlr_max"],
        default="none",
    )
    return pd.DataFrame({"max_current_a": current, "limit_kind": kind}, index=frame.index)


def german_circuits(lines: pd.DataFrame, tielines: pd.DataFrame) -> pd.DataFrame:
    """One row per circuit that a German TSO lists, indexed by EIC code.

    A tie line is listed by the TSO on each side under the same EIC code: the
    lower of the two limits counts, and the larger reactance and length (some
    TSOs give their share up to the border, others the whole circuit). The
    substation names are those of the German TSO that comes first in the sheet.
    Rows without a limit or a reactance are dropped.
    """
    german = lines[lines["tso"].isin(GERMAN_TSOS)]
    listed = tielines[tielines["tso"].isin(GERMAN_TSOS)]["eic"]
    both = pd.concat([german, tielines[tielines["eic"].isin(listed)]], ignore_index=True)
    both = pd.concat([both, maximum_current(both)], axis=1)
    both["is_german"] = both["tso"].isin(GERMAN_TSOS)
    both = both.sort_values("is_german", ascending=False, kind="stable")
    grouped = both.groupby("eic", sort=False)
    circuits = grouped[["name", "tso", "sub1", "sub2", "voltage_kv", "sheet"]].first()
    circuits["max_current_a"] = grouped["max_current_a"].min()
    lowest = both.dropna(subset="max_current_a").sort_values("max_current_a", kind="stable")
    circuits["limit_kind"] = lowest.drop_duplicates("eic").set_index("eic")["limit_kind"]
    circuits[["x", "r", "length_km"]] = grouped[["x", "r", "length_km"]].max()
    usable = circuits["max_current_a"].notna() & (circuits["x"] > 0)
    return circuits[usable]


def name_tee_points(circuits: pd.DataFrame) -> pd.DataFrame:
    """Give the tee point of each tee its own name.

    A circuit with a tee is listed as one row per leg, named
    ``Y-<end> (-<other ends>) <circuit>``, and the tee point is called ``Y-<end>``
    after one of the ends. Several circuits can branch near the same substation, so
    that name alone would join tees that are kilometres apart: the tee point is
    renamed ``Y-<end> [<circuit>]``. Rows that name a tee point at both ends (the
    link between two tees of one circuit) are renamed at both.
    """
    circuits = circuits.copy()
    tee = circuits["name"].str.extract(r"^Y\s*-.*\)\s*(?P<circuit>.*?)\s*$")["circuit"]
    for end in ("sub1", "sub2"):
        is_tee = tee.notna() & circuits[end].str.startswith("Y-")
        circuits.loc[is_tee, end] = circuits.loc[is_tee, end] + " [" + tee[is_tee] + "]"
    return circuits


def read_map_positions(map_html: str | Path) -> pd.DataFrame:
    """End points of each circuit on JAO's map: ``x0, y0, x1, y1`` by EIC code."""
    text = Path(map_html).read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r'<script type="application/json" data-for="[^"]*">(.*?)</script>', text, re.S
    )
    if match is None:
        raise ValueError(f"{map_html} is not the leaflet map of the Static Grid Model")
    calls = json.loads(match.group(1))["x"]["calls"]
    rows = {}
    for call in calls:
        if call["method"] != "addPolylines":
            continue
        for shape, popup in zip(call["args"][0], call["args"][4], strict=True):
            eic = re.search(r"EIC Code:<b> ([^<]+)<br>", popup)
            if eic is None:
                continue
            path = shape[0][0]
            rows.setdefault(
                eic.group(1).strip(),
                (path["lng"][0], path["lat"][0], path["lng"][-1], path["lat"][-1]),
            )
    return pd.DataFrame.from_dict(rows, orient="index", columns=["x0", "y0", "x1", "y1"])


def substation_positions(circuits: pd.DataFrame, positions: pd.DataFrame) -> pd.DataFrame:
    """Position of each substation: the median over the circuits that end there.

    Only circuits of the ``Lines`` sheet count. The map draws tie lines up to the
    border only, and it shifts parallel circuits apart by a few kilometres, which
    the median removes.
    """
    inner = circuits[circuits["sheet"] == "Lines"].join(positions, how="inner")
    ends = pd.concat(
        [
            inner[["sub1", "x0", "y0"]].set_axis(["substation", "x", "y"], axis=1),
            inner[["sub2", "x1", "y1"]].set_axis(["substation", "x", "y"], axis=1),
        ]
    )
    return ends.groupby("substation")[["x", "y"]].median()


def merge_stubs(circuits: pd.DataFrame, stub_km: float = STUB_KM) -> pd.Series:
    """Map each substation name to the place it stands for.

    Names joined by a circuit of the ``Lines`` sheet shorter than ``stub_km`` are
    one place, named after a real substation rather than a tee point (``Y-...``)
    where there is one. Tie lines do not count: their length may be one TSO's share.
    """
    names = pd.unique(circuits[["sub1", "sub2"]].to_numpy().ravel())
    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            name = parent[name]
        return name

    stubs = circuits[(circuits["length_km"] < stub_km) & (circuits["sheet"] == "Lines")]
    for row in stubs.itertuples():
        a, b = find(row.sub1), find(row.sub2)
        if a != b:
            if a.startswith("Y-") and not b.startswith("Y-"):
                a, b = b, a
            parent[b] = a
    return pd.Series({name: find(name) for name in names}, name="place")


def to_lines(circuits: pd.DataFrame) -> pd.DataFrame:
    """Circuits as PyPSA-style lines between the places of ``merge_stubs``.

    ``circuits`` needs ``bus0`` and ``bus1``. Reactances are referred to 380 kV so
    that one voltage level describes the network; ratings are
    ``sqrt(3) * voltage * current``.
    """
    scale = (BASE_KV / circuits["voltage_kv"]) ** 2
    lines = pd.DataFrame(
        {
            "bus0": circuits["bus0"],
            "bus1": circuits["bus1"],
            "x": circuits["x"] * scale,
            "r": circuits["r"].fillna(0.0) * scale,
            "s_nom": np.sqrt(3) * circuits["voltage_kv"] * circuits["max_current_a"] / 1000.0,
            "length": circuits["length_km"].clip(lower=STUB_KM) * 1000.0,
        }
    )
    return lines[lines["bus0"] != lines["bus1"]]
