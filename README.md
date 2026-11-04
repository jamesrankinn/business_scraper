# beauty-leads

Finds barbershops, hair salons, nail salons, tanning studios, lash/brow, esthetics, massage, tattoo
and med spa businesses across British Columbia, works out which booking software each one uses
(Vagaro, Fresha, Booksy, Square, Squire, Jane, GlossGenius…), estimates what they pay for it and what
they can afford, and ranks every business by **expected monthly value**: what the lead is worth to
you per month if you pitch it. It then drafts a tailored email or Instagram DM for each lead.

```
 collect                 merge               enrich                        score              export
 ───────                 ─────               ──────                        ─────              ──────
 Google Places ─┐                         ┌─ website → booking software ─┐
 OpenStreetMap ─┤                         │                              │   decision     leads.xlsx
 Vancouver lic.─┼─► dedupe + chains ──────┤                              ├─► matrix ────► leads.csv
 BC Registries ─┤   + franchises          └─ BC Registries → years,      │   (EMV)        report.html
 your CSVs ─────┘                            sole prop vs corp ──────────┘
```

## Quick start

```bash
pip install -e .
```

See the output first, using fictional businesses (no keys needed):

```bash
beauty-leads demo
```

Then open `reports/demo/report.html`, or `reports/demo/leads.xlsx` in Excel.

Run it for real:

```bash
export GOOGLE_MAPS_API_KEY=your-key
```

```bash
beauty-leads run
```

`run` collects every free source, plus Google if the key is set. It then merges, checks websites
and writes `reports/`. You can also run the steps one at a time:

```bash
beauty-leads collect google --city Kelowna --city Vernon
```

```bash
beauty-leads collect osm
```

```bash
beauty-leads collect vancouver
```

```bash
beauty-leads collect orgbook
```

```bash
beauty-leads merge
```

```bash
beauty-leads enrich websites
```

```bash
beauty-leads enrich registry
```

```bash
beauty-leads score --top 30
```

```bash
beauty-leads export
```

Everything is cached in `data/`, so re-running a step doesn't re-download pages or re-bill Google.

## Data sources

| Source | Cost | Gives you | Notes |
|---|---|---|---|
| **Google Places API** | Paid, with a monthly free allowance | Name, address, phone, website, rating, review count, price level, open/closed | The most complete source. Requesting phone/website bills at Google's "Enterprise" Text Search rate; check [current pricing](https://developers.google.com/maps/billing-and-pricing/pricing). A full BC sweep (60 cities × 10 terms) is up to ~1,800 requests. `max_requests_per_run` (default 300) stops runaway bills. |
| **OpenStreetMap** (Overpass) | Free | Name, address, phone, website, brand tag | Patchier coverage, but free. The brand tag helps flag franchises. |
| **Vancouver business licences** | Free | **Number of employees**, legal name, licence status | The best staff-size signal available. Vancouver only. |
| **BC Registries** (OrgBook BC) | Free | Every registered name, registration date, sole prop / partnership / corporation, CRA business number | No addresses. Gives "years in business" for scoring. Names with no listing elsewhere go on a separate *registry-only* sheet. |
| **Your CSVs** | — | Anything: other cities' licence data, bought lists, call notes | Columns are auto-detected. `software`, `monthly spend` and `chairs` columns override the estimates. |

**Yellow Pages:** yellowpages.ca blocks automated access (it returns HTTP 403), so this tool doesn't
scrape it. Google Places covers the same businesses with more data. If you get a Yellow Pages export
some other way, import it as a CSV.

### Why it doesn't scrape the Vagaro, Fresha or Booksy directories

Listing every Vagaro/Fresha/Booksy shop in BC straight from their "find a salon" pages would be the
fastest way to find competitor customers. All three forbid it (checked November 2026):

| Platform | robots.txt | Terms |
|---|---|---|
| Vagaro | Blocks every crawler not on its named allowlist (`User-agent: * / Disallow: /`); even search engines are kept out of `/salons/` | [User Agreement](https://www.vagaro.com/user-agreement-2015): no robots, spiders or other automated means to retrieve, scrape or index the site |
| Fresha | Allows crawling and publishes a salon sitemap | [Terms of Use](https://terms.fresha.com/terms-use) (updated 12 Aug 2026): no scraping, crawlers or robots; non-commercial use only; no use "to design or build a competitive product" |
| Booksy | Allows category/city listings and profiles | [Terms of Service](https://booksy.com/en-ca/p/terms): no "robot, spider, scraper"; personal use only; no use "for competitive analysis or benchmarking" |

Paid third-party "Booksy/Fresha/Vagaro scrapers" exist, but using them still breaks these terms.
This tool gets the same answer from sources the businesses publish themselves: their **own
website** (a "Book now" button to vagaro.com) and their **Google listing** (many list
`booksy.com/...` or `vagaro.com/...` as their website). Booking-platform pages are never fetched;
recognising the link is enough.

To get a Google key: [Google Cloud Console](https://console.cloud.google.com/) → create a project →
enable **Places API (New)** → Credentials → API key. Restrict the key to the Places API.

## What you get

`reports/leads.xlsx` has these sheets:

* **Summary**: totals, a breakdown by software (locations, what they pay now, our potential and
  expected MRR) and tier counts
* **Email outreach** / **Instagram outreach**: each lead once, on its best channel, with a drafted
  email or DM (see below)
* **Leads**: one row per location, sorted by expected monthly value, with contact details, detected
  software and evidence, estimated spend, our price, savings, staff, revenue, every matrix factor, a
  pitch line and a plain-English "why"
* **Accounts**: chains rolled up, so one call can cover several locations
* **Registry-only**: registered beauty businesses with no listing found anywhere (names only)
* **Assumptions**: every price and weight used

`report.html` has the same data as a filterable page. Filter by "Reach via", tap a lead to see its
factors, and copy its drafted DM or email.

`outreach.csv` (plus `outreach_email.csv` and `outreach_instagram.csv`) is ready for a mail-merge or
a DM session.

## Outreach: email and Instagram

Each lead gets one channel, in this order:

1. **Email**, if its website publishes an address and doesn't say "no solicitation"
2. **Instagram**, from its website, Google listing, OpenStreetMap or your CSV
3. **Phone**, when that's all there is

Drafts are tailored to the lead's situation. A Vagaro shop we'd save money hears "shops your size on
Vagaro usually pay ~$250/mo; ours is $200". A Square shop hears about deposits and no-shows. A shop
with no online booking hears about a booking link for their bio. Edit the wording in
`config/outreach.yaml`, and **fill in `sender:` first**: the drafts say CHANGE-ME until you do.

**The tool never sends anything.** You send each message yourself:

* **Instagram** forbids automated or bulk DMs and restricts accounts that send them. Send by hand,
  personalise the first line, and keep it to a modest number a day.
* **Email (CASL).** Canada's anti-spam law lets you email an address a business has *published*,
  about something relevant to the business, unless it says it doesn't want unsolicited messages.
  The tool checks each site for that kind of notice ("no solicitation") and moves those leads off
  email. Every email must identify you, include a mailing address, and offer an unsubscribe that you
  honour within 10 business days. The template footer covers this once `sender:` is filled in. CASL
  can also apply to DMs, so treat them the same way: relevant, identify yourself, stop when asked.
* Spend figures in the drafts are estimates, so the wording says "usually" and "around". Keep it that
  way.

## The decision matrix

See **[docs/DECISION_MATRIX.md](docs/DECISION_MATRIX.md)** for the full method and a worked example.
In short:

```
expected monthly value = our price × close %
our price  = 20% under what they pay now (bounded by our list price)
close %    = 1–35% × switchability × decision_maker × fit^1.5
```

**Switchability** says how winnable users of each platform are (Vagaro high, Boulevard low).
**Decision maker** separates an independent owner from a franchise. **Fit** weighs savings,
affordability, team size, review volume, years in business, reachability and category.

## Make it yours

Copy only the keys you want to change into your own file, then run with `--config`:

```yaml
# my.yaml
our_product:
  pricing: {base_per_location: 99, per_extra_staff: 15}
region:
  cities: [Kelowna, West Kelowna, Vernon, Penticton]
```

```bash
beauty-leads --config my.yaml run
```

The settings with the most effect:

* `our_product.pricing`: your real price
* `config/software_catalog.yaml`: competitor prices and switchability. **These are estimates from
  public comparison sites and vary a lot between sources.** Replace them with what owners tell you.
* `scoring.close_probability.max`: calibrate it against your real win rate

### Feed your calls back in

The fastest way to sharpen the model is a spreadsheet of what owners tell you:

```csv
Business Name,Phone,City,Current Software,Monthly Spend,Chairs,Notes
Fadehouse Barber Co.,604-555-0101,Surrey,Vagaro,200,6,hates add-on fees
```

```bash
beauty-leads collect csv --file calls.csv
```

```bash
beauty-leads merge
```

```bash
beauty-leads export
```

Reported software, spend and chair counts override every estimate for that business.

## Being a good citizen

* Website checks identify themselves (set a real contact in `http.user_agent`), obey `robots.txt`,
  wait 1.5 s between requests to the same site, and look at no more than 4 pages per business
  (homepage, up to two "Book" pages, one contact page).
* Adult "body rub" businesses, dental clinics, pet groomers and supply wholesalers are dropped
  (`exclude_keywords`).
* Only public business information is collected.
* **Contacting leads:** see "Outreach" above. This is a practical summary, not legal advice; check
  the rules for your situation before you run a campaign.

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

The OrgBook source has been run against the live API. Google, Overpass and Vancouver open data
couldn't be reached from the environment this was built in. Their parsers are tested against the
documented response formats, but run each one once on a small scope before a full sweep, for example:

```bash
beauty-leads collect google --city Squamish --term "barber shop"
```
