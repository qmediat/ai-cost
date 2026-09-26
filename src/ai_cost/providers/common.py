"""What the live provider readers share (ADR-0008, decision 11).

A day's interval in the provider's zone, whether a window holds a whole day, the answer when a source is not read, an
amount as the provider's exact text.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any

from ..models import Provider, ProviderDays, Span, Window


def day_interval(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """The day from midnight to midnight in the provider's zone."""
    start = datetime.combine(day, time.min, zone)
    return start, start + timedelta(days=1)


def days_touched(window: Window, zone: tzinfo) -> tuple[date, date]:
    """The first and last date, in the provider's zone, that the window touches."""
    first = window.start.astimezone(zone).date()
    last = (window.end - timedelta(microseconds=1)).astimezone(zone).date()
    return first, max(first, last)


def holds_a_whole_day(window: Window, zone: tzinfo) -> bool:
    """Whether some provider day lies wholly inside the window: without one there is nothing to compare or ask for."""
    first, last = days_touched(window, zone)
    for day in (first, first + timedelta(days=1)):
        start, end = day_interval(day, zone)
        if window.start <= start and end <= window.end and day <= last:
            return True
    return False


def not_read(provider: Provider, source: str, window: Window, reason: str, offline: bool) -> ProviderDays:
    """A source not read: offline is a missing span (said as information), anything else an unreadable one."""
    span = Span(window.start, window.end, reason)
    if offline:
        return ProviderDays(provider, source, missing=(span,), offline=True)
    return ProviderDays(provider, source, unreadable=(span,))


SECRET_MIN = 8  # every provider key is longer; a shorter value is not masked


def redact(text: str, secrets: Iterable[str]) -> str:
    """``text`` with every secret replaced: an exception of a transport can quote a header value it refused.

    A value shorter than ``SECRET_MIN`` is no key a provider issues; masking it would only mangle the message.
    """
    for secret in secrets:
        if len(secret) >= SECRET_MIN:
            text = text.replace(secret, "***")
    return text


def key_problem(name: str, value: str) -> str:
    """Why a key from the environment cannot be sent ("" when it can): empty, or a space or line break inside."""
    if not value:
        return f"{name} is not set"
    if any(c.isspace() or ord(c) < 32 for c in value):
        return f"{name} holds a space or a line break: set it without one"
    return ""


def exact_amount(value: Any, what: str, error: type[ValueError]) -> Decimal:
    """An amount as the provider wrote it (a decimal number or its text); anything else raises ``error``."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        raise error(f"{what} is missing or not a number: {value!r}")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise error(f"{what} is not a number: {value!r}") from None
    if not amount.is_finite():
        raise error(f"{what} is not finite: {value!r}")
    return amount
