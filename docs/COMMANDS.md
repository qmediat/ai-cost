# Commands

| command | does |
|---|---|
| `report` (default) | the three groups for a window ([ANSWERS.md](ANSWERS.md)). `--session <id\|latest\|all\|path>` `--project DIR` (every source scoped to that directory — [SETUP.md §5](SETUP.md#5-per-project-per-day)) `--all-projects` · `--unpriced fail\|skip` · `--since/--until` `--hours N` (N > 0; default the config's `window_default_hours`, 24) · `--github owner/repo` · `--group real,api,vendor` · `--items scope.md` `--vendor-profile NAME` · `--setting PLUGIN.KEY=VALUE` · `--format md\|json\|table` `--out FILE` `--detail` |
| `log` | append one usage line for a request your program made: `--provider openai --model gpt-5.5 --input 1200 --output 300 [--cost 0.0123] [--ref job-42] [--tag ci] [--origin-session S --origin-repo owner/name --origin-pr N --run-id R]`, or `--from-response resp.json [--provider …]` for a raw Anthropic / Google / OpenAI response; `--log FILE` picks the file — a model the pricebook does not list needs `--cost` (or an entry in your prices file): the line is refused rather than written as a row every report would fail on ([USAGE-LOG.md](USAGE-LOG.md)) |
| `prices show` | merged registry (shipped defaults → your overrides) |
| `prices check` | re-read every vendor page, report `confirmed` / `changed?` / `not-found` / `fetch-failed`; exit 4 on drift. A model is read in its own row (its name as a whole word, up to the next model name). Where the vendor lists its cache and long-context rates in that row (Anthropic, Google, OpenAI), they are part of what confirms the price and a `!!` line names what the row lacks; elsewhere a rate the row does not show is named on a `~` line (`listed apart from its row: cached_input 0.015`), without failing the check. `--quiet` prints only what changed, vanished or is new (`doctor` names the `~` rates of the last check). A model the vendor's page names that the price book neither prices nor lists under the provider's `no_row` is a `new <id>` line — the page is read with its markdown twin (`<url>.md`) where the vendor serves one |
| `prices update` | check, then write unambiguous changes to **your** `~/.config/ai-cost/prices.json`: a changed input/output pair read from a row of exactly those two amounts, where the check expected a pair (Alibaba, xAI); a vendor whose row holds cache rates (Anthropic, Google, OpenAI) is always a hand edit |
| `doctor` | diagnostics: sources found (Claude, Codex, Gemini CLI, Grok Build, the usage log), your plans and how each provider's rows of the last 24 h were billed (a `!!` line names the key to set and the value that fits), plugins, config, prices, both schedules, the newest daily index, the last reconciliation; exit 1 on problems ([SETUP.md §10](SETUP.md#10-troubleshooting-by-doctor-line)) |
| `daily` | write one UTC day's reports: `global.{md,json}` over every project and `<project dir>.{md,json}` per Claude project touched that day, plus `index.json` — `--date YYYY-MM-DD` (default yesterday), `--out DIR` (default `AI_COST_REPORTS_DIR` or `$XDG_DATA_HOME/ai-cost/reports`), `--quiet`; the job `install --schedule-reports` runs |
| `import deepseek` | `<export ZIP>` (or the two CSV files with `--captured WHEN`): check DeepSeek's usage export and keep its days as the provider's day report — [SETUP.md §6](SETUP.md#6-reconcile-with-the-provider) |
| `reconcile` | `--provider xai --usd 156.11 [--tokens 80200000] --hours 24` (or `--since/--until`): the local count of one provider against the figure its console shows, gap vs `reconcile.tolerance_pct` (5); exit 1 above it; history in `~/.local/state/ai-cost/reconcile.jsonl` |
| `monitor` | rolling-window totals (`--hours N`, default the config's `window_default_hours`) → `~/.local/state/ai-cost/history.jsonl` with `--append`; budget check from config, exit 3 on breach; `--history N` |
| `install` | `--init-config` (writes the config, prints what to put in it), `--schedule DAYS`, `--unschedule`, `--schedule-reports [--at HH:MM]` (the daily job, 06:40 local by default), `--unschedule-reports` |
| `selftest` | offline, no network, no secrets; also runs the tests of every loaded plugin that ships some |

## The window

The window comes from `--since/--until`, else from the session's first and last timestamp, else the last `--hours`
(the config's `window_default_hours`, 24 by default).
Every source is filtered to that window, so a report is reproducible. Without `--session/--project`, the cwd's
project transcripts are read (every project's when the cwd has none).

## One session

`--session <id|prefix|path>` (ADR-0009): the report keeps only the rows stamped with that session —
its transcript (subagents included) and every run it launched that carries its id (a usage-log line's
`origin_session`, a plugin's stamp), from every project. Collection runs to a day after the last turn, so a late
run is in; the header counts the rows of other sessions and the rows without a stamp in the session's span, and the
unstamped ones are listed under the report, never summed. No subscription fee is allocated to a session: "real" is
its cash; a vendor quote needs `--items`. `--project`, `--all-projects` and `--github` are refused with it; `latest`
and `all` stay period reports. Codex usage is summed per turn inside the
window, so a session resumed from before it or still running after it contributes only the turns in between.

## Exit codes and what to do

| code | meaning | what to do |
|---|---|---|
| 0 | done | — |
| 1 | a failure to act on (`doctor` found problems; `reconcile` above tolerance; a file the tool cannot write) | read the message; `doctor` for the state |
| 2 | bad arguments or a config that does not parse (the message names the key) | fix the flag or the config |
| 3 | `monitor`: a budget breached | the printed `BUDGET BREACH` lines |
| 4 | `prices check`: a vendor page shows a different price | verify on the page, then `prices update` or edit your `prices.json` |
| 5 | `report`: a model with no price and no reported cost | add it to your `prices.json`, or `--unpriced skip` to count and skip such rows |
