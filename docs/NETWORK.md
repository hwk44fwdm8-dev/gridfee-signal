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

Boundary zones are ordinary buses connected by AC lines. HVDC interconnectors and other
controllable links are not modelled yet.

## The 22-zone network of Germany

The repository does not ship a network of Germany yet. Susceptances and capacities
between zones must come from a documented source; none are made up here. The examples
under `examples/networks/` are synthetic test networks.

## Optional: derive a zone network from PyPSA-Eur

A zone network can be derived from a PyPSA-Eur network without making PyPSA-Eur a
dependency of this tool. The steps below are a guide and have not been run against a
PyPSA-Eur build in this repository; check the result before use.

1. Build or download a PyPSA-Eur base network (the `.nc` file with the transmission
   grid), outside of this tool.
2. Create a busmap: a mapping from every PyPSA-Eur bus to a `zone_id`, for example by
   a spatial join of the bus coordinates with the zone shapes, and to a boundary zone
   per neighbouring country.
3. Aggregate with PyPSA:

   ```python
   import pypsa
   from pypsa.clustering.spatial import get_clustering_from_busmap

   n = pypsa.Network("base.nc")
   clustering = get_clustering_from_busmap(n, busmap)
   lines = clustering.n.lines  # one aggregated line per pair of zones
   ```

4. Write `lines.csv` from the aggregated lines: `from_zone = bus0`, `to_zone = bus1`,
   `susceptance_mw_per_rad = v_nom**2 / x` and `capacity_mw = s_nom` (apply your own
   security margin, e.g. PyPSA-Eur's `s_max_pu`).

Aggregating parallel paths into one equivalent line per zone pair is an approximation;
intra-zone congestion disappears.
