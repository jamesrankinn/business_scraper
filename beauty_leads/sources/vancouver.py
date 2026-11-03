"""City of Vancouver business licences (Opendatasoft open data portal).

Free, no key, and includes `numberofemployees` — the best staff-size signal
we have. The City consolidated licence categories in May 2024, so instead of
hard-coding category names we read the live list of business types/subtypes
(facets) and keep the ones matching `sources.vancouver_licences.type_keywords`.

Portal: https://opendata.vancouver.ca/explore/dataset/business-licences/
"""

from __future__ import annotations

import logging
from typing import Iterator

from ..config import Config
from ..http import HttpClient
from ..models import Record

log = logging.getLogger(__name__)


def _odsql_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def matching_facets(facets_json: dict, keywords: list[str]) -> dict[str, list[str]]:
    """{'businesstype': [...], 'businesssubtype': [...]} values matching keywords."""
    kws = [k.lower() for k in keywords]
    out: dict[str, list[str]] = {}
    for facet in facets_json.get("facets", []) or []:
        values = []
        for item in facet.get("facets", []) or []:
            value = item.get("value") or item.get("name") or ""
            if any(k in value.lower() for k in kws):
                values.append(value)
        if values:
            out[facet.get("name")] = values
    return out


def _int(value) -> int | None:
    try:
        n = int(float(value))
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None


def parse_licence(row: dict, province: str = "BC") -> Record | None:
    name = (row.get("businesstradename") or "").strip() or (row.get("businessname") or "").strip()
    if not name:
        return None
    unit, house, street = row.get("unit"), row.get("house"), row.get("street")
    street_line = " ".join(str(x) for x in (house, street) if x) or None
    if unit and street_line:
        street_line = f"{unit}-{street_line}"
    city = row.get("city") or "Vancouver"
    postal = row.get("postalcode")
    geo = row.get("geo_point_2d") or {}
    source_id = str(row.get("licencenumber") or row.get("licencersn") or f"{name}|{street_line}")
    types = [t for t in (row.get("businesstype"), row.get("businesssubtype")) if t]
    return Record(
        source="vancouver",
        source_id=source_id,
        name=name,
        types=types,
        address=", ".join(x for x in (street_line, city, row.get("province") or province, postal) if x) or None,
        street=street_line,
        city=city,
        province=row.get("province") or province,
        postal_code=postal,
        lat=geo.get("lat"),
        lon=geo.get("lon"),
        num_employees=_int(row.get("numberofemployees")),
        business_status="OPERATIONAL" if (row.get("status") or "").lower() == "issued" else row.get("status"),
        extra={
            "legal_name": row.get("businessname"),
            "licence_status": row.get("status"),
            "licence_year": row.get("folderyear"),
            "licence_type": row.get("businesstype"),
            "licence_subtype": row.get("businesssubtype"),
            "local_area": row.get("localarea"),
        },
    )


def collect(cfg: Config, http: HttpClient, **_) -> Iterator[Record]:
    base = cfg.get("sources.vancouver_licences.base_url").rstrip("/")
    dataset = cfg.get("sources.vancouver_licences.dataset", "business-licences")
    keywords = cfg.get("sources.vancouver_licences.type_keywords", [])
    only_status = cfg.get("sources.vancouver_licences.only_status")
    province = cfg.get("region.province", "BC")

    res = http.get(f"{base}/{dataset}/facets", params=[("facet", "businesstype"), ("facet", "businesssubtype")])
    if not res.ok:
        raise RuntimeError(f"Vancouver facets request failed: {res.status} {res.error or res.text[:300]}")
    facets = res.json()
    matches = matching_facets(facets, keywords)
    if not matches:
        available = [i.get("value") for f in facets.get("facets", []) for i in f.get("facets", [])][:60]
        raise RuntimeError(
            "No Vancouver licence types matched sources.vancouver_licences.type_keywords. "
            f"Types the City publishes include: {available}. Add the beauty-related ones to the keywords."
        )
    log.info("Vancouver licence types matched: %s", matches)

    best: dict[str, tuple[str, Record]] = {}
    for field_name, values in matches.items():
        for value in values:
            where = f"{field_name}={_odsql_str(value)}"
            if only_status:
                where += f" AND status={_odsql_str(only_status)}"
            res = http.get(f"{base}/{dataset}/exports/json", params={"where": where})
            if not res.ok:
                log.error("Vancouver export failed for %s=%s: %s", field_name, value, res.status)
                continue
            for row in res.json():
                rec = parse_licence(row, province)
                if not rec:
                    continue
                year = str(row.get("folderyear") or "")
                prev = best.get(rec.source_id)
                if prev is None or year > prev[0]:
                    best[rec.source_id] = (year, rec)
    log.info("Vancouver: %d licences", len(best))
    for _, rec in best.values():
        yield rec
