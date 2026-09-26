"""xAI's own day report (ADR-0008, decision 11): the team's billed usage from the Management API, per UTC day.

``POST https://management-api.x.ai/v1/billing/teams/<team>/usage`` with a management key (``XAI_MANAGEMENT_KEY``)
answers ``timeSeries[{groupLabels, dataPoints[{timestamp, values:[usd]}]}]``; asked with ``timezone: UTC`` its days are
UTC days, so ``daily`` holds them whole. The time range's end is exclusive ("not including", the API reference), so a
query ends at the next midnight. Amounts are read as the answer's own decimal text. Every line of the team is usage
(the models, ``File Storage``): gross and net are the same figure, xAI states no discount.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..models import Provider, ProviderDay, ProviderDays, ProviderLine, Window
from ..timeutil import parse_ts
from .common import day_interval, days_touched, exact_amount, holds_a_whole_day, key_problem, not_read, redact

SOURCE = "management-api"
ENDPOINT = "https://management-api.x.ai/v1/billing/teams/{team}/usage"
KEY_ENV = "XAI_MANAGEMENT_KEY"
SETTLE = timedelta(hours=24)  # a day is closed a day after it ended: xAI documents no delay (the tool's rule)
TIMEOUT_S = 30

Post = Callable[[str, str, bytes], str]


@dataclass(frozen=True)
class XaiRequest:
    """What one read needs: the team, the key, the window, when it is read, whether the network is allowed."""

    team: str
    key: str
    window: Window
    read_at: datetime
    offline: bool = False


class UsageShapeError(ValueError):
    """The answer is not the documented shape: nothing of it is used."""


def request_body(first: date, last: date) -> dict[str, Any]:
    """The usage query: USD per day and line (``description``), UTC days ``first`` through ``last`` (end exclusive)."""
    return {
        "analyticsRequest": {
            "timeRange": {
                "startTime": f"{first.isoformat()} 00:00:00",
                "endTime": f"{(last + timedelta(days=1)).isoformat()} 00:00:00",
                "timezone": "UTC",
            },
            "timeUnit": "TIME_UNIT_DAY",
            "values": [{"name": "usd", "aggregation": "AGGREGATION_SUM"}],
            "groupBy": ["description"],
            "filters": [],
        }
    }


def parse_usage(text: str) -> dict[date, dict[str, Decimal]]:
    """``{UTC date: {line label: USD}}`` from the answer; a shape it does not document raises ``UsageShapeError``."""
    try:
        data = json.loads(text, parse_float=Decimal)
    except ValueError as exc:
        raise UsageShapeError(f"not JSON ({exc})") from exc
    if not isinstance(data, Mapping) or not isinstance(data.get("timeSeries"), list):
        raise UsageShapeError("no timeSeries list")
    if data.get("limitReached") is True:
        raise UsageShapeError("the answer stopped at the API's limit: a part of the usage is missing")
    days: dict[date, dict[str, Decimal]] = defaultdict(dict)
    for series in data["timeSeries"]:
        label = _label(series)
        for stamp, usd in _points(series):
            days[stamp][label] = days[stamp].get(label, Decimal(0)) + usd
    return dict(days)


def _label(series: Any) -> str:
    labels = series.get("groupLabels") if isinstance(series, Mapping) else None
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) for x in labels):
        raise UsageShapeError(f"a series without its labels: {str(series)[:120]}")
    return " ".join(labels)


def _points(series: Mapping[str, Any]) -> list[tuple[date, Decimal]]:
    points = series.get("dataPoints")
    if not isinstance(points, list):
        raise UsageShapeError("a series without dataPoints")
    found = []
    for point in points:
        text = point.get("timestamp") if isinstance(point, Mapping) else None
        stamp = parse_ts(text) if isinstance(text, str) else None  # an ISO time, never a number or a boolean
        values = point.get("values") if isinstance(point, Mapping) else None
        if stamp is None or not isinstance(values, list) or len(values) != 1:
            raise UsageShapeError(f"a data point without its time or one value: {str(point)[:120]}")
        amount = exact_amount(values[0], "a data point's value", UsageShapeError)
        if amount < 0:
            raise UsageShapeError(f"a negative amount: {values[0]!r}")
        found.append((stamp.date(), amount))
    return found


def provider_days(
    by_day: Mapping[date, Mapping[str, Decimal]], first: date, last: date, read_at: datetime
) -> tuple[ProviderDay, ...]:
    """Every UTC day of the query: a day the answer does not name is a zero day (the query covered it)."""
    days = []
    for index in range((last - first).days + 1):
        day = first + timedelta(days=index)
        start, end = day_interval(day, timezone.utc)
        lines = tuple(
            ProviderLine(label, usd, usd) for label, usd in sorted(by_day.get(day, {}).items()) if usd != 0
        )
        days.append(ProviderDay(Provider.XAI, start, end, lines, end + SETTLE <= read_at, read_at, SOURCE))
    return tuple(days)


def post(url: str, key: str, body: bytes) -> str:
    """One POST with the management key; the answer's text, or the ``urllib`` error."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return str(response.read().decode("utf-8"))


def read_days(request: XaiRequest, send: Post = post) -> ProviderDays:
    """The team's days touching the window, or the whole window as a span not read, with the reason."""
    if not holds_a_whole_day(request.window, timezone.utc):
        return ProviderDays(Provider.XAI, SOURCE)  # nothing to compare: the Management API is not asked
    reason = _why_unread(request)
    if reason:
        return not_read(Provider.XAI, SOURCE, request.window, reason, request.offline)
    first, last = days_touched(request.window, timezone.utc)
    try:
        body = json.dumps(request_body(first, last)).encode()
        url = ENDPOINT.format(team=urllib.parse.quote(request.team, safe=""))
        by_day = parse_usage(send(url, request.key, body))
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code} from the Management API ({_hint(exc.code)})"
    except (urllib.error.URLError, OSError, http.client.HTTPException, UnicodeDecodeError) as exc:
        reason = f"the Management API could not be read ({exc.__class__.__name__}: {exc})"
    except UsageShapeError as exc:
        reason = f"the usage answer is not the documented shape: {exc}"
    else:
        days = provider_days(by_day, first, last, request.read_at)
        return ProviderDays(Provider.XAI, SOURCE, days, captured=request.read_at)
    return not_read(Provider.XAI, SOURCE, request.window, redact(reason, [request.key]), offline=False)


def _why_unread(request: XaiRequest) -> str:
    if request.offline:
        return "offline (AI_COST_OFFLINE): the Management API is not asked"
    problem = key_problem(KEY_ENV, request.key)
    return f"{problem}: a management key with billing read access is needed" if problem else ""


def _hint(code: int) -> str:
    return {
        401: "the management key is not accepted",
        403: "the key may not read this team's billing",
        404: "no such team",
        429: "rate limited: the next report asks again",
    }.get(code, "the report reads it again next time")
