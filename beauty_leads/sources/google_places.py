"""Google Places API (New) — Text Search.

Official, paid API (Google gives a monthly free allowance; check current
pricing at https://developers.google.com/maps/billing-and-pricing/pricing).
Requesting phone/website/rating fields bills at the "Enterprise" Text Search
rate, so every page counts. Responses are cached locally, and a per-run
request budget (`sources.google_places.max_requests_per_run`) stops runaway
costs.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
"""

from __future__ import annotations

import logging
from typing import Iterator

from ..config import Config
from ..http import HttpClient
from ..models import Record
from ..normalize import find_postal

log = logging.getLogger(__name__)

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELDS = [
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.addressComponents",
    "places.location",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.rating",
    "places.userRatingCount",
    "places.priceLevel",
    "places.businessStatus",
    "places.types",
    "places.primaryType",
    "places.googleMapsUri",
    "nextPageToken",
]




def parse_place(place: dict, province: str = "BC") -> Record | None:
    comps = {}
    for comp in place.get("addressComponents", []) or []:
        for t in comp.get("types", []):
            comps.setdefault(t, comp)
    prov = (comps.get("administrative_area_level_1") or {}).get("shortText")
    if prov and province and prov.upper() != province.upper():
        return None  # text search can return results outside the province

    street_bits = [
        (comps.get("street_number") or {}).get("longText"),
        (comps.get("route") or {}).get("longText"),
    ]
    street = " ".join(b for b in street_bits if b) or None
    city = (comps.get("locality") or comps.get("postal_town") or comps.get("administrative_area_level_3") or {}).get("longText")
    postal = (comps.get("postal_code") or {}).get("longText") or find_postal(place.get("formattedAddress"))
    loc = place.get("location") or {}
    types = list(place.get("types", []) or [])
    if place.get("primaryType") and place["primaryType"] not in types:
        types.insert(0, place["primaryType"])

    return Record(
        source="google",
        source_id=place["id"],
        name=(place.get("displayName") or {}).get("text") or "",
        types=types,
        address=place.get("formattedAddress"),
        street=street,
        city=city,
        province=prov or province,
        postal_code=postal,
        lat=loc.get("latitude"),
        lon=loc.get("longitude"),
        phone=place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber"),
        website=place.get("websiteUri"),
        rating=place.get("rating"),
        review_count=place.get("userRatingCount"),
        price_level=place.get("priceLevel"),
        business_status=place.get("businessStatus"),
        extra={"google_maps_url": place.get("googleMapsUri"), "primary_type": place.get("primaryType")},
    )


def collect(cfg: Config, http: HttpClient, cities: list[str] | None = None,
            terms: list[str] | None = None, max_requests: int | None = None, **_) -> Iterator[Record]:
    api_key = cfg.google_api_key()
    if not api_key:
        raise RuntimeError(
            "No Google API key. Create one at https://console.cloud.google.com/ (enable 'Places API (New)') "
            "and run:  export GOOGLE_MAPS_API_KEY=your-key"
        )
    province = cfg.get("region.province", "BC")
    cities = cities or cfg.get("region.cities", [])
    terms = terms or cfg.get("sources.google_places.search_terms", [])
    max_pages = int(cfg.get("sources.google_places.max_pages_per_query", 3))
    budget = int(max_requests or cfg.get("sources.google_places.max_requests_per_run", 300))
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": ",".join(FIELDS)}

    spent = 0
    seen: set[str] = set()

    def request(body: dict, use_cache: bool = True):
        nonlocal spent
        res = http.post_json(ENDPOINT, body, headers=headers, use_cache=use_cache)
        if not res.from_cache:
            spent += 1
        return res

    for city in cities:
        for term in terms:
            body = {"textQuery": f"{term} in {city}, {province}", "pageSize": 20,
                    "regionCode": "CA", "languageCode": "en"}
            page_token = None
            for page in range(max_pages):
                if spent >= budget:
                    log.warning("Google request budget (%d) reached; stopping. Raise "
                                "sources.google_places.max_requests_per_run to continue.", budget)
                    return
                req = dict(body, pageToken=page_token) if page_token else body
                res = request(req)
                if not res.ok and page_token:
                    # Cached page 1 may hold an expired page token: refresh page 1 once.
                    fresh = request(body, use_cache=False)
                    token = fresh.json().get("nextPageToken") if fresh.ok else None
                    res = request(dict(body, pageToken=token), use_cache=False) if token else fresh
                if not res.ok:
                    log.error("Google Places error for %r: %s %s", body["textQuery"], res.status, res.error or res.text[:300])
                    break
                data = res.json()
                for place in data.get("places", []) or []:
                    rec = parse_place(place, province)
                    if rec and rec.source_id not in seen:
                        seen.add(rec.source_id)
                        yield rec
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
    log.info("Google Places: %d billable requests this run, %d unique places", spent, len(seen))
