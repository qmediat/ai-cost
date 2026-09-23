"""Grok Build sessions: per-turn rows per model, the CLI's counters and cost ticks, windowing, counted skips."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..collectors import BUILTIN
from ..collectors.grok_build import TICKS_PER_USD, GrokBuildSource, collect_grok_build
from ..groups import real_group
from ..models import Billing, RowKind, Window
from ..pricing import price
from ..timeutil import iso
from .fixtures import BASE, WINDOW, defaults, paths_in

CWD = "/Users/me/Projects/app"


def _usage(
    model: str, inputs: int, cached: int, out: int, reasoning: int, calls: int, ticks: int
) -> dict[str, Any]:
    return {
        "inputTokens": inputs,
        "outputTokens": out,
        "cachedReadTokens": cached,
        "cacheCreationTokens": 0,
        "reasoningTokens": reasoning,
        "totalTokens": inputs + out,
        "modelCalls": calls,
        "costUsdTicks": ticks,
        "primaryModelId": model,
    }


def _turn(number: int, minutes: int, usages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    first = next(iter(usages.values()))
    return {
        **first,
        "turnNumber": number,
        "endedAt": iso(BASE + timedelta(minutes=minutes)),
        "modelUsage": usages,
    }


def _write(home: Path, cwd: str, session: str, document: Any) -> Path:
    folder = home / ".grok" / "sessions" / quote(cwd, safe="") / session
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "usage.json"
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return path


def test_one_row_per_turn_and_model_with_the_counters_the_pricer_reads(tmp_path: Path) -> None:
    turn = _turn(
        1,
        5,
        {
            "grok-4.7": _usage("grok-4.7", 165_943, 5_248, 173, 5_241, 1, 3_564_980_000),
            "grok-4.6": _usage("grok-4.6", 1_000, 0, 10, 0, 2, 0),
        },
    )
    _write(tmp_path, CWD, "sess-1", {"sessionId": "sess-1", "session": turn, "turns": [turn]})
    collected = collect_grok_build(tmp_path / ".grok", WINDOW, Billing.API)
    assert not collected.skipped and [r.model for r in collected.rows] == ["grok-4.7", "grok-4.6"]
    row = collected.rows[0]
    assert row.tokens.input == 165_943 and row.tokens.cached_input == 5_248, "input includes the cached reads"
    assert row.tokens.output == 173 + 5_241, "reasoning is billed as output"
    assert row.tokens.requests == 1 and row.cost_reported == 3_564_980_000 / TICKS_PER_USD
    assert row.scope.workspace == CWD and row.ref == "sess-1" and row.kind is RowKind.SESSION
    assert row.billing is Billing.API and row.source == "grok-build" and row.at == BASE + timedelta(minutes=5)
    assert (
        collected.rows[1].cost_reported is None
    ), "zero ticks: the CLI did not price it, the list price applies"


def test_turns_outside_the_window_and_files_older_than_it_are_not_counted(tmp_path: Path) -> None:
    inside = _turn(1, 5, {"grok-4.7": _usage("grok-4.7", 10, 0, 1, 0, 1, 100)})
    after = _turn(2, 200, {"grok-4.7": _usage("grok-4.7", 20, 0, 2, 0, 1, 100)})
    _write(tmp_path, CWD, "s", {"sessionId": "s", "turns": [inside, after]})
    old = _write(tmp_path, CWD, "old", {"sessionId": "old", "turns": [inside]})
    stale = (WINDOW.start - timedelta(hours=2)).timestamp()
    os.utime(old, (stale, stale))
    rows = collect_grok_build(tmp_path / ".grok", WINDOW).rows
    assert [(r.ref, r.tokens.input) for r in rows] == [("s", 10)], "the later turn and the stale file are out"


def test_a_file_without_turns_counts_its_session_block_once_at_updated_at(tmp_path: Path) -> None:
    session = _usage("grok-4.7", 50, 5, 5, 0, 1, 10)
    stamp = iso(BASE + timedelta(minutes=7))
    _write(tmp_path, CWD, "s", {"sessionId": "s", "updatedAt": stamp, "session": session, "turns": []})
    rows = collect_grok_build(tmp_path / ".grok", WINDOW).rows
    assert len(rows) == 1 and rows[0].at == BASE + timedelta(minutes=7) and rows[0].tokens.input == 50
    _write(tmp_path, CWD, "empty", {"sessionId": "empty", "turns": []})
    assert len(collect_grok_build(tmp_path / ".grok", WINDOW).rows) == 1, "nothing to count is nothing"
    path = _write(tmp_path, CWD, "odd", {"sessionId": "odd", "updatedAt": "not a time", "session": session})
    when = (BASE + timedelta(minutes=9)).timestamp()
    os.utime(path, (when, when))
    rows = collect_grok_build(tmp_path / ".grok", WINDOW).rows
    assert [r.at for r in rows if r.ref == "odd"] == [
        BASE + timedelta(minutes=9)
    ], "a stamp that is none: mtime"


def test_bad_turns_and_files_are_counted_skips_never_silent_zeros(tmp_path: Path) -> None:
    good = _turn(1, 5, {"grok-4.7": _usage("grok-4.7", 10, 0, 1, 0, 1, 100)})
    no_stamp = {**good, "endedAt": None}
    inverted = _turn(3, 6, {"grok-4.7": _usage("grok-4.7", 10, 20, 1, 0, 1, 100)})
    bad_counter = _turn(4, 6, {"grok-4.7": {**_usage("grok-4.7", 10, 0, 1, 0, 1, 100), "outputTokens": 1.5}})
    _write(tmp_path, CWD, "s", {"sessionId": "s", "turns": [good, no_stamp, inverted, bad_counter, [1]]})
    _write(tmp_path, CWD, "list", {"sessionId": "list", "turns": "none"})
    _write(tmp_path, CWD, "array", [1, 2])
    _write(tmp_path, CWD, "torn", "{not json")
    collected = collect_grok_build(tmp_path / ".grok", WINDOW)
    assert [r.tokens.input for r in collected.rows] == [10], "the good turn counts, the four bad ones do not"
    reasons = sorted(s.reason for s in collected.skipped)
    assert sum("turn 2: endedAt" in r for r in reasons) == 1
    assert sum("turn 3: cachedReadTokens 20 exceed inputTokens 10" in r for r in reasons) == 1
    assert sum("turn 4: outputTokens must be a whole number" in r for r in reasons) == 1
    assert sum("turn 5: turn 5 must be an object" in r for r in reasons) == 1
    assert sum(r.startswith("unusable") for r in reasons) == 3, "turns not a list, a JSON array, a torn file"
    assert all(s.source == "grok-build" for s in collected.skipped)


def test_the_xai_rule_trusts_the_cli_estimate_only_when_the_config_says_so(tmp_path: Path) -> None:
    from dataclasses import replace

    ticks = 3_564_980_000
    turn = _turn(1, 5, {"grok-4.7": _usage("grok-4.7", 165_943, 5_248, 173, 5_241, 1, ticks)})
    _write(tmp_path, CWD, "s", {"sessionId": "s", "turns": [turn]})
    paths = paths_in(tmp_path)
    config, book = defaults(paths)
    rows = collect_grok_build(paths.grok_home, WINDOW, Billing.API).rows
    trusted = real_group(rows, book, config, WINDOW)
    assert config.xai_trust_cli_cost and abs(trusted.cash_usd - ticks / TICKS_PER_USD) < 1e-9
    assert any("CLI-reported" in note for line in trusted.usage for note in line.notes)
    at_list = real_group(rows, book, replace(config, xai_trust_cli_cost=False), WINDOW)
    expected = price(rows[0], book, config).usd
    assert (
        abs(at_list.cash_usd - expected) < 1e-9 and abs(expected - 0.3565) < 0.0005
    ), "the list price of grok-4.7"


def test_the_source_is_built_in_and_follows_the_xai_billing_rule(tmp_path: Path) -> None:
    from ..config import builtin_config, parse_config
    from ..ops import ReportRequest
    from ..plugins import Context

    assert any(isinstance(source, GrokBuildSource) for source in BUILTIN)
    turn = _turn(1, 5, {"grok-4.7": _usage("grok-4.7", 10, 0, 1, 0, 1, 100)})
    _write(tmp_path, CWD, "s", {"sessionId": "s", "turns": [turn]})
    paths = paths_in(tmp_path)
    for rule, expected in (
        ("api", Billing.API),
        ("subscription", Billing.SUBSCRIPTION),
        ("mixed", Billing.UNKNOWN),
    ):
        raw = builtin_config()
        raw["providers"]["xai"]["billing"] = rule
        config = parse_config(raw, "cfg")
        assert Billing.rule(config.xai_billing) is expected
        ctx = Context(paths, config, WINDOW, ReportRequest())
        assert [r.billing for r in GrokBuildSource().collect(ctx).rows] == [expected], rule
    assert parse_config(builtin_config(), "cfg").xai_billing == "api", "the shipped rule is read, not dead"
    assert Window(BASE, BASE + timedelta(hours=1)).contains(BASE)
