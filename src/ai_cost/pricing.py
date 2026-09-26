"""Row → money. One pricer per price shape, chosen by a dispatch table (no provider switch anywhere else).

The reported-cost fallback of 1.0 is kept (consult C6): a row whose model has no list price but carries a CLI-reported
cost is priced at that cost; a row with neither raises ``PricingError``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Callable

from .config import Config, PriceBook
from .errors import PricingError
from .models import (
    ELAPSED_RUNNER,
    Billing,
    Money,
    PeakOffpeakPrice,
    PerUnitPrice,
    PriceEntry,
    Provider,
    Rate,
    RowKind,
    Tokens,
    TokenTierPrice,
    UsageRow,
)

MILLION = 1_000_000.0
Pricer = Callable[[UsageRow, PriceEntry, PriceBook, Config], Money]


def normalize(tokens: Tokens, cache_ttl_default: str) -> Tokens:
    """Assign an unsplit ``cache_creation`` count to the configured TTL tier (consult C3: explicit, typed step)."""
    if not tokens.cache_write_unsplit:
        return tokens
    if cache_ttl_default == "5m":
        return Tokens(
            **{
                **tokens.__dict__,
                "cache_write_5m": tokens.cache_write_5m + tokens.cache_write_unsplit,
                "cache_write_unsplit": 0,
            }
        )
    return Tokens(
        **{
            **tokens.__dict__,
            "cache_write_1h": tokens.cache_write_1h + tokens.cache_write_unsplit,
            "cache_write_unsplit": 0,
        }
    )


def current_price(price: TokenTierPrice, at: datetime | None) -> TokenTierPrice:
    """The announced ``next`` price once its date has come (``from``, else the day after ``valid_until``)."""
    if price.next is None or at is None:
        return price
    switch = price.next_from or (price.valid_until + timedelta(days=1) if price.valid_until else None)
    if switch and at.date() >= switch:
        return current_price(price.next, at)
    return price


def _tier(price: TokenTierPrice, tokens: Tokens) -> TokenTierPrice:
    """The long-context tier applies to a single request above the threshold, never to aggregated usage (C2)."""
    volume = tokens.input or tokens.prompt
    if price.long and price.long_threshold and volume >= price.long_threshold and tokens.requests <= 1:
        return price.long
    return price


ANTHROPIC_COUNTERS = frozenset(
    {"input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "cache_write_unsplit", "web_search"}
)
OPENAI_COUNTERS = frozenset(
    {"input", "cached_input", "output"}
)  # OpenAI, xAI and every OpenAI-compatible API
GOOGLE_COUNTERS = frozenset({"prompt", "cached", "output"})
PEAK_OFFPEAK_COUNTERS = frozenset({"cache_hit", "cache_miss", "output"})
TOKEN_TIER_COUNTERS = (
    ANTHROPIC_COUNTERS | OPENAI_COUNTERS
)  # a user-added provider priced by tiers: either family
_COUNTERS_OF: Mapping[Provider, frozenset[str]] = {
    Provider.ANTHROPIC: ANTHROPIC_COUNTERS,
    Provider.OPENAI: OPENAI_COUNTERS,
    Provider.XAI: OPENAI_COUNTERS,
    Provider.GOOGLE: GOOGLE_COUNTERS,
    Provider.DEEPSEEK: PEAK_OFFPEAK_COUNTERS,
    Provider.GITHUB: frozenset(),  # per-unit billing: a logged GitHub request carries a cost, never token counters
}
_TIER_FAMILY: Mapping[Provider, frozenset[str]] = {  # what a token-tier entry of a built-in provider reads
    Provider.ANTHROPIC: ANTHROPIC_COUNTERS,
    Provider.GOOGLE: GOOGLE_COUNTERS,
    Provider.OPENAI: OPENAI_COUNTERS,
    Provider.XAI: OPENAI_COUNTERS,
    Provider.DEEPSEEK: OPENAI_COUNTERS,  # its chat API is OpenAI-compatible: a tiers-priced DeepSeek model reads those
}
DEEPSEEK_NATIVE = frozenset(
    {"cache_hit", "cache_miss"}
)  # what the DeepSeek API answers with, whatever the entry's shape


def folds_to_tiers(provider: Provider, entry: PriceEntry | None) -> bool:
    """Whether a row's native DeepSeek counters are read as the token-tier family (a tiers-priced entry)."""
    return provider == Provider.DEEPSEEK and isinstance(entry, TokenTierPrice)


def as_tier_family(counters: Mapping[str, int]) -> dict[str, int]:
    """DeepSeek's ``cache_hit`` / ``cache_miss`` said as ``input`` (hit + miss) and ``cached_input`` (hit).

    Both spellings on one line are a ``ValueError``: they are two representations of the same usage, and keeping one
    silently would drop the other's tokens. The writer (``log.validated``) and the reader (``usage_log.parse_line``)
    apply this one rule.
    """
    if not DEEPSEEK_NATIVE & set(counters):
        return dict(counters)
    if {"input", "cached_input"} & set(counters):
        raise ValueError(
            "cache_hit/cache_miss and input/cached_input are two spellings of the same usage: give one"
        )
    rest = {name: value for name, value in counters.items() if name not in DEEPSEEK_NATIVE}
    hit, miss = counters.get("cache_hit", 0), counters.get("cache_miss", 0)
    return {**rest, "input": hit + miss, "cached_input": hit}


_BY_SHAPE: Mapping[type, frozenset[str]] = {  # an entry's shape decides when it is not a token-tier one
    PeakOffpeakPrice: PEAK_OFFPEAK_COUNTERS,
    PerUnitPrice: frozenset(),  # per-unit entries count reviews and minutes, never per-request tokens
}


def counters_of(provider: Provider, entry: PriceEntry | None = None) -> frozenset[str] | None:
    """The per-request counters the pricing of a row reads.

    By the shape of its price entry when the pricebook has one (a user-added DeepSeek model with token tiers reads
    ``input`` / ``cached_input``; an Anthropic entry reads cache reads and writes, an OpenAI one ``cached_input`` —
    a counter the other family names would carry a zero rate), else by the provider's built-in formula; ``None``
    when neither says (an unlisted model of a user-added provider: nothing is checked). A usage-log line (or ``ai-cost log``) that names another
    counter would be priced without it: the reader makes such a line a counted skip and the writer refuses it.
    """
    if (
        provider == Provider.GITHUB
    ):  # entry_for bills every GitHub row per unit, whatever a prices file lists for it
        return frozenset()
    if isinstance(entry, TokenTierPrice):
        return _TIER_FAMILY.get(provider, TOKEN_TIER_COUNTERS)
    return _BY_SHAPE.get(type(entry), _COUNTERS_OF.get(provider))


def price_token_tiers(row: UsageRow, entry: PriceEntry, book: PriceBook, config: Config) -> Money:
    """Token-tier models.

    Anthropic: input / cache write 5m,1h / cache read / output. OpenAI and xAI: input / cached / output.
    Google: prompt / cached / output.
    """
    assert isinstance(entry, TokenTierPrice)
    tokens = normalize(row.tokens, config.cache_ttl_default)
    price = _tier(current_price(entry, row.at), tokens)
    if row.provider == Provider.GOOGLE:
        uncached = max(0, tokens.prompt - tokens.cached)
        usd = (
            uncached * price.input + tokens.cached * price.cached_input + tokens.output * price.output
        ) / MILLION
        return Money(usd)
    uncached = max(0, tokens.input - tokens.cached_input)
    usd = (
        uncached * price.input
        + tokens.cached_input * price.cached_input
        + tokens.cache_read * price.cache_read
        + tokens.cache_write_5m * price.cache_write_5m
        + tokens.cache_write_1h * price.cache_write_1h
        + tokens.output * price.output
    ) / MILLION
    usd += tokens.web_search * book.github.web_search_per_1000 / 1000.0
    return Money(usd)


def is_peak(at: datetime | None, peak_hours_utc: tuple[tuple[int, int], ...]) -> bool:
    """DeepSeek peak: Mon–Fri inside the configured UTC windows (holidays not modelled)."""
    if at is None or at.weekday() >= 5:
        return False
    return any(start <= at.hour < end for start, end in peak_hours_utc)


def price_peak_offpeak(row: UsageRow, entry: PriceEntry, book: PriceBook, config: Config) -> Money:
    """DeepSeek: cache hit / miss / output at the tariff of the call's hour."""
    assert isinstance(entry, PeakOffpeakPrice)
    peak = is_peak(row.at, book.peak_hours_utc)
    rate: Rate = entry.peak if peak else entry.offpeak
    tokens = row.tokens
    usd = (
        tokens.cache_hit * rate.cache_hit + tokens.cache_miss * rate.cache_miss + tokens.output * rate.output
    ) / MILLION
    return Money(usd, "peak" if peak else "off-peak")


REPORTED_ON_THE_BILL = "a count: the amount is in the GitHub usage report (providers.github.bill)"
NO_PRICE = "a count: a Copilot review has no price — providers.github.bill reads the amounts"


def on_the_bill(row: UsageRow) -> bool:
    """A GitHub row whose amount the read usage report holds (``settled_by`` marked it ``API_SETTLED``): a count."""
    return row.provider == Provider.GITHUB and row.invoice is None and row.billing is Billing.API_SETTLED


def _minutes_said(name: str, minutes: float, rates: Mapping[str, float]) -> str:
    if name in rates:
        return f"{name} {minutes:.0f} min"
    if name == ELAPSED_RUNNER:
        return f"{minutes:.0f} min elapsed without a billable time (no /timing): not priced"
    return f"{name} {minutes:.0f} min: no list price for this runner, not priced"


def _minutes_price(by_os: Mapping[str, float], rates: Mapping[str, float]) -> Money:
    """Billable minutes of a runner the price list names; other minutes (an elapsed time, an unknown runner) unpriced."""
    usd = sum(minutes * rates[name] for name, minutes in by_os.items() if name in rates)
    return Money(usd, ", ".join(_minutes_said(name, minutes, rates) for name, minutes in by_os.items()))


def price_per_unit(row: UsageRow, entry: PriceEntry, book: PriceBook, config: Config) -> Money:
    """GitHub counts: a Copilot review has no price (ADR-0007); Actions billable minutes × the per-runner list price."""
    assert isinstance(entry, PerUnitPrice)
    tokens = row.tokens
    if row.kind is RowKind.COPILOT:
        return Money(0.0, REPORTED_ON_THE_BILL if config.github.bill is not None else NO_PRICE)
    if not tokens.billable and entry.public_repos_free:
        return Money(0.0, "public repo, minutes free")
    rates = entry.usd_per_minute
    if tokens.by_os:
        return _minutes_price(tokens.by_os, rates)
    return _minutes_price({config.github.actions_runner: tokens.minutes}, rates)


PRICERS: Mapping[type, Pricer] = {
    TokenTierPrice: price_token_tiers,
    PeakOffpeakPrice: price_peak_offpeak,
    PerUnitPrice: price_per_unit,
}


def entry_for(row: UsageRow, book: PriceBook) -> PriceEntry | None:
    """The price entry a row is billed with; GitHub rows share one per-unit entry."""
    if row.provider == Provider.GITHUB:
        return book.github
    return book.entry(row.provider, row.model)


def _own_figure(row: UsageRow, config: Config) -> Money | None:
    """A row whose amount is not a list price: a usage-report line, a GitHub count under a report, a cost-only line."""
    if row.invoice is not None:
        return Money(row.invoice.gross, "usage report, gross")
    if on_the_bill(row):
        return Money(0.0, REPORTED_ON_THE_BILL)
    if row.cost_reported is not None and row.tokens == Tokens():  # a cost-only line: nothing to price at list
        return Money(float(row.cost_reported), "reported charge (no counters)")
    return None


def price(row: UsageRow, book: PriceBook, config: Config) -> Money:
    """List price of one row; reported cost when unpriced or when the row has no counters to price; else ``PricingError``.

    A usage-report line is its own list price: the gross amount, before anything a plan includes (ADR-0007).
    """
    own = _own_figure(row, config)
    if own is not None:
        return own
    entry = entry_for(row, book)
    if entry is None:
        if row.cost_reported is not None:
            return Money(float(row.cost_reported), "no list price — CLI-reported cost")
        raise PricingError(
            f"no price for {row.provider.value}/{row.model} and no reported cost (add it to prices.json)"
        )
    return PRICERS[type(entry)](row, entry, book, config)
