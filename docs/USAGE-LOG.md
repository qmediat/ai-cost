# Report your own app's calls

Anything that calls an LLM API outside the CLIs — a script, a web app, an MCP server, a CI job — appends **one
JSON line per request** to the usage log; the next `report` prices it exactly like the CLI rows. Log only what no
CLI transcript already records (a call an app logs AND a CLI transcript records would count twice).

The usage log is `$XDG_DATA_HOME/ai-cost/usage.jsonl` (`~/.local/share/ai-cost/usage.jsonl` by default), or the file
named by `AI_COST_USAGE_LOG`; the report reads it like every other source.

## The line

The line (`schema` 1): required `schema`, `at` (ISO-8601 with zone, when the request finished), `provider`
(pricebook id: `anthropic`, `openai`, `google`, `xai`, `deepseek`, or one you added), `model` (as the API reports
it), and `tokens` or `cost`. Counters use the provider's own names so the pricing stays exact — only the ones its
formula reads, or the line is a counted skip:

```json
{"schema":1,"at":"2026-09-20T10:00:00Z","provider":"openai","model":"gpt-5.5","source":"my-app",
 "tokens":{"input":1200,"cached_input":800,"output":300},"cost":0.0123,"ref":"job-42","branch":"feat/x","tags":["ci"]}
```

| provider | counters |
|---|---|
| anthropic | `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h` (or `cache_write_unsplit` when the response does not say which TTL), `web_search` (searches, billed per thousand) |
| openai, xai, any OpenAI-compatible API | `input` (includes the cached part), `cached_input`, `output` |
| google | `prompt` (includes `cached`), `cached`, `output`, `thoughts` |
| deepseek | `cache_hit`, `cache_miss`, `output` |
| anything else | `input`, `output` |

Reasoning tokens are billed as output for every provider (Google's `thoughts` count as output). A counter the
provider's formula never reads (an `input` on a deepseek line) makes the line a counted skip, and `ai-cost log` /
`record()` refuse it up front — nothing is priced at zero in silence.

Optional: `cost` (USD the request was charged — the row's cash under an `api` rule; a row with `cost` and no list
price is priced by it; a `currency` other than `USD` is a counted skip), `billing` (`api` \| `subscription`; a line
with tokens or a cost is `api` by default), `event_id` (a repeated id in one file is read once — safe to replay an
import), `source` (your program's name, shown in the report header), `ref`, `session` (name a CLI session id and the
row takes that session's scope: its project and branch), `branch`, `pr`, `tags[]` (attribution keys), and who
launched the work — `origin_session` (the session that started it, e.g. a Claude Code session id), `origin_repo`
(`owner/name`), `origin_pr` (a positive integer, only with `origin_repo`), `run_id` (one id per invocation; `ai-cost
log --origin-session … --origin-repo … --origin-pr … --run-id …`). `origin_session` and `run_id` are letters, digits
and `. _ : -`, at most 200 characters; a malformed origin keeps the line's usage and is one counted warning per file.
`pr` stays free text; `origin_pr` is the number.

A line that fails validation (a boolean, a negative or fractional count, a malformed provider id) is a counted skip,
never a guess; a well-formed provider or model the pricebook does not list, with no cost, is an unpriced row under
`--unpriced`.

## From a shell, straight from the API's response

The shapes of Anthropic, Google, OpenAI Chat Completions and OpenAI Responses are recognised. Google, DeepSeek and
Anthropic name themselves — Google by `usageMetadata`, DeepSeek by `prompt_cache_hit_tokens`, Anthropic by
`type: message` or its cache counters; a bare OpenAI-shaped usage block needs `--provider`:

```bash
curl -s https://api.openai.com/v1/chat/completions … > resp.json
ai-cost log --from-response resp.json --provider openai --ref job-42 --source my-script
```

## From Python, in process

The line is validated the way the reader reads it; a `ValueError` names the problem:

```python
from ai_cost.log import record          # pip/pipx install; with the single file: sys.path.insert(0, "/path/to/ai-cost")

resp = client.chat.completions.create(model="gpt-5.5", messages=msgs)   # any OpenAI-compatible client
u = resp.usage
record("openai", resp.model,
       {"input": u.prompt_tokens, "cached_input": u.prompt_tokens_details.cached_tokens, "output": u.completion_tokens},
       ref="job-42", source="my-app", event_id=resp.id)
```

## From Node (an MCP server, an Express app)

Write the line yourself, one `appendFile` per request; the format is the contract, any language can write it:

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

## A plugin for a source of your own

Any program can add a source (rows for a window), an enricher (a pass over every row, e.g. to settle what a key was
actually charged), doctor lines and tests. A plugin is a Python module that exports
`PLUGIN = ai_cost.plugins.Plugin(...)`. The protocol is in
[`src/ai_cost/plugins.py`](../src/ai_cost/plugins.py): a `Source` has a `name` and `collect(ctx) -> Collected`, an
`Enricher` has `enrich(rows, ctx) -> rows`, and `Context` gives them the paths, the config, the window, the request,
the plugin's settings and the shared `skipped` / `warnings` sinks.

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

Register it in one of three ways: an entry point in your package (the `ai_cost.plugins` group:
`[project.entry-points."ai_cost.plugins"] my_llm_costs = "my_llm_costs"`), `"plugins": ["my_llm_costs"]` in the
config, or `AI_COST_PLUGINS=my_llm_costs` in the environment (the module must be importable: on `PYTHONPATH`, or
installed next to `ai-cost`). Its settings live under `plugin_settings.<name>` in the config and can be overridden for
one run with `--setting <name>.<key>=<value>`. `doctor` lists every loaded plugin and says why one could not be
loaded; a plugin that raises costs its own rows, never the report. A `Plugin(tests_package="my_llm_costs.tests")` gets
its tests run by `ai-cost selftest`.
