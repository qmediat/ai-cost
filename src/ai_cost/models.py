"""Typed data that crosses function boundaries (Invariant #14b, DESIGN.md "Data model").

Parsing produces these; everything downstream consumes them. A dict never leaves a ``parse_*`` function.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Union

_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


@dataclass(frozen=True, order=True)
class Provider:
    """Who bills for a row: a provider id of the pricebook, which is the registry (ADR-0004).

    The seven the core has real-billing rules for are constants; any other id a prices file lists is a valid provider
    for a plugin's rows and is priced at list price (``api``) unless a rule says otherwise. ``value`` keeps the
    enum-era spelling, so ``row.provider.value`` and the JSON output are unchanged.
    """

    value: str

    _interned: ClassVar[dict[str, Provider]] = {}
    ANTHROPIC: ClassVar[Provider]
    OPENAI: ClassVar[Provider]
    GOOGLE: ClassVar[Provider]
    XAI: ClassVar[Provider]
    DEEPSEEK: ClassVar[Provider]
    ALIBABA: ClassVar[Provider]
    GITHUB: ClassVar[Provider]

    def __new__(cls, value: str) -> Provider:
        """One instance per id, so ``is`` comparisons hold as they did for the enum this type replaced.

        The id is checked here, on every construction path: anything but lowercase letters, digits, ``_``, ``.`` and
        ``-`` is a ``ValueError``, whether it comes through ``Provider(...)`` or ``Provider.of(...)``.
        """
        if not isinstance(value, str) or not _PROVIDER_ID.fullmatch(value):
            raise ValueError(f"not a provider id: {value!r}")
        instance = cls._interned.get(value)
        if instance is None:
            instance = cls._interned[value] = super().__new__(cls)
        return instance

    def __reduce__(self) -> tuple[type[Provider], tuple[str]]:
        """Copies and pickles rebuild through ``__new__`` with the id, so they return the interned instance."""
        return (Provider, (self.value,))

    def __str__(self) -> str:
        return self.value

    @classmethod
    def of(cls, text: str) -> Provider:
        """A provider id as text (the documented way in); the check itself lives in ``__new__``."""
        return cls(text)


Provider.ANTHROPIC = Provider("anthropic")
Provider.OPENAI = Provider("openai")
Provider.GOOGLE = Provider("google")
Provider.XAI = Provider("xai")
Provider.DEEPSEEK = Provider("deepseek")
Provider.ALIBABA = Provider("alibaba")  # Alibaba Cloud Model Studio — the Qwen models (2026-09-24)
Provider.GITHUB = Provider("github")


class Billing(Enum):
    """How a row was paid for."""

    @classmethod
    def rule(cls, text: str) -> Billing:
        """A configured billing rule (``api`` | ``subscription``) as the default for rows without their own evidence."""
        return {"api": cls.API, "subscription": cls.SUBSCRIPTION}.get(text, cls.UNKNOWN)

    SUBSCRIPTION = "subscription"
    API = "api"
    API_SETTLED = (
        "api-settled"  # pay-per-token, but the cash sits on another row: a ledger line or a sibling row
    )
    UNKNOWN = "unknown"


class RowKind(Enum):
    """What shape of record a row came from (a grouping and rendering key; ``UsageRow.source`` names the source).

    ``TRANSCRIPT``: an assistant message of a chat transcript on disk. ``SESSION``: a turn of a CLI session log.
    ``CHAT``: a model message of a chat log. ``LOG``: a line a program wrote to a usage log. ``LEDGER``: a row that
    carries the cash of calls another row already counted (skipped by the API-equivalent group). ``REVIEW``: a
    review run. ``COPILOT`` / ``ACTIONS``: GitHub counts (reviews, minutes). ``INVOICE``: a line of a provider's usage
    report, carrying its own amounts (``UsageRow.invoice``, ADR-0007). ``UNTRACKED``: what a provider's day report
    bills beyond the local records of that day (``UsageRow.untracked``, ADR-0008).
    """

    TRANSCRIPT = "transcript"
    SESSION = "session"
    CHAT = "chat"
    LOG = "log"
    LEDGER = "ledger"
    REVIEW = "review"
    COPILOT = "copilot"
    ACTIONS = "actions"
    INVOICE = "invoice"
    UNTRACKED = "untracked"


class Size(Enum):
    """Effort band of a work item (vendor group)."""

    XS = "XS"
    S = "S"
    M = "M"
    L = "L"
    XL = "XL"


class CheckStatus(Enum):
    """Outcome of a price drift check for one model."""

    CONFIRMED = "confirmed"
    CHANGED = "changed?"
    NOT_FOUND = "not-found"
    APPLIED = "applied"
    FETCH_FAILED = "fetch-failed"


@dataclass(frozen=True)
class Tokens:
    """Every counter a source can report; zero when absent (ADR: C2/C3 keep ``requests`` and the unsplit write)."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_unsplit: int = 0
    cached_input: int = 0
    prompt: int = 0
    cached: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    requests: int = 0
    web_search: int = 0
    reviews: int = 0
    minutes: float = 0.0
    by_os: Mapping[str, float] = field(default_factory=dict)
    billable: bool = False

    def add(self, other: Tokens) -> Tokens:
        """Field-wise sum (used when aggregating rows of one model)."""
        by_os = dict(self.by_os)
        for key, value in other.by_os.items():
            by_os[key] = by_os.get(key, 0.0) + value
        return Tokens(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write_5m=self.cache_write_5m + other.cache_write_5m,
            cache_write_1h=self.cache_write_1h + other.cache_write_1h,
            cache_write_unsplit=self.cache_write_unsplit + other.cache_write_unsplit,
            cached_input=self.cached_input + other.cached_input,
            prompt=self.prompt + other.prompt,
            cached=self.cached + other.cached,
            cache_hit=self.cache_hit + other.cache_hit,
            cache_miss=self.cache_miss + other.cache_miss,
            requests=self.requests + other.requests,
            web_search=self.web_search + other.web_search,
            reviews=self.reviews + other.reviews,
            minutes=self.minutes + other.minutes,
            by_os=by_os,
            billable=self.billable or other.billable,
        )


@dataclass(frozen=True)
class Scope:
    """Where a row came from, for attribution (ADR-0003): checkout keys and the paths the turn touched."""

    branch: str = ""
    workspace: str = ""
    pr: str = ""
    paths: tuple[str, ...] = ()

    def identity(self) -> tuple[str, ...]:
        """The keys that decide a label outright (paths only vote when none of these matched)."""
        return tuple(key for key in (self.branch, self.workspace, self.pr) if key)


SEAT_UNIT = "UserMonths"  # the usage-report unit of a seat: a subscription, not usage (ADR-0007)
ELAPSED_RUNNER = (
    "elapsed"  # the runner key of a run's wall-clock minutes when /timing gave none: never priced
)


@dataclass(frozen=True)
class InvoiceAmounts:
    """One line of a provider's usage report as the report states it (ADR-0007): summed, never computed.

    ``gross`` is the usage before anything included in a plan, ``discount`` what the plan covered, ``net`` what is
    billed; ``day`` is the report's UTC day.
    """

    day: date
    unit: str
    quantity: float
    unit_price: float
    gross: float
    discount: float
    net: float

    @property
    def is_seat(self) -> bool:
        """A seat line: its net amount is a subscription share, not usage."""
        return self.unit == SEAT_UNIT


class BillScope(Enum):
    """Whose usage report ``providers.github.bill`` names; the value is the REST path's singular.

    No enterprise: its report leaves out the usage assigned to cost centers unless asked per cost center (ADR-0007).
    """

    ORGANIZATION = "organization"
    USER = "user"


@dataclass(frozen=True)
class BillAccount:
    """The GitHub account whose usage report prices the GitHub rows (``providers.github.bill``)."""

    scope: BillScope
    name: str

    @property
    def endpoint(self) -> str:
        """The REST path of its itemized usage report."""
        return f"{self.scope.value}s/{self.name}/settings/billing/usage"

    @property
    def label(self) -> str:
        """How warnings name it: ``organization acme``."""
        return f"{self.scope.value} {self.name}"


@dataclass(frozen=True)
class BillSubtotal:
    """The exact sum of one product / SKU / unit of a usage report over the lines it covers (ADR-0007)."""

    product: str
    sku: str
    unit: str
    lines: int
    quantity: Decimal | None  # None when a line stated no quantity
    gross: Decimal
    discount: Decimal
    net: Decimal


@dataclass(frozen=True)
class OutsideDay:
    """A UTC day the window touches but does not hold whole (or that is not over): its exact amounts, not in totals."""

    day: date
    why: str
    lines: int
    gross: Decimal
    discount: Decimal
    net: Decimal


@dataclass(frozen=True)
class RepositoryDay:
    """One repository's counted lines of one product / SKU on one UTC day the window touches: the report's exact sums.

    A repository's day holds every use of that day in it (every PR, every person): a whole the report does not split.
    ``final`` is false while the day may still grow (not over, or ended less than ``SETTLE_HOURS`` before the reading).
    """

    day: date
    repository: str  # owner/name
    product: str
    sku: str
    lines: int
    gross: Decimal
    discount: Decimal
    net: Decimal
    final: bool


@dataclass(frozen=True)
class BillSummary:
    """What a report's GitHub amounts rest on: the report's own figures, exact, and what they leave out."""

    account: str
    read_at: datetime
    days: tuple[date, ...]  # the whole UTC days in the totals
    provisional: tuple[date, ...]  # of those, the ones read less than SETTLE_HOURS after they ended
    counted: tuple[BillSubtotal, ...]
    left_out: tuple[BillSubtotal, ...]  # other products, and Actions of repositories not named with --github
    outside: tuple[OutsideDay, ...]
    missing: tuple[str, ...]  # months that could not be read, with the reason
    unreadable_lines: int = 0
    months_read: tuple[
        str, ...
    ] = ()  # YYYY-MM of every month whose report was read: its amounts are the report's
    repositories: tuple[
        RepositoryDay, ...
    ] = ()  # every touched day's counted lines per repository, whole or not


# the decimals a provider's report states amounts to (GitHub's usage report); a conversion keeps them
STATED_PLACES = 9


@dataclass(frozen=True)
class ProviderLine:
    """One label of a provider's day report (a model, a SKU) with its amounts as the provider states them (ADR-0008).

    ``gross`` is the usage before anything free or discounted, ``net`` what the account pays; ``excluded`` lines (tax,
    rounding, other services) are listed and never compared; ``requests`` is the provider's count when it keeps one.
    An amount converted from another currency is the provider's own quotient to ``STATED_PLACES`` decimals.
    """

    label: str
    gross: Decimal
    net: Decimal
    requests: int | None = None
    excluded: bool = False
    # the provider's own conversion when the account bills another currency (``PLN ÷ 3.80055``)
    rate_note: str = ""


@dataclass(frozen=True)
class ProviderDay:
    """One day of one account as the provider states it (ADR-0008): its own interval, exact lines, whether it is closed.

    ``start``/``end`` are aware instants in the provider's own offset; ``closed`` is the reader's claim, from the
    provider's own rule, that the day will not grow; ``captured`` is the earliest instant the figures can have been
    produced; ``currency`` is ``USD`` or names the provider's own conversion.
    """

    provider: Provider
    start: datetime
    end: datetime
    lines: tuple[ProviderLine, ...]
    closed: bool
    captured: datetime
    source: str
    currency: str = "USD"
    counts_requests: bool = (
        False  # the provider states a request count per line (DeepSeek): a day without one is 0
    )

    @property
    def gross(self) -> Decimal:
        """The usage the provider bills this day, before anything free or discounted."""
        return sum((line.gross for line in self.lines if not line.excluded), Decimal(0))

    @property
    def net(self) -> Decimal:
        """What the account pays for this day's usage."""
        return sum((line.net for line in self.lines if not line.excluded), Decimal(0))

    @property
    def requests(self) -> int | None:
        """The provider's request count; ``None`` when the provider states none (xAI, Google).

        The sum when every usage line has one; 0 for a day without usage lines from a provider that counts requests.
        """
        counts = [line.requests for line in self.lines if not line.excluded]
        if any(c is None for c in counts) or not (counts or self.counts_requests):
            return None
        return sum(c for c in counts if c is not None)


@dataclass(frozen=True)
class Span:
    """An interval of provider data a reader could not give, and why (ADR-0008): never a zero."""

    start: datetime
    end: datetime
    why: str


@dataclass(frozen=True)
class ProviderDays:
    """What a reader returns for a window: the days it holds, and the spans it holds nothing for (missing, unreadable)."""

    provider: Provider
    source: str
    days: tuple[ProviderDay, ...] = ()
    # no data: nothing imported there, a live source not asked (offline)
    missing: tuple[Span, ...] = ()
    # the newest capture the reader holds (an import's, a live read's time), for doctor
    captured: datetime | None = None
    # data that exists but could not be read: a broken store, a refused or failed request
    unreadable: tuple[Span, ...] = ()
    # a live source not asked (AI_COST_OFFLINE): its span is missing, not a problem
    offline: bool = False
    # the source's days are UTC days, so a UTC `daily` file can hold one whole (xAI, Google, a DeepSeek import taken in
    # UTC; never Alibaba's UTC+8 days)
    utc_days: bool = False


@dataclass(frozen=True)
class Untracked:
    """What a provider day bills beyond the day's local records, per group (ADR-0008); a row's own amounts."""

    day: str  # the provider day, as its interval reads: ``2026-09-21T00:00+02:00/2026-09-22T00:00+02:00``
    api: float
    real: float


@dataclass(frozen=True)
class DayComparison:
    """One provider day against the day's local records (ADR-0008): both differences, or why it was not compared.

    ``local_api`` / ``local_real`` are the day's local amounts in the API and real group; a difference within the float
    error of the local sum is zero; ``why_not`` is empty for a compared day. ``unknown_rows`` are rows of unknown
    billing (API priced, no cash); ``local_requests`` sums the rows that count requests, ``uncounted_rows`` the others.
    """

    day: ProviderDay
    why_not: str = ""
    local_api: Decimal | None = None
    local_real: Decimal | None = None
    api_diff: Decimal | None = None
    real_diff: Decimal | None = None
    unknown_rows: int = 0
    local_requests: int = 0
    uncounted_rows: int = 0


@dataclass(frozen=True)
class ProviderSummary:
    """Everything a report says about one provider's day reports: compared days, the others, the missing spans."""

    provider: Provider
    source: str
    days: tuple[DayComparison, ...]
    missing: tuple[Span, ...] = ()
    captured: datetime | None = None
    unreadable: tuple[Span, ...] = ()
    offline: bool = False
    utc_days: bool = False

    @property
    def compared(self) -> tuple[DayComparison, ...]:
        """The days whose differences were computed."""
        return tuple(day for day in self.days if not day.why_not)

    @property
    def not_compared(self) -> tuple[DayComparison, ...]:
        """The days listed with the reason they were not compared."""
        return tuple(day for day in self.days if day.why_not)


def client_outside(client: str, clients: Sequence[str]) -> bool:
    """Whether a row's client — named, never empty — is one the config puts outside the tracked work."""
    return bool(client) and client in clients


@dataclass(frozen=True)
class UsageRow:
    """One priced unit of usage: a call, a review, a review count, a run."""

    provider: Provider
    model: str
    kind: RowKind
    at: datetime | None
    ref: str
    billing: Billing
    tokens: Tokens
    cost_reported: float | None = (
        None  # the source's own figure: cash for an API row, its estimate for a plan row
    )
    scope: Scope = field(default_factory=Scope)
    source: str = ""  # the name of the source that produced the row (a built-in or a plugin source)
    share: float = 1.0  # this row's fraction of its session's usage (input + output), across every window
    client: str = (
        ""  # the program that wrote the session, as its file names it (a Codex rollout's originator)
    )
    invoice: InvoiceAmounts | None = None  # a usage-report line's own amounts (RowKind.INVOICE)
    untracked: Untracked | None = None  # a provider day's difference (RowKind.UNTRACKED, ADR-0008)
    origin_session: str = ""  # the session that launched the work: a transcript id, a line's origin_session


@dataclass(frozen=True)
class Money:
    """A priced row: amount in USD and how it was computed."""

    usd: float
    note: str = ""


@dataclass(frozen=True)
class TokenTierPrice:
    """USD per 1M tokens for models billed by input / cached / output (Anthropic, OpenAI, Google, xAI)."""

    input: float
    output: float
    cached_input: float = 0.0
    cache_read: float = 0.0
    cache_write_5m: float = 0.0
    cache_write_1h: float = 0.0
    long_threshold: int = 0
    long: TokenTierPrice | None = None
    aliases: tuple[str, ...] = ()
    valid_until: date | None = None
    next: TokenTierPrice | None = None  # the announced price after this one
    next_from: date | None = None  # when it applies (default: the day after valid_until)


@dataclass(frozen=True)
class Rate:
    """One DeepSeek tariff: cache hit / cache miss / output per 1M tokens."""

    cache_hit: float
    cache_miss: float
    output: float


@dataclass(frozen=True)
class PeakOffpeakPrice:
    """Models with a peak and an off-peak tariff (DeepSeek)."""

    peak: Rate
    offpeak: Rate
    display: str = ""


@dataclass(frozen=True)
class PerUnitPrice:
    """Per-unit prices: Actions minutes per runner OS, web search (a Copilot review has no price, ADR-0007)."""

    usd_per_minute: Mapping[str, float] = field(default_factory=dict)
    public_repos_free: bool = True
    web_search_per_1000: float = 0.0


PriceEntry = Union[TokenTierPrice, PeakOffpeakPrice, PerUnitPrice]


@dataclass(frozen=True)
class Window:
    """A half-open time window; every source is filtered to it."""

    start: datetime
    end: datetime

    def hours(self) -> float:
        """Length in hours."""
        return max(0.0, (self.end - self.start).total_seconds() / 3600.0)

    def contains(self, at: datetime | None) -> bool:
        """``start <= at < end`` (half-open, so adjacent windows never share a record; a missing timestamp never fits)."""
        return at is not None and self.start <= at < self.end


@dataclass(frozen=True)
class WorkItem:
    """One deliverable for the vendor group (a PR, or a hand-sized scope line)."""

    id: str
    title: str
    size: Size
    pr: str | None = None
    rounds: int = 0
    loc: int | None = None


@dataclass(frozen=True)
class Skipped:
    """A file or record the collectors could not use — counted, never silent (Invariant #14d)."""

    source: str
    path: str
    reason: str


@dataclass(frozen=True)
class Collected:
    """What one collector returns."""

    rows: Sequence[UsageRow] = ()
    items: Sequence[WorkItem] = ()
    skipped: Sequence[Skipped] = ()
    span: Window | None = None
