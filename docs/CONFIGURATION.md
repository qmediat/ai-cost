# Your plans and prices: configuration

`ai-cost install --init-config` writes `~/.config/ai-cost/config.json` from the shipped defaults
([`src/ai_cost/data/config.json`](../src/ai_cost/data/config.json)); it ships no subscriptions, add yours
([SETUP.md](SETUP.md#1-the-first-10-minutes), steps 3–4). `ai-cost doctor` lists what the file still lacks: a CLI
whose files never say how a session was paid (Claude Code always; Gemini CLI and Grok Build when they run on an
account plan) needs its `providers.<name>.billing`, and a provider used on a plan needs that plan under
`subscriptions`.

## Where things live

Override in the environment when your CLIs do:

| what | default | override |
|---|---|---|
| config, price overrides | `~/.config/ai-cost/config.json`, `prices.json` | `AI_COST_CONFIG_DIR`, `AI_COST_CONFIG`, `AI_COST_PRICES` |
| state (price-check result, monitor history, reconcile history, job logs) | `~/.local/state/ai-cost/` | `AI_COST_STATE_DIR` |
| usage log your programs write | `$XDG_DATA_HOME/ai-cost/usage.jsonl` (`~/.local/share/ai-cost/usage.jsonl`) | `AI_COST_USAGE_LOG`, more files in config `usage_logs` |
| daily reports | `$XDG_DATA_HOME/ai-cost/reports/<date>/` | `AI_COST_REPORTS_DIR`, `daily --out DIR` |
| Claude Code, Codex CLI, Gemini CLI, Grok Build CLI homes | `~/.claude`, `~/.codex`, `~/.gemini`, `~/.grok` | `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME`, `GROK_HOME` (the CLIs' own variables) |
| plugins | the `ai_cost.plugins` entry points and config `plugins` | `AI_COST_PLUGINS` |
| network | the price-drift check, the GitHub usage report (`providers.github.bill`) and `--github` only | `AI_COST_OFFLINE=1` disables the check and the usage report (its amounts are then missing, said in the report) |

## config.json

`~/.config/ai-cost/config.json` is merged over the shipped defaults (`ai-cost prices show --format json --snapshot`
prints the shipped prices; the shipped config is the file `install --init-config` writes). A `null` anywhere in it
means "no override" (the shipped value stays); inside a list a `null` is an error — a list such as `subscriptions`
replaces the shipped list whole, so there is no shipped item a `null` could stand for.

| key | type | default | meaning |
|---|---|---|---|
| `subscriptions[]` | `[{plan, seats, covers[], attribution}]` | `[]` | the plans you pay. `plan` is a name from `ai-cost prices show` (`claude-max-20x`, `chatgpt-plus`, `copilot-pro`, `github-team`, …); `seats` multiplies per-seat plans only; `covers` the providers it pays for; `attribution` `time` \| `full` \| `none` |
| `providers.<name>.billing` | `api` \| `subscription` \| `mixed` | `anthropic` mixed, `openai` mixed, `google` api, `xai` api, `deepseek` api | the rule for rows of that provider without billing evidence of their own; `mixed` = no single rule (the files decide, else unknown). Any provider id the pricebook lists (or one you add to `prices.json`) may have a rule: a plugin's xai or deepseek rows follow it as the built-in sources follow `anthropic.billing`, `openai.billing` and `google.billing` (Gemini CLI sessions) |
| `providers.anthropic.cache_ttl_default` | `5m` \| `1h` | `1h` | the tier of a legacy unsplit cache write |
| `providers.openai.default_model` | string | `""` | the model of a Codex rollout that does not name its own (empty = such rows are `unknown`) |
| `providers.xai.trust_cli_cost` | bool | `true` | `real` uses the Grok CLI's own cost figure for its sessions and review runs — a positive CLI-reported cost is the row's cash; 0 or absent means the CLI did not price the run and the list price applies; `false` = the list price |
| `providers.github.bill` | `{"scope": "organization" \| "user", "name": "<account>"}` or `null` | `null` | the account whose usage report prices GitHub: Copilot credits and seats, and Actions of the repositories named with `--github`. Read through `gh` (an organization's report needs an owner or billing manager; a user's shows only a personally bought plan); an enterprise is refused — its report leaves out cost-center usage, name the organization. With it, every other GitHub row is a count and a configured plan whose seats the report bills (`copilot-business` for "Copilot Business") gives way to the report; `doctor` checks that it can be read |
| `providers.<name>.report` | `{"source": …, <its settings>}`: `import` (DeepSeek), `management-api` + `team` (xAI), `bigquery-export` + `table` [+ `services`] (Google), `bss-api` [+ `products`, `endpoint`] (Alibaba), or `null` | `null` | the provider's own day report the account view compares with the local rows (ADR-0008): a positive difference is an untracked row, a negative one a warning; nothing is read without it ([SETUP.md §6](SETUP.md#6-reconcile-with-the-provider)) |
| `providers.github.actions_plan_exhausted`, `actions_runner` | bool, `linux` \| `windows` \| `macos` | false, linux | without `bill`: once the month's included minutes are gone, `real` prices `--github` minutes at the list price; the runner of an Actions row that names none. `copilot_plan_exhausted` is no longer read (a Copilot review has no price since 2.6) |
| `budgets.daily_usd`, `monthly_usd`, `per_provider_daily_usd.<name>` | numbers | 0, 0, `{}` — `daily_usd` / `monthly_usd` 0 = no check; a provider listed in `per_provider_daily_usd` is checked, 0 included (any spend is a breach), a provider left out is not | what `monitor` checks (exit 3 on breach) — API-equivalent figures |
| `window_default_hours` | number > 0 | 24 | the window when nothing else sets it |
| `reconcile.tolerance_pct` | number ≥ 0 | 5 | the gap `reconcile` accepts, in percent of the reported figure |
| `sizing.packet_bytes`, `sizing.loc` | `{XS, S, M, L}` thresholds | shipped | automatic work-item sizing for a plugin that supplies items |
| `vendor.default_profile`, `vendor.hours_per_day`, `vendor.profiles.<name>` | see the shipped file | `consultancy-eu`, 6 | the vendor quote (`--group vendor`): bands, staffing rates, time factors, senior review, package rounding |
| `plugins[]` | module names | `[]` | plugin modules loaded besides the `ai_cost.plugins` entry points ([USAGE-LOG.md](USAGE-LOG.md#a-plugin-for-a-source-of-your-own)); `AI_COST_PLUGINS=a,b` adds more |
| `plugin_settings.<plugin>.<key>` | anything | `{}` | a plugin's own settings; `--setting <plugin>.<key>=<value>` overrides one for a run |
| `usage_logs[]` | file paths | `[]` | more usage-log files to read (the default one is always read) |
| `outside_scope_clients[]` | client names | `[]` | clients whose sessions are outside the work you track (a Codex rollout's `originator`, e.g. `codex_work_desktop` for the Codex app used privately): no billing rule applies to their rows (a plan their file names still does), so rows without evidence stay unknown — out of real, still priced in the API-only group — are said apart in the report header and `doctor`, and are never a problem to fix; an empty name is a config error |

## prices.json

`~/.config/ai-cost/prices.json` — price overrides in the shape of
[`src/ai_cost/data/prices.json`](../src/ai_cost/data/prices.json), the registry the shipped file carries (the single
source; `prices show --format json --snapshot` prints it). An override may change numbers, never a price's shape;
a `null` anywhere in your file means "no override" (the shipped value stays), and to clear a shipped block set it to an
empty value: `"next": {}`, `"long": {}`, `"valid_until": ""`; a plan set to `null` is removed. A model you use that the
registry lacks goes here (`{"providers": {"openai": {"models": {"my-model": {"input": 1.0, "output": 4.0}}}}}`), or
the row carries a `cost` and needs no price.

A row's `aliases` are other ids it is billed under — a rolling name such as `gemini-pro-latest` — each a bare id (any
day) or `{"id": "gemini-pro-latest", "from": "2026-10-16"}` / `{"id": …, "until": "2026-10-15"}` (both ends
inclusive). A usage row's model is looked up as: the row with that id; else the row whose alias window holds the day
the usage row was made (none holds it: unpriced); else the row whose id it extends by a same-model suffix only
(`-preview`, `-exp`, either with a date — `MM-DD` included —, `-latest`, a date, then the endpoint `-customtools`,
then an optional `[…]` such as `[1m]`) — any other
extension (`-tts`, `-image`, `-live`) is another model, unpriced. To move an alias when the vendor does, close its
window on the old row and open it on the new one in your file, e.g. under `providers.google.models`:
`"gemini-3.1-pro-preview": {"aliases": [{"id": "gemini-pro-latest", "from": "2026-10-16", "until": "2026-12-31"}]}`
and `"gemini-4-argon": {"aliases": [{"id": "gemini-pro-latest", "from": "2027-01-01"}]}` (a list replaces the
shipped list whole). Windows of one alias that overlap in two rows, or an alias equal to a row id, are refused.
`auto_check_days: 0` turns the price-drift check off.

Where the numbers come from and what they leave out: [`references/pricing-sources.md`](../references/pricing-sources.md)
and [`references/vendor-pricing.md`](../references/vendor-pricing.md).
