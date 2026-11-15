# Embed it in an app

An app that calls model APIs records each call and shows its operators what the calls cost — per installation, with
no person at a console.

| Your app is written in | Use |
|---|---|
| Python | `ai_cost.api`: an `Engine` bound to one installation |
| Any other language, a script, a cron job | `ai-cost --instance DIR …`: the same engine, the same JSON |

## The same five steps, twice

```python
from pathlib import Path
from ai_cost.api import Call, Engine, Instance, Period

engine = Engine(Instance(Path("/var/lib/my-app/ai-cost")))        # a str is taken too; made absolute
engine.record(Call("openai", "gpt-6-sol", {"input": 1200, "output": 300}, labels={"tenant": "acme"}))
quote = engine.price(Call("openai", "gpt-6-sol", {"input": 1200, "output": 300}))   # Price(usd, note)
screen = engine.report_json(Period(hours=24), where={"tenant": "acme"}, by="feature")
problems = engine.doctor()                                                          # [] when all is well
```

```bash
ai-cost instance init /var/lib/my-app/ai-cost
ai-cost --instance /var/lib/my-app/ai-cost log --provider openai --model gpt-6-sol --input 1200 --output 300 --label tenant=acme
ai-cost --instance /var/lib/my-app/ai-cost price --provider openai --model gpt-6-sol --input 1200 --output 300 --format json
ai-cost --instance /var/lib/my-app/ai-cost report --hours 24 --where tenant=acme --by feature --format json
ai-cost --instance /var/lib/my-app/ai-cost doctor
```

The engine creates the directory on the first `record`. Counters use the provider's own names
([USAGE-LOG.md](USAGE-LOG.md#the-line)). `AI_COST_INSTANCE=DIR` does what `--instance DIR` does.

**Naming the directory.** The command takes what you type: `.` is the directory it runs in (`ai-cost instance init
.`), a leading `~` is your home; an empty or blank `--instance` / `instance init ''` / `--outside-log ''` is refused
(exit 2), and an empty `AI_COST_INSTANCE` counts as unset. The library wants a real path: `Instance` refuses, as
`ApiError`, a root that is empty or normalises to the current directory — `""`, `"."`, `"./"`, `"a/.."`,
`Path("")`, `Path(".")` alike, since `Path("")` reads `"."` and an unset variable must never mean "here" — and a
leading `~` (it is never expanded: call `Path.expanduser()` first). For the current directory give `Path.cwd()`.

**The same JSON:** `engine.report_json(period, …)` and `ai-cost --instance DIR report --format json` with the same
period and options print the same bytes (computed at the same moment). On an instance the command takes the engine's
defaults: groups `real,api`, every project, an unpriced model skipped and counted; a flag you give still decides.

## The instance directory

| Path | Holds |
|---|---|
| `usage/YYYY-MM.jsonl` | the calls, one line each, by the call's UTC month; a report reads only the months it covers |
| `config.json` | plans and budgets ([CONFIGURATION.md](CONFIGURATION.md)); absent: the shipped defaults — an app that pays per token configures nothing |
| `prices.json` | price rows the shipped list lacks; absent: the shipped prices |
| `state/` | `monitor` history |

An app that cannot write into the instance directory (a container, a serverless function) prints one usage line per
call, each with its `event_id`, into its log stream; the operator's side feeds the stream to `ai-cost --instance DIR
import usage FILE|-` (Python: `engine.import_usage(lines)` → `UsageImport`). A line already there is not written
again, so a stream fed twice counts once; a line the import takes is one `record` accepts — no keys beyond the
documented ones; `UsageImport.skipped` and `.examples` are keyed by `SkipClass`. Feed a stream OR list its file
under `usage_logs`, never both: the two are separate logs and each call would count twice
([COMMANDS.md](COMMANDS.md#import-usage)).

The app on another machine: `ai-cost collect HOST:DIR --into PULLS` on the machine that reports pulls `usage/` over
ssh into one file per instance, to list under that machine's `usage_logs` ([COMMANDS.md](COMMANDS.md#collect)); the
file never shrinks — a smaller pull is refused and the file kept.

An instance reads nothing else: no coding-CLI file (`read_clis=True` / `--read-clis` adds them), no plugin
(`Instance.plugins` / `--plugin MODULE` names the ones the app trusts), no GitHub account or provider day report
(`--github` is refused), no environment variable. A config's `usage_logs` entry outside the directory needs
`Instance.outside_logs` / `--outside-log PATH`. An `Engine` reads the config and the prices once: build a new one
after changing them.

## What an operator screen usually shows

| JSON field ([JSON.md](JSON.md)) | On the screen |
|---|---|
| `api.total_usd` | what the calls cost at list price |
| `real.total_usd` | what was paid: per-token calls plus plan shares; with `where`, the selected calls' cash only — no plan share |
| `models[]` | per model: calls, tokens, price |
| `attribution.lines[]` (`by`) | one line per label value, then `(no <label>)`: `label`, `calls`, `api_usd`, `real_usd` |
| `window_iso`, `generated_at` | the period and when it was computed |
| `warnings`, `skipped` | what could not be counted, and why |

**Compatibility:** within one major version a field of `docs/JSON.md` is never renamed or removed; fields may be
added. `ai_cost.api.__all__` is the library interface; every other module is internal.

## Errors and limits

| Case | Result |
|---|---|
| A call the report would skip (a counter the provider does not price, a time without a zone, a malformed label) | `ApiError`, nothing written; the command exits 2 |
| A model without a list price | `record` needs the call's `cost`; `price` gives `usd=None` (the command exits 5); `report` skips it, counted |
| A call that carries its `cost` | `price` answers that cost, "reported by the caller" |
| A line of 4096 bytes or more | refused: one `write` of at most 4096 bytes (the line and the newline that may precede it) is what keeps parallel writers from interleaving |
| A line in a segment of another month, a file in `usage/` that is no month | a counted skip; named by `warnings` and `doctor` |
| Time | a report reads the period's lines: about 0.05 s per 1 000 calls on a laptop (83 334 calls: 4.2 s) |
| The network | never used |

`price` is the provider's list price unless the call says what it was charged: discounts and what an account was
actually charged are not known to it. Times are UTC at second precision; give aware datetimes. Not supported: a
usage directory on a network filesystem, an app with no disk or no Python 3.9.
