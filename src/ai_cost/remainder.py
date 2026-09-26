"""What a provider's day reports bill beyond the local records (ADR-0008). Pure: no I/O.

For every provider day the report can hold whole, the day's local rows are priced the way the API-only and the real
group price them; the provider's gross minus the local API amount and its net minus the local cash are the day's two
differences. A positive difference is one ``UNTRACKED`` row; a negative one is a warning; a day that cannot be
compared is listed with its reason, never apportioned.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal

from .config import Config, PriceBook
from .errors import PricingError
from .groups import is_seat, row_cash
from .models import (
    Billing,
    DayComparison,
    Provider,
    ProviderDay,
    ProviderDays,
    ProviderSummary,
    RowKind,
    Span,
    Tokens,
    Untracked,
    UsageRow,
    Window,
)
from .pricing import price

UNTRACKED_MODEL = "untracked"
FLOAT_BOUND = (
    2.0**-50
)  # relative error of a float sum of priced rows, each a handful of operations (ADR-0008)
NOT_CLOSED = "not closed yet: the provider may still add to it"  # the one reason daily reads a day again
NEGATIVE_CAUSES = "a price above the provider's, a call counted twice, or a row of another account"


@dataclass(frozen=True)
class Comparison:
    """What the comparison adds to a report: the difference rows, a summary per provider, the header warnings."""

    rows: tuple[UsageRow, ...] = ()
    summaries: tuple[ProviderSummary, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Local:
    """A day's local amounts as floats, and what the section says about its rows."""

    api: tuple[float, ...]
    real: tuple[float, ...]
    unknown: int
    requests: int
    uncounted: int


class _Timeline:
    """One provider's rows by time, so a day's rows are one bisection away (rows without a time are never on it)."""

    def __init__(self, rows: Sequence[UsageRow], provider: Provider) -> None:
        timed = sorted(
            (
                (r.at, index, r)
                for index, r in enumerate(rows)
                if r.at is not None and _on_timeline(r, provider)
            ),
            key=lambda entry: (entry[0], entry[1]),
        )
        self.times = [at for at, _, _ in timed]
        self.rows = [row for _, _, row in timed]

    def between(self, day: ProviderDay) -> list[UsageRow]:
        """The rows with ``start <= at < end``."""
        low = bisect.bisect_left(self.times, day.start)
        high = bisect.bisect_left(self.times, day.end)
        return self.rows[low:high]


def _on_timeline(row: UsageRow, provider: Provider) -> bool:
    return row.provider == provider and row.kind is not RowKind.UNTRACKED


def compare(
    reports: Sequence[ProviderDays],
    rows: Sequence[UsageRow],
    window: Window,
    book: PriceBook,
    config: Config,
) -> Comparison:
    """Every provider's days against the local rows (the caller reads a report only for an account view)."""
    added: list[UsageRow] = []
    summaries: list[ProviderSummary] = []
    warnings: list[str] = []
    for report in reports:
        timeline = _Timeline(rows, report.provider)
        ordered = sorted(report.days, key=lambda d: d.start)
        days = tuple(_compare_day(day, timeline, window, book, config) for day in ordered)
        summary = ProviderSummary(
            report.provider,
            report.source,
            days,
            report.missing,
            report.captured,
            report.unreadable,
            report.offline,
            report.utc_days,
        )
        summaries.append(summary)
        added += [row for row in map(untracked_row, summary.compared) if row is not None]
        warnings += summary_warnings(summary)
    return Comparison(tuple(added), tuple(summaries), tuple(warnings))


def _compare_day(
    day: ProviderDay, timeline: _Timeline, window: Window, book: PriceBook, config: Config
) -> DayComparison:
    why = why_not_compared(day, window)
    if why:
        return DayComparison(day, why)
    rows = timeline.between(day)
    if any(r.kind is RowKind.LEDGER or r.billing is Billing.API_SETTLED for r in rows):
        return DayComparison(day, "its local cash sits on other rows (a ledger or a settled row)")
    try:
        local = _local(rows, book, config)
    except PricingError as exc:
        return DayComparison(day, f"a local row has no price ({exc})")
    return DayComparison(
        day,
        local_api=exact(local.api),
        local_real=exact(local.real),
        api_diff=difference(day.gross, local.api),
        real_diff=difference(day.net, local.real),
        unknown_rows=local.unknown,
        local_requests=local.requests,
        uncounted_rows=local.uncounted,
    )


def why_not_compared(day: ProviderDay, window: Window) -> str:
    """Why a day cannot be compared in this window ("" when it can): cut by the window, still open, another currency."""
    if not (window.start <= day.start and day.end <= window.end):
        return "the window holds only part of the day"
    if not day.closed:
        return NOT_CLOSED
    if day.currency != "USD":
        return f"in {day.currency}, without the provider's own rate"
    return ""


def _local(rows: Sequence[UsageRow], book: PriceBook, config: Config) -> _Local:
    """The day's API amounts (every row a pay-per-use account can have billed) and cash (the real group's rule)."""
    billable = [r for r in rows if r.billing is not Billing.SUBSCRIPTION and not is_seat(r)]
    api = tuple(price(r, book, config).usd for r in billable)
    cash = [row_cash(r, book, config) for r in rows]
    counted = Counter(r.tokens.requests > 0 for r in rows)
    return _Local(
        api=api,
        real=tuple(money.usd for _, money in filter(None, cash)),
        unknown=sum(r.billing is Billing.UNKNOWN for r in rows),
        requests=sum(r.tokens.requests for r in rows),
        uncounted=counted[False],
    )


def exact(amounts: Sequence[float]) -> Decimal:
    """The float sum of local amounts as a Decimal, shown no finer than its own error bound."""
    return Decimal(math.fsum(amounts)).quantize(_step(amounts))


def difference(provider: Decimal, amounts: Sequence[float]) -> Decimal:
    """``provider − Σ amounts``; zero within the float error bound of the sum, shown no finer than that bound."""
    diff = provider - Decimal(math.fsum(amounts))
    if abs(diff) <= Decimal(_bound(amounts)):
        return Decimal(0)
    exponent = provider.as_tuple().exponent  # a zero states no precision: the local sum's own bound decides
    stated = exponent if isinstance(exponent, int) and provider != 0 else None
    return diff.quantize(_step(amounts, stated))


def _bound(amounts: Sequence[float]) -> float:
    """The float error of a sum of priced rows: every row's magnitude × the relative error of a few operations."""
    return math.fsum(abs(a) for a in amounts) * FLOAT_BOUND


def _step(amounts: Sequence[float], provider_exponent: int | None = None) -> Decimal:
    """The finest digit worth showing: the provider's own precision, never finer than the local sum's error."""
    finest = provider_exponent if provider_exponent is not None else -18
    bound = _bound(amounts)
    if bound > 0:
        finest = max(finest, math.ceil(math.log10(bound)))
    return Decimal(1).scaleb(min(finest, -2))


def interval(day: ProviderDay) -> str:
    """How a provider day reads in a ref or a warning: its own interval, in its own offset."""
    return f"{day.start.isoformat(timespec='minutes')}/{day.end.isoformat(timespec='minutes')}"


def untracked_row(compared: DayComparison) -> UsageRow | None:
    """The day's ``UNTRACKED`` row when either difference is positive; each group books its own (a negative is 0)."""
    api, real = compared.api_diff or Decimal(0), compared.real_diff or Decimal(0)
    if api <= 0 and real <= 0:
        return None
    day = compared.day
    return UsageRow(
        provider=day.provider,
        model=UNTRACKED_MODEL,
        kind=RowKind.UNTRACKED,
        at=day.start.astimezone(timezone.utc),
        ref=f"{day.source}:{interval(day)}",
        billing=Billing.API,
        tokens=Tokens(),
        source=day.source,
        untracked=Untracked(
            interval(day), api=float(max(api, Decimal(0))), real=float(max(real, Decimal(0)))
        ),
    )


def summary_warnings(summary: ProviderSummary) -> list[str]:
    """The header lines: every negative day, the days not compared, the spans without data."""
    name = summary.provider.value
    warnings = [
        f"provider report ({name}): the local records exceed the report on {interval(c.day)} by "
        f"{_negatives(c)} — {NEGATIVE_CAUSES}"
        for c in summary.compared
        if (c.api_diff or 0) < 0 or (c.real_diff or 0) < 0
    ]
    reasons = Counter(c.why_not for c in summary.not_compared)
    if reasons:
        listed = "; ".join(f"{why} ×{n}" for why, n in sorted(reasons.items()))
        warnings.append(f"provider report ({name}): {sum(reasons.values())} day(s) not compared — {listed}")
    warnings += [f"provider report ({name}): no data for {span_text(s)}" for s in summary.missing]
    warnings += [f"provider report ({name}): could not read {span_text(s)}" for s in summary.unreadable]
    return warnings


def span_text(span: Span) -> str:
    """How a span reads: its interval and why."""
    return (
        f"{span.start.isoformat(timespec='minutes')}..{span.end.isoformat(timespec='minutes')} ({span.why})"
    )


def _negatives(compared: DayComparison) -> str:
    parts = [
        f"{-diff} USD ({group})"
        for group, diff in (("real", compared.real_diff), ("API", compared.api_diff))
        if diff is not None and diff < 0
    ]
    return " and ".join(parts)
