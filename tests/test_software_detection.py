from beauty_leads.enrich.software import detect, software_for_url
from beauty_leads.enrich.website import check_website


def test_software_for_url(cfg):
    assert software_for_url(cfg, "https://www.vagaro.com/fadehouse") == ("vagaro", True)
    assert software_for_url(cfg, "https://booksy.com/en-ca/123_shop") == ("booksy", True)
    assert software_for_url(cfg, "https://book.squareup.com/appointments/abc") == ("square_appointments", True)
    assert software_for_url(cfg, "https://squareup.com/shop/x") == ("square_appointments", False)
    assert software_for_url(cfg, "https://mysalon.as.me/") == ("acuity", True)
    # Host matching, not substring matching:
    assert software_for_url(cfg, "https://notvagaro.com/") == (None, False)
    assert software_for_url(cfg, "https://example.com/?ref=vagaro.com") == (None, False)


def test_detect_booking_button(cfg):
    html = """<html><head><title>Fadehouse</title></head><body>
      <a href="https://www.vagaro.com/fadehouse">Book Now</a>
      <a href="https://instagram.com/fadehouse">IG</a>
      <a href="mailto:hello@fadehouse.ca">Email us</a>
    </body></html>"""
    d = detect(cfg, html, "https://fadehouse.ca/")
    assert d["software"] == "vagaro"
    assert d["confidence"] == "high"
    assert d["socials"]["instagram"] == "https://instagram.com/fadehouse"
    assert d["emails"] == ["hello@fadehouse.ca"]


def test_detect_widget_in_script(cfg):
    html = """<html><body><div id="w"></div>
      <script>window.cfg = {"widget": "https://booksy.com/widget/code.js?id=1"};</script>
      <iframe src="//booksy.com/widget/index.html?id=1"></iframe></body></html>"""
    d = detect(cfg, html, "https://shop.ca/")
    assert d["software"] == "booksy"
    assert d["confidence"] == "high"


def test_detect_wix_bookings(cfg):
    html = """<html><head><link href="https://static.wixstatic.com/x.css" rel="stylesheet"></head>
      <body><a href="https://glow.ca/book-online">Book</a></body></html>"""
    d = detect(cfg, html, "https://glow.ca/")
    assert d["site_builder"] == "wix"
    assert d["software"] == "wix_bookings"


def test_check_website_platform_url_needs_no_fetch(cfg, fake_http):
    http = fake_http({})
    r = check_website(cfg, http, "https://www.vagaro.com/fadehouse")
    assert r["software"] == "vagaro" and r["status"] == "platform_url"
    assert http.requested == []


def test_check_website_social_and_none(cfg, fake_http):
    assert check_website(cfg, fake_http({}), "instagram.com/myshop")["software"] == "social_only"
    assert check_website(cfg, fake_http({}), None)["software"] == "no_website"
    http = fake_http({"http://plain.ca": "<html><body><p>Call us to book: 604-555-0100</p></body></html>"})
    assert check_website(cfg, http, "plain.ca")["software"] == "none_detected"


def test_check_website_follows_booking_link(cfg, fake_http):
    http = fake_http({
        "https://salon.ca": '<html><body><a href="/book">Book an appointment</a></body></html>',
        "https://salon.ca/book": '<html><body><iframe src="https://www.fresha.com/a/salon-x"></iframe></body></html>',
    })
    r = check_website(cfg, http, "https://salon.ca")
    assert r["software"] == "fresha"
    assert "https://salon.ca/book" in r["pages_checked"]


def test_check_website_redirect_to_platform(cfg, fake_http):
    http = fake_http({"https://shop.ca": (200, "<html></html>", "https://booksy.com/en-ca/999_shop")})
    assert check_website(cfg, http, "https://shop.ca")["software"] == "booksy"


def test_check_website_unreachable(cfg, fake_http):
    r = check_website(cfg, fake_http({}), "https://down.ca")
    assert r["software"] == "unknown" and r["status"] == "fetch_failed"
