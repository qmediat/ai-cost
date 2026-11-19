# Development

`ai-cost` is a Python 3 package (stdlib only) shipped as one executable file: a zipapp that
`python3 scripts/build.py src dist/ai-cost` builds from `src/ai_cost/`, tests included.

`src/ai_cost/` is the package: `models.py` (the typed data — `UsageRow`, `Tokens`, `Window`, `WorkItem`, `Report`),
`pricing.py` (one pricer per price shape, chosen from a dispatch table), `groups.py` (real / api / vendor),
`collectors/` (one module per built-in source — `claude`, `codex`, `gemini_cli`, `grok_build`, `github`, `github_bill`, `usage_log` — each
returning `Collected` rows plus counted `Skipped` records), `plugins.py` (the source / enricher / placer protocol and
discovery), `config.py` with `data/{prices,config}.json`, `prices_check.py`, `ops.py` (report assembly and the
per-project scope), `doctor.py`, `monitor.py`, `schedules.py` (the launchd / cron jobs), `report_warnings.py` (the header's warnings), `output.py` (`Emit` and the state-file helpers), `daily.py`, `reconcile.py`, `render.py`, `cli.py`. The design note and
the architecture decision records are kept with the development source, not in the published tree.

```bash
PYTHONPATH=src python3 -m ai_cost selftest      # or python3 -m pytest — the tests live in src/ai_cost/tests/
python3 scripts/build.py src dist/ai-cost   # the shipped single file, reproducibly (fixed timestamps: a rebuild is byte-identical)
python3 scripts/check_supports.py           # the README's "Supports · version" heading and __version__ name the package version
python3 scripts/check_addresses.py          # every address-shaped text is in qmediat.io or on its allowed list (tracked files; --walk: every file)
```

A release bumps the version in `pyproject.toml` and `src/ai_cost/__init__.py` and the README heading
`## Supports · version X.Y.Z` with it: `scripts/check_supports.py` runs in CI and before every publication, and fails
while any of the three differs.
