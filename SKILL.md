---
name: ai-cost
description: >-
  Price a block of AI-assisted work three ways with the `ai-cost` CLI: REAL (what you paid — subscriptions prorated
  to the window + pay-per-token API keys, list prices, no promos), API-ONLY (what the same tokens would cost on
  pay-per-use APIs as if no subscription existed, GitHub Copilot credits and Actions minutes included) and VENDOR
  (what an outside firm would quote for the same scope — effort bands × junior / mid / senior staffing with
  different rates and speeds, packaged with an integration allowance). Reads Claude Code transcripts, Codex CLI
  rollouts, Gemini CLI sessions, Grok Build CLI sessions, whatever a plugin adds, and optionally GitHub via gh; every
  row placed by the working directory its CLI ran in, so `--project DIR` scopes every source. Price registry with
  source URLs, drift check (`prices check/update`), `doctor`, `monitor` with budgets, `daily` reports per project
  (scheduled), `reconcile` against a provider's console figure, offline `selftest`. TRIGGERS: "/ai-cost", "what did
  this cost", "token cost", "how much did the AI work cost", "koszt tokenów", "koszt AI", "ile kosztowała ta praca",
  "koszt per projekt", "cost per project", "what did yesterday cost", "raport dzienny kosztów", "api vs
  subscription", "AI budget", "does it match the console", "are the model prices current", "vendor quote for this
  scope".
---

# /ai-cost — what the AI work cost (real · API-only · vendor)

`ai-cost` is one executable file (a zipapp built from the package `src/ai_cost/`, Python 3.9+, stdlib only). It reads
local files and sends nothing; the network is used only when asked for — the optional price-drift check, and
`--github` for live GitHub counts through `gh`. Human docs: `README.md`;
design: the design note and ADRs kept with the development source (not shipped); sources and assumptions: `references/pricing-sources.md`,
`references/vendor-pricing.md`. Run it by its full path when the shell's PATH is not the user's login PATH.

## When the user asks

| ask | run |
|---|---|
| "what did this session / this work cost" | `ai-cost report --session <id or latest>` |
| a window rather than a session ("last night", "this week") | `ai-cost report --since 2026-09-19T15:00Z --until 2026-09-20T01:00Z --all-projects` |
| "what would a contractor charge" with a scope table | `ai-cost report --group vendor --items scope.md [--vendor-profile NAME]` |
| "are the prices current" | `ai-cost prices check` (exit 4 = drift; then `prices update` or edit `~/.config/ai-cost/prices.json`) |
| "what did feature X / PR X cost", "split the cost by task" | `ai-cost report --since … --until … --all-projects --attribute "<label>=<branch\|PR number\|paths regex>"` (repeatable; `mixed` and `unattributed` are always in the table) |
| "what did project X cost", "cost per project" | `ai-cost report --project /path/to/X --since … --until …` — every source scoped to that directory; the header counts what was left out (other directories, named workspaces) and what is included without a directory. Run from inside the project, a plain `report` does the same |
| "what did yesterday cost, per project", "daily report" | `ai-cost daily` (yesterday, UTC; `--date YYYY-MM-DD` for another day) → `<reports>/<date>/global.md`, one `<project>.md` per project, `index.json`; `ai-cost install --schedule-reports [--at HH:MM]` makes it a morning job |
| "does the tool agree with the provider's console / invoice" | `ai-cost reconcile --provider xai --usd <console figure> [--tokens N] --hours 24` — exit 1 above `reconcile.tolerance_pct` (5); `doctor` shows the last one |
| "what are we spending per day / budget" | `ai-cost monitor --append` (exit 3 on breach) — also the line to put in a scheduler |
| "my app / script calls the API: count that too" | the program appends one line per request to the usage log (README: "Let an app or script report its own calls") — `ai-cost log --from-response resp.json --provider openai` or `ai_cost.log.record(...)`; the next `report` includes it |
| "something is off / it does not see my sources" | `ai-cost doctor` |

Prefer `--format md` for the user and `--format json --detail` when you need the rows. `AI_COST_OFFLINE=1` skips the
background price check.

## Reading the report — what to tell the user

1. **Real** = money that left the account in the window: `cash_usd` (API keys) plus the prorated share of
   subscriptions (Claude plan, ChatGPT plan, Copilot, GitHub). Say both numbers; the subscriptions are sunk cost,
   the cash is marginal. Rows whose billing is unknown are priced in `api` only; the header counts them.
2. **API-only** = the same tokens at list price without any plan. It is what an API-key-only setup would have paid
   and the fair number to compare with a vendor. Copilot reviews are priced at the per-credit overage, Actions
   minutes only for private repos.
3. **Vendor** = three options, each a range (band min–max): junior (cheap rate, more time, senior review on top),
   mid (the blended rate quotes are built on), senior (fastest, highest rate). Quote the senior and mid ranges,
   mention the junior calendar. State the profile and that band sizing is a proxy when `--items` was not given.
4. Always state the window, the sources found (the report header lists them) and the `prices checked_at` date.

## Getting the inputs right

- **Session id:** the current transcript's file name under `~/.claude/projects/<project>/`; `--session latest`
  picks the newest for the cwd's project; `--all-projects` for a time window across projects.
- **Codex billing:** a rollout that names a ChatGPT plan is a subscription session; the others follow
  `providers.openai.billing` in the config (`api`, `subscription`, or `mixed` = no single rule) and stay `unknown`
  without it. A plugin that knows what the key was charged (a ledger, a proxy log) can settle them.
- **Gemini CLI:** sessions under `~/.gemini/tmp/*/chats/` (`GEMINI_CLI_HOME` overrides the home); `~/.gemini/projects.json`
  maps each folder to its working directory.
- **Grok Build CLI:** `~/.grok/sessions/<cwd, URL-encoded>/<session>/usage.json` (`GROK_HOME` overrides); one row per
  turn and model; the CLI's own cost (`costUsdTicks / 1e10`) is what `real` uses under `providers.xai.trust_cli_cost`,
  `api` prices at list; billing follows `providers.xai.billing`.
- **Per project:** a row's working directory decides (Codex `cwd`, the Gemini map, the Grok session directory); a
  row without a scope takes the scope of the row sharing its ref (a usage-log line with `session`). `--project`
  leaves out other directories and named workspaces that are no directory, includes rows that name nothing, and
  says so in the header. A worktree is its own project.
- **Plugins:** `ai_cost.plugins` entry points, `plugins: [...]` in the config, or `AI_COST_PLUGINS=mod1,mod2`; settings
  under `plugin_settings.<name>`, overridable per run with `--setting <name>.<key>=<value>`. `doctor` lists what
  loaded and why a module did not.
- **Scope for vendor:** hand-size items when the number will be shown to anyone — a Markdown table with a `size`
  column (XS/S/M/L/XL), or a JSON list of `{id, title, size}`.
- **Your plans:** `ai-cost install --init-config`, then edit `subscriptions` (plan, seats,
  `attribution: time|full|none`) and `providers.github.copilot_plan_exhausted` once the month's Copilot credits are
  gone.

## Prices

The registry is `src/ai_cost/data/prices.json`, carried inside the shipped file (USD, list prices, `checked_at`,
each provider with its `source` URL; `prices show --format json --snapshot` prints it);
`~/.config/ai-cost/prices.json` overrides numbers, never a price's shape. `prices show` prints the merged table;
`prices check` re-reads the vendor pages and flags `changed?` (verify by hand — layouts change), `prices update`
writes only unambiguous input/output pairs to the user file. Any `report` prints the last saved check's drift and,
when that check is older than `auto_check_days` (7; 0 = off), starts a detached `prices check --quiet` for the next
run (never waits for the network); `install --schedule 3` adds a launchd/cron job. `doctor` warns when a price
carries an expired `valid_until`.

## Operations

- `doctor` — transcripts / rollouts / Gemini sessions found, plugins loaded (and why one did not), config + prices
  present and fresh, `valid_until`, state dir writable, schedule installed. Exit 1 on problems.
- `monitor [--hours N] --append [--quiet]` — appends `{ts, window, hours, real_usd, cash_usd, api_usd,
  by_provider_api_usd}` to `~/.local/state/ai-cost/history.jsonl`; budgets from `config.budgets` (daily, per
  provider); exit 3 on breach. `monitor --history 30` prints the tail. Suitable for a cron line.
- `daily [--date YYYY-MM-DD] [--out DIR] [--quiet]` — the day's `global.{md,json}`, `<project dir>.{md,json}` per
  Claude project touched that day (its path from the transcripts' `cwd`), `index.json` with totals and notes (a
  project that cannot be placed, one without usage, one that failed); unpriced rows skipped and counted.
  `install --schedule-reports [--at HH:MM]` / `--unschedule-reports` manage the job (launchd calendar / cron).
- `reconcile --provider P --usd X [--tokens N] [--hours N | --since/--until]` — local cash and tokens of `P` vs the
  console figure, gap vs `reconcile.tolerance_pct`; exit 1 above; history `reconcile.jsonl` in the state dir.
- `doctor` also prints the Grok Build session count, both schedules, the newest daily index and the last reconcile;
  a torn index or history line is a red line.
- `selftest [-v]` — the package's tests (`src/ai_cost/tests/`, pytest-compatible) run from the shipped file on
  synthetic fixtures in a temp HOME, then the tests of every loaded plugin that ships some. Offline, no secrets.

## Not in scope

Negotiated discounts, free credits, marketplace billing, batch/flex tiers, VAT, and the cost of the human hours
spent steering the agents. Vendor numbers are estimates from public rate data — edit the profile before quoting
them to anyone.
