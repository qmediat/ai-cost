# Limits

- `real` attributes subscriptions by time (window hours / 730). A plan you would pay for anyway is a sunk cost; the
  `cash_usd` line is the marginal money that actually left the account.
- Gemini's implicit-cache discount and DeepSeek's off-peak halving are modelled from the documented rules, not from
  your invoice. Check the provider consoles when it matters.
- The core has no built-in work items: without `--items` (or a plugin that supplies items) there is no vendor
  quote. A plugin's automatic sizing is a proxy — hand-size the scope for a quote you will show anyone.
- Price pages change; the checker is a heuristic and never rewrites a price silently.
- `api` prices a row's aggregated counters: a CLI turn of several model calls never gets a long-context tier even if
  one call crossed the threshold. Where a CLI reports its own cost (Grok Build), `real` trusts it and `reconcile`
  measures the gap.
- Under `--project`, rows that name no scope at all (a usage-log line without `session`) are included in every
  project's report, and rows of named workspaces that are no directory are left out; both are counted in the header.
