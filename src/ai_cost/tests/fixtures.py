"""Synthetic sources in a temporary HOME: the shapes the real files have, with numbers chosen to be checked by hand."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import Config, Paths, PriceBook, builtin_config, load_config, load_pricebook, parse_config
from ..models import Window
from ..timeutil import iso

BASE = datetime(2026, 9, 19, 15, 0, tzinfo=timezone.utc)  # a Saturday: DeepSeek off-peak
WINDOW = Window(BASE - timedelta(minutes=1), BASE + timedelta(hours=1))


TEST_SUBSCRIPTIONS = [  # the plans the tests reason about; the public package ships none (ADR-0004, decision 6)
    {"plan": "claude-max-20x", "seats": 1, "covers": ["anthropic"], "attribution": "time"},
    {"plan": "chatgpt-business", "seats": 1, "covers": ["openai"], "attribution": "time"},
    {"plan": "copilot-pro", "seats": 1, "covers": ["github-copilot"], "attribution": "time"},
    {"plan": "github-free", "seats": 1, "covers": ["github-actions"], "attribution": "time"},
]


def with_test_plans(raw: dict[str, Any]) -> dict[str, Any]:
    """A config dict as a configured user's: the test plans in place of the empty shipped list, Claude on a plan."""
    raw["subscriptions"] = json.loads(json.dumps(TEST_SUBSCRIPTIONS))
    raw.setdefault("providers", {}).setdefault("anthropic", {})["billing"] = "subscription"
    return raw


def paths_in(tmp: Path) -> Paths:
    """A ``Paths`` whose every root lives under ``tmp``."""
    return Paths(
        claude_home=tmp / ".claude",
        codex_home=tmp / ".codex",
        gemini_home=tmp / ".gemini",
        grok_home=tmp / ".grok",
        usage_log=tmp / "usage" / "usage.jsonl",
        state_dir=tmp / "state",
        reports_dir=tmp / "reports",
        user_config_dir=tmp / "cfg",
        offline=True,
    )


def defaults(paths: Paths) -> tuple[Config, PriceBook]:
    """Built-in config and prices (no user files under ``tmp``), with the test plans where the package ships none."""
    config = load_config(paths)
    if not config.subscriptions:
        configured = parse_config(with_test_plans(builtin_config()), "test")
        config = replace(
            config, subscriptions=configured.subscriptions, billing_rules=configured.billing_rules
        )
    return config, load_pricebook(paths)


def _assistant(
    message_id: str,
    minutes: int,
    usage: Any,  # any shape on purpose: malformed blocks are fixtures too
    model: str = "claude-fable-5-1",
    branch: str | None = None,
    tools: Sequence[Mapping[str, Any]] = (),
) -> str:
    """One transcript entry; ``tools`` become ``tool_use`` content blocks, ``branch`` the entry's ``gitBranch``."""
    record: dict[str, Any] = {
        "type": "assistant",
        "timestamp": iso(BASE + timedelta(minutes=minutes)),
        "message": {"id": message_id, "model": model, "usage": usage},
    }
    if branch is not None:
        record["gitBranch"] = branch
    if tools:
        record["message"]["content"] = [{"type": "tool_use", "name": "Bash", "input": dict(t)} for t in tools]
    return json.dumps(record)


USAGE_SMALL = {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 500_000}


def write_attribution_session(paths: Paths, project: Path) -> Path:
    """A second session whose turns touch different PRs: the ADR-0003 cases, one message each."""
    from ..collectors.claude import project_dir

    root = project_dir(paths.claude_home, project)
    root.mkdir(parents=True, exist_ok=True)
    session = root / "sess-attr.jsonl"
    streamed = _assistant("a4", 4, USAGE_SMALL, tools=[{"file_path": "/w/pkg/cost/y.py"}])
    lines = [
        _assistant(
            "a1", 1, USAGE_SMALL, tools=[{"command": "sed -i x pkg/cost/x.py && git add pkg/cost/x.py"}]
        ),
        _assistant("a2", 2, USAGE_SMALL, tools=[{"command": "cp ws-d/a b; ls lint-tree/c"}]),
        _assistant("a3", 3, USAGE_SMALL),
        _assistant("a4", 4, USAGE_SMALL),
        streamed,
        _assistant("a5", 5, USAGE_SMALL, branch="feat/lint", tools=[{"file_path": "/w/pkg/cost/z.py"}]),
        _assistant(
            "a6", 6, USAGE_SMALL, branch="HEAD", tools=[{"command": "bash lint-tree/scripts/lint.sh"}]
        ),
    ]
    session.write_text("\n".join(lines) + "\n")
    return session


def _session_records() -> list[str]:
    """The lines of the fixture session: a streamed duplicate, a 5m cache write with web search, an unsplit legacy
    record, a truncated line, a non-JSON line, two zero-token turns and a record without a timestamp."""
    first = _assistant(
        "m1",
        1,
        {
            "input_tokens": 1000,
            "output_tokens": 2000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 100_000,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 100_000},
        },
    )
    second = {
        "input_tokens": 0,
        "output_tokens": 500,
        "cache_creation_input_tokens": 40_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 40_000, "ephemeral_1h_input_tokens": 0},
        "server_tool_use": {"web_search_requests": 2},
    }
    return [
        json.dumps({"type": "user", "timestamp": iso(BASE)}),
        first,
        first,
        _assistant("m2", 30, second),
        _assistant("m3", 40, {"input_tokens": 10, "output_tokens": 10, "cache_creation_input_tokens": 1000}),
        '{"type":"assistant","message":{"usage":{"input_tokens":1',  # a truncated record
        "garbage that is not even an object",
        _assistant("m5", 46, {"input_tokens": 0, "output_tokens": 0}, model="<synthetic>"),
        _assistant("m6", 55, {"input_tokens": 0, "output_tokens": 0}, model="<synthetic>"),
        _assistant("m4", 45, {"input_tokens": 1, "output_tokens": 1}).replace('"timestamp"', '"stamp"'),
    ]


def write_recent_claude_session(paths: Paths, project: Path, messages: int = 3) -> Path:
    """A session whose messages finished in the last hour: rows inside the doctor's 24 h window."""
    from ..collectors.claude import project_dir

    root = project_dir(paths.claude_home, project)
    root.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc)
    lines = []
    for n in range(messages):
        record = json.loads(_assistant(f"recent-{n}", 0, USAGE_SMALL))
        record["timestamp"] = iso(end - timedelta(minutes=10 * (n + 1)))
        record["cwd"] = str(project)
        lines.append(json.dumps(record))
    path = root / "sess-recent.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def write_claude_session(paths: Paths, project: Path) -> Path:
    """One session (``_session_records``) and a subagent transcript under it."""
    from ..collectors.claude import project_dir

    root = project_dir(paths.claude_home, project)
    root.mkdir(parents=True)
    session = root / "sess-1.jsonl"
    session.write_text("\n".join(_session_records()) + "\n")
    sub = root / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text(
        _assistant("s1", 50, {"input_tokens": 100_000, "output_tokens": 1000}, model="claude-sonnet-5") + "\n"
    )
    return session


def _event(stamp: datetime, inp: int, out: int, cached: int = 0, plan: str = "", limits: bool = True) -> str:
    """One ``token_count`` event; ``limits=False`` writes ``rate_limits: null``, as the Codex app does."""
    usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}
    rate_limits = {"limit_id": "codex" if plan else "premium", "plan_type": plan or None} if limits else None
    payload: dict[str, Any] = {
        "type": "token_count",
        "info": {"total_token_usage": usage, "last_token_usage": usage},
        "rate_limits": rate_limits,
    }
    return json.dumps({"timestamp": iso(stamp), "type": "event_msg", "payload": payload})


def write_rollout(
    paths: Paths,
    day: str,
    session: str,
    started: datetime,
    model: str,
    events: list[tuple[datetime, int, int, int]],
    switch_to: str | None = None,
    branch: str = "",
    plan: str = "",
    originator: str = "codex_exec",
    limits: bool = True,
) -> Path:
    """A Codex rollout with per-turn ``token_count`` events; ``switch_to`` changes the model before the last one.

    ``plan`` names the ChatGPT plan the events report (``team``); empty = the API key or unknown. ``originator`` is the
    program the header names (``codex_exec``; the Codex app writes ``codex_work_desktop``); empty leaves it out.
    ``limits=False`` writes every event with ``rate_limits: null`` (a Codex app session, 2026-09-25).
    """
    folder = paths.codex_home / "sessions" / day[:4] / day[5:7] / day[8:10]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{started.strftime('%Y-%m-%dT%H-%M-%S')}-{session}.jsonl"
    lines = [
        json.dumps(
            {
                "timestamp": iso(started),
                "type": "session_meta",
                "payload": {
                    "session_id": session,
                    **({"originator": originator} if originator else {}),
                    **({"git": {"branch": branch}} if branch else {}),
                },
            }
        ),
        json.dumps({"timestamp": iso(started), "type": "turn_context", "payload": {"model": model}}),
    ]
    lines += [_event(stamp, inp, out, cached, plan, limits) for stamp, inp, out, cached in events]
    if switch_to:
        lines.insert(
            len(lines) - 1,
            json.dumps({"timestamp": iso(started), "type": "turn_context", "payload": {"model": switch_to}}),
        )
    path.write_text("\n".join(lines) + "\n")
    return path


_ROLLOUTS = (  # day, session, start (minutes from BASE), branch, plan, turns as (minutes, input, output, cached)
    ("2026-09-19", "sid-plan", 5, "", "team", [(5, 500_000, 9_500, 400_000)]),
    ("2026-09-19", "sid-api", 6, "feat/api", "", [(6, 60_000, 400, 0)]),
    ("2026-09-19", "sid-key-noledger", 7, "", "", [(7, 10_000, 100, 0)]),
    ("2026-09-18", "sid-resumed", -19 * 60, "", "", [(-19 * 60, 100_000, 1000, 0), (20, 5_000, 50, 0)]),
    ("2026-09-19", "sid-straddle", 30, "", "", [(30, 1_000, 10, 0), (120, 999_000, 9_990, 0)]),
)


def write_codex(paths: Paths) -> None:
    """Rollouts: one on a plan, two on the key (one the ledger never saw), a resumed one, a straddling one."""
    for day, session, start, branch, plan, turns in _ROLLOUTS:
        events = [(BASE + timedelta(minutes=m), i, o, c) for m, i, o, c in turns]
        started = BASE + timedelta(minutes=start)
        write_rollout(paths, day, session, started, "gpt-6-astra", events, branch=branch, plan=plan)


GROK_TICKS = 3_564_980_000  # the CLI's 0.356498 USD for the turn below (grok-4.7, a real run of 2026-09-21)
GROK_CWD = "/Users/me/Projects/app"


def write_grok(paths: Paths, cwd: str = GROK_CWD, minutes: int = 5, session: str = "grok-sess-1") -> Path:
    """One Grok Build session of one turn under ``cwd``: the counters a real review run reported."""
    usage = {
        "inputTokens": 165_943,
        "outputTokens": 173,
        "cachedReadTokens": 5_248,
        "cacheCreationTokens": 0,
        "reasoningTokens": 5_241,
        "totalTokens": 166_116,
        "modelCalls": 1,
        "costUsdTicks": GROK_TICKS,
    }
    ended = iso(BASE + timedelta(minutes=minutes))
    turn = {
        **usage,
        "turnNumber": 1,
        "endedAt": ended,
        "primaryModelId": "grok-4.7",
        "modelUsage": {"grok-4.7": usage},
    }
    folder = paths.grok_home / "sessions" / quote(cwd, safe="") / session
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "usage.json"
    path.write_text(json.dumps({"sessionId": session, "updatedAt": ended, "session": turn, "turns": [turn]}))
    return path


def fake_gh(tmp: Path) -> Path:
    """A ``gh`` stub on PATH: one private repo, three runs (in window with /timing, out of window, in window without /timing), two reviews."""
    folder = tmp / "bin"
    folder.mkdir(exist_ok=True)
    script = folder / "gh"
    in_a, in_b = iso(BASE + timedelta(minutes=2)), iso(BASE + timedelta(minutes=12))
    out_a, out_b = iso(BASE - timedelta(days=1)), iso(BASE - timedelta(days=1) + timedelta(minutes=30))
    late_a, late_b = iso(BASE + timedelta(minutes=40)), iso(BASE + timedelta(minutes=42))
    script.write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '  *"repo view"*) echo true ;;\n'
        f'  *"run list"*) echo \'[{{"databaseId":1,"createdAt":"{in_a}","updatedAt":"{in_b}","status":"completed"}},{{"databaseId":2,"createdAt":"{out_a}","updatedAt":"{out_b}","status":"completed"}},{{"databaseId":3,"createdAt":"{late_a}","updatedAt":"{late_b}","status":"completed"}}]\' ;;\n'
        '  *"runs/1/timing"*) echo \'{"billable":{"UBUNTU":{"total_ms":210000,"jobs":2},"MACOS":{"total_ms":60000,"jobs":1}}}\' ;;\n'
        '  *"runs/2/timing"*) exit 1 ;;\n'
        '  *"runs/3/timing"*) exit 1 ;;\n'
        '  *"pr list"*) echo \'[{"number":7}]\' ;;\n'
        f'  *"pulls/7/reviews"*) echo \'["{iso(BASE + timedelta(minutes=3))}","{iso(BASE - timedelta(days=2))}",null]\' ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    script.chmod(0o755)
    return folder
