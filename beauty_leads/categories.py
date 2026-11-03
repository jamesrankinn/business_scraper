"""Classify a business into one of the configured beauty categories."""

from __future__ import annotations

import re
from functools import lru_cache

from .config import Config
from .models import Record
from .normalize import strip_accents

# Source types too broad to override what the business name says.
GENERIC_TYPES = {"beauty_salon", "hair_care", "spa", "shop=beauty", "shop=hairdresser"}


@lru_cache(maxsize=4096)
def _pattern(keyword: str) -> re.Pattern:
    # Whole-word match, allowing simple plurals ("nail" matches "nails").
    kw = re.escape(keyword.lower()).replace(r"\ ", r"[\s\-]*")
    return re.compile(rf"(?<![a-z0-9]){kw}(?:s|es)?(?![a-z0-9])")


def _text_score(text: str, spec: dict) -> float:
    # Each matched span counts once, so "nail" and "nails" don't both score "Nails".
    spans: dict[tuple[int, int], float] = {}
    for weight, kws in ((1.0, spec.get("keywords")), (0.5, spec.get("weak_keywords"))):
        for kw in kws or []:
            for m in _pattern(kw).finditer(text):
                spans[m.span()] = max(spans.get(m.span(), 0.0), weight)
    return sum(spans.values())


def classify_text(cfg: Config, text: str) -> str | None:
    """Best category for free text (a name, licence type, etc.), or None."""
    text = strip_accents(text or "").lower().replace("_", " ")
    best_key, best_score = None, 0.0
    for key, spec in cfg.categories.items():  # dict order == priority on ties
        score = _text_score(text, spec)
        if score > best_score:
            best_key, best_score = key, score
    return best_key


def _type_tokens(spec: dict) -> list[str]:
    tokens = list(spec.get("google_types", []) or [])
    for tag, values in (spec.get("osm_tags", {}) or {}).items():
        tokens.extend(f"{tag}={v}" for v in values)
    return tokens


def classify_record(cfg: Config, record: Record) -> str | None:
    """Pick a category for a record.

    1. A category the source already assigned.
    2. A specific category in the name ("Luxe Lash Studio" -> lash_brow).
    3. A specific source type (Google barber_shop, OSM hairdresser=barber).
    4. A hair-salon guess from the name ("Hair by Jen").
    5. A generic source type (beauty_salon, shop=beauty).
    6. Keywords inside the raw type strings (licence categories etc.).
    """
    if record.category and record.category in cfg.categories:
        return record.category

    from_name = classify_text(cfg, record.name)
    if from_name and from_name != "hair_salon":
        return from_name

    types = {t.lower() for t in record.types or []}
    specific: list[str] = []
    generic: list[str] = []
    for key, spec in cfg.categories.items():
        for token in _type_tokens(spec):
            if token in types:
                (generic if token in GENERIC_TYPES else specific).append(key)
    if specific:
        return specific[0]
    if from_name:
        return from_name
    if generic:
        return generic[0]
    return classify_text(cfg, " ".join(sorted(types)))
