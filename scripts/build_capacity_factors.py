"""Hourly wind and solar capacity factors per zone from ERA5 (see docs/DATA.md).

ERA5 is the Copernicus Climate Change Service's reanalysis (0.25 degree grid,
hourly). It is read from Google's public copy (ARCO-ERA5), which needs no account:
gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3

    pip install -e ".[weather]"
    python scripts/build_capacity_factors.py --year 2023 --nuts <NUTS level 2 geojson>

The NUTS file is the one scripts/derive_de22_network.py uses. A zone's factor is
the mean over the grid points inside it of the factor at each point.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr

from gridfee.injections.weather import (
    solar_capacity_factor,
    wind_capacity_factor,
    write_capacity_factors,
    zone_mean,
)

ROOT = Path(__file__).resolve().parents[1]
DE22 = ROOT / "data" / "networks" / "de22"
STORE = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
U100, V100 = "100m_u_component_of_wind", "100m_v_component_of_wind"
SSRD = "surface_solar_radiation_downwards"
# Germany with a margin, degrees north and east.
NORTH, SOUTH, WEST, EAST = 55.25, 47.0, 5.5, 15.25


def grid_point_zones(
    latitude: np.ndarray, longitude: np.ndarray, nuts_file: Path, network: Path = DE22
) -> tuple[pd.Series, dict[str, tuple[float, float]]]:
    """Zone of every grid point inside a zone, indexed by (latitude, longitude).

    Also returns, for zones too small to hold a grid point, the point nearest to them.
    """
    zone_of_nuts = pd.read_csv(network / "zone_nuts2.csv", comment="#").set_index("nuts2")[
        "zone_id"
    ]
    regions = gpd.read_file(nuts_file).set_index("NUTS_ID").loc[zone_of_nuts.index, ["geometry"]]
    zones = regions.assign(zone_id=zone_of_nuts).dissolve("zone_id")
    lat, lon = np.meshgrid(latitude, longitude, indexing="ij")
    points = gpd.GeoDataFrame(
        {"latitude": lat.ravel(), "longitude": lon.ravel()},
        geometry=gpd.points_from_xy(lon.ravel(), lat.ravel()),
        crs=4326,
    )
    inside = gpd.sjoin(points, zones, predicate="within", how="inner")["zone_id"]
    index = pd.MultiIndex.from_frame(points.loc[inside.index, ["latitude", "longitude"]])
    empty = zones.loc[sorted(set(zones.index) - set(inside))].to_crs(3035)
    nearest = gpd.sjoin_nearest(empty, points.to_crs(3035), how="left")
    nearest = nearest[~nearest.index.duplicated()]
    substitutes = {zone: (row.latitude, row.longitude) for zone, row in nearest.iterrows()}
    return pd.Series(inside.to_numpy(), index=index, name="zone_id"), substitutes


def load_days(dataset: xr.Dataset, days: pd.DatetimeIndex, workers: int) -> xr.Dataset:
    def one(day: pd.Timestamp) -> xr.Dataset:
        return dataset.sel(time=slice(day, day + pd.Timedelta(hours=23))).load()

    with ThreadPoolExecutor(workers) as pool:
        return xr.concat(list(pool.map(one, days)), dim="time")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--nuts", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--network", type=Path, default=DE22, help="Network directory with zone_nuts2.csv."
    )
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    out = args.out or ROOT / "data" / "weather" / f"capacity_factors_{args.year}.csv"

    store = xr.open_zarr(STORE, storage_options={"token": "anon"}, chunks=None)
    area = store[[U100, V100, SSRD]].sel(latitude=slice(NORTH, SOUTH), longitude=slice(WEST, EAST))
    # Hours of the year in UTC, with a margin for the Europe/Berlin year and the
    # hour after each hour, which the hourly means below need.
    first = pd.Timestamp(args.year, 1, 1) - pd.Timedelta(days=1)
    days = pd.date_range(first, pd.Timestamp(args.year + 1, 1, 1), freq="D")
    data = load_days(area, days, args.workers)

    cell_zone, substitutes = grid_point_zones(
        data.latitude.values, data.longitude.values, args.nuts, args.network
    )

    def per_zone(cell_factors: pd.DataFrame) -> pd.DataFrame:
        zonal = zone_mean(cell_factors, cell_zone)
        for zone, cell in substitutes.items():
            zonal[zone] = cell_factors[cell]
        return zonal.sort_index(axis=1)

    def cells(values: xr.DataArray) -> pd.DataFrame:
        frame = values.stack(cell=("latitude", "longitude")).to_pandas()
        frame.index = frame.index.tz_localize("UTC")
        return frame

    # ERA5 wind is the value at the hour; the mean of both ends stands for the hour
    # that starts there. Radiation is the sum (J/m2) over the hour that ends at the
    # time stamp, so the hour starting at t is the value stamped t + 1 h.
    speed = cells(np.hypot(data[U100], data[V100]))
    wind = pd.DataFrame(wind_capacity_factor(speed), index=speed.index, columns=speed.columns)
    wind = (wind + wind.shift(-1)) / 2
    solar = cells(data[SSRD] / 3600.0).shift(-1)
    solar = pd.DataFrame(solar_capacity_factor(solar), index=solar.index, columns=solar.columns)

    berlin = wind.index.tz_convert("Europe/Berlin")
    in_year = berlin.year == args.year
    factors = {
        "wind_onshore": per_zone(wind)[in_year],
        "solar": per_zone(solar)[in_year],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    write_capacity_factors(
        factors,
        out,
        f"Hourly capacity factors per zone, {args.year} (Europe/Berlin year, hours in UTC).\n"
        "Generated using Copernicus Climate Change Service information: ERA5 hourly data on\n"
        "single levels (100 m wind, surface solar radiation downwards), read from ARCO-ERA5.\n"
        "ASSUMPTION: generic power curve and mean over the grid points of each zone\n"
        "(nearest grid point for zones that hold none: "
        + (", ".join(sorted(substitutes)) or "none")
        + ").\n"
        "Built by scripts/build_capacity_factors.py; see docs/DATA.md.",
    )
    counts = cell_zone.value_counts().sort_index()
    print(f"grid points per zone: {counts.to_dict()}; nearest point used for {substitutes}")
    print(f"mean factors: { {t: round(float(f.mean().mean()), 3) for t, f in factors.items()} }")
    print(f"wrote {out} ({len(factors['solar'])} hours)")


if __name__ == "__main__":
    main()
