"""Turn scored leads into outreach lists with drafted emails and Instagram DMs.

Nothing here sends messages. You review and send each one yourself:
* Instagram forbids automated DMs and restricts accounts that send them.
* Canada's anti-spam law (CASL) allows emailing a *published* business
  address about something relevant to the business, unless the site says it
  doesn't want unsolicited messages (those leads are routed away from email),
  and every email must identify you and offer an unsubscribe.
"""

from __future__ import annotations

from .config import Config
from .scoring import NO_SOFTWARE_STATES, Assessment


class _Blank(dict):
    def __missing__(self, key):  # unknown placeholder -> leave it visible
        return "{" + key + "}"


def _money(x: float | None) -> str:
    if x is None:
        return "?"
    return f"${round(x / 5) * 5:,.0f}"


def message_group(cfg: Config, a: Assessment) -> str:
    if a.software in ("none_detected", "social_only", "no_website"):
        return "no_booking"
    if a.software in NO_SOFTWARE_STATES or not a.current_spend:
        return "unknown"
    switch = float((cfg.software.get(a.software) or {}).get("switchability", 0.5))
    if a.savings is not None and a.savings > 0:
        return "premium" if switch <= 0.3 else "save_money"
    return "features"


def _sender(cfg: Config) -> dict:
    s = cfg.outreach.get("sender", {}) or {}
    contact = " · ".join(x for x in (s.get("email"), s.get("phone"), s.get("website")) if x)
    return {
        "sender_name": s.get("name", ""),
        "sender_business": s.get("business", ""),
        "sender_contact": contact,
        "sender_address": s.get("mailing_address", ""),
    }


def sender_is_configured(cfg: Config) -> bool:
    return not any("CHANGE-ME" in str(v) for v in (cfg.outreach.get("sender") or {}).values())


def draft(cfg: Config, a: Assessment) -> dict:
    b = a.business
    cat = cfg.categories.get(b.category, {})
    noun = cat.get("noun") or cat.get("label", "business").lower()
    group = message_group(cfg, a)
    tpl = (cfg.outreach.get("templates") or {}).get(group, {})
    values = _Blank(
        business=b.name, city=b.city or "BC", noun=noun, nouns=noun + "s", staff=a.staff_est,
        software=a.software_label, spend=_money(a.current_spend), price=_money(a.suggested_price),
        savings=_money(a.savings) if a.savings and a.savings > 0 else "$0", **_sender(cfg),
    )
    body = (tpl.get("email_body", "") + (cfg.outreach.get("email_footer") or "")).format_map(values)
    return {
        "message_group": group,
        "email_subject": tpl.get("email_subject", "").format_map(values),
        "email_body": body.strip() + "\n",
        "dm_text": " ".join(tpl.get("dm", "").format_map(values).split()),
    }


def outreach_rows(cfg: Config, assessments: list[Assessment]) -> list[dict]:
    rows = []
    for a in assessments:
        if a.excluded_reason or a.channel == "none":
            continue
        b = a.business
        rows.append({
            "tier": a.tier,
            "expected_mrr_cad": round(a.expected_mrr, 2),
            "business": b.name,
            "city": b.city,
            "category": cfg.categories.get(b.category, {}).get("label", b.category),
            "channel": a.channel,
            "email": next(iter(a.emails), None) if a.email_ok else None,
            "instagram": f"@{a.instagram}" if a.instagram else None,
            "instagram_url": a.instagram_url,
            "phone": b.phone,
            "software": a.software_label,
            "current_spend_cad": None if a.current_spend is None else round(a.current_spend),
            "our_price_cad": round(a.suggested_price),
            "no_solicitation": a.no_solicitation,
            **draft(cfg, a),
            "website": b.website,
            "google_maps_url": b.google_maps_url,
            "business_key": b.key,
            "close_status": a.crm_status,
        })
    rows.sort(key=lambda r: -r["expected_mrr_cad"])
    return rows
