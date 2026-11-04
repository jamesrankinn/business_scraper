# The decision matrix

For every business location the tool answers five sales questions, in order:

1. **How big are they?** Staff count, then annual revenue.
2. **What do they use now, and what does it cost?** Booking software and estimated monthly spend.
3. **What could we charge them?** Our suggested price.
4. **How likely are we to win them?** Close probability.
5. **What is the lead worth?** Expected monthly value (EMV) = our price × close probability.

Sort by EMV and call from the top. Every number below is a setting in
`config/default.yaml` or `config/software_catalog.yaml`.

## 1. Size and ability to pay

| Estimate | How it's computed | Where it comes from |
|---|---|---|
| Staff | Licence employee count when available (Vancouver), or what you recorded on a call (CSV). Otherwise a blend of the category default and `reviews ÷ reviews_per_staff`, trusting reviews more as their count rises (full weight at 200+). | `categories.*.default_staff`, `reviews_per_staff` |
| Annual revenue | staff × `revenue_per_staff_cad` × Google price-level multiplier ($ 0.85, $$ 1.0, $$$ 1.25, $$$$ 1.5) | `categories.*.revenue_per_staff_cad`, `scoring.price_level_multiplier` |
| Software budget | revenue × 2% ÷ 12. Salons typically spend roughly 1–3% of revenue on software. | `scoring.software_budget_pct_of_revenue` |

The review-based staff estimate is rough. Replace it with real numbers whenever you have them.

## 2. Current software and spend

Detected from the business's website (see `beauty_leads/enrich/software.py`):

* the listed website *is* a booking page (`vagaro.com/yourshop`): high confidence
* the site embeds a widget or script from a booking platform: high confidence
* a "Book now" button links to one: high confidence
* the platform's URL appears somewhere in the page source: lower confidence
* none of those, so we follow up to two "Book" links on the site and look again
* still nothing: `enrich render` opens the site in a headless browser and watches which servers the
  page calls. Widgets that only appear through JavaScript show up here. Requests to booking
  platforms are recorded and blocked, never sent
* nothing found: *No online booking (phone/DM)*; Instagram/Linktree only: *social only*

**Spend** = the catalog's list price for that team size, plus typical add-ons, converted to CAD. Card
processing fees are excluded. If you record a real number from a call (CSV column `monthly spend`),
it replaces the estimate.

## 3. Our price

With `strategy: undercut` (the default):

```
list   = base_per_location + per_extra_staff × max(0, staff − included_staff)
price  = their current spend × (1 − undercut_pct)
         but at least max(min_monthly_price, list × min_of_list)
         and at most  list × max_over_list
price  = list, if they pay nothing today (or spend is unknown)
```

So a Vagaro shop paying ~$180/mo gets quoted ~$144 (20% savings). A Square shop paying ~$68 is not
chased down to $54; it's quoted at 60% of list and pitched on features. Set `strategy: list` to always quote list.

## 4. Close probability

```
close % = min + (max − min) × switchability × decision_maker × fit ^ fit_exponent
```

**Gates.** These multiply, so a bad value can't be averaged away:

| Gate | Meaning | Values |
|---|---|---|
| `switchability` | How winnable users of their current software are | per software in the catalog: Vagaro 0.8, Fresha 0.7, Booksy 0.65, Square 0.35, Boulevard 0.25, Mangomint 0.2; no booking found 0.55 |
| `decision_maker` | Can the person you talk to say yes? | independent 1.0, 2–5 locations 0.85, 6+ 0.5, franchise 0.15 |

**Fit.** A weighted average of seven 0–1 factors:

| Factor | Weight | Scoring |
|---|---|---|
| `savings` | 2.0 | 0.2 + 2 × (savings ÷ current spend), so 20% savings gives 0.6 and 40%+ gives 1.0. They pay nothing today: 0.35. Unknown: 0.5. |
| `affordability` | 2.0 | (software budget ÷ our price) ÷ 2, capped at 1 |
| `size_fit` | 1.5 | 1.0 inside `sweet_spot_staff` (2–15); tapers outside |
| `business_volume` | 1.5 | log-scaled review count (300+ reviews gives 1.0); ×0.7 if rating < 3.5 |
| `stability` | 1.0 | years registered with BC Registries: <1 → 0.3, <2 → 0.6, <5 → 0.85, 5+ → 1.0; +0.1 if incorporated; temporarily closed → 0.2 |
| `reachability` | 1.5 | usable email 0.4 + Instagram 0.35 + phone 0.15 + website 0.1 (capped at 1). "Usable" means published and the site doesn't say "no solicitation". Weighted toward email and DMs, the main outreach channels. |
| `category_fit` | 1.0 | per category: barber 1.0, hair 1.0, lash 0.9, nails 0.85, esthetics 0.85, med spa 0.85, tattoo 0.8, tanning 0.7, massage 0.7 (BC RMT clinics often need insurer direct billing; raise it if you offer that) |

Permanently closed businesses, and franchises if `exclude_franchises: true`, get 0%.

## 5. Expected monthly value

`EMV = price × close %`: the booking subscription this lead is worth per month, on average, if you
pitch it.

## 6. Website audit and agency services

**Website audit** (`beauty_leads/enrich/audit.py`): the homepage starts at 100 and loses points for
what owners and their clients notice:

| Issue | Points |
|---|---|
| Not mobile-friendly (no responsive layout) | −25 |
| No HTTPS ("Not secure" warning) | −20 |
| Footer © year 5+ years old / 3–4 years old | −20 / −15 |
| Outdated code (`<font>`, frames, Flash, jQuery 1.x) | −10 |
| WordPress older than 5.x | −10 |
| No online booking | −10 |
| No tap-to-call link, generic title, no meta description, basic template builder (GoDaddy/Weebly) | −5 each |
| No main heading, no schema markup, most images without alt text | −3 each |
| No share preview image, no Instagram link | −2 each |

Status: **Good** ≥ 75, **Decent** ≥ 50, **Outdated** below 50. **No Website** covers no site at all,
Instagram-only, a parked or "coming soon" page, or a Vagaro/Booksy page used as the website. A site
that is down or has a broken SSL certificate counts as Outdated. A site that blocks the check (HTTP
403) is left unscored rather than guessed.

**Services to pitch** (`our_product.services`, labelled like Services Interested in Close):

| Service | When |
|---|---|
| New Website | No Website |
| Website Redesign | Outdated |
| Online Booking | no online booking found |
| Tool Replacement / Integration | pays for booking software they'd switch from (switchability ≥ 0.5), or 2+ paid tools |
| Forms & Intake | med spa, massage, tattoo, lash, and no form tool detected |
| Memberships / Subscriptions | tanning, massage, med spa, lash, esthetics |
| Payments / E-commerce | site has a shop / gift card link but no store platform |
| Email Marketing | no Mailchimp/Klaviyo and 150+ reviews |
| SEO | Outdated/Decent site missing title, description or schema |
| Hosting & Maintenance | with a new site or redesign |

**Website close %** uses the same formula as section 4, but the switchability gate is replaced by
how open their website status makes them (`scoring.web_openness`): No Website 0.8, Outdated 0.7,
Decent 0.35, Good 0.15. It's then scaled down when the project costs more than their estimated
website budget (1.5% of revenue). So a shop that loves Boulevard but has a 2016 website is still a
real website lead.

`expected project value = website close % × (project fees + 12 × monthly service fees)`

## 7. First-year value and tiers

`Year 1 = 12 × EMV + expected project value`. Leads are sorted and tiered by it: **A** ≥ $1,200,
**B** ≥ $750, **C** ≥ $350, **D** below, **X** excluded. Set `scoring.rank_by: monthly` to rank by
EMV alone (tiers A ≥ $30, B ≥ $15, C ≥ $6).

**The service prices in `our_product.services` are placeholders.** Put your real project and
monthly fees in before trusting Year 1.

## Worked example

A 5-chair barbershop in Surrey (licence lists 5 employees) with 250 Google reviews at 4.7, a "$$"
price level, registered in 2020, with a phone, website, published email and Instagram:

| Step | Vagaro | Boulevard | No online booking |
|---|---|---|---|
| Revenue | 5 × $65,000 = $325,000 | same | same |
| Software budget | $542/mo | same | same |
| Pays now | (30 + 4×10 + 60) USD × 1.38 = **$179** | **$243** | $0 |
| Our list price | 79 + 3×10 = $109 | $109 | $109 |
| Our price | $179 × 0.8 = **$144** | $194 capped at 1.5 × list = **$164** | **$109** |
| Switchability | 0.80 | 0.25 | 0.55 |
| Fit | 91.9 | 96.8 | 87.2 |
| Close % | **25.0%** | **9.1%** | **16.2%** |
| **EMV** | **$35.84 (A)** | **$14.86 (C)** | **$17.68 (B)** |

Boulevard has the best fit, because we'd save them the most money. It still ranks last: its users
like the product, and the switchability gate keeps one strong factor from hiding that.

## Calibrating it

The defaults are educated guesses. After your first 20–30 real pitches:

1. **Put what you learn into a CSV**: name, phone, city, current software, monthly spend, chairs,
   outcome. Import it with `beauty-leads collect csv --file calls.csv`. Reported software, spend and
   staff override every estimate.
2. **Fix the catalog**: if Vagaro owners keep saying "$200/location", adjust
   `software.vagaro.pricing.typical_addons` until the estimate matches.
3. **Fix the gates**: if Fresha shops say yes far more often than Booksy shops, raise Fresha's
   `switchability`.
4. **Fix the range**: set `close_probability.max` so the average close % across the leads you pitched
   roughly equals your actual win rate.
