from beauty_leads.categories import classify_record, classify_text
from beauty_leads.models import Record
from beauty_leads.normalize import (
    clean_emails, find_postal, format_phone, host_of, name_similarity, normalize_name,
    normalize_phone, street_number, url_key,
)


def test_phone_normalisation():
    assert normalize_phone("+1 (604) 555-0123") == "6045550123"
    assert normalize_phone("604.555.0123") == "6045550123"
    assert normalize_phone("555-0123") is None
    assert format_phone("16045550123") == "(604) 555-0123"


def test_names_and_similarity():
    assert normalize_name("The Fade Factory Barbershop Ltd.") == "fade factory barbershop"
    assert name_similarity("Coastline Cuts", "Coastline Cuts Barbershop") >= 0.6
    # Sharing only generic words is not a match.
    assert name_similarity("Luxe Nails", "Glow Nails") <= 0.5
    assert name_similarity("Hair & Co", "Hair and Co.") == 1.0


def test_urls_postal_street():
    assert host_of("https://www.Vagaro.com/fadehouse") == "vagaro.com"
    assert url_key("http://www.shop.ca/book/") == "shop.ca/book"
    assert url_key("shop.ca") == "shop.ca"
    assert find_postal("1420 Douglas St, Victoria, BC V8W 2G1") == "V8W 2G1"
    assert street_number("101-1420 Douglas St") == "1420"
    assert street_number("1420 Douglas St") == "1420"


def test_clean_emails_drops_junk():
    assert clean_emails(["Info@Shop.ca", "image@2x.png", "user@example.com", "info@shop.ca"]) == ["info@shop.ca"]


def test_classify_text(cfg):
    assert classify_text(cfg, "Fadehouse Barber Co.") == "barber"
    assert classify_text(cfg, "Polished Nail Bar") == "nails"
    assert classify_text(cfg, "Nail Salon") == "nails"          # 'salon' is only a weak hair signal
    assert classify_text(cfg, "Sunkissed Tan Studio") == "tanning"
    assert classify_text(cfg, "Lash Atelier") == "lash_brow"
    assert classify_text(cfg, "Hair & Nails by Kim") == "hair_salon"
    assert classify_text(cfg, "Lash Extensions Studio") == "lash_brow"
    assert classify_text(cfg, "Titan Plumbing") is None


def test_classify_record_prefers_specific_types(cfg):
    # Name says "cuts" (hair), Google says barber_shop: barber wins.
    r = Record(source="google", source_id="1", name="Coastline Cuts", types=["barber_shop"])
    assert classify_record(cfg, r) == "barber"
    # Generic beauty_salon type doesn't override a hair name.
    r = Record(source="google", source_id="2", name="Hair by Jen", types=["beauty_salon"])
    assert classify_record(cfg, r) == "hair_salon"
    # Name with no keywords falls back to the type.
    r = Record(source="google", source_id="3", name="Studio Nine", types=["nail_salon"])
    assert classify_record(cfg, r) == "nails"
    r = Record(source="osm", source_id="4", name="Glow", types=["shop=beauty"])
    assert classify_record(cfg, r) == "esthetics_spa"
    r = Record(source="osm", source_id="5", name="Bob's Hardware", types=["shop=hardware"])
    assert classify_record(cfg, r) is None
