"""Power transfer distribution factors of a zone network."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from gridfee.network.schema import ZoneNetwork


def compute_ptdf(
    network: ZoneNetwork, reference: str | Mapping[str, float] | None = None
) -> pd.DataFrame:
    """PTDF matrix, lines in rows and zones in columns.

    Entry (l, n) is the change of the flow on line l, counted positive from its
    ``from_zone`` to its ``to_zone``, when 1 MW more is injected at zone n and
    withdrawn at the reference.

    ``reference`` is either one zone id (single slack; default: the first zone) or a
    mapping zone id -> weight for a distributed slack, where the injection is
    withdrawn across zones in proportion to the weights. Differences between two
    columns do not depend on the reference.
    """
    zone_ids = network.zone_ids
    weights = _reference_weights(zone_ids, reference)

    position = {z: i for i, z in enumerate(zone_ids)}
    incidence = np.zeros((len(network.lines), len(zone_ids)))
    susceptance = np.empty(len(network.lines))
    for row, line in enumerate(network.lines):
        incidence[row, position[line.from_zone]] = 1.0
        incidence[row, position[line.to_zone]] = -1.0
        susceptance[row] = line.susceptance_mw_per_rad

    # flows = diag(b) K theta and injections = K' diag(b) K theta. Fix theta = 0 at
    # the first zone to make the nodal susceptance matrix invertible.
    weighted_incidence = susceptance[:, None] * incidence
    nodal = incidence.T @ weighted_incidence
    ptdf = np.zeros_like(incidence)
    if len(zone_ids) > 1:
        ptdf[:, 1:] = np.linalg.solve(nodal[1:, 1:], weighted_incidence[:, 1:].T).T

    ptdf -= (ptdf @ weights)[:, None]
    return pd.DataFrame(
        ptdf,
        index=pd.Index(network.line_ids, name="line_id"),
        columns=pd.Index(zone_ids, name="zone_id"),
    )


def _reference_weights(
    zone_ids: list[str], reference: str | Mapping[str, float] | None
) -> np.ndarray:
    if reference is None:
        reference = zone_ids[0]
    if isinstance(reference, str):
        reference = {reference: 1.0}

    unknown = sorted(set(reference) - set(zone_ids))
    if unknown:
        raise ValueError(f"reference refers to unknown zones: {unknown}")
    weights = np.array([float(reference.get(z, 0.0)) for z in zone_ids])
    if not np.all(np.isfinite(weights)) or (weights < 0).any() or weights.sum() <= 0:
        raise ValueError("reference weights must be finite, non-negative and sum to more than 0")
    return weights / weights.sum()
