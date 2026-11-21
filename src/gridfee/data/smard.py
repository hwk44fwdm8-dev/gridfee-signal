"""Collector for SMARD, the public market data platform of the Bundesnetzagentur.

The API needs no key. Filter ids and region codes below are the ones listed in
the bundesAPI/smard-api documentation
(https://github.com/bundesAPI/smard-api, ``openapi.yaml``); ids that are not
documented there are deliberately not used. SMARD has no documented filter for
cross-border flows, so those are not collected here.

Hourly values are the energy of the hour in MWh, which equals the mean power in
MW. Hours are returned on a Europe/Berlin index.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path

import pandas as pd
import requests

from gridfee.network.schema import TSO_ZONES

logger = logging.getLogger(__name__)

BASE_URL = "https://www.smard.de/app/chart_data"
TIMEZONE = "Europe/Berlin"

# "Stromerzeugung: ..." filters of the bundesAPI/smard-api documentation.
GENERATION_FILTERS: dict[str, int] = {
    "lignite": 1223,  # Braunkohle
    "nuclear": 1224,  # Kernenergie
    "wind_offshore": 1225,  # Wind Offshore
    "hydro": 1226,  # Wasserkraft
    "other_conventional": 1227,  # Sonstige Konventionelle
    "other_renewable": 1228,  # Sonstige Erneuerbare
    "biomass": 4066,  # Biomasse
    "wind_onshore": 4067,  # Wind Onshore
    "solar": 4068,  # Photovoltaik
    "hard_coal": 4069,  # Steinkohle
    "pumped_storage": 4070,  # Pumpspeicher
    "gas": 4071,  # Erdgas
}
LOAD_FILTER = 410  # Stromverbrauch: Gesamt (Netzlast)
TSO_RES_FILTERS: dict[str, int] = {
    name: GENERATION_FILTERS[name] for name in ("wind_onshore", "wind_offshore", "solar")
}
SMARD_REGIONS = ("DE", *TSO_ZONES)

_CHUNK = pd.Timedelta(days=7)


class SmardError(RuntimeError):
    """SMARD returned no usable data for a request."""


class SmardNoDataError(SmardError):
    """SMARD does not publish this series for this region."""


class SmardClient:
    """Reads hourly SMARD series and keeps the downloaded files in ``cache_dir``."""

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch_hourly(
        self,
        filter_id: int,
        region: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.Series:
        """Values for the hours in ``[start, end)``; hours SMARD does not have are NaN."""
        _check_request(filter_id, region)
        start, end = _to_berlin(start), _to_berlin(end)
        if start >= end:
            raise ValueError("start must be before end")
        hours = pd.date_range(start.tz_convert("UTC"), end.tz_convert("UTC"), freq="h")[:-1]

        index_name = f"{filter_id}/{region}/index_hour.json"
        try:
            chunk_starts = self._get(index_name, cache=False)["timestamps"]
        except SmardError as err:
            cause = err.__cause__
            if isinstance(cause, requests.HTTPError) and cause.response.status_code == 404:
                raise SmardNoDataError(
                    f"SMARD has no series for filter {filter_id} in region {region}"
                ) from err
            raise
        start_ms, end_ms = _to_ms(start), _to_ms(end)
        wanted = [t for t in chunk_starts if t < end_ms and t + _CHUNK.value // 10**6 > start_ms]

        values: dict[int, float] = {}
        now_ms = _to_ms(pd.Timestamp.now(tz="UTC"))
        for chunk_start in wanted:
            # A chunk whose week is not over yet still changes, so it is not cached.
            complete = chunk_start + 2 * _CHUNK.value // 10**6 < now_ms
            name = f"{filter_id}/{region}/{filter_id}_{region}_hour_{chunk_start}.json"
            for timestamp, value in self._get(name, cache=complete)["series"]:
                if value is not None:
                    values[int(timestamp)] = float(value)

        series = pd.Series(values, dtype=float)
        series.index = pd.to_datetime(series.index, unit="ms", utc=True)
        series = series.reindex(hours)
        series.index = series.index.tz_convert(TIMEZONE).rename("hour")
        return series

    def _get(self, name: str, *, cache: bool) -> dict:
        path = self.cache_dir / name if self.cache_dir is not None else None
        if cache and path is not None and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        url = f"{BASE_URL}/{name}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as err:
            raise SmardError(f"SMARD request failed: {url}: {err}") from err
        if cache and path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        return payload


def fill_gaps(series: pd.Series, max_gap_hours: int, label: str) -> tuple[pd.Series, int]:
    """Interpolate runs of up to ``max_gap_hours`` missing hours linearly and log them.

    Longer runs, and gaps at either end of the series, raise :class:`SmardError`:
    they are too long to bridge without making data up.
    """
    missing = series.isna()
    if not missing.any():
        return series, 0
    if missing.all():
        raise SmardError(f"{label}: SMARD has no values in the requested period")
    run_id = (missing != missing.shift()).cumsum()
    runs = missing[missing].groupby(run_id[missing]).agg(["size"])["size"]
    first = series.index[missing][0]
    if runs.max() > max_gap_hours or missing.iloc[0] or missing.iloc[-1]:
        raise SmardError(
            f"{label}: {int(missing.sum())} missing hours in {len(runs)} gaps, the longest "
            f"{int(runs.max())} h (first at {first}); gaps longer than {max_gap_hours} h or at "
            "the edge of the period are not filled"
        )
    filled = series.interpolate(method="linear")
    logger.warning(
        "%s: filled %d missing hours in %d gaps by linear interpolation (first at %s)",
        label,
        int(missing.sum()),
        len(runs),
        first,
    )
    return filled, int(missing.sum())


def collect_year(
    year: int,
    out_dir: str | Path,
    client: SmardClient | None = None,
    max_gap_hours: int = 3,
) -> dict[str, pd.DataFrame]:
    """Download one calendar year and write one CSV per table to ``out_dir``.

    Tables (hours x columns, MW): ``load_tso``, ``wind_onshore_tso``,
    ``wind_offshore_tso``, ``solar_tso`` (one column per TSO zone), ``load_de`` and
    ``generation_de`` (one column per fuel). ``gaps`` lists the hours filled per
    series. A TSO zone for which SMARD has no values at all for a technology
    (offshore wind in a zone without a coast) is left out of that table.
    """
    client = client or SmardClient()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(year=year, month=1, day=1, tz=TIMEZONE)
    end = pd.Timestamp(year=year + 1, month=1, day=1, tz=TIMEZONE)
    gaps: list[dict] = []

    def table(columns: Mapping[str, tuple[int, str]], optional: bool = False) -> pd.DataFrame:
        frame = {}
        for column, (filter_id, region) in columns.items():
            label = f"filter {filter_id} / {region} / {year}"
            try:
                raw = client.fetch_hourly(filter_id, region, start, end)
            except SmardNoDataError:
                if not optional:
                    raise
                logger.warning("%s: SMARD has no such series, column %r left out", label, column)
                continue
            frame[column], filled = fill_gaps(raw, max_gap_hours, label)
            gaps.append({"filter_id": filter_id, "region": region, "filled_hours": filled})
        return pd.DataFrame(frame)

    tables = {"load_tso": table({tso: (LOAD_FILTER, tso) for tso in TSO_ZONES})}
    for tech, filter_id in TSO_RES_FILTERS.items():
        tables[f"{tech}_tso"] = table({tso: (filter_id, tso) for tso in TSO_ZONES}, optional=True)
    tables["load_de"] = table({"load": (LOAD_FILTER, "DE")})
    tables["generation_de"] = table({fuel: (fid, "DE") for fuel, fid in GENERATION_FILTERS.items()})
    tables["gaps"] = pd.DataFrame(gaps)

    for name, frame in tables.items():
        frame.to_csv(out_dir / table_filename(name, year), index=name != "gaps")
    return tables


def table_filename(name: str, year: int) -> str:
    return f"smard_{name}_{year}.csv"


def read_table(directory: str | Path, name: str, year: int) -> pd.DataFrame:
    """Read a table written by :func:`collect_year` back with its Europe/Berlin index."""
    path = Path(directory) / table_filename(name, year)
    if not path.is_file():
        raise SmardError(f"missing {path}; run `gridfee collect --year {year}` first")
    frame = pd.read_csv(path, index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True).tz_convert(TIMEZONE).rename("hour")
    return frame


def check_annual_totals(
    hourly_mw: pd.DataFrame, reference_twh: Mapping[str, float], tolerance: float = 0.05
) -> pd.DataFrame:
    """Compare annual sums of ``hourly_mw`` columns with published totals in TWh."""
    unknown = sorted(set(reference_twh) - set(hourly_mw.columns))
    if unknown:
        raise ValueError(f"reference series not in the data: {unknown}")
    rows = []
    for series, reference in reference_twh.items():
        total = float(hourly_mw[series].sum()) / 1e6
        deviation = total / reference - 1.0
        rows.append(
            {
                "series": series,
                "smard_twh": total,
                "reference_twh": float(reference),
                "deviation": deviation,
                "passed": abs(deviation) <= tolerance,
            }
        )
    return pd.DataFrame(
        rows, columns=["series", "smard_twh", "reference_twh", "deviation", "passed"]
    )


def read_reference_totals(path: str | Path, year: int) -> dict[str, float]:
    """Read ``year,series,twh,source`` rows (``#`` comments allowed) for one year."""
    frame = pd.read_csv(path, comment="#")
    missing = {"year", "series", "twh"} - set(frame.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    frame = frame[frame["year"] == year]
    return dict(zip(frame["series"], frame["twh"].astype(float), strict=True))


def _check_request(filter_id: int, region: str) -> None:
    known: Iterable[int] = (*GENERATION_FILTERS.values(), LOAD_FILTER)
    if filter_id not in known:
        raise ValueError(f"filter id {filter_id} is not one of the verified SMARD filters")
    if region not in SMARD_REGIONS:
        raise ValueError(f"region {region!r} is not one of {SMARD_REGIONS}")


def _to_berlin(timestamp: pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(timestamp)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(TIMEZONE)
    return timestamp.tz_convert(TIMEZONE)


def _to_ms(timestamp: pd.Timestamp) -> int:
    return int(timestamp.tz_convert("UTC").timestamp() * 1000)
