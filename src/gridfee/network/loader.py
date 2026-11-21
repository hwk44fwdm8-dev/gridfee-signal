"""Load a zone network from a directory holding ``zones.csv`` and ``lines.csv``."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from gridfee.network.schema import Line, Zone, ZoneNetwork

ZONES_FILE = "zones.csv"
LINES_FILE = "lines.csv"


class NetworkConfigError(ValueError):
    """The network config files are missing or invalid."""


def load_network(directory: str | Path) -> ZoneNetwork:
    directory = Path(directory)
    zones = _read_records(directory / ZONES_FILE, Zone)
    lines = _read_records(directory / LINES_FILE, Line)
    try:
        return ZoneNetwork(zones=zones, lines=lines)
    except ValidationError as err:
        raise NetworkConfigError(f"invalid network in {directory}:\n{err}") from err


def _read_records(path: Path, model: type[Zone] | type[Line]) -> list[Zone] | list[Line]:
    if not path.is_file():
        raise NetworkConfigError(f"missing network config file: {path}")

    # Read everything as text so that ids such as "01" keep their leading zeros;
    # pydantic then parses the numeric columns.
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, comment="#", skipinitialspace=True)
    frame.columns = [c.strip() for c in frame.columns]

    fields = model.model_fields
    required = {name for name, info in fields.items() if info.is_required()}
    missing = sorted(required - set(frame.columns))
    unknown = sorted(set(frame.columns) - set(fields))
    if missing or unknown:
        raise NetworkConfigError(
            f"{path}: missing columns {missing}, unknown columns {unknown}; expected {list(fields)}"
        )

    records = []
    for row_number, row in enumerate(frame.to_dict(orient="records"), start=2):
        values = {k: v.strip() for k, v in row.items() if v.strip() != ""}
        try:
            records.append(model(**values))
        except ValidationError as err:
            raise NetworkConfigError(f"{path}, line {row_number}:\n{err}") from err
    return records
