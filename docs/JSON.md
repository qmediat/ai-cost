# JSON output (`--format json`)

Top level: `version`, `generated_at`, `window` (`{start, end}`), `window_iso` (`[start, end]`), `window_hours`,
`row_count`, `sources`, `warnings`, `skipped` (`[{source, path, reason}]`), `prices_checked_at`, `real`
(`subscriptions[]` — a share the usage report states has `attribution` `invoice`, `seats` 0 and its user-months in
`note` —, `usage[]`, `cash_usd`, `subscription_usd`, `total_usd`, `unknown_billing`, `unknown_by_provider`, `unfigured_ledger`), `api` (`lines[]`,
`total_usd`), `models[]` (with the API group: one per model — `provider`, `model`, `records`, `model_calls`, `tokens`, `api_usd`, `paid_usd`, `payments[]` with `paid` (`plan`, `metered beyond the plan`, `API key`, `billed`, `untracked`, `settled on another row`, `unknown`), `evidence` (`list price`, `reported`, `ledger`, `usage report`, `provider report`, `no amount`), `usd`, `rows` and `text` (the table's cell); the payments sum to the real group's cash on the model), `vendor` (when items exist), `attribution` (with `--attribute`: `lines[]`, each with its `models[]` — `provider`, `model`, `calls`, `model_calls`, `tokens`,
`api_usd`, `cash_usd`, `notes`, `payments[]`; one per model the label's rows name, the largest list price first; cash is
what the real group counts on that model's own rows — a ledger's settlement included, 0 under a plan —, `notes` name the
billing rules that counted it (`api-key (ledger)`, `plan`, …) and `payments[]` split that cash as the per-model table
does, each with its `text` — the cell as the table prints it; a provider report's difference is the model
`untracked`), `github_bill` (with `providers.github.bill`:
`account`, `read_at`, `days`, `provisional`, `counted[]` / `left_out[]` (`product`, `sku`, `unit`, `lines`, `quantity`,
`gross`, `discount`, `net` — amounts as exact text), `outside[]` (days the window only touches), `repositories[]` (every
touched day's counted lines per repository: `day`, `repository`, `product`, `sku`, `lines`, `gross`, `discount`, `net`,
`final` — false while the day may still grow; a repository's day holds every use in it that day, the report splits it
no further), `missing` (months not read, with the reason)), `provider_reports` (with `providers.<name>.report`: per provider `days[]` —
`day` (`start`, `end`, `lines[]` with `gross` / `net` / `requests` / `excluded` / `rate_note` — the provider's own
conversion, e.g. `PLN ÷ 3.75385` —, `closed`, `currency`, `counts_requests` — the provider states a request
count per line —, `requests` — `null` when it states none), `local_api`, `local_real`, `api_diff`, `real_diff` (exact
text; `null` when not compared), `why_not`, `unknown_rows`, `local_requests`, `uncounted_rows` — plus `missing[]` and
`unreadable[]` spans with their reason, `offline` (a live source not asked) and `utc_days` (its days are UTC days, so
`daily` reads a day not final again)). A line's `calls` is what it folded in
(rows, review runs, Copilot reviews — the `Records` column) and `model_calls` the API requests its sources reported
(`Model calls`; 0 where none does); `--detail` adds `rows[]`, where
`client` is the program that wrote the session (a Codex rollout's `originator`), `cost_reported` is the source's
own figure for the row: cash for an `api` row (a charge the source reported), the
source's list-price estimate for a `subscription` row (never counted as cash; the API-equivalent fallback when the
model has no list price), a part of the session's figure when a session spans several rows; `source` names the
source that produced the row and `kind` its record shape (`transcript`, `session`, `chat`, `log`, `ledger`,
`review`, `copilot`, `actions`, `invoice` — a usage-report line, its amounts in `invoice`, `untracked` — a provider
day's difference, its amounts in `untracked`). Every dataclass property is
serialised, so computed totals are always present.
Changes since 1.0: `window` is an object (the list moved to `window_iso`), `seats` multiply only per-seat plans, an
unpriced model exits 5 unless `--unpriced skip`.
