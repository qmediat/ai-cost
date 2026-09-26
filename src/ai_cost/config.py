"""Configuration and the price book: defaults from package data, user overrides, parsed once into typed values.

The built-in registry is ``ai_cost/data/prices.json`` (the single source; ``prices show --snapshot`` prints it) and
``ai_cost/data/config.json``. The user's ``~/.config/ai-cost/{config,prices}.json`` override keys; overrides are parsed
through the same constructors, so a price cannot change its shape (ADR-0002, consult C4).
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

from .errors import ConfigError
from .models import (
    BillAccount,
    BillScope,
    PeakOffpeakPrice,
    PerUnitPrice,
    PriceEntry,
    Provider,
    Rate,
    Size,
    TokenTierPrice,
)
from .values import as_object, object_at

JsonDict = dict[str, Any]

_TOKEN_KEYS = {
    "input",
    "output",
    "cached_input",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "long",
    "aliases",
    "valid_until",
    "next",
}
_PEAK_KEYS = {"peak", "offpeak", "display"}
_RATE_KEYS = {"cache_hit", "cache_miss", "output"}


@dataclass(frozen=True)
class Paths:
    """Where the sources and the state live; every path is overridable through the environment."""

    claude_home: Path
    codex_home: Path
    gemini_home: Path
    grok_home: Path
    state_dir: Path
    reports_dir: Path  # where ``daily`` writes: AI_COST_REPORTS_DIR, else $XDG_DATA_HOME/ai-cost/reports
    user_config_dir: Path
    offline: bool
    config_file: Path | None = None  # AI_COST_CONFIG, resolved by from_env only
    prices_file: Path | None = None  # AI_COST_PRICES, resolved by from_env only
    usage_log: Path | None = (
        None  # the default usage log (AI_COST_USAGE_LOG, else $XDG_DATA_HOME/ai-cost/usage.jsonl)
    )

    @classmethod
    def from_env(cls) -> Paths:
        """Resolve every path from the environment.

        ``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``, ``GEMINI_CLI_HOME``, ``GROK_HOME``, ``AI_COST_STATE_DIR``,
        ``AI_COST_REPORTS_DIR``, ``AI_COST_CONFIG_DIR``, ``AI_COST_OFFLINE``, ``AI_COST_CONFIG``, ``AI_COST_PRICES``
        and ``AI_COST_USAGE_LOG`` (else ``XDG_DATA_HOME``).
        """
        home = Path.home()
        return cls(
            claude_home=Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")),
            codex_home=Path(os.environ.get("CODEX_HOME", home / ".codex")),
            gemini_home=Path(os.environ.get("GEMINI_CLI_HOME", home / ".gemini")),
            grok_home=Path(os.environ.get("GROK_HOME", home / ".grok")),
            state_dir=Path(os.environ.get("AI_COST_STATE_DIR", home / ".local" / "state" / "ai-cost")),
            reports_dir=reports_dir_path(os.environ, home),
            user_config_dir=Path(os.environ.get("AI_COST_CONFIG_DIR", home / ".config" / "ai-cost")),
            usage_log=usage_log_path(os.environ, home),
            offline=os.environ.get("AI_COST_OFFLINE", "") not in ("", "0"),
            config_file=Path(os.environ["AI_COST_CONFIG"]) if os.environ.get("AI_COST_CONFIG") else None,
            prices_file=Path(os.environ["AI_COST_PRICES"]) if os.environ.get("AI_COST_PRICES") else None,
        )

    def user_config_file(self) -> Path:
        """``AI_COST_CONFIG`` (when built by ``from_env``) or ``<user_config_dir>/config.json``."""
        return self.config_file or self.user_config_dir / "config.json"

    def user_prices_file(self) -> Path:
        """``AI_COST_PRICES`` (when built by ``from_env``) or ``<user_config_dir>/prices.json``."""
        return self.prices_file or self.user_config_dir / "prices.json"


@dataclass(frozen=True)
class Plan:
    """A subscription plan and its list price."""

    name: str
    provider: str
    monthly_usd: float
    per_seat: bool = False
    allowance_units: int = 0
    actions_minutes: int = 0
    source: str = ""


@dataclass(frozen=True)
class Subscription:
    """One plan the user pays for, as configured."""

    plan: str
    seats: int = 1
    covers: tuple[str, ...] = ()
    attribution: str = "time"


@dataclass(frozen=True)
class Staffing:
    """One staffing option of a vendor profile."""

    rate: float
    time_factor: float
    senior_review_pct: float


@dataclass(frozen=True)
class VendorProfile:
    """How an outside firm would quote (DESIGN.md, references/vendor-pricing.md)."""

    currency: str
    description: str
    bands_hours: Mapping[Size, tuple[float, float]]
    integration_pct: float
    staffing: Mapping[str, Staffing]
    package_min_hours: float
    package_round_hours: float


@dataclass(frozen=True)
class GithubSettings:
    """Where the GitHub amounts come from (``bill``, ADR-0007) and how ``--github`` Actions minutes are priced without it."""

    bill: BillAccount | None = None
    actions_plan_exhausted: bool = False
    actions_runner: str = "linux"
    retired: tuple[
        str, ...
    ] = ()  # keys the config still sets that are no longer read (said in the report header)


@dataclass(frozen=True)
class Budgets:
    """Thresholds ``monitor`` checks."""

    daily_usd: float
    monthly_usd: float
    per_provider_daily_usd: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Sizing:
    """Thresholds for automatic work-item sizing (ascending, one per band below XL)."""

    packet_bytes: tuple[int, int, int, int]
    loc: tuple[int, int, int, int]


@dataclass(frozen=True)
class Config:
    """Everything the user can change without touching prices."""

    subscriptions: tuple[Subscription, ...]
    cache_ttl_default: str
    openai_default_model: str
    xai_trust_cli_cost: bool
    github: GithubSettings
    budgets: Budgets
    sizing: Sizing
    vendor_default_profile: str
    vendor_profiles: Mapping[str, VendorProfile]
    hours_per_day: float
    window_default_hours: float
    reconcile_tolerance_pct: (
        float  # ``reconcile``: a gap above it between the local count and the provider's figure is a failure
    )
    plugins: tuple[str, ...] = ()  # module names loaded besides the entry points (ADR-0004)
    usage_logs: tuple[str, ...] = ()  # usage-log files read besides the default one (ADR-0005)
    outside_scope_clients: tuple[
        str, ...
    ] = ()  # clients whose sessions are outside the tracked work (UsageRow.client)
    plugin_settings: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )  # plugin_settings.<plugin name>
    billing_rules: Mapping[str, str] = field(
        default_factory=dict
    )  # every providers.<name>.billing: the rule for that provider's rows without evidence of their own

    @property
    def anthropic_billing(self) -> str:
        """``providers.anthropic.billing``: api | subscription | "" (mixed) — read from ``billing_rules``, the one place."""
        return self.billing_rules.get("anthropic", "")

    @property
    def openai_billing(self) -> str:
        """``providers.openai.billing`` for Codex sessions without plan evidence: api | subscription | "" (mixed)."""
        return self.billing_rules.get("openai", "")

    @property
    def google_billing(self) -> str:
        """``providers.google.billing`` for Gemini CLI rows: api | subscription | "" (mixed)."""
        return self.billing_rules.get("google", "")

    @property
    def xai_billing(self) -> str:
        """``providers.xai.billing`` for Grok Build rows (the session files say nothing): api | subscription | ""."""
        return self.billing_rules.get("xai", "")


@dataclass(frozen=True)
class PriceBook:
    """Every price the tool can apply, typed by shape."""

    checked_at: date
    auto_check_days: int
    sources: Mapping[Provider, str]
    models: Mapping[Provider, Mapping[str, PriceEntry]]
    github: PerUnitPrice
    plans: Mapping[str, Plan]
    peak_hours_utc: tuple[tuple[int, int], ...]
    raw: JsonDict = field(default_factory=dict, compare=False, repr=False)
    retired: tuple[
        str, ...
    ] = ()  # keys of the user's prices file that are no longer read (said in the header)

    def entry(self, provider: Provider, model: str) -> PriceEntry | None:
        """The price entry for a model; a dated id (``claude-opus-5-20261001``) resolves to its prefix."""
        table = self.models.get(provider, {})
        if model in table:
            return table[model]
        for name in sorted(table, key=len, reverse=True):
            if model.startswith(name):
                return table[name]
        return None


# ---- loading ---------------------------------------------------------------------------------------------------


def _read_json(path: Path) -> JsonDict:
    if not path.exists():
        return {}
    try:
        with path.open() as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object at the top level")
    return data


def _package_json(name: str) -> JsonDict:
    text = resources.files("ai_cost").joinpath("data").joinpath(name).read_text()
    data = json.loads(text)
    assert isinstance(data, dict)
    return data


def deep_merge(base: JsonDict, over: JsonDict) -> JsonDict:
    """Recursive dict merge; lists replace."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _data_home(environ: Mapping[str, str], home: Path) -> Path:
    """``$XDG_DATA_HOME`` (a ``~`` in it is the user's home), else ``~/.local/share`` — on every platform."""
    if environ.get("XDG_DATA_HOME"):
        return Path(environ["XDG_DATA_HOME"]).expanduser()
    return home / ".local" / "share"


def usage_log_path(environ: Mapping[str, str], home: Path) -> Path:
    """``AI_COST_USAGE_LOG``, else ``$XDG_DATA_HOME/ai-cost/usage.jsonl`` (``~/.local/share`` by default)."""
    if environ.get("AI_COST_USAGE_LOG"):
        return Path(environ["AI_COST_USAGE_LOG"]).expanduser()
    return _data_home(environ, home) / "ai-cost" / "usage.jsonl"


def reports_dir_path(environ: Mapping[str, str], home: Path) -> Path:
    """``AI_COST_REPORTS_DIR``, else ``$XDG_DATA_HOME/ai-cost/reports`` — where ``daily`` writes its files."""
    if environ.get("AI_COST_REPORTS_DIR"):
        return Path(environ["AI_COST_REPORTS_DIR"]).expanduser()
    return _data_home(environ, home) / "ai-cost" / "reports"


def load_config(paths: Paths) -> Config:
    """Built-in defaults overridden by the user's config file (``subscriptions`` replaces, dicts merge).

    A null anywhere in the user file is "no override": the shipped value stays, as in the prices file.
    """
    user = _without_nulls(_read_json(paths.user_config_file())) or {}
    raw = deep_merge(_package_json("config.json"), user)
    return parse_config(raw, str(paths.user_config_file()))


def load_pricebook(paths: Paths) -> PriceBook:
    """Built-in registry overridden by the user's prices file, validated shape by shape."""
    base = _package_json("prices.json")
    return parse_pricebook(base, _read_json(paths.user_prices_file()), str(paths.user_prices_file()))


def builtin_prices() -> JsonDict:
    """The registry as shipped (``prices show --snapshot``)."""
    return _package_json("prices.json")


def builtin_config() -> JsonDict:
    """The defaults as shipped (``install --init-config``)."""
    return _package_json("config.json")


# ---- parsing: config ------------------------------------------------------------------------------------------


def _finite(value: Any, where: str) -> float:
    """One JSON number as a finite float.

    A boolean, a non-number, an infinity or an integer too large for a float is a ``ConfigError`` naming the path.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ConfigError(f"{where} must be finite, got {value!r}") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{where} must be finite, got {value!r}")
    return number


def _number(raw: Mapping[str, Any], key: str, where: str, default: float | None = None) -> float:
    """``raw[key]`` as a finite float; a missing required key or anything but a JSON number is a ``ConfigError``.

    One rule with ``_finite``/``_integer``: a numeric string is not a number in a config file.
    """
    value = raw.get(key)
    if value is None:  # absent or an explicit null: the default, as for every other shape
        value = default
    if value is None:
        raise ConfigError(f"{where}: {key} is required")
    return _finite(value, f"{where}: {key}")


def _whole(value: Any, where: str) -> int:
    """One JSON number as an exact int; a fraction is a ``ConfigError`` naming the path — nothing is truncated."""
    number = _finite(value, where)
    if not number.is_integer():
        raise ConfigError(f"{where} must be a whole number, got {value!r}")
    return value if isinstance(value, int) else int(number)


def _integer(raw: Mapping[str, Any], key: str, where: str, default: int | None = None) -> int:
    """``raw[key]`` as an exact non-negative int.

    Missing when required, a non-number, a fraction or a negative is a ``ConfigError``: every integer setting is a
    count (seats, units, minutes, days, thresholds).
    """
    value = raw.get(key)
    if value is None:  # absent or an explicit null: the default, as for every other shape
        value = default
    if value is None:
        raise ConfigError(f"{where}: {key} is required")
    whole = _whole(value, f"{where}: {key}")
    if whole < 0:
        raise ConfigError(f"{where}: {key} must be non-negative, got {whole}")
    return whole


MAX_WINDOW_HOURS = 24 * 366 * 100  # a century: keeps every window arithmetic inside datetime's range
MAX_CHECK_DAYS = 366 * 100
_N = TypeVar("_N", int, float)


def _capped(value: _N, limit: _N, where: str) -> _N:
    """A finite setting that later feeds ``timedelta``; above ``limit`` it is a ``ConfigError``, not an overflow."""
    if value > limit:
        raise ConfigError(f"{where} must be at most {limit}, got {value}")
    return value


def _positive(raw: Mapping[str, Any], key: str, where: str, default: float) -> float:
    value = _number(raw, key, where, default)
    if value <= 0:
        raise ConfigError(f"{where}: {key} must be > 0, got {value}")
    return value


def _date(raw: Mapping[str, Any], key: str, where: str) -> date | None:
    """``YYYY-MM-DD`` or none; an empty string clears a shipped date (a null would be "no override")."""
    value = raw.get(key)
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{where}: {key} must be YYYY-MM-DD, got {value!r}") from exc


def _flag(raw: Mapping[str, Any], key: str, where: str, default: bool) -> bool:
    """A JSON boolean (absent or null = default); a quoted "false" or any other value is a ``ConfigError``."""
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be true or false, got {value!r}")
    return value


def _nonnegative(raw: Mapping[str, Any], key: str, where: str, default: float = 0) -> float:
    value = _number(raw, key, where, default)
    if value < 0:
        raise ConfigError(f"{where}: {key} must be >= 0, got {value}")
    return value


def _text_or(raw: Mapping[str, Any], key: str, default: str, where: str) -> str:
    """``raw[key]`` as text, or ``default`` when absent OR null (a null in a user file is "no override").

    Any other non-string is a ``ConfigError``: a number where a currency or a model name belongs is a typo, never a name.
    """
    value = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be a string, got {type(value).__name__}")
    return value


def _optional_text(raw: Mapping[str, Any], key: str, where: str) -> str:
    """A string or nothing (absent / null / empty = ""); any other JSON value is a ``ConfigError``."""
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key} must be a string, got {type(value).__name__}")
    return value


def _text(raw: Mapping[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: {key} is required (a non-empty string)")
    return value


def _staffing(raw: Mapping[str, Any], where: str) -> Staffing:
    return Staffing(
        rate=_number(raw, "rate", where),
        time_factor=_number(raw, "time_factor", where, 1.0),
        senior_review_pct=_number(raw, "senior_review_pct", where, 0),
    )


def _profile(raw: Mapping[str, Any], where: str) -> VendorProfile:
    bands = {}
    for name, pair in _section(raw, "bands_hours", where).items():
        try:
            if isinstance(pair, str) or len(pair) != 2:
                raise TypeError(pair)
            bands[Size(name)] = (
                _finite(pair[0], f"{where}: bands_hours.{name}"),
                _finite(pair[1], f"{where}: bands_hours.{name}"),
            )
        except (ValueError, IndexError, TypeError, KeyError) as exc:
            raise ConfigError(f"{where}: bands_hours.{name} must be [min, max] for XS..XL") from exc
    missing = [size.value for size in Size if size not in bands]
    if missing:
        raise ConfigError(f"{where}: bands_hours must define every band, missing {missing}")
    package = _section(raw, "package", where)
    staffing = _section(raw, "staffing", where)
    if not staffing:
        raise ConfigError(f"{where}: staffing must define at least one option (junior / mid / senior ...)")
    return VendorProfile(
        currency=_text_or(raw, "currency", "USD", where),
        description=_text_or(raw, "description", "", where),
        bands_hours=bands,
        integration_pct=_number(raw, "integration_pct", where, 0),
        staffing={
            name: _staffing(_section(staffing, name, f"{where}.staffing"), f"{where}.staffing.{name}")
            for name in staffing
        },
        package_min_hours=_number(package, "min_hours", f"{where}.package", 0),
        package_round_hours=_number(package, "round_to_hours", f"{where}.package", 0),
    )


def _thresholds(raw: Mapping[str, Any], key: str, where: str) -> tuple[int, int, int, int]:
    """XS, S, M, L thresholds as non-negative integers — a size band below zero is no band (like every other count)."""
    bands = ("XS", "S", "M", "L")
    if any(band not in raw for band in bands):
        raise ConfigError(f"{where}: sizing.{key} needs integer XS, S, M, L thresholds")
    xs, small, medium, large = (_integer(raw, band, f"{where}: sizing.{key}") for band in bands)
    return xs, small, medium, large


def _section(raw: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any]:
    """``raw[key]`` as an object (absent = empty); anything else is a ``ConfigError`` naming the path."""
    try:
        return as_object(raw.get(key), key)
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _entries(raw: Mapping[str, Any], key: str, where: str) -> list[Mapping[str, Any]]:
    value = raw.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ConfigError(f"{where}: {key} must be a list of objects")
    return value


def _subscriptions(raw: Mapping[str, Any], where: str) -> tuple[Subscription, ...]:
    subs = []
    for entry in _entries(raw, "subscriptions", where):
        attribution = _text_or(entry, "attribution", "time", f"{where}: subscriptions")
        if attribution not in ("time", "full", "none"):
            raise ConfigError(
                f"{where}: subscriptions[{entry.get('plan')}].attribution must be time, full or none"
            )
        subs.append(
            Subscription(
                plan=_text(entry, "plan", f"{where}: subscriptions"),
                seats=_integer(entry, "seats", f"{where}: subscriptions", 1),
                covers=_names(entry, "covers", f"{where}: subscriptions"),
                attribution=attribution,
            )
        )
    return tuple(subs)


def _vendor_settings(raw: Mapping[str, Any], where: str) -> tuple[str, dict[str, VendorProfile], float]:
    """``(default profile, profiles, hours per day)``; the default must be one of the profiles."""
    vendor = _section(raw, "vendor", where)
    profiles_raw = _section(vendor, "profiles", f"{where}: vendor")
    profiles = {
        name: _profile(
            _section(profiles_raw, name, f"{where}: vendor.profiles"), f"{where}: vendor.profiles.{name}"
        )
        for name in profiles_raw
    }
    default_profile = _text_or(vendor, "default_profile", "", f"{where}: vendor")
    if default_profile not in profiles:
        raise ConfigError(
            f"{where}: vendor.default_profile {default_profile!r} is not one of {sorted(profiles)}"
        )
    return default_profile, profiles, _positive(vendor, "hours_per_day", f"{where}: vendor", 6)


def _budgets(raw: Mapping[str, Any], where: str) -> Budgets:
    budgets = _section(raw, "budgets", where)
    per_provider = _section(budgets, "per_provider_daily_usd", f"{where}: budgets")
    return Budgets(
        daily_usd=_nonnegative(budgets, "daily_usd", f"{where}: budgets"),
        monthly_usd=_nonnegative(budgets, "monthly_usd", f"{where}: budgets"),
        per_provider_daily_usd={
            k: _nonnegative(per_provider, k, f"{where}: budgets.per_provider_daily_usd") for k in per_provider
        },
    )


def _sizing(raw: Mapping[str, Any], where: str) -> Sizing:
    sizing = _section(raw, "sizing", where)
    return Sizing(
        packet_bytes=_thresholds(_section(sizing, "packet_bytes", f"{where}: sizing"), "packet_bytes", where),
        loc=_thresholds(_section(sizing, "loc", f"{where}: sizing"), "loc", where),
    )


RETIRED_GITHUB_KEYS = (
    "copilot_plan_exhausted",
)  # a Copilot review has no price since ai-cost 2.6 (ADR-0007)
_ACCOUNT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _bill(github: Mapping[str, Any], where: str) -> BillAccount | None:
    """``providers.github.bill``: ``{"scope": organization|user, "name": …}``; absent or null = none."""
    raw = _section(github, "bill", where)
    if not raw:
        return None
    scope = _text(raw, "scope", f"{where}.bill")
    if scope == "enterprise":
        raise ConfigError(
            f"{where}.bill: an enterprise report leaves out the usage assigned to cost centers — name the "
            'organization instead ("scope": "organization")'
        )
    try:
        kind = BillScope(scope)
    except ValueError:
        raise ConfigError(f"{where}.bill: scope must be organization or user, not {scope!r}") from None
    name = _text(raw, "name", f"{where}.bill")
    if not _ACCOUNT_NAME.fullmatch(name):
        raise ConfigError(f"{where}.bill: name {name!r} is not a GitHub account name")
    return BillAccount(kind, name)


def _github_settings(providers: Mapping[str, Any], where: str) -> GithubSettings:
    github = _section(providers, "github", f"{where}: providers")
    return GithubSettings(
        bill=_bill(github, f"{where}: providers.github"),
        actions_plan_exhausted=_flag(github, "actions_plan_exhausted", f"{where}: providers.github", False),
        actions_runner=_text_or(github, "actions_runner", "linux", f"{where}: providers.github"),
        retired=tuple(f"providers.github.{key}" for key in RETIRED_GITHUB_KEYS if key in github),
    )


def parse_config(raw: JsonDict, where: str) -> Config:
    """Validate the merged JSON into a ``Config``; every problem names the offending key."""
    providers = _section(raw, "providers", where)
    anthropic = _section(providers, "anthropic", f"{where}: providers")
    default_profile, profiles, hours_per_day = _vendor_settings(raw, where)
    return Config(
        subscriptions=_subscriptions(raw, where),
        cache_ttl_default=_text_or(anthropic, "cache_ttl_default", "1h", f"{where}: providers.anthropic"),
        openai_default_model=_text_or(
            _section(providers, "openai", f"{where}: providers"),
            "default_model",
            "",  # the public core ships no default model (ADR-0004): a rollout without one is "unknown"
            f"{where}: providers.openai",
        ),
        xai_trust_cli_cost=_flag(
            _section(providers, "xai", f"{where}: providers"),
            "trust_cli_cost",
            f"{where}: providers.xai",
            True,
        ),
        github=_github_settings(providers, where),
        budgets=_budgets(raw, where),
        sizing=_sizing(raw, where),
        vendor_default_profile=default_profile,
        vendor_profiles=profiles,
        hours_per_day=hours_per_day,
        window_default_hours=_capped(
            _positive(raw, "window_default_hours", where, 24),
            MAX_WINDOW_HOURS,
            f"{where}: window_default_hours",
        ),
        reconcile_tolerance_pct=_nonnegative(
            _section(raw, "reconcile", where), "tolerance_pct", f"{where}: reconcile", 5
        ),
        plugins=_names(raw, "plugins", where),
        usage_logs=_names(raw, "usage_logs", where),
        outside_scope_clients=_clients(raw, where),
        plugin_settings=_plugin_settings(raw, where),
        billing_rules=_billing_rules(providers, where),
    )


# ---- parsing: prices ------------------------------------------------------------------------------------------


def _token_tier(raw: Mapping[str, Any], where: str) -> TokenTierPrice:
    unknown = set(raw) - _TOKEN_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown price keys {sorted(unknown)} for a token-tier model")
    if "input" not in raw or "output" not in raw:
        raise ConfigError(f"{where}: a token-tier price needs input and output")
    long_raw = raw.get("long")
    long_entry = None
    threshold = 0
    for key, block in (("long", long_raw), ("next", raw.get("next"))):
        if block is not None and not isinstance(block, Mapping):
            raise ConfigError(f"{where}: {key} must be an object with input/output, got {block!r}")
    if long_raw:
        threshold = _integer(long_raw, "threshold", f"{where}.long", 0)
        long_entry = _token_tier({k: v for k, v in long_raw.items() if k != "threshold"}, f"{where}.long")
    next_raw = raw.get("next")
    next_entry = None
    next_from = None
    if next_raw:
        next_from = _date(next_raw, "from", f"{where}.next")
        next_entry = _token_tier({k: v for k, v in next_raw.items() if k != "from"}, f"{where}.next")
    return TokenTierPrice(
        next=next_entry,
        next_from=next_from,
        input=_number(raw, "input", where),
        output=_number(raw, "output", where),
        cached_input=_number(raw, "cached_input", where, 0),
        cache_read=_number(raw, "cache_read", where, 0),
        cache_write_5m=_number(raw, "cache_write_5m", where, 0),
        cache_write_1h=_number(raw, "cache_write_1h", where, 0),
        long_threshold=threshold,
        long=long_entry,
        aliases=_names(raw, "aliases", where),
        valid_until=_date(raw, "valid_until", where),
    )


def _plugin_settings(raw: Mapping[str, Any], where: str) -> Mapping[str, Mapping[str, Any]]:
    """``plugin_settings``: one object per plugin name; a non-object section is a ``ConfigError`` naming it."""
    sections = _section(raw, "plugin_settings", where)
    return {str(name): _section(sections, str(name), f"{where}: plugin_settings") for name in sections}


def _billing_rules(providers: Mapping[str, Any], where: str) -> Mapping[str, str]:
    """Every ``providers.<name>.billing``, each checked: a typo under any provider is a ConfigError, never ignored."""
    for name, section in providers.items():
        if not isinstance(section, Mapping):  # "deepseek": "api" is a mistake, not a rule
            raise ConfigError(f"{where}: providers.{name} must be an object, got {type(section).__name__}")
    return {
        name: _billing_choice(section, f"{where}: providers.{name}")
        for name, section in providers.items()
        if "billing" in section
    }


def _billing_choice(raw: Mapping[str, Any], where: str) -> str:
    """``billing``: ``api`` or ``subscription`` as the rule for rows without their own evidence.

    ``mixed`` (or absent) means there is no single rule: a row keeps what its source found, else ``UNKNOWN``.
    """
    value = _optional_text(raw, "billing", where)
    if value == "mixed":
        return ""
    if value not in ("", "api", "subscription"):
        raise ConfigError(f"{where}: billing must be api, subscription or mixed, got {value!r}")
    return value


def _clients(raw: Mapping[str, Any], where: str) -> tuple[str, ...]:
    """``outside_scope_clients``: names exactly as a row's ``client`` spells them.

    An empty or space-padded name could never match, so it is a ``ConfigError``, never trimmed in silence.
    """
    names = _names(raw, "outside_scope_clients", where)
    bad = [name for name in names if not name or name != name.strip()]
    if bad:
        raise ConfigError(
            f"{where}: outside_scope_clients holds {bad!r} — a client is named exactly as the rollout's originator, "
            "e.g. codex_work_desktop"
        )
    return names


def _names(raw: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    """A list of strings (absent or null = none); anything else is a ``ConfigError``."""
    value = raw.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: {key} must be a list of strings")
    return tuple(value)


def _hour_pairs(raw: Mapping[str, Any], key: str, where: str) -> tuple[tuple[int, int], ...]:
    """``[[from, to], ...]`` UTC hour ranges (absent or null = none); anything else is a ``ConfigError``."""
    value = raw.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{where}: {key} must be a list of [from_hour, to_hour] pairs")
    try:
        pairs = []
        for pair in value:
            if isinstance(pair, str) or len(pair) != 2:
                raise TypeError(pair)
            pairs.append((_whole(pair[0], f"{where}: {key}"), _whole(pair[1], f"{where}: {key}")))
        return tuple(pairs)
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        raise ConfigError(f"{where}: {key} must be a list of [from_hour, to_hour] pairs") from exc


def _rate(raw: Mapping[str, Any], where: str) -> Rate:
    missing = _RATE_KEYS - set(raw)
    if missing:
        raise ConfigError(f"{where}: rate needs {sorted(missing)}")
    return Rate(
        cache_hit=_number(raw, "cache_hit", where),
        cache_miss=_number(raw, "cache_miss", where),
        output=_number(raw, "output", where),
    )


def _peak_offpeak(raw: Mapping[str, Any], where: str) -> PeakOffpeakPrice:
    unknown = set(raw) - _PEAK_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown price keys {sorted(unknown)} for a peak/off-peak model")
    return PeakOffpeakPrice(
        peak=_rate(_section(raw, "peak", where), f"{where}.peak"),
        offpeak=_rate(_section(raw, "offpeak", where), f"{where}.offpeak"),
        display=_text_or(raw, "display", "", where),
    )


_CLEARABLE_BLOCKS = ("next", "long")


def _apply_clears(merged: JsonDict, over: Mapping[str, Any]) -> JsonDict:
    """A model override clears a shipped block with an empty value: ``"next": {}``, ``"long": {}``, ``"valid_until": ""``.

    Used by the parser and by the printed registry alike, so both read one rule (a null is "no override").
    """
    for key in _CLEARABLE_BLOCKS:
        if over.get(key) == {}:
            merged.pop(key, None)
    if over.get("valid_until") == "":
        merged.pop("valid_until", None)
    return merged


def _model_entry(base: JsonDict | None, over: JsonDict | None, where: str) -> PriceEntry:
    """Parse one model: the built-in entry decides the shape; an override must keep it (ADR-0002)."""
    for label, entry in (("built-in", base), ("override", over)):
        if entry is not None and not isinstance(entry, Mapping):
            raise ConfigError(f"{where}: the {label} entry must be an object, got {type(entry).__name__}")
    shape_src = base if base is not None else (over or {})
    is_peak = "peak" in shape_src
    if base is not None and over is not None:
        if is_peak and set(over) & _TOKEN_KEYS:
            raise ConfigError(f"{where}: override uses token-tier keys on a peak/off-peak model")
        if not is_peak and set(over) & {"peak", "offpeak"}:
            raise ConfigError(f"{where}: override uses peak/off-peak keys on a token-tier model")
    merged = _apply_clears(deep_merge(base or {}, over or {}), over or {})
    return _peak_offpeak(merged, where) if is_peak else _token_tier(merged, where)


def _plans(raw: Mapping[str, Any], where: str = "prices: plans") -> dict[str, Plan]:
    """One ``Plan`` per named object; a null entry (a user-file override) removes the plan instead of zeroing it."""
    plans = {}
    for name in raw:
        if raw[name] is None:
            continue
        entry = _section(raw, name, where)
        plans[name] = Plan(
            name=name,
            provider=_text_or(entry, "provider", "", f"{where}.{name}"),
            monthly_usd=_number(entry, "monthly_usd", f"{where}.{name}", 0),
            per_seat=_flag(entry, "per_seat", f"{where}.{name}", False),
            allowance_units=_integer(entry, "allowance_units", f"{where}.{name}", 0),
            actions_minutes=_integer(entry, "actions_minutes", f"{where}.{name}", 0),
            source=_text_or(entry, "source", "", f"{where}.{name}"),
        )
    return plans


def _github(raw: Mapping[str, Any], where: str = "prices: github") -> PerUnitPrice:
    actions = _section(raw, "actions", where)
    minutes = _section(actions, "usd_per_minute", f"{where}.actions")
    return PerUnitPrice(
        usd_per_minute={k: _number(minutes, k, f"{where}.actions.usd_per_minute") for k in minutes},
        public_repos_free=_flag(actions, "public_repos_free", f"{where}.actions", True),
        web_search_per_1000=_number(raw, "web_search_per_1000", where, 0),
    )


def _merged_provider(
    base_providers: Mapping[str, Any], over_providers: Mapping[str, Any], name: str, where: str
) -> JsonDict:
    """One provider's built-in section overlaid with the user's; each must be an object (or null) when present."""
    return deep_merge(
        dict(_section(base_providers, name, f"{where}: providers")),
        dict(_section(over_providers, name, f"{where} (user file): providers")),
    )


def _provider_models(
    b_prov: Mapping[str, Any], o_prov: Mapping[str, Any], provider: str, where: str
) -> dict[str, PriceEntry]:
    b_models = _section(b_prov, "models", f"{where}: providers.{provider}")
    o_models = _section(o_prov, "models", f"{where} (user file): providers.{provider}")
    return {
        name: _model_entry(
            b_models.get(name), o_models.get(name), f"{where}: providers.{provider}.models.{name}"
        )
        for name in sorted(set(b_models) | set(o_models))
    }


def _without_nulls(value: Any) -> Any:
    """The override without its null entries at any depth: a null means "no override", as the parser reads it.

    An object that held only nulls is no override at all (``None``, dropped by its parent); a literal empty object
    is kept — it clears a shipped model block (``_apply_clears``: ``next``, ``long``; ``valid_until: ""``). Lists are
    left as written: a list replaces the shipped list whole, so a null inside one stands for no shipped item — the
    parser of that key reports it as the error it is.
    """
    if not isinstance(value, dict):
        return value
    if not value:
        return {}
    cleaned: dict[str, Any] = {}
    for key, entry in value.items():
        kept = None if entry is None else _without_nulls(entry)
        if kept is not None:
            cleaned[key] = kept
    return cleaned or None


def _effective_registry(base: JsonDict, over: JsonDict, removed_plans: set[str]) -> JsonDict:
    """The merged registry as the parser reads it, so ``prices show --format json`` prints what the book prices with.

    ``over`` is already null-stripped (a null anywhere is absent, the base entry stays); a plan the user file set to
    null is removed here as it is from the book; a model block the user file cleared is cleared here too.
    """
    merged = deep_merge(base, over)
    if removed_plans and isinstance(merged.get("plans"), dict):
        merged["plans"] = {k: v for k, v in merged["plans"].items() if k not in removed_plans}
    for provider, models in object_at(over, "providers").items():
        for model, entry in object_at(models, "models").items() if isinstance(models, Mapping) else ():
            target = object_at(merged, "providers", str(provider), "models", str(model))
            if isinstance(entry, Mapping) and target:
                _apply_clears(target if isinstance(target, dict) else dict(target), entry)
    return merged


def _registry(base: Mapping[str, Any], over: Mapping[str, Any], where: str) -> list[Provider]:
    """The pricebook IS the provider registry: every provider the shipped file or the user file lists, in order."""
    providers: list[Provider] = []
    for name in (*base, *(name for name in over if name not in base)):
        try:
            providers.append(Provider.of(str(name)))
        except ValueError as exc:
            raise ConfigError(f"{where}: providers.{name}: {exc}") from exc
    return providers


def parse_pricebook(base: JsonDict, over: JsonDict, where: str) -> PriceBook:
    """Merge and validate the registry; the built-in entry of each model decides its shape."""
    removed_plans = {
        name for name, entry in _section(over, "plans", f"{where} (user file)").items() if entry is None
    }
    over = (
        _without_nulls(over) or {}
    )  # a null anywhere in the user file is "no override": parser and printout
    models: dict[Provider, dict[str, PriceEntry]] = {}
    sources: dict[Provider, str] = {}
    base_providers = _section(base, "providers", where)
    over_providers = _section(over, "providers", f"{where} (user file)")
    for provider in _registry(base_providers, over_providers, where):
        b_prov = _section(base_providers, provider.value, f"{where}: providers")
        o_prov = _section(over_providers, provider.value, f"{where} (user file): providers")
        shipped = _text_or(b_prov, "source", "", f"{where}: providers.{provider.value}")
        sources[provider] = _text_or(
            o_prov, "source", shipped, f"{where} (user file): providers.{provider.value}"
        )
        models[provider] = _provider_models(b_prov, o_prov, provider.value, where)
    github_raw = _merged_provider(base_providers, over_providers, "github", where)
    anthropic = _merged_provider(base_providers, over_providers, "anthropic", where)
    tools = _section(anthropic, "tools", f"{where}: providers.anthropic")
    github_raw["web_search_per_1000"] = tools.get("web_search_per_1000", 0)
    deepseek = _merged_provider(base_providers, over_providers, "deepseek", where)
    merged = _effective_registry(base, over, removed_plans)
    github_over = _section(over_providers, "github", f"{where} (user file): providers")
    return PriceBook(
        checked_at=_date(merged, "checked_at", where) or date(2000, 1, 1),
        auto_check_days=_capped(
            _integer(merged, "auto_check_days", where, 7), MAX_CHECK_DAYS, f"{where}: auto_check_days"
        ),
        sources=sources,
        models=models,
        github=_github(github_raw),
        plans=_plans(_section(merged, "plans", where), f"{where}: plans"),
        peak_hours_utc=_hour_pairs(deepseek, "peak_hours_utc", f"{where}: providers.deepseek"),
        raw=merged,
        retired=("providers.github.copilot",) if "copilot" in github_over else (),
    )
