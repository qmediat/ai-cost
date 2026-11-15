"""Timestamps: one parser for every form the sources use, one formatter, one clock."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .errors import UsageError

_EPOCH = re.compile(r"\d{9,}(\.\d+)?")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def now() -> datetime:
    """The current time, timezone-aware UTC."""
    return datetime.now(timezone.utc)


def _from_text(text: str) -> datetime:
    if text == "now":
        return now()
    if _EPOCH.fullmatch(text):
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    text = text.replace("Z", "+00:00")
    if _DATE_ONLY.fullmatch(text):
        text += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(text)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_ts(value: str | int | float | None) -> datetime | None:
    """ISO-8601 (``Z`` or offset), a date, epoch seconds or ``now`` → aware UTC datetime; ``None`` stays ``None``."""
    if value is None or isinstance(value, bool):  # true is an int in Python, never a time
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return _from_text(str(value).strip())
    except (ValueError, OverflowError, OSError):  # a malformed stamp in a source file is its record's problem
        return None


def parse_cli_ts(value: str | None, flag: str) -> datetime | None:
    """``parse_ts`` for a command-line value: something that does not parse is a usage error naming the flag."""
    if value is None:
        return None
    parsed = parse_ts(value)
    if parsed is None:
        raise UsageError(f"{flag}: cannot read {value!r} (ISO-8601, YYYY-MM-DD, epoch seconds or 'now')")
    return parsed


def in_utc(at: datetime) -> datetime:
    """The time in UTC: every price day and tariff hour is decided on it; a naive time is taken as UTC already."""
    return at.astimezone(timezone.utc) if at.tzinfo is not None else at


def zoned(text: str | None, flag: str) -> None:
    """On an instance every time given says its zone, as the library's must: a ``UsageError`` naming the flag.

    The one rule for both doors: the command shows it as exit 2, the library as ``ApiError`` (``api._said``). A blank
    time is refused too, never read as absent.
    """
    if text is not None and not text.strip():
        raise UsageError(f"{flag}: give a time (an empty value is never the default window)")
    if text is not None and not has_zone(text):
        raise UsageError(
            f"{flag} {text!r}: give the zone (Z or +HH:MM) — an instance takes no time without one"
        )


def has_zone(text: str) -> bool:
    """Whether a time as written says its zone: ``now``, epoch seconds, a ``Z`` or an offset (a date alone does not).

    Text that is no time at all answers True: the parser that follows says what is wrong with it.
    """
    text = text.strip()
    if text == "now" or _EPOCH.fullmatch(text):
        return True
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed.tzinfo is not None


def iso(value: datetime | None) -> str:
    """``2026-09-20T00:10:31Z`` or an empty string."""
    if value is None:
        return ""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes(count: float) -> timedelta:
    """A duration in minutes (readability helper for window padding)."""
    return timedelta(minutes=count)
