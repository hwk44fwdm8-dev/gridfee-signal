# Data and historical injections

## SMARD collector (`gridfee.data.smard`)

Source: SMARD, the market data platform of the Bundesnetzagentur, through its public
chart data API (no key). Licence of the data: CC BY 4.0, "Bundesnetzagentur | SMARD.de".

Load and generation filter ids and the region codes are the ones in the
[bundesAPI/smard-api](https://github.com/bundesAPI/smard-api) documentation. That
documentation lists no cross-border series, so their ids come from the configuration
file SMARD's own chart application reads,
[market_data_configuration.json](https://www.smard.de/app/chart_configuration/market_data_configuration.json),
which names every series and the regions it exists for. The client refuses any id that
is in neither source.

| series | filter | regions |
| --- | --- | --- |
| load (Netzlast) | 410 | DE, 50Hertz, Amprion, TenneT, TransnetBW |
| wind onshore | 4067 | DE and the four TSO zones |
| wind offshore | 1225 | DE, 50Hertz, TenneT (SMARD has no series for the other two) |
| solar | 4068 | DE and the four TSO zones |
| lignite, nuclear, hydro, other conventional, other renewable | 1223, 1224, 1226, 1227, 1228 | DE |
| biomass, hard coal, pumped storage, gas | 4066, 4069, 4070, 4071 | DE |

Values are the energy of each hour in MWh, equal to the mean power in MW.

**Cross-border flows.** `smard_physical_flows_tso_<year>.csv` holds the physical net
export (MW, positive = out of Germany) over each border of each TSO zone, one column
`<TSO zone>_<neighbour>`. It is SMARD's "Physikalischer Stromfluss" export series plus
its import series (imports are negative in SMARD).

| TSO zone | neighbour: export id / import id |
| --- | --- |
| 50Hertz | DK2: 4752 / 4894, PL: 4753 / 4895, CZ: 4754 / 4896 |
| Amprion | FR: 4755 / 4897, LU: 4756 / 4898, NL: 4757 / 4899, AT: 4759 / 4900, CH: 4760 / 4901, BE: 4968 / 4970 |
| TenneT | DK1: 4761 / 4902, NL: 4762 / 4903, AT: 4763 / 4904, SE4: 4764 / 4905, CZ: 4765 / 4906, NO2: 4964 / 4966 |
| TransnetBW | FR: 4766 / 4907, AT: 4767 / 4908, CH: 4768 / 4909 |

`smard_net_export_de_<year>.csv` holds Germany's physical (4963) and commercial (4629)
net export. The owner's draft collector used ids 4075–4083 for cross-border flows; these
are in neither source and most return errors, so they are not used.

No published annual figure for physical flows was found, so they are not checked against
one. In the 2023 data the annual sums over the borders of each TSO zone match that
zone's own net export series (4963) to within 0.02 TWh.

**Time.** All tables use an hourly Europe/Berlin index; the day of the change to summer
time has 23 hours, the day of the change back 25.

**Gaps.** Runs of up to `--max-gap-hours` (default 3) missing hours inside a series are
filled by linear interpolation; each fill is logged and counted in
`smard_gaps_<year>.csv`. A longer gap, or one at the start or end of the year, stops the
run with an error for load and generation. Cross-border series have such gaps in the
source (2023: four hours on 17 June on the French border of TransnetBW), so there they
stay missing, are logged and are counted in the `missing_hours` column of the gaps file.

**Cache.** Downloaded files are kept under `data/cache/smard/`. Files for weeks that
are not yet over are downloaded again each time, because SMARD still changes them.

**Check against published totals.** `--reference` compares annual sums with
`data/reference/annual_totals.csv`. That file holds only figures read from a cited
source: the Bundesnetzagentur press release of 3 January 2024 on the 2023 electricity
market. It gives no figure for single conventional fuels, biomass or hydro, so those are
checked only through the sums.

| series | BNetzA 2023 (TWh) | SMARD data today (TWh) | deviation |
| --- | --- | --- | --- |
| wind onshore | 118.7 | 119.0 | +0.3 % |
| wind offshore | 23.5 | 23.5 | +0.1 % |
| solar | 55.2 | 55.5 | +0.6 % |
| renewables (sum) | 251.2 | 251.6 | +0.2 % |
| conventional (sum) | 197.2 | 202.0 | +2.4 % |
| total generation | 448.5 | 453.6 | +1.1 % |
| load | 456.8 | 458.4 | +0.3 % |
| commercial net export | −11.7 (42.4 exported − 54.1 imported) | −11.7 | +0.3 % |

SMARD's own annual review gives 119.2 TWh for onshore wind; the press release figure is
used so that all rows have one source. The commercial exports and imports per border
that SMARD serves today add up to larger gross totals than the published 42.4 and
54.1 TWh, so only the net figure is checked.

## Historical injections (`gridfee.injections`)

- zonal demand = load of the TSO zone × population share of the zone
- zonal feed-in = wind or solar series of the TSO zone × capacity share of the zone

Shares are fractions of the TSO zone and must sum to 1 within it.

```
# population shares        # capacity shares
zone_id,share              technology,zone_id,share
DE19,0.37                  wind_onshore,DE19,0.31
```

Shares are given per TSO zone as a whole or not at all. Where a TSO zone has no shares
for a quantity, its zones get equal shares, flagged `placeholder = True` in
`shares.csv` and announced in a warning.

**All shares are placeholders today.** Real shares need the owner's
`aggregate_to_22_zones.py`, which is not in the drafts repository yet. Until then the
zonal series only show the mechanics: for example, offshore wind is spread evenly over
all TenneT zones, inland ones included.

**Energy balance.** For every TSO zone and quantity, the zonal series must add up to
the TSO-zone series in every hour (`energy_balance.csv`). The build fails if they do
not. A TSO zone without a series for a technology must have none of it in its zones.

**Zone-to-TSO assignment.** Each zone belongs to one TSO zone, but real control areas
do not follow state borders. `data/networks/de22/TSO_SOURCES.md` lists the source for
every zone and the zones that are split between two TSOs. For split zones the TSO-zone
load and feed-in are attributed to the wrong zones to some degree.
