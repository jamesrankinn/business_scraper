# beauty-leads

Finds barbershops, hair salons, nail salons, tanning studios, lash/brow and esthetics businesses
across British Columbia, works out which booking software each one uses (Vagaro, Fresha, Booksy,
Square, Squire, GlossGenius…), estimates what they pay for it and what they can afford, and ranks
every business by **expected monthly value**: what the lead is worth to you per month if you pitch it.

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

To get a Google key: [Google Cloud Console](https://console.cloud.google.com/) → create a project →
enable **Places API (New)** → Credentials → API key. Restrict the key to the Places API.

## What you get

`reports/leads.xlsx` has these sheets:

* **Summary**: totals, a breakdown by software (locations, what they pay now, our potential and
  expected MRR) and tier counts
* **Leads**: one row per location, sorted by expected monthly value, with contact details, detected
  software and evidence, estimated spend, our price, savings, staff, revenue, every matrix factor, a
  pitch line and a plain-English "why"
* **Accounts**: chains rolled up, so one call can cover several locations
* **Registry-only**: registered beauty businesses with no listing found anywhere (names only)
* **Assumptions**: every price and weight used

`report.html` has the same data as a filterable page. Tap a lead to see its factors and pitch.

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
  wait 1.5 s between requests to the same site, and look at no more than 3 pages per business.
* Only public business information is collected.
* **Contacting leads:** commercial email in Canada is governed by CASL. Business emails that are
  conspicuously published may be emailed about things relevant to the business, unless the site says
  it doesn't want unsolicited messages. Always identify yourself and include an unsubscribe option.
  Check the rules for your situation before you run a campaign.

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
