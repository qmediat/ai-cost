"""The assembled report, its renderings, the default project, monitor's exit code and time parsing."""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast
from unittest import mock

from .. import cli
from .. import ops as ops_module
from ..collectors.claude import project_dir
from ..errors import PricingError, UsageError
from ..models import Window
from ..ops import (
    MonitorEntry,
    ReportRequest,
    _breaches,
    build_report,
    cron_has_entry,
    cron_line,
    cron_without_entry,
    load_items,
    monitor,
    resolve_window,
)
from ..process import self_command
from ..render import render_json, render_markdown
from ..timeutil import iso, parse_ts
from .fixtures import (
    BASE,
    TEST_SUBSCRIPTIONS,
    USAGE_SMALL,
    WINDOW,
    _assistant,
    defaults,
    paths_in,
    write_claude_session,
    write_codex,
    write_rollout,
)


def _everything(tmp_path: Path) -> tuple[Path, ReportRequest]:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    write_codex(paths)
    return tmp_path, ReportRequest(
        session="sess-1", project=tmp_path / "proj", groups=("real", "api", "vendor"), detail=True
    )


def test_report_window_from_the_session_span_and_all_groups(tmp_path: Path) -> None:
    tmp, request = _everything(tmp_path)
    paths = paths_in(tmp)
    config, book = defaults(paths)
    report = build_report(request, paths, config, book)
    assert report.window_iso[0] == iso(BASE.replace(minute=59, hour=14))
    assert report.real and report.api and len(report.rows) >= 6, "Claude + Codex rows"
    assert report.vendor is None, "no work items from any source: no vendor quote"
    assert any("not JSON" in s.reason for s in report.skipped), "skipped records are surfaced, not hidden"


def test_session_id_is_found_in_any_project(tmp_path: Path) -> None:
    """``--session <id>`` without ``--project`` searches every project: the id is unique and the cwd is unrelated."""
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "elsewhere")
    config, book = defaults(paths)
    report = build_report(ReportRequest(session="sess-1", groups=("api",)), paths, config, book)
    assert (
        report.api
        and report.api.total_usd > 0
        and not any("no Claude transcripts" in w for w in report.warnings)
    )


def test_markdown_and_json_renderings(tmp_path: Path) -> None:
    tmp, request = _everything(tmp_path)
    paths = paths_in(tmp)
    config, book = defaults(paths)
    report = build_report(request, paths, config, book)
    text = render_markdown(report)
    assert all(section in text for section in ("## 1. Real cost", "## 2. API-only", "## Summary"))
    data = json.loads(render_json(report, detail=False))
    assert "rows" not in data and data["api"]["lines"]
    assert (
        data["real"]["total_usd"] > 0 and data["real"]["cash_usd"] >= 0
    ), "computed totals are properties: kept"
    assert data["api"]["total_usd"] > 0


def test_since_that_does_not_parse_is_a_usage_error(tmp_path: Path) -> None:
    env = {
        "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"),
        "AI_COST_STATE_DIR": str(tmp_path / "state"),
        "AI_COST_CONFIG_DIR": str(tmp_path / "cfg"),
        "AI_COST_OFFLINE": "1",
    }
    err = io.StringIO()
    with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()), redirect_stderr(err):
        assert cli.main(["report", "--since", "yesterday", "--all-projects"]) == 2
    assert "--since" in err.getvalue() and "yesterday" in err.getvalue()
    assert parse_ts("yesterday") is None, "a malformed stamp inside a source file is skipped, not a crash"
    assert parse_ts(1e20) is None, "an out-of-range epoch is malformed too"
    try:
        load_items(tmp_path / "nope.md")
    except UsageError as exc:
        assert "--items" in str(exc)
    else:
        raise AssertionError("a missing --items file is a usage error")


def test_self_command_runs_the_zipapp_by_path_and_the_package_by_module(tmp_path: Path) -> None:
    """The background price check and the schedule must start THIS program, also when it is the standalone file."""
    archive = tmp_path / "ai-cost"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("__main__.py", "")
    assert self_command(str(archive)) == [sys.executable, str(archive.resolve())]
    assert self_command("ai_cost") == [sys.executable, "-m", "ai_cost"]


def test_default_report_reads_the_cwd_project(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    write_claude_session(paths, project)
    config, book = defaults(paths)
    cwd = os.getcwd()
    os.chdir(project)
    try:
        report = build_report(
            ReportRequest(since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)),
            paths,
            config,
            book,
        )
    finally:
        os.chdir(cwd)
    assert any(
        row.provider.value == "anthropic" for row in report.rows
    ) and "claude transcripts ×2" in " ".join(report.sources)


def test_items_from_markdown_and_json(tmp_path: Path) -> None:
    md = tmp_path / "items.md"
    md.write_text("| ID | Size | Title |\n|---|---|---|\n| R1 | P1 | x |\n| R2 | M | y |\n")
    assert [i.size.value for i in load_items(md)] == ["S", "M"], "an unknown size falls back to S"
    js = tmp_path / "items.json"
    js.write_text(json.dumps([{"id": 1, "size": "l"}]))
    assert load_items(js)[0].size.value == "L"


def test_monitor_budget_breach_exits_3_and_appends_history(tmp_path: Path) -> None:
    tmp, _ = _everything(tmp_path)
    paths = paths_in(tmp)
    config, book = defaults(paths)
    tight = config.__class__(
        **{**config.__dict__, "budgets": config.budgets.__class__(daily_usd=0.01, monthly_usd=1)}
    )
    lines: list[str] = []
    request = ReportRequest(
        all_projects=True, since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("real", "api")
    )
    code = monitor(request, paths, tight, book, append=True, emit=lines.append)
    assert (
        code == 3
        and (paths.state_dir / "history.jsonl").exists()
        and any(line.startswith("BUDGET BREACH") for line in lines)
    )


def test_monitor_monthly_budget_is_one_report_over_the_month(tmp_path: Path) -> None:
    """The month-to-date figure is one report from the 1st to the window's end, whatever the monitor's window."""
    tmp, _ = _everything(tmp_path)
    paths = paths_in(tmp)
    config, book = defaults(paths)
    monthly = config.__class__(
        **{**config.__dict__, "budgets": config.budgets.__class__(daily_usd=0, monthly_usd=1)}
    )
    lines: list[str] = []
    narrow = ReportRequest(
        all_projects=True,
        since=iso(BASE + timedelta(minutes=39)),
        until=iso(BASE + timedelta(minutes=41)),
        groups=("real", "api"),
    )
    assert monitor(narrow, paths, monthly, book, append=False, emit=lines.append) == 3
    assert any("month-to-date" in line for line in lines)


def test_schedule_interval_must_be_at_least_one_day(tmp_path: Path) -> None:
    env = {
        "AI_COST_STATE_DIR": str(tmp_path / "state"),
        "AI_COST_CONFIG_DIR": str(tmp_path / "cfg"),
        "HOME": str(tmp_path),
    }
    err = io.StringIO()
    with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()), redirect_stderr(err):
        assert cli.main(["install", "--schedule", "0"]) == 2
    assert "--schedule" in err.getvalue() and not (tmp_path / "Library").exists()


def test_cron_entry_is_found_and_removed_by_its_tag(tmp_path: Path) -> None:
    line = cron_line(3, [sys.executable, "/opt/my tools/ai-cost"], tmp_path / "log")
    crontab = (
        "0 0 * * * backup\n"
        + line
        + "\n17 6 */3 * * python -m ai_cost prices check --quiet\n"
        + "17 6 */3 * * '/opt/my tools/ai-cost' prices check --quiet >> /tmp/l 2>&1\n"
    )
    assert cron_has_entry(crontab) and "'/opt/my tools/ai-cost'" in line
    assert cron_without_entry(crontab) == [
        "0 0 * * * backup"
    ], "tagged, legacy and quoted-legacy lines all go"
    assert not cron_has_entry("0 0 * * * backup")


def test_cli_report_json_and_exit_codes(tmp_path: Path) -> None:
    tmp, _ = _everything(tmp_path)
    paths = paths_in(tmp)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text(
        json.dumps({"subscriptions": TEST_SUBSCRIPTIONS})
    )  # the package ships none
    env = {
        "CLAUDE_CONFIG_DIR": str(paths.claude_home),
        "CODEX_HOME": str(paths.codex_home),
        "AI_COST_STATE_DIR": str(paths.state_dir),
        "AI_COST_CONFIG_DIR": str(paths.user_config_dir),
        "AI_COST_OFFLINE": "1",
    }
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            code = cli.main(
                [
                    "report",
                    "--session",
                    "sess-1",
                    "--project",
                    str(tmp / "proj"),
                    "--format",
                    "json",
                    "--no-auto-check",
                ]
            )
        assert code == 0 and json.loads(out.getvalue())["real"]["subscriptions"]
        with redirect_stdout(io.StringIO()):
            assert cli.main(["prices", "check"]) == 2, "offline → usage error, never a network call"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_time_parsing_forms() -> None:
    assert parse_ts("1789863031") is not None and parse_ts("1789863031").year == 2026  # type: ignore[union-attr]
    assert parse_ts("2026-09-20").hour == 0  # type: ignore[union-attr]
    assert parse_ts("2026-09-20T00:10:31Z").minute == 10  # type: ignore[union-attr]
    assert parse_ts(None) is None


def test_json_carries_window_hours_and_row_count(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    report = build_report(request, paths, config, book)
    data = json.loads(render_json(report, detail=False))
    assert abs(data["window_hours"] - 61 / 60) < 1e-9 and data["row_count"] == len(report.rows) > 0


def test_unpriced_rows_fail_by_default_and_are_counted_with_skip(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    root = project_dir(paths.claude_home, tmp_path / "proj")
    root.mkdir(parents=True)
    (root / "u.jsonl").write_text(_assistant("u1", 1, USAGE_SMALL, model="claude-unknown-9") + "\n")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    try:
        build_report(request, paths, config, book)
    except PricingError as exc:
        assert exc.code == 5 and "claude-unknown-9" in str(exc)
    else:
        raise AssertionError("an unpriced model must fail loud by default")
    report = build_report(replace(request, unpriced="skip"), paths, config, book)
    assert report.api is not None and report.api.total_usd == 0
    assert any("claude-unknown-9" in w for w in report.warnings)
    assert any(s.source == "pricing" and "claude-unknown-9" in s.path for s in report.skipped)


def test_daily_budget_scale_uses_exact_window_hours(tmp_path: Path) -> None:
    config, _ = defaults(paths_in(tmp_path))
    config = replace(config, budgets=replace(config.budgets, daily_usd=144.0))
    minute = 1 / 60
    entry = MonitorEntry("t", ("a", "b"), round(minute, 2), 0, 0, 0.11, {})
    assert _breaches(config, entry, 0.0, minute / 24.0), "0.11 in one minute breaches 144/day (limit 0.10)"
    rounded = entry.hours / 24.0
    assert not _breaches(config, entry, 0.0, rounded), "the rounded hours (0.02) would have hidden it"


def test_a_provider_budget_with_no_rows_is_not_a_breach(tmp_path: Path) -> None:
    config, _ = defaults(paths_in(tmp_path))
    config = replace(config, budgets=replace(config.budgets, per_provider_daily_usd={"xai": 0.0}))
    entry = MonitorEntry("t", ("a", "b"), 1.0, 0, 0, 0.0, {})
    assert _breaches(config, entry, 0.0, 1 / 24) == [], "no rows for xai: nothing spent, no KeyError"


def test_an_absurd_or_negative_hours_window_is_a_usage_error_not_an_overflow() -> None:
    for hours in (1e12, -1.0, float("inf")):
        try:
            resolve_window(ReportRequest(hours=hours), None, 24)
        except UsageError as exc:
            assert "--hours" in str(exc), str(exc)
        else:
            raise AssertionError(f"--hours {hours} must be a UsageError")
    for hours in (0, 0.0):
        try:
            resolve_window(ReportRequest(hours=hours), None, 24)
        except UsageError:
            continue
        raise AssertionError("--hours 0 is not a window; only an absent --hours takes the default")
    assert resolve_window(ReportRequest(), None, 24).hours() > 23.9, "absent --hours = the config default"


def test_items_json_entries_must_be_objects(tmp_path: Path) -> None:
    bad = tmp_path / "items.json"
    bad.write_text('[{"id": "a", "size": "M"}, "just a string", 3]')
    try:
        load_items(bad)
    except UsageError as exc:
        assert "entry 2" in str(exc) and "str" in str(exc), str(exc)
    else:
        raise AssertionError("a non-object entry must be a UsageError, not an AttributeError")
    good = tmp_path / "good.json"
    good.write_text('[{"id": "a", "size": "M"}, {"title": "b"}]')
    assert [(i.id, i.size.value) for i in load_items(good)] == [("a", "M"), ("2", "S")]


def test_items_that_are_neither_a_list_nor_a_table_are_a_usage_error(tmp_path: Path) -> None:
    for text in ('{"id": "a"}', '"just a string"', "no table here\n"):
        path = tmp_path / "items.txt"
        path.write_text(text)
        try:
            load_items(path)
        except UsageError as exc:
            assert "--items" in str(exc), str(exc)
        else:
            raise AssertionError(f"{text!r} must be a UsageError, not zero items and exit 0")


def test_prices_show_says_the_drift_check_is_off_for_zero_days(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_prices_file().write_text(json.dumps({"auto_check_days": 0}))
    out = io.StringIO()
    with (
        mock.patch.dict(
            os.environ, {"AI_COST_CONFIG_DIR": str(paths.user_config_dir), "AI_COST_OFFLINE": "1"}
        ),
        redirect_stdout(out),
    ):
        cli.main(["prices", "show"])
    assert "drift check off" in out.getvalue() and "every 0 days" not in out.getvalue()


def test_until_alone_takes_the_config_default_hours_not_a_hardcoded_day() -> None:
    window = resolve_window(ReportRequest(until=iso(BASE)), None, 6)
    assert abs(window.hours() - 6) < 1e-9 and window.end == BASE
    explicit = resolve_window(ReportRequest(until=iso(BASE), hours=2), None, 6)
    assert abs(explicit.hours() - 2) < 1e-9, "--hours is honoured next to --until"


def test_hours_with_since_is_a_usage_error_not_silently_ignored() -> None:
    try:
        resolve_window(ReportRequest(since=iso(BASE), hours=0), None, 24)
    except UsageError as exc:
        assert "--since" in str(exc) and "--hours" in str(exc)
    else:
        raise AssertionError("--since with --hours must be refused")


def test_hours_is_validated_even_when_a_session_span_wins(tmp_path: Path) -> None:
    span = Window(BASE, BASE + timedelta(hours=1))
    try:
        resolve_window(ReportRequest(session="sess-1", hours=-2), span, 24)
    except UsageError as exc:
        assert "--hours" in str(exc)
    else:
        raise AssertionError("a negative --hours must be refused even when the span decides the window")
    assert (
        resolve_window(ReportRequest(session="sess-1", hours=2), span, 24).start < span.start
    ), "the span wins"


def test_an_unreadable_items_file_is_a_usage_error(tmp_path: Path) -> None:
    items = tmp_path / "items.md"
    items.write_text("| id | size |\n|---|---|\n| a | S |\n")
    with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):  # root can read chmod 0
        try:
            load_items(items)
        except UsageError as exc:
            assert "cannot read" in str(exc)
        else:
            raise AssertionError("an unreadable --items file must be a UsageError, not an OSError traceback")


def test_out_that_cannot_be_written_is_a_usage_error(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    err = io.StringIO()
    env = {
        "CLAUDE_CONFIG_DIR": str(paths.claude_home),
        "CODEX_HOME": str(paths.codex_home),
        "AI_COST_OFFLINE": "1",
    }
    env.update({"AI_COST_CONFIG_DIR": str(paths.user_config_dir), "AI_COST_STATE_DIR": str(paths.state_dir)})
    with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()), redirect_stderr(err):
        code = cli.main(
            ["report", "--project", str(tmp_path / "proj"), "--out", str(tmp_path / "missing" / "r.md")]
        )
    assert code == 2 and "--out" in err.getvalue(), err.getvalue()


def test_json_output_is_the_same_from_one_run_to_the_next(tmp_path: Path) -> None:
    tmp, request = _everything(tmp_path)
    paths = paths_in(tmp)
    config, book = defaults(paths)
    report = build_report(request, paths, config, book)
    first = json.loads(render_json(report, detail=True))
    second = json.loads(render_json(report, detail=True))
    assert first == second and all(
        line["notes"] == sorted(line["notes"]) for line in first["api"]["lines"]
    ), "sets are sorted"


def test_unpriced_skip_with_real_only_drops_what_the_real_rules_would_price_at_list(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    root = project_dir(paths.claude_home, tmp_path / "proj")
    root.mkdir(parents=True)
    (root / "s.jsonl").write_text(_assistant("c1", 30, USAGE_SMALL, model="claude-99-unlisted") + "\n")
    config, book = defaults(paths)
    base = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), unpriced="skip"
    )
    as_plan = build_report(replace(base, groups=("real",)), paths, config, book)
    assert any(r.model == "claude-99-unlisted" for r in as_plan.rows), "the plan rule needs no list: kept"
    api_billed = replace(config, billing_rules={**config.billing_rules, "anthropic": "api"})
    as_api = build_report(replace(base, groups=("real",)), paths, api_billed, book)
    assert not any(
        r.model == "claude-99-unlisted" for r in as_api.rows
    ), "real would price it at list: skipped"
    assert any("claude-99-unlisted" in s.path for s in as_api.skipped), "counted, and no exit 5"


def test_an_empty_scope_is_a_scope_but_a_file_without_a_table_is_not(tmp_path: Path) -> None:
    empty_json = tmp_path / "empty.json"
    empty_json.write_text("[]")
    assert load_items(empty_json) == []
    header_only = tmp_path / "scope.md"
    header_only.write_text("| id | title | size |\n|---|---|---|\n")
    assert load_items(header_only) == []
    prose = tmp_path / "notes.md"
    prose.write_text("nothing tabular here\n")
    try:
        load_items(prose)
    except UsageError as exc:
        assert "neither" in str(exc)
    else:
        raise AssertionError("a file with no table and no JSON list must be a UsageError")


def test_unpriced_skip_with_real_only_keeps_an_unknown_billing_row_as_unknown(tmp_path: Path) -> None:
    from ..models import Billing

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    at = BASE + timedelta(minutes=9)
    write_rollout(paths, "2026-09-19", "sid-unknown-unlisted", at, "gpt-99-unlisted", [(at, 300, 3, 0)])
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj",
        since=iso(WINDOW.start),
        until=iso(WINDOW.end),
        unpriced="skip",
        groups=("real",),
    )
    report = build_report(request, paths, config, book)
    kept = [r for r in report.rows if r.ref == "sid-unknown-unlisted"]
    assert kept and kept[0].billing is Billing.UNKNOWN, "no rule prices it at list: it stays, as unknown"
    assert report.real is not None and report.real.unknown_billing >= 1, "and the real group lists it as such"


def test_an_until_at_the_dawn_of_time_is_a_usage_error_not_an_overflow() -> None:
    try:
        resolve_window(ReportRequest(until="0001-01-01T00:00:00Z"), None, 24)
    except UsageError as exc:
        assert "--until" in str(exc) and "earliest" in str(exc)
    else:
        raise AssertionError("a window that cannot start must be a UsageError, never an OverflowError")


def test_monitor_append_into_an_unwritable_state_dir_is_a_tool_error(tmp_path: Path) -> None:
    from ..errors import ToolError

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    paths.state_dir.parent.mkdir(parents=True, exist_ok=True)
    paths.state_dir.write_text("a file where the state directory should be")
    request = ReportRequest(project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end))
    try:
        monitor(request, paths, config, book, append=True, emit=lambda _: None)
    except ToolError as exc:
        assert "cannot write" in str(exc) and "history.jsonl" in str(exc)
    else:
        raise AssertionError("an unwritable history file must be a ToolError, never a traceback")


def test_a_window_at_the_edge_of_time_is_a_usage_error_before_any_source_pads_it() -> None:
    for since, until in (
        ("0001-01-01T00:00:00Z", "0001-01-02T00:00:00Z"),
        ("9999-12-30T00:00:00Z", "9999-12-31T23:59:59Z"),
    ):
        try:
            resolve_window(ReportRequest(since=since, until=until), None, 24)
        except UsageError as exc:
            assert "--since/--until" in str(exc)
        else:
            raise AssertionError(f"{since}..{until}: a window the collectors cannot pad must be a UsageError")


def test_a_session_span_at_the_edge_of_time_is_a_usage_error_not_an_overflow() -> None:
    from datetime import datetime, timezone

    edge = Window(
        datetime(1, 1, 1, 0, 0, 30, tzinfo=timezone.utc), datetime(1, 1, 1, 1, 0, tzinfo=timezone.utc)
    )
    try:
        resolve_window(ReportRequest(session="edge"), edge, 24)
    except UsageError as exc:
        assert "--session" in str(exc)
    else:
        raise AssertionError("a span the collectors cannot pad must be a UsageError, never an OverflowError")


def test_cli_log_appends_from_flags_and_from_a_response(tmp_path: Path) -> None:
    from .test_usage_log import _isolated

    with _isolated(tmp_path):
        _run_cli_log_appends_from_flags_and_from_a_response(tmp_path)


def _run_cli_log_appends_from_flags_and_from_a_response(tmp_path: Path) -> None:
    log = tmp_path / "usage.jsonl"
    assert (
        cli.main(
            [
                "log",
                "--provider",
                "openai",
                "--model",
                "gpt-5.5",
                "--input",
                "12",
                "--output",
                "3",
                "--ref",
                "j1",
                "--log",
                str(log),
            ]
        )
        == 0
    )
    response = tmp_path / "r.json"
    response.write_text(
        json.dumps(
            {
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 9},
            }
        )
    )
    assert cli.main(["log", "--from-response", str(response), "--tag", "ci", "--log", str(log)]) == 0
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [line["provider"] for line in lines] == ["openai", "anthropic"] and lines[1]["tags"] == ["ci"]
    assert cli.main(["log", "--provider", "openai", "--log", str(log)]) == 2, "no model: a usage error"
    assert (
        cli.main(["log", "--provider", "openai", "--model", "m", "--log", str(log)]) == 2
    ), "no counters, no cost"


def test_the_usage_log_is_a_built_in_source_of_the_report(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    assert paths.usage_log is not None
    log = paths.usage_log  # the report reads the log of its Paths, whatever the environment says
    log.parent.mkdir(parents=True, exist_ok=True)
    at = iso(BASE.replace(minute=30))
    log.write_text(
        json.dumps(
            {
                "schema": 1,
                "at": at,
                "provider": "openai",
                "model": "gpt-5.5",
                "tokens": {"input": 1000, "output": 100, "weird": 1},
            }
        )
        + "\n"
    )
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    with mock.patch.dict(os.environ, {"AI_COST_USAGE_LOG": str(log)}):
        report = build_report(request, paths, config, book)
    assert any(
        r.source == "usage-log" and r.model == "gpt-5.5" for r in report.rows
    ), "read like every other source"
    assert any(
        "unknown token keys" in w for w in report.warnings
    ), "an unknown counter is a warning, not silence"


def test_a_plugin_whose_tests_package_does_not_import_is_a_counted_selftest_failure() -> None:
    from ..plugins import Plugin

    lines: list[str] = []
    broken = Plugin(name="acme", sources=(), enrichers=(), doctor=None, tests_package="no.such.acme.tests")
    code = cli._plugin_tests([broken], lines.append, False, lambda *a, **k: 0)
    assert code == 1 and any("FAIL acme selftest" in line and "cannot be run" in line for line in lines)
    lines.clear()
    importable = Plugin(
        name="acme", tests_package="json"
    )  # imports, but its test modules blow up on collection

    def exploding_run(*args: object, **kwargs: object) -> int:
        raise ImportError("a test module of the plugin does not import")

    assert cli._plugin_tests([importable], lines.append, False, exploding_run) == 1
    assert any("cannot be run" in line and "does not import" in line for line in lines), lines


def test_the_zipapp_never_packs_build_metadata(tmp_path: Path) -> None:
    import importlib.util

    script = Path(__file__).resolve().parents[3] / "scripts" / "build.py"
    if (
        not script.exists()
    ):  # inside the shipped zipapp there is no build script to test: nothing to assert here
        return
    spec = importlib.util.spec_from_file_location("build", script)
    assert spec and spec.loader
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("")
    (src / "pkg.egg-info").mkdir()
    (src / "pkg.egg-info" / "PKG-INFO").write_text("Name: pkg\n")
    (src / "pkg" / "__pycache__").mkdir()
    (src / "pkg" / "__pycache__" / "x.pyc").write_bytes(b"")
    out = tmp_path / "app"
    build.build(src, out)
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
    assert "pkg/__init__.py" in names and not any(
        ".egg-info" in n or n.endswith(".pyc") for n in names
    ), names


def test_a_plugin_that_raises_costs_its_own_rows_never_the_report(tmp_path: Path) -> None:
    from collections.abc import Sequence

    from ..models import Collected, UsageRow
    from ..plugins import Context, Loaded, Plugin

    class Exploding:
        name = "boom"

        def collect(self, ctx: Context) -> Collected:
            raise RuntimeError("the plugin's own defect")

    class Tripping:
        name = "trip"

        def enrich(self, rows: Sequence[UsageRow], ctx: Context) -> list[UsageRow]:
            raise KeyError("missing")

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    loaded = Loaded(plugins=[Plugin(name="acme", sources=(Exploding(),), enrichers=(Tripping(),))])
    request = ReportRequest(project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end))
    report = build_report(request, paths, config, book, loaded)
    assert report.rows, "the built-in rows are still there"
    assert sum(s.source == "plugin acme" for s in report.skipped) == 2, "one counted skip per failing piece"

    class Shapeless:  # an empty name, and a collect() that returns the wrong thing
        name = ""

        def collect(self, ctx: Context) -> Collected:
            return "not a Collected"  # type: ignore[return-value]

    shapeless = Loaded(plugins=[Plugin(name="acme", sources=(Shapeless(),))])
    second = build_report(request, paths, config, book, shapeless)
    assert second.rows and any("source Shapeless failed" in w for w in second.warnings), second.warnings

    class Forgetful:  # an enricher that returns nothing
        name = "forget"

        def enrich(self, rows: Sequence[UsageRow], ctx: Context) -> list[UsageRow]:
            return None  # type: ignore[return-value]

    forgetful = Loaded(plugins=[Plugin(name="acme", enrichers=(Forgetful(),))])
    third = build_report(request, paths, config, book, forgetful)
    assert third.rows and any(
        "enricher forget failed" in w and "must return rows" in w for w in third.warnings
    )

    class Meddling:  # appends a foreign item to the list it was given, then raises
        name = "meddle"

        def enrich(self, rows: Sequence[UsageRow], ctx: Context) -> list[UsageRow]:
            cast("list[object]", rows).append("not a row")
            raise RuntimeError("after the damage")

    meddling = Loaded(plugins=[Plugin(name="acme", enrichers=(Meddling(),))])
    fourth = build_report(request, paths, config, book, meddling)
    assert fourth.rows == report.rows, "a raise after an in-place change leaves the rows as they were"
    assert any("source boom failed" in w for w in report.warnings) and any(
        "enricher trip failed" in w for w in report.warnings
    )


def test_a_plugin_doctor_that_raises_is_one_red_line_not_an_abort(tmp_path: Path) -> None:
    from ..ops import doctor
    from ..plugins import Loaded, Plugin

    def bad_doctor(ctx: object, line: object) -> None:
        raise RuntimeError("the plugin's doctor is broken")

    paths = paths_in(tmp_path)
    config, book = defaults(paths)
    lines: list[str] = []
    loaded = Loaded(plugins=[Plugin(name="acme", doctor=bad_doctor)])
    with mock.patch("ai_cost.ops.load_configured_plugins", return_value=loaded):
        doctor(paths, config, book, lines.append)
    assert any("plugin acme: doctor failed" in line and "broken" in line for line in lines), lines

    class Nameless:
        def collect(self, ctx: object) -> object:
            return None

    lines.clear()
    nameless = Loaded(plugins=[Plugin(name="acme", sources=(Nameless(),))])  # type: ignore[arg-type]
    with mock.patch("ai_cost.ops.load_configured_plugins", return_value=nameless):
        doctor(paths, config, book, lines.append)
    assert any("plugin acme: Nameless" in line for line in lines), "a nameless source is listed by its type"


def test_selftest_still_runs_when_the_user_config_does_not_parse(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text("{not json")
    lines: list[str] = []
    plugins, code = cli._selftest_plugins(paths, lines.append)
    assert plugins == () and code == 1 and any("FAIL user config" in line for line in lines), lines


def test_unschedule_removes_an_earlier_label_of_the_launchd_job_too(tmp_path: Path) -> None:
    from .. import ops

    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "ai-cost.prices-check.plist").write_text("<plist/>")
    (agents / "acme.ai-cost.prices-check.plist").write_text(
        "<plist/>"
    )  # the job under a name an earlier version used
    (agents / "other.plist").write_text("<plist/>")
    lines: list[str] = []
    with (
        mock.patch.object(Path, "home", return_value=tmp_path),
        mock.patch.object(ops, "is_macos", return_value=True),
        mock.patch("subprocess.run") as run,
    ):
        assert ops.schedule_installed() is True
        assert ops.remove_schedule(lines.append) == 0
        assert ops.schedule_installed() is False
    unloaded = [call.args[0][2] for call in run.call_args_list if call.args[0][:2] == ["launchctl", "unload"]]
    assert len(unloaded) == 2 and sum("removed" in line for line in lines) == 2, (unloaded, lines)
    assert (agents / "other.plist").exists(), "a file that is not this job is untouched"
    (agents / "stuck.ai-cost.prices-check.plist").mkdir()  # a directory where the agent file should be
    lines.clear()
    with (
        mock.patch.object(Path, "home", return_value=tmp_path),
        mock.patch.object(ops, "is_macos", return_value=True),
        mock.patch("subprocess.run"),
    ):
        assert ops.remove_schedule(lines.append) == 1, "said and exit 1, never a traceback"
    assert any("cannot remove" in line for line in lines), lines


def test_a_plugin_source_returning_foreign_rows_or_items_costs_only_itself(tmp_path: Path) -> None:
    from ..models import Collected
    from ..plugins import Context, Loaded, Plugin

    class Foreign:
        name = "foreign"

        def collect(self, ctx: Context) -> Collected:
            return Collected(rows=["not a row"])  # type: ignore[list-item]

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end))
    report = build_report(
        request, paths, config, book, Loaded(plugins=[Plugin(name="acme", sources=(Foreign(),))])
    )
    assert report.rows and any(
        "source foreign failed" in w and "UsageRow" in w for w in report.warnings
    ), report.warnings


def test_live_github_rows_reach_the_plugin_enrichers_and_nothing_collected_is_dropped(tmp_path: Path) -> None:
    from collections.abc import Sequence

    from ..models import Billing, Collected, Provider, RowKind, Tokens, UsageRow
    from ..plugins import Context, Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    live_row = UsageRow(
        provider=Provider.GITHUB,
        model="actions",
        kind=RowKind.ACTIONS,
        source="github-live",
        at=WINDOW.start,
        ref="o/r",
        billing=Billing.API,
        tokens=Tokens(minutes=5.0, billable=True),
    )
    seen: list[str] = []

    class Witness:
        name = "witness"

        def enrich(self, rows: Sequence[UsageRow], ctx: Context) -> list[UsageRow]:
            seen.extend(row.source for row in rows)
            return list(rows)

    class Reporter:  # a plugin source with a GitHub row of its own
        name = "reporter"

        def collect(self, ctx: Context) -> Collected:
            return Collected(rows=[replace(live_row, source="reporter", ref="from-plugin")])

    loaded = Loaded(plugins=[Plugin(name="acme", sources=(Reporter(),), enrichers=(Witness(),))])
    request = ReportRequest(
        project=tmp_path / "proj",
        since=iso(WINDOW.start),
        until=iso(WINDOW.end),
        github=("o/r",),
        groups=("api",),
    )
    with mock.patch.object(ops_module, "collect_github", return_value=Collected(rows=[live_row])):
        report = build_report(request, paths, config, book, loaded)
    assert "github-live" in seen, "the enricher ran after the live rows were collected"
    assert {r.source for r in report.rows if r.provider == Provider.GITHUB} == {"github-live", "reporter"}


def test_claude_rows_follow_the_configured_anthropic_billing(tmp_path: Path) -> None:
    from ..groups import real_rule
    from ..models import Billing, Provider, RowKind

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("real",)
    )
    on_plan = build_report(request, paths, config, book)
    assert on_plan.real is not None and {r.billing for r in on_plan.rows} == {Billing.SUBSCRIPTION}
    assert any(line.label == "plan" for line in on_plan.real.usage)
    api_billed = build_report(
        request, paths, replace(config, billing_rules={**config.billing_rules, "anthropic": "api"}), book
    )
    assert api_billed.real is not None and {r.billing for r in api_billed.rows} == {Billing.API}
    assert any(line.label == "api" and line.usd > 0 for line in api_billed.real.usage)
    mixed = build_report(
        request, paths, replace(config, billing_rules={**config.billing_rules, "anthropic": ""}), book
    )
    assert mixed.real is not None and {r.billing for r in mixed.rows} == {Billing.UNKNOWN}
    assert mixed.real.unknown_billing == len(mixed.rows), "not guessed: counted as unknown"
    logged = replace(next(iter(on_plan.rows)), kind=RowKind.LOG, billing=Billing.API, cost_reported=0.25)
    label, money = real_rule(Provider.ANTHROPIC)(logged, book, config)
    assert (label, money.usd) == ("api", 0.25), "a source-reported cost is what was charged"
    label, money = real_rule(Provider.of("acme"))(replace(logged, provider=Provider.of("acme")), book, config)
    assert money.usd == 0.25 and label == logged.model


def test_install_schedule_fails_loud_when_an_earlier_label_stays_or_a_directory_cannot_be_made(
    tmp_path: Path,
) -> None:
    import subprocess

    from .. import ops
    from ..errors import ToolError

    paths = paths_in(tmp_path)
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "stuck.ai-cost.prices-check.plist").mkdir()  # an earlier label that cannot be unlinked
    lines: list[str] = []
    loaded = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with (
        mock.patch.object(Path, "home", return_value=tmp_path),
        mock.patch.object(ops, "is_macos", return_value=True),
        mock.patch("subprocess.run", return_value=loaded),
    ):
        assert (
            ops.install_schedule(paths, 7, ["ai-cost"], lines.append) == 1
        ), "the new job loads, the old one stays: 1"
    assert any("cannot remove" in line for line in lines) and any("loaded" in line for line in lines), lines
    (tmp_path / "state-file").write_text("a file where the state directory should be")
    blocked = replace(paths, state_dir=tmp_path / "state-file")
    try:
        ops.install_schedule(blocked, 7, ["ai-cost"], lines.append)
    except ToolError as exc:
        assert "state directory" in str(exc)
    else:
        raise AssertionError(
            "a file in the way of the state directory must be a ToolError, never a traceback"
        )


def test_selftest_counts_a_plugin_the_config_names_but_cannot_load(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text(json.dumps({"plugins": ["no_such_ai_cost_plugin"]}))
    lines: list[str] = []
    with mock.patch.dict(os.environ, {"AI_COST_PLUGINS": ""}):  # only the config's list decides here
        plugins, code = cli._selftest_plugins(paths, lines.append)
    assert code == 1 and not any(p.name == "no_such_ai_cost_plugin" for p in plugins), (
        plugins,
        code,
    )  # a bundle may add its own
    assert any("FAIL plugin" in line and "no_such_ai_cost_plugin" in line for line in lines), lines


def test_a_source_whose_skips_are_not_a_list_keeps_none_of_its_rows(tmp_path: Path) -> None:
    from ..models import Collected
    from ..plugins import Context, Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)

    class HalfBroken:  # rows that would count, skips that cannot be read
        name = "half"

        def collect(self, ctx: Context) -> Collected:
            good = build_report(request, paths, config, book).rows[0]
            return Collected(rows=[replace(good, source="half")], skipped=None)  # type: ignore[arg-type]

    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    loaded = Loaded(plugins=[Plugin(name="acme", sources=(HalfBroken(),))])
    report = build_report(request, paths, config, book, loaded)
    assert not any(r.source == "half" for r in report.rows), "a source that fails its checks keeps nothing"
    assert any("source half failed" in w for w in report.warnings)


def test_a_plan_rule_without_plans_is_said_in_the_header(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text(
        json.dumps({"subscriptions": [], "providers": {"anthropic": {"billing": "subscription"}}})
    )
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    config = replace(config, subscriptions=())  # what install --init-config leaves behind
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("real",)
    )
    report = build_report(request, paths, config, book)
    assert any(
        "providers.anthropic.billing = subscription but no subscription covers anthropic" in w
        for w in report.warnings
    )
    quiet = build_report(request, paths, replace(config, billing_rules={}), book)
    assert not any("no subscription covers" in w for w in quiet.warnings), "no plan rule: nothing to add"
    xai_plan = replace(config, billing_rules={"anthropic": "api", "xai": "subscription"})
    said = build_report(request, paths, xai_plan, book)
    assert any(
        "providers.xai.billing = subscription but no subscription covers xai" in w for w in said.warnings
    )
    partly = replace(
        config, subscriptions=defaults(paths)[0].subscriptions
    )  # the test plans cover anthropic, not xai
    both = replace(
        partly, billing_rules={"anthropic": "subscription", "xai": "subscription", "github": "subscription"}
    )
    warned = build_report(request, paths, both, book).warnings
    assert any("covers xai" in w for w in warned) and not any("covers anthropic" in w for w in warned), warned
    assert not any(
        "covers github" in w for w in warned
    ), "copilot-pro covers github-copilot, so github is covered"


def test_a_source_that_yields_its_rows_or_has_no_name_is_reported_as_it_is(tmp_path: Path) -> None:
    from collections.abc import Iterator

    from ..models import Collected, UsageRow
    from ..plugins import Context, Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    seed = build_report(request, paths, config, book).rows[0]

    class Yielding:  # rows as a generator: consumed once, counted from the checked list
        name = "yielding"

        def collect(self, ctx: Context) -> Collected:
            def rows() -> Iterator[UsageRow]:
                yield replace(seed, source="yielding")

            return Collected(rows=rows())  # type: ignore[arg-type]

    class Nameless:
        def collect(self, ctx: Context) -> Collected:
            return Collected(rows=[replace(seed, source="nameless")])

    loaded = Loaded(plugins=[Plugin(name="acme", sources=(Yielding(), Nameless()))])  # type: ignore[arg-type]
    report = build_report(request, paths, config, book, loaded)
    assert any(s.startswith("yielding ×1") for s in report.sources), report.sources
    assert any(s.startswith("Nameless ×1") for s in report.sources), "named by its class, its rows are valid"
    assert not any("its rows are missing" in w for w in report.warnings), report.warnings


def test_an_inverted_or_empty_window_is_a_usage_error_never_a_silent_empty_report(tmp_path: Path) -> None:
    from ..ops import resolve_window

    later, earlier = iso(WINDOW.end), iso(WINDOW.start)
    for since, until in ((later, earlier), (earlier, earlier)):
        try:
            resolve_window(ReportRequest(since=since, until=until), None, 24.0)
        except UsageError as exc:
            assert "not before" in str(exc) and "empty" in str(exc), str(exc)
        else:
            raise AssertionError(f"--since {since} --until {until} must be refused")
    assert resolve_window(ReportRequest(since=earlier, until=later), None, 24.0).start < WINDOW.end


def test_a_source_whose_skips_are_not_skips_keeps_nothing_and_the_doctor_survives_a_broken_marker(
    tmp_path: Path,
) -> None:
    from ..errors import ToolError
    from ..models import Collected
    from ..plugins import Context, Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    seed = build_report(request, paths, config, book).rows[0]

    class Stringy:
        name = "stringy"

        def collect(self, ctx: Context) -> Collected:
            return Collected(rows=[replace(seed, source="stringy")], skipped="oops")  # type: ignore[arg-type]

    report = build_report(
        request, paths, config, book, Loaded(plugins=[Plugin(name="acme", sources=(Stringy(),))])
    )
    assert not any(r.source == "stringy" for r in report.rows) and any(
        "Skipped items only" in w for w in report.warnings
    )
    lines: list[tuple[bool, str]] = []
    with mock.patch.object(ops_module, "load_configured_plugins", side_effect=ToolError("marker broken")):
        ops_module._doctor_plugins(paths, config, lambda ok, text: lines.append((ok, text)), lambda _: None)
    assert lines == [(False, "plugins: marker broken")], lines


def test_a_plugins_row_without_billing_evidence_follows_the_configured_rule(tmp_path: Path) -> None:
    from ..config import builtin_config, parse_config
    from ..errors import ConfigError
    from ..models import Billing, Collected, Provider
    from ..plugins import Context, Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    seed = build_report(request, paths, config, book).rows[0]
    unknown = replace(seed, provider=Provider.XAI, model="grok-4.6", source="acme", billing=Billing.UNKNOWN)

    class Acme:
        name = "acme"

        def collect(self, ctx: Context) -> Collected:
            return Collected(rows=[unknown])

    loaded = Loaded(plugins=[Plugin(name="acme", sources=(Acme(),))])
    assert config.billing_rules["xai"] == "api", "the shipped rule the source itself never read"
    ruled = build_report(request, paths, config, book, loaded)
    assert next(r.billing for r in ruled.rows if r.source == "acme") is Billing.API
    unruled = build_report(request, paths, replace(config, billing_rules={}), book, loaded)
    assert next(r.billing for r in unruled.rows if r.source == "acme") is Billing.UNKNOWN
    for section, word in (({"billing": "prepaid"}, "prepaid"), ("api", "must be an object")):
        raw = builtin_config()
        raw["providers"]["deepseek"] = section
        try:
            parse_config(raw, "test")
        except ConfigError as exc:
            assert "providers.deepseek" in str(exc) and word in str(exc), str(exc)
        else:
            raise AssertionError(f"providers.deepseek = {section!r} must be a ConfigError")


def test_the_sources_line_names_the_programs_a_source_reports_for(tmp_path: Path) -> None:
    from ..models import Collected
    from ..plugins import Context, Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    seed = build_report(request, paths, config, book).rows[0]

    class Relay:  # a source whose rows come from other programs, as the usage log's do
        name = "relay"

        def collect(self, ctx: Context) -> Collected:
            return Collected(
                rows=[
                    replace(seed, source="my-app"),
                    replace(seed, source="ci"),
                    replace(seed, source="relay"),
                ]
            )

    report = build_report(
        request, paths, config, book, Loaded(plugins=[Plugin(name="acme", sources=(Relay(),))])
    )
    assert "relay ×3 (ci, my-app)" in report.sources, report.sources


def test_selftest_names_the_plugin_loader_not_the_config_when_the_marker_is_broken(tmp_path: Path) -> None:
    from ..errors import ToolError

    paths = paths_in(tmp_path)
    lines: list[str] = []
    with mock.patch.object(ops_module, "load_configured_plugins", side_effect=ToolError("marker broken")):
        plugins, code = cli._selftest_plugins(paths, lines.append)
    assert (
        plugins == () and code == 1 and lines == ["  FAIL plugins: marker broken — no plugin was tested"]
    ), lines


def test_a_source_whose_rows_name_no_source_keeps_nothing(tmp_path: Path) -> None:
    from ..models import Collected
    from ..plugins import Context, Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    seed = build_report(request, paths, config, book).rows[0]

    class Unset:
        name = "unset"

        def collect(self, ctx: Context) -> Collected:
            return Collected(rows=[replace(seed, source="")])

    report = build_report(
        request, paths, config, book, Loaded(plugins=[Plugin(name="acme", sources=(Unset(),))])
    )
    assert not any(s.startswith("unset") for s in report.sources)
    assert any(
        "source unset failed" in w and "name their source" in w for w in report.warnings
    ), report.warnings


def test_month_to_date_at_the_first_midnight_is_zero_not_an_empty_window_error(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from ..ops import month_to_date

    paths = paths_in(tmp_path)
    config, book = defaults(paths)
    assert month_to_date(paths, config, book, datetime(2026, 9, 1, tzinfo=timezone.utc)) == 0.0


def test_a_plugin_whose_sources_are_not_iterable_costs_itself_and_init_config_says_what_it_cannot_write(
    tmp_path: Path,
) -> None:
    from ..errors import ToolError
    from ..plugins import Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    broken = Loaded(plugins=[Plugin(name="acme", sources=None, enrichers=None)])  # type: ignore[arg-type]
    report = build_report(request, paths, config, book, broken)
    assert report.rows and sum("plugin acme" in s.source for s in report.skipped) == 2, report.skipped
    assert any(
        "sources failed" in s.reason and "sources is not a sequence" in s.reason for s in report.skipped
    )
    (tmp_path / "cfg-file").write_text("a file where the config directory should be")
    blocked = replace(paths, user_config_dir=tmp_path / "cfg-file")
    try:
        ops_module.init_config(blocked, False, lambda _: None)
    except ToolError as exc:
        assert "config directory" in str(exc) or "cannot write" in str(exc), str(exc)
    else:
        raise AssertionError("an unwritable config location must be a ToolError, never a traceback")


def test_the_doctor_diagnoses_a_plugin_whose_sources_are_not_a_sequence(tmp_path: Path) -> None:
    from ..plugins import Loaded, Plugin

    paths = paths_in(tmp_path)
    config, _ = defaults(paths)
    lines: list[tuple[bool, str]] = []
    loaded = Loaded(plugins=[Plugin(name="acme", sources=None), Plugin(name="beta")])  # type: ignore[arg-type]
    with mock.patch.object(ops_module, "load_configured_plugins", return_value=loaded):
        ops_module._doctor_plugins(paths, config, lambda ok, text: lines.append((ok, text)), lambda _: None)
    assert (
        False,
        "plugin acme: sources is not a sequence ('NoneType' object is not iterable)",
    ) in lines, lines
    assert (True, "plugin beta: no sources") in lines, "the next plugin is still listed"


def test_a_plugin_row_with_a_str_provider_costs_the_plugin_not_the_report(tmp_path: Path) -> None:
    from ..models import Collected
    from ..plugins import Context, Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    seed = build_report(request, paths, config, book).rows[0]

    class Stringy:
        name = "stringy"

        def collect(self, ctx: Context) -> Collected:
            return Collected(rows=[replace(seed, provider="acme", source="stringy")])  # type: ignore[arg-type]

    report = build_report(
        request, paths, config, book, Loaded(plugins=[Plugin(name="acme", sources=(Stringy(),))])
    )
    assert report.rows and not any(r.source == "stringy" for r in report.rows)
    assert any(
        "source stringy failed" in w and "Provider and a Billing" in w for w in report.warnings
    ), report.warnings


def test_an_enricher_that_drops_or_copies_a_row_costs_itself_and_a_broken_loader_is_a_warning(
    tmp_path: Path,
) -> None:
    from collections.abc import Sequence

    from ..errors import ToolError
    from ..models import UsageRow
    from ..plugins import Context, Loaded, Plugin

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        project=tmp_path / "proj", since=iso(WINDOW.start), until=iso(WINDOW.end), groups=("api",)
    )
    before = build_report(request, paths, config, book).rows

    class Dropper:
        name = "dropper"

        def enrich(self, rows: Sequence[UsageRow], ctx: Context) -> list[UsageRow]:
            return list(rows)[1:]

    report = build_report(
        request, paths, config, book, Loaded(plugins=[Plugin(name="acme", enrichers=(Dropper(),))])
    )
    assert report.rows == before and any(
        "one row per input row" in w for w in report.warnings
    ), report.warnings
    with mock.patch.object(ops_module, "load_configured_plugins", side_effect=ToolError("marker broken")):
        report = build_report(request, paths, config, book)
    assert report.rows == before and any(
        w == "plugins: marker broken" for w in report.warnings
    ), report.warnings


def test_live_github_rows_without_a_plan_are_said_in_the_header(tmp_path: Path) -> None:
    from ..config import Subscription
    from ..models import Billing, Collected, Provider, RowKind, Tokens, UsageRow

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    live = UsageRow(
        provider=Provider.GITHUB,
        model="actions",
        kind=RowKind.ACTIONS,
        source="github",
        at=WINDOW.start,
        ref="o/r",
        billing=Billing.SUBSCRIPTION,
        tokens=Tokens(minutes=5.0, billable=True),
    )
    request = ReportRequest(
        project=tmp_path / "proj",
        since=iso(WINDOW.start),
        until=iso(WINDOW.end),
        github=("o/r",),
        groups=("real",),
    )
    with mock.patch.object(ops_module, "collect_github", return_value=Collected(rows=[live])):
        covered = build_report(request, paths, config, book)  # the test plans cover github-copilot
        bare = build_report(request, paths, replace(config, subscriptions=()), book)
        lookalike = replace(config, subscriptions=(Subscription(plan="x", covers=("githubish",)),))
        unrelated = build_report(request, paths, lookalike, book)
    assert not any("no subscription covers github" in w for w in covered.warnings)
    assert any("no subscription covers github" in w for w in bare.warnings), bare.warnings
    assert any(
        "no subscription covers github" in w for w in unrelated.warnings
    ), "a cover is github or github-<product>, never a name that starts with the letters"
