# Network config

`gridfee.network.load_network(directory)` reads `zones.csv` and `lines.csv` from a
directory and returns a validated `ZoneNetwork`. The column tables are in the
[README](../README.md#network-config).

## Rules checked on load

- `zone_id` and `line_id` are unique. Ids are read as text, so `01` stays `01`.
- An internal zone has a `tso_zone` out of `50Hertz`, `Amprion`, `TenneT`, `TransnetBW`.
- A boundary zone has a `country` and no `tso_zone`.
- A line connects two different, existing zones. Several lines between the same two
  zones are allowed.
- `susceptance_mw_per_rad` and `capacity_mw` are finite and greater than 0.
- The network is connected.
- Unknown columns are rejected, so a misspelt column name cannot be ignored silently.

Errors name the file and the line number.

## Susceptance

In the DC approximation the flow on a line is

    flow(from -> to) = susceptance_mw_per_rad * (angle_from - angle_to)

Only the ratios between the susceptances of the lines affect flows, PTDFs and shadow
prices; multiplying all of them by the same factor changes nothing but the voltage
angles. For a single circuit with series reactance `x` (ohm) at nominal voltage `v_nom`
(kV), `susceptance_mw_per_rad = v_nom**2 / x`.

Boundary zones are ordinary buses connected by AC lines. HVDC links have no model of
their own: `lines.csv` only knows AC-style lines (see below for how the shipped network
treats them).

## The 22-zone network of Germany (`data/networks/de22/`)

**The lines are an assumption, not TSO data.** They are derived from open data, by
`scripts/derive_de22_network.py`, and have not been compared with the TSOs' own grid
models. The examples under `examples/networks/` are synthetic test networks.

Sources:

- **Grid:** PyPSA-Eur's prebuilt electricity network, version 0.8
  (`buses.csv`, `lines.csv`, `links.csv` from
  <https://data.pypsa.org/workflows/eur/osm/0.8/>, which PyPSA-Eur's `data/versions.csv`
  lists as the archive of <https://zenodo.org/records/23896564>). It is built from
  OpenStreetMap and licensed under the ODbL; see [LICENSE-DATA.md](../LICENSE-DATA.md).
  PyPSA-Eur itself and its Snakemake workflow are not run.
- **Zone shapes:** Eurostat GISCO NUTS 2021 regions, level 2, 1:1 million. The NUTS2
  regions of each zone are in `zone_nuts2.csv` (from the owner's drafts). The shapes are
  only used to place buses; they are not redistributed here.

Steps of the script:

1. Keep buses, AC lines and DC links that are in service; drop AC below 220 kV (as
   PyPSA-Eur does). Lines carry PyPSA-Eur's `x` (ohm) and `s_nom` (MVA), which it
   computes from the voltage, the number of circuits and standard line types, not from
   measured values.
2. Busmap: each German bus goes to the zone whose NUTS2 region contains it (3 buses just
   off the generalised coastline go to the nearest region). Foreign buses are used only
   if a line or link from Germany ends there; they go to one boundary zone per
   neighbouring area as SMARD names it (`DK1`/`DK2` split at 10.9° E, Norway as `NO2`,
   Sweden as `SE4`). The neighbours' own grids are not modelled.
3. `pypsa.clustering.spatial.get_clustering_from_busmap(n, busmap)` aggregates the AC
   grid: lines inside a zone disappear, and all lines between two zones become one
   equivalent line whose rating is the sum of the ratings and whose reactance is the
   parallel combination, each line scaled to the distance between the zone centres.
4. `susceptance_mw_per_rad = v_nom**2 / x` and
   `capacity_mw = 0.7 * summed rating + rating of DC links`. The 0.7 is PyPSA-Eur's
   default `s_max_pu`, its stand-in for the N-1 security margin.
5. DC links between two zones (the cables to Norway, Sweden, Denmark East and Belgium;
   none is in service between two German zones in this data) count with their full
   rating. Where a link is the only connection of a boundary zone, the line's
   susceptance has no effect and is set equal to the capacity. A link parallel to AC
   lines would be treated as an AC line; the script warns, and it does not occur.

`lines_parts.csv` lists the summed AC rating and DC rating behind each line.

To rebuild (needs `pip install -e ".[derive]"` for geopandas):

```bash
python scripts/derive_de22_network.py --osm <dir with the three CSVs> \
  --nuts NUTS_RG_01M_2021_4326_LEVL_2.geojson
```

Known weaknesses of the result:

- **Capacities are generous.** Summing all circuits between two zones assumes they load
  evenly, and congestion inside a zone disappears. The 2023 backcast finds no congested
  line between German zones (with placeholder shares; see
  [METHODOLOGY.md](METHODOLOGY.md)).
- **Some interconnectors are too small.** Observed 2023 flows exceed the capacity to
  Denmark East in 2,271 hours (up to 1,004 MW against the 600 MW of the one DC cable
  in the data), and by a few percent to Sweden, Norway, Belgium and
  France. The backcast cuts such flows to the capacity and reports it.
- **OpenStreetMap is incomplete and unofficial**, and ratings come from standard line
  types.
- A zone belongs to one TSO in `zones.csv` although several zones are split between two
  ([TSO_SOURCES.md](../data/networks/de22/TSO_SOURCES.md)).

### Published limits for four lines (JAO)

Four lines between German zones no longer use the standard rating. Their rating is the
sum, over the circuits in [`jao_circuits.csv`](../data/networks/de22/jao_circuits.csv),
of `sqrt(3) x voltage x maximum current`, with the maximum current that the TSOs publish
in JAO's Core Static Grid Model (3rd release, workbook of 1 March 2023,
<https://www.jao.eu/static-grid-model>). These are the four lines that carried the whole
2023 fee signal with the standard ratings.

| Line | Circuits in the JAO workbook | A each | Rating before, MVA | Rating now, MVA | `capacity_mw` before | now |
| --- | --- | --- | --- | --- | --- | --- |
| `DE05-DE06` Sachsen-Anhalt–Thüringen | Lauchstaedt–Vieselbach 471, 472 (380 kV) | 3,600 | 3,396.2 | 4,738.9 | 2,377.3 | 3,317.2 |
| `DE05-DE09` Sachsen-Anhalt–Niedersachsen-Süd | Helmstedt–Wolmirstedt 491, 492 (380 kV) | 3,150 | 3,396.2 | 4,146.5 | 2,377.3 | 2,902.6 |
| `DE16-DE20` Hessen–BW-Karlsruhe | Buerstadt–Rheinau KURPFA W, O (380 kV) | 3,096 | 3,396.2 | 4,075.4 | 2,377.3 | 2,852.8 |
| `DE17-DE20` Rheinland-Pfalz–BW-Karlsruhe | Daxlanden–Maximiliansau (220 kV); Daxlanden–Weingarten (380 kV); Mutterstadt–GKM S, N (220 kV) | 2,500; 2,720; 1,380 | 2,457.8 | 3,794.6 | 1,720.4 | 2,656.2 |

How the circuits were matched, and what remains an assumption:

- **Matching is by substation.** For the first three lines the OpenStreetMap data has
  exactly two 380 kV circuits between the two zones, between the same two substations
  as the two JAO circuits. The zone of each substation follows from its location
  (Lauchstädt, Wolmirstedt: Sachsen-Anhalt; Vieselbach: Thüringen; Helmstedt:
  Niedersachsen-Süd; Bürstadt: Hessen; Rheinau, Daxlanden, GKM: BW-Karlsruhe;
  Maximiliansau, Weingarten, Mutterstadt: Rheinland-Pfalz). JAO's map was not used
  for these four lines.
- **`DE17-DE20` does not match one to one.** JAO lists the 380 kV circuit
  Daxlanden–Weingarten as a tie-line between Amprion and TransnetBW; in the
  OpenStreetMap data that circuit ends west of the Rhine and does not reach the zone
  BW-Karlsruhe, and Mutterstadt–GKM is counted there as four circuits where JAO has two.
  The capacity uses the four JAO circuits. The susceptance is still the one derived
  from OpenStreetMap, so it is without the 380 kV circuit.
- **Which column.** `Fixed` where the workbook has it, else `Period 1` (the only period
  given for these circuits). For Buerstadt–Rheinau Amprion publishes only `DLRmax(A)`,
  the upper end of the weather-dependent rating, so this line's limit is an upper bound.
  Circuits listed by both TSOs have the same value in both rows, except
  Daxlanden–Weingarten, where TransnetBW gives a second period of 3,150 A; the lower
  value is used.
- **The 0.7 stays an assumption.** The published value is the current a circuit can
  carry, not what the TSOs allow in N-1 secure operation.
- **Only these four lines.** All other lines keep the standard ratings; the comparison
  suggests those are too low as well (380 kV standard: 2,580 A per circuit).
- **One equivalent line per pair of zones** remains the model. The published limits
  are those of the circuits, and flows inside a zone are still not modelled.

The workbook is not in this repository (JAO's terms restrict reproduction); the ten
rows above are quoted with their source.

## The 38-region network of Germany (`data/networks/de38/`)

The same model one level finer: each of Germany's 38 NUTS2 regions is a zone (the zone
id is the NUTS2 code, so `DE11` here is Stuttgart, not zone `DE11` of the 22-zone
network), with the same boundary zones and the same TSO per region as its zone in
`de22`. Every AC line is built from the circuits that the four German TSOs publish in
JAO's Core Static Grid Model (workbook of 1 March 2023): 96 lines, 75 of them between
two German regions, from 304 circuits that cross a region border (211 at 380 kV, 93 at
220 kV). It is rebuilt by `scripts/derive_de38_network.py`.

**What is TSO data and what is not.** Published per circuit: the two substations, the
voltage, the reactance and the maximum current. Not published, and so assumptions of
this project: where each substation is, the 0.7 security margin, and that all circuits
between two regions act as one line.

How a line is made:

1. **Circuits.** All rows of the German TSOs in the sheets `Lines` and `Tielines`. A
   tie line is listed by the TSO on each side under one EIC code; the lower limit and
   the larger reactance count (some TSOs give their share up to the border).
2. **Limit.** `Fixed` if given, else the lowest of `Period 1` to `6`, else `DLRmax(A)`,
   the upper end of the weather-dependent rating. `jao_circuits.csv` says which was
   used per circuit: of the 304, 144 fixed, 79 seasonal and 81 `DLRmax`. For 16 lines
   every circuit is `DLRmax`, so their capacity is an upper bound.
3. **Place of each substation.** The workbook has no coordinates. Substations are placed
   by JAO's map of the same publication (median over the circuits that end there) and
   then by NUTS2 region. Checked against the substations in the OpenStreetMap data:
   half of the 451 map positions are within 0.6 km of one, a tenth are more than 8 km
   off. Eight substations that the map puts on the wrong side of a region border are
   placed by hand in `jao_places.csv`, with the reason; so are the foreign ends of tie
   lines (by country) and names the two TSOs of a tie line spell differently.
4. **Tee points.** A circuit with a tee is listed as one row per leg, to a point named
   `Y-<substation>`. The map draws these schematically, so a tee point is put in the
   region of the substation at the end of its shortest leg. Ends less than 1 km apart
   count as one place. One tee is listed with a wrong end name and is corrected in
   `jao_corrections.csv`.
5. **Aggregation** as in the 22-zone network: ratings add up, reactances combine in
   parallel after scaling to the distance between the region centres, and
   `capacity_mw = 0.7 x sum of sqrt(3) x voltage x maximum current`.
6. **DC links** (to Belgium, Denmark East, Norway, Sweden) keep the rating from the
   OpenStreetMap data, because JAO gives a cable's current, not the converter's power.

Compared with the same 38 regions derived from OpenStreetMap as for `de22`
(`derive_de22_network.py --network de38` writes that version): 84 AC lines exist in
both, and for 52 of them the two capacities are within 25 % of each other. Summed over
the 84, the JAO capacity is 15 % lower. Eight lines exist only in the JAO version and
one only in the OpenStreetMap version; these are long circuits that pass through a
third region, or substations near a border that the two sources place differently.

Limits of this network, beyond those of `de22`:

- **A wrong region for one substation moves whole circuits to another line.** The check
  against OpenStreetMap found eight; others near a border can remain. The first run
  showed what one does: with Würgassen placed near Kassel, the line
  Kassel–Braunschweig was priced in 715 hours of 2023; placed correctly it does not
  exist.
- **Weak lines decide.** 16 of the 75 lines between German regions are a single circuit
  and the smallest is 211 MW. In the DC model such a line in parallel to strong ones
  is full first and then limits the flow on the strong ones too. Operators avoid this
  with switching and phase shifters, which the model does not have.
- **Lines between regions that do not touch** exist (Mecklenburg-Vorpommern–Berlin, for
  example): one circuit that passes through a third region without a substation there.
- **Tie lines.** The observed flow to a neighbour exceeds 0.7 times the published limit
  in many hours (Netherlands from TenneT in 591 hours, France from TransnetBW in 254,
  with flows up to 0.9 and 1.0 times the published limit), so the 0.7 is too low
  there. The backcast cuts the flow to the capacity and reports it.
- **One publication.** The limits are those of March 2023 for the whole year.

The workbook and the map are not in this repository (JAO's terms restrict
reproduction). `jao_circuits.csv` lists the circuits behind each line by EIC code, so
every capacity can be recomputed from the workbook; it does not repeat the currents.

For your own network, `gridfee.network.pypsa_eur.derive_zone_lines(buses, lines, links,
busmap)` does steps 3 to 5 for any busmap.

## The substation network of Germany (`data/networks/desub/`)

The grid as the TSOs publish it, without merging: one bus per substation and voltage
level, one branch per circuit and per 380/220 kV transformer, all from JAO's Core
Static Grid Model (workbook of 1 March 2023). The 22 zones of `de22` are kept only to
report the fee signal (`report_zone` of each substation). Neighbouring countries stay
one boundary zone each, split by TSO where `--borders-by-tso` is set.

| | Count |
| --- | --- |
| Substations (`substations.csv`, kind `substation`) | 373 |
| Tee points on lines (kind `tee`) | 51 |
| Buses in Germany: 220 kV / 380 kV | 218 / 285 |
| Circuits: 220 kV / 380 kV | 329 / 541 |
| 380/220 kV transformers | 103 |
| DC links to neighbours (`dc_links.csv`) | 4 |

It is built in two steps. `scripts/derive_desub_substations.py` writes
`substations.csv`; `scripts/derive_desub_network.py` writes `buses.csv` and
`branches.csv` here and, outside the repository, the `zones.csv` and `lines.csv` that
`gridfee backcast --network` reads. Those two hold JAO's reactances and currents in
another form and are not committed (JAO's terms restrict reproduction); with the
workbook and the map anyone can rebuild them.

How it is made:

1. **Substations.** Every end of a German circuit and every German transformer's
   substation, under JAO's name. Circuit sections that share one EIC code but have
   different ends are kept apart (suffix `/2`, `/3`).
2. **Positions** are OpenStreetMap's, not JAO's: the substation of that name within
   40 km of where JAO's map draws it (`position_basis` `name`, 319 substations; on
   part of the name, `name_part`, 31), else an unnamed OpenStreetMap substation or
   line end within 3 km (`nearest`, 13), else by hand with the reason in
   `substation_positions.csv` (`manual`, 5), else the mean of the neighbours
   (`neighbours`: 5 substations and all tee points). The name matches were not checked
   one by one.
3. **Buses.** A substation with both voltage levels is two buses, `<id>-380` and
   `<id>-220`, joined only by its transformers.
4. **Circuits.** Susceptance from the published reactance; limit `0.7 x sqrt(3) x
   voltage x current`, with the current chosen as in the 38-region network (`Fixed`,
   else the lowest period, else `DLRmax`; `limit_kind` in `branches.csv`: 418 fixed,
   171 period, 280 `DLRmax`, the last an upper bound).
5. **Transformers.** The 103 units of the German TSOs between 380 and 220 kV, each a
   branch with the published reactance and `0.7 x sqrt(3) x voltage x current`
   (`Fixed` current, else the lower, else the upper published one: 48, 22 and 33
   units; 235 to 950 MW). Left out of the 127 German rows: 5 at Bürs in Austria and
   19 with 380 kV on both sides (phase shifters and the converter transformers of
   offshore links), which the model does not operate.
6. **DC links** land where their published routes do: ALEGrO at Oberzier, Kontek at
   Bentwisch, NordLink at Wilster/West, Baltic Cable at Herrenwyk (ASSUMPTION from
   general knowledge of the links; ratings as in the 38-region network).

Limits of this network:

- **No 110 kV.** Load and plants connected below 220 kV sit on one substation, on its
  220 kV bus where it has one. In reality the 110 kV grid feeds a town from several
  substations and carries some power in parallel to the transmission grid.
- **One line is left out:** Gütersloh–Hesseln at 220 kV, because JAO lists no
  transformer at either end, so the pair has no connection to the rest. Their load
  is not lost: each substation still has a connected bus (`connected` in `buses.csv`
  and `branches.csv`).
- **No switching, no phase shifters, no N-1 calculation.** Every published element is
  in service all year with 0.7 of its limit. Operators open busbars and steer phase
  shifters to keep weak 220 kV paths from filling; the model cannot.
- **Baltic Cable** (600 MW) reaches the grid through one 258 MW transformer at Siems
  and one 253 MW line to Lübeck, so its observed flow cannot be carried in many hours.
- **Simbach** has no substation outline in OpenStreetMap; it is placed where its two
  220 kV lines end.
- **One publication,** as for the 38 regions; tie lines as there.
