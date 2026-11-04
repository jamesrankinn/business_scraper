import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from beauty_leads import crm_close
from beauty_leads.db import Database
from beauty_leads.enrich.website import check_website
from beauty_leads.http import HttpClient
from beauty_leads.models import Business
from beauty_leads.normalize import url_key
from beauty_leads.scoring import Scorer

# ---------------------------------------------------------------------------
# Browser recheck
# ---------------------------------------------------------------------------
JS_WIDGET_PAGE = b"""<html><body><h1>Glow Studio</h1><div id="book"></div>
<script>
  // Built at runtime, so a plain HTML fetch can't see the platform's address.
  var host = "www." + "vag" + "aro" + ".com";
  var s = document.createElement("script");
  s.src = "https://" + host + "/resources/WidgetEmbeddedLoader/glowstudio";
  document.body.appendChild(s);
</script>
<a href="mailto:hello@glow.example">hello@glow.example</a></body></html>"""
PLAIN_PAGE = b"<html><body><p>Call us to book.</p></body></html>"


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /private\n"
        elif self.path.startswith("/js"):
            body = JS_WIDGET_PAGE
        else:
            body = PLAIN_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def site():
    srv = HTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_browser_recheck_finds_js_widget(cfg, tmp_path, site):
    pytest.importorskip("playwright.sync_api")
    from beauty_leads.enrich.browser import recheck_with_browser

    cfg.raw["http"]["per_host_delay_seconds"] = 0
    cfg.raw["browser"]["pause_seconds"] = 0
    http = HttpClient(cfg, cache_path=tmp_path / "cache.sqlite")
    urls = {"js": f"{site}/js/", "plain": f"{site}/plain/", "private": f"{site}/private/"}
    with Database(tmp_path / "t.db") as db:
        db.replace_businesses([Business(key=k, name=k, category="hair_salon", website=u) for k, u in urls.items()])
        for k, u in urls.items():
            # The plain fetch saw no booking software on any of them.
            plain = check_website(cfg, http, u) if k != "private" else {"status": "ok", "software": "none_detected"}
            assert plain["software"] == "none_detected"
            db.save_website_check(url_key(u), plain)

        stats = recheck_with_browser(cfg, db, http)
        checks = db.website_checks()
        js = checks[url_key(urls["js"])]
        assert js["software"] == "vagaro" and js["rendered"] is True
        assert any("vagaro.com" in e for e in js["evidence"])
        assert "hello@glow.example" in js["emails"]
        assert checks[url_key(urls["plain"])]["software"] == "none_detected"
        assert checks[url_key(urls["private"])]["render_status"] == "robots_blocked"
        assert stats["vagaro"] == 1
        # Already rendered: a second run has nothing to do.
        assert recheck_with_browser(cfg, db, http) == {}
    http.close()


# ---------------------------------------------------------------------------
# Close CRM
# ---------------------------------------------------------------------------
class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeClose:
    """Just enough of the Close API to test push/pull."""

    def __init__(self, existing_fields=(), existing_leads=()):
        self.fields = []
        for i, f in enumerate(existing_fields):
            f = {"name": f} if isinstance(f, str) else dict(f)
            self.fields.append({"id": f"cf_{i}", "type": "text", "choices": None, "accepts_multiple_values": False, **f})
        self.leads = {f"lead_old{i}": {"id": f"lead_old{i}", "display_name": n, "name": n}
                      for i, n in enumerate(existing_leads)}
        self.created, self.updates, self.notes, self.calls, self.auth = [], [], [], [], None
        self.rate_limit_once = True

    def request(self, method, url, json=None, params=None, timeout=None):
        path = url.split("/api/v1/", 1)[1]
        self.calls.append((method, path))
        if self.rate_limit_once and method == "POST" and path == "lead/":
            self.rate_limit_once = False
            return Resp(429, {"error": {"rate_reset": 0.01}}, {"RateLimit": "limit=10, remaining=0, reset=0.01"})
        if path == "custom_field/lead/" and method == "GET":
            return Resp(200, {"data": self.fields, "has_more": False})
        if path == "custom_field/lead/" and method == "POST":
            f = {"id": f"cf_new{len(self.fields)}", **json}
            self.fields.append(f)
            return Resp(200, f)
        if path == "data/search/":
            cond = json["query"]["queries"][1]
            value, field = cond["condition"]["value"], cond["field"]
            if field["type"] == "custom_field":
                hits = [l for l in self.leads.values() if l.get(f"custom.{field['custom_field_id']}") == value]
            else:
                hits = [l for l in self.leads.values() if value.lower() in l["display_name"].lower()]
            return Resp(200, {"data": [{"id": l["id"], "display_name": l["display_name"]} for l in hits], "cursor": None})
        if path == "lead/" and method == "POST":
            lid = f"lead_new{len(self.leads)}"
            self.created.append(json)
            self.leads[lid] = {"id": lid, "display_name": json["name"], **json}
            return Resp(200, self.leads[lid])
        if path.startswith("lead/") and method == "PUT":
            self.updates.append(json)
            self.leads[path.split("/")[1]].update(json)
            return Resp(200, self.leads[path.split("/")[1]])
        if path.startswith("lead/") and method == "GET":
            lid = path.split("/")[1]
            if lid not in self.leads:
                return Resp(404, {"error": "not found"})
            return Resp(200, {"status_label": "Qualified", "html_url": f"https://app.close.com/lead/{lid}/", **self.leads[lid]})
        if path == "activity/note/":
            self.notes.append(json)
            return Resp(200, {"id": "acti_1", **json})
        return Resp(400, {"error": f"unexpected {method} {path}"})


# The fields in the real Head Way Design Close account (names and choices).
HEADWAY_FIELDS = [
    {"name": "Current Paid Tools", "type": "choices", "accepts_multiple_values": True,
     "choices": ["Acuity", "Calendly", "Fresha", "GoDaddy", "Jotform / Typeform", "Klaviyo", "Mailchimp", "Other",
                 "Shopify", "Square", "Squarespace", "Stripe", "Vagaro", "Wix", "WordPress"]},
    {"name": "Est. Monthly Software Spend", "type": "number"},
    {"name": "Industry", "type": "choices", "choices": ["Beauty & Wellness", "Other", "Retail"]},
    {"name": "Lead Source", "type": "choices", "choices": ["Cold Call", "Google Maps", "Other", "Social DM"]},
    {"name": "Website Status", "type": "choices", "choices": ["Decent", "Good", "No Website", "Outdated"]},
    {"name": "Priority", "type": "choices", "choices": ["Cold", "Hot", "Warm"]},
    {"name": "Import Batch", "type": "text"},
]


def _assessments(cfg):
    checks = {url_key("https://fade.example"): {"software": "vagaro", "confidence": "high", "evidence": ["x"],
                                                "emails": ["hi@fade.example"], "tools": ["wix", "mailchimp"],
                                                "socials": {"instagram": "https://instagram.com/fade.house"}},
              url_key("https://glow.example"): {"software": "booksy", "confidence": "high", "evidence": [],
                                                "emails": []}}
    biz = [
        Business(key="google:fade", name="Fade House", category="barber", city="Surrey", street="1 Main St",
                 phone="(604) 555-0101", website="https://fade.example", review_count=400, rating=4.8, sources=["google"]),
        Business(key="google:glow", name="Glow Lash", category="lash_brow", city="Kelowna", phone="(250) 555-0102",
                 website="https://glow.example", review_count=150, rating=4.9, sources=["google"]),
        Business(key="osm:node/7", name="Ink Den Tattoo", category="tattoo", city="Victoria",
                 website="https://instagram.com/inkden", review_count=90, rating=4.9, sources=["osm"]),
        Business(key="google:closed", name="Gone Salon", category="hair_salon", business_status="CLOSED_PERMANENTLY",
                 sources=["google"]),
    ]
    return Scorer(cfg, checks).assess_all(biz)


def _by_key(assessments, key):
    return next(a for a in assessments if a.business.key == key)


def test_close_payloads_fill_existing_fields(cfg):
    from datetime import date
    fields = {f["name"]: {"id": f"cf_{i}", **f} for i, f in enumerate(HEADWAY_FIELDS)}
    existing = {c: fields[n] for c, n in cfg.get("close.existing_fields").items()}
    ids = {k: f"cf_{k}" for k, *_ in crm_close.FIELD_SPECS}
    schema = crm_close.Schema(ids=ids, existing=existing)
    fade = _by_key(_assessments(cfg), "google:fade")
    p = crm_close.lead_payload(cfg, fade, schema)
    assert p["name"] == "Fade House" and p["url"] == "https://fade.example"
    assert p["contacts"] == [{"emails": [{"email": "hi@fade.example", "type": "office"}],
                              "phones": [{"phone": "(604) 555-0101", "type": "office"}],
                              "urls": [{"url": "https://www.instagram.com/fade.house/", "type": "url"}]}]
    assert p["addresses"][0] == {"label": "business", "address_1": "1 Main St", "city": "Surrey", "state": "BC",
                                 "country": "CA"}
    assert p["custom.cf_bl_id"] == "google:fade" and p["custom.cf_software"] == "Vagaro"
    # Your fields, with your choices:
    assert p[f"custom.{fields['Current Paid Tools']['id']}"] == ["Vagaro", "Wix", "Mailchimp"]
    assert p[f"custom.{fields['Est. Monthly Software Spend']['id']}"] == round(fade.replaceable_spend, 2)
    assert fade.replaceable_spend > fade.current_spend  # Wix + Mailchimp included
    assert p[f"custom.{fields['Industry']['id']}"] == "Beauty & Wellness"
    assert p[f"custom.{fields['Lead Source']['id']}"] == "Google Maps"
    assert p[f"custom.{fields['Priority']['id']}"] == {"A": "Hot", "B": "Warm"}.get(fade.tier, "Cold")
    assert p[f"custom.{fields['Import Batch']['id']}"].endswith("beauty-leads")
    assert f"custom.{fields['Website Status']['id']}" not in p  # they have a website
    # Instagram-only tattoo shop: "No Website", source "Other", Booksy-like unknowns become "Other".
    ink = _by_key(_assessments(cfg), "osm:node/7")
    pi = crm_close.lead_payload(cfg, ink, schema)
    assert pi[f"custom.{fields['Website Status']['id']}"] == "No Website"
    assert pi[f"custom.{fields['Lead Source']['id']}"] == "Other"
    glow = _by_key(_assessments(cfg), "google:glow")
    assert crm_close.tool_choices(cfg, glow, fields["Current Paid Tools"]) == ["Other"]
    note = crm_close.note_text(cfg, fade)
    assert "Instagram DM draft" in note and "Email draft" in note and "Wix, Mailchimp" in note
    dry = crm_close.dry_run_payloads(cfg, _assessments(cfg), tiers=["A", "B", "C", "D"])
    assert {d["lead"]["name"] for d in dry} == {"Fade House", "Glow Lash", "Ink Den Tattoo"}  # closed shop never pushed
    assert "<Current Paid Tools>" in "".join(dry[0]["lead"])


def test_close_push_and_pull_with_your_fields(cfg, tmp_path):
    fake = FakeClose(existing_fields=HEADWAY_FIELDS + ["BL Tier"], existing_leads=["Glow Lash"])
    client = crm_close.CloseClient("key", session=fake, sleep=lambda s: None)
    assessments = _assessments(cfg)
    with Database(tmp_path / "c.db") as db:
        r = crm_close.push(cfg, db, client, assessments, tiers=["A", "B", "C", "D"])
        assert (r.created, r.linked, r.updated, r.errors) == (2, 1, 0, 0)
        names = {f["name"] for f in fake.fields}
        # Your fields are reused; duplicates of them are not created.
        assert "BL Est. Current Spend" not in names and "Reported Software" not in names
        assert "Reported Monthly Spend" not in names and {"Beauty Leads ID", "Reported Staff"} <= names
        assert sum(1 for f in fake.fields if f["name"] == "BL Tier") == 1
        tools_id = next(f["id"] for f in fake.fields if f["name"] == "Current Paid Tools")
        spend_id = next(f["id"] for f in fake.fields if f["name"] == "Est. Monthly Software Spend")
        assert any(c.get(f"custom.{tools_id}") == ["Vagaro", "Wix", "Mailchimp"] for c in fake.created)
        # Glow Lash was already in Close: matched by name, score fields + a note added, your fields untouched.
        links = db.crm_leads()
        assert links["google:glow"]["lead_id"] == "lead_old0"
        assert all(f"custom.{tools_id}" not in u for u in fake.updates)
        assert len(fake.notes) == 3

        # Re-push: only score fields refresh, no new notes.
        fake.notes.clear()
        r2 = crm_close.push(cfg, db, client, assessments, tiers=["A", "B", "C", "D"])
        assert (r2.created, r2.linked, r2.updated) == (0, 0, 3) and fake.notes == []

        # After a call you fix their tools and spend in Close; pull treats your edits as the truth.
        fade_lead = links["google:fade"]["lead_id"]
        fake.leads[fade_lead].update({f"custom.{tools_id}": ["Square", "Wix"], f"custom.{spend_id}": 60})
        stats = crm_close.pull(cfg, db, client)
        assert stats == {"pulled": 3, "missing": 0, "with_reports": 1}
        fade = _by_key(assessments, "google:fade").business
        rescored = Scorer(cfg, crm=db.crm_leads()).assess(fade)
        assert rescored.software == "square_appointments" and rescored.software_confidence == "reported"
        assert rescored.current_spend == 60
        assert rescored.crm_status == "Qualified" and rescored.crm_url.endswith(f"{fade_lead}/")
        # Leads you didn't touch keep the tool's estimates.
        ink = Scorer(cfg, crm=db.crm_leads()).assess(_by_key(assessments, "osm:node/7").business)
        assert ink.software == "social_only"


def test_close_auth_error_is_clear(cfg):
    class Deny:
        auth = None

        def request(self, *a, **k):
            return Resp(401, {"error": "unauthorized"})

    client = crm_close.CloseClient("bad", session=Deny(), sleep=lambda s: None)
    with pytest.raises(crm_close.CloseError, match="CLOSE_API_KEY"):
        client.lead_fields()


def test_close_csv(cfg):
    rows = crm_close.close_csv_rows(cfg, _assessments(cfg))
    assert [r["Company"] for r in rows][:1] == ["Fade House"]
    fade = rows[0]
    assert fade["Beauty Leads ID"] == "google:fade" and fade["Country"] == "CA"
    assert fade["Current Paid Tools"] == "Vagaro, Wix, Mailchimp" and fade["Industry"] == "Beauty & Wellness"
