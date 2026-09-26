"""Google's own day report (ADR-0008, decision 11): the Cloud Billing export in BigQuery, read through ``bq``.

The export holds every Cloud service of the billing account, hourly, in the account's currency, with Google's own
``currency_conversion_rate`` on each line ("``cost`` ÷ ``currency_conversion_rate`` is the cost in US dollars"). The
reader sums each UTC day exactly (``NUMERIC`` in BigQuery: the export's amounts have six decimals) and converts by the
line's own rate — never another. Only ``regular`` lines of the configured services are usage; taxes, rounding,
adjustments and other services are listed as excluded. Credits (free tier, promotions) make the difference between
gross (``cost``) and net (``cost + credits``). The export starts where Google started it: a day before its first line
is not covered (missing), never a zero day.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..models import Provider, ProviderDay, ProviderDays, ProviderLine, Span, Window
from .common import day_interval, days_touched, exact_amount, holds_a_whole_day, not_read

SOURCE = "bigquery-export"
DEFAULT_SERVICES = ("Gemini API",)
SETTLE = timedelta(hours=72)  # late usage can still arrive: a day is closed three days after it ended
TIMEOUT_S = 120
MAX_ROWS = 1_000_000  # an answer this long may be cut by bq: refused, never summed
# a query that would scan more fails loudly, naming the cap, instead of billing. Both parts of the query read every
# row (usage_start_time is no partition column): 3 630 bytes for the 36 rows of a live export on 2026-09-26, about
# 101 bytes a row, so the cap is reached near ten million rows
MAX_BYTES = 10**9
TABLE = re.compile(r"[a-z][a-z0-9-]{4,61}[a-z0-9]\.[A-Za-z0-9_]{1,1024}\.[A-Za-z0-9_]{1,1024}")
USAGE_TYPE = "regular"
QUERY = """
WITH usage AS (
  SELECT FORMAT_DATE('%F', DATE(usage_start_time)) AS day,
         service.description AS service,
         sku.description AS sku,
         cost_type,
         currency,
         CAST(CAST(currency_conversion_rate AS NUMERIC) AS STRING) AS rate,
         CAST(SUM(CAST(cost AS NUMERIC)) AS STRING) AS cost,
         CAST(SUM(IFNULL((SELECT SUM(CAST(c.amount AS NUMERIC)) FROM UNNEST(credits) AS c), 0)) AS STRING) AS credits
  FROM `{table}`
  WHERE usage_start_time >= TIMESTAMP(@first) AND usage_start_time < TIMESTAMP(@after)
  GROUP BY day, service, sku, cost_type, currency, rate
), coverage AS (
  SELECT FORMAT_DATE('%F', DATE(MIN(usage_start_time))) AS first_day,
         FORMAT_DATE('%F', DATE(MAX(usage_start_time))) AS last_day
  FROM `{table}`
)
SELECT usage.*, coverage.first_day, coverage.last_day FROM coverage LEFT JOIN usage ON TRUE
"""

Run = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class GoogleRequest:
    """One read: the export table (``project.dataset.table``), the services that are usage, the window, when."""

    table: str
    services: tuple[str, ...]
    window: Window
    read_at: datetime
    offline: bool = False


class ExportShapeError(ValueError):
    """``bq``'s answer is not the shape the query asks for: nothing of it is used."""


def bq_command(request: GoogleRequest, first: date, last: date) -> list[str]:
    """The ``bq query`` for the UTC days ``first`` through ``last`` (the table is checked by ``TABLE`` first)."""
    project = request.table.split(".", 1)[0]
    return [
        "bq",
        f"--project_id={project}",
        "--format=json",
        "query",
        "--use_legacy_sql=false",
        f"--max_rows={MAX_ROWS}",
        f"--maximum_bytes_billed={MAX_BYTES}",
        f"--parameter=first:STRING:{first.isoformat()}",
        f"--parameter=after:STRING:{(last + timedelta(days=1)).isoformat()}",
        QUERY.format(table=request.table),
    ]


def run_bq(command: Sequence[str]) -> str:
    """``bq``'s standard output; a failure is a ``RuntimeError`` with the end of what it said."""
    if shutil.which(command[0]) is None:
        raise RuntimeError("bq (Google Cloud CLI) is not installed or not on PATH")
    done = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        said = [line.strip() for line in (done.stderr or done.stdout).splitlines() if line.strip()]
        raise RuntimeError(" ".join(said[-3:]) if said else f"exit {done.returncode}")
    return done.stdout


def parse_rows(text: str) -> list[Mapping[str, Any]]:
    """The JSON rows of ``bq``'s answer, which starts at the first line that opens the array.

    gcloud may print a notice before them on standard output (an old Python), so nothing before that line is read.
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), None)
    if start is None:
        raise ExportShapeError(f"no JSON rows in the answer: {text.strip()[:120]!r}")
    try:
        rows = json.loads("\n".join(lines[start:]))
    except ValueError as exc:
        raise ExportShapeError(f"not JSON ({exc})") from exc
    if not isinstance(rows, list) or not all(isinstance(r, Mapping) for r in rows):
        raise ExportShapeError("not a list of rows")
    if len(rows) >= MAX_ROWS:
        raise ExportShapeError(
            f"the answer reached bq's row cap ({MAX_ROWS}): it may be cut, so nothing is used"
        )
    return rows


def usd_line(row: Mapping[str, Any], services: Sequence[str]) -> tuple[date, ProviderLine]:
    """One grouped row as a line in USD, by the row's own rate; not usage (tax, another service) is ``excluded``."""
    try:
        day = date.fromisoformat(str(row["day"]))
    except (KeyError, ValueError):
        raise ExportShapeError(f"a row without its day: {dict(row)}") from None
    currency = str(row.get("currency") or "")
    rate = Decimal(1) if currency == "USD" else exact_amount(row.get("rate"), "rate", ExportShapeError)
    if rate <= 0:
        raise ExportShapeError(f"a row whose rate is not positive: {dict(row)}")
    cost = exact_amount(row.get("cost"), "cost", ExportShapeError)
    credits = exact_amount(row.get("credits"), "credits", ExportShapeError)
    usage = row.get("service") in services and row.get("cost_type") == USAGE_TYPE
    label = f"{row.get('service')} / {row.get('sku')} ({row.get('cost_type')})"
    converted = "" if currency == "USD" else f"{currency} ÷ {rate}"
    return day, ProviderLine(
        label, cost / rate, (cost + credits) / rate, excluded=not usage, rate_note=converted
    )


def exported_span(rows: Sequence[Mapping[str, Any]]) -> tuple[date, date] | None:
    """The export's first and last day (every row carries them); ``None`` for an empty table."""
    stated = {(str(row.get("first_day")), str(row.get("last_day"))) for row in rows if row.get("first_day")}
    if len(stated) > 1:
        raise ExportShapeError(f"the export states several spans {sorted(stated)}")
    if not stated:
        return None
    first, last = stated.pop()
    try:
        return date.fromisoformat(first), date.fromisoformat(last)
    except ValueError:
        raise ExportShapeError(f"the export's span {first}..{last} is not two dates") from None


def provider_days(
    by_day: Mapping[date, Sequence[ProviderLine]], first: date, last: date, read_at: datetime
) -> tuple[ProviderDay, ...]:
    """Every UTC day of the query: a day without a row is a zero day (the export covered it); closed after ``SETTLE``."""
    days = []
    for index in range((last - first).days + 1):
        day = first + timedelta(days=index)
        start, end = day_interval(day, timezone.utc)
        lines = tuple(sorted(by_day.get(day, ()), key=lambda line: line.label))
        days.append(ProviderDay(Provider.GOOGLE, start, end, lines, end + SETTLE <= read_at, read_at, SOURCE))
    return tuple(days)


def covered_days(
    request: GoogleRequest, rows: Sequence[Mapping[str, Any]], first: date, last: date
) -> ProviderDays:
    """The days the export covers — from its first day to its newest — and the rest of the window as missing spans.

    A day between two exported days without a line of its own is a zero day; a day before the export began, or after
    the newest day it has reached (a lag, a stopped export), is missing, never zero.
    """
    by_day: dict[date, list[ProviderLine]] = defaultdict(list)
    for row in rows:
        if row.get("day") is not None:
            day, line = usd_line(row, request.services)
            by_day[day].append(line)
    span = exported_span(rows)
    if span is None:
        hole = Span(request.window.start, request.window.end, "the billing export holds no line yet")
        return ProviderDays(Provider.GOOGLE, SOURCE, missing=(hole,), captured=request.read_at)
    begin, end = max(first, span[0]), min(last, span[1])
    days = provider_days(by_day, begin, end, request.read_at) if begin <= end else ()
    return ProviderDays(Provider.GOOGLE, SOURCE, days, _outside(request.window, begin, end), request.read_at)


def _outside(window: Window, begin: date, end: date) -> tuple[Span, ...]:
    """The parts of the window before the export's first day and after its newest one."""
    covered_from = day_interval(begin, timezone.utc)[0]
    covered_to = day_interval(end, timezone.utc)[1]
    spans = []
    if window.start < covered_from:
        spans.append(
            Span(window.start, min(covered_from, window.end), "before the billing export's first line")
        )
    if covered_to < window.end:
        spans.append(
            Span(max(covered_to, window.start), window.end, "after the billing export's newest line")
        )
    return tuple(spans)


def read_days(request: GoogleRequest, run: Run = run_bq) -> ProviderDays:
    """The account's days touching the window, or the whole window as a span not read, with the reason."""
    if not holds_a_whole_day(request.window, timezone.utc):
        return ProviderDays(Provider.GOOGLE, SOURCE)  # nothing to compare: BigQuery is not asked
    reason = _why_unread(request)
    if reason:
        return not_read(Provider.GOOGLE, SOURCE, request.window, reason, request.offline)
    first, last = days_touched(request.window, timezone.utc)
    try:
        return covered_days(request, parse_rows(run(bq_command(request, first, last))), first, last)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        reason = f"bq could not read {request.table}: {exc}"
    except ExportShapeError as exc:
        reason = f"the billing export answer is not the shape asked for: {exc}"
    return not_read(Provider.GOOGLE, SOURCE, request.window, reason, offline=False)


def _why_unread(request: GoogleRequest) -> str:
    if request.offline:
        return "offline (AI_COST_OFFLINE): BigQuery is not asked"
    if not TABLE.fullmatch(request.table):
        return f"{request.table!r} is not project.dataset.table"
    return ""
