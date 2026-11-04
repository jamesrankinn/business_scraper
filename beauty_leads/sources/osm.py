"""OpenStreetMap via the Overpass API (free, no key).

Coverage is patchier than Google but it's free and often includes website,
phone and a `brand` tag (handy for spotting franchises). Please keep usage
light: one province-wide query per run is plenty.
"""

from __future__ import annotations

import logging
from typing import Iterator

from ..config import Config
from ..http import HttpClient
from ..models import Record

log = logging.getLogger(__name__)

QUERY = """
[out:json][timeout:300];
area["ISO3166-2"="{iso}"]->.a;
(
  nwr["shop"="hairdresser"](area.a);
  nwr["shop"="beauty"](area.a);
  nwr["shop"="tanning"](area.a);
  nwr["leisure"="tanning_salon"](area.a);
  nwr["beauty"](area.a);
  nwr["shop"="massage"](area.a);
  nwr["shop"="tattoo"](area.a);
  nwr["healthcare:speciality"~"massage|cosmetic|aesthetic"](area.a);
);
out center tags;
"""

TYPE_TAGS = ("shop", "beauty", "hairdresser", "leisure", "amenity", "healthcare", "healthcare:speciality")


def _first(tags: dict, *keys: str) -> str | None:
    for key in keys:
        if tags.get(key):
            return tags[key].split(";")[0].strip()
    return None


def parse_element(el: dict, province: str = "BC") -> Record | None:
    tags = el.get("tags") or {}
    name = tags.get("name")
    if not name:
        return None
    lat = el.get("lat", (el.get("center") or {}).get("lat"))
    lon = el.get("lon", (el.get("center") or {}).get("lon"))
    house, street = tags.get("addr:housenumber"), tags.get("addr:street")
    unit = tags.get("addr:unit")
    street_line = " ".join(x for x in (house, street) if x) or None
    if unit and street_line:
        street_line = f"{unit}-{street_line}"
    city = tags.get("addr:city")
    postal = tags.get("addr:postcode")
    address = ", ".join(x for x in (street_line, city, tags.get("addr:province") or province, postal) if x) or None
    types = [f"{k}={v}" for k in TYPE_TAGS for v in (tags.get(k, "").split(";") if tags.get(k) else []) if v]

    return Record(
        source="osm",
        source_id=f"{el.get('type', 'node')}/{el.get('id')}",
        name=name,
        types=types,
        address=address,
        street=street_line,
        city=city,
        province=tags.get("addr:province") or province,
        postal_code=postal,
        lat=lat,
        lon=lon,
        phone=_first(tags, "phone", "contact:phone"),
        website=_first(tags, "website", "contact:website", "url"),
        email=_first(tags, "email", "contact:email"),
        brand=tags.get("brand"),
        extra={k: tags[k] for k in ("contact:instagram", "contact:facebook", "opening_hours", "brand:wikidata") if k in tags},
    )


def collect(cfg: Config, http: HttpClient, **_) -> Iterator[Record]:
    url = cfg.get("sources.osm.overpass_url", "https://overpass-api.de/api/interpreter")
    iso = cfg.get("sources.osm.area_iso", "CA-BC")
    province = cfg.get("region.province", "BC")
    res = http.get(url, params={"data": QUERY.format(iso=iso)})
    if not res.ok:
        raise RuntimeError(f"Overpass query failed: {res.status} {res.error or res.text[:300]}")
    elements = res.json().get("elements", [])
    count = 0
    for el in elements:
        rec = parse_element(el, province)
        if rec:
            count += 1
            yield rec
    log.info("OSM: %d named beauty places", count)
