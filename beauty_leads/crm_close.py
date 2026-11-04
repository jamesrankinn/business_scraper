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
from datetime import date

import requests

from .config import Config
from .normalize import normalize_name
from .outreach import draft
from .scoring import NO_SOFTWARE_STATES, Assessment

log = logging.getLogger(__name__)

CLOSE_API = "https://api.close.com/api/v1"

# (our key, default Close field name, type, choices, written on push?,
#  concept of your own existing field that makes this one unnecessary)
FIELD_SPECS = [
    ("bl_id", "Beauty Leads ID", "text", None, True, None),
    ("tier", "BL Tier", "choices", ["A", "B", "C", "D", "X"], True, None),
    ("expected", "BL Expected MRR", "number", None, True, None),
    ("price", "BL Our Price", "number", None, True, None),
    ("spend", "BL Est. Current Spend", "number", None, True, "spend"),
    ("close_pct", "BL Close %", "number", None, True, None),
    ("software", "BL Software", "text", None, True, None),
    ("category", "BL Category", "text", None, True, None),
    ("channel", "BL Channel", "choices", ["email", "instagram", "phone", "none"], True, None),
    ("instagram", "Instagram", "text", None, True, None),
    # Filled in by you during calls; read back by `close pull`.
    ("reported_software", "Reported Software", "text", None, False, "tools"),
    ("reported_spend", "Reported Monthly Spend", "number", None, False, "spend"),
    ("reported_staff", "Reported Staff", "number", None, False, None),
]


@dataclass
class Schema:
    ids: dict        # our key -> Close custom field id (fields this tool owns)
    existing: dict   # concept -> your own Close field {id, name, type, choices, ...}


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

    def ensure_fields(self, cfg: Config) -> Schema:
        """Find your existing fields to reuse, and create the tool's own fields that are missing."""
        names = field_names(cfg)
        by_name = {f["name"].strip().lower(): f for f in self.lead_fields()}
        existing = {}
        for concept, name in (cfg.get("close.existing_fields") or {}).items():
            if name and name.strip().lower() in by_name:
                existing[concept] = by_name[name.strip().lower()]
        ids = {}
        for key, _default, ftype, choices, _, replaced_by in FIELD_SPECS:
            if replaced_by and replaced_by in existing:
                continue  # your own field already covers this
            name = names[key]
            if name.lower() in by_name:
                ids[key] = by_name[name.lower()]["id"]
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
        return Schema(ids=ids, existing=existing)

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


def _allowed(field: dict, value):
    """Keep a value only if the field accepts it (choices fields)."""
    if value in (None, "", []):
        return None
    if field.get("type") == "choices" and field.get("choices") is not None:
        if isinstance(value, list):
            value = [v for v in value if v in field["choices"]]
            return value or None
        return value if value in field["choices"] else None
    return value


def tool_choices(cfg: Config, a: Assessment, field: dict) -> list[str]:
    """Booking software + other tools, as choices of your "Current Paid Tools" field."""
    c = cfg.get("close", {}) or {}
    mapping, other = c.get("tool_choices", {}) or {}, c.get("other_tool_choice", "Other")
    label_to_key = {spec.get("label", k): k for k, spec in (cfg.catalog.get("tools") or {}).items()}
    keys = ([a.software] if a.software not in NO_SOFTWARE_STATES else []) + [label_to_key.get(t, t) for t in a.tools]
    out: list[str] = []
    for key in keys:
        choice = _allowed(field, mapping.get(key, other))
        if choice and choice not in out:
            out.append(choice)
    return out


def existing_values(cfg: Config, a: Assessment, existing: dict, today: date | None = None) -> dict:
    """Values for fields that already exist in your Close. Written on new leads only."""
    c, b, out = cfg.get("close", {}) or {}, a.business, {}

    def put(concept: str, value) -> None:
        field = existing.get(concept)
        if field is not None:
            value = _allowed(field, value)
            if value is not None:
                out[f"custom.{field['id']}"] = value

    if "tools" in existing:
        choices = tool_choices(cfg, a, existing["tools"])
        if choices:
            put("tools", choices if existing["tools"].get("accepts_multiple_values") else choices[0])
    if a.replaceable_spend is not None:
        put("spend", round(a.replaceable_spend, 2))
    put("industry", c.get("industry_value"))
    origins = c.get("source_by_origin", {}) or {}
    put("source", origins.get("google") if "google" in b.sources else origins.get("default"))
    if a.software in ("no_website", "social_only"):
        put("website_status", "No Website")
    put("priority", (c.get("priority_by_tier", {}) or {}).get(a.tier))
    put("batch", f"{(today or date.today()).isoformat()} {c.get('batch_label', 'beauty-leads')}")
    return out


def lead_payload(cfg: Config, a: Assessment, schema: Schema) -> dict:
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
        **score_fields(cfg, a, schema.ids),
        **existing_values(cfg, a, schema.existing),
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
    if a.tools:
        spend += f"; also {', '.join(a.tools)}" + (f" ~${a.tools_spend:,.0f}/mo" if a.tools_spend else "")
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
    lines += ["", "After talking to them, correct their tools, monthly spend and staff in Close, "
                  "then run `beauty-leads close pull` so the scores use the real numbers."]
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
    schema = client.ensure_fields(cfg)
    known = db.crm_leads()
    result = PushResult()
    for a in select_for_push(cfg, assessments, tiers, limit):
        b = a.business
        try:
            data = dict((known.get(b.key) or {}).get("data") or {})
            lead_id = (known.get(b.key) or {}).get("lead_id")
            how = "mapped" if lead_id else ""
            if not lead_id:
                lead_id, how = client.find_lead(schema.ids, b.key, lead_name(a))
            if lead_id:
                # Existing lead: refresh only our score fields; leave everything you typed alone.
                client.update_lead(lead_id, score_fields(cfg, a, schema.ids))
                if how == "name":
                    result.linked += 1
                    client.add_note(lead_id, note_text(cfg, a))
                else:
                    result.updated += 1
            else:
                payload = lead_payload(cfg, a, schema)
                lead_id = client.create_lead(payload)["id"]
                client.add_note(lead_id, note_text(cfg, a))
                result.created += 1
                # Remember what we wrote into your fields, so `pull` can tell your edits from ours.
                data["pushed"] = {c: payload.get(f"custom.{f['id']}") for c, f in schema.existing.items()
                                  if c in ("tools", "spend")}
            db.save_crm_lead(b.key, "close", lead_id, data)
        except CloseError as exc:
            log.error("Close push failed for %s: %s", b.name, exc)
            result.errors += 1
            if "401" in str(exc) or "custom field" in str(exc):
                raise
    return result


def _booking_from_tools(cfg: Config, tools) -> str | None:
    """'Vagaro' in your Current Paid Tools -> our 'vagaro' key."""
    reverse = cfg.get("close.booking_choices", {}) or {}
    for t in tools if isinstance(tools, list) else [tools]:
        if t in reverse:
            return reverse[t]
    return None


def pull(cfg: Config, db, client: CloseClient) -> dict:
    schema = client.ensure_fields(cfg)
    ids, existing = schema.ids, schema.existing
    fields = ["id", "display_name", "status_label", "html_url"]
    fields += [f"custom.{ids[k]}" for k in ("reported_software", "reported_spend", "reported_staff") if k in ids]
    fields += [f"custom.{existing[c]['id']}" for c in ("tools", "spend") if c in existing]
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
        old = row.get("data") or {}
        pushed = old.get("pushed") or {}
        get = lambda key: lead.get(f"custom.{ids[key]}") if key in ids else None  # noqa: E731
        data = {
            "status_label": lead.get("status_label"),
            "html_url": lead.get("html_url"),
            "reported_software": get("reported_software"),
            "reported_spend": get("reported_spend"),
            "reported_staff": get("reported_staff"),
            "pushed": pushed,
        }
        # Your own fields count as "reported" once you change them from what we pushed.
        if "tools" in existing:
            now = lead.get(f"custom.{existing['tools']['id']}")
            if now and now != pushed.get("tools"):
                data["reported_software"] = _booking_from_tools(cfg, now) or data["reported_software"]
        if "spend" in existing:
            now = lead.get(f"custom.{existing['spend']['id']}")
            if now not in (None, "") and now != pushed.get("spend"):
                data["reported_spend"] = now
        if any(data[k] not in (None, "") for k in ("reported_software", "reported_spend", "reported_staff")):
            stats["with_reports"] += 1
        db.save_crm_lead(business_key, "close", row["lead_id"], data)
        stats["pulled"] += 1
    return stats


# --- CSV for Close's importer (no API key needed) ----------------------------------
CLOSE_CSV_COLUMNS = [
    "Company", "URL", "Description", "Contact Email", "Contact Phone", "Instagram", "Address 1", "City",
    "State", "Zip", "Country", "Beauty Leads ID", "BL Tier", "BL Expected MRR", "BL Our Price",
    "BL Close %", "BL Software", "BL Category", "BL Channel",
    # Your own Close fields (names from close.existing_fields):
    "Current Paid Tools", "Est. Monthly Software Spend", "Industry", "Lead Source", "Website Status",
    "Priority", "Import Batch",
]


def _existing_csv(cfg: Config, a: Assessment) -> dict:
    """Your own fields as plain CSV columns (multi-value tools joined with commas)."""
    names = cfg.get("close.existing_fields") or {}
    fake = {c: {"id": c, "type": None, "choices": None, "accepts_multiple_values": True} for c in names}
    vals = {k.split(".", 1)[1]: v for k, v in existing_values(cfg, a, fake).items()}
    column = {"tools": "Current Paid Tools", "spend": "Est. Monthly Software Spend", "industry": "Industry",
              "source": "Lead Source", "website_status": "Website Status", "priority": "Priority",
              "batch": "Import Batch"}
    return {column[c]: (", ".join(v) if isinstance(v, list) else v) for c, v in vals.items() if c in column}


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
            "BL Close %": round(a.close_probability * 100, 1), "BL Software": a.software_label,
            "BL Category": cfg.categories.get(b.category, {}).get("label", b.category), "BL Channel": a.channel,
            **_existing_csv(cfg, a),
        })
    return rows


def dry_run_payloads(cfg: Config, assessments: list[Assessment], tiers=None, limit=None) -> list[dict]:
    """Payloads as they would be sent, assuming your Close has the fields in close.existing_fields."""
    existing = {c: {"id": f"<{n}>", "name": n, "type": None, "choices": None,
                    "accepts_multiple_values": c == "tools"}
                for c, n in (cfg.get("close.existing_fields") or {}).items() if n}
    names = field_names(cfg)
    ids = {key: f"<{names[key]}>" for key, *_spec, replaced_by in FIELD_SPECS
           if not (replaced_by and replaced_by in existing)}
    schema = Schema(ids=ids, existing=existing)
    return [{"lead": lead_payload(cfg, a, schema), "note": note_text(cfg, a)}
            for a in select_for_push(cfg, assessments, tiers, limit)]


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)
