from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from gridfee.backcast import read_cost_parameters, read_fuel_prices
from gridfee.data import smard
from gridfee.data.tyndp import read_market, replay, split_other_res, write_replay


@pytest.fixture
def inputs(tmp_path):
    index = pd.date_range("2023-01-01", periods=2, freq="h", tz="Europe/Berlin")
    for name in ["load_tso", "wind_onshore_tso", "wind_offshore_tso", "solar_tso"]:
        pd.DataFrame({"50Hertz": [1, 1], "TenneT": [3, 3]}, index=index).to_csv(
            tmp_path / smard.table_filename(name, 2023)
        )
    pd.DataFrame({"50Hertz_FR": [-1, 1], "TenneT_FR": [-3, 3]}, index=index).to_csv(
        tmp_path / smard.table_filename("physical_flows_tso", 2023)
    )
    market = pd.DataFrame(
        {"DE00_31": [80, 80], "DE00_LOAD": [100, 100], "DE00_DSRe": [10, 10]}, index=[1, 2]
    )
    exchanges = pd.DataFrame({"FR00->DE00": [10, 10], "DE00->DEKF": [50, 50]}, index=[1, 2])
    mapping = pd.DataFrame(
        {
            "code": ["31", "LOAD", "DSRe"],
            "category": ["wind_onshore", "load", "load"],
            "multiplier": [1, 1, -1],
        }
    )
    return market, exchanges, mapping, tmp_path


def test_replay(inputs):
    tables, national, gap, missing = replay(*inputs, 2030)
    assert not missing
    assert gap.eq(0).all()
    assert national.FR.tolist() == [-10, -10]
    assert tables["load_tso"].iloc[0].tolist() == [22.5, 67.5]
    assert tables["physical_flows_tso"].iloc[0].tolist() == [-2.5, -7.5]
    assert str(tables["load_de"].index[0]) == "2030-01-01 00:00:00+01:00"
    write_replay(tables, inputs[-1] / "out", 2030)
    loaded = smard.read_table(inputs[-1] / "out", "generation_de", 2030)
    assert len(loaded) == 2
    assert loaded.wind_onshore.eq(80).all()


def test_fixed_cet_calendar(inputs):
    market, exchanges, mapping, raw = inputs
    market = pd.concat([market.iloc[:1]] * 8736, ignore_index=True)
    exchanges = pd.concat([exchanges.iloc[:1]] * 8736, ignore_index=True)
    market.index = exchanges.index = range(1, 8737)
    tables, _, _, _ = replay(market, exchanges, mapping, raw, 2030)
    index = tables["load_de"].index
    assert str(index[-1]) == "2030-12-30 23:00:00+01:00"
    assert str(index[5000].utcoffset()) == "1:00:00"
    assert index.to_series().diff().dropna().eq(pd.Timedelta(hours=1)).all()


@pytest.mark.parametrize("case", ["empty", "hours", "technology", "balance", "duplicate"])
def test_replay_errors(inputs, case):
    market, exchanges, mapping, raw = inputs
    if case == "empty":
        market = market.iloc[:0]
    elif case == "hours":
        market.index = [2, 3]
    elif case == "technology":
        market["DE00_new"] = 1
    elif case == "balance":
        market["DE00_LOAD"] = 200
    else:
        mapping = pd.concat([mapping, mapping.iloc[:1]])
    with pytest.raises(ValueError):
        replay(market, exchanges, mapping, raw, 2030)


def test_unknown_border_reported(inputs):
    market, exchanges, mapping, raw = inputs
    exchanges = exchanges.rename(columns={"FR00->DE00": "UK00->DE00"})
    _, _, _, missing = replay(market, exchanges, mapping, raw, 2030)
    assert missing == ["UK"]


def test_border_routing(inputs):
    market, exchanges, mapping, raw = inputs
    reference = smard.read_table(raw, "physical_flows_tso", 2023)
    reference["50Hertz_DK2"] = [1, 1]
    reference.to_csv(raw / smard.table_filename("physical_flows_tso", 2023))
    exchanges["DE00->DKE1"] = -2
    exchanges["DE00->DKBH"] = -3
    exchanges["DE00->UK00"] = -4
    market["DE00_LOAD"] += 9
    routing = pd.DataFrame(
        [("DKBH", "50Hertz", "DK2"), ("UK", "TenneT", "NO2"), ("FR", "TenneT", "NO2")],
        columns=["neighbour", "tso", "smard_neighbour"],
    )
    tables, _, _, missing = replay(market, exchanges, mapping, raw, 2030, border_routing=routing)
    assert not missing
    flows = tables["physical_flows_tso"]
    assert flows["50Hertz_DK2"].eq(-5).all()
    assert flows["TenneT_NO2"].eq(-4).all()
    assert flows["50Hertz_FR"].eq(-2.5).all()
    assert flows.sum(axis=1).eq(-19).all()
    with pytest.raises(ValueError, match="duplicate border routing"):
        replay(*inputs, 2030, border_routing=pd.concat([routing, routing]))
    exchanges = exchanges.rename(columns={"DE00->UK00": "DE00->NEW"})
    _, _, _, missing = replay(market, exchanges, mapping, raw, 2030, border_routing=routing)
    assert missing == ["NEW"]


def workbook(path):
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, header, start in [("Hourly Market Data", 13, 14), ("Crossborder exchanges", 11, 12)]:
        sheet = book.create_sheet(name)
        sheet.cell(1, 2, "NT30")
        sheet.cell(4, 2, "2009")
        sheet.cell(header, 1, "Hour")
        sheet.cell(header, 2, "DE00_LOAD" if header == 13 else "FR00->DE00")
        sheet.cell(start, 1, 1)
        sheet.cell(start, 2, 10)
    book.save(path)


def test_read_market(tmp_path):
    path = tmp_path / "market.xlsx"
    workbook(path)
    market, exchanges = read_market(path, "NT", 2030, 2009)
    assert market.DE00_LOAD.tolist() == [10]
    assert exchanges["FR00->DE00"].tolist() == [10]
    with pytest.raises(ValueError, match="expected"):
        read_market(path, "NT", 2040, 2009)


def test_other_res(tmp_path):
    path = tmp_path / "plants.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Other RES"
    sheet.cell(4, 2, "DE00")
    sheet.cell(5, 2, "2030")
    sheet.cell(6, 2, "NationalTrends")
    for i, value in enumerate([4, 0, 0, 2, 1], 5):
        sheet.cell(11, i, value)
    book.save(path)
    market = pd.DataFrame({"DE00_35": [7]}, index=[1])
    split = split_other_res(market, path, "NT", 2030)
    assert split.DE00_biomass.tolist() == [4]
    assert split.DE00_waste.tolist() == [2]
    assert split.DE00_35.eq(0).all()
    with pytest.raises(ValueError, match="components"):
        split_other_res(market * 2, path, "NT", 2030)
    with pytest.raises(ValueError, match="scenario/year"):
        split_other_res(market, path, "NT", 2040)


def test_committed_mapping_and_costs():
    root = Path(__file__).parents[1]
    mapping = pd.read_csv(root / "data/tyndp/technology_mapping.csv", dtype={"code": str})
    assert not mapping.code.duplicated().any()
    costs = read_cost_parameters(root / "data/costs/generation_costs_2030.csv")
    assert costs.loc["gas", "fuel_eur_per_mwh_th"] == 27
    monthly = read_fuel_prices(root / "data/costs/fuel_prices_2030.csv")
    assert monthly.shape == (12, 4)
