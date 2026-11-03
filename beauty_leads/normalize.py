"""Normalisation helpers: phones, URLs, names, postal codes, distances."""

from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlsplit

LEGAL_SUFFIXES = {
    "ltd", "limited", "inc", "incorporated", "corp", "corporation", "co", "company",
    "llc", "ulc", "llp", "lp", "enterprises", "enterprise", "holdings", "group",
}
# Words so common in this industry that two names sharing only these are not
# the same business ("Hair Studio" vs "Nail Studio").
GENERIC_WORDS = {
    "the", "and", "of", "by", "a", "an", "at", "on", "in", "for",
    "hair", "salon", "salons", "studio", "studios", "barber", "barbers", "barbershop",
    "shop", "nail", "nails", "spa", "beauty", "lounge", "bar", "lash", "lashes", "brow",
    "brows", "tanning", "tan", "esthetics", "aesthetics", "skin", "care", "design",
    "designs", "boutique", "house", "room", "co", "company", "and", "cuts", "cut",
    "parlour", "parlor", "gallery", "collective", "atelier", "clinic", "centre", "center",
    "wax", "waxing", "mens", "men", "womens", "unisex", "grooming", "style", "styles",
}


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_name(name: str | None) -> str:
    """'The Fade Factory Barbershop Ltd.' -> 'fade factory barbershop'."""
    if not name:
        return ""
    text = strip_accents(name).lower()
    text = text.replace("&", " and ").replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in text.split() if w not in LEGAL_SUFFIXES]
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def name_key(name: str | None) -> str:
    return normalize_name(name).replace(" ", "")


def distinctive_tokens(name: str | None) -> set[str]:
    return {w for w in normalize_name(name).split() if w not in GENERIC_WORDS and len(w) > 1}


def name_similarity(a: str | None, b: str | None) -> float:
    """0-1 similarity that is robust to word order and generic words."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    da, db = distinctive_tokens(a), distinctive_tokens(b)
    if da and db and not (da & db):
        # e.g. "Luxe Nails" vs "Glow Nails": share only generic words.
        return min(seq, jac, 0.5)
    return max(seq, jac)


def normalize_phone(phone: str | None) -> str | None:
    """Return a 10-digit North American number, or None."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return digits


def format_phone(phone: str | None) -> str | None:
    digits = normalize_phone(phone)
    if not digits:
        return phone or None
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def normalize_postal(code: str | None) -> str | None:
    if not code:
        return None
    text = re.sub(r"\s+", "", code).upper()
    if re.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d", text):
        return f"{text[:3]} {text[3:]}"
    return None


POSTAL_RE = re.compile(r"\b([A-Za-z]\d[A-Za-z])\s?(\d[A-Za-z]\d)\b")


def find_postal(text: str | None) -> str | None:
    if not text:
        return None
    m = POSTAL_RE.search(text)
    return normalize_postal(m.group(1) + m.group(2)) if m else None


def street_number(address: str | None) -> str | None:
    if not address:
        return None
    m = re.match(r"\s*(?:#?\s*\d+[A-Za-z]?\s*[-–]\s*)?(\d+[A-Za-z]?)\b", address)
    return m.group(1).lower() if m else None


def ensure_scheme(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "http://" + url
    return url


def host_of(url: str | None) -> str | None:
    url = ensure_scheme(url)
    if not url:
        return None
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def host_matches(host: str | None, domain: str) -> bool:
    if not host:
        return False
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def url_key(url: str | None) -> str | None:
    """Stable key for a URL: host + path, no scheme/query/fragment/trailing slash."""
    url = ensure_scheme(url)
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = host_of(url)
    if not host:
        return None
    path = parts.path.rstrip("/")
    return f"{host}{path}".lower()


def is_platform_url(url: str | None, platform_hosts: list[str]) -> bool:
    host = host_of(url)
    return any(host_matches(host, p) for p in platform_hosts)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
_BAD_EMAIL_PARTS = ("example.", "sentry", "wixpress", "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                    "domain.com", "email.com", "yourname", "name@", "user@")


def clean_emails(candidates) -> list[str]:
    out: list[str] = []
    for raw in candidates:
        email = raw.strip().strip(".").lower()
        if not EMAIL_RE.fullmatch(email):
            continue
        if any(bad in email for bad in _BAD_EMAIL_PARTS):
            continue
        if email not in out:
            out.append(email)
    return out
