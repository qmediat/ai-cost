# Changelog

All notable changes to `ai-cost` are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.6.0] - 2026-09-26

### Changed

- **A Copilot code review has no price.** Since 2026-06-01 GitHub bills Copilot in AI credits (0.01 USD each) and does
  not disclose the review model: every review consumes its own number of credits. The constant 13 × 0.04 USD = 0.52
  USD a review (the old premium-request rule) is gone from the price list, the price check and the pricing; a
  Copilot review row is a count. The amounts come from the account's usage report (below).
- `providers.github.copilot_plan_exhausted` is no longer read; a config that still sets it gets a header warning and
  a `doctor` problem naming `providers.github.bill`.
- Actions minutes of `--github`: a run without `/timing` shows its elapsed time under `elapsed` and is never priced
  (it is not what GitHub bills); a runner the price list does not name is not priced at the Linux rate any more.

### Added

- **`providers.github.bill`** — `{"scope": "organization" | "user", "name": "<account>"}`: every report reads the
  account's usage report through `gh` (`…/settings/billing/usage`, one call per month the window touches, nothing
  kept on disk) and prices GitHub from it: Copilot credits (API-only = gross, real = net) and seats (a subscription
  share, replacing a configured plan whose seats it bills, `copilot-business` for "Copilot Business"), and the
  Actions of the repositories named with `--github`. Only the UTC days that lie whole inside the window and have
  ended enter the totals; the others are listed with their exact amounts, never prorated. With `bill` set, every
  other GitHub row of a month whose report was read (a count, a plugin's row, a logged charge) is settled by it, so
  nothing is booked twice; a month that could not be read settles nothing (a failed read never zeroes an amount). An
  enterprise is refused: its report leaves out the usage assigned to cost centers — name the organization.
- `--github` Actions minutes are one row per repository and UTC day of the runs' start (the usage report bills per
  day), no longer one row for the whole window.
- The report's **"GitHub usage report"** section and JSON `github_bill`: the report's own figures, exact (amounts as
  text in JSON) — the counted subtotals per product / SKU / unit, what is left out, the days outside the totals, the
  months not read and why.
- `doctor` reads the account's report once: readable with its newest day, or the reason (404: who can read it).
- A day read less than 12 h after it ended is provisional (GitHub updates Actions storage within 6 to 12 hours):
  `daily` records it — and a day whose report could not be read — in `index.json` (`github_provisional`), and every
  later run reads each such day of the last 7 again once the report can be read; a day already written keeps its
  files whenever its report cannot be read (a re-read or a hand-run `daily --date`). `monitor` lists the
  GitHub amounts its window leaves out in the new `outside` field of its history line.
- Scheduled jobs (launchd, cron) carry the PATH of the shell that installs them: launchd's own finds no Homebrew
  `gh`. With `providers.github.bill` set, `doctor` flags a daily job installed before 2.6 — run
  `ai-cost install --schedule-reports` again.
- Header warnings: Copilot reviews counted but not priced (no usage report), a configured GitHub plan booked beside
  the seats the report bills, and a `github.copilot` block left in the user's prices file.

## [2.5.0] - 2026-09-26

### Changed

- `ai-cost install --schedule …` / `--schedule-reports` refuse (exit 2) when this copy runs from npx's cache, which
  npm may prune: a job registered there would stop in silence. The rest of the same call (`--init-config`,
  `--unschedule…`) still runs; install globally (`npm install -g ai-costs`) and schedule from there.

### Added

- The npm package **`ai-costs`**: `npm install -g ai-costs` (or `npx ai-costs`) gives the same `ai-cost` command. It
  carries the same single-file program as the Python distribution and starts it with a Python 3.9+ found on the
  machine (`python3`, or `AI_COST_PYTHON`); installing it runs nothing and downloads nothing.
- Every Codex row carries its `client`: the program that wrote the rollout (`originator` — `codex_exec`, or
  `codex_work_desktop` for the Codex app, whose sessions may name no ChatGPT plan). It is in `--format json --detail`.
- Config `outside_scope_clients`: clients whose sessions are outside the work you track. A billing rule never
  applies to their rows (a plan their file names still does), so rows without evidence stay unknown and out of the
  real group; the API-only group still prices their tokens. The report header and `doctor` say them apart
  (`… from clients outside scope, left unknown on purpose`) and never ask to fix them — a provider with unknown
  rows in scope as well gets its `!!` for those alone. `doctor` lists each listed client with the rows it matched
  (a typo shows 0); an empty name is a config error. `reconcile` notes them apart from the rows a rule could place
  (the JSON history line gains `outside_scope`).

## [2.4.0] - 2026-09-25

### Added

- `ai-cost install --init-config` prints what to put in the new file — your plans, and the billing rule of every
  provider whose session files cannot say how you paid — with the link to the setup guide; on an existing file it
  points to `ai-cost doctor`.
- `ai-cost doctor` shows the setup: each declared plan, a CLI whose files never say how a session was paid and has no
  `providers.<name>.billing`, a provider used on a plan that no subscription covers, and one line per provider with
  its rows of the last 24 h by billing (on a plan, per token, unknown) and the rule applied. Every `!!` line names the
  key to set and the value that fits. The usage logs (the default one and those in `usage_logs`) are listed as
  sources, and the budgets `monitor` checks are shown.
- `ai-cost --help` ends with where to start and the link to the setup guide; every `install` option has a description.
- The JSON report's `real` group has `unknown_by_provider` (the rows of unknown billing a rule could place, per
  provider) and `unfigured_ledger` (ledger rows without a figure); the two sum to `unknown_billing`.

### Changed

- `doctor` no longer counts a CLI you do not use as a problem (a `--` line naming the variable that points at its
  home); a session directory that cannot be read, and finding no usage anywhere, are. A state directory that does
  not exist yet but can be created is `ok`; a file or a dangling link in its way is a problem.
- A plan declared without `covers` pays for the provider the price registry names for it: neither the report
  header nor `doctor` says any more that no subscription covers that provider. A plan with `covers` pays for what
  they name.

### Fixed

- A CLI's session directory that exists but cannot be listed (an unreadable `~/.codex`, `~/.claude`, …) crashed
  `report` and `doctor` with a `PermissionError` on Python 3.9–3.12 and was read as empty in silence on 3.13+; it is
  now a counted skip (`cannot list: …`) in the report and a `!!` line in `doctor`, on every Python; one project
  directory that cannot be listed is skipped on its own and the other projects are read.
- The report header no longer says GitHub plan rows cost nothing when `copilot_plan_exhausted` or
  `actions_plan_exhausted` already prices them.
- The report header's line about rows of unknown billing names each provider, its count and the values its rule can
  take.
- The shipped budgets are 0 (no check): `monitor` reports a breach only against budgets you set. A config file
  written by an earlier `install --init-config` keeps the budgets it copied then (150 / 2500 USD); edit or remove
  them there.
- `ai-cost install` without an action, or `--force` without `--init-config`, is a usage error (exit 2) that says what
  to give.
- The README's links point to the GitHub repository, so they work on PyPI too; help texts and the daily report's note
  on split subscription shares no longer cite internal decision records.

## [2.3.0] - 2026-09-24

### Added

- Provider `alibaba` (Alibaba Cloud Model Studio, international/Singapore list prices): `qwen3.8-max` (2 / 6 USD per
  1M, cache hits at 10 % of the input price), `qwen3.8-flash` (0.15 / 0.47), `qwen3.7-plus` and `qwen3.6-flash` with
  their 256K–1M long tier. A usage-log row with `provider: alibaba` and one of these model ids is priced like any
  other; `ai-cost prices check` verifies the amounts against the Model Studio pricing page.

## [2.2.3] - 2026-09-23

First public release on PyPI and GitHub.

### Changed

- The README opens with the Quantum Media Technologies wordmark and closes with a "Made by" line, both linking to
  www.qmediat.io/open-source; the copyright line names the company as LICENSE does.
- The distribution name on PyPI is **`ai-costs`** (`pip install ai-costs`): PyPI refuses `ai-cost` as too similar to
  an existing, empty project. The command stays `ai-cost` and the package stays `ai_cost`.

## [2.2.2] - 2026-09-23

Tagged, never published.

### Changed

- The price drift check identifies itself as `ai-cost/<version> (+https://github.com/qmediat/ai-cost)` and no
  longer retries as a browser when a vendor page refuses it: such a page is reported as `fetch-failed` and its
  prices are maintained by hand in your prices file.

### Added

- `SECURITY.md`: where to report privately, and a plain statement of what the tool reads and when it touches the
  network.

## [2.2.1] - 2026-09-23

Tagged, never published (2.2.0 was tagged but never published: its usage-log path check skipped an
absent file below a symlinked directory such as macOS's `/var`).

### Fixed

- A usage-log path below a symbolic link to an existing directory (macOS `/var`, `/tmp`, a linked home) is treated
  like any other path: only a link whose target is absent makes the path a counted skip.

## [2.2.0] - 2026-09-23

Tagged, never published.

### Added

- **Per-project scope.** Every row carries the working directory its CLI ran in (a Codex CLI rollout's `cwd`, a
  Gemini CLI folder mapped through `~/.gemini/projects.json`, a Grok Build CLI session directory), so `--project DIR`
  and a plain `report` run from inside a project keep that project's rows on every source; the report header counts
  what was kept, left out and unscoped.
- **Grok Build CLI source** (`grok-build`): one row per turn and model from `~/.grok/sessions/<cwd>/<id>/usage.json`,
  priced from the CLI's own cost estimate.
- **`ai-cost daily`**: one UTC day's reports — `global.{md,json}` over every project and one `<project>.{md,json}`
  per project touched that day, plus `index.json`; `install --schedule-reports [--at HH:MM]` runs it every morning
  (launchd on macOS, cron elsewhere).
- **`ai-cost reconcile`**: the local count of one provider against the figure its console shows, with a tolerance and
  a history file; exit 1 above tolerance.
- `doctor` shows both schedules, the newest daily index and the last reconciliation.
- `grok-4.7` in the price registry.

### Changed

- The `Calls` column is `Runs`; `Model calls` shows the requests a source reports.
- `reconcile` counts API-billed rows only and carries the report's warnings; `daily` splits a day's plan shares
  between projects by their API-equivalent share instead of repeating them, fails loud on a project it could not
  write, and names a day whose rows were all unpriced.

### Fixed

- A usage-log path that is a symlink loop or a dangling link is skipped and counted on Python 3.13 and newer, where
  `Path.resolve()` no longer raises on a loop.
- A zipapp reached through a symlinked directory (`/tmp` on macOS, a linked `~/bin`) recognises its own plugin
  bundle marker: the location check resolves symlinks on both sides.

## [2.1.0] - 2026-09-21

### Added

- **Plugin boundary** (`ai_cost.plugins`): a `Source` (`collect(ctx)`), an `Enricher` (`enrich(rows, ctx)`),
  doctor lines and a tests package per plugin; discovered through `importlib.metadata` entry points
  (group `ai_cost.plugins`), `plugins: [...]` in the config, `AI_COST_PLUGINS`, or the `ai_cost_bundle` marker of a
  bundled build. Plugin settings live under `plugin_settings.<name>`; `--setting NAME.KEY=VALUE` overrides one run.
- Gemini CLI source (`~/.gemini/tmp/*/chats/session-*.jsonl`).
- `scripts/build.py` builds the single-file zipapp of the package; the published artefacts are checked before every
  release.

### Changed

- Providers are a registry: the set is what the price book names (built-in plus the user's file); an unknown
  provider without a price follows `--unpriced`.
- Row kinds are neutral (`transcript`, `session`, `chat`, `log`, `ledger`, `review`, `copilot`, `actions`) and every
  row names its `source`; a per-session cost is split across the session's rows by `share`.
- A row's billing comes from what its source saw (a Codex CLI rollout naming a plan is a subscription turn; a
  usage-log line says `billing`), then from `providers.<name>.billing`, and stays unknown without evidence. The
  public default for Claude Code transcripts is `mixed`: a fresh install counts them as unknown billing until the
  config says how the account pays.
- The shipped `config.json` has no subscriptions, no default model and a generic vendor profile.

## [2.0.0] - 2026-09-20

### Changed

- The single-file tool became a typed package (`src/ai_cost/`, one module per concern) shipped as one executable
  zipapp; tests are named functions run by pytest and by the shipped file's `selftest`.
- Every boundary is a dataclass or an enum; price shapes and provider rules are dispatch tables; every skipped record
  is counted and shown by `doctor`.

### Added

- `report --attribute` — cost attributed to named scopes (branches, workspaces, pull requests, path patterns).
- `ai-cost log --from-response` for raw Anthropic, Google and OpenAI responses, with the provider-native counter
  names, so every pricer keeps its semantics.
