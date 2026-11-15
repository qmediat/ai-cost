"""An app's labels on its usage-log lines: written by ``log --label``, filtered by ``--where``, grouped by ``--by``."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from ..config import GithubSettings, Paths
from ..errors import UsageError
from ..groups import Report
from ..models import (
    BillAccount,
    Billing,
    BillScope,
    Collected,
    Estimate,
    Provider,
    RowKind,
    Size,
    Tokens,
    UsageRow,
    WorkItem,
)
from ..ops import ReportRequest, build_report
from ..plugins import Context, Loaded, Plugin
from ..render import render_markdown
from ..timeutil import iso
from .fixtures import BASE, WINDOW, defaults, paths_in, run_cli, write_claude_session
from .test_project_scope import _rollout


def _line(tenant: str | None, feature: str = "chat", minutes: int = 3) -> str:
    entry: dict[str, object] = {
        "schema": 1,
        "at": iso(BASE + timedelta(minutes=minutes)),
        "provider": "openai",
        "model": "gpt-5.5",
        "tokens": {"input": 1000},
    }
    if tenant is not None:
        entry["labels"] = {"tenant": tenant, "feature": feature}
    return json.dumps(entry)


def _logged(tmp: Path, tenants: Sequence[str | None]) -> Paths:
    paths = paths_in(tmp)
    assert paths.usage_log is not None
    paths.usage_log.parent.mkdir(parents=True, exist_ok=True)
    paths.usage_log.write_text("\n".join(_line(tenant) for tenant in tenants) + "\n", encoding="utf-8")
    return paths


def _report(paths: Paths, where: Mapping[str, str] | None = None, by: str = "", **more: object) -> Report:
    config, book = defaults(paths)  # the test plans: OpenAI rows sit beside a plan share
    request = ReportRequest(
        all_projects="project" not in more,
        since=iso(WINDOW.start),
        until=iso(WINDOW.end),
        where=dict(where or {}),
        by=by,
        **{"groups": ("real", "api"), **more},  # type: ignore[arg-type]
    )
    return build_report(request, paths, config, book)


def test_where_keeps_the_exact_label_and_carries_no_plan_share(tmp_path: Path) -> None:
    paths = _logged(tmp_path, ["a", "b", "b", "A", None])
    whole, one = _report(paths), _report(paths, {"tenant": "a"})
    assert len(whole.rows) == 5 and len(one.rows) == 1
    assert whole.real is not None and whole.real.subscription_usd > 0, "the plan share of the whole period"
    assert one.real is not None and one.real.subscription_usd == 0 and one.real.total_usd == one.real.cash_usd
    assert one.sources[-2:] == [
        "usage-log ×1",
        "labels tenant=a: 1 row(s) — the real group is their cash only, no plan share; a filter selects usage "
        "rows: work items and estimates are left out",
    ], "no source line counts a row outside the filter"


def test_by_lists_one_line_per_value_then_the_rows_without_the_label(tmp_path: Path) -> None:
    group = _report(_logged(tmp_path, ["b", "a", "b", None]), by="tenant").attribution
    assert group is not None
    assert [(line.label, line.calls) for line in group.lines] == [("a", 1), ("b", 2), ("(no tenant)", 1)]
    assert group.heading == "by the label tenant"


def test_a_malformed_label_name_or_by_beside_attribute_is_a_usage_error(tmp_path: Path) -> None:
    paths = _logged(tmp_path, ["a"])
    for kwargs in ({"by": "no spaces"}, {"where": {"a/b": "x"}}, {"by": "tenant", "attribute": ("x=y",)}):
        try:
            _report(paths, **kwargs)
        except UsageError:
            continue
        raise AssertionError(f"{kwargs} accepted")


def _users_files(tmp: Path) -> dict[str, str]:
    """Every file the command reads under ``tmp``: the developer's own config, prices and logs never decide a test."""
    return {
        "AI_COST_USAGE_LOG": str(tmp / "usage.jsonl"),
        "AI_COST_CONFIG_DIR": str(tmp / "cfg"),
        "AI_COST_CONFIG": str(tmp / "cfg" / "config.json"),
        "AI_COST_PRICES": str(tmp / "cfg" / "prices.json"),
        "AI_COST_STATE_DIR": str(tmp / "state"),
        "CLAUDE_CONFIG_DIR": str(tmp / ".claude"),
        "CODEX_HOME": str(tmp / ".codex"),
        "GEMINI_CLI_HOME": str(tmp / ".gemini"),
        "GROK_HOME": str(tmp / ".grok"),
        "AI_COST_OFFLINE": "1",
    }


def test_the_command_writes_labels_and_reports_by_them(tmp_path: Path) -> None:
    log = ["log", "--provider", "openai", "--model", "gpt-5.5", "--input", "1000", "--at", iso(BASE)]
    window = ["--since", iso(WINDOW.start), "--until", iso(WINDOW.end), "--format", "json", "--no-auto-check"]
    with mock.patch.dict(os.environ, _users_files(tmp_path)):
        written = [run_cli([*log, "--label", f"tenant={tenant}"]) for tenant in ("a", "b")]
        twice = run_cli([*log, "--label", "tenant=a", "--label", "tenant=b"])
        code, out, _ = run_cli(["report", *window, "--where", "tenant=a", "--by", "tenant"])
    assert [json.loads(result[1])["labels"] for result in written] == [{"tenant": "a"}, {"tenant": "b"}]
    assert twice[0] == 2 and "tenant given twice" in twice[2], twice
    report = json.loads(out)
    assert code == 0 and report["row_count"] == 1
    assert [line["label"] for line in report["attribution"]["lines"]] == ["a", "(no tenant)"]


def _entry(minutes: int = 3, **keys: object) -> str:
    entry: dict[str, object] = {
        "schema": 1,
        "at": iso(BASE + timedelta(minutes=minutes)),
        "provider": "openai",
        "model": "gpt-5.5",
        "tokens": {"input": 1000},
    }
    entry.update(keys)
    return json.dumps(entry)


def _with_lines(tmp: Path, lines: Sequence[str]) -> Paths:
    paths = paths_in(tmp)
    assert paths.usage_log is not None
    paths.usage_log.parent.mkdir(parents=True, exist_ok=True)
    paths.usage_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths


def test_an_unlabelled_row_still_places_a_labelled_line_of_its_session(tmp_path: Path) -> None:
    paths = _with_lines(tmp_path, [_entry(session="sid-b", labels={"tenant": "acme"})])
    _rollout(paths, "sid-b", str(tmp_path / "B"))  # the Codex session: project B, no labels
    in_a = _report(paths, {"tenant": "acme"}, project=tmp_path / "A")
    in_b = _report(paths, {"tenant": "acme"}, project=tmp_path / "B")
    assert in_a.rows == [], "the line takes its session's project (B): not A's, whatever the filter keeps"
    assert [row.ref for row in in_b.rows] == ["sid-b"] and in_b.rows[0].labels == {"tenant": "acme"}


def test_by_prices_at_list_like_attribute_so_an_unpriced_plan_row_is_skipped(tmp_path: Path) -> None:
    line = _entry(model="gpt-nowhere", billing="subscription", labels={"tenant": "acme"})
    report = _report(_with_lines(tmp_path, [line]), by="tenant", groups=("real",), unpriced="skip")
    assert report.rows == [] and [skip.source for skip in report.skipped] == ["pricing"]


def test_values_spelled_like_the_missing_group_or_its_quoted_form_are_lines_of_their_own(
    tmp_path: Path,
) -> None:
    lines = [
        _entry(labels={"tenant": "(no tenant)"}),
        _entry(labels={"tenant": '"(no tenant)"'}),
        _entry(),
        _entry(),
    ]
    group = _report(_with_lines(tmp_path, lines), by="tenant").attribution
    assert group is not None
    assert [(line.label, line.calls) for line in group.lines] == [
        (json.dumps('"(no tenant)"'), 1),
        ('"(no tenant)"', 1),
        ("(no tenant)", 2),
    ], "three groups, three names: the missing marker, a value, the value quoted"


def test_a_filtered_report_reads_no_github_usage_report_and_says_so(tmp_path: Path) -> None:
    paths = _with_lines(tmp_path, [_entry(labels={"tenant": "a"})])
    config, book = defaults(paths)
    config = replace(config, github=GithubSettings(bill=BillAccount(BillScope.ORGANIZATION, "acme")))
    request = ReportRequest(
        all_projects=True, since=iso(WINDOW.start), until=iso(WINDOW.end), where={"tenant": "a"}
    )
    with mock.patch("ai_cost.ops.collect_bill", side_effect=AssertionError("the usage report was read")):
        report = build_report(request, paths, config, book)
    assert (
        "GitHub usage report: not read — a report filtered by labels holds part of the account"
        in report.sources
    )


def test_a_filtered_report_says_it_carries_no_plan_share_and_heads_its_group_by_the_label(
    tmp_path: Path,
) -> None:
    text = render_markdown(_report(_logged(tmp_path, ["a", "b"]), {"tenant": "a"}, by="tenant"))
    assert "Labels tenant=a: a selection of the period carries no subscription fee" in text
    assert "Subscriptions prorated to the window" not in text
    assert "Real (API keys; no subscription share for a label filter)" in text
    assert "## 4. Attribution (by the label tenant)" in text and "branch / workspace / PR" not in text


def test_a_filtered_report_counts_and_names_only_what_it_kept(tmp_path: Path) -> None:
    lines = [_entry(source="app-a", labels={"tenant": "a"}), _entry(source="app-b", labels={"tenant": "b"})]
    paths = _with_lines(tmp_path, lines)
    write_claude_session(paths, tmp_path / "proj")  # transcripts of the period, none of them labelled
    report = _report(paths, {"tenant": "a"})
    assert report.sources == [
        "usage-log ×1 (app-a)",
        "labels tenant=a: 1 row(s) — the real group is their cash only, no plan share; a filter selects usage "
        "rows: work items and estimates are left out",
    ], report.sources


def test_an_empty_by_and_a_malformed_name_name_their_flag(tmp_path: Path) -> None:
    window = ["--since", iso(WINDOW.start), "--until", iso(WINDOW.end), "--no-auto-check"]
    with mock.patch.dict(os.environ, _users_files(tmp_path)):
        empty = run_cli(["report", *window, "--by", ""])
        malformed = run_cli(["report", *window, "--where", "a/b=x"])
    assert empty[0] == 2 and "--by:" in empty[2], empty
    assert malformed[0] == 2 and "--where:" in malformed[2], malformed
    paths = _logged(tmp_path, ["a"])
    for kwargs, flag in (({"by": "a b"}, "--by:"), ({"where": {"a b": "x"}}, "--where:")):
        try:
            _report(paths, **kwargs)
        except UsageError as exc:
            assert str(exc).startswith(flag), exc
        else:
            raise AssertionError(f"{kwargs} accepted")


def _plugin_row(source: str, labels: object) -> UsageRow:
    """One row as a plugin source hands it; the labels may be any shape: the boundary checks them."""
    tokens = Tokens(input=1000)
    return UsageRow(Provider.OPENAI, "gpt-5.5", RowKind.LOG, BASE, "p-1", Billing.API, tokens, source=source, labels=labels)  # type: ignore[arg-type]


class _Source:
    """A plugin source whose rows carry the labels it is given, and one work item and one estimate."""

    name = "app-plugin"

    def __init__(self, labels: object) -> None:
        self.labels = labels

    def collect(self, ctx: Context) -> Collected:
        row = _plugin_row(self.name, self.labels)
        item = WorkItem("i-1", "a scope", Size.S)
        estimate = Estimate(Provider.OPENAI, "run-1", BASE, Decimal("1.5"), "a run", "no meter")
        return Collected(rows=[row], items=[item], estimates=[estimate])


def _plugged(paths: Paths, labels: object, **more: object) -> Report:
    config, book = defaults(paths)
    request = ReportRequest(all_projects=True, since=iso(WINDOW.start), until=iso(WINDOW.end), **more)  # type: ignore[arg-type]
    loaded = Loaded(plugins=[Plugin(name="app", sources=(_Source(labels),))])
    return build_report(request, paths, config, book, loaded)


def test_a_plugin_row_whose_labels_break_the_rule_fails_its_own_source(tmp_path: Path) -> None:
    report = _plugged(paths_in(tmp_path), ["tenant", "a"], by="tenant")
    assert report.rows == [] and any("labels follow the label rule" in skip.reason for skip in report.skipped)


def test_a_filtered_report_leaves_work_items_and_estimates_out(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    whole = _plugged(paths, {"tenant": "a"}, groups=("real", "api", "vendor"))
    one = _plugged(paths, {"tenant": "a"}, groups=("real", "api", "vendor"), where={"tenant": "a"})
    assert whole.vendor is not None and whole.estimates is not None and whole.estimates.shown
    assert len(one.rows) == 1 and one.vendor is None and (one.estimates is None or not one.estimates.shown)


class _Named:
    """A plugin source named like a usage-log writer: its rows must be counted on its own line, once."""

    name = "app-a"

    def collect(self, ctx: Context) -> Collected:
        return Collected(rows=[_plugin_row(self.name, {"tenant": "a"})])


def test_each_sources_line_counts_the_rows_it_took_once(tmp_path: Path) -> None:
    paths = _with_lines(tmp_path, [_entry(source="app-a", labels={"tenant": "a"})])
    config, book = defaults(paths)
    request = ReportRequest(
        all_projects=True, since=iso(WINDOW.start), until=iso(WINDOW.end), where={"tenant": "a"}
    )
    loaded = Loaded(plugins=[Plugin(name="named", sources=(_Named(),))])
    report = build_report(request, paths, config, book, loaded)
    counted = report.sources[:-1]
    assert counted == ["usage-log ×1 (app-a)", "app-a ×1"], counted
    assert sum(int(line.split("×")[1].split()[0]) for line in counted) == len(report.rows) == 2


class _Tenant:
    """An enricher that labels every Claude row: a filtered report then keeps transcript rows."""

    name = "tenant"

    def enrich(self, rows: list[UsageRow], ctx: Context) -> list[UsageRow]:
        return [
            replace(row, labels={"tenant": "a"}) if row.kind is RowKind.TRANSCRIPT else row for row in rows
        ]


def test_the_transcripts_line_of_a_filtered_report_counts_sessions_not_rows(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")  # one session: its own messages and a subagent's
    config, book = defaults(paths)
    request = ReportRequest(
        all_projects=True, since=iso(WINDOW.start), until=iso(WINDOW.end), where={"tenant": "a"}
    )
    loaded = Loaded(plugins=[Plugin(name="tenant", enrichers=(_Tenant(),))])
    report = build_report(request, paths, config, book, loaded)
    assert (
        len(report.rows) > 1 and report.sources[0] == "claude transcripts: 1 session(s) kept"
    ), report.sources


def test_where_refuses_github_and_items_in_one_line(tmp_path: Path) -> None:
    paths = _logged(tmp_path, ["a"])
    with mock.patch("ai_cost.ops.collect_github", side_effect=AssertionError("GitHub was asked")):
        for more, flag in (
            ({"github": ("acme/app",)}, "--github"),
            ({"items": tmp_path / "scope.md"}, "--items"),
        ):
            try:
                _report(paths, {"tenant": "a"}, **more)
            except UsageError as exc:
                assert str(exc).startswith(f"--where with {flag}:"), exc
            else:
                raise AssertionError(f"{flag} accepted beside --where")


def test_a_label_value_over_the_writers_limit_is_refused_by_every_flag(tmp_path: Path) -> None:
    long = "x" * 257
    log = ["log", "--provider", "openai", "--model", "gpt-5.5", "--input", "1", "--label", f"tenant={long}"]
    with mock.patch.dict(os.environ, _users_files(tmp_path)):
        written = run_cli(log)
        filtered = run_cli(["report", "--where", f"tenant={long}", "--no-auto-check"])
    assert written[0] == 2 and written[2].startswith("ai-cost: --label:"), written
    assert filtered[0] == 2 and filtered[2].startswith("ai-cost: --where:"), filtered


def test_a_label_value_with_a_control_character_is_foreign_to_the_reader_and_refused_by_the_writer(
    tmp_path: Path,
) -> None:
    paths = _with_lines(tmp_path, [_entry(labels={"tenant": "a\nb"})])
    report = _report(paths)
    assert len(report.rows) == 1 and report.rows[0].labels == {}, "the usage counts, the labels do not"
    assert any("carry a `labels` key the reader does not use" in warning for warning in report.warnings)
    log = ["log", "--provider", "openai", "--model", "gpt-5.5", "--input", "1"]
    with mock.patch.dict(os.environ, _users_files(tmp_path)):
        written = run_cli([*log, "--label", "tenant=a\tb"])
        filtered = run_cli(["report", "--where", "tenant=a\rb", "--no-auto-check"])
    assert written[0] == 2 and "--label:" in written[2] and "control character" in written[2], written
    assert filtered[0] == 2 and "--where:" in filtered[2], filtered


def test_a_filtered_reports_counts_are_its_final_rows(tmp_path: Path) -> None:
    lines = [_entry(labels={"tenant": "a"}), _entry(model="gpt-nowhere", labels={"tenant": "a"})]
    report = _report(_with_lines(tmp_path, lines), {"tenant": "a"}, unpriced="skip")
    assert report.row_count == 1 and [skip.source for skip in report.skipped] == ["pricing"]
    assert report.sources[-2:] == [
        "usage-log ×1",
        "labels tenant=a: 1 row(s) — the real group is their cash only, no plan share; a filter selects usage "
        "rows: work items and estimates are left out",
    ], report.sources


class _Rebuilder:
    """An enricher that returns every row built afresh (a new ``UsageRow``, not ``replace``)."""

    name = "rebuilder"

    def enrich(self, rows: list[UsageRow], ctx: Context) -> list[UsageRow]:
        return [
            UsageRow(
                row.provider,
                row.model,
                row.kind,
                row.at,
                row.ref,
                row.billing,
                row.tokens,
                source=row.source,
                labels=dict(row.labels),
            )
            for row in rows
        ]


def test_rows_an_enricher_built_afresh_are_still_counted_by_their_line(tmp_path: Path) -> None:
    paths = _with_lines(tmp_path, [_entry(labels={"tenant": "a"})])
    config, book = defaults(paths)
    request = ReportRequest(
        all_projects=True, since=iso(WINDOW.start), until=iso(WINDOW.end), where={"tenant": "a"}
    )
    loaded = Loaded(plugins=[Plugin(name="rebuild", enrichers=(_Rebuilder(),))])
    report = build_report(request, paths, config, book, loaded)
    assert report.row_count == 1 and report.sources[0] == "usage-log ×1", report.sources
