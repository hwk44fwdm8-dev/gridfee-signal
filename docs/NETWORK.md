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

For your own network, `gridfee.network.pypsa_eur.derive_zone_lines(buses, lines, links,
busmap)` does steps 3 to 5 for any busmap.
