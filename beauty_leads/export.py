"""Write results: leads/accounts CSVs, an Excel workbook, and an HTML report."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime
from pathlib import Path

from .config import Config
from .normalize import name_key
from .scoring import Assessment, accounts, market_summary

LEAD_COLUMNS = [
    "tier", "expected_mrr_cad", "potential_mrr_cad", "close_probability", "fit_score",
    "name", "category", "city", "address", "phone", "email", "website", "instagram",
    "software", "software_confidence", "current_spend_cad", "current_spend_source",
    "our_price_cad", "savings_cad", "staff_est", "staff_source", "annual_revenue_est_cad",
    "software_budget_cad", "rating", "reviews", "price_level", "years_registered", "entity_type",
    "chain_locations", "franchise", "site_builder", "ltv_cad", "pitch", "why", "software_evidence",
    "excluded_reason", "google_maps_url", "sources", "chain_key", "business_key",
]


def _write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    columns = columns or (list(rows[0].keys()) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def registry_only_rows(db) -> list[dict]:
    """Registered beauty businesses we found no listing for (names only)."""
    matched = {m["match"]["source_id"] for m in db.registry_matches().values() if m.get("match")}
    known_names = set()
    for b in db.iter_businesses():
        known_names.update(name_key(n) for n in [b.name, *b.alt_names, b.extra.get("legal_name")] if n)
    rows = []
    for rec in db.iter_records("orgbook"):
        names = rec.extra.get("all_names") or [rec.name]
        if rec.source_id in matched or any(name_key(n) in known_names for n in names):
            continue
        rows.append({
            "name": rec.name,
            "category": rec.category,
            "registration_date": rec.extra.get("registration_date"),
            "entity_type": rec.extra.get("entity_type_label"),
            "registry_id": rec.source_id,
            "orgbook_url": f"https://orgbook.gov.bc.ca/entity/{rec.source_id}",
        })
    rows.sort(key=lambda r: r.get("registration_date") or "", reverse=True)
    return rows


def assumptions_rows(cfg: Config) -> list[dict]:
    p = cfg.get("our_product.pricing", {})
    s = cfg.get("scoring", {})
    rows = [
        {"setting": "Our base price / location", "value": f"${p.get('base_per_location')} CAD"},
        {"setting": "Staff included / extra staff", "value": f"{p.get('included_staff')} included, ${p.get('per_extra_staff')} each extra"},
        {"setting": "Pricing strategy", "value": f"{p.get('strategy')} ({int(float(p.get('undercut_pct', 0)) * 100)}% under current spend)"},
        {"setting": "Minimum price", "value": f"${p.get('min_monthly_price')} CAD"},
        {"setting": "Close probability range", "value": f"{s.get('close_probability', {}).get('min')} - {s.get('close_probability', {}).get('max')}"},
        {"setting": "Software budget (% of revenue)", "value": f"{float(s.get('software_budget_pct_of_revenue', 0)) * 100:.1f}%"},
        {"setting": "USD to CAD", "value": str(cfg.usd_to_cad)},
    ]
    for k, w in (s.get("weights") or {}).items():
        rows.append({"setting": f"Weight: {k}", "value": str(w)})
    for key, spec in cfg.software.items():
        rows.append({"setting": f"Software: {spec.get('label', key)}",
                     "value": f"pricing {spec.get('pricing')} | switchability {spec.get('switchability')} | confidence {spec.get('confidence')}"})
    return rows


def export_all(cfg: Config, assessments: list[Assessment], out_dir: str | Path, db=None,
               demo: bool = False, html_limit: int = 3000) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    leads = [a.flat() for a in assessments]
    accts = accounts(assessments)
    summary = market_summary(assessments)
    reg_rows = registry_only_rows(db) if db is not None else []
    paths: dict[str, Path] = {}

    paths["leads_csv"] = out / "leads.csv"
    _write_csv(paths["leads_csv"], leads, LEAD_COLUMNS + [c for c in (leads[0] if leads else {}) if c.startswith("f_")])
    paths["accounts_csv"] = out / "accounts.csv"
    _write_csv(paths["accounts_csv"], accts)
    if reg_rows:
        paths["registry_only_csv"] = out / "registry_only.csv"
        _write_csv(paths["registry_only_csv"], reg_rows)
    paths["summary_json"] = out / "summary.json"
    paths["summary_json"].write_text(json.dumps(summary, indent=2))

    paths["xlsx"] = out / "leads.xlsx"
    write_xlsx(paths["xlsx"], leads, accts, summary, reg_rows, assumptions_rows(cfg))

    paths["html"] = out / "report.html"
    paths["html"].write_text(render_html(cfg, assessments, summary, demo=demo, limit=html_limit), encoding="utf-8")
    return paths


# --- Excel -----------------------------------------------------------------
def write_xlsx(path: Path, leads: list[dict], accts: list[dict], summary: dict,
               reg_rows: list[dict], assumptions: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="0E5F5B")

    def sheet(ws, rows, columns=None, widths=None):
        columns = columns or (list(rows[0].keys()) if rows else ["(empty)"])
        ws.append(columns)
        for cell in ws[1]:
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="center")
        for r in rows:
            ws.append([r.get(c) for c in columns])
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        for i, col in enumerate(columns, 1):
            ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(col, min(40, max(10, len(col) + 2)))

    ws = wb.active
    ws.title = "Summary"
    ws.append(["BC beauty leads", datetime.now().strftime("%Y-%m-%d %H:%M")])
    ws["A1"].font = Font(bold=True, size=14)
    for k in ("locations", "live_locations", "accounts", "software_checked", "software_identified",
              "potential_mrr_cad", "expected_mrr_cad"):
        ws.append([k.replace("_", " "), summary.get(k)])
    ws.append([])
    ws.append(["Software", "Locations", "Est. current spend CAD/mo", "Our potential MRR", "Expected MRR"])
    for cell in ws[ws.max_row]:
        cell.font, cell.fill = head_font, head_fill
    for name, s in summary["by_software"].items():
        ws.append([name, s["locations"], s["current_spend_cad"], s["potential_mrr_cad"], s["expected_mrr_cad"]])
    ws.append([])
    ws.append(["Tier", "Locations"])
    for t, n in summary["by_tier"].items():
        ws.append([t, n])
    ws.column_dimensions["A"].width = 36
    for col in "BCDE":
        ws.column_dimensions[col].width = 22

    widths = {"name": 32, "address": 40, "why": 80, "pitch": 60, "software_evidence": 50, "website": 30, "email": 28}
    lead_cols = LEAD_COLUMNS + [c for c in (leads[0] if leads else {}) if c.startswith("f_")]
    sheet(wb.create_sheet("Leads"), leads, lead_cols, widths)
    sheet(wb.create_sheet("Accounts"), accts, None, {"account": 32, "pitch": 60, "cities": 30})
    sheet(wb.create_sheet("Registry-only"), reg_rows, None, {"name": 40, "orgbook_url": 50})
    sheet(wb.create_sheet("Assumptions"), assumptions, None, {"setting": 40, "value": 100})
    wb.save(path)


# --- HTML report -----------------------------------------------------------
def _money(x) -> str:
    return "—" if x is None else f"${x:,.0f}"


def render_html(cfg: Config, assessments: list[Assessment], summary: dict, demo: bool = False,
                limit: int = 3000, fragment: bool = False) -> str:
    rows = []
    for a in assessments[:limit]:
        f = a.flat()
        rows.append({k: f.get(k) for k in (
            "tier", "expected_mrr_cad", "potential_mrr_cad", "close_probability", "fit_score", "name",
            "category", "city", "address", "phone", "email", "website", "instagram", "software",
            "software_confidence", "current_spend_cad", "our_price_cad", "savings_cad", "staff_est",
            "staff_source", "rating", "reviews", "years_registered", "chain_locations", "franchise",
            "pitch", "why", "software_evidence", "excluded_reason", "google_maps_url")})
        rows[-1]["category_label"] = cfg.categories.get(a.business.category, {}).get("label", a.business.category)
        rows[-1]["factors"] = {k: round(v, 2) for k, v in a.factors.items()}

    sw_rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td class='n'>{s['locations']:,}</td>"
        f"<td class='n'>{_money(s['current_spend_cad'])}</td><td class='n'>{_money(s['potential_mrr_cad'])}</td>"
        f"<td class='n strong'>{_money(s['expected_mrr_cad'])}</td>"
        f"<td class='barcell'><span class='bar' style='--w:{(s['locations'] / max(1, summary['live_locations'])) * 100:.1f}%'></span></td></tr>"
        for name, s in summary["by_software"].items()
    )
    cat_labels = {k: v.get("label", k) for k, v in cfg.categories.items()}
    p = cfg.get("our_product.pricing", {})
    checked = summary.get("software_checked", 0)
    identified = summary.get("software_identified", 0)
    live = max(1, summary["live_locations"])
    now = datetime.now()
    generated = f"{now:%B} {now.day}, {now.year}"  # %-d isn't portable to Windows
    banner = ("<p class='demo'>Demo data: these businesses are fictional, generated to show what the report looks like. "
              "Run the real pipeline to replace them.</p>") if demo else ""
    truncated = (f"<p class='note'>Showing the top {limit:,} of {len(assessments):,} locations. "
                 "The full list is in leads.csv / leads.xlsx.</p>") if len(assessments) > limit else ""

    data_json = json.dumps(rows).replace("</", "<\\/")
    body = REPORT_TEMPLATE.format(
        banner=banner,
        generated=generated,
        locations=f"{summary['live_locations']:,}",
        accounts=f"{summary['accounts']:,}",
        expected=_money(summary["expected_mrr_cad"]),
        potential=_money(summary["potential_mrr_cad"]),
        identified_pct=f"{identified / live * 100:.0f}%",
        identified=f"{identified:,}",
        checked=f"{checked:,}",
        sw_rows=sw_rows,
        truncated=truncated,
        cat_labels=json.dumps(cat_labels),
        data=data_json,
        base_price=p.get("base_per_location"),
        undercut=int(float(p.get("undercut_pct", 0.2)) * 100),
        strategy=html.escape(str(p.get("strategy", "undercut"))),
        pmin=int(float(cfg.get("scoring.close_probability.min", 0.02)) * 100),
        pmax=int(float(cfg.get("scoring.close_probability.max", 0.35)) * 100),
    )
    if fragment:
        return body
    return "<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n" \
           "<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>\n" \
           + body.replace("</style>", "</style>\n</head>\n<body>", 1) + "\n</body>\n</html>\n"


REPORT_TEMPLATE = r"""<title>BC Beauty Leads</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@75..100,500..800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: one column of sections, summary before detail; the lead table scrolls inside its own box. */
:root {{
  --bg: #f3f6f6; --surface: #ffffff; --fg: #142221; --muted: #5a6c6b; --line: #d5dfde;
  --accent: #0e6e69; --accent-soft: #d9efed;
  --tier-a: #136f3e; --tier-a-bg: #dcf2e4; --tier-b: #1d5a8f; --tier-b-bg: #dde9f6;
  --tier-c: #8a5a00; --tier-c-bg: #f7ead0; --tier-d: #5a6c6b; --tier-d-bg: #e7eded;
  --tier-x: #a3302b; --tier-x-bg: #f6dedc; --warn-bg: #fff3d6; --warn-fg: #6b4a00;
  --display: "Archivo", "Arial Narrow", system-ui, sans-serif;
  --body: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #0f1716; --surface: #16211f; --fg: #e3ecea; --muted: #93a7a4; --line: #2a3a38;
  --accent: #4fc2b9; --accent-soft: #173a37;
  --tier-a: #7fd9a2; --tier-a-bg: #173826; --tier-b: #8ec0ef; --tier-b-bg: #172c40;
  --tier-c: #f0c46c; --tier-c-bg: #3a2c10; --tier-d: #9fb1ae; --tier-d-bg: #222f2d;
  --tier-x: #f19a93; --tier-x-bg: #3e1d1b; --warn-bg: #3a2c10; --warn-fg: #f0d79a;
  color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #0f1716; --surface: #16211f; --fg: #e3ecea; --muted: #93a7a4; --line: #2a3a38;
  --accent: #4fc2b9; --accent-soft: #173a37;
  --tier-a: #7fd9a2; --tier-a-bg: #173826; --tier-b: #8ec0ef; --tier-b-bg: #172c40;
  --tier-c: #f0c46c; --tier-c-bg: #3a2c10; --tier-d: #9fb1ae; --tier-d-bg: #222f2d;
  --tier-x: #f19a93; --tier-x-bg: #3e1d1b; --warn-bg: #3a2c10; --warn-fg: #f0d79a;
  color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.5 var(--body); margin: 0; }}
.wrap {{ max-width: 1240px; margin: 0 auto; padding-block: 28px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 28px; }}
.wrap > * {{ min-width: 0; }}
header {{ display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 24px; }}
h1 {{ font: 800 clamp(30px, 5vw, 44px)/1 var(--display); font-stretch: 80%; letter-spacing: -0.01em; margin: 0; text-wrap: balance; }}
h2 {{ font: 700 20px/1.2 var(--display); font-stretch: 88%; margin: 0 0 12px; }}
.meta {{ color: var(--muted); font-size: 13px; }}
.demo {{ background: var(--warn-bg); color: var(--warn-fg); border-radius: 6px; padding: 10px 14px; margin: 0; font-weight: 500; }}
.note {{ color: var(--muted); font-size: 13px; margin: 8px 0 0; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }}
.tile {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; display: grid; gap: 4px; min-width: 0; }}
.tile .label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }}
.tile .value {{ font: 700 28px/1.1 var(--display); font-stretch: 85%; font-variant-numeric: tabular-nums; }}
.tile .sub {{ font-size: 13px; color: var(--muted); }}
.tile.key {{ border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }}
.tile.key .value {{ color: var(--accent); }}
.scroll {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }}
th {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; white-space: nowrap; background: var(--surface); }}
td.n, th.n {{ text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; white-space: nowrap; }}
td.strong {{ font-weight: 600; color: var(--accent); }}
.barcell {{ width: 22%; min-width: 90px; vertical-align: middle; }}
.bar {{ display: block; height: 8px; width: var(--w); min-width: 2px; background: var(--accent); border-radius: 4px; opacity: .75; }}
.filters {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }}
.filters label {{ display: grid; gap: 3px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
.filters input, .filters select {{ font: 14px var(--body); color: var(--fg); background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 7px 9px; min-width: 0; }}
.filters input {{ width: min(260px, 100%); }}
input:focus-visible, select:focus-visible, button:focus-visible, th button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
#leads th button {{ all: unset; cursor: pointer; }}
#leads th button[aria-sort="descending"]::after {{ content: " ↓"; }}
#leads th button[aria-sort="ascending"]::after {{ content: " ↑"; }}
#leads tbody tr.row {{ cursor: pointer; }}
#leads tbody tr.row:hover td {{ background: var(--accent-soft); }}
.biz {{ font-weight: 600; }}
.sub2 {{ color: var(--muted); font-size: 13px; }}
.pill {{ display: inline-block; font: 600 12px/1 var(--mono); padding: 4px 7px; border-radius: 4px; }}
.t-A {{ color: var(--tier-a); background: var(--tier-a-bg); }} .t-B {{ color: var(--tier-b); background: var(--tier-b-bg); }}
.t-C {{ color: var(--tier-c); background: var(--tier-c-bg); }} .t-D {{ color: var(--tier-d); background: var(--tier-d-bg); }}
.t-X {{ color: var(--tier-x); background: var(--tier-x-bg); }}
.chip {{ display: inline-block; font-size: 12px; padding: 2px 7px; border-radius: 10px; border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }}
tr.detail td {{ background: var(--bg); }}
.detail-grid {{ display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 18px; padding: 6px 2px; }}
.detail-grid p {{ margin: 0 0 8px; max-width: 70ch; }}
.factors {{ display: grid; grid-template-columns: max-content 1fr max-content; gap: 4px 10px; align-items: center; font-size: 13px; }}
.factors .track {{ height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; }}
.factors .fill {{ display: block; height: 100%; background: var(--accent); }}
.contact {{ font-family: var(--mono); font-size: 13px; user-select: all; }}
a {{ color: var(--accent); }}
.how {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px 28px; font-size: 14px; color: var(--muted); }}
.how p {{ margin: 0; max-width: 65ch; }}
.how b {{ color: var(--fg); }}
@media (max-width: 720px) {{
  .detail-grid {{ grid-template-columns: 1fr; }}
  .hide-sm {{ display: none; }}
}}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style>
<div class="wrap">
  <header>
    <h1>BC Beauty Leads</h1>
    <span class="meta">Generated {generated} · all money CAD per month</span>
  </header>
  {banner}
  <section class="tiles" aria-label="Summary">
    <div class="tile key"><span class="label">Expected monthly value</span><span class="value">{expected}</span><span class="sub">Our price × close probability, summed</span></div>
    <div class="tile"><span class="label">Potential MRR</span><span class="value">{potential}</span><span class="sub">If every live location signed</span></div>
    <div class="tile"><span class="label">Locations</span><span class="value">{locations}</span><span class="sub">{accounts} accounts after grouping chains</span></div>
    <div class="tile"><span class="label">Software identified</span><span class="value">{identified_pct}</span><span class="sub">{identified} named platforms · {checked} checked</span></div>
  </section>

  <section>
    <h2>Who they book with</h2>
    <div class="scroll"><table>
      <thead><tr><th>Software</th><th class="n">Locations</th><th class="n">Est. spend now</th><th class="n">Our potential</th><th class="n">Expected</th><th class="hide-sm">Share of locations</th></tr></thead>
      <tbody>{sw_rows}</tbody>
    </table></div>
  </section>

  <section>
    <h2>Leads</h2>
    <div class="filters">
      <label for="q">Search<input id="q" type="search" placeholder="Name, city, phone…"></label>
      <label for="f-tier">Tier<select id="f-tier"><option value="">All</option><option>A</option><option>B</option><option>C</option><option>D</option><option>X</option></select></label>
      <label for="f-cat">Category<select id="f-cat"><option value="">All</option></select></label>
      <label for="f-city">City<select id="f-city"><option value="">All</option></select></label>
      <label for="f-sw">Software<select id="f-sw"><option value="">All</option></select></label>
    </div>
    <div class="scroll"><table id="leads">
      <thead><tr>
        <th><button data-k="tier">Tier</button></th>
        <th><button data-k="name">Business</button></th>
        <th><button data-k="software">Software</button></th>
        <th class="n"><button data-k="current_spend_cad">Pays now</button></th>
        <th class="n"><button data-k="our_price_cad">Our price</button></th>
        <th class="n hide-sm"><button data-k="close_probability">Close %</button></th>
        <th class="n"><button data-k="expected_mrr_cad" aria-sort="descending">Expected</button></th>
        <th class="n hide-sm"><button data-k="staff_est">Staff</button></th>
      </tr></thead>
      <tbody></tbody>
    </table></div>
    <p class="note" id="count"></p>
    {truncated}
  </section>

  <section>
    <h2>How the numbers are built</h2>
    <div class="how">
      <p><b>Staff</b> comes from the business licence when there is one, otherwise a rough estimate from Google review count and the category's typical team size.</p>
      <p><b>Pays now</b> is the catalog list price of the software we found on their website, for that team size, with typical add-ons. Card processing fees are excluded.</p>
      <p><b>Our price</b> uses the {strategy} strategy: {undercut}% under what they pay now, or our list price (${base_price}/location plus extra staff) when they pay nothing.</p>
      <p><b>Close %</b> is {pmin}–{pmax}%, scaled by two gates that multiply (how winnable users of their software are, and whether the owner makes the call) and a weighted fit score (savings, affordability, size, volume, stability, reachability, category). Tap a lead to see its factors.</p>
      <p><b>Expected</b> is our price × close %: what this lead is worth per month, on average, if you pitch it. Sort by it to decide who to call first.</p>
    </div>
  </section>
</div>
<script>
(function () {{
  const DATA = {data};
  const CATS = {cat_labels};
  const $ = (s) => document.querySelector(s);
  const tbody = $("#leads tbody");
  let sortKey = "expected_mrr_cad", sortDir = -1;
  const open = new Set();
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}}[c]));
  const money = (x) => x == null ? "—" : "$" + Math.round(x).toLocaleString("en-CA");
  const fill = (id, values) => {{
    const sel = $(id);
    [...new Set(values.filter(Boolean))].sort().forEach((v) => {{
      const o = document.createElement("option"); o.value = v; o.textContent = id === "#f-cat" ? (CATS[v] || v) : v; sel.appendChild(o);
    }});
  }};
  fill("#f-cat", DATA.map((d) => d.category));
  fill("#f-city", DATA.map((d) => d.city));
  fill("#f-sw", DATA.map((d) => d.software));

  function detail(d) {{
    const f = d.factors || {{}};
    const bars = Object.entries(f).map(([k, v]) =>
      `<span>${{esc(k.replace(/_/g, " "))}}</span><span class="track"><span class="fill" style="width:${{v * 100}}%"></span></span><span class="n">${{v.toFixed(2)}}</span>`).join("");
    const contact = [d.phone, d.email].filter(Boolean).map((c) => `<span class="contact">${{esc(c)}}</span>`).join(" · ");
    const links = [
      d.website ? `<a href="${{esc(d.website)}}" target="_blank" rel="noopener">website</a>` : "",
      d.instagram ? `<a href="${{esc(d.instagram)}}" target="_blank" rel="noopener">instagram</a>` : "",
      d.google_maps_url ? `<a href="${{esc(d.google_maps_url)}}" target="_blank" rel="noopener">map</a>` : "",
    ].filter(Boolean).join(" · ");
    return `<tr class="detail"><td colspan="8"><div class="detail-grid">
      <div>
        ${{d.pitch ? `<p><b>Pitch:</b> ${{esc(d.pitch)}}</p>` : ""}}
        <p><b>Why:</b> ${{esc(d.why)}}</p>
        ${{d.software_evidence ? `<p class="sub2">Evidence: ${{esc(d.software_evidence)}}</p>` : ""}}
        ${{d.excluded_reason ? `<p><b>Excluded:</b> ${{esc(d.excluded_reason)}}</p>` : ""}}
        <p>${{contact || '<span class="sub2">No phone or email found</span>'}}</p>
        <p class="sub2">${{esc(d.address || "")}} ${{links ? "· " + links : ""}}</p>
      </div>
      <div class="factors" aria-label="Decision matrix factors">${{bars}}</div>
    </div></td></tr>`;
  }}

  function render() {{
    const q = $("#q").value.trim().toLowerCase();
    const tier = $("#f-tier").value, cat = $("#f-cat").value, city = $("#f-city").value, sw = $("#f-sw").value;
    const rows = DATA.filter((d) =>
      (!tier || d.tier === tier) && (!cat || d.category === cat) && (!city || d.city === city) && (!sw || d.software === sw) &&
      (!q || [d.name, d.city, d.phone, d.email, d.address, d.software].join(" ").toLowerCase().includes(q)));
    rows.sort((a, b) => {{
      const x = a[sortKey], y = b[sortKey];
      if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * sortDir;
    }});
    const shown = rows.slice(0, 500);
    tbody.innerHTML = shown.map((d, i) => {{
      const id = d.name + "|" + (d.address || "");
      const sub = [CATS[d.category] || d.category, d.city, d.chain_locations > 1 ? d.chain_locations + " locations" : "", d.franchise ? "franchise" : ""].filter(Boolean).join(" · ");
      const row = `<tr class="row" data-id="${{esc(id)}}" tabindex="0" aria-expanded="${{open.has(id)}}">
        <td><span class="pill t-${{esc(d.tier)}}">${{esc(d.tier)}}</span></td>
        <td><div class="biz">${{esc(d.name)}}</div><div class="sub2">${{esc(sub)}}</div></td>
        <td>${{esc(d.software)}}${{d.software_confidence ? ` <span class="chip">${{esc(d.software_confidence)}}</span>` : ""}}</td>
        <td class="n">${{money(d.current_spend_cad)}}</td>
        <td class="n">${{money(d.our_price_cad)}}</td>
        <td class="n hide-sm">${{(d.close_probability * 100).toFixed(0)}}%</td>
        <td class="n strong">${{money(d.expected_mrr_cad)}}</td>
        <td class="n hide-sm">${{d.staff_est ?? "—"}}</td></tr>`;
      return row + (open.has(id) ? detail(d) : "");
    }}).join("");
    $("#count").textContent = `${{rows.length.toLocaleString("en-CA")}} matching locations` + (rows.length > 500 ? " (first 500 shown; narrow the filters to see more)" : "");
  }}

  function toggle(tr) {{
    const id = tr.dataset.id;
    open.has(id) ? open.delete(id) : open.add(id);
    render();
  }}
  tbody.addEventListener("click", (e) => {{ const tr = e.target.closest("tr.row"); if (tr && !e.target.closest("a")) toggle(tr); }});
  tbody.addEventListener("keydown", (e) => {{ const tr = e.target.closest("tr.row"); if (tr && (e.key === "Enter" || e.key === " ")) {{ e.preventDefault(); toggle(tr); }} }});
  document.querySelectorAll("#leads th button").forEach((btn) => btn.addEventListener("click", () => {{
    const k = btn.dataset.k;
    sortDir = sortKey === k ? -sortDir : (["name", "software", "tier"].includes(k) ? 1 : -1);
    sortKey = k;
    document.querySelectorAll("#leads th button").forEach((b) => b.removeAttribute("aria-sort"));
    btn.setAttribute("aria-sort", sortDir === 1 ? "ascending" : "descending");
    render();
  }}));
  ["#q", "#f-tier", "#f-cat", "#f-city", "#f-sw"].forEach((s) => $(s).addEventListener("input", render));
  render();
}})();
</script>"""
