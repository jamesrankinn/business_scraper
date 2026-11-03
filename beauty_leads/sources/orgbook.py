"""BC Registries via OrgBook BC (free public API).

OrgBook lists every registered BC business name (sole proprietorships,
partnerships, corporations) with registration date and status, but NO
address or phone. We use it two ways:

1. `collect`: keyword-search active registrations ("barber", "nails"...) to
   build a local index of registered beauty businesses. Names that never match
   a Google/OSM/licence listing are reported as "registry-only" leads.
2. `lookup_name`: look up one business name (see enrich/registry.py) to get
   its registration date and entity type (sole prop vs corporation).

API: https://orgbook.gov.bc.ca/api/
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

from ..categories import classify_text
from ..config import Config
from ..http import HttpClient
from ..models import Record

log = logging.getLogger(__name__)

ENTITY_TYPES = {
    "SP": "Sole proprietorship",
    "GP": "General partnership",
    "BC": "BC company",
    "ULC": "Unlimited liability company",
    "CC": "Community contribution company",
    "BEN": "Benefit company",
    "CP": "Cooperative",
    "S": "Society",
    "A": "Extraprovincial company",
    "LP": "Limited partnership",
    "LL": "Limited liability partnership",
}
INCORPORATED = {"BC", "ULC", "CC", "BEN", "A", "C", "CUL", "CCC"}


def parse_topic(topic: dict) -> dict | None:
    entries = [n for n in topic.get("names", []) or [] if n.get("text")]
    # The names list also holds the CRA business number; keep it separately.
    business_number = next((n["text"] for n in entries if n.get("type") == "business_number"), None)
    names = [n["text"] for n in entries if n.get("type") != "business_number"]
    names.sort(key=lambda t: 0 if any(n.get("type") == "entity_name" and n["text"] == t for n in entries) else 1)
    if not names:
        return None
    attrs = {a.get("type"): a.get("value") for a in topic.get("attributes", []) or []}
    entity_type = attrs.get("entity_type")
    return {
        "source_id": topic.get("source_id"),
        "topic_id": topic.get("id"),
        "name": names[0],
        "all_names": names,
        "business_number": business_number,
        "registration_date": (attrs.get("registration_date") or "")[:10] or None,
        "entity_status": attrs.get("entity_status"),
        "entity_type": entity_type,
        "entity_type_label": ENTITY_TYPES.get(entity_type or "", entity_type),
        "incorporated": entity_type in INCORPORATED,
        "home_jurisdiction": attrs.get("home_jurisdiction"),
    }


def search(http: HttpClient, base: str, query: str, page: int = 1, page_size: int = 100) -> dict:
    res = http.get(f"{base}/search/topic", params={
        "q": query, "inactive": "false", "latest": "true", "revoked": "false",
        "page": page, "page_size": page_size,
    })
    if not res.ok:
        raise RuntimeError(f"OrgBook search failed: {res.status} {res.error or res.text[:200]}")
    return res.json()


def lookup_name(cfg: Config, http: HttpClient, name: str, limit: int = 10) -> list[dict]:
    base = cfg.get("sources.orgbook.base_url", "https://orgbook.gov.bc.ca/api/v4").rstrip("/")
    data = search(http, base, name, page_size=limit)
    return [p for p in (parse_topic(t) for t in data.get("results", [])) if p]


def to_record(parsed: dict, category: str) -> Record:
    return Record(
        source="orgbook",
        source_id=parsed["source_id"],
        name=parsed["name"],
        category=category,
        province="BC",
        registry_only=True,
        business_status="OPERATIONAL" if parsed.get("entity_status") == "ACT" else parsed.get("entity_status"),
        extra={k: parsed.get(k) for k in ("registration_date", "entity_type", "entity_type_label",
                                           "incorporated", "topic_id", "all_names", "business_number")},
    )


def excluded(name: str, words: list[str]) -> bool:
    tokens = set(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())
    return any(w in tokens for w in words)


def collect(cfg: Config, http: HttpClient, terms: list[str] | None = None, **_) -> Iterator[Record]:
    base = cfg.get("sources.orgbook.base_url", "https://orgbook.gov.bc.ca/api/v4").rstrip("/")
    terms = terms or cfg.get("sources.orgbook.search_terms", [])
    max_pages = int(cfg.get("sources.orgbook.max_pages_per_term", 20))
    exclude = [w.lower() for w in cfg.get("sources.orgbook.exclude_words", [])]
    seen: set[str] = set()
    for term in terms:
        for page in range(1, max_pages + 1):
            try:
                data = search(http, base, term, page=page)
            except RuntimeError as exc:
                log.warning("OrgBook %r page %d failed (%s); moving on to the next term", term, page, exc)
                break
            results = data.get("results", []) or []
            for topic in results:
                parsed = parse_topic(topic)
                if not parsed or parsed["source_id"] in seen:
                    continue
                if parsed.get("entity_status") not in (None, "ACT"):
                    continue
                category = classify_text(cfg, " ".join(parsed["all_names"]))
                if not category or excluded(" ".join(parsed["all_names"]), exclude):
                    continue  # fuzzy search returns lots of unrelated names ("Barber Motors")
                seen.add(parsed["source_id"])
                yield to_record(parsed, category)
            if not data.get("next") or not results:
                break
    log.info("OrgBook: %d active registered beauty businesses", len(seen))
