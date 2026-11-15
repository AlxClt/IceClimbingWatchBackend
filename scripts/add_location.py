"""Script for adding a new location to the database. Optionally triggers the data update, otherwise it will be picked up in next run

Example: python scripts/add_location.py --name="La Grave" --country=FR --lat=45.045574 --lon=6.30621 --ground_elevation_m=1493 
"""
import sys
import argparse
from pathlib import Path

from update_ice_weather_db import open_db, run, DEFAULT_DB

def add_location(db_path: Path,
                 name: str, 
                 country: str,
                 lat: float, 
                 lon: float, 
                 ground_elevation_m: int,
                 forecasted_values_source_id: int | None, 
                 measured_values_source_id: int | None):

    try:
        assert country in ('CH', 'FR', 'IT')
    except AssertionError:
        raise ValueError(f"Value for country must be among ('CH', 'FR', 'IT'). Provided value: {country}")

    conn = open_db(db_path)

    if forecasted_values_source_id is None:
        pass 
    else:
        assert type(forecasted_values_source_id)==int
        forecasted_values_source_id_exists = conn.execute(
                f"SELECT 1 FROM sources WHERE source_id={forecasted_values_source_id}"
            ).fetchone()
        if not forecasted_values_source_id_exists:
            raise ValueError("forecasted_values_source_id must correspond to an existing source in the source table")

    if measured_values_source_id is None:
        pass 
    else:
        assert type(measured_values_source_id)==int
        measured_values_source_id_exists = conn.execute(
                f"SELECT 1 FROM sources WHERE source_id={measured_values_source_id}"
            ).fetchone()
        if not measured_values_source_id_exists:
            raise ValueError("measured_values_source_id must correspond to an existing source in the source table")
  
    values = {
        "name": name,
        "country": country,
        "latitude": lat,
        "longitude": lon,
        "ground_elevation_m": ground_elevation_m,
        "forecasted_values_source_id": forecasted_values_source_id,
        "measured_values_source_id": measured_values_source_id,
    }
    values = {k: v for k, v in values.items() if v is not None}
    with conn: 
        conn.execute(
            f"INSERT INTO locations ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
    conn.close()
    return


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite file (default: {DEFAULT_DB})")
    p.add_argument("--name", type=str, help="Location name")
    p.add_argument("--country", type=str, help="Location country (FR, CH, IT)")
    p.add_argument("--lat", type=float, help="Location latitude")
    p.add_argument("--lon", type=float, help="Location longitude")
    p.add_argument("--ground_elevation_m", type=int, help="Location altitude")
    p.add_argument("--forecasted_values_source_id", type=int, default=None, help="source id for forecast data (must exist in source db)")
    p.add_argument("--measured_values_source_id", type=int, default=None, help="source id for measured data (must exist in source db)")

    args = p.parse_args()


    add_location(db_path=args.db,
                name=args.name, 
                country=args.country,
                lat=args.lat, 
                lon=args.lon, 
                ground_elevation_m=args.ground_elevation_m,
                forecasted_values_source_id=args.forecasted_values_source_id, 
                measured_values_source_id=args.measured_values_source_id)


if __name__ == "__main__":
    main()
