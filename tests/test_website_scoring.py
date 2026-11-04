from datetime import date

from beauty_leads.enrich.audit import audit_page, audit_without_site, status_for
from beauty_leads.enrich.website import check_website
from beauty_leads.models import Business
from beauty_leads.normalize import url_key
from beauty_leads.outreach import draft, site_line
from beauty_leads.scoring import Scorer, market_summary

TODAY = date(2026, 11, 4)

MODERN = """<!doctype html><html><head><title>Glow Lash Studio | Kelowna lash extensions</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Classic, hybrid and volume lash extensions in downtown Kelowna. Book online.">
<meta property="og:image" content="https://glow.example/og.jpg">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"BeautySalon","name":"Glow"}</script>
</head><body><h1>Lashes that last</h1><a href="tel:+12505550100">Call</a>
<a href="https://instagram.com/glowlash">Instagram</a><footer>© 2026 Glow Lash Studio</footer></body></html>"""

OLD = """<html><head><title>Home</title><script src="/js/jquery-1.8.3.min.js"></script>
<meta name="generator" content="WordPress 4.9.8"></head>
<body><font face="Arial">Welcome to our salon!</font><table><tr><td>Call 250-555-0101</td></tr></table>
<a href="/shop">Shop products</a><p>Copyright © 2014 Old Town Hair</p></body></html>"""


def test_audit_modern_site_is_good():
    a = audit_page(MODERN, "https://glow.example/", software="vagaro", today=TODAY)
    assert a["status"] == "Good" and a["score"] >= 90 and a["copyright_year"] == 2026


def test_audit_old_site_lists_issues():
    a = audit_page(OLD, "http://oldtown.example/", software="none_detected", tools=["wordpress"], today=TODAY)
    assert a["status"] == "Outdated" and a["score"] < 20
    text = " | ".join(a["issues"])
    for expected in ("No HTTPS", "Not mobile-friendly", "Footer still says © 2014", "jQuery 1.x", "<font>",
                     "old WordPress version (4.x)", "No online booking", "No tap-to-call", "Generic or missing page title"):
        assert expected in text, expected
    assert a["has_shop"] is True


def test_audit_parked_and_missing_sites():
    parked = audit_page("<html><body><h1>This domain is for sale!</h1></body></html>", "https://x.example/", today=TODAY)
    assert parked["status"] == "No Website"
    assert audit_without_site({}, "social_only")["status"] == "No Website"
    down = audit_without_site({"status": "fetch_failed", "error": "SSLError: certificate verify failed"}, "unknown")
    assert down["status"] == "Outdated" and "didn't load" in down["issues"][0]
    # A 403 is probably bot protection, not a broken site: don't guess.
    assert audit_without_site({"status": "fetch_failed", "error": "HTTP 403"}, "unknown") is None
    assert status_for(80) == "Good" and status_for(60) == "Decent" and status_for(10) == "Outdated"


def test_check_website_stores_audit(cfg, fake_http):
    http = fake_http({"https://oldtown.example": OLD})
    r = check_website(cfg, http, "https://oldtown.example")
    assert r["audit"]["status"] == "Outdated" and "wordpress" in r["tools"]


def _biz(**kw):
    base = dict(key="k", name="Old Town Hair", category="hair_salon", city="Nanaimo", phone="(250) 555-0101",
                website="https://oldtown.example", review_count=220, rating=4.6, sources=["google"])
    base.update(kw)
    return Business(**base)


def _check(software="vagaro", audit=None, tools=None, **kw):
    return {url_key("https://oldtown.example"): {"software": software, "confidence": "high", "evidence": [],
                                                 "audit": audit, "tools": tools or [], **kw}}


OUTDATED = {"score": 30, "status": "Outdated", "has_shop": False,
            "issues": ["Not mobile-friendly (no responsive layout)", "Footer still says © 2017", "No tap-to-call phone link"]}
GOOD = {"score": 88, "status": "Good", "has_shop": False, "issues": ["No preview image when the link is shared"]}


def test_opportunities_and_first_year_value(cfg):
    bad = Scorer(cfg, _check(audit=OUTDATED, tools=["wordpress"])).assess(_biz())
    good = Scorer(cfg, _check(audit=GOOD)).assess(_biz())
    assert "Website Redesign" in bad.opportunities and "Hosting & Maintenance" in bad.opportunities
    assert "Website Redesign" not in good.opportunities
    assert bad.project_value > good.project_value
    assert bad.web_close_probability > good.web_close_probability
    assert bad.first_year_value == bad.expected_mrr * 12 + bad.expected_project_value
    assert bad.first_year_value > good.first_year_value
    assert any("website Outdated (30/100)" in r for r in bad.reasons)

    social = Scorer(cfg).assess(_biz(website="https://instagram.com/oldtown", category="tattoo"))
    assert social.web_status == "No Website"
    assert {"New Website", "Online Booking", "Forms & Intake"} <= set(social.opportunities)
    platform = Scorer(cfg).assess(_biz(website="https://www.vagaro.com/oldtown"))
    assert platform.web_status == "No Website" and "New Website" in platform.opportunities
    assert platform.issues_platform_page


def test_love_their_booking_tool_but_bad_website_is_still_a_lead(cfg):
    # Boulevard users rarely switch booking software, but an outdated site is still a website deal.
    blvd_bad = Scorer(cfg, _check("boulevard", audit=OUTDATED)).assess(_biz())
    blvd_good = Scorer(cfg, _check("boulevard", audit=GOOD)).assess(_biz())
    assert blvd_bad.close_probability < 0.12
    assert blvd_bad.web_close_probability > 2 * blvd_good.web_close_probability
    assert blvd_bad.first_year_value > 2 * blvd_good.first_year_value


def test_rank_by_monthly_still_available(cfg):
    cfg.raw["scoring"]["rank_by"] = "monthly"
    a = Scorer(cfg, _check(audit=OUTDATED)).assess(_biz())
    assert a.rank_value == a.expected_mrr


def test_site_line_in_email(cfg):
    a = Scorer(cfg, _check(audit=OUTDATED, emails=["hi@oldtown.example"])).assess(_biz())
    line = site_line(cfg, a, "salons")
    assert line == ("I also took a quick look at your website: it isn't mobile-friendly and the footer still says "
                    "© 2017. We rebuild sites for salons too, if that's ever on your list.")
    body = draft(cfg, a)["email_body"]
    assert line in body and "\n\n\n" not in body
    assert body.index(line) < body.index("Would a 10-minute look")  # before the closing question
    good = Scorer(cfg, _check(audit=GOOD, emails=["hi@oldtown.example"])).assess(_biz())
    assert site_line(cfg, good, "salons") == ""
    assert "look at your website" not in draft(cfg, good)["email_body"]


def test_summary_counts_websites(cfg):
    out = Scorer(cfg, _check(audit=OUTDATED)).assess_all([_biz(), _biz(key="k2", website=None)])
    s = market_summary(out)
    assert s["needs_website"] == 2 and s["by_website_status"] == {"Outdated": 1, "No Website": 1}
    assert s["by_opportunity"]["Hosting & Maintenance"] == 2
