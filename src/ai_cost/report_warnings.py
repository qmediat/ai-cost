"""The report's header warnings: the config's state, retired keys, billing that leaves rows out of the real group."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .config import Config, Paths, PriceBook
from .groups import (
    INVOICE_ATTRIBUTION,
    RealGroup,
)
from .models import (
    RowKind,
    UsageRow,
)
from .onboarding import (
    BILL_HINT,
    billing_hint,
    outside_scope,
    paid_for,
    provider_use,
)


def config_warnings(paths: Paths, config: Config, book: PriceBook) -> list[str]:
    """The retired keys, then the config: without a user one the package defaults; with one, a plan per plan rule."""
    return [*_retired(paths, config, book), *_config_state(paths, config, book)]


def _config_state(paths: Paths, config: Config, book: PriceBook) -> list[str]:
    if paths.instance is not None and not paths.instance.root.is_dir():
        return [
            f"instance {paths.instance.root} does not exist yet: nothing is recorded (ai-cost instance init)"
        ]
    if paths.user_config_file().exists():
        on_plan = sorted(name for name, rule in config.billing_rules.items() if rule == "subscription")
        return [
            f"providers.{name}.billing = subscription but no subscription covers {name}: those plan rows cost "
            f"nothing here — add the plan to {paths.user_config_file()}"
            for name in on_plan
            if not paid_for(name, config, book)
        ]
    # an app that pays per token configures nothing: for an instance the shipped defaults are its config
    return [] if paths.instance is not None else _no_user_config(config)


def _no_user_config(config: Config) -> list[str]:
    """A user without a config: the package's defaults, and the command that writes one."""
    if not config.subscriptions:
        return [
            "no user config — no subscriptions configured (the package ships none); run `ai-cost install --init-config` and add yours"
        ]
    plans = ", ".join(s.plan for s in config.subscriptions)
    return [f"no user config — subscriptions are the defaults ({plans}); run `ai-cost install --init-config`"]


def _retired(paths: Paths, config: Config, book: PriceBook) -> list[str]:
    """The keys the user's files still set that 2.6 no longer reads: a Copilot review has no price (ADR-0007)."""
    said = [
        f"{key} is no longer read (a Copilot review has no price since 2.6): {BILL_HINT}"
        for key in config.github.retired
    ]
    return said + [
        f"{key} in {paths.user_prices_file()} is no longer read (a Copilot review has no price since 2.6) — remove it"
        for key in book.retired
    ]


def billing_warnings(
    real: RealGroup | None, rows: Sequence[UsageRow], config: Config, one_session: bool = False
) -> list[str]:
    """Copilot counts without a usage report, and rows of unknown billing (priced by the API group only).

    Those of clients outside scope are said as such; the rest are counted per provider with the rule to set.
    """
    counted = _copilot_counted(rows, config, one_session) + _plans_beside_seats(real)
    if real is None or not real.unknown_billing:
        return counted
    return counted + _outside_warning(outside_scope(rows, config)) + _unknown_warning(real, rows, config)


def _plans_beside_seats(real: RealGroup | None) -> list[str]:
    """A configured GitHub plan still booked while the usage report states seats: said, the user decides."""
    shares = real.subscriptions if real is not None else ()
    seats = sorted({s.plan for s in shares if s.attribution == INVOICE_ATTRIBUTION})
    kept = sorted(
        s.plan for s in shares if s.provider == "github" and s.attribution != INVOICE_ATTRIBUTION and s.usd
    )
    if not seats or not kept:
        return []
    return [
        f"subscription {', '.join(kept)} stays booked beside the seats the GitHub usage report bills "
        f"({', '.join(seats)}) — remove it from subscriptions if those seats replaced it"
    ]


def _copilot_counted(rows: Sequence[UsageRow], config: Config, one_session: bool = False) -> list[str]:
    """Copilot reviews without a usage report: counted, never priced — said, so a total is never read as complete.

    A session report reads no usage report (it bills per repository and day, not per session): its reviews are said too.
    """
    reviews = sum(row.tokens.reviews for row in rows if row.kind is RowKind.COPILOT)
    if not reviews or (config.github.bill is not None and not one_session):
        return []
    if one_session:
        return [
            f"{reviews} Copilot review(s) counted, not priced: the GitHub usage report bills a repository's day, "
            "not a session — its amount is in `report --since/--until` for those days"
        ]
    return [f"{reviews} Copilot review(s) counted, not priced (a review has no price) — {BILL_HINT}"]


def _outside_warning(clients: Mapping[str, int]) -> list[str]:
    if not clients:
        return []
    return [
        f"{sum(clients.values())} usage row(s) from clients outside scope ({', '.join(sorted(clients))}) have "
        "unknown billing and stay out of the real group on purpose (outside_scope_clients); the API-only group "
        "still prices their tokens"
    ]


def _unknown_warning(real: RealGroup, rows: Sequence[UsageRow], config: Config) -> list[str]:
    """The unknown rows a rule or a plugin could place, per provider, and the ledger rows without a figure."""
    outside = {use.provider: use.outside for use in provider_use(rows, config.outside_scope_clients)}
    left = {name: n - outside.get(name, 0) for name, n in sorted(real.unknown_by_provider.items())}
    left = {name: n for name, n in left.items() if n}
    unfigured = real.unfigured_ledger
    if not left and not unfigured:
        return []
    counts = [f"{name} {n}" for name, n in left.items()]
    counts += [f"{unfigured} ledger row(s) without a figure, counted nowhere else"] if unfigured else []
    rules = "; ".join(f"providers.{name}.billing — {billing_hint(name)}" for name in left)
    fix = f"set {rules}; or add" if left else "add"
    return [
        f"{sum(left.values()) + unfigured} usage row(s) with unknown billing are left out of the real group "
        f"({', '.join(counts)}; the API group prices the ones with tokens) — {fix} a plugin that knows how those "
        f"sessions were paid{_booked_by_report(left, rows)}"
    ]


def _booked_by_report(left: Mapping[str, int], rows: Sequence[UsageRow]) -> str:
    """The providers whose compared days put the charge of those rows into an untracked row: said, never hidden."""
    booked = sorted({row.provider.value for row in rows if row.kind is RowKind.UNTRACKED} & set(left))
    if not booked:
        return ""
    return f"; on the days a provider report compared ({', '.join(booked)}), their charge is in its untracked row"
