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

`capacity_shares.csv`, `residual_shares.csv` and the MW in `capacity_by_zone.csv` are
derived from the Marktstammdatenregister (© Bundesnetzagentur,
[Datenlizenz Deutschland – Namensnennung – Version 2.0](https://www.govdata.de/dl-de/by-2-0)),
full export of 1 October 2026. The data was aggregated to zones; no unit data is
included in this repository.

## `data/weather/`

`capacity_factors_2023.csv` was generated using Copernicus Climate Change Service
information (2026): ERA5 hourly data on single levels, Hersbach et al.,
[doi:10.24381/cds.adbb2d47](https://doi.org/10.24381/cds.adbb2d47), licensed under
CC-BY 4.0, read from the ARCO-ERA5 copy on Google Cloud. Neither the European
Commission nor ECMWF is responsible for any use made of it. The data was converted
to capacity factors and averaged over zones; no ERA5 fields are included in this
repository.

