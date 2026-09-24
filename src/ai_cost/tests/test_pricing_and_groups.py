"""Money: every price shape, the tiers, the peak calendar, the three groups and the vendor arithmetic."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config, PriceBook, Subscription
from ..errors import ConfigError, PricingError
from ..groups import api_group, real_group, subscription_shares, vendor_group
from ..models import Billing, Provider, RowKind, Size, Tokens, UsageRow, WorkItem
from ..pricing import is_peak, price
from ..timeutil import parse_ts
from .fixtures import BASE, WINDOW, defaults, paths_in

MILLION = 1_000_000


def _row(
    provider: Provider,
    model: str,
    tokens: Tokens,
    kind: RowKind = RowKind.REVIEW,
    billing: Billing = Billing.API,
    at: datetime = BASE,
    cost: float | None = None,
) -> UsageRow:
    return UsageRow(provider, model, kind, at, "t", billing, tokens, cost)


def _setup(tmp_path: Path) -> tuple[Config, PriceBook]:
    return defaults(paths_in(tmp_path))


def test_fable_pricing_with_cache_tiers(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    row = _row(
        Provider.ANTHROPIC,
        "claude-fable-5-1",
        Tokens(input=1000, output=2000, cache_read=MILLION, cache_write_1h=100_000),
        RowKind.TRANSCRIPT,
    )
    expected = (1000 * 10 + 2000 * 50 + MILLION * 0.25 + 100_000 * 20) / MILLION
    assert abs(price(row, book, config).usd - expected) < 1e-9


def test_web_search_and_5m_cache_write(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    row = _row(
        Provider.ANTHROPIC,
        "claude-fable-5-1",
        Tokens(output=500, cache_write_5m=40_000, web_search=2),
        RowKind.TRANSCRIPT,
    )
    assert abs(price(row, book, config).usd - ((500 * 50 + 40_000 * 12.5) / MILLION + 2 * 10 / 1000)) < 1e-9


def test_unsplit_cache_write_follows_the_configured_ttl(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    row = _row(
        Provider.ANTHROPIC,
        "claude-fable-5-1",
        Tokens(input=10, output=10, cache_write_unsplit=1000),
        RowKind.TRANSCRIPT,
    )
    assert abs(price(row, book, config).usd - (10 * 10 + 10 * 50 + 1000 * 20) / MILLION) < 1e-9
    five = Config(**{**config.__dict__, "cache_ttl_default": "5m"})
    assert abs(price(row, book, five).usd - (10 * 10 + 10 * 50 + 1000 * 12.5) / MILLION) < 1e-9


def test_dated_model_id_resolves_to_its_prefix(tmp_path: Path) -> None:
    _, book = _setup(tmp_path)
    assert book.entry(Provider.ANTHROPIC, "claude-opus-5-20261001") is book.entry(
        Provider.ANTHROPIC, "claude-opus-5"
    )


def test_openai_cached_input(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    row = _row(
        Provider.OPENAI,
        "gpt-6-astra",
        Tokens(input=500_000, cached_input=400_000, output=9_500),
        RowKind.SESSION,
    )
    assert (
        abs(price(row, book, config).usd - ((500_000 - 400_000) * 10 + 400_000 * 1 + 9_500 * 50) / MILLION)
        < 1e-9
    )


def test_gemini_cache_tier_and_long_context_only_for_single_requests(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    flash = _row(
        Provider.GOOGLE,
        "gemini-3.8-flash",
        Tokens(prompt=3_000_000, cached=2_800_000, output=50_400, requests=1),
    )
    assert (
        abs(
            price(flash, book, config).usd
            - ((3_000_000 - 2_800_000) * 0.75 + 2_800_000 * 0.075 + 50_400 * 3.75) / MILLION
        )
        < 1e-9
    )
    single = _row(Provider.GOOGLE, "gemini-3.1-pro-preview", Tokens(prompt=300_000, output=1000, requests=1))
    many = _row(Provider.GOOGLE, "gemini-3.1-pro-preview", Tokens(prompt=300_000, output=1000, requests=7))
    assert (
        price(single, book, config).usd > price(many, book, config).usd
    ), "aggregated short requests never pay the long tier"


def test_next_price_applies_from_its_date(tmp_path: Path) -> None:
    """A registry entry with ``next`` switches on ``from`` (or the day after ``valid_until``), never before."""
    config, book = _setup(tmp_path)
    entry = book.entry(Provider.GOOGLE, "gemini-3.8-flash")
    assert (
        entry is not None and getattr(entry, "next", None) is not None
    ), "the fixture registry carries a next block"
    tokens = Tokens(prompt=MILLION, requests=1)
    before = _row(Provider.GOOGLE, "gemini-3.8-flash", tokens, at=datetime(2026, 9, 19, tzinfo=timezone.utc))
    after = _row(Provider.GOOGLE, "gemini-3.8-flash", tokens, at=datetime(2027, 1, 5, tzinfo=timezone.utc))
    assert abs(price(before, book, config).usd - 0.75) < 1e-9
    assert abs(price(after, book, config).usd - 1.5) < 1e-9


def test_seats_multiply_only_per_seat_plans(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    per_seat = next(name for name, plan in book.plans.items() if plan.per_seat)
    flat = next(name for name, plan in book.plans.items() if not plan.per_seat)
    two = Config(
        **{
            **config.__dict__,
            "subscriptions": (
                Subscription(per_seat, seats=2, attribution="full"),
                Subscription(flat, seats=2, attribution="full"),
            ),
        }
    )
    shares = {s.plan: s.monthly_usd for s in subscription_shares(two, book, WINDOW)}
    assert shares[per_seat] == book.plans[per_seat].monthly_usd * 2
    assert (
        shares[flat] == book.plans[flat].monthly_usd
    ), "a flat plan costs the same however many seats are typed"


def test_unknown_billing_rows_stay_out_of_the_real_group(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    row = _row(Provider.OPENAI, "gpt-6-astra", Tokens(input=10, output=10), RowKind.SESSION, Billing.UNKNOWN)
    group = real_group([row], book, config, WINDOW)
    assert group.usage == [] and group.unknown_billing == 1


def test_grok_long_tier(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    row = _row(Provider.XAI, "grok-4.6", Tokens(input=250_000, cached_input=500, output=30_000))
    assert (
        abs(price(row, book, config).usd - ((250_000 - 500) * 4.0 + 500 * 1.0 + 30_000 * 12.0) / MILLION)
        < 1e-9
    )


def test_qwen_flat_rate_and_the_256k_long_tier(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    flat = _row(Provider.ALIBABA, "qwen3.8-max", Tokens(input=300_000, cached_input=20_000, output=10_000))
    assert (
        abs(
            price(flat, book, config).usd - ((300_000 - 20_000) * 2.0 + 20_000 * 0.2 + 10_000 * 6.0) / MILLION
        )
        < 1e-9
    )
    long_ = _row(Provider.ALIBABA, "qwen3.7-plus", Tokens(input=300_000, cached_input=0, output=10_000))
    assert abs(price(long_, book, config).usd - (300_000 * 1.2 + 10_000 * 4.8) / MILLION) < 1e-9
    short = _row(Provider.ALIBABA, "qwen3.7-plus", Tokens(input=200_000, cached_input=0, output=10_000))
    assert abs(price(short, book, config).usd - (200_000 * 0.4 + 10_000 * 1.6) / MILLION) < 1e-9


def test_deepseek_peak_calendar(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    assert is_peak(parse_ts("2026-09-22T02:30:00Z"), book.peak_hours_utc)
    assert not is_peak(parse_ts("2026-09-22T05:00:00Z"), book.peak_hours_utc)
    assert not is_peak(parse_ts("2026-09-20T02:30:00Z"), book.peak_hours_utc), "weekend is off-peak"
    row = _row(Provider.DEEPSEEK, "deepseek-flash", Tokens(cache_hit=1_000, cache_miss=89_000, output=40_000))
    money = price(row, book, config)
    assert (
        money.note == "off-peak"
        and abs(money.usd - (1_000 * 0.003 + 89_000 * 0.15 + 40_000 * 0.6) / MILLION) < 1e-9
    )


def test_copilot_and_actions_per_unit(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    reviews = _row(
        Provider.GITHUB, "copilot-code-review", Tokens(reviews=3), RowKind.COPILOT, Billing.SUBSCRIPTION
    )
    assert abs(price(reviews, book, config).usd - 3 * 13 * 0.04) < 1e-9
    private = _row(
        Provider.GITHUB,
        "actions",
        Tokens(by_os={"linux": 3.5, "macos": 1.0, "windows": 2.0}, billable=True),
        RowKind.ACTIONS,
        Billing.SUBSCRIPTION,
    )
    assert abs(price(private, book, config).usd - (3.5 * 0.006 + 1.0 * 0.062 + 2.0 * 0.01)) < 1e-9
    public = _row(
        Provider.GITHUB, "actions", Tokens(minutes=100, billable=False), RowKind.ACTIONS, Billing.SUBSCRIPTION
    )
    assert price(public, book, config).usd == 0


def test_unpriced_model_uses_reported_cost_or_fails_loud(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    reported = _row(Provider.XAI, "grok-9", Tokens(input=1), cost=0.42)
    assert price(reported, book, config).usd == 0.42
    try:
        price(_row(Provider.XAI, "grok-9", Tokens(input=1)), book, config)
    except PricingError as exc:
        assert exc.code == 5 and "grok-9" in str(exc)
    else:
        raise AssertionError("an unpriced row without a reported cost must fail loud")


def _rows() -> list[UsageRow]:
    return [
        _row(
            Provider.ANTHROPIC,
            "claude-fable-5-1",
            Tokens(input=1000, output=2000),
            RowKind.TRANSCRIPT,
            Billing.SUBSCRIPTION,
        ),
        _row(
            Provider.OPENAI,
            "gpt-6-astra",
            Tokens(input=500_000, cached_input=400_000, output=9_500),
            RowKind.SESSION,
            Billing.SUBSCRIPTION,
        ),
        _row(
            Provider.OPENAI,
            "gpt-6-astra",
            Tokens(input=60_000, output=400),
            RowKind.SESSION,
            Billing.API,
            cost=0.62,
        ),
        _row(
            Provider.OPENAI,
            "gpt-6-astra",
            Tokens(input=60_000, output=400),
            RowKind.LEDGER,
            Billing.API,
            cost=0.62,
        ),
        _row(Provider.XAI, "grok-4.6", Tokens(input=250_000, cached_input=500, output=30_000), cost=0.51),
        _row(
            Provider.GITHUB, "copilot-code-review", Tokens(reviews=3), RowKind.COPILOT, Billing.SUBSCRIPTION
        ),
    ]


def test_api_group_prices_every_call_and_skips_the_ledger(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    group = api_group(_rows(), book, config)
    openai = next(line for line in group.lines if line.provider == Provider.OPENAI)
    assert openai.calls == 2, "rollouts carry every Codex call; the ledger would double count"
    copilot = next(line for line in group.lines if line.provider == Provider.GITHUB)
    assert copilot.calls == 3 and abs(copilot.usd - 3 * 13 * 0.04) < 1e-9


def test_real_group_rules(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    group = real_group(_rows(), book, config, WINDOW)
    by_label = {(line.provider, line.label): line for line in group.usage}
    assert by_label[(Provider.ANTHROPIC, "plan")].usd == 0
    assert (
        by_label[(Provider.OPENAI, "plan")].usd == 0
        and by_label[(Provider.OPENAI, "api-key (ledger)")].usd == 0.62
    )
    assert abs(by_label[(Provider.XAI, "grok-4.6")].usd - 0.51) < 1e-9, "Grok uses the CLI-reported cost"
    assert by_label[(Provider.GITHUB, "copilot-code-review")].usd == 0, "within the plan allowance"
    assert abs(group.subscriptions[0].usd - 200 * (61 / 60) / 730) < 1e-9, "prorated by time"


def test_real_group_config_switches(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    switched = Config(
        **{
            **config.__dict__,
            "subscriptions": (Subscription("claude-pro", seats=2, attribution="full"),),
            "github": config.github.__class__(copilot_plan_exhausted=True),
        }
    )
    group = real_group(_rows(), book, switched, WINDOW)
    assert next(line for line in group.usage if line.provider == Provider.GITHUB).usd > 0
    flat_or_seat = book.plans["claude-pro"].per_seat
    assert group.subscription_usd == (40 if flat_or_seat else 20), "seats count only on a per-seat plan"
    assert subscription_shares(switched, book, WINDOW)[0].attribution == "full"


def test_api_total_exceeds_real_cash(tmp_path: Path) -> None:
    config, book = _setup(tmp_path)
    assert api_group(_rows(), book, config).total_usd > real_group(_rows(), book, config, WINDOW).cash_usd


def test_unknown_vendor_profile_is_a_config_error(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    try:
        vendor_group([WorkItem("a", "", Size.S)], config, "typo")
    except ConfigError as exc:
        assert exc.code == 2 and "typo" in str(exc) and "consultancy-eu" in str(exc)
    else:
        raise AssertionError("an unknown profile must be a ConfigError, not a KeyError")


def test_vendor_arithmetic(tmp_path: Path) -> None:
    config, _ = _setup(tmp_path)
    items = [WorkItem("a", "", Size.S), WorkItem("b", "", Size.XS), WorkItem("c", "", Size.M)]
    group = vendor_group(items, config, "consultancy-eu")
    assert group.base_hours == (12.0, 24.0)
    senior = next(o for o in group.options if o.staffing == "senior")
    assert senior.hours == (16.0, 32.0), "12..24 × 1.25 packaged to 16/32 h"
    assert senior.cost == (2400.0, 4800.0), "at 150/h"
    junior = next(o for o in group.options if o.staffing == "junior")
    assert junior.hours == (36.0, 68.0) and junior.blended_rate == 86.0 and junior.cost == (3096.0, 5848.0)
    assert (
        vendor_group([WorkItem("a", "", Size.XS)], config, "consultancy-eu").options[2].hours[0] == 8
    ), "package minimum"


def test_a_plugin_provider_listed_in_the_prices_file_is_priced_at_list_never_a_key_error(
    tmp_path: Path,
) -> None:
    from ..config import builtin_prices, parse_pricebook
    from ..models import Billing, Provider, RowKind, Scope, Tokens, UsageRow
    from ..render import plain

    over = {"providers": {"acme": {"models": {"acme-1": {"input": 2.0, "output": 6.0}}}}}
    book = parse_pricebook(builtin_prices(), over, "prices")
    acme = Provider.of("acme")
    assert acme in book.models and book.entry(acme, "acme-1") is not None, "the pricebook is the registry"
    row = UsageRow(
        provider=acme,
        model="acme-1",
        kind=RowKind.LOG,
        source="acme-plugin",
        at=BASE,
        ref="req-1",
        billing=Billing.API,
        tokens=Tokens(input=1_000_000, output=1_000_000),
        scope=Scope(),
    )
    config, _ = defaults(paths_in(tmp_path))
    api = {line.label: line for line in api_group([row], book, config).lines}
    assert abs(api["acme-1"].usd - 8.0) < 1e-9, "priced from the user's entry"
    real = real_group([row], book, config, WINDOW)
    assert (
        abs(real.cash_usd - 8.0) < 1e-9
    ), "an unknown provider's key-paid row is cash at list, not a KeyError"
    assert plain(row)["provider"] == "acme", "serialised as its id, like the built-in six"


def test_a_provider_id_must_be_a_plain_lowercase_token() -> None:
    from ..config import builtin_prices, parse_pricebook
    from ..errors import ConfigError
    from ..models import Provider

    for bad in ("Acme", "acme corp", "", "a" * 65):
        try:
            Provider.of(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must not be a provider id")
    try:
        parse_pricebook(builtin_prices(), {"providers": {"Acme": {}}}, "prices")
    except ConfigError as exc:
        assert "providers.Acme" in str(exc)
    else:
        raise AssertionError("a bad provider key in the prices file must be a ConfigError")


def test_a_plugin_providers_plan_row_and_ledger_row_are_priced_like_anyone_elses(tmp_path: Path) -> None:
    from ..config import builtin_prices, parse_pricebook
    from ..models import Billing, Provider, RowKind, Scope, Tokens, UsageRow

    book = parse_pricebook(
        builtin_prices(),
        {"providers": {"acme": {"models": {"acme-1": {"input": 2.0, "output": 6.0}}}}},
        "prices",
    )
    config, _ = defaults(paths_in(tmp_path))
    acme = Provider.of("acme")
    base: dict[str, Any] = {
        "provider": acme,
        "model": "acme-1",
        "source": "acme-plugin",
        "at": BASE,
        "scope": Scope(),
        "tokens": Tokens(input=1_000_000),
    }
    plan_row = UsageRow(kind=RowKind.LOG, ref="r1", billing=Billing.SUBSCRIPTION, **base)
    ledger_row = UsageRow(kind=RowKind.LEDGER, ref="r2", billing=Billing.API, cost_reported=0.75, **base)
    real = real_group([plan_row, ledger_row], book, config, WINDOW)
    labels = {line.label: line.usd for line in real.usage}
    assert labels.get("plan") == 0.0, "a plan-billed row of any provider is the plan's, never cash at list"
    assert abs(real.cash_usd - 0.75) < 1e-9, "a ledger row is cash by its own figure, whoever the provider is"


def test_a_provider_survives_copies_and_pickles_as_the_same_instance() -> None:
    import copy
    import pickle

    assert copy.deepcopy(Provider.OPENAI) is Provider.OPENAI
    assert pickle.loads(pickle.dumps(Provider.of("acme"))) is Provider.of("acme")
    assert copy.deepcopy({Provider.GOOGLE: 1}) == {Provider.GOOGLE: 1}, "a dict keyed by providers copies"


def test_the_xai_rule_follows_the_row_and_trusts_an_apps_own_cost(tmp_path: Path) -> None:
    from dataclasses import replace

    from ..groups import real_rule
    from .fixtures import WINDOW, defaults, paths_in

    config, book = defaults(paths_in(tmp_path))
    logged = UsageRow(
        provider=Provider.XAI,
        model="grok-4.6",
        kind=RowKind.LOG,
        source="usage-log",
        at=WINDOW.start,
        ref="job",
        billing=Billing.API,
        tokens=Tokens(input=1000, output=100),
        cost_reported=0.3,
    )
    rule = real_rule(Provider.XAI)
    assert rule(replace(logged, billing=Billing.SUBSCRIPTION), book, config)[0] == "plan"
    distrust = replace(config, xai_trust_cli_cost=False)
    assert (
        rule(logged, book, distrust)[1].usd == 0.3
    ), "an app's own figure is what it paid, whatever the CLI flag"
    estimate = replace(logged, kind=RowKind.REVIEW)
    assert rule(estimate, book, config)[1].usd == 0.3, "the review CLI's estimate when trusted"
    assert rule(estimate, book, distrust)[1].usd != 0.3, "the list price when not"


def test_a_ledger_row_without_a_figure_is_unknown_and_a_github_row_follows_its_billing(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from ..groups import real_group, real_rule
    from .fixtures import WINDOW, defaults, paths_in

    config, book = defaults(paths_in(tmp_path))
    ledger = UsageRow(
        provider=Provider.of("acme"),
        model="m",
        kind=RowKind.LEDGER,
        source="acme-ledger",
        at=WINDOW.start,
        ref="call-1",
        billing=Billing.API,
        tokens=Tokens(),
        cost_reported=None,
    )
    real = real_group([ledger, replace(ledger, cost_reported=0.4)], book, config, WINDOW)
    assert real.unknown_billing == 1 and [line.usd for line in real.usage] == [
        0.4
    ], "no figure: counted, never 0"
    github = UsageRow(
        provider=Provider.GITHUB,
        model="copilot-code-review",
        kind=RowKind.COPILOT,
        source="usage-log",
        at=WINDOW.start,
        ref="pr-9",
        billing=Billing.SUBSCRIPTION,
        tokens=Tokens(reviews=2),
    )
    rule = real_rule(Provider.GITHUB)
    assert rule(github, book, config)[1].usd == 0.0, "a plan row within the allowance"
    assert (
        rule(replace(github, cost_reported=0.35), book, config)[1].usd == 0.35
    ), "a reported charge is the charge"
    assert (
        rule(replace(github, billing=Billing.API), book, config)[1].usd > 0
    ), "an explicit pay-per-use row is cash"


def test_a_provider_id_is_checked_on_every_construction_path() -> None:
    for bad in ("Acme", "", "a b", 7):
        try:
            Provider(bad)  # type: ignore[arg-type]
        except ValueError as exc:
            assert "not a provider id" in str(exc)
        else:
            raise AssertionError(f"Provider({bad!r}) must be refused like Provider.of")
    assert Provider("acme") is Provider.of("acme")


def test_an_unknown_billing_row_is_never_cash_whatever_figure_it_carries(tmp_path: Path) -> None:
    from dataclasses import replace

    from ..groups import real_group
    from .fixtures import WINDOW, defaults, paths_in

    config, book = defaults(paths_in(tmp_path))
    row = UsageRow(
        provider=Provider.of("acme"),
        model="m",
        kind=RowKind.LOG,
        source="acme",
        at=WINDOW.start,
        ref="r",
        billing=Billing.UNKNOWN,
        tokens=Tokens(input=10),
        cost_reported=0.4,
    )
    real = real_group([row, replace(row, cost_reported=None)], book, config, WINDOW)
    assert real.usage == [] and real.unknown_billing == 2, "a figure without a rule is an estimate, not cash"
    known = real_group([replace(row, billing=Billing.API)], book, config, WINDOW)
    assert [line.usd for line in known.usage] == [0.4], "a source that knows the key was charged says API"
