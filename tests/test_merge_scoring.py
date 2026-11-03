from datetime import date

import pytest

from beauty_leads.merge import merge_records
from beauty_leads.models import Business, Record
from beauty_leads.scoring import Scorer, accounts, market_summary, resolve_known_software, software_monthly_price


def g(i, name, city="Surrey", street="100 Main St", phone=None, website=None, types=("barber_shop",), **kw):
    return Record(source="google", source_id=f"g{i}", name=name, city=city, street=street,
                  address=f"{street}, {city}, BC", province="BC", phone=phone, website=website,
                  types=list(types), **kw)


def test_dedupe_across_sources(cfg):
    recs = [
        g(1, "Coastline Cuts", city="Victoria", street="1420 Douglas St", phone="(250) 555-0105", review_count=140),
        Record(source="osm", source_id="node/1", name="Coastline Cuts Barbershop", city="Victoria",
               street="1420 Douglas St", province="BC", phone="+1 250 555 0105", types=["shop=hairdresser"]),
        Record(source="vancouver", source_id="L1", name="Main Street Fade Room", city="Vancouver",
               street="3920 Main St", province="BC", num_employees=7, types=["Barber Shop"]),
        g(2, "Main Street Fade Room", city="Vancouver", street="3920 Main St"),
        Record(source="orgbook", source_id="FM1", name="SOME BARBER", registry_only=True),
        g(3, "Walmart Supercentre", types=["department_store"]),
    ]
    biz = merge_records(cfg, recs)
    names = sorted(b.name for b in biz)
    assert names == ["Coastline Cuts", "Main Street Fade Room"]
    fade = next(b for b in biz if b.name == "Main Street Fade Room")
    assert fade.num_employees == 7 and set(fade.sources) == {"google", "vancouver"}
    coast = next(b for b in biz if b.name == "Coastline Cuts")
    assert coast.category == "barber" and coast.phone == "(250) 555-0105"


def test_shared_phone_different_address_not_merged(cfg):
    recs = [g(1, "Fadehouse Barber Co.", street="10355 152 St", phone="604-555-0101"),
            g(2, "Fadehouse Barber Co.", city="Langley", street="20202 66 Ave", phone="604-555-0101")]
    biz = merge_records(cfg, recs)
    assert len(biz) == 2
    assert all(b.chain_locations == 2 and b.chain_key for b in biz)


def test_chains_and_franchises(cfg):
    recs = [
        g(1, "Fadehouse Barber Co. - Surrey", website="https://fadehouse.ca"),
        g(2, "Fadehouse Barber Co.", city="Langley", street="5 Fraser Hwy", website="https://fadehouse.ca/langley"),
        # Same name, different own websites: unrelated businesses.
        g(3, "Luxe Nails", city="Kelowna", street="1 A St", website="https://luxenails-kelowna.ca", types=["nail_salon"]),
        g(4, "Luxe Nails", city="Vancouver", street="2 B St", website="https://luxe-van.ca", types=["nail_salon"]),
        g(5, "Great Clips", city="Burnaby", street="9 C St", types=["hair_salon"]),
        # Generic names are never chains.
        g(6, "Hair Salon", city="Delta", street="1 D St", types=["hair_salon"]),
        g(7, "Hair Salon", city="Hope", street="1 E St", types=["hair_salon"]),
    ]
    biz = {(b.name, b.city): b for b in merge_records(cfg, recs)}
    assert biz[("Fadehouse Barber Co. - Surrey", "Surrey")].chain_locations == 2
    assert biz[("Luxe Nails", "Kelowna")].chain_locations == 1
    assert biz[("Great Clips", "Burnaby")].franchise == "great clips"
    assert biz[("Hair Salon", "Delta")].chain_locations == 1


def biz(name="Shop", category="barber", website="https://shop.ca", reviews=300, rating=4.7, **kw):
    return Business(key=f"k:{name}", name=name, category=category, city="Surrey", phone="(604) 555-0100",
                    website=website, review_count=reviews, rating=rating, sources=["google"], **kw)


def checks(software, url="https://shop.ca"):
    from beauty_leads.normalize import url_key
    return {url_key(url): {"software": software, "confidence": "high", "evidence": ["test"], "emails": ["a@shop.ca"]}}


def test_software_price_models(cfg):
    vagaro = cfg.software["vagaro"]
    assert software_monthly_price(vagaro, 1) == 30 + 60
    assert software_monthly_price(vagaro, 5) == 30 + 40 + 60
    gloss = cfg.software["glossgenius"]
    assert software_monthly_price(gloss, 1) == 24
    assert software_monthly_price(gloss, 3) == 48
    assert software_monthly_price(gloss, 50) == 148


def test_unhappy_software_beats_loved_software(cfg):
    a_vagaro = Scorer(cfg, checks("vagaro")).assess(biz())
    a_blvd = Scorer(cfg, checks("boulevard")).assess(biz())
    assert a_vagaro.close_probability > 2 * a_blvd.close_probability
    assert a_vagaro.current_spend > 0
    assert a_vagaro.suggested_price == pytest.approx(a_vagaro.current_spend * 0.8)
    assert a_vagaro.savings > 0


def test_franchise_is_gated(cfg):
    indie = Scorer(cfg, checks("vagaro")).assess(biz())
    fran = Scorer(cfg, checks("vagaro")).assess(biz(franchise="great clips"))
    assert fran.close_probability < indie.close_probability * 0.25
    cfg.raw["scoring"]["exclude_franchises"] = True
    excluded = Scorer(cfg, checks("vagaro")).assess(biz(franchise="great clips"))
    assert excluded.tier == "X" and excluded.expected_mrr == 0


def test_closed_is_excluded(cfg):
    a = Scorer(cfg, checks("vagaro")).assess(biz(business_status="CLOSED_PERMANENTLY"))
    assert a.tier == "X" and a.expected_mrr == 0


def test_price_floor_against_cheap_competitor(cfg):
    a = Scorer(cfg, checks("square_appointments")).assess(biz(num_employees=7))
    list_price = 79 + 10 * 5
    assert a.list_price == list_price
    assert a.suggested_price == pytest.approx(list_price * 0.6)
    assert a.savings < 0  # we'd cost more: sell on features


def test_no_software_uses_list_price(cfg):
    a = Scorer(cfg, checks("none_detected")).assess(biz(num_employees=4))
    assert a.current_spend == 0 and a.suggested_price == 79 + 10 * 2


def test_url_alone_detects_software_and_social(cfg):
    assert Scorer(cfg).assess(biz(website="https://www.vagaro.com/shop")).software == "vagaro"
    assert Scorer(cfg).assess(biz(website="https://instagram.com/shop")).software == "social_only"
    assert Scorer(cfg).assess(biz(website=None)).software == "no_website"
    assert Scorer(cfg).assess(biz()).software == "unknown"


def test_reported_data_overrides_estimates(cfg):
    b = biz(num_employees=6, extra={"known_software": "Vagaro", "known_monthly_spend_cad": 200})
    b.sources = ["google", "csv:calls"]
    a = Scorer(cfg, checks("booksy")).assess(b)
    assert a.software == "vagaro" and a.software_confidence == "reported"
    assert a.current_spend == 200 and a.current_spend_source == "reported"
    assert a.staff_source == "reported" and a.suggested_price == pytest.approx(160)
    assert resolve_known_software(cfg, "square") == "square_appointments"
    assert resolve_known_software(cfg, "paper") == "none_detected"


def test_registry_years_feed_stability(cfg):
    reg = {"shop": {"match": {"registration_date": "2010-05-01", "incorporated": True, "entity_type_label": "BC company"}}}
    a = Scorer(cfg, checks("vagaro"), reg, today=date(2026, 1, 1)).assess(biz())
    assert a.years_registered == pytest.approx(15.67, abs=0.05)
    assert a.factors["stability"] == 1.0
    new = {"shop": {"match": {"registration_date": "2025-09-01"}}}
    b = Scorer(cfg, checks("vagaro"), new, today=date(2026, 1, 1)).assess(biz())
    assert b.factors["stability"] == 0.3


def test_rollups(cfg):
    b1, b2 = biz("A"), biz("B")
    b1.chain_key = b2.chain_key = "chain:ab"
    b1.chain_locations = b2.chain_locations = 2
    out = Scorer(cfg, checks("vagaro")).assess_all([b1, b2, biz("C", website=None)])
    acc = accounts(out)
    assert acc[0]["locations"] == 2
    s = market_summary(out)
    assert s["live_locations"] == 3 and s["accounts"] == 2
    assert s["by_software"]["Vagaro"]["locations"] == 2
