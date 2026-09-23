# Price registry — sources, what is verified, how it is refreshed

The registry is `src/ai_cost/data/prices.json`, carried inside the shipped zipapp (list prices, USD, checked on the
date in `checked_at`); `ai-cost prices show --format json --snapshot` prints it as shipped, `prices show` the merged
view with `~/.config/ai-cost/prices.json` on top. To refresh a price: edit the data file, rebuild the zipapp
(`python3 scripts/build.py src dist/ai-cost`) and commit; the selftest reads the registry the file carries.

| provider | what is priced | source page | notes |
|---|---|---|---|
| anthropic | input, 5 m / 1 h cache write, cache read, output; web search per 1,000 | https://platform.claude.com/docs/en/about-claude/pricing | Fable/Mythos 5.1 cache reads are 0.025× input ($0.25/M). Claude Code uses the 1 h cache; transcripts carry the 5m/1h split since 2026 (`usage.cache_creation`), older records fall back to `providers.anthropic.cache_ttl_default`. |
| openai | input, cached input, output | https://developers.openai.com/api/docs/pricing | Standard tier. Fast mode = 2×, Batch/Flex = 0.5× (not modelled). A ledger line for this provider is priced from the same table. |
| google | input, cached input, output; >200k tier for Pro models | https://ai.google.dev/gemini-api/docs/pricing | 3.8/3.7 Flash: $0.75/$3.75 until 2026-12-31, then $1.50/$7.50 (`valid_until` + `next` blocks; `ai-cost doctor` warns when expired). Implicit caching is applied by Google automatically; the CLI stats report `cached` tokens. |
| xai | input, cached input, output; ≥200k tier | https://docs.x.ai/developers/models | `real` prefers the cost the Grok CLI reports per turn (`providers.xai.trust_cli_cost`; `costUsdTicks / 1e10` in a Grok Build session file — since 2.2 the only Grok source; a review wrapper's meta file only ties the session to its round). The `api` group prices a turn's aggregated counters, so a turn of several model calls never gets the ≥200k tier — on 2026-09-21 that put `api` 18 % under the CLI's own figure for a review-heavy day. `grok-4.7` = `grok-4.6` prices (2 / 0.5 / 6; long 4 / 1 / 12), read off the page and confirmed against the CLI's cost of a real run. |
| deepseek | cache hit / miss input, output; peak vs off-peak | https://api-docs.deepseek.com/quick_start/pricing | Peak = Mon–Fri 01:00–04:00 and 06:00–10:00 UTC; off-peak is half price. Chinese public holidays (off-peak) are not modelled. |
| github | Copilot credits per code review + overage; Actions per-minute | https://docs.github.com/en/copilot/get-started/plans · https://docs.github.com/en/billing/managing-billing-for-your-products/managing-billing-for-github-actions/about-billing-for-github-actions | 13 credits per Copilot code review (since 2026-06-01), $0.04 per credit over the plan. Actions: public repos free; private $0.006/min Linux. |
| plans | monthly prices of the subscriptions the `real` group prorates | https://claude.com/pricing · https://chatgpt.com/pricing/ · Copilot plans page · https://github.com/pricing · https://vercel.com/pricing | Edit `subscriptions` in your config to what you actually pay. |

## Refreshing

- `ai-cost prices check` — fetches every source page, finds each model name and compares the dollar amounts that
  follow it with the registry. Statuses: `confirmed`, `changed?` (verify on the page), `not-found` (renamed or
  JS-rendered page), `fetch-failed HTTP 403` (page blocks scripts — check by hand). Exit 4 when anything is `changed?`.
- `ai-cost prices update` — the same check, then writes unambiguous input/output pairs to **your** file, never the
  repo's. Anything else you edit by hand.
- **Auto-check:** any `report` prints a one-line warning when the last saved check found drift, and when that check
  is older than `auto_check_days` (7) it starts a detached `prices check --quiet` (lock file, at most one at a
  time) whose result shows in the next report. Reports never wait for the network. `AI_COST_OFFLINE=1` disables it.
- **Scheduled:** `ai-cost install --schedule 3` installs a launchd agent (macOS) or a cron line (Linux) that runs the
  check every 3 days and logs to `~/.local/state/ai-cost/prices-check.log`; `--unschedule` removes it.

## Known limits

Vendor pages change layout; the parser is a heuristic (amounts within 600 characters after the model name). It never
rewrites a price without `update`, and `update` only touches your override file. Marketplace/CCU billing, negotiated
discounts, free credits, data-residency multipliers, and batch/flex tiers are out of scope by design: the `real` group
uses list prices.
