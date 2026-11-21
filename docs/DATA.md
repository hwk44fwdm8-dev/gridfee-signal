# Data and historical injections

## SMARD collector (`gridfee.data.smard`)

Source: SMARD, the market data platform of the Bundesnetzagentur, through its public
chart data API (no key). Licence of the data: CC BY 4.0, "Bundesnetzagentur | SMARD.de".

Filter ids and region codes are the ones in the
[bundesAPI/smard-api](https://github.com/bundesAPI/smard-api) documentation. The client
refuses any other id.

| series | filter | regions |
| --- | --- | --- |
| load (Netzlast) | 410 | DE, 50Hertz, Amprion, TenneT, TransnetBW |
| wind onshore | 4067 | DE and the four TSO zones |
| wind offshore | 1225 | DE, 50Hertz, TenneT (SMARD has no series for the other two) |
| solar | 4068 | DE and the four TSO zones |
| lignite, nuclear, hydro, other conventional, other renewable | 1223, 1224, 1226, 1227, 1228 | DE |
| biomass, hard coal, pumped storage, gas | 4066, 4069, 4070, 4071 | DE |

Values are the energy of each hour in MWh, equal to the mean power in MW.

**Not collected: cross-border flows.** The owner's draft collector used filter ids
4075–4083 for them. These ids are not in the API documentation, and when tried, most
returned errors. They are left out rather than guessed. A documented source (for example
the ENTSO-E Transparency Platform, which needs a free key) has to be chosen before
boundary zones can get historical flows. Note also that SMARD's foreign trade figures
are commercial schedules, not physical flows.

**Time.** All tables use an hourly Europe/Berlin index; the day of the change to summer
time has 23 hours, the day of the change back 25.

**Gaps.** Runs of up to `--max-gap-hours` (default 3) missing hours inside a series are
filled by linear interpolation; each fill is logged and counted in
`smard_gaps_<year>.csv`. A longer gap, or one at the start or end of the year, stops the
run with an error.

**Cache.** Downloaded files are kept under `data/cache/smard/`. Files for weeks that
are not yet over are downloaded again each time, because SMARD still changes them.

**Check against published totals.** `--reference` compares annual sums with
`data/reference/annual_totals.csv`. That file holds only figures read from a cited
source; today that is onshore wind in 2023 (119.2 TWh; SMARD data gave 119.0 TWh).
Other fuels are not checked until a published figure for each is added.

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
