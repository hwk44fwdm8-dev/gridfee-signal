# gridfee-signal

Open-source congestion-based grid fee signals for battery storage in Germany.

Germany is redesigning grid fees for battery storage (BNetzA's AgNeS process). Today a
battery pays the same fee whether it relieves or worsens grid congestion. This tool
computes an hourly congestion signal per zone from a DC optimal power flow on a zonal
network of Germany and turns it into a fee signal with a relief credit for storage that
charges during congestion. It is meant for historical backcasts and for forward-looking
scenarios driven by user-supplied inputs.

**Status: milestone 3 of 6.** The network loader, the DC OPF runner, the SMARD collector,
historical injections, the fee signal and the validation are in place, so a historical
backcast runs end to end. Scenarios and dispatch are not built yet (see
[Roadmap](#roadmap)).

**Read before using results:** the lines between the 22 zones are an assumption derived
from open data ([docs/NETWORK.md](docs/NETWORK.md)). The shares that place load and
generation in the zones are placeholders unless the files in `data/shares/` (Eurostat
population, MaStR capacity) are passed. With one cost for all generation other than
wind and solar, the 2023 backcast gives a neutral fee signal in more than 99.6 % of
hours. With one cost per fuel (`--generation-shares`, `--generation-costs`) it is not
neutral in 599 hours (6.8 %), but the dispatch behind it is far from the observed one
(gas 123 TWh instead of 51). With plants in efficiency classes, monthly gas prices and
each fuel's highest observed output of the day as its capacity and its lowest offered
at no cost (`--plant-efficiency`, `--fuel-prices`), and the observed cross-border flow
fixed per TSO zone and neighbour (`--borders-by-tso`), the fuel mix is within 7 % of
SMARD's per fuel and the signal is not neutral in 312 hours (3.6 %), each with a priced
line between German zones (into BW-Karlsruhe, out of Sachsen-Anhalt). That agreement
comes largely from the observed bounds, and the costs and line capacities remain
assumptions. 70 further hours have unserved load; their prices reflect the model's
penalty and not data, so they are kept out of the signal and listed apart
([docs/METHODOLOGY.md](docs/METHODOLOGY.md)).

**What is not in this repository.** Networks built from the line limits that the TSOs
publish through JAO's Core Static Grid Model, and every result computed on them, are
kept in a private repository ([gridfee-signal-private](https://github.com/hwk44fwdm8-dev/gridfee-signal-private)):
JAO's terms of use do not allow passing on its data or anything derived from it. Here,
`data/networks/de22` and `data/networks/de38` (the 38 NUTS2 regions) carry lines
estimated from OpenStreetMap-based data only. The scripts that read JAO's files
(`scripts/derive_de38_network.py`, `scripts/derive_desub_substations.py`) stay here as
code; they need JAO's workbook and map and the private repository's data files.

## Quick start

Install Git and Python 3.11 or newer, then open a terminal:

```bash
git clone https://github.com/hwk44fwdm8-dev/gridfee-signal.git
cd gridfee-signal
python3 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell, use `py -3 -m venv .venv` and
`.venv\Scripts\Activate.ps1` for the last two commands.
To explore the demo without solving a model, skip to the
[interactive results viewer](#interactive-results-viewer).

```bash
python -m pip install -e .

gridfee run \
  --network examples/networks/three_zone \
  --load examples/inputs/three_zone/load.csv \
  --generators examples/inputs/three_zone/generators.csv \
  --out results/three_zone
```

This solves three hours on a synthetic network with three internal zones and one
boundary zone. It writes `flows_mw.csv`,
`shadow_prices.csv`, `nodal_prices.csv`, `dispatch_mw.csv` and `ptdf.csv`. Add
`--hours hours.csv` to solve only a subset of hours (for example representative hours).

The same from Python:

```python
import pandas as pd
from gridfee.network import load_network
from gridfee.flows import run_dc_opf

network = load_network("examples/networks/three_zone")
load = pd.read_csv("examples/inputs/three_zone/load.csv", index_col=0)
generators = pd.read_csv("examples/inputs/three_zone/generators.csv", index_col=0)

result = run_dc_opf(network, load, generators)
result.shadow_prices  # hours x lines
result.ptdf  # lines x zones
```

## Architecture

```mermaid
flowchart LR
    cfg[zones.csv + lines.csv] --> network[network: loader]
    smard[SMARD data] --> data[data: collector]
    data --> inj[injections: historical]
    scen[scenario YAML + profiles] -.-> dispatch[dispatch]
    network --> flows[flows: DC OPF, PyPSA + HiGHS]
    inj --> flows
    dispatch -.-> flows
    flows --> out[shadow prices + PTDF]
    out --> signal[signal: fee per zone and hour]
    signal --> validation[validation against SMARD]
```

Solid arrows exist today; dashed ones are planned.

## Historical data and injections

```bash
gridfee collect --year 2023 --reference data/reference/annual_totals.csv
gridfee injections --zones data/networks/de22/zones.csv --year 2023
```

`collect` downloads hourly load, onshore wind, offshore wind and solar per TSO zone,
national load and generation by fuel, and physical cross-border flows per TSO zone and
neighbour, from [SMARD](https://www.smard.de) (public, no key)
into `data/raw/`. Downloads are cached in `data/cache/smard/`. Hours are Europe/Berlin.
Runs of up to 3 missing hours are interpolated and logged; longer gaps stop the run
(in cross-border series they stay missing and are reported).
With `--reference`, annual totals are compared with Bundesnetzagentur figures (±5 %).

`gridfee collect-units --year 2023` adds the hourly output per generation unit that
SMARD publishes (units of plants of 100 MW or more) and compares its sums per fuel with
the national series; see [docs/DATA.md](docs/DATA.md) for what the list covers. No
other command uses these tables yet.

`injections` splits each TSO zone's series over its zones: demand by population share,
wind and solar by capacity share (`--population-shares`, `--capacity-shares`). It writes
hourly demand, feed-in and net injection per zone, the shares used and an energy-balance
report per TSO zone.

Read [docs/DATA.md](docs/DATA.md) before using the output. In short:

- **Shares are placeholders unless given.** Without share files every zone of a TSO
  zone gets the same share, and the output flags this (`shares.csv`, column
  `placeholder`, and a warning). `data/shares/` holds shares built from Eurostat
  population and the MaStR register; where offshore wind comes ashore is an assumption.
- **Every zone of a TSO zone has the same hourly wind and solar shape** unless
  `--capacity-factors` is given. `data/weather/capacity_factors_2023.csv` is built from
  ERA5 with a generic power curve, which is an assumption.
- **Four zones' TSO differs from the drafts** and several zones are split between two
  TSOs; see [TSO_SOURCES.md](data/networks/de22/TSO_SOURCES.md).

## Backcast, fee signal and validation

```bash
gridfee collect --year 2023
gridfee backcast --year 2023          # writes results/backcast_2023/
```

`backcast` builds the injections, solves the DC OPF for every hour on
`data/networks/de22`, and writes flows, shadow prices, the fee signal
(`fee_signal.csv`, `fee_signal_class.csv`, `--format parquet` with
`pip install -e ".[parquet]"`) and `validation.md`. The full year takes about two
minutes. One problem for the year grows with the hours times the size of the network;
`--block-hours 24 --workers 8` solves it a day at a time in eight processes, which a
network of several hundred zones needs to fit in memory. `gridfee signal --results <dir>` computes the signal from the output of
`gridfee run`, and `gridfee validate --results <dir> --year 2023` repeats the checks (into
`revalidation.md`).

`gridfee backcast --method two-run` is a second method next to this single run: a market
run with Germany as one price zone, then a grid run that moves plants at least cost
until no line is overloaded. It writes the modelled redispatch per hour, generator and
zone (`redispatch_*.csv`) and the fee signal of the grid run; its assumptions are in
[docs/METHODOLOGY.md](docs/METHODOLOGY.md#two-run-method-market-run-then-grid-run-gridfee-backcast---method-two-run).

### Figures

```bash
pip install -e ".[plots]"
gridfee plot --results results/backcast_2023 --network data/networks/de38 \
    --regions NUTS_RG_01M_2021_4326_LEVL_2.geojson
```

`plot` reads a results directory and writes PNG files into its `plots/` folder:

| file | shows |
| --- | --- |
| `map_signal_hours.png` | hours with a positive and with a negative signal per zone |
| `map_mean_signal.png` | mean fee signal per zone |
| `map_lines.png` | hours in which each line is full and has a price; unserved load per zone |
| `timeseries_signal.png` | hours with a signal per day; mean signal per day and zone |
| `timeseries_lines.png` | hours per day in which each priced line is full |
| `timeseries_unserved.png` | unserved load per day and zone |

`--network` must be the network the results were computed on. `--regions` is optional:
a GeoJSON file with one outline per zone (for the 38 regions, the NUTS 2021 level-2
file of [Eurostat GISCO](https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units/territorial-units-statistics),
whose `NUTS_ID` is the zone id). Without it the zones are drawn as points. Hours with
unserved load are not in the fee signal, so the line figures leave them out as well.
The tables behind the figures are functions of `gridfee.plots` that need only pandas.
The figures inherit every assumption of the run they show.

A **positive** signal means that feeding in at the zone adds to congestion and taking
power out (charging a battery) relieves it; **negative** is the reverse. Method, sign
convention, what is observed and what is assumed: [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

## Interactive results viewer

Clone the repository and activate the Python environment as shown in
[Quick start](#quick-start), then run from the repository root:

```bash
python -m pip install -e ".[viewer]"
python -m streamlit run src/gridfee/viewer.py --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false
```

Open the local URL printed by Streamlit (normally <http://127.0.0.1:8501>). The
app starts with synthetic data so you can try it without downloading data or
running a model. The headless flag skips Streamlit's optional email prompt; open
the URL in your browser yourself.

To inspect a completed backcast or `run` + `signal`, stop the demo and start it
with your results directory and the **same network used in that run**:

```bash
python -m streamlit run src/gridfee/viewer.py --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false -- \
  --results results/backcast_2023 --network data/networks/de22
```

Filter dates (Europe/Berlin) and explore the daily fee signal, zone locations,
congested lines, and modeled unserved load. Select the same start and end date
to see hourly line loading, line prices and unserved load where available.
The viewer reads `fee_signal`,
`shadow_prices`, and `flows_mw` as CSV or Parquet, plus `load_shedding_mw`
when present. Timestamps must include a UTC offset (for example
`2023-01-01T00:00:00+01:00`). It does not rerun the model, write to the run directory, or send
your results to a hosted service. Restart it after replacing files in the run.

The **Site and asset** tab is a look-back on the selected dates, not a forecast:
what a constant load, generator or battery would have paid/earned under that
run's modelled fee signal. Positive fees mean the asset pays; negative fees mean
it earns (signal × net injection, feed-in positive). Missing-signal and unserved
hours are excluded. Battery operation is an idealised daily perfect-foresight
best case, with one charge-energy budget per day, round-trip losses, no
inter-day energy transfer and no chronological state-of-charge constraint.

## Network config

A network is a directory with two CSV files. Lines starting with `#` are comments.
See [docs/NETWORK.md](docs/NETWORK.md) for all rules and for deriving a zone network
from PyPSA-Eur.

`zones.csv`

| column | meaning |
| --- | --- |
| `zone_id` | unique id |
| `name` | display name |
| `kind` | `internal` (zone in Germany, default) or `boundary` (neighbouring country) |
| `tso_zone` | `50Hertz`, `Amprion`, `TenneT` or `TransnetBW`; required for internal zones, empty for boundary zones |
| `country` | country code; required for boundary zones |
| `lat`, `lon` | optional coordinates |

`lines.csv`

| column | meaning |
| --- | --- |
| `line_id` | unique id |
| `from_zone`, `to_zone` | the two zones; flows count positive from `from_zone` to `to_zone` |
| `susceptance_mw_per_rad` | MW of flow per radian of voltage angle difference |
| `capacity_mw` | flow limit in both directions |

## Sign conventions

- **Flow**: positive from `from_zone` to `to_zone`.
- **PTDF** (line l, zone n): change of the flow on l when 1 MW more is injected at n and
  withdrawn at the reference zone (default: the first zone; a distributed reference is
  supported).
- **Shadow price** of a line, in EUR/MWh per MW: the cost saved by one more MW of
  capacity. Positive when the line is congested from `from_zone` to `to_zone`, negative
  when congested the other way, 0 when not congested.
- **Nodal price**: `price[n] - price[m] = -sum over lines l of (PTDF[l,n] - PTDF[l,m]) * shadow_price[l]`.

- **Fee signal** (zone z): `sum over lines l of PTDF[l,z] * shadow_price[l]`, the nodal
  price at the PTDF reference minus the nodal price of z. Positive: feeding in adds to
  congestion, taking out relieves it.

## Limitations

- Zone-level precision only; nothing can be said about individual substations or lines.
- DC (linear) approximation: no losses, no reactive power, no voltage limits.
- The hours of an OPF run are independent of each other.

- The 22-zone lines are an assumption derived from OpenStreetMap-based data, and DC
  cables are treated as lines; see [docs/NETWORK.md](docs/NETWORK.md).
- The 38-region lines are derived the same way and have not been used for a backcast
  that is documented here.
- The backcast is not a market model, and its zone shares are placeholders unless the
  files in `data/shares/` are passed; see
  [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

`SCENARIOS.md` and `LIMITATIONS.md` follow with the modules they describe.

## Roadmap

| milestone | content | state |
| --- | --- | --- |
| M1 | scaffold, network loader, DC OPF runner, synthetic-network tests, CI | done |
| M2 | SMARD collector, historical injections, energy-balance checks | done |
| M3 | fee signal and validation; 2023 backcast end to end | done |
| M4 | scenario schema, profile import, dispatch, scenario runner | planned |
| M5 | sensitivity runs, scenario comparison, CLI polish, docs | planned |
| M6 | BESS impact module, dashboard (stretch) | planned |

## Licence

MIT, see [LICENSE](LICENSE). The derived network data is under the ODbL, see
[LICENSE-DATA.md](LICENSE-DATA.md).
