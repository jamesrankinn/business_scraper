"""Close CRM integration (https://developer.close.com).

push  Create a Close lead per business (or update the one already there):
      contacts, address, scores in custom fields, and a note with the pitch
      and drafted email/DM. Existing Close leads are matched by our ID field,
      then by name, so shops you already have in Close are enriched, not
      duplicated. Re-pushing only refreshes the "BL" score fields; it never
      touches your contacts, status, notes or anything you typed.

pull  Read back what you record in Close during calls: lead status plus the
      "Reported Software / Reported Monthly Spend / Reported Staff" fields.
      Reported values override the tool's estimates in the next scoring run.

Auth: an API key from Close (Settings > Developer > API Keys) in the
CLOSE_API_KEY environment variable. Creating custom fields needs a Close user
role with the "manage organization" permission (admins have it).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import requests

from .config import Config
from .normalize import normalize_name
from .outreach import draft
from .scoring import Assessment

log = logging.getLogger(__name__)

CLOSE_API = "https://api.close.com/api/v1"

# (our key, default Close field name, type, choices, written on push?)
FIELD_SPECS = [
    ("bl_id", "Beauty Leads ID", "text", None, True),
    ("tier", "BL Tier", "choices", ["A", "B", "C", "D", "X"], True),
    ("expected", "BL Expected MRR", "number", None, True),
    ("price", "BL Our Price", "number", None, True),
    ("spend", "BL Est. Current Spend", "number", None, True),
    ("close_pct", "BL Close %", "number", None, True),
    ("software", "BL Software", "text", None, True),
    ("category", "BL Category", "text", None, True),
    ("channel", "BL Channel", "choices", ["email", "instagram", "phone", "none"], True),
    ("instagram", "Instagram", "text", None, True),
    # Filled in by you during calls; read back by `close pull`.
    ("reported_software", "Reported Software", "text", None, False),
    ("reported_spend", "Reported Monthly Spend", "number", None, False),
    ("reported_staff", "Reported Staff", "number", None, False),
]


class CloseError(RuntimeError):
    pass


def field_names(cfg: Config) -> dict[str, str]:
    overrides = cfg.get("close.field_names", {}) or {}
    return {key: overrides.get(key, name) for key, name, *_ in FIELD_SPECS}


class CloseClient:
    def __init__(self, api_key: str, base_url: str = CLOSE_API, session=None, sleep=time.sleep):
        self.base = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.auth = (api_key, "")
        self.sleep = sleep

    def request(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"
        for attempt in range(6):
            resp = self.session.request(method, url, json=body, params=params, timeout=30)
            if resp.status_code == 429:
                self.sleep(_retry_seconds(resp, attempt))
                continue
            if resp.status_code == 401:
                raise CloseError("Close rejected the API key (401). Check CLOSE_API_KEY.")
            if resp.status_code >= 400:
                raise CloseError(f"{method} {path} failed: {resp.status_code} {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise CloseError(f"{method} {path}: still rate limited after 6 tries")

    # --- custom fields -------------------------------------------------------
    def lead_fields(self) -> list[dict]:
        out, skip = [], 0
        while True:
            page = self.request("GET", "custom_field/lead/", params={"_skip": skip, "_limit": 100})
            out.extend(page.get("data", []))
            if not page.get("has_more"):
                return out
            skip += 100

    def ensure_fields(self, cfg: Config) -> dict[str, str]:
        """{our key: Close custom field id}, creating any that are missing."""
        names = field_names(cfg)
        existing = {f["name"].strip().lower(): f["id"] for f in self.lead_fields()}
        ids = {}
        for key, _default, ftype, choices, _ in FIELD_SPECS:
            name = names[key]
            if name.lower() in existing:
                ids[key] = existing[name.lower()]
                continue
            body = {"name": name, "type": ftype}
            if choices:
                body["choices"] = choices
            try:
                created = self.request("POST", "custom_field/lead/", body=body)
            except CloseError as exc:
                raise CloseError(f"Couldn't create the Close custom field {name!r} ({exc}). Ask a Close admin "
                                 "to create it, or use an API key from an admin user.") from exc
            ids[key] = created["id"]
            log.info("Created Close custom field %r", name)
        return ids

    # --- leads -----------------------------------------------------------------
    def search_leads(self, field: dict, value: str, mode: str = "exact_value", limit: int = 5) -> list[dict]:
        body = {
            "_fields": {"lead": ["id", "display_name"]},
            "_limit": limit,
            "query": {"type": "and", "queries": [
                {"type": "object_type", "object_type": "lead"},
                {"type": "field_condition", "field": field,
                 "condition": {"type": "text", "mode": mode, "value": value}},
            ]},
        }
        return self.request("POST", "data/search/", body=body).get("data", [])

    def find_lead(self, ids: dict, business_key: str, name: str) -> tuple[str | None, str]:
        hits = self.search_leads({"type": "custom_field", "custom_field_id": ids["bl_id"]}, business_key)
        if hits:
            return hits[0]["id"], "id"
        hits = self.search_leads({"type": "regular_field", "object_type": "lead", "field_name": "name"}, name, mode="phrase")
        for h in hits:
            if normalize_name(h.get("display_name")) == normalize_name(name):
                return h["id"], "name"
        return None, ""

    def create_lead(self, payload: dict) -> dict:
        return self.request("POST", "lead/", body=payload)

    def update_lead(self, lead_id: str, payload: dict) -> dict:
        return self.request("PUT", f"lead/{lead_id}/", body=payload)

    def get_lead(self, lead_id: str, fields: list[str]) -> dict:
        return self.request("GET", f"lead/{lead_id}/", params={"_fields": ",".join(fields)})

    def add_note(self, lead_id: str, text: str) -> dict:
        return self.request("POST", "activity/note/", body={"lead_id": lead_id, "note": text})


def _retry_seconds(resp, attempt: int) -> float:
    m = re.search(r"reset=([\d.]+)", resp.headers.get("RateLimit", "") or "")
    if m:
        return float(m.group(1)) + 0.1
    try:
        reset = resp.json().get("error", {}).get("rate_reset")
        if reset:
            return float(reset) + 0.1
    except (ValueError, AttributeError):
        pass
    ra = resp.headers.get("Retry-After", "")
    return float(ra) if ra.replace(".", "", 1).isdigit() else 2.0 ** attempt


# --- payloads (pure functions, easy to test) ---------------------------------
def lead_name(a: Assessment) -> str:
    b = a.business
    return f"{b.name} ({b.city})" if b.chain_locations > 1 and b.city else b.name


def score_fields(cfg: Config, a: Assessment, ids: dict) -> dict:
    b = a.business
    values = {
        "bl_id": b.key,
        "tier": a.tier,
        "expected": round(a.expected_mrr, 2),
        "price": round(a.suggested_price, 2),
        "spend": None if a.current_spend is None else round(a.current_spend, 2),
        "close_pct": round(a.close_probability * 100, 1),
        "software": a.software_label,
        "category": cfg.categories.get(b.category, {}).get("label", b.category),
        "channel": a.channel,
        "instagram": f"@{a.instagram}" if a.instagram else None,
    }
    return {f"custom.{ids[k]}": v for k, v in values.items() if k in ids and v is not None}


def lead_payload(cfg: Config, a: Assessment, ids: dict) -> dict:
    b = a.business
    contact: dict = {}
    if a.emails:
        contact["emails"] = [{"email": e, "type": "office"} for e in a.emails[:3]]
    if b.phone:
        contact["phones"] = [{"phone": b.phone, "type": "office"}]
    if a.instagram_url:
        contact["urls"] = [{"url": a.instagram_url, "type": "url"}]
    payload = {
        "name": lead_name(a),
        "url": b.website,
        "description": f"{a.pitch} | {' • '.join(a.reasons)}"[:2000] if a.pitch else " • ".join(a.reasons)[:2000],
        **score_fields(cfg, a, ids),
    }
    if contact:
        payload["contacts"] = [contact]
    if b.street or b.address or b.city:
        address = {
            "label": "business",
            "address_1": b.street or (b.address or "").split(",")[0] or None,
            "city": b.city, "state": b.province or "BC", "zipcode": b.postal_code, "country": "CA",
        }
        payload["addresses"] = [{k: v for k, v in address.items() if v not in (None, "")}]
    return {k: v for k, v in payload.items() if v not in (None, "")}


def note_text(cfg: Config, a: Assessment) -> str:
    d = draft(cfg, a) if a.channel != "none" else {}
    spend = "unknown" if a.current_spend is None else f"${a.current_spend:,.0f}/mo (estimate)"
    lines = [
        f"Beauty Leads: tier {a.tier}, expected ${a.expected_mrr:,.0f}/mo "
        f"(our price ${a.suggested_price:,.0f} x {a.close_probability:.0%} close)",
        f"Software: {a.software_label}" + (f" ({a.software_confidence})" if a.software_confidence else "")
        + f". Current spend: {spend}",
        f"Best channel: {a.channel}",
        f"Why: {' • '.join(a.reasons)}",
    ]
    if a.no_solicitation:
        lines.append(f'Warning: their site says "{a.no_solicitation}". Don\'t cold-email; use Instagram or a call.')
    if a.pitch:
        lines.append(f"Pitch: {a.pitch}")
    if a.software_evidence:
        lines.append(f"Evidence: {a.software_evidence[0]}")
    if d.get("dm_text") and a.instagram:
        lines += ["", "Instagram DM draft:", d["dm_text"]]
    if d.get("email_body") and a.email_ok:
        lines += ["", f"Email draft (subject: {d['email_subject']}):", d["email_body"]]
    lines += ["", "Log what you learn in Reported Software / Reported Monthly Spend / Reported Staff, "
                  "then run `beauty-leads close pull`."]
    return "\n".join(lines)


# --- push / pull -------------------------------------------------------------------
@dataclass
class PushResult:
    created: int = 0
    updated: int = 0
    linked: int = 0
    skipped: int = 0
    errors: int = 0


def select_for_push(cfg: Config, assessments: list[Assessment], tiers: list[str] | None = None,
                    limit: int | None = None) -> list[Assessment]:
    tiers = [t.upper() for t in (tiers or cfg.get("close.push_tiers", ["A", "B"]))]
    chosen = [a for a in assessments if not a.excluded_reason and a.tier in tiers]
    return chosen[: limit or None]


def push(cfg: Config, db, client: CloseClient, assessments: list[Assessment], tiers=None,
         limit: int | None = None) -> PushResult:
    ids = client.ensure_fields(cfg)
    known = db.crm_leads()
    result = PushResult()
    for a in select_for_push(cfg, assessments, tiers, limit):
        b = a.business
        try:
            lead_id = (known.get(b.key) or {}).get("lead_id")
            how = "mapped" if lead_id else ""
            if not lead_id:
                lead_id, how = client.find_lead(ids, b.key, lead_name(a))
            if lead_id:
                # Refresh only our score fields; leave everything you typed alone.
                client.update_lead(lead_id, score_fields(cfg, a, ids))
                if how == "name":
                    result.linked += 1
                    client.add_note(lead_id, note_text(cfg, a))
                else:
                    result.updated += 1
            else:
                lead = client.create_lead(lead_payload(cfg, a, ids))
                lead_id = lead["id"]
                client.add_note(lead_id, note_text(cfg, a))
                result.created += 1
            db.save_crm_lead(b.key, "close", lead_id, (known.get(b.key) or {}).get("data", {}))
        except CloseError as exc:
            log.error("Close push failed for %s: %s", b.name, exc)
            result.errors += 1
            if "401" in str(exc) or "custom field" in str(exc):
                raise
    return result


def pull(cfg: Config, db, client: CloseClient) -> dict:
    ids = client.ensure_fields(cfg)
    fields = ["id", "display_name", "status_label", "html_url"] + [
        f"custom.{ids[k]}" for k in ("reported_software", "reported_spend", "reported_staff")]
    stats = {"pulled": 0, "missing": 0, "with_reports": 0}
    for business_key, row in db.crm_leads().items():
        try:
            lead = client.get_lead(row["lead_id"], fields)
        except CloseError as exc:
            if " 404 " in str(exc):
                db.save_crm_lead(business_key, "close", row["lead_id"], {"deleted": True})
                stats["missing"] += 1
                continue
            raise
        data = {
            "status_label": lead.get("status_label"),
            "html_url": lead.get("html_url"),
            "reported_software": lead.get(f"custom.{ids['reported_software']}"),
            "reported_spend": lead.get(f"custom.{ids['reported_spend']}"),
            "reported_staff": lead.get(f"custom.{ids['reported_staff']}"),
        }
        if any(data[k] not in (None, "") for k in ("reported_software", "reported_spend", "reported_staff")):
            stats["with_reports"] += 1
        db.save_crm_lead(business_key, "close", row["lead_id"], data)
        stats["pulled"] += 1
    return stats


# --- CSV for Close's importer (no API key needed) ----------------------------------
CLOSE_CSV_COLUMNS = [
    "Company", "URL", "Description", "Contact Email", "Contact Phone", "Instagram", "Address 1", "City",
    "State", "Zip", "Country", "Beauty Leads ID", "BL Tier", "BL Expected MRR", "BL Our Price",
    "BL Est. Current Spend", "BL Close %", "BL Software", "BL Category", "BL Channel",
]


def close_csv_rows(cfg: Config, assessments: list[Assessment]) -> list[dict]:
    rows = []
    for a in assessments:
        if a.excluded_reason:
            continue
        b = a.business
        rows.append({
            "Company": lead_name(a), "URL": b.website, "Description": a.pitch,
            "Contact Email": next(iter(a.emails), None), "Contact Phone": b.phone,
            "Instagram": f"@{a.instagram}" if a.instagram else None,
            "Address 1": b.street, "City": b.city, "State": b.province or "BC", "Zip": b.postal_code,
            "Country": "CA", "Beauty Leads ID": b.key, "BL Tier": a.tier,
            "BL Expected MRR": round(a.expected_mrr, 2), "BL Our Price": round(a.suggested_price, 2),
            "BL Est. Current Spend": None if a.current_spend is None else round(a.current_spend, 2),
            "BL Close %": round(a.close_probability * 100, 1), "BL Software": a.software_label,
            "BL Category": cfg.categories.get(b.category, {}).get("label", b.category), "BL Channel": a.channel,
        })
    return rows


def dry_run_payloads(cfg: Config, assessments: list[Assessment], tiers=None, limit=None) -> list[dict]:
    fake_ids = {key: f"cf_<{name}>" for key, name in field_names(cfg).items()}
    return [{"lead": lead_payload(cfg, a, fake_ids), "note": note_text(cfg, a)}
            for a in select_for_push(cfg, assessments, tiers, limit)]


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)
