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
  With published limits on these four lines the signal shrinks to 21 hours and the
  unserved load disappears: see
  [2023 with published limits](#2023-with-published-limits-on-the-four-priced-lines).
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

**By plant location.** The plants named in the export are located on the substations
of the substation-level network (`data/redispatch/plants_2023.csv`, docs/DATA.md):
95 % of the year's current-driven redispatch energy has a substation and with it a
NUTS2 region and a reporting zone. `gridfee.validation.hourly_redispatch_by_location`
sums raised and lowered feed-in per hour at either level. The comparison of direction
above is still per TSO zone; it can be made per substation or reporting zone once a
backcast on the substation-level network exists.

## 2023 with published limits on the four priced lines

The runs above use the standard ratings of the OpenStreetMap data for every line. The
four lines between German zones that carried the fee signal now take their rating from
the maximum currents in JAO's Core Static Grid Model
([NETWORK.md](NETWORK.md#published-limits-for-four-lines-jao)); their capacities rise by
20 to 54 %. The same run as "bounds per day" above, on 22 November 2026:

| Hours in 2023 | Standard ratings | Published limits on four lines |
| --- | --- | --- |
| Hours in the fee signal | 8,686 | 8,756 |
| Fee signal not neutral | 312 (3.6 %) | 21 (0.2 %) |
| Hours with unserved load, kept out of the signal | 70 (22.6 GWh) | 0 |
| Hessen–BW-Karlsruhe priced | 101 | 6 |
| Rheinland-Pfalz–BW-Karlsruhe priced | 89 | 0 |
| Sachsen-Anhalt–Thüringen priced | 86 | 0 |
| Sachsen-Anhalt–Niedersachsen-Süd priced | 60 | 15 |

- **Most of the earlier signal came from the estimated limits.** With the published
  limits 21 hours remain, on two lines. The unserved load in BW-Karlsruhe is gone, so
  it was a result of the two lines into that zone being rated too low.
- **Where:** as before, negative in Baden-Württemberg (mean over the 21 hours
  -25 EUR/MWh in BW-Karlsruhe) and positive in the east (+25 EUR/MWh in
  Sachsen-Anhalt). Validation 13 of 13; the fuel mix is bounded by observation as
  before.
- **Against redispatch:** all 21 hours had redispatch (mean 9,367 MWh per hour against
  2,283 in the other hours; 18 of the 21 are in the tenth of the hours with the most
  redispatch). They hold 0.2 of the year's 20.1 TWh, 1 %. Direction: 50Hertz lowered
  and TransnetBW raised in all 21 hours, as the signal says; Amprion again has the
  wrong sign in the 6 hours with Hessen–BW-Karlsruhe priced.
- **What this means.** Real redispatch ran in 6,180 of 8,756 hours. A model with one
  line per pair of zones and realistic limits on those lines sees almost none of it:
  the congestion behind redispatch is mostly on single circuits and inside zones, with
  N-1 margins that the flat 0.7 does not describe. The 21 hours are a consistent but
  very small sample, and the limit of Hessen–BW-Karlsruhe is an upper bound
  (`DLRmax`).


## 2023 on the 38 NUTS2 regions with published limits on every line

The same run as "bounds per day", on `data/networks/de38`
([NETWORK.md](NETWORK.md#the-38-region-network-of-germany-datanetworksde38)): the OPF is
solved for the 38 NUTS2 regions, every AC line has the reactance and the maximum
current that the TSOs publish through JAO, and shares, weather factors and plant
classes are built per region (`data/shares/de38`, `data/weather/de38`). Run on
22 November 2026:

```bash
S=data/shares/de38
gridfee backcast --year 2023 --network data/networks/de38 \
    --population-shares $S/population_shares.csv --capacity-shares $S/capacity_shares.csv \
    --capacity-factors data/weather/de38/capacity_factors_2023.csv \
    --generation-shares $S/generation_shares.csv \
    --generation-costs data/costs/generation_costs_2023.csv \
    --plant-efficiency $S/plant_efficiency.csv --fuel-prices data/costs/fuel_prices_2023.csv \
    --plant-window day --borders-by-tso \
    --report-zones data/networks/de22/zone_nuts2.csv --report-weights $S/capacity_by_zone.csv
```

| Hours in 2023 | 22 zones, published limits on four lines | 38 regions, published limits on every line |
| --- | --- | --- |
| Hours in the fee signal | 8,756 | 8,370 |
| Fee signal not neutral | 21 (0.2 %) | 1,007 (12.0 %) |
| Lines between German zones with a price | 2 | 10 |
| Hours with unserved load, kept out of the signal | 0 | 386 (125.1 GWh) |
| Signal hours with redispatch | 21 of 21 | 906 of 1,007 |
| Redispatch per hour, signal hours against other hours | 9,367 against 2,283 MWh | 5,179 against 1,640 MWh |
| Share of the redispatch volume in signal hours | 1 % | 30 % (5.2 of 17.3 TWh) |

- **The signal now covers a real part of the congestion.** 1,007 hours are not
  neutral, 581 of them in the fifth of the hours with the most redispatch, and they
  hold 30 % of the redispatch volume of the hours solved.
- **Three weak lines carry most of it.** Bremen–Lüneburg is priced in 393 hours (one
  380 kV and one 220 kV circuit, 1,274 MW), Oberpfalz–Mittelfranken in 313 (one 220 kV
  circuit, 292 MW) and Niederbayern–Mittelfranken in 213 (one 220 kV circuit, 294 MW).
  The other seven lines together are priced in 72 hours. Whether these three are the
  real bottlenecks is not shown: redispatch is not published per line, and a single
  220 kV circuit next to 380 kV lines is where the DC model overstates congestion
  ([NETWORK.md](NETWORK.md#the-38-region-network-of-germany-datanetworksde38)). In the
  313 hours of Oberpfalz–Mittelfranken redispatch was not above average (2,135 MWh
  per hour), which speaks against that line; in the hours of the other two it was
  far above (7,395 and 5,612).
- **Direction per TSO zone** (hours with the same direction of signal and redispatch,
  of the hours compared): Amprion 681 of 797, TransnetBW 620 of 820, 50Hertz 504 of
  818, TenneT 463 of 872. Amprion had the wrong sign on the 22 zones. TenneT reaches
  from the North Sea to Austria and cannot be judged as one area.
- **Unserved load in 386 hours**, 279 of them at the Polish border and 154 in
  Niederbayern (an hour can have both). To Poland the published limit of
  Vierraden–Krajnik (1,500 A, PSE's side) gives 1,382 MW, and the model cannot steer
  the export between that line and Hagenwerder–Mikulowa as the phase shifters there
  do. In Niederbayern the 294 MW line to Mittelfranken is full while the region
  imports. Both are limits of the model, not findings; the hours are kept out of the
  signal as before.
- **Validation** 13 of 13. Flows to four neighbours were cut to the line capacity
  (see NETWORK.md), most to the Netherlands from TenneT (62 GWh).

### The signal for the 22 zones (`fee_signal_report*`)

`--report-zones` writes, besides the signal per region, the signal for the 22 zones:
each zone's signal is the mean of its regions' signals, weighted by population
(`--report-weights`). A battery's region inside a zone is not known to this file, and
population stands in for where connections are; that is an assumption.

The mean hides what happens inside a zone. In the 1,007 signal hours the regions of
one zone have opposite signs in 816 hours in Bayern-Nord (Oberpfalz, Oberfranken,
Mittelfranken, Unterfranken; mean spread 72 EUR/MWh), in 503 in NRW-Nord/Ost and in
391 in Niedersachsen-Nord (Lüneburg and Weser-Ems, on either side of Bremen). For
these zones the 22-zone value says little about a single site; `fee_signal.csv` has
the regions. Mean signal over the signal hours: most positive in Hamburg/Schleswig-
Holstein (+34 EUR/MWh), Mecklenburg-Vorpommern (+26) and Brandenburg (+24), most
negative in Bremen (-45), Bayern-Mitte (-21) and Bayern-Süd/West (-16).
