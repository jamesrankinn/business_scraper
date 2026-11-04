from beauty_leads.categories import classify_text
from beauty_leads.enrich.software import detect
from beauty_leads.enrich.website import check_website
from beauty_leads.merge import merge_records
from beauty_leads.models import Business, Record
from beauty_leads.normalize import instagram_handle, url_key
from beauty_leads.outreach import draft, message_group, outreach_rows, sender_is_configured
from beauty_leads.scoring import Scorer


def test_new_categories(cfg):
    assert classify_text(cfg, "Inkwell Tattoo Collective") == "tattoo"
    assert classify_text(cfg, "Kneaded RMT Clinic") == "massage"
    assert classify_text(cfg, "Stillwater Massage Therapy") == "massage"
    assert classify_text(cfg, "Lumen Med Spa") == "med_spa"
    assert classify_text(cfg, "Glow Lab Medical Aesthetics") == "med_spa"
    assert classify_text(cfg, "Westside Laser Clinic") == "med_spa"
    assert classify_text(cfg, "Serenity Day Spa") == "esthetics_spa"
    assert classify_text(cfg, "Nails & Massage") == "nails"


def test_exclusions_drop_adult_and_dental(cfg):
    recs = [Record(source="google", source_id=str(i), name=n, city="Surrey", province="BC", street=f"{i} A St",
                   types=t) for i, (n, t) in enumerate([
                       ("Velvet Touch", ["Body-Rub Parlour"]),
                       ("Sensual Massage Studio", ["massage"]),
                       ("Smile Dental Spa", ["dentist"]),
                       ("Stillwater Massage Therapy", ["massage"]),
                   ], 1)]
    assert [b.name for b in merge_records(cfg, recs)] == ["Stillwater Massage Therapy"]


def test_instagram_handles():
    assert instagram_handle("https://www.instagram.com/Fade.House/?hl=en") == "fade.house"
    assert instagram_handle("@fade_house") == "fade_house"
    assert instagram_handle("instagram.com/p/Cx123/") is None
    assert instagram_handle("https://instagram.com/") is None
    assert instagram_handle("Fade House Barbers") is None


def test_detect_no_solicitation_and_contact_links(cfg):
    html = """<html><body>
      <a href="https://www.instagram.com/p/abc123/">our latest post</a>
      <a href="https://www.instagram.com/glowstudio/">Follow us</a>
      <a href="/contact-us">Contact</a>
      <footer>Please, no solicitation. We do not accept sales calls.</footer></body></html>"""
    d = detect(cfg, html, "https://glow.ca/")
    assert d["socials"]["instagram"] == "https://www.instagram.com/glowstudio/"
    assert d["no_solicitation"] and "solicitation" in d["no_solicitation"].lower()
    assert d["contact_links"] == ["https://glow.ca/contact-us"]
    assert detect(cfg, "<p>Book with us! Email hello@glow.ca</p>", "https://glow.ca/")["no_solicitation"] is None


def test_check_website_reads_contact_page_for_email(cfg, fake_http):
    http = fake_http({
        "https://glow.ca": '<html><body><a href="https://www.vagaro.com/glow">Book</a><a href="/about">About us</a></body></html>',
        "https://glow.ca/about": '<html><body>Say hi: <a href="mailto:hello@glow.ca">hello@glow.ca</a></body></html>',
    })
    r = check_website(cfg, http, "https://glow.ca")
    assert r["software"] == "vagaro" and r["emails"] == ["hello@glow.ca"]
    assert "https://glow.ca/about" in r["pages_checked"]


def _biz(**kw):
    base = dict(key="k", name="Glow Studio", category="lash_brow", city="Surrey", phone="(604) 555-0100",
                website="https://glow.ca", review_count=200, rating=4.8, sources=["google"])
    base.update(kw)
    return Business(**base)


def _check(software="vagaro", **kw):
    return {url_key("https://glow.ca"): {"software": software, "confidence": "high", "evidence": [], **kw}}


def test_channel_selection(cfg):
    email = Scorer(cfg, _check(emails=["hi@glow.ca"], socials={"instagram": "https://instagram.com/glow"})).assess(_biz())
    assert email.channel == "email" and email.instagram == "glow" and email.email_ok
    blocked = Scorer(cfg, _check(emails=["hi@glow.ca"], no_solicitation="no solicitation",
                                 socials={"instagram": "https://instagram.com/glow"})).assess(_biz())
    assert blocked.channel == "instagram" and not blocked.email_ok
    assert any("do not cold-email" in r for r in blocked.reasons)
    ig_site = Scorer(cfg).assess(_biz(website="https://www.instagram.com/glow.lash/"))
    assert ig_site.channel == "instagram" and ig_site.instagram == "glow.lash"
    from_csv = Scorer(cfg).assess(_biz(website=None, extra={"instagram": "@glow_csv"}))
    assert from_csv.instagram == "glow_csv"
    phone_only = Scorer(cfg).assess(_biz(website=None))
    assert phone_only.channel == "phone"
    assert email.factors["reachability"] > phone_only.factors["reachability"]


def test_message_groups_and_drafts(cfg):
    vagaro = Scorer(cfg, _check("vagaro", emails=["hi@glow.ca"])).assess(_biz())
    assert message_group(cfg, vagaro) == "save_money"
    d = draft(cfg, vagaro)
    assert "Vagaro" in d["email_subject"] and "lash studio" in d["email_body"]
    assert "unsubscribe" in d["email_body"].lower() and "mailing address" in d["email_body"]
    assert "{" not in d["email_body"] and "{" not in d["dm_text"]
    assert len(d["dm_text"]) < 400
    assert message_group(cfg, Scorer(cfg, _check("boulevard")).assess(_biz())) == "premium"
    assert message_group(cfg, Scorer(cfg, _check("square_appointments")).assess(_biz(num_employees=7))) == "features"
    assert message_group(cfg, Scorer(cfg, _check("none_detected")).assess(_biz())) == "no_booking"
    assert message_group(cfg, Scorer(cfg).assess(_biz())) == "unknown"


def test_outreach_rows_and_sender(cfg):
    a = Scorer(cfg, _check(emails=["hi@glow.ca"])).assess(_biz())
    closed = Scorer(cfg).assess(_biz(key="c", business_status="CLOSED_PERMANENTLY"))
    unreachable = Scorer(cfg).assess(_biz(key="u", website=None, phone=None))
    rows = outreach_rows(cfg, [a, closed, unreachable])
    assert [r["business_key"] for r in rows] == ["k"] and rows[0]["email"] == "hi@glow.ca"
    assert not sender_is_configured(cfg)
    cfg.outreach["sender"] = {"name": "Sam", "business": "Booksmith", "email": "sam@booksmith.ca",
                              "mailing_address": "1 Main St, Kelowna, BC"}
    assert sender_is_configured(cfg)
    assert "Sam\nBooksmith · sam@booksmith.ca\n1 Main St, Kelowna, BC" in draft(cfg, a)["email_body"]
