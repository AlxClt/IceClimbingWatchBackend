"""Database helpers for the camptocamp scraper (routes, outings and their associations)."""
import datetime
from functools import partial
from typing import Optional, Set, List, Literal, Dict, Tuple

from sqlalchemy import Engine
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, select, text

from c2c.config import read_config
from c2c.schema import (
    Base,
    Routes,
    Orientations,
    Outings,
    Countries,
)

def get_engine(dbstring: str) -> Engine:
    engine = create_engine(dbstring)
    return engine

def create_db(dbstring) -> None:

    engine = create_engine(dbstring)
    Base.metadata.create_all(engine, checkfirst=True)
    populate_orientation_table(engine)

def get_obj(Obj, idColumn: str, session: Session, objData: dict):

    obj = session.scalar(
        select(Obj).where(getattr(Obj, idColumn) == objData[idColumn])
    )
    if not obj:
        obj = Obj(**objData)
        session.add(obj)

    return obj

# -----------------------
# Routes data
# -----------------------

def populate_orientation_table(engine) -> None:

    with Session(engine) as session:

        conf = read_config()
        
        for orient in conf['orientations']:
            session.add(Orientations(orientation=orient))
        
        session.commit()
    
    return

get_country = partial(get_obj, Countries, "countryName")
get_outings = partial(get_obj, Outings, "outing_id")
get_route = partial(get_obj, Routes, "route_id")

def get_orientation(session: Session, orientation: str) -> Orientations:

    orientation_obj = session.scalar(
            select(Orientations).where(Orientations.orientation == orientation)
        )
    if not orientation_obj:
        orientation_obj = Orientations(orientation=orientation)
        session.add(orientation_obj)
    
    return orientation_obj

def insert_route(session: Session,
                 route_id: int,
                 name: str,
                 lat: float | None = None,
                 lon: float | None = None,
                 snow_ice_mixed: int | None = None,
                 mountain_climbing: int | None = None,
                 ice_climbing: int | None = None,
                 elevation_min: int | None = None,
                 elevation_max: int | None = None,
                 difficulties_height: int | None = None,
                 height_diff_difficulties: int | None = None,
                 glacier: str | None = None,
                 global_rating: str | None = None,
                 ice_rating: str | None = None,
                 mixed_rating: str | None = None,
                 rock_free_rating: str | None = None, 
                 orientations: List = [],
                 outings: List = [],
                 countries: List =  [],
                 last_updated: str | None = None
                 ) -> None:

    # Retrieving orientations
    orientationsList: List[Orientations] = []
    for orient in orientations:
        orientationsList.append(get_orientation(session, orient))

    
    # Retrieving countries
    countriesList: List[Countries] = []
    for countryData in countries:
        countriesList.append(get_country(session, countryData))

    # Retrieving outings
    outingsList: List[Outings] = []
    for outingData in outings:
        outingsList.append(get_outings(session, outingData))

    route = Routes(
        route_id=route_id,
        name=name,
        lat=lat,
        lon=lon,
        snow_ice_mixed=snow_ice_mixed,
        mountain_climbing=mountain_climbing,
        ice_climbing=ice_climbing,
        elevation_min=elevation_min,
        elevation_max=elevation_max,
        difficulties_height=difficulties_height,
        height_diff_difficulties=height_diff_difficulties,
        glacier=glacier,
        global_rating=global_rating,
        ice_rating=ice_rating,
        mixed_rating=mixed_rating,
        rock_free_rating=rock_free_rating, 
        orientations=orientationsList,
        outings=outingsList,
        countries = countriesList,
        last_updated = last_updated
     )
    
    session.add(route)
    session.commit()

def insert_outing(session: Session,
                  outing_id: int,
                  date: str,
                  conditions: str | None,
                  last_updated: str | None = None,
                  routes: List = []
                 ) -> None:

    """
    Mainly used to add an outing to a previously parsed route
    """

    # Retrieving routes 
    routesList: List[Routes] = []
    for routeData in routes:
        routesList.append(get_route(session, routeData))

    outing = Outings(
        outing_id=outing_id,
        date=date,
        conditions=conditions,
        last_updated=last_updated,
        routes = routesList
     )
    
    session.add(outing)
    session.commit()

def load_scraped_routes_ids(engine: Engine, min_date: Optional[datetime.datetime]) -> Set[int]:
    """Return set of route IDs updated after min_date (or all if None)."""
    query = "SELECT route_id FROM Routes"
    if min_date:
        query += " WHERE last_updated >= :min_date AND last_updated IS NOT NULL"

    route_ids: Set[int] = set()
    with engine.connect() as conn:
        result = conn.execute(text(query), {"min_date": min_date.strftime("%Y-%m-%d")} if min_date else {})
        route_ids = {row.route_id for row in result}

    return route_ids

def load_scraped_outings_ids(engine: Engine, min_date: Optional[datetime.datetime], mode: Literal['update_date', 'outing_date']) -> Set[int]:
    """Return set of route IDs updated after min_date, or outings that happened after min_date"""

    if mode not in {'update_date', 'outing_date'}:
        raise ValueError("mode must be either 'update_date' or 'outing_date'")
        
    query = "SELECT outing_id FROM Outings"
    if min_date: 
        if mode=='update_date':
            query += " WHERE last_updated >= :min_date AND last_updated IS NOT NULL"
        else:
            query += " WHERE date >= :min_date"

    outings_ids: Set[int] = set()
    with engine.connect() as conn:
        result = conn.execute(text(query), {"min_date": min_date.strftime("%Y-%m-%d")} if min_date else {})
        outings_ids = {row.outing_id for row in result}

    return outings_ids

def load_outings_for_refresh(engine: Engine, min_date: datetime.datetime) -> Dict[int, Tuple[str, Optional[str]]]:
    """Return {outing_id: (date, conditions)} for outings whose date is >= min_date"""
    query = "SELECT outing_id, date, conditions FROM Outings WHERE date >= :min_date"

    with engine.connect() as conn:
        result = conn.execute(text(query), {"min_date": min_date.strftime("%Y-%m-%d")})
        outings = {row.outing_id: (row.date, row.conditions) for row in result}

    return outings

def update_outing(session: Session,
                  outing_id: int,
                  date: str,
                  conditions: str | None,
                  last_updated: str | None = None
                  ) -> bool:
    """
    Update the scalar fields of an existing outing (route associations are left untouched).
    Returns False if the outing does not exist.
    """
    outing = session.get(Outings, outing_id)
    if outing is None:
        return False

    outing.date = date
    outing.conditions = conditions
    outing.last_updated = last_updated
    session.commit()

    return True

def get_last_outing_date(engine: Engine) -> Optional[datetime.datetime]:
    """Return the date of the most recent outing in db, or None if there are no outings"""
    query = "SELECT MAX(date) FROM Outings"

    with engine.connect() as conn:
        result_str = conn.execute(text(query)).scalar_one()

    if result_str is None:
        return None

    return datetime.datetime.strptime(result_str, "%Y-%m-%d")

def check_route_existence(engine: Engine, route_id: int) -> bool:
    """Checks whether the route exists in db"""
    query = "SELECT route_id FROM Routes WHERE route_id = :route_id"

    with engine.connect() as conn:
        route = conn.execute(text(query), {"route_id": route_id})

    result = route.first() is not None

    return result

def check_outing_existence(engine: Engine, outing_id: int) -> bool:

    """Checks whether the outing exists in db"""
    query = "SELECT outing_id FROM Outings WHERE outing_id = :outing_id"

    outings_ids: Set[int] = set()
    with engine.connect() as conn:
        result = conn.execute(text(query), {"outing_id": outing_id})
        outings_ids = {row.outing_id for row in result}

    result = (len(outings_ids)>0)
    return result
