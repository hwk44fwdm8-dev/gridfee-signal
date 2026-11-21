"""SMARD collector, tested against a fake HTTP session shaped like the real API."""

from __future__ import annotations

import logging

import pandas as pd
import pytest
import requests

from gridfee.data import smard
from gridfee.data.smard import SmardClient, SmardError, SmardNoDataError

HOUR_MS = 3600 * 1000


def ms(text: str) -> int:
    return int(pd.Timestamp(text, tz="Europe/Berlin").timestamp() * 1000)


class FakeResponse:
    def __init__(self, payload: dict | None, status: int = 200) -> None:
        self.payload, self.status_code = payload, status

    def raise_for_status(self) -> None:
        if self.status_code != 200:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)

    def json(self) -> dict:
        return self.payload


class FakeSession:
    """Serves weekly chunks; the value of an hour is its number since ``first``."""

    def __init__(self, first: int, weeks: int, missing: set[int] = frozenset()) -> None:
        self.first, self.missing = first, set(missing)
        # Like SMARD, chunks start on Monday 00:00 Berlin time, so a week with a
        # clock change has 167 or 169 hours.
        monday = pd.Timestamp(first, unit="ms", tz="UTC").tz_convert("Europe/Berlin")
        mondays = pd.date_range(monday, periods=weeks + 1, freq="7D")
        self.starts = [int(t.timestamp() * 1000) for t in mondays]
        self.absent_series: set[tuple[int, str]] = set()
        self.urls: list[str] = []

    def value(self, timestamp: int) -> float | None:
        return None if timestamp in self.missing else float((timestamp - self.first) // HOUR_MS)

    def get(self, url: str, timeout: float) -> FakeResponse:
        self.urls.append(url)
        filter_id, region, name = url.removeprefix(smard.BASE_URL + "/").split("/")
        if (int(filter_id), region) in self.absent_series:
            return FakeResponse(None, 404)
        starts = self.starts[:-1]
        if name == "index_hour.json":
            return FakeResponse({"timestamps": starts})
        start = int(name.removesuffix(".json").rsplit("_", 1)[1])
        assert name == f"{filter_id}_{region}_hour_{start}.json" and start in starts
        end = self.starts[starts.index(start) + 1]
        series = [[t, self.value(t)] for t in range(start, end, HOUR_MS)]
        return FakeResponse({"meta_data": {"version": 1}, "series": series})

    def chunk_requests(self) -> int:
        return sum("index_hour" not in url for url in self.urls)


FIRST = ms("2022-12-26")  # a Monday, like the real SMARD chunk starts


def test_filter_ids_are_the_documented_ones():
    # bundesAPI/smard-api openapi.yaml, "filter" parameter.
    assert smard.GENERATION_FILTERS == {
        "lignite": 1223,
        "nuclear": 1224,
        "wind_offshore": 1225,
        "hydro": 1226,
        "other_conventional": 1227,
        "other_renewable": 1228,
        "biomass": 4066,
        "wind_onshore": 4067,
        "solar": 4068,
        "hard_coal": 4069,
        "pumped_storage": 4070,
        "gas": 4071,
    }
    assert smard.LOAD_FILTER == 410
    assert smard.SMARD_REGIONS == ("DE", "50Hertz", "Amprion", "TenneT", "TransnetBW")


@pytest.mark.parametrize(("filter_id", "region"), [(4075, "DE"), (410, "FR"), (4083, "DE")])
def test_undocumented_filters_and_regions_are_refused(filter_id, region):
    session = FakeSession(FIRST, 3)
    with pytest.raises(ValueError, match="verified SMARD filters|region"):
        SmardClient(session=session).fetch_hourly(
            filter_id, region, pd.Timestamp("2023-01-01"), pd.Timestamp("2023-01-02")
        )
    assert session.urls == []


def test_fetch_returns_berlin_hours_across_chunks():
    session = FakeSession(FIRST, 4)
    series = SmardClient(session=session).fetch_hourly(
        410, "TenneT", pd.Timestamp("2023-01-01"), pd.Timestamp("2023-01-10")
    )
    assert len(series) == 9 * 24
    assert str(series.index.tz) == "Europe/Berlin"
    assert series.index[0] == pd.Timestamp("2023-01-01", tz="Europe/Berlin")
    assert series.index[-1] == pd.Timestamp("2023-01-09 23:00", tz="Europe/Berlin")
    assert series.iloc[0] == 6 * 24  # hours since Monday 26 December
    assert (series.diff().dropna() == 1).all()
    assert session.chunk_requests() == 3  # the fourth week is not needed


def test_daylight_saving_days_have_23_and_25_hours():
    client = SmardClient(session=FakeSession(ms("2023-03-20"), 40))
    spring = client.fetch_hourly(410, "DE", pd.Timestamp("2023-03-26"), pd.Timestamp("2023-03-27"))
    autumn = client.fetch_hourly(410, "DE", pd.Timestamp("2023-10-29"), pd.Timestamp("2023-10-30"))
    assert (len(spring), len(autumn)) == (23, 25)
    assert not spring.isna().any() and not autumn.isna().any()
    assert autumn.index.is_unique


def test_last_hour_of_the_169_hour_week_is_found():
    # Monday 23 October 2023 starts a chunk of 169 hours: its last hour begins more
    # than 7 days after the chunk does.
    session = FakeSession(ms("2023-10-23"), 2)
    series = SmardClient(session=session).fetch_hourly(
        410, "DE", pd.Timestamp("2023-10-29 23:00"), pd.Timestamp("2023-10-30 01:00")
    )
    assert series.tolist() == [168.0, 169.0]


def test_cache_avoids_second_download(tmp_path):
    session = FakeSession(FIRST, 3)
    args = (4068, "Amprion", pd.Timestamp("2023-01-01"), pd.Timestamp("2023-01-05"))
    first = SmardClient(tmp_path, session).fetch_hourly(*args)
    assert session.chunk_requests() == 2
    second = SmardClient(tmp_path, session).fetch_hourly(*args)
    assert session.chunk_requests() == 2
    pd.testing.assert_series_equal(first, second)
    assert (tmp_path / "4068" / "Amprion" / f"4068_Amprion_hour_{FIRST}.json").is_file()


def test_unfinished_week_is_not_cached(tmp_path):
    now = pd.Timestamp.now(tz="Europe/Berlin").floor("h")
    session = FakeSession(int((now - pd.Timedelta(days=3)).timestamp() * 1000), 1)
    for _ in range(2):
        SmardClient(tmp_path, session).fetch_hourly(410, "DE", now - pd.Timedelta(days=2), now)
    assert session.chunk_requests() == 2
    assert not list(tmp_path.rglob("*.json"))


def test_missing_values_come_back_as_nan():
    hole = ms("2023-01-02 05:00")
    series = SmardClient(session=FakeSession(FIRST, 3, {hole})).fetch_hourly(
        410, "DE", pd.Timestamp("2023-01-02"), pd.Timestamp("2023-01-03")
    )
    assert series.isna().sum() == 1
    assert pd.isna(series[pd.Timestamp("2023-01-02 05:00", tz="Europe/Berlin")])


def test_http_errors_are_reported():
    session = FakeSession(FIRST, 3)
    session.absent_series.add((1225, "Amprion"))
    with pytest.raises(SmardNoDataError, match="1225.*Amprion"):
        SmardClient(session=session).fetch_hourly(
            1225, "Amprion", pd.Timestamp("2023-01-01"), pd.Timestamp("2023-01-02")
        )

    class Broken:
        def get(self, url, timeout):
            return FakeResponse(None, 500)

    with pytest.raises(SmardError, match="500") as caught:
        SmardClient(session=Broken()).fetch_hourly(
            410, "DE", pd.Timestamp("2023-01-01"), pd.Timestamp("2023-01-02")
        )
    assert not isinstance(caught.value, SmardNoDataError)


def hourly(values: list[float | None]) -> pd.Series:
    index = pd.date_range("2023-01-01", periods=len(values), freq="h", tz="Europe/Berlin")
    return pd.Series(values, index=index, dtype=float)


def test_short_gaps_are_interpolated_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="gridfee.data.smard"):
        filled, count = smard.fill_gaps(hourly([1, None, None, 4, 5, None, 7]), 3, "load")
    assert filled.tolist() == [1, 2, 3, 4, 5, 6, 7]
    assert count == 3
    assert "load: filled 3 missing hours in 2 gaps" in caplog.text


def test_complete_series_is_untouched(caplog):
    series = hourly([1, 2, 3])
    filled, count = smard.fill_gaps(series, 3, "load")
    assert filled is series and count == 0 and caplog.text == ""


@pytest.mark.parametrize(
    "values",
    [[1, None, None, None, None, 6], [None, 2, 3], [1, 2, None], [None, None]],
    ids=["long", "start", "end", "empty"],
)
def test_long_and_edge_gaps_are_not_filled(values):
    with pytest.raises(SmardError, match="load"):
        smard.fill_gaps(hourly(values), 3, "load")


def test_collect_year_writes_tables(tmp_path):
    session = FakeSession(ms("2022-12-26"), 54, {ms("2023-06-01 12:00")})
    session.absent_series |= {(1225, "Amprion"), (1225, "TransnetBW")}
    tables = smard.collect_year(2023, tmp_path / "raw", SmardClient(tmp_path / "cache", session))

    assert set(tables) == {
        "load_tso",
        "wind_onshore_tso",
        "wind_offshore_tso",
        "solar_tso",
        "load_de",
        "generation_de",
        "gaps",
    }
    assert list(tables["load_tso"].columns) == ["50Hertz", "Amprion", "TenneT", "TransnetBW"]
    assert list(tables["wind_offshore_tso"].columns) == ["50Hertz", "TenneT"]
    assert list(tables["generation_de"].columns) == list(smard.GENERATION_FILTERS)
    for name in set(tables) - {"gaps"}:
        assert len(tables[name]) == 8760
        assert not tables[name].isna().any().any()
    assert (tables["gaps"]["filled_hours"] == 1).all()

    read = smard.read_table(tmp_path / "raw", "load_tso", 2023)
    pd.testing.assert_frame_equal(read, tables["load_tso"], check_freq=False)
    assert str(read.index.tz) == "Europe/Berlin"
    with pytest.raises(SmardError, match="gridfee collect"):
        smard.read_table(tmp_path / "raw", "load_tso", 2022)


def test_annual_totals_are_checked_against_reference(tmp_path):
    index = pd.date_range("2023-01-01", periods=8760, freq="h", tz="Europe/Berlin")
    hourly_mw = pd.DataFrame({"wind_onshore": 13_000.0, "solar": 6_000.0}, index=index)
    reference = tmp_path / "ref.csv"
    reference.write_text(
        "# comment\nyear,series,twh,source\n2023,wind_onshore,119.2,a\n"
        "2023,solar,50.0,b\n2022,solar,1.0,c\n"
    )
    expected = smard.read_reference_totals(reference, 2023)
    assert expected == {"wind_onshore": 119.2, "solar": 50.0}

    report = smard.check_annual_totals(hourly_mw, expected).set_index("series")
    assert report.loc["wind_onshore", "smard_twh"] == pytest.approx(113.88)
    assert report.loc["wind_onshore", "deviation"] == pytest.approx(113.88 / 119.2 - 1)
    assert report["passed"].to_dict() == {"wind_onshore": True, "solar": False}
    with pytest.raises(ValueError, match="gas"):
        smard.check_annual_totals(hourly_mw, {"gas": 1.0})
