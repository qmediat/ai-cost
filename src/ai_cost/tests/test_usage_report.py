"""The GitHub usage report (ADR-0007): whole UTC days, exact sums, what is left out and what could not be read."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any
from unittest import mock

from .. import ops as ops_module
from ..collectors import github_bill
from ..collectors.github import GhCall
from ..collectors.github_bill import (
    BillRequest,
    BillResult,
    bill_source,
    bill_warnings,
    collect_bill,
    parse_line,
    probe_bill,
)
from ..config import GithubSettings
from ..groups import Report, api_group, real_group
from ..models import BillAccount, BillScope, BillSubtotal, RowKind, Window
from ..ops import ReportRequest, build_report
from ..render import render_json, render_markdown
from .fixtures import defaults, paths_in


def _plain_config() -> Any:
    from ..config import builtin_config, parse_config

    return parse_config(builtin_config(), "test")


ACME = BillAccount(BillScope.ORGANIZATION, "acme")
READ_AT = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
TWO_DAYS = Window(datetime(2026, 9, 23, tzinfo=timezone.utc), datetime(2026, 9, 25, tzinfo=timezone.utc))


def _month() -> str:
    return resources.files(__package__).joinpath("data/github-usage-month.json").read_text(encoding="utf-8")


@contextmanager
def _report(*answers: GhCall) -> Iterator[mock.MagicMock]:
    """``gh api`` answers in turn (the fixture month by default); the per-process memo starts empty."""
    replies = list(answers) or [GhCall(_month(), "")]
    with (
        mock.patch.dict(github_bill._FETCHED, clear=True),
        mock.patch.object(github_bill, "run_gh", side_effect=replies) as call,
    ):
        yield call


def _request(**changes: Any) -> BillRequest:
    return replace(BillRequest(ACME, READ_AT), **changes)


def _said(bill: BillResult) -> list[str]:
    assert bill.summary is not None
    return bill_warnings(bill.summary)


def _sku(sums: tuple[BillSubtotal, ...], sku: str) -> BillSubtotal:
    return next(s for s in sums if s.sku == sku)


def test_the_whole_days_become_rows_and_the_summary_sums_them_exactly() -> None:
    with _report() as call:
        bill = collect_bill(_request(), TWO_DAYS)
    assert call.call_args.args[0] == ["api", "organizations/acme/settings/billing/usage?year=2026&month=9"]
    assert bill.summary is not None and [d.isoformat() for d in bill.summary.days] == [
        "2026-09-23",
        "2026-09-24",
    ]
    credits = _sku(bill.summary.counted, "Copilot AI Credits")
    assert (credits.lines, credits.quantity, credits.gross, credits.discount, credits.net) == (
        3,
        Decimal("1847.520162"),
        Decimal("18.47520162"),
        Decimal("16.50448134"),
        Decimal("1.97072028"),
    )
    seats = _sku(bill.summary.counted, "Copilot Business")
    assert (seats.quantity, seats.net) == (Decimal("0.133333332"), Decimal("2.533333308"))
    assert {row.kind for row in bill.rows} == {RowKind.INVOICE} and len(bill.rows) == 5
    assert _said(bill) == [] and bill.skipped == [], "nothing partial, nothing unreadable"


def test_the_groups_price_the_rows_at_gross_and_net_and_the_seats_as_shares(tmp_path: Path) -> None:
    config, book = defaults(paths_in(tmp_path))
    config = replace(config, subscriptions=(), github=GithubSettings(bill=ACME))
    with _report():
        rows = collect_bill(_request(), TWO_DAYS).rows
    api = {line.label: line.usd for line in api_group(rows, book, config).lines}
    assert abs(api["Copilot AI Credits"] - 18.47520162) < 1e-12 and "Copilot Business" not in api
    real = real_group(rows, book, config, TWO_DAYS)
    assert abs(real.cash_usd - 1.97072028) < 1e-12, "what was billed beyond the included credits"
    assert [(s.plan, s.usd) for s in real.subscriptions] == [("Copilot Business", 2.533333308)]


def test_actions_count_only_for_a_repository_named_with_github_and_other_products_are_left_out() -> None:
    with _report():
        plain = collect_bill(_request(), TWO_DAYS).summary
    with _report():
        named = collect_bill(_request(actions_repos=frozenset({"acme/gadgets"})), TWO_DAYS).summary
    assert plain is not None and named is not None
    assert {s.sku for s in plain.left_out} == {"Actions Linux", "Actions storage", "Enterprise Cloud"}
    minutes = _sku(named.counted, "Actions Linux")
    assert (minutes.lines, minutes.quantity, minutes.gross, minutes.net) == (
        1,
        Decimal("121.0"),
        Decimal("0.726"),
        Decimal("0.0"),
    ), "the gadgets minutes only: widgets was not named"
    assert _sku(named.left_out, "Actions Linux").quantity == Decimal("38.0")


def test_days_the_window_only_touches_are_outside_the_totals_with_their_amounts() -> None:
    window = Window(
        datetime(2026, 9, 24, 12, tzinfo=timezone.utc), datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
    )
    read_at = datetime(2026, 9, 25, 10, 30, tzinfo=timezone.utc)
    with _report():
        bill = collect_bill(_request(read_at=read_at), window)
    assert bill.rows == [] and bill.summary is not None and bill.summary.days == ()
    outside = {d.day.isoformat(): d for d in bill.summary.outside}
    assert outside["2026-09-24"].why == "the window holds 12.00 h of it"
    assert outside["2026-09-24"].net == Decimal("3.237386934"), "credits 1.97072028 + the seat 1.266666654"
    assert outside["2026-09-25"].why == "the day is not over" and outside["2026-09-25"].gross == Decimal(
        "2.45861137"
    )
    assert any("2 day(s) the window only touches are not in the totals" in w for w in _said(bill))


def test_a_day_read_soon_after_it_ended_is_in_the_totals_and_said_provisional() -> None:
    with _report():
        bill = collect_bill(_request(read_at=datetime(2026, 9, 25, 6, tzinfo=timezone.utc)), TWO_DAYS)
    assert bill.summary is not None and [d.isoformat() for d in bill.summary.provisional] == ["2026-09-24"]
    assert any("2026-09-24 ended less than 12 h before this reading" in w for w in _said(bill))


def test_a_line_without_an_amount_is_skipped_and_counted() -> None:
    body = json.loads(_month())
    del body["usageItems"][4]["netAmount"]  # the widgets credits of 2026-09-23
    with _report(GhCall(json.dumps(body), "")):
        bill = collect_bill(_request(), TWO_DAYS)
    assert bill.summary is not None and bill.summary.unreadable_lines == 1
    assert [s.reason for s in bill.skipped] == ["netAmount is missing"]
    assert any("1 line(s) could not be read — the GitHub amounts are short by them" in w for w in _said(bill))


def test_a_month_that_cannot_be_read_is_named_and_nothing_is_estimated() -> None:
    with _report(GhCall("", "gh: Not Found (HTTP 404)")):
        bill = collect_bill(_request(), TWO_DAYS)
    assert bill.rows == [] and bill.summary is not None and bill.summary.counted == ()
    assert bill.summary.missing[0].startswith("2026-09: HTTP 404: this gh login cannot read the usage report")
    assert any("2026-09: HTTP 404" in w and "nothing estimated" in w for w in _said(bill))


def test_offline_reads_nothing_and_says_so() -> None:
    with _report() as call:
        bill = collect_bill(_request(offline=True), TWO_DAYS)
    assert call.call_count == 0 and bill.rows == []
    assert bill.summary is not None and "offline (AI_COST_OFFLINE)" in bill.summary.missing[0]


def test_a_month_is_fetched_once_per_process_and_a_window_across_months_reads_each() -> None:
    across = Window(datetime(2026, 8, 31, tzinfo=timezone.utc), datetime(2026, 9, 25, tzinfo=timezone.utc))
    empty = GhCall(json.dumps({"usageItems": []}), "")
    with _report(empty, GhCall(_month(), "")) as call:
        first = collect_bill(_request(), across)
        again = collect_bill(_request(), across)
    assert call.call_count == 2, "August and September, once each"
    assert first.summary is not None and first.summary == again.summary and len(first.summary.days) == 25


def test_a_qualified_repository_name_is_kept_and_a_bare_one_goes_under_its_organization() -> None:
    line = parse_line(json.loads(_month(), parse_float=Decimal)["usageItems"][4])
    assert github_bill.repository_of(line, ACME) == "acme/widgets"
    assert github_bill.repository_of(replace(line, repository="other/tool"), ACME) == "other/tool"
    assert github_bill.repository_of(replace(line, repository=""), ACME) == ""


def test_a_malformed_amount_is_refused_by_name() -> None:
    item = json.loads(_month(), parse_float=Decimal)["usageItems"][4]
    for key, value in (("grossAmount", True), ("netAmount", "1.0"), ("date", "yesterday")):
        try:
            parse_line({**item, key: value})
        except ValueError as exc:
            assert key.replace("Amount", "") in str(exc) or "date" in str(exc), str(exc)
        else:
            raise AssertionError(f"{key}={value!r} must be refused")


def test_doctor_probe_names_the_newest_day_or_the_failure() -> None:
    with _report():
        assert probe_bill(_request()).newest == datetime(2026, 9, 25).date()
    with _report(GhCall("", "gh: Not Found (HTTP 404)")):
        assert "HTTP 404" in probe_bill(_request()).failure


def test_the_report_shows_the_exact_figures_in_markdown_and_json(tmp_path: Path) -> None:
    paths = replace(
        paths_in(tmp_path), offline=False
    )  # the report is read through the stubbed gh, not the network
    config, book = defaults(paths)
    config = replace(config, subscriptions=(), github=GithubSettings(bill=ACME))
    request = ReportRequest(all_projects=True, since="2026-09-23T00:00:00Z", until="2026-09-25T00:00:00Z")
    with _report(), mock.patch.object(ops_module, "now", return_value=READ_AT):
        report = build_report(replace(request, groups=("real", "api")), paths, config, book)
    markdown = render_markdown(report)
    assert (
        "## GitHub usage report (organization acme)" in markdown and "Read 2026-09-26T12:00:00Z." in markdown
    )
    assert (
        "| copilot | Copilot AI Credits | AICredits | 3 | 1847.520162 | 18.47520162 | 16.50448134 | 1.97072028 |"
        in markdown
    )
    data = json.loads(render_json(report, detail=False))
    credits = next(s for s in data["github_bill"]["counted"] if s["sku"] == "Copilot AI Credits")
    assert (credits["gross"], credits["net"]) == ("18.47520162", "1.97072028"), "exact, as text"


def test_monitor_names_the_github_amounts_its_totals_leave_out() -> None:
    window = Window(
        datetime(2026, 9, 24, 12, tzinfo=timezone.utc), datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
    )
    with _report():
        bill = collect_bill(_request(read_at=datetime(2026, 9, 25, 10, 30, tzinfo=timezone.utc)), window)
    assert ops_module.bill_outside(bill.summary) == (
        "github 2026-09-24 (the window holds 12.00 h of it): net 3.237386934 USD, gross 3.237386934",
        "github 2026-09-25 (the day is not over): net 2.45861137 USD, gross 2.45861137",
    )


def test_a_project_report_leaves_the_account_figures_to_the_global_one(tmp_path: Path) -> None:
    paths = replace(paths_in(tmp_path), offline=False)
    config, book = defaults(paths)
    config = replace(config, subscriptions=(), github=GithubSettings(bill=ACME))
    request = ReportRequest(project=tmp_path, since="2026-09-23T00:00:00Z", until="2026-09-25T00:00:00Z")
    with _report(), mock.patch.object(ops_module, "now", return_value=READ_AT):
        report = build_report(replace(request, groups=("real", "api")), paths, config, book)
    assert report.github_bill is None, "its lines name repositories, not this directory"
    assert {row.model for row in report.rows} == {
        "Copilot Business"
    }, "the seats stay, like a configured plan"
    assert any("github-bill ×3" in w for w in report.warnings), report.warnings


def test_a_malformed_line_of_another_day_is_not_the_windows_loss() -> None:
    body = json.loads(_month())
    del body["usageItems"][-1]["netAmount"]  # 2026-09-25: outside the two days
    with _report(GhCall(json.dumps(body), "")):
        bill = collect_bill(_request(), TWO_DAYS)
    assert bill.summary is not None and bill.summary.unreadable_lines == 0 and bill.skipped == []


def test_offline_is_information_for_doctor_not_a_problem() -> None:
    from ..onboarding import Mark, github_checks

    config = replace(_plain_config(), github=GithubSettings(bill=ACME))
    probe = probe_bill(_request(offline=True))
    assert probe.offline and [c.mark for c in github_checks(config, [], probe)] == [Mark.INFO]


def test_the_boundaries_of_a_whole_and_a_provisional_day() -> None:
    day = datetime(2026, 9, 24).date()
    ended = datetime(2026, 9, 25, tzinfo=timezone.utc)
    assert github_bill.is_provisional(day, ended + timedelta(hours=11, minutes=59))
    assert not github_bill.is_provisional(day, ended + timedelta(hours=12)), "12 h after the end it is final"
    whole = Window(datetime(2026, 9, 24, tzinfo=timezone.utc), ended)
    assert github_bill.is_whole(day, whole, today=day + timedelta(days=1))
    assert not github_bill.is_whole(day, whole, today=day), "the day that is not over is never whole"
    late = Window(datetime(2026, 9, 24, tzinfo=timezone.utc), datetime(2026, 9, 28, tzinfo=timezone.utc))
    assert github_bill.touched_days(late, today=day)[-1] == day, "no day after today is read"


def test_the_days_of_a_month_not_read_are_not_whole_days_of_the_totals() -> None:
    with _report(GhCall("", "gh: Not Found (HTTP 404)")):
        bill = collect_bill(_request(), TWO_DAYS)
    assert bill.summary is not None and bill.summary.days == ()
    assert bill_source(bill.summary) == "github usage report (organization acme): whole UTC days none (0)"


def test_a_project_report_keeps_the_seats_and_points_at_no_section(tmp_path: Path) -> None:
    paths = replace(paths_in(tmp_path), offline=False)
    config, book = defaults(paths)
    config = replace(config, subscriptions=(), github=GithubSettings(bill=ACME))
    window = ReportRequest(project=tmp_path, since="2026-09-24T12:00:00Z", until="2026-09-26T00:00:00Z")
    with _report(), mock.patch.object(ops_module, "now", return_value=READ_AT):
        report = build_report(replace(window, groups=("real", "api")), paths, config, book)
    assert report.github_bill is None
    assert not any("GitHub usage report section" in w for w in report.warnings), report.warnings
    assert any(
        s.endswith("its seats only — the account's figures are in an --all-projects report")
        for s in report.sources
    )


def test_copilot_counts_without_a_usage_report_are_said_not_priced(tmp_path: Path) -> None:
    from ..models import Billing, Provider, Tokens, UsageRow

    config, _ = defaults(paths_in(tmp_path))
    review = UsageRow(
        Provider.GITHUB,
        "copilot-code-review",
        RowKind.COPILOT,
        READ_AT,
        "o/r",
        Billing.SUBSCRIPTION,
        Tokens(reviews=3),
    )
    said = ops_module._copilot_counted([review], replace(config, github=GithubSettings()))
    assert said and said[0].startswith("3 Copilot review(s) counted, not priced")
    assert ops_module._copilot_counted([review], replace(config, github=GithubSettings(bill=ACME))) == []


def test_a_configured_plan_beside_the_billed_seats_is_said(tmp_path: Path) -> None:
    from ..config import Subscription

    config, book = defaults(paths_in(tmp_path))
    config = replace(config, subscriptions=(Subscription("copilot-pro"),), github=GithubSettings(bill=ACME))
    with _report():
        rows = collect_bill(_request(), TWO_DAYS).rows
    real = real_group(rows, book, config, TWO_DAYS)
    said = ops_module._plans_beside_seats(real)
    assert said == [
        "subscription copilot-pro stays booked beside the seats the GitHub usage report bills (Copilot Business) — "
        "remove it from subscriptions if those seats replaced it"
    ]


def test_a_copilot_price_left_in_the_users_prices_file_is_said(tmp_path: Path) -> None:
    from ..config import builtin_prices, parse_pricebook

    config, _ = defaults(paths_in(tmp_path))
    old = {"providers": {"github": {"copilot": {"units_per_code_review": 13}}}}
    book = parse_pricebook(builtin_prices(), old, "prices")
    said = ops_module._retired(paths_in(tmp_path), config, book)
    assert (
        len(said) == 1
        and said[0].startswith("providers.github.copilot in ")
        and said[0].endswith("— remove it")
    )
    assert parse_pricebook(builtin_prices(), {}, "prices").retired == ()


def test_a_month_that_failed_is_not_asked_again_in_the_same_process() -> None:
    with _report(GhCall("", "gh: HTTP 503")) as call:
        first = collect_bill(_request(), TWO_DAYS)
        again = collect_bill(_request(), TWO_DAYS)
    assert call.call_count == 1, "one failing call per month, not one per report"
    assert first.summary is not None and again.summary is not None
    assert first.summary.missing == again.summary.missing == ("2026-09: gh: HTTP 503",)


def test_a_failed_read_leaves_the_github_counts_their_own_amounts(tmp_path: Path) -> None:
    from ..models import Billing, Collected, Provider, Tokens, UsageRow

    paths = replace(paths_in(tmp_path), offline=False)
    config, book = defaults(paths)
    config = replace(config, subscriptions=(), github=GithubSettings(bill=ACME, actions_plan_exhausted=True))
    minutes = UsageRow(
        Provider.GITHUB,
        "actions",
        RowKind.ACTIONS,
        TWO_DAYS.start,
        "acme/widgets",
        Billing.SUBSCRIPTION,
        Tokens(by_os={"linux": 100.0}, billable=True),
        source="github",
    )
    request = ReportRequest(
        all_projects=True,
        since="2026-09-23T00:00:00Z",
        until="2026-09-25T00:00:00Z",
        github=("acme/widgets",),
    )
    reports: dict[str, Report] = {}
    for label, answer in (("read", GhCall(_month(), "")), ("failed", GhCall("", "gh: HTTP 401"))):
        with (
            _report(answer),
            mock.patch.object(ops_module, "collect_github", return_value=Collected(rows=[minutes])),
            mock.patch.object(ops_module, "now", return_value=READ_AT),
        ):
            reports[label] = build_report(replace(request, groups=("real", "api")), paths, config, book)

    def actions_usd(report: Report) -> float:
        assert report.real is not None
        return sum(line.usd for line in report.real.usage if line.label == "actions")

    assert actions_usd(reports["read"]) == 0, "the read report holds these minutes"
    assert (
        abs(actions_usd(reports["failed"]) - 100 * 0.006) < 1e-9
    ), "a failed read never turns an amount into zero"


def test_a_month_not_read_leaves_its_days_to_their_own_rows_and_the_read_month_to_the_report() -> None:
    from ..collectors.github_bill import settled_by
    from ..models import Billing, Provider, Tokens, UsageRow

    across = Window(datetime(2026, 8, 31, tzinfo=timezone.utc), datetime(2026, 9, 25, tzinfo=timezone.utc))
    named = _request(actions_repos=frozenset({"acme/gadgets"}))
    with _report(GhCall("", "gh: HTTP 502"), GhCall(_month(), "")):
        bill = collect_bill(named, across)
    assert bill.summary is not None and bill.summary.months_read == ("2026-09",)
    assert _sku(bill.summary.counted, "Actions Linux").lines == 1, "September's gadgets minutes: the report's"
    september = UsageRow(
        Provider.GITHUB,
        "actions",
        RowKind.ACTIONS,
        datetime(2026, 9, 24, tzinfo=timezone.utc),
        "acme/gadgets",
        Billing.SUBSCRIPTION,
        Tokens(minutes=121),
        source="github",
    )
    august = replace(september, at=across.start, tokens=Tokens(minutes=40))
    assert settled_by(september, bill.summary, ACME), "its day's month was read: a count"
    assert not settled_by(august, bill.summary, ACME), "August was not read: its own rules price it, once"
