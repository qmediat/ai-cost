"""Daily reports (global + per project, the index, the job) and reconcile (local count vs the provider's figure)."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from .. import cli, ops
from ..collectors.grok_build import TICKS_PER_USD
from ..config import Config, Paths, PriceBook
from ..daily import (
    DailyIndex,
    Output,
    daily_reports,
    day_window,
    newest_index_file,
    project_path,
    projects_touched,
    read_index,
)
from ..errors import ToolError, UsageError
from ..models import Tokens
from ..ops import (
    DAILY_CADENCE,
    DAILY_JOB,
    PRICES_JOB,
    Cadence,
    ReportRequest,
    cron_has_entry,
    cron_without_entry,
)
from ..reconcile import (
    Reported,
    billed_tokens,
    describe,
    gap_pct,
    last_reconciliation,
    reconcile,
    run_reconcile,
)
from ..timeutil import iso
from .fixtures import BASE, GROK_TICKS, defaults, paths_in, write_grok

DAY = BASE.date()


def _transcript(
    paths: Paths, project: Path, name: str, cwd: str | None, minutes: int = 5, tokens: int = 100
) -> Path:
    """A transcript of one user and one assistant entry (``tokens`` input, 0 = no usage), naming ``cwd`` when given."""
    from ..collectors.claude import project_dir

    root = project_dir(paths.claude_home, project)
    root.mkdir(parents=True, exist_ok=True)
    stamp = iso(BASE + timedelta(minutes=minutes))
    extra = {"cwd": cwd} if cwd is not None else {}
    lines = [json.dumps({"type": "user", "timestamp": stamp, **extra})]
    if tokens:
        usage = {"input_tokens": tokens, "output_tokens": tokens // 10}
        message = {"id": name, "model": "claude-sonnet-5", "usage": usage}
        lines.append(json.dumps({"type": "assistant", "timestamp": stamp, **extra, "message": message}))
    path = root / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_the_day_window_is_the_utc_day_half_open() -> None:
    window = day_window(date(2026, 9, 19))
    assert iso(window.start) == "2026-09-19T00:00:00Z" and iso(window.end) == "2026-09-20T00:00:00Z"
    assert window.contains(window.start) and not window.contains(window.end)


def test_projects_touched_and_their_working_directory(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    fresh = _transcript(paths, tmp_path / "fresh", "f1", str(tmp_path / "fresh"))
    stale = _transcript(paths, tmp_path / "stale", "s1", str(tmp_path / "stale"))
    _transcript(paths, tmp_path / "nameless", "n1", None)
    old = (day_window(DAY).start - timedelta(days=2)).timestamp()
    os.utime(stale, (old, old))
    touched = projects_touched(paths.claude_home, day_window(DAY))
    assert len(touched) == 2 and fresh.parent in touched and stale.parent not in touched
    assert project_path(fresh.parent) == tmp_path / "fresh"
    assert (
        project_path(stale.parent) == tmp_path / "stale"
    ), "the newest transcript names it, whatever its age"
    nameless = next(p for p in touched if "nameless" in p.name)
    assert project_path(nameless) is None, "no entry names a working directory"
    assert project_path(tmp_path / "absent") is None
    stray = _transcript(paths, tmp_path / "stray", "s9", str(tmp_path / "elsewhere"))
    assert (
        project_path(stray.parent) is None
    ), "a cwd that does not encode to the directory's name never counts"


def test_daily_writes_the_global_report_one_per_project_and_the_index(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    project = tmp_path / "proj"
    _transcript(paths, project, "p1", str(project))
    _transcript(paths, tmp_path / "nameless", "n1", None)
    _transcript(paths, tmp_path / "idle", "i1", str(tmp_path / "idle"), minutes=-3 * 24 * 60)
    write_grok(paths, cwd=str(project))
    write_grok(paths, cwd=str(tmp_path / "other"), session="grok-other")
    config, book = defaults(paths)
    lines: list[str] = []
    assert daily_reports(paths, config, book, DAY, paths.reports_dir, Output(lines.append, lines.append)) == 0
    folder = paths.reports_dir / DAY.isoformat()
    assert (folder / "global.md").exists() and (folder / "global.json").exists()
    index = read_index(folder / "index.json")
    assert index.day == DAY.isoformat() and index.rows == 4, "two Claude rows and two Grok turns in the day"
    assert [p.path for p in index.projects] == [str(project)]
    project_report = json.loads(Path(index.projects[0].json_file).read_text())
    assert project_report["row_count"] == 2, "the project's Claude row and its own Grok session"
    assert any("left out (grok-build ×1)" in w for w in project_report["warnings"])
    assert len(index.notes) == 2 and not list(folder.glob("*idle*"))
    assert sum("no transcript names its working directory" in note for note in index.notes) == 1
    assert sum("no usage rows in the day" in note for note in index.notes) == 1
    assert lines[0].startswith(f"daily {DAY}: real") and lines[1].startswith("  note: ")
    assert newest_index_file(paths.reports_dir) == folder / "index.json"
    later = paths.reports_dir / "2026-09-20"
    later.mkdir()
    (later / "index.json").write_text("{torn")
    assert newest_index_file(paths.reports_dir) == later / "index.json", "dates sort as names"
    try:
        read_index(later / "index.json")
    except ToolError as exc:
        assert "not a daily index" in str(exc)
    else:
        raise AssertionError("a torn index is a ToolError, never a traceback or a silent None")


def _two_project_day(
    tmp_path: Path,
) -> tuple[Paths, Config, PriceBook, DailyIndex, dict[str, Any], dict[str, Any]]:
    """Two projects (1 000 and 3 000 Claude tokens) plus an unknown plan, one daily run: the index, global and the files."""
    from ..config import Subscription

    paths = paths_in(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    _transcript(paths, one, "o1", str(one), tokens=1_000)
    _transcript(paths, two, "t1", str(two), tokens=3_000)
    config, book = defaults(paths)
    odd = Subscription("no-such-plan", covers=("acme",))
    config = replace(config, subscriptions=(*config.subscriptions, odd))
    lines: list[str] = []
    assert daily_reports(paths, config, book, DAY, paths.reports_dir, Output(lines.append, lines.append)) == 0
    index = read_index(paths.reports_dir / DAY.isoformat() / "index.json")
    overall = json.loads((paths.reports_dir / DAY.isoformat() / "global.json").read_text())
    by_name = {p.project_dir: json.loads(Path(p.json_file).read_text()) for p in index.projects}
    return paths, config, book, index, overall, by_name


def test_per_project_files_split_the_plan_shares_by_api_equivalent(tmp_path: Path) -> None:
    _, _, _, _, overall, by_name = _two_project_day(tmp_path)
    plan = {
        name: next(s for s in r["real"]["subscriptions"] if s["provider"] == "anthropic")
        for name, r in by_name.items()
    }
    whole = next(s for s in overall["real"]["subscriptions"] if s["provider"] == "anthropic")["usd"]
    shares = sorted(s["usd"] for s in plan.values())
    assert (
        abs(sum(shares) - whole) < 1e-9 and abs(shares[1] / shares[0] - 3.0) < 1e-6
    ), "split 1:3 by API-equivalent, summing to the day's share"
    unused = [s for r in by_name.values() for s in r["real"]["subscriptions"] if s["provider"] == "openai"]
    assert unused and all(
        s["usd"] == 0.0 and "global.md" in s["note"] for s in unused
    ), "no project used OpenAI: the share stays global"
    assert all(
        any("split by API-equivalent share" in w and "ADR" not in w for w in r["warnings"])
        for r in by_name.values()
    ), "the note says the policy in words; the decision records are not published"


def test_a_split_share_keeps_its_own_note_and_the_totals_follow_the_split(tmp_path: Path) -> None:
    _, _, _, index, _, by_name = _two_project_day(tmp_path)
    odd = [s for r in by_name.values() for s in r["real"]["subscriptions"] if s["plan"] == "no-such-plan"]
    assert odd and all(
        s["note"].startswith("unknown plan") and "global.md" in s["note"] for s in odd
    ), "the share's own note survives the split"
    for name, report in by_name.items():
        split = sum(s["usd"] for s in report["real"]["subscriptions"])
        assert abs(report["real"]["total_usd"] - (report["real"]["cash_usd"] + split)) < 1e-9, name
        listed = next(p.real_usd for p in index.projects if p.project_dir == name)
        assert abs(listed - report["real"]["total_usd"]) < 1e-9, "the index carries the split total"


def test_a_project_whose_files_cannot_be_written_is_a_failure_and_exit_1(tmp_path: Path) -> None:
    from ..collectors.claude import project_dir

    paths = paths_in(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    _transcript(paths, one, "o1", str(one), tokens=1_000)
    _transcript(paths, two, "t1", str(two), tokens=3_000)
    config, book = defaults(paths)
    lines: list[str] = []
    assert daily_reports(paths, config, book, DAY, paths.reports_dir, Output(lines.append, lines.append)) == 0
    blocked = paths.reports_dir / DAY.isoformat() / (project_dir(paths.claude_home, one).name + ".md")
    blocked.unlink()
    blocked.mkdir()  # a directory where the file must go: that project's write fails
    lines.clear()
    assert daily_reports(paths, config, book, DAY, paths.reports_dir, Output(lines.append, lines.append)) == 1
    index = read_index(paths.reports_dir / DAY.isoformat() / "index.json")
    assert len(index.failures) == 1 and "cannot write" in index.failures[0] and len(index.projects) == 1
    assert any(line.startswith(f"daily {DAY}: FAIL ") for line in lines), "said, not a silent note"
    doctor_lines: list[str] = []
    with mock.patch.object(ops, "schedule_installed", return_value=True):
        assert (
            ops.doctor(paths, config, book, doctor_lines.append) == 1
        ), "a failed project is a red doctor line"
    assert any("!!  daily reports: last" in line and "1 project(s) FAILED" in line for line in doctor_lines)


def test_a_project_whose_rows_are_all_unpriced_says_so(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    project = tmp_path / "proj"
    _transcript(paths, project, "p1", str(project), tokens=0)
    path = write_grok(paths, cwd=str(project))
    document = json.loads(path.read_text())
    for block in (document["session"], *document["turns"]):
        block["primaryModelId"] = "grok-unknown-9"
        block["modelUsage"] = {"grok-unknown-9": {**block["modelUsage"]["grok-4.7"], "costUsdTicks": 0}}
    path.write_text(json.dumps(document))
    config, book = defaults(paths)
    lines: list[str] = []
    daily_reports(paths, config, book, DAY, paths.reports_dir, Output(lines.append, lines.append))
    index = read_index(paths.reports_dir / DAY.isoformat() / "index.json")
    assert not index.projects and any("every row of the day is unpriced" in note for note in index.notes)


def test_removing_an_absent_cron_job_says_not_installed_and_rewrites_nothing() -> None:
    lines: list[str] = []
    with (
        mock.patch.object(ops, "is_macos", return_value=False),
        mock.patch.object(ops, "_crontab", return_value="0 0 * * * backup\n"),
        mock.patch.object(ops, "_crontab_write") as write,
    ):
        assert ops.remove_job(DAILY_JOB, lines.append) == 0
    assert lines == ["not installed"] and not write.called


def test_the_doctor_shows_the_last_daily_run_and_flags_a_torn_index(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    config, book = defaults(paths)
    _transcript(paths, tmp_path / "proj", "p1", str(tmp_path / "proj"))
    lines: list[str] = []
    with mock.patch.object(ops, "schedule_installed", return_value=False):
        ops.doctor(paths, config, book, lines.append)
    assert any("daily reports: none yet" in line for line in lines)
    assert any("scheduled daily reports: not installed" in line for line in lines)
    assert any("last reconcile: never" in line for line in lines)
    daily_reports(paths, config, book, DAY, paths.reports_dir, Output(lambda _: None, lambda _: None))
    lines.clear()
    with mock.patch.object(ops, "schedule_installed", return_value=True):
        ops.doctor(paths, config, book, lines.append)
    assert any(f"ok  daily reports: last {DAY} (1 project(s))" in line for line in lines), lines
    (paths.reports_dir / "2026-12-31").mkdir()
    (paths.reports_dir / "2026-12-31" / "index.json").write_text("[]")
    lines.clear()
    with mock.patch.object(ops, "schedule_installed", return_value=True):
        code = ops.doctor(paths, config, book, lines.append)
    assert code == 1 and any("!!  daily reports:" in line for line in lines), lines


def test_the_daily_job_has_its_own_cron_tag_and_calendar_plist(tmp_path: Path) -> None:
    daily = ops.job_cron_line(DAILY_JOB, DAILY_CADENCE, ["ai-cost", *DAILY_JOB.argv_tail], tmp_path / "log")
    prices = ops.cron_line(3, ["ai-cost", *PRICES_JOB.argv_tail], tmp_path / "log")
    assert daily.startswith("40 6 * * * ai-cost daily >> ") and daily.endswith("# ai-cost:daily-report")
    crontab = "0 0 * * * backup\n" + prices + "\n" + daily + "\n"
    assert cron_has_entry(crontab, DAILY_JOB) and cron_has_entry(crontab)
    assert cron_without_entry(crontab, DAILY_JOB) == ["0 0 * * * backup", prices], "only the daily line goes"
    assert cron_without_entry(crontab) == ["0 0 * * * backup", daily], "only the price-check line goes"
    plist = ops._plist(DAILY_JOB, Cadence(hour=7, minute=5), ["ai-cost", "daily"], tmp_path / "log")
    assert "<key>Label</key><string>ai-cost.daily-report</string>" in plist
    assert "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer>" in plist
    assert "<key>Minute</key><integer>5</integer></dict>" in plist and "StartInterval" not in plist
    assert "<key>StartInterval</key><integer>259200</integer>" in ops._plist(
        PRICES_JOB, Cadence(days=3), ["ai-cost"], tmp_path / "log"
    )
    assert (
        ops.describe_cadence(DAILY_CADENCE) == "daily at 06:40"
        and ops.describe_cadence(Cadence(days=2)) == "every 2 day(s)"
    )


def test_install_and_remove_the_daily_job_on_macos(tmp_path: Path) -> None:
    import subprocess

    paths = paths_in(tmp_path)
    loaded = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    lines: list[str] = []
    with (
        mock.patch.object(Path, "home", return_value=tmp_path),
        mock.patch.object(ops, "is_macos", return_value=True),
        mock.patch("subprocess.run", return_value=loaded),
    ):
        assert ops.install_job(paths, DAILY_JOB, DAILY_CADENCE, ["ai-cost"], lines.append) == 0
        agent = tmp_path / "Library" / "LaunchAgents" / "ai-cost.daily-report.plist"
        assert agent.exists() and ops.schedule_installed(DAILY_JOB) and not ops.schedule_installed(PRICES_JOB)
        assert "<string>daily</string>" in agent.read_text() and any(
            "daily at 06:40" in line for line in lines
        )
        assert ops.remove_job(DAILY_JOB, lines.append) == 0 and not agent.exists()
        assert ops.remove_job(DAILY_JOB, lines.append) == 0 and lines[-1] == "not installed"


def test_the_install_flags_for_the_daily_job_and_their_usage_errors(tmp_path: Path) -> None:
    assert cli._cadence("07:05") == Cadence(hour=7, minute=5) and cli._cadence("23:59") == Cadence(
        hour=23, minute=59
    )
    for bad in ("25:00", "7", "07:60", "x:y", "-1:00"):
        try:
            cli._cadence(bad)
        except UsageError as exc:
            assert "--at" in str(exc)
        else:
            raise AssertionError(bad)
    env = {
        "AI_COST_STATE_DIR": str(tmp_path / "state"),
        "AI_COST_CONFIG_DIR": str(tmp_path / "cfg"),
        "HOME": str(tmp_path),
    }
    err = io.StringIO()
    with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()), redirect_stderr(err):
        assert cli.main(["install", "--at", "07:00"]) == 2
    assert "--at sets the time of --schedule-reports" in err.getvalue()


def test_the_daily_command_parses_its_date_and_writes_where_told(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    _transcript(paths, tmp_path / "proj", "p1", str(tmp_path / "proj"))
    env = {
        "CLAUDE_CONFIG_DIR": str(paths.claude_home),
        "CODEX_HOME": str(paths.codex_home),
        "GEMINI_CLI_HOME": str(paths.gemini_home),
        "GROK_HOME": str(paths.grok_home),
        "AI_COST_STATE_DIR": str(paths.state_dir),
        "AI_COST_CONFIG_DIR": str(paths.user_config_dir),
        "AI_COST_REPORTS_DIR": str(tmp_path / "env-reports"),
        "AI_COST_USAGE_LOG": str(paths.usage_log),
        "AI_COST_OFFLINE": "1",
    }
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
        assert cli.main(["daily", "--date", "nope"]) == 2
        assert cli.main(["daily", "--date", DAY.isoformat(), "--quiet"]) == 0
        assert cli.main(["daily", "--date", DAY.isoformat(), "--out", str(tmp_path / "flag-reports")]) == 0
    assert "--date" in err.getvalue() and "nope" in err.getvalue()
    assert (tmp_path / "env-reports" / DAY.isoformat() / "index.json").exists(), "AI_COST_REPORTS_DIR"
    assert (tmp_path / "flag-reports" / DAY.isoformat() / "global.md").exists(), "--out wins"
    assert out.getvalue().count(f"daily {DAY}:") == 1, "--quiet printed nothing, the other run one line"


def test_gap_and_billed_tokens_arithmetic() -> None:
    assert abs(gap_pct(154.25, 156.11) - (-1.1915)) < 0.001
    assert gap_pct(1.0, 0.0) == 100.0 and gap_pct(0.0, 0.0) == 0.0 and gap_pct(0.0, 5.0) == -100.0
    tokens = Tokens(
        input=10, cached_input=4, output=2, prompt=5, cached=1, cache_hit=3, cache_miss=2, cache_read=7
    )
    assert (
        billed_tokens(tokens) == 10 + 2 + 7 + 5 + 3 + 2
    ), "cached_input and cached are inside input and prompt"


def test_reconcile_compares_the_local_count_with_the_reported_figure(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_grok(paths)
    config, book = defaults(paths)
    request = ReportRequest(since=iso(BASE), until=iso(BASE + timedelta(hours=1)))
    local = GROK_TICKS / TICKS_PER_USD
    outcome = reconcile(request, paths, config, book, Reported("xai", local, 165_943 + 173 + 5_241))
    assert outcome.rows == 1 and abs(outcome.local_usd - local) < 1e-9 and outcome.local_tokens == 171_357
    assert (
        outcome.plan_rows == 0 and outcome.skipped == 0 and outcome.warnings
    ), "the report's warnings travel"
    assert any(text.startswith("  warn      ") for text in describe(outcome))
    assert outcome.within_tolerance and outcome.usd_gap_pct == 0.0 and outcome.tokens_gap_pct == 0.0
    off = reconcile(request, paths, config, book, Reported("xai", local * 1.2))
    assert not off.within_tolerance and abs(off.usd_gap_pct - (-16.67)) < 0.01 and off.tokens_gap_pct is None
    assert off.unknown_billing == 0
    mixed = replace(config, billing_rules={**config.billing_rules, "xai": ""})
    unknown = reconcile(request, paths, mixed, book, Reported("xai", local))
    assert unknown.unknown_billing == 1 and unknown.local_usd == 0.0 and unknown.local_tokens == 0
    assert unknown.rows == 0, "an unknown row is neither cash nor a console token: it is named, not counted"
    assert any("1 row(s) of unknown billing" in text for text in describe(unknown))
    assert (
        reconcile(request, paths, config, book, Reported("google", 0.0)).rows == 0
    ), "no rows: zero against zero"


def test_run_reconcile_keeps_a_history_the_doctor_reads_and_flags_when_torn(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_grok(paths)
    config, book = defaults(paths)
    request = ReportRequest(since=iso(BASE), until=iso(BASE + timedelta(hours=1)))
    local = GROK_TICKS / TICKS_PER_USD
    outcome = reconcile(request, paths, config, book, Reported("xai", local))
    lines: list[str] = []
    assert run_reconcile(request, paths, config, book, Reported("xai", local * 1.2), lines.append) == 1
    assert lines[0].startswith("reconcile xai ") and lines[-1].startswith("reconcile: ABOVE TOLERANCE")
    assert run_reconcile(request, paths, config, book, Reported("xai", local, 171_357), lines.append) == 0
    assert lines[-1] == "reconcile: within tolerance" and any("171 357 tokens" in line for line in lines)
    last = last_reconciliation(paths)
    assert last is not None and last.within_tolerance and last.reported_tokens == 171_357 and last.checked_at
    assert last.warnings == outcome.warnings, "the history keeps what the count was built on"
    history = paths.state_dir / "reconcile.jsonl"
    assert len(history.read_text().splitlines()) == 2
    older = json.loads(history.read_text().splitlines()[0])
    for key in ("warnings", "skipped", "plan_rows", "unknown_billing"):
        older.pop(key, None)
    with history.open("a") as handle:
        handle.write(json.dumps(older) + "\n")
    old = last_reconciliation(paths)
    assert (
        old is not None and old.warnings == () and old.plan_rows == 0
    ), "a 2.2.0 line without the new keys reads"
    with history.open("a") as handle:
        handle.write("{torn\n")
    try:
        last_reconciliation(paths)
    except ToolError as exc:
        assert "not a reconciliation" in str(exc)
    else:
        raise AssertionError("a torn last line is a ToolError, never a silent None")
    doctor_lines: list[str] = []
    with mock.patch.object(ops, "schedule_installed", return_value=False):
        assert ops.doctor(paths, config, book, doctor_lines.append) == 1
    assert any("!!  last reconcile:" in line for line in doctor_lines)


def test_reconcile_counts_a_ledger_rows_cash_but_never_its_tokens() -> None:
    from ..models import Billing, Provider, RowKind, Scope, UsageRow
    from ..reconcile import _console_bills

    at = BASE + timedelta(minutes=5)
    session = UsageRow(
        Provider.OPENAI,
        "gpt-5.5",
        RowKind.SESSION,
        at,
        "sid",
        Billing.API_SETTLED,
        Tokens(input=10, output=1),
    )
    ledger = replace(session, kind=RowKind.LEDGER, billing=Billing.API, cost_reported=0.5, scope=Scope())
    assert _console_bills(session) and not _console_bills(ledger), "the ledger's tokens are the session's"
    assert not _console_bills(replace(session, billing=Billing.SUBSCRIPTION))


def test_reconcile_leaves_plan_rows_out_of_the_console_comparison(tmp_path: Path) -> None:
    from .fixtures import write_codex

    paths = paths_in(tmp_path)
    write_codex(paths)  # sid-plan on a ChatGPT plan, sid-api and sid-key-noledger on the key or unknown
    config, book = defaults(paths)
    request = ReportRequest(since=iso(BASE), until=iso(BASE + timedelta(hours=1)))
    outcome = reconcile(request, paths, config, book, Reported("openai", 1.0))
    assert (
        outcome.plan_rows == 1 and outcome.unknown_billing == 4 and outcome.rows == 0
    ), "mixed rule: no API row"
    assert outcome.local_tokens == 0, "the plan session's tokens are not in the count, nor the unknown ones"
    assert any("plan row(s) are not counted" in text for text in describe(outcome))


def test_the_reconcile_command_validates_its_flags(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_grok(paths)
    env = {
        "CLAUDE_CONFIG_DIR": str(paths.claude_home),
        "CODEX_HOME": str(paths.codex_home),
        "GEMINI_CLI_HOME": str(paths.gemini_home),
        "GROK_HOME": str(paths.grok_home),
        "AI_COST_STATE_DIR": str(paths.state_dir),
        "AI_COST_CONFIG_DIR": str(paths.user_config_dir),
        "AI_COST_USAGE_LOG": str(paths.usage_log),
        "AI_COST_OFFLINE": "1",
    }
    window = ["--since", iso(BASE), "--until", iso(BASE + timedelta(hours=1))]
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
        assert cli.main(["reconcile", "--provider", "Not An Id", "--usd", "1", *window]) == 2
        assert cli.main(["reconcile", "--provider", "xai", "--usd", "-1", *window]) == 2
        assert cli.main(["reconcile", "--provider", "xai", "--usd", "nan", *window]) == 2
        assert cli.main(["reconcile", "--provider", "xai", "--usd", "1", "--tokens", "-5", *window]) == 2
        assert cli.main(["reconcile", "--provider", "xai", "--usd", "0.356498", *window]) == 0
        assert cli.main(["reconcile", "--provider", "xai", "--usd", "1", *window]) == 1
    text = err.getvalue()
    assert "--provider" in text and "--usd" in text and "--tokens" in text
    assert out.getvalue().count("reconcile xai ") == 2


def test_a_daily_index_round_trips_through_json(tmp_path: Path) -> None:
    from ..render import plain

    index = DailyIndex("2026-09-19", "now", ("a", "b"), "/d", 1.5, 2.5, 0.5, 3, (), ("note",))
    path = tmp_path / "index.json"
    path.write_text(json.dumps(plain(index)))
    assert read_index(path) == index


def test_reconcile_says_rows_of_clients_outside_scope_apart_from_the_rule_to_set() -> None:
    from ..reconcile import Reconciliation, describe

    base = Reconciliation("openai", ("a", "b"), 24.0, 0, 0.0, 0, 1.0, None, 5.0)
    only = describe(replace(base, unknown_billing=2, outside_scope=2))
    assert any("2 row(s) of clients outside scope are left unknown on purpose" in text for text in only), only
    assert not any("set providers.openai.billing" in text for text in only), "no rule can place them"
    mixed = describe(replace(base, unknown_billing=3, outside_scope=2))
    assert any(
        "1 row(s) of unknown billing" in t and "set providers.openai.billing" in t for t in mixed
    ), mixed
