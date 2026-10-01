"""One row per model: tokens once, how it was paid and on what evidence, beside the list price."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ..groups import api_group, real_group
from ..models import Billing, Provider, RowKind, Tokens, UsageRow
from ..permodel import Evidence, Paid, model_lines, payment_of
from ..render import render_json, render_markdown
from .fixtures import BASE, WINDOW, defaults, paths_in


def _row(
    provider: Provider, model: str, billing: Billing, kind: RowKind = RowKind.LOG, **extra: object
) -> UsageRow:
    fields: dict[str, object] = {
        "tokens": Tokens(input=100_000, output=10_000),
        "ref": f"{model}-1",
        "source": "t",
    }
    fields.update(extra)
    return UsageRow(provider=provider, model=model, kind=kind, at=BASE, billing=billing, **fields)  # type: ignore[arg-type]


PLAN = _row(Provider.ANTHROPIC, "claude-fable-5-1", Billing.SUBSCRIPTION, RowKind.TRANSCRIPT)
LISTED = _row(Provider.DEEPSEEK, "deepseek-flash", Billing.API)
REPORTED = _row(Provider.XAI, "grok-4.7", Billing.API, RowKind.SESSION, cost_reported=0.39)
SETTLED = _row(Provider.OPENAI, "gpt-6-astra", Billing.API_SETTLED, RowKind.SESSION, ref="cx")
LEDGER = _row(
    Provider.OPENAI, "gpt-6-astra", Billing.API, RowKind.LEDGER, ref="cx", cost_reported=1.56, tokens=Tokens()
)
UNKNOWN = _row(Provider.OPENAI, "gpt-5.5", Billing.UNKNOWN)


def _book(tmp_path: Path):  # type: ignore[no-untyped-def]
    return defaults(paths_in(tmp_path))


def test_each_row_is_paid_one_way_on_one_kind_of_evidence(tmp_path: Path) -> None:
    config, book = _book(tmp_path)
    assert payment_of(PLAN, book, config)[:2] == (Paid.PLAN, Evidence.NONE)
    assert payment_of(LISTED, book, config)[:2] == (Paid.API, Evidence.LIST)
    assert payment_of(REPORTED, book, config) == (Paid.API, Evidence.REPORTED, 0.39)
    assert payment_of(SETTLED, book, config) == (
        Paid.ELSEWHERE,
        Evidence.NONE,
        0.0,
    ), "its amount is on another row"
    assert payment_of(LEDGER, book, config) == (Paid.API, Evidence.LEDGER, 1.56)
    assert payment_of(UNKNOWN, book, config) == (Paid.UNKNOWN, Evidence.NONE, 0.0)
    unfigured = replace(LEDGER, cost_reported=None)
    assert payment_of(unfigured, book, config) == (Paid.UNKNOWN, Evidence.NONE, 0.0), "never booked as 0 cash"


def test_a_model_paid_two_ways_shows_both_and_its_tokens_once(tmp_path: Path) -> None:
    config, book = _book(tmp_path)
    rows = [SETTLED, LEDGER, replace(SETTLED, billing=Billing.SUBSCRIPTION, ref="cx-plan")]
    (astra,) = model_lines(rows, api_group(rows, book, config), book, config)
    assert (astra.model, astra.records, astra.paid_usd) == (
        "gpt-6-astra",
        2,
        1.56,
    ), "the ledger line is no record"
    assert {(p.paid, p.evidence) for p in astra.payments} == {
        (Paid.API, Evidence.LEDGER),
        (Paid.ELSEWHERE, Evidence.NONE),
        (Paid.PLAN, Evidence.NONE),
    }
    assert astra.tokens.input == 200_000, "the ledger line adds cash, never tokens"


def test_the_models_paid_sum_to_the_real_groups_cash(tmp_path: Path) -> None:
    config, book = _book(tmp_path)
    rows = [PLAN, LISTED, REPORTED, SETTLED, LEDGER, UNKNOWN]
    lines = model_lines(rows, api_group(rows, book, config), book, config)
    real = real_group(rows, book, config, WINDOW)
    assert abs(sum(line.paid_usd for line in lines) - real.cash_usd) < 1e-9
    prices = [line.api_usd for line in lines]
    assert prices == sorted(prices, reverse=True) and prices[0] > 0, "the largest list price first"


def test_the_report_shows_the_table_and_json_carries_it(tmp_path: Path) -> None:
    from ..ops import ReportRequest, build_report
    from ..timeutil import iso
    from .fixtures import write_claude_session

    paths = paths_in(tmp_path)
    write_claude_session(paths, tmp_path / "proj")
    config, book = defaults(paths)
    request = ReportRequest(
        since=iso(WINDOW.start), until=iso(WINDOW.end), all_projects=True, groups=("real", "api")
    )
    report = build_report(request, paths, config, book)
    text = render_markdown(report)
    assert "## Per model" in text and "plan ×" in text
    data = json.loads(render_json(report, detail=False))
    assert data["models"] and data["models"][0]["payments"][0]["paid"] == "plan"
    only_real = build_report(replace(request, groups=("real",)), paths, config, book)
    assert only_real.models == (), "the table needs the list price: the API group"


def test_the_github_rows_are_paid_as_the_usage_report_says(tmp_path: Path) -> None:
    from datetime import date

    from ..models import SEAT_UNIT, InvoiceAmounts, Untracked

    config, book = _book(tmp_path)
    day = date(2026, 9, 19)
    seat = _row(
        Provider.GITHUB,
        "Copilot Business",
        Billing.API,
        RowKind.INVOICE,
        tokens=Tokens(),
        invoice=InvoiceAmounts(day, SEAT_UNIT, 1, 19.0, 19.0, 0.0, 19.0),
    )
    minutes = _row(
        Provider.GITHUB,
        "Actions Linux",
        Billing.API,
        RowKind.INVOICE,
        tokens=Tokens(),
        invoice=InvoiceAmounts(day, "minute", 100, 0.008, 0.8, 0.0, 0.8),
    )
    settled = _row(
        Provider.GITHUB, "copilot-code-review", Billing.API_SETTLED, RowKind.COPILOT, tokens=Tokens(reviews=5)
    )
    untracked = _row(
        Provider.DEEPSEEK,
        "deepseek-flash",
        Billing.API,
        RowKind.UNTRACKED,
        tokens=Tokens(),
        untracked=Untracked("2026-09-19T00:00Z/2026-09-20T00:00Z", 0.3, 0.3),
    )
    assert payment_of(seat, book, config) == (Paid.PLAN, Evidence.USAGE_REPORT, 0.0), "a seat is a share"
    assert payment_of(minutes, book, config) == (Paid.BILLED, Evidence.USAGE_REPORT, 0.8)
    assert payment_of(settled, book, config) == (
        Paid.ELSEWHERE,
        Evidence.USAGE_REPORT,
        0.0,
    ), "the report, no ledger"
    assert payment_of(untracked, book, config) == (Paid.UNTRACKED, Evidence.PROVIDER_REPORT, 0.3)


def test_a_plan_row_that_still_carries_cash_is_metered_not_plan(tmp_path: Path) -> None:
    config, book = _book(tmp_path)
    over = replace(config, github=replace(config.github, actions_plan_exhausted=True))
    actions = _row(
        Provider.GITHUB,
        "actions-linux",
        Billing.SUBSCRIPTION,
        RowKind.ACTIONS,
        tokens=Tokens(minutes=120, by_os={"linux": 120}, billable=True),
    )
    paid, evidence, usd = payment_of(actions, book, over)
    assert paid is Paid.METERED and usd > 0 and evidence is Evidence.LIST
    line = model_lines([actions], api_group([actions], book, over), book, over)[0]
    assert (
        line.payments[0].text.startswith("metered beyond the plan ")
        and "(list price)" in line.payments[0].text
    )


def test_minutes_no_list_price_covers_are_unknown_not_plan(tmp_path: Path) -> None:
    config, book = _book(tmp_path)
    over = replace(config, github=replace(config.github, actions_plan_exhausted=True))
    odd = _row(
        Provider.GITHUB,
        "actions-odd",
        Billing.SUBSCRIPTION,
        RowKind.ACTIONS,
        tokens=Tokens(minutes=30, by_os={"riscv": 30}, billable=True),
    )
    assert payment_of(odd, book, over) == (Paid.UNKNOWN, Evidence.NONE, 0.0)


def test_a_reported_figure_is_known_by_its_value_and_an_unlisted_price_is_said(tmp_path: Path) -> None:
    config, book = _book(tmp_path)
    reported = _row(Provider.OPENAI, "gpt-6-astra", Billing.API, RowKind.SESSION, cost_reported=0.42)
    assert payment_of(reported, book, config)[:2] == (Paid.API, Evidence.REPORTED)
    unlisted = _row(Provider.XAI, "grok-99-unlisted", Billing.API, RowKind.SESSION, cost_reported=0.7)
    (line,) = model_lines([unlisted], api_group([unlisted], book, config), book, config)
    assert line.api_unlisted and line.api_usd == 0.7


def test_a_seat_is_no_model_row(tmp_path: Path) -> None:
    from datetime import date

    from ..models import SEAT_UNIT, InvoiceAmounts

    config, book = _book(tmp_path)
    seat = _row(
        Provider.GITHUB,
        "Copilot Business",
        Billing.API,
        RowKind.INVOICE,
        tokens=Tokens(),
        invoice=InvoiceAmounts(date(2026, 9, 19), SEAT_UNIT, 1, 19.0, 19.0, 0.0, 19.0),
    )
    assert (
        model_lines([seat, LISTED], api_group([seat, LISTED], book, config), book, config)[0].model
        == "deepseek-flash"
    )
    assert len(model_lines([seat], api_group([seat], book, config), book, config)) == 0


def test_a_copilot_count_without_the_usage_report_is_unknown_never_plan(tmp_path: Path) -> None:
    config, book = _book(tmp_path)
    counted = _row(
        Provider.GITHUB,
        "copilot-code-review",
        Billing.SUBSCRIPTION,
        RowKind.COPILOT,
        tokens=Tokens(reviews=1),
    )
    assert payment_of(counted, book, config)[:2] == (Paid.UNKNOWN, Evidence.NONE), "the report was not read"
    settled = replace(counted, billing=Billing.API_SETTLED)
    assert payment_of(settled, book, config)[:2] == (Paid.ELSEWHERE, Evidence.USAGE_REPORT)
    charged = replace(counted, billing=Billing.API, cost_reported=0.5)
    paid, evidence, usd = payment_of(charged, book, config)
    assert (paid, evidence, usd) == (
        Paid.API,
        Evidence.REPORTED,
        0.5,
    ), "a Copilot row with its own charge keeps it"
    (line,) = model_lines([charged], api_group([charged], book, config), book, config)
    assert abs(line.paid_usd - real_group([charged], book, config, WINDOW).cash_usd) < 1e-9


def test_a_payment_below_half_a_cent_says_so() -> None:
    from ..permodel import Payment

    assert Payment(Paid.API, Evidence.LIST, 0.003, 1).text == "API key < 0.01 (list price)"
    assert (
        Payment(Paid.API, Evidence.LIST, -1.234, 1).text == "API key -1.23 (list price)"
    ), "a credit keeps its sign"
    assert Payment(Paid.API, Evidence.LIST, -0.001, 1).text == "API key > -0.01 (list price)", "never -0.00"
    assert Payment(Paid.API, Evidence.LIST, 0.006, 1).text == "API key 0.01 (list price)"
