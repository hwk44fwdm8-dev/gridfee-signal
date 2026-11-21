"""Load a zone network from a directory holding ``zones.csv`` and ``lines.csv``."""

from __future__ import annotations

import csv
from pathlib import Path

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

    # Parsed by hand rather than with pandas: every value stays text (ids such as
    # "01" keep their leading zeros, pydantic parses the numbers) and errors can
    # name the line in the file even when comments or blank lines precede it.
    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = [
            (number, next(csv.reader([text], skipinitialspace=True)))
            for number, text in enumerate(file.read().splitlines(), start=1)
            if text.strip() and not text.lstrip().startswith("#")
        ]
    if not rows:
        raise NetworkConfigError(f"{path}: file has no header row")
    header = [column.strip() for column in rows[0][1]]

    fields = model.model_fields
    required = {name for name, info in fields.items() if info.is_required()}
    missing = sorted(required - set(header))
    unknown = sorted(set(header) - set(fields))
    if missing or unknown or len(set(header)) != len(header):
        raise NetworkConfigError(
            f"{path}: missing columns {missing}, unknown columns {unknown}; "
            f"expected each of {list(fields)} at most once"
        )

    records = []
    for number, cells in rows[1:]:
        if len(cells) > len(header):
            raise NetworkConfigError(
                f"{path}, line {number}: {len(cells)} values for {len(header)} columns"
            )
        values = {k: v.strip() for k, v in zip(header, cells, strict=False) if v.strip() != ""}
        try:
            records.append(model(**values))
        except ValidationError as err:
            raise NetworkConfigError(f"{path}, line {number}:\n{err}") from err
    return records
