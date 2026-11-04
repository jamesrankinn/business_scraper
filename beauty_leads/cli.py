"""Command line interface.

    beauty-leads demo                      # fictional data, no keys needed
    beauty-leads collect google            # Google Places (needs GOOGLE_MAPS_API_KEY)
    beauty-leads collect osm               # OpenStreetMap (free)
    beauty-leads collect vancouver         # Vancouver business licences (free)
    beauty-leads collect orgbook           # BC Registries names (free)
    beauty-leads collect csv --file x.csv  # anything else
    beauty-leads merge                     # dedupe into businesses + chains
    beauty-leads enrich websites           # detect booking software
    beauty-leads enrich registry           # years in business / entity type
    beauty-leads score                     # print top leads
    beauty-leads export                    # CSV + Excel + HTML report
    beauty-leads run                       # all of the above for free sources (+google if key set)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import load_config
from .db import Database
from .http import HttpClient
from .merge import merge_records
from .scoring import Scorer, market_summary

log = logging.getLogger("beauty_leads")


def _ns(args, **overrides) -> argparse.Namespace:
    return argparse.Namespace(**{**vars(args), **overrides})


def _ctx(args):
    cfg = load_config(args.config)
    db = Database(args.db or cfg.path("database"))
    return cfg, db


def cmd_collect(args) -> int:
    from .sources import SOURCES

    cfg, db = _ctx(args)
    if args.source not in SOURCES:
        print(f"Unknown source {args.source!r}. Choose from: {', '.join(SOURCES)}", file=sys.stderr)
        return 2
    http = HttpClient(cfg)
    opts = {"file": args.file, "cities": args.city or None, "terms": args.term or None, "max_requests": args.max_requests}
    try:
        batch, total = [], 0
        for rec in SOURCES[args.source].collect(cfg, http, **opts):
            batch.append(rec)
            if len(batch) >= 200:
                total += db.upsert_records(batch)
                batch = []
        total += db.upsert_records(batch)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        http.close()
    print(f"{args.source}: saved {total} records")
    return 0


def cmd_merge(args) -> int:
    cfg, db = _ctx(args)
    businesses = merge_records(cfg, db.iter_records())
    db.replace_businesses(businesses)
    chains = len({b.chain_key for b in businesses if b.chain_key})
    print(f"merged {sum(db.record_counts().values())} records into {len(businesses)} businesses ({chains} multi-location accounts)")
    return 0


def cmd_enrich(args) -> int:
    cfg, db = _ctx(args)
    if db.business_count() == 0:
        print("No businesses yet. Run `beauty-leads collect ...` then `beauty-leads merge` first.", file=sys.stderr)
        return 1
    http = HttpClient(cfg)
    try:
        if args.what in ("websites", "all"):
            from .enrich.website import enrich_websites
            stats = enrich_websites(cfg, db, http, limit=args.limit, force=args.force, workers=args.workers)
            print("websites checked:", ", ".join(f"{cfg.software_label(k)}: {v}" for k, v in sorted(stats.items(), key=lambda kv: -kv[1])) or "none new")
        if args.what in ("render", "all"):
            from .enrich.browser import recheck_with_browser
            try:
                stats = recheck_with_browser(cfg, db, http, limit=args.limit, force=args.force)
                print("browser recheck:", ", ".join(f"{cfg.software_label(k)}: {v}" for k, v in sorted(stats.items(), key=lambda kv: -kv[1])) or "nothing to recheck")
            except RuntimeError as exc:
                if args.what == "render":
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                print(f"skipping browser recheck: {exc}")
        if args.what in ("registry", "all"):
            from .enrich.registry import match_registry
            stats = match_registry(cfg, db, http, online=args.online, limit=args.limit)
            print(f"registry matches: {stats}")
    finally:
        http.close()
    return 0


def _assess(cfg, db):
    scorer = Scorer(cfg, db.website_checks(), db.registry_matches(), crm=db.crm_leads())
    return scorer.assess_all(db.iter_businesses())


def cmd_close(args) -> int:
    from . import crm_close

    cfg, db = _ctx(args)
    assessments = _assess(cfg, db)
    if args.action == "push" and args.dry_run:
        payloads = crm_close.dry_run_payloads(cfg, assessments, args.tier, args.limit)
        out = Path(args.out or cfg.base_dir / "reports" / "close_dry_run.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(crm_close.dumps(payloads), encoding="utf-8")
        print(f"dry run: {len(payloads)} leads would be pushed to Close. Payloads written to {out}")
        return 0
    env = cfg.get("close.api_key_env", "CLOSE_API_KEY")
    key = os.environ.get(env)
    if not key:
        print(f"error: set {env} to a Close API key (Close: Settings > Developer > API Keys).\n"
              "No key? `beauty-leads export` also writes reports/close_import.csv for Close's CSV importer.",
              file=sys.stderr)
        return 1
    client = crm_close.CloseClient(key, cfg.get("close.base_url", crm_close.CLOSE_API))
    try:
        if args.action == "push":
            r = crm_close.push(cfg, db, client, assessments, args.tier, args.limit)
            print(f"Close: {r.created} created, {r.linked} matched to leads already in Close, "
                  f"{r.updated} refreshed, {r.errors} errors")
        else:
            s = crm_close.pull(cfg, db, client)
            print(f"Close: pulled {s['pulled']} leads ({s['with_reports']} with reported data, "
                  f"{s['missing']} deleted in Close). Re-run `beauty-leads export` to rescore.")
    except crm_close.CloseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_score(args) -> int:
    cfg, db = _ctx(args)
    assessments = _assess(cfg, db)
    if not assessments:
        print("No businesses to score. Run collect + merge first (or try `beauty-leads demo`).")
        return 1
    s = market_summary(assessments)
    print(f"\n{s['live_locations']} live locations · {s['accounts']} accounts · "
          f"expected first year ${s['first_year_value_cad']:,.0f} "
          f"(${s['expected_mrr_cad']:,.0f}/mo booking + ${s['expected_project_value_cad']:,.0f} website work) · "
          f"{s['needs_website']} need a better website (CAD)\n")
    print(f"{'tier':4} {'year 1':>8} {'/mo':>6}  {'website':12} {'business':32} {'city':15} software")
    for a in assessments[: args.top]:
        b = a.business
        web = (a.web_status or "-") + (f" {a.web_score}" if a.web_score is not None and a.web_status not in (None, "No Website") else "")
        print(f"{a.tier:4} {a.first_year_value:>8,.0f} {a.expected_mrr:>6.0f}  {web[:12]:12} "
              f"{b.name[:32]:32} {(b.city or '')[:15]:15} {a.software_label}")
    print("\nBy software:")
    for name, row in list(s["by_software"].items())[:12]:
        print(f"  {name:38} {row['locations']:>5} locations  est. spend ${row['current_spend_cad']:>10,.0f}/mo  "
              f"expected ${row['expected_mrr_cad']:>9,.0f}/mo")
    return 0


def cmd_export(args) -> int:
    from .export import export_all

    cfg, db = _ctx(args)
    assessments = _assess(cfg, db)
    out = Path(args.out or cfg.base_dir / "reports")
    paths = export_all(cfg, assessments, out, db=db)
    for k, p in paths.items():
        print(f"{k:22} {p}")
    _warn_sender(cfg)
    return 0


def _warn_sender(cfg) -> None:
    from .outreach import sender_is_configured
    if not sender_is_configured(cfg):
        print("\nnote: the outreach drafts still say CHANGE-ME. Fill in `sender:` in config/outreach.yaml\n"
              "      (name, business, email, mailing address) before sending anything.")


def cmd_run(args) -> int:
    cfg, db = _ctx(args)
    sources = args.sources or (["osm", "vancouver", "orgbook"] + (["google"] if cfg.google_api_key() else []))
    if "google" not in sources and not cfg.google_api_key():
        print("note: GOOGLE_MAPS_API_KEY not set, skipping Google Places (the most complete source).")
    for src in sources:
        print(f"== collect {src}")
        sub = _ns(args, source=src, file=None, city=None, term=None, max_requests=None)
        if cmd_collect(sub) != 0:
            print(f"   ({src} failed; continuing with the other sources)")
    print("== merge")
    cmd_merge(args)
    print("== enrich")
    cmd_enrich(_ns(args, what="all", force=False, workers=None, online=False))
    print("== export")
    return cmd_export(args)


def cmd_demo(args) -> int:
    from .demo import run_demo

    paths = run_demo(args.out, args.config)
    print("Demo report (fictional businesses):")
    for k, p in paths.items():
        print(f"  {k:22} {p}")
    return 0


def cmd_stats(args) -> int:
    cfg, db = _ctx(args)
    print("records by source:", db.record_counts() or "none")
    print("businesses:", db.business_count())
    print("websites checked:", len(db.website_checks()))
    print("registry matches:", sum(1 for m in db.registry_matches().values() if m.get("match")))
    print("linked to Close:", len(db.crm_leads()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="beauty-leads", description="Find and score BC beauty-industry leads.")
    p.add_argument("--config", help="your config YAML (only the keys you change; defaults fill the rest)")
    p.add_argument("--db", help="SQLite database path (default from config)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="pull listings from a source")
    c.add_argument("source", help="google | osm | vancouver | orgbook | csv")
    c.add_argument("--file", help="CSV file (for the csv source)")
    c.add_argument("--city", action="append", help="limit Google to this city (repeatable)")
    c.add_argument("--term", action="append", help="limit Google/OrgBook to this search term (repeatable)")
    c.add_argument("--max-requests", type=int, help="Google request budget for this run")
    c.set_defaults(func=cmd_collect)

    sub.add_parser("merge", help="dedupe records into businesses").set_defaults(func=cmd_merge)

    e = sub.add_parser("enrich", help="detect booking software / match BC registry")
    e.add_argument("what", choices=["websites", "render", "registry", "all"],
                   help="websites: plain fetch; render: headless-browser recheck of sites where nothing was "
                        "found; registry: BC Registries match; all: everything")
    e.add_argument("--limit", type=int, help="max websites / online lookups this run")
    e.add_argument("--force", action="store_true", help="re-check websites already checked")
    e.add_argument("--workers", type=int, help="parallel website checks")
    e.add_argument("--online", action="store_true", help="registry: also search OrgBook live for unmatched names")
    e.set_defaults(func=cmd_enrich)

    s = sub.add_parser("score", help="print the top leads")
    s.add_argument("--top", type=int, default=25)
    s.set_defaults(func=cmd_score)

    x = sub.add_parser("export", help="write CSV, Excel and HTML report")
    x.add_argument("--out", help="output folder (default reports/)")
    x.set_defaults(func=cmd_export)

    r = sub.add_parser("run", help="collect free sources (+Google if keyed), merge, enrich, export")
    r.add_argument("--sources", nargs="+", help="which sources to collect (default: all free + google if key set)")
    r.add_argument("--limit", type=int, help="max websites to check this run")
    r.add_argument("--out", help="output folder")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("demo", help="build a report from fictional sample businesses")
    d.add_argument("--out", help="output folder (default reports/demo)")
    d.set_defaults(func=cmd_demo)

    c2 = sub.add_parser("close", help="push leads to / pull call notes from Close CRM")
    c2.add_argument("action", choices=["push", "pull"])
    c2.add_argument("--tier", nargs="+", help="tiers to push (default from config: A B)")
    c2.add_argument("--limit", type=int, help="max leads to push")
    c2.add_argument("--dry-run", action="store_true", help="write the payloads to a file instead of sending")
    c2.add_argument("--out", help="dry-run output file")
    c2.set_defaults(func=cmd_close)

    sub.add_parser("stats", help="show what's in the database").set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
