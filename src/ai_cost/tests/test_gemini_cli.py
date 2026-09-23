"""Gemini CLI sessions: the jsonl and the older json shape, streamed duplicates, windowing, counted skips."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from ..collectors.gemini_cli import collect_gemini_cli
from ..models import Billing, Window
from ..timeutil import iso
from .fixtures import BASE, WINDOW


def _gemini(
    message_id: str, minutes: int, tokens: object, model: str = "gemini-3.8-flash"
) -> dict[str, object]:
    return {
        "id": message_id,
        "timestamp": iso(BASE + timedelta(minutes=minutes)),
        "type": "gemini",
        "tokens": tokens,
    }


def _write(chats: Path, name: str, records: list[object]) -> Path:
    chats.mkdir(parents=True, exist_ok=True)
    path = chats / name
    if name.endswith(".jsonl"):
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
    else:
        path.write_text(json.dumps({"sessionId": "s", "messages": records}))
    return path


def test_jsonl_sessions_count_each_message_once_and_price_thoughts_as_output(tmp_path: Path) -> None:
    chats = tmp_path / ".gemini" / "tmp" / "my-project" / "chats"
    first = _gemini(
        "g1", 5, {"input": 1000, "output": 10, "cached": 400, "thoughts": 90, "tool": 0, "total": 1100}
    )
    first["model"] = "gemini-3.8-flash"
    records = [
        {"sessionId": "abc", "projectHash": "h", "startTime": iso(BASE), "kind": "main"},
        {"id": "u1", "timestamp": iso(BASE), "type": "user", "content": "hi"},
        {"$set": {"lastUpdated": iso(BASE)}},
        first,
        first,  # the streamed update repeats the same id and counts
        {
            **_gemini("g2", 200, {"input": 5, "output": 1, "cached": 0, "thoughts": 0}),
            "model": "gemini-3.8-flash",
        },
    ]
    _write(chats, "session-2026-09-19T15-00-abc.jsonl", records)
    collected = collect_gemini_cli(tmp_path / ".gemini", WINDOW)
    assert len(collected.rows) == 1, "one row for g1 (counted once); g2 is after the window"
    row = collected.rows[0]
    assert row.tokens.prompt == 1000 and row.tokens.cached == 400 and row.tokens.output == 100
    assert row.ref == "my-project/abc" and row.scope.workspace == "my-project"
    assert row.billing is Billing.UNKNOWN and row.model == "gemini-3.8-flash"
    assert (
        collect_gemini_cli(tmp_path / ".gemini", None).rows[1].tokens.prompt == 5
    ), "no window: every message"


def test_the_older_json_shape_and_malformed_files_are_handled(tmp_path: Path) -> None:
    chats = tmp_path / ".gemini" / "tmp" / "old" / "chats"
    _write(
        chats,
        "session-2026-05-01T11-54-old1.json",
        [_gemini("o1", 7, {"input": 3, "output": 2, "cached": 1})],
    )
    _write(
        chats,
        "session-2026-05-01T11-55-bad1.jsonl",
        [_gemini("b1", 8, "oops"), _gemini("b2", 9, {"input": 1})],
    )
    (chats / "session-2026-05-01T11-56-bad2.jsonl").write_text("{not json\n")
    (chats / "session-2026-05-01T11-57-bad3.json").write_text(json.dumps({"messages": "none"}))
    collected = collect_gemini_cli(tmp_path / ".gemini", Window(BASE, BASE + timedelta(hours=1)))
    assert sorted(r.ref for r in collected.rows) == [
        "old/bad1",
        "old/s",
    ], "a .json document names its session"
    assert sum("tokens must be an object" in s.reason for s in collected.skipped) == 1
    assert sum("unusable" in s.reason for s in collected.skipped) == 1, "the .json without a messages list"
    assert (
        sum(s.reason == "line 1: not JSON" for s in collected.skipped) == 1
    ), "a bad .jsonl line, not the file"


def test_gemini_rows_are_paid_as_the_google_billing_rule_says(tmp_path: Path) -> None:
    from ..config import builtin_config, parse_config
    from ..models import Billing

    chats = tmp_path / ".gemini" / "tmp" / "p" / "chats"
    _write(chats, "session-2026-05-01T11-54-s1.jsonl", [_gemini("g1", 7, {"input": 3, "output": 2})])
    window = Window(BASE, BASE + timedelta(hours=1))
    for rule, expected in (
        ("api", Billing.API),
        ("subscription", Billing.SUBSCRIPTION),
        ("mixed", Billing.UNKNOWN),
    ):
        raw = builtin_config()
        raw["providers"]["google"]["billing"] = rule
        billing = Billing.rule(parse_config(raw, "cfg").google_billing)
        rows = collect_gemini_cli(tmp_path / ".gemini", window, billing).rows
        assert [r.billing for r in rows] == [expected], rule
    assert parse_config(builtin_config(), "cfg").google_billing == "api", "the shipped rule is read, not dead"


def test_a_gemini_row_paid_by_a_plan_is_the_plans_not_cash_at_list(tmp_path: Path) -> None:
    from ..groups import real_group
    from ..models import Billing
    from .fixtures import defaults, paths_in

    chats = tmp_path / ".gemini" / "tmp" / "p" / "chats"
    _write(chats, "session-2026-05-01T11-54-s1.jsonl", [_gemini("g1", 7, {"input": 3000, "output": 2000})])
    window = Window(BASE, BASE + timedelta(hours=1))
    config, book = defaults(paths_in(tmp_path))
    priced = "gemini-3.8-flash"
    plan_rows = [
        replace(r, model=priced)
        for r in collect_gemini_cli(tmp_path / ".gemini", window, Billing.SUBSCRIPTION).rows
    ]
    api_rows = [
        replace(r, model=priced) for r in collect_gemini_cli(tmp_path / ".gemini", window, Billing.API).rows
    ]
    assert real_group(plan_rows, book, config, window).cash_usd == 0.0, "a plan row is the plan's"
    assert real_group(api_rows, book, config, window).cash_usd > 0.0, "an API row is cash at list"
