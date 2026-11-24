import json
from importlib.resources import files


def read_config() -> dict:
    """Return the scraper configuration (c2c/config.json, shipped with the package)."""
    return json.loads(files("c2c").joinpath("config.json").read_bytes())
