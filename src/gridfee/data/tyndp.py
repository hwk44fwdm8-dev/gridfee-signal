"""Replay published TYNDP market results in the collector's table format."""

from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

from gridfee.data import smard


def _open_workbook(path):
    return openpyxl.load_workbook(path, read_only=True, data_only=True)


GERMANY = {"DE00", "DEKF"}
NEIGHBOURS = {
    "AT00": "AT",
    "BE00": "BE",
    "CH00": "CH",
    "CZ00": "CZ",
    "DKE1": "DK2",
    "DKW1": "DK1",
    "DKKF": "DK2",
    "FR00": "FR",
    "LUG1": "LU",
    "LUV1": "LU",
    "NL00": "NL",
    "NOS0": "NO2",
    "PL00": "PL",
    "SE04": "SE4",
    "UK00": "UK",
}


def read_market(path: Path, scenario: str, year: int, climate_year: int):
    workbook = _open_workbook(path)
    frames = []
    try:
        for sheet, header, start in [
            ("Hourly Market Data", 13, 14),
            ("Crossborder exchanges", 11, 12),
        ]:
            rows = workbook[sheet].values
            try:
                metadata = [next(rows) for _ in range(start - 1)]
            except StopIteration as exc:
                raise ValueError(f"{sheet}: missing header rows") from exc
            expected = f"{scenario}{year % 100:02d}"
            if metadata[0][1] != expected or str(metadata[3][1]) != str(climate_year):
                raise ValueError(f"{sheet}: expected {expected}, climate year {climate_year}")
            labels = metadata[header - 1]
            selected = [
                i
                for i, label in enumerate(labels)
                if i == 0 or any(node in str(label) for node in GERMANY)
            ]
            frame = pd.DataFrame(
                ([row[i] for i in selected] for row in rows),
                columns=[labels[i] for i in selected],
            ).set_index(labels[0])
            frames.append(frame)
    finally:
        workbook.close()
    return frames


def split_other_res(market, plants, scenario, year):
    workbook = _open_workbook(plants)
    try:
        rows = list(workbook["Other RES"].values)
        if len(rows) < 10 + len(market) or rows[3][1] != "DE00":
            raise ValueError("Other RES workbook must describe DE00")
        if str(rows[4][1]) != str(year) or rows[5][1] != {
            "NT": "NationalTrends",
            "DE": "DistributedEnergy",
            "GA": "GlobalAmbition",
        }.get(scenario, scenario):
            raise ValueError("Other RES scenario/year mismatch")
        values = (
            pd.DataFrame([r[4:9] for r in rows[10 : 10 + len(market)]], index=market.index)
            .astype(float)
            .fillna(0)
        )
        if not np.allclose(values.sum(axis=1), market["DE00_35"].fillna(0), atol=0.01, rtol=0):
            raise ValueError("Other RES components do not match published output")
        market = market.copy()
        market["DE00_35"] = 0.0
        for i, code in enumerate(["biomass", "geothermal", "marine", "waste", "other"]):
            market[f"DE00_{code}"] = values[i]
        return market
    finally:
        workbook.close()


def replay(market, exchanges, mapping, raw, year, reference_year=2023, border_routing=None):
    overrides = {}
    if border_routing is not None:
        if border_routing.neighbour.duplicated().any():
            raise ValueError("duplicate border routing")
        for row in border_routing.itertuples(index=False):
            if row.tso not in smard.TSO_ZONES or not isinstance(row.smard_neighbour, str):
                raise ValueError("invalid border routing destination")
            overrides[row.neighbour] = f"{row.tso}_{row.smard_neighbour}"
    if market.empty or not market.index.equals(exchanges.index):
        raise ValueError("market and exchange hours must be nonempty and identical")
    if list(market.index) != list(range(1, len(market) + 1)):
        raise ValueError("hours must be consecutive, starting at 1")
    if len(market) > (8784 if pd.Timestamp(year, 12, 31).is_leap_year else 8760):
        raise ValueError("too many hours for target year")
    market = market.astype(float).fillna(0)
    exchanges = exchanges.astype(float).fillna(0)
    if not np.isfinite(market.to_numpy()).all() or not np.isfinite(exchanges.to_numpy()).all():
        raise ValueError("non-finite market data")
    if mapping.code.duplicated().any():
        raise ValueError("duplicate technology mapping")
    rules = mapping.set_index("code")
    generation = pd.DataFrame(0.0, index=market.index, columns=list(smard.GENERATION_FILTERS))
    load = pd.Series(0.0, index=market.index)
    for column, values in market.items():
        node, code = column.split("_", 1)
        if node not in GERMANY:
            continue
        if code not in rules.index:
            raise ValueError(f"unmapped technology: {column}")
        rule = rules.loc[code]
        if rule.category == "load":
            load += values * rule.multiplier
        elif rule.category != "ignore":
            if rule.category not in generation:
                raise ValueError(f"unknown category: {rule.category}")
            generation[rule.category] += values * rule.multiplier
    national = {}
    for column, values in exchanges.items():
        origin, destination = column.split("->")
        if origin in GERMANY and destination in GERMANY:
            continue
        if origin not in GERMANY and destination not in GERMANY:
            continue
        neighbour = destination if origin in GERMANY else origin
        neighbour = NEIGHBOURS.get(neighbour, neighbour)
        national[neighbour] = national.get(neighbour, 0) + values * (1 if origin in GERMANY else -1)
    national = pd.DataFrame(national, index=market.index)
    gap = generation.sum(axis=1) - national.sum(axis=1) - load
    if (load < 0).any() or not load.sum() > 0:
        raise ValueError("effective load must be nonnegative with positive total")
    error = float(gap.abs().sum() / load.abs().sum())
    if error > 0.01:
        raise ValueError(f"energy balance absolute hourly error {error:.2%} exceeds 1%")
    tables = {"generation_de": generation, "load_de": load.to_frame("load")}
    tables["net_export_de"] = national.sum(axis=1).to_frame("physical")
    routing = []
    for name, series in [("load_tso", load)] + [
        (f"{tech}_tso", generation[tech]) for tech in smard.TSO_RES_FILTERS
    ]:
        reference = smard.read_table(raw, name, reference_year)
        totals = reference.sum()
        if reference.empty or not totals.sum() > 0 or (totals < 0).any():
            raise ValueError(f"invalid reference shares for {name}")
        tables[name] = pd.DataFrame(
            {tso: series * value / totals.sum() for tso, value in totals.items()}
        )
    reference = smard.read_table(raw, "physical_flows_tso", reference_year)
    flows = pd.DataFrame(0.0, index=market.index, columns=reference.columns)
    for neighbour, values in national.items():
        columns = [c for c in reference if c.split("_", 1)[1] == neighbour]
        if not columns and neighbour in overrides:
            column = overrides[neighbour]
            flows[column] = flows.get(column, 0.0) + values
            continue
        totals = reference[columns].abs().sum()
        if not totals.sum() > 0:
            if values.abs().max() > 0:
                routing.append(neighbour)
            continue
        for column, weight in (totals / totals.sum()).items():
            flows[column] += values * weight
    tables["physical_flows_tso"] = flows
    index = pd.date_range(
        pd.Timestamp(year, 1, 1, tz=timezone(timedelta(hours=1))),
        periods=len(market),
        freq="h",
        name="hour",
    )
    for frame in tables.values():
        frame.index = index
    return tables, national, gap, routing


def write_replay(tables, out, year):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        frame.to_csv(out / smard.table_filename(name, year))
