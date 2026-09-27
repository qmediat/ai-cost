"""Provider day reports against the local rows (ADR-0008): the untracked row, its kind in every group, what is not compared."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..attribution import attribution_group, parse_rules
from ..config import Config, PriceBook
from ..groups import UNTRACKED_LABEL, api_group, real_group
from ..models import (
    Billing,
    Provider,
    ProviderDay,
    ProviderDays,
    ProviderLine,
    RowKind,
    Scope,
    Span,
    Tokens,
    Untracked,
    UsageRow,
    Window,
)
from ..ops import ReportRequest, build_report
from ..providers import ReportSource
from ..providers.deepseek_export import import_export
from ..reconcile import Reported, reconcile
from ..remainder import compare, difference
from ..render import render_json, render_markdown
from .fixtures import defaults, paths_in
from .test_deepseek_export import _export

DAY = datetime(2026, 9, 21, tzinfo=timezone.utc)
WINDOW = Window(DAY, DAY + timedelta(days=1))


def _day(
    gross: str, net: str | None = None, *, closed: bool = True, start: datetime = DAY, **kw: Any
) -> ProviderDay:
    line = ProviderLine(
        "deepseek-flash", Decimal(gross), Decimal(net if net is not None else gross), requests=3
    )
    return ProviderDay(
        Provider.DEEPSEEK, start, start + timedelta(days=1), (line,), closed, DAY, "test", **kw
    )


def _row(usd: float, minutes: int = 60, billing: Billing = Billing.API, **kw: Any) -> UsageRow:
    """A cost-only DeepSeek row: its own figure is its price in both groups."""
    fields: dict[str, Any] = {
        "provider": Provider.DEEPSEEK,
        "model": "deepseek-flash",
        "kind": RowKind.LOG,
        "at": DAY + timedelta(minutes=minutes),
        "ref": f"log:{minutes}",
        "billing": billing,
        "tokens": Tokens(),
        "cost_reported": usd,
    }
    return UsageRow(**(fields | kw))


def _compare(tmp: Path, days: list[ProviderDay], rows: list[UsageRow]) -> Any:
    config, book = defaults(paths_in(tmp))
    report = ProviderDays(Provider.DEEPSEEK, "test", tuple(days))
    return compare([report], rows, WINDOW, book, config), config, book


def _groups(rows: list[UsageRow], config: Config, book: PriceBook) -> tuple[float, float]:
    return api_group(rows, book, config).total_usd, real_group(rows, book, config, WINDOW).cash_usd


def test_a_positive_difference_is_one_untracked_row_booked_gross_in_api_and_net_in_real(
    tmp_path: Path,
) -> None:
    rows = [_row(1.25), _row(0.5, minutes=90)]
    done, config, book = _compare(tmp_path, [_day("3.00", "2.50")], rows)
    (untracked,) = done.rows
    assert (untracked.kind, untracked.model, untracked.at, untracked.scope) == (
        RowKind.UNTRACKED,
        "untracked",
        DAY,
        Scope(),
    )
    assert (untracked.untracked.api, untracked.untracked.real) == (1.25, 0.75)
    api, cash = _groups([*rows, untracked], config, book)
    assert (api, cash) == (3.0, 2.5), "each group equals the provider's figure: gross in API, net in real"
    line = next(line for line in real_group([untracked], book, config, WINDOW).usage)
    assert (line.label, line.calls) == (UNTRACKED_LABEL, 0), "an amount, not a run"
    assert line.notes == {
        "provider report − this machine's records"
    }, "it says whose records it is set against"


def test_a_row_of_unknown_billing_is_never_counted_twice(tmp_path: Path) -> None:
    rows = [_row(2.0, billing=Billing.UNKNOWN)]
    done, config, book = _compare(tmp_path, [_day("2.00")], rows)
    (untracked,) = done.rows
    assert (untracked.untracked.api, untracked.untracked.real) == (0.0, 2.0)
    assert _groups([*rows, untracked], config, book) == (2.0, 2.0)
    assert done.summaries[0].days[0].unknown_rows == 1


def test_a_negative_difference_is_a_warning_and_no_row(tmp_path: Path) -> None:
    done, _, _ = _compare(tmp_path, [_day("1.00")], [_row(1.2)])
    assert done.rows == ()
    (warning,) = done.warnings
    assert "exceed the report on 2026-09-21T00:00+00:00/2026-09-22T00:00+00:00 by 0.20" in warning
    assert "USD (real) and 0.20" in warning and "a call counted twice" in warning


def test_a_row_a_plan_paid_is_not_subtracted(tmp_path: Path) -> None:
    done, _, _ = _compare(tmp_path, [_day("1.00")], [_row(0.4, billing=Billing.SUBSCRIPTION)])
    assert (done.rows[0].untracked.api, done.rows[0].untracked.real) == (1.0, 1.0)


def test_a_day_whose_cash_sits_on_other_rows_is_not_compared(tmp_path: Path) -> None:
    for row in (_row(1.0, kind=RowKind.LEDGER), _row(1.0, billing=Billing.API_SETTLED)):
        done, _, _ = _compare(tmp_path, [_day("5.00")], [row])
        assert done.rows == () and "sits on other rows" in done.summaries[0].days[0].why_not


def test_a_day_with_an_unpriced_row_is_not_compared(tmp_path: Path) -> None:
    unpriced = _row(0.0, cost_reported=None, model="no-such-model", tokens=Tokens(cache_miss=1000))
    done, _, _ = _compare(tmp_path, [_day("5.00")], [unpriced])
    assert done.rows == () and done.summaries[0].days[0].why_not.startswith("a local row has no price")


def test_days_the_window_cuts_or_that_are_still_open_are_listed_not_apportioned(tmp_path: Path) -> None:
    cut = _day("4.00", start=DAY - timedelta(hours=12))
    open_ = _day("6.00", closed=False)
    done, _, _ = _compare(tmp_path, [cut, open_], [_row(1.0)])
    assert done.rows == ()
    assert [c.why_not for c in done.summaries[0].days] == [
        "the window holds only part of the day",
        "not closed yet: the provider may still add to it",
    ]
    assert "2 day(s) not compared" in done.warnings[0]


def test_a_day_in_another_currency_without_the_providers_rate_is_not_compared(tmp_path: Path) -> None:
    done, _, _ = _compare(tmp_path, [_day("4.00", currency="PLN")], [])
    assert done.summaries[0].days[0].why_not == "in PLN, without the provider's own rate"


def test_a_difference_within_the_float_error_of_the_local_sum_is_zero() -> None:
    amounts = [0.1] * 10  # 0.9999999999999999 in floats
    assert difference(Decimal("1.0000000000000000"), amounts) == 0
    assert difference(Decimal("1.0000000100000000"), amounts) == Decimal("1.00E-8")


def test_missing_spans_are_warned_never_zero(tmp_path: Path) -> None:
    config, book = defaults(paths_in(tmp_path))
    report = ProviderDays(
        Provider.DEEPSEEK, "test", missing=(Span(DAY, DAY + timedelta(days=1), "not in any imported export"),)
    )
    done = compare([report], [_row(1.0)], WINDOW, book, config)
    assert done.rows == () and done.warnings == (
        "provider report (deepseek): no data for 2026-09-21T00:00+00:00..2026-09-22T00:00+00:00 (not in any imported export)",
    )


def test_the_untracked_row_weighs_nothing_in_the_subscription_split_and_stays_unattributed(
    tmp_path: Path,
) -> None:
    config, book = defaults(paths_in(tmp_path))  # the test plans: claude-max-20x covers anthropic
    plan_row = _row(1.0, provider=Provider.ANTHROPIC, model="claude-fable-5-1", scope=Scope(branch="feature"))
    untracked = replace(
        plan_row,
        kind=RowKind.UNTRACKED,
        model="untracked",
        scope=Scope(),
        cost_reported=None,
        untracked=Untracked("day", api=3.0, real=3.0),
    )
    rules = parse_rules(["feature=^feature$"])
    split = {
        line.label: line.subscription_usd
        for line in attribution_group([plan_row, untracked], rules, book, config, WINDOW).lines
    }
    alone = {
        line.label: line.subscription_usd
        for line in attribution_group([plan_row], rules, book, config, WINDOW).lines
    }
    plan = next(
        s for s in real_group([plan_row], book, config, WINDOW).subscriptions if s.plan == "claude-max-20x"
    )
    assert (
        split == alone and split["feature"] == plan.usd > 0
    ), "the plan's share stays with the work that used it"


def test_the_provider_outside_line_names_the_days_currency(tmp_path: Path) -> None:
    from ..ops import provider_outside

    done, _, _ = _compare(tmp_path, [_day("62.40", currency="CNY")], [])
    (line,) = provider_outside(done.summaries)
    assert "gross 62.40 CNY, net 62.40 CNY" in line and "USD" not in line


def _report(tmp: Path, request: ReportRequest) -> Any:
    paths = paths_in(tmp)
    config, book = defaults(paths)
    config = replace(config, provider_reports={"deepseek": ReportSource("deepseek", "import")})
    import_export(_export(tmp), paths.state_dir)
    assert paths.usage_log is not None
    paths.usage_log.parent.mkdir(parents=True)
    line = {"schema": 1, "at": "2026-09-21T10:00:00Z", "provider": "deepseek", "model": "deepseek-flash"}
    paths.usage_log.write_text(json.dumps(line | {"billing": "api", "cost": 8.0}) + "\n")
    return build_report(request, paths, config, book), paths, config, book


def test_a_report_of_the_account_books_the_providers_difference(tmp_path: Path) -> None:
    request = ReportRequest(
        all_projects=True, since="2026-09-20T22:00:00Z", until="2026-09-21T22:00:00Z", unpriced="skip"
    )
    report, *_ = _report(tmp_path, request)
    (summary,) = report.provider_reports
    (day,) = summary.compared
    assert (day.day.gross, day.local_real, day.real_diff) == (
        Decimal("8.7635721780000000"),
        Decimal("8.00000000000000"),
        Decimal("0.76357217800000"),
    )
    untracked = [r for r in report.rows if r.kind is RowKind.UNTRACKED]
    assert len(untracked) == 1, "--unpriced skip keeps a row whose amount is its own"
    assert "## Provider report: deepseek (deepseek-export)" in render_markdown(report)
    assert (
        json.loads(render_json(report, detail=False))["provider_reports"][0]["days"][0]["real_diff"]
        == "0.76357217800000"
    )


def test_reconcile_still_shows_the_gap_the_difference_closes(tmp_path: Path) -> None:
    request = ReportRequest(all_projects=True, since="2026-09-20T22:00:00Z", until="2026-09-21T22:00:00Z")
    _, paths, config, book = _report(tmp_path, request)
    result = reconcile(request, paths, config, book, Reported("deepseek", 8.763572178))
    assert result.local_usd == 8.0 and result.rows == 1, "the untracked row is not local"


def test_a_project_report_compares_no_provider_day(tmp_path: Path) -> None:
    request = ReportRequest(
        project=tmp_path / "somewhere", since="2026-09-20T22:00:00Z", until="2026-09-21T22:00:00Z"
    )
    report, *_ = _report(tmp_path, request)
    assert not any(r.kind is RowKind.UNTRACKED for r in report.rows)
    assert report.provider_reports == ()
    assert "provider reports: not read — a per-project report holds part of the account" in report.sources


def test_a_report_of_one_session_compares_no_provider_day(tmp_path: Path) -> None:
    request = ReportRequest(
        session="some-session", since="2026-09-20T22:00:00Z", until="2026-09-21T22:00:00Z"
    )
    report, *_ = _report(tmp_path, request)
    assert not any(r.kind is RowKind.UNTRACKED for r in report.rows)
    assert report.provider_reports == ()
    assert "provider reports: not read — a report of one session holds part of the account" in report.sources


def test_the_report_source_is_named_in_the_config_or_refused() -> None:
    from ..config import builtin_config, parse_config
    from ..errors import ConfigError

    raw = builtin_config() | {"providers": {"deepseek": {"report": {"source": "import"}}}}
    assert parse_config(raw, "test").provider_reports == {"deepseek": ReportSource("deepseek", "import")}
    for providers in ({"deepseek": {"report": {"source": "api"}}}, {"xai": {"report": {"source": "import"}}}):
        try:
            parse_config(builtin_config() | {"providers": providers}, "test")
        except ConfigError as exc:
            assert "is not read (known:" in str(exc)
        else:
            raise AssertionError(f"{providers} must be refused")


def test_doctor_says_each_sources_state(tmp_path: Path) -> None:
    from ..onboarding import Mark, ProviderUse, provider_report_checks

    config, _ = defaults(paths_in(tmp_path))
    at = DAY + timedelta(days=3)
    fresh = ProviderDays(Provider.DEEPSEEK, "test", (_day("1.00"),), captured=at - timedelta(days=1))
    stale = replace(fresh, captured=at - timedelta(days=9))
    broken = ProviderDays(Provider.DEEPSEEK, "test", unreadable=(Span(DAY, at, "store cannot be read"),))
    empty = ProviderDays(Provider.DEEPSEEK, "test")
    marks = [provider_report_checks(config, [r], [], at)[0].mark for r in (fresh, stale, broken, empty)]
    assert marks == [Mark.OK, Mark.INFO, Mark.PROBLEM, Mark.PROBLEM]
    hint = provider_report_checks(config, [], [ProviderUse("deepseek", per_token=3)], at)
    assert [c.mark for c in hint] == [Mark.INFO] and "ai-cost import deepseek" in hint[0].text


def test_the_import_command_says_what_changed(tmp_path: Path) -> None:
    import contextlib
    import io
    import os
    from unittest import mock

    from ..cli import main

    out = io.StringIO()
    with (
        mock.patch.dict(os.environ, {"AI_COST_STATE_DIR": str(tmp_path / "state")}),
        contextlib.redirect_stdout(out),
    ):
        code = main(["import", "deepseek", str(_export(tmp_path))])
    assert code == 0 and "imported deepseek: 3 day(s) added, 0 replaced, 0 kept" in out.getvalue()
    assert "these days are not UTC days" in out.getvalue()


def test_the_section_lists_the_providers_request_count_beside_the_local_one(tmp_path: Path) -> None:
    request = ReportRequest(
        all_projects=True, since="2026-09-20T22:00:00Z", until="2026-09-21T22:00:00Z", unpriced="skip"
    )
    report, *_ = _report(tmp_path, request)
    text = render_markdown(report)
    assert "provider 142 request(s), local 0, 1 local row(s) without a request count" in text


def test_a_zero_provider_day_still_shows_a_small_local_excess(tmp_path: Path) -> None:
    done, _, _ = _compare(tmp_path, [_day("0")], [_row(0.001)])
    (compared,) = done.summaries[0].compared
    assert compared.real_diff is not None and compared.real_diff < 0 and done.rows == ()
    assert "by 0.001" in done.warnings[0], "a zero states no precision: never rounded to cents"


def test_a_daily_day_is_kept_when_a_provider_it_compared_cannot_be_read_now(tmp_path: Path) -> None:
    from ..daily import Output, daily_reports
    from ..providers.deepseek_export import store_path

    paths = paths_in(tmp_path)
    config, book = defaults(paths)
    config = replace(config, provider_reports={"deepseek": ReportSource("deepseek", "import")})
    import_export(_export(tmp_path, lambda kind, text: text.replace("+02:00", "+00:00")), paths.state_dir)
    lines: list[str] = []
    day = DAY.date()  # 09-21: a UTC day of the export, closed before its capture
    daily_reports(paths, config, book, day, paths.reports_dir, Output(lines.append, lines.append))
    written = (paths.reports_dir / day.isoformat() / "global.json").read_text()
    assert json.loads(written)["provider_reports"][0]["days"][0]["why_not"] == ""
    store_path(paths.state_dir).write_text("{broken")
    daily_reports(paths, config, book, day, paths.reports_dir, Output(lines.append, lines.append))
    assert (
        f"daily {day}: kept as written — the provider report of deepseek compared this day before and cannot now"
        in lines
    )
    assert (paths.reports_dir / day.isoformat() / "global.json").read_text() == written
