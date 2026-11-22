"""Command line interface. Scenario commands arrive with later milestones."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import typer

from gridfee.backcast import BackcastError, build_backcast_inputs, run_backcast
from gridfee.data import smard
from gridfee.flows import OPFError, OPFResult, run_dc_opf
from gridfee.injections import build_historical_injections, check_energy_balance, read_shares
from gridfee.injections.weather import read_capacity_factors
from gridfee.network import NetworkConfigError, ZoneNetwork, load_network, load_zones
from gridfee.signal import NEUTRAL, SignalError, compute_fee_signal, write_signal
from gridfee.validation import FAIL, ValidationError, validate_backcast, write_report

DE22 = Path("data/networks/de22")
# Signals smaller than this are solver noise, not congestion.
DEFAULT_THRESHOLD = 1e-6
SHEDDING_TOLERANCE_MW = 1e-3

app = typer.Typer(
    help="Congestion-based grid fee signals for battery storage in Germany.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Congestion-based grid fee signals for battery storage in Germany."""


@app.command()
def run(
    network_dir: Path = typer.Option(
        ..., "--network", help="Directory with zones.csv and lines.csv."
    ),
    load: Path = typer.Option(..., help="CSV: first column snapshot, one column per zone, MW."),
    generators: Path = typer.Option(
        ..., help="CSV: generator_id, zone, p_nom_mw, marginal_cost_eur_per_mwh."
    ),
    availability: Path | None = typer.Option(
        None, help="CSV: first column snapshot, one column per generator, 0..1."
    ),
    hours: Path | None = typer.Option(
        None, help="CSV whose first column lists the snapshots to solve (default: all)."
    ),
    load_shedding_cost: float | None = typer.Option(
        None, help="Allow unserved load at this cost in EUR/MWh (default: not allowed)."
    ),
    out: Path = typer.Option(Path("results"), help="Output directory."),
) -> None:
    """Solve the DC OPF and write flows, shadow prices, nodal prices and the PTDF."""
    try:
        network = load_network(network_dir)
        result = run_dc_opf(
            network,
            load_mw=_read_hourly(load),
            generators=pd.read_csv(generators, dtype={"generator_id": str, "zone": str}).set_index(
                "generator_id"
            ),
            availability=None if availability is None else _read_hourly(availability),
            snapshots=None if hours is None else _read_hourly(hours).index,
            load_shedding_cost=load_shedding_cost,
        )
    except (NetworkConfigError, OPFError, OSError, KeyError, ValueError) as err:
        typer.secho(f"Error: {err}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from err

    _write_opf(result, out)
    congested = result.congested()
    typer.echo(
        f"Solved {len(result.flows_mw)} hours on {len(network.zones)} zones and "
        f"{len(network.lines)} lines; {int(congested.any(axis=1).sum())} hours with congestion. "
        f"Results in {out}/"
    )


@app.command()
def collect(
    year: int = typer.Option(..., help="Calendar year to download."),
    out: Path = typer.Option(Path("data/raw"), help="Output directory for the tables."),
    cache: Path = typer.Option(Path("data/cache/smard"), help="Cache of downloaded files."),
    max_gap_hours: int = typer.Option(3, help="Longest run of missing hours to interpolate."),
    reference: Path | None = typer.Option(
        None, help="CSV year,series,twh,source of published annual totals to check against."
    ),
    tolerance: float = typer.Option(0.05, help="Allowed deviation from the reference totals."),
) -> None:
    """Download hourly SMARD load, generation and cross-border flow data for one year."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        tables = smard.collect_year(year, out, smard.SmardClient(cache), max_gap_hours)
        totals = None
        if reference is not None:
            expected = smard.read_reference_totals(reference, year)
            totals = smard.check_annual_totals(
                smard.annual_check_series(tables), expected, tolerance
            )
    except (smard.SmardError, OSError, ValueError) as err:
        typer.secho(f"Error: {err}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from err

    filled = int(tables["gaps"]["filled_hours"].sum())
    typer.echo(f"Wrote {len(tables)} tables for {year} to {out}/; {filled} missing hours filled.")
    unfilled = int(tables["gaps"]["missing_hours"].sum())
    if unfilled:
        typer.secho(
            f"{unfilled} hours of cross-border series are still missing (gaps too long to "
            f"fill); see {smard.table_filename('gaps', year)}.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if totals is not None:
        typer.echo(totals.to_string(index=False))
        if not totals["passed"].all():
            typer.secho("Error: annual totals outside the tolerance", fg="red", err=True)
            raise typer.Exit(code=1)


@app.command()
def injections(
    zones: Path = typer.Option(..., help="zones.csv with the TSO zone of every zone."),
    data: Path = typer.Option(Path("data/raw"), help="Directory written by `gridfee collect`."),
    year: int = typer.Option(..., help="Calendar year."),
    population_shares: Path | None = typer.Option(
        None, help="CSV zone_id,share (share of the TSO zone's load). Default: placeholders."
    ),
    capacity_shares: Path | None = typer.Option(
        None, help="CSV technology,zone_id,share. Default: placeholders."
    ),
    capacity_factors: Path | None = typer.Option(
        None,
        help="CSV of hourly capacity factors per zone (scripts/build_capacity_factors.py). "
        "Default: every zone of a TSO zone has the TSO zone's hourly shape.",
    ),
    out: Path = typer.Option(Path("results/injections"), help="Output directory."),
) -> None:
    """Split the TSO-zone load and wind/solar series of a year over the zones."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        zone_list = load_zones(zones)
        tso_load = smard.read_table(data, "load_tso", year)
        tso_res = {
            tech: smard.read_table(data, f"{tech}_tso", year) for tech in smard.TSO_RES_FILTERS
        }
        result = build_historical_injections(
            zone_list,
            tso_load,
            tso_res,
            None if population_shares is None else read_shares(population_shares),
            None if capacity_shares is None else read_shares(capacity_shares, "technology"),
            capacity_factors=None
            if capacity_factors is None
            else read_capacity_factors(capacity_factors),
        )
        balance = check_energy_balance(zone_list, result, tso_load, tso_res)
    except (NetworkConfigError, smard.SmardError, OSError, ValueError) as err:
        typer.secho(f"Error: {err}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from err

    out.mkdir(parents=True, exist_ok=True)
    result.demand_mw.to_csv(out / "demand_mw.csv")
    for tech, frame in result.res_mw.items():
        frame.to_csv(out / f"{tech}_mw.csv")
    result.net_injection_mw.to_csv(out / "net_injection_mw.csv")
    result.shares.to_csv(out / "shares.csv", index=False)
    balance.to_csv(out / "energy_balance.csv", index=False)

    typer.echo(balance.to_string(index=False))
    typer.echo(f"Injections for {len(result.demand_mw)} hours in {out}/")
    if result.placeholder_quantities:
        typer.secho(
            "PLACEHOLDER shares (uniform within each TSO zone) were used for: "
            + ", ".join(result.placeholder_quantities)
            + ". These results do not show where power is produced or used.",
            fg=typer.colors.YELLOW,
            err=True,
        )


@app.command()
def signal(
    results: Path = typer.Option(
        ..., help="Directory written by `gridfee run` (shadow_prices.csv and ptdf.csv)."
    ),
    threshold: float = typer.Option(
        DEFAULT_THRESHOLD, help="Signals of at most this size (EUR/MWh) are neutral."
    ),
    file_format: str = typer.Option("csv", "--format", help="csv or parquet."),
    out: Path | None = typer.Option(None, help="Output directory (default: --results)."),
) -> None:
    """Compute the fee signal per zone and hour from shadow prices and the PTDF."""
    try:
        shadow_prices = _read_hourly(results / "shadow_prices.csv")
        # keep_default_na: a line may be called "NA".
        ptdf = pd.read_csv(
            results / "ptdf.csv", index_col=0, dtype={0: str}, keep_default_na=False
        ).astype(float)
        result = compute_fee_signal(shadow_prices, ptdf, threshold)
        paths = write_signal(result, out or results, file_format)
    except (SignalError, OSError, ValueError) as err:
        typer.secho(f"Error: {err}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from err
    typer.echo(result.summary().to_string())
    typer.echo("Wrote " + ", ".join(str(path) for path in paths))


@app.command()
def validate(
    results: Path = typer.Option(
        ..., help="Directory written by `gridfee backcast` (flows_mw.csv, dispatch_mw.csv)."
    ),
    year: int = typer.Option(..., help="Calendar year of the backcast."),
    network_dir: Path = typer.Option(
        DE22, "--network", help="Directory with zones.csv and lines.csv."
    ),
    data: Path = typer.Option(Path("data/raw"), help="Directory written by `gridfee collect`."),
    wind_tolerance: float = typer.Option(0.10, help="Allowed deviation of the wind north share."),
    flow_tolerance: float = typer.Option(
        0.10, help="Allowed deviation of the net export per neighbour."
    ),
    out: Path | None = typer.Option(
        None,
        help="Report file, not .csv (default: revalidation.md in results; the report of "
        "`gridfee backcast`, with its notes on placeholders, stays as it is).",
    ),
) -> None:
    """Check backcast results against SMARD data and write a Markdown report."""
    try:
        network = load_network(network_dir)
        flows = _read_berlin_hours(results / "flows_mw.csv")
        dispatch = _read_berlin_hours(results / "dispatch_mw.csv")
        report = _validate(
            network,
            flows,
            dispatch,
            data,
            year,
            wind_tolerance,
            flow_tolerance,
            out or results / "revalidation.md",
            [f"Written by `gridfee validate` from the results in {results}."],
        )
    except (NetworkConfigError, ValidationError, smard.SmardError, OSError, ValueError) as err:
        typer.secho(f"Error: {err}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from err
    _finish_validation(*report)


@app.command()
def backcast(
    year: int = typer.Option(..., help="Calendar year, collected with `gridfee collect`."),
    network_dir: Path = typer.Option(
        DE22, "--network", help="Directory with zones.csv and lines.csv."
    ),
    data: Path = typer.Option(Path("data/raw"), help="Directory written by `gridfee collect`."),
    population_shares: Path | None = typer.Option(
        None, help="CSV zone_id,share (share of the TSO zone's load). Default: placeholders."
    ),
    capacity_shares: Path | None = typer.Option(
        None, help="CSV technology,zone_id,share. Default: placeholders."
    ),
    capacity_factors: Path | None = typer.Option(
        None,
        help="CSV of hourly capacity factors per zone (scripts/build_capacity_factors.py). "
        "Default: every zone of a TSO zone has the TSO zone's hourly shape.",
    ),
    residual_shares: Path | None = typer.Option(
        None,
        help="CSV zone_id,share: each zone's share of Germany's generation other than wind "
        "and solar. Default: placeholder, the share of demand.",
    ),
    residual_cost: float = typer.Option(
        1.0,
        help="Marginal cost of generation other than wind and solar, EUR/MWh. The fee "
        "signal scales with it; the default of 1 gives a signal in units of this cost.",
    ),
    load_shedding_cost: float = typer.Option(
        1000.0, help="Cost of unserved load in EUR/MWh, which keeps every hour solvable."
    ),
    hours: Path | None = typer.Option(
        None, help="CSV whose first column lists the hours to solve (default: all)."
    ),
    threshold: float = typer.Option(
        DEFAULT_THRESHOLD, help="Signals of at most this size (EUR/MWh) are neutral."
    ),
    file_format: str = typer.Option("csv", "--format", help="Fee signal as csv or parquet."),
    wind_tolerance: float = typer.Option(0.10, help="Allowed deviation of the wind north share."),
    flow_tolerance: float = typer.Option(
        0.10, help="Allowed deviation of the net export per neighbour."
    ),
    out: Path | None = typer.Option(
        None, help="Output directory (default: results/backcast_YEAR)."
    ),
) -> None:
    """Historical backcast: injections, DC OPF, fee signal and validation in one go."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = out or Path("results") / f"backcast_{year}"
    try:
        network = load_network(network_dir)
        internal = [zone for zone in network.zones if zone.zone_id in network.internal_zone_ids]
        tso_load = smard.read_table(data, "load_tso", year)
        tso_res = {
            tech: smard.read_table(data, f"{tech}_tso", year) for tech in smard.TSO_RES_FILTERS
        }
        zonal = build_historical_injections(
            internal,
            tso_load,
            tso_res,
            None if population_shares is None else read_shares(population_shares),
            None if capacity_shares is None else read_shares(capacity_shares, "technology"),
            capacity_factors=None
            if capacity_factors is None
            else read_capacity_factors(capacity_factors),
        )
        inputs = build_backcast_inputs(
            network,
            zonal,
            smard.read_table(data, "generation_de", year),
            smard.read_table(data, "physical_flows_tso", year),
            residual_cost,
            None
            if residual_shares is None
            else pd.read_csv(residual_shares, comment="#", dtype={"zone_id": str}).set_index(
                "zone_id"
            )["share"],
        )
        subset = None
        if hours is not None:
            subset = _read_berlin_hours(hours).index
            unusable = subset.intersection(inputs.skipped_hours)
            if len(unusable):
                raise BackcastError(
                    f"{len(unusable)} of the requested hours lack cross-border or generation "
                    f"data, e.g. {unusable[0]}"
                )
        result = run_backcast(network, inputs, subset, load_shedding_cost, threshold)
        opf = result.opf
        _write_opf(opf, out)
        write_signal(result.signal, out, file_format)
        inputs.clipped_exchange.to_csv(out / "clipped_exchange.csv", index=False)
        pd.Series(inputs.skipped_hours, name="hour").to_csv(out / "skipped_hours.csv", index=False)

        placeholders = list(zonal.placeholder_quantities)
        if inputs.placeholder_residual_shares:
            placeholders.append("generation other than wind and solar")
        shed = opf.load_shedding_mw
        shed_hours = int((shed.sum(axis=1) > SHEDDING_TOLERANCE_MW).sum())
        notes = [
            f"{len(opf.flows_mw)} hours solved; {len(inputs.skipped_hours)} hours of the year "
            "left out because cross-border flows or national generation are missing.",
            "The line capacities and susceptances are an ASSUMPTION derived from PyPSA-Eur's "
            "OpenStreetMap-based network, not TSO data (docs/NETWORK.md).",
        ]
        if placeholders:
            notes.append(
                "PLACEHOLDER zone shares were used for: " + ", ".join(placeholders) + ". The "
                "results do not show where power is produced or used."
            )
        if capacity_factors is not None:
            notes.append(
                f"Wind and solar feed-in is split over the zones of each TSO zone hour by hour "
                f"with the capacity factors in {capacity_factors.name}, built from weather data "
                "with a generic power curve (an ASSUMPTION, docs/DATA.md)."
            )
        for row in inputs.clipped_exchange.itertuples():
            notes.append(
                f"{row.neighbour}: the observed flow is above the {row.line_capacity_mw:,.0f} MW "
                f"of line capacity in {row.hours} hours (up to {row.max_observed_mw:,.0f} MW) "
                f"and was cut to it ({row.clipped_mwh / 1e3:,.1f} GWh)."
            )
        if shed_hours:
            notes.append(
                f"Load was left unserved in {shed_hours} hours ({shed.sum().sum() / 1e3:,.1f} "
                "GWh). Shadow prices and the fee signal of those hours reflect the cost of "
                "unserved load."
            )
        report = _validate(
            network,
            opf.flows_mw,
            opf.dispatch_mw,
            data,
            year,
            wind_tolerance,
            flow_tolerance,
            out / "validation.md",
            notes,
        )
    except (
        NetworkConfigError,
        BackcastError,
        OPFError,
        SignalError,
        ValidationError,
        smard.SmardError,
        OSError,
        KeyError,
        ValueError,
    ) as err:
        typer.secho(f"Error: {err}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from err

    congested = opf.congested()
    boundary = set(network.boundary_zone_ids)
    inside = [
        line.line_id
        for line in network.lines
        if line.from_zone not in boundary and line.to_zone not in boundary
    ]
    labels = result.signal.classification
    typer.echo(
        f"Solved {len(opf.flows_mw)} hours on {len(network.zones)} zones and "
        f"{len(network.lines)} lines. Hours with congestion: "
        f"{int(congested[inside].any(axis=1).sum())} between German zones, "
        f"{int(congested.drop(columns=inside).any(axis=1).sum())} on lines to neighbours. "
        f"Hours with a fee signal other than neutral in any zone: "
        f"{int((labels != NEUTRAL).any(axis=1).sum())}. Results in {out}/"
    )
    for note in notes[1:]:
        typer.secho(note, fg=typer.colors.YELLOW, err=True)
    _finish_validation(*report)


def _write_opf(result: OPFResult, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    frames = {
        "flows_mw": result.flows_mw,
        "shadow_prices": result.shadow_prices,
        "nodal_prices": result.nodal_prices,
        "dispatch_mw": result.dispatch_mw,
        "ptdf": result.ptdf,
    }
    if result.load_shedding_mw is not None:
        frames["load_shedding_mw"] = result.load_shedding_mw
    else:
        # Do not leave the shortfall of an earlier run next to these results.
        (out / "load_shedding_mw.csv").unlink(missing_ok=True)
    for name, frame in frames.items():
        frame.to_csv(out / f"{name}.csv")


def _validate(
    network: ZoneNetwork,
    flows: pd.DataFrame,
    dispatch: pd.DataFrame,
    data: Path,
    year: int,
    wind_tolerance: float,
    flow_tolerance: float,
    report: Path,
    notes: list[str],
) -> tuple[pd.DataFrame, Path]:
    checks = validate_backcast(
        network,
        flows,
        dispatch,
        {
            tech: smard.read_table(data, f"{tech}_tso", year)
            for tech in ("wind_onshore", "wind_offshore")
        },
        smard.read_table(data, "physical_flows_tso", year),
        smard.read_table(data, "generation_de", year),
        wind_tolerance,
        flow_tolerance,
    )
    if report.suffix.lower() == ".csv":
        raise ValueError(f"{report}: the report is Markdown; its checks go to a .csv next to it")
    write_report(checks, report, f"Validation of the {year} backcast against SMARD", notes)
    checks.to_csv(report.with_suffix(".csv"), index=False)
    return checks, report


def _finish_validation(checks: pd.DataFrame, report: Path) -> None:
    failed = checks[checks["status"] == FAIL]
    judged = int((checks["status"] != "info").sum())
    typer.echo(f"Validation: {judged - len(failed)} of {judged} checks passed; report in {report}")
    if not failed.empty:
        typer.secho(
            "Error: validation failed for "
            + ", ".join(failed["check"] + " (" + failed["item"] + ")"),
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


def _read_berlin_hours(path: Path) -> pd.DataFrame:
    """Read a CSV whose first column holds hours with a UTC offset, as Europe/Berlin."""
    frame = pd.read_csv(path, index_col=0, dtype={0: str})
    without_offset = [hour for hour in frame.index if pd.Timestamp(hour).tzinfo is None]
    if without_offset:
        raise ValueError(
            f"{path}: hours need a UTC offset, e.g. 2023-01-01T00:00:00+01:00; "
            f"got {without_offset[0]!r}"
        )
    frame.index = pd.to_datetime(frame.index, utc=True).tz_convert(smard.TIMEZONE)
    return frame


def _read_hourly(path: Path) -> pd.DataFrame:
    """Read a CSV whose first column holds the snapshots, kept as written."""
    return pd.read_csv(path, index_col=0, dtype={0: str})
