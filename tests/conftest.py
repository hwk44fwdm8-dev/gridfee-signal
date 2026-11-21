from pathlib import Path

import pandas as pd
import pytest

from gridfee.network import ZoneNetwork, load_network

EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.fixture(scope="session")
def two_zone() -> ZoneNetwork:
    return load_network(EXAMPLES / "networks" / "two_zone")


@pytest.fixture(scope="session")
def three_zone() -> ZoneNetwork:
    return load_network(EXAMPLES / "networks" / "three_zone")


@pytest.fixture
def hours() -> pd.DatetimeIndex:
    return pd.date_range("2023-01-01", periods=4, freq="h", tz="Europe/Berlin", name="snapshot")


def make_generators(rows: dict[str, tuple[str, float, float]]) -> pd.DataFrame:
    """rows: generator id -> (zone, p_nom_mw, marginal_cost_eur_per_mwh)."""
    return pd.DataFrame.from_dict(
        rows, orient="index", columns=["zone", "p_nom_mw", "marginal_cost_eur_per_mwh"]
    )
