"""Attribute every priced row, whole, to a label: by its checkout scope first, by the paths it touched second.

The rules are ADR-0003. A row never splits between labels; what does not match lands in ``mixed`` (several labels)
or ``unattributed`` (none), and both are always reported with the keys they absorbed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config, PriceBook
from .errors import UsageError
from .groups import (
    AttributionGroup,
    AttributionLine,
    api_group,
    invoice_shares,
    real_group,
    subscription_shares,
)
from .models import Provider, Scope, TokenTierPrice, UsageRow, Window
from .pricing import current_price, entry_for

MIXED = "mixed"
UNATTRIBUTED = "unattributed"
RESERVED = frozenset({MIXED, UNATTRIBUTED})
POLICY = (
    "a row is attributed whole: by branch / workspace / PR when one label matches those keys (several → mixed), "
    "else by the label that matches most of the paths the turn touched (tie → mixed, none → unattributed); "
    "subscription shares follow each label's API-equivalent share of the plan's provider"
)


@dataclass(frozen=True)
class Rule:
    """One ``LABEL=REGEX`` from the command line."""

    label: str
    pattern: re.Pattern[str]


def parse_rules(specs: Sequence[str]) -> tuple[Rule, ...]:
    """``LABEL=REGEX`` specs → rules; a malformed spec, a bad regex, a reserved or repeated label is a usage error."""
    rules: list[Rule] = []
    for spec in specs:
        label, sep, regex = spec.partition("=")
        if not sep or not label or not regex:
            raise UsageError(f"--attribute: expected LABEL=REGEX, got {spec!r}")
        if label in RESERVED or any(rule.label == label for rule in rules):
            raise UsageError(f"--attribute: label {label!r} is reserved or repeated")
        try:
            rules.append(Rule(label, re.compile(regex)))
        except re.error as exc:
            raise UsageError(f"--attribute {label}: bad regex ({exc})") from exc
    return tuple(rules)


def label_for(scope: Scope, rules: Sequence[Rule]) -> str:
    """The label of one row (ADR-0003): identity keys decide; paths only when no key matched."""
    by_key = {rule.label for rule in rules for key in scope.identity() if rule.pattern.search(key)}
    if len(by_key) == 1:
        return by_key.pop()
    if by_key:
        return MIXED
    votes = {rule.label: sum(1 for segment in scope.paths if rule.pattern.search(segment)) for rule in rules}
    best = max(votes.values(), default=0)
    if best == 0:
        return UNATTRIBUTED
    winners = [label for label, count in votes.items() if count == best]
    return winners[0] if len(winners) == 1 else MIXED


def _context_usd(rows: Sequence[UsageRow], book: PriceBook) -> float:
    """What the label paid for cache reads: the context a turn inherited, not the work it did."""
    total = 0.0
    for row in rows:
        if row.provider != Provider.ANTHROPIC or not row.tokens.cache_read:
            continue
        entry = entry_for(row, book)
        if isinstance(entry, TokenTierPrice):
            total += row.tokens.cache_read * current_price(entry, row.at).cache_read / 1e6
    return total


def _subscription_split(
    labelled: dict[str, list[UsageRow]], book: PriceBook, config: Config, window: Window
) -> dict[str, float]:
    """Split each plan's window share between the labels.

    In proportion to their API-equivalent cost of the plan's provider; a provider nobody was labelled for lands
    whole on ``unattributed``.
    """
    by_label_provider: dict[str, dict[str, float]] = {}
    for label, rows in labelled.items():
        weights_of = by_label_provider.setdefault(label, {})
        for line in api_group(
            rows, book, config
        ).lines:  # one line per model: every model of a provider counts
            weights_of[line.provider.value] = weights_of.get(line.provider.value, 0.0) + line.usd
    out = dict.fromkeys(labelled, 0.0)
    every_row = [row for rows in labelled.values() for row in rows]
    for share in subscription_shares(config, book, window, every_row) + invoice_shares(every_row):
        weights = {label: usd.get(share.provider, 0.0) for label, usd in by_label_provider.items()}
        total = sum(weights.values())
        if total <= 0:
            out[UNATTRIBUTED] += share.usd
            continue
        for label, weight in weights.items():
            out[label] += share.usd * weight / total
    return out


def attribution_group(
    rows: Sequence[UsageRow], rules: Sequence[Rule], book: PriceBook, config: Config, window: Window
) -> AttributionGroup:
    """One line per label (rules, then ``mixed``, then ``unattributed``); the lines sum to the api/real totals."""
    labelled: dict[str, list[UsageRow]] = {rule.label: [] for rule in rules}
    labelled[MIXED] = []
    labelled[UNATTRIBUTED] = []
    for row in rows:
        labelled[label_for(row.scope, rules)].append(row)
    subscriptions = _subscription_split(labelled, book, config, window)
    lines = []
    for label, subset in labelled.items():
        api = api_group(subset, book, config)
        real = real_group(subset, book, config, window)
        context = _context_usd(subset, book)
        lines.append(
            AttributionLine(
                label=label,
                calls=sum(line.calls for line in api.lines),
                api_usd=api.total_usd,
                usd_context=context,
                cash_usd=real.cash_usd,
                subscription_usd=subscriptions[label],
                keys=tuple(sorted({key for row in subset for key in row.scope.identity()})),
            )
        )
    return AttributionGroup(policy=POLICY, lines=lines)
