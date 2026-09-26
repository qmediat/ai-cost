"""The live provider day readers (ADR-0008, decision 11): xAI's Management API and Google's billing export."""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from ..daily import Output
from ..models import Provider, ProviderDays, Window
from ..providers import google_billing, xai_usage
from ..providers.google_billing import GoogleRequest
from ..providers.xai_usage import XaiRequest

READ_AT = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
TABLE = "billing-project-1.billing_export.gcp_billing_export_v1_000000_000000_000000"


def _data(name: str) -> str:
    return resources.files(__package__).joinpath(f"data/{name}").read_text(encoding="utf-8")


def _xai(window: Window, answer: str = "", key: str = "k", offline: bool = False) -> ProviderDays:
    sent: list[bytes] = []

    def send(url: str, token: str, body: bytes) -> str:
        sent.append(body)
        return answer or _data("xai-usage-days.json")

    request = XaiRequest("team-1", key, window, READ_AT, offline)
    days = xai_usage.read_days(request, send)
    assert len(sent) <= 1
    return days


def test_xai_days_are_utc_days_with_every_line_of_the_team_exact() -> None:
    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 23, tzinfo=timezone.utc))
    report = _xai(window)
    assert [(d.start.isoformat(), d.gross, d.closed) for d in report.days] == [
        ("2026-09-20T00:00:00+00:00", Decimal("46.28813632"), True),
        ("2026-09-21T00:00:00+00:00", Decimal("117.50240232"), True),
        ("2026-09-22T00:00:00+00:00", Decimal("26.02198632"), True),
    ], "the answer's own decimal text, summed exactly; File Storage is usage too"
    labels = [line.label for line in report.days[1].lines]
    assert labels == ["API grok-4.6", "API grok-4.7", "File Storage"]
    assert all(line.gross == line.net for day in report.days for line in day.lines)


def test_the_xai_query_asks_for_utc_days_and_the_usd_value() -> None:
    body = xai_usage.request_body(date(2026, 9, 20), date(2026, 9, 22))["analyticsRequest"]
    assert body["timeRange"] == {
        "startTime": "2026-09-20 00:00:00",
        "endTime": "2026-09-23 00:00:00",
        "timezone": "UTC",
    }
    assert body["values"] == [{"name": "usd", "aggregation": "AGGREGATION_SUM"}] and body["groupBy"] == [
        "description"
    ]


def test_a_day_less_than_a_day_old_is_not_closed() -> None:
    window = Window(datetime(2026, 9, 25, tzinfo=timezone.utc), datetime(2026, 9, 26, tzinfo=timezone.utc))
    (day,) = _xai(window, answer='{"timeSeries": [], "limitReached": false}').days
    assert (day.lines, day.closed) == ((), False), "a zero day the query covered, ended less than 24 h before"


def test_xai_without_a_key_or_refused_is_unreadable_and_offline_is_only_missing() -> None:
    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))
    assert "XAI_MANAGEMENT_KEY is not set" in _xai(window, key="").unreadable[0].why
    offline = _xai(window, offline=True)
    assert offline.offline and "offline" in offline.missing[0].why and offline.unreadable == ()

    def refuse(url: str, token: str, body: bytes) -> str:
        raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)  # type: ignore[arg-type]

    report = xai_usage.read_days(XaiRequest("team-1", "k", window, READ_AT), refuse)
    assert report.days == () and report.unreadable[0].why.startswith("HTTP 403")


def test_an_xai_answer_cut_at_its_limit_or_of_another_shape_is_never_used() -> None:
    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))
    cut = json.loads(_data("xai-usage-days.json")) | {"limitReached": True}
    for answer in (
        json.dumps(cut),
        '{"series": []}',
        '{"timeSeries": [{"groupLabels": ["x"], "dataPoints": [{"timestamp": "2026-09-20T00:00:00Z", "values": ["n/a"]}]}]}',
    ):
        report = _xai(window, answer=answer)
        assert report.days == () and "not the documented shape" in report.unreadable[0].why


def _google(window: Window, answer: str | None = None, table: str = TABLE) -> ProviderDays:
    commands: list[Sequence[str]] = []

    def run(command: Sequence[str]) -> str:
        commands.append(command)
        return _data("google-billing-days.json") if answer is None else answer

    request = GoogleRequest(table, google_billing.DEFAULT_SERVICES, window, READ_AT)
    days = google_billing.read_days(request, run)
    assert all(c[0] == "bq" and f"`{table}`" in c[-1] for c in commands)
    return days


def test_google_days_convert_by_each_lines_own_rate_and_count_only_the_services_named() -> None:
    window = Window(datetime(2026, 7, 31, tzinfo=timezone.utc), datetime(2026, 8, 3, tzinfo=timezone.utc))
    report = _google(window)
    first = report.days[0]
    usage = [line for line in first.lines if not line.excluded]
    storage = [line for line in first.lines if line.excluded]
    assert {line.label.split(" / ")[0] for line in usage} == {"Gemini API"}
    assert {line.label.split(" / ")[0] for line in storage} == {
        "Cloud Storage"
    }, "another service is listed, excluded"
    assert first.gross == sum(
        Decimal(r) / Decimal("3.75385") for r in ("2.740132", "0.024505", "3.407607", "0.01224", "2.278927")
    )
    rates = {line.rate_note for line in report.days[1].lines if not line.excluded}
    assert rates == {"PLN ÷ 3.80055"}, "August's rate for August's lines, never another"
    assert report.days[2].gross == 0 and len(report.days) == 3


def test_a_google_answer_of_another_shape_is_unreadable_and_no_row_is_a_zero_day() -> None:
    window = Window(datetime(2026, 7, 31, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert "not the shape asked for" in _google(window, answer="Error in query string").unreadable[0].why
    empty = _google(window, answer='\n[{"first_day": "2026-07-01", "last_day": "2026-08-03"}]\n')
    assert [day.lines for day in empty.days] == [()], "no row on a covered day is a zero day"
    none_yet = _google(window, answer='\n[{"first_day": null}]\n')
    assert none_yet.days == () and none_yet.missing[0].why == "the billing export holds no line yet"
    bad = '[{"day": "2026-07-31", "currency": "PLN", "rate": "0", "cost": "1", "credits": "0", "first_day": "2026-07-01", "last_day": "2026-08-03"}]'
    assert "rate is not positive" in _google(window, answer=bad).unreadable[0].why


def test_a_table_name_that_is_not_project_dataset_table_is_never_queried() -> None:
    window = Window(datetime(2026, 7, 31, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc))
    report = _google(window, table="x`; DROP TABLE y; --")
    assert report.days == () and "is not project.dataset.table" in report.unreadable[0].why


def test_the_alibaba_signature_is_the_documented_one() -> None:
    from ..providers.alibaba_bills import authorization, signed_headers

    params = {"ImageId": "win2019_1809_x64_dtc_zh-cn_40G_alibase_20230811.vhd", "RegionId": "cn-shanghai"}
    headers = signed_headers(
        "ecs.cn-shanghai.aliyuncs.com",
        "2023-10-26T10:22:32Z",
        "3156853299f313e23d1673dc12e1703d",
        "RunInstances",
        "2014-05-26",
    )
    signed = authorization("YourAccessKeyId", "YourAccessKeySecret", params, headers)
    assert signed.endswith("Signature=06563a9e1b43f5dfe96b81484da74bceab24a1d853912eee15083a6f0f3283c0")


def test_an_alibaba_day_is_closed_after_noon_on_the_4th_of_the_next_month_beijing_time() -> None:
    from ..providers.alibaba_bills import BILL_ZONE, closed

    day = date(2026, 9, 21)
    assert not closed(day, datetime(2026, 10, 4, 11, 59, tzinfo=BILL_ZONE))
    assert closed(day, datetime(2026, 10, 4, 12, 0, tzinfo=BILL_ZONE))
    assert closed(
        date(2026, 12, 31), datetime(2027, 1, 4, 12, tzinfo=BILL_ZONE)
    ), "December closes in January"


def test_alibaba_bills_are_read_page_by_page_per_beijing_day() -> None:
    from ..providers import alibaba_bills

    pages = {
        "": {
            "Items": [
                {
                    "ProductCode": "sfm",
                    "Item": "PayAsYouGoBill",
                    "Currency": "USD",
                    "PretaxGrossAmount": Decimal("1.25"),
                    "PretaxAmount": Decimal("1.00"),
                }
            ],
            "NextToken": "p2",
        },
        "p2": {
            "Items": [
                {
                    "ProductCode": "sfm",
                    "Item": "PayAsYouGoBill",
                    "Currency": "USD",
                    "PretaxGrossAmount": 2,
                    "PretaxAmount": Decimal("2.00"),
                }
            ]
        },
    }
    asked: list[str] = []

    def send(url: str, headers: Mapping[str, str]) -> str:
        token = url.split("NextToken=")[1].split("&")[0] if "NextToken=" in url else ""
        asked.append(url)
        assert headers["Authorization"].startswith("ACS3-HMAC-SHA256 Credential=id,")
        return json.dumps({"Success": True, "Code": "Success", "Data": pages[token]}, default=str)

    window = Window(
        datetime(2026, 9, 20, 16, tzinfo=timezone.utc), datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
    )
    request = alibaba_bills.AlibabaRequest("id", "secret", window, READ_AT)
    (day,) = alibaba_bills.read_days(request, send).days
    assert (day.start.isoformat(), day.gross, day.net, day.closed) == (
        "2026-09-21T00:00:00+08:00",
        Decimal("3.25"),
        Decimal("3.00"),
        False,
    )
    assert len(asked) == 2 and all("BillingDate=2026-09-21" in url for url in asked)


def test_alibaba_without_keys_or_with_a_refused_answer_is_unreadable() -> None:
    from ..providers import alibaba_bills

    window = Window(
        datetime(2026, 9, 20, 16, tzinfo=timezone.utc), datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
    )
    keyless = alibaba_bills.read_days(alibaba_bills.AlibabaRequest("", "", window, READ_AT))
    assert "ALIBABA_BILL_ACCESS_KEY_ID" in keyless.unreadable[0].why
    refused = alibaba_bills.read_days(
        alibaba_bills.AlibabaRequest("id", "s", window, READ_AT),
        lambda url, headers: '{"Success": false, "Code": "Forbidden.RAM"}',
    )
    assert "Forbidden.RAM" in refused.unreadable[0].why


def test_each_report_source_takes_its_own_settings() -> None:
    from ..config import builtin_config, parse_config
    from ..errors import ConfigError
    from ..providers import ReportSource

    def reports(providers: dict[str, Any]) -> Any:
        return parse_config(builtin_config() | {"providers": providers}, "test").provider_reports

    got = reports(
        {
            "xai": {"report": {"source": "management-api", "team": "t-1"}},
            "google": {"report": {"source": "bigquery-export", "table": TABLE}},
        }
    )
    assert got["xai"] == ReportSource("xai", "management-api", {"team": "t-1"})
    assert got["google"].names("services", google_billing.DEFAULT_SERVICES) == ("Gemini API",)
    for bad, said in (
        ({"xai": {"report": {"source": "management-api"}}}, "team"),
        ({"xai": {"report": {"source": "management-api", "team": "t", "key": "secret"}}}, "not ['key']"),
    ):
        try:
            reports(bad)
        except ConfigError as exc:
            assert said in str(exc)
        else:
            raise AssertionError(f"{bad} must be refused")


def test_a_live_source_offline_is_pending_and_its_day_is_read_again(tmp_path: Path) -> None:
    from dataclasses import replace

    from ..daily import Output, daily_reports, read_index
    from ..providers import ReportSource
    from .fixtures import defaults, paths_in

    paths = paths_in(tmp_path)  # offline: the live source is not asked
    config, book = defaults(paths)
    config = replace(config, provider_reports={"xai": ReportSource("xai", "management-api", {"team": "t"})})
    lines: list[str] = []
    day = date(2026, 9, 20)
    daily_reports(paths, config, book, day, paths.reports_dir, Output(lines.append, lines.append))
    assert read_index(paths.reports_dir / day.isoformat() / "index.json").providers_pending == ("xai",)
    daily_reports(
        paths, config, book, day + timedelta(days=2), paths.reports_dir, Output(lines.append, lines.append)
    )
    assert f"daily {day}: the xai provider report was not final when written — reading it again" in lines
    assert not any(
        "kept as written" in line for line in lines
    ), "a provider that had nothing holds nothing back"
    assert read_index(paths.reports_dir / day.isoformat() / "index.json").providers_pending == ("xai",)


def test_transport_and_decoding_failures_are_unreadable_never_raised() -> None:
    import http.client

    from ..providers import alibaba_bills

    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))

    def cut(url: str, token: str, body: bytes) -> str:
        raise http.client.IncompleteRead(b"{")

    xai = xai_usage.read_days(XaiRequest("t", "k", window, READ_AT), cut)
    assert "IncompleteRead" in xai.unreadable[0].why

    def undecodable(command: Sequence[str]) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    google = google_billing.read_days(GoogleRequest(TABLE, ("Gemini API",), window, READ_AT), undecodable)
    assert "bq could not read" in google.unreadable[0].why
    beijing = Window(
        datetime(2026, 9, 20, 16, tzinfo=timezone.utc), datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
    )
    empty_amount = '{"Success": true, "Data": {"Items": [{"ProductCode": "sfm", "Item": "PayAsYouGoBill", "Currency": "USD", "PretaxGrossAmount": "", "PretaxAmount": "1"}]}}'
    alibaba = alibaba_bills.read_days(
        alibaba_bills.AlibabaRequest("id", "s", beijing, READ_AT), lambda u, h: empty_amount
    )
    assert "PretaxGrossAmount is not a number" in alibaba.unreadable[0].why


def test_a_reader_that_fails_unforeseen_is_an_unreadable_span(tmp_path: Path) -> None:
    from unittest import mock

    from .. import providers
    from ..providers import ReadContext, ReportSource, read_reports

    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))
    ctx = ReadContext(tmp_path, window, READ_AT, False, {})

    def broken(source: ReportSource, context: ReadContext) -> ProviderDays:
        raise ZeroDivisionError("deep in a parser")

    with mock.patch.dict(providers.READERS, {("xai", "management-api"): broken}):
        (report,) = read_reports({"xai": ReportSource("xai", "management-api", {"team": "t"})}, ctx)
    assert report.days == () and report.unreadable[0].why == (
        "the management-api reader failed: ZeroDivisionError: deep in a parser"
    )
    assert report.utc_days, "xAI's days are UTC days even when its read failed: daily reads that day again"


def test_a_window_without_a_whole_day_asks_no_provider() -> None:
    from ..providers import alibaba_bills

    hours = Window(
        datetime(2026, 9, 20, 9, tzinfo=timezone.utc), datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    )

    def never(*args: Any) -> str:
        raise AssertionError("no whole day: nothing to ask for")

    assert xai_usage.read_days(XaiRequest("t", "k", hours, READ_AT), never) == ProviderDays(
        Provider.XAI, xai_usage.SOURCE
    )
    assert google_billing.read_days(GoogleRequest(TABLE, ("Gemini API",), hours, READ_AT), never).days == ()
    assert alibaba_bills.read_days(alibaba_bills.AlibabaRequest("i", "s", hours, READ_AT), never).days == ()


def test_the_team_is_one_path_segment() -> None:
    urls: list[str] = []

    def send(url: str, token: str, body: bytes) -> str:
        urls.append(url)
        return _data("xai-usage-days.json")

    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))
    xai_usage.read_days(XaiRequest("a/b?c", "k", window, READ_AT), send)
    assert urls == ["https://management-api.x.ai/v1/billing/teams/a%2Fb%3Fc/usage"]


def test_only_pay_as_you_go_items_of_the_named_products_are_usage() -> None:
    from ..providers import alibaba_bills

    items = [
        {
            "ProductCode": "sfm",
            "Item": "PayAsYouGoBill",
            "Currency": "USD",
            "PretaxGrossAmount": 2,
            "PretaxAmount": 2,
        },
        {
            "ProductCode": "sfm",
            "Item": "SubscriptionOrder",
            "Currency": "USD",
            "PretaxGrossAmount": 50,
            "PretaxAmount": 50,
        },
        {
            "ProductCode": "oss",
            "Item": "PayAsYouGoBill",
            "Currency": "USD",
            "PretaxGrossAmount": 1,
            "PretaxAmount": 1,
        },
    ]
    beijing = Window(
        datetime(2026, 9, 20, 16, tzinfo=timezone.utc), datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
    )
    request = alibaba_bills.AlibabaRequest("i", "s", beijing, READ_AT, products=("sfm",))
    day = alibaba_bills.provider_day(date(2026, 9, 21), items, request)
    assert (day.gross, day.net) == (Decimal(2), Decimal(2))
    assert sorted((line.label, line.excluded) for line in day.lines) == [
        ("oss (PayAsYouGoBill)", True),
        ("sfm (PayAsYouGoBill)", False),
        ("sfm (SubscriptionOrder)", True),
    ]


def test_the_day_currency_is_its_usage_lines_currency() -> None:
    from ..providers import alibaba_bills

    pay = {
        "ProductCode": "sfm",
        "Item": "PayAsYouGoBill",
        "Currency": "USD",
        "PretaxGrossAmount": 2,
        "PretaxAmount": 2,
    }
    storage = {
        "ProductCode": "oss",
        "Item": "PayAsYouGoBill",
        "Currency": "CNY",
        "PretaxGrossAmount": 1,
        "PretaxAmount": 1,
    }
    beijing = Window(
        datetime(2026, 9, 20, 16, tzinfo=timezone.utc), datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
    )
    request = alibaba_bills.AlibabaRequest("i", "s", beijing, READ_AT, products=("sfm",))
    other = alibaba_bills.provider_day(date(2026, 9, 21), [pay, storage], request)
    assert other.currency == "USD", "an excluded line in another currency does not stop the day's comparison"
    assert [line.label for line in other.lines] == ["oss (PayAsYouGoBill, CNY)", "sfm (PayAsYouGoBill)"]
    mixed = [pay, {**pay, "Currency": "CNY"}]
    try:
        alibaba_bills.provider_day(date(2026, 9, 21), mixed, request)
    except alibaba_bills.MixedCurrencyError as exc:
        assert str(exc) == "usage items in CNY, USD: a day in several currencies is not summed"
    else:
        raise AssertionError("usage in two currencies must not be summed")
    answer = json.dumps({"Success": True, "Data": {"Items": mixed}})
    report = alibaba_bills.read_days(request, lambda url, headers: answer)
    assert report.days == () and report.unreadable[0].why.startswith("usage items in CNY, USD")


def test_an_alibaba_refusal_names_its_code_and_what_it_means() -> None:
    import io
    import urllib.error

    from ..providers import alibaba_bills

    def refuse(url: str, headers: Mapping[str, str]) -> str:
        body = io.BytesIO(b'{"Code": "SignatureDoesNotMatch", "Message": "..."}')
        raise urllib.error.HTTPError(url, 400, "Bad Request", None, body)  # type: ignore[arg-type]

    beijing = Window(
        datetime(2026, 9, 20, 16, tzinfo=timezone.utc), datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
    )
    report = alibaba_bills.read_days(alibaba_bills.AlibabaRequest("id", "s", beijing, READ_AT), refuse)
    assert report.unreadable[0].why == (
        "HTTP 400 from BSS: SignatureDoesNotMatch — the secret does not belong to the AccessKey id"
    )


def test_bq_absent_is_said() -> None:
    from unittest import mock

    window = Window(datetime(2026, 7, 31, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc))
    with mock.patch("shutil.which", return_value=None):
        report = google_billing.read_days(GoogleRequest(TABLE, ("Gemini API",), window, READ_AT))
    assert "bq (Google Cloud CLI) is not installed" in report.unreadable[0].why


def test_doctor_says_a_live_source_offline_as_information(tmp_path: Path) -> None:
    from ..onboarding import Mark, provider_report_checks
    from .fixtures import defaults, paths_in

    config, _ = defaults(paths_in(tmp_path))
    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))
    offline = xai_usage.read_days(XaiRequest("t", "k", window, READ_AT, offline=True))
    (check,) = provider_report_checks(config, [offline], [], READ_AT)
    assert check.mark is Mark.INFO and "offline" in check.text


def test_a_day_still_open_is_read_again_and_written_once_closed(tmp_path: Path) -> None:
    from dataclasses import replace
    from unittest import mock

    from .. import providers
    from ..daily import Output, daily_reports, read_index
    from ..models import ProviderDay, ProviderLine
    from ..providers import ReadContext, ReportSource
    from .fixtures import defaults, paths_in

    paths = paths_in(tmp_path)
    config, book = defaults(paths)
    config = replace(config, provider_reports={"xai": ReportSource("xai", "management-api", {"team": "t"})})
    day = date(2026, 9, 20)
    closed = {"now": False}

    def reader(source: ReportSource, ctx: ReadContext) -> ProviderDays:
        start = datetime(2026, 9, 20, tzinfo=timezone.utc)
        line = ProviderLine("API grok-4.6", Decimal("1.5"), Decimal("1.5"))
        found = ProviderDay(
            Provider.XAI, start, start + timedelta(days=1), (line,), closed["now"], READ_AT, "t"
        )
        return ProviderDays(Provider.XAI, "management-api", (found,), captured=READ_AT, utc_days=True)

    lines: list[str] = []
    with mock.patch.dict(providers.READERS, {("xai", "management-api"): reader}):
        daily_reports(paths, config, book, day, paths.reports_dir, Output(lines.append, lines.append))
        assert read_index(paths.reports_dir / day.isoformat() / "index.json").providers_pending == ("xai",)
        closed["now"] = True
        daily_reports(
            paths,
            config,
            book,
            day + timedelta(days=2),
            paths.reports_dir,
            Output(lines.append, lines.append),
        )
    assert f"daily {day}: the xai provider report was not final when written — reading it again" in lines
    assert read_index(paths.reports_dir / day.isoformat() / "index.json").providers_pending == ()


def test_a_key_with_a_line_break_is_refused_before_it_is_sent_and_never_said() -> None:
    from ..providers import alibaba_bills

    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))

    def never(*args: Any) -> str:
        raise AssertionError("a broken key is never sent")

    report = xai_usage.read_days(XaiRequest("t", "xai-secret\n", window, READ_AT), never)
    assert report.unreadable[0].why.startswith("XAI_MANAGEMENT_KEY holds a space or a line break")
    assert "xai-secret" not in report.unreadable[0].why
    beijing = Window(
        datetime(2026, 9, 20, 16, tzinfo=timezone.utc), datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
    )
    ali = alibaba_bills.read_days(alibaba_bills.AlibabaRequest("id", "s e", beijing, READ_AT), never)
    assert "ALIBABA_BILL_ACCESS_KEY_SECRET holds a space" in ali.unreadable[0].why


def test_the_guard_masks_every_key_an_exception_quotes(tmp_path: Path) -> None:
    from unittest import mock

    from .. import providers
    from ..providers import ReadContext, ReportSource, read_reports

    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))
    ctx = ReadContext(tmp_path, window, READ_AT, False, {"XAI_MANAGEMENT_KEY": "xai-live-key"})

    def quoting(source: ReportSource, context: ReadContext) -> ProviderDays:
        raise ValueError("Invalid header value b'Bearer xai-live-key\\n'")

    with mock.patch.dict(providers.READERS, {("xai", "management-api"): quoting}):
        (report,) = read_reports({"xai": ReportSource("xai", "management-api", {"team": "t"})}, ctx)
    assert "xai-live-key" not in report.unreadable[0].why and "***" in report.unreadable[0].why


def test_google_days_after_the_exports_newest_line_are_missing_not_zero() -> None:
    window = Window(datetime(2026, 7, 31, tzinfo=timezone.utc), datetime(2026, 8, 6, tzinfo=timezone.utc))
    report = _google(window)
    assert report.days[-1].start.date() == date(2026, 8, 3)
    assert [(s.start.date(), s.why) for s in report.missing] == [
        (date(2026, 8, 4), "after the billing export's newest line")
    ]


def test_one_alibaba_day_that_fails_costs_that_day_only() -> None:
    import urllib.error

    from ..providers import alibaba_bills

    good = '{"Success": true, "Data": {"Items": [{"ProductCode": "sfm", "Item": "PayAsYouGoBill", "Currency": "USD", "PretaxGrossAmount": 1, "PretaxAmount": 1}]}}'

    def send(url: str, headers: Mapping[str, str]) -> str:
        if "BillingDate=2026-09-22" in url:
            raise urllib.error.URLError("timed out")
        return good

    window = Window(
        datetime(2026, 9, 20, 16, tzinfo=timezone.utc), datetime(2026, 9, 22, 16, tzinfo=timezone.utc)
    )
    report = alibaba_bills.read_days(alibaba_bills.AlibabaRequest("id", "s", window, READ_AT), send)
    assert [d.start.date() for d in report.days] == [date(2026, 9, 21)]
    assert [(s.start.isoformat(), "timed out" in s.why) for s in report.unreadable] == [
        ("2026-09-22T00:00:00+08:00", True)
    ]


def test_every_reader_has_its_settings_and_every_setting_a_reader() -> None:
    from ..providers import NAMES, READERS, SETTINGS, TEXT

    assert set(READERS) == set(SETTINGS)
    assert all(kind in (TEXT, NAMES) for takes in SETTINGS.values() for kind, _ in takes.values())


def test_a_project_report_asks_no_provider(tmp_path: Path) -> None:
    from dataclasses import replace
    from unittest import mock

    from .. import providers
    from ..ops import ReportRequest, build_report
    from ..providers import ReportSource
    from .fixtures import defaults, paths_in

    paths = paths_in(tmp_path)
    config, book = defaults(paths)
    config = replace(config, provider_reports={"xai": ReportSource("xai", "management-api", {"team": "t"})})

    def never(*args: Any) -> ProviderDays:
        raise AssertionError("a per-project report reads no provider")

    request = ReportRequest(project=tmp_path, since="2026-09-20T00:00:00Z", until="2026-09-21T00:00:00Z")
    with mock.patch.dict(providers.READERS, {("xai", "management-api"): never}):
        report = build_report(request, paths, config, book)
    assert "provider reports: not read — a per-project report holds part of the account" in report.sources


def test_a_utc_provider_without_data_yet_is_pending_and_a_deepseek_day_is_not() -> None:
    from ..daily import providers_pending
    from ..models import DayComparison, ProviderDay, ProviderSummary, Span

    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    hole = (Span(start, start + timedelta(days=1), "after the billing export's newest line"),)
    empty = ProviderSummary(Provider.GOOGLE, "bigquery-export", (), hole, utc_days=True)
    offset = timezone(timedelta(hours=2))
    day = ProviderDay(
        Provider.DEEPSEEK,
        start.astimezone(offset),
        start.astimezone(offset) + timedelta(days=1),
        (),
        True,
        start,
        "x",
    )
    deepseek = ProviderSummary(
        Provider.DEEPSEEK,
        "deepseek-export",
        (DayComparison(day, "the window holds only part of the day"),),
        hole,
    )

    class Seen:
        provider_reports = (empty, deepseek)

    assert providers_pending(Seen()) == ("google",)  # type: ignore[arg-type]


def test_a_boolean_timestamp_is_not_a_time() -> None:
    window = Window(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, tzinfo=timezone.utc))
    answer = '{"timeSeries": [{"groupLabels": ["x"], "dataPoints": [{"timestamp": true, "values": [1]}]}]}'
    assert "without its time" in _xai(window, answer=answer).unreadable[0].why


def test_an_answer_at_bqs_row_cap_is_refused() -> None:
    from unittest import mock

    window = Window(datetime(2026, 7, 31, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc))
    with mock.patch.object(google_billing, "MAX_ROWS", 3):
        report = _google(window)
    assert "row cap" in report.unreadable[0].why


def _quiet() -> Output:
    """An output that drops every line, for a test that checks none."""
    return Output(lambda line: None, lambda line: None)


def test_a_written_day_is_kept_only_for_what_the_new_report_would_lose(tmp_path: Path) -> None:
    import json as json_module

    from ..daily import _why_keep
    from ..models import ProviderSummary, Span

    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    hole = (Span(start, start + timedelta(days=1), "after the billing export's newest line"),)
    written = tmp_path / "global.json"

    class Seen:
        provider_reports = (ProviderSummary(Provider.GOOGLE, "bigquery-export", (), hole, utc_days=True),)
        github_bill = None

    written.write_text(
        json_module.dumps({"provider_reports": [{"provider": "google", "days": [{"why_not": ""}]}]})
    )
    assert _why_keep(Seen(), written, _quiet()) == "the provider report of google compared this day before and cannot now"  # type: ignore[arg-type]
    written.write_text(json_module.dumps({"provider_reports": [{"provider": "google", "days": []}]}))
    assert _why_keep(Seen(), written, _quiet()) == "", "nothing compared before: nothing to lose"  # type: ignore[arg-type]
    written.write_text('{"provider_reports": [{"provider": "goo')
    said: list[str] = []
    assert _why_keep(Seen(), written, Output(said.append, said.append)) == ""  # type: ignore[arg-type]
    assert said == [f"daily: {written} cannot be read — the new report replaces it"]


def test_a_day_read_but_not_compared_for_a_local_reason_replaces_the_written_one(tmp_path: Path) -> None:
    import json as json_module

    from ..daily import _why_keep
    from ..models import DayComparison, ProviderDay, ProviderSummary

    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    day = ProviderDay(Provider.XAI, start, start + timedelta(days=1), (), True, READ_AT, "management-api")
    local = DayComparison(day, "its local cash sits on other rows (a ledger or a settled row)")
    written = tmp_path / "global.json"
    written.write_text(
        json_module.dumps({"provider_reports": [{"provider": "xai", "days": [{"why_not": ""}]}]})
    )

    class Now:
        provider_reports = (ProviderSummary(Provider.XAI, "management-api", (local,), utc_days=True),)
        github_bill = None

    assert _why_keep(Now(), written, _quiet()) == "", "the local records changed: the new report is what they say now"  # type: ignore[arg-type]


def test_a_converted_amount_is_shown_with_the_providers_own_rate() -> None:
    from ..models import DayComparison, ProviderDay, ProviderLine, ProviderSummary
    from ..render import _provider_section

    start = datetime(2026, 7, 31, tzinfo=timezone.utc)
    line = ProviderLine("Gemini API / tokens (regular)", Decimal(1), Decimal(1), rate_note="PLN ÷ 3.75385")
    day = ProviderDay(
        Provider.GOOGLE, start, start + timedelta(days=1), (line,), True, READ_AT, "bigquery-export"
    )
    summary = ProviderSummary(Provider.GOOGLE, "bigquery-export", (DayComparison(day, "not compared here"),))
    assert any(
        text.endswith("in USD by the provider's own rate: PLN ÷ 3.75385")
        for text in _provider_section(summary)
    )
