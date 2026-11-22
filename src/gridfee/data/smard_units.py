"""Hourly output per generation unit from SMARD's power plant data.

SMARD publishes the output of generation units of 100 MW or more (source: ENTSO-E)
next to its power plant list. The list its own application reads,
``https://www.smard.de/app/power_plant_data/power_plant_metadata.json``, gives each
unit ("block") a ``productionId``; that id is a series of the same chart data API
:mod:`gridfee.data.smard` reads, in the region of the unit's TSO. Only ids from that
list are requested. Names come from the application's German translation file.

The list is SMARD's current one. A unit that has left it (the nuclear units shut down
in April 2023) has no id here and is not collected.

Values are the energy of the hour in MWh, equal to the mean power in MW, on a
Europe/Berlin index. Missing hours stay missing: nothing is interpolated.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from gridfee.data.smard import TIMEZONE, SmardClient, SmardError, table_filename

logger = logging.getLogger(__name__)

APP_URL = "https://www.smard.de/app"
METADATA_NAME = "power_plant_data/power_plant_metadata.json"
TRANSLATION_NAME = "assets/translations/lang-de.json"

# The fuel SMARD's list gives a unit (the label of the Bundesnetzagentur power plant
# list) -> the fuel of ``generation_de`` it is compared with. Labels that do not say
# which national series the unit's output is counted in are left out and reported as
# not compared: "Wärme" (steam turbines of combined-cycle plants) and "Abfall" (waste,
# split between renewable and conventional in the national series).
FUEL_OF_RESOURCE: dict[str, str] = {
    "Braunkohle": "lignite",
    "Steinkohle": "hard_coal",
    "Erdgas": "gas",
    "Kernenergie": "nuclear",
    "Pumpspeicher": "pumped_storage",
    "Biomasse": "biomass",
    # Assumption: oil and "other non-renewable" (the units are steelworks gas plants)
    # are part of SMARD's "Sonstige Konventionelle".
    "Mineralölprodukte": "other_conventional",
    "Sonstige konventionelle Energieträger": "other_conventional",
}
NOT_COMPARED = "not_compared"

UNIT_COLUMNS = [
    "unit_id",
    "plant",
    "unit",
    "plant_eic",
    "unit_eic",
    "production_id",
    "series_unit_id",
    "tso",
    "country",
    "resource",
    "fuel",
    "power_mw",
    "status",
    "commissioning",
    "postal_code",
    "city",
    "latitude",
    "longitude",
]


def _text(translations: dict, key: str | None) -> str | None:
    """SMARD's keys are ``<group>.<text>``; untranslated ones keep the text itself."""
    if key is None:
        return None
    return translations.get(key, key.split(".", 1)[-1]).strip()


def read_unit_list(client: SmardClient) -> pd.DataFrame:
    """The units of SMARD's power plant list that have an output series.

    ``unit_id`` is SMARD's block id (the MaStR unit number ``SEE...`` or a ``BNA...``
    number of the power plant list). SMARD gives some units the same series (the gas
    and the steam turbine of one combined-cycle plant): ``series_unit_id`` names the
    largest unit of such a group, under which the shared series is stored.
    """
    metadata = client._get(METADATA_NAME, cache=False, base_url=APP_URL)
    translations = client._get(TRANSLATION_NAME, cache=False, base_url=APP_URL)
    rows = []
    for plant in metadata["plants"]:
        latitude, longitude = (plant.get("coordinates") or [None, None])[:2]
        for block in plant["blocks"]:
            if block.get("productionId") is None:
                continue
            resource = _text(translations, block["resource"])
            rows.append(
                {
                    "unit_id": block["id"],
                    "plant": _text(translations, plant["name"]),
                    "unit": _text(translations, block["name"]),
                    "plant_eic": plant.get("code"),
                    "unit_eic": block.get("blockCode"),
                    "production_id": int(block["productionId"]),
                    "series_unit_id": None,
                    "tso": plant["regionId"],
                    "country": _text(translations, plant.get("country")),
                    "resource": resource,
                    "fuel": FUEL_OF_RESOURCE.get(resource, NOT_COMPARED),
                    "power_mw": block.get("power"),
                    "status": _text(translations, block.get("status")),
                    "commissioning": block.get("commissioning"),
                    "postal_code": plant.get("postalCode"),
                    "city": _text(translations, plant.get("city")),
                    "latitude": latitude,
                    "longitude": longitude,
                }
            )
    units = pd.DataFrame(rows, columns=UNIT_COLUMNS).sort_values("unit_id", ignore_index=True)
    repeated = units["unit_id"][units["unit_id"].duplicated()].tolist()
    if repeated:
        raise SmardError(f"SMARD's power plant list repeats unit_id {repeated}")
    if units.groupby("production_id")["tso"].nunique().gt(1).any():
        raise SmardError("SMARD's power plant list has one output series in two TSO zones")
    largest = units.fillna({"power_mw": 0.0}).groupby("production_id")["power_mw"].idxmax()
    units["series_unit_id"] = units["production_id"].map(
        units["unit_id"].loc[largest].set_axis(largest.index)
    )
    return units


def collect_units_year(
    year: int, out_dir: str | Path, client: SmardClient | None = None
) -> dict[str, pd.DataFrame]:
    """Download one year of output per unit and write the tables to ``out_dir``.

    ``unit_generation`` (hours x ``unit_id``, MW) holds the series SMARD has at least
    one value for in the year, each under its ``series_unit_id``. ``units`` lists
    every unit with an output series; the hours with a value (``hours``) and the
    energy (``energy_mwh``) of a series are on the row of its ``series_unit_id`` and
    empty on the rows of the other units that share it.
    """
    client = client or SmardClient()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start = pd.Timestamp(year=year, month=1, day=1, tz=TIMEZONE)
    end = pd.Timestamp(year=year + 1, month=1, day=1, tz=TIMEZONE)

    units = read_unit_list(client)
    series = {
        unit.unit_id: client.fetch_series(unit.production_id, unit.tso, start, end)
        for unit in units[units["unit_id"] == units["series_unit_id"]].itertuples()
    }
    hours = pd.date_range(start, end, freq="h", inclusive="left", name="hour")
    generation = pd.DataFrame(series, index=hours)
    units["hours"] = generation.notna().sum().reindex(units["unit_id"]).astype("Int64").to_numpy()
    units["energy_mwh"] = generation.sum().reindex(units["unit_id"]).to_numpy()
    generation = generation.loc[:, generation.notna().any()]
    logger.info(
        "%d of %d output series (%d units) have values in %d",
        generation.shape[1],
        len(series),
        len(units),
        year,
    )

    tables = {"unit_generation": generation, "units": units}
    generation.to_csv(out_dir / table_filename("unit_generation", year))
    units.to_csv(out_dir / table_filename("units", year), index=False)
    return tables


def read_units(directory: str | Path, year: int) -> pd.DataFrame:
    path = Path(directory) / table_filename("units", year)
    if not path.is_file():
        raise SmardError(f"missing {path}; run `gridfee collect-units --year {year}` first")
    return pd.read_csv(path)


def fuel_coverage(
    unit_generation: pd.DataFrame, units: pd.DataFrame, generation_de: pd.DataFrame
) -> pd.DataFrame:
    """Per fuel: the units' energy next to the national generation of that fuel.

    A series counts under the fuel of its ``series_unit_id``; ``units`` and
    ``capacity_mw`` include the units that share it. ``share`` is the units' annual
    energy over the national one. ``max_hourly_excess_mw``
    is the largest amount by which the units' sum exceeds the national series in an
    hour (0 if never), a check that the units are counted in that series. Units whose
    fuel label maps to no national series are the row ``not_compared``.
    """
    if not unit_generation.index.equals(generation_de.index):
        raise ValueError("unit and national generation do not cover the same hours")
    fuel_of = units.set_index("unit_id")["fuel"]
    unknown = sorted(set(unit_generation.columns) - set(fuel_of.index))
    if unknown:
        raise ValueError(f"units not in the unit list: {unknown}")
    rows = []
    fuels = [*generation_de.columns, NOT_COMPARED]
    for fuel in fuels:
        columns = [unit for unit in unit_generation.columns if fuel_of[unit] == fuel]
        members = units[units["series_unit_id"].isin(columns)]
        if fuel == NOT_COMPARED and not columns:
            continue
        hourly = unit_generation[columns].sum(axis=1)
        row = {
            "fuel": fuel,
            "units": len(members),
            "capacity_mw": float(members["power_mw"].sum()),
            "unit_twh": float(hourly.sum()) / 1e6,
            "missing_unit_hours": int(unit_generation[columns].isna().sum().sum()),
        }
        if fuel in generation_de.columns:
            national = generation_de[fuel]
            total = float(national.sum())
            row["national_twh"] = total / 1e6
            row["share"] = float(hourly.sum()) / total if total else float("nan")
            row["max_hourly_excess_mw"] = float((hourly - national).clip(lower=0).max())
        rows.append(row)
    return pd.DataFrame(
        rows,
        columns=[
            "fuel",
            "units",
            "capacity_mw",
            "unit_twh",
            "national_twh",
            "share",
            "max_hourly_excess_mw",
            "missing_unit_hours",
        ],
    )
