"""Helpers extracting route fields from camptocamp API documents."""
import json
from typing import List

from pyproj import Transformer


def to_latlon(x, y, source_epsg=3857):
    """
    Convert projected coordinates (x, y) to latitude and longitude.

    Parameters:
        x, y: coordinates in meters
        source_epsg: EPSG code of the input projection (default 3857, Web Mercator, used by camptocamp)

    Returns:
        (latitude, longitude) in decimal degrees
    """
    transformer = Transformer.from_crs(f"EPSG:{source_epsg}", "EPSG:4326", always_xy=True)
    lon, lat = transformer.transform(x, y)
    return lat, lon

def filter_area(area: dict) -> bool:
    if area.get('area_type', '')=='country':
        return True
    return False

def filter_langs(lang: dict) -> bool:
    if lang.get('lang', '')=='fr':
        return True
    return False


def get_geo_coordinates(routeData: dict) -> tuple:
    geometry = routeData.get("geometry", {}) or {}
    geom = geometry.get("geom")

    coords = None
    if geom:
        try:
            coords = json.loads(geom).get("coordinates")
        except (json.JSONDecodeError, TypeError):
            coords = None
    if coords:
        lat, lon = to_latlon(*coords)
    else:
        lat, lon = None, None

    return lon, lat

def get_countries_list(route: dict) -> List:
    
    areas = route.get("areas", {}) or {}
    admin = filter(filter_area,  areas)

    countries = []
    for adm in list(admin):
        lang = list(filter(filter_langs, adm.get('locales', [])))
        name = None
        if len(lang)>0:
            name = lang[0].get('title', None) 
        countries.append({"country_id":adm['document_id'], "countryName":name})
    
    return countries


def get_title(route: dict) -> str:
    
    locales = route.get("locales", {}) or {}
    locales = list(filter(filter_langs,  locales)) # filter_langs does the right job in this case: keep only the french version

    waypoint = route.get("associations", {}).get("waypoints", [{}])[0].get("locales", [])  #first waypoint should be the main (not always true?)
    waypoint = list(filter(filter_langs,  waypoint))
    
    if (len(waypoint)==0) & (len(locales)==0):
        return ''

    if len(locales)==0:
        return waypoint[0].get("title", "")
    
    if len(waypoint)==0:
        return locales[0].get("title", "")
    
    return ', '.join([waypoint[0].get("title", ""), locales[0].get("title", "")])
