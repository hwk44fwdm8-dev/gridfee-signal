# Methodology

Status: milestone 3. This covers the fee signal, the historical backcast and its
validation. Scenarios and dispatch follow in later milestones.

## Fee signal

For every hour h and zone z:

    signal[h, z] = sum over lines l of PTDF[l, z] * shadow_price[h, l]      (EUR/MWh)

`PTDF[l, z]` is the flow that 1 MW fed in at z puts on line l (positive from the line's
`from_zone` to its `to_zone`); `shadow_price[h, l]` is positive when l is congested in
that direction and negative when congested the other way.

**Sign convention**

| Signal | Feeding in at z (generation, battery discharging) | Taking out at z (load, battery charging) |
| --- | --- | --- |
| positive | adds to congestion | relieves congestion |
| negative | relieves congestion | adds to congestion |
| neutral | no effect | no effect |

The value is the system cost caused by one more MWh fed in at z in that hour, and saved
by one more MWh taken out. A battery that charges in a positive hour or discharges in a
negative hour relieves the grid. How this turns into a fee or a relief credit is not
decided here.

The signal equals the nodal price at the PTDF's reference minus the nodal price of z.
Its level depends on that reference; the difference between two zones does not. The
backcast uses a reference spread over the German zones in proportion to their demand,
so a zone's signal is measured against the demand-weighted average of Germany.

**Classification:** `positive` if the signal is above the threshold, `negative` if below
minus the threshold, else `neutral`. The threshold is in EUR/MWh (`--threshold`); the
default of 0.000001 only removes solver noise.

Outputs: `fee_signal` and `fee_signal_class` (hours x zones, CSV or Parquet) and
`fee_signal_summary.csv` (hours per class and mean signal per zone).

## Historical backcast (`gridfee backcast`)

| Input of the OPF | Source | Status |
| --- | --- | --- |
| Demand per zone | SMARD load per TSO zone x population share | shares are placeholders |
| Wind and solar per zone, cost 0, can be curtailed | SMARD per TSO zone x capacity share | shares are placeholders |
| Exchange with each neighbour | SMARD physical net export, summed over TSO zones: a load at the boundary zone when exporting, a free feed-in that can be curtailed when importing | observed; cut to the line capacity where it exceeds it |
| All other generation | one "residual" generator per zone, same marginal cost everywhere; capacity = zone share x the year's highest national generation other than wind and solar (SMARD) | location is a placeholder (share of demand) unless `--residual-shares` is given |
| Lines | `data/networks/de22/lines.csv` | assumption, see [NETWORK.md](NETWORK.md) |

Consequences:

- **This is not a market model.** Conventional plants, storage and prices are not
  represented; that is the dispatch module of milestone 4. The backcast shows the
  congestion that wind, solar and cross-border flows cause on the assumed lines.
- **The signal is in units of the residual cost.** With the default `--residual-cost 1`
  a signal of 1 means "as much as one MWh of other generation costs". No cost is taken
  from data.
- **`--load-shedding-cost`** (default 1000) keeps every hour solvable. Hours with
  unserved load are reported; their prices reflect that penalty.
- The fee signal covers the German zones only.
- Hours without complete cross-border or generation data are left out and listed.

### Result for 2023 with what ships today

Run on 21 November 2026 with SMARD data for 2023, placeholder shares everywhere and the
derived lines: 8,756 of 8,760 hours solved, **no line between German zones is ever
congested, and the fee signal is neutral in every zone and hour.** Only the lines to
Sweden, Denmark East, Norway and Belgium are full, which follows from cutting observed
flows to their capacity. Load is left unserved in 20 hours (19.8 GWh).

This does not mean that there was no congestion in 2023. With placeholder shares the
wind of a TSO zone is spread evenly over its zones (TenneT's from the coast to
Bavaria) and other generation sits where the load is, so little power has to cross
the country; and the line capacities are generous. Real zone shares are needed before
the signal says anything.

## Validation (`gridfee validate`, also run by `backcast`)

| Check | Verdict | What it compares |
| --- | --- | --- |
| Wind north share | pass within 10 % (relative) | Share of wind energy fed in in the 50Hertz and TenneT control areas: model dispatch vs SMARD. Equal by construction before curtailment, so it fails only if the model curtails wind far more in one part than happened. |
| Net export per neighbour | pass within 10 % of the observed gross exchange | Model line flows vs SMARD physical flows. The observed flows are an input, so a failure points at line capacities that are too small. The 10 % is this project's choice; the specification names none. |
| Net export per TSO zone and neighbour | none (info) | How the model splits a neighbour's flow over the control areas. |
| Other generation, wind and solar fed in | none (info) | National energy, model vs SMARD. |

The report is a Markdown file with a pass/fail table, the comparisons without a verdict
and notes on placeholders, cut flows and unserved load; the same rows are written as CSV.

For 2023 all 13 checks with a verdict pass (largest deviation: Denmark East, 5.6 %). The
per-TSO comparison shows that flows inside the model differ from reality, e.g. to
Czechia: observed +5.1 TWh from 50Hertz and -6.2 TWh into TenneT, modelled -6.4 and
+5.4 TWh.

Not done: the directional check against BNetzA redispatch data (a stretch goal).
