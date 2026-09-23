# ai-cost — setup and operation (for the person AND their AI agent)

This is the deterministic path from an empty machine to daily, per-project cost reports. If you are an AI agent
reading this: run the numbered steps in order, run every **Verify** command, and stop at the first one whose output
does not match — say what you saw instead of improvising. Everything here is idempotent (safe to re-run). Nothing
here needs our setup: `ai-cost` reads only files on the machine it runs on and sends nothing anywhere; the only
network use is the optional price-drift check of the vendors' public pricing pages and `--github` through `gh`.

Contents: [1. The first 10 minutes](#1-the-first-10-minutes) · [2. What is read, from where](#2-what-is-read-from-where) ·
[3. Configuration reference](#3-configuration-reference) · [4. Count your own apps and MCP servers](#4-count-your-own-apps-and-mcp-servers) ·
[5. Per project, per day](#5-per-project-per-day) · [6. Reconcile with the provider](#6-reconcile-with-the-provider) ·
[7. A plugin for a source of your own](#7-a-plugin-for-a-source-of-your-own) · [8. Operate it from an agent](#8-operate-it-from-an-agent) ·
[9. Exit codes and what to do](#9-exit-codes-and-what-to-do) · [10. Troubleshooting by doctor line](#10-troubleshooting-by-doctor-line)

## 1. The first 10 minutes

Requirements: Python ≥ 3.9 (`python3 --version`), a POSIX shell. `gh` only for `--github`; `launchctl` (macOS) or
`crontab` for the scheduled jobs.

| step | command | Verify |
|---|---|---|
| 1. Get the tool | one executable file, no dependencies: download `ai-cost` from the release page of the repository you got this from, `chmod +x ai-cost`, put it on `PATH` (for example `~/.local/bin/ai-cost`). Once the package is on PyPI: `pipx install ai-cost` (or `pip install ai-cost`). From a checkout: `python3 scripts/build.py src dist/ai-cost` writes `dist/ai-cost` | `ai-cost --version` prints `ai-cost 2.2.0` (or newer) |
| 2. Prove it works offline | `ai-cost selftest` — the shipped tests run from the file itself against synthetic data in a temp directory; no network, no secrets | last line `ai-cost selftest: N passed, 0 failed` (N ≥ 274) |
| 3. Write your config | `ai-cost install --init-config` → `~/.config/ai-cost/config.json` (`AI_COST_CONFIG_DIR` moves it). The file ships with **no subscriptions on purpose**: an example plan would be counted as money you paid. Add yours (step 4) | `written /…/config.json — edit your plans, seats and budgets` |
| 4. Tell it what you pay for | edit `subscriptions` — one object per plan you pay: `{"plan": "<name from ai-cost prices show>", "seats": 1, "covers": ["anthropic"], "attribution": "time"}`. `covers` names the provider(s) the plan pays for (`anthropic`, `openai`, `github-copilot`, `github-actions`, …); `attribution` is `time` (window hours ÷ 730 of the monthly fee), `full` (the whole fee) or `none`. Then set `providers.<name>.billing` to `subscription` for a provider your plan pays (`anthropic` for a Claude plan, `openai` when Codex runs on a ChatGPT plan), `api` for one you pay per token, `mixed` when the files must decide (Codex rollouts say which turns ran on a plan) | `ai-cost doctor` prints `ok  subscription <plan> ×<seats>` per plan and no `!!` line |
| 5. See what is on disk | `ai-cost doctor` — every source found with its count, plugins, config, prices, both schedules | `doctor: all good` (a `--` line is informational: a source you do not use, a job not installed) |
| 6. First report | `ai-cost report --hours 24 --all-projects --group real,api --format table --unpriced skip` | a `# AI cost report — … (24.0 h)` header, `Sources: …` naming what was found, sections 1 (real) and 2 (API-only) |
| 7. Per project | `cd` into a project and run `ai-cost report --hours 24`, or `ai-cost report --project /path/to/project --hours 24` | the header lists `--project …: N row(s) … left out` / `… included` lines: what could and could not be placed (§5) |
| 8. Every morning, per project | `ai-cost install --schedule-reports` (06:40 local; `--at 07:15` to move it) — then `ai-cost daily` once by hand to see the files | `doctor` prints `ok  scheduled daily reports: installed`; `ai-cost daily` prints `daily <date>: real … USD, api-only … USD, N row(s), M project(s) → <reports>/<date>` |
| 9. Keep prices current | `ai-cost install --schedule 3` (a drift check every 3 days) — or `ai-cost prices check` when you wonder | `doctor` prints `ok  scheduled price check: installed`; `prices check` exits 0 (4 = a page changed: verify, then `prices update`) |
| 10. Check it against the invoice | when a provider's console shows a figure for a window: `ai-cost reconcile --provider xai --usd 156.11 --hours 24` | `reconcile: within tolerance` (exit 0) — or the gap and exit 1 (§6) |

Where things live (override in the environment when your CLIs do):

| what | default | override |
|---|---|---|
| config, price overrides | `~/.config/ai-cost/config.json`, `prices.json` | `AI_COST_CONFIG_DIR`, `AI_COST_CONFIG`, `AI_COST_PRICES` |
| state (price-check result, monitor history, reconcile history, job logs) | `~/.local/state/ai-cost/` | `AI_COST_STATE_DIR` |
| usage log your programs write | `$XDG_DATA_HOME/ai-cost/usage.jsonl` (`~/.local/share/ai-cost/usage.jsonl`) | `AI_COST_USAGE_LOG`, more files in config `usage_logs` |
| daily reports | `$XDG_DATA_HOME/ai-cost/reports/<date>/` | `AI_COST_REPORTS_DIR`, `daily --out DIR` |
| Claude Code, Codex CLI, Gemini CLI, Grok Build CLI homes | `~/.claude`, `~/.codex`, `~/.gemini`, `~/.grok` | `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME`, `GROK_HOME` (the CLIs' own variables) |
| network | the price-drift check and `--github` only | `AI_COST_OFFLINE=1` disables the check |

## 2. What is read, from where

| source | files | one row per | billing evidence in the file | the row's working directory |
|---|---|---|---|---|
| Claude Code | `~/.claude/projects/<project>/<session>.jsonl` + `<session>/**/*.jsonl` (subagents) | assistant message (streamed repeats of one `message.id` counted once) | none — `providers.anthropic.billing` decides (`subscription` for a Claude plan) | the project directory of the transcript (its entries' `cwd`) |
| Codex CLI | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | (model, UTC day) of a session, summed per turn inside the window | a turn that names a ChatGPT plan (`rate_limits.plan_type`) is a plan turn; the rest follow `providers.openai.billing` | `session_meta.payload.cwd` |
| Gemini CLI | `~/.gemini/tmp/<project>/chats/session-*.jsonl` (`.json` in older versions) | model message (`type: "gemini"`, one per `id`) | none — `providers.google.billing` | the folder, mapped to its path through `~/.gemini/projects.json` (by name, or by the sha256 older versions used) |
| Grok Build CLI | `~/.grok/sessions/<cwd, URL-encoded>/<session>/usage.json` | turn × model (`turns[].modelUsage`) | none — `providers.xai.billing`; the CLI's own cost (`costUsdTicks / 1e10`) is used by `real` under `providers.xai.trust_cli_cost` (default true) | the session directory's name, decoded |
| usage log | `$XDG_DATA_HOME/ai-cost/usage.jsonl` + config `usage_logs` | one JSON line = one request (§4) | the line's `billing` (`api` by default when it has tokens or a cost) | none; `session` / `ref` can name a CLI session, and the row then takes that session's scope |
| GitHub (opt-in) | `gh` API: Copilot reviews submitted in the window, Actions billable minutes | review / run | a plan that covers `github-copilot` / `github-actions`, or the `copilot_plan_exhausted` / `actions_plan_exhausted` switches | none |
| plugins | whatever they read (§7) | as they say | as they say | as they say |

A row's billing comes first from its file (a named plan, a log line's `billing`), then from `providers.<name>.billing`,
and stays `unknown` otherwise: an unknown row is priced in the API-only group and left out of `real` whatever
figure it carries (a figure without a rule is an estimate, not a charge); the report header counts them. Everything
a file cannot say (a torn line, a counter that is not a number, a turn without a timestamp) is a **counted skip**,
listed by `doctor` and `report --format json` (`skipped[]`), never a silent zero.

## 3. Configuration reference

`~/.config/ai-cost/config.json` is merged over the shipped defaults (`ai-cost prices show --format json --snapshot`
prints the shipped prices; the shipped config is the file `install --init-config` writes). A `null` anywhere means
"no override"; a list replaces the shipped list whole.

| key | type | default | meaning |
|---|---|---|---|
| `subscriptions[]` | `[{plan, seats, covers[], attribution}]` | `[]` | the plans you pay. `plan` is a name from `ai-cost prices show` (`claude-max-20x`, `chatgpt-plus`, `copilot-pro`, `github-team`, …); `seats` multiplies per-seat plans only; `covers` the providers it pays for; `attribution` `time` \| `full` \| `none` |
| `providers.<name>.billing` | `api` \| `subscription` \| `mixed` | `anthropic` mixed, `openai` mixed, `google` api, `xai` api, `deepseek` api | the rule for rows of that provider without billing evidence of their own; `mixed` = no single rule (the files decide, else unknown). Any provider id the pricebook lists (or one you add to `prices.json`) may have a rule |
| `providers.anthropic.cache_ttl_default` | `5m` \| `1h` | `1h` | the tier of a legacy unsplit cache write |
| `providers.openai.default_model` | string | `""` | the model of a Codex rollout that does not name its own (empty = such rows are `unknown`) |
| `providers.xai.trust_cli_cost` | bool | `true` | `real` uses the Grok CLI's own cost figure for its sessions and review runs; `false` = the list price |
| `providers.github.copilot_plan_exhausted`, `actions_plan_exhausted`, `actions_runner` | bool, bool, `linux` \| `windows` \| `macos` | false, false, linux | once the month's included credits / minutes are gone, `real` prices GitHub rows per unit; the runner OS when a run's timing is unknown |
| `budgets.daily_usd`, `monthly_usd`, `per_provider_daily_usd.<name>` | numbers | 150, 2500, `{google: 60, xai: 30}` | what `monitor` checks (exit 3 on breach) — API-equivalent figures |
| `window_default_hours` | number > 0 | 24 | the window when nothing else sets it |
| `reconcile.tolerance_pct` | number ≥ 0 | 5 | the gap `reconcile` accepts, in percent of the reported figure |
| `sizing.packet_bytes`, `sizing.loc` | `{XS, S, M, L}` thresholds | shipped | automatic work-item sizing for a plugin that supplies items |
| `vendor.default_profile`, `vendor.hours_per_day`, `vendor.profiles.<name>` | see the shipped file | `consultancy-eu`, 6 | the vendor quote (`--group vendor`): bands, staffing rates, time factors, senior review, package rounding |
| `plugins[]` | module names | `[]` | plugin modules loaded besides the `ai_cost.plugins` entry points (§7); `AI_COST_PLUGINS=a,b` adds more |
| `plugin_settings.<plugin>.<key>` | anything | `{}` | a plugin's own settings; `--setting <plugin>.<key>=<value>` overrides one for a run |
| `usage_logs[]` | file paths | `[]` | more usage-log files to read (the default one is always read) |

`~/.config/ai-cost/prices.json` overrides prices in the shape of the shipped registry: numbers only, never a price's
shape; a model you use that the registry lacks goes here (`{"providers": {"openai": {"models": {"my-model": {"input": 1.0, "output": 4.0}}}}}`),
or the row carries a `cost` and needs no price. `auto_check_days: 0` in it turns the drift check off.

## 4. Count your own apps and MCP servers

Anything that calls an LLM API outside the CLIs — a script, a web app, an MCP server, a CI job — appends **one
JSON line per request** to the usage log; the next `report` prices it exactly like the CLI rows. Log only what no
CLI transcript already records (a call an app logs AND a CLI transcript records would count twice).

The line (`schema` 1): required `schema`, `at` (ISO-8601 with zone, when the request finished), `provider`
(pricebook id: `anthropic`, `openai`, `google`, `xai`, `deepseek`, or one you added), `model` (as the API reports
it), and `tokens` or `cost`. Counters use the provider's own names — only the ones its formula reads, or the line is
a counted skip:

| provider | counters |
|---|---|
| anthropic | `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h` (or `cache_write_unsplit`), `web_search` |
| openai, xai, any OpenAI-compatible API | `input` (includes the cached part), `cached_input`, `output` |
| google | `prompt` (includes `cached`), `cached`, `output`, `thoughts` |
| deepseek | `cache_hit`, `cache_miss`, `output` |
| anything else | `input`, `output` |

Optional: `cost` (USD the request was charged — the row's cash under an `api` rule; also the price of a model the
registry lacks), `billing` (`api` \| `subscription`), `event_id` (a repeated id in one file is read once — safe to
replay an import), `source` (your program's name, shown in the report header), `ref`, `session` (name a CLI session
id and the row takes that session's scope: its project and branch), `branch`, `pr`, `tags[]` (attribution keys).

**From a shell, straight from the API's response** (the shapes of Anthropic, Google, OpenAI Chat Completions and
OpenAI Responses are recognised; OpenAI-shaped responses need `--provider`):

```bash
curl -s https://api.openai.com/v1/chat/completions … > resp.json
ai-cost log --from-response resp.json --provider openai --ref job-42 --source my-script
```

**From Python, in process** (the line is validated the way the reader reads it; a `ValueError` names the problem):

```python
from ai_cost.log import record          # pip/pipx install; with the single file: sys.path.insert(0, "/path/to/ai-cost")

resp = client.chat.completions.create(model="gpt-5.5", messages=msgs)   # any OpenAI-compatible client
u = resp.usage
record("openai", resp.model,
       {"input": u.prompt_tokens, "cached_input": u.prompt_tokens_details.cached_tokens, "output": u.completion_tokens},
       ref="job-42", source="my-app", event_id=resp.id)
```

**From Node (an MCP server, an Express app) — write the line yourself**, one `appendFile` per request; the format
is the contract, any language can write it:

```js
import { appendFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

const LOG = process.env.AI_COST_USAGE_LOG
  ?? join(process.env.XDG_DATA_HOME ?? join(homedir(), ".local", "share"), "ai-cost", "usage.jsonl");

export async function logUsage(provider, model, tokens, extra = {}) {   // tokens: the provider's own counter names (table above)
  const line = { schema: 1, at: new Date().toISOString(), provider, model, tokens, ...extra };
  await appendFile(LOG, JSON.stringify(line) + "\n", { encoding: "utf8" });   // mkdir -p the directory once at startup
}

// after an Anthropic call:
await logUsage("anthropic", msg.model, {
  input: msg.usage.input_tokens, output: msg.usage.output_tokens,
  cache_read: msg.usage.cache_read_input_tokens ?? 0, cache_write_1h: msg.usage.cache_creation?.ephemeral_1h_input_tokens ?? 0,
}, { source: "my-mcp-server", event_id: msg.id });
```

A line ≤ 4 KB written with one `write` is atomic on POSIX; the reader tolerates a torn last line and counts it. A
log in another place: `--log FILE` for `ai-cost log`, and list the file under `usage_logs` in the config so reports
read it (the command says so when you forget).

## 5. Per project, per day

Every CLI row carries the working directory its CLI ran in (§2, last column). `ai-cost report --project DIR` — and
a plain `report` run from inside a project — keeps the project's Claude transcripts and every row whose directory is
`DIR` or below it (symlinks resolved), **leaves out** rows of other directories and rows of named workspaces that are
no directory (a plugin's review workspace, a Gemini folder the map does not know — `--attribute` places those),
**includes** rows that name nothing (a usage-log line without `session`, a GitHub row), and counts each group per
source in the header, so the number says what it could not place. A git worktree is its own project. `--all-projects`
and `--session <id>` never filter.

`ai-cost daily` prices the UTC day before today (`--date YYYY-MM-DD` for another) once over every project and once per
Claude project whose transcripts were written that day, and writes under `<reports>/<date>/`:

| file | content |
|---|---|
| `global.md`, `global.json` | the whole day, every project (`--all-projects`) |
| `<project dir name>.md`, `.json` | the `--project` scope of that project's directory (read from its transcripts' `cwd`); a project with no usage that day gets no file, a note instead |
| `index.json` | `{day, generated_at, window, directory, real_usd, api_usd, cash_usd, rows, projects: [{project_dir, path, markdown, json_file, real_usd, api_usd, rows}], notes: [...]}` |

Unpriced rows are skipped and counted (a new model never fails the job); a project that cannot be placed or priced is
a note. `ai-cost install --schedule-reports` registers the job at 06:40 local time (`--at HH:MM`; launchd on macOS runs
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
paste (or a script's, from an export). A gap is something to look at — the window's edges (the console's day may
not be UTC), a source the tool does not read, a price it applies differently (the API-only group prices a turn's
aggregated counters, so a turn of several calls never gets a long-context tier) — never a number to hide.

## 7. A plugin for a source of your own

A plugin is a Python module that exports `PLUGIN = ai_cost.plugins.Plugin(...)`; it adds sources (rows for a
window), enrichers (a pass over every row), doctor lines and tests. The protocol is `src/ai_cost/plugins.py`.

```python
# my_llm_costs/__init__.py
from __future__ import annotations
import json
from pathlib import Path
from ai_cost.models import Billing, Collected, Provider, RowKind, Scope, Skipped, Tokens, UsageRow
from ai_cost.plugins import Context, Plugin
from ai_cost.timeutil import parse_ts

class ProxyLog:
    """Rows from an LLM proxy's JSONL log: {ts, model, prompt_tokens, completion_tokens, cwd}."""
    name = "proxy-log"                                       # on every row; shown in the report header

    def collect(self, ctx: Context) -> Collected:
        path = Path(ctx.settings.get("path", "~/proxy.jsonl")).expanduser()   # plugin_settings.my_llm_costs.path
        rows, skipped = [], []
        for n, line in enumerate(path.read_text().splitlines(), 1):
            try:
                r = json.loads(line)
                at = parse_ts(r["ts"])
                if not ctx.window.contains(at):
                    continue
                rows.append(UsageRow(
                    provider=Provider.OPENAI, model=str(r["model"]), kind=RowKind.LOG, source=self.name, at=at,
                    ref=str(r.get("id", n)), billing=Billing.API,
                    tokens=Tokens(input=int(r["prompt_tokens"]), output=int(r["completion_tokens"])),
                    scope=Scope(workspace=str(r.get("cwd", ""))),      # a directory → --project can place the row
                ))
            except (KeyError, ValueError, TypeError) as exc:
                skipped.append(Skipped(self.name, str(path), f"line {n}: {exc}"))   # counted, never silent
        return Collected(rows=rows, skipped=skipped)

PLUGIN = Plugin(name="my_llm_costs", sources=(ProxyLog(),))
```

Register it in one of three ways: an entry point in your package (`[project.entry-points."ai_cost.plugins"]
my_llm_costs = "my_llm_costs"`), `"plugins": ["my_llm_costs"]` in the config, or `AI_COST_PLUGINS=my_llm_costs` in
the environment (the module must be importable: on `PYTHONPATH`, or installed next to `ai-cost`). `doctor` lists
every loaded plugin and says why one could not be loaded; a plugin that raises costs its own rows, never the report.
A `Plugin(tests_package="my_llm_costs.tests")` gets its tests run by `ai-cost selftest`.

## 8. Operate it from an agent

| the person asks | run | read |
|---|---|---|
| what did this session cost | `ai-cost report --session latest` (or the id / a transcript path) | sections 1 and 2; say the window, the sources found, `prices checked_at` |
| what did the last N hours / this week cost | `ai-cost report --hours N --all-projects` or `--since … --until …` | as above |
| what did project X cost | `ai-cost report --project /path/to/X --since … --until …` | the header's `--project …` lines say what was left out and what is included without a directory |
| what did yesterday cost, per project | `ai-cost daily` then the files under `<reports>/<date>/`; `index.json` for the numbers | `projects[].real_usd`, `notes[]` |
| what did feature / PR X cost | `ai-cost report --since … --until … --all-projects --attribute "X=<branch\|PR number\|paths regex>"` | section 4: `mixed` and `unattributed` are always there |
| my app calls the API, count it | §4: one usage-log line per request, then any `report` | the header names the source |
| is it right | `ai-cost reconcile --provider P --usd <console figure> --hours 24` | exit 0/1 and the gap line |
| something is off | `ai-cost doctor` | the `!!` lines (§10) |
| are prices current | `ai-cost prices check` | exit 4 = a page changed; verify, then `prices update` |
| for machines | add `--format json` (`--detail` for every row) to any `report` | the JSON contract in the README |

Prefer `--format md` for people and `--format json --detail` when you need rows. `AI_COST_OFFLINE=1` skips the
background price check. Never round away small numbers; quote both `real` and `api`, and say which subscriptions were
prorated. For Codex and Gemini agents the same file works: `SKILL.md` and `standalone/SKILL.md` are open-standard
skill files readable by any agent.

## 9. Exit codes and what to do

| code | meaning | what to do |
|---|---|---|
| 0 | done | — |
| 1 | a failure to act on (`doctor` found problems; `reconcile` above tolerance; a file the tool cannot write) | read the message; `doctor` for the state |
| 2 | bad arguments or a config that does not parse (the message names the key) | fix the flag or the config |
| 3 | `monitor`: a budget breached | the printed `BUDGET BREACH` lines |
| 4 | `prices check`: a vendor page shows a different price | verify on the page, then `prices update` or edit your `prices.json` |
| 5 | `report`: a model with no price and no reported cost | add it to your `prices.json`, or `--unpriced skip` to count and skip such rows |

## 10. Troubleshooting by doctor line

| line | meaning | fix |
|---|---|---|
| `!!  Claude transcripts: 0 files` | nothing under `~/.claude/projects` | `CLAUDE_CONFIG_DIR` if Claude Code keeps its files elsewhere |
| `--  Gemini CLI sessions: 0` / `--  Grok Build sessions: 0` | you do not use that CLI, or its home differs | informational; `GEMINI_CLI_HOME` / `GROK_HOME` |
| `!!  plugin X: cannot import …` | a configured plugin is not importable | install it, or remove it from `plugins` / `AI_COST_PLUGINS` |
| `!!  subscription X ×1 — unknown plan` | a plan name not in the registry | a name from `ai-cost prices show`, or add the plan to your `prices.json` |
| `!!  providers.X.billing = subscription but no subscription covers X` | plan rows would cost nothing | add the plan with `covers: ["X"]`, or set the rule to `api` |
| `!!  prices checked_at … days ago` | the drift check has not run for a while | `ai-cost prices check`; `install --schedule 3` |
| `!!  X/model price valid until … — expired: NO next block` | a dated price ran out | `prices update`, or edit your `prices.json` |
| `!!  daily reports: … is not a daily index` | a torn `index.json` | re-run `ai-cost daily --date <that day>` |
| `!!  last reconcile: … not a reconciliation` | a torn last line in `reconcile.jsonl` | delete that line, or re-run `reconcile` |
| `!!  state dir … writable` | the state directory cannot be written | `AI_COST_STATE_DIR`, or fix permissions |
| `unpriced rows skipped: …` in a report header | a model the registry lacks | add it to your `prices.json` (§3) |
| `N usage row(s) with unknown billing are left out of the real group` | rows without a billing rule | set `providers.<name>.billing` |
