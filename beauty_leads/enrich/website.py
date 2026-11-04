"""Visit each business website (politely) and record what booking software it uses."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import Config
from ..db import Database
from ..http import HttpClient
from ..normalize import ensure_scheme, host_of, url_key
from .software import detect, social_kind, software_for_url

log = logging.getLogger(__name__)

MAX_EXTRA_PAGES = 2


def _result(status: str, software: str, **kw) -> dict:
    base = {"status": status, "software": software, "confidence": None, "evidence": [],
            "all_software": {}, "site_builder": None, "socials": {}, "emails": [], "pages_checked": []}
    base.update(kw)
    return base


def check_website(cfg: Config, http: HttpClient, website: str | None) -> dict:
    url = ensure_scheme(website)
    if not url:
        return _result("no_website", "no_website")

    # 1. The listed website may itself be a booking page (vagaro.com/yourshop).
    key, strong = software_for_url(cfg, url)
    if key and strong:
        return _result("platform_url", key, confidence="high", evidence=[f"listed website: {url}"])

    kind = social_kind(url)
    if kind in ("instagram", "facebook", "tiktok"):
        return _result("social", "social_only", socials={kind: url})

    # 2. Fetch the homepage.
    res = http.get(url, crawl=True)
    if not res.ok and url.startswith("http://"):
        alt = http.get("https://" + url[len("http://"):], crawl=True)
        if alt.ok or not res.error:
            res = alt
    if not res.ok:
        status = "robots_blocked" if res.error == "blocked by robots.txt" else "fetch_failed"
        return _result(status, "unknown", error=res.error or f"HTTP {res.status}")

    key, strong = software_for_url(cfg, res.url)
    if key and strong:
        return _result("platform_url", key, confidence="high", evidence=[f"website redirects to: {res.url}"])

    found = detect(cfg, res.text, res.url)
    pages = [res.url]

    def absorb(inner: dict) -> None:
        """Merge contact details found on a sub-page."""
        found["emails"] = list(dict.fromkeys(found["emails"] + inner["emails"]))
        for kk, vv in inner["socials"].items():
            found["socials"].setdefault(kk, vv)
        found["no_solicitation"] = found.get("no_solicitation") or inner.get("no_solicitation")

    # 3. No software on the homepage: follow a couple of "Book now" links.
    if not found["software"]:
        for link in found["same_host_booking_links"] + [u for u in found["booking_links"] if u not in found["same_host_booking_links"]]:
            if len(pages) > MAX_EXTRA_PAGES:
                break
            k, s = software_for_url(cfg, link)
            if k:
                found.update(software=k, confidence="high" if s else "medium",
                             evidence=[f"booking link: {link[:120]}"])
                break
            if link in pages:
                continue
            sub = http.get(link, crawl=True)
            pages.append(link)
            if not sub.ok:
                continue
            k, s = software_for_url(cfg, sub.url)
            if k:
                found.update(software=k, confidence="high", evidence=[f"booking page redirects to: {sub.url[:120]}"])
                break
            inner = detect(cfg, sub.text, sub.url)
            absorb(inner)
            if inner["software"]:
                found.update(software=inner["software"], confidence=inner["confidence"],
                             evidence=inner["evidence"], all_software=inner["all_software"])
                break

    # 4. No email yet: emails usually live on the contact / about page.
    if not found["emails"]:
        for link in found.get("contact_links", []):
            if link in pages or len(pages) > MAX_EXTRA_PAGES + 1:
                continue
            sub = http.get(link, crawl=True)
            pages.append(link)
            if sub.ok:
                inner = detect(cfg, sub.text, sub.url)
                absorb(inner)
                if not found["software"] and inner["software"]:
                    found.update(software=inner["software"], confidence=inner["confidence"],
                                 evidence=inner["evidence"], all_software=inner["all_software"])
            break

    software = found["software"]
    if not software:
        # A link-in-bio page with no booking platform means they book by DM.
        software = "social_only" if kind == "linktree" else "none_detected"
    for transient in ("same_host_booking_links", "contact_links"):
        found.pop(transient, None)
    return _result("ok", software, **{k: v for k, v in found.items() if k not in ("software",)},
                   pages_checked=pages, final_url=res.url, host=host_of(res.url))


def enrich_websites(cfg: Config, db: Database, http: HttpClient, limit: int | None = None,
                    force: bool = False, workers: int | None = None) -> dict:
    done = db.website_checks()
    todo: dict[str, str] = {}
    for biz in db.iter_businesses():
        key = url_key(biz.website) or f"__none__:{biz.key}"
        if biz.website and (force or key not in done) and key not in todo:
            todo[key] = biz.website
    items = list(todo.items())[: limit or None]
    workers = workers or int(cfg.get("http.website_workers", 8))
    log.info("Checking %d websites with %d workers (%d already done)", len(items), workers, len(done))

    stats: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_website, cfg, http, url): key for key, url in items}
        for i, fut in enumerate(as_completed(futures), 1):
            key = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # never let one bad site kill the run
                log.exception("Website check crashed for %s", key)
                result = _result("error", "unknown", error=str(exc))
            db.save_website_check(key, result)
            stats[result["software"]] = stats.get(result["software"], 0) + 1
            if i % 25 == 0:
                log.info("  %d/%d websites checked", i, len(items))
    return stats
