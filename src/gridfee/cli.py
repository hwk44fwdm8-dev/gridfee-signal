"""Command line interface. Further commands arrive with later milestones."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import typer

from gridfee.data import smard
from gridfee.flows import OPFError, run_dc_opf
from gridfee.injections import build_historical_injections, check_energy_balance, read_shares
from gridfee.network import NetworkConfigError, load_network, load_zones

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


def _read_hourly(path: Path) -> pd.DataFrame:
    """Read a CSV whose first column holds the snapshots, kept as written."""
    return pd.read_csv(path, index_col=0, dtype={0: str})
