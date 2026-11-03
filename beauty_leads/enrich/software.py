"""Detect booking software, website builder, socials and emails from a page.

Detection is fingerprint based: a business that uses Vagaro links to
vagaro.com from a "Book now" button, embeds a vagaro.com widget, or simply
lists a vagaro.com page as its website. Each URL on the page is weighted by
where it appears (iframe/script > booking button > other link > raw text).
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config import Config
from ..normalize import EMAIL_RE, clean_emails, host_matches, host_of

URL_RE = re.compile(r"""(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s"'<>()\\]*""")
BOOKING_WORDS = re.compile(r"\b(book|booking|appointment|schedule|reserve|online booking)\b", re.I)
SOCIAL_HOSTS = {
    "instagram": ["instagram.com"],
    "facebook": ["facebook.com", "fb.com"],
    "tiktok": ["tiktok.com"],
    "linktree": ["linktr.ee", "linktree.com", "beacons.ai"],
}
WIX_BOOKING_PATHS = ("/book-online", "/booking-calendar", "/service-page", "/bookings-checkout")


def _fp_match(url: str, fingerprint: str) -> bool:
    """Match 'vagaro.com' or 'squareup.com/appointments' against a URL."""
    fp_host, _, fp_path = fingerprint.lower().partition("/")
    if not host_matches(host_of(url), fp_host):
        return False
    if not fp_path:
        return True
    try:
        path = urlsplit(url if "://" in url else "http://" + url.lstrip("/")).path.lower()
    except ValueError:
        return False
    return path.lstrip("/").startswith(fp_path)


def software_for_url(cfg: Config, url: str | None) -> tuple[str | None, bool]:
    """(software_key, strong?) for a single URL, or (None, False)."""
    if not url:
        return None, False
    for key, spec in cfg.software.items():
        if any(_fp_match(url, fp) for fp in spec.get("fingerprints", []) or []):
            return key, True
    for key, spec in cfg.software.items():
        if any(_fp_match(url, fp) for fp in spec.get("weak_fingerprints", []) or []):
            return key, False
    return None, False


def social_kind(url: str | None) -> str | None:
    host = host_of(url)
    for kind, hosts in SOCIAL_HOSTS.items():
        if any(host_matches(host, h) for h in hosts):
            return kind
    return None


def _abs(base: str, href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return None
    try:
        return urljoin(base, href)
    except ValueError:
        return None


def detect(cfg: Config, html: str, page_url: str) -> dict:
    """Analyse one HTML page. Returns a dict (see keys below)."""
    soup = BeautifulSoup(html or "", "html.parser")
    weighted: list[tuple[str, float, str]] = []  # (url, weight, context)
    anchors: list[tuple[str, str]] = []

    for tag, attr, weight, ctx in (
        ("iframe", "src", 3.0, "embedded widget"),
        ("script", "src", 3.0, "script"),
        ("form", "action", 3.0, "form"),
        ("link", "href", 1.0, "link tag"),
        ("img", "src", 1.0, "image"),
    ):
        for el in soup.find_all(tag):
            url = _abs(page_url, el.get(attr))
            if url:
                weighted.append((url, weight, ctx))
    for a in soup.find_all("a"):
        url = _abs(page_url, a.get("href"))
        if not url:
            continue
        text = " ".join(a.get_text(" ", strip=True).split())[:80]
        is_booking = bool(BOOKING_WORDS.search(text) or BOOKING_WORDS.search(url))
        weighted.append((url, 3.0 if is_booking else 2.0, f'"{text}" button' if is_booking and text else "link"))
        anchors.append((url, text))
    # Raw URLs in inline scripts / JSON blobs (many widgets are injected by JS).
    for m in URL_RE.finditer(html or ""):
        raw = m.group(0)
        weighted.append(("https:" + raw if raw.startswith("//") else raw, 1.0, "page source"))

    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for url, weight, ctx in weighted:
        key, strong = software_for_url(cfg, url)
        if not key:
            continue
        scores[key] = scores.get(key, 0.0) + (weight if strong else weight * 0.4)
        ev = evidence.setdefault(key, [])
        item = f"{ctx}: {url[:120]}"
        if len(ev) < 5 and item not in ev:
            ev.append(item)

    lower = (html or "").lower()
    builder = None
    for name, needles in (cfg.catalog.get("site_builders") or {}).items():
        if any(n.lower() in lower for n in needles):
            builder = name
            break
    if builder == "wix" and "wix_bookings" in cfg.software:
        for url, _text in anchors:
            if host_of(url) == host_of(page_url) and any(p in urlsplit(url).path.lower() for p in WIX_BOOKING_PATHS):
                scores["wix_bookings"] = scores.get("wix_bookings", 0.0) + 2.0
                evidence.setdefault("wix_bookings", []).append(f"Wix booking page: {url[:120]}")
                break

    socials: dict[str, str] = {}
    for url, _text in anchors:
        kind = social_kind(url)
        if kind and kind not in socials:
            socials[kind] = url

    emails = [a.get("href", "")[7:].split("?")[0] for a in soup.find_all("a") if (a.get("href") or "").lower().startswith("mailto:")]
    emails += EMAIL_RE.findall(soup.get_text(" "))

    page_host = host_of(page_url)
    booking_links = []
    for url, text in anchors:
        if (BOOKING_WORDS.search(text) or BOOKING_WORDS.search(urlsplit(url).path)) and url not in booking_links:
            if social_kind(url) is None:
                booking_links.append(url)

    best = max(scores, key=scores.get) if scores else None
    best_score = scores.get(best, 0.0) if best else 0.0
    return {
        "software": best,
        "software_score": round(best_score, 2),
        "confidence": "high" if best_score >= 3 else "medium" if best_score >= 2 else "low" if best else None,
        "evidence": evidence.get(best, []) if best else [],
        "all_software": {k: round(v, 2) for k, v in sorted(scores.items(), key=lambda kv: -kv[1])},
        "site_builder": builder,
        "socials": socials,
        "emails": clean_emails(emails),
        "booking_links": booking_links[:5],
        "same_host_booking_links": [u for u in booking_links if host_of(u) == page_host][:3],
        "title": (soup.title.get_text(strip=True)[:120] if soup.title else None),
    }
