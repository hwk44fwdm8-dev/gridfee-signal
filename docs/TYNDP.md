# Replaying TYNDP market outcomes

This is a downscaling input converter, not a European market model. Download the
published workbooks from https://2024.entsos-tyndp-scenarios.eu/download/ and unzip
them locally. Install `pip install -e '.[derive]'` for Excel support. No downloads
are performed by the converter or its tests.

```
gridfee collect --year 2023 --out /path/raw2023
python scripts/build_tyndp_inputs.py \
  --market /path/MMStandardOutputFile_NT2030_Plexos_CY2009_2.5_v40.xlsx \
  --plants /path/PEMMDB_DE00_NationalTrends_2030.xlsx \
  --raw /path/raw2023 --scenario NT --year 2030 --climate-year 2009 \
  --out /path/raw2030
gridfee backcast --year 2030 --data /path/raw2030
```

Pass network and spatial-share arguments separately as appropriate. Target year,
scenario, climate year and reference year are settings, not inferred from names.
Workbook scenario/climate metadata must match. The published NT2030 CY2009 file
has 8,736 hours: December 31 is absent, never filled. Hour 1 is January 1 at
00:00 fixed UTC+1; successive hours have no DST discontinuity. The existing SMARD
reader converts these instants to Europe/Berlin when reading them back.

The reviewable `data/tyndp/technology_mapping.csv` maps published series suffixes
to SMARD categories. Germany combines DE00 and DEKF; their mutual exchanges cancel.
DE00 Other RES is split using matching PEMMDB hourly Small Biomass, Waste and
other component outputs; their sum must match the published aggregate. Small
biomass maps to biomass, waste to other conventional generation. Hydrogen and
other gas generation map to gas; battery and pumped hydro discharge to pumped
storage. Missing cells in published technology columns mean inactive generation
and are zero. Unknown technologies are errors, not silently dropped.

Effective load is native demand plus pumping, battery charging and electrolysis,
minus explicit and implicit DSR, plus dumped energy and minus energy not served.
The last two terms reconcile published generation before dumping with served
demand. No residual is silently manufactured: absolute hourly imbalance summed
over the run must be at most 1% of effective load. Both signed and absolute errors
are printed. This is an accounting replay, not a forecast of gross demand.

Load and renewable output are split by 2023 annual TSO sums (per technology for
renewables). Each neighbour's signed exchange is split using each TSO column's
2023 sum of absolute hourly exchanges. TYNDP DKE1/DKW1/DKKF map to DK2/DK1/DK2;
Luxembourg nodes combine to LU. Unknown or unroutable nonzero neighbours stop
conversion before writing SMARD tables; national exchanges are saved separately
for diagnosis. In particular, UK and new offshore hubs have no historical SMARD
routing. They must not be dropped or assigned to an arbitrary TSO. Until their
routing is specified this published workbook cannot produce a complete backcast
input set under the historical routing rule.

`generation_costs_2030.csv` retains 2023 efficiencies and VOM; fuel and CO2 values
are from TYNDP Matrix 2024 and NT2030 emission factors. Monthly prices repeat the
annual values twelve times. Non-CCS factors represent each aggregated fuel.
Hydrogen blue has a source price of 17.64 EUR/GJ (63.504 EUR/MWh thermal), retained
in `hydrogen_price_2030.csv` for review. The unchanged solver's single gas category
cannot price hydrogen separately after aggregation. Other conventional generation
retains its published hourly profile because it has no cost row.

The unchanged backcast validation still calls these SMARD-shaped inputs SMARD;
it compares against the replay reference, not independent observations. Generation
bounds remain observed-output bounds (now scenario-output bounds), not installed
capacity. No line capacity or solver behavior is changed by this converter.
