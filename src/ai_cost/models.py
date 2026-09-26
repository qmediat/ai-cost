"""Typed data that crosses function boundaries (Invariant #14b, DESIGN.md "Data model").

Parsing produces these; everything downstream consumes them. A dict never leaves a ``parse_*`` function.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
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
    review run. ``COPILOT`` / ``ACTIONS``: GitHub per-unit rows.
    """

    TRANSCRIPT = "transcript"
    SESSION = "session"
    CHAT = "chat"
    LOG = "log"
    LEDGER = "ledger"
    REVIEW = "review"
    COPILOT = "copilot"
    ACTIONS = "actions"


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
    """Per-unit prices: Copilot credits per review and their overage, Actions minutes per runner OS, web search."""

    units_per_review: int = 0
    overage_usd_per_unit: float = 0.0
    usd_per_minute: Mapping[str, float] = field(default_factory=dict)
    public_repos_free: bool = True
    web_search_per_1000: float = 0.0
    copilot_source: str = ""  # where the Copilot overage price is published; empty = only the provider page


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
