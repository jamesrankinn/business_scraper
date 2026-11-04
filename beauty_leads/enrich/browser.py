"""Second pass for websites where no booking software was found.

Many salon sites (Wix, Squarespace, custom builds) only add their booking
widget with JavaScript, so the plain HTML fetch in website.py misses it. This
pass opens those sites in a headless Chromium, the way a visitor's browser
would, and watches which servers the page tries to contact.

Politeness:
* robots.txt is checked for every page before it is opened;
* images, video and fonts are never downloaded;
* requests to booking platforms (vagaro.com, booksy.com...) are recorded and
  then BLOCKED, so the platforms' own servers are never contacted. The URL of
  the request is all we need to identify the software.

Needs:  pip install -e ".[browser]"  then  python -m playwright install chromium
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from ..config import Config
from ..db import Database
from ..http import HttpClient
from ..normalize import ensure_scheme, host_of, url_key
from .audit import audit_page
from .software import detect, find_tools, software_for_url

log = logging.getLogger(__name__)

SKIP_TYPES = {"image", "media", "font"}
STRONG_TYPES = {"script", "document", "xhr", "fetch", "stylesheet", "other"}
RECHECK_SOFTWARE = {"none_detected", "unknown"}


def _needs_recheck(check: dict) -> bool:
    if check.get("rendered"):
        return False
    if check.get("status") in ("robots_blocked", "platform_url", "social", "no_website"):
        return False
    return check.get("software") in RECHECK_SOFTWARE


def render(cfg: Config, context, url: str, timeout_s: float) -> dict:
    """Open one page; return what the browser saw. Never raises."""
    seen: list[tuple[str, str]] = []  # (url, resource type)

    def on_route(route):
        req = route.request
        seen.append((req.url, req.resource_type))
        key, _ = software_for_url(cfg, req.url)
        if key or req.resource_type in SKIP_TYPES:
            return route.abort()
        return route.continue_()

    page = context.new_page()
    page.route("**/*", on_route)
    out = {"url": url, "final_url": url, "html": "", "frames": [], "error": None}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=min(8.0, timeout_s) * 1000)
        except Exception:  # busy pages never go idle; what loaded so far is enough
            pass
        out["final_url"] = page.url
        out["html"] = page.content()
        out["frames"] = [f.url for f in page.frames if f.url and f.url != "about:blank"]
    except Exception as exc:  # timeouts, DNS errors, navigation aborted to a platform
        out["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
    finally:
        page.close()
    out["requests"] = seen
    return out


def analyse(cfg: Config, rendered: dict) -> dict:
    """Software found in the rendered page: network requests first, then the DOM."""
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}

    def hit(key: str, weight: float, note: str) -> None:
        scores[key] = scores.get(key, 0.0) + weight
        ev = evidence.setdefault(key, [])
        if len(ev) < 4 and note not in ev:
            ev.append(note)

    for req_url, rtype in rendered.get("requests", []):
        key, strong = software_for_url(cfg, req_url)
        if key:
            weight = 3.0 if (strong and rtype in STRONG_TYPES) else 1.5 if strong else 0.6
            label = "redirected to" if rtype == "document" else f"page loaded a {rtype} from"
            hit(key, weight, f"{label}: {req_url[:120]}")
    for frame_url in rendered.get("frames", []):
        key, strong = software_for_url(cfg, frame_url)
        if key:
            hit(key, 3.0 if strong else 1.0, f"embedded frame: {frame_url[:120]}")

    dom = detect(cfg, rendered.get("html", ""), rendered.get("final_url") or rendered["url"]) if rendered.get("html") else None
    if dom and dom["software"]:
        for key, score in dom["all_software"].items():
            scores[key] = scores.get(key, 0.0) + score
        evidence.setdefault(dom["software"], []).extend(dom["evidence"])

    best = max(scores, key=scores.get) if scores else None
    best_score = scores.get(best, 0.0) if best else 0.0
    tools = find_tools(cfg, " ".join(u for u, _ in rendered.get("requests", [])) + " " + (rendered.get("html") or ""))
    return {
        "tools": tools,
        "software": best,
        "confidence": "high" if best_score >= 3 else "medium" if best_score >= 1.5 else "low" if best else None,
        "evidence": (evidence.get(best) or [])[:4] if best else [],
        "all_software": {k: round(v, 2) for k, v in sorted(scores.items(), key=lambda kv: -kv[1])},
        "dom": dom,
    }


def recheck_with_browser(cfg: Config, db: Database, http: HttpClient, limit: int | None = None,
                         force: bool = False) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            'The browser recheck needs Playwright:  pip install -e ".[browser]"  '
            "then  python -m playwright install chromium"
        ) from exc

    checks = db.website_checks()
    todo: dict[str, str] = {}
    for biz in db.iter_businesses():
        key = url_key(biz.website)
        if key and key in checks and key not in todo and (force or _needs_recheck(checks[key])):
            todo[key] = ensure_scheme(biz.website)
    items = list(todo.items())[: limit or None]
    timeout = float(cfg.get("browser.timeout_seconds", 20))
    pause = float(cfg.get("browser.pause_seconds", 1.0))
    log.info("Browser recheck of %d websites (no booking software found by the plain fetch)", len(items))

    stats: dict[str, int] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=cfg.get("browser.executable_path") or None)
        context = browser.new_context(
            user_agent=cfg.get("http.user_agent"), locale="en-CA", viewport={"width": 1280, "height": 900},
            service_workers="block",
        )
        try:
            for i, (key, url) in enumerate(items, 1):
                old = checks[key]
                if not http.allowed_by_robots(url):
                    db.save_website_check(key, {**old, "rendered": True, "render_status": "robots_blocked"})
                    stats["robots_blocked"] = stats.get("robots_blocked", 0) + 1
                    continue
                result = analyse(cfg, rendered := render(cfg, context, url, timeout))
                pages = [rendered["final_url"]]
                # Still nothing: open one "Book" page from the rendered site.
                dom = result["dom"]
                if not result["software"] and dom:
                    for link in dom.get("same_host_booking_links", [])[:1]:
                        if http.allowed_by_robots(link):
                            sub = render(cfg, context, link, timeout)
                            pages.append(link)
                            result = analyse(cfg, sub)
                            dom = result["dom"] or dom
                updated = dict(old)
                updated.update(rendered=True, render_status="error" if rendered["error"] and not result["software"] else "ok",
                               render_error=rendered["error"], pages_rendered=pages)
                if result["software"]:
                    updated.update(software=result["software"], confidence=result["confidence"],
                                   evidence=result["evidence"], all_software=result["all_software"], status="ok")
                elif old.get("status") == "fetch_failed" and rendered["html"]:
                    updated.update(software="none_detected", status="ok")  # the browser got in where requests didn't
                if dom:
                    updated["emails"] = list(dict.fromkeys((old.get("emails") or []) + dom.get("emails", [])))
                    socials = dict(old.get("socials") or {})
                    for k, v in (dom.get("socials") or {}).items():
                        socials.setdefault(k, v)
                    updated["socials"] = socials
                    updated["no_solicitation"] = old.get("no_solicitation") or dom.get("no_solicitation")
                    updated["site_builder"] = old.get("site_builder") or dom.get("site_builder")
                updated["tools"] = list(dict.fromkeys((old.get("tools") or []) + result.get("tools", [])))
                if rendered["html"]:
                    # The rendered page is what visitors see, so audit that instead of the raw HTML.
                    updated["audit"] = audit_page(rendered["html"], rendered["final_url"], software=updated["software"],
                                                  tools=updated["tools"], thresholds=cfg.get("scoring.website_status"))
                db.save_website_check(key, updated)
                stats[updated["software"]] = stats.get(updated["software"], 0) + 1
                if i % 10 == 0:
                    log.info("  %d/%d rendered", i, len(items))
                time.sleep(pause)
        finally:
            context.close()
            browser.close()
    return stats
