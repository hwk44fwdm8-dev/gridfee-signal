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
| Demand per zone | SMARD load per TSO zone x population share | Eurostat population with `--population-shares data/shares/population_shares.csv`, else placeholders |
| Wind and solar per zone, cost 0, can be curtailed | SMARD per TSO zone x capacity share | MaStR capacity with `--capacity-shares data/shares/capacity_shares.csv`, else placeholders |
| Exchange with each neighbour | SMARD physical net export, summed over TSO zones: a load at the boundary zone when exporting, a free feed-in that can be curtailed when importing | observed; cut to the line capacity where it exceeds it |
| All other generation | one "residual" generator per zone, same marginal cost everywhere; capacity = zone share x the year's highest national generation other than wind and solar (SMARD) | MaStR capacity with `--residual-shares data/shares/residual_shares.csv`, else a placeholder (share of demand) |
| ... or, with `--generation-shares`: one generator per fuel and zone | SMARD national generation per fuel x the zone's share of the fuel's capacity. A fuel in `--generation-costs` is dispatched at its cost up to zone share x the year's highest national output of the fuel; the others feed in their observed output at cost 0 | MaStR capacity (`data/shares/generation_shares.csv`); costs are an assumption (`data/costs/generation_costs_2023.csv`, [DATA.md](DATA.md)) |
| Lines | `data/networks/de22/lines.csv` | assumption, see [NETWORK.md](NETWORK.md) |

Consequences:

- **This is not a market model.** Conventional plants, storage and prices are not
  represented; that is the dispatch module of milestone 4. The backcast shows the
  congestion that wind, solar and cross-border flows cause on the assumed lines.
- **The signal is in units of the residual cost.** With the default `--residual-cost 1`
  a signal of 1 means "as much as one MWh of other generation costs". With
  `--generation-costs` the signal is in EUR/MWh of the assumed costs.
- **`--load-shedding-cost`** (default 1000) keeps every hour solvable. Hours with
  unserved load are reported; their prices reflect that penalty.
- The fee signal covers the German zones only.
- Hours without complete cross-border or generation data are left out and listed.

### Result for 2023 with placeholder shares

Run on 21 November 2026 with SMARD data for 2023, placeholder shares everywhere and the
derived lines: 8,756 of 8,760 hours solved, **no line between German zones is ever
congested, and the fee signal is neutral in every zone and hour.** Only the lines to
Sweden, Denmark East, Norway and Belgium are full, which follows from cutting observed
flows to their capacity. Load is left unserved in 20 hours (19.8 GWh).

This does not mean that there was no congestion in 2023. With placeholder shares the
wind of a TSO zone is spread evenly over its zones (TenneT's from the coast to
Bavaria) and other generation sits where the load is, so little power has to cross
the country; and the line capacities are generous.

### Result for 2023 with shares from public data

Run on 22 November 2026 with the share files in `data/shares/`
([DATA.md](DATA.md#zone-shares-from-public-data)), the same SMARD data and the same
lines. 8,756 of 8,760 hours solved in each run; validation 13 of 13.

| | Placeholder shares | Population, wind and solar shares | Plus other generation by capacity |
| --- | --- | --- | --- |
| Hours with a congested line between German zones | 0 | 0 | 5 |
| Hours with a fee signal other than neutral | 0 | 5 | 30 |
| ... of which with unserved load | 0 | 4 | 28 |
| Hours with unserved load (energy) | 20 (19.8 GWh) | 24 (20.2 GWh) | 48 (23.8 GWh) |

**The signal stays neutral in more than 99.6 % of hours.** Where it is not:

- **Between German zones** only the line Sachsen-Anhalt–Niedersachsen-Süd (DE05-DE09,
  2,377 MW) is ever full, and only with other generation placed by capacity: 16 January
  21:00–23:00 and 21 January 21:00–24:00.
- **The other hours** come from lines to neighbours that are full while the neighbour
  has a second line into Germany, so that power has a way round: Saarland–France
  (DE18-FR, 2,784 MW) in all 30 hours, Niedersachsen-Nord–Netherlands (DE08-NL) in 6.
  The hours lie on 16, 19, 21 and 22 January, 8, 9 and 28 February and 4 to 6 March.
- **Where:** in every one of those hours all 22 zones have a signal. Feeding in adds
  to congestion (positive) in the north and west, most in the Saarland, and relieves
  it (negative) in Bayern and Baden-Württemberg, most in BW-Freiburg. Sachsen and
  Thüringen change sign between hours.
- **Size:** 28 of the 30 hours have unserved load, so their signal reflects the
  `--load-shedding-cost` of 1000, not a cost from data.

So real shares do not make the 2023 backcast show congestion between north and south.
What is left as the cause: the assumed line capacities (all circuits between two zones
added up, at 70 %, with nothing inside a zone), and that the backcast is not a market
model, so other generation runs wherever it relieves the lines at no extra cost.

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

The check against published redispatch is a command of its own, see
[Check against published redispatch](#check-against-published-redispatch-gridfee-redispatch).

### Result for 2023 with hourly wind and solar per zone from weather

Run on 22 November 2026 as the last column above, plus `--capacity-factors
data/weather/capacity_factors_2023.csv` ([DATA.md](DATA.md#hourly-capacity-factors-from-era5)):
onshore wind and solar of each TSO zone are split over its zones hour by hour by
capacity times the zone's weather, instead of by capacity alone. 8,756 hours solved,
validation 13 of 13, load unserved in 46 hours (24.5 GWh).

**The signal barely changes: 34 hours are not neutral (30 before), and a line between
German zones has a price in the same 5 hours.**

The reason is not that the lines are never full. "Congested" in the tables above
means that a line has a shadow price. Counting instead the hours in which the flow
sits at the line's capacity:

| | Placeholder shares | Shares from public data | Plus weather |
| --- | --- | --- | --- |
| Hours with a line between German zones at its capacity | 1,430 | 1,098 | 1,204 |
| ... of which the line has a shadow price | 0 | 5 | 5 |
| Sachsen-Anhalt–Niedersachsen-Süd (DE05-DE09) at capacity | 383 | 946 | 1,035 |
| Rheinland-Pfalz–BW-Karlsruhe (DE17-DE20) at capacity | 58 | 106 | 117 |
| Hessen–BW-Karlsruhe (DE16-DE20) at capacity | 11 | 57 | 63 |

A line at its capacity has no price when the power can be produced on its other side
at the same cost. In this backcast all generation other than wind and solar has one
cost and enough capacity in every zone, so moving it across a full line is free, and
the fee signal, which is built from shadow prices, stays neutral. The same makes the
solver indifferent between many dispatches, so a flow at capacity in a single hour
is not proof that the line was needed. **What keeps the signal neutral is therefore
the single cost of other generation, not the zone shares or the weather.** A signal
needs costs that differ between plants or zones (fuel-specific costs, or observed
output of single plants), which is dispatch modelling and not part of this step.

### Result for 2023 with one cost per fuel

Same inputs as above, with `--generation-shares data/shares/generation_shares.csv
--generation-costs data/costs/generation_costs_2023.csv` instead of
`--residual-shares`. Nuclear, lignite, hard coal and gas are dispatched at 14, 116,
156 and 107 EUR/MWh; biomass, hydro, pumped storage and the two "other" series feed
in their observed national output. 8,756 hours solved, 13 of 13 checks pass.

| Hours in 2023 | One cost | One cost per fuel |
| --- | --- | --- |
| Fee signal not neutral | 34 | 599 (6.8 %) |
| ... of which with unserved load | 26 | 23 |
| A line between German zones at its capacity | about 1,200 | 139 |
| ... of which the line has a price | 5 | 54 |

- **Lines between German zones with a price:** Sachsen-Anhalt–Niedersachsen-Süd in 36
  hours, Hessen–BW-Karlsruhe in 18, Sachsen-Anhalt–Thüringen in 2,
  Rheinland-Pfalz–BW-Karlsruhe in 1.
- **Most non-neutral hours come from lines to neighbours:** Saarland–France has a price
  in 337 hours and BW-Freiburg–France in 240. The observed exchange is fixed, so a full
  border line now makes generation on one side dearer than on the other. (The cables to
  Denmark East, Norway and Sweden are full in thousands of hours but are a neighbour's
  only connection, which gives no signal inside Germany.)
- **Where:** positive (feeding in adds to congestion) in the north and west in about
  475 of the 599 hours, most in the Saarland; negative in Bayern and Baden-Württemberg
  in about 480, most in BW-Freiburg. Over the non-neutral hours without unserved load
  the mean is +36 EUR/MWh in the Saarland and -49 EUR/MWh in BW-Freiburg.
- **When:** 289 of the hours are in December and January, 61 from May to August.

**The dispatch behind this is far from the observed one.**

| TWh in 2023 | SMARD | Model |
| --- | --- | --- |
| Gas | 51.1 | 122.5 | 48.7 (-5 %) | 49.8 (-3 %) |
| Lignite | 77.8 | 36.0 | 88.4 (+14 %) | 82.0 (+5 %) |
| Hard coal | 39.7 | 5.3 | 31.2 (-21 %) | 36.8 (-7 %) |
| Nuclear | 6.7 | 7.8 | 7.1 (+5 %) | 6.8 (+1 %) |
| All other than wind and solar | 255.4 | 251.6 | 255.4 | 255.4 |
| Hours with unserved load | 0 | 23 | 5 (1.3 GWh) | 70 (22.6 GWh) |

The last row does not include the supply of step 4 (5.2 TWh with bounds per day).

**How much of this agreement is the model's:** little. Steps 1 and 2 alone, as a
merit order of the four fuels on SMARD's national series with the year's highest output
as capacity and no network, give lignite 100.6 TWh, hard coal 16.8 and gas 50.3. The
bounds of step 3 do most of the rest, and the tighter they are, the closer the mix is by
construction. What the costs still
decide is which plants, and so which zones, cover the range between a day's lowest and
highest output. The remaining error has one direction: the model runs lignite before
hard coal whenever it can.

| Hours in 2023 | One cost per fuel | Bounds per week | Bounds per day |
| --- | --- | --- | --- |
| Hours in the fee signal | 8,756 | 8,751 | 8,686 |
| Fee signal not neutral | 599 | 546 | 312 (3.6 %) |
| A line between German zones has a price | 54 | 546 | 312 |
| Hours with unserved load, kept out of the signal | 23 (not kept out) | 5 | 70 |

For the run with bounds per day:

- **Every non-neutral hour now has a priced line between German zones.** With the
  observed flow fixed on each border, a full line to a neighbour no longer moves the
  signal inside Germany.
- **Lines with a price:** Hessen–BW-Karlsruhe in 101 hours, Rheinland-Pfalz–BW-Karlsruhe
  in 89, Sachsen-Anhalt–Thüringen in 86, Sachsen-Anhalt–Niedersachsen-Süd in 60.
- **Where:** negative in Baden-Württemberg (mean over the 312 hours -44 EUR/MWh in
  BW-Karlsruhe), positive in the east (+29 EUR/MWh in Sachsen-Anhalt).

**Limits of this result**

- **Unserved load in 70 hours (22.6 GWh), 65 of them in BW-Karlsruhe** on 14 winter
  days, with the lines from Hessen and Rheinland-Pfalz full. Each fuel is held to its
  national observed range and placed by capacity share, so plants in the south cannot
  run above the national level of the day. In reality grid operators then start
  plants there (redispatch, grid reserve); the model has no such plants. These hours
  are kept out of the signal. With bounds per week it is 5 hours (1.3 GWh).
- **The line capacities are assumptions**, and the signal now rests on four of them.
  A run with published limits on them is not public (see
  [Runs on networks with published limits](#runs-on-networks-with-published-limits)).
- **The costs are assumptions** (generic lignite price and other variable costs, one
  CO2 price and one hard coal price for the year, import price of gas with an unknown
  heating value basis, a plant list of 2020). No sensitivity run was made.
- **Not a market model.** There is still no heat demand, start-up cost or reserve; the
  observed bounds replace them.

## Check against published redispatch (`gridfee redispatch`)

`gridfee redispatch --results results/backcast_2023 --redispatch <export.csv>` holds the
fee signal against the redispatch measures the four TSOs publish on netztransparenz.de
(docs/DATA.md) and writes `redispatch.md` into the results. Only current-driven
measures (`Strombedingter Redispatch`) count; voltage-driven ones and countertrading
are left out. A measure's energy is spread evenly over its interval.

The publication names the plant and the TSO that instructed it, not the plant's zone.
The direction is therefore compared per TSO control area: where the mean signal of a
TSO's zones is positive (feed-in adds to congestion), redispatch should lower feed-in,
and where it is negative, raise it. Nothing is said about the direction inside a
control area; TenneT's reaches from the North Sea to Bavaria.

### Result for 2023 (run of the previous section, 312 signal hours)

- **Timing fits.** All 312 signal hours had real redispatch (69 % of the other hours
  had), with 7,432 MWh per hour on average against 2,055 MWh in the other hours. 160 of
  the 312 lie in the tenth of the hours with the most redispatch, 225 in the top fifth,
  none in the three tenths with (almost) none.
- **The signal misses most of the congestion.** The 312 hours hold 2.3 of the year's
  19.5 TWh of current-driven redispatch (12 %). Redispatch took place in 6,110 of
  8,686 hours; the model prices a line between German zones in 312. Of the 312 hours
  with the most redispatch, 93 have a signal.
- **Direction fits at both ends, not in between.**

| priced line | hours | 50Hertz | Amprion | TenneT | TransnetBW |
| --- | --- | --- | --- | --- | --- |
| Sachsen-Anhalt–Thüringen | 86 | +21 / −2,171 (86 of 86) | −1 / +2,642 (74 of 86) | −3 / −3,458 (0 of 86) | −25 / +2,044 (86 of 86) |
| Sachsen-Anhalt–Niedersachsen-Süd | 60 | +29 / −2,606 (59 of 59) | −10 / +1,505 (53 of 53) | −9 / 0 (27 of 58) | −0.2 / +921 (57 of 57) |
| Hessen–BW-Karlsruhe | 101 | +10 / −951 (97 of 97) | +15 / +1,901 (7 of 98) | −0.5 / −2,564 (80 of 101) | −62 / +1,606 (101 of 101) |
| Rheinland-Pfalz–BW-Karlsruhe | 89 | +3 / −334 (62 of 63) | +24 / +703 (6 of 71) | −3 / −1,359 (0 of 89) | −59 / +868 (78 of 78) |

  Each cell: mean signal in EUR/MWh / mean net redispatch in MWh per hour (raised minus
  lowered), and in brackets the hours in which both point the same way, of the hours
  in which both are non-zero. In 50Hertz (signal positive, feed-in lowered) and
  TransnetBW (signal negative, feed-in raised) the direction agrees in all but one
  hour. In Amprion it does not when a line into BW-Karlsruhe is priced: the signal is
  positive in Rheinland-Pfalz and NRW, while the TSOs raised feed-in there. In TenneT
  the mean signal is close to zero and redispatch lowers feed-in on balance, which is
  what one expects from wind in the north of its area, and cannot be checked per zone.

What this says about the four lines: the model finds congestion in hours in which the
real grid was congested, and its north-east to south-west direction is the real one,
but the real bottlenecks bind far more often than these four lines do, and the model
puts the western bottleneck further south than the redispatch pattern suggests
(feed-in was raised in Amprion's area, which points to a real bottleneck north of it; an
inference, not a published fact). The
check cannot confirm the four lines themselves: redispatch is not published per line.


## Runs on networks with published limits

The runs above use the standard ratings of the OpenStreetMap data for every line.
Runs with the limits that the TSOs publish through JAO's Core Static Grid Model (the
22 zones with published limits on four lines, the 38 NUTS2 regions and the substation
network with published limits on every line) and their results are kept in the private
repository [gridfee-signal-private](https://github.com/hwk44fwdm8-dev/gridfee-signal-private), because
JAO's terms of use do not allow passing on anything derived from its data.

### The signal for the 22 zones (`fee_signal_report*`)

On a finer network, `--report-zones` writes, besides the signal per region, the signal
for the 22 zones: each zone's signal is the mean of its regions' signals, weighted by
population (`--report-weights`). A battery's region inside a zone is not known to this
file, and population stands in for where connections are; that is an assumption. The
mean hides what happens inside a zone: regions of one zone can have opposite signs in
the same hour, and `fee_signal.csv` has the regions.

## Two-run method: market run, then grid run (`gridfee backcast --method two-run`)

The single run above dispatches every plant with the line limits in one optimisation.
`--method two-run` is a second way to get the fee signal from the same inputs, closer
to how the German market and the TSOs work, and it gives a modelled redispatch volume.
The single run is the default and is not changed by it.

1. **Market run.** Germany is one price zone: all demand, all generators and the
   observed exchange with the neighbours are put on one bus, without any line. Plants
   are dispatched by cost alone. The price of the hour is the cost of the last plant
   needed (`market_price.csv`, `market_dispatch_mw.csv`).
2. **Grid run.** The network with its line limits is solved again. Every generator
   may be raised from its market dispatch up to its available capacity, or lowered
   down to zero; the sum of what these movements cost is minimised. Without an
   overload nothing moves. The flows, the dispatch, the line shadow prices and the
   fee signal written are those of this run; the signal is computed from the shadow
   prices and the PTDF exactly as in the single run.

Modelled redispatch is the grid run's dispatch minus the market run's, in MW per hour,
which with hourly data is MWh: `redispatch_mw.csv` per generator (fuel or plant class
and zone; negative = lowered), `redispatch_raised_by_zone_mw.csv` and
`redispatch_lowered_by_zone_mw.csv` per zone of the network, and
`redispatch_hourly.csv` with the MWh raised, the MWh lowered and their cost per hour.

The method adds these **assumptions**:

- **What a plant is paid to move** (`--redispatch-markup`, default 1 EUR/MWh): a
  plant that is raised is paid its marginal cost of the backcast plus the markup per
  MWh; a plant that is lowered returns the marginal cost it saves less the markup.
  Plants are thus moved at cost, as the cost-based German redispatch intends, and the
  markup stands for the cost of changing a schedule. It is not taken from a source.
  It keeps the grid run from moving plants without an overload. No start-up costs,
  minimum loads, ramping or lead times: every plant can move in every hour.
- **What curtailed wind and solar cost** (`--curtailment-cost`, default: the market
  run's price of the hour, not below zero): lowering feed-in that has no marginal
  cost costs this per MWh plus the markup. That is wind and solar, and also the
  feed-in that follows its observed output, the lowest output of a plant class that
  runs at no cost, and imports. The default is the market revenue the curtailed plant
  loses; the feed-in premium that a plant under the EEG is compensated for as well is
  not included. With a higher value conventional plants are lowered before wind and
  solar, as the law's minimum factors intend.
- **The market run sees no grid at all**, also not the lines to the neighbours: the
  observed exchange is given to it. The grid run may cut an import or leave an export
  unserved (at `--load-shedding-cost`) like the single run.
- Where several generators have the same cost the market run's choice among them is
  arbitrary, and so is the redispatch between them. With one residual cost for all
  zones (no `--generation-costs`) the volume therefore says little.

In a congested hour the two runs end at the same least-cost dispatch if every plant
has a cost, and the line prices of the grid run are those of the single run plus two
markups. They differ where feed-in at no cost is curtailed: the single run curtails it
for free, the grid run at the curtailment cost. Hours in which either run leaves load
unserved are not part of the signal.

What is not modelled and matters for a comparison with the published volume:
redispatch for voltage, countertrading, measures abroad, the grid reserve, and that
TSOs plan redispatch on forecasts with a security margin (n-1 is only in the line
margins of the network).

**2023 on the 38-region network**: a comparison of the modelled redispatch with the
volume the TSOs published for 2023 exists, but its figures are not published here yet.
