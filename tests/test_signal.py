"""Fee signal: hand-checked numbers, the sign convention and the classification."""

from __future__ import annotations

import pandas as pd
import pytest

from gridfee.flows import run_dc_opf
from gridfee.signal import (
    NEGATIVE,
    NEUTRAL,
    POSITIVE,
    SignalError,
    classify_signal,
    compute_fee_signal,
    write_signal,
)

from .conftest import make_generators

HOURS = pd.Index(["h1", "h2", "h3"], name="snapshot")
# Line A-B, reference A: 1 MW injected at B flows B -> A, i.e. -1 on the line.
PTDF = pd.DataFrame(
    [[0.0, -1.0]], index=pd.Index(["A-B"], name="line_id"), columns=pd.Index(["A", "B"])
)
SHADOW = pd.DataFrame({"A-B": [60.0, 0.0, -20.0]}, index=HOURS)


def test_signal_is_ptdf_times_shadow_price():
    result = compute_fee_signal(SHADOW, PTDF)

    assert result.signal["A"].tolist() == [0.0, 0.0, 0.0]
    assert result.signal["B"].tolist() == [-60.0, 0.0, 20.0]
    assert result.signal.index.equals(HOURS)


def test_sign_convention_feeding_in_behind_a_full_line_is_negative():
    # h1: A-B is full from A to B. Feeding in at B relieves it: negative signal.
    # h3: it is full from B to A. Feeding in at B adds to it: positive signal.
    labels = compute_fee_signal(SHADOW, PTDF).classification

    assert labels["B"].tolist() == [NEGATIVE, NEUTRAL, POSITIVE]
    assert labels["A"].tolist() == [NEUTRAL] * 3


def test_signal_sums_over_lines_and_aligns_them_by_id():
    ptdf = pd.DataFrame(
        {"A": [0.0, 0.0], "B": [-0.5, 0.25], "C": [0.5, -1.0]},
        index=pd.Index(["L1", "L2"], name="line_id"),
    )
    shadow = pd.DataFrame({"L2": [4.0], "L1": [10.0]}, index=["h1"])

    signal = compute_fee_signal(shadow, ptdf).signal

    assert signal.loc["h1"].tolist() == [0.0, -0.5 * 10 + 0.25 * 4, 0.5 * 10 - 1.0 * 4]


def test_signal_is_reference_price_minus_zone_price(three_zone):
    load = pd.DataFrame({"C": [250.0, 30.0]}, index=["peak", "off"])
    generators = make_generators({"cheap": ("A", 300.0, 20.0), "dear": ("C", 300.0, 80.0)})

    opf = run_dc_opf(three_zone, load, generators, ptdf_reference="A")
    signal = compute_fee_signal(opf.shadow_prices, opf.ptdf).signal

    assert opf.congested().loc["peak"].any()
    expected = -opf.nodal_prices.sub(opf.nodal_prices["A"], axis=0)
    pd.testing.assert_frame_equal(
        signal, expected, atol=1e-6, check_names=False, check_column_type=False
    )
    # C sits behind the congestion: feeding in there relieves it.
    assert signal.loc["peak", "C"] == pytest.approx(-60.0)
    assert (signal.loc["off"].abs() < 1e-9).all()


def test_difference_between_zones_does_not_depend_on_the_reference(three_zone):
    load = pd.DataFrame({"C": [250.0]}, index=["peak"])
    generators = make_generators({"cheap": ("A", 300.0, 20.0), "dear": ("C", 300.0, 80.0)})

    spreads = []
    for reference in ("A", "C", {"A": 1.0, "B": 2.0, "C": 3.0}):
        opf = run_dc_opf(three_zone, load, generators, ptdf_reference=reference)
        signal = compute_fee_signal(opf.shadow_prices, opf.ptdf).signal
        spreads.append(signal.loc["peak", "A"] - signal.loc["peak", "C"])

    assert spreads == pytest.approx([60.0, 60.0, 60.0])


def test_threshold_makes_small_signals_neutral():
    signal = pd.DataFrame({"Z": [5.0, 5.01, -5.0, -5.01, 0.0]})

    assert classify_signal(signal, 5.0)["Z"].tolist() == [
        NEUTRAL,
        POSITIVE,
        NEUTRAL,
        NEGATIVE,
        NEUTRAL,
    ]
    assert classify_signal(signal)["Z"].tolist() == [
        POSITIVE,
        POSITIVE,
        NEGATIVE,
        NEGATIVE,
        NEUTRAL,
    ]
    assert compute_fee_signal(SHADOW, PTDF, threshold=30.0).classification["B"].tolist() == [
        NEGATIVE,
        NEUTRAL,
        NEUTRAL,
    ]


@pytest.mark.parametrize("threshold", [-1.0, float("nan"), float("inf")])
def test_bad_threshold_is_rejected(threshold):
    with pytest.raises(SignalError, match="threshold"):
        classify_signal(pd.DataFrame({"Z": [1.0]}), threshold)


def test_lines_must_match():
    with pytest.raises(SignalError, match="only in shadow_prices: \\['X'\\]"):
        compute_fee_signal(SHADOW.assign(X=0.0), PTDF)
    with pytest.raises(SignalError, match="only in ptdf: \\['A-B'\\]"):
        compute_fee_signal(SHADOW.rename(columns={"A-B": "B-A"}), PTDF)


def test_missing_values_are_rejected():
    with pytest.raises(SignalError, match="missing values"):
        compute_fee_signal(SHADOW.mask(SHADOW == 0.0), PTDF)


def test_summary_counts_hours_per_class():
    summary = compute_fee_signal(SHADOW, PTDF).summary()

    assert summary.loc["B", ["positive_hours", "negative_hours", "neutral_hours"]].tolist() == [
        1,
        1,
        1,
    ]
    assert summary.loc["B", "mean_signal"] == pytest.approx(-40.0 / 3)


@pytest.mark.parametrize("file_format", ["csv", "parquet"])
def test_write_signal(tmp_path, file_format):
    result = compute_fee_signal(SHADOW, PTDF)

    paths = write_signal(result, tmp_path / "out", file_format)

    assert [path.name for path in paths] == [
        f"fee_signal.{file_format}",
        f"fee_signal_class.{file_format}",
        "fee_signal_summary.csv",
    ]
    read = pd.read_csv if file_format == "csv" else pd.read_parquet
    kwargs = {"index_col": 0} if file_format == "csv" else {}
    assert read(paths[0], **kwargs)["B"].tolist() == [-60.0, 0.0, 20.0]
    assert read(paths[1], **kwargs)["B"].tolist() == [NEGATIVE, NEUTRAL, POSITIVE]


def test_unknown_format_is_rejected(tmp_path):
    with pytest.raises(SignalError, match="format"):
        write_signal(compute_fee_signal(SHADOW, PTDF), tmp_path, "xlsx")
