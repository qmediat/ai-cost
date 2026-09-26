"""Alibaba Cloud's own day report (ADR-0008, decision 11): BSS ``DescribeInstanceBill`` per billing date.

A RAM user whose only permission is ``bssapi:DescribeInstanceBill`` signs each request with ACS3-HMAC-SHA256 (the
keys in ``ALIBABA_BILL_ACCESS_KEY_ID`` / ``ALIBABA_BILL_ACCESS_KEY_SECRET``). Every time on an Alibaba bill is UTC+8
("All times on your bills … are in UTC+8"), so a provider day runs from 00:00+08:00; a month's data is final "after
12:00 on the 4th of the following month", and a day is closed only then. ``PretaxGrossAmount`` is the gross,
``PretaxAmount`` the net; an item in a currency other than USD keeps its currency and is not compared.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..models import Provider, ProviderDay, ProviderDays, ProviderLine, Span, Window
from .common import day_interval, days_touched, exact_amount, holds_a_whole_day, key_problem, not_read, redact

SOURCE = "bss-api"
ENDPOINT = "business.ap-southeast-1.aliyuncs.com"  # the international site's BSS endpoint
ACTION, VERSION = "DescribeInstanceBill", "2017-12-14"
KEY_ID_ENV, SECRET_ENV = "ALIBABA_BILL_ACCESS_KEY_ID", "ALIBABA_BILL_ACCESS_KEY_SECRET"
BILL_ZONE = timezone(timedelta(hours=8))
FINAL_AT = time(12)  # on the 4th of the next month, UTC+8
ALGORITHM = "ACS3-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
TIMEOUT_S = 30
PAGE = 300

Send = Callable[[str, Mapping[str, str]], str]


@dataclass(frozen=True)
class AlibabaRequest:
    """One read: the keys, the endpoint, the products that count (empty = every product), the window, when."""

    key_id: str
    secret: str
    window: Window
    read_at: datetime
    endpoint: str = ENDPOINT
    products: tuple[str, ...] = ()
    offline: bool = False


class BillShapeError(ValueError):
    """The answer is not the documented shape, or says it failed: nothing of it is used."""


class MixedCurrencyError(ValueError):
    """A day's usage items in more than one currency: their sum is no amount, so the day is not used."""


def _encode(text: str) -> str:
    """RFC 3986: unreserved characters stay, everything else is percent-encoded (a space is %20)."""
    return urllib.parse.quote(text, safe="-_.~")


def canonical_query(params: Mapping[str, str]) -> str:
    """The query parameters sorted by name, each name and value encoded."""
    return "&".join(f"{_encode(k)}={_encode(v)}" for k, v in sorted(params.items()))


def signed_headers(
    host: str, stamp: str, nonce: str, action: str = ACTION, version: str = VERSION
) -> dict[str, str]:
    """The headers a signature covers, before ``Authorization``."""
    return {
        "host": host,
        "x-acs-action": action,
        "x-acs-content-sha256": EMPTY_SHA256,
        "x-acs-date": stamp,
        "x-acs-signature-nonce": nonce,
        "x-acs-version": version,
    }


def authorization(key_id: str, secret: str, params: Mapping[str, str], headers: Mapping[str, str]) -> str:
    """ACS3-HMAC-SHA256 over a POST to ``/`` with the parameters in the query and an empty body."""
    names = sorted(headers)
    canonical = "\n".join(
        [
            "POST",
            "/",
            canonical_query(params),
            "".join(f"{name}:{headers[name].strip()}\n" for name in names),
            ";".join(names),
            EMPTY_SHA256,
        ]
    )
    to_sign = f"{ALGORITHM}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signature = hmac.new(secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{ALGORITHM} Credential={key_id},SignedHeaders={';'.join(names)},Signature={signature}"


def send(url: str, headers: Mapping[str, str]) -> str:
    """One signed POST; the answer's text, or the ``urllib`` error."""
    request = urllib.request.Request(url, data=b"", method="POST", headers=dict(headers))
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return str(response.read().decode("utf-8"))


def bill_days(window: Window) -> list[date]:
    """The UTC+8 billing dates the window touches."""
    first, last = days_touched(window, BILL_ZONE)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def closed(day: date, read_at: datetime) -> bool:
    """A billing date is final once its month's data is: after 12:00 UTC+8 on the 4th of the next month."""
    next_month = (day.replace(day=28) + timedelta(days=4)).replace(day=4)
    return read_at >= datetime.combine(next_month, FINAL_AT, BILL_ZONE)


def _page(request: AlibabaRequest, day: date, token: str, sender: Send) -> Mapping[str, Any]:
    params = {"BillingCycle": day.strftime("%Y-%m"), "BillingDate": day.isoformat(), "Granularity": "DAILY"}
    params |= {"MaxResults": str(PAGE)} | ({"NextToken": token} if token else {})
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = signed_headers(request.endpoint, stamp, secrets.token_hex(16))
    headers["Authorization"] = authorization(request.key_id, request.secret, params, headers)
    text = sender(f"https://{request.endpoint}/?{canonical_query(params)}", headers)
    try:
        answer = json.loads(text, parse_float=Decimal)
    except ValueError as exc:
        raise BillShapeError(f"not JSON ({exc})") from exc
    if (
        not isinstance(answer, Mapping)
        or answer.get("Success") is not True
        or not isinstance(answer.get("Data"), Mapping)
    ):
        raise BillShapeError(f"{_code_of(answer)}: not a successful bill")
    return answer["Data"]  # type: ignore[no-any-return]


def day_items(request: AlibabaRequest, day: date, sender: Send) -> list[Mapping[str, Any]]:
    """Every item of one billing date, page by page."""
    items: list[Mapping[str, Any]] = []
    token = ""
    for _ in range(1000):  # a bound, never a loop without end: 300 000 items a day
        data = _page(request, day, token, sender)
        page = data.get("Items")
        if not isinstance(page, list) or not all(isinstance(item, Mapping) for item in page):
            raise BillShapeError("Data.Items is not a list of items")
        items += page
        token = str(data.get("NextToken") or "")
        if not token:
            return items
    raise BillShapeError("more than 1000 pages for one day")


USAGE_ITEM = (
    "PayAsYouGoBill"  # the other items (SubscriptionOrder, Refund, Adjustment) are listed, never usage
)


def provider_day(day: date, items: Sequence[Mapping[str, Any]], request: AlibabaRequest) -> ProviderDay:
    """One billing date: a line per product and item kind, summed exactly.

    Only pay-as-you-go items are usage; a subscription order, a refund or an adjustment is listed as excluded, and so
    is a product not named in ``products`` (when it names any). A line in another currency than USD names it; usage in
    more than one currency raises ``MixedCurrencyError``.
    """
    sums: dict[tuple[str, str, str], list[Decimal]] = {}
    for item in items:
        key = (
            str(item.get("ProductCode") or "?"),
            str(item.get("Item") or "?"),
            str(item.get("Currency") or "?"),
        )
        total = sums.setdefault(key, [Decimal(0), Decimal(0)])
        total[0] += exact_amount(item.get("PretaxGrossAmount"), "PretaxGrossAmount", BillShapeError)
        total[1] += exact_amount(item.get("PretaxAmount"), "PretaxAmount", BillShapeError)
    lines = tuple(
        ProviderLine(
            _label(code, kind, currency), gross, net, excluded=_excluded(code, kind, request.products)
        )
        for (code, kind, currency), (gross, net) in sorted(sums.items())
    )
    currencies = sorted({cur for (code, kind, cur) in sums if not _excluded(code, kind, request.products)})
    if len(currencies) > 1:
        raise MixedCurrencyError(
            f"usage items in {', '.join(currencies)}: a day in several currencies is not summed"
        )
    currency = currencies[0] if currencies else "USD"
    start, end = day_interval(day, BILL_ZONE)
    return ProviderDay(
        Provider.ALIBABA, start, end, lines, closed(day, request.read_at), request.read_at, SOURCE, currency
    )


def _label(code: str, kind: str, currency: str) -> str:
    return f"{code} ({kind})" if currency == "USD" else f"{code} ({kind}, {currency})"


def _excluded(code: str, kind: str, products: Sequence[str]) -> bool:
    return kind != USAGE_ITEM or (bool(products) and code not in products)


def read_days(request: AlibabaRequest, sender: Send = send) -> ProviderDays:
    """The account's billing dates touching the window, or the whole window as a span not read, with the reason."""
    if not holds_a_whole_day(request.window, BILL_ZONE):
        return ProviderDays(Provider.ALIBABA, SOURCE)  # nothing to compare: BSS is not asked
    reason = _why_unread(request)
    if reason:
        return not_read(Provider.ALIBABA, SOURCE, request.window, reason, request.offline)
    days: list[ProviderDay] = []
    failed: list[Span] = []
    for day in bill_days(request.window):  # one day that fails costs that day, never the ones already read
        found = _one_day(request, day, sender)
        days += [found] if isinstance(found, ProviderDay) else []
        failed += [found] if isinstance(found, Span) else []
    return ProviderDays(
        Provider.ALIBABA, SOURCE, tuple(days), captured=request.read_at, unreadable=tuple(failed)
    )


def _one_day(request: AlibabaRequest, day: date, sender: Send) -> ProviderDay | Span:
    """One billing date, or the span of that day with why it could not be read."""
    try:
        return provider_day(day, day_items(request, day, sender), request)
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code} from BSS: {_refusal(exc)}"
    except (urllib.error.URLError, OSError, http.client.HTTPException, UnicodeDecodeError) as exc:
        reason = f"BSS could not be read ({exc.__class__.__name__}: {exc})"
    except BillShapeError as exc:
        reason = f"the bill answer is not the documented shape: {exc}"
    except MixedCurrencyError as exc:
        reason = str(exc)
    start, end = day_interval(day, BILL_ZONE)
    return Span(
        max(start, request.window.start), min(end, request.window.end), redact(reason, [request.secret])
    )


HINTS = {  # what an Alibaba error code means for the person who set the keys (the codes carry no secret)
    "SignatureDoesNotMatch": "the secret does not belong to the AccessKey id",
    "InvalidAccessKeyId.NotFound": "no such AccessKey id",
    "Forbidden.RAM": "the RAM user may not call DescribeInstanceBill (bssapi:DescribeInstanceBill)",
    "Throttling.User": "rate limited: the next report asks again",
}


def _refusal(exc: urllib.error.HTTPError) -> str:
    """The error code of Alibaba's answer body, and what it means; the body is read, never a header."""
    try:
        code = _code_of(json.loads(exc.read().decode("utf-8", "replace")))
    except (ValueError, OSError):
        code = "no readable error body"
    return f"{code} — {HINTS.get(code, 'read again next time')}"


def _code_of(answer: Any) -> str:
    return str(answer.get("Code") or "no code") if isinstance(answer, Mapping) else "not an object"


def _why_unread(request: AlibabaRequest) -> str:
    if request.offline:
        return "offline (AI_COST_OFFLINE): BSS is not asked"
    problem = key_problem(KEY_ID_ENV, request.key_id) or key_problem(SECRET_ENV, request.secret)
    return f"{problem}: a RAM user with bssapi:DescribeInstanceBill is needed" if problem else ""
