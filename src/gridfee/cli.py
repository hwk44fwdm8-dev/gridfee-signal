"""Command line interface. Further commands arrive with later milestones."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import typer

from gridfee.flows import OPFError, run_dc_opf
from gridfee.network import NetworkConfigError, load_network

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
    for name, frame in frames.items():
        frame.to_csv(out / f"{name}.csv")

    congested = result.congested()
    typer.echo(
        f"Solved {len(result.flows_mw)} hours on {len(network.zones)} zones and "
        f"{len(network.lines)} lines; {int(congested.any(axis=1).sum())} hours with congestion. "
        f"Results in {out}/"
    )


def _read_hourly(path: Path) -> pd.DataFrame:
    """Read a CSV whose first column holds the snapshots, kept as written."""
    return pd.read_csv(path, index_col=0, dtype={0: str})
