---
name: ai-cost
description: >-
  Count and price the LLM usage of any work: every model the user called through Claude Code, Codex CLI, Gemini CLI,
  Grok Build CLI, their own scripts or applications. Reads the usage that is already on disk (transcripts, session logs, a one-line
  JSONL usage log any program can write), prices it at list price with cache tiers, and reports real cost vs
  API-equivalent cost per provider, model, day or label. Works with only this file (the AI does the counting with a
  stdlib Python snippet) or with the optional `ai-cost` package for reports, budgets, price drift checks and
  attribution. TRIGGERS: "what did this cost", "token cost", "how much did the AI work cost", "koszt tokenów",
  "koszt AI", "ile kosztowała ta praca", "LLM usage", "count my API usage", "price this session".
---

# ai-cost — count and price LLM usage (single-file skill)

This file is enough on its own. With it, an AI assistant (Claude Code or any agent that can read files and run
Python 3) counts the tokens every LLM consumed during the user's work and prices them. Installing the `ai-cost`
package adds reports, budgets, a price-drift check and attribution, but nothing below needs it.

## 1. Decide what to count

Ask (or infer from the request) three things: **which tools or apps** made LLM calls, **which time window**, and
**how the user pays** (subscription, API key, both). Then collect from every source that exists:

| source | where the usage is | one line per | fields |
|---|---|---|---|
| Claude Code | `~/.claude/projects/<project-slug>/*.jsonl` (+ `<session>/subagents/*.jsonl`) | assistant message (`type: "assistant"`, streamed entries repeat the same `message.id` — count each id once) | `message.model`, `message.usage.{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}}`, `timestamp` |
| Codex CLI | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | turn (`payload.type: "token_count"` → `payload.info.last_token_usage.{input_tokens, cached_input_tokens, output_tokens}`), model from `turn_context.payload.model`; `payload.rate_limits.plan_type` on a `token_count` event (`team`, `plus`, …) = a ChatGPT plan session, null = the API key or unknown | `timestamp` |
| Gemini CLI | `~/.gemini/tmp/<project>/chats/session-*.jsonl` (`$GEMINI_CLI_HOME` instead of `~/.gemini` when set; older versions: `.json` with `messages[]`) | model message (`type: "gemini"`, same `id` may repeat — count once) | `model`, `tokens.{input, cached, output, thoughts}`, `timestamp`; `input` includes `cached`; `thoughts` are billed as output |
| Grok Build CLI | `~/.grok/sessions/<working directory, URL-encoded>/<session>/usage.json` (`$GROK_HOME` instead of `~/.grok` when set) | turn (`turns[]`, one `modelUsage` entry per model) | `endedAt`, per model `inputTokens` (includes `cachedReadTokens`), `cachedReadTokens`, `outputTokens`, `reasoningTokens` (billed as output), `modelCalls`, `costUsdTicks` (the CLI's own estimate, 1e-10 USD) |
| the user's scripts and apps | `usage.jsonl` they write (schema in §3), or the raw API responses they saved | request | `provider`, `model`, `tokens`, `cost` |
| raw API responses | Anthropic `usage.*`, OpenAI Chat `usage.prompt_tokens/completion_tokens/prompt_tokens_details.cached_tokens`, OpenAI Responses `usage.input_tokens/output_tokens/input_tokens_details.cached_tokens`, Google `usageMetadata.promptTokenCount/candidatesTokenCount/cachedContentTokenCount/thoughtsTokenCount` | request | as listed |

Only count what no other source already recorded: a call an app logs AND a CLI transcript records would count twice.

## 2. Count (stdlib Python, run it as is)

```python
import json, glob, math, os, re, sys, datetime as dt
from collections import defaultdict

def arg(i):                                                                              # an ISO-8601 stamp from argv, e.g. 2026-09-20T08:00+00:00; a mistyped one is said, not a traceback
    if len(sys.argv) <= i: return None
    try: return dt.datetime.fromisoformat(sys.argv[i].replace("Z", "+00:00"))                # a trailing Z on every Python the package runs on (3.9+)
    except ValueError: sys.exit(f"usage: count_usage.py [SINCE] [UNTIL] as ISO-8601 (2026-09-20T08:00+00:00), not {sys.argv[i]!r}")
SINCE, UNTIL = arg(1), arg(2)
totals = defaultdict(lambda: defaultdict(int))                                      # (provider, model, billing, UTC day, band) → counters

def aware(t): return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)   # a naive stamp is UTC
SINCE, UNTIL = aware(SINCE), aware(UNTIL)
def ts(s):
    try: return aware(dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")))
    except Exception: return None
def inside(t): return t is not None and (SINCE is None or t >= SINCE) and (UNTIL is None or t < UNTIL)   # half-open, like the package
def day(t): return t.astimezone(dt.timezone.utc).date().isoformat()                       # dated prices apply per UTC day
def amount(c):                                                                            # a cost as the package accepts it: a finite non-negative number, else None
    try: return float(c) if isinstance(c, (int, float)) and type(c) is not bool and math.isfinite(float(c)) and c >= 0 else None
    except OverflowError: return None                                                     # an integer beyond float range
def home(var, default): return os.path.expanduser(os.environ.get(var) or default)         # an override may say ~, as the package expands it
def band(prov, t, counts):                                            # the price band a request falls in, when the provider has one
    if prov == "deepseek":                                                              # DeepSeek's peak: Mon–Fri 01:00–04:00 and 06:00–10:00 UTC (the package's prices.json)
        u = t.astimezone(dt.timezone.utc)
        return "peak" if u.weekday() < 5 and (1 <= u.hour < 4 or 6 <= u.hour < 10) else "off-peak"
    if prov == "google" and (whole(counts.get("prompt")) or 0) >= 200000: return "long"     # the long-context tier of one request (the package: prompt >= 200000); a counter that is not a count says nothing here
    return "-"
def whole(v):                                          # a counter as the package's count() takes it: a non-negative int up to 2**53, or a float that is one (1200.0); else None
    if type(v) is int or (isinstance(v, float) and math.isfinite(v) and v.is_integer()): return int(v) if 0 <= v <= 2**53 else None
    return None
def billable(*counts):                                 # a record with at least one positive counter: only such a record may claim its id (a streamed empty first record must not shadow the later real one)
    return any((whole(x) or 0) > 0 for x in counts)
def add(key, **counts):                                # every counter whole (absent = 0), else the whole record is skipped: False
    clean = {k: (0 if v is None else whole(v)) for k, v in counts.items()}
    if any(v is None for v in clean.values()): return False
    if "thoughts" in clean: clean["output"] = clean.get("output", 0) + clean.pop("thoughts")   # reasoning is billed as output, whoever reports it (Gemini thoughts, Grok reasoningTokens)
    for k, v in clean.items(): totals[key][k] += v
    return True
def obj(x): return x if isinstance(x, dict) else {}                                      # a nested value that is not an object: nothing
def lines(path):                                                                          # a file that cannot be read (permission, a directory): skipped, the rest still counts
    try:
        with open(path, encoding="utf-8", errors="replace") as handle: yield from handle
    except OSError: return
def records(path, jsonl=None):                   # JSON Lines (one object per line) unless told otherwise; an older Gemini .json: its messages[] list
    if path.endswith(".jsonl") if jsonl is None else jsonl:
        for line in lines(path):
            try: yield json.loads(line)
            except (ValueError, RecursionError): pass                                        # a torn line, or one nested beyond reason: skipped, the rest still counts
    else:
        try: doc = json.loads("".join(lines(path)))
        except (ValueError, RecursionError): return
        yield from (doc.get("messages") or []) if isinstance(doc, dict) else ()
# billing: "plan" when the file says so, the log line's own key ("api" when it has none), "?" when the file cannot tell
seen = set()
claude_home = home("CLAUDE_CONFIG_DIR", "~/.claude")                                           # the CLIs' own overrides, as the package reads them
codex_home = home("CODEX_HOME", "~/.codex")
for path in glob.glob(os.path.join(claude_home, "projects/*/**/*.jsonl"), recursive=True):   # Claude Code
    for line in lines(path):
        if '"usage"' not in line: continue
        try: r = json.loads(line)
        except (ValueError, RecursionError): continue                                        # a torn line, or one nested beyond reason: skipped
        if not isinstance(r, dict): continue                                             # not an object: skipped
        m = obj(r.get("message")); u = obj(m.get("usage")); when = ts(r.get("timestamp"))
        if r.get("type") != "assistant" or not u or not inside(when): continue
        mid = m.get("id") or r.get("uuid"); mid = mid if isinstance(mid, (str, int)) else None    # only an id can repeat; id-less lines all count
        if mid is not None and mid in seen: continue
        cc = obj(u.get("cache_creation"))
        positive = billable(u.get("input_tokens"), u.get("output_tokens"), u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"), cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens"))
        kept = add(("anthropic", str(m.get("model", "?")), "?", day(when), "-"), input=u.get("input_tokens"), output=u.get("output_tokens"),
            cache_read=u.get("cache_read_input_tokens"),
            cache_write_5m=cc.get("ephemeral_5m_input_tokens"), cache_write_1h=cc.get("ephemeral_1h_input_tokens"),
            cache_write_unsplit=0 if cc else u.get("cache_creation_input_tokens"))
        if mid is not None and positive and kept: seen.add(mid)                            # remembered only for a record that counted
for path in glob.glob(os.path.join(codex_home, "sessions/*/*/*/rollout-*.jsonl")):               # Codex CLI
    model = "?"
    for r in records(path):
        if not isinstance(r, dict): continue                                             # not an object: skipped
        p = obj(r.get("payload"))
        if r.get("type") == "turn_context": model = str(p.get("model") or model)
        when = ts(r.get("timestamp"))
        if p.get("type") == "token_count" and inside(when):
            last = obj(obj(p.get("info")).get("last_token_usage"))
            billing = "plan" if obj(p.get("rate_limits")).get("plan_type") else "?"      # a ChatGPT plan turn, or the key / unknown
            add(("openai", model, billing, day(when), "-"), input=last.get("input_tokens"), cached_input=last.get("cached_input_tokens"),
                output=last.get("output_tokens"))
seen = set()
for path in glob.glob(os.path.join(home("GEMINI_CLI_HOME", "~/.gemini"), "tmp/*/chats/session-*.json*")):   # Gemini CLI
    for r in records(path):
        if not isinstance(r, dict): continue                                             # not an object: skipped
        when = ts(r.get("timestamp"))
        if r.get("type") != "gemini" or not isinstance(r.get("tokens"), dict) or not inside(when): continue
        gid = r.get("id") if isinstance(r.get("id"), (str, int)) else None
        if gid is not None and gid in seen: continue
        t = r["tokens"]; positive = billable(t.get("input"), t.get("cached"), t.get("output"), t.get("thoughts"))
        kept = add(("google", str(r.get("model", "?")), "?", day(when), band("google", when, {"prompt": t.get("input")})), prompt=t.get("input"), cached=t.get("cached"), output=t.get("output"),
            thoughts=t.get("thoughts"))
        if gid is not None and positive and kept: seen.add(gid)                          # remembered only for a record that counted something, as the Claude loop above
for path in glob.glob(os.path.join(home("GROK_HOME", "~/.grok"), "sessions/*/*/usage.json")):   # Grok Build CLI
    try: doc = json.loads("".join(lines(path)))
    except (ValueError, RecursionError): continue                                          # not JSON, or nested beyond reason: skipped
    turns = doc.get("turns") if isinstance(doc, dict) else None
    for t in turns if isinstance(turns, list) else []:
        if not isinstance(t, dict): continue                                             # not an object: skipped
        when = ts(t.get("endedAt"))
        if not inside(when): continue
        per_model = obj(t.get("modelUsage")) or {str(t.get("primaryModelId") or "?"): t}   # one entry per model; an old file without the split: the turn under its primary model
        for model, u in per_model.items():
            u = obj(u); key = ("xai", str(model), "?", day(when), "-")                      # the file never says how the session was paid
            if add(key, input=u.get("inputTokens"), cached_input=u.get("cachedReadTokens"), output=u.get("outputTokens"),
                   thoughts=u.get("reasoningTokens"), requests=u.get("modelCalls")):
                ticks = whole(u.get("costUsdTicks"))
                if ticks: totals[key]["cli_cost_usd"] += ticks / 1e10                        # the CLI's own price estimate in USD (verified to the cent, 2026-09-21): what the package's `real` trusts under xai.trust_cli_cost; the §4 formula gives the list price
PRICED = {"anthropic": {"input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "cache_write_unsplit", "web_search"},
          "openai": {"input", "cached_input", "output"}, "xai": {"input", "cached_input", "output"},
          "google": {"prompt", "cached", "output", "thoughts"}, "deepseek": {"cache_hit", "cache_miss", "output"},
          "github": set()}                                                                # what each §4 formula reads; github lines carry a cost, never token counters
ALL = set().union(*PRICED.values())
logs = [home("AI_COST_USAGE_LOG", os.path.join(home("XDG_DATA_HOME", "~/.local/share"), "ai-cost/usage.jsonl"))]   # the default log, or the override
cfg = home("AI_COST_CONFIG", os.path.join(home("AI_COST_CONFIG_DIR", "~/.config/ai-cost"), "config.json"))
try: listed = obj(json.load(open(cfg, encoding="utf-8"))).get("usage_logs")
except (OSError, ValueError, RecursionError): listed = None
logs += [os.path.expanduser(p) for p in listed if isinstance(p, str)] if isinstance(listed, list) else []   # the files the config lists; anything but a list of paths is ignored
for path in dict.fromkeys(os.path.realpath(p) for p in logs if os.path.exists(p)):               # usage log (§3), each file once
    seen = set()                                                                                 # event_id: read once per file
    for r in records(path, jsonl=True):                                                          # a log is JSON Lines whatever its name
        if not isinstance(r, dict) or type(r.get("schema")) is not int or r["schema"] != 1: continue   # not a log line: skipped
        when = ts(r.get("at"))
        if not inside(when) or r.get("currency") not in (None, "USD"): continue                 # outside the window, or not USD: skipped
        prov, model, billing = r.get("provider"), r.get("model"), ("api" if r.get("billing") is None else r.get("billing"))   # an absent or null billing means api, as the package reads it; "" or 0 is malformed
        if not (isinstance(prov, str) and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", prov) and isinstance(model, str) and model
                and billing in ("api", "subscription")): continue                            # ids and billing as the schema says, or a skip
        tokens, cost = r.get("tokens"), r.get("cost")
        if cost is not None:
            cost = amount(cost)
            if cost is None: continue                                                            # a cost that is not a finite non-negative number: a skip
        if tokens is not None and not (isinstance(tokens, dict) and all(whole(v) is not None for c, v in tokens.items() if c in ALL)): continue   # malformed counters: the whole line is a skip, cost or not
        given = dict(tokens or {})
        if "thoughts" in given: given["output"] = given.get("output", 0) + given.pop("thoughts")   # thoughts are output, whoever bills them — folded before the check, as the package's alias
        family = PRICED.get(prov)                                                                    # None: an unlisted provider — nothing is checked and every known counter is kept, as the package
        if family is not None and set(given) & (ALL - family): continue                            # a counter this provider's formula never reads: the whole line is a skip, as the package; an unknown key (a typo) is ignored
        counters = {c: v for c, v in given.items() if c in (ALL if family is None else family)}
        if not any(counters.values()) and cost is None: continue                                 # neither a positive counter nor a cost: a skip
        eid = r.get("event_id")
        if eid is not None and not isinstance(eid, str): continue                                # an id must be a string, as the package reads it: else the line is a skip
        if eid:                                                                                  # an empty id is no id: never deduped, as the package
            if eid in seen: continue
            seen.add(eid)                                                                        # remembered only for a line that counts
        key = (prov, model, billing, day(when), band(prov, when, counters))
        add(key, requests=1, **counters)
        if cost is not None: totals[key]["cost_usd"] += cost                                     # a float: sub-microdollar charges are kept, as the package keeps them
for (prov, model, billing, when, tier), c in sorted(totals.items()):
    print(prov, model, billing, when, tier, dict(c))
```

Save it as `count_usage.py`, run `python3 count_usage.py 2026-09-20T08:00+00:00 2026-09-20T18:00+00:00` and keep the
output table: it is the evidence for every number that follows. Each line is `provider model billing day band counters`:
one bucket per UTC day (dated prices apply per day) and per price band when the provider has one — `peak` / `off-peak`
for DeepSeek by its published windows (Mon–Fri 01:00–04:00 and 06:00–10:00 UTC), `long` for a Google request above 200K prompt tokens — so the §4 tiers apply per bucket;
`billing`
is `plan` when the file says so (a Codex turn on a ChatGPT plan), the log line's own key (`api` when it has none), or `?` when the file cannot
tell — a Claude transcript never says how it was paid, so ask the user or their config. Older Gemini CLI `.json` sessions
are read like the `.jsonl` ones.

## 3. Let an app or script report its own calls

Any program appends one JSON line per request to `$XDG_DATA_HOME/ai-cost/usage.jsonl` (`~/.local/share/ai-cost/usage.jsonl`
by default, or the file named by `AI_COST_USAGE_LOG`). Required: `schema`, `at`, `provider`, `model`, and `tokens` or `cost`. Counters use the
provider's own names so the pricing below stays exact:

```json
{"schema":1,"at":"2026-09-20T10:00:00Z","provider":"openai","model":"gpt-5.5","source":"my-app",
 "tokens":{"input":1200,"cached_input":800,"output":300},"cost":0.0123,"ref":"job-42","branch":"feat/x","tags":["ci"]}
```

Counters per provider: anthropic `input, output, cache_read, cache_write_5m, cache_write_1h` (`cache_write_unsplit`
when the response does not say which TTL), `web_search` (searches); openai and OpenAI-compatible APIs `input, cached_input, output`; google
`prompt, cached, output, thoughts`; deepseek `cache_hit, cache_miss, output`; anything else `input, output`. Only the
counters the provider's formula reads — an `input` on a deepseek line would be priced at zero, so the package makes
such a line a counted skip. `cost` is USD; a `currency` other than `USD` is a counted skip; `billing` (`api` |
`subscription`) says how it was paid. The package checks the counters against the model's own price shape
when its prices file lists the model (a user-added DeepSeek model with token tiers reads `input` / `cached_input`);
the snippet knows only the built-in formulas. Python one-liner after a call: `open(log, "a", encoding="utf-8").write(json.dumps({...}) + "\n")`. When the app keeps
raw responses instead, map their usage blocks with the table in §1.

## 4. Price it

Take the list price per million tokens from the provider's pricing page (fetch it if you can; otherwise ask the user
for the numbers or use the ones they keep in a `prices.json`). Apply the tiers the API bills:

| provider | formula per row |
|---|---|
| anthropic | `input×in + output×out + cache_read×cache_read_rate + cache_write_5m×(1.25·in) + cache_write_1h×(2·in) + web_search×(search_rate ÷ 1000)` (`cache_write_unsplit`, the legacy counter, at the TTL the user's config names — the package default `cache_ttl_default` is `1h`, so 2·in unless they set `5m`; `search_rate` is the price per 1000 web searches) |
| openai / OpenAI-compatible | `max(0, input − cached_input)×in + cached_input×cached_rate + output×out` (`cached_input` is part of `input`; never below zero) |
| xai from Grok Build | the same formula; `output` already holds the reasoning tokens; a bucket's `cli_cost_usd` is the CLI's own estimate — report it as the real charge when the user trusts the CLI (the package default), the formula as the API-equivalent |
| google | `max(0, prompt − cached)×in + cached×cached_rate + (output + thoughts)×out` (long-context tiers above the provider's threshold) |
| deepseek | `cache_miss×in + cache_hit×cached_rate + output×out`, peak / off-peak by UTC hour when the page says so |
| any row with `cost` | that cost is the real charge; the formula gives the API-equivalent |

Report two numbers per provider and model: **real** (subscription usage → 0 plus the plan's monthly fee prorated to the
window hours ÷ 730; API-key usage → the formula or the logged `cost`) and **API-equivalent** (every token at list
price as if no subscription existed). State the window, the sources found, and what was skipped or ambiguous
(unknown models, lines without a timestamp). Never round away small numbers: print 4 decimals for USD.

## 5. With the package instead (optional)

```bash
pipx install ai-costs           # the PyPI name is ai-costs, the command is ai-cost; or the single-file build from the GitHub release
ai-cost report --since 2026-09-20T08:00Z --until 2026-09-20T18:00Z      # real / API-only / vendor quote
ai-cost log --from-response resp.json --provider openai --ref job-42     # apps: one line per request
ai-cost report --attribute "feature=feat/x|src/x/"                      # who paid for what
ai-cost report --project /path/to/project --hours 24                    # every source scoped to one working directory
ai-cost daily && ai-cost install --schedule-reports                      # yesterday's global + per-project files, every morning
ai-cost reconcile --provider xai --usd 156.11 --hours 24                # the local count vs the provider's console
ai-cost prices check                                                     # list prices vs the vendors' pages
```

The package reads the same sources as §1, keeps a price registry with the URL of every number, and answers with the
same two columns plus a vendor quote. Everything it does is offline except the optional price-drift check.
