"""Website audit: how much would this business benefit from a new or redesigned site?

Scores the homepage 0-100 from things a salon owner (and their clients)
would notice: mobile-friendliness, "Not secure" warnings, a stale copyright
year, missing Google basics, no tap-to-call, old code, a parked page. Every
deduction becomes a plain-English issue you can quote in a pitch.

Status buckets match the "Website Status" field in Close:
  No Website  none, Instagram-only, parked / "coming soon" page
  Outdated    score below scoring.website_status.decent (default 50)
  Decent      below scoring.website_status.good (default 75)
  Good        everything else
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

YEAR_RE = re.compile(r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}\s*[-–—]\s*)?((?:19|20)\d{2})", re.I)
PARKED_RE = re.compile(
    r"(domain (?:is|may be) for sale|buy this domain|this domain (?:is parked|has expired)|parked free"
    r"|website (?:is )?coming soon|site (?:is )?under construction|launching soon|future home of)",
    re.I,
)
BUSINESS_SCHEMA_RE = re.compile(
    r'"@type"\s*:\s*\[?\s*"(LocalBusiness|HairSalon|BeautySalon|NailSalon|DaySpa|HealthAndBeautyBusiness'
    r'|TattooParlor|MedicalSpa|MedicalBusiness|Store|Organization)"', re.I)
OLD_TECH = [
    (re.compile(r"<font[\s>]", re.I), "<font> tags"),
    (re.compile(r"<marquee", re.I), "scrolling <marquee> text"),
    (re.compile(r"<frameset|<frame\s", re.I), "frames"),
    (re.compile(r"\.swf\b|shockwave-flash", re.I), "Flash"),
    (re.compile(r"jquery[-.]?1\.\d+(?:\.\d+)?(?:\.min)?\.js|jquery/1\.\d", re.I), "jQuery 1.x"),
]
GENERIC_TITLES = {"", "home", "index", "untitled", "welcome", "home page", "homepage", "new page", "my site"}
SHOP_RE = re.compile(r"\b(shop|store|products|cart|gift cards?)\b", re.I)

# (points, issue) deductions
P = {
    "https": 20, "mobile": 25, "copyright_old": 15, "copyright_ancient": 20, "old_tech": 10,
    "old_wordpress": 10, "no_booking": 10, "no_tel": 5, "title": 5, "description": 5, "builder": 5,
    "no_h1": 3, "schema": 3, "alt": 3, "og": 2, "no_instagram": 2,
}


def status_for(score: int | None, cfg_thresholds: dict | None = None) -> str | None:
    if score is None:
        return None
    t = {"good": 75, "decent": 50, **(cfg_thresholds or {})}
    return "Good" if score >= t["good"] else "Decent" if score >= t["decent"] else "Outdated"


def audit_page(html: str, final_url: str, software: str | None = None, tools: list[str] | None = None,
               today: date | None = None, thresholds: dict | None = None) -> dict:
    today = today or date.today()
    soup = BeautifulSoup(html or "", "html.parser")
    text = " ".join(soup.get_text(" ").split())
    lower = (html or "").lower()
    issues: list[str] = []
    score = 100

    def deduct(key: str, issue: str) -> None:
        nonlocal score
        score -= P[key]
        issues.append(issue)

    # A placeholder page is not really a website.
    if PARKED_RE.search(text) and len(text) < 2500:
        return {"score": 5, "status": "No Website", "issues": ["Domain shows a parked / 'coming soon' page"],
                "has_shop": False, "copyright_year": None}

    if not (final_url or "").lower().startswith("https://"):
        deduct("https", "No HTTPS: browsers label the site 'Not secure'")
    viewport = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
    if not viewport or "width" not in (viewport.get("content") or "").lower():
        deduct("mobile", "Not mobile-friendly (no responsive layout)")

    years = [int(y) for y in YEAR_RE.findall(html or "") if 1995 <= int(y) <= today.year]
    year = max(years) if years else None
    if year and year <= today.year - 5:
        deduct("copyright_ancient", f"Footer still says © {year}")
    elif year and year <= today.year - 3:
        deduct("copyright_old", f"Footer still says © {year}")

    old = [label for rx, label in OLD_TECH if rx.search(html or "")]
    if old:
        deduct("old_tech", f"Outdated code ({', '.join(old)})")
    gen = soup.find("meta", attrs={"name": re.compile(r"^generator$", re.I)})
    m = re.search(r"wordpress\s+(\d+)\.", (gen.get("content") or "") if gen else "", re.I)
    if m and int(m.group(1)) < 5:
        deduct("old_wordpress", f"Runs an old WordPress version ({m.group(1)}.x)")

    if software == "none_detected":
        deduct("no_booking", "No online booking")
    if not soup.find("a", href=re.compile(r"^tel:", re.I)):
        deduct("no_tel", "No tap-to-call phone link")
    title = (soup.title.get_text(strip=True) if soup.title else "").strip().lower()
    if title in GENERIC_TITLES or len(title) < 4:
        deduct("title", "Generic or missing page title (hurts Google results)")
    desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if not desc or len((desc.get("content") or "").strip()) < 20:
        deduct("description", "No meta description (Google shows a random snippet)")
    if set(tools or []) & {"godaddy", "weebly"}:
        deduct("builder", "Built on a basic template builder")
    if not soup.find("h1"):
        deduct("no_h1", "No main heading on the homepage")
    if not BUSINESS_SCHEMA_RE.search(html or ""):
        deduct("schema", "No business details for Google (schema markup)")
    imgs = soup.find_all("img")
    if len(imgs) >= 5 and sum(1 for i in imgs if not (i.get("alt") or "").strip()) / len(imgs) > 0.5:
        deduct("alt", "Most images have no alt text")
    if not soup.find("meta", attrs={"property": re.compile(r"^og:image$", re.I)}):
        deduct("og", "No preview image when the link is shared")
    if "instagram.com/" not in lower:
        deduct("no_instagram", "Doesn't link to their Instagram")

    score = max(0, min(100, score))
    has_shop = any(SHOP_RE.search(a.get_text(" ", strip=True) or "") or SHOP_RE.search(a.get("href") or "")
                   for a in soup.find_all("a"))
    return {"score": score, "status": status_for(score, thresholds), "issues": issues,
            "has_shop": has_shop, "copyright_year": year}


def audit_without_site(check: dict | None, software: str) -> dict | None:
    """Audit for businesses we couldn't or didn't load a site for."""
    if software in ("no_website", "social_only"):
        issue = "Only has Instagram/social, no website" if software == "social_only" else "No website"
        return {"score": 0, "status": "No Website", "issues": [issue], "has_shop": False, "copyright_year": None}
    err = (check or {}).get("error") or ""
    if (check or {}).get("status") == "fetch_failed" and re.search(r"SSL|certificate|NameResolution|resolve|Connection", err, re.I):
        return {"score": 10, "status": "Outdated", "issues": ["Website didn't load (down, or broken SSL certificate)"],
                "has_shop": False, "copyright_year": None}
    return None
