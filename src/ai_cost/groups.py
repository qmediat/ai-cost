"""The three answers: real (what was paid), api (as if pay-per-use only), vendor (an outside quote). Pure functions."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Callable

from .config import Config, PriceBook, Staffing, VendorProfile
from .errors import ConfigError, PricingError
from .models import Billing, Money, Provider, RowKind, Size, Skipped, Tokens, UsageRow, Window, WorkItem
from .pricing import price

HOURS_PER_MONTH = 730.0


@dataclass
class Line:
    """One aggregated line of a group table."""

    provider: Provider
    label: str
    calls: int = 0
    usd: float = 0.0
    tokens: Tokens = field(default_factory=Tokens)
    notes: set[str] = field(default_factory=set)

    def add(self, row: UsageRow, money: Money) -> None:
        """Fold one priced row in."""
        self.calls += row.tokens.reviews if row.kind is RowKind.COPILOT else 1
        self.usd += money.usd
        self.tokens = self.tokens.add(row.tokens)
        if money.note:
            self.notes.add(money.note)

    @property
    def model_calls(self) -> int:
        """API requests the sources reported (``Tokens.requests``); 0 where no source knows them."""
        return self.tokens.requests


@dataclass(frozen=True)
class SubscriptionShare:
    """A plan's cost attributed to the window."""

    plan: str
    provider: str
    seats: int
    monthly_usd: float
    attribution: str
    usd: float
    note: str = ""


@dataclass(frozen=True)
class RealGroup:
    """What was paid: prorated subscriptions + API keys at list price."""

    subscriptions: Sequence[SubscriptionShare]
    usage: Sequence[Line]
    unknown_billing: int = (
        0  # rows nobody could attribute to a plan or a key (the API group prices the ones with tokens)
    )
    unknown_by_provider: Mapping[str, int] = field(
        default_factory=dict
    )  # the rows a billing rule could place, per provider id
    unfigured_ledger: int = (
        0  # ledger rows without a figure; ``unknown_billing`` = these + the per-provider rows
    )

    @property
    def cash_usd(self) -> float:
        """Money that left the account through API keys."""
        return sum(line.usd for line in self.usage)

    @property
    def subscription_usd(self) -> float:
        """The window's share of the subscriptions."""
        return sum(share.usd for share in self.subscriptions)

    @property
    def total_usd(self) -> float:
        """Cash plus subscription share."""
        return self.cash_usd + self.subscription_usd


@dataclass(frozen=True)
class ApiGroup:
    """Every token at pay-per-use list price."""

    lines: Sequence[Line]

    @property
    def total_usd(self) -> float:
        """Sum of all lines."""
        return sum(line.usd for line in self.lines)


@dataclass(frozen=True)
class VendorOption:
    """One staffing option of a vendor quote, as a range."""

    staffing: str
    rate: float
    time_factor: float
    senior_review_pct: float
    blended_rate: float
    hours: tuple[float, float]
    days: tuple[float, float]
    cost: tuple[float, float]


@dataclass(frozen=True)
class VendorGroup:
    """What an outside firm would quote for the items."""

    profile: str
    currency: str
    description: str
    items: Sequence[WorkItem]
    band_counts: Mapping[Size, int]
    base_hours: tuple[float, float]
    integration_pct: float
    package_min_hours: float
    package_round_hours: float
    options: Sequence[VendorOption]


@dataclass(frozen=True)
class AttributionLine:
    """One label's share of the window; ``keys`` are the branches / workspaces / PRs it absorbed."""

    label: str
    calls: int
    api_usd: float
    usd_context: float  # cache reads: the context the session carried into the turns, not their own work
    cash_usd: float
    subscription_usd: float
    keys: tuple[str, ...]

    @property
    def usd_work(self) -> float:
        """API-equivalent cost minus the context tax."""
        return self.api_usd - self.usd_context

    @property
    def real_usd(self) -> float:
        """Cash plus the label's subscription share."""
        return self.cash_usd + self.subscription_usd


@dataclass(frozen=True)
class AttributionGroup:
    """Per-label lines (the rules' labels, then ``mixed``, then ``unattributed``); they sum to the window totals."""

    policy: str
    lines: Sequence[AttributionLine]

    @property
    def total_api_usd(self) -> float:
        """Sum of every line's API-equivalent cost."""
        return sum(line.api_usd for line in self.lines)

    @property
    def total_real_usd(self) -> float:
        """Sum of every line's real cost."""
        return sum(line.real_usd for line in self.lines)


@dataclass(frozen=True)
class Report:
    """Everything a run produced; rendered by ``render.py``."""

    version: str
    generated_at: str
    window: Window
    window_iso: tuple[str, str]
    sources: Sequence[str]
    warnings: Sequence[str]
    skipped: Sequence[Skipped]
    prices_checked_at: str
    rows: Sequence[UsageRow]
    real: RealGroup | None
    api: ApiGroup | None
    vendor: VendorGroup | None
    attribution: AttributionGroup | None = None

    @property
    def window_hours(self) -> float:
        """Length of the window in hours (also in JSON)."""
        return self.window.hours()

    @property
    def row_count(self) -> int:
        """Number of usage rows in the window, priced or not (in JSON even when ``rows`` is left out)."""
        return len(self.rows)


# ---- api --------------------------------------------------------------------------------------------------------


def api_group(rows: Sequence[UsageRow], book: PriceBook, config: Config) -> ApiGroup:
    """Aggregate by (provider, model); every LEDGER row is skipped, a plugin's too.

    A ledger row is cash only — its tokens sit on the session rows it settles — so pricing it at list would count the
    same calls twice.
    """
    lines: dict[tuple[Provider, str], Line] = {}
    for row in rows:
        if row.kind is RowKind.LEDGER:
            continue
        line = lines.setdefault((row.provider, row.model), Line(row.provider, row.model))
        line.add(row, price(row, book, config))
    return ApiGroup(sorted(lines.values(), key=lambda line: -line.usd))


# ---- real -------------------------------------------------------------------------------------------------------

RealRule = Callable[[UsageRow, PriceBook, Config], tuple[str, Money]]


def _rule_anthropic(row: UsageRow, book: PriceBook, config: Config) -> tuple[str, Money]:
    """The row's own billing decides (the configured rule on a transcript, a log line's key): plan rows cost nothing."""
    if row.billing is Billing.SUBSCRIPTION:
        return "plan", Money(0.0, "Claude plan")
    return "api", _reported_or_list(row, book, config)


def _rule_openai(row: UsageRow, book: PriceBook, config: Config) -> tuple[str, Money]:
    if (
        row.billing is Billing.API
    ):  # each row of a session carries its part of the reported cost, else the list price
        if row.cost_reported is not None:
            return "api-key (rollout)", Money(float(row.cost_reported), "pay-per-token, source-reported")
        return "api-key (rollout)", price(row, book, config)
    return "plan", Money(0.0, "ChatGPT plan")


def _rule_github(row: UsageRow, book: PriceBook, config: Config) -> tuple[str, Money]:
    """A plan row is within the allowance until the config says it is exhausted; a pay-per-use row is cash."""
    if row.cost_reported is not None:
        return row.model, Money(float(row.cost_reported), "source-reported")
    if row.billing is Billing.API:  # a plugin's or a logged row that says it paid per use
        return row.model, price(row, book, config)
    exhausted = (
        config.github.copilot_plan_exhausted
        if row.kind is RowKind.COPILOT
        else config.github.actions_plan_exhausted
    )
    if exhausted:
        return row.model, price(row, book, config)
    return row.model, Money(0.0, "within plan allowance")


def _rule_xai(row: UsageRow, book: PriceBook, config: Config) -> tuple[str, Money]:
    """A plan row is the plan's; a reported cost is what was paid — a CLI's own estimate only when the config trusts it.

    A ``REVIEW`` or ``SESSION`` row's figure is what the Grok CLI computed (``costUsdTicks``, a review wrapper's
    ``cost_usd``), never an invoice: ``xai.trust_cli_cost`` decides; a log line's figure is a charge the program saw.
    """
    if row.billing is Billing.SUBSCRIPTION:
        return "plan", Money(0.0, "xai plan")
    estimate = row.kind in (
        RowKind.REVIEW,
        RowKind.SESSION,
    )  # the CLI's own figure: xai.trust_cli_cost decides
    if row.cost_reported is not None and (config.xai_trust_cli_cost or not estimate):
        return row.model, Money(float(row.cost_reported), "CLI-reported" if estimate else "source-reported")
    return row.model, price(row, book, config)


def _rule_api(row: UsageRow, book: PriceBook, config: Config) -> tuple[str, Money]:
    """Pay-per-token at list — the rule of every provider without one of its own (a plugin's included).

    A row a plan paid for (``Billing.SUBSCRIPTION``, from the provider's configured rule or the source's evidence)
    is the plan's: no cash.
    """
    if row.billing is Billing.SUBSCRIPTION:
        return "plan", Money(0.0, f"{row.provider.value} plan")
    return row.model, _reported_or_list(row, book, config)


def _reported_or_list(row: UsageRow, book: PriceBook, config: Config) -> Money:
    """What the source says the request was charged, else the list price (the api group prices at list regardless)."""
    if row.cost_reported is not None:
        return Money(float(row.cost_reported), "source-reported")
    return price(row, book, config)


def real_rule(provider: Provider) -> RealRule:
    """The real-billing rule of a provider; one without a rule of its own (a plugin's) is pay-per-token at list."""
    return REAL_RULES.get(provider, _rule_api)


REAL_RULES: Mapping[Provider, RealRule] = {
    Provider.ANTHROPIC: _rule_anthropic,
    Provider.OPENAI: _rule_openai,
    Provider.GITHUB: _rule_github,
    Provider.XAI: _rule_xai,
    Provider.GOOGLE: _rule_api,
    Provider.DEEPSEEK: _rule_api,
}


def subscription_shares(config: Config, book: PriceBook, window: Window) -> list[SubscriptionShare]:
    """Attribution ``time``: window hours / 730; ``full``: the whole month; ``none``: nothing."""
    hours = window.hours()
    shares = []
    for sub in config.subscriptions:
        plan = book.plans.get(sub.plan)
        if plan is None:
            shares.append(
                SubscriptionShare(
                    sub.plan,
                    "?",
                    sub.seats,
                    0.0,
                    sub.attribution,
                    0.0,
                    "unknown plan — add it to prices.json plans",
                )
            )
            continue
        monthly = plan.monthly_usd * (sub.seats if plan.per_seat else 1)
        usd = {"full": monthly, "none": 0.0}.get(sub.attribution, monthly * hours / HOURS_PER_MONTH)
        shares.append(SubscriptionShare(sub.plan, plan.provider, sub.seats, monthly, sub.attribution, usd))
    return shares


def needs_list_price(row: UsageRow, book: PriceBook, config: Config) -> bool:
    """Whether the real group would price this row at list — the rules themselves decide, nothing is duplicated."""
    if row.billing in (Billing.UNKNOWN, Billing.API_SETTLED) or row.kind is RowKind.LEDGER:
        return False  # left out, skipped, or cash by its own figure
    try:
        real_rule(row.provider)(row, book, config)
    except PricingError:
        return True
    return False


def real_group(rows: Sequence[UsageRow], book: PriceBook, config: Config, window: Window) -> RealGroup:
    """Subscriptions prorated + the rules table per provider.

    A Codex API call is cash once: by its ledger row when a plugin supplied one, else by the session cost the
    source reported — split across the session's rows by their share of its usage, so the parts sum to that cost
    across rows and windows — else at list price; rows whose cash sits on a ledger row are ``API_SETTLED`` and
    skipped here.
    """
    lines: dict[tuple[Provider, str], Line] = {}
    unknown: Counter[str] = Counter()
    unfigured = 0
    for row in rows:
        # Never cash, whatever figure it carries: a figure without a rule is an estimate (a plan session's "what it
        # would have cost"); a source that knows what the key was charged says API.
        if row.billing is Billing.UNKNOWN:
            unknown[row.provider.value] += 1
            continue
        if row.billing is Billing.API_SETTLED:
            continue  # a ledger line or a sibling row carries what the key was charged
        if row.kind is RowKind.LEDGER:  # a ledger row is cash by its own figure, whoever the provider is
            if row.cost_reported is None:  # a charge record without a figure: unknown, never booked as 0
                unfigured += 1
                continue
            label, money = "api-key (ledger)", Money(float(row.cost_reported), "pay-per-token, ledgered")
        else:
            label, money = real_rule(row.provider)(row, book, config)
        lines.setdefault((row.provider, label), Line(row.provider, label)).add(row, money)
    usage = sorted(lines.values(), key=lambda line: -line.usd)
    shares = subscription_shares(config, book, window)
    return RealGroup(shares, usage, sum(unknown.values()) + unfigured, dict(unknown), unfigured)


# ---- vendor -----------------------------------------------------------------------------------------------------


def _package(hours: float, profile: VendorProfile) -> float:
    hours = max(hours, profile.package_min_hours)
    step = profile.package_round_hours
    return math.ceil(hours / step) * step if step else hours


def _option(
    name: str, staffing: Staffing, base: tuple[float, float], profile: VendorProfile, hours_per_day: float
) -> VendorOption:
    senior_rate = profile.staffing["senior"].rate if "senior" in profile.staffing else staffing.rate
    hours: list[float] = []
    cost: list[float] = []
    blended = staffing.rate
    for base_hours in base:
        staffed = base_hours * staffing.time_factor
        review = staffed * staffing.senior_review_pct / 100.0
        blended = (
            ((staffed * staffing.rate + review * senior_rate) / (staffed + review))
            if staffed + review
            else staffing.rate
        )
        packaged = _package((staffed + review) * (1 + profile.integration_pct / 100.0), profile)
        hours.append(round(packaged, 1))
        cost.append(round(packaged * blended))
    return VendorOption(
        staffing=name,
        rate=staffing.rate,
        time_factor=staffing.time_factor,
        senior_review_pct=staffing.senior_review_pct,
        blended_rate=round(blended, 1),
        hours=(hours[0], hours[1]),
        days=(round(hours[0] / hours_per_day, 1), round(hours[1] / hours_per_day, 1)),
        cost=(cost[0], cost[1]),
    )


def vendor_group(items: Sequence[WorkItem], config: Config, profile_name: str | None = None) -> VendorGroup:
    """Band hours × staffing options, packaged the way agencies sell (references/vendor-pricing.md)."""
    name = profile_name or config.vendor_default_profile
    profile = config.vendor_profiles.get(name)
    if profile is None:
        raise ConfigError(
            f"unknown vendor profile {name!r}; known: {', '.join(sorted(config.vendor_profiles))}"
        )
    counts: dict[Size, int] = {}
    for item in items:
        counts[item.size] = counts.get(item.size, 0) + 1
    base = (
        sum(profile.bands_hours[item.size][0] for item in items),
        sum(profile.bands_hours[item.size][1] for item in items),
    )
    options = [
        _option(label, staffing, base, profile, config.hours_per_day)
        for label, staffing in profile.staffing.items()
    ]
    return VendorGroup(
        profile=name,
        currency=profile.currency,
        description=profile.description,
        items=list(items),
        band_counts=counts,
        base_hours=base,
        integration_pct=profile.integration_pct,
        package_min_hours=profile.package_min_hours,
        package_round_hours=profile.package_round_hours,
        options=options,
    )
