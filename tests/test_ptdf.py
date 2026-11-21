import numpy as np
import pandas as pd
import pytest

from gridfee.flows import build_pypsa_network, compute_ptdf


def test_two_zone_ptdf(two_zone):
    ptdf = compute_ptdf(two_zone)
    # 1 MW injected at S and withdrawn at the reference N flows against N -> S.
    assert ptdf.loc["N-S"].to_dict() == {"N": 0.0, "S": -1.0}


def test_three_zone_ptdf_matches_hand_calculation(three_zone):
    # Triangle A-B-C with equal susceptances: an injection at C withdrawn at A
    # splits 2/3 over the direct line and 1/3 over the detour via B.
    expected = pd.DataFrame(
        [
            [0, -2 / 3, -1 / 3, -1 / 3],
            [0, 1 / 3, -1 / 3, -1 / 3],
            [0, -1 / 3, -2 / 3, -2 / 3],
            [0, 0, 0, -1],
        ],
        index=["A-B", "B-C", "A-C", "C-X"],
        columns=["A", "B", "C", "X"],
    )
    ptdf = compute_ptdf(three_zone, reference="A")
    np.testing.assert_allclose(ptdf.to_numpy(), expected.to_numpy(), atol=1e-12)
    assert list(ptdf.index) == list(expected.index)
    assert list(ptdf.columns) == list(expected.columns)


def test_ptdf_matches_pypsa(three_zone):
    n = build_pypsa_network(three_zone)
    n.determine_network_topology()
    sub_network = n.sub_networks.obj.iloc[0]
    sub_network.calculate_PTDF()
    theirs = pd.DataFrame(
        sub_network.PTDF,
        index=sub_network.branches_i().get_level_values(1),
        columns=sub_network.buses_o,
    )
    ours = compute_ptdf(three_zone, reference=sub_network.buses_o[0])
    theirs = theirs.loc[ours.index, ours.columns]
    np.testing.assert_allclose(ours.to_numpy(), theirs.to_numpy(), atol=1e-9)


def test_reference_only_shifts_columns(three_zone):
    single = compute_ptdf(three_zone, reference="A")
    other = compute_ptdf(three_zone, reference="C")
    distributed = compute_ptdf(three_zone, reference={"A": 1, "B": 1, "C": 2})

    for ptdf in (other, distributed):
        shift = (ptdf - single).to_numpy()
        np.testing.assert_allclose(shift, shift[:, [0]].repeat(shift.shape[1], axis=1), atol=1e-12)

    np.testing.assert_allclose(other["C"].to_numpy(), 0, atol=1e-12)
    weighted = distributed["A"] * 0.25 + distributed["B"] * 0.25 + distributed["C"] * 0.5
    np.testing.assert_allclose(weighted.to_numpy(), 0, atol=1e-12)


@pytest.mark.parametrize("reference", ["Z", {"A": -1.0, "B": 2.0}, {"A": 0.0}, {}])
def test_invalid_reference(three_zone, reference):
    with pytest.raises(ValueError):
        compute_ptdf(three_zone, reference=reference)
