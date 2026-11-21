"""Validated zone network: zones (buses) and the lines between them."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

TSO_ZONES = ("50Hertz", "Amprion", "TenneT", "TransnetBW")


class ZoneKind(StrEnum):
    INTERNAL = "internal"
    BOUNDARY = "boundary"


class Zone(BaseModel):
    """One bus of the network.

    An internal zone lies in Germany and belongs to one TSO control area. A boundary
    zone stands for a neighbouring country and carries its country code instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    zone_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: ZoneKind = ZoneKind.INTERNAL
    tso_zone: str | None = None
    country: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _check_kind(self) -> Self:
        if self.kind is ZoneKind.INTERNAL:
            if self.tso_zone not in TSO_ZONES:
                raise ValueError(
                    f"internal zone {self.zone_id!r} needs tso_zone in {TSO_ZONES}, "
                    f"got {self.tso_zone!r}"
                )
        else:
            if self.tso_zone is not None:
                raise ValueError(f"boundary zone {self.zone_id!r} must not have a tso_zone")
            if not self.country:
                raise ValueError(f"boundary zone {self.zone_id!r} needs a country code")
        return self


class Line(BaseModel):
    """An AC corridor between two zones in the DC (linear) approximation.

    ``susceptance_mw_per_rad`` is the flow in MW per radian of voltage angle
    difference: flow(from -> to) = susceptance * (theta_from - theta_to).
    ``capacity_mw`` limits the flow in both directions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str = Field(min_length=1)
    from_zone: str
    to_zone: str
    susceptance_mw_per_rad: float = Field(gt=0, allow_inf_nan=False)
    capacity_mw: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _check_endpoints(self) -> Self:
        if self.from_zone == self.to_zone:
            raise ValueError(f"line {self.line_id!r} connects zone {self.from_zone!r} to itself")
        return self


class ZoneNetwork(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    zones: tuple[Zone, ...] = Field(min_length=1)
    lines: tuple[Line, ...] = ()

    @model_validator(mode="after")
    def _check_network(self) -> Self:
        zone_ids = [z.zone_id for z in self.zones]
        _require_unique(zone_ids, "zone_id")
        _require_unique([ln.line_id for ln in self.lines], "line_id")

        known = set(zone_ids)
        for ln in self.lines:
            for end in (ln.from_zone, ln.to_zone):
                if end not in known:
                    raise ValueError(f"line {ln.line_id!r} refers to unknown zone {end!r}")

        unreachable = self._unreachable_from(zone_ids[0])
        if unreachable:
            raise ValueError(
                "network is not connected; zones unreachable from "
                f"{zone_ids[0]!r}: {sorted(unreachable)}"
            )
        return self

    def _unreachable_from(self, start: str) -> set[str]:
        neighbours: dict[str, set[str]] = {z.zone_id: set() for z in self.zones}
        for ln in self.lines:
            neighbours[ln.from_zone].add(ln.to_zone)
            neighbours[ln.to_zone].add(ln.from_zone)
        seen = {start}
        stack = [start]
        while stack:
            for nxt in neighbours[stack.pop()] - seen:
                seen.add(nxt)
                stack.append(nxt)
        return set(neighbours) - seen

    @property
    def zone_ids(self) -> list[str]:
        return [z.zone_id for z in self.zones]

    @property
    def internal_zone_ids(self) -> list[str]:
        return [z.zone_id for z in self.zones if z.kind is ZoneKind.INTERNAL]

    @property
    def boundary_zone_ids(self) -> list[str]:
        return [z.zone_id for z in self.zones if z.kind is ZoneKind.BOUNDARY]

    @property
    def line_ids(self) -> list[str]:
        return [ln.line_id for ln in self.lines]

    @property
    def zone_to_tso(self) -> dict[str, str]:
        """TSO control area of every internal zone."""
        return {z.zone_id: z.tso_zone for z in self.zones if z.tso_zone is not None}

    def zones_of_tso(self, tso_zone: str) -> list[str]:
        if tso_zone not in TSO_ZONES:
            raise KeyError(f"unknown TSO zone {tso_zone!r}; expected one of {TSO_ZONES}")
        return [z.zone_id for z in self.zones if z.tso_zone == tso_zone]

    def zones_frame(self) -> pd.DataFrame:
        return pd.DataFrame([z.model_dump(mode="json") for z in self.zones]).set_index("zone_id")

    def lines_frame(self) -> pd.DataFrame:
        columns = list(Line.model_fields)
        rows = [ln.model_dump() for ln in self.lines]
        return pd.DataFrame(rows, columns=columns).set_index("line_id")


def _require_unique(values: list[str], label: str) -> None:
    seen: set[str] = set()
    duplicates = sorted({v for v in values if v in seen or seen.add(v)})
    if duplicates:
        raise ValueError(f"duplicate {label}: {duplicates}")
