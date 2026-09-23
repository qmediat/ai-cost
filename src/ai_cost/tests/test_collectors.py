"""Every collector against the synthetic sources: what is counted, what is windowed, what is skipped."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from ..collectors import collect_claude, collect_codex, collect_github, find_session_files
from ..collectors.claude import project_dir
from ..models import Billing, Window
from ..timeutil import iso
from .fixtures import (
    BASE,
    USAGE_SMALL,
    WINDOW,
    _assistant,
    fake_gh,
    paths_in,
    write_claude_session,
    write_codex,
    write_rollout,
)


def test_session_and_subagent_transcripts_are_found(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    files = find_session_files(paths.claude_home, tmp_path / "proj", "sess-1", False)
    assert [name for name, _ in files] == ["sess-1", "sess-1"]


def test_session_latest_picks_the_newest_transcript(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    files = find_session_files(paths.claude_home, tmp_path / "proj", "latest", False)
    assert files and files[0][0] == "sess-1"


def test_claude_dedupes_streamed_messages_and_counts_malformed_lines(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    collected = collect_claude(
        find_session_files(paths.claude_home, tmp_path / "proj", "sess-1", False), None
    )
    assert len(collected.rows) == 4, "the <synthetic> zero-token turn is no row: nothing to price"
    assert [s.reason for s in collected.skipped] == [
        "line 6: not JSON",
        "line 7: not JSON",
        "line 10: no timestamp",
    ]
    assert (
        collected.span is not None
        and collected.span.start == BASE
        and collected.span.end
        == BASE + timedelta(minutes=55)  # the zero-token turn at 55 still widens the span
    )


def test_claude_cache_split_and_unsplit_records(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    rows = collect_claude(
        find_session_files(paths.claude_home, tmp_path / "proj", "sess-1", False), None
    ).rows
    first = next(r for r in rows if r.tokens.output == 2000)
    assert first.tokens.cache_write_1h == 100_000 and first.tokens.cache_write_unsplit == 0
    legacy = next(r for r in rows if r.tokens.output == 10)
    assert legacy.tokens.cache_write_unsplit == 1000
    searched = next(r for r in rows if r.tokens.web_search)
    assert searched.tokens.cache_write_5m == 40_000 and searched.tokens.web_search == 2


def test_codex_rollouts_are_windowed_per_turn(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_codex(paths)
    rows = {r.ref: r for r in collect_codex(paths.codex_home, WINDOW, "gpt-6-astra").rows}
    assert set(rows) == {"sid-plan", "sid-api", "sid-key-noledger", "sid-resumed", "sid-straddle"}
    assert rows["sid-plan"].billing is Billing.SUBSCRIPTION and rows["sid-plan"].tokens.input == 500_000
    assert (
        rows["sid-api"].billing is Billing.UNKNOWN
    ), "no plan in the rollout, no rule: unknown, never guessed"
    assert rows["sid-api"].cost_reported is None and rows["sid-api"].share == 1.0
    ruled = collect_codex(paths.codex_home, WINDOW, "gpt-6-astra", Billing.API).rows
    assert {r.billing for r in ruled if r.ref == "sid-api"} == {
        Billing.API
    }, "the configured rule fills the gap"
    assert (
        rows["sid-resumed"].tokens.input == 5_000
    ), "a session resumed from before the window counts only its in-window turn"
    assert rows["sid-straddle"].tokens.input == 1_000, "turns after --until are excluded"


def test_codex_files_last_written_before_the_window_are_skipped_by_mtime(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_codex(paths)
    stale = next((paths.codex_home / "sessions").glob("*/*/*/rollout-*sid-resumed.jsonl"))
    old = (BASE - timedelta(days=30)).timestamp()
    os.utime(stale, (old, old))
    assert "sid-resumed" not in {r.ref for r in collect_codex(paths.codex_home, WINDOW, "gpt-6-astra").rows}


def test_codex_model_switch_prices_each_turn_with_its_model(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_rollout(
        paths,
        "2026-09-19",
        "sid-switch",
        BASE + timedelta(minutes=8),
        "gpt-6-astra",
        [(BASE + timedelta(minutes=8), 1000, 10, 0), (BASE + timedelta(minutes=9), 2000, 20, 0)],
        switch_to="gpt-5.5",
    )
    rows = {
        r.model: r
        for r in collect_codex(paths.codex_home, WINDOW, "gpt-6-astra").rows
        if r.ref == "sid-switch"
    }
    assert rows["gpt-6-astra"].tokens.input == 1000 and rows["gpt-5.5"].tokens.input == 2000


def test_session_by_path_brings_its_subagents(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    session = write_claude_session(paths, tmp_path / "proj")
    files = find_session_files(paths.claude_home, None, str(session), False)
    assert [p.name for _, p in files] == ["sess-1.jsonl", "agent-a.jsonl"]


def test_codex_non_object_lines_are_counted(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_codex(paths)
    rollout = next(paths.codex_home.glob("sessions/*/*/*/rollout-*sid-plan.jsonl"))
    rollout.write_text(rollout.read_text() + '["token_count"]\n')
    collected = collect_codex(paths.codex_home, WINDOW, "gpt-6-astra")
    assert any("not an object" in s.reason for s in collected.skipped)


def test_github_failed_queries_are_skipped_not_zero(tmp_path: Path) -> None:
    """``gh`` that cannot list runs or PRs must not produce zero-cost rows."""
    folder = tmp_path / "bin"
    folder.mkdir()
    stub = folder / "gh"
    stub.write_text('#!/bin/bash\ncase "$*" in *"repo view"*) echo true ;; *) exit 1 ;; esac\n')
    stub.chmod(0o755)
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{folder}{os.pathsep}{original}"
    try:
        collected = collect_github(["acme/private"], WINDOW, "linux")
    finally:
        os.environ["PATH"] = original
    assert collected.rows == []
    assert sorted(s.reason[:14] for s in collected.skipped) == ["gh pr list fai", "gh run list fa"]


def test_github_misshapen_json_is_unknown_not_zero(tmp_path: Path) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    stub = folder / "gh"
    stub.write_text(
        '#!/bin/bash\ncase "$*" in *"repo view"*) echo true ;; *"run list"*) echo \'{"runs":[]}\' ;; '
        '*"pr list"*) echo \'[{"id":7}]\' ;; *) exit 1 ;; esac\n'
    )
    stub.chmod(0o755)
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{folder}{os.pathsep}{original}"
    try:
        collected = collect_github(["acme/private"], WINDOW, "linux")
    finally:
        os.environ["PATH"] = original
    assert collected.rows == [] and len(collected.skipped) == 2


def test_github_failed_review_lookup_skips_the_repository(tmp_path: Path) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    stub = folder / "gh"
    stub.write_text(
        '#!/bin/bash\ncase "$*" in *"repo view"*) echo true ;; *"run list"*) echo "[]" ;; '
        '*"pr list"*) echo \'[{"number":7}]\' ;; *) exit 1 ;; esac\n'
    )
    stub.chmod(0o755)
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{folder}{os.pathsep}{original}"
    try:
        collected = collect_github(["acme/private"], WINDOW, "linux")
    finally:
        os.environ["PATH"] = original
    assert [r.model for r in collected.rows] == [
        "actions"
    ], "no Copilot row when one PR's reviews are unknown"
    assert any("gh pr list failed" in s.reason for s in collected.skipped)


def test_github_reviews_and_billable_minutes(tmp_path: Path) -> None:
    folder = fake_gh(tmp_path)
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{folder}{os.pathsep}{original}"
    try:
        collected = collect_github(["acme/private"], WINDOW, "windows")
    finally:
        os.environ["PATH"] = original
    actions = next(r for r in collected.rows if r.model == "actions")
    copilot = next(r for r in collected.rows if r.model == "copilot-code-review")
    assert copilot.tokens.reviews == 1, "only reviews submitted inside the window"
    assert actions.tokens.by_os == {"linux": 3.5, "macos": 1.0, "windows": 2.0} and actions.tokens.billable
    assert any("no /timing" in s.reason for s in collected.skipped)


def test_bad_usage_is_a_counted_skip(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    root = project_dir(paths.claude_home, tmp_path / "p3")
    root.mkdir(parents=True)
    lines = [
        _assistant("b1", 1, {"input_tokens": "oops", "output_tokens": 1}),
        _assistant("b2", 2, USAGE_SMALL),
    ]
    (root / "s.jsonl").write_text("\n".join(lines) + "\n")
    collected = collect_claude([("s", root / "s.jsonl")], None)
    assert len(collected.rows) == 1 and any("bad usage" in s.reason for s in collected.skipped)


def test_codex_rows_split_at_a_day_boundary(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    night = BASE.replace(hour=23, minute=50)
    events = [(night, 1000, 10, 0), (night + timedelta(minutes=20), 2000, 20, 0)]
    write_rollout(paths, "2026-09-19", "sid-midnight", night, "gpt-6-astra", events)
    window = Window(night - timedelta(minutes=1), night + timedelta(hours=1))
    rows = [r for r in collect_codex(paths.codex_home, window, "gpt-6-astra").rows if r.ref == "sid-midnight"]
    assert sorted(r.tokens.input for r in rows) == [
        1000,
        2000,
    ], "one row per model and day: a tariff never straddles"


def test_usage_blocks_that_are_not_objects_are_counted_skips(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    root = project_dir(paths.claude_home, tmp_path / "p4")
    root.mkdir(parents=True)
    lines = [
        _assistant("n1", 1, "oops"),
        _assistant("n2", 2, {"input_tokens": 1, "cache_creation": [1, 2]}),
        _assistant("n3", 3, {"input_tokens": 1, "server_tool_use": "x"}),
        _assistant("n5", 5, {"input_tokens": 1, "cache_creation": []}),  # falsy but not an object: still bad
        _assistant("n6", 6, 0),  # a present usage that is not an object, falsy or not, is bad data too
        _assistant("n4", 4, USAGE_SMALL),
    ]
    (root / "s.jsonl").write_text("\n".join(lines) + "\n")
    collected = collect_claude([("s", root / "s.jsonl")], None)
    assert len(collected.rows) == 1
    assert sum("bad usage" in s.reason for s in collected.skipped) == 5


def test_a_rollout_event_with_non_object_usage_is_a_counted_skip(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    at = BASE + timedelta(minutes=42)
    write_rollout(paths, "2026-09-19", "sid-bad-usage", at, "gpt-6-astra", [(at, 10, 1, 0)])
    rollout = next((paths.codex_home / "sessions").rglob("*sid-bad-usage*.jsonl"))
    bad = {
        "timestamp": iso(at),
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"last_token_usage": "x"}},
    }
    with rollout.open("a") as handle:
        handle.write(json.dumps(bad) + "\n")
    collected = collect_codex(paths.codex_home, WINDOW, "gpt-6-astra")
    assert "sid-bad-usage" in {r.ref for r in collected.rows}, "the good turn still counts"
    assert any(
        "last_token_usage must be an object" in s.reason and "sid-bad-usage" in s.path
        for s in collected.skipped
    )


def test_a_rollout_with_non_object_parts_is_a_counted_skip(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    at = BASE + timedelta(minutes=43)
    write_rollout(paths, "2026-09-19", "sid-bad-shape", at, "gpt-6-astra", [(at, 10, 1, 0)])
    rollout = next((paths.codex_home / "sessions").rglob("*sid-bad-shape*.jsonl"))
    with rollout.open("a") as handle:
        bad = {"timestamp": iso(at), "type": "event_msg", "payload": {"type": "token_count", "info": [1, 2]}}
        handle.write(json.dumps(bad) + "\n")
    collected = collect_codex(paths.codex_home, WINDOW, "gpt-6-astra")
    assert "sid-bad-shape" in {r.ref for r in collected.rows}, "the good turn still counts"
    assert any("info must be an object" in s.reason for s in collected.skipped)


def test_a_malformed_usage_record_still_widens_the_span(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    root = project_dir(paths.claude_home, tmp_path / "p5")
    root.mkdir(parents=True)
    lines = [_assistant("w1", 1, USAGE_SMALL), _assistant("w2", 70, "oops")]  # the bad one is the latest
    (root / "s.jsonl").write_text("\n".join(lines) + "\n")
    collected = collect_claude([("s", root / "s.jsonl")], None)
    assert collected.span is not None and collected.span.end == BASE + timedelta(minutes=70)


def test_a_non_object_git_block_costs_the_branch_not_the_usage(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    at = BASE + timedelta(minutes=44)
    write_rollout(paths, "2026-09-19", "sid-bad-git", at, "gpt-6-astra", [(at, 10, 1, 0)])
    rollout = next((paths.codex_home / "sessions").rglob("*sid-bad-git*.jsonl"))
    text = rollout.read_text().replace('"git": {', '"git": "none", "was": {', 1)
    rollout.write_text(text)
    rows = [r for r in collect_codex(paths.codex_home, WINDOW, "gpt-6-astra").rows if r.ref == "sid-bad-git"]
    assert rows and rows[0].scope.branch == "" and rows[0].tokens.input == 10


def test_a_bad_counter_in_one_turn_skips_that_event_only(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    inside, outside = BASE + timedelta(minutes=45), BASE + timedelta(hours=5)
    write_rollout(paths, "2026-09-19", "sid-late-bad", inside, "gpt-6-astra", [(inside, 10, 1, 0)])
    rollout = next((paths.codex_home / "sessions").rglob("*sid-late-bad*.jsonl"))
    late = {"type": "token_count", "info": {"last_token_usage": {"input_tokens": "many", "output_tokens": 1}}}
    with rollout.open("a") as handle:
        handle.write(json.dumps({"timestamp": iso(outside), "type": "event_msg", "payload": late}) + "\n")
    collected = collect_codex(paths.codex_home, WINDOW, "gpt-6-astra")
    rows = [r for r in collected.rows if r.ref == "sid-late-bad"]
    assert rows and rows[0].tokens.input == 10, "the in-window turn still counts; its cash is not discarded"
    assert any(
        "input_tokens" in s.reason and "sid-late-bad" in s.path for s in collected.skipped
    ), "the bad event is one counted skip"


def test_an_assistant_record_whose_message_is_not_an_object_is_a_counted_skip(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    root = project_dir(paths.claude_home, tmp_path / "p6")
    root.mkdir(parents=True)
    record = json.dumps(
        {"type": "assistant", "timestamp": iso(BASE), "message": "gone", "usage": {"input_tokens": 1}}
    )
    (root / "s.jsonl").write_text(record + "\n" + _assistant("ok1", 2, USAGE_SMALL) + "\n")
    collected = collect_claude([("s", root / "s.jsonl")], None)
    assert len(collected.rows) == 1 and any("message is not an object" in s.reason for s in collected.skipped)


def test_non_finite_boolean_or_huge_claude_counters_are_counted_skips(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    root = project_dir(paths.claude_home, tmp_path / "p7")
    root.mkdir(parents=True)
    lines = [
        _assistant("c1", 1, {"input_tokens": 1e400, "output_tokens": 1}),
        _assistant("c2", 2, {"input_tokens": True, "output_tokens": 1}),
        _assistant("c3", 3, {"input_tokens": 10**400, "output_tokens": 1}),
        _assistant("c4", 4, USAGE_SMALL),
    ]
    (root / "s.jsonl").write_text("\n".join(lines) + "\n")
    collected = collect_claude([("s", root / "s.jsonl")], None)
    assert len(collected.rows) == 1 and sum("bad usage" in s.reason for s in collected.skipped) == 3
    assert any("at most" in s.reason for s in collected.skipped), "a 400-digit count never reaches pricing"


def test_the_shares_of_a_weightless_session_still_sum_to_one(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    night = BASE.replace(hour=23, minute=50)
    turns = [(night, 0, 0, 5), (night + timedelta(minutes=20), 0, 0, 7)]  # cached tokens only: no weight
    write_rollout(paths, "2026-09-19", "sid-weightless", night, "gpt-6-astra", turns)
    window = Window(night - timedelta(minutes=1), night + timedelta(hours=1))
    rows = [
        r for r in collect_codex(paths.codex_home, window, "gpt-6-astra").rows if r.ref == "sid-weightless"
    ]
    assert (
        len(rows) == 2 and abs(sum(r.share for r in rows) - 1.0) < 1e-9
    ), "a per-session cost is charged once"


def test_a_broken_symlink_among_the_transcripts_is_a_counted_skip_not_a_traceback(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    root = write_claude_session(paths, tmp_path / "proj").parent
    (root / "gone.jsonl").symlink_to(tmp_path / "never-existed.jsonl")
    files = find_session_files(paths.claude_home, tmp_path / "proj", None, False)
    assert any(name == "gone" for name, _ in files), "listed: its fate is decided when it is read"
    collected = collect_claude(files, None)
    assert collected.rows and any("gone.jsonl" in s.path for s in collected.skipped)


def test_a_rollout_whose_only_turn_is_rejected_never_falls_back_to_its_total(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    started, later = BASE + timedelta(minutes=10), BASE + timedelta(hours=5)
    write_rollout(paths, "2026-09-19", "sid-only-bad", started, "gpt-6-astra", [])
    rollout = next((paths.codex_home / "sessions").rglob("*sid-only-bad*.jsonl"))
    info = {
        "total_token_usage": {"input_tokens": 5000, "output_tokens": 50},
        "last_token_usage": {"input_tokens": "many", "output_tokens": 50},
    }
    payload = {"type": "token_count", "info": info}
    with rollout.open("a") as handle:
        handle.write(json.dumps({"timestamp": iso(later), "type": "event_msg", "payload": payload}) + "\n")
    collected = collect_codex(paths.codex_home, WINDOW, "gpt-6-astra")
    assert "sid-only-bad" not in {
        r.ref for r in collected.rows
    }, "the total is not placed at the session start"
    assert any("input_tokens" in s.reason and "sid-only-bad" in s.path for s in collected.skipped)


def test_a_turn_without_a_timestamp_is_a_counted_skip_not_a_lost_or_misplaced_turn(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    at = BASE + timedelta(minutes=12)
    write_rollout(paths, "2026-09-19", "sid-no-stamp", at, "gpt-6-astra", [(at, 10, 1, 0)])
    rollout = next((paths.codex_home / "sessions").rglob("*sid-no-stamp*.jsonl"))
    info = {
        "total_token_usage": {"input_tokens": 900},
        "last_token_usage": {"input_tokens": 890, "output_tokens": 9},
    }
    with rollout.open("a") as handle:
        handle.write(
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": info}}) + "\n"
        )
    collected = collect_codex(paths.codex_home, WINDOW, "gpt-6-astra")
    row = next(r for r in collected.rows if r.ref == "sid-no-stamp")
    assert (
        row.tokens.input == 10
    ), "the placed turn counts; the unplaceable one is neither added nor the total"
    assert any("timestamp" in s.reason and "sid-no-stamp" in s.path for s in collected.skipped)


def test_a_falsy_non_object_last_token_usage_is_a_counted_skip_and_still_marks_a_per_turn_rollout(
    tmp_path: Path,
) -> None:
    paths = paths_in(tmp_path)
    at = BASE + timedelta(minutes=14)
    for n, bad in enumerate((0, "", [])):
        sid = f"sid-falsy-{n}"
        write_rollout(paths, "2026-09-19", sid, at, "gpt-6-astra", [])
        rollout = next((paths.codex_home / "sessions").rglob(f"*{sid}*.jsonl"))
        info = {"total_token_usage": {"input_tokens": 5000, "output_tokens": 50}, "last_token_usage": bad}
        payload = {"type": "token_count", "info": info}
        with rollout.open("a") as handle:
            handle.write(json.dumps({"timestamp": iso(at), "type": "event_msg", "payload": payload}) + "\n")
    collected = collect_codex(paths.codex_home, WINDOW, "gpt-6-astra")
    assert not any(r.ref.startswith("sid-falsy") for r in collected.rows), "the total is never the fallback"
    assert sum("last_token_usage must be an object" in s.reason for s in collected.skipped) == 3, "each said"


def test_a_bucket_without_weight_in_a_weighted_session_has_a_share_of_zero(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    night = BASE.replace(hour=23, minute=50)
    turns = [
        (night, 0, 0, 5),
        (night + timedelta(minutes=20), 1000, 10, 0),
    ]  # a cached-only day, then a real one
    write_rollout(paths, "2026-09-19", "sid-zero-day", night, "gpt-6-astra", turns)
    window = Window(night - timedelta(minutes=1), night + timedelta(hours=1))
    rows = [r for r in collect_codex(paths.codex_home, window, "gpt-6-astra").rows if r.ref == "sid-zero-day"]
    assert sorted(r.share for r in rows) == [
        0.0,
        1.0,
    ], "the split is the truth: nothing for the cached-only day"


def test_a_weightless_session_splits_its_shares_by_turns_so_windows_inside_one_day_never_double_count(
    tmp_path: Path,
) -> None:
    paths = paths_in(tmp_path)
    ten, eleven = BASE.replace(hour=10, minute=10), BASE.replace(hour=11, minute=10)
    turns = [
        (ten, 0, 0, 5),
        (eleven, 0, 0, 7),
        (eleven + timedelta(minutes=5), 0, 0, 0),
    ]  # the last: no tokens
    write_rollout(paths, "2026-09-19", "sid-same-day", ten, "gpt-6-astra", turns)
    shares = []
    for window in (Window(ten - timedelta(minutes=1), eleven), Window(eleven, eleven + timedelta(hours=1))):
        rows = [
            r for r in collect_codex(paths.codex_home, window, "gpt-6-astra").rows if r.ref == "sid-same-day"
        ]
        shares.append(round(sum(r.share for r in rows), 9))
    assert shares == [0.5, 0.5], "one bucket per window, each half by turns: the empty turn counts nowhere"


def test_a_gemini_session_line_that_is_not_json_or_has_a_bad_counter_is_a_counted_skip_not_a_lost_file(
    tmp_path: Path,
) -> None:
    from ..collectors.gemini_cli import collect_gemini_cli

    paths = paths_in(tmp_path)
    chats = paths.gemini_home / "tmp" / "proj-a" / "chats"
    chats.mkdir(parents=True)
    good = {
        "id": "m1",
        "type": "gemini",
        "model": "gemini-3.8-flash",
        "timestamp": iso(BASE),
        "tokens": {"input": 10, "output": 2},
    }
    bad_counter = dict(good, id="m2", tokens={"input": 1e999, "output": 2})
    lines = [json.dumps(good), "{not json", json.dumps(bad_counter), json.dumps(dict(good, id="m3"))]
    (chats / "session-abc.jsonl").write_text("\n".join(lines) + "\n")
    collected = collect_gemini_cli(paths.gemini_home, WINDOW)
    assert [r.ref for r in collected.rows] == ["proj-a/abc", "proj-a/abc"], "the two good messages"
    reasons = sorted(s.reason for s in collected.skipped)
    assert reasons == ["line 2: not JSON", "line 3: input must be finite, got inf"], reasons


def test_a_rollout_without_a_model_is_unknown_when_no_default_is_configured(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    at = BASE + timedelta(minutes=17)
    write_rollout(paths, "2026-09-19", "sid-no-model", at, "", [(at, 10, 1, 0)])
    rows = [r for r in collect_codex(paths.codex_home, WINDOW, "").rows if r.ref == "sid-no-model"]
    assert rows and rows[0].model == "unknown", "never our model name"


def test_a_gemini_session_takes_its_id_from_the_header_and_a_json_document_alike(tmp_path: Path) -> None:
    from ..collectors.gemini_cli import collect_gemini_cli

    paths = paths_in(tmp_path)
    chats = paths.gemini_home / "tmp" / "proj-h" / "chats"
    chats.mkdir(parents=True)
    full = "45062053-b2c0-45e5-9ffc-9bed0350d45f"
    message = {
        "id": "m1",
        "type": "gemini",
        "model": "gemini-3.8-flash",
        "timestamp": iso(WINDOW.start),
        "tokens": {"input": 10, "output": 2},
    }
    header = {"sessionId": full, "projectHash": "h", "startTime": iso(WINDOW.start), "kind": "main"}
    tagged = {
        **message,
        "id": "m2",
        "sessionId": full,
    }  # a message that repeats the id is a message, not a header
    (chats / "session-2026-09-19T20-18-45062053.jsonl").write_text(
        json.dumps(header) + "\n" + json.dumps(message) + "\n" + json.dumps(tagged) + "\n"
    )
    (chats / "session-2026-02-16T14-49-e5461bf8.json").write_text(
        json.dumps({"sessionId": "e5461bf8-976c-4f81-98f7-f945629efd1d", "messages": [message]})
    )
    (chats / "session-2026-01-01T00-00-deadbeef.jsonl").write_text(json.dumps(message) + "\n")  # no header
    refs = sorted(r.ref for r in collect_gemini_cli(paths.gemini_home, WINDOW).rows)
    assert refs == [
        f"proj-h/{full}",
        f"proj-h/{full}",
        "proj-h/deadbeef",
        "proj-h/e5461bf8-976c-4f81-98f7-f945629efd1d",
    ], "the header's full id when there is one, the file name's short id otherwise; a tagged message still counts"


def test_a_malformed_timing_block_is_a_counted_skip_with_elapsed_time_used() -> None:
    import json
    from datetime import timedelta
    from unittest import mock

    from ..collectors import github as github_module
    from ..models import Skipped

    run = {
        "databaseId": 7,
        "createdAt": iso(WINDOW.start),
        "updatedAt": iso(WINDOW.start + timedelta(minutes=3)),
    }
    skipped: list[Skipped] = []
    with mock.patch.object(github_module, "gh", return_value=json.dumps({"billable": {"UBUNTU": "oops"}})):
        minutes = github_module._minutes("o/r", [run], WINDOW, "linux", skipped)
    assert round(minutes["linux"], 3) == 3.0 and skipped and "malformed /timing" in skipped[0].reason, (
        minutes,
        skipped,
    )
    skipped.clear()
    stringy = json.dumps({"billable": {"UBUNTU": {"total_ms": "1e9"}}})
    with mock.patch.object(github_module, "gh", return_value=stringy):
        github_module._minutes("o/r", [run], WINDOW, "linux", skipped)
    assert skipped and "total_ms is not a non-negative number" in skipped[0].reason, skipped
    for bad in (False, "", -5, float("inf"), float("nan"), 10**400):
        skipped.clear()
        with mock.patch.object(
            github_module, "gh", return_value=json.dumps({"billable": {"UBUNTU": {"total_ms": bad}}})
        ):
            github_module._minutes("o/r", [run], WINDOW, "linux", skipped)
        assert skipped and "not a non-negative number" in skipped[0].reason, (bad, skipped)
    assert "total_ms is not a non-negative number below" in skipped[0].reason


def test_a_codex_session_that_switches_to_the_key_half_way_is_billed_per_turn(tmp_path: Path) -> None:
    from ..collectors.codex import collect_codex
    from ..models import Billing

    paths = paths_in(tmp_path)
    day = paths.codex_home / "sessions" / "2026" / "09" / "21"
    day.mkdir(parents=True)

    def turn(minutes: int, plan: str | None) -> dict[str, object]:
        usage = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}
        return {
            "timestamp": iso(WINDOW.start + timedelta(minutes=minutes)),
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": usage},
                "rate_limits": {"plan_type": plan},
            },
        }

    lines = [
        {"type": "session_meta", "timestamp": iso(WINDOW.start), "payload": {"session_id": "sw-1"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.5"}},
        turn(1, "team"),
        turn(2, None),  # the plan ran out and the API key took over: no plan on this turn
        turn(3, None),
    ]
    (day / "rollout-sw.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    rows = collect_codex(paths.codex_home, WINDOW, "gpt-5.5", Billing.API).rows
    by_billing = {row.billing: row.tokens.input for row in rows}
    assert by_billing == {Billing.SUBSCRIPTION: 100, Billing.API: 200}, rows
    assert round(sum(row.share for row in rows), 6) == 1.0, "the session's cost is still split once"


def test_a_run_without_timing_or_elapsed_time_is_a_counted_loss() -> None:
    import json as json_module
    from unittest import mock

    from ..collectors import github as github_module
    from ..models import Skipped

    run = {"databaseId": 8, "createdAt": iso(WINDOW.start)}  # no updatedAt
    skipped: list[Skipped] = []
    with mock.patch.object(
        github_module, "gh", return_value=json_module.dumps({"billable": {"UBUNTU": "oops"}})
    ):
        assert github_module._minutes("o/r", [run], WINDOW, "linux", skipped) == {}
    assert (
        skipped and "run not counted" in skipped[0].reason and "malformed /timing" in skipped[0].reason
    ), skipped


def test_a_run_without_a_created_at_is_a_counted_loss() -> None:
    from unittest import mock

    from ..collectors import github as github_module
    from ..models import Skipped

    skipped: list[Skipped] = []
    with mock.patch.object(github_module, "gh", return_value="{}"):
        assert github_module._minutes("o/r", [{"databaseId": 9}], WINDOW, "linux", skipped) == {}
    assert skipped and "no createdAt" in skipped[0].reason, skipped
