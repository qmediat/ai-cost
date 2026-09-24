# Changelog

All notable changes to `ai-cost` are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
