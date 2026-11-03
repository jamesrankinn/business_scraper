"""Offline demo with FICTIONAL businesses so you can see the full pipeline and
report without API keys. Every name is invented, websites use the reserved
.example domain, and phone numbers use the fictional 555-01xx range.
"""

from __future__ import annotations

from pathlib import Path

from .categories import classify_text
from .config import Config, load_config
from .db import Database
from .enrich.registry import match_registry
from .export import export_all
from .merge import merge_records
from .models import Record
from .normalize import url_key
from .scoring import Scorer

# name, google type, city, street, reviews, rating, price level, software, chain/franchise note
DEMO = [
    ("Fadehouse Barber Co.", "barber_shop", "Surrey", "10355 152 St", 612, 4.8, "MODERATE", "vagaro", None),
    ("Fadehouse Barber Co.", "barber_shop", "Langley", "20202 66 Ave", 388, 4.7, "MODERATE", "vagaro", "chain"),
    ("Fadehouse Barber Co.", "barber_shop", "Abbotsford", "32900 South Fraser Way", 201, 4.6, "MODERATE", "vagaro", "chain"),
    ("Northpoint Barbers", "barber_shop", "North Vancouver", "128 Lonsdale Ave", 455, 4.9, "MODERATE", "squire", None),
    ("Coastline Cuts", "barber_shop", "Victoria", "1420 Douglas St", 140, 4.4, "INEXPENSIVE", "booksy", None),
    ("Okanagan Gents", "barber_shop", "Kelowna", "1585 Ellis St", 298, 4.7, "MODERATE", "booksy", None),
    ("Sharp & Steady Barbershop", "barber_shop", "Kamloops", "455 Victoria St", 96, 4.5, "INEXPENSIVE", "thecut", None),
    ("Main Street Fade Room", "barber_shop", "Vancouver", "3920 Main St", 520, 4.8, "MODERATE", "square_appointments", None),
    ("Riverbend Barber Lounge", "barber_shop", "Prince George", "1188 3rd Ave", 61, 4.2, "INEXPENSIVE", "none_detected", None),
    ("Island Clippers", "barber_shop", "Nanaimo", "50 Victoria Cres", 34, 4.0, None, "no_website", None),
    ("Snip Republic Burnaby", "hair_salon", "Burnaby", "4700 Kingsway", 233, 3.9, "INEXPENSIVE", "unknown", "franchise"),
    ("Willow & Ash Hair Studio", "hair_salon", "Vancouver", "2210 W 4th Ave", 410, 4.9, "EXPENSIVE", "boulevard", None),
    ("Marigold Hair Lounge", "hair_salon", "Richmond", "8171 Ackroyd Rd", 175, 4.6, "MODERATE", "fresha", None),
    ("Tidewater Salon", "hair_salon", "Nanaimo", "6631 Island Hwy N", 122, 4.5, "MODERATE", "vagaro", None),
    ("Copper Comb Salon", "hair_salon", "Penticton", "350 Main St", 88, 4.7, "MODERATE", "glossgenius", None),
    ("Harbourlight Hair Co.", "hair_salon", "Victoria", "780 Fort St", 260, 4.6, "EXPENSIVE", "mangomint", None),
    ("Kettle Valley Hair", "hair_salon", "Vernon", "3101 30 Ave", 45, 4.3, "MODERATE", "social_only", None),
    ("Polished Nail Bar", "nail_salon", "Coquitlam", "2929 Barnet Hwy", 340, 4.4, "MODERATE", "fresha", None),
    ("Lotus Nails & Spa", "nail_salon", "Surrey", "15355 Fraser Hwy", 512, 4.2, "INEXPENSIVE", "none_detected", None),
    ("Gloss Theory Nails", "nail_salon", "Kelowna", "2271 Harvey Ave", 190, 4.8, "MODERATE", "vagaro", None),
    ("Pearl Tip Nail Studio", "nail_salon", "Chilliwack", "45610 Luckakuck Way", 72, 4.6, "MODERATE", "acuity", None),
    ("Sunkissed Tan Studio", "tanning_studio", "Langley", "6339 200 St", 156, 4.5, "MODERATE", "mindbody", None),
    ("Bronze Bay Tanning", "tanning_studio", "Kamloops", "1210 Summit Dr", 64, 4.1, "INEXPENSIVE", "none_detected", None),
    ("Lash Atelier Kits", "beauty_salon", "Vancouver", "2096 W Broadway", 230, 4.9, "EXPENSIVE", "glossgenius", None),
    ("Brow Theory", "beauty_salon", "Burnaby", "4567 Lougheed Hwy", 118, 4.7, "MODERATE", "vagaro", None),
    ("Fernwood Esthetics", "spa", "Victoria", "1300 Gladstone Ave", 84, 4.8, "MODERATE", "jane", None),
    ("Glacier Skin Studio", "skin_care_clinic", "Whistler", "4314 Main St", 97, 4.6, "VERY_EXPENSIVE", "boulevard", None),
    ("Cedar Wax Room", "beauty_salon", "Squamish", "38085 2nd Ave", 52, 4.9, "MODERATE", "square_appointments", None),
    ("Blush & Bloom Beauty", "beauty_salon", "Maple Ridge", "22709 Lougheed Hwy", 39, 4.4, None, "social_only", None),
    ("Harmony Day Spa", "spa", "Courtenay", "2401 Cliffe Ave", 143, 4.5, "EXPENSIVE", "booker", None),
    ("Zen Den Massage & Spa", "spa", "Port Coquitlam", "2850 Shaughnessy St", 18, 3.2, None, "none_detected", None),
]

FINGERPRINT_URL = {
    "vagaro": "https://www.vagaro.com/{slug}",
    "fresha": "https://www.fresha.com/a/{slug}",
    "booksy": "https://booksy.com/en-ca/{slug}",
    "square_appointments": "https://book.squareup.com/appointments/{slug}",
    "glossgenius": "https://{slug}.glossgenius.com",
    "squire": "https://getsquire.com/booking/book/{slug}",
    "thecut": "https://thecut.co/{slug}",
    "boulevard": "https://www.joinblvd.com/b/{slug}",
    "mangomint": "https://booking.mangomint.com/{slug}",
    "mindbody": "https://clients.mindbodyonline.com/{slug}",
    "acuity": "https://{slug}.as.me",
    "jane": "https://{slug}.janeapp.com",
    "booker": "https://go.booker.com/location/{slug}",
}
AREA = {"Victoria": "250", "Nanaimo": "250", "Kelowna": "250", "Kamloops": "250", "Penticton": "250",
        "Vernon": "250", "Prince George": "250", "Courtenay": "250"}


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-").replace("--", "-")


def build_demo(cfg: Config, db: Database) -> None:
    records, checks, registry = [], {}, []
    for i, (name, gtype, city, street, reviews, rating, price, software, note) in enumerate(DEMO, 1):
        slug = _slug(name)  # chain locations share a website, which is how chains are detected
        website = None if software == "no_website" else (
            f"https://instagram.com/{slug.replace('-', '')}" if software == "social_only" else f"https://{slug}.example")
        phone = f"({AREA.get(city, '604')}) 555-01{i:02d}"
        records.append(Record(
            source="google", source_id=f"demo-{i:03d}", name=name, types=[gtype, "point_of_interest"],
            address=f"{street}, {city}, BC", street=street, city=city, province="BC",
            phone=phone, website=website, rating=rating, review_count=reviews,
            price_level=f"PRICE_LEVEL_{price}" if price else None, business_status="OPERATIONAL",
            extra={"google_maps_url": None},
        ))
        if website and software not in ("social_only",):
            key = url_key(website)
            if software == "unknown":
                checks[key] = {"status": "fetch_failed", "software": "unknown", "error": "timeout"}
            elif software == "none_detected":
                checks[key] = {"status": "ok", "software": "none_detected", "confidence": None, "evidence": [],
                               "site_builder": "wix", "socials": {}, "emails": [f"hello@{slug}.example"]}
            else:
                url = FINGERPRINT_URL[software].format(slug=slug)
                checks[key] = {"status": "ok", "software": software, "confidence": "high",
                               "evidence": [f'"Book now" button: {url}'], "site_builder": "squarespace",
                               "socials": {"instagram": f"https://instagram.com/{slug.replace('-', '')}"},
                               "emails": [f"bookings@{slug}.example"] if i % 3 else []}
        if i % 4 != 0 and name.upper() not in {r.name for r in registry}:
            year = 2008 + (i * 7) % 17
            registry.append(Record(
                source="orgbook", source_id=f"FM-DEMO-{i:04d}", name=name.upper(),
                category=classify_text(cfg, name),
                province="BC", registry_only=True,
                extra={"registration_date": f"{year}-0{1 + i % 9}-15", "entity_type": "BC" if i % 3 == 0 else "SP",
                       "entity_type_label": "BC company" if i % 3 == 0 else "Sole proprietorship",
                       "incorporated": i % 3 == 0, "all_names": [name.upper()]},
            ))

    # A Vancouver business licence for one shop: same place, adds employee count.
    records.append(Record(source="vancouver", source_id="DEMO-LIC-1", name="Main Street Fade Room",
                          types=["Personal Services", "Barber Shop"], street="3920 Main St", city="Vancouver",
                          province="BC", postal_code="V5V 3P2", num_employees=7, business_status="OPERATIONAL",
                          extra={"legal_name": "MAIN STREET FADE ROOM LTD."}))
    # An OSM duplicate of a Google listing: should merge, not double count.
    records.append(Record(source="osm", source_id="node/demo-1", name="Coastline Cuts Barbershop",
                          types=["shop=hairdresser", "hairdresser=barber"], street="1420 Douglas St",
                          city="Victoria", province="BC", phone="250 555 0105"))
    # Registered names with no listing anywhere: shown as registry-only leads.
    for j, nm in enumerate(["PRAIRIE ROSE NAILS", "SILVERLINE BARBERING", "VELVET BROW STUDIO"], 1):
        registry.append(Record(source="orgbook", source_id=f"FM-DEMO-R{j}", name=nm, province="BC",
                               registry_only=True, category=classify_text(cfg, nm),
                               extra={"registration_date": f"2025-0{j}-02", "entity_type_label": "Sole proprietorship"}))

    db.upsert_records(records + registry)
    db.replace_businesses(merge_records(cfg, db.iter_records()))
    for key, data in checks.items():
        db.save_website_check(key, data)
    match_registry(cfg, db)


def run_demo(out_dir: str | Path | None = None, config_path: str | None = None) -> dict:
    cfg = load_config(config_path)
    cfg.raw.setdefault("franchise_brands", []).append("snip republic")  # fictional franchise for the demo
    db_path = cfg.base_dir / "data" / "demo.db"
    if db_path.exists():
        db_path.unlink()
    with Database(db_path) as db:
        build_demo(cfg, db)
        scorer = Scorer(cfg, db.website_checks(), db.registry_matches())
        assessments = scorer.assess_all(db.iter_businesses())
        return export_all(cfg, assessments, out_dir or cfg.base_dir / "reports" / "demo", db=db, demo=True)
