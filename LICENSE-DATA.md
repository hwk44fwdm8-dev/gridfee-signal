# Data licences

The code is under the MIT licence ([LICENSE](LICENSE)). Some data files are derived
from open data with its own terms.

## `data/networks/de22/lines.csv`, `lines_parts.csv` and the coordinates and boundary zones in `zones.csv`

Derived from PyPSA-Eur's "Prebuilt Electricity Network for PyPSA-Eur based on
OpenStreetMap Data", version 0.8 (<https://zenodo.org/records/23896564>), which is
licensed under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
Contains information from OpenStreetMap, © OpenStreetMap contributors. These derived
files are made available under the ODbL as well.

The assignment of buses to zones used NUTS 2021 boundaries from Eurostat GISCO
(© EuroGeographics for the administrative boundaries). No boundary data is included
in this repository.

## SMARD

Data downloaded by `gridfee collect` comes from SMARD (Bundesnetzagentur, CC BY 4.0)
and is not stored in the repository.

## `data/shares/`

`population_shares.csv` is derived from Eurostat's table `demo_r_d2jan` (© European
Union; Eurostat data may be reused with the source named).

`capacity_shares.csv`, `residual_shares.csv`, `generation_shares.csv` and the MW in `capacity_by_zone.csv` are
derived from the Marktstammdatenregister (© Bundesnetzagentur,
[Datenlizenz Deutschland – Namensnennung – Version 2.0](https://www.govdata.de/dl-de/by-2-0)),
full export of 1 October 2026. The data was aggregated to zones; no unit data is
included in this repository.

## `data/costs/`

`generation_costs_2023.csv` holds 14 values (efficiencies, CO2 intensities, variable
costs, the lignite and nuclear fuel prices) read from `outputs/costs_2025.csv` of PyPSA
[technology-data](https://github.com/PyPSA/technology-data) v0.13.4. That repository
releases its scripts under GPLv3 and states no licence for its output files ("different
licenses and terms of use may apply to the various input data"); the values are cited
here with their source. The 2023 prices are single published figures (Verein der
Kohlenimporteure, Jahresbericht 2024; Monitoring der Energiewende in
Baden-Württemberg, Statusbericht 2024), also cited with their source.

`fuel_prices_2023.csv` holds twelve monthly values from the Statistisches Bundesamt
(Destatis), Monatliche Erdgasimporte, cited with their source in the file. The licence
of that table was not checked.

`data/shares/plant_efficiency.csv` is derived (shares of capacity per efficiency class
and zone, no plant-level rows) from: Open Power System Data. 2020. Data Package
Conventional power plants. Version 2020-10-01.
https://doi.org/10.25832/conventional_power_plants/2020-10-01 (primary data: BNetzA
Kraftwerksliste, Umweltbundesamt, and efficiencies researched by OPSD). The data package
asks for this attribution and states no licence.

## `data/weather/`

`capacity_factors_2023.csv` was generated using Copernicus Climate Change Service
information (2026): ERA5 hourly data on single levels, Hersbach et al.,
[doi:10.24381/cds.adbb2d47](https://doi.org/10.24381/cds.adbb2d47), licensed under
CC-BY 4.0, read from the ARCO-ERA5 copy on Google Cloud. Neither the European
Commission nor ECMWF is responsible for any use made of it. The data was converted
to capacity factors and averaged over zones; no ERA5 fields are included in this
repository.

