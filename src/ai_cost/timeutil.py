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
    if value is None:
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


def iso(value: datetime | None) -> str:
    """``2026-09-20T00:10:31Z`` or an empty string."""
    if value is None:
        return ""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes(count: float) -> timedelta:
    """A duration in minutes (readability helper for window padding)."""
    return timedelta(minutes=count)
