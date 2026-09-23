"""Configuration merge semantics, price-book parsing by shape, the drift checker and what ``update`` may write."""

from __future__ import annotations

import email
import json
import os
import time
import urllib.error
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any
from unittest import mock

from ..config import (
    Paths,
    builtin_config,
    builtin_prices,
    load_config,
    load_pricebook,
    parse_config,
    parse_pricebook,
)
from ..errors import ConfigError
from ..models import CheckStatus, PeakOffpeakPrice, Provider, TokenTierPrice
from ..ops import doctor
from ..prices_check import (
    USER_AGENT,
    CheckResult,
    ModelCheck,
    ProviderCheck,
    apply_check,
    auto_check,
    check_due,
    exit_code,
    expected_amounts,
    fetch,
    load_result,
    match_model,
    name_variants,
    numbers_near,
    page_text,
    save_result,
    state_file,
    take_lock,
)
from .fixtures import defaults, paths_in, with_test_plans


def test_user_config_subscriptions_replace_and_dicts_merge(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir()
    (paths.user_config_dir / "config.json").write_text(
        json.dumps({"subscriptions": [{"plan": "claude-pro"}], "budgets": {"daily_usd": 1}})
    )
    config = load_config(paths)
    assert [s.plan for s in config.subscriptions] == ["claude-pro"]
    assert config.budgets.daily_usd == 1 and config.budgets.monthly_usd == 2500


def test_user_prices_override_by_model_and_keep_the_shape(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir()
    (paths.user_config_dir / "prices.json").write_text(
        json.dumps({"providers": {"openai": {"models": {"gpt-6-astra": {"input": 12, "output": 60}}}}})
    )
    book = load_pricebook(paths)
    astra = book.entry(Provider.OPENAI, "gpt-6-astra")
    assert (
        isinstance(astra, TokenTierPrice)
        and astra.input == 12
        and astra.output == 60
        and astra.cached_input == 1
    )
    assert book.entry(Provider.OPENAI, "gpt-5.5") is not None


def test_override_cannot_change_a_models_shape(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir()
    (paths.user_config_dir / "prices.json").write_text(
        json.dumps({"providers": {"deepseek": {"models": {"deepseek-flash": {"input": 0.4, "output": 1.6}}}}})
    )
    try:
        load_pricebook(paths)
    except ConfigError as exc:
        assert "deepseek-flash" in str(exc) and "peak" in str(exc)
    else:
        raise AssertionError("token-tier keys on a peak/off-peak model must be rejected")


def test_unknown_price_keys_are_named(tmp_path: Path) -> None:
    _, book = defaults(paths_in(tmp_path))
    try:
        parse_pricebook(
            book.raw, {"providers": {"xai": {"models": {"grok-4.6": {"inputs": 1}}}}}, "user.json"
        )
    except ConfigError as exc:
        assert "grok-4.6" in str(exc) and "inputs" in str(exc)
    else:
        raise AssertionError("an unknown key must be an error")


def test_user_defined_peak_model_parses(tmp_path: Path) -> None:
    _, book = defaults(paths_in(tmp_path))
    over = {
        "providers": {
            "deepseek": {
                "models": {
                    "deepseek-v5-lite": {
                        "peak": {"cache_hit": 0.01, "cache_miss": 0.5, "output": 2.0},
                        "offpeak": {"cache_hit": 0.005, "cache_miss": 0.25, "output": 1.0},
                    }
                }
            }
        }
    }
    merged = parse_pricebook(book.raw, over, "user.json")
    assert isinstance(merged.entry(Provider.DEEPSEEK, "deepseek-v5-lite"), PeakOffpeakPrice)


def test_price_parser_finds_amounts_after_the_model_name() -> None:
    page = "<html><body><h1>Pricing</h1><table><tr><td>claude-fable-5-1</td><td>$10 / MTok</td><td>$12.50</td><td>$20</td><td>$0.25</td><td>$50 / MTok</td></tr></table></body></html>"
    assert numbers_near(page_text(page), "claude-fable-5-1") == [10.0, 12.5, 20.0, 0.25, 50.0]
    assert numbers_near(page_text(page), "gpt-9") is None


def test_display_name_variants_per_provider(tmp_path: Path) -> None:
    _, book = defaults(paths_in(tmp_path))
    fable = book.entry(Provider.ANTHROPIC, "claude-fable-5-1")
    assert fable is not None and "Claude Fable 5.1" in name_variants(
        Provider.ANTHROPIC, "claude-fable-5-1", fable
    )
    flash = book.entry(Provider.GOOGLE, "gemini-3.8-flash")
    assert flash is not None and "Gemini 3.8 Flash" in name_variants(
        Provider.GOOGLE, "gemini-3.8-flash", flash
    )


def test_a_batch_table_before_the_standard_one_still_confirms(tmp_path: Path) -> None:
    _, book = defaults(paths_in(tmp_path))
    entry = book.entry(Provider.OPENAI, "gpt-5.6-sol")
    assert entry is not None
    page = page_text(
        "<h2>Batch</h2><table><tr><td>gpt-5.6-sol</td><td>$2.00</td><td>$0.20</td><td>$10.00</td></tr></table><h2>Standard</h2><table><tr><td>gpt-5.6-sol</td><td>$4.00</td><td>$0.40</td><td>$20.00</td></tr></table>"
    )
    assert match_model([page], Provider.OPENAI, "gpt-5.6-sol", entry).status is CheckStatus.CONFIRMED
    changed = TokenTierPrice(input=6, output=20)
    assert match_model([page], Provider.OPENAI, "gpt-5.6-sol", changed).status is CheckStatus.CHANGED


def test_update_writes_only_token_tier_pairs_to_the_user_file(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    _, book = defaults(paths)
    result = CheckResult("2026-09-20T00:00:00Z")
    result.providers["openai"] = ProviderCheck(
        "u",
        "fetched",
        {
            "gpt-6-astra": ModelCheck(CheckStatus.CHANGED, [10, 50], [12.0, 60.0]),
            "gpt-5.5": ModelCheck(CheckStatus.CONFIRMED, [5, 30], [5.0, 30.0]),
        },
    )
    result.providers["deepseek"] = ProviderCheck(
        "u", "fetched", {"deepseek-flash": ModelCheck(CheckStatus.CHANGED, [0.3, 1.2], [0.4, 1.6])}
    )
    applied = apply_check(result, book, paths.user_prices_file())
    assert applied == [("openai", "gpt-6-astra", 12.0, 60.0)]
    written = json.loads(paths.user_prices_file().read_text())
    assert (
        written["providers"]["openai"]["models"]["gpt-6-astra"]["input"] == 12.0
        and "deepseek" not in written["providers"]
    )
    assert result.providers["openai"].models["gpt-6-astra"].status is CheckStatus.APPLIED
    assert result.drift() == [("deepseek", "deepseek-flash")], "a peak/off-peak model stays for a hand edit"
    unresolved = sum(
        check.status is CheckStatus.CHANGED
        for prov in result.providers.values()
        for check in prov.models.values()
    )
    assert unresolved == 1 and exit_code(unresolved) == 4, "one applied, one left: update must not exit 0"
    assert exit_code(0) == 0


def test_non_numeric_config_and_price_values_are_config_errors(tmp_path: Path) -> None:
    from ..config import builtin_config, builtin_prices, parse_config, parse_pricebook

    raw = builtin_config()
    raw["vendor"]["profiles"]["consultancy-eu"]["staffing"]["senior"]["rate"] = "a lot"
    try:
        parse_config(raw, "cfg")
    except ConfigError as exc:
        assert "staffing.senior" in str(exc) and "rate" in str(exc) and exc.code == 2
    else:
        raise AssertionError("a non-numeric rate must be a ConfigError")
    prices = builtin_prices()
    prices["providers"]["openai"]["models"]["gpt-6-astra"]["input"] = "ten"
    try:
        parse_pricebook(prices, {}, "prices")
    except ConfigError as exc:
        assert "gpt-6-astra" in str(exc) and "input" in str(exc)
    else:
        raise AssertionError("a non-numeric price must be a ConfigError")


def test_apply_leaves_a_pair_listed_output_first(tmp_path: Path) -> None:
    """A vendor page that prints output before input must not be written swapped into the override."""
    paths = paths_in(tmp_path)
    _, book = defaults(paths)
    result = CheckResult(checked_at="2026-09-20T00:00:00Z")
    result.providers["openai"] = ProviderCheck(
        "u", "fetched", {"gpt-6-astra": ModelCheck(CheckStatus.CHANGED, [10.0, 50.0], [60.0, 12.0])}
    )
    assert apply_check(result, book, paths.user_prices_file()) == []
    assert result.providers["openai"].models["gpt-6-astra"].status is CheckStatus.CHANGED
    assert not paths.user_prices_file().exists()


def test_lock_is_atomic_and_a_stale_lock_is_replaced(tmp_path: Path) -> None:
    lock = tmp_path / "prices-check.lock"
    assert take_lock(lock) and not take_lock(lock)
    os.utime(lock, (1, 1))  # ancient: stale
    assert take_lock(lock)


def test_wrong_typed_config_sections_are_config_errors(tmp_path: Path) -> None:
    from ..config import builtin_config, parse_config

    for path, value in (
        ("subscriptions", None),
        ("subscriptions", {"plan": "x"}),
        ("budgets", "cheap"),
        ("providers", [1]),
    ):
        raw = builtin_config()
        raw[path] = value
        try:
            parse_config(raw, "cfg")
        except ConfigError as exc:
            assert path in str(exc)
        else:
            if value is not None:
                raise AssertionError(f"{path}={value!r} must be a ConfigError")
    raw = builtin_config()
    raw["budgets"]["per_provider_daily_usd"] = [60]
    try:
        parse_config(raw, "cfg")
    except ConfigError as exc:
        assert "per_provider_daily_usd" in str(exc)
    else:
        raise AssertionError("a list where an object is expected must be a ConfigError")


def test_a_file_url_is_refused_before_any_request() -> None:
    calls: list[Any] = []

    def opener(request: Any, timeout: int) -> Any:
        calls.append(request)
        raise AssertionError("must not be called")

    try:
        fetch("file:///etc/passwd", opener=opener)
    except urllib.error.URLError as exc:
        assert "refused" in str(exc) and calls == []
    else:
        raise AssertionError("a file:// source must be refused")


def test_redirect_to_a_non_http_url_is_refused() -> None:
    def opener(request: Any, timeout: int) -> Any:
        raise urllib.error.HTTPError(
            request.full_url, 302, "moved", email.message_from_string("Location: file:///etc/passwd\n"), None
        )

    try:
        fetch("https://vendor.example/prices", opener=opener)
    except urllib.error.URLError as exc:
        assert "refused" in str(exc)
    else:
        raise AssertionError("a redirect to file:// must be refused")


def test_structural_config_typos_are_config_errors(tmp_path: Path) -> None:
    from ..config import builtin_config, builtin_prices, parse_config, parse_pricebook

    raw = builtin_config()
    raw["subscriptions"] = [{"seats": 1}]
    try:
        parse_config(raw, "cfg")
    except ConfigError as exc:
        assert "plan" in str(exc)
    else:
        raise AssertionError("a subscription without a plan must be a ConfigError")
    raw = builtin_config()
    del raw["vendor"]["profiles"]["consultancy-eu"]["bands_hours"]["XL"]
    try:
        parse_config(raw, "cfg")
    except ConfigError as exc:
        assert "XL" in str(exc) and "consultancy-eu" in str(exc)
    else:
        raise AssertionError("a profile missing a band must be a ConfigError")
    prices = builtin_prices()
    prices["checked_at"] = "yesterday"
    try:
        parse_pricebook(prices, {}, "prices")
    except ConfigError as exc:
        assert "checked_at" in str(exc)
    else:
        raise AssertionError("a non-date checked_at must be a ConfigError")


def test_seats_and_next_blocks_are_validated(tmp_path: Path) -> None:
    from ..config import builtin_config, builtin_prices, parse_config, parse_pricebook

    raw = with_test_plans(builtin_config())
    raw["subscriptions"][0]["seats"] = "two"
    try:
        parse_config(raw, "cfg")
    except ConfigError as exc:
        assert "seats" in str(exc)
    else:
        raise AssertionError("a non-numeric seats must be a ConfigError")
    prices = builtin_prices()
    prices["providers"]["google"]["models"]["gemini-3.8-flash"]["next"] = 5
    try:
        parse_pricebook(prices, {}, "prices")
    except ConfigError as exc:
        assert "next" in str(exc) and "gemini-3.8-flash" in str(exc)
    else:
        raise AssertionError("a non-object next block must be a ConfigError")


def test_expected_amounts_follow_the_active_price_and_every_deepseek_tariff(tmp_path: Path) -> None:
    from ..config import builtin_prices, parse_pricebook

    prices = builtin_prices()
    prices["providers"]["google"]["models"]["gemini-3.8-flash"]["next"]["from"] = "2020-01-01"
    book = parse_pricebook(prices, {}, "prices")
    flash = book.entry(Provider.GOOGLE, "gemini-3.8-flash")
    assert flash is not None and expected_amounts(flash) == [
        1.5,
        7.5,
    ], "the page is compared with today's price"
    deepseek = book.entry(Provider.DEEPSEEK, "deepseek-flash")
    assert deepseek is not None and len(expected_amounts(deepseek)) == 6, "peak and off-peak, all three rates"
    prices = builtin_prices()
    prices["providers"]["xai"]["models"]["grok-4.6"]["long"]["threshold"] = "lots"
    try:
        parse_pricebook(prices, {}, "prices")
    except ConfigError as exc:
        assert "threshold" in str(exc)
    else:
        raise AssertionError("a non-numeric long.threshold must be a ConfigError")


def test_update_leaves_a_model_whose_next_price_is_already_active(tmp_path: Path) -> None:
    """Writing input/output at the top level while `next` is what prices the rows would change nothing."""
    paths = paths_in(tmp_path)
    from ..config import builtin_prices, parse_pricebook

    prices = builtin_prices()
    prices["providers"]["google"]["models"]["gemini-3.8-flash"]["next"]["from"] = "2020-01-01"
    book = parse_pricebook(prices, {}, "prices")
    result = CheckResult(checked_at="2026-09-20T00:00:00Z")
    result.providers["google"] = ProviderCheck(
        "u", "fetched", {"gemini-3.8-flash": ModelCheck(CheckStatus.CHANGED, [1.5, 7.5], [1.6, 8.0])}
    )
    assert apply_check(result, book, paths.user_prices_file()) == []


def test_explicit_paths_never_read_the_environment(tmp_path: Path) -> None:
    """A fixture's Paths must not follow AI_COST_PRICES to the user's real overrides (a selftest wrote there)."""
    with mock.patch.dict(
        os.environ, {"AI_COST_PRICES": "/nowhere/prices.json", "AI_COST_CONFIG": "/nowhere/c.json"}
    ):
        explicit = paths_in(tmp_path)
        assert explicit.user_prices_file() == tmp_path / "cfg" / "prices.json"
        assert explicit.user_config_file() == tmp_path / "cfg" / "config.json"
        from_env = Paths.from_env()
        assert from_env.user_prices_file() == Path("/nowhere/prices.json")
        assert from_env.user_config_file() == Path("/nowhere/c.json")


def test_auto_check_reports_saved_drift_and_spawns_once_when_due(tmp_path: Path) -> None:
    paths = Paths(**{**paths_in(tmp_path).__dict__, "offline": False})
    _, book = defaults(paths)
    stale = CheckResult(
        "2000-01-01T00:00:00Z",
        {
            "openai": ProviderCheck(
                "u", "fetched", {"gpt-6-astra": ModelCheck(CheckStatus.CHANGED, [10, 50], [12.0])}
            )
        },
    )
    save_result(paths, stale)
    spawned: list[list[str]] = []

    def spawn(command: list[str], **_: object) -> None:
        spawned.append(command)

    drift, started = auto_check(paths, book, spawn=spawn)
    _, started_again = auto_check(paths, book, spawn=spawn)
    assert drift == [("openai", "gpt-6-astra")] and started and not started_again and len(spawned) == 1
    assert spawned[0][-3:] == ["prices", "check", "--quiet"]


def test_auto_check_is_quiet_when_recent_or_offline(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    _, book = defaults(paths)
    assert not check_due(book, CheckResult("2999-01-01T00:00:00Z"))
    assert auto_check(
        paths, book, spawn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned"))
    ) == ([], False)


def test_pricebook_and_config_shapes_are_config_errors(tmp_path: Path) -> None:
    prices = builtin_prices()
    prices["providers"] = []
    prices2 = builtin_prices()
    prices2["plans"]["claude-pro"]["monthly_usd"] = "twenty"
    raw = builtin_config()
    raw["vendor"]["profiles"]["consultancy-eu"]["bands_hours"]["S"] = {"min": 3, "max": 6}
    raw2 = builtin_config()
    raw2["vendor"]["hours_per_day"] = 0
    cases: list[tuple[Callable[[], object], str]] = [
        (lambda: parse_pricebook(prices, {}, "prices"), "providers"),
        (lambda: parse_pricebook(prices2, {}, "prices"), "monthly_usd"),
        (lambda: parse_config(raw, "cfg"), "bands_hours"),
        (lambda: parse_config(raw2, "cfg"), "hours_per_day"),
    ]
    for call, word in cases:
        try:
            call()
        except ConfigError as exc:
            assert word in str(exc)
        else:
            raise AssertionError(f"{word}: expected a ConfigError")


def test_corrupt_price_state_never_raises(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    assert CheckResult.from_json({"providers": [1]}).providers == {}
    assert CheckResult.from_json({"providers": {"x": {"models": "no"}}}).providers["x"].models == {}
    corrupt = {"providers": {"x": {"models": {"m": {"status": "ok", "expected": 5, "seen": ["a"]}}}}}
    assert CheckResult.from_json(corrupt).providers["x"].models["m"].expected == []
    booleans = {"providers": {"x": {"models": {"m": {"status": "ok", "expected": [True], "seen": [1.5]}}}}}
    assert (
        CheckResult.from_json(booleans).providers["x"].models["m"].expected == []
    ), "a boolean is not an amount"
    huge = {"providers": {"x": {"models": {"m": {"status": "ok", "expected": [10**400], "seen": [None]}}}}}
    models = CheckResult.from_json(huge).providers["x"].models["m"]
    assert models.expected == [] and models.seen == [], "an amount no float holds, or a null, is corrupt"
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    state_file(paths).write_text("[1, 2, 3]")
    assert load_result(paths) is None


def test_config_sections_as_lists_are_config_errors(tmp_path: Path) -> None:
    bands = builtin_config()
    bands["vendor"]["profiles"]["consultancy-eu"]["bands_hours"] = [1, 2]
    band_string = builtin_config()
    band_string["vendor"]["profiles"]["consultancy-eu"]["bands_hours"][
        "S"
    ] = "12-18"  # would parse as (1.0, 2.0)
    staffing = builtin_config()
    staffing["vendor"]["profiles"]["consultancy-eu"]["staffing"] = [1]
    no_staffing = builtin_config()
    no_staffing["vendor"]["profiles"]["consultancy-eu"]["staffing"] = None
    minutes = builtin_prices()
    minutes["providers"]["github"]["actions"]["usd_per_minute"] = [0.008]
    cases: list[tuple[Callable[[], object], str]] = [
        (lambda: parse_config(bands, "cfg"), "bands_hours"),
        (lambda: parse_config(band_string, "cfg"), "bands_hours"),
        (lambda: parse_config(staffing, "cfg"), "staffing"),
        (lambda: parse_config(no_staffing, "cfg"), "staffing"),
        (lambda: parse_pricebook(minutes, {}, "prices"), "usd_per_minute"),
    ]
    for call, word in cases:
        try:
            call()
        except ConfigError as exc:
            assert word in str(exc)
        else:
            raise AssertionError(f"{word}: expected a ConfigError")


def test_alias_and_peak_hour_shapes_are_config_errors(tmp_path: Path) -> None:
    aliases = builtin_prices()
    aliases["providers"]["openai"]["models"]["gpt-6-astra"]["aliases"] = 5
    peak = builtin_prices()
    peak["providers"]["deepseek"]["peak_hours_utc"] = {"from": 1}
    strings = builtin_prices()
    strings["providers"]["deepseek"]["peak_hours_utc"] = ["12-18"]  # a string pair would parse as (1, 2)
    null_rate = builtin_prices()
    null_rate["providers"]["deepseek"]["models"]["deepseek-flash"]["peak"] = None
    cases = ((aliases, "aliases"), (peak, "peak_hours_utc"), (strings, "peak_hours_utc"), (null_rate, "peak"))
    assert parse_pricebook(
        builtin_prices(), {"providers": {"github": None}}, "prices"
    ).github, "null = absent"
    for raw, word in cases:
        try:
            parse_pricebook(raw, {}, "prices")
        except ConfigError as exc:
            assert word in str(exc)
        else:
            raise AssertionError(f"{word}: expected a ConfigError")


def test_flags_must_be_booleans_and_budgets_non_negative() -> None:
    quoted = builtin_config()
    quoted["providers"]["github"] = {"copilot_plan_exhausted": "false"}
    negative = builtin_config()
    negative["budgets"]["daily_usd"] = -1
    seat = builtin_prices()
    seat["plans"]["claude-pro"]["per_seat"] = "yes"
    null_flag = builtin_prices()
    null_flag["plans"]["claude-pro"]["per_seat"] = None
    assert (
        parse_pricebook(null_flag, {}, "prices").plans["claude-pro"].per_seat is False
    ), "null = absent = default"
    cases: list[tuple[Callable[[], object], str]] = [
        (lambda: parse_config(quoted, "cfg"), "copilot_plan_exhausted"),
        (lambda: parse_config(negative, "cfg"), "daily_usd"),
        (lambda: parse_pricebook(seat, {}, "prices"), "per_seat"),
    ]
    for call, word in cases:
        try:
            call()
        except ConfigError as exc:
            assert word in str(exc)
        else:
            raise AssertionError(f"{word}: expected a ConfigError")


def test_the_copilot_check_survives_a_null_provider_override() -> None:
    from ..prices_check import _copilot_check

    book = parse_pricebook(builtin_prices(), {"providers": {"github": None}}, "prices")
    assert book.raw["providers"][
        "github"
    ], "null = absent: the base provider (and its source URL) stays in raw"
    with mock.patch("ai_cost.prices_check.fetch_variants", side_effect=OSError("offline")):
        check = _copilot_check(["no prices here"], book)
    assert check.status is CheckStatus.NOT_FOUND


def test_huge_and_infinite_numbers_are_config_errors() -> None:
    for value in (1e400, 10**400):
        raw = with_test_plans(builtin_config())
        raw["subscriptions"][0]["seats"] = value
        try:
            parse_config(raw, "cfg")
        except ConfigError as exc:
            assert "seats" in str(exc)
        else:
            raise AssertionError(f"{value!r}: expected a ConfigError")


def test_fractional_integer_settings_are_config_errors_never_truncated() -> None:
    config_cases: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
        ("sizing.loc", lambda c: c["sizing"]["loc"].__setitem__("S", 1.9)),
        ("seats", lambda c: c["subscriptions"][0].__setitem__("seats", 1.5)),
    ]
    price_cases: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
        ("peak_hours_utc", lambda p: p["providers"]["deepseek"].__setitem__("peak_hours_utc", [[1.5, 9]])),
        ("auto_check_days", lambda p: p.__setitem__("auto_check_days", 2.5)),
    ]
    attempts: list[tuple[str, Callable[[], object]]] = []
    for word, mutate in config_cases:
        raw = with_test_plans(builtin_config())
        mutate(raw)
        attempts.append((word, partial(parse_config, raw, "cfg")))
    for word, mutate in price_cases:
        prices = builtin_prices()
        mutate(prices)
        attempts.append((word, partial(parse_pricebook, prices, {}, "prices")))
    for word, attempt in attempts:
        try:
            attempt()
        except ConfigError as exc:
            assert word in str(exc), str(exc)
            continue
        raise AssertionError(f"{word} = a fraction must be a ConfigError, not a truncated int")
    whole_config = with_test_plans(builtin_config())
    whole_config["sizing"]["loc"]["S"] = 400.0
    whole_prices = builtin_prices()
    whole_prices["auto_check_days"] = 3.0
    assert parse_config(whole_config, "cfg").sizing.loc[1] == 400, "a whole-valued float is that integer"
    assert parse_pricebook(whole_prices, {}, "prices").auto_check_days == 3


def test_huge_numbers_inside_thresholds_hour_pairs_and_bands_are_config_errors() -> None:
    thresholds = builtin_config()
    thresholds["sizing"]["loc"]["S"] = 1e400
    bands = builtin_config()
    bands["vendor"]["profiles"]["consultancy-eu"]["bands_hours"]["S"] = [1, 10**400]
    hours = builtin_prices()
    hours["providers"]["deepseek"]["peak_hours_utc"] = [[1e400, 2]]
    cases: list[tuple[Callable[[], object], str]] = [
        (lambda: parse_config(thresholds, "cfg"), "sizing.loc"),
        (lambda: parse_config(bands, "cfg"), "bands_hours"),
        (lambda: parse_pricebook(hours, {}, "prices"), "peak_hours_utc"),
    ]
    for call, word in cases:
        try:
            call()
        except ConfigError as exc:
            assert word in str(exc), (word, str(exc))
        else:
            raise AssertionError(f"{word}: expected a ConfigError")


def test_paths_read_the_gemini_cli_home_and_config_lists_plugin_modules() -> None:
    with mock.patch.dict(os.environ, {"GEMINI_CLI_HOME": "/srv/gemini"}, clear=False):
        assert Paths.from_env().gemini_home == Path("/srv/gemini")
    with mock.patch.dict(os.environ, {}, clear=True):
        assert Paths.from_env().gemini_home == Path.home() / ".gemini"
    assert parse_config(builtin_config(), "cfg").plugins == (), "the public package names no plugin"
    listed = builtin_config()
    listed["plugins"] = ["acme_extras", "acme.cost"]
    assert parse_config(listed, "cfg").plugins == ("acme_extras", "acme.cost")
    listed["plugins"] = "acme_extras"
    try:
        parse_config(listed, "cfg")
    except ConfigError as exc:
        assert "plugins" in str(exc)
    else:
        raise AssertionError("plugins must be a list of module names")


def test_plugin_settings_and_provider_billing_are_validated() -> None:
    raw = builtin_config()
    raw["plugin_settings"] = {"acme": {"state_dir": "~/acme", "workspaces": ["a-*"]}}
    raw["providers"]["openai"]["billing"] = "subscription"
    config = parse_config(raw, "cfg")
    assert config.plugin_settings["acme"]["workspaces"] == ["a-*"] and config.openai_billing == "subscription"
    assert parse_config(builtin_config(), "cfg").openai_billing == "", "mixed = no single rule"
    breaks: list[tuple[Callable[[dict[str, Any]], None], str]] = [
        (lambda c: c.__setitem__("plugin_settings", {"acme": "no"}), "plugin_settings"),
        (lambda c: c["providers"]["openai"].__setitem__("billing", "prepaid"), "billing"),
    ]
    for mutate, word in breaks:
        bad = builtin_config()
        mutate(bad)
        try:
            parse_config(bad, "cfg")
        except ConfigError as exc:
            assert word in str(exc), str(exc)
        else:
            raise AssertionError(f"{word}: expected a ConfigError")


def test_window_hours_and_check_days_are_capped_so_timedelta_never_overflows() -> None:
    for key, value in (
        ("window_default_hours", 1e12),
        ("window_default_hours", -5),
        ("window_default_hours", 0),
    ):
        raw = builtin_config()
        raw[key] = value
        try:
            parse_config(raw, "cfg")
        except ConfigError as exc:
            assert key in str(exc), str(exc)
        else:
            raise AssertionError(f"{key} = {value} must be a ConfigError")
    prices = builtin_prices()
    prices["auto_check_days"] = 10**7
    try:
        parse_pricebook(prices, {}, "prices")
    except ConfigError as exc:
        assert "auto_check_days" in str(exc) and "at most" in str(exc)
    else:
        raise AssertionError("a ten-million-day check interval must be a ConfigError")
    assert parse_config(builtin_config(), "cfg").window_default_hours == 24


def test_the_price_check_identifies_itself_and_never_as_a_browser() -> None:
    import inspect

    from .. import __version__

    assert inspect.signature(fetch).parameters["agent"].default == USER_AGENT
    assert f"ai-cost/{__version__} (+https://github.com/qmediat/ai-cost)" == USER_AGENT
    assert "Chrome" not in USER_AGENT and "Safari" not in USER_AGENT and "Mozilla" not in USER_AGENT


def test_a_null_plan_override_removes_the_plan_instead_of_zeroing_it() -> None:
    name = next(iter(builtin_prices()["plans"]))
    book = parse_pricebook(builtin_prices(), {"plans": {name: None}}, "prices")
    assert name not in book.plans, "null = absent, never a zero-dollar plan"
    assert parse_pricebook(builtin_prices(), {}, "prices").plans[name].monthly_usd > 0


def test_numeric_strings_are_not_numbers_anywhere_in_a_config() -> None:
    prices = builtin_prices()
    prices["providers"]["openai"]["models"]["gpt-6-astra"]["input"] = "10"
    config = builtin_config()
    config["window_default_hours"] = "24"
    for attempt, word in (
        (lambda: parse_pricebook(prices, {}, "prices"), "input"),
        (lambda: parse_config(config, "cfg"), "window_default_hours"),
    ):
        try:
            attempt()
        except ConfigError as exc:
            assert word in str(exc) and "number" in str(exc), str(exc)
        else:
            raise AssertionError(
                f"a numeric string for {word} must be a ConfigError, like every other number"
            )


def test_integer_settings_are_non_negative_and_zero_check_days_means_never() -> None:
    raw = with_test_plans(builtin_config())
    raw["subscriptions"][0]["seats"] = -3
    try:
        parse_config(raw, "cfg")
    except ConfigError as exc:
        assert "seats" in str(exc) and "non-negative" in str(exc)
    else:
        raise AssertionError("negative seats must be a ConfigError, not a negative subscription share")
    prices = builtin_prices()
    prices["auto_check_days"] = 0
    assert check_due(parse_pricebook(prices, {}, "prices"), None) is False, "0 = the drift check is off"
    stale = CheckResult("2000-01-01T00:00:00Z")
    assert check_due(parse_pricebook(builtin_prices(), {}, "prices"), stale) is True, "an old check is due"
    assert check_due(parse_pricebook(prices, {}, "prices"), stale) is False, "0 days: never, however old"


def test_doctor_does_not_flag_price_age_when_the_drift_check_is_off(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_prices_file().write_text(json.dumps({"auto_check_days": 0, "checked_at": "2000-01-01"}))
    lines: list[str] = []
    doctor(paths, load_config(paths), load_pricebook(paths), lines.append)  # empty source dirs still exit 1
    price_lines = [line for line in lines if "prices checked_at" in line]
    assert price_lines and all(line.startswith("  ok") for line in price_lines), "\n".join(lines)
    assert any("drift check is off" in line for line in price_lines)


def test_an_explicit_null_number_means_the_default_like_every_other_shape() -> None:
    raw = builtin_config()
    raw["budgets"]["daily_usd"] = None
    raw["window_default_hours"] = None
    config = parse_config(raw, "cfg")
    assert config.window_default_hours == 24, "null = absent = the default"
    prices = builtin_prices()
    prices["auto_check_days"] = None
    assert parse_pricebook(prices, {}, "prices").auto_check_days == 7


def test_the_printed_registry_matches_what_the_book_prices_with() -> None:
    base = builtin_prices()
    name = next(iter(base["plans"]))
    book = parse_pricebook(base, {"providers": {"github": None}, "plans": {name: None}}, "prices")
    assert (
        book.raw["providers"]["github"] == base["providers"]["github"]
    ), "a null provider override masks nothing"
    assert (
        name not in book.raw["plans"] and name not in book.plans
    ), "a null plan override removes the plan everywhere"
    model = next(iter(base["providers"]["openai"]["models"]))
    nested = parse_pricebook(base, {"providers": {"openai": {"models": {model: None}}}}, "prices")
    assert nested.raw["providers"]["openai"]["models"][model] == base["providers"]["openai"]["models"][model]
    assert nested.entry(Provider.OPENAI, model) is not None, "a null model override is absent, at any depth"
    field_null = parse_pricebook(
        base, {"providers": {"openai": {"models": {model: {"cached_input": None}}}}}, "prices"
    )
    entry = field_null.entry(Provider.OPENAI, model)
    assert (
        isinstance(entry, TokenTierPrice)
        and entry.cached_input == base["providers"]["openai"]["models"][model]["cached_input"]
    )
    assert (
        field_null.raw["providers"]["openai"]["models"][model]["cached_input"] == entry.cached_input
    ), "one reading"
    unknown = parse_pricebook(base, {"providers": {"openai": {"models": {"gpt-nope": None}}}}, "prices")
    assert (
        unknown.entry(Provider.OPENAI, "gpt-nope") is None
    ), "a null override of an unknown model is no override"


def test_an_empty_value_clears_a_shipped_block_while_null_keeps_it() -> None:
    base = builtin_prices()
    flash = next(
        m for m, e in base["providers"]["google"]["models"].items() if isinstance(e, dict) and e.get("next")
    )
    cleared = parse_pricebook(
        base, {"providers": {"google": {"models": {flash: {"next": {}, "valid_until": ""}}}}}, "prices"
    )
    entry = cleared.entry(Provider.GOOGLE, flash)
    assert isinstance(entry, TokenTierPrice) and entry.next is None and entry.valid_until is None, "cleared"
    kept = parse_pricebook(
        base, {"providers": {"google": {"models": {flash: {"next": None, "valid_until": None}}}}}, "prices"
    )
    kept_entry = kept.entry(Provider.GOOGLE, flash)
    assert (
        isinstance(kept_entry, TokenTierPrice) and kept_entry.next is not None
    ), "null = no override: the shipped block stays"


def test_an_empty_section_in_a_user_file_is_no_override_outside_the_clearable_blocks(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    shipped = load_config(paths).budgets
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text(json.dumps({"budgets": {}}))
    assert load_config(paths).budgets == shipped, "an empty budgets object keeps the shipped budgets"
    base = builtin_prices()
    book = parse_pricebook(base, {"providers": {}}, "prices")
    assert book.raw["providers"] == base["providers"], "an empty providers object prints the shipped registry"
    nested = parse_pricebook(base, {"providers": {"openai": {"models": {}}}}, "prices")
    assert nested.raw["providers"]["openai"]["models"] == base["providers"]["openai"]["models"]
    assert nested.entry(Provider.OPENAI, next(iter(base["providers"]["openai"]["models"]))) is not None


def test_a_null_text_setting_in_the_user_file_means_the_default(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    user = {
        "providers": {
            "anthropic": {"billing": None},
            "github": {"actions_runner": None},
            "openai": {"default_model": None},
        }
    }
    paths.user_config_file().write_text(json.dumps(user))
    config = load_config(paths)
    assert (
        config.anthropic_billing == "" and config.github.actions_runner == "linux"
    ), "never 'None': the shipped defaults (anthropic billing mixed)"
    assert (
        config.openai_default_model == ""
    ), "the multi-line site too: the public core ships no default model"
    paths.user_config_file().write_text(json.dumps({"providers": {"anthropic": {"billing": "prepaid"}}}))
    try:
        load_config(paths)
    except ConfigError as exc:
        assert "billing" in str(exc) and "api, subscription or mixed" in str(exc)
    else:
        raise AssertionError("a billing typo must be a ConfigError, never silently API cash")


def test_a_null_in_the_user_config_keeps_the_shipped_value_for_every_key(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    shipped = load_config(paths)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    user = {"vendor": {"default_profile": None}, "budgets": {"daily_usd": None, "monthly_usd": None}}
    paths.user_config_file().write_text(json.dumps(user))
    config = load_config(paths)
    assert config.vendor_default_profile == shipped.vendor_default_profile, "not an error, not ''"
    assert config.budgets == shipped.budgets, "not zero: the shipped budgets"


def test_a_lock_released_between_the_failed_create_and_the_reclaim_is_taken(tmp_path: Path) -> None:
    from ..prices_check import _reclaim, take_lock

    lock = tmp_path / "check.lock"
    assert _reclaim(lock) is True, "a missing lock holds nothing"
    real_open = os.open
    attempts: list[int] = []

    def racing_open(path: Any, flags: int, mode: int = 0o777) -> int:
        attempts.append(1)
        if (
            len(attempts) == 1
        ):  # another process holds the lock at the first attempt and releases it right after
            raise FileExistsError(str(path))
        return real_open(path, flags, mode)

    with mock.patch("os.open", side_effect=racing_open):
        assert take_lock(lock) is True, "the second attempt creates the lock"
    assert lock.read_text() == str(os.getpid())


def test_a_non_string_text_setting_is_a_config_error_not_a_name(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text(json.dumps({"providers": {"github": {"actions_runner": 3}}}))
    try:
        load_config(paths)
    except ConfigError as exc:
        assert "providers.github: actions_runner must be a string, got int" in str(exc)
    else:
        raise AssertionError("3 is not a runner name")


def test_a_null_inside_a_list_is_an_error_naming_the_key(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    one = {"plan": "claude-max-20x", "seats": 1, "covers": ["anthropic"], "attribution": "time"}
    paths.user_config_file().write_text(json.dumps({"subscriptions": [None, one]}))
    try:
        load_config(paths)
    except ConfigError as exc:
        assert "subscriptions" in str(exc), "a list replaces whole: a null item stands for nothing"
    else:
        raise AssertionError("[null, plan] is not a list of subscriptions")
    try:
        parse_pricebook(builtin_prices(), {"providers": {"deepseek": {"peak_hours_utc": [None]}}}, "prices")
    except ConfigError as exc:
        assert "peak_hours_utc" in str(exc), "never a silently cleared peak calendar"
    else:
        raise AssertionError("[null] is not a list of hour pairs")


def test_a_list_item_that_is_all_null_stays_an_item_for_the_parser(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_config_file().write_text(json.dumps({"subscriptions": [{"plan": None}]}))
    try:
        load_config(paths)
    except ConfigError as exc:
        assert "subscriptions" in str(exc) and "plan" in str(exc), "the entry is validated, not dropped"
    else:
        raise AssertionError("an entry without a plan is not a subscription")


def test_a_non_string_provider_source_is_a_config_error_not_a_url(tmp_path: Path) -> None:
    try:
        parse_pricebook(builtin_prices(), {"providers": {"openai": {"source": 5}}}, "prices")
    except ConfigError as exc:
        assert "providers.openai" in str(exc) and "source" in str(exc)
    else:
        raise AssertionError("5 is not a URL")
    book = parse_pricebook(builtin_prices(), {"providers": {"openai": {"source": None}}}, "prices")
    assert (
        book.sources[Provider.OPENAI] == builtin_prices()["providers"]["openai"]["source"]
    ), "null = shipped"


def test_an_unwritable_state_or_prices_file_is_a_tool_error_not_a_traceback(tmp_path: Path) -> None:
    from ..errors import ToolError

    paths = paths_in(tmp_path)
    paths.state_dir.parent.mkdir(parents=True, exist_ok=True)
    paths.state_dir.write_text("a file where the state directory should be")
    try:
        save_result(paths, CheckResult("2026-09-20T00:00:00Z"))
    except ToolError as exc:
        assert "cannot write" in str(exc) and "prices-check.json" in str(exc)
    else:
        raise AssertionError("a file in place of the state directory must be a ToolError")
    _, book = defaults(paths)
    result = CheckResult("2026-09-20T00:00:00Z")
    changed = {"gpt-6-astra": ModelCheck(CheckStatus.CHANGED, [10, 50], [12.0, 60.0])}
    result.providers["openai"] = ProviderCheck("u", "fetched", changed)
    blocked = paths.user_config_dir / "blocked"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("a file where the prices directory should be")
    try:
        apply_check(result, book, blocked / "prices.json")
    except ToolError as exc:
        assert "cannot write" in str(exc)
    else:
        raise AssertionError("an unwritable prices file must be a ToolError")


def test_reclaim_never_deletes_a_lock_another_process_recreated(tmp_path: Path) -> None:
    from ..prices_check import LOCK_SECONDS

    lock = tmp_path / "check.lock"
    lock.write_text("stale")
    old = time.time() - LOCK_SECONDS - 10
    os.utime(lock, (old, old))
    real_open = os.open

    def open_after_another_took_it(path: Any, flags: int, mode: int = 0o777) -> int:
        if Path(path) == lock and lock.read_text() == "stale":  # our first create: another process was faster
            lock.write_text("fresh-other")  # it reclaimed the stale lock and holds a fresh one now
            raise FileExistsError(str(path))
        return real_open(path, flags, mode)

    with mock.patch("os.open", side_effect=open_after_another_took_it):
        assert (
            take_lock(lock) is False
        ), "the lock is fresh under the reclaim marker: theirs, not ours to delete"
    assert lock.read_text() == "fresh-other", "untouched"
    assert not list(tmp_path.glob("*.reclaim")), "the marker is released"


def test_a_reclaim_in_progress_elsewhere_wins_and_a_crashed_one_expires(tmp_path: Path) -> None:
    from ..prices_check import LOCK_SECONDS

    lock, marker = tmp_path / "check.lock", tmp_path / "check.lock.reclaim"
    old = time.time() - LOCK_SECONDS - 10
    lock.write_text("stale")
    os.utime(lock, (old, old))
    marker.write_text("")  # a live reclaim by another process
    assert take_lock(lock) is False and lock.read_text() == "stale" and marker.exists(), "yield to it"
    os.utime(marker, (old, old))  # the marker of a reclaim that crashed long ago
    assert take_lock(lock) is True, "cleared, then the stale lock is reclaimed and replaced"
    assert lock.read_text() == str(os.getpid()) and not marker.exists()


def test_a_non_string_copilot_source_is_a_config_error(tmp_path: Path) -> None:
    try:
        parse_pricebook(builtin_prices(), {"providers": {"github": {"copilot": {"source": 5}}}}, "prices")
    except ConfigError as exc:
        assert "copilot" in str(exc) and "source" in str(exc)
    else:
        raise AssertionError("5 is not a URL")
    shipped = builtin_prices()["providers"]["github"]["copilot"].get("source", "")
    assert parse_pricebook(builtin_prices(), {}, "prices").github.copilot_source == shipped


def test_prices_update_writes_under_a_null_provider_override(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    paths.user_prices_file().write_text(
        json.dumps({"providers": {"openai": None}})
    )  # null = no override, so far
    _, book = defaults(paths)
    result = CheckResult("2026-09-20T00:00:00Z")
    changed = {"gpt-6-astra": ModelCheck(CheckStatus.CHANGED, [10, 50], [12.0, 60.0])}
    result.providers["openai"] = ProviderCheck("u", "fetched", changed)
    assert apply_check(result, book, paths.user_prices_file()) == [("openai", "gpt-6-astra", 12.0, 60.0)]
    written = json.loads(paths.user_prices_file().read_text())
    assert written["providers"]["openai"]["models"]["gpt-6-astra"] == {"input": 12.0, "output": 60.0}


def test_prices_update_with_an_unreadable_user_file_is_a_tool_error(tmp_path: Path) -> None:
    from ..errors import ToolError

    paths = paths_in(tmp_path)
    _, book = defaults(paths)
    result = CheckResult("2026-09-20T00:00:00Z")
    changed = {"gpt-6-astra": ModelCheck(CheckStatus.CHANGED, [10, 50], [12.0, 60.0])}
    result.providers["openai"] = ProviderCheck("u", "fetched", changed)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    not_json, a_list, a_dir = (
        paths.user_config_dir / name for name in ("not-json.json", "list.json", "dir.json")
    )
    not_json.write_text("{not json")
    a_list.write_text("[]")
    a_dir.mkdir()
    for target in (not_json, a_list, a_dir):
        try:
            apply_check(result, book, target)
        except ToolError as exc:
            assert "cannot read" in str(exc) and target.name in str(exc)
        else:
            raise AssertionError(
                f"{target.name}: an unreadable user file must be a ToolError, never a traceback"
            )


def test_a_negative_sizing_threshold_is_a_config_error_like_every_other_count() -> None:
    raw = builtin_config()
    raw["sizing"]["loc"]["XS"] = -1
    try:
        parse_config(raw, "cfg")
    except ConfigError as exc:
        assert "sizing.loc" in str(exc) and "XS" in str(exc)
    else:
        raise AssertionError("a size band below zero is not a band")


def test_prices_update_never_overwrites_a_malformed_shape_on_its_path(tmp_path: Path) -> None:
    from ..errors import ToolError

    paths = paths_in(tmp_path)
    paths.user_config_dir.mkdir(parents=True, exist_ok=True)
    _, book = defaults(paths)
    result = CheckResult("2026-09-20T00:00:00Z")
    changed = {"gpt-6-astra": ModelCheck(CheckStatus.CHANGED, [10, 50], [12.0, 60.0])}
    result.providers["openai"] = ProviderCheck("u", "fetched", changed)
    shapes: list[dict[str, Any]] = [{"providers": []}, {"providers": {"openai": "oops"}}]
    for shape in shapes:
        paths.user_prices_file().write_text(json.dumps(shape))
        try:
            apply_check(result, book, paths.user_prices_file())
        except ToolError as exc:
            assert "cannot update" in str(exc) and "providers" in str(exc), str(exc)
        else:
            raise AssertionError(f"{shape}: a malformed path must be a ToolError, never rewritten")
        assert json.loads(paths.user_prices_file().read_text()) == shape, "untouched"
