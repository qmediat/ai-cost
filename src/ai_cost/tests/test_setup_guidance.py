"""What the tool tells a new user to set up: install's next steps, the help texts and the doctor's setup lines."""

from __future__ import annotations

import argparse
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock

from .. import cli
from .. import ops as ops_module
from ..collectors.github_bill import BillProbe
from ..config import Config, PriceBook, builtin_config, load_config, load_pricebook, parse_config
from ..groups import real_group
from ..models import BillAccount, Billing, BillScope, Provider, RowKind, Tokens, UsageRow
from ..onboarding import (
    SETUP_GUIDE,
    START_HERE,
    Check,
    Found,
    Mark,
    ProviderUse,
    SourceFiles,
    budget_check,
    github_checks,
    init_config_lines,
    provider_use,
    setup_checks,
    source_checks,
    source_files,
)
from ..ops import ReportRequest, build_report, doctor, init_config
from ..plugins import Loaded
from ..timeutil import iso
from .fixtures import WINDOW, defaults, paths_in, write_claude_session, write_recent_claude_session

_PLAN = [{"plan": "claude-max-20x", "seats": 1, "covers": ["anthropic"], "attribution": "time"}]
_LOG_LINE = {
    "schema": 1,
    "at": "2026-09-25T10:00:00Z",
    "provider": "openai",
    "model": "gpt-5.5",
    "tokens": {"input": 1200, "output": 300},
}


def _row(
    provider: Provider,
    billing: Billing,
    ref: str = "r",
    kind: RowKind = RowKind.SESSION,
    source: str = "cli",
    client: str = "",
) -> UsageRow:
    tokens = Tokens(input=10, output=2)
    return UsageRow(provider, "model-x", kind, None, ref, billing, tokens, source=source, client=client)


def _config(
    rules: dict[str, str],
    plans: list[dict[str, object]] | None = None,
    outside: tuple[str, ...] = (),
    **github: object,
) -> Config:
    raw = builtin_config()
    raw["subscriptions"] = plans or []
    raw["outside_scope_clients"] = list(outside)
    for name, rule in rules.items():
        raw["providers"].setdefault(name, {})["billing"] = rule
    raw["providers"]["github"].update(github)
    return parse_config(raw, "test")


def _files(provider: str, count: int, says_billing: bool = False) -> SourceFiles:
    found = Found.FILES if count else Found.ABSENT
    return SourceFiles(
        f"{provider} files", provider, count, Path("/nowhere"), says_billing, "HOME_VAR", found, "cli"
    )


def _book() -> PriceBook:
    return load_pricebook(paths_in(Path("/nonexistent-ai-cost-home")))


def _doctor_lines(paths_root: Path, config: Config | None = None) -> tuple[int, list[str]]:
    """The doctor over ``paths_root`` with no plugins and no scheduler: only the tool's own lines."""
    paths = paths_in(paths_root)
    lines: list[str] = []
    with (
        mock.patch.object(ops_module, "load_configured_plugins", return_value=Loaded()),
        mock.patch.object(ops_module, "schedule_installed", return_value=False),
    ):
        code = doctor(paths, config or load_config(paths), load_pricebook(paths), lines.append)
    return code, lines


def _problems(lines: list[str]) -> list[str]:
    """The red lines, without the two price-age checks whose verdict depends on today's date."""
    dated = ("prices checked_at", "price valid until")
    return [line for line in lines if line.startswith("  !!") and not any(d in line for d in dated)]


def _problem_texts(checks: list[Check]) -> list[str]:
    return [c.text for c in checks if c.mark is Mark.PROBLEM]


def test_init_config_prints_the_file_then_what_to_put_in_it(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    lines: list[str] = []
    assert init_config(paths, False, lines.append) == 0
    assert lines[0] == f"written {paths.user_config_file()}"
    text = "\n".join(lines)
    for needed in (
        '"subscriptions"',
        '"covers"',
        "ai-cost prices show",
        '"providers"',
        "billing",
        "ai-cost doctor",
    ):
        assert needed in text, needed
    assert lines[-1] == f"guide: {SETUP_GUIDE}"
    lines.clear()
    assert init_config(paths, False, lines.append) == 0
    assert lines == init_config_lines(paths.user_config_file(), written=False)
    assert (
        "ai-cost doctor" in lines[0] and SETUP_GUIDE in lines[0]
    ), "an existing file still says where to go next"


def _main(argv: list[str], tmp_path: Path) -> tuple[int, str]:
    env = {"AI_COST_CONFIG_DIR": str(tmp_path / "cfg"), "AI_COST_STATE_DIR": str(tmp_path / "state")}
    err = io.StringIO()
    with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()), redirect_stderr(err):
        return cli.main(argv), err.getvalue()


def test_install_without_an_action_is_a_usage_error_that_says_where_to_start(tmp_path: Path) -> None:
    code, err = _main(["install"], tmp_path)
    assert code == 2 and "nothing to do" in err and "--init-config" in err, err
    code, err = _main(["install", "--force"], tmp_path)
    assert code == 2 and "--force" in err and "--init-config" in err, err
    assert not (tmp_path / "cfg").exists(), "a refused command writes nothing"


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


def test_every_install_option_says_what_it_does() -> None:
    install = _subparsers()["install"]
    silent = [a.option_strings for a in install._actions if a.option_strings and not a.help]
    assert not silent, f"options without help: {silent}"


def test_the_help_keeps_the_guide_url_whole_at_every_terminal_width() -> None:
    for columns in ("40", "70", "80", "100", "200"):
        with mock.patch.dict(os.environ, {"COLUMNS": columns}):
            top = cli.build_parser().format_help()
        lines = top.splitlines()
        assert f"guide: {SETUP_GUIDE}" in lines, (columns, top)
        assert all(line in lines for line in START_HERE), (columns, top)


def test_the_help_names_no_internal_record() -> None:
    texts = [cli.build_parser().format_help(), *(sub.format_help() for sub in _subparsers().values())]
    assert not [
        t for t in texts if "ADR-" in t
    ], "the decision records are not published: help never cites them"


def test_doctor_on_an_empty_home_says_nothing_was_found_and_nothing_else(tmp_path: Path) -> None:
    code, lines = _doctor_lines(tmp_path)
    for label, variable in (("Claude transcripts", "CLAUDE_CONFIG_DIR"), ("Codex rollouts", "CODEX_HOME")):
        said = [line for line in lines if line.startswith(f"  --  {label}: none (")]
        assert said and "does not exist" in said[0] and variable in said[0], (label, lines)
    assert code == 1 and [line for line in _problems(lines) if "no usage found" in line], lines
    assert any(line.startswith("  ok  state dir") and "created on the first write" in line for line in lines)
    assert any(line.startswith("  --  budgets for monitor: none") for line in lines), "the shipped budgets"


def test_doctor_names_the_missing_claude_rule_and_is_quiet_once_it_is_set(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    _, lines = _doctor_lines(tmp_path, _config({"anthropic": "mixed"}))
    red = _problems(lines)
    assert len(red) == 1, red
    assert "providers.anthropic.billing names no rule (mixed)" in red[0] and "Claude plan" in red[0], red
    _, lines = _doctor_lines(tmp_path, _config({"anthropic": "subscription"}, _PLAN))
    assert _problems(lines) == [], lines
    assert any(line.startswith("  ok  subscription claude-max-20x ×1") for line in lines)
    assert any(
        line.startswith("  --  Codex rollouts: none (") for line in lines
    ), "a CLI not used is information"


def test_doctor_counts_the_window_rows_of_each_provider(tmp_path: Path) -> None:
    write_recent_claude_session(paths_in(tmp_path), tmp_path / "proj", messages=3)
    _, lines = _doctor_lines(tmp_path, _config({"anthropic": "mixed"}))
    said = [line for line in lines if "anthropic: 3 row(s) in the last 24 h" in line]
    assert len(said) == 1 and said[0].startswith("  --  ") and "3 of unknown billing" in said[0], lines
    assert (
        len([line for line in _problems(lines) if "anthropic" in line]) == 1
    ), "the missing rule is said once"
    _, lines = _doctor_lines(tmp_path, _config({"anthropic": "subscription"}, _PLAN))
    assert any(
        line.startswith("  ok  anthropic: 3 row(s) in the last 24 h — 3 on a plan") for line in lines
    ), lines
    assert _problems(lines) == [], lines


def test_doctor_flags_a_plan_rule_that_no_subscription_covers(tmp_path: Path) -> None:
    write_claude_session(paths_in(tmp_path), tmp_path / "proj")
    _, lines = _doctor_lines(tmp_path, _config({"anthropic": "subscription"}))
    red = _problems(lines)
    assert len(red) == 1 and "anthropic: used on a plan but no subscription covers anthropic" in red[0], red
    assert any(line.startswith("  --  subscriptions: none declared") for line in lines)


def test_a_plan_without_covers_pays_for_the_provider_the_registry_names() -> None:
    plan = [
        {"plan": "claude-max-20x", "seats": 1}
    ]  # covers is optional: the registry says the plan is anthropic's
    rows = [_row(Provider.ANTHROPIC, Billing.SUBSCRIPTION)]
    assert _problem_texts(setup_checks([], _config({"anthropic": "subscription"}, plan), _book(), rows)) == []


def test_rows_a_plugin_placed_need_no_rule_for_their_cli() -> None:
    rows = [
        _row(Provider.ANTHROPIC, Billing.SUBSCRIPTION),
        _row(Provider.ANTHROPIC, Billing.SUBSCRIPTION, "b"),
    ]
    config = _config({"anthropic": "mixed"}, _PLAN)
    checks = setup_checks([_files("anthropic", 4)], config, _book(), rows)
    assert _problem_texts(checks) == [], "every row of the window is placed: no rule is missing"
    alone = setup_checks([_files("anthropic", 4)], config, _book(), [])
    assert len(_problem_texts(alone)) == 1, "no rows to prove it: the rule is still asked for"
    other = [_row(Provider.ANTHROPIC, Billing.API, "log", source="usage-log")]
    by_log = setup_checks([_files("anthropic", 4)], config, _book(), other)
    assert (
        len(_problem_texts(by_log)) == 1
    ), "another source's rows of the provider prove nothing about the CLI"


def test_a_cli_home_that_cannot_be_listed_is_counted_never_a_crash(tmp_path: Path) -> None:
    if os.geteuid() == 0:  # root lists any directory: the case cannot be made here
        return
    paths = paths_in(tmp_path)
    (paths.codex_home / "sessions").mkdir(parents=True)
    (paths.claude_home / "projects").mkdir(parents=True)
    for home in (paths.codex_home, paths.claude_home):
        home.chmod(0)
    try:
        found = {f.label: f.found for f in source_files(paths)}
        config, book = defaults(paths)
        report = build_report(ReportRequest(all_projects=True, hours=24.0, groups=()), paths, config, book)
    finally:
        for home in (paths.codex_home, paths.claude_home):
            home.chmod(0o755)
    assert found["Codex rollouts"] is Found.UNREADABLE and found["Claude transcripts"] is Found.UNREADABLE
    listed = [s for s in report.skipped if s.reason.startswith("cannot list")]
    assert {s.source for s in listed} == {"codex", "claude"}, "the names each collector's own skips carry"
    unread = [SourceFiles("x", "openai", 0, paths.codex_home, True, "CODEX_HOME", Found.UNREADABLE, "codex")]
    assert not [t for t in _problem_texts(source_checks(unread, [], 0)) if t.startswith("no usage found")]


def test_one_project_that_cannot_be_listed_leaves_the_others_read(tmp_path: Path) -> None:
    if os.geteuid() == 0:  # root lists any directory: the case cannot be made here
        return
    paths = paths_in(tmp_path)
    write_recent_claude_session(paths, tmp_path / "readable", messages=2)
    locked = write_recent_claude_session(paths, tmp_path / "locked", messages=1).parent
    locked.chmod(0)
    try:
        config, book = defaults(paths)
        request = ReportRequest(all_projects=True, hours=24.0, groups=("api",))
        report = build_report(request, paths, config, book)
    finally:
        locked.chmod(0o755)
    assert len(report.rows) == 2, "the readable project's rows are all there"
    assert [(s.source, s.path) for s in report.skipped if s.reason.startswith("cannot list")] == [
        ("claude", str(locked))
    ], report.skipped


def test_doctor_names_a_project_directory_it_cannot_read(tmp_path: Path) -> None:
    if os.geteuid() == 0:  # root lists any directory: the case cannot be made here
        return
    paths = paths_in(tmp_path)
    locked = write_recent_claude_session(paths, tmp_path / "locked", messages=1).parent
    locked.chmod(0)
    try:
        alone = next(f for f in source_files(paths) if f.label == "Claude transcripts")
        write_recent_claude_session(paths, tmp_path / "readable", messages=2)
        _, lines = _doctor_lines(tmp_path, _config({"anthropic": "subscription"}, _PLAN))
    finally:
        locked.chmod(0o755)
    assert alone.found is Found.UNREADABLE and alone.unlisted == 1
    assert not [t for t in _problem_texts(source_checks([alone], [], 0)) if t.startswith("no usage found")]
    said = [line for line in _problems(lines) if line.startswith("  !!  Claude transcripts: 1 under")]
    assert len(said) == 1 and "1 directory(ies) under it cannot be read" in said[0], lines


def test_the_report_header_counts_the_usage_report_and_the_allowance_switch_as_paid(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True)
    paths.user_config_file().write_text("{}")
    book = _book()
    unpaid = _config({"github": "subscription"})
    assert [w for w in ops_module._config_warnings(paths, unpaid, book) if "covers github" in w]
    for paid in ({"actions_plan_exhausted": True}, {"bill": {"scope": "organization", "name": "acme"}}):
        assert (
            ops_module._config_warnings(paths, _config({"github": "subscription"}, **paid), book) == []
        ), paid


def test_a_retired_github_key_is_said_in_the_header_and_by_doctor(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True)
    paths.user_config_file().write_text("{}")
    retired = _config({}, copilot_plan_exhausted=True)
    said = ops_module._config_warnings(paths, retired, _book())
    assert [
        w for w in said if w.startswith("providers.github.copilot_plan_exhausted is no longer read")
    ], said
    problems = _problem_texts(github_checks(retired, [], None))
    assert [t for t in problems if t.startswith("providers.github.copilot_plan_exhausted: no longer read")]


def test_a_plan_with_covers_pays_only_for_what_they_name() -> None:
    plan = [{"plan": "claude-max-20x", "seats": 1, "covers": ["openai"], "attribution": "time"}]
    rows = [_row(Provider.ANTHROPIC, Billing.SUBSCRIPTION)]
    red = _problem_texts(setup_checks([], _config({}, plan), _book(), rows))
    assert [
        t for t in red if t.startswith("anthropic: used on a plan")
    ], "covers given: the registry does not add"


def test_github_rows_on_a_plan_are_paid_for_by_the_usage_report_or_the_allowance_switch() -> None:
    rows = [_row(Provider.GITHUB, Billing.SUBSCRIPTION)]
    unpaid = _problem_texts(setup_checks([], _config({}), _book(), rows))
    assert [t for t in unpaid if t.startswith("github: used on a plan")], unpaid
    for paid in ({"actions_plan_exhausted": True}, {"bill": {"scope": "user", "name": "someone"}}):
        assert _problem_texts(setup_checks([], _config({}, **paid), _book(), rows)) == [], paid


def test_doctor_says_whether_the_usage_report_can_be_read() -> None:
    account = BillAccount(BillScope.ORGANIZATION, "acme")
    config = _config({}, bill={"scope": "organization", "name": "acme"})
    good = github_checks(config, [], BillProbe(account, "", date(2026, 9, 25)))
    assert [(c.mark, c.text) for c in good] == [
        (Mark.OK, "GitHub usage report (organization acme): readable, newest day with lines 2026-09-25")
    ]
    bad = github_checks(config, [], BillProbe(account, "HTTP 404: no access"))
    assert bad[0].mark is Mark.PROBLEM and "not readable — HTTP 404: no access" in bad[0].text
    counted = github_checks(_config({}), provider_use([_row(Provider.GITHUB, Billing.SUBSCRIPTION)]), None)
    assert [c.mark for c in counted] == [Mark.INFO] and "counted, not priced" in counted[0].text


def test_doctor_says_when_the_state_dir_cannot_be_written(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.state_dir.write_text("a file where the state directory should be")
    _, lines = _doctor_lines(tmp_path)
    assert [
        line for line in _problems(lines) if f"state dir {paths.state_dir} is not writable" in line
    ], lines


def test_a_dangling_link_as_the_state_dir_is_not_writable(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.state_dir.symlink_to(tmp_path / "nowhere" / "state")
    _, lines = _doctor_lines(tmp_path)
    assert [
        line for line in _problems(lines) if f"state dir {paths.state_dir} is not writable" in line
    ], lines


def test_a_cli_home_that_exists_empty_or_unreadable_is_told_apart(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    (paths.codex_home / "sessions").mkdir(parents=True)
    by_label = {f.label: f for f in source_files(paths)}
    assert (
        by_label["Codex rollouts"].found is Found.EMPTY
        and by_label["Claude transcripts"].found is Found.ABSENT
    )
    empty = next(c for c in source_checks(list(by_label.values()), [], 0) if c.text.startswith("Codex"))
    assert empty.mark is Mark.INFO and "0 under" in empty.text and "CODEX_HOME" in empty.text
    if os.geteuid() == 0:  # root reads any directory: the unreadable case cannot be made here
        return
    (paths.codex_home / "sessions").chmod(0)
    try:
        unreadable = next(f for f in source_files(paths) if f.label == "Codex rollouts")
    finally:
        (paths.codex_home / "sessions").chmod(0o755)
    assert unreadable.found is Found.UNREADABLE
    assert _problem_texts(source_checks([unreadable], [], 0))[0].startswith("Codex rollouts: ")


def test_rows_in_the_window_or_a_failed_collection_are_not_no_usage() -> None:
    nothing = [_files("anthropic", 0)]
    assert [t for t in _problem_texts(source_checks(nothing, [], 0)) if t.startswith("no usage found")]
    assert _problem_texts(source_checks(nothing, [], 2)) == [], "a plugin's rows are usage"
    assert _problem_texts(source_checks(nothing, [], None)) == [], "a failed collection proves nothing"


def test_the_usage_logs_count_as_a_source_when_they_have_lines(tmp_path: Path) -> None:
    default, listed = tmp_path / "usage.jsonl", tmp_path / "app.jsonl"
    empty = source_checks([_files("anthropic", 0)], [default, listed], 0)
    assert (
        empty[1].mark is Mark.INFO and SETUP_GUIDE in empty[1].text
    ), "absent: where to read how to write one"
    assert empty[2].mark is Mark.INFO and "listed in usage_logs" in empty[2].text
    assert [t for t in _problem_texts(empty) if t.startswith("no usage found")]
    listed.write_text(json.dumps(_LOG_LINE) + "\n")
    found = source_checks([_files("anthropic", 0)], [default, listed, listed], 0)
    assert (
        len(found) == 3 and found[2].mark is Mark.OK and not _problem_texts(found)
    ), "listed once, and usage"


def test_a_usage_log_that_cannot_be_read_is_a_problem_not_a_source(tmp_path: Path) -> None:
    folder = tmp_path / "usage.jsonl"
    folder.mkdir()
    checks = source_checks([_files("anthropic", 0)], [folder], 0)
    assert [t for t in _problem_texts(checks) if "not a readable file" in t], checks
    assert not [
        t for t in _problem_texts(checks) if t.startswith("no usage found")
    ], "an unread log may hold usage"
    if os.geteuid() == 0:  # root reads any file: the unreadable case cannot be made here
        return
    locked = tmp_path / "locked.jsonl"
    locked.write_text(json.dumps(_LOG_LINE) + "\n")
    locked.chmod(0)
    try:
        checks = source_checks([_files("anthropic", 0)], [locked], 0)
    finally:
        locked.chmod(0o644)
    assert checks[1].mark is Mark.PROBLEM and "not a readable file" in checks[1].text, checks


def test_unknown_rows_are_a_problem_once_and_name_the_rule_to_set() -> None:
    rows = [_row(Provider.XAI, Billing.UNKNOWN), _row(Provider.XAI, Billing.API, "k")]
    alone = setup_checks([], _config({"xai": "mixed"}), _book(), rows)
    use = [c for c in alone if c.text.startswith("xai:")]
    assert len(use) == 1 and use[0].mark is Mark.PROBLEM and "1 of unknown billing" in use[0].text, use
    assert "providers.xai.billing = mixed" in use[0].text and '"api" when you pay per token' in use[0].text
    with_files = setup_checks([_files("xai", 1)], _config({"xai": "mixed"}), _book(), rows)
    marks = [(c.mark, c.text.split(":")[0]) for c in with_files if "xai" in c.text]
    assert marks == [
        (Mark.PROBLEM, "xai files"),
        (Mark.INFO, "xai"),
    ], "said once: the files line names the rule"


def test_rows_on_a_plan_need_a_plan_that_pays_for_their_provider() -> None:
    rows = [_row(Provider.OPENAI, Billing.SUBSCRIPTION), _row(Provider.OPENAI, Billing.API, "k")]
    checks = setup_checks([_files("openai", 2, says_billing=True)], _config({}), _book(), rows)
    assert _problem_texts(checks) == [
        "openai: used on a plan but no subscription covers openai — the plan's fee is missing from real: add it "
        'under subscriptions with "covers": ["openai"]'
    ]
    covered = _config({}, [{"plan": "chatgpt-plus", "seats": 1, "covers": ["openai"], "attribution": "time"}])
    assert _problem_texts(setup_checks([], covered, _book(), rows)) == []


def test_provider_use_counts_every_billing_once() -> None:
    rows = [
        _row(Provider.OPENAI, Billing.SUBSCRIPTION),
        _row(Provider.OPENAI, Billing.API_SETTLED),
        _row(Provider.OPENAI, Billing.API),
        _row(Provider.GOOGLE, Billing.UNKNOWN),
    ]
    assert provider_use(rows) == [ProviderUse("google", unknown=1), ProviderUse("openai", 1, 2, 0)]


def test_the_budgets_in_force_are_shown() -> None:
    budgets = _config({}).budgets
    assert budget_check(budgets).text.startswith("budgets for monitor: none")
    set_ = replace(budgets, daily_usd=150.0, monthly_usd=0.0, per_provider_daily_usd={"google": 0.0})
    check = budget_check(set_)
    assert check.mark is Mark.INFO and "daily 150 USD" in check.text and "google 0 USD a day" in check.text
    assert "monthly" not in check.text, "0 = no check: not shown as a budget"


def test_the_report_names_each_provider_with_unknown_rows_and_its_rule(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    mixed = replace(config, billing_rules={**config.billing_rules, "anthropic": ""})
    request = ReportRequest(
        since=iso(WINDOW.start), until=iso(WINDOW.end), all_projects=True, groups=("real",)
    )
    report = build_report(request, paths, mixed, book)
    assert report.real is not None and report.real.unknown_billing > 0
    assert report.real.unknown_by_provider == {"anthropic": report.real.unknown_billing}
    said = [w for w in report.warnings if "unknown billing" in w]
    assert len(said) == 1 and f"anthropic {report.real.unknown_billing}" in said[0], said
    assert "providers.anthropic.billing — " in said[0] and "<name>" not in said[0], said


def test_unknown_rows_are_counted_per_provider_and_a_ledger_row_apart(tmp_path: Path) -> None:
    config, book = defaults(paths_in(tmp_path))
    rows = [_row(Provider.XAI, Billing.UNKNOWN), _row(Provider.XAI, Billing.UNKNOWN, "b")]
    rows += [
        _row(Provider.GOOGLE, Billing.UNKNOWN, "c"),
        _row(Provider.OPENAI, Billing.API, "l", RowKind.LEDGER),
    ]
    group = real_group(rows, book, config, WINDOW)
    assert group.unknown_by_provider == {"xai": 2, "google": 1} and group.unfigured_ledger == 1
    assert group.unknown_billing == 4, "the per-provider rows and the ledger row, each counted once"
    said = ops_module._billing_warnings(group, rows, config)[0]
    assert (
        "xai 2" in said and "1 ledger row(s) without a figure" in said and "providers.openai" not in said
    ), said


_APP = "codex_work_desktop"


def test_unknown_rows_of_clients_outside_scope_are_said_never_asked_for() -> None:
    app = [
        _row(Provider.OPENAI, Billing.UNKNOWN, "a", client=_APP),
        _row(Provider.OPENAI, Billing.UNKNOWN, "b", client=_APP),
    ]
    config = _config({}, outside=(_APP,))
    use = [c for c in setup_checks([], config, _book(), app) if c.text.startswith("openai:")]
    assert len(use) == 1 and use[0].mark is Mark.INFO, use
    assert "2 of unknown billing (2 from clients outside scope, left unknown on purpose)" in use[0].text, use
    mixed = [*app, _row(Provider.OPENAI, Billing.UNKNOWN, "c", client="codex_exec")]
    assert [
        c for c in setup_checks([], config, _book(), mixed) if c.mark is Mark.PROBLEM
    ], "codex_exec is in scope"
    unlisted = setup_checks([], _config({}), _book(), app)
    assert [
        c for c in unlisted if c.mark is Mark.PROBLEM
    ], "a client the config does not name is not outside scope"
    unnamed = [_row(Provider.OPENAI, Billing.UNKNOWN, "u", client="")]
    blank = setup_checks([], replace(_config({}), outside_scope_clients=("",)), _book(), unnamed)
    assert [c for c in blank if c.mark is Mark.PROBLEM], "a row that names no client is never outside scope"


def test_the_report_header_says_outside_scope_rows_apart_from_the_ones_to_fix(tmp_path: Path) -> None:
    _, book = defaults(paths_in(tmp_path))
    config = _config({}, outside=(_APP,))
    app = [_row(Provider.OPENAI, Billing.UNKNOWN, "a", client=_APP)]
    alone = ops_module._billing_warnings(real_group(app, book, config, WINDOW), app, config)
    assert len(alone) == 1 and f"clients outside scope ({_APP})" in alone[0], alone
    assert "providers.openai.billing" not in alone[0], "nothing to fix for rows left unknown on purpose"
    mixed = [*app, _row(Provider.OPENAI, Billing.UNKNOWN, "c", client="codex_exec")]
    said = ops_module._billing_warnings(real_group(mixed, book, config, WINDOW), mixed, config)
    assert len(said) == 2 and said[1].startswith("1 usage row(s) with unknown billing"), said
    assert "openai 1" in said[1] and "providers.openai.billing — " in said[1], said


def test_a_plugin_row_of_a_client_outside_scope_keeps_its_unknown_billing() -> None:
    config = _config({"xai": "api"}, outside=(_APP,))
    rows = [_row(Provider.XAI, Billing.UNKNOWN, "a", client=_APP), _row(Provider.XAI, Billing.UNKNOWN, "b")]
    billed = {row.ref: row.billing for row in ops_module._with_billing_rules(rows, config)}
    assert billed == {"a": Billing.UNKNOWN, "b": Billing.API}


def test_only_unknown_rows_of_a_client_outside_scope_count_as_outside() -> None:
    rows = [
        _row(Provider.OPENAI, Billing.SUBSCRIPTION, "p", client=_APP),
        _row(Provider.OPENAI, Billing.UNKNOWN, "u"),
    ]
    use = provider_use(rows, (_APP,))
    assert use == [
        ProviderUse("openai", on_plan=1, unknown=1, outside=0)
    ], "a plan row of the app is not unknown"


def test_the_doctor_asks_only_for_the_rows_in_scope() -> None:
    config = _config({}, outside=(_APP,))
    rows = [
        _row(Provider.OPENAI, Billing.UNKNOWN, "a", client=_APP),
        _row(Provider.OPENAI, Billing.UNKNOWN, "c"),
    ]
    red = _problem_texts(setup_checks([], config, _book(), rows))
    assert len(red) == 1 and "— the 1 in scope stay out of real: set the rule" in red[0], red


def test_the_outside_scope_line_says_the_api_group_still_prices_them(tmp_path: Path) -> None:
    _, book = defaults(paths_in(tmp_path))
    config = _config({}, outside=(_APP,))
    app = [_row(Provider.OPENAI, Billing.UNKNOWN, "a", client=_APP)]
    said = ops_module._billing_warnings(real_group(app, book, config, WINDOW), app, config)
    assert said and "the API-only group still prices their tokens" in said[0], said


def test_doctor_names_each_client_outside_scope_with_its_rows() -> None:
    config = _config({}, outside=(_APP, "codex_desktop_typo"))
    rows = [_row(Provider.OPENAI, Billing.UNKNOWN, "a", client=_APP)]
    lines = [
        c.text for c in setup_checks([], config, _book(), rows) if c.text.startswith("clients outside scope")
    ]
    assert lines == [
        f"clients outside scope: {_APP} (1 row(s) in the last 24 h, 1 of unknown billing), codex_desktop_typo"
        " (0 row(s) in the last 24 h, 0 of unknown billing) — their unknown rows are left unknown on purpose"
    ], "a name that matches nothing shows 0: a typo is visible"
