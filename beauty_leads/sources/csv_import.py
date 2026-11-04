"""Import any CSV of businesses: other cities' licence data, lists you've
bought or exported, notes from door-knocking, etc.

Columns are auto-detected from common header names (case-insensitive).
Unknown columns are kept in `extra`.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Iterator

from ..config import Config
from ..models import Record

COLUMN_ALIASES = {
    "name": ["name", "business name", "business", "company", "trade name", "businesstradename", "dba", "businessname"],
    "category": ["category", "type", "business type", "businesstype", "industry"],
    "address": ["address", "full address", "location"],
    "street": ["street", "street address", "address line 1", "address1"],
    "city": ["city", "municipality", "town"],
    "province": ["province", "state", "prov"],
    "postal_code": ["postal code", "postal", "postcode", "zip", "postalcode"],
    "phone": ["phone", "telephone", "phone number", "tel"],
    "website": ["website", "url", "web", "site"],
    "email": ["email", "e-mail", "email address"],
    "instagram": ["instagram", "ig", "instagram handle", "insta"],
    "num_employees": ["employees", "number of employees", "numberofemployees", "staff", "chairs"],
    "rating": ["rating", "stars"],
    "review_count": ["reviews", "review count", "number of reviews"],
    "software": ["software", "booking software", "current software", "system"],
    "monthly_spend": ["monthly spend", "current spend", "pays per month", "software cost"],
}


def _detect(headers: list[str]) -> dict[str, str]:
    lower = {h.strip().lower(): h for h in headers}
    mapping = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower:
                mapping[field] = lower[alias]
                break
    return mapping


def _num(value, cast=float):
    try:
        return cast(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def collect(cfg: Config, http=None, file: str | None = None, **_) -> Iterator[Record]:
    if not file:
        raise ValueError("csv source needs --file path/to/list.csv")
    path = Path(file)
    source = f"csv:{path.stem}"
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        mapping = _detect(reader.fieldnames or [])
        if "name" not in mapping:
            raise ValueError(f"Couldn't find a business-name column in {path}. Headers: {reader.fieldnames}")
        used = set(mapping.values())
        for i, row in enumerate(reader):
            get = lambda f: (row.get(mapping[f]) or "").strip() if f in mapping else ""  # noqa: E731
            name = get("name")
            if not name:
                continue
            ident = hashlib.sha1(f"{name}|{get('address')}|{get('street')}|{get('phone')}".encode()).hexdigest()[:12]
            extra = {k: v for k, v in row.items() if k not in used and v}
            # Ground truth from calls beats any estimate: keep it for scoring.
            if get("software"):
                extra["known_software"] = get("software")
            if get("instagram"):
                extra["instagram"] = get("instagram")
            if get("monthly_spend"):
                extra["known_monthly_spend_cad"] = _num(get("monthly_spend"))
            yield Record(
                source=source,
                source_id=ident,
                name=name,
                types=[get("category")] if get("category") else [],
                address=get("address") or None,
                street=get("street") or None,
                city=get("city") or None,
                province=get("province") or cfg.get("region.province", "BC"),
                postal_code=get("postal_code") or None,
                phone=get("phone") or None,
                website=get("website") or None,
                email=get("email") or None,
                num_employees=_num(get("num_employees"), int) if get("num_employees") else None,
                rating=_num(get("rating")) if get("rating") else None,
                review_count=_num(get("review_count"), int) if get("review_count") else None,
                extra=extra,
            )
