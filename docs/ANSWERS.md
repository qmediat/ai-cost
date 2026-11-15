# The three answers: real, API-only, vendor

One command, three honest answers for a block of work done with LLMs — through Claude Code, Codex CLI, Gemini CLI,
Grok Build CLI, your own scripts and applications, or anything a plugin can read:

| group | question it answers | how |
|---|---|---|
| **real** | What did *you* pay? | Subscriptions prorated to the window + pay-per-token API keys at list price. No promos, no negotiated discounts. |
| **api** | What would the same tokens cost on pay-per-use APIs alone, as if no subscription existed? | Every token at the provider's list price, cache tiers applied the way the APIs bill. GitHub included: the account's usage report at gross — Copilot credits, and Actions of the repositories you name (a Copilot review has no price of its own). |
| **vendor** | What would an outside firm have quoted for the same scope? | Effort bands per item × three staffing options (junior / mid / senior differ in rate *and* in time, juniors get senior review), packaged with an integration allowance, minimum size and rounding. |

What it reads and from where: [SOURCES.md](SOURCES.md). Every command and flag: [COMMANDS.md](COMMANDS.md).

## A sample

```
$ ai-cost report --session latest
# AI cost report — 2026-09-19T15:01:09Z → 2026-09-20T00:35:24Z (9.6 h)
## 1. Real cost (what you paid): 54.71 USD
## 2. API-only cost (as if no subscription existed): 175.02 USD
## Per model
| Provider | Model | Records | Model calls | Tokens | Paid USD | How it was paid | API list USD |
|---|---|---|---|---|---|---|---|
| anthropic | claude-opus-5-5 | 2474 |  | … | 0.00 | plan ×2474 | 694.47 |
| xai | grok-4.7 | 29 | 41 | … | 15.24 | API key 15.24 (reported) | 15.24 |
| deepseek | deepseek-flash | 111 | 111 | … | 2.35 | API key 2.35 (list price) | 2.35 |
## 3. Vendor quote (what an outside firm would charge): profile `consultancy-eu`
| Staffing | Rate EUR/h | Time factor | Senior review | Hours | Working days | Quote EUR |
| junior | 70 | 1.8 | 25% | 148–296 | 24.7–49.3 | 12 950–25 900 |
| mid    | 100 | 1.2 | 10% | 96–192  | 16–32     | 12 600–25 200 |
| senior | 150 | 1.0 | 0%  | 76–152  | 12.7–25.3 | 11 250–22 500 |
```

## Attributing cost to labels (pull requests, features, teams)

One session serves several tasks in turn, so a window alone cannot say what task X cost. `--attribute LABEL=REGEX`
(repeatable) labels every priced row whole: by its branch / workspace / PR number when exactly one label
matches (several → `mixed`), otherwise by the label that matches most of the paths and commands the turn touched
(tie → `mixed`, none → `unattributed`). The section prints each label's calls, API-equivalent cost, the part of it
that was cache-read context, cash, subscription share (split by the label's share of the plan's provider — a
policy, printed as such) and the keys it absorbed, and the lines sum to the window totals.

```
ai-cost report --since 2026-09-20T11:00Z --until 2026-09-20T14:36Z --all-projects \
  --attribute "billing=feat/billing|src/billing/|pulls/66" \
  --attribute "docs=docs/|README"
```

Per project and per day: [SETUP.md §5](SETUP.md#5-per-project-per-day). Against the provider's console and the
provider's own day reports: [SETUP.md §6](SETUP.md#6-reconcile-with-the-provider).
