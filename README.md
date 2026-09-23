<p align="left">
  <a href="https://www.qmediat.io/open-source?utm_source=oss-readme&utm_medium=ai-cost&utm_campaign=open-source">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/qmediat/.github/b35746f6b3c933d9eeb539033ef40ea9876349ae/assets/qmediat-wordmark-light.svg">
      <img src="https://raw.githubusercontent.com/qmediat/.github/b35746f6b3c933d9eeb539033ef40ea9876349ae/assets/qmediat-wordmark-badge.svg" alt="Quantum Media Technologies" height="40">
    </picture>
  </a>
</p>

# ai-cost — what did the AI-assisted work cost?

One command, three honest answers for a block of work done with LLMs — through Claude Code, Codex CLI, Gemini CLI,
Grok Build CLI, your own scripts and applications, or anything a plugin can read:

| group | question it answers | how |
|---|---|---|
| **real** | What did *you* pay? | Subscriptions prorated to the window + pay-per-token API keys at list price. No promos, no negotiated discounts. |
| **api** | What would the same tokens cost on pay-per-use APIs alone, as if no subscription existed? | Every token at the provider's list price, cache tiers applied the way the APIs bill. GitHub included: Copilot code reviews at the per-credit overage, Actions minutes at the per-minute price (private repos). |
| **vendor** | What would an outside firm have quoted for the same scope? | Effort bands per item × three staffing options (junior / mid / senior differ in rate *and* in time, juniors get senior review), packaged with an integration allowance, minimum size and rounding. |

It is a Python 3 package (stdlib only) shipped as one executable file: a zipapp that
`python3 scripts/build.py src dist/ai-cost` builds from `src/ai_cost/`, tests included. It reads what is already on disk — Claude Code
transcripts, Codex CLI rollouts, Gemini CLI sessions, Grok Build CLI sessions, the usage log any program can write —
and whatever a plugin adds. Nothing is sent anywhere. The network is used only when asked for: the optional price-drift check reads the
vendors' public pricing pages, and `--github` queries GitHub through `gh` for live counts.

```
$ ai-cost report --session latest
# AI cost report — 2026-09-19T15:01:09Z → 2026-09-20T00:35:24Z (9.6 h)
## 1. Real cost (what you paid): 54.71 USD
## 2. API-only cost (as if no subscription existed): 175.02 USD
## 3. Vendor quote (what an outside firm would charge): profile `consultancy-eu`
| Staffing | Rate EUR/h | Time factor | Senior review | Hours | Working days | Quote EUR |
| junior | 70 | 1.8 | 25% | 148–296 | 24.7–49.3 | 12 950–25 900 |
| mid    | 100 | 1.2 | 10% | 96–192  | 16–32     | 12 600–25 200 |
| senior | 150 | 1.0 | 0%  | 76–152  | 12.7–25.3 | 11 250–22 500 |
```

## Install

New here? [`docs/SETUP.md`](docs/SETUP.md) is the deterministic path — the first 10 minutes with a Verify command per
step, what is read from where, the config reference, the usage-log hook for apps and MCP servers (Python and Node),
a plugin skeleton, the daily job, `reconcile`, exit codes and every `doctor` line — written for the person and for the
AI agent operating the tool on their behalf.

```bash
pipx install ai-cost             # or: pip install ai-cost
# or the single executable file from the GitHub release: put it on PATH and run it
# or no package at all: standalone/SKILL.md is a one-file Claude Code skill that counts and prices with a stdlib snippet
ai-cost selftest [-v]            # the package's own tests, run from the shipped file, offline
ai-cost install --init-config    # writes ~/.config/ai-cost/config.json — put YOUR plans, seats and budgets there
ai-cost install --schedule 3     # price drift check every 3 days (launchd on macOS, cron elsewhere)
ai-cost install --schedule-reports   # yesterday's global + per-project reports every morning (06:40 local; --at HH:MM)
ai-cost doctor                   # sources found, plugins loaded, config, price freshness, both schedules, last daily run
```

Requirements: Python ≥ 3.9. `gh` only for `--github`.

## Commands

| command | does |
|---|---|
| `report` (default) | the three groups for a window. `--session <id\|latest\|all\|path>` `--project DIR` (every source scoped to that directory — "Per project" below) `--all-projects` · `--unpriced fail\|skip` · `--since/--until` `--hours N` (N > 0; default the config's `window_default_hours`, 24) · `--github owner/repo` · `--group real,api,vendor` · `--items scope.md` `--vendor-profile NAME` · `--setting PLUGIN.KEY=VALUE` · `--format md\|json\|table` `--out FILE` `--detail` |
| `log` | append one usage line for a request your program made: `--provider openai --model gpt-5.5 --input 1200 --output 300 [--cost 0.0123] [--ref job-42] [--tag ci]`, or `--from-response resp.json [--provider …]` for a raw Anthropic / Google / OpenAI response; `--log FILE` picks the file — a model the pricebook does not list needs `--cost` (or an entry in your prices file): the line is refused rather than written as a row every report would fail on |
| `prices show` | merged registry (shipped defaults → your overrides) |
| `prices check` | re-read every vendor page, report `confirmed` / `changed?` / `not-found` / `fetch-failed`; exit 4 on drift |
| `prices update` | check, then write unambiguous changes to **your** `~/.config/ai-cost/prices.json` |
| `doctor` | diagnostics: sources found (Claude, Codex, Gemini CLI, Grok Build), plugins, config, prices, both schedules, the newest daily index, the last reconciliation; exit 1 on problems |
| `daily` | write one UTC day's reports: `global.{md,json}` over every project and `<project dir>.{md,json}` per Claude project touched that day, plus `index.json` — `--date YYYY-MM-DD` (default yesterday), `--out DIR` (default `AI_COST_REPORTS_DIR` or `$XDG_DATA_HOME/ai-cost/reports`), `--quiet`; the job `install --schedule-reports` runs |
| `reconcile` | `--provider xai --usd 156.11 [--tokens 80200000] --hours 24` (or `--since/--until`): the local count of one provider against the figure its console shows, gap vs `reconcile.tolerance_pct` (5); exit 1 above it; history in `~/.local/state/ai-cost/reconcile.jsonl` |
| `monitor` | rolling-window totals (`--hours N`, default the config's `window_default_hours`) → `~/.local/state/ai-cost/history.jsonl` with `--append`; budget check from config, exit 3 on breach; `--history N` |
| `install` | `--init-config`, `--schedule DAYS`, `--unschedule`, `--schedule-reports [--at HH:MM]` (the daily job, 06:40 local by default), `--unschedule-reports` |
| `selftest` | offline, no network, no secrets; also runs the tests of every loaded plugin that ships some |

The window comes from `--since/--until`, else from the session's first and last timestamp, else the last `--hours`
(the config's `window_default_hours`, 24 by default).
Every source is filtered to that window, so a report is reproducible. Without `--session/--project`, the cwd's
project transcripts are read (every project's when the cwd has none). Codex usage is summed per turn inside the
window, so a session resumed from before it or still running after it contributes only the turns in between.

**Per project.** Every CLI row carries the working directory its CLI ran in (a Codex rollout's `cwd`, a Gemini CLI
folder mapped through `~/.gemini/projects.json`, a Grok Build session's directory), and a row that names no scope
takes the scope of the row sharing its ref (a usage-log line with `session`, a ledger line). `--project DIR` — and
a plain `report` run from inside a project — then keeps the project's Claude transcripts and every row whose
directory is `DIR` or below it (symlinks resolved), leaves out rows of other directories and rows of named
workspaces that are no directory (a review workspace, a folder the map does not know — `--attribute` places those),
and includes rows that name nothing; the header counts each group per source, so the number is honest about what it
could not place. A git worktree is its own project. `--all-projects` and `--session <id>` never filter.

## Sources it reads

| source | path | used for |
|---|---|---|
| Claude Code transcripts | `~/.claude/projects/<project>/<session>.jsonl` (+ `<session>/**/*.jsonl` subagents) | tokens per model incl. cache write 5 m / 1 h, cache read, server tools; message ids de-duplicated |
| Codex CLI rollouts | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | one row per model and UTC day of every session with turns inside the window: input / cached / output per turn. A rollout that names a ChatGPT plan (`rate_limits.plan_type`) is a subscription session; the rest follow `providers.openai.billing` and stay `unknown` without it. The header's `cwd` is the row's working directory |
| Gemini CLI sessions | `~/.gemini/tmp/<project>/chats/session-*.jsonl` (older versions: `.json`) | one row per model message (streamed repeats of one message id counted once); `input` includes `cached`, `thoughts` are billed as output. `~/.gemini/projects.json` maps the folder (a name, or the sha256 of the path in older versions) to the working directory |
| Grok Build CLI sessions | `~/.grok/sessions/<working directory, URL-encoded>/<session>/usage.json` (`GROK_HOME` overrides the home) | one row per turn and model: `inputTokens` (includes `cachedReadTokens`), `outputTokens` + `reasoningTokens` as output, `modelCalls` as the requests, `costUsdTicks / 1e10` as the CLI's own cost estimate — `real` uses it when `providers.xai.trust_cli_cost` (default), `api` prices at list; billing follows `providers.xai.billing` |
| usage log | `$XDG_DATA_HOME/ai-cost/usage.jsonl` (`AI_COST_USAGE_LOG` overrides; more files in config `usage_logs`) | one JSON line per request that any program writes — `ai-cost log` or the schema below; provider-native counters, `cost` when the program knows it, `event_id` read once |
| GitHub (optional) | `gh` | Copilot reviews submitted inside the window, Actions **billable** minutes per runner OS (`/actions/runs/{id}/timing`, elapsed time as fallback), private/public |
| plugins | whatever they read | see below |

A row's billing comes first from what its source saw (a named plan, a log line's `billing` key), then from the
provider's configured billing, and stays `unknown` otherwise — an unknown row is priced in `api` and left out of
`real` whatever figure it carries (a figure without a rule is an estimate, not a charge), and the report header says
how many there were. A reported `cost` is the charge of a row that says it paid per use.

## Let an app or script report its own calls

Any program appends one JSON line per request to the usage log (`$XDG_DATA_HOME/ai-cost/usage.jsonl`,
`~/.local/share/ai-cost/usage.jsonl` by default, or the file named by `AI_COST_USAGE_LOG`); the report reads it
like every other source. Required: `schema` (1), `at` (ISO 8601), `provider`, `model`, and `tokens` or `cost`.
Counters use the provider's own names so the pricing stays exact:

```json
{"schema":1,"at":"2026-09-20T10:00:00Z","provider":"openai","model":"gpt-5.5","source":"my-app",
 "tokens":{"input":1200,"cached_input":800,"output":300},"cost":0.0123,"ref":"job-42","branch":"feat/x","tags":["ci"]}
```

Counters per provider: anthropic `input, output, cache_read, cache_write_5m, cache_write_1h` (or
`cache_write_unsplit` when the response does not say which TTL) and `web_search` (searches, billed per thousand); openai and OpenAI-compatible APIs `input,
cached_input, output`; google `prompt, cached, output, thoughts` (thoughts count as output — for every provider: reasoning tokens are billed as output); deepseek `cache_hit,
cache_miss, output`; anything else `input, output`. A counter the provider's formula never reads (an `input` on a
deepseek line) makes the line a counted skip, and `ai-cost log` / `record()` refuse it up front — nothing is priced
at zero in silence. Optional keys: `cost` (what the request was charged, USD — a `currency` other than `USD` is a
counted skip; a row with `cost` and no list price is priced by it), `billing` (`api` | `subscription`; a line with tokens or a cost is
`api` by default), `ref`, `session`, `branch`, `pr`, `tags`, `source`, `event_id` (a repeated id within one file is
read once). From Python: `ai_cost.log.record("openai", "gpt-5.5", {"input": 12, "output": 3}, ref="job-42")`;
from a shell: `ai-cost log --from-response resp.json --provider openai` maps a raw API response's usage block
(Google, DeepSeek and Anthropic name themselves — Google by `usageMetadata`, DeepSeek by `prompt_cache_hit_tokens`, Anthropic by `type: message` or its cache counters; a bare OpenAI-shaped usage block needs `--provider`). A line that fails validation (a boolean, a
negative or fractional count, a malformed provider id) is a counted skip, never a guess; a well-formed provider or model the pricebook does not list, with no cost, is an unpriced row under `--unpriced`.

## Plugins

Any program can add a source (rows for a window), an enricher (a pass over every row, e.g. to settle what a key was
actually charged), doctor lines and tests. A plugin is a module exporting `PLUGIN = ai_cost.plugins.Plugin(...)`,
discovered through the `ai_cost.plugins` entry-point group, the config list `plugins: ["my_package"]`, or the
environment variable `AI_COST_PLUGINS=my_package,other`. Its settings live under `plugin_settings.<name>` in the
config and can be overridden for one run with `--setting <name>.<key>=<value>`. The protocol is in
[`src/ai_cost/plugins.py`](src/ai_cost/plugins.py): a `Source` has a `name` and `collect(ctx) -> Collected`, an
`Enricher` has `enrich(rows, ctx) -> rows`, and `Context` gives them the paths, the config, the window, the request,
the plugin's settings and the shared `skipped` / `warnings` sinks.

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

## Per project, per day: the daily job

`ai-cost daily` prices the UTC day before today once over every project and once per Claude project directory whose
transcripts were written that day, and writes `<reports>/<date>/global.md` + `global.json`, `<project dir
name>.md` + `.json` (the `--project` scope of the project's working directory, read from its transcripts' `cwd`)
and `index.json` — `{day, generated_at, window, directory, real_usd, api_usd, cash_usd, rows, projects: [{project_dir,
path, markdown, json_file, real_usd, api_usd, rows}], notes}`. A project no transcript can place, one with no usage
that day, or one whose report failed is a note, never an abort; unpriced rows are skipped and counted, so a new
model never fails the job. `<reports>` is `AI_COST_REPORTS_DIR`, else `$XDG_DATA_HOME/ai-cost/reports`
(`~/.local/share/ai-cost/reports`). `ai-cost install --schedule-reports` registers the job at 06:40 local time
(`--at HH:MM` to move it; launchd on macOS runs a missed time at wake, cron does not); `--unschedule-reports`
removes it; `doctor` shows whether it is installed and what the newest index says. `ai-cost daily --date 2026-09-20`
re-runs a day; `--out DIR` writes elsewhere.

## Reconcile with the provider's console

`ai-cost reconcile --provider xai --usd 156.11 --tokens 80200000 --hours 24` prints the local count for the window
(the cash `real` attributes to the provider — a CLI's own figure where the config trusts it, else the list price —
and the tokens it billed, every counter once) next to the figures you read off the provider's console or export, the
gap in percent against `reconcile.tolerance_pct` (5 in the shipped config) and a verdict; above tolerance is exit 1.
Every run is appended to `~/.local/state/ai-cost/reconcile.jsonl` and `doctor` shows the last one. Nothing is
fetched: no provider offers one public spend endpoint every user could call, so the figure is yours to paste (or a
script's, from an export). A gap is something to look at — the window's edges, a source the tool does not read, a
price it applies differently — never a number to hide.

## JSON contract (`--format json`)

Top level: `version`, `generated_at`, `window` (`{start, end}`), `window_iso` (`[start, end]`), `window_hours`,
`row_count`, `sources`, `warnings`, `skipped` (`[{source, path, reason}]`), `prices_checked_at`, `real`
(`subscriptions[]`, `usage[]`, `cash_usd`, `subscription_usd`, `total_usd`, `unknown_billing`), `api` (`lines[]`,
`total_usd`), `vendor` (when items exist), `attribution` (with `--attribute`). A line's `calls` is what it folded in
(rows, review runs, Copilot reviews — the `Runs` column) and `model_calls` the API requests its sources reported
(`Model calls`; 0 where none does); `--detail` adds `rows[]`, where
`cost_reported` is the source's own figure for the row: cash for an `api` row (a charge the source reported), the
source's list-price estimate for a `subscription` row (never counted as cash; the API-equivalent fallback when the
model has no list price), a part of the session's figure when a session spans several rows; `source` names the
source that produced the row and `kind` its record shape (`transcript`, `session`, `chat`, `log`, `ledger`,
`review`, `copilot`, `actions`). Every dataclass property is serialised, so computed totals are always present.
Changes since 1.0: `window` is an object (the list moved to `window_iso`), `seats` multiply only per-seat plans, an
unpriced model exits 5 unless `--unpriced skip`.

## Configuration

`~/.config/ai-cost/config.json` (`install --init-config` writes it from the shipped defaults,
[`src/ai_cost/data/config.json`](src/ai_cost/data/config.json); it ships no subscriptions, add yours) — subscriptions
(plan, seats, attribution `time` | `full` | `none`), provider billing switches (`providers.<name>.billing`, the rule for that provider's rows that carry no billing evidence of their own — a plugin's xai or deepseek rows follow it as the built-in sources follow `anthropic.billing`,
`openai.billing`, `google.billing` for Gemini CLI sessions, `github.copilot_plan_exhausted`, `xai.trust_cli_cost` — a
positive CLI-reported cost is the row's
cash, 0 or absent means the CLI did not price the run and the list price applies), budgets, item sizing thresholds,
vendor profiles, `plugins` and `plugin_settings`. A `null` anywhere in it means "no override" (the shipped value
stays); inside a list a `null` is an error — a list such as `subscriptions` replaces the shipped list whole, so there
is no shipped item a `null` could stand for. `openai.default_model` names the model of a Codex rollout that does not
say its own (empty by default: such rows are `unknown`).
`~/.config/ai-cost/prices.json` — price overrides in the shape of
[`src/ai_cost/data/prices.json`](src/ai_cost/data/prices.json), the registry the shipped file carries (the single
source; `prices show --format json --snapshot` prints it). An override may change numbers, never a price's shape;
a `null` anywhere in your file means "no override" (the shipped value stays), and to clear a shipped block set it to an
empty value: `"next": {}`, `"long": {}`, `"valid_until": ""`; a plan set to `null` is removed.
`auto_check_days: 0` turns the price-drift check off. `reconcile.tolerance_pct` (5) is the gap `reconcile` accepts.
Env: `AI_COST_CONFIG`, `AI_COST_PRICES`, `AI_COST_CONFIG_DIR`, `AI_COST_STATE_DIR`, `AI_COST_USAGE_LOG` (else
`$XDG_DATA_HOME/ai-cost/usage.jsonl`), `AI_COST_REPORTS_DIR` (else `$XDG_DATA_HOME/ai-cost/reports`), `AI_COST_OFFLINE=1`,
`AI_COST_PLUGINS`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME` (`~/.gemini` by default), `GROK_HOME`
(`~/.grok` by default).

Where the numbers come from and what they leave out: [`references/pricing-sources.md`](references/pricing-sources.md)
and [`references/vendor-pricing.md`](references/vendor-pricing.md).

## Development

`src/ai_cost/` is the package: `models.py` (the typed data — `UsageRow`, `Tokens`, `Window`, `WorkItem`, `Report`),
`pricing.py` (one pricer per price shape, chosen from a dispatch table), `groups.py` (real / api / vendor),
`collectors/` (one module per built-in source — `claude`, `codex`, `gemini_cli`, `grok_build`, `github`, `usage_log` — each
returning `Collected` rows plus counted `Skipped` records), `plugins.py` (the source / enricher protocol and
discovery), `config.py` with `data/{prices,config}.json`, `prices_check.py`, `ops.py` (report assembly and the
per-project scope, doctor, monitor, the scheduled jobs), `daily.py`, `reconcile.py`, `render.py`, `cli.py`. The design note and
the architecture decision records are kept with the development source, not in the published tree.

```bash
PYTHONPATH=src python3 -m ai_cost selftest      # or python3 -m pytest — the tests live in src/ai_cost/tests/
python3 scripts/build.py src dist/ai-cost   # the shipped single file, reproducibly (fixed timestamps: a rebuild is byte-identical)
```

## Honest limits

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

MIT © 2026 Quantum Media Technologies sp. z o.o. Not affiliated with Anthropic, OpenAI, Google, xAI, DeepSeek or GitHub.

---

Made by [Quantum Media Technologies](https://www.qmediat.io/open-source?utm_source=oss-readme&utm_medium=ai-cost&utm_campaign=open-source) · [more open source from qmediat](https://github.com/qmediat)
