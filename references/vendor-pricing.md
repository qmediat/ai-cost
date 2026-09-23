# How outside firms price the same work — and how the `vendor` group models it

The `vendor` group answers one question: *what would an agency or audit firm have quoted for this scope?* It is
an estimate built from the way such firms actually sell, not from token counts.

## What the market does (2026)

**Effort bands, not hours.** Security and remediation firms scope work in bands per item (XS / S / M / L) and quote a
range. The reference quote this profile mirrors (an external security audit of a static site, September 2026) put every
"small" item at **S = 3–6 h**, summed 4 items to 12–24 h, added **+25 % "integration, review and release"** and priced
15–30 h at **EUR 1,500–3,000**, i.e. a blended **~EUR 100/h**. Band width (2×) is the firm's uncertainty margin.

**Packages, not T&M.** The quote is a fixed-price package: a minimum size, rounded up, with the allowance baked in.
Time & material is offered for ongoing work (the same report estimated "5–8 person-days per month" of maintenance,
without a rate, leaving the client to multiply).

**Seniority changes both the rate and the hours.** A junior is cheaper per hour but slower, and their work is reviewed
by a senior; a senior is expensive per hour and fastest. Blended ("team") rates hide this. The `vendor` group exposes it
as three options per profile:

| option | rate | time factor | senior review | meaning |
|---|---|---|---|---|
| junior | low | 1.8× | 25 % of their hours at the senior rate | cheapest rate, longest calendar, review overhead |
| mid | middle | 1.2× | 10 % | the blended rate most quotes are built on |
| senior | high | 1.0× | 0 % | band hours are senior hours |

Rates used as defaults (edit them in `~/.config/ai-cost/config.json` → `vendor.profiles`):

- **`audit-firm-eu`** (EUR): junior 70, mid 100, senior 150. Anchored on the reference quote's EUR 100/h blended
  rate; audit-firm staff bill roughly $100–175/h and seniors/partners $250–350/h in the US
  ([soc2auditors.org](https://soc2auditors.org/soc-2-audit-cost/)), European boutiques sit below that.
- **`software-house-pl-2026`** (USD): junior 30, mid 45, senior 70. Polish contract rates 2026: developers $25–65/h,
  mid ~$34/h, seniors $55–80/h ([lemon.io](https://lemon.io/rate-calculator/poland/),
  [hauerpower](https://www.hauerpower.com/en/insights-posts/cost-of-hiring-polish-developers),
  [remotecrew](https://www.remotecrew.io/blog/software-developer-per-hour-rate-by-country)); ongoing B2B seniors invoice
  closer to EUR 35–40/h equivalent, agency-placed contract work more.

## The arithmetic

```
base_hours        = Σ band_hours[size]                      (min and max separately)
staffed_hours     = base_hours × time_factor
review_hours      = staffed_hours × senior_review_pct
hours             = (staffed_hours + review_hours) × (1 + integration_pct)
hours             = max(hours, package.min_hours) rounded up to package.round_to_hours
cost              = staffed_hours × (1 + integration_pct) × rate
                  + review_hours  × (1 + integration_pct) × senior_rate
working_days      = hours / hours_per_day (default 6 productive h/day)
```

Bands (hours): XS 1–2 · S 3–6 · M 8–16 · L 16–32 · XL 32–64. Only S comes from the reference quote; the others follow
the usual doubling. Change them per profile.

## Sizing the items

- **Hand-sized (recommended):** `--items scope.md` with a Markdown table that has a `size` column, or JSON
  `[{"id","title","size"}]`. A findings table from an audit works as-is once you add the column.
- **Automatic:** without `--items`, the work items a source reports become the items — a plugin's source returns
  one `WorkItem` per deliverable it knows on `Collected.items` (the built-in sources report none). A source that
  sizes by bytes uses the thresholds in `config.sizing.packet_bytes`: XS < 15 KB, S < 50 KB, M < 120 KB,
  L < 300 KB, else XL. That is a proxy for diff size and is flagged in the report as such.

## What it deliberately ignores

Travel, kickoff/report meetings, the audit itself (the reference report did not price its own audit), re-test fees,
VAT, and currency conversion beyond the `eur_per_usd` constant used for display. Add them as extra items if you want
them in the quote.
