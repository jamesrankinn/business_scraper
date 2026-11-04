"""The decision matrix.

For every business location we estimate, in order:

  staff  ->  annual revenue  ->  software budget ("can they afford it")
  current software  ->  what they pay now
  our price ("how much could we sell it for")
  nine 0-1 factors  ->  weighted fit score  ->  close probability
  expected monthly value (EMV) = our price x close probability

Every step records a plain-English reason so a salesperson can see *why* a
lead scored the way it did. All tunable numbers live in config/*.yaml.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date

from .config import Config
from .enrich.audit import audit_without_site
from .enrich.software import social_kind, software_for_url
from .models import Business
from .normalize import instagram_handle, name_key, normalize_name, url_key

NO_SOFTWARE_STATES = {"unknown", "none_detected", "social_only", "no_website"}


@dataclass
class Assessment:
    business: Business
    software: str
    software_label: str
    software_confidence: str | None
    software_evidence: list[str]
    site_builder: str | None
    socials: dict
    emails: list[str]
    registry: dict | None
    years_registered: float | None
    staff_est: int
    staff_source: str
    annual_revenue_est: float
    software_budget: float
    current_spend: float | None
    current_spend_source: str
    list_price: float
    suggested_price: float
    savings: float | None
    factors: dict[str, float]
    fit_score: float
    close_probability: float
    expected_mrr: float
    potential_mrr: float
    ltv: float
    tier: str
    excluded_reason: str | None
    reasons: list[str] = field(default_factory=list)
    pitch: str = ""
    instagram: str | None = None          # handle, no @
    no_solicitation: str | None = None    # the site's "no solicitation" wording, if any
    channel: str = "none"                 # email | instagram | phone | none
    tools: list[str] = field(default_factory=list)   # other paid tools (Wix, Mailchimp...)
    tools_spend: float = 0.0              # their estimated monthly cost, CAD
    # Website & agency services
    web_status: str | None = None         # No Website | Outdated | Decent | Good
    web_score: int | None = None          # 0-100 audit score
    web_issues: list[str] = field(default_factory=list)
    opportunities: list[str] = field(default_factory=list)   # service labels to pitch
    project_value: float = 0.0            # one-time fees for those services
    services_monthly: float = 0.0         # recurring fees for those services
    web_budget: float = 0.0               # est. website budget from revenue
    web_close_probability: float = 0.0
    expected_project_value: float = 0.0   # web close % x (project + 12 x services monthly)
    first_year_value: float = 0.0         # 12 x expected_mrr + expected_project_value
    rank_value: float = 0.0               # what leads are sorted and tiered by
    crm_status: str | None = None         # lead status in Close, after `close pull`
    crm_url: str | None = None

    @property
    def email_ok(self) -> bool:
        """Published email we may contact under CASL implied consent."""
        return bool(self.emails) and not self.no_solicitation

    @property
    def issues_platform_page(self) -> bool:
        return any("booking page instead of its own website" in i for i in self.web_issues)

    @property
    def replaceable_spend(self) -> float | None:
        """Booking software + other tools: everything an agency could replace."""
        if self.current_spend is None and not self.tools_spend:
            return None
        return (self.current_spend or 0.0) + self.tools_spend

    @property
    def instagram_url(self) -> str | None:
        return f"https://www.instagram.com/{self.instagram}/" if self.instagram else None

    def flat(self) -> dict:
        b = self.business
        return {
            "tier": self.tier,
            "first_year_value_cad": round(self.first_year_value, 2),
            "expected_mrr_cad": round(self.expected_mrr, 2),
            "potential_mrr_cad": round(self.potential_mrr, 2),
            "close_probability": round(self.close_probability, 3),
            "fit_score": round(self.fit_score, 1),
            "name": b.name,
            "category": b.category,
            "city": b.city,
            "address": b.address,
            "phone": b.phone,
            "channel": self.channel,
            "email": "; ".join(self.emails),
            "email_ok": self.email_ok,
            "no_solicitation": self.no_solicitation,
            "instagram": f"@{self.instagram}" if self.instagram else None,
            "instagram_url": self.instagram_url,
            "website": b.website,
            "software": self.software_label,
            "software_confidence": self.software_confidence,
            "software_evidence": " | ".join(self.software_evidence[:2]),
            "current_spend_cad": None if self.current_spend is None else round(self.current_spend, 2),
            "other_tools": ", ".join(self.tools),
            "tools_spend_cad": round(self.tools_spend, 2),
            "replaceable_spend_cad": None if self.replaceable_spend is None else round(self.replaceable_spend, 2),
            "current_spend_source": self.current_spend_source,
            "our_price_cad": round(self.suggested_price, 2),
            "savings_cad": None if self.savings is None else round(self.savings, 2),
            "staff_est": self.staff_est,
            "staff_source": self.staff_source,
            "annual_revenue_est_cad": round(self.annual_revenue_est),
            "software_budget_cad": round(self.software_budget, 2),
            "rating": b.rating,
            "reviews": b.review_count,
            "price_level": (b.price_level or "").replace("PRICE_LEVEL_", "").lower() or None,
            "years_registered": None if self.years_registered is None else round(self.years_registered, 1),
            "entity_type": (self.registry or {}).get("entity_type_label"),
            "chain_locations": b.chain_locations,
            "franchise": b.franchise,
            "site_builder": self.site_builder,
            "ltv_cad": round(self.ltv),
            "excluded_reason": self.excluded_reason,
            "pitch": self.pitch,
            "website_status": self.web_status,
            "website_score": self.web_score,
            "website_issues": " | ".join(self.web_issues),
            "opportunities": ", ".join(self.opportunities),
            "project_value_cad": round(self.project_value),
            "services_monthly_cad": round(self.services_monthly),
            "web_budget_cad": round(self.web_budget),
            "web_close_probability": round(self.web_close_probability, 3),
            "expected_project_value_cad": round(self.expected_project_value, 2),
            "why": " • ".join(self.reasons),
            "google_maps_url": b.google_maps_url,
            "sources": ", ".join(b.sources),
            "chain_key": b.chain_key,
            "business_key": b.key,
            "close_status": self.crm_status,
            "close_url": self.crm_url,
            **{f"f_{k}": round(v, 2) for k, v in self.factors.items()},
        }


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def software_monthly_price(spec: dict, staff: int) -> float:
    """Estimated monthly list price in catalog currency for one location."""
    pricing = spec.get("pricing") or {}
    if pricing.get("tiers"):
        for max_staff, price in pricing["tiers"]:
            if staff <= max_staff:
                return float(price) + float(pricing.get("typical_addons", 0))
        return float(pricing["tiers"][-1][1]) + float(pricing.get("typical_addons", 0))
    base = float(pricing.get("base", 0))
    extra = float(pricing.get("per_extra_staff", 0)) * max(0, staff - int(pricing.get("included_staff", 1)))
    return base + extra + float(pricing.get("typical_addons", 0))


def our_list_price(cfg: Config, staff: int) -> float:
    p = cfg.get("our_product.pricing", {})
    return float(p.get("base_per_location", 79)) + float(p.get("per_extra_staff", 10)) * max(
        0, staff - int(p.get("included_staff", 2))
    )


def resolve_known_software(cfg: Config, text: str | None) -> str | None:
    if not text:
        return None
    t = normalize_name(text)
    for key, spec in {**cfg.software, **cfg.no_software}.items():
        if t in (normalize_name(key), normalize_name(spec.get("label", ""))):
            return key
    for key, spec in cfg.software.items():
        label = normalize_name(spec.get("label", ""))
        if label and (label.split()[0] in t.split() or normalize_name(key) in t):
            return key
    if t in ("none", "paper", "phone", "no software", "nothing", "book"):
        return "none_detected"
    return None


class Scorer:
    def __init__(self, cfg: Config, website_checks: dict[str, dict] | None = None,
                 registry_matches: dict[str, dict] | None = None, today: date | None = None,
                 crm: dict[str, dict] | None = None):
        self.cfg = cfg
        self.checks = website_checks or {}
        self.registry = registry_matches or {}
        self.today = today or date.today()
        self.crm = crm or {}
        s = cfg.get("scoring", {})
        self.weights: dict[str, float] = s.get("weights", {})
        self.fit_exp = float(s.get("fit_exponent", 1.5))
        self.dm = {"independent": 1.0, "small_chain": 0.85, "large_chain": 0.5, "franchise": 0.15,
                   **(s.get("decision_maker") or {})}
        self.p_min = float(s.get("close_probability", {}).get("min", 0.01))
        self.p_max = float(s.get("close_probability", {}).get("max", 0.35))
        self.budget_pct = float(s.get("software_budget_pct_of_revenue", 0.02))
        self.price_mult = s.get("price_level_multiplier", {})
        self.staff_lo, self.staff_hi = s.get("staff_bounds", [1, 40])
        self.tiers = s.get("tiers", {"A": 25, "B": 12, "C": 5})
        self.exclude_franchises = bool(s.get("exclude_franchises", False))
        self.sweet_lo, self.sweet_hi = cfg.get("our_product.sweet_spot_staff", [2, 15])
        self.months = float(cfg.get("our_product.months_retained", 30))
        self.fx = cfg.usd_to_cad
        self.web_openness = {"No Website": 0.8, "Outdated": 0.7, "Decent": 0.35, "Good": 0.15, "unknown": 0.4,
                             **(s.get("web_openness") or {})}
        self.web_budget_pct = float(cfg.get("our_product.web_budget_pct_of_revenue", 0.015))
        self.rank_by = s.get("rank_by", "first_year")
        self.first_year_tiers = s.get("first_year_tiers", {"A": 1200, "B": 750, "C": 350})
        self.reach = {"email": 0.4, "instagram": 0.35, "phone": 0.15, "website": 0.1,
                      **(s.get("reachability") or {})}

    # --- inputs --------------------------------------------------------------
    def software_for(self, b: Business) -> tuple[str, str | None, list[str], dict]:
        known = resolve_known_software(self.cfg, b.extra.get("known_software"))
        check = self.checks.get(url_key(b.website) or "", {}) if b.website else {}
        if known:
            return known, "reported", [f"reported: {b.extra.get('known_software')}"], check
        if not b.website:
            return "no_website", None, [], {}
        if not check:
            # Not crawled yet, but the URL alone can be conclusive:
            # vagaro.com/yourshop, or an Instagram page as the "website".
            key, strong = software_for_url(self.cfg, b.website)
            if key and strong:
                return key, "high", [f"listed website: {b.website}"], {}
            if social_kind(b.website) in ("instagram", "facebook", "tiktok"):
                return "social_only", None, [], {"socials": {social_kind(b.website): b.website}}
            return "unknown", None, [], {}
        return check.get("software") or "unknown", check.get("confidence"), check.get("evidence", []), check

    def estimate_staff(self, b: Business) -> tuple[int, str]:
        cat = self.cfg.categories.get(b.category, {})
        default = float(cat.get("default_staff", 3))
        if b.num_employees:
            src = "reported" if any(s.startswith(("csv", "close")) for s in b.sources) else "business licence"
            return int(_clamp(b.num_employees, self.staff_lo, self.staff_hi)), src
        if b.review_count:
            rps = float(cat.get("reviews_per_staff", 60))
            w = _clamp(b.review_count / 200.0)
            est = (1 - w) * default + w * (b.review_count / rps)
            return int(_clamp(round(est), self.staff_lo, self.staff_hi)), f"rough, from {b.review_count} Google reviews"
        return int(default), f"category default ({cat.get('label', b.category)})"

    def registry_for(self, b: Business) -> tuple[dict | None, float | None]:
        m = self.registry.get(name_key(b.name)) or {}
        entry = m.get("match")
        if not entry:
            # Registry-only source records can also carry registration data.
            reg = b.extra.get("registration_date")
            entry = {"registration_date": reg, "entity_type_label": b.extra.get("entity_type_label"),
                     "incorporated": b.extra.get("incorporated")} if reg else None
        years = None
        if entry and entry.get("registration_date"):
            try:
                y, mth, d = (int(x) for x in entry["registration_date"][:10].split("-"))
                years = (self.today - date(y, mth, d)).days / 365.25
            except ValueError:
                years = None
        return entry, years

    # --- the matrix ----------------------------------------------------------
    def apply_crm(self, b: Business) -> tuple[Business, dict]:
        """What you recorded in Close beats any estimate."""
        data = (self.crm.get(b.key) or {}).get("data") or {}
        if not data:
            return b, {}
        extra = dict(b.extra)
        if data.get("reported_software"):
            extra["known_software"] = data["reported_software"]
        if data.get("reported_spend") not in (None, ""):
            extra["known_monthly_spend_cad"] = float(data["reported_spend"])
        staff = b.num_employees
        if data.get("reported_staff") not in (None, ""):
            staff = int(float(data["reported_staff"]))
        reported = any(data.get(k) not in (None, "") for k in ("reported_software", "reported_spend", "reported_staff"))
        sources = b.sources + ["close"] if reported else b.sources
        return replace(b, extra=extra, num_employees=staff, sources=sources), data

    def assess(self, b: Business) -> Assessment:
        b, crm = self.apply_crm(b)
        cfg, reasons = self.cfg, []
        cat = cfg.categories.get(b.category, {})
        software, confidence, evidence, check = self.software_for(b)
        sw_spec = cfg.software.get(software) or {}
        no_sw_spec = cfg.no_software.get(software) or {}
        label = cfg.software_label(software)

        staff, staff_src = self.estimate_staff(b)
        mult = float(self.price_mult.get(b.price_level or "", 1.0))
        revenue = staff * float(cat.get("revenue_per_staff_cad", 70000)) * mult
        budget = revenue * self.budget_pct / 12.0
        reasons.append(f"~{staff} staff ({staff_src})")

        # What they pay now.
        known_spend = b.extra.get("known_monthly_spend_cad")
        if known_spend:
            current, spend_src = float(known_spend), "reported"
        elif sw_spec:
            current, spend_src = software_monthly_price(sw_spec, staff) * self.fx, f"{sw_spec.get('confidence', 'low')}-confidence estimate"
        elif software in ("none_detected", "social_only", "no_website"):
            current, spend_src = 0.0, "no software found"
        else:
            current, spend_src = None, "unknown"
        if current:
            reasons.append(f"{label} costs them ~${current:,.0f}/mo ({spend_src})")
        elif software != "unknown":
            reasons.append(label)

        # What we'd charge.
        p = cfg.get("our_product.pricing", {})
        list_price = our_list_price(cfg, staff)
        min_price = float(p.get("min_monthly_price", 49))
        if p.get("strategy", "undercut") == "undercut" and current:
            anchor = current * (1 - float(p.get("undercut_pct", 0.2)))
            floor = max(min_price, list_price * float(p.get("min_of_list", 0.6)))
            price = _clamp(anchor, floor, max(floor, list_price * float(p.get("max_over_list", 1.5))))
        else:
            price = max(list_price, min_price)
        savings = (current - price) if current is not None else None

        # Factors (each 0-1).
        f: dict[str, float] = {}
        f["switchability"] = float(sw_spec.get("switchability", no_sw_spec.get("switchability", 0.5)))

        if current is None:
            f["savings"] = 0.5
        elif current == 0:
            f["savings"] = 0.35  # we'd be a new cost, not a saving
        else:
            pct = (current - price) / current
            f["savings"] = _clamp(0.2 + 2.0 * pct)
            if savings and savings > 0:
                reasons.append(f"we'd save them ~${savings:,.0f}/mo at ${price:,.0f}")
            else:
                reasons.append(f"we'd cost ${-savings:,.0f}/mo more than now: sell on features")

        f["affordability"] = _clamp((budget / price) / 2.0) if price else 1.0
        if budget < price:
            reasons.append(f"est. software budget ${budget:,.0f}/mo is below our ${price:,.0f}")

        if b.franchise:
            f["decision_maker"] = float(self.dm["franchise"])
            reasons.append(f"franchise ({b.franchise}): software likely chosen by head office")
        elif b.chain_locations >= 6:
            f["decision_maker"] = float(self.dm["large_chain"])
            reasons.append(f"{b.chain_locations}-location chain: longer sales cycle")
        elif b.chain_locations >= 2:
            f["decision_maker"] = float(self.dm["small_chain"])
            reasons.append(f"{b.chain_locations}-location account: one deal, several locations")
        else:
            f["decision_maker"] = float(self.dm["independent"])

        if self.sweet_lo <= staff <= self.sweet_hi:
            f["size_fit"] = 1.0
        elif staff < self.sweet_lo:
            f["size_fit"] = _clamp(staff / self.sweet_lo, 0.3)
        else:
            f["size_fit"] = _clamp(self.sweet_hi / staff, 0.3)

        if b.review_count:
            vol = _clamp(math.log10(1 + b.review_count) / math.log10(1 + 300))
            if b.rating is not None and b.rating < 3.5:
                vol *= 0.7
            f["business_volume"] = vol
        else:
            f["business_volume"] = 0.4

        entry, years = self.registry_for(b)
        if years is None:
            stab = 0.5
        elif years < 1:
            stab = 0.3
        elif years < 2:
            stab = 0.6
        elif years < 5:
            stab = 0.85
        else:
            stab = 1.0
        if years is not None:
            reasons.append(f"registered {years:.0f} yr ago" if years >= 1 else "registered < 1 yr ago")
        if entry and entry.get("incorporated"):
            stab = min(1.0, stab + 0.1)
        if (b.business_status or "").upper() == "CLOSED_TEMPORARILY":
            stab = 0.2
        f["stability"] = stab

        emails = list(dict.fromkeys((b.emails or []) + (check.get("emails") or [])))
        socials = dict(check.get("socials") or {})
        instagram = None
        for candidate in (b.extra.get("instagram"), socials.get("instagram"), b.extra.get("contact:instagram"),
                          b.website if social_kind(b.website) == "instagram" else None):
            instagram = instagram_handle(candidate)
            if instagram:
                break
        if instagram:
            socials["instagram"] = f"https://www.instagram.com/{instagram}/"
        tool_specs = cfg.catalog.get("tools") or {}
        tools = [t for t in (check.get("tools") or []) if t in tool_specs]
        tools_spend = sum(float(tool_specs[t].get("monthly", 0)) for t in tools) * self.fx
        if tools:
            labels = ", ".join(tool_specs[t].get("label", t) for t in tools)
            reasons.append(f"also uses {labels}" + (f" (~${tools_spend:,.0f}/mo)" if tools_spend else ""))
        no_solicit = check.get("no_solicitation")
        email_ok = bool(emails) and not no_solicit
        if no_solicit:
            reasons.append(f"site says \"{no_solicit}\": do not cold-email")
        r = self.reach
        f["reachability"] = min(1.0, (r["email"] if email_ok else 0) + (r["instagram"] if instagram else 0)
                                + (r["phone"] if b.phone else 0) + (r["website"] if b.website else 0))
        channel = "email" if email_ok else "instagram" if instagram else "phone" if b.phone else "none"
        f["category_fit"] = float(cat.get("fit", 0.8))

        # Gates multiply; everything else is a weighted average ("fit").
        gates = ("switchability", "decision_maker")
        total_w = sum(self.weights.get(k, 0) for k in f if k not in gates) or 1.0
        fit = sum(f[k] * self.weights.get(k, 0) for k in f if k not in gates) / total_w
        prob = self.p_min + (self.p_max - self.p_min) * f["switchability"] * f["decision_maker"] * fit ** self.fit_exp

        excluded = None
        if (b.business_status or "").upper() == "CLOSED_PERMANENTLY":
            excluded = "permanently closed"
        elif b.franchise and self.exclude_franchises:
            excluded = f"franchise ({b.franchise})"
        if excluded:
            prob = 0.0

        emv = price * prob

        # --- Website & agency services ---------------------------------------
        audit = None
        if b.website and (check.get("status") == "platform_url" or software_for_url(cfg, b.website)[1]):
            audit = {"score": 0, "status": "No Website", "has_shop": False,
                     "issues": [f"Uses a {label} booking page instead of its own website"]}
        audit = audit or check.get("audit") or audit_without_site(check, software)
        web_status = (audit or {}).get("status")
        web_score = (audit or {}).get("score")
        web_issues = list((audit or {}).get("issues") or [])
        if web_status:
            detail = "; ".join(web_issues[:2]) if web_status != "Good" else ""
            shown = f" ({web_score}/100)" if web_score is not None and web_status != "No Website" else ""
            reasons.append(f"website {web_status}{shown}" + (f": {detail}" if detail else ""))
        opp_keys = self.opportunities(b, software, current, f["switchability"], tools, audit)
        services = cfg.get("our_product.services", {}) or {}
        project_value = sum(float(services[k].get("project", 0)) for k in opp_keys)
        services_monthly = sum(float(services[k].get("monthly", 0)) for k in opp_keys)
        web_budget = revenue * self.web_budget_pct
        openness = float(self.web_openness.get(web_status or "unknown", self.web_openness["unknown"]))
        p_web = self.p_min + (self.p_max - self.p_min) * openness * f["decision_maker"] * fit ** self.fit_exp
        if project_value > 0:
            p_web *= _clamp(web_budget / project_value, 0.4, 1.0)
            if web_budget < project_value:
                reasons.append(f"est. website budget ~${web_budget:,.0f} vs ${project_value:,.0f} of projects")
        if excluded:
            p_web = 0.0
        expected_project = p_web * (project_value + 12 * services_monthly)
        first_year = 12 * emv + expected_project

        if self.rank_by == "monthly":
            rank_value, thresholds = emv, self.tiers
        else:
            rank_value, thresholds = first_year, self.first_year_tiers
        tier = "X" if excluded else next(
            (t for t, thr in sorted(thresholds.items(), key=lambda kv: -kv[1]) if rank_value >= thr), "D")
        pitch = sw_spec.get("pitch") or no_sw_spec.get("pitch") or ""

        return Assessment(
            business=b, software=software, software_label=label, software_confidence=confidence,
            software_evidence=evidence, site_builder=check.get("site_builder"), socials=socials, emails=emails,
            registry=entry, years_registered=years, staff_est=staff, staff_source=staff_src,
            annual_revenue_est=revenue, software_budget=budget, current_spend=current,
            current_spend_source=spend_src, list_price=list_price, suggested_price=price, savings=savings,
            factors=f, fit_score=fit * 100, close_probability=prob, expected_mrr=emv, potential_mrr=price,
            ltv=price * self.months, tier=tier, excluded_reason=excluded, reasons=reasons, pitch=pitch,
            instagram=instagram, no_solicitation=no_solicit, channel=channel,
            crm_status=crm.get("status_label"), crm_url=crm.get("html_url"),
            tools=[tool_specs[t].get("label", t) for t in tools], tools_spend=tools_spend,
            web_status=web_status, web_score=web_score, web_issues=web_issues,
            opportunities=[services[k].get("label", k) for k in opp_keys], project_value=project_value,
            services_monthly=services_monthly, web_budget=web_budget, web_close_probability=p_web,
            expected_project_value=expected_project, first_year_value=first_year, rank_value=rank_value,
        )

    def opportunities(self, b: Business, software: str, current: float | None, switch: float,
                      tools: list[str], audit: dict | None) -> list[str]:
        """Which of your services this business is a candidate for (keys of our_product.services)."""
        p = self.cfg.get("our_product", {}) or {}
        services = p.get("services", {}) or {}
        status = (audit or {}).get("status")
        issues = " ".join((audit or {}).get("issues") or []).lower()
        tools_set = set(tools)
        out: list[str] = []

        def add(key: str) -> None:
            if key in services and key not in out:
                out.append(key)

        if status == "No Website":
            add("new_website")
        elif status == "Outdated":
            add("website_redesign")
        if software in ("none_detected", "social_only", "no_website"):
            add("online_booking")
        if current and (switch >= 0.5 or len(tools_set) >= 2):
            add("tool_replacement")
        if b.category in (p.get("intake_categories") or []) and not tools_set & {"jotform", "typeform"}:
            add("forms_intake")
        if b.category in (p.get("membership_categories") or []):
            add("memberships")
        if (audit or {}).get("has_shop") and not tools_set & {"shopify", "square_online"}:
            add("payments")
        if not tools_set & {"mailchimp", "klaviyo"} and (b.review_count or 0) >= 150:
            add("email_marketing")
        if status in ("Outdated", "Decent") and any(w in issues for w in ("page title", "meta description", "schema")):
            add("seo")
        if status in ("No Website", "Outdated"):
            add("hosting")
        return out

    def assess_all(self, businesses) -> list[Assessment]:
        out = [self.assess(b) for b in businesses]
        out.sort(key=lambda a: (-a.rank_value, a.business.name))
        return out


# --- roll-ups --------------------------------------------------------------
def accounts(assessments: list[Assessment]) -> list[dict]:
    groups: dict[str, list[Assessment]] = defaultdict(list)
    for a in assessments:
        groups[a.business.chain_key or a.business.key].append(a)
    rows = []
    for key, items in groups.items():
        items.sort(key=lambda a: -a.expected_mrr)
        top = items[0]
        tiers = sorted({a.tier for a in items})
        rows.append({
            "account": top.business.name,
            "locations": len(items),
            "cities": ", ".join(sorted({a.business.city for a in items if a.business.city})),
            "category": top.business.category,
            "software": ", ".join(sorted({a.software_label for a in items})),
            "first_year_value_cad": round(sum(a.first_year_value for a in items), 2),
            "expected_mrr_cad": round(sum(a.expected_mrr for a in items), 2),
            "potential_mrr_cad": round(sum(a.potential_mrr for a in items), 2),
            "project_value_cad": round(sum(a.project_value for a in items)),
            "website_status": ", ".join(sorted({a.web_status for a in items if a.web_status})),
            "opportunities": ", ".join(dict.fromkeys(o for a in items for o in a.opportunities)),
            "current_spend_cad": round(sum(a.current_spend or 0 for a in items), 2),
            "best_tier": tiers[0] if tiers else "D",
            "phone": next((a.business.phone for a in items if a.business.phone), None),
            "email": next((a.emails[0] for a in items if a.email_ok), None),
            "instagram": next((f"@{a.instagram}" for a in items if a.instagram), None),
            "website": next((a.business.website for a in items if a.business.website), None),
            "franchise": top.business.franchise,
            "pitch": top.pitch,
            "account_key": key,
            "_rank": sum(a.rank_value for a in items),
        })
    rows.sort(key=lambda r: -r["_rank"])
    for r in rows:
        r.pop("_rank")
    return rows


def market_summary(assessments: list[Assessment]) -> dict:
    live = [a for a in assessments if not a.excluded_reason]
    by_sw: dict[str, dict] = defaultdict(lambda: {"locations": 0, "current_spend_cad": 0.0, "potential_mrr_cad": 0.0, "expected_mrr_cad": 0.0})
    by_cat: dict[str, dict] = defaultdict(lambda: {"locations": 0, "expected_mrr_cad": 0.0})
    by_city: dict[str, dict] = defaultdict(lambda: {"locations": 0, "expected_mrr_cad": 0.0})
    by_tier: dict[str, int] = defaultdict(int)
    by_web: dict[str, int] = defaultdict(int)
    by_opp: dict[str, dict] = defaultdict(lambda: {"locations": 0, "project_value_cad": 0.0})
    for a in assessments:
        by_tier[a.tier] += 1
    for a in live:
        s = by_sw[a.software_label]
        s["locations"] += 1
        s["current_spend_cad"] += a.current_spend or 0
        s["potential_mrr_cad"] += a.potential_mrr
        s["expected_mrr_cad"] += a.expected_mrr
        by_cat[a.business.category]["locations"] += 1
        by_cat[a.business.category]["expected_mrr_cad"] += a.expected_mrr
        by_city[a.business.city or "Unknown"]["locations"] += 1
        by_city[a.business.city or "Unknown"]["expected_mrr_cad"] += a.expected_mrr
        by_web[a.web_status or "Not checked"] += 1
        services = a.opportunities
        for o in services:
            by_opp[o]["locations"] += 1

    def rnd(d):
        return {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in d.items()}

    return {
        "locations": len(assessments),
        "live_locations": len(live),
        "accounts": len({a.business.chain_key or a.business.key for a in live}),
        "potential_mrr_cad": round(sum(a.potential_mrr for a in live), 2),
        "expected_mrr_cad": round(sum(a.expected_mrr for a in live), 2),
        "first_year_value_cad": round(sum(a.first_year_value for a in live), 2),
        "expected_project_value_cad": round(sum(a.expected_project_value for a in live), 2),
        "needs_website": sum(1 for a in live if a.web_status in ("No Website", "Outdated")),
        "by_website_status": dict(sorted(by_web.items(), key=lambda kv: -kv[1])),
        "by_opportunity": {k: v["locations"] for k, v in sorted(by_opp.items(), key=lambda kv: -kv[1]["locations"])},
        "software_identified": sum(1 for a in live if a.software not in NO_SOFTWARE_STATES),
        "software_checked": sum(1 for a in live if a.software != "unknown"),
        "by_software": dict(sorted(rnd(by_sw).items(), key=lambda kv: -kv[1]["locations"])),
        "by_category": dict(sorted(rnd(by_cat).items(), key=lambda kv: -kv[1]["locations"])),
        "by_city": dict(sorted(rnd(by_city).items(), key=lambda kv: -kv[1]["expected_mrr_cad"])),
        "by_tier": dict(sorted(by_tier.items())),
    }


def assessment_dict(a: Assessment) -> dict:
    d = asdict(a)
    d["business"] = a.business.to_dict()
    return d
