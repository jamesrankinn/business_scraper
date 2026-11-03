"""Data sources. Each module exposes `collect(cfg, http, **options) -> Iterator[Record]`."""

from . import csv_import, google_places, orgbook, osm, vancouver

SOURCES = {
    "google": google_places,
    "osm": osm,
    "vancouver": vancouver,
    "orgbook": orgbook,
    "csv": csv_import,
}
