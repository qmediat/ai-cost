# Commands

Every command below reads the machine's own files. `--instance DIR` (or `AI_COST_INSTANCE`), before the subcommand,
points `report`, `log`, `price`, `doctor`, `monitor`, `prices show` and `import usage` at one app installation instead — its usage
segments, config, prices and state only ([EMBED.md](EMBED.md)). With it: `--read-clis` adds the machine's
coding-CLI files, `--outside-log PATH` allows a usage log outside the directory, `--plugin MODULE` runs a plugin
(both repeatable); `report` defaults to `--group real,api --unpriced skip` over every project, as the library does;
`--github`, `--session` without `--read-clis` (a session is a coding-CLI transcript) and a time without a zone or a
blank one (`--at`, `--since`, `--until`) are refused. The other commands refuse an
instance with one line (exit 2). `--instance .` (and `AI_COST_INSTANCE=.`, `instance init .`) is the working
directory and `~` is your home; an empty or blank `--instance`, `instance init ''` or `--outside-log ''` is refused
(exit 2), never the machine's own log; an empty `AI_COST_INSTANCE` counts as unset. A global option never takes a
subcommand's name or another option as its value: a directory named like a subcommand is given as `./log`. An
empty `--group` or `--github` is refused (exit 2), never read as the default.

| command | does |
|---|---|
| `report` (default) | the three groups for a window ([ANSWERS.md](ANSWERS.md)). `--session <id\|latest\|all\|path>` `--project DIR` (every source scoped to that directory — [SETUP.md §5](SETUP.md#5-per-project-per-day)) `--all-projects` · `--unpriced fail\|skip` · `--since/--until` `--hours N` (N > 0; default the config's `window_default_hours`, 24) · `--github owner/repo` · `--group real,api,vendor` · `--items scope.md` `--vendor-profile NAME` · `--setting PLUGIN.KEY=VALUE` · `--where LABEL=VALUE` (selects usage rows whose label is exactly that — the real group without a plan share; work items and estimates of a source, the GitHub usage report and the provider reports are left out of a filtered report; repeatable, one value per label) `--by LABEL` (the attribution group: one line per value of the label, then `(no LABEL)`; a value spelled like that is shown in quotes) · `--format md\|json\|table` `--out FILE` `--detail` |
| `log` | append one usage line for a request your program made: `--provider openai --model gpt-5.5 --input 1200 --output 300 [--cost 0.0123] [--ref job-42] [--tag ci] [--origin-session S --origin-repo owner/name --origin-pr N --run-id R] [--label NAME=VALUE]`, or `--from-response resp.json [--provider …]` for a raw Anthropic / Google / OpenAI response; `--log FILE` picks the file — a model the pricebook does not list needs `--cost` (or an entry in your prices file): the line is refused rather than written as a row every report would fail on ([USAGE-LOG.md](USAGE-LOG.md)) |
| `price` | the list price of one request: `--provider openai --model gpt-6-sol --input 1200 --output 300 [--cost X] [--at WHEN] [--format json]` → `{"usd", "note"}` (the note names the tier or the tariff window; with `--cost`, that cost, "reported by the caller"); exit 5 when nothing prices it |
| `instance init DIR` | create an app installation's directory: `usage/`, `state/`, a starting `config.json` (plans and budgets, empty); what exists is kept, so twice changes nothing ([EMBED.md](EMBED.md)) |
| `prices show` | merged registry (shipped defaults → your overrides) |
| `prices check` | re-read every vendor page, report `confirmed` / `changed?` / `not-found` / `fetch-failed`; exit 4 on drift. A model is read in its own row (its name as a whole word, up to the next model name). Where the vendor lists its cache and long-context rates in that row (Anthropic, Google, OpenAI), they are part of what confirms the price and a `!!` line names what the row lacks; elsewhere a rate the row does not show is named on a `~` line (`listed apart from its row: cached_input 0.015`), without failing the check. `--quiet` prints only what changed, vanished or is new (`doctor` names the `~` rates of the last check). A model the vendor's page names that the price book neither prices nor lists under the provider's `no_row` is a `new <id>` line — the page is read with its markdown twin (`<url>.md`) where the vendor serves one |
| `prices update` | check, then write unambiguous changes to **your** `~/.config/ai-cost/prices.json`: a changed input/output pair read from a row of exactly those two amounts, where the check expected a pair (Alibaba, xAI); a vendor whose row holds cache rates (Anthropic, Google, OpenAI) is always a hand edit |
| `doctor` | diagnostics: sources found (Claude, Codex, Gemini CLI, Grok Build, the usage log), your plans and how each provider's rows of the last 24 h were billed (a `!!` line names the key to set and the value that fits), plugins, config, prices, both schedules, the newest daily index, the last reconciliation; exit 1 on problems ([SETUP.md §10](SETUP.md#10-troubleshooting-by-doctor-line)) |
| `daily` | write one UTC day's reports: `global.{md,json}` over every project and `<project dir>.{md,json}` per Claude project touched that day, plus `index.json` — `--date YYYY-MM-DD` (default yesterday), `--out DIR` (default `AI_COST_REPORTS_DIR` or `$XDG_DATA_HOME/ai-cost/reports`), `--quiet`; the job `install --schedule-reports` runs |
| `import deepseek` | `<export ZIP>` (or the two CSV files with `--captured WHEN`): check DeepSeek's usage export and keep its days as the provider's day report — [SETUP.md §6](SETUP.md#6-reconcile-with-the-provider) |
| `import usage` | `FILE` or `-` (stdin), only with `--instance`: a log stream's usage lines into the instance, each `event_id` once — [below](#import-usage) |
| `reconcile` | `--provider xai --usd 156.11 [--tokens 80200000] --hours 24` (or `--since/--until`): the local count of one provider against the figure its console shows, gap vs `reconcile.tolerance_pct` (5); exit 1 above it; history in `~/.local/state/ai-cost/reconcile.jsonl` |
| `monitor` | rolling-window totals (`--hours N`, default the config's `window_default_hours`) → `~/.local/state/ai-cost/history.jsonl` with `--append`; budget check from config, exit 3 on breach; `--history N` |
| `install` | `--init-config` (writes the config, prints what to put in it), `--schedule DAYS`, `--unschedule`, `--schedule-reports [--at HH:MM]` (the daily job, 06:40 local by default), `--unschedule-reports` |
| `collect` | `HOST:PATH … --into DIR [--timeout SECONDS]`: pull app instances' usage logs from other machines over ssh, one file each — [below](#collect) |
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

## collect

`ai-cost collect box:/var/lib/my-app/ai-cost user@db:app --into /srv/ai-cost-pulls` — each `HOST:PATH` is an app
instance on another machine (`HOST` as ssh takes it: `user@host` or a config alias; `PATH` its root there, relative
to the remote home, a leading `~/` meaning the home; a `..` component is refused, exit 2 — only the remote could
resolve it). One ssh call per argument (`ssh -o BatchMode=yes -o
ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3`, the connect seconds from `--timeout`;
`AI_COST_SSH` names another executable) runs `sh` on the host, which prints every `usage/YYYY-MM.jsonl` in name order,
each read once and ended by a newline. Links: a linked `PATH` is followed (it is what you named); a `usage` that is a
symbolic link fails the pull (`PATH/usage is a link: not pulled`), since it may lead to another instance's records;
so does a month entry that is a link or not a regular file (`PATH/usage/NAME is a link: not pulled` / `… is not a
regular file: not pulled`) — nothing is skipped silently, and the previous file is kept. Nothing but `sh` and `awk` (POSIX) is needed there — a host without `awk` is a
failed pull that names it — and the path
reaches it quoted. A remote instance without `usage/` is an empty file; a `PATH` that is no directory or cannot be
entered, or a `usage` that cannot be entered or listed, is a failed pull. `AI_COST_OFFLINE=1` refuses the command (exit 2).

| | |
|---|---|
| The file | `DIR/<host>-<path>-<16 hex>.jsonl`: the path normalised first, lexically — empty and `.` components dropped (`//`, `/./`, a trailing `/` or `/.`) and one leading `~` (so `app`, `~/app`, `./app/` and `app/.` are one file; `~/~/app` is the directory `~/app` of the home); host and path with every run of characters other than letters, digits, `.`, `_`, `-` as one `-`, cut to 40 and 80 characters, and a 64-bit digest (16 hex digits of the SHA-256) of host and normalised path, so two instances do not share a file. A new file gets mode 644 under the umask; a re-pull keeps the file's mode (`box:/var/lib/my-app/ai-cost` → `box-var-lib-my-app-ai-cost-e6efe3aeae981afa.jsonl`) |
| A pull | written to a temporary file in `DIR`, then moved over the old one: a reader never sees half a file; a re-pull replaces it whole |
| The file never shrinks | segments only grow, so a pull with fewer bytes than the file (an empty one included) is refused: `pulled N lines (B bytes) where FILE holds … — kept; delete FILE to accept a smaller pull`, a failed pull. An empty file, or none, takes any pull. After renaming a host, drop its old file from `usage_logs` |
| A failure | the line names ssh's exit status (or `local error`) and the first line it said, and whether a previous file was kept; the other hosts are still pulled; exit 1 and `N of M pulls failed`. A `--into` that cannot be created or written is exit 2 before any ssh |
| Use the files | the reporting machine's config: `"usage_logs": ["/srv/ai-cost-pulls/box-var-lib-my-app-ai-cost-e6efe3aeae981afa.jsonl"]`; on an instance: `ai-cost --instance DIR --outside-log FILE report` with the file under the instance's `usage_logs`. `collect` changes no config |
| Not done | retries, parallel pulls, compression, incremental sync; `--instance` is refused (it reads nothing of an instance). Only the connect and a dead link are bounded (`ConnectTimeout`, `ServerAlive*`): a remote that stays connected and stalls is not — wrap a scheduled run in the scheduler's own timeout |

## import usage

`ai-cost --instance DIR import usage FILE` (or `-` for stdin) — for an app that cannot write into the instance
directory itself (a container, a serverless function): it prints one usage line per model call
([USAGE-LOG.md](USAGE-LOG.md)) into its log stream, and a job on the operator's side feeds the stream here. The input
is read one line at a time, as UTF-8 (a byte that is not is replaced; a byte-order mark at the start is dropped).
Each month the stream touches costs one read of that month's segment, to learn its ids: memory grows with the
ids, the time with the segment's size.

A line an import takes is one `record` accepts: the documented keys only, labels in their own shape, a well-formed
origin. The reader still counts a line it takes more loosely (an extra key of your own); the import refuses it,
names the key and exits 1, so an instance's log holds only lines `record` would have written.

| A line of the stream | Is |
|---|---|
| blank | ignored |
| no JSON object with `schema` and one of `at`, `provider`, `model` (the stream's other lines, the app's own JSON log records — a `schema` version of their own included) | `not a usage line`: counted, not a failure, never shown |
| a log prefix before the object (`journalctl`, `kubectl logs --timestamps`, `docker logs -t`) | read from its first `{` |
| names `"schema"` but no object parses (a torn line) | `a usage line that cannot be read` |
| without `event_id` | `no event_id`: an import is safe to repeat only by the line's id |
| with an `event_id` its month's segment already holds, or an earlier line of this import | `already there`, not written — and not validated again, so a rule or a price changed since never fails a replay |
| one `log` / `Engine.record` would refuse, a time without a zone, a time relative to the import (`now`) | `invalid usage line` |
| 4096 bytes or more, as `record` would write it | `line too long` |
| otherwise | written as `record` writes a call: the segment of the line's own UTC month, its own `at`, its labels and keys |

The output is `imported N`, then `already there N` when not zero, then one line per class of skip with its first line
as the example:

```
imported 120
already there 30
skipped 3 — not a usage line
skipped 2 — invalid usage line: line 14: invalid call: unknown key(s) region; the keys are …
```

An app whose own JSON records carry `schema` together with `at`, `provider` or `model` filters them out of the stream
before the import: by the rule above such a record is a usage line, and without an `event_id` it fails the run.

A stream with lines but not one usage line starts with `imported 0 — no usage line found in N lines` (exit 0): a
writer whose lines never parse (single quotes, an unquoted key) is seen there, not by the exit.

Exit 0, or 1 when a line was skipped for any class but `not a usage line` (the good lines are imported all the same);
exit 2 without an instance, for an instance config or prices file that does not parse, or for a stream that cannot
be opened or read — a read that fails midway keeps what was written, and the next run finds it `already there`; a
segment that leads out of the instance stops the import, exit 1, as `log` refuses it.

**A replay carries the call's own time.** An `event_id` is held per month segment: the same id sent again with an `at`
in ANOTHER month is stored in that month too, and a report of one month counts the copy it holds (a report over both
counts the call once). A stream replays its lines unchanged, so this does not arise; an app must not re-stamp `at`.

**Import a stream or list its file, never both.** A file fed to `import usage` must not also be under the instance's
`usage_logs` (or `--outside-log`): the segments and a listed log are separate logs, an `event_id` is read once per
log, so every imported call would count twice. The same stream fed twice, or an import cut short and run again, adds nothing
that is already there. Each `event_id` is **counted** once: two imports at once, or a `record` during an import, may
store a line twice, and every report still counts it once (there is no lock). In Python:
`Engine.import_usage(lines)` → `UsageImport(imported, already_there, skipped, examples)`, both maps keyed by
`SkipClass` ([EMBED.md](EMBED.md)).

## Exit codes and what to do

| code | meaning | what to do |
|---|---|---|
| 0 | done | — |
| 1 | a failure to act on (`doctor` found problems; `reconcile` above tolerance; a file the tool cannot write; a `collect` pull that failed; `import usage`: a usage line skipped) | read the message; `doctor` for the state |
| 2 | bad arguments or a config that does not parse (the message names the key) | fix the flag or the config |
| 3 | `monitor`: a budget breached | the printed `BUDGET BREACH` lines |
| 4 | `prices check`: a vendor page shows a different price | verify on the page, then `prices update` or edit your `prices.json` |
| 5 | `report`: a model with no price and no reported cost; `price`: nothing prices the request | add it to your `prices.json`, or `--unpriced skip` to count and skip such rows |
