# ai-cost — setup and operation (for the person AND their AI agent)

This is the deterministic path from an empty machine to daily, per-project cost reports. If you are an AI agent
reading this: run the numbered steps in order, run every **Verify** command, and stop at the first one whose output
does not match — say what you saw instead of improvising. Everything here is idempotent (safe to re-run). Nothing
here needs our setup: `ai-cost` reads only files on the machine it runs on and sends nothing anywhere; the only
network use is the optional price-drift check of the vendors' public pricing pages, and — when you ask for it — the
GitHub account's usage report (`providers.github.bill`) and `--github`, both through `gh`, and `collect`, through `ssh`.

Contents: [1. The first 10 minutes](#1-the-first-10-minutes) · [2. What is read, from where](#2-what-is-read-from-where) ·
[3. Configuration reference](#3-configuration-reference) · [4. Count your own apps and MCP servers](#4-count-your-own-apps-and-mcp-servers) ·
[5. Per project, per day](#5-per-project-per-day) · [6. Reconcile with the provider](#6-reconcile-with-the-provider) ·
[7. A plugin for a source of your own](#7-a-plugin-for-a-source-of-your-own) · [8. Operate it from an agent](#8-operate-it-from-an-agent) ·
[9. Exit codes and what to do](#9-exit-codes-and-what-to-do) · [10. Troubleshooting by doctor line](#10-troubleshooting-by-doctor-line)

## 1. The first 10 minutes

Requirements: Python ≥ 3.9 (`python3 --version`), a POSIX shell. `gh` only for GitHub (`providers.github.bill`, `--github`); `launchctl` (macOS) or
`crontab` for the scheduled jobs.

| step | command | Verify |
|---|---|---|
| 1. Get the tool | `pipx install ai-costs` (or `pip install ai-costs`; the distribution is `ai-costs`, the command stays `ai-cost`). From npm: `npm install -g ai-costs` — the same program, started with the Python ≥ 3.9 on the machine (`AI_COST_PYTHON` names another); install it globally rather than through `npx` when you schedule jobs, so they keep pointing at a file that stays. Or one executable file, no dependencies: download `ai-cost` from the release page of github.com/qmediat/ai-cost, `chmod +x ai-cost`, put it on `PATH` (for example `~/.local/bin/ai-cost`). Or no package at all: `standalone/SKILL.md` is a one-file Claude Code skill that counts and prices with a stdlib snippet. From a checkout: `python3 scripts/build.py src dist/ai-cost` writes `dist/ai-cost` | `ai-cost --version` prints `ai-cost <version>`, the one you installed |
| 2. Prove it works offline | `ai-cost selftest` (`-v` prints the passes too) — the shipped tests run from the file itself against synthetic data in a temp directory; no network, no secrets | last line `ai-cost selftest: N passed, 0 failed` (N ≥ 299) |
| 3. Write your config | `ai-cost install --init-config` → `~/.config/ai-cost/config.json` (`AI_COST_CONFIG_DIR` moves it). The file ships with **no subscriptions on purpose**: an example plan would be counted as money you paid. Add yours (step 4) | `written /…/config.json`, then the next steps (the keys below) and the link to this guide |
| 4. Tell it what you pay for | edit `subscriptions` — one object per plan you pay: `{"plan": "<name from ai-cost prices show>", "seats": 1, "covers": ["anthropic"], "attribution": "time"}`. `covers` names the provider(s) the plan pays for (`anthropic`, `openai`, `github-copilot`, `github-actions`, …); `attribution` is `time` (window hours ÷ 730 of the monthly fee), `full` (the whole fee) or `none`. Then set `providers.<name>.billing` to `subscription` for a provider your plan pays (`anthropic` for a Claude plan, `openai` when Codex runs on a ChatGPT plan), `api` for one you pay per token, `mixed` when the files must decide (a Codex rollout that names its ChatGPT plan is placed on it; some sessions — Codex desktop ones among them — name none and stay unknown under `mixed`). Gemini CLI and Grok Build files do not say it either: `google` and `xai` default to `api` (an API key); signed in with an account plan instead, set `subscription` and add that plan (a plan the registry lacks, a free tier too, goes into your `prices.json` `plans`) | `ai-cost doctor` prints `ok  subscription <plan> ×<seats>` per plan, one `<provider>: N row(s) in the last 24 h — … (providers.<provider>.billing = …)` line per provider you used, and no `!!` line |
| 4b. If you pay for GitHub Copilot | set `providers.github.bill` to the account that pays: `{"scope": "organization", "name": "<org>"}` (or `"user"` for a personally bought plan). Its usage report prices Copilot to the credit — a review has no price of its own — and states the seats; `gh` must be logged in as an owner or billing manager of that organization | `ai-cost doctor` prints `ok  GitHub usage report (organization <org>): readable, newest day with lines <date>` |
| 5. See what is on disk | `ai-cost doctor` — every source found with its count, plugins, config, prices, both schedules | `doctor: all good` (a `--` line is informational: a source you do not use, a job not installed) |
| 6. First report | `ai-cost report --hours 24 --all-projects --group real,api --format table --unpriced skip` | a `# AI cost report — … (24.0 h)` header, `Sources: …` naming what was found, sections 1 (real) and 2 (API-only) |
| 7. Per project | `cd` into a project and run `ai-cost report --hours 24`, or `ai-cost report --project /path/to/project --hours 24` | the header lists `--project …: N row(s) … left out` / `… included` lines: what could and could not be placed (§5) |
| 8. Every morning, per project | `ai-cost install --schedule-reports` (06:40 local; `--at 07:15` to move it) — then `ai-cost daily` once by hand to see the files | `doctor` prints `ok  scheduled daily reports: installed`; `ai-cost daily` prints `daily <date>: real … USD, api-only … USD, N row(s), M project(s) → <reports>/<date>` |
| 9. Keep prices current | `ai-cost install --schedule 3` (a drift check every 3 days) — or `ai-cost prices check` when you wonder | `doctor` prints `ok  scheduled price check: installed`; `prices check` exits 0 (4 = a page changed: verify, then `prices update`) |
| 10. Check it against the invoice | when a provider's console shows a figure for a window: `ai-cost reconcile --provider xai --usd 156.11 --hours 24` | `reconcile: within tolerance` (exit 0) — or the gap and exit 1 (§6) |

Where things live, and the variables that move them: [CONFIGURATION.md](CONFIGURATION.md#where-things-live).

## 2. What is read, from where

Every source, the files it reads, one row per what, the billing evidence in the file and the row's working
directory: [SOURCES.md](SOURCES.md).

## 3. Configuration reference

Every key of `config.json`, the price overrides in `prices.json` and where things live: [CONFIGURATION.md](CONFIGURATION.md).

## 4. Count your own apps and MCP servers

One JSON line per request your script, app, MCP server or CI job makes — its fields, the counters per provider,
and how to write it from a shell, Python or Node: [USAGE-LOG.md](USAGE-LOG.md).

## 5. Per project, per day

Every CLI row carries the working directory its CLI ran in ([SOURCES.md](SOURCES.md), last column), and a row that
names no scope takes the scope of the row sharing its ref (a usage-log line with `session`, a ledger line). `ai-cost report --project DIR` — and
a plain `report` run from inside a project — keeps the project's Claude transcripts and every row whose directory is
`DIR` or below it (symlinks resolved), **leaves out** rows of other directories and rows of named workspaces that are
no directory (a plugin's review workspace, a Gemini folder the map does not know — `--attribute` places those),
**includes** rows that name nothing (a usage-log line without `session`, a GitHub row), and counts each group per
source in the header, so the number says what it could not place. A git worktree is its own project. `--all-projects`
never filters; `--session <id>` keeps the rows stamped with that session, from every project (ADR-0009).

`ai-cost daily` prices the UTC day before today (`--date YYYY-MM-DD` for another) once over every project and once per
Claude project whose transcripts were written that day, and writes under `<reports>/<date>/`:

| file | content |
|---|---|
| `global.md`, `global.json` | the whole day, every project (`--all-projects`) |
| `<project dir name>.md`, `.json` | the `--project` scope of that project's directory (read from its transcripts' `cwd`); a project with no usage that day gets no file, a note instead |
| `index.json` | `{day, generated_at, window, directory, real_usd, api_usd, cash_usd, rows, projects: [{project_dir, path, markdown, json_file, real_usd, api_usd, rows}], notes: [...]}` |

Unpriced rows are skipped and counted (a new model never fails the job); a project that cannot be placed or priced, or whose report failed, is
a note, never an abort. `ai-cost install --schedule-reports` registers the job at 06:40 local time (`--at HH:MM`; launchd on macOS runs
a missed time at wake, cron does not), `--unschedule-reports` removes it, its log is `<state>/daily-report.log`, and
`doctor` shows the schedule and the newest index. To read the numbers from a script: `jq '.projects[] | [.path, .real_usd]' index.json`.

## 6. Reconcile with the provider

When a provider's console (or an export) shows what a window cost, compare:

```bash
ai-cost reconcile --provider xai --usd 156.11 --tokens 80200000 --since 2026-09-20T08:00Z --until 2026-09-21T08:00Z
```

prints the local cash `real` attributes to the provider (a CLI's own figure where `trust_cli_cost` says so, else the
list price) and the tokens it billed (every counter once), the reported figures, the gap in percent against
`reconcile.tolerance_pct`, a note when rows of unknown billing are in the tokens but not in the cash, and a verdict.
Exit 0 within tolerance, 1 above it; every run is appended to `<state>/reconcile.jsonl` and `doctor` shows the last.
Nothing is fetched: no provider offers one public spend endpoint every user could call, so the figure is yours to
paste (or a script's, from an export). GitHub needs no reconcile once `providers.github.bill` is set: the report's
"GitHub usage report" section is the account's own report, to the last digit — real = the net of the usage lines
(the cash `reconcile --provider github` compares) + the net of the seat lines (subscription shares, not cash). A gap is something to look at — the window's edges (the console's day may
not be UTC), a source the tool does not read, a price it applies differently (the API-only group prices a turn's
aggregated counters, so a turn of several calls never gets a long-context tier) — never a number to hide.

### Provider day reports: what no local record shows

A provider's own day report names what the account was billed: every machine, key and project of the account, while
`ai-cost` reads the records of the machine it runs on. The difference holds a call this machine did not record (a
script, another tool) and the work of other machines on the same account. For each day the report can hold whole,
`ai-cost` compares the provider's figure with this machine's rows of the same interval.

- A positive difference becomes one **untracked** row (`provider report − this machine's records`): the gross amount
  in the API-only group, the net in the real group.
- A negative difference is only said, and nothing is booked: a price above the provider's, a call counted twice, or a
  row of another account.
- A day the window cuts, a day the provider may still add to, or a day whose cash sits on other rows is listed with
  its amounts, never apportioned.
- Only an account view compares (`--all-projects`, no session filter); a per-project report never does.

The "Provider report" section (JSON `provider_reports`, amounts as text) holds the figures. DeepSeek is imported from
its usage export; xAI, Google and Alibaba are read live.

### Provider day reports (DeepSeek)

DeepSeek has no usage API, so its usage export is imported:

| Step | Do | Verify |
|---|---|---|
| 1. Name the source | `"providers": {"deepseek": {"report": {"source": "import"}}}` in your config | `ai-cost doctor` prints a `provider report deepseek` line |
| 2. Export | DeepSeek platform → Usage → export a date range (a ZIP with `cost-<from>_<to>.csv` and `amount-<from>_<to>.csv`) | the ZIP holds both files |
| 3. Import | `ai-cost import deepseek <the ZIP>` (CSV files: add `--captured <when you exported>`) | it prints the days added and replaced |
| 4. Compare | `ai-cost report --all-projects --since <a day start> --until <a later day start>` in the export's own offset | the "Provider report: deepseek" section lists each day with the local amounts and the difference |

- The import refuses an export that does not add up. That covers two accounts, a duplicated row, a cost that its
  priced amounts do not make, an unknown column or counter, and an export older than the one already stored. It keeps amounts,
  counters and times, never the account or a key.
- Each day keeps the interval the export states: a daylight-saving day is 23 or 25 hours. The time between two
  days with usage is a zero interval. A later import replaces every stored day its days touch.
- A day is closed once it had ended when the export was taken; the export's own time is read as the earliest instant
  it can mean.
- The export's days keep its offset (the platform wrote `+02:00` in 2026-09). A UTC `daily` file holds one whole only
  when the export was taken in UTC; otherwise a report over whole days of that offset compares them.

### Provider day reports read live (xAI, Google, Alibaba)

Each is optional. The config names the source; the key comes from the environment, never from the config.

| Provider | Least privilege | Config | Verify |
|---|---|---|---|
| xAI | a management key with billing read access (xAI console → Settings → Management keys), in `XAI_MANAGEMENT_KEY` | `"xai": {"report": {"source": "management-api", "team": "<team id>"}}` | `ai-cost doctor` prints `provider report xai (management-api): newest closed day …` |
| Google | the Cloud Billing export to BigQuery, enabled by a billing account administrator; the reader needs BigQuery Data Viewer on the dataset and Job User on the project, through the `gcloud` / `bq` login | `"google": {"report": {"source": "bigquery-export", "table": "<project>.<dataset>.gcp_billing_export_v1_<account>", "services": ["Gemini API"]}}` | `bq query` of the table answers; `doctor` prints the line |
| Alibaba | a RAM user whose only permission is `bssapi:DescribeInstanceBill`, its AccessKey in `ALIBABA_BILL_ACCESS_KEY_ID` / `ALIBABA_BILL_ACCESS_KEY_SECRET` | `"alibaba": {"report": {"source": "bss-api"}}` (`"products": ["<code>"]` narrows, `"endpoint"` changes the host) | `doctor` prints the line |

- **xAI days are UTC days.** A day is closed 24 hours after it ends. xAI documents no delay, so this rule is the
  tool's.
- **Google days are UTC days, summed from the hourly export.** A day is closed 72 hours after it ends, because late
  usage can still arrive.
  - An account in another currency is converted by Google's own rate on each line, never another, and rounded to
    nine decimals (the precision the GitHub usage report states its amounts in); the report prints the rate.
  - Credits make the difference between gross and net.
  - Taxes and other services are listed, never compared.
- **Alibaba's bill times are UTC+8.** A day is final after 12:00 UTC+8 on the 4th of the next month, when Alibaba
  finalizes the month. A UTC+8 day never fits a UTC `daily` file: only a report over whole UTC+8 days compares it.
- **The network is used only when a source is configured**, and never with `AI_COST_OFFLINE=1`. `doctor` then says the
  source was not asked. A report whose window holds no whole provider day (a few hours, a rolling `monitor` window
  that crosses midnight) does not ask either: there is no day to compare.
- **`daily` reads a day again when its provider report was not final**: still open, not read, not asked, or not
  reached by the export yet. It retries each run for 7 days. A written day is kept only when the new report would
  lose a provider day it compared.

## 7. A plugin for a source of your own

The protocol, a skeleton and the three ways to register one: [USAGE-LOG.md](USAGE-LOG.md#a-plugin-for-a-source-of-your-own).

## 8. Operate it from an agent

| the person asks | run | read |
|---|---|---|
| what did this session cost | `ai-cost report --session <id>` (or a unique prefix / a transcript path; `latest` is the newest transcript's period) | sections 1 and 2; say what the header left out (other sessions, unstamped rows), the sources found, `prices checked_at` |
| what did the last N hours / this week cost | `ai-cost report --hours N --all-projects` or `--since … --until …` | as above |
| what did project X cost | `ai-cost report --project /path/to/X --since … --until …` | the header's `--project …` lines say what was left out and what is included without a directory |
| what did yesterday cost, per project | `ai-cost daily` then the files under `<reports>/<date>/`; `index.json` for the numbers | `projects[].real_usd`, `notes[]` |
| what did feature / PR X cost | `ai-cost report --since … --until … --all-projects --attribute "X=<branch\|PR number\|paths regex>"` | section 4: `mixed` and `unattributed` are always there |
| my app calls the API, count it | §4: one usage-log line per request, then any `report` | the header names the source |
| is it right | `ai-cost reconcile --provider P --usd <console figure> --hours 24` | exit 0/1 and the gap line |
| something is off | `ai-cost doctor` | the `!!` lines (§10) |
| are prices current | `ai-cost prices check` | exit 4 = a page changed; verify, then `prices update` |
| for machines | add `--format json` (`--detail` for every row) to any `report` | the JSON contract: [JSON.md](JSON.md) |

Prefer `--format md` for people and `--format json --detail` when you need rows. `AI_COST_OFFLINE=1` skips the
background price check. Never round away small numbers; quote both `real` and `api`, and say which subscriptions were
prorated. For Codex and Gemini agents the same file works: `SKILL.md` and `standalone/SKILL.md` are open-standard
skill files readable by any agent.

## 9. Exit codes and what to do

Every exit code and what to do about it: [COMMANDS.md](COMMANDS.md#exit-codes-and-what-to-do).

## 10. Troubleshooting by doctor line

| line | meaning | fix |
|---|---|---|
| `--  Claude transcripts: none (… does not exist)` or `: 0 under …` (likewise Codex rollouts, Gemini CLI sessions, Grok Build sessions) | you do not use that CLI, or it keeps its files elsewhere | informational — if you do use it, the variable the line names: `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `GEMINI_CLI_HOME` / `GROK_HOME` |
| `!!  <CLI>: … cannot be read` / `… N directory(ies) under it cannot be read` | the CLI's session directory, or a project directory under it, exists but cannot be listed: its files would be missed (a report counts each as a `cannot list` skip and reads the rest) | fix the directory's permissions |
| `!!  no usage found: …` | no CLI files, no usage log, no plugin rows in the last 24 h | point the homes above at where your CLIs keep their files; §4 for your own programs |
| `--  usage log: none yet` / `usage log …: absent (listed in usage_logs)` | no program writes that usage log (yet) | informational; §4 |
| `!!  usage log …: not a readable file` | the path is a directory, or the file cannot be read: its lines would be skipped | fix the path in `usage_logs` / `AI_COST_USAGE_LOG`, or the file's permissions |
| `--  budgets for monitor: …` | the budgets `monitor` checks, or none | informational; `budgets` in §3 (a config written by an earlier `install --init-config` carries the budgets that version shipped) |
| `!!  <CLI>: providers.<name>.billing names no rule (mixed) …` | the CLI's files never say how a session was paid (Claude Code, and Gemini CLI or Grok Build without their shipped `api` rule) | set the rule the line names: `subscription` on a plan (and the plan under `subscriptions`), `api` on a key (step 4) |
| `!!  <name>: N row(s) in the last 24 h — …, U of unknown billing` | rows no rule could place: a Codex turn whose rollout names no plan, a plugin's row | set `providers.<name>.billing`, or a plugin that knows how they were paid |
| `!!  <name>: used on a plan but no subscription covers <name>` | a rule or the files put rows on a plan that no declared plan pays for (its `covers`, or the provider the registry names for it): the plan's fee is missing from real | add the plan under `subscriptions`; when only the rule put them there and you pay per token, set the rule to `api` (rows the files place on a plan need the plan) |
| `!!  plugin X: cannot import …` | a configured plugin is not importable | install it, or remove it from `plugins` / `AI_COST_PLUGINS` |
| `!!  subscription X ×1 — unknown plan: …` | a plan name not in the registry | a name from `ai-cost prices show`, or add the plan to your `prices.json` |
| `providers.X.billing = subscription but no subscription covers X` in a report header | plan rows would cost nothing | add the plan with `covers: ["X"]`, or set the rule to `api` |
| `!!  prices checked_at … days ago` | the drift check has not run for a while | `ai-cost prices check`; `install --schedule 3` |
| `!!  X/model price valid until … — expired: NO next block` | a dated price ran out | `prices update`, or edit your `prices.json` |
| `!!  daily reports: … is not a daily index` | a torn `index.json` | re-run `ai-cost daily --date <that day>` |
| `!!  last reconcile: … not a reconciliation` | a torn last line in `reconcile.jsonl` | delete that line, or re-run `reconcile` |
| `!!  state dir … is not writable` | the state directory can be neither written nor created (`ok … (created on the first write)` when it does not exist yet) | `AI_COST_STATE_DIR`, or fix permissions |
| `unpriced rows skipped: …` in a report header | a model the registry lacks | add it to your `prices.json` (§3) |
| `N usage row(s) from clients outside scope (…) have unknown billing and stay out of the real group on purpose` in a report header, `(N from clients outside scope, left unknown on purpose)` in a `doctor` line | rows of a client listed in `outside_scope_clients` | informational (a provider with unknown rows in scope too still gets a `!!` naming `the N in scope`) |
| `--  clients outside scope: <name> (N row(s) in the last 24 h, U of unknown billing), …` | each client of `outside_scope_clients` and the rows it matched | informational — a name with 0 rows over time is likely a typo (the client is spelled as the rollout's `originator`) |
| `N usage row(s) with unknown billing are left out of the real group (<name> N, …)` in a report header | rows without a billing rule, counted per provider | set the `providers.<name>.billing` the line names |
