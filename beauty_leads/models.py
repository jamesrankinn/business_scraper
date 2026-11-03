"""Data models shared across sources, enrichment and scoring."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class Record:
    """One listing from one source (a Google place, an OSM node, a licence...)."""

    source: str
    source_id: str
    name: str
    category: str | None = None          # our category key, if the source tells us
    types: list[str] = field(default_factory=list)   # raw source types/tags
    address: str | None = None
    street: str | None = None
    city: str | None = None
    province: str | None = None
    postal_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    phone: str | None = None
    website: str | None = None
    email: str | None = None
    rating: float | None = None
    review_count: int | None = None
    price_level: str | None = None
    business_status: str | None = None   # OPERATIONAL / CLOSED_TEMPORARILY / CLOSED_PERMANENTLY
    num_employees: int | None = None
    brand: str | None = None             # franchise brand (OSM brand tag etc.)
    registry_only: bool = False          # e.g. OrgBook: a name with no address/contact
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Business:
    """A deduplicated business location built from one or more Records."""

    key: str
    name: str
    category: str
    address: str | None = None
    street: str | None = None
    city: str | None = None
    province: str | None = None
    postal_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    phone: str | None = None
    website: str | None = None
    emails: list[str] = field(default_factory=list)
    rating: float | None = None
    review_count: int | None = None
    price_level: str | None = None
    business_status: str | None = None
    num_employees: int | None = None
    brand: str | None = None
    franchise: str | None = None
    chain_key: str | None = None
    chain_locations: int = 1
    sources: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    alt_names: list[str] = field(default_factory=list)
    google_maps_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Business":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
