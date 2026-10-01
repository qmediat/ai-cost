"""One row per model: its tokens once, what paid for them and on what evidence, and the list price.

The real group sums cash per billing line and the API group prices every token at list; neither says, for one model,
how its usage was paid. A ``ModelLine`` does: its payments split by how (plan, metered beyond the plan, API key, billed
by a usage report, untracked, settled on another row, unknown) and by the evidence of the amount (list price, a figure
the tool or the source reported, a ledger, the usage report, a provider report, no amount of its own). The cash of each row is the real group's own
(``row_cash``): the two never disagree. A subscription fee is not split across models (a non-goal): a plan row's
payment is "plan", 0 cash, and its list price stands beside it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from .config import Config, PriceBook
from .groups import ApiGroup, Line, is_seat, row_cash
from .models import Billing, Provider, RowKind, Tokens, UsageRow
from .pricing import NOT_PRICED, REPORTED_FALLBACK


class Paid(Enum):
    """How a row was paid."""

    PLAN = "plan"
    METERED = (
        "metered beyond the plan"  # a plan row that still carries cash (GitHub Actions past its allowance)
    )
    API = "API key"
    BILLED = "billed"  # a usage-report line: what the invoice bills for it
    UNTRACKED = "untracked"  # a provider day's charge no local record accounts for
    ELSEWHERE = "settled on another row"
    UNKNOWN = "unknown"


class Evidence(Enum):
    """What the amount rests on."""

    LIST = "list price"
    REPORTED = "reported"  # the tool's or the source's own figure
    LEDGER = "ledger"
    USAGE_REPORT = "usage report"
    PROVIDER_REPORT = "provider report"
    NONE = "no amount"


@dataclass(frozen=True)
class Payment:
    """The rows of one model paid one way, on one kind of evidence."""

    paid: Paid
    evidence: Evidence
    usd: float
    rows: int

    @property
    def text(self) -> str:
        """``API key 12.40 (reported)``, ``plan ×2401`` — one cell of the per-model table (JSON ``text``)."""
        if (
            not self.usd
        ):  # no amount of its own: the count, and the record that holds its amount when one does
            held = f" ({self.evidence.value})" if self.evidence is not Evidence.NONE else ""
            return f"{self.paid.value} ×{self.rows}{held}"
        amount = _amount(self.usd)
        return f"{self.paid.value} {amount} ({self.evidence.value})"


def _amount(usd: float) -> str:
    """A payment as the tables print it: a fraction of a cent says so, a credit (below zero) keeps its sign."""
    if 0 < usd < 0.005:
        return "< 0.01"
    if -0.005 < usd < 0:
        return "> -0.01"
    return f"{usd:,.2f}"


@dataclass(frozen=True)
class ModelLine:
    """One model of the report: its API-group line, and how the cash the real group counts was paid."""

    provider: Provider
    model: str
    records: int  # as the API group counts them (a Copilot row: its reviews; a usage-report line: none)
    model_calls: int  # API requests the sources reported; 0 where no source knows them
    tokens: Tokens
    api_usd: float
    payments: tuple[Payment, ...] = field(default_factory=tuple)
    api_unlisted: bool = False  # no list price: the API figure is the one the tool reported

    @property
    def paid_usd(self) -> float:
        """The cash of the model's rows, as the real group counts it."""
        return sum(payment.usd for payment in self.payments)


def model_lines(
    rows: Sequence[UsageRow], api: ApiGroup, book: PriceBook, config: Config
) -> tuple[ModelLine, ...]:
    """Every model the rows name, the largest list price first, then the largest cash, then by name."""
    rows_of: dict[tuple[Provider, str], list[UsageRow]] = {}
    for row in rows:
        if not is_seat(
            row
        ):  # a seat is a subscription share: the real group's subscriptions show it, not a model
            rows_of.setdefault((row.provider, row.model), []).append(row)
    lines = {(line.provider, line.label): line for line in api.lines}
    made = [_line(key, model_rows, lines, book, config) for key, model_rows in rows_of.items()]
    return tuple(sorted(made, key=lambda m: (-m.api_usd, -m.paid_usd, m.provider.value, m.model)))


def _line(
    key: tuple[Provider, str],
    rows: Sequence[UsageRow],
    lines: Mapping[tuple[Provider, str], Line],
    book: PriceBook,
    config: Config,
) -> ModelLine:
    """A model's API line (none for a model the API group skips: a ledger's only) beside its payments."""
    api = lines.get(key) or Line(key[0], key[1])
    payments = payments_of(rows, book, config)
    unlisted = REPORTED_FALLBACK in api.notes  # the API group fell back to the figure the tool reported
    return ModelLine(key[0], key[1], api.calls, api.model_calls, api.tokens, api.usd, payments, unlisted)


def payments_of(rows: Sequence[UsageRow], book: PriceBook, config: Config) -> tuple[Payment, ...]:
    """The rows' payments, one per way and evidence, the largest amount first: the one rule for every per-model view."""
    totals: dict[tuple[Paid, Evidence], tuple[float, int]] = {}
    for row in rows:
        paid, evidence, usd = payment_of(row, book, config)
        before = totals.get((paid, evidence), (0.0, 0))
        totals[(paid, evidence)] = (before[0] + usd, before[1] + 1)
    made = [Payment(paid, evidence, usd, count) for (paid, evidence), (usd, count) in totals.items()]
    return tuple(sorted(made, key=lambda p: (-p.usd, p.paid.value, p.evidence.value)))


def payment_of(row: UsageRow, book: PriceBook, config: Config) -> tuple[Paid, Evidence, float]:
    """How one row was paid, on what evidence, and its cash — the real group's own figure."""
    fixed = _no_cash_of_its_own(row)
    if fixed is not None:
        return fixed[0], fixed[1], 0.0
    cash = row_cash(row, book, config)
    if cash is None:  # a charge record without a figure: unknown, never booked as 0
        return Paid.UNKNOWN, Evidence.NONE, 0.0
    paid, evidence = _classify(row, cash[1].usd, cash[1].note)
    return paid, evidence, cash[1].usd


def _no_cash_of_its_own(row: UsageRow) -> tuple[Paid, Evidence] | None:
    """A row that carries no cash of its own: unknown billing, settled on another record, a seat."""
    if (
        row.billing is Billing.API_SETTLED
    ):  # its cash is on another record: the usage report (GitHub), a ledger, a sibling
        return Paid.ELSEWHERE, Evidence.USAGE_REPORT if row.provider is Provider.GITHUB else Evidence.NONE
    if is_seat(row):  # a seat is a subscription share, never usage
        return Paid.PLAN, Evidence.USAGE_REPORT
    return (Paid.UNKNOWN, Evidence.NONE) if row.billing is Billing.UNKNOWN else None


def _record(row: UsageRow) -> str:
    """Which record carries the row's own amount: a ledger line, a usage-report line, a provider day, or none."""
    if row.kind is RowKind.LEDGER:
        return "ledger"
    return "invoice" if row.invoice is not None else "untracked" if row.untracked is not None else ""


_BY_RECORD: dict[str, tuple[Paid, Evidence]] = {
    "ledger": (Paid.API, Evidence.LEDGER),
    "invoice": (Paid.BILLED, Evidence.USAGE_REPORT),
    "untracked": (Paid.UNTRACKED, Evidence.PROVIDER_REPORT),
}


def _classify(row: UsageRow, usd: float, note: str) -> tuple[Paid, Evidence]:
    """How a row with cash of its own was paid, and what its amount rests on.

    The amount is the source's own figure when it equals the row's ``cost_reported``; else a list price. A zero the
    rule could not price (its note says ``not priced``: a runner without a list price) is unknown, never plan-covered.
    """
    special = _BY_RECORD.get(_record(row))
    if special is not None:
        return special
    # a zero the rule could not price, or a Copilot count whose amount no usage report read gives: unknown, never plan
    if NOT_PRICED in note or (row.kind is RowKind.COPILOT and not usd):
        return Paid.UNKNOWN, Evidence.NONE
    reported = row.cost_reported is not None and abs(float(row.cost_reported) - usd) < 1e-9
    evidence = Evidence.REPORTED if reported else Evidence.LIST
    if row.billing is Billing.SUBSCRIPTION:
        return (Paid.METERED, evidence) if usd else (Paid.PLAN, Evidence.NONE)
    return Paid.API, evidence
