# AGENTS.md

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
| A release | README "Supports" updated to the new version; the release check fails when its version differs |
