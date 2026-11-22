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
A border SMARD has no values for in a year (Belgium before 2020, for example) is an empty
column.

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

Without share files the zonal series only show the mechanics: for example, offshore
wind is spread evenly over all TenneT zones, inland ones included. Share files built
from public data ship in `data/shares/`; see [Zone shares from public data](#zone-shares-from-public-data).

**Energy balance.** For every TSO zone and quantity, the zonal series must add up to
the TSO-zone series in every hour (`energy_balance.csv`). The build fails if they do
not. A TSO zone without a series for a technology must have none of it in its zones.

**Zone-to-TSO assignment.** Each zone belongs to one TSO zone, but real control areas
do not follow state borders. `data/networks/de22/TSO_SOURCES.md` lists the source for
every zone and the zones that are split between two TSOs. For split zones the TSO-zone
load and feed-in are attributed to the wrong zones to some degree.


## Zone shares from public data

`data/shares/` holds share files for the 22 zones, built by two scripts:

```bash
python scripts/extract_mastr.py --zip <URL or path of the MaStR full export> --out mastr/
python scripts/build_zone_shares.py --mastr mastr/
gridfee backcast --year 2023 --population-shares data/shares/population_shares.csv \
    --capacity-shares data/shares/capacity_shares.csv \
    --residual-shares data/shares/residual_shares.csv
```

| File | Content | Source |
| --- | --- | --- |
| `population_shares.csv` | zone's share of its TSO zone's population, used for demand | Eurostat `demo_r_d2jan`, NUTS2, 1 January 2023 |
| `capacity_shares.csv` | zone's share of its TSO zone's onshore wind, offshore wind and solar capacity | MaStR |
| `residual_shares.csv` | zone's share of Germany's capacity other than wind and solar | MaStR |
| `generation_shares.csv` | zone's share of Germany's capacity per fuel (`--generation-shares`) | MaStR |
| `capacity_by_zone.csv` | the population and MW behind the shares | both |
| `ags_nuts2.csv`, `offshore_landing.csv` | lookups used by the build script | see below |

**MaStR.** The Marktstammdatenregister of the Bundesnetzagentur publishes all registered
units as one ZIP of XML files of about 3 GB
([download page](https://www.marktstammdatenregister.de/MaStR/Datendownload)). The files
here were built from the snapshot `Gesamtdatenexport_20261001_26.1.zip`.
`extract_mastr.py` reads only the unit files it needs, over HTTP range requests if given
a URL, which takes about 15 minutes.

- **Units counted:** those in operation on 1 July 2023 (`--date`): commissioned on or
  before that day (`Inbetriebnahmedatum`) and not finally shut down by it
  (`DatumEndgueltigeStilllegung`). The register's present status is not used, because
  the snapshot is from 2026. Units without a commissioning date are not counted. This
  gives 59.2 GW onshore wind, 8.4 GW offshore wind and 75.0 GW solar.
- **Capacity:** gross capacity (`Bruttoleistung`) for wind and solar, net capacity
  (`Nettonennleistung`) for other generation.
- **Zone:** from the municipality key (`Gemeindeschluessel`), whose first digits give the
  federal state and Regierungsbezirk and so the NUTS2 region (`ags_nuts2.csv`) and the
  zone (`zone_nuts2.csv`). Checked once against the coordinates of 40,530 onshore wind
  units and the NUTS 2021 boundaries: 139 units (339 MW of 59 GW) fall in another zone
  by coordinates. Solar units have no coordinates in the public export. 9 wind units
  (29 MW) and 18 solar units (7 MW) have no key and are left out.
- **Offshore wind, ASSUMPTION:** MaStR's unit files do not say where a wind farm comes
  ashore. `offshore_landing.csv` places Baltic Sea units in Mecklenburg-Vorpommern
  (DE03), North Sea areas N-4 and N-5 in Schleswig-Holstein (DE07) and the other North
  Sea areas in Niedersachsen-Nord (DE08). It is written from general knowledge of the
  grid connections and is not checked against a TSO or BSH document.
- **Other generation:** combustion, biomass, hydro, nuclear and geothermal units, without
  emergency generators; 96.0 GW. Units in MaStR's storage file (batteries, pumped
  hydro) are not included. The public export withholds the municipality key of large
  units (74.4 GW of the 96.0 GW) but names their district (Landkreis); such a unit
  takes the zone of the other units in its district. ASSUMPTION: generation follows
  capacity whatever the fuel, so a lignite plant and a peaking gas turbine of the same
  size count the same.

- **Per fuel (`generation_shares.csv`):** the same units, split by MaStR's
  `Energietraeger` into SMARD's national series, plus pumped storage from MaStR's
  storage file (6.4 GW; plants abroad that feed into the German grid are not included).
  ASSUMPTION: the mapping, since SMARD publishes no list of units per series. "Other
  conventional" is mineral oil, other gases, non-biogenic waste, heat and hydrogen;
  "other renewable" is geothermal, mine gas, sewage sludge and pressure relief.
  Nuclear counts the three reactors in operation on 1 January 2023 (they ran until
  15 April).

**Limits.** Capacity shares place feed-in in proportion to installed capacity, so wind
is assumed to blow equally within a TSO zone. Population places demand without regard
to industry. Each zone still belongs to one TSO zone only (see above).

## Generation costs per fuel (ASSUMPTION)

`data/costs/generation_costs_2023.csv` gives one cost per fuel for
`gridfee backcast --generation-costs`:

    cost = (fuel price + CO2 price x CO2 per MWh of heat) / efficiency + other variable cost

| Fuel | Fuel price, EUR/MWh heat | Efficiency | Cost, EUR/MWh electricity |
| --- | --- | --- | --- |
| Nuclear | 3.41 | 0.326 | 14.0 |
| Gas | 42.01 | 0.57 | 107.3 |
| Lignite | 3.30 | 0.33 | 116.2 |
| Hard coal | 26.16 | 0.356 | 155.6 |

- **CO2 price:** 83.47 EUR/t, the mean EEX settlement price of 2023 (Verein der
  Kohlenimporteure, Jahresbericht 2024, "Importkohlemarkt auf einen Blick").
- **Hard coal:** 213 EUR/t SKE, the border price of power plant coal in 2023 as carried
  forward by the Verein der Kohlenimporteure (same table); 1 t SKE = 8.141 MWh.
- **Gas:** 11,670 EUR/TJ, the border price of natural gas in 2023 as quoted in
  "Monitoring der Energiewende in Baden-Württemberg, Statusbericht 2024". The original
  statistic was not read, and the value is not converted between upper and lower
  heating value.
- **Lignite and nuclear fuel, efficiencies, CO2 per MWh of heat, other variable cost:**
  PyPSA [technology-data](https://github.com/PyPSA/technology-data) v0.13.4,
  `outputs/costs_2025.csv`. These are generic values for model studies, not 2023 data
  for German plants. Every gas plant counts as a combined-cycle plant.
- **Fuels without a row** (biomass, hydro, pumped storage, other conventional, other
  renewable) are not dispatched by cost: they feed in their observed national output.
  Their output follows subsidies, heat demand, water or storage operation, not a fuel
  price.

The order of gas and lignite depends on these choices: with a gas efficiency of 0.52
instead of 0.57, gas would cost more than lignite.

## Hourly capacity factors from ERA5

With capacity shares alone every zone of a TSO zone has the TSO zone's hourly shape:
when TenneT's wind is at half its capacity, the model has it at half in
Schleswig-Holstein and in Bayern. `--capacity-factors` (on `gridfee injections` and
`gridfee backcast`) takes a file of hourly factors per zone. A zone's part of its TSO
zone's observed feed-in in an hour is then

    capacity share x factor / sum over the zones of the TSO zone (capacity share x factor)

so the TSO-zone series from SMARD is still met exactly in every hour, and only the
differences between the zones of one TSO zone matter, not the level of the factors.
In hours in which that sum is 0 (for example SMARD reports solar feed-in while the
weather data has none) the capacity shares alone are used.

`data/weather/capacity_factors_2023.csv` holds factors for onshore wind and solar,
built with

    pip install -e ".[weather]"
    python scripts/build_capacity_factors.py --year 2023 --nuts <NUTS level 2 geojson>

- **Source:** ERA5, the reanalysis of the Copernicus Climate Change Service (hourly,
  0.25 degree grid, licence CC-BY 4.0): 100 m wind and surface solar radiation
  downwards. It is read from Google's public copy of ERA5 (ARCO-ERA5,
  `gs://gcp-public-data-arco-era5`), which needs no account. Copernicus's own Climate
  Data Store offers the same data but needs a personal token.
- **Wind (ASSUMPTION):** one generic turbine for all zones: no output below 3 m/s,
  rising with the cube of the wind speed to full output at 12 m/s, off from 25 m/s.
  Hub heights, turbine types and wake losses are not modelled.
- **Solar (ASSUMPTION):** output proportional to the radiation on a horizontal plane,
  full output at 1000 W/m2. Panel tilt, orientation and temperature are not modelled.
- **Zone (ASSUMPTION):** the mean over the ERA5 grid points inside the zone's NUTS2
  regions (2 points for Berlin up to 66 for Niedersachsen-Nord), not weighted by where
  the plants stand. Bremen holds no grid point and uses the nearest one.
- **Hours:** ERA5 wind is the value at the full hour, so an hour uses the mean of its
  start and end; radiation is the sum over the hour before the time stamp, so an hour
  uses the value stamped at its end.
- **Offshore wind** is not covered: it keeps its capacity shares. Both zones in which
  TenneT's offshore wind comes ashore lie on the North Sea coast.
- **Check:** capacity (`capacity_by_zone.csv`) times factor, summed per TSO zone,
  correlates with SMARD's hourly series at 0.96 for onshore wind (0.90 in TransnetBW)
  and 0.98 for solar in 2023. The levels differ (solar is up to 84 % too high, since
  full output is assumed at 1000 W/m2), which the split above cancels out.

