"""Deduplicate records from all sources into businesses, then find chains.

Two records are the same location when they share
  * a phone number and look alike (similar name, nearby, or same street number)
  * a normalised name + postal code, or name + street number + city
  * a very similar name within ~150 m
Two locations are the same *account* (chain) when they share their own
website domain, an OSM brand, or a distinctive name.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from .categories import classify_record
from .config import Config
from .models import Business, Record
from .normalize import (
    distinctive_tokens, format_phone, haversine_m, host_of, is_platform_url, name_key,
    name_similarity, normalize_name, normalize_phone, normalize_postal, street_number,
)

# Which source wins for each field when records disagree.
SOURCE_PRIORITY = ["google", "vancouver", "osm", "csv", "orgbook"]


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for i in range(len(self.parent)):
            out[self.find(i)].append(i)
        return out


def _priority(rec: Record) -> int:
    src = rec.source.split(":")[0]
    return SOURCE_PRIORITY.index(src) if src in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def _close(a: Record, b: Record, metres: float) -> bool:
    if None in (a.lat, a.lon, b.lat, b.lon):
        return False
    return haversine_m(a.lat, a.lon, b.lat, b.lon) <= metres


def _street_no(rec: Record) -> str | None:
    return street_number(rec.street) or street_number(rec.address)


def _same_place(a: Record, b: Record) -> bool:
    sim = name_similarity(a.name, b.name)
    sa, sb = _street_no(a), _street_no(b)
    if sa and sb and sa != sb and not _close(a, b, 60):
        return False
    return sim >= 0.6 or _close(a, b, 300) or (sa is not None and sa == sb)


def dedupe(cfg: Config, records: list[Record]) -> list[list[Record]]:
    uf = UnionFind(len(records))
    by_key: dict[str, list[int]] = defaultdict(list)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)

    for i, r in enumerate(records):
        nk = name_key(r.name)
        phone = normalize_phone(r.phone)
        postal = normalize_postal(r.postal_code)
        sno = _street_no(r)
        if phone:
            by_key[f"phone:{phone}"].append(i)
        if nk and postal:
            by_key[f"np:{nk}|{postal}"].append(i)
        if nk and sno and r.city:
            by_key[f"ns:{nk}|{sno}|{r.city.lower()}"].append(i)
        if r.lat is not None and r.lon is not None:
            grid[(int(r.lat / 0.002), int(r.lon / 0.002))].append(i)

    for key, idxs in by_key.items():
        first = idxs[0]
        for j in idxs[1:]:
            if key.startswith("phone:") and not _same_place(records[first], records[j]):
                continue  # shared central booking line across a chain
            uf.union(first, j)

    for (gx, gy), idxs in grid.items():
        neighbours = [k for dx in (-1, 0, 1) for dy in (-1, 0, 1) for k in grid.get((gx + dx, gy + dy), [])]
        for i in idxs:
            for j in neighbours:
                if j <= i or uf.find(i) == uf.find(j):
                    continue
                if _close(records[i], records[j], 150) and name_similarity(records[i].name, records[j].name) >= 0.85:
                    uf.union(i, j)

    return [[records[i] for i in idxs] for idxs in uf.groups().values()]


def _pick(recs: list[Record], attr: str, prefer: list[str] | None = None):
    ordered = sorted(recs, key=_priority)
    if prefer:
        ordered = sorted(ordered, key=lambda r: prefer.index(r.source.split(":")[0]) if r.source.split(":")[0] in prefer else 99)
    for r in ordered:
        val = getattr(r, attr)
        if val not in (None, "", []):
            return val
    return None


def _franchise(cfg: Config, *names: str | None) -> str | None:
    text = " " + " ".join(normalize_name(n) for n in names if n) + " "
    for brand in cfg.franchise_brands:
        if f" {normalize_name(brand)} " in text:
            return brand
    return None


def build_business(cfg: Config, recs: list[Record]) -> Business | None:
    recs = sorted(recs, key=_priority)
    primary = recs[0]
    types = list(dict.fromkeys(t for r in recs for t in r.types))
    probe = Record(source="merge", source_id="", name=primary.name, types=types,
                   category=next((r.category for r in recs if r.category), None))
    category = classify_record(cfg, probe)
    if not category:
        for r in recs[1:]:
            category = classify_record(cfg, Record(source="merge", source_id="", name=r.name, types=types))
            if category:
                break
    if not category:
        return None

    google = next((r for r in recs if r.source == "google"), None)
    key = f"google:{google.source_id}" if google else f"{primary.source}:{primary.source_id}"
    names = list(dict.fromkeys(r.name for r in recs))
    emails = list(dict.fromkeys(r.email.lower() for r in recs if r.email))
    brand = _pick(recs, "brand")
    extra: dict = {}
    for r in reversed(recs):  # higher-priority sources overwrite
        extra.update(r.extra or {})

    status = _pick(recs, "business_status", prefer=["google", "vancouver", "csv", "osm"])
    return Business(
        key=key,
        name=primary.name,
        category=category,
        address=_pick(recs, "address"),
        street=_pick(recs, "street"),
        city=_pick(recs, "city"),
        province=_pick(recs, "province"),
        postal_code=normalize_postal(_pick(recs, "postal_code")) or _pick(recs, "postal_code"),
        lat=_pick(recs, "lat"),
        lon=_pick(recs, "lon"),
        phone=format_phone(_pick(recs, "phone")),
        website=_pick(recs, "website", prefer=["google", "csv", "osm", "vancouver"]),
        emails=emails,
        rating=_pick(recs, "rating"),
        review_count=_pick(recs, "review_count"),
        price_level=_pick(recs, "price_level"),
        business_status=status,
        num_employees=_pick(recs, "num_employees", prefer=["csv", "vancouver"]),
        brand=brand,
        franchise=_franchise(cfg, primary.name, brand, *names[1:]),
        sources=sorted({r.source for r in recs}),
        source_ids=[f"{r.source}:{r.source_id}" for r in recs],
        alt_names=names[1:],
        google_maps_url=(google.extra or {}).get("google_maps_url") if google else None,
        extra=extra,
    )


LOCATION_WORDS = {"downtown", "north", "south", "east", "west", "location", "bc", "kits", "kitsilano",
                  "yaletown", "gastown", "mall", "centre", "center", "village", "uptown", "midtown"}


def _city_words(cfg: Config) -> set[str]:
    words = set(LOCATION_WORDS)
    for city in cfg.get("region.cities", []):
        words.update(normalize_name(city).split())
    return words


def _chain_name(name: str, city_words: set[str]) -> str | None:
    """Name with location words removed: 'Fade Factory - Surrey' -> 'fadefactory'."""
    base = re.split(r"\s[-|@–]\s|\(", name)[0]
    words = [w for w in normalize_name(base).split() if w not in city_words]
    if not distinctive_tokens(" ".join(words)):
        return None  # "Hair Salon" is not a chain
    return "".join(words) or None


def _own_host(cfg: Config, b: Business) -> str | None:
    if b.website and not is_platform_url(b.website, cfg.platform_hosts):
        return host_of(b.website)
    return None


def assign_chains(cfg: Config, businesses: list[Business]) -> None:
    city_words = _city_words(cfg)
    uf = UnionFind(len(businesses))
    strong: dict[str, list[int]] = defaultdict(list)   # same own website / same brand
    by_name: dict[str, list[int]] = defaultdict(list)  # same distinctive name
    for i, b in enumerate(businesses):
        host = _own_host(cfg, b)
        if host:
            strong[f"web:{host}"].append(i)
        if b.brand:
            strong[f"brand:{normalize_name(b.brand)}"].append(i)
        cn = _chain_name(b.name, city_words)
        if cn:
            by_name[cn].append(i)
    for idxs in strong.values():
        for j in idxs[1:]:
            uf.union(idxs[0], j)
    # Same name alone is weaker: two unrelated "Luxe Nails" in different
    # cities with different websites are not one account.
    for idxs in by_name.values():
        for a_pos, a in enumerate(idxs):
            for b_idx in idxs[a_pos + 1:]:
                ba, bb = businesses[a], businesses[b_idx]
                ha, hb = _own_host(cfg, ba), _own_host(cfg, bb)
                if ba.category == bb.category and not (ha and hb and ha != hb):
                    uf.union(a, b_idx)
    used: set[str] = set()
    for root, idxs in uf.groups().items():
        if len(idxs) < 2:
            continue
        chain_key = "chain:" + (_chain_name(businesses[root].name, city_words) or businesses[root].key)
        if chain_key in used:
            chain_key = f"{chain_key}#{businesses[root].key}"
        used.add(chain_key)
        for i in idxs:
            businesses[i].chain_key = chain_key
            businesses[i].chain_locations = len(idxs)


def merge_records(cfg: Config, records: Iterable[Record]) -> list[Business]:
    province = (cfg.get("region.province") or "").upper()
    usable = [
        r for r in records
        if not r.registry_only and r.name and (not r.province or not province or r.province.upper() == province)
    ]
    businesses = [b for b in (build_business(cfg, g) for g in dedupe(cfg, usable)) if b]
    # Keys must be unique even if two groups share a primary record id.
    seen: dict[str, int] = {}
    for b in businesses:
        if b.key in seen:
            seen[b.key] += 1
            b.key = f"{b.key}#{seen[b.key]}"
        else:
            seen[b.key] = 0
    assign_chains(cfg, businesses)
    return sorted(businesses, key=lambda b: (b.city or "", b.name.lower()))
