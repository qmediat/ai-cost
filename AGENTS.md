# AGENTS.md

## Add it to a project

| | |
|---|---|
| Install | `pipx install ai-costs` (or `pip install ai-costs`, `npm install -g ai-costs`); needs Python 3.9 or newer, nothing else |
| A machine with Claude Code, Codex CLI, Gemini CLI or Grok Build CLI | Nothing to add: `ai-cost report` reads their files |
| An app, script, server or CI job that calls a model API | Append one JSON line per request to the usage log: [docs/USAGE-LOG.md](docs/USAGE-LOG.md) — `ai-cost log …` from a shell, `ai_cost.log.record(…)` in Python, one `appendFile` in Node, the same line from any language |
| Where the log is | `~/.local/share/ai-cost/usage.jsonl`; another file: `AI_COST_USAGE_LOG` for the writer, `usage_logs` in the config for the reader |
| A call a CLI transcript already records | Not logged again: it would count twice |
| Tell projects apart | A line's `source`, `ref` and `tags`; `--attribute LABEL=REGEX` in the report; or one log file per project |
| Plans and billing rules | `ai-cost install --init-config`, then `ai-cost doctor` names every key that is missing |
| A budget in CI | `ai-cost monitor` exits 3 on a breach |
| An app on another machine | The log is a local file: copy it to the machine that reports and list it under `usage_logs` |
| Check the integration | `ai-cost doctor` lists the usage log and its counted skips; `ai-cost report --hours 1` shows the new rows |

## Operate the tool

| | |
|---|---|
| Check the machine | `ai-cost doctor` |
| Cost of one session | `ai-cost report --session <id> --format json` |
| Cost of one project | `ai-cost report --project <dir>` |
| Every step with its check | [docs/SETUP.md](docs/SETUP.md) |
| What to run for each question | [docs/SETUP.md §8](docs/SETUP.md#8-operate-it-from-an-agent) |
| Exit codes | [docs/COMMANDS.md](docs/COMMANDS.md#exit-codes-and-what-to-do) |

## Work on the code

| | |
|---|---|
| Test | `PYTHONPATH=src python3 -m ai_cost selftest` (or `python3 -m pytest -q src/ai_cost/tests`) |
| Build | `python3 scripts/build.py src dist/ai-cost` |
| Format, lint, types | `ruff check src` · `black --check src` · `mypy --strict src/ai_cost` |
| README "Supports" and `__version__` against the package version | `python3 scripts/check_supports.py` |
| Every email address in `qmediat.io` or on the allowed list | `python3 scripts/check_addresses.py` |
| Dependencies | Python standard library only |
| Detail | [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) |

## Where things are

| | |
|---|---|
| `src/ai_cost/models.py` | Typed data: UsageRow, Tokens, Window, WorkItem, Report |
| `src/ai_cost/collectors/` | One module per source |
| `src/ai_cost/pricing.py` | One pricer per price shape |
| `src/ai_cost/groups.py` | Real, API-only, vendor |
| `src/ai_cost/data/` | Shipped prices and default config |
| `src/ai_cost/tests/` | Tests, shipped inside the file |

## Rules

| | |
|---|---|
| Network | Only when a command asks for it |
| A record that cannot be read | Counted and shown, never dropped |
| A user-visible change | One CHANGELOG.md entry |
| Addresses | The addresses this repository publishes for the project are in `qmediat.io` (`security@`, `dev@`); the qmediat team's commits are authored as `dev@qmediat.io`, a contributor's with their own address; no personal or account address in any file — enforced by `python3 scripts/check_addresses.py`; no shape is exempt — an address-shaped text a file must show is one allowlist entry |
| A release | README "Supports" updated to the new version; the release check fails when its version differs |
