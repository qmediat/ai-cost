"""The GitHub account's usage report (ADR-0007): Copilot amounts per UTC day, exactly as the report states them.

``collect_bill`` reads ``GET /<scope>s/<name>/settings/billing/usage?year=Y&month=M`` through ``gh`` — once per
calendar month the window touches and per process — and turns the counted lines of the UTC days that lie whole inside
the window and have ended into ``RowKind.INVOICE`` rows. Counted: every Copilot line, and the Actions lines of the
repositories named with ``--github``; everything else is left out with its exact amounts. The days the window only
touches are stated with their exact amounts, never prorated. Nothing is kept on disk: a later correction in the
report is what the next run reads.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..models import (
    BillAccount,
    Billing,
    BillSubtotal,
    BillSummary,
    InvoiceAmounts,
    OutsideDay,
    Provider,
    RowKind,
    Scope,
    Skipped,
    Tokens,
    UsageRow,
    Window,
)
from .github import run_gh

SOURCE_NAME = "github-bill"
KNOWN_UNITS = frozenset({"AICredits", "UserMonths", "Minutes", "GigabyteHours"})
SETTLE_HOURS = 12  # "GitHub updates your artifact storage usage within 6 to 12 hours" (docs, Actions billing)
_AMOUNTS = ("grossAmount", "discountAmount", "netAmount")


class BillError(Exception):
    """A month of the report that could not be read; the message says why."""


# One answer — or one failure — per account and month per process: ``daily`` builds many reports from it.
_FETCHED: dict[tuple[str, int, int], str | BillError] = {}


@dataclass(frozen=True)
class BillLine:
    """One ``usageItems`` entry, typed; the amounts stay ``Decimal`` so every sum the tool prints is exact."""

    day: date
    product: str
    sku: str
    unit: str
    quantity: Decimal | None
    unit_price: Decimal | None
    gross: Decimal
    discount: Decimal
    net: Decimal
    organization: str
    repository: str


@dataclass(frozen=True)
class BillRequest:
    """Whose report, when it is read, whether the network is off, and which repositories' Actions count."""

    account: BillAccount
    read_at: datetime
    offline: bool = False
    actions_repos: frozenset[str] = frozenset()  # ``owner/name`` in lower case, as named with --github

    @property
    def today(self) -> date:
        """The UTC day of the reading."""
        return self.read_at.astimezone(timezone.utc).date()


@dataclass
class BillResult:
    """The rows of the whole days, what could not be used, and the summary the header lines are said from."""

    rows: list[UsageRow] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    summary: BillSummary | None = None


# ---- parsing ------------------------------------------------------------------------------------------------------


def _decimal(item: Mapping[str, Any], key: str) -> Decimal | None:
    """A finite number (``None`` when absent); a boolean, a string or NaN is a ``ValueError``."""
    value = item.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{key} is not a number")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError(f"{key} is not finite")
    return number


def _amount(item: Mapping[str, Any], key: str) -> Decimal:
    number = _decimal(item, key)
    if number is None:
        raise ValueError(f"{key} is missing")
    return number


def _text(item: Mapping[str, Any], key: str, required: bool = True) -> str:
    value = item.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value):
        raise ValueError(f"{key} is not text")
    return value


def parse_line(item: Any) -> BillLine:
    """One ``usageItems`` entry; the day, product, SKU, unit and the three amounts are required, the rest is not.

    A required field that is missing or malformed is a ``ValueError`` naming it.
    """
    if not isinstance(item, Mapping):
        raise ValueError("not an object")
    stamp = _text(item, "date")
    try:
        day = date.fromisoformat(stamp[:10])
    except ValueError:
        raise ValueError(f"date {stamp!r} is not a date") from None
    gross, discount, net = (_amount(item, key) for key in _AMOUNTS)
    return BillLine(
        day=day,
        product=_text(item, "product"),
        sku=_text(item, "sku"),
        unit=_text(item, "unitType"),
        quantity=_decimal(item, "quantity"),
        unit_price=_decimal(item, "pricePerUnit"),
        gross=gross,
        discount=discount,
        net=net,
        organization=_text(item, "organizationName", required=False),
        repository=_text(item, "repositoryName", required=False),
    )


def parse_items(body: str) -> list[Any]:
    """The ``usageItems`` of an answer, numbers as ``Decimal``; anything else is a ``BillError``."""
    try:
        parsed = json.loads(body, parse_float=Decimal)
    except ValueError as exc:
        raise BillError(f"the answer is not JSON ({exc})") from exc
    items = parsed.get("usageItems") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        raise BillError("the answer has no usageItems list")
    return items


# ---- fetching -----------------------------------------------------------------------------------------------------


def _reason(failure: str, account: BillAccount) -> str:
    if "404" in failure:
        return (
            f"HTTP 404: this gh login cannot read the usage report of {account.label} (an owner or billing "
            "manager can), or the account does not exist"
        )
    return failure


def fetch_month(request: BillRequest, year: int, month: int) -> str:
    """The month's answer, fetched once per process — a failure too, so an outage costs one call, not one per report.

    Offline, a failed call or a malformed answer is a ``BillError``.
    """
    if request.offline:
        raise BillError("offline (AI_COST_OFFLINE): the usage report is not read")
    key = (request.account.endpoint, year, month)
    if key not in _FETCHED:
        _FETCHED[key] = _call(request.account, year, month)
    answer = _FETCHED[key]
    if isinstance(answer, BillError):
        raise answer
    return answer


def _call(account: BillAccount, year: int, month: int) -> str | BillError:
    call = run_gh(["api", f"{account.endpoint}?year={year}&month={month}"])
    if call.failure:
        return BillError(_reason(call.failure, account))
    try:
        parse_items(call.stdout)  # a malformed answer is refused before anything reuses it
    except BillError as exc:
        return exc
    return call.stdout


# ---- days of the window -------------------------------------------------------------------------------------------


def midnight(day: date) -> datetime:
    """The start of a UTC day."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def touched_days(window: Window, today: date) -> list[date]:
    """The UTC days that overlap the window and have begun, oldest first."""
    if window.end <= window.start:
        return []
    first = window.start.astimezone(timezone.utc).date()
    last = min((window.end - timedelta(microseconds=1)).astimezone(timezone.utc).date(), today)
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def is_whole(day: date, window: Window, today: date) -> bool:
    """Whether the day lies whole inside the window and has ended: only those days enter the totals."""
    start = midnight(day)
    return day < today and window.start <= start and start + timedelta(days=1) <= window.end


def is_provisional(day: date, read_at: datetime) -> bool:
    """A day read less than ``SETTLE_HOURS`` after it ended: the report may still add to it."""
    return midnight(day) + timedelta(days=1, hours=SETTLE_HOURS) > read_at


def _why_outside(day: date, window: Window, today: date) -> str:
    if day >= today:
        return "the day is not over"
    start = midnight(day)
    inside = min(window.end, start + timedelta(days=1)) - max(window.start, start)
    return f"the window holds {inside.total_seconds() / 3600:.2f} h of it"


# ---- lines → rows and sums ----------------------------------------------------------------------------------------


def repository_of(line: BillLine, account: BillAccount) -> str:
    """``owner/name`` of the line's repository ("" for none): a qualified name as given, else under its organization."""
    if not line.repository:
        return ""
    if "/" in line.repository:
        return line.repository
    return f"{line.organization or account.name}/{line.repository}"


def is_counted(line: BillLine, request: BillRequest) -> bool:
    """Copilot always; Actions only for a repository named with ``--github`` (a CI run is not AI work by itself)."""
    if line.product == "copilot":
        return True
    return line.product == "actions" and repository_of(line, request.account).lower() in request.actions_repos


def settled_by(row: UsageRow, bill: BillSummary, account: BillAccount) -> bool:
    """Whether the read usage report already holds this GitHub row's amount, so the row is only a count.

    Only a month whose report was read settles anything — a ``--github`` Actions row is one UTC day, so its own
    month decides; when that month could not be read the row keeps its own rules (a published per-minute price, a
    charge a program logged): a failed read never turns an amount into zero, and the unread month has no report line
    to count it twice. A ``--github`` repository of another owner is not in this account's report.
    """
    if row.provider != Provider.GITHUB or row.invoice is not None or row.at is None:
        return False
    if f"{row.at.astimezone(timezone.utc):%Y-%m}" not in bill.months_read:
        return False
    owner, named, _ = row.ref.partition("/")
    return owner.lower() == account.name.lower() if row.kind is RowKind.ACTIONS and named else True


def invoice_row(line: BillLine, account: BillAccount) -> UsageRow:
    """A usage line is placed on its repository (or its organization); a seat line names nothing, like a plan."""
    amounts = InvoiceAmounts(
        day=line.day,
        unit=line.unit,
        quantity=float(line.quantity or 0),
        unit_price=float(line.unit_price or 0),
        gross=float(line.gross),
        discount=float(line.discount),
        net=float(line.net),
    )
    place = repository_of(line, account) or line.organization or account.name
    return UsageRow(
        provider=Provider.GITHUB,
        model=line.sku,
        kind=RowKind.INVOICE,
        at=midnight(line.day),
        ref=f"{place}@{line.day.isoformat()}",
        billing=Billing.SUBSCRIPTION if amounts.is_seat else Billing.API,
        tokens=Tokens(),
        scope=Scope() if amounts.is_seat else Scope(workspace=place),
        source=SOURCE_NAME,
        invoice=amounts,
    )


def _total(lines: Sequence[BillLine], pick: str) -> Decimal:
    return sum((getattr(line, pick) for line in lines), Decimal(0))


def _subtotal(key: tuple[str, str, str], lines: Sequence[BillLine]) -> BillSubtotal:
    known = [line.quantity for line in lines if line.quantity is not None]
    quantity = sum(known, Decimal(0)) if len(known) == len(lines) else None
    gross, discount, net = (_total(lines, pick) for pick in ("gross", "discount", "net"))
    return BillSubtotal(*key, len(lines), quantity, gross, discount, net)


def subtotals(lines: Iterable[BillLine]) -> tuple[BillSubtotal, ...]:
    """One exact sum per product / SKU / unit; the quantity is unknown when any line left it out."""
    groups: dict[tuple[str, str, str], list[BillLine]] = {}
    for line in lines:
        groups.setdefault((line.product, line.sku, line.unit), []).append(line)
    return tuple(_subtotal(key, of_key) for key, of_key in sorted(groups.items()))


def outside_days(
    lines: Sequence[BillLine], days: Iterable[date], window: Window, today: date
) -> tuple[OutsideDay, ...]:
    """Each touched day outside the totals that has counted lines, with its exact amounts."""
    found = []
    for day in days:
        of_day = [line for line in lines if line.day == day]
        if of_day:
            gross, discount, net = (_total(of_day, pick) for pick in ("gross", "discount", "net"))
            found.append(OutsideDay(day, _why_outside(day, window, today), len(of_day), gross, discount, net))
    return tuple(found)


# ---- the collector ------------------------------------------------------------------------------------------------


@dataclass
class _Read:
    """What the months of the window gave: their lines, the months that failed, the lines that could not be read."""

    lines: list[BillLine] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unread: set[tuple[int, int]] = field(default_factory=set)  # (year, month) of the months not read
    unreadable: int = 0


def _day_of(item: Any) -> date | None:
    """The day a raw line names, when it names one; ``None`` when that much cannot be read."""
    try:
        return date.fromisoformat(str(item.get("date"))[:10]) if isinstance(item, Mapping) else None
    except ValueError:
        return None


def _parse_month(body: str, wanted: set[date], label: str, read: _Read, result: BillResult) -> None:
    """The lines of the wanted days; one that cannot be read is a skipped entry and counted, never dropped silently.

    A line of another day is not the window's: it is passed over whether it reads or not.
    """
    for index, item in enumerate(parse_items(body)):
        day = _day_of(item)
        if day is not None and day not in wanted:
            continue
        try:
            line = parse_line(item)
        except ValueError as exc:
            result.skipped.append(Skipped(SOURCE_NAME, f"{label} usageItems[{index}]", str(exc)))
            read.unreadable += 1
            continue
        if line.day in wanted:
            read.lines.append(line)


def read_lines(request: BillRequest, days: Sequence[date], result: BillResult) -> _Read:
    """The report's lines of the given days, month by month; a month that cannot be read is named with the reason."""
    read = _Read()
    for year, month in sorted({(day.year, day.month) for day in days}):
        label = f"{year:04d}-{month:02d}"
        try:
            _parse_month(fetch_month(request, year, month), set(days), label, read, result)
        except BillError as exc:
            result.skipped.append(Skipped(SOURCE_NAME, f"{request.account.label} {label}", str(exc)))
            read.missing.append(f"{label}: {exc}")
            read.unread.add((year, month))
    return read


def bill_warnings(summary: BillSummary, with_section: bool = True) -> list[str]:
    """The report header's lines about the usage report.

    Without its section (a per-project report) the days outside the totals are listed nowhere, so nothing points there.
    """
    where = f"GitHub usage report ({summary.account})"
    said = [f"{where}: {month} — no GitHub amount for it, nothing estimated" for month in summary.missing]
    if summary.outside and with_section:
        said.append(
            f"{where}: {len(summary.outside)} day(s) the window only touches are not in the totals (their amounts "
            "are in the GitHub usage report section)"
        )
    if summary.provisional:
        days = ", ".join(day.isoformat() for day in summary.provisional)
        said.append(
            f"{where}: {days} ended less than {SETTLE_HOURS} h before this reading — GitHub may still add to it"
        )
    if summary.unreadable_lines:
        said.append(
            f"{where}: {summary.unreadable_lines} line(s) could not be read — the GitHub amounts are short by them"
        )
    unknown = [(s.unit, s.sku) for s in summary.counted if s.unit not in KNOWN_UNITS]
    return said + [
        f"{where}: unit {unit!r} (SKU {sku!r}) is new to ai-cost — counted by its amounts"
        for unit, sku in unknown
    ]


def bill_source(summary: BillSummary, per_project: bool = False) -> str:
    """The sources line: the account and the whole UTC days in the totals."""
    span = f"{summary.days[0].isoformat()}..{summary.days[-1].isoformat()}" if summary.days else "none"
    text = f"github usage report ({summary.account}): whole UTC days {span} ({len(summary.days)})"
    return text + (
        "; its seats only — the account's figures are in an --all-projects report" if per_project else ""
    )


def _summary(request: BillRequest, window: Window, days: Sequence[date], read: _Read) -> BillSummary:
    read_months = [day for day in days if (day.year, day.month) not in read.unread]
    whole = sorted(day for day in read_months if is_whole(day, window, request.today))
    counted = [line for line in read.lines if is_counted(line, request)]
    return BillSummary(
        account=request.account.label,
        read_at=request.read_at,
        days=tuple(whole),
        provisional=tuple(day for day in whole if is_provisional(day, request.read_at)),
        counted=subtotals(line for line in counted if line.day in whole),
        left_out=subtotals(
            line for line in read.lines if line.day in whole and not is_counted(line, request)
        ),
        outside=outside_days(counted, sorted(set(days) - set(whole)), window, request.today),
        missing=tuple(read.missing),
        unreadable_lines=read.unreadable,
        months_read=tuple(sorted({f"{day:%Y-%m}" for day in read_months})),
    )


def collect_bill(request: BillRequest, window: Window) -> BillResult:
    """Rows for the whole, ended UTC days of the window; the summary holds the report's exact figures (ADR-0007)."""
    result = BillResult()
    days = touched_days(window, request.today)
    read = read_lines(request, days, result)
    summary = _summary(request, window, days, read)
    whole = set(summary.days)
    counted = [line for line in read.lines if is_counted(line, request)]
    result.rows = [invoice_row(line, request.account) for line in counted if line.day in whole]
    result.summary = summary
    return result


@dataclass(frozen=True)
class BillProbe:
    """``doctor``'s answer about the account: why its report cannot be read, or its newest day with lines."""

    account: BillAccount
    failure: str = ""
    newest: date | None = None
    offline: bool = False  # not read on purpose (AI_COST_OFFLINE): information, not a problem


def probe_bill(request: BillRequest) -> BillProbe:
    """Read the current month's report once: readable (and the newest day it holds), or why not."""
    if request.offline:
        return BillProbe(request.account, "offline (AI_COST_OFFLINE): not read", offline=True)
    try:
        items = parse_items(fetch_month(request, request.today.year, request.today.month))
    except BillError as exc:
        return BillProbe(request.account, str(exc))
    days = []
    for item in items:
        try:
            days.append(parse_line(item).day)
        except ValueError:
            continue  # a malformed line is counted where amounts are read (collect_bill), not here
    return BillProbe(request.account, "", max(days, default=None))
